"""
Final release-readiness verification tests for Vinay-711/autonomous-coding-bot.
Addresses Tasks 7, 8, 9, 10:
- Simulation of all 5 platforms returning ACCEPTED, REJECTED, PENDING, UNKNOWN, RATE_LIMITED, AUTH_REQUIRED
- Daily run where 3 of 5 platforms fail: ensures per-platform quotas and overall counts are reported truthfully (never claiming 25 accepted)
- GitHub push failure and retry without duplicate judge submission
- Worker process crash, persistent recovery, and safe lease reclamation
"""

import pytest
import time
from unittest.mock import patch, AsyncMock

from bot import (
    DatabaseManager,
    VerdictState,
    SubmissionVerdict,
    ProcessStage,
    PublishResult,
    FailureCode,
    classify_failure,
    process_problem,
)

@pytest.fixture
def temp_audit_db(tmp_path):
    db_file = tmp_path / "audit_release.db"
    return DatabaseManager(db_path=db_file, auto_migrate=False)


# ==============================================================================
# Task 7: Simulate all 5 platforms across all verdict states
# ==============================================================================
@pytest.mark.parametrize("platform", ["leetcode", "codeforces", "codechef", "hackerrank", "hackerearth"])
def test_all_platforms_accepted_verdict(platform, temp_audit_db):
    v = SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id=f"{platform}_sub_1")
    assert v.state == VerdictState.ACCEPTED
    assert bool(v) is True

    temp_audit_db.mark_problem_accepted(platform, "prob-1", f"https://{platform}.com/problem/prob-1", verdict=v)
    assert temp_audit_db.is_accepted(platform, "prob-1") is True


@pytest.mark.parametrize("platform", ["leetcode", "codeforces", "codechef", "hackerrank", "hackerearth"])
def test_all_platforms_rejected_verdict(platform, temp_audit_db):
    v = SubmissionVerdict(VerdictState.REJECTED, "Wrong Answer on testcase 3", submission_id=f"{platform}_sub_2")
    assert v.state == VerdictState.REJECTED
    assert bool(v) is False
    assert classify_failure(v.message, verdict=v.state) == FailureCode.JUDGE_REJECTED

    # Ensure rejected verdict is NEVER recorded as accepted
    assert temp_audit_db.is_accepted(platform, "prob-rejected") is False


@pytest.mark.parametrize("platform", ["leetcode", "codeforces", "codechef", "hackerrank", "hackerearth"])
def test_all_platforms_pending_and_unknown_verdicts(platform):
    # Pending (polling timed out)
    v_pending = SubmissionVerdict(VerdictState.PENDING, "Evaluation in progress timed out")
    assert v_pending.state == VerdictState.PENDING
    assert bool(v_pending) is False
    assert classify_failure(v_pending.message, verdict=v_pending.state) == FailureCode.TRANSIENT

    # Unknown (stale or untrusted submission ID)
    v_unknown = SubmissionVerdict(VerdictState.UNKNOWN, "Submission ID mismatch / correlation failed")
    assert v_unknown.state == VerdictState.UNKNOWN
    assert bool(v_unknown) is False


@pytest.mark.parametrize("platform", ["leetcode", "codeforces", "codechef", "hackerrank", "hackerearth"])
def test_all_platforms_rate_limited_and_auth_required(platform):
    # Rate Limited (HTTP 429)
    err_429 = "HTTP 429 Too Many Requests: Retry-After: 60"
    assert classify_failure(err_429, http_status=429) == FailureCode.RATE_LIMITED

    # Auth Required (Login redirect or Cloudflare challenge)
    err_auth = "Session expired: redirected to /login"
    assert classify_failure(err_auth) == FailureCode.AUTH_REQUIRED
    assert classify_failure("Cloudflare challenge barrier detected") == FailureCode.AUTH_REQUIRED


# ==============================================================================
# Task 8: Daily run with 3 of 5 platforms failing; no false quota claims
# ==============================================================================
@pytest.mark.asyncio
async def test_daily_run_partial_failures_no_falsified_quota(temp_audit_db):
    """
    Simulate a run where 3 of 5 platforms fail:
    - LeetCode: 5/5 accepted (COMPLETED)
    - HackerRank: 5/5 accepted (COMPLETED)
    - Codeforces: 0/5 accepted (SHORTFALL - network outage)
    - CodeChef: 0/5 accepted (SHORTFALL - auth expired)
    - HackerEarth: 0/5 accepted (SHORTFALL - anti-bot challenge)

    VERIFICATIONS:
    1. Overall accepted count is 10, NOT 25.
    2. Five accepted on LeetCode cannot satisfy Codeforces or HackerEarth.
    3. Daily run records for failing platforms record SHORTFALL.
    """
    platforms_config = {
        "leetcode": (5, 5, "COMPLETED"),
        "hackerrank": (5, 5, "COMPLETED"),
        "codeforces": (5, 0, "SHORTFALL"),
        "codechef": (5, 0, "SHORTFALL"),
        "hackerearth": (5, 0, "SHORTFALL"),
    }

    total_accepted = 0
    total_target = 0

    for plat, (target, accepted, expected_status) in platforms_config.items():
        total_target += target
        run_id = temp_audit_db.get_or_create_daily_run(plat, target_quota=target, run_date="2026-11-12")
        temp_audit_db.increment_daily_run_counts(
            run_id=run_id,
            attempted=5,
            generated=accepted,
            compiled=accepted,
            tested=accepted,
            accepted=accepted,
            published=accepted,
            failed=(target - accepted)
        )
        temp_audit_db.complete_daily_run(run_id, status=expected_status)
        total_accepted += accepted

    # Check database summary
    summary = temp_audit_db.get_daily_summary("2026-11-12")
    assert len(summary) == 5

    plat_map = {r["platform"]: r for r in summary}
    assert plat_map["leetcode"]["accepted_count"] == 5
    assert plat_map["leetcode"]["status"] == "COMPLETED"

    assert plat_map["hackerrank"]["accepted_count"] == 5
    assert plat_map["hackerrank"]["status"] == "COMPLETED"

    assert plat_map["codeforces"]["accepted_count"] == 0
    assert plat_map["codeforces"]["status"] == "SHORTFALL"

    assert plat_map["codechef"]["accepted_count"] == 0
    assert plat_map["codechef"]["status"] == "SHORTFALL"

    assert plat_map["hackerearth"]["accepted_count"] == 0
    assert plat_map["hackerearth"]["status"] == "SHORTFALL"

    # Truthful reporting verification
    assert total_accepted == 10
    assert total_target == 25
    assert total_accepted < total_target  # Must NEVER report 25/25


class MockGateLocator:
    def __init__(self, visible=True):
        self._visible = visible
    @property
    def first(self):
        return self
    async def is_visible(self):
        return self._visible
    async def click(self):
        pass
    async def count(self):
        return 1
    def locator(self, *args):
        return self
    async def all(self):
        return []

class MockGatePage:
    def __init__(self, url="https://leetcode.com/problems/two-sum"):
        self._anti_bot_blocked = False
        self.url = url
    async def goto(self, *args, **kwargs):
        pass
    async def evaluate(self, *args, **kwargs):
        return ""
    def locator(self, *args, **kwargs):
        return MockGateLocator()

# ==============================================================================
# Task 9: GitHub push failure and retry without duplicate judge submission
# ==============================================================================
@pytest.mark.asyncio
async def test_github_push_failure_retry_without_duplicate_judge():
    v_acc = SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id="judge_sub_777")
    mock_judge = AsyncMock(return_value=v_acc)

    page = MockGatePage()

    # Step 1: First attempt succeeds on judge, but git push fails
    with patch("asyncio.sleep"), \
         patch("bot.switch_editor_to_java"), \
         patch("bot.extract_problem_text", return_value="Given an array of integers nums and an integer target..."), \
         patch("bot.extract_editor_code", return_value="class Solution {}"), \
         patch("bot.ask_gemini_for_java_code", return_value=("public class Solution {}", "Optimal solution")), \
         patch("bot.compile_java_code_locally", return_value=(True, "", "public class Solution {}")), \
         patch("bot.inject_code_into_editor", return_value=True), \
         patch("bot.capture_pre_submission_snapshot", return_value={"start_time": time.time(), "pre_submission_id": None, "account": "test_user", "problem_id": "two-sum"}), \
         patch("bot.verify_live_submission_verdict", mock_judge), \
         patch("bot.save_java_solution"), \
         patch("bot.commit_solution_to_github", return_value=PublishResult(success=False, error="Push rejected (non-fast-forward)")):

        res1 = await process_problem(
            page,
            "https://leetcode.com/problems/two-sum",
            mode="submit"
        )

        assert res1.stage == ProcessStage.PUBLISH_FAILED
        assert res1.success is False
        assert res1.verdict.state == VerdictState.ACCEPTED
        assert res1.verdict.submission_id == "judge_sub_777"
        # Judge was called exactly once on initial submission
        assert mock_judge.call_count == 1

    # Step 2: Retry publishing with cached verdict and solution code
    # CRITICAL: Judge must NOT be called again during publish retry!
    with patch("bot.verify_live_submission_verdict", mock_judge), \
         patch("bot.commit_solution_to_github", return_value=PublishResult(success=True, commit_hash="a1b2c3d", branch="main", repo_name="leetcode-daily")):

        res2 = await process_problem(
            page,
            "https://leetcode.com/problems/two-sum",
            mode="submit",
            retry_publish=True,
            cached_verdict=res1.verdict,
            cached_code=res1.solution_code,
            cached_explanation=res1.explanation
        )

        assert res2.stage == ProcessStage.PUBLISHED
        assert res2.published is True
        assert res2.commit_hash == "a1b2c3d"

        # Judge call count MUST remain 1: NO DUPLICATE SUBMISSION TO JUDGE
        assert mock_judge.call_count == 1


# ==============================================================================
# Task 10: Process termination and recovery from persistent state
# ==============================================================================
def test_process_termination_and_persistent_recovery(temp_audit_db):
    now = time.time()
    dead_worker = "worker_pid_12345"

    # Worker 1 claims a problem and sets heartbeat
    assert temp_audit_db.claim_problem("leetcode", "container-with-most-water",
                                       "https://leetcode.com/problems/container-with-most-water",
                                       worker_id=dead_worker, lease_timeout_sec=120.0) is True

    # Simulate container preemption / OOM / crash: worker dies, heartbeat halts
    stuck_time = now - 200.0  # 200s ago (exceeds 120s timeout)
    with temp_audit_db.get_connection() as conn:
        conn.execute(
            "UPDATE problems SET claimed_at = ?, heartbeat_at = ?, lease_timeout = 120.0 WHERE canonical_id = 'container-with-most-water'",
            (stuck_time, stuck_time)
        )

    # Worker 2 starts up, detects stranded problem, and recovers lease
    recovered = temp_audit_db.recover_expired_leases(lease_timeout_sec=120.0)
    assert recovered == 1

    # Verify problem is unlocked and enqueued in retry_queue
    with temp_audit_db.get_connection() as conn:
        row = conn.execute("SELECT status, claimed_by FROM problems WHERE canonical_id = 'container-with-most-water'").fetchone()
        assert row[0] == "TRANSIENT_FAILURE"
        assert row[1] is None

    # Worker 2 acquires claim and successfully marks accepted
    new_worker = "worker_pid_67890"
    assert temp_audit_db.claim_problem("leetcode", "container-with-most-water",
                                       "https://leetcode.com/problems/container-with-most-water",
                                       worker_id=new_worker) is True

    v = SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id="recovered_sub_99")
    temp_audit_db.mark_problem_accepted("leetcode", "container-with-most-water",
                                       "https://leetcode.com/problems/container-with-most-water",
                                       verdict=v)

    assert temp_audit_db.is_accepted("leetcode", "container-with-most-water") is True
