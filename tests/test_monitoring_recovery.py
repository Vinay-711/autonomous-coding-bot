"""
Unit and integration tests for production-grade monitoring and recovery system in AutoCoder.
Covers:
- Structured Cloud JSON logging with all required lifecycle attributes
- Sensitive data redaction (API keys, tokens, cookies, passwords, JWT, emails, session IDs)
- Persistent retry queue with exponential backoff, jitter, and retry exhaustion
- Failure classification into 7 typed FailureCodes
- Automatic retry limited strictly to transient failures
- Per-platform circuit breakers (CLOSED -> OPEN -> HALF_OPEN -> CLOSED)
- Stuck job detection via worker leases and heartbeat timestamps
- Safe expired lease reclamation by recovered workers
- Daily run summaries with tested_count and complete metrics
- Opt-in notification system on auth needed, quota missed, and run failure
- Notification failure resilience (webhook errors do not crash worker)
- Strict safeguard: unconfirmed submission never logged as ACCEPTED
- Duplicate delivery prevention and atomic claiming
- CLI commands: autocoder status --detailed and autocoder retry-failed --dry-run
"""

import time
import json
import pytest
import urllib.request
from unittest.mock import patch, MagicMock

from bot import (
    DatabaseManager,
    FailureCode,
    classify_failure,
    redact_sensitive_data,
    log_event,
    get_platform_limiter,
    CircuitState,
    NotificationEvent,
    NotificationManager,
    print_status_summary,
    handle_retry_failed,
    VerdictState,
    ProcessStage,
)

@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "monitor_test.db"
    return DatabaseManager(db_path=db_file, auto_migrate=False)


# ==============================================================================
# 1. Structured JSON Logging & Unconfirmed Submission Guard
# ==============================================================================
def test_structured_json_logging_fields(capsys):
    with patch("bot.STRUCTURED_LOGGING", True):
        log_event(
            level="INFO",
            message="Evaluating problem submission",
            run_id=42,
            problem_id="two-sum",
            platform="leetcode",
            attempt_id=101,
            state="SUBMITTED",
            duration=3.14159,
            failure_code=None,
        )
        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert payload["severity"] == "INFO"
        assert payload["run_id"] == 42
        assert payload["problem_id"] == "two-sum"
        assert payload["platform"] == "leetcode"
        assert payload["attempt_id"] == 101
        assert payload["state"] == "SUBMITTED"
        assert payload["duration"] == 3.142
        assert payload["component"] == "autocoder"
        assert "failure_code" not in payload


def test_structured_logging_with_failure_code(capsys):
    with patch("bot.STRUCTURED_LOGGING", True):
        log_event(
            level="ERROR",
            message="Network reset while scraping",
            run_id=7,
            problem_id="cf-1234a",
            platform="codeforces",
            attempt_id=12,
            state="GENERATED",
            duration=1.2,
            failure_code=FailureCode.TRANSIENT,
        )
        captured = capsys.readouterr()
        payload = json.loads(captured.err)

        assert payload["severity"] == "ERROR"
        assert payload["failure_code"] == "TRANSIENT"
        assert payload["state"] == "GENERATED"


def test_unconfirmed_submission_never_logged_as_accepted(capsys):
    """
    CRITICAL REQUIREMENT 11:
    Never log an unconfirmed submission as accepted.
    If unconfirmed=True, mode=run, or confirmed=False, state MUST be coerced from ACCEPTED to SUBMITTED.
    """
    with patch("bot.STRUCTURED_LOGGING", True):
        # Case A: unconfirmed flag set
        log_event("INFO", "Submission sent", state="ACCEPTED", unconfirmed=True)
        captured = capsys.readouterr()
        p1 = json.loads(captured.out)
        assert p1["state"] == "SUBMITTED"

        # Case B: run mode (testing only, no judge acceptance)
        log_event("INFO", "Local test run", state="ACCEPTED", mode="run")
        captured = capsys.readouterr()
        p2 = json.loads(captured.out)
        assert p2["state"] == "SUBMITTED"

        # Case C: save mode (save locally only)
        log_event("INFO", "Local save", state="ACCEPTED", mode="save")
        captured = capsys.readouterr()
        p3 = json.loads(captured.out)
        assert p3["state"] == "SUBMITTED"

        # Case D: confirmed=False
        log_event("INFO", "Unconfirmed judge ping", state="ACCEPTED", confirmed=False)
        captured = capsys.readouterr()
        p4 = json.loads(captured.out)
        assert p4["state"] == "SUBMITTED"

        # Case E: Confirmed accepted submission
        log_event("INFO", "Confirmed accepted", state="ACCEPTED", confirmed=True)
        captured = capsys.readouterr()
        p5 = json.loads(captured.out)
        assert p5["state"] == "ACCEPTED"


# ==============================================================================
# 2. Sensitive Data Redaction
# ==============================================================================
def test_comprehensive_sensitive_data_redaction():
    # 1. API Keys
    s1 = "API key is AIzaSyD9876543210abcdefghijklmnopq"
    assert redact_sensitive_data(s1) == "API key is [REDACTED_API_KEY]"
    assert redact_sensitive_data("api_key=AIzaSyD9876543210abcdefghijklmnopq") == "api_key=[REDACTED_API_KEY]"

    # 2. GitHub Tokens
    s2 = "Token ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    assert "[REDACTED_GITHUB_TOKEN]" in redact_sensitive_data(s2)
    assert "ghp_abcdef" not in redact_sensitive_data(s2)

    # 3. Bearer & JWT
    s3 = "Authorization: Bearer secret_bearer_token_xyz"
    assert redact_sensitive_data(s3) == "Authorization: Bearer [REDACTED_TOKEN]"

    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.doNotLeakThisSignatureValue"
    assert redact_sensitive_data(f"JWT: {jwt}") == "JWT: [REDACTED_JWT]"

    # 4. Cookies
    s4 = "Cookie: sessionid=abcd1234efgh; cf_clearance=cf9876xyz; connect.sid=sess555;"
    redacted = redact_sensitive_data(s4)
    assert "sessionid=[REDACTED_COOKIE]" in redacted
    assert "cf_clearance=[REDACTED_COOKIE]" in redacted
    assert "connect.sid=[REDACTED_COOKIE]" in redacted
    assert "abcd1234" not in redacted
    assert "cf9876xyz" not in redacted

    # 5. Passwords & Secrets
    s5 = "password: super_secret_pass_123; token='secret_token_abc'"
    redacted5 = redact_sensitive_data(s5)
    assert "[REDACTED_PASSWORD]" in redacted5
    assert "[REDACTED_SECRET]" in redacted5

    # 6. PII: Emails & Session Identifiers
    s6 = "User contact is developer@example.com with session_id=sess_998877665544"
    redacted6 = redact_sensitive_data(s6)
    assert "[REDACTED_EMAIL]" in redacted6
    assert "developer@example.com" not in redacted6
    assert "[REDACTED_SESSION]" in redacted6
    assert "sess_998877665544" not in redacted6


# ==============================================================================
# 3. Failure Classification (7 Typed Codes)
# ==============================================================================
def test_failure_classification():
    # RATE_LIMITED
    assert classify_failure("Rate limit reached. HTTP 429 Too Many Requests") == FailureCode.RATE_LIMITED
    assert classify_failure("", http_status=429) == FailureCode.RATE_LIMITED
    assert classify_failure("Quota exceeded: cooling down for 60s") == FailureCode.RATE_LIMITED

    # AUTH_REQUIRED
    assert classify_failure("Session expired, redirected to login") == FailureCode.AUTH_REQUIRED
    assert classify_failure("Cloudflare challenge / captcha encountered") == FailureCode.AUTH_REQUIRED
    assert classify_failure("", http_status=401) == FailureCode.AUTH_REQUIRED
    assert classify_failure("", http_status=403) == FailureCode.AUTH_REQUIRED

    # PLATFORM_CHANGED
    assert classify_failure("Locator for code editor failed, changed selector") == FailureCode.PLATFORM_CHANGED
    assert classify_failure("Editor injection verification failed: readback mismatch") == FailureCode.PLATFORM_CHANGED
    assert classify_failure("Wrong platform page detected") == FailureCode.PLATFORM_CHANGED

    # JUDGE_REJECTED
    assert classify_failure("Wrong Answer on testcase 5", verdict=VerdictState.REJECTED) == FailureCode.JUDGE_REJECTED
    assert classify_failure("Compilation error on judge", verdict=VerdictState.REJECTED) == FailureCode.JUDGE_REJECTED

    # PUBLISH_FAILED
    assert classify_failure("Git push failed: non-fast-forward", stage=ProcessStage.PUBLISH_FAILED) == FailureCode.PUBLISH_FAILED
    assert classify_failure("GitHub publish error: pull rebase conflict") == FailureCode.PUBLISH_FAILED

    # TRANSIENT
    assert classify_failure("Network timeout during fetch (180s)") == FailureCode.TRANSIENT
    assert classify_failure("Connection reset by peer (ECONNRESET)") == FailureCode.TRANSIENT
    assert classify_failure("CDP reconnection notice: target closed") == FailureCode.TRANSIENT
    assert classify_failure("Worker lease expired / heartbeat lost") == FailureCode.TRANSIENT
    assert classify_failure("", http_status=503) == FailureCode.TRANSIENT

    # PERMANENT
    assert classify_failure("Live rated contest active, submissions blocked") == FailureCode.PERMANENT
    assert classify_failure("Unsupported interactive problem") == FailureCode.PERMANENT
    assert classify_failure("Problem not found (404)") == FailureCode.PERMANENT


# ==============================================================================
# 4. Persistent Retry Queue: Backoff, Jitter & Retry Exhaustion
# ==============================================================================
def test_retry_queue_transient_only(temp_db):
    """REQUIREMENT 5: Retry only transient failures automatically."""
    temp_db.claim_problem("leetcode", "two-sum", "https://leetcode.com/problems/two-sum")
    prob_id = temp_db.get_problem_id("leetcode", "two-sum")

    # Non-transient failures MUST NOT be enqueued automatically
    assert temp_db.enqueue_retry(prob_id, "leetcode", "two-sum", "https://leetcode.com/problems/two-sum",
                                failure_code=FailureCode.JUDGE_REJECTED, error_msg="Wrong Answer") is False
    assert temp_db.enqueue_retry(prob_id, "leetcode", "two-sum", "https://leetcode.com/problems/two-sum",
                                failure_code=FailureCode.PERMANENT, error_msg="Contest blocked") is False
    assert temp_db.enqueue_retry(prob_id, "leetcode", "two-sum", "https://leetcode.com/problems/two-sum",
                                failure_code=FailureCode.AUTH_REQUIRED, error_msg="Login required") is False

    # Transient failure MUST be enqueued
    assert temp_db.enqueue_retry(prob_id, "leetcode", "two-sum", "https://leetcode.com/problems/two-sum",
                                failure_code=FailureCode.TRANSIENT, error_msg="Network timeout") is True

    items = temp_db.get_all_retry_queue_items()
    assert len(items) == 1
    assert items[0]["canonical_id"] == "two-sum"
    assert items[0]["retry_count"] == 1
    assert items[0]["status"] == "PENDING"


def test_retry_queue_exponential_backoff_and_exhaustion(temp_db):
    """Verify exponential backoff with jitter and retry limit exhaustion."""
    temp_db.claim_problem("codeforces", "1000A", "https://codeforces.com/problem/1000A")
    prob_id = temp_db.get_problem_id("codeforces", "1000A")

    # Attempt 1: base backoff ~30s
    temp_db.enqueue_retry(prob_id, "codeforces", "1000A", "https://codeforces.com/problem/1000A",
                          FailureCode.TRANSIENT, "Network drop 1", max_retries=3, base_backoff=30.0)
    it1 = temp_db.get_all_retry_queue_items()[0]
    assert it1["retry_count"] == 1
    assert 20.0 <= it1["backoff_seconds"] <= 40.0

    # Attempt 2: backoff ~60s
    temp_db.enqueue_retry(prob_id, "codeforces", "1000A", "https://codeforces.com/problem/1000A",
                          FailureCode.TRANSIENT, "Network drop 2", max_retries=3, base_backoff=30.0)
    it2 = temp_db.get_all_retry_queue_items()[0]
    assert it2["retry_count"] == 2
    assert 45.0 <= it2["backoff_seconds"] <= 75.0

    # Attempt 3: backoff ~120s
    temp_db.enqueue_retry(prob_id, "codeforces", "1000A", "https://codeforces.com/problem/1000A",
                          FailureCode.TRANSIENT, "Network drop 3", max_retries=3, base_backoff=30.0)
    it3 = temp_db.get_all_retry_queue_items()[0]
    assert it3["retry_count"] == 3
    assert it3["status"] == "PENDING"

    # Attempt 4: Max retries (3) reached -> Transitions to EXHAUSTED
    enqueued = temp_db.enqueue_retry(prob_id, "codeforces", "1000A", "https://codeforces.com/problem/1000A",
                                     FailureCode.TRANSIENT, "Network drop 4", max_retries=3, base_backoff=30.0)
    assert enqueued is False

    it4 = temp_db.get_all_retry_queue_items()[0]
    assert it4["status"] == "EXHAUSTED"


# ==============================================================================
# 5. Worker Crashes, Heartbeats & Safe Lease Reclamation
# ==============================================================================
def test_heartbeat_lease_expiration_and_recovery(temp_db):
    """
    REQUIREMENT 7 & 8:
    Detect a stuck job using leases and heartbeat timestamps.
    Allow a recovered worker to reclaim expired leases safely.
    """
    now = time.time()
    worker_dead = "worker_crashed_999"

    # Worker 1 claims problem
    assert temp_db.claim_problem("hackerrank", "sherlock-valid", "https://hackerrank.com/sherlock-valid",
                                worker_id=worker_dead, lease_timeout_sec=60.0) is True

    # Active heartbeat updated
    assert temp_db.record_heartbeat("hackerrank", "sherlock-valid", worker_id=worker_dead) is True

    # Another worker cannot claim it while heartbeat is fresh
    assert temp_db.claim_problem("hackerrank", "sherlock-valid", "https://hackerrank.com/sherlock-valid",
                                worker_id="worker_live_100", lease_timeout_sec=60.0) is False

    # Simulate worker crash: time passes 100 seconds past the 60s lease timeout
    past_time = now - 120.0
    with temp_db.get_connection() as conn:
        conn.execute(
            "UPDATE problems SET claimed_at = ?, heartbeat_at = ?, lease_timeout = 60.0 WHERE canonical_id = 'sherlock-valid'",
            (past_time, past_time)
        )

    # Recovered worker performs startup lease recovery
    recovered_count = temp_db.recover_expired_leases(lease_timeout_sec=60.0)
    assert recovered_count == 1

    # Verify problem was reset to TRANSIENT_FAILURE and queued in retry_queue
    with temp_db.get_connection() as conn:
        row = conn.execute("SELECT status, claimed_by, failure_reason FROM problems WHERE canonical_id = 'sherlock-valid'").fetchone()
        assert row[0] == "TRANSIENT_FAILURE"
        assert row[1] is None
        assert "stuck job recovered" in row[2]

    q_items = temp_db.get_all_retry_queue_items(platform="hackerrank")
    assert len(q_items) == 1
    assert q_items[0]["canonical_id"] == "sherlock-valid"
    assert q_items[0]["failure_code"] == "TRANSIENT"

    # Recovered worker claims the recovered problem safely
    assert temp_db.claim_problem("hackerrank", "sherlock-valid", "https://hackerrank.com/sherlock-valid",
                                worker_id="worker_live_100", lease_timeout_sec=60.0) is True


# ==============================================================================
# 6. Per-Platform Circuit Breakers
# ==============================================================================
@pytest.mark.asyncio
async def test_per_platform_circuit_breaker_lifecycle():
    limiter = get_platform_limiter("codechef")
    limiter.reset()
    assert limiter.state == CircuitState.CLOSED
    assert limiter.is_paused() is False

    # Simulate 3 consecutive failures
    limiter.record_failure(is_rate_limit=False, reason="HTTP 500 error")
    assert limiter.state == CircuitState.CLOSED
    limiter.record_failure(is_rate_limit=False, reason="HTTP 500 error")
    assert limiter.state == CircuitState.CLOSED

    # 3rd failure trips the circuit
    limiter.record_failure(is_rate_limit=False, reason="HTTP 500 error")
    assert limiter.state == CircuitState.OPEN
    assert limiter.is_paused() is True

    # Acquisition fails while OPEN
    ok, err = await limiter.acquire()
    assert ok is False
    assert "Circuit breaker OPEN" in err

    # Check status diagnostics
    status = limiter.get_status()
    assert status["platform"] == "codechef"
    assert status["state"] == "OPEN"
    assert status["consecutive_failures"] == 3

    # Fast-forward open cooldown
    limiter.circuit_open_until = time.time() - 1.0
    assert limiter.state == CircuitState.HALF_OPEN

    # Success in HALF_OPEN resets circuit
    limiter.record_success()
    assert limiter.state == CircuitState.CLOSED
    assert limiter.consecutive_failures == 0


def test_circuit_breaker_manual_trip_and_reset():
    limiter = get_platform_limiter("hackerearth")
    limiter.reset()

    # Manual trip when anti-bot challenge is detected
    limiter.trip(reason="Cloudflare challenge detected", duration=120.0)
    assert limiter.state == CircuitState.OPEN
    assert limiter.is_paused() is True
    assert limiter.last_failure_reason == "Cloudflare challenge detected"

    # Operator reset
    limiter.reset()
    assert limiter.state == CircuitState.CLOSED
    assert limiter.is_paused() is False


# ==============================================================================
# 7. Notification System & Failure Resilience
# ==============================================================================
def test_notification_opt_in_and_triggers():
    # 1. Opt-out by default when not configured
    mgr_disabled = NotificationManager(enabled=False)
    assert mgr_disabled.send_notification(NotificationEvent.AUTH_REQUIRED, "Title", "Msg") is False

    # 2. Console notification when enabled
    mgr_console = NotificationManager(channel="console", enabled=True)
    assert mgr_console.send_notification(NotificationEvent.QUOTA_MISSED, "Quota Shortfall", "Solved 3/5", platform="leetcode") is True

    # 3. Webhook notification with sensitive data redaction
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        mgr_webhook = NotificationManager(webhook_url="https://hooks.slack.com/services/test/webhook", channel="slack", enabled=True)
        ok = mgr_webhook.send_notification(
            event=NotificationEvent.AUTH_REQUIRED,
            title="Login Required for LeetCode",
            message="Cookie sessionid=secret_cookie_123 expired with token ghp_secretpassword12345678901234567890",
            platform="leetcode",
            details={"api_key": "AIzaSyD9876543210abcdefghijklmnopq"}
        )
        assert ok is True
        mock_urlopen.assert_called_once()
        req_arg = mock_urlopen.call_args[0][0]
        posted_body = json.loads(req_arg.data.decode("utf-8"))
        body_str = json.dumps(posted_body)

        # Redaction verification: no secrets leaked in notification payload
        assert "secret_cookie_123" not in body_str
        assert "ghp_secretpassword" not in body_str
        assert "AIzaSy" not in body_str
        assert "[REDACTED_COOKIE]" in body_str
        assert "[REDACTED_GITHUB_TOKEN]" in body_str
        assert "[REDACTED_API_KEY]" in body_str


def test_notification_failure_resilience(capsys):
    """Verify that external notification HTTP errors NEVER crash the autonomous worker."""
    with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("http://bad.url", 500, "Internal Server Error", {}, None)):
        mgr = NotificationManager(webhook_url="https://hooks.example.com/bad", channel="webhook", enabled=True)
        # Must return False and log a warning, NOT raise an exception
        ok = mgr.send_notification(NotificationEvent.RUN_FAILED, "Batch Failure", "Fatal error occurred")
        assert ok is False


# ==============================================================================
# 8. Daily Run Summaries with All 7 Metrics (including tested_count)
# ==============================================================================
def test_daily_run_summary_tracks_all_metrics(temp_db):
    run_id = temp_db.get_or_create_daily_run("leetcode", target_quota=5, run_date="2026-11-12")
    temp_db.increment_daily_run_counts(
        run_id=run_id,
        attempted=4,
        generated=3,
        compiled=3,
        tested=3,
        accepted=2,
        published=2,
        failed=1,
        skipped=1,
    )

    summary = temp_db.get_daily_summary("2026-11-12")
    assert len(summary) == 1
    row = summary[0]
    assert row["platform"] == "leetcode"
    assert row["target_quota"] == 5
    assert row["attempted_count"] == 4
    assert row["generated_count"] == 3
    assert row["compiled_count"] == 3
    assert row["tested_count"] == 3
    assert row["accepted_count"] == 2
    assert row["published_count"] == 2
    assert row["failed_count"] == 1
    assert row["skipped_count"] == 1


# ==============================================================================
# 9. CLI Commands: status --detailed and retry-failed --dry-run
# ==============================================================================
def test_cli_status_detailed_output(capsys, temp_db):
    run_id = temp_db.get_or_create_daily_run("leetcode", target_quota=5)
    temp_db.increment_daily_run_counts(run_id, attempted=2, generated=2, compiled=2, tested=2, accepted=1, published=1, failed=1)

    # Insert a transient failure into retry queue
    temp_db.claim_problem("leetcode", "two-sum", "https://leetcode.com/problems/two-sum")
    prob_id = temp_db.get_problem_id("leetcode", "two-sum")
    temp_db.enqueue_retry(prob_id, "leetcode", "two-sum", "https://leetcode.com/problems/two-sum",
                          FailureCode.TRANSIENT, "Network timeout during testing")

    print_status_summary(detailed=True, db=temp_db)
    captured = capsys.readouterr().out

    assert "DETAILED MONITORING & RECOVERY METRICS (--detailed)" in captured
    assert "Failure Classification Breakdown:" in captured
    assert "Per-Platform Circuit Breakers:" in captured
    assert "Persistent Retry Queue (1 items):" in captured
    assert "two-sum" in captured
    assert "TRANSIENT" in captured


@pytest.mark.asyncio
async def test_cli_retry_failed_dry_run(capsys, temp_db):
    temp_db.claim_problem("codeforces", "2000B", "https://codeforces.com/problem/2000B")
    prob_id = temp_db.get_problem_id("codeforces", "2000B")
    temp_db.enqueue_retry(prob_id, "codeforces", "2000B", "https://codeforces.com/problem/2000B",
                          FailureCode.TRANSIENT, "ECONNRESET from Codeforces API")

    eligible_count = await handle_retry_failed(dry_run=True, db=temp_db)
    assert eligible_count == 1

    captured = capsys.readouterr().out
    assert "DRY-RUN SIMULATION" in captured
    assert "2000B" in captured
    assert "TRANSIENT" in captured
    assert "No problems were executed and database state was not modified" in captured

    # Verify queue status remains PENDING in dry-run
    items = temp_db.get_all_retry_queue_items()
    assert items[0]["status"] == "PENDING"

