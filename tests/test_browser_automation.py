"""
Test Suite for Browser Automation Reliability and Anti-Bot Safeguards.
Verifies adapters, authentication checks, URL validation, event-based waits,
injection verification, stale selector handling, CAPTCHA/challenge detection,
rate limiting/circuit breakers, contest guards, and sensitive data redaction.
"""

import time
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot import (
    AuthState,
    AuthStateResult,
    SecurityChallengeDetection,
    InjectionResult,
    ProblemExtractionResult,
    SelectorResolutionResult,
    redact_sensitive_data,
    CircuitState,
    PlatformRateLimiter,
    get_rate_limiter,
    detect_security_challenge_or_captcha,
    check_live_or_rated_contest,
    resolve_verified_selector,
    BrowserManager,
    BasePlatformAdapter,
    LeetCodeAdapter,
    CodeforcesAdapter,
    HackerRankAdapter,
    CodeChefAdapter,
    HackerEarthAdapter,
    GenericPlatformAdapter,
    get_platform_adapter,
    process_problem,
    ProcessResult,
    ProcessStage,
    SubmissionVerdict,
    VerdictState,
    extract_problem_text,
    extract_editor_code,
    inject_code_into_editor,
    ensure_live_page,
    DatabaseManager,
    SolvedRegistry,
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Provides a fresh isolated database for every test."""
    test_db_path = tmp_path / "browser_test.db"
    test_db = DatabaseManager(db_path=test_db_path, auto_migrate=False)
    old_db = SolvedRegistry._db
    SolvedRegistry.set_db(test_db)
    yield test_db
    SolvedRegistry.set_db(old_db)


class MockElementLocator:
    """Mock locator for testing DOM element interactions and selector resolution."""
    def __init__(self, visible=True, text="", count=1, raise_error=None):
        self._visible = visible
        self._text = text
        self._count = count
        self._raise_error = raise_error
        self.clicked = False

    @property
    def first(self):
        return self

    async def is_visible(self):
        if self._raise_error:
            raise self._raise_error
        return self._visible

    async def count(self):
        return self._count

    async def inner_text(self):
        if self._raise_error:
            raise self._raise_error
        return self._text

    async def click(self):
        self.clicked = True

    async def wait_for(self, *args, **kwargs):
        if not self._visible or self._raise_error:
            raise TimeoutError("Element not visible")

    def locator(self, *args, **kwargs):
        return self

    async def all(self):
        return [self] if self._count > 0 else []


class MockBrowserPage:
    """Mock Playwright Page for simulating varied DOM states and navigation behaviors."""
    def __init__(self, url="https://leetcode.com/problems/two-sum", eval_return="", eval_dict=None, redirect_url=None):
        self.url = url
        self.redirect_url = redirect_url
        self.eval_return = eval_return
        self.eval_dict = eval_dict or {}
        self._anti_bot_blocked = False
        self._is_closed = False
        self.locators = {}
        self.goto_calls = []
        self.keyboard = AsyncMock()

    def is_closed(self):
        return self._is_closed

    async def goto(self, url, *args, **kwargs):
        self.goto_calls.append(url)
        self.url = self.redirect_url if self.redirect_url else url

    async def evaluate(self, script, *args, **kwargs):
        if callable(self.eval_return):
            return self.eval_return(script)
        for pattern, res in self.eval_dict.items():
            if pattern in script:
                return res
        return self.eval_return

    def locator(self, selector, *args, **kwargs):
        if selector in self.locators:
            return self.locators[selector]
        return MockElementLocator(visible=True, text="", count=1)


# ==============================================================================
# 1. Expired Session & Authentication State Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_expired_session():
    """Detects expired session / logged-out auth state and halts before submitting."""
    adapter = get_platform_adapter("leetcode")
    page = MockBrowserPage(
        url="https://leetcode.com/problems/two-sum",
        eval_dict={
            "UserAvatar": "LOGGED_OUT"
        }
    )

    auth = await adapter.check_auth_state(page)
    assert auth.state == AuthState.LOGGED_OUT
    assert auth.requires_human is True
    assert "login" in auth.details.lower()

    # In submit mode, process_problem must refuse submission
    with patch("bot.switch_editor_to_java"), \
         patch("bot.extract_problem_text", return_value="Given an array of integers... Input: nums = [2,7,11,15]"), \
         patch("bot.extract_editor_code", return_value="class Solution {}"), \
         patch("bot.verify_and_repair_java_pipeline") as mock_pipeline:
        res = await process_problem(page, "https://leetcode.com/problems/two-sum", mode="submit")
        assert res.success is False
        assert "Authentication required" in res.error
        mock_pipeline.assert_not_called()


# ==============================================================================
# 2. Missing Editor Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_missing_editor():
    """Handles missing Monaco/Ace/textarea editor gracefully and reports failure without crash."""
    page = MockBrowserPage(
        url="https://leetcode.com/problems/two-sum",
        eval_return=""  # All window.monaco / ace queries return empty / null
    )
    # Ensure all locator lookups return not visible / 0 count
    page.locator = lambda sel, *args, **kwargs: MockElementLocator(visible=False, count=0)

    # 1. extract_editor_code returns empty string
    starter = await extract_editor_code(page)
    assert starter == ""

    # 2. inject_code_into_editor returns InjectionResult with success=False and verified=False
    with patch("pyperclip.copy", side_effect=Exception("No clipboard")):
        inj = await inject_code_into_editor(page, "public class Solution {}")
        assert bool(inj) is False
        assert inj.success is False
        assert inj.verified is False
        assert "No supported editor found" in inj.error


# ==============================================================================
# 3. Changed Selector & Stale Selector Diagnostics Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_changed_selector():
    """Primary selector fails, stale locator warning is recorded, and verified fallback is used."""
    page = MockBrowserPage(url="https://leetcode.com/problems/two-sum")

    # Primary selectors fail
    page.locators["div[data-track-load='description_content']"] = MockElementLocator(count=0)
    page.locators["div.elfjS"] = MockElementLocator(count=0)
    # Verified fallback selector matches valid CP problem statement
    valid_cp_text = "Given an array of integers nums and an integer target, return indices. Input: nums=[2,7,11,15], target=9. Output: [0,1]. Constraints: 2 <= nums.length <= 104."
    page.locators["article"] = MockElementLocator(visible=True, text=valid_cp_text, count=1)

    desc = await extract_problem_text(page, ["div[data-track-load='description_content']", "div.elfjS"])
    assert desc == valid_cp_text

    # Test that an unverified fallback (e.g. navbar or footer) is rejected
    unverified_text = "Home | Problems | Contest | Discuss | Copyright 2026"
    page.locators["article"] = MockElementLocator(visible=True, text=unverified_text, count=1)
    desc_rejected = await extract_problem_text(page, ["div[data-track-load='description_content']"])
    assert desc_rejected == ""


# ==============================================================================
# 4. Failed Code Injection & Readback Verification Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_failed_code_injection():
    """Injection seems to occur but readback mismatch or empty aborts submission immediately."""
    page = MockBrowserPage(url="https://leetcode.com/problems/two-sum")

    solution_code = "public class Solution { public int[] twoSum(int[] nums, int target) { return new int[0]; } }"

    # Simulate injection claiming success via monaco_api, but editor readback returning empty
    with patch.object(page, "evaluate", side_effect=lambda js, *args: "monaco_api" if "setValue" in js else ""):
        inj = await inject_code_into_editor(page, solution_code)
        assert bool(inj) is False
        assert inj.verified is False
        assert "empty" in inj.error.lower()

    # Test readback containing completely different class name
    with patch.object(page, "evaluate", side_effect=lambda js, *args: "monaco_api" if "setValue" in js else "class StaleOldSolution {}"):
        inj2 = await inject_code_into_editor(page, solution_code)
        assert bool(inj2) is False
        assert inj2.verified is False
        assert "Expected class 'Solution' missing" in inj2.error

    # In process_problem, failed injection must immediately abort submission
    with patch("bot.switch_editor_to_java"), \
         patch("bot.extract_problem_text", return_value="Given nums and target... Constraints: 2 <= nums.length <= 104"), \
         patch("bot.extract_editor_code", return_value="class Solution {}"), \
         patch("bot.inject_code_into_editor", return_value=InjectionResult(success=True, verified=False, error="Readback mismatch")), \
         patch("bot.verify_live_submission_verdict") as mock_judge:
        res = await process_problem(page, "https://leetcode.com/problems/two-sum", mode="submit")
        assert res.success is False
        assert res.stage == ProcessStage.GENERATED
        assert "Editor injection verification failed" in res.error
        mock_judge.assert_not_called()


# ==============================================================================
# 5. CAPTCHA / Security Challenge Encountered Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_captcha_challenge_encountered():
    """Detects Cloudflare Turnstile / IUAM challenge, pauses platform, requests human action, no bypass."""
    page = MockBrowserPage(
        url="https://codeforces.com/problemset/problem/1294/C",
        eval_dict={
            "turnstileToken": {"detected": True, "type": "cloudflare_turnstile", "details": "Cloudflare Turnstile challenge detected"}
        }
    )

    det = await detect_security_challenge_or_captcha(page)
    assert det.detected is True
    assert det.challenge_type == "cloudflare_turnstile"

    # process_problem must halt and pause the platform without attempting to solve
    limiter = get_rate_limiter("codeforces")
    limiter.record_success()  # reset
    assert limiter.consecutive_failures == 0

    res = await process_problem(page, "https://codeforces.com/problemset/problem/1294/C", mode="submit")
    assert res.success is False
    assert "Security challenge detected" in res.error
    assert page._anti_bot_blocked is True
    # Rate limiter recorded the failure
    assert limiter.consecutive_failures >= 1


# ==============================================================================
# 6. HTTP 429 & Rate Limiter / Circuit Breaker Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_http_429():
    """HTTP 429 response parses Retry-After, trips circuit breaker, and enters exponential backoff."""
    limiter = PlatformRateLimiter("codechef", min_interval=0.1, failure_threshold=2, base_open_duration=5.0)

    # 1. Simulate 429 with Retry-After: 10
    limiter.handle_http_response(429, {"Retry-After": "10"})
    assert limiter.is_paused() is True
    assert limiter.consecutive_failures == 1

    ok, reason = await limiter.acquire()
    assert ok is False
    assert "Rate limited on codechef" in reason
    assert "Retry-After active" in reason

    # 2. Simulate second failure reaching threshold -> trips circuit breaker
    limiter.handle_http_response(429, {"Retry-After": "5"})
    assert limiter.circuit_state == CircuitState.OPEN

    ok, reason = await limiter.acquire()
    assert ok is False
    assert "Circuit breaker OPEN" in reason

    # 3. Success resets breaker
    limiter.record_success()
    assert limiter.circuit_state == CircuitState.CLOSED
    assert limiter.consecutive_failures == 0


# ==============================================================================
# 7. Browser Disconnected & Health Check Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_browser_disconnected():
    """Browser CDP connection lost; BrowserManager detects unhealthy page and attempts reconnection."""
    dead_page = MockBrowserPage()
    dead_page._is_closed = True

    # 1. Closed page is unhealthy
    assert await BrowserManager.is_healthy(dead_page) is False

    # 2. Responsive page is healthy
    live_page = MockBrowserPage(eval_return=2)
    assert await BrowserManager.is_healthy(live_page) is True

    # 3. Exception in evaluate is detected as unhealthy
    failing_page = MockBrowserPage(eval_return=lambda s: (_ for _ in ()).throw(RuntimeError("Target closed")))
    assert await BrowserManager.is_healthy(failing_page) is False

    # 4. ensure_live_page calls reconnect when page is unhealthy
    mock_p = MagicMock()
    mock_browser = AsyncMock()
    mock_context = AsyncMock()
    mock_new_page = MockBrowserPage(eval_return=2)
    mock_context.new_page = AsyncMock(return_value=mock_new_page)
    mock_browser.contexts = [mock_context]
    mock_p.chromium.connect_over_cdp = AsyncMock(return_value=mock_browser)

    b, c, p = await ensure_live_page(dead_page, p_playwright=mock_p, browser=None, context=None)
    assert p == mock_new_page
    mock_p.chromium.connect_over_cdp.assert_called_once_with("http://localhost:9222")


# ==============================================================================
# 8. Login Redirected Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_login_redirected():
    """Navigating to problem URL redirects to login/auth wall; validates URL and aborts."""
    adapter = get_platform_adapter("leetcode")
    page = MockBrowserPage(
        url="https://leetcode.com/problems/two-sum",
        redirect_url="https://leetcode.com/accounts/login/?next=/problems/two-sum"
    )

    # Pre-navigation URL check when at redirect page
    redirected_page = MockBrowserPage(url="https://leetcode.com/accounts/login/?next=/problems/two-sum")
    valid, err = await adapter.validate_url_and_platform(redirected_page, "https://leetcode.com/problems/two-sum")
    assert valid is False
    assert "login/auth barrier" in err

    # In process_problem, navigation lands on redirect_url and aborts before extracting or submitting
    with patch("bot.switch_editor_to_java") as mock_lang:
        res = await process_problem(page, "https://leetcode.com/problems/two-sum", mode="submit")
        assert res.success is False
        assert "login/auth barrier" in res.error
        mock_lang.assert_not_called()


# ==============================================================================
# 9. Network Timeout Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_network_timeout():
    """Page navigation times out; caught cleanly and returned as failure result."""
    page = MockBrowserPage()
    page.goto = AsyncMock(side_effect=TimeoutError("Navigation timeout of 45000ms exceeded"))

    res = await process_problem(page, "https://leetcode.com/problems/two-sum", mode="run")
    assert res.success is False
    assert res.stage == ProcessStage.GENERATED
    assert "Navigation timeout" in res.error


# ==============================================================================
# 10. Wrong Platform Page Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_wrong_platform_page():
    """Navigating to wrong platform domain or 404 page is rejected by URL validation."""
    adapter = get_platform_adapter("leetcode")

    # Domain mismatch
    wrong_domain_page = MockBrowserPage(url="https://codeforces.com/problemset/problem/1294/C")
    valid, err = await adapter.validate_url_and_platform(wrong_domain_page, "https://leetcode.com/problems/two-sum")
    assert valid is False
    assert "Wrong platform domain" in err

    # 404 page
    not_found_page = MockBrowserPage(url="https://leetcode.com/404")
    valid404, err404 = await adapter.validate_url_and_platform(not_found_page, "https://leetcode.com/problems/two-sum")
    assert valid404 is False
    assert "404" in err404


# ==============================================================================
# 11. Live Rated Contest Guard Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_live_rated_contest_blocked():
    """Never automatically submits into active or live rated contests."""
    # 1. URL-based check
    contest_url = "https://leetcode.com/contest/weekly-contest-380/problems/find-maximum-number-of-elements-in-subset"
    adapter = get_platform_adapter(contest_url)
    page = MockBrowserPage(url=contest_url)

    is_contest, reason = await adapter.is_live_or_rated_contest(page, contest_url)
    assert is_contest is True
    assert "weekly/biweekly contest" in reason

    # 2. DOM-based timer check
    timer_page = MockBrowserPage(
        url="https://codeforces.com/contest/1900/problem/A",
        eval_dict={
            "contest-timer": {"is_live": True, "reason": "Active countdown timer detected: 01:45:22"}
        }
    )
    is_live, msg = await check_live_or_rated_contest(timer_page, timer_page.url, "codeforces")
    assert is_live is True
    assert "Active countdown timer" in msg

    # In process_problem, submission is strictly refused
    res = await process_problem(page, contest_url, mode="submit")
    assert res.success is False
    assert "Refusing submission into active/rated contest" in res.error


# ==============================================================================
# 12. Sensitive Data Redaction Tests
# ==============================================================================
def test_sensitive_data_redaction():
    """Sensitive API keys, tokens, cookies, and passwords are fully redacted from failure evidence."""
    raw_text = (
        "Error loading page with key AIzaSyD1234567890abcdef1234567890abcde; "
        "Authorization: Bearer secret_bearer_token_xyz.123-abc; "
        "Cookie: sessionid=my_secret_session_id_999; csrftoken=csrf_tok_888; "
        "Credentials: password='super_secret_password_!' and pwd=pass123"
    )

    redacted = redact_sensitive_data(raw_text)
    assert "[REDACTED_API_KEY]" in redacted
    assert "AIzaSyD" not in redacted

    assert "[REDACTED_TOKEN]" in redacted
    assert "secret_bearer_token" not in redacted

    assert "[REDACTED_COOKIE]" in redacted
    assert "my_secret_session_id" not in redacted
    assert "csrf_tok_888" not in redacted

    assert "[REDACTED_PASSWORD]" in redacted
    assert "super_secret_password" not in redacted
    assert "pass123" not in redacted
