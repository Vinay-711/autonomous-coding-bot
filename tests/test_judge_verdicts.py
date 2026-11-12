import json
import time
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from bot import (
    VerdictState,
    SubmissionVerdict,
    ProcessStage,
    ProcessResult,
    PublishResult,
    DatabaseManager,
    SolvedRegistry,
    normalize_problem_identity,
    get_kolkata_now,
    get_kolkata_today_str,
    print_status_summary,
    get_repo_default_branch,
    extract_problem_id,
    verify_live_submission_verdict,
    record_submission_evidence,
    save_java_solution,
    commit_solution_to_github,
    process_problem,
    solve_platform_quota,
    execute_batch,
    SOLUTIONS_DIR,
)

@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Provides a pristine, isolated SQLite database for each test to guarantee no state pollution."""
    test_db_path = tmp_path / "autocoder_test.db"
    test_db = DatabaseManager(db_path=test_db_path, auto_migrate=False)
    old_db = SolvedRegistry._db
    SolvedRegistry.set_db(test_db)
    yield test_db
    SolvedRegistry.set_db(old_db)

class MockLocator:
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

class MockPipelinePage:
    def __init__(self, url="https://leetcode.com/problems/two-sum"):
        self._anti_bot_blocked = False
        self.url = url

    async def goto(self, *args, **kwargs):
        pass

    async def evaluate(self, *args, **kwargs):
        return ""

    def locator(self, *args, **kwargs):
        return MockLocator()


class MockPage:
    """Mock Playwright Page for testing verdict verification with zero browser overhead."""
    def __init__(self, eval_return=None, eval_side_effect=None, url="https://example.com"):
        self.eval_return = eval_return
        self.eval_side_effect = eval_side_effect
        self._anti_bot_blocked = False
        self.url = url

    async def evaluate(self, script, *args, **kwargs):
        if self.eval_side_effect:
            if callable(self.eval_side_effect):
                return self.eval_side_effect(script)
            raise self.eval_side_effect
        if callable(self.eval_return):
            return self.eval_return(script)
        return self.eval_return


# ==============================================================================
# 1. Typed Verdict States & Data Model Tests
# ==============================================================================
def test_verdict_state_enum_and_boolean():
    """Verify typed states and backward-compatible boolean evaluation."""
    assert VerdictState.ACCEPTED == "ACCEPTED"
    assert VerdictState.REJECTED == "REJECTED"
    assert VerdictState.PENDING == "PENDING"
    assert VerdictState.UNKNOWN == "UNKNOWN"
    assert VerdictState.ERROR == "ERROR"

    # Only ACCEPTED evaluates to True in boolean contexts
    assert bool(VerdictState.ACCEPTED) is True
    assert bool(VerdictState.REJECTED) is False
    assert bool(VerdictState.PENDING) is False
    assert bool(VerdictState.UNKNOWN) is False
    assert bool(VerdictState.ERROR) is False


def test_submission_verdict_dataclass_and_unpacking():
    """Verify SubmissionVerdict holds evidence and can be unpacked as (state, message)."""
    v = SubmissionVerdict(
        state=VerdictState.ACCEPTED,
        message="Accepted (Score: 100)",
        submission_id="sub_12345",
        platform="hackerrank",
        problem_id="simple-array-sum",
        evidence={"score": 100},
    )
    assert bool(v) is True
    assert v.submission_id == "sub_12345"
    assert v.evidence["score"] == 100

    # Test tuple unpacking for backward compatibility
    st, msg = v
    assert st == VerdictState.ACCEPTED
    assert msg == "Accepted (Score: 100)"
    assert bool(st) is True


def test_extract_problem_id():
    """Verify problem ID extraction across all supported platforms."""
    assert extract_problem_id("https://codeforces.com/problemset/problem/1294/C", "codeforces") == "1294C"
    assert extract_problem_id("https://codeforces.com/contest/1352/problem/C/", "codeforces") == "1352C"
    assert extract_problem_id("https://www.hackerrank.com/challenges/simple-array-sum/problem", "hackerrank") == "simple-array-sum"
    assert extract_problem_id("https://leetcode.com/problems/two-sum/description/", "leetcode") == "two-sum"
    assert extract_problem_id("https://www.codechef.com/problems/FLOW001", "codechef") == "FLOW001"
    assert extract_problem_id("https://www.hackerearth.com/practice/algorithm/favourite-singer/", "hackerearth") == "favourite-singer"


# ==============================================================================
# 2. HackerRank Verdict Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_hackerrank_accepted_current_submission():
    """Accepted current submission with valid timestamp and ID -> ACCEPTED."""
    now = time.time()
    page = MockPage({
        "models": [{
            "id": 9001,
            "status": "Accepted",
            "score": 10.0,
            "created_at": now + 2,
            "hacker_username": "vinay"
        }]
    })
    v = await verify_live_submission_verdict(
        page, "hackerrank", "https://www.hackerrank.com/challenges/simple-array-sum",
        submission_start_time=now, pre_submission_id="9000", account="vinay",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.ACCEPTED
    assert v.submission_id == "9001"
    assert "Score: 10.0" in v.message
    assert bool(v) is True


@pytest.mark.asyncio
async def test_hackerrank_wrong_answer():
    """Wrong Answer submission -> REJECTED."""
    now = time.time()
    page = MockPage({
        "models": [{
            "id": 9002,
            "status": "Wrong Answer",
            "score": 0.0,
            "created_at": now + 1,
            "hacker_username": "vinay"
        }]
    })
    v = await verify_live_submission_verdict(
        page, "hackerrank", "https://www.hackerrank.com/challenges/simple-array-sum",
        submission_start_time=now, account="vinay",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.REJECTED
    assert bool(v) is False


@pytest.mark.asyncio
async def test_hackerrank_compilation_error():
    """Compilation Error submission -> REJECTED."""
    now = time.time()
    page = MockPage({
        "models": [{
            "id": 9003,
            "status": "Compilation Error",
            "score": 0.0,
            "created_at": now + 1,
            "hacker_username": "vinay"
        }]
    })
    v = await verify_live_submission_verdict(
        page, "hackerrank", "https://www.hackerrank.com/challenges/simple-array-sum",
        submission_start_time=now, account="vinay",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.REJECTED


@pytest.mark.asyncio
async def test_hackerrank_pending_timeout():
    """Processing/Queued state that times out -> PENDING."""
    now = time.time()
    page = MockPage({
        "models": [{
            "id": 9004,
            "status": "Processing",
            "score": 0.0,
            "created_at": now + 1,
            "hacker_username": "vinay"
        }]
    })
    v = await verify_live_submission_verdict(
        page, "hackerrank", "https://www.hackerrank.com/challenges/simple-array-sum",
        submission_start_time=now, account="vinay",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.PENDING
    assert v.submission_id == "9004"
    assert bool(v) is False


@pytest.mark.asyncio
async def test_hackerrank_stale_accepted_submission():
    """Historical submission matching pre_submission_id -> UNKNOWN (stale rejected)."""
    now = time.time()
    page = MockPage({
        "models": [{
            "id": 8888,
            "status": "Accepted",
            "score": 10.0,
            "created_at": now - 3600,
            "hacker_username": "vinay"
        }]
    })
    v = await verify_live_submission_verdict(
        page, "hackerrank", "https://www.hackerrank.com/challenges/simple-array-sum",
        submission_start_time=now, pre_submission_id="8888", account="vinay",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.UNKNOWN
    assert bool(v) is False


@pytest.mark.asyncio
async def test_hackerrank_no_submission_detected():
    """Empty models list -> UNKNOWN."""
    page = MockPage({"models": []})
    v = await verify_live_submission_verdict(
        page, "hackerrank", "https://www.hackerrank.com/challenges/simple-array-sum",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.UNKNOWN


@pytest.mark.asyncio
async def test_hackerrank_network_failure():
    """Network failure / API exception -> ERROR."""
    page = MockPage(eval_side_effect=Exception("Connection refused"))
    v = await verify_live_submission_verdict(
        page, "hackerrank", "https://www.hackerrank.com/challenges/simple-array-sum",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.ERROR


# ==============================================================================
# 3. Codeforces Verdict Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_codeforces_accepted_current_submission():
    """Codeforces OK verdict -> ACCEPTED."""
    now = time.time()
    cf_data = {
        "status": "OK",
        "result": [{
            "id": 3001,
            "contestId": 1294,
            "creationTimeSeconds": int(now + 2),
            "problem": {"contestId": 1294, "index": "C"},
            "author": {"members": [{"handle": "coder_cf"}]},
            "verdict": "OK"
        }]
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(cf_data).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        v = await verify_live_submission_verdict(
            MockPage(), "codeforces", "https://codeforces.com/problemset/problem/1294/C",
            submission_start_time=now, account="coder_cf",
            timeout=0.05, initial_interval=0.01
        )
        assert v.state == VerdictState.ACCEPTED
        assert v.submission_id == "3001"
        assert bool(v) is True


@pytest.mark.asyncio
async def test_codeforces_wrong_answer():
    """Codeforces WRONG_ANSWER -> REJECTED."""
    now = time.time()
    cf_data = {
        "status": "OK",
        "result": [{
            "id": 3002,
            "contestId": 1294,
            "creationTimeSeconds": int(now + 2),
            "problem": {"contestId": 1294, "index": "C"},
            "author": {"members": [{"handle": "coder_cf"}]},
            "verdict": "WRONG_ANSWER"
        }]
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(cf_data).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        v = await verify_live_submission_verdict(
            MockPage(), "codeforces", "https://codeforces.com/problemset/problem/1294/C",
            submission_start_time=now, account="coder_cf",
            timeout=0.05, initial_interval=0.01
        )
        assert v.state == VerdictState.REJECTED
        assert bool(v) is False


@pytest.mark.asyncio
async def test_codeforces_compilation_error():
    """Codeforces COMPILATION_ERROR -> REJECTED."""
    now = time.time()
    cf_data = {
        "status": "OK",
        "result": [{
            "id": 3003,
            "contestId": 1294,
            "creationTimeSeconds": int(now + 2),
            "problem": {"contestId": 1294, "index": "C"},
            "author": {"members": [{"handle": "coder_cf"}]},
            "verdict": "COMPILATION_ERROR"
        }]
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(cf_data).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        v = await verify_live_submission_verdict(
            MockPage(), "codeforces", "https://codeforces.com/problemset/problem/1294/C",
            submission_start_time=now, account="coder_cf",
            timeout=0.05, initial_interval=0.01
        )
        assert v.state == VerdictState.REJECTED


@pytest.mark.asyncio
async def test_codeforces_testing_fallback_removed():
    """REQUIREMENT 4: TESTING must NEVER qualify as ACCEPTED. Times out to PENDING."""
    now = time.time()
    cf_data = {
        "status": "OK",
        "result": [{
            "id": 3004,
            "contestId": 1294,
            "creationTimeSeconds": int(now + 2),
            "problem": {"contestId": 1294, "index": "C"},
            "author": {"members": [{"handle": "coder_cf"}]},
            "verdict": "TESTING"
        }]
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(cf_data).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        v = await verify_live_submission_verdict(
            MockPage(), "codeforces", "https://codeforces.com/problemset/problem/1294/C",
            submission_start_time=now, account="coder_cf",
            timeout=0.05, initial_interval=0.01
        )
        # MUST be PENDING, absolutely NOT ACCEPTED
        assert v.state == VerdictState.PENDING
        assert bool(v) is False
        assert v.submission_id == "3004"


@pytest.mark.asyncio
async def test_codeforces_stale_accepted_submission():
    """Stale submission from earlier run -> UNKNOWN."""
    now = time.time()
    cf_data = {
        "status": "OK",
        "result": [{
            "id": 2999,
            "contestId": 1294,
            "creationTimeSeconds": int(now - 500),  # 500s ago
            "problem": {"contestId": 1294, "index": "C"},
            "author": {"members": [{"handle": "coder_cf"}]},
            "verdict": "OK"
        }]
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(cf_data).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        v = await verify_live_submission_verdict(
            MockPage(), "codeforces", "https://codeforces.com/problemset/problem/1294/C",
            submission_start_time=now, account="coder_cf",
            timeout=0.05, initial_interval=0.01
        )
        assert v.state == VerdictState.UNKNOWN


@pytest.mark.asyncio
async def test_codeforces_no_submission_detected():
    """No matching submission found -> UNKNOWN."""
    cf_data = {"status": "OK", "result": []}
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(cf_data).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        v = await verify_live_submission_verdict(
            MockPage(), "codeforces", "https://codeforces.com/problemset/problem/1294/C",
            submission_start_time=time.time(), account="coder_cf",
            timeout=0.05, initial_interval=0.01
        )
        assert v.state == VerdictState.UNKNOWN


@pytest.mark.asyncio
async def test_codeforces_api_network_failure():
    """Codeforces API network failure -> ERROR."""
    with patch("urllib.request.urlopen", side_effect=Exception("503 Service Unavailable")):
        v = await verify_live_submission_verdict(
            MockPage(), "codeforces", "https://codeforces.com/problemset/problem/1294/C",
            submission_start_time=time.time(), account="coder_cf",
            timeout=0.05, initial_interval=0.01
        )
        assert v.state == VerdictState.ERROR


@pytest.mark.asyncio
async def test_codeforces_anti_bot_detection():
    """Codeforces Turnstile / anti-bot challenge on page -> ERROR with anti-bot notice."""
    page = MockPage(eval_return=["Please complete the anti-bot verification challenge"])
    v = await verify_live_submission_verdict(
        page, "codeforces", "https://codeforces.com/problemset/problem/1294/C",
        submission_start_time=time.time(), account="coder_cf",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.ERROR
    assert page._anti_bot_blocked is True
    assert "anti-bot" in v.message.lower()


# ==============================================================================
# 4. LeetCode Verdict Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_leetcode_accepted_current_submission():
    """Accepted current submission in scoped container -> ACCEPTED."""
    page = MockPage({"text": "Accepted\nRuntime: 2 ms\nMemory: 42 MB", "submissionId": "7001"})
    v = await verify_live_submission_verdict(
        page, "leetcode", "https://leetcode.com/problems/two-sum/",
        submission_start_time=time.time(), pre_submission_id="7000",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.ACCEPTED
    assert v.submission_id == "7001"
    assert bool(v) is True


@pytest.mark.asyncio
async def test_leetcode_wrong_answer():
    """Wrong Answer in scoped container -> REJECTED."""
    page = MockPage({"text": "Wrong Answer\n14/50 test cases passed", "submissionId": "7002"})
    v = await verify_live_submission_verdict(
        page, "leetcode", "https://leetcode.com/problems/two-sum/",
        submission_start_time=time.time(),
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.REJECTED
    assert bool(v) is False


@pytest.mark.asyncio
async def test_leetcode_compilation_error():
    """Compile Error in scoped container -> REJECTED."""
    page = MockPage({"text": "Compile Error\nLine 5: error: cannot find symbol", "submissionId": "7003"})
    v = await verify_live_submission_verdict(
        page, "leetcode", "https://leetcode.com/problems/two-sum/",
        submission_start_time=time.time(),
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.REJECTED


@pytest.mark.asyncio
async def test_leetcode_pending_timeout():
    """Pending/Judging state timing out -> PENDING."""
    page = MockPage({"text": "Judging...\nPlease wait", "submissionId": None})
    v = await verify_live_submission_verdict(
        page, "leetcode", "https://leetcode.com/problems/two-sum/",
        submission_start_time=time.time(),
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.PENDING
    assert bool(v) is False


@pytest.mark.asyncio
async def test_leetcode_stale_accepted_submission():
    """Result with same ID as pre_submission_id -> UNKNOWN (stale)."""
    page = MockPage({"text": "Accepted\nRuntime: 2 ms", "submissionId": "7000"})
    v = await verify_live_submission_verdict(
        page, "leetcode", "https://leetcode.com/problems/two-sum/",
        submission_start_time=time.time(), pre_submission_id="7000",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.UNKNOWN


@pytest.mark.asyncio
async def test_leetcode_unrelated_accepted_text_in_dom():
    """REQUIREMENT 3 & 10: Unrelated 'Accepted' text in DOM outside scoped result container -> not ACCEPTED."""
    # When scoped submission container is not present, evaluate returns None
    page = MockPage(None)
    v = await verify_live_submission_verdict(
        page, "leetcode", "https://leetcode.com/problems/two-sum/",
        submission_start_time=time.time(),
        timeout=0.05, initial_interval=0.01
    )
    # Must NOT return 'Submitted (No negative verdict detected)' as ACCEPTED
    assert v.state == VerdictState.UNKNOWN
    assert bool(v) is False


@pytest.mark.asyncio
async def test_leetcode_evaluation_error():
    """Page evaluate crash -> ERROR."""
    page = MockPage(eval_side_effect=Exception("Page crashed"))
    v = await verify_live_submission_verdict(
        page, "leetcode", "https://leetcode.com/problems/two-sum/",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.ERROR


# ==============================================================================
# 5. CodeChef Verdict Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_codechef_accepted_current_submission():
    """CodeChef Correct Answer -> ACCEPTED."""
    page = MockPage({"text": "Correct Answer\nScore: 100/100", "submissionId": "5001"})
    v = await verify_live_submission_verdict(
        page, "codechef", "https://www.codechef.com/problems/FLOW001",
        submission_start_time=time.time(), pre_submission_id="5000",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.ACCEPTED
    assert v.submission_id == "5001"
    assert bool(v) is True


@pytest.mark.asyncio
async def test_codechef_wrong_answer():
    """CodeChef Wrong Answer -> REJECTED."""
    page = MockPage({"text": "Wrong Answer\nWA on test 2", "submissionId": "5002"})
    v = await verify_live_submission_verdict(
        page, "codechef", "https://www.codechef.com/problems/FLOW001",
        submission_start_time=time.time(),
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.REJECTED


@pytest.mark.asyncio
async def test_codechef_compilation_error():
    """CodeChef Compilation Error -> REJECTED."""
    page = MockPage({"text": "Compilation Error\njavac error", "submissionId": "5003"})
    v = await verify_live_submission_verdict(
        page, "codechef", "https://www.codechef.com/problems/FLOW001",
        submission_start_time=time.time(),
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.REJECTED


@pytest.mark.asyncio
async def test_codechef_pending_timeout_no_false_positive():
    """REQUIREMENT 5: Running state must NOT return 'CodeChef submission sent' fallback. Times out to PENDING."""
    page = MockPage({"text": "Running\nJudging in queue...", "submissionId": None})
    v = await verify_live_submission_verdict(
        page, "codechef", "https://www.codechef.com/problems/FLOW001",
        submission_start_time=time.time(),
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.PENDING
    assert bool(v) is False


@pytest.mark.asyncio
async def test_codechef_stale_accepted_submission():
    """CodeChef result matching pre_submission_id -> UNKNOWN."""
    page = MockPage({"text": "Correct Answer\nScore: 100/100", "submissionId": "5000"})
    v = await verify_live_submission_verdict(
        page, "codechef", "https://www.codechef.com/problems/FLOW001",
        submission_start_time=time.time(), pre_submission_id="5000",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.UNKNOWN


@pytest.mark.asyncio
async def test_codechef_unrelated_accepted_text_in_dom():
    """Unrelated page text outside submission status container -> not ACCEPTED."""
    page = MockPage(None)
    v = await verify_live_submission_verdict(
        page, "codechef", "https://www.codechef.com/problems/FLOW001",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.UNKNOWN
    assert bool(v) is False


# ==============================================================================
# 6. HackerEarth Verdict Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_hackerearth_accepted_current_submission():
    """HackerEarth Accepted in modal -> ACCEPTED."""
    page = MockPage({"text": "Accepted\n100/100 points", "submissionId": "4001"})
    v = await verify_live_submission_verdict(
        page, "hackerearth", "https://www.hackerearth.com/practice/algorithm/favourite-singer/",
        submission_start_time=time.time(), pre_submission_id="4000",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.ACCEPTED
    assert v.submission_id == "4001"
    assert bool(v) is True


@pytest.mark.asyncio
async def test_hackerearth_wrong_answer():
    """HackerEarth Wrong Answer in modal -> REJECTED."""
    page = MockPage({"text": "Wrong Answer\nSample test failed", "submissionId": "4002"})
    v = await verify_live_submission_verdict(
        page, "hackerearth", "https://www.hackerearth.com/practice/algorithm/favourite-singer/",
        submission_start_time=time.time(),
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.REJECTED


@pytest.mark.asyncio
async def test_hackerearth_compilation_error():
    """HackerEarth Compilation Error in modal -> REJECTED."""
    page = MockPage({"text": "Compilation Error\nCannot find symbol", "submissionId": "4003"})
    v = await verify_live_submission_verdict(
        page, "hackerearth", "https://www.hackerearth.com/practice/algorithm/favourite-singer/",
        submission_start_time=time.time(),
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.REJECTED


@pytest.mark.asyncio
async def test_hackerearth_pending_timeout_no_false_positive():
    """REQUIREMENT 6: Queued state must NOT return 'evaluation pending' as ACCEPTED. Times out to PENDING."""
    page = MockPage({"text": "Queued\nRunning test cases...", "submissionId": None})
    v = await verify_live_submission_verdict(
        page, "hackerearth", "https://www.hackerearth.com/practice/algorithm/favourite-singer/",
        submission_start_time=time.time(),
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.PENDING
    assert bool(v) is False


@pytest.mark.asyncio
async def test_hackerearth_stale_accepted_submission():
    """HackerEarth result matching pre_submission_id -> UNKNOWN."""
    page = MockPage({"text": "Accepted\n100/100 points", "submissionId": "4000"})
    v = await verify_live_submission_verdict(
        page, "hackerearth", "https://www.hackerearth.com/practice/algorithm/favourite-singer/",
        submission_start_time=time.time(), pre_submission_id="4000",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.UNKNOWN


@pytest.mark.asyncio
async def test_hackerearth_unrelated_accepted_text_in_dom():
    """REQUIREMENT 10: Unrelated text in page body outside modal -> not ACCEPTED."""
    page = MockPage(None)
    v = await verify_live_submission_verdict(
        page, "hackerearth", "https://www.hackerearth.com/practice/algorithm/favourite-singer/",
        timeout=0.05, initial_interval=0.01
    )
    assert v.state == VerdictState.UNKNOWN
    assert bool(v) is False


# ==============================================================================
# 7. Universal Fallback & Unsupported Platforms
# ==============================================================================
@pytest.mark.asyncio
async def test_universal_unsupported_platform_fallback():
    """Unsupported platform must return UNKNOWN, NEVER 'Submitted successfully'."""
    v = await verify_live_submission_verdict(
        MockPage(), "unknown_platform", "https://unknown.com/problem/123",
        timeout=0.05
    )
    assert v.state == VerdictState.UNKNOWN
    assert bool(v) is False


# ==============================================================================
# 8. Evidence Persistence & Solution Archival Tests
# ==============================================================================
def test_record_submission_evidence(tmp_path):
    """Verify detailed evidence is captured and written to submissions log."""
    verdict = SubmissionVerdict(
        state=VerdictState.ACCEPTED,
        message="Accepted (Score: 100)",
        submission_id="sub_998877",
        platform="hackerrank",
        problem_id="diagonal-difference",
        account="vinay",
        correlation_mechanism="hackerrank_rest_api_submission_id",
        evidence={"score": 100, "challenge": "diagonal-difference"},
    )
    test_log = tmp_path / "submissions.jsonl"
    with patch("bot.SUBMISSIONS_LOG_FILE", test_log), patch("bot.LOGS_DIR", tmp_path):
        record_submission_evidence(verdict, "https://www.hackerrank.com/challenges/diagonal-difference", "hackerrank")
        assert test_log.exists()
        lines = test_log.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["submission_id"] == "sub_998877"
        assert record["verdict"] == "ACCEPTED"
        assert record["correlation_mechanism"] == "hackerrank_rest_api_submission_id"


def test_save_java_solution_includes_verdict_and_submission_id(tmp_path):
    """Verify saved Java file header contains verifiable submission ID and verdict."""
    verdict = SubmissionVerdict(
        state=VerdictState.ACCEPTED,
        message="Accepted",
        submission_id="sub_cf_12345",
        correlation_mechanism="codeforces_api_user_status_id",
    )
    with patch("bot.SOLUTIONS_DIR", tmp_path):
        save_java_solution(
            "test_sol.java",
            "public class Main {}",
            "https://codeforces.com/problemset/problem/1294/C",
            "Optimal greedy algorithm",
            "Codeforces",
            verdict=verdict,
        )
        saved_file = tmp_path / "test_sol.java"
        assert saved_file.exists()
        content = saved_file.read_text(encoding="utf-8")
        assert "Submission ID: sub_cf_12345" in content
        assert "Verdict: ACCEPTED" in content


def test_commit_solution_to_github_includes_submission_id(tmp_path):
    """Verify README.md created by commit_solution_to_github contains submission ID."""
    verdict = SubmissionVerdict(
        state=VerdictState.ACCEPTED,
        message="Accepted (Score: 100)",
        submission_id="sub_hr_9988",
        correlation_mechanism="hackerrank_rest_api_submission_id",
    )
    repo_dir = tmp_path / "hackerrank-daily"
    repo_dir.mkdir(parents=True)
    with patch("bot.GITHUB_USERNAME", "testuser"), \
         patch("bot.REPOS_DIR", tmp_path), \
         patch("subprocess.run") as mock_sub:
        mock_sub.return_value.returncode = 0
        mock_sub.return_value.stdout = "ahead"
        ok = commit_solution_to_github(
            "hackerrank",
            "https://www.hackerrank.com/challenges/simple-array-sum",
            "public class Solution {}",
            "Array summation",
            "HackerRank",
            verdict=verdict,
        )
        assert ok.success is True
        # Check generated README
        readmes = list(repo_dir.glob("**/README.md"))
        assert len(readmes) == 1
        readme_content = readmes[0].read_text(encoding="utf-8")
        assert "Submission ID: `sub_hr_9988`" in readme_content


@pytest.mark.asyncio
async def test_process_problem_skips_commit_on_unaccepted_verdict():
    """Verify that process_problem strictly refuses to commit when judge does not return ACCEPTED."""
    page = MockPipelinePage()

    with patch("asyncio.sleep"):
        # Case 1: Rejected verdict -> returns False, never commits
        with patch("bot.switch_editor_to_java"), \
             patch("bot.extract_problem_text", return_value="Solve Two Sum"), \
             patch("bot.extract_editor_code", return_value="class Solution {}"), \
             patch("bot.ask_gemini_for_java_code", return_value=("public class Solution {}", "Greedy")), \
             patch("bot.compile_java_code_locally", return_value=(True, "", "public class Solution {}")), \
             patch("bot.inject_code_into_editor", return_value=True), \
             patch("bot.capture_pre_submission_snapshot", return_value={"start_time": time.time(), "pre_submission_id": None, "account": None, "problem_id": "two-sum"}), \
             patch("bot.verify_live_submission_verdict", return_value=SubmissionVerdict(VerdictState.REJECTED, "Wrong Answer")), \
             patch("bot.commit_solution_to_github") as mock_commit:
            res = await process_problem(page, "https://leetcode.com/problems/two-sum", mode="submit")
            assert res.success is False
            assert res.stage == ProcessStage.REJECTED
            mock_commit.assert_not_called()

        # Case 2: Pending verdict -> returns False, never commits
        with patch("bot.switch_editor_to_java"), \
             patch("bot.extract_problem_text", return_value="Solve Two Sum"), \
             patch("bot.extract_editor_code", return_value="class Solution {}"), \
             patch("bot.ask_gemini_for_java_code", return_value=("public class Solution {}", "Greedy")), \
             patch("bot.compile_java_code_locally", return_value=(True, "", "public class Solution {}")), \
             patch("bot.inject_code_into_editor", return_value=True), \
             patch("bot.capture_pre_submission_snapshot", return_value={"start_time": time.time(), "pre_submission_id": None, "account": None, "problem_id": "two-sum"}), \
             patch("bot.verify_live_submission_verdict", return_value=SubmissionVerdict(VerdictState.PENDING, "Judging")), \
             patch("bot.commit_solution_to_github") as mock_commit:
            res = await process_problem(page, "https://leetcode.com/problems/two-sum", mode="submit")
            assert res.success is False
            assert res.stage == ProcessStage.REJECTED
            mock_commit.assert_not_called()

        # Case 3: Accepted verdict -> returns True, commits to GitHub
        with patch("bot.switch_editor_to_java"), \
             patch("bot.extract_problem_text", return_value="Solve Two Sum"), \
             patch("bot.extract_editor_code", return_value="class Solution {}"), \
             patch("bot.ask_gemini_for_java_code", return_value=("public class Solution {}", "Greedy")), \
             patch("bot.compile_java_code_locally", return_value=(True, "", "public class Solution {}")), \
             patch("bot.inject_code_into_editor", return_value=True), \
             patch("bot.capture_pre_submission_snapshot", return_value={"start_time": time.time(), "pre_submission_id": None, "account": None, "problem_id": "two-sum"}), \
             patch("bot.verify_live_submission_verdict", return_value=SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id="sub_999")), \
             patch("bot.save_java_solution"), \
             patch("bot.commit_solution_to_github", return_value=PublishResult(success=True, branch="main", repo_name="leetcode-daily")) as mock_commit:
            res = await process_problem(page, "https://leetcode.com/problems/two-sum", mode="submit")
            assert res.success is True
            assert res.stage == ProcessStage.PUBLISHED
            mock_commit.assert_called_once()


# ==============================================================================
# 9. Structured ProcessResult & Safe Publishing Tests
# ==============================================================================

def test_process_result_and_publish_result_models():
    """Verify ProcessStage, ProcessResult, and PublishResult behavior."""
    assert ProcessStage.PUBLISHED == "PUBLISHED"
    assert ProcessStage.PUBLISH_FAILED == "PUBLISH_FAILED"
    assert ProcessStage.TESTED == "TESTED"
    assert ProcessStage.COMPILED == "COMPILED"

    res_ok = ProcessResult(stage=ProcessStage.PUBLISHED, success=True, published=True)
    assert bool(res_ok) is True
    assert res_ok == ProcessStage.PUBLISHED
    assert res_ok == "PUBLISHED"
    assert res_ok == True

    res_fail = ProcessResult(stage=ProcessStage.PUBLISH_FAILED, success=False, published=False, publish_error="push error")
    assert bool(res_fail) is False
    assert res_fail == ProcessStage.PUBLISH_FAILED
    assert res_fail == False

    pub_ok = PublishResult(success=True, branch="main", repo_name="repo")
    assert bool(pub_ok) is True
    assert pub_ok == True
    success_flag, err_msg = pub_ok
    assert success_flag is True
    assert err_msg is None

    pub_fail = PublishResult(success=False, error="rebase conflict")
    assert bool(pub_fail) is False
    assert pub_fail == False


def test_get_repo_default_branch(tmp_path):
    """Verify default branch detection from git repository."""
    assert get_repo_default_branch(tmp_path) == "main"

    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value.returncode = 0
        mock_sub.return_value.stdout = "origin/master\n"
        assert get_repo_default_branch(tmp_path) == "master"

    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value.returncode = 0
        mock_sub.return_value.stdout = "origin/develop\n"
        assert get_repo_default_branch(tmp_path) == "develop"


def test_commit_solution_to_github_failed_pull_no_commit(tmp_path):
    """Verify that if git pull --rebase fails, commit and push are NEVER attempted."""
    repo_dir = tmp_path / "leetcode-daily"
    repo_dir.mkdir(parents=True)
    verdict = SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id="sub_1")

    call_commands = []
    def fake_subprocess_run(cmd, *args, **kwargs):
        call_commands.append(cmd)
        mock = MagicMock()
        if "pull" in cmd:
            mock.returncode = 1
            mock.stderr = "fatal: unable to rebase"
            mock.stdout = ""
        else:
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
        return mock

    with patch("bot.GITHUB_USERNAME", "testuser"), \
         patch("bot.REPOS_DIR", tmp_path), \
         patch("subprocess.run", side_effect=fake_subprocess_run):
        res = commit_solution_to_github(
            "leetcode",
            "https://leetcode.com/problems/two-sum",
            "class Solution {}",
            "Greedy",
            "LeetCode",
            verdict=verdict
        )
        assert res.success is False
        assert "Pull failed" in res.error
        commit_calls = [c for c in call_commands if "commit" in c]
        assert len(commit_calls) == 0, f"Expected no git commit calls, got: {commit_calls}"
        push_calls = [c for c in call_commands if "push" in c]
        assert len(push_calls) == 0, f"Expected no git push calls, got: {push_calls}"


def test_commit_solution_to_github_failed_commit_no_push(tmp_path):
    """Verify that if git commit fails, git push is NEVER attempted."""
    repo_dir = tmp_path / "leetcode-daily"
    repo_dir.mkdir(parents=True)
    verdict = SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id="sub_1")

    call_commands = []
    def fake_subprocess_run(cmd, *args, **kwargs):
        call_commands.append(cmd)
        mock = MagicMock()
        if "status" in cmd:
            mock.returncode = 0
            mock.stdout = "A  Solution.java\n"
            mock.stderr = ""
        elif "commit" in cmd:
            mock.returncode = 1
            mock.stderr = "error: unable to write commit object"
            mock.stdout = ""
        else:
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
        return mock

    with patch("bot.GITHUB_USERNAME", "testuser"), \
         patch("bot.REPOS_DIR", tmp_path), \
         patch("subprocess.run", side_effect=fake_subprocess_run):
        res = commit_solution_to_github(
            "leetcode",
            "https://leetcode.com/problems/two-sum",
            "class Solution {}",
            "Greedy",
            "LeetCode",
            verdict=verdict
        )
        assert res.success is False
        assert "Commit failed" in res.error
        push_calls = [c for c in call_commands if "push" in c]
        assert len(push_calls) == 0, f"Expected no git push calls, got: {push_calls}"


def test_commit_solution_to_github_stages_only_solution_and_readme_never_git_add_all(tmp_path):
    """Verify that git add . is NEVER called; only targeted solution and readme files are staged."""
    repo_dir = tmp_path / "leetcode-daily"
    repo_dir.mkdir(parents=True)
    verdict = SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id="sub_1")

    call_commands = []
    def fake_subprocess_run(cmd, *args, **kwargs):
        call_commands.append(cmd)
        mock = MagicMock()
        if "status" in cmd:
            mock.returncode = 0
            mock.stdout = "A  solutions/..."
        else:
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
        return mock

    with patch("bot.GITHUB_USERNAME", "testuser"), \
         patch("bot.REPOS_DIR", tmp_path), \
         patch("subprocess.run", side_effect=fake_subprocess_run):
        res = commit_solution_to_github(
            "leetcode",
            "https://leetcode.com/problems/two-sum",
            "class Solution {}",
            "Greedy",
            "LeetCode",
            verdict=verdict
        )
        assert res.success is True
        for c in call_commands:
            if c[:2] == ["git", "add"]:
                assert "." not in c, f"Found unsafe 'git add .' invocation: {c}"
                staged = [arg for arg in c[2:] if not arg.startswith("-")]
                assert len(staged) == 2
                assert any("Solution.java" in s for s in staged)
                assert any("README.md" in s for s in staged)


def test_commit_solution_to_github_refuses_non_accepted_verdict(tmp_path):
    """Verify commit_solution_to_github refuses to publish non-accepted verdicts."""
    repo_dir = tmp_path / "leetcode-daily"
    repo_dir.mkdir(parents=True)
    verdict = SubmissionVerdict(VerdictState.REJECTED, "Wrong Answer")

    with patch("bot.GITHUB_USERNAME", "testuser"), \
         patch("bot.REPOS_DIR", tmp_path):
        res = commit_solution_to_github(
            "leetcode",
            "https://leetcode.com/problems/two-sum",
            "class Solution {}",
            "Greedy",
            "LeetCode",
            verdict=verdict
        )
        assert res.success is False
        assert "Cannot commit solution with non-accepted verdict" in res.error


@pytest.mark.asyncio
async def test_process_problem_accepted_and_push_success_yields_published():
    """Verify Accepted + push success -> PUBLISHED."""
    page = MockPipelinePage()
    with patch("asyncio.sleep"), \
         patch("bot.switch_editor_to_java"), \
         patch("bot.extract_problem_text", return_value="Solve Two Sum"), \
         patch("bot.extract_editor_code", return_value="class Solution {}"), \
         patch("bot.ask_gemini_for_java_code", return_value=("public class Solution {}", "Greedy")), \
         patch("bot.compile_java_code_locally", return_value=(True, "", "public class Solution {}")), \
         patch("bot.inject_code_into_editor", return_value=True), \
         patch("bot.capture_pre_submission_snapshot", return_value={"start_time": time.time(), "pre_submission_id": None, "account": None, "problem_id": "two-sum"}), \
         patch("bot.verify_live_submission_verdict", return_value=SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id="sub_ok_100")), \
         patch("bot.save_java_solution"), \
         patch("bot.commit_solution_to_github", return_value=PublishResult(success=True, branch="main", repo_name="leetcode-daily")) as mock_commit:
        result = await process_problem(page, "https://leetcode.com/problems/two-sum", mode="submit")
        assert result.stage == ProcessStage.PUBLISHED
        assert result.success is True
        assert result.published is True
        assert result.verdict.state == VerdictState.ACCEPTED
        mock_commit.assert_called_once()


@pytest.mark.asyncio
async def test_process_problem_accepted_and_push_failure_yields_publish_failed():
    """Verify Accepted + push failure -> PUBLISH_FAILED."""
    page = MockPipelinePage()
    with patch("asyncio.sleep"), \
         patch("bot.switch_editor_to_java"), \
         patch("bot.extract_problem_text", return_value="Solve Two Sum"), \
         patch("bot.extract_editor_code", return_value="class Solution {}"), \
         patch("bot.ask_gemini_for_java_code", return_value=("public class Solution {}", "Greedy")), \
         patch("bot.compile_java_code_locally", return_value=(True, "", "public class Solution {}")), \
         patch("bot.inject_code_into_editor", return_value=True), \
         patch("bot.capture_pre_submission_snapshot", return_value={"start_time": time.time(), "pre_submission_id": None, "account": None, "problem_id": "two-sum"}), \
         patch("bot.verify_live_submission_verdict", return_value=SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id="sub_ok_100")), \
         patch("bot.save_java_solution"), \
         patch("bot.commit_solution_to_github", return_value=PublishResult(success=False, error="Git push failed: network error")) as mock_commit:
        result = await process_problem(page, "https://leetcode.com/problems/two-sum", mode="submit")
        assert result.stage == ProcessStage.PUBLISH_FAILED
        assert result.success is False
        assert result.published is False
        assert result.verdict.state == VerdictState.ACCEPTED
        assert "Git push failed" in result.publish_error
        mock_commit.assert_called_once()


@pytest.mark.asyncio
async def test_process_problem_mode_run_yields_tested_never_accepted():
    """Verify Run-only -> TESTED, never ACCEPTED, never published."""
    page = MockPipelinePage()
    with patch("asyncio.sleep"), \
         patch("bot.switch_editor_to_java"), \
         patch("bot.extract_problem_text", return_value="Solve Two Sum"), \
         patch("bot.extract_editor_code", return_value="class Solution {}"), \
         patch("bot.ask_gemini_for_java_code", return_value=("public class Solution {}", "Greedy")), \
         patch("bot.compile_java_code_locally", return_value=(True, "", "public class Solution {}")), \
         patch("bot.inject_code_into_editor", return_value=True), \
         patch("bot.save_java_solution") as mock_save, \
         patch("bot.verify_live_submission_verdict") as mock_verify, \
         patch("bot.commit_solution_to_github") as mock_commit:
        result = await process_problem(page, "https://leetcode.com/problems/two-sum", mode="run")
        assert result.stage == ProcessStage.TESTED
        assert result.success is True
        assert result.published is False
        assert result.verdict is None
        mock_verify.assert_not_called()
        mock_commit.assert_not_called()
        mock_save.assert_called_once()
        assert mock_save.call_args[1].get("verdict") is None


@pytest.mark.asyncio
async def test_process_problem_mode_save_yields_compiled_never_accepted():
    """Verify Save-only -> local save, COMPILED, never ACCEPTED, never submitted."""
    page = MockPipelinePage()
    with patch("asyncio.sleep"), \
         patch("bot.switch_editor_to_java"), \
         patch("bot.extract_problem_text", return_value="Solve Two Sum"), \
         patch("bot.extract_editor_code", return_value="class Solution {}"), \
         patch("bot.ask_gemini_for_java_code", return_value=("public class Solution {}", "Greedy")), \
         patch("bot.compile_java_code_locally", return_value=(True, "", "public class Solution {}")), \
         patch("bot.save_java_solution") as mock_save, \
         patch("bot.inject_code_into_editor") as mock_inject, \
         patch("bot.verify_live_submission_verdict") as mock_verify, \
         patch("bot.commit_solution_to_github") as mock_commit:
        result = await process_problem(page, "https://leetcode.com/problems/two-sum", mode="save")
        assert result.stage == ProcessStage.COMPILED
        assert result.success is True
        assert result.published is False
        assert result.verdict is None
        mock_inject.assert_not_called()
        mock_verify.assert_not_called()
        mock_commit.assert_not_called()
        mock_save.assert_called_once()
        assert mock_save.call_args[1].get("verdict") is None


@pytest.mark.asyncio
async def test_process_problem_retry_publish_no_duplicate_judge_submission():
    """Verify Retry publish -> no duplicate judge submission (0 evaluate/submit calls on page)."""
    class SpyPage:
        def __init__(self):
            self.goto_calls = 0
            self.eval_calls = 0
            self.click_calls = 0
            self._anti_bot_blocked = False

        async def goto(self, *args, **kwargs):
            self.goto_calls += 1

        async def evaluate(self, *args, **kwargs):
            self.eval_calls += 1
            return ""

        def locator(self, *args, **kwargs):
            spy = self
            class SpyLocator:
                @property
                def first(self):
                    return self
                async def is_visible(self):
                    return True
                async def click(self):
                    spy.click_calls += 1
            return SpyLocator()

    page = SpyPage()
    cached_verdict = SubmissionVerdict(
        state=VerdictState.ACCEPTED,
        message="Accepted",
        submission_id="sub_cached_42",
        correlation_mechanism="leetcode_rest_api_submission_id"
    )

    with patch("bot.save_java_solution"), \
         patch("bot.verify_live_submission_verdict") as mock_judge, \
         patch("bot.commit_solution_to_github", return_value=PublishResult(success=True, branch="main", repo_name="leetcode-daily")) as mock_commit:
        result = await process_problem(
            page,
            "https://leetcode.com/problems/two-sum",
            mode="submit",
            retry_publish=True,
            cached_verdict=cached_verdict,
            cached_code="public class Solution {}",
            cached_explanation="Hash Map"
        )
        assert result.stage == ProcessStage.PUBLISHED
        assert result.success is True
        assert result.published is True
        assert page.goto_calls == 0
        assert page.eval_calls == 0
        assert page.click_calls == 0
        mock_judge.assert_not_called()
        mock_commit.assert_called_once()


@pytest.mark.asyncio
async def test_solve_platform_quota_retries_publish_and_credits_quota():
    """Verify solve_platform_quota retries publish on PUBLISH_FAILED without resubmitting to judge."""
    page = MockPipelinePage()
    accepted_verdict = SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id="sub_q1")

    call_count = 0
    async def mock_process_problem(p, url, mode="both", retry_publish=False, **kwargs):
        nonlocal call_count
        call_count += 1
        if retry_publish:
            return ProcessResult(
                stage=ProcessStage.PUBLISHED,
                success=True,
                published=True,
                solution_code="code",
                verdict=accepted_verdict
            )
        return ProcessResult(
            stage=ProcessStage.PUBLISH_FAILED,
            success=False,
            published=False,
            publish_error="Temporary git push network timeout",
            solution_code="code",
            explanation="Greedy",
            verdict=accepted_verdict
        )

    with patch("asyncio.sleep"), \
         patch("bot.ensure_live_page", return_value=(None, None, page)), \
         patch("bot.discover_leetcode_candidates", return_value=["https://leetcode.com/problems/two-sum"]), \
         patch("bot.SolvedRegistry.is_solved", return_value=False), \
         patch("bot.SolvedRegistry.mark_solved") as mock_mark, \
         patch("bot.process_problem", side_effect=mock_process_problem):
        acc, _, _, _ = await solve_platform_quota(page, "leetcode", target_accepted=1, mode="both")
        assert acc == 1
        assert call_count == 2
        mock_mark.assert_called_once_with("leetcode", "https://leetcode.com/problems/two-sum", verdict=accepted_verdict)


@pytest.mark.asyncio
async def test_solve_platform_quota_does_not_credit_unconfirmed_publish():
    """Verify solve_platform_quota does not increment quota or mark solved if publish fails and retry fails."""
    page = MockPipelinePage()
    accepted_verdict = SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id="sub_q2")

    async def mock_process_problem(p, url, mode="both", retry_publish=False, **kwargs):
        return ProcessResult(
            stage=ProcessStage.PUBLISH_FAILED,
            success=False,
            published=False,
            publish_error="Permanent auth error",
            solution_code="code",
            explanation="Greedy",
            verdict=accepted_verdict
        )

    with patch("asyncio.sleep"), \
         patch("bot.ensure_live_page", return_value=(None, None, page)), \
         patch("bot.discover_leetcode_candidates", return_value=["https://leetcode.com/problems/two-sum"]), \
         patch("bot.SolvedRegistry.is_solved", return_value=False), \
         patch("bot.SolvedRegistry.mark_solved") as mock_mark, \
         patch("bot.process_problem", side_effect=mock_process_problem):
        acc, _, _, _ = await solve_platform_quota(page, "leetcode", target_accepted=1, mode="both")
        assert acc == 0
        mock_mark.assert_not_called()


@pytest.mark.asyncio
async def test_execute_batch_returns_boolean_reflecting_quota_completion():
    """Verify execute_batch returns True on quota fulfillment and False on deficit."""
    with patch("bot.async_playwright") as mock_playwright:
        mock_p = MagicMock()
        mock_browser = MagicMock()
        mock_browser.pages = []
        mock_browser.new_page = AsyncMock()
        mock_browser.close = AsyncMock()
        mock_browser.add_init_script = AsyncMock()
        mock_p.chromium.launch_persistent_context = AsyncMock(return_value=mock_browser)
        mock_p.chromium.connect_over_cdp = AsyncMock(return_value=mock_browser)
        mock_playwright.return_value.__aenter__.return_value = mock_p

        # Case 1: Quota met -> returns True
        with patch("bot.solve_platform_quota", return_value=(2, None, None, None)), \
             patch("urllib.request.urlopen"):
            success = await execute_batch(platform="leetcode", count=2, difficulty="medium", mode="both", headless=True)
            assert success is True

        # Case 2: Quota unmet -> returns False
        with patch("bot.solve_platform_quota", return_value=(1, None, None, None)), \
             patch("urllib.request.urlopen"):
            success = await execute_batch(platform="leetcode", count=3, difficulty="medium", mode="both", headless=True)
            assert success is False


# ==============================================================================
# Prompt 3 Tests: Duplicate Detection, Solved History, Quotas & Storage
# ==============================================================================

def test_existing_unverified_local_file_does_not_count_as_accepted(tmp_path):
    """Verify that mere existence of a .java file or directory does not count as accepted."""
    # Create fake local solutions
    fake_solution = SOLUTIONS_DIR / "LeetCode_FakeProblem_Solution.java"
    try:
        SOLUTIONS_DIR.mkdir(parents=True, exist_ok=True)
        fake_solution.write_text("// dummy unverified solution", encoding="utf-8")

        # Check via SolvedRegistry
        assert SolvedRegistry.is_solved("leetcode", "fake-problem") is False
        assert SolvedRegistry.is_accepted("leetcode", "fake-problem") is False
        all_solved = SolvedRegistry.get_all_solved()
        assert "fake_problem" not in all_solved.get("leetcode", set())
        assert "fake-problem" not in all_solved.get("leetcode", set())
    finally:
        if fake_solution.exists():
            fake_solution.unlink()


def test_duplicate_urls_normalize_to_one_problem():
    """Verify that multiple query parameter variations, hash fragments, and subpaths normalize to the canonical ID."""
    url_variations = [
        ("https://leetcode.com/problems/two-sum", "leetcode", "two-sum"),
        ("https://leetcode.com/problems/two-sum/", "leetcode", "two-sum"),
        ("https://leetcode.com/problems/two-sum/description/?envType=study-plan", "leetcode", "two-sum"),
        ("https://leetcode.com/problems/two-sum#solution", "leetcode", "two-sum"),
        ("https://codeforces.com/problemset/problem/4/A", "codeforces", "4A"),
        ("https://codeforces.com/problemset/problem/4/A/", "codeforces", "4A"),
        ("https://www.hackerrank.com/challenges/simple-array-sum/problem?isFullScreen=true", "hackerrank", "simple-array-sum"),
        ("https://www.hackerrank.com/challenges/simple-array-sum", "hackerrank", "simple-array-sum"),
        ("https://www.codechef.com/problems/FLOW001?tab=statement", "codechef", "FLOW001"),
        ("https://www.codechef.com/problems/FLOW001/", "codechef", "FLOW001"),
        ("https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/zoos-06063825/", "hackerearth", "zoos-06063825"),
    ]

    for raw_url, expected_plat, expected_canon in url_variations:
        plat, canon = normalize_problem_identity(raw_url, expected_plat)
        assert plat == expected_plat, f"Failed plat for {raw_url}: got {plat}"
        assert canon == expected_canon, f"Failed canon for {raw_url}: got {canon}"


def test_two_workers_cannot_claim_same_problem_simultaneously(tmp_path):
    """Verify transactional locking prevents two workers from claiming the same problem simultaneously."""
    db = DatabaseManager(db_path=tmp_path / "claim_test.db", auto_migrate=False)

    # Worker 1 claims the problem
    c1 = db.claim_problem("leetcode", "trapping-rain-water", "https://leetcode.com/problems/trapping-rain-water", worker_id="worker_alpha")
    assert c1 is True

    # Worker 2 attempts to claim while lease is active
    c2 = db.claim_problem("leetcode", "trapping-rain-water", "https://leetcode.com/problems/trapping-rain-water", worker_id="worker_beta")
    assert c2 is False

    # Worker 1 can re-acquire its own lease
    c1_again = db.claim_problem("leetcode", "trapping-rain-water", "https://leetcode.com/problems/trapping-rain-water", worker_id="worker_alpha")
    assert c1_again is True

    # After being marked ACCEPTED, neither worker can claim it
    db.mark_problem_accepted("leetcode", "trapping-rain-water", "https://leetcode.com/problems/trapping-rain-water")
    c3 = db.claim_problem("leetcode", "trapping-rain-water", "https://leetcode.com/problems/trapping-rain-water", worker_id="worker_gamma")
    assert c3 is False


def test_failed_candidate_is_not_retried_endlessly(tmp_path):
    """Verify that failure count accumulates and permanent failures are excluded from candidate pools."""
    db = DatabaseManager(db_path=tmp_path / "failure_test.db", auto_migrate=False)

    # Transient failures accumulate up to max_failures (3)
    assert db.is_permanent_failure("codeforces", "100_a") is False

    db.release_problem("codeforces", "100_a", status="FAILED", failure_reason="Temporary rate limit", is_transient=True)
    assert db.is_permanent_failure("codeforces", "100_a") is False

    db.release_problem("codeforces", "100_a", status="FAILED", failure_reason="Gateway 504 timeout", is_transient=True)
    assert db.is_permanent_failure("codeforces", "100_a") is False

    # Third failure hits threshold -> permanently excluded
    db.release_problem("codeforces", "100_a", status="FAILED", failure_reason="Third timeout", is_transient=True)
    assert db.is_permanent_failure("codeforces", "100_a") is True

    # Direct REJECTED or FAILED status is immediately permanent
    db.release_problem("codeforces", "200_b", status="REJECTED", failure_reason="Wrong Answer on test 4", is_transient=False)
    assert db.is_permanent_failure("codeforces", "200_b") is True


@pytest.mark.asyncio
async def test_five_accepted_on_one_platform_cannot_satisfy_another_platform_quota():
    """Verify that meeting or exceeding quota on one platform does not fulfill other platforms' quotas."""
    with patch("bot.async_playwright") as mock_playwright:
        mock_p = MagicMock()
        mock_browser = MagicMock()
        mock_browser.pages = []
        mock_browser.new_page = AsyncMock()
        mock_browser.close = AsyncMock()
        mock_browser.add_init_script = AsyncMock()
        mock_p.chromium.launch_persistent_context = AsyncMock(return_value=mock_browser)
        mock_p.chromium.connect_over_cdp = AsyncMock(return_value=mock_browser)
        mock_playwright.return_value.__aenter__.return_value = mock_p

        # Simulate: LeetCode has 5 accepted, but Codeforces, HackerRank, CodeChef, and HackerEarth have 0
        async def mock_solve_quota(page, plat, target_accepted=5, **kwargs):
            if plat == "leetcode":
                return 5, None, None, None
            return 0, None, None, None

        with patch("bot.solve_platform_quota", side_effect=mock_solve_quota), \
             patch("urllib.request.urlopen"):
            # Target is 5 per platform across all 5 platforms (total 25)
            # 5 on LeetCode cannot satisfy the other 4 platforms!
            success = await execute_batch(platform="all", count=5, difficulty="medium", mode="both", headless=True)
            assert success is False


def test_restart_preserves_progress(tmp_path):
    """Verify that restarting the bot preserves verified solved status and daily run history."""
    db_file = tmp_path / "persistence_test.db"

    # Session 1: Record 2 accepted problems and 1 daily run
    db1 = DatabaseManager(db_path=db_file, auto_migrate=False)
    v1 = SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id="sub_p1")
    v2 = SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id="sub_p2")
    db1.mark_problem_accepted("hackerrank", "sherlock-and-valid-string", "https://hackerrank.com/challenges/sherlock-and-valid-string", verdict=v1)
    db1.mark_problem_accepted("hackerrank", "almost-sorted", "https://hackerrank.com/challenges/almost-sorted", verdict=v2)

    run_id = db1.get_or_create_daily_run("hackerrank", target_quota=5, run_date="2026-11-12")
    db1.increment_daily_run_counts(run_id, attempted=2, generated=2, compiled=2, accepted=2, published=2)

    # Session 2: Fresh instance pointing to the same SQLite file
    db2 = DatabaseManager(db_path=db_file, auto_migrate=False)
    assert db2.is_accepted("hackerrank", "sherlock-and-valid-string") is True
    assert db2.is_accepted("hackerrank", "almost-sorted") is True
    assert db2.is_accepted("hackerrank", "unknown-challenge") is False

    summary = db2.get_daily_summary("2026-11-12")
    assert len(summary) == 1
    row = summary[0]
    assert row["platform"] == "hackerrank"
    assert row["target_quota"] == 5
    assert row["accepted_count"] == 2
    assert row["published_count"] == 2


def test_migration_preserves_existing_history_without_inventing_acceptance(tmp_path):
    """Verify that migration imports solved_history.json but marks entries without verified evidence as UNVERIFIED."""
    db_file = tmp_path / "migration_test.db"
    hist_file = tmp_path / "solved_history.json"
    subs_file = tmp_path / "submissions.jsonl"

    # Legacy history contains 4 problems
    history_content = {
        "leetcode": ["two_sum", "reverse_integer"],
        "codeforces": ["4_a", "71_a"]
    }
    hist_file.write_text(json.dumps(history_content), encoding="utf-8")

    # Only "two-sum" and "4_a" have confirmed judge ACCEPTED evidence in submissions.jsonl
    subs_lines = [
        json.dumps({
            "timestamp": "2026-11-12T10:00:00",
            "platform": "leetcode",
            "url": "https://leetcode.com/problems/two-sum/",
            "submission_id": "sub_ev_1",
            "verdict": "ACCEPTED",
            "message": "Accepted"
        }),
        json.dumps({
            "timestamp": "2026-11-12T10:05:00",
            "platform": "codeforces",
            "url": "https://codeforces.com/problemset/problem/4/A",
            "submission_id": "sub_ev_2",
            "verdict": "ACCEPTED",
            "message": "OK"
        })
    ]
    subs_file.write_text("\n".join(subs_lines) + "\n", encoding="utf-8")

    db = DatabaseManager(db_path=db_file, auto_migrate=False)
    db.migrate_from_solved_history_if_needed(history_file=hist_file, submissions_file=subs_file)

    # Verified entries with judge evidence qualify as ACCEPTED
    assert db.is_accepted("leetcode", "two_sum") is True
    assert db.is_accepted("codeforces", "4_a") is True

    # Legacy entries WITHOUT judge evidence are preserved as UNVERIFIED and do NOT count as ACCEPTED
    assert db.is_accepted("leetcode", "reverse_integer") is False
    assert db.is_accepted("codeforces", "71_a") is False

    stats = db.get_all_time_stats()
    assert stats["leetcode"]["accepted"] == 1
    assert stats["leetcode"]["unverified"] == 1
    assert stats["codeforces"]["accepted"] == 1
    assert stats["codeforces"]["unverified"] == 1


def test_daily_quota_boundaries_use_kolkata_timezone():
    """Verify that daily quota date boundaries are strictly calculated in Asia/Kolkata timezone."""
    now_kolkata = get_kolkata_now()
    today_str = get_kolkata_today_str()

    assert now_kolkata.tzinfo is not None
    assert "Kolkata" in str(now_kolkata.tzinfo)
    assert len(today_str) == 10
    assert today_str == now_kolkata.strftime("%Y-%m-%d")


def test_print_status_summary_command(capsys, tmp_path):
    """Verify the status command generates a human-readable report with verified metrics."""
    db_file = tmp_path / "status_test.db"
    db = DatabaseManager(db_path=db_file, auto_migrate=False)

    # Insert test data
    v = SubmissionVerdict(VerdictState.ACCEPTED, "Accepted", submission_id="sub_stat")
    db.mark_problem_accepted("leetcode", "merge-two-sorted-lists", "https://leetcode.com/problems/merge-two-sorted-lists", verdict=v)
    run_id = db.get_or_create_daily_run("leetcode", target_quota=5)
    db.increment_daily_run_counts(run_id, attempted=1, generated=1, compiled=1, accepted=1, published=1)

    print_status_summary(db)
    captured = capsys.readouterr().out

    assert "AutoCoder AI: Verified Solved Status & Quota Summary" in captured
    assert "Asia/Kolkata" in captured
    assert "LeetCode" in captured
    assert "Verified Accepted" in captured
    assert "Confirmed Published" in captured
    assert "Legacy Unverified" in captured



