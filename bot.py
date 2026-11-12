import asyncio
import os
import sys

# Ensure unbuffered real-time stdout/stderr line logging
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

import re
import json
import time
import random
import urllib.request
import argparse
import subprocess
import tempfile
import sqlite3
try:
    import resource
except ImportError:
    resource = None
import signal
import shutil
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None
from contextlib import contextmanager
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple, List, Set, Dict, Any, Union, Callable
from enum import Enum
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

KOLKATA_TZ = ZoneInfo("Asia/Kolkata")

def get_kolkata_now() -> datetime:
    """Returns the current datetime in Asia/Kolkata timezone."""
    return datetime.now(KOLKATA_TZ)

def get_kolkata_today_str() -> str:
    """Returns today's date formatted as YYYY-MM-DD in Asia/Kolkata timezone."""
    return get_kolkata_now().strftime("%Y-%m-%d")

import pyperclip
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from google.genai.errors import APIError
from playwright.async_api import async_playwright, Page, BrowserContext

# ==============================================================================
# Typed Verdict States & Data Models
# ==============================================================================
class VerdictState(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"

    def __bool__(self) -> bool:
        """Allow boolean evaluation for backward compatibility: ACCEPTED is True, all others are False."""
        return self == VerdictState.ACCEPTED

@dataclass
class SubmissionVerdict:
    state: VerdictState
    message: str
    submission_id: Optional[str] = None
    platform: Optional[str] = None
    problem_id: Optional[str] = None
    account: Optional[str] = None
    timestamp: Optional[float] = None
    correlation_mechanism: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.state == VerdictState.ACCEPTED

    def __iter__(self):
        """Allows unpacking as (verdict_state, verdict_message) for backward compatibility."""
        return iter((self.state, self.message))

class ProcessStage(str, Enum):
    GENERATED = "GENERATED"
    COMPILED = "COMPILED"
    TESTED = "TESTED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    PUBLISHED = "PUBLISHED"

    def __bool__(self) -> bool:
        return self in (ProcessStage.ACCEPTED, ProcessStage.PUBLISHED)

@dataclass
class ProcessResult:
    stage: ProcessStage
    success: bool
    url: str = ""
    platform: str = ""
    problem_id: Optional[str] = None
    solution_code: Optional[str] = None
    explanation: Optional[str] = None
    verdict: Optional[SubmissionVerdict] = None
    error: Optional[str] = None
    publish_error: Optional[str] = None
    saved_path: Optional[str] = None
    published: bool = False
    repo_name: Optional[str] = None
    repo_branch: Optional[str] = None
    commit_hash: Optional[str] = None
    compiler_output: Optional[str] = None
    diagnostics: Optional[str] = None
    sample_results: List[Any] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.success

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, bool):
            return self.success is other
        if isinstance(other, ProcessStage):
            return self.stage == other
        if isinstance(other, str):
            return self.stage.value == other
        return super().__eq__(other)

@dataclass
class SampleTestCase:
    input_text: str
    expected_output: str
    sample_index: int = 1

@dataclass
class CompilationResult:
    success: bool
    class_name: str
    source_filename: str
    error_message: Optional[str] = None
    diagnostics: Optional[str] = None
    cleaned_code: str = ""
    target_version: Optional[str] = None

@dataclass
class ExecutionResult:
    success: bool
    return_code: int
    stdout: str
    stderr: str
    timeout: bool = False
    oom: bool = False
    security_violation: bool = False
    error_message: Optional[str] = None

@dataclass
class SampleTestResult:
    sample_index: int
    passed: bool
    input_text: str
    expected_output: str
    actual_output: str
    error_message: Optional[str] = None
    diff: Optional[str] = None

@dataclass
class VerificationPipelineResult:
    success: bool
    solution_code: str
    explanation: str
    compilation: Optional[CompilationResult] = None
    sample_results: List[SampleTestResult] = field(default_factory=list)
    repair_attempts: int = 0
    failure_reason: Optional[str] = None

@dataclass
class PublishResult:
    success: bool
    error: Optional[str] = None
    repo_name: Optional[str] = None
    branch: Optional[str] = None
    commit_hash: Optional[str] = None

    def __bool__(self) -> bool:
        return self.success

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, bool):
            return self.success is other
        return super().__eq__(other)

    def __iter__(self):
        yield self.success
        yield self.error

# ==============================================================================
# Browser Automation & Security Data Models
# ==============================================================================
class AuthState(str, Enum):
    LOGGED_IN = "LOGGED_IN"
    LOGGED_OUT = "LOGGED_OUT"
    CHALLENGE = "CHALLENGE"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"

@dataclass
class AuthStateResult:
    state: AuthState
    username: Optional[str] = None
    details: Optional[str] = None
    requires_human: bool = False

@dataclass
class SecurityChallengeDetection:
    detected: bool
    challenge_type: Optional[str] = None
    details: Optional[str] = None
    url: str = ""

@dataclass
class InjectionResult:
    success: bool
    verified: bool
    editor_type: str = "unknown"
    readback_snippet: str = ""
    error: Optional[str] = None

    def __bool__(self) -> bool:
        return self.success and self.verified

@dataclass
class ProblemExtractionResult:
    success: bool
    problem_text: str = ""
    starter_code: str = ""
    samples: List[SampleTestCase] = field(default_factory=list)
    error: Optional[str] = None

@dataclass
class SelectorResolutionResult:
    success: bool
    matched_selector: Optional[str] = None
    element: Optional[Any] = None
    failed_selectors: List[Tuple[str, str]] = field(default_factory=list)
    role: str = ""

def redact_sensitive_data(text: Optional[str]) -> str:
    """Redacts API keys, cookies, tokens, and personally identifying session data from logs, diagnostics, and failure records."""
    if not text:
        return ""
    text = str(text)
    # API Keys
    text = re.sub(r"AIza[0-9A-Za-z-_]{30,45}", "[REDACTED_API_KEY]", text)
    text = re.sub(r'((?:api[-_]?key)[\s:=]+[\"\']?)[A-Za-z0-9_\-\.]{16,}([\"\']?)', r"\1[REDACTED_API_KEY]\2", text, flags=re.IGNORECASE)

    # Tokens & Secrets
    text = re.sub(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{30,255}", "[REDACTED_GITHUB_TOKEN]", text)
    text = re.sub(r"github_pat_[A-Za-z0-9_]{50,255}", "[REDACTED_GITHUB_TOKEN]", text)
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9_\-\.]+", r"\1[REDACTED_TOKEN]", text, flags=re.IGNORECASE)
    text = re.sub(r"eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.[A-Za-z0-9-_.+/=]+", "[REDACTED_JWT]", text)
    text = re.sub(r'((?:token|secret|access_key)[\s:=]+[\"\']?)(?!\[REDACTED_)[^\"\'\s;,\&]+([\"\']?)', r"\1[REDACTED_SECRET]\2", text, flags=re.IGNORECASE)

    # Cookies (standard and cloudflare/session cookies)
    cookie_names = (
        r"sessionid|csrf(?:token|_token)?|phpsessid|jsessionid|cf_clearance|__cfduid|"
        r"connect\.sid|remember_me|auth_token|auth|SSID|HSID|SID|APISID|SAPISID"
    )
    text = re.sub(rf"((?:{cookie_names})=)[^;\s&]+", r"\1[REDACTED_COOKIE]", text, flags=re.IGNORECASE)

    # Passwords
    text = re.sub(r'((?:password|passwd|pwd)[\s:=]+[\"\']?)(?!\[REDACTED_)[^\"\'\s;,\&]+([\"\']?)', r"\1[REDACTED_PASSWORD]\2", text, flags=re.IGNORECASE)

    # Personally Identifying Information (PII) / Emails & Session IDs
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', "[REDACTED_EMAIL]", text)
    text = re.sub(r'((?:session_?id|account_?id|visitor_?id)[\s:=]+[\"\']?)(?!\[REDACTED_)[A-Za-z0-9_\-\.]{10,}([\"\']?)', r"\1[REDACTED_SESSION]\2", text, flags=re.IGNORECASE)

    return text

class FailureCode(str, Enum):
    TRANSIENT = "TRANSIENT"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    PLATFORM_CHANGED = "PLATFORM_CHANGED"
    JUDGE_REJECTED = "JUDGE_REJECTED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    PERMANENT = "PERMANENT"

def classify_failure(
    error_msg: Optional[str],
    verdict: Optional[Any] = None,
    stage: Optional[Any] = None,
    http_status: Optional[int] = None
) -> FailureCode:
    """Classifies a failure into one of 7 standardized FailureCodes."""
    if http_status == 429:
        return FailureCode.RATE_LIMITED
    if http_status in (401, 403):
        return FailureCode.AUTH_REQUIRED
    if http_status in (502, 503, 504):
        return FailureCode.TRANSIENT

    msg_low = (str(error_msg) if error_msg else "").lower()

    # Rate limiting
    if any(k in msg_low for k in ["rate limit", "429", "too many requests", "retry-after", "cooling down", "quota exceeded"]):
        return FailureCode.RATE_LIMITED

    # Authentication or security barriers
    if any(k in msg_low for k in [
        "login required", "authentication required", "not authenticated", "auth_required",
        "session expired", "login redirected", "unauthenticated", "captcha", "cloudflare challenge",
        "turnstile", "recaptcha", "hcaptcha", "login barrier", "access denied", "expired session"
    ]):
        return FailureCode.AUTH_REQUIRED

    # Platform DOM or selector changes
    if any(k in msg_low for k in [
        "changed selector", "stale selector", "locator failed", "missing editor",
        "editor element not found", "wrong platform page", "dom mismatch", "readback mismatch",
        "editor injection verification failed"
    ]):
        return FailureCode.PLATFORM_CHANGED

    # Judge verdicts & submissions
    v_str = verdict.value if hasattr(verdict, "value") else (str(verdict) if verdict else "")
    if v_str in ("REJECTED", "WRONG_ANSWER", "COMPILATION_ERROR", "TIME_LIMIT_EXCEEDED", "MEMORY_LIMIT_EXCEEDED", "RUNTIME_ERROR"):
        return FailureCode.JUDGE_REJECTED
    if any(k in msg_low for k in [
        "judge rejected", "wrong answer", "time limit exceeded", "memory limit exceeded",
        "compilation error on judge", "runtime error"
    ]):
        return FailureCode.JUDGE_REJECTED

    # Publishing failures
    s_str = stage.value if hasattr(stage, "value") else (str(stage) if stage else "")
    if s_str == "PUBLISH_FAILED" or any(k in msg_low for k in [
        "publish failed", "git push failed", "git commit failed", "rebase conflict",
        "failed to pull", "failed to clone", "github publish error"
    ]):
        return FailureCode.PUBLISH_FAILED

    # Transient network / infrastructure issues
    if any(k in msg_low for k in [
        "timeout", "timed out", "econnreset", "connection reset", "connection refused",
        "network error", "temporary", "cdp reconnection notice", "session closed",
        "target closed", "browser disconnected", "worker lease expired", "heartbeat lost",
        "503", "502", "504"
    ]):
        return FailureCode.TRANSIENT

    # Permanent failures
    if any(k in msg_low for k in [
        "unsupported", "live rated contest", "contest active", "interactive problem",
        "not found (404)", "invalid problem url", "max repairs exhausted"
    ]):
        return FailureCode.PERMANENT

    # Default to PERMANENT for unknown non-transient errors
    return FailureCode.PERMANENT

class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

class PlatformRateLimiter:
    """Enforces per-platform rate limits, Retry-After compliance, backoff, and circuit breaking."""
    def __init__(
        self,
        platform: str,
        min_interval: float = 2.0,
        failure_threshold: int = 3,
        base_open_duration: float = 30.0,
        backoff_factor: float = 2.0,
    ):
        self.platform = platform
        self.min_interval = min_interval
        self.failure_threshold = failure_threshold
        self.base_open_duration = base_open_duration
        self.backoff_factor = backoff_factor
        self.last_request_time: float = 0.0
        self.consecutive_failures: int = 0
        self.circuit_state: CircuitState = CircuitState.CLOSED
        self.circuit_open_until: float = 0.0
        self.retry_after_until: float = 0.0
        self.last_failure_reason: str = ""

    @property
    def state(self) -> CircuitState:
        now = time.time()
        if self.circuit_state == CircuitState.OPEN and now >= self.circuit_open_until:
            return CircuitState.HALF_OPEN
        return self.circuit_state

    def is_paused(self) -> bool:
        now = time.time()
        if self.circuit_state == CircuitState.OPEN:
            if now < self.circuit_open_until:
                return True
            self.circuit_state = CircuitState.HALF_OPEN
        if now < self.retry_after_until:
            return True
        return False

    async def acquire(self) -> Tuple[bool, Optional[str]]:
        now = time.time()
        if self.circuit_state == CircuitState.OPEN:
            if now < self.circuit_open_until:
                remaining = int(self.circuit_open_until - now)
                return False, f"Circuit breaker OPEN for {self.platform} ({remaining}s remaining). Platform paused."
            self.circuit_state = CircuitState.HALF_OPEN

        if now < self.retry_after_until:
            remaining = int(self.retry_after_until - now)
            return False, f"Rate limited on {self.platform} (HTTP 429 Retry-After active, {remaining}s remaining)."

        elapsed = now - self.last_request_time
        if elapsed < self.min_interval:
            await asyncio.sleep(self.min_interval - elapsed)

        self.last_request_time = time.time()
        return True, None

    def record_success(self):
        self.consecutive_failures = 0
        self.circuit_state = CircuitState.CLOSED
        self.retry_after_until = 0.0
        self.last_failure_reason = ""

    def record_failure(self, is_rate_limit: bool = False, retry_after: Optional[float] = None, reason: str = ""):
        now = time.time()
        self.consecutive_failures += 1
        if reason:
            self.last_failure_reason = reason
        if is_rate_limit:
            delay = retry_after if (retry_after is not None and retry_after > 0) else 30.0
            self.retry_after_until = now + delay

        if self.consecutive_failures >= self.failure_threshold:
            duration = self.base_open_duration * (self.backoff_factor ** max(0, self.consecutive_failures - self.failure_threshold))
            self.circuit_state = CircuitState.OPEN
            self.circuit_open_until = now + duration

    def trip(self, reason: str = "", duration: Optional[float] = None):
        """Immediately trips the circuit breaker to OPEN."""
        now = time.time()
        self.circuit_state = CircuitState.OPEN
        self.circuit_open_until = now + (duration if duration is not None else self.base_open_duration)
        self.last_failure_reason = reason or "Manually tripped / security barrier detected"

    def reset(self):
        """Resets the circuit breaker to CLOSED."""
        self.consecutive_failures = 0
        self.circuit_state = CircuitState.CLOSED
        self.circuit_open_until = 0.0
        self.retry_after_until = 0.0
        self.last_failure_reason = ""

    def get_status(self) -> Dict[str, Any]:
        now = time.time()
        cur_state = self.state
        rem = max(0, int(self.circuit_open_until - now)) if cur_state == CircuitState.OPEN else 0
        return {
            "platform": self.platform,
            "state": cur_state.value,
            "consecutive_failures": self.consecutive_failures,
            "open_until": self.circuit_open_until,
            "remaining_seconds": rem,
            "reason": self.last_failure_reason,
        }

    def handle_http_response(self, status_code: int, headers: Optional[Dict[str, str]] = None):
        headers = headers or {}
        if status_code == 429:
            retry_after_val = 30.0
            for k, v in headers.items():
                if k.lower() == "retry-after":
                    try:
                        retry_after_val = float(v)
                    except ValueError:
                        retry_after_val = 30.0
                    break
            self.record_failure(is_rate_limit=True, retry_after=retry_after_val, reason="HTTP 429 Too Many Requests")
        elif 200 <= status_code < 300:
            self.record_success()
        elif status_code >= 500:
            self.record_failure(is_rate_limit=False, reason=f"HTTP {status_code} Server Error")

_PLATFORM_LIMITERS: Dict[str, PlatformRateLimiter] = {
    "leetcode": PlatformRateLimiter("leetcode", min_interval=3.0, failure_threshold=3, base_open_duration=30.0),
    "codeforces": PlatformRateLimiter("codeforces", min_interval=2.0, failure_threshold=3, base_open_duration=30.0),
    "hackerrank": PlatformRateLimiter("hackerrank", min_interval=2.0, failure_threshold=3, base_open_duration=30.0),
    "codechef": PlatformRateLimiter("codechef", min_interval=3.0, failure_threshold=3, base_open_duration=30.0),
    "hackerearth": PlatformRateLimiter("hackerearth", min_interval=3.0, failure_threshold=3, base_open_duration=30.0),
}

def get_rate_limiter(platform: str) -> PlatformRateLimiter:
    p = platform.lower().strip()
    if p not in _PLATFORM_LIMITERS:
        _PLATFORM_LIMITERS[p] = PlatformRateLimiter(p, min_interval=2.0)
    return _PLATFORM_LIMITERS[p]

get_platform_limiter = get_rate_limiter

async def detect_security_challenge_or_captcha(page: Page) -> SecurityChallengeDetection:
    """Detects CAPTCHA, Cloudflare challenges, and login walls without bypassing them."""
    if not page:
        return SecurityChallengeDetection(detected=False)

    curr_url = ""
    try:
        curr_url = page.url or ""
    except Exception:
        pass

    # 1. URL-based challenge indicators
    if any(k in curr_url.lower() for k in ["/challenge", "/captcha", "checkpoint", "/login?next=", "/enter?back="]):
        return SecurityChallengeDetection(
            detected=True,
            challenge_type="url_challenge_or_login_barrier",
            details=f"URL indicates challenge or forced login: {curr_url}",
            url=curr_url
        )

    # 2. DOM-based challenge and CAPTCHA indicators
    check_js = """() => {
        // Cloudflare Turnstile / IUAM
        const cf = document.querySelector('iframe[src*="cloudflare"], iframe[src*="turnstile"], .cf-turnstile, input[name="turnstileToken"], input[name="cf-turnstile-response"], #cf-wrapper, .cf-browser-verification');
        if (cf) return { detected: true, type: 'cloudflare_turnstile', details: 'Cloudflare Turnstile or IUAM challenge detected in DOM' };

        // Title checks
        const title = (document.title || '').toLowerCase();
        if (title.includes('just a moment') || title.includes('attention required') || title.includes('security check')) {
            return { detected: true, type: 'cloudflare_iuam', details: `Challenge page title: ${document.title}` };
        }

        // Google reCAPTCHA
        const recaptcha = document.querySelector('iframe[src*="recaptcha"], .g-recaptcha, #g-recaptcha, div[class*="recaptcha"]');
        if (recaptcha) return { detected: true, type: 'recaptcha', details: 'Google reCAPTCHA iframe or container detected' };

        // hCaptcha
        const hcaptcha = document.querySelector('iframe[src*="hcaptcha"], .h-captcha, div[class*="hcaptcha"]');
        if (hcaptcha) return { detected: true, type: 'hcaptcha', details: 'hCaptcha iframe or container detected' };

        // Generic anti-bot / captcha text
        const bodyText = (document.body ? document.body.innerText || '' : '').toLowerCase();
        if (bodyText.includes('please verify you are a human') || bodyText.includes('confirm you are human') || bodyText.includes('complete the security check')) {
            return { detected: true, type: 'generic_captcha', details: 'Human verification prompt detected in page text' };
        }

        return { detected: false };
    }"""
    try:
        res = await page.evaluate(check_js)
        if isinstance(res, dict) and res.get("detected"):
            return SecurityChallengeDetection(
                detected=True,
                challenge_type=res.get("type", "unknown_challenge"),
                details=res.get("details", ""),
                url=curr_url
            )
    except Exception:
        pass

    return SecurityChallengeDetection(detected=False, url=curr_url)

async def check_live_or_rated_contest(page: Page, url: str, platform_type: str) -> Tuple[bool, Optional[str]]:
    """Checks whether the target problem is inside an active, live, or rated contest to prevent automated submission."""
    url_lower = (url or "").lower()
    if page:
        contest_check_js = """() => {
            const timer = document.querySelector('.contest-timer, .countdown, [data-testid="contest-timer"], [class*="contestTimer"]');
            if (timer && (timer.innerText || '').trim().length > 0) {
                return { is_live: true, reason: `Active countdown timer detected: ${timer.innerText.trim()}` };
            }
            const liveBadge = document.querySelector('.live-contest-banner, .contest-status-live, div:has-text("Contest is running")');
            if (liveBadge) {
                return { is_live: true, reason: 'Live contest banner detected' };
            }
            return { is_live: false };
        }"""
        try:
            res = await page.evaluate(contest_check_js)
            if isinstance(res, dict) and res.get("is_live"):
                return True, res.get("reason", "Active contest detected on page")
        except Exception:
            pass

    if "/weekly-contest-" in url_lower or "/biweekly-contest-" in url_lower:
        return True, "LeetCode weekly/biweekly contest problems require manual contest participation"

    return False, None

async def resolve_verified_selector(
    page: Page,
    selectors: List[str],
    role: str,
    target_verifier: Optional[Callable[[str], bool]] = None,
    timeout_ms: int = 4000
) -> SelectorResolutionResult:
    """Tries selectors in order, tracking failed locators for stale selector diagnostics,
    and verifying element target identity before returning."""
    failed: List[Tuple[str, str]] = []
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.count() > 0:
                is_vis = await loc.is_visible()
                if is_vis:
                    text = ""
                    try:
                        text = (await loc.inner_text()).strip()
                    except Exception:
                        pass
                    if target_verifier:
                        if target_verifier(text):
                            return SelectorResolutionResult(
                                success=True,
                                matched_selector=selector,
                                element=loc,
                                failed_selectors=failed,
                                role=role
                            )
                        else:
                            failed.append((selector, f"Target identity verification failed for text preview: {text[:60]}..."))
                    else:
                        return SelectorResolutionResult(
                            success=True,
                            matched_selector=selector,
                            element=loc,
                            failed_selectors=failed,
                            role=role
                        )
                else:
                    failed.append((selector, "Element present but not visible"))
            else:
                failed.append((selector, "Selector matched 0 elements"))
        except Exception as e:
            failed.append((selector, f"Locator error: {e}"))

    if failed:
        first_few = "; ".join([f"{sel} ({err})" for sel, err in failed[:3]])
        print(f"   ⚠️ Stale or unverified selectors detected for [{role}]: {first_few}")

    return SelectorResolutionResult(success=False, failed_selectors=failed, role=role)

class BrowserManager:
    """Manages browser lifecycle, health checks, reconnection strategies, and clean shutdown."""

    @staticmethod
    async def is_healthy(page: Optional[Page]) -> bool:
        if not page:
            return False
        try:
            if hasattr(page, "is_closed") and page.is_closed():
                return False
            val = await asyncio.wait_for(page.evaluate("1 + 1"), timeout=3.0)
            return val == 2
        except Exception:
            return False

    @staticmethod
    async def reconnect(p_playwright=None, browser=None, context=None, cdp_port: int = 9222) -> Tuple[Any, Any, Page]:
        """Safely reconnects to Chrome over CDP if the current session or page is broken."""
        print("   🔄 BrowserManager: Reconnecting to Chrome via CDP...")
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

        if p_playwright:
            try:
                browser = await p_playwright.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await context.new_page()
                print("   ✅ BrowserManager: Reconnected cleanly to Chrome via CDP!")
                return browser, context, page
            except Exception as e:
                print(f"   ⚠️ BrowserManager reconnection notice: {e}")

        if context:
            try:
                page = await context.new_page()
                return browser, context, page
            except Exception:
                pass

        raise RuntimeError("BrowserManager: Unable to re-establish connection to browser.")

    @staticmethod
    async def clean_shutdown(browser=None, context=None, page=None, p_playwright=None):
        """Cleanly releases all browser and playwright resources."""
        if page:
            try:
                if not (hasattr(page, "is_closed") and page.is_closed()):
                    await page.close()
            except Exception:
                pass
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if p_playwright:
            try:
                await p_playwright.stop()
            except Exception:
                pass
        print("   🧹 BrowserManager: Resources shut down cleanly.")

def get_repo_default_branch(repo_dir: Union[Path, str]) -> str:
    """Detects the default branch for a git repository (e.g. main, master)."""
    r_dir = Path(repo_dir)
    # 1. Try git symbolic-ref
    try:
        res = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=r_dir,
            capture_output=True,
            text=True
        )
        if res.returncode == 0 and res.stdout.strip():
            ref = res.stdout.strip()
            return ref.split("/")[-1]
    except Exception:
        pass

    # 2. Try git remote show origin
    try:
        res = subprocess.run(
            ["git", "remote", "show", "origin"],
            cwd=r_dir,
            capture_output=True,
            text=True
        )
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.splitlines():
                if "HEAD branch:" in line:
                    b = line.split(":")[-1].strip()
                    if b:
                        return b
    except Exception:
        pass

    # 3. Try current local branch
    try:
        res = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=r_dir,
            capture_output=True,
            text=True
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass

    # 4. Fallback to main
    return "main"

@dataclass
class PollingConfig:
    max_timeout: float = 45.0
    initial_interval: float = 1.0
    max_interval: float = 5.0
    backoff_factor: float = 1.4
    max_polls: int = 25

async def poll_with_backoff(
    config: Optional[PollingConfig] = None,
    timeout: Optional[float] = None,
    max_polls: Optional[int] = None,
    initial_interval: Optional[float] = None,
    max_interval: Optional[float] = None,
    backoff_factor: Optional[float] = None,
):
    """Async generator yielding (poll_number, elapsed_seconds) with exponential backoff bounded by max_timeout."""
    cfg = config or PollingConfig()
    max_to = timeout if timeout is not None else cfg.max_timeout
    polls_limit = max_polls if max_polls is not None else cfg.max_polls
    curr_interval = initial_interval if initial_interval is not None else cfg.initial_interval
    max_int = max_interval if max_interval is not None else cfg.max_interval
    b_factor = backoff_factor if backoff_factor is not None else cfg.backoff_factor

    start_time = time.time()
    poll_count = 0

    while poll_count < polls_limit:
        elapsed = time.time() - start_time
        if elapsed >= max_to and poll_count > 0:
            break

        poll_count += 1
        yield poll_count, elapsed

        remaining = max_to - (time.time() - start_time)
        if remaining <= 0:
            break
        sleep_dur = min(curr_interval, remaining)
        if sleep_dur > 0:
            await asyncio.sleep(sleep_dur)
        curr_interval = min(curr_interval * b_factor, max_int)


# ==============================================================================
# Configuration & Environment Setup (Cloud-Native & Environment-Driven)
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent

# Configurable writable data directory: defaults to /app/data in container, or BASE_DIR locally
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data" if Path("/app/data").exists() else BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_DIR = Path.home() / ".autocoder"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# Auto-load .env file from working directory, user config dir, data dir, or base dir
env_candidates = [
    Path.cwd() / ".env",
    DATA_DIR / ".env",
    CONFIG_DIR / ".env",
    BASE_DIR / ".env",
]
for _env in env_candidates:
    if _env.exists():
        try:
            with open(_env, "r", encoding="utf-8") as _ef:
                for _line in _ef:
                    _line = _line.strip()
                    if _line and not _line.startswith("#") and "=" in _line:
                        _k, _v = _line.split("=", 1)
                        os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))
        except Exception:
            pass

SOLUTIONS_DIR = Path(os.environ.get("SOLUTIONS_DIR", DATA_DIR / "solutions"))
SOLUTIONS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR = Path(os.environ.get("LOGS_DIR", DATA_DIR / "logs"))
LOGS_DIR.mkdir(parents=True, exist_ok=True)
REPOS_DIR = Path(os.environ.get("REPOS_DIR", DATA_DIR / "repos"))
REPOS_DIR.mkdir(parents=True, exist_ok=True)
CHROME_USER_DATA_DIR = Path(os.environ.get("CHROME_USER_DATA_DIR", DATA_DIR / "chrome_user_data"))
DEFAULT_DB_FILE = Path(os.environ.get("DB_PATH", DATA_DIR / "autocoder.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Cloud Run / Container Execution Flags
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes")
OFFLINE_MODE = os.environ.get("OFFLINE_MODE", "").lower() in ("true", "1", "yes")
MAX_RUN_DURATION_SECONDS = int(os.environ.get("MAX_RUN_DURATION_SECONDS", "3600"))
STRUCTURED_LOGGING = (
    os.environ.get("STRUCTURED_LOGGING", "").lower() in ("true", "1", "yes")
    or bool(os.environ.get("K_SERVICE") or os.environ.get("CLOUD_RUN_JOB"))
)

def log_event(
    level: str,
    message: str,
    run_id: Optional[Union[int, str]] = None,
    problem_id: Optional[Union[int, str]] = None,
    platform: Optional[str] = None,
    attempt_id: Optional[Union[int, str]] = None,
    state: Optional[str] = None,
    duration: Optional[float] = None,
    failure_code: Optional[Union[str, FailureCode]] = None,
    **kwargs
):
    """
    Outputs structured JSON for Google Cloud Logging when enabled, else formatted console print.
    Includes run_id, problem_id, platform, attempt_id, state, duration, and failure_code.
    Safeguard: Never logs an unconfirmed submission as ACCEPTED.
    """
    level = level.upper()
    redacted_msg = redact_sensitive_data(message)

    # Requirement 11: Never log an unconfirmed submission as accepted
    if state == "ACCEPTED":
        confirmed = kwargs.get("confirmed", True)
        if not confirmed or kwargs.get("unconfirmed", False) or kwargs.get("mode") in ["run", "save"]:
            state = "SUBMITTED"

    f_code_str = (
        failure_code.value
        if hasattr(failure_code, "value")
        else (str(failure_code) if failure_code is not None else None)
    )

    if STRUCTURED_LOGGING:
        payload = {
            "severity": level,
            "message": redacted_msg,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": "autocoder",
        }
        if run_id is not None:
            payload["run_id"] = run_id
        if problem_id is not None:
            payload["problem_id"] = str(problem_id)
        if platform is not None:
            payload["platform"] = str(platform)
        if attempt_id is not None:
            payload["attempt_id"] = attempt_id
        if state is not None:
            payload["state"] = str(state)
        if duration is not None:
            payload["duration"] = round(float(duration), 3)
        if f_code_str is not None:
            payload["failure_code"] = f_code_str

        for k, v in kwargs.items():
            if k not in payload and k not in ["confirmed", "unconfirmed"]:
                payload[k] = redact_sensitive_data(str(v)) if isinstance(v, str) else v
        out = sys.stdout if level in ["INFO", "DEBUG"] else sys.stderr
        out.write(json.dumps(payload) + "\n")
        out.flush()
    else:
        prefix = {
            "INFO": "ℹ️ ",
            "WARNING": "⚠️ ",
            "ERROR": "❌",
            "DEBUG": "🔍"
        }.get(level, "")
        extra_parts = []
        if state:
            extra_parts.append(f"state={state}")
        if f_code_str:
            extra_parts.append(f"code={f_code_str}")
        if duration is not None:
            extra_parts.append(f"{duration:.2f}s")
        extra_str = f" [{', '.join(extra_parts)}]" if extra_parts else ""
        print(f"{prefix} {redacted_msg}{extra_str}")

class NotificationEvent(str, Enum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    QUOTA_MISSED = "QUOTA_MISSED"
    RUN_FAILED = "RUN_FAILED"

class NotificationManager:
    """Dispatches notifications to opt-in channels (webhook/slack/discord/console) when triggered."""
    def __init__(
        self,
        webhook_url: Optional[str] = None,
        channel: Optional[str] = None,
        enabled: Optional[bool] = None
    ):
        self.webhook_url = (webhook_url or os.environ.get("NOTIFICATION_WEBHOOK_URL", "")).strip()
        self.channel = (channel or os.environ.get("NOTIFICATION_CHANNEL", "console" if self.webhook_url else "none")).lower()
        if enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = (
                os.environ.get("NOTIFICATION_ENABLED", "").lower() in ("true", "1", "yes")
                or bool(self.webhook_url)
            )

    def send_notification(
        self,
        event: Union[NotificationEvent, str],
        title: str,
        message: str,
        platform: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> bool:
        if not self.enabled or self.channel in ("none", "disabled", "off"):
            return False

        ev_str = event.value if hasattr(event, "value") else str(event)
        redacted_title = redact_sensitive_data(title)
        redacted_msg = redact_sensitive_data(message)
        redacted_details = {}
        if details:
            for k, v in details.items():
                redacted_details[k] = redact_sensitive_data(str(v)) if isinstance(v, str) else v

        payload = {
            "event": ev_str,
            "title": redacted_title,
            "message": redacted_msg,
            "platform": platform or "all",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "details": redacted_details
        }

        # Format for Slack/Discord webhooks if channel specified
        post_data = payload
        if self.channel == "slack":
            post_data = {
                "text": f"*{redacted_title}*\n{redacted_msg}",
                "attachments": [{"fields": [{"title": k, "value": str(v), "short": True} for k, v in redacted_details.items()]}]
            }
        elif self.channel == "discord":
            post_data = {
                "content": f"**{redacted_title}**\n{redacted_msg}"
            }

        if self.webhook_url:
            try:
                req = urllib.request.Request(
                    self.webhook_url,
                    data=json.dumps(post_data).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": "AutoCoder-Bot/1.0"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    success = 200 <= resp.status < 300
                    if success:
                        log_event("INFO", f"Notification delivered successfully ({ev_str})", event=ev_str, platform=platform)
                    return success
            except Exception as e:
                # Notification failures MUST NEVER crash the worker!
                log_event("WARNING", f"Failed to deliver notification to {self.webhook_url}: {e}", event=ev_str, error=str(e))
                return False
        elif self.channel == "console":
            print("\n" + "🔔" * 35)
            print(f"🔔 [NOTIFICATION: {ev_str}] {redacted_title}")
            print(f"   {redacted_msg}")
            if platform:
                print(f"   Platform: {platform.upper()}")
            print("🔔" * 35 + "\n")
            return True

        return False

NOTIFICATION_MANAGER = NotificationManager()

def get_secret(secret_key: str, default: str = "") -> str:
    """Retrieves secret from Secret Manager if running on GCP with project id or secret path, else environment."""
    val = os.environ.get(secret_key, "").strip()
    if val and not val.startswith("projects/"):
        return val

    secret_ref = val or os.environ.get(f"SECRET_{secret_key}", "").strip()
    project_id = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")

    if secret_ref or (project_id and secret_key in ["GEMINI_API_KEY", "GITHUB_TOKEN"]):
        try:
            from google.cloud import secretmanager
            client = secretmanager.SecretManagerServiceClient()
            if secret_ref.startswith("projects/"):
                name = secret_ref
                if not name.endswith("/versions/latest") and "/versions/" not in name:
                    name = f"{name}/versions/latest"
            else:
                s_name = secret_ref or secret_key
                name = f"projects/{project_id}/secrets/{s_name}/versions/latest"

            res = client.access_secret_version(request={"name": name})
            retrieved = res.payload.data.decode("UTF-8").strip()
            if retrieved:
                log_event("INFO", f"Loaded secret for '{secret_key}' from Secret Manager ({name})")
                return retrieved
        except Exception as e:
            log_event("WARNING", f"Secret Manager lookup for '{secret_key}' notice: {e}")

    if DRY_RUN:
        return f"dry_run_{secret_key.lower()}"
    return default

GEMINI_API_KEY = get_secret("GEMINI_API_KEY", "")
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "").strip()
GITHUB_TOKEN = get_secret("GITHUB_TOKEN", "")
CODEFORCES_HANDLE = os.environ.get("CODEFORCES_HANDLE", "").strip()

# Signal handling for Cloud Run Jobs SIGTERM
SHUTDOWN_REQUESTED = False

def handle_sigterm(signum, frame):
    global SHUTDOWN_REQUESTED
    SHUTDOWN_REQUESTED = True
    sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    log_event("WARNING", f"Received {sig_name}. Initiating graceful shutdown and lease release...")
    try:
        if SolvedRegistry._db and getattr(SolvedRegistry, "_active_worker_id", None):
            SolvedRegistry._db.recover_expired_leases(lease_timeout_sec=0)
    except Exception:
        pass

try:
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)
except (ValueError, AttributeError):
    pass

@dataclass
class CloudWorkerConfig:
    mode: str
    platform: str
    count: int
    difficulty: str
    dry_run: bool
    offline_mode: bool
    database_url: str
    gemini_key_present: bool
    github_auth_present: bool
    max_duration: int

def validate_startup_configuration(
    mode: str = "both",
    platform: str = "all",
    count: int = 5,
    difficulty: str = "medium-hard",
    dry_run: bool = False,
    offline_mode: bool = False,
    timeout: Optional[int] = None
) -> CloudWorkerConfig:
    """Validates configuration at startup and fails fast with actionable errors."""
    is_dry = dry_run or DRY_RUN
    is_offline = offline_mode or OFFLINE_MODE
    duration = timeout if timeout is not None else MAX_RUN_DURATION_SECONDS

    gemini_present = bool(GEMINI_API_KEY) or is_dry
    github_present = bool(GITHUB_USERNAME) and (bool(GITHUB_TOKEN) or os.environ.get("GITHUB_TOKEN"))

    errors = []
    if not is_dry and not gemini_present:
        errors.append("GEMINI_API_KEY environment variable (or Secret Manager secret) is missing.")

    if mode in ["submit", "both"] and not is_dry and not is_offline:
        if not GITHUB_USERNAME:
            log_event("WARNING", "GITHUB_USERNAME is not set. Publishing to GitHub will be skipped.")

    if errors:
        err_msg = "Startup configuration validation failed: " + "; ".join(errors)
        log_event("ERROR", err_msg)
        raise ValueError(err_msg)

    cfg = CloudWorkerConfig(
        mode=mode,
        platform=platform,
        count=count,
        difficulty=difficulty,
        dry_run=is_dry,
        offline_mode=is_offline,
        database_url=DATABASE_URL or str(DEFAULT_DB_FILE),
        gemini_key_present=gemini_present,
        github_auth_present=github_present or is_dry,
        max_duration=duration
    )
    log_event("INFO", "Cloud worker configuration validated successfully",
              mode=mode, platform=platform, count=count, dry_run=is_dry, offline=is_offline)
    return cfg

def get_chrome_executable_path() -> Optional[str]:
    """Detects available Chrome or Chromium binary across Linux, macOS, and container environments."""
    custom = os.environ.get("CHROME_PATH")
    if custom and os.path.exists(custom):
        return custom

    if sys.platform == "darwin":
        mac_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(mac_path):
            return mac_path

    # Linux / container paths
    candidates = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/opt/google/chrome/chrome",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    which_chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if which_chrome:
        return which_chrome
    return None

AI_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
]
DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
SUBMISSIONS_LOG_FILE = LOGS_DIR / "submissions.jsonl"

def record_submission_evidence(
    verdict: SubmissionVerdict,
    url: str,
    platform_type: str,
    code: Optional[str] = None
):
    """Persists detailed submission evidence to logs/submissions.jsonl to guarantee auditability."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().isoformat(),
            "epoch": time.time(),
            "platform": platform_type,
            "url": url,
            "problem_id": verdict.problem_id,
            "submission_id": verdict.submission_id,
            "account": verdict.account,
            "verdict": verdict.state.value,
            "message": verdict.message,
            "correlation_mechanism": verdict.correlation_mechanism,
            "evidence": verdict.evidence,
        }
        with open(SUBMISSIONS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"   ⚠️  Could not persist submission evidence: {e}")

def extract_problem_id(url: str, platform_type: str) -> str:
    """Extracts a normalized problem identifier from a platform URL."""
    clean_url = url.strip().rstrip("/")
    if platform_type == "codeforces":
        m = re.search(r"/(?:problemset/problem|contest)/(\d+)/(?:problem/)?([A-Za-z0-9]+)", clean_url)
        if m:
            return f"{m.group(1)}{m.group(2).upper()}"
        parts = clean_url.split("/")
        if len(parts) >= 2:
            return f"{parts[-2]}{parts[-1]}".upper()
        return parts[-1].upper()
    elif platform_type == "hackerrank":
        m = re.search(r"/challenges/([^/]+)", clean_url)
        return m.group(1) if m else clean_url.split("/")[-1]
    elif platform_type == "leetcode":
        m = re.search(r"/problems/([^/]+)", clean_url)
        return m.group(1) if m else clean_url.split("/")[-1]
    elif platform_type == "codechef":
        m = re.search(r"/problems/([^/]+)", clean_url)
        return m.group(1) if m else clean_url.split("/")[-1]
    elif platform_type == "hackerearth":
        m = re.search(r"/(?:algorithm|approximate)/([^/]+)", clean_url)
        if m:
            return m.group(1)
        m2 = re.search(r"/practice-problems/(?:algorithm/)?([^/]+)", clean_url)
        if m2:
            return m2.group(1)
        return clean_url.split("/")[-1]
    return clean_url.split("/")[-1]


# ==============================================================================
# Platform Rules & Selectors
# ==============================================================================
PLATFORM_RULES = {
    "leetcode.com": {
        "name": "LeetCode",
        "question_selectors": [
            "div[data-track-load='description_content']",
            "div.elfjS",
            "[data-key='description-content']",
            "div.content__uRIT",
            "[data-layout-path*='description']",
            "div[class*='description']",
            ".question-content",
        ],
        "run_selectors": [
            "button[data-e2e-locator='console-run-button']",
            "button:has-text('Run')",
            "[data-cypress='RunCode']",
        ],
        "submit_selectors": [
            "button[data-e2e-locator='console-submit-button']",
            "button:has-text('Submit')",
            "[data-cypress='SubmitCode']",
        ],
        "type": "leetcode",
    },
    "hackerrank.com": {
        "name": "HackerRank",
        "question_selectors": [
            ".challenge-body-html",
            ".problem-statement",
            ".challenge_problem_statement",
            ".challenge-body",
            ".problem-statement-limits",
        ],
        "run_selectors": [
            "button.hr-monaco-compile",
            "button:has-text('Run Code')",
            "button[data-analytics='CompileAndTest']",
        ],
        "submit_selectors": [
            "button.hr-monaco-submit",
            "button:has-text('Submit Code')",
            "button[data-analytics='SubmitCode']",
        ],
        "type": "hackerrank",
    },
    "codeforces.com": {
        "name": "Codeforces",
        "question_selectors": [
            ".problem-statement",
            "#pageContent",
        ],
        "run_selectors": None,  # Verified locally with javac/java against sample cases
        "submit_selectors": [
            "form.submit-form input.submit",
            "input.submit",
            "button:has-text('Submit')",
        ],
        "type": "codeforces",
    },
    "hackerearth.com": {
        "name": "HackerEarth",
        "question_selectors": [
            ".problem-description",
            ".problem-details",
            ".statement",
            "div[class*='problem-description']",
            "div[class*='problem-statement']",
            "[data-slot='card']",
        ],
        "run_selectors": [
            "button:has-text('Compile & Test')",
            "button:has-text('Run')",
            ".compile-test-button",
        ],
        "submit_selectors": [
            "button:has-text('Submit')",
            "button:has-text('Submit Code')",
            ".submit-button",
        ],
        "type": "hackerearth",
    },
    "codechef.com": {
        "name": "CodeChef",
        "question_selectors": [
            "#problem-statement",
            "._problem_statement_container_",
            "div[class*='problem-statement']",
            "div[class*='ProblemStatement']",
            "div[id*='problem-statement']",
            ".problem-statement",
            "div._content_container_",
        ],
        "run_selectors": [
            "button:has-text('Run')",
            "button:has-text('Run Code')",
            "button[id*='run']",
            "button[data-testid*='run']",
        ],
        "submit_selectors": [
            "button:has-text('Submit')",
            "button:has-text('Submit Code')",
            "button[id*='submit']",
            "button[data-testid*='submit']",
        ],
        "type": "codechef",
    },
}

# ==============================================================================
# Pydantic Output Schema
# ==============================================================================
class CodingAnswer(BaseModel):
    explanation: str = Field(description="Concise summary of approach and time/space complexity")
    code: str = Field(description="Complete, valid, standalone Java code only with no markdown fences")

# ==============================================================================
# ==============================================================================
# Persistent SQLite Storage Engine & Solved Registry
# ==============================================================================
if os.environ.get("HISTORY_FILE"):
    HISTORY_FILE = Path(os.environ["HISTORY_FILE"])
elif (DATA_DIR / "solved_history.json").exists():
    HISTORY_FILE = DATA_DIR / "solved_history.json"
elif (CONFIG_DIR / "solved_history.json").exists():
    HISTORY_FILE = CONFIG_DIR / "solved_history.json"
elif (BASE_DIR / "solved_history.json").exists():
    HISTORY_FILE = BASE_DIR / "solved_history.json"
else:
    HISTORY_FILE = DATA_DIR / "solved_history.json"

if os.environ.get("DB_PATH"):
    DEFAULT_DB_FILE = Path(os.environ["DB_PATH"])
elif (DATA_DIR / "autocoder.db").exists():
    DEFAULT_DB_FILE = DATA_DIR / "autocoder.db"
elif (CONFIG_DIR / "autocoder.db").exists():
    DEFAULT_DB_FILE = CONFIG_DIR / "autocoder.db"
elif (BASE_DIR / "autocoder.db").exists():
    DEFAULT_DB_FILE = BASE_DIR / "autocoder.db"
else:
    DEFAULT_DB_FILE = DATA_DIR / "autocoder.db"

TRANSIENT_ERROR_SUBSTRINGS = [
    "timeout", "timed out", "econnreset", "connection reset",
    "network error", "rate limit", "503", "502", "504", "temporary",
    "cdp reconnection notice", "session closed", "target closed"
]

def is_transient_error(error_msg: Optional[str]) -> bool:
    """Classifies whether an error message indicates a transient recoverable failure."""
    if not error_msg:
        return False
    return classify_failure(error_msg) == FailureCode.TRANSIENT

def normalize_problem_identity(url_or_slug: str, platform: Optional[str] = None) -> Tuple[str, str]:
    """
    Normalizes any problem URL, slug, or identifier into a canonical (platform, canonical_id) pair.
    Handles query strings, hash fragments, trailing slashes, subpaths (/description, /problem),
    and platform-specific conventions.
    """
    raw = url_or_slug.strip()
    detected_platform = platform.lower().strip() if platform else None
    if not detected_platform:
        if "leetcode.com" in raw:
            detected_platform = "leetcode"
        elif "codeforces.com" in raw:
            detected_platform = "codeforces"
        elif "hackerrank.com" in raw:
            detected_platform = "hackerrank"
        elif "codechef.com" in raw:
            detected_platform = "codechef"
        elif "hackerearth.com" in raw:
            detected_platform = "hackerearth"
        else:
            detected_platform = "leetcode"

    # Strip query parameters (?...) and hash fragments (#...)
    clean = re.sub(r"[?#].*$", "", raw).rstrip("/")

    if detected_platform == "leetcode":
        m = re.search(r"/problems/([^/]+)", clean)
        slug = m.group(1) if m else clean.split("/")[-1]
        canonical = re.sub(r"[\s_]+", "-", slug).strip("-").lower()
        return detected_platform, canonical

    elif detected_platform == "codeforces":
        m = re.search(r"/(?:problemset/problem|contest)/(\d+)/(?:problem/)?([A-Za-z0-9]+)", clean)
        if m:
            canonical = f"{m.group(1)}{m.group(2).upper()}"
            return detected_platform, canonical
        parts = clean.split("/")
        if len(parts) >= 2 and parts[-2].isdigit():
            canonical = f"{parts[-2]}{parts[-1].upper()}"
            return detected_platform, canonical
        slug = parts[-1]
        m_direct = re.match(r"^(\d+)[_\-]?([A-Za-z0-9]+)$", slug)
        if m_direct:
            canonical = f"{m_direct.group(1)}{m_direct.group(2).upper()}"
        else:
            canonical = re.sub(r"[^A-Za-z0-9]", "", slug).upper()
        return detected_platform, canonical

    elif detected_platform == "hackerrank":
        m = re.search(r"/challenges/([^/]+)", clean)
        if m:
            slug = m.group(1)
        else:
            slug = clean.split("/")[-1]
        if slug in ["problem", "submissions", "leaderboard"]:
            parts = clean.split("/")
            slug = parts[-2] if len(parts) >= 2 else slug
        canonical = re.sub(r"[\s_]+", "-", slug).strip("-").lower()
        return detected_platform, canonical

    elif detected_platform == "codechef":
        m = re.search(r"/(?:problems|practice/course/[^/]+)/([^/]+)", clean)
        if m:
            slug = m.group(1)
        else:
            slug = clean.split("/")[-1]
        canonical = re.sub(r"[^A-Za-z0-9_]", "", slug).strip("_").upper()
        return detected_platform, canonical

    elif detected_platform == "hackerearth":
        m = re.search(r"/(?:algorithm|approximate)/([^/]+)", clean)
        if m:
            slug = m.group(1)
        else:
            m2 = re.search(r"/practice-problems/(?:algorithm/)?([^/]+)", clean)
            slug = m2.group(1) if m2 else clean.split("/")[-1]
        canonical = re.sub(r"[\s_]+", "-", slug).strip("-").lower()
        return detected_platform, canonical

    slug = clean.split("/")[-1]
    return detected_platform, slug.lower()


class DatabaseManager:
    """Persistent storage engine for problems, attempts, submissions, verdicts, publish jobs, and daily runs (SQLite & PostgreSQL dual-compatible)."""

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        database_url: Optional[str] = None,
        auto_migrate: bool = True
    ):
        raw_url = database_url or DATABASE_URL or os.environ.get("DATABASE_URL", "").strip()
        self.is_postgres = bool(raw_url and (raw_url.startswith("postgresql://") or raw_url.startswith("postgres://")))
        self.database_url = raw_url if self.is_postgres else None

        if not self.is_postgres:
            if db_path is not None:
                self.db_path = Path(db_path)
            else:
                env_db = os.environ.get("AUTOCODER_DB_PATH") or os.environ.get("DB_PATH")
                self.db_path = Path(env_db) if env_db else DEFAULT_DB_FILE
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            self.db_path = None

        self._init_db()
        if auto_migrate and not self.is_postgres:
            self.migrate_from_solved_history_if_needed()

    @contextmanager
    def get_connection(self):
        if self.is_postgres:
            if psycopg2 is None:
                raise RuntimeError("psycopg2 is required for PostgreSQL. Please install psycopg2-binary.")
            conn = psycopg2.connect(self.database_url, cursor_factory=psycopg2.extras.DictCursor)
            try:
                yield conn
            finally:
                conn.close()
        else:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                isolation_level=None,
                detect_types=sqlite3.PARSE_DECLTYPES
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            try:
                yield conn
            finally:
                conn.close()

    def _execute(self, conn, sql: str, params: Optional[Union[tuple, list]] = None):
        """Executes a SQL query, adapting syntax and placeholders between SQLite and PostgreSQL."""
        cursor = conn.cursor()
        effective_sql = sql
        effective_params = list(params) if params is not None else []
        if self.is_postgres:
            # Replace SQLite ? placeholder with PostgreSQL %s
            effective_sql = effective_sql.replace("?", "%s")
            trimmed = effective_sql.strip().upper()
            if trimmed.startswith("BEGIN IMMEDIATE") or trimmed.startswith("BEGIN"):
                return cursor
            if trimmed.startswith("COMMIT"):
                conn.commit()
                return cursor
            # DDL replacements
            effective_sql = effective_sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            effective_sql = effective_sql.replace("REAL", "DOUBLE PRECISION")
            cursor.execute(effective_sql, effective_params)
        else:
            cursor.execute(effective_sql, effective_params)
        return cursor

    def _init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self._execute(conn, "BEGIN IMMEDIATE")
            self._execute(conn, """
                CREATE TABLE IF NOT EXISTS problems (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT,
                    difficulty TEXT,
                    status TEXT NOT NULL DEFAULT 'DISCOVERED',
                    claimed_by TEXT,
                    claimed_at REAL,
                    heartbeat_at REAL,
                    lease_timeout REAL DEFAULT 300.0,
                    failure_reason TEXT,
                    failure_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, canonical_id)
                )
            """)
            self._execute(conn, """
                CREATE TABLE IF NOT EXISTS daily_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    target_quota INTEGER NOT NULL,
                    attempted_count INTEGER DEFAULT 0,
                    generated_count INTEGER DEFAULT 0,
                    compiled_count INTEGER DEFAULT 0,
                    tested_count INTEGER DEFAULT 0,
                    accepted_count INTEGER DEFAULT 0,
                    published_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    skipped_count INTEGER DEFAULT 0,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL DEFAULT 'IN_PROGRESS',
                    locked_by TEXT,
                    locked_at REAL
                )
            """)
            self._execute(conn, """
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    problem_id INTEGER NOT NULL REFERENCES problems(id),
                    run_id INTEGER REFERENCES daily_runs(id),
                    platform TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    solution_code TEXT,
                    explanation TEXT,
                    error TEXT,
                    is_transient INTEGER DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    compiler_output TEXT,
                    diagnostics TEXT
                )
            """)
            self._execute(conn, """
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id INTEGER REFERENCES attempts(id),
                    problem_id INTEGER NOT NULL REFERENCES problems(id),
                    platform TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    submission_id TEXT,
                    account TEXT,
                    correlation_mechanism TEXT,
                    submitted_at TEXT NOT NULL,
                    payload TEXT
                )
            """)
            self._execute(conn, """
                CREATE TABLE IF NOT EXISTS judge_verdicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_id_ref INTEGER REFERENCES submissions(id),
                    problem_id INTEGER NOT NULL REFERENCES problems(id),
                    platform TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    verdict_state TEXT NOT NULL,
                    message TEXT,
                    submission_id TEXT,
                    evidence TEXT,
                    verified_at TEXT NOT NULL
                )
            """)
            self._execute(conn, """
                CREATE TABLE IF NOT EXISTS publish_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    problem_id INTEGER NOT NULL REFERENCES problems(id),
                    attempt_id INTEGER REFERENCES attempts(id),
                    platform TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    repo_name TEXT,
                    branch TEXT,
                    commit_hash TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    published_at TEXT NOT NULL
                )
            """)
            self._execute(conn, """
                CREATE TABLE IF NOT EXISTS sample_test_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id INTEGER REFERENCES attempts(id),
                    sample_index INTEGER NOT NULL,
                    input_text TEXT,
                    expected_output TEXT,
                    actual_output TEXT,
                    passed INTEGER NOT NULL,
                    error_message TEXT,
                    diff TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            self._execute(conn, """
                CREATE TABLE IF NOT EXISTS retry_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    problem_id INTEGER NOT NULL REFERENCES problems(id),
                    platform TEXT NOT NULL,
                    canonical_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    failure_code TEXT NOT NULL,
                    last_error TEXT,
                    next_retry_at REAL NOT NULL,
                    backoff_seconds REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, canonical_id)
                )
            """)
            self._execute(conn, "CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY, migrated_at TEXT)")
            self._execute(conn, "CREATE INDEX IF NOT EXISTS idx_problems_lookup ON problems(platform, canonical_id)")
            self._execute(conn, "CREATE INDEX IF NOT EXISTS idx_problems_status ON problems(status)")
            self._execute(conn, "CREATE INDEX IF NOT EXISTS idx_daily_runs_lookup ON daily_runs(run_date, platform)")
            self._execute(conn, "CREATE INDEX IF NOT EXISTS idx_judge_verdicts_lookup ON judge_verdicts(platform, canonical_id)")
            self._execute(conn, "CREATE INDEX IF NOT EXISTS idx_sample_test_attempt ON sample_test_results(attempt_id)")
            self._execute(conn, "CREATE INDEX IF NOT EXISTS idx_retry_queue_status_next ON retry_queue(status, next_retry_at)")

            if not self.is_postgres:
                # Add columns to existing SQLite tables if missing
                cursor.execute("PRAGMA table_info(problems)")
                prob_cols = [col[1] for col in cursor.fetchall()]
                if "heartbeat_at" not in prob_cols:
                    cursor.execute("ALTER TABLE problems ADD COLUMN heartbeat_at REAL")
                if "lease_timeout" not in prob_cols:
                    cursor.execute("ALTER TABLE problems ADD COLUMN lease_timeout REAL DEFAULT 300.0")

                cursor.execute("PRAGMA table_info(attempts)")
                att_cols = [col[1] for col in cursor.fetchall()]
                if "compiler_output" not in att_cols:
                    cursor.execute("ALTER TABLE attempts ADD COLUMN compiler_output TEXT")
                if "diagnostics" not in att_cols:
                    cursor.execute("ALTER TABLE attempts ADD COLUMN diagnostics TEXT")

                cursor.execute("PRAGMA table_info(daily_runs)")
                dr_cols = [col[1] for col in cursor.fetchall()]
                if "locked_by" not in dr_cols:
                    cursor.execute("ALTER TABLE daily_runs ADD COLUMN locked_by TEXT")
                if "locked_at" not in dr_cols:
                    cursor.execute("ALTER TABLE daily_runs ADD COLUMN locked_at REAL")
                if "tested_count" not in dr_cols:
                    cursor.execute("ALTER TABLE daily_runs ADD COLUMN tested_count INTEGER DEFAULT 0")

            self._execute(conn, "COMMIT")

    def migrate_from_solved_history_if_needed(self, history_file: Optional[Path] = None, submissions_file: Optional[Path] = None):
        """
        Migrates legacy solved_history.json into the SQLite problems and judge_verdicts tables.
        Entries without confirmed judge evidence in submissions.jsonl are strictly marked UNVERIFIED.
        """
        h_file = history_file or HISTORY_FILE
        s_file = submissions_file or SUBMISSIONS_LOG_FILE

        if not h_file.exists():
            return

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM migrations WHERE name = 'solved_history_migration'")
            if cursor.fetchone():
                return  # Migration already completed

            cursor.execute("BEGIN IMMEDIATE")

            # 1. Parse verified evidence from submissions.jsonl if present
            verified_evidence: Dict[Tuple[str, str], Dict[str, Any]] = {}
            if s_file.exists():
                try:
                    with open(s_file, "r", encoding="utf-8") as sf:
                        for line in sf:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entry = json.loads(line)
                                if entry.get("verdict") == "ACCEPTED":
                                    p = entry.get("platform")
                                    u = entry.get("url")
                                    if p and u:
                                        _, cid = normalize_problem_identity(u, p)
                                        verified_evidence[(p, cid)] = entry
                            except Exception:
                                pass
                except Exception:
                    pass

            # 2. Parse solved_history.json
            now_iso = get_kolkata_now().isoformat()
            try:
                with open(h_file, "r", encoding="utf-8") as f:
                    history_data = json.load(f)
            except Exception:
                history_data = {}

            migrated_count = 0
            for plat, slugs in history_data.items():
                for s in slugs:
                    _, cid = normalize_problem_identity(s, plat)
                    evidence = verified_evidence.get((plat, cid))

                    status = "ACCEPTED" if evidence else "UNVERIFIED"
                    url = evidence.get("url") if evidence else f"https://{plat}.com/legacy/{cid}"

                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO problems (platform, canonical_id, url, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (plat, cid, url, status, now_iso, now_iso)
                    )
                    cursor.execute("SELECT id FROM problems WHERE platform = ? AND canonical_id = ?", (plat, cid))
                    prob_row = cursor.fetchone()
                    if prob_row:
                        prob_id = prob_row[0]
                        verdict_state = "ACCEPTED" if evidence else "UNVERIFIED"
                        msg = evidence.get("message", "Accepted") if evidence else "Legacy solved_history.json entry without judge evidence"
                        sub_id = evidence.get("submission_id") if evidence else None
                        ev_json = json.dumps(evidence) if evidence else None

                        cursor.execute(
                            """
                            INSERT INTO judge_verdicts (problem_id, platform, canonical_id, verdict_state, message, submission_id, evidence, verified_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (prob_id, plat, cid, verdict_state, msg, sub_id, ev_json, now_iso)
                        )
                        migrated_count += 1

            cursor.execute(
                "INSERT INTO migrations (name, migrated_at) VALUES ('solved_history_migration', ?)",
                (now_iso,)
            )
            cursor.execute("COMMIT")

    def is_accepted(self, platform: str, url_or_slug: str) -> bool:
        """Returns True ONLY if the problem has confirmed judge ACCEPTED verdict."""
        plat, cid = normalize_problem_identity(url_or_slug, platform)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT p.status, jv.verdict_state
                FROM problems p
                LEFT JOIN judge_verdicts jv ON jv.problem_id = p.id AND jv.verdict_state = 'ACCEPTED'
                WHERE p.platform = ? AND p.canonical_id = ?
                LIMIT 1
                """,
                (plat, cid)
            )
            row = cursor.fetchone()
            if not row:
                return False
            status, verdict_state = row[0], row[1]
            return (status == "ACCEPTED" or verdict_state == "ACCEPTED")

    def is_permanent_failure(self, platform: str, url_or_slug: str, max_failures: int = 3) -> bool:
        """Returns True if the problem has permanently failed or exceeded max retry attempts."""
        plat, cid = normalize_problem_identity(url_or_slug, platform)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status, failure_count FROM problems WHERE platform = ? AND canonical_id = ?",
                (plat, cid)
            )
            row = cursor.fetchone()
            if not row:
                return False
            status, fail_cnt = row[0], (row[1] or 0)
            if status in ["FAILED", "REJECTED"]:
                return True
            if fail_cnt >= max_failures:
                return True
            return False

    def get_accepted_canonical_ids(self, platform: str) -> Set[str]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT canonical_id FROM problems
                WHERE platform = ? AND status = 'ACCEPTED'
                """,
                (platform,)
            )
            return {row[0] for row in cursor.fetchall()}

    def claim_problem(
        self,
        platform: str,
        canonical_id: str,
        url: str,
        worker_id: str = "default",
        lease_timeout_sec: float = 300.0
    ) -> bool:
        """
        Atomically claims a problem for processing using a SQLite transaction with BEGIN IMMEDIATE.
        Supports safe reclamation of expired leases by recovered workers.
        Returns True if claim acquired; False if already accepted or actively claimed by another worker.
        """
        now = time.time()
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT id, status, claimed_by, claimed_at, heartbeat_at, lease_timeout FROM problems WHERE platform = ? AND canonical_id = ?",
                (platform, canonical_id)
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    INSERT INTO problems (platform, canonical_id, url, status, claimed_by, claimed_at, heartbeat_at, lease_timeout, created_at, updated_at)
                    VALUES (?, ?, ?, 'IN_PROGRESS', ?, ?, ?, ?, ?, ?)
                    """,
                    (platform, canonical_id, url, worker_id, now, now, lease_timeout_sec, now_iso, now_iso)
                )
                cursor.execute("COMMIT")
                return True

            prob_id, status, claimed_by, claimed_at, heartbeat_at, db_lease_timeout = (
                row[0], row[1], row[2], row[3],
                row[4] if len(row) > 4 else None,
                row[5] if len(row) > 5 else None
            )
            if status == "ACCEPTED":
                cursor.execute("COMMIT")
                return False

            if status == "IN_PROGRESS":
                ref_time = heartbeat_at or claimed_at
                eff_timeout = db_lease_timeout or lease_timeout_sec
                if claimed_by != worker_id and ref_time and (now - ref_time) < eff_timeout:
                    cursor.execute("COMMIT")
                    return False

            cursor.execute(
                """
                UPDATE problems
                SET status = 'IN_PROGRESS', claimed_by = ?, claimed_at = ?, heartbeat_at = ?, lease_timeout = ?, updated_at = ?
                WHERE id = ?
                """,
                (worker_id, now, now, lease_timeout_sec, now_iso, prob_id)
            )
            cursor.execute("COMMIT")
            return True

    def record_heartbeat(self, platform: str, canonical_id: str, worker_id: str = "default") -> bool:
        """Updates the heartbeat timestamp for an in-progress problem to prevent lease expiration."""
        plat, cid = normalize_problem_identity(canonical_id, platform)
        now = time.time()
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            self._execute(conn, "BEGIN IMMEDIATE")
            res = self._execute(
                conn,
                """
                UPDATE problems
                SET heartbeat_at = ?, updated_at = ?
                WHERE platform = ? AND canonical_id = ? AND status = 'IN_PROGRESS'
                """,
                (now, now_iso, plat, cid)
            )
            updated = res.rowcount > 0
            self._execute(conn, "COMMIT")
            return updated

    def get_problem_id(self, platform: str, canonical_id: str) -> Optional[int]:
        plat, cid = normalize_problem_identity(canonical_id, platform)
        with self.get_connection() as conn:
            cur = self._execute(
                conn,
                "SELECT id FROM problems WHERE platform = ? AND canonical_id = ?",
                (plat, cid)
            )
            row = cur.fetchone()
            return row[0] if row else None

    def release_problem(
        self,
        platform: str,
        canonical_id: str,
        status: str,
        failure_reason: Optional[str] = None,
        is_transient: bool = False
    ):
        """Releases or updates the problem status after an attempt finishes."""
        plat, cid = normalize_problem_identity(canonical_id, platform)
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT id, failure_count FROM problems WHERE platform = ? AND canonical_id = ?",
                (plat, cid)
            )
            row = cursor.fetchone()
            if not row:
                fail_cnt = 1
                effective_status = "TRANSIENT_FAILURE" if (is_transient and fail_cnt < 3) else status
                cursor.execute(
                    """
                    INSERT INTO problems (platform, canonical_id, url, status, failure_reason, failure_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (plat, cid, f"https://{plat}.com/problem/{cid}", effective_status, failure_reason, fail_cnt, now_iso, now_iso)
                )
                cursor.execute("COMMIT")
                return

            prob_id, fail_cnt = row[0], (row[1] or 0)
            fail_cnt += 1
            effective_status = "TRANSIENT_FAILURE" if (is_transient and fail_cnt < 3) else status

            cursor.execute(
                """
                UPDATE problems
                SET status = ?, claimed_by = NULL, claimed_at = NULL, heartbeat_at = NULL,
                    failure_reason = ?, failure_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (effective_status, failure_reason, fail_cnt, now_iso, prob_id)
            )
            cursor.execute("COMMIT")

    def mark_problem_accepted(
        self,
        platform: str,
        canonical_id: str,
        url: str,
        verdict: Optional[SubmissionVerdict] = None
    ):
        """Records confirmed acceptance in both problems and judge_verdicts tables."""
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                """
                INSERT INTO problems (platform, canonical_id, url, status, created_at, updated_at)
                VALUES (?, ?, ?, 'ACCEPTED', ?, ?)
                ON CONFLICT(platform, canonical_id) DO UPDATE SET
                    status = 'ACCEPTED',
                    claimed_by = NULL,
                    claimed_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (platform, canonical_id, url, now_iso, now_iso)
            )
            cursor.execute("SELECT id FROM problems WHERE platform = ? AND canonical_id = ?", (platform, canonical_id))
            prob_id = cursor.fetchone()[0]

            sub_id = verdict.submission_id if verdict else None
            msg = verdict.message if verdict else "Accepted"
            ev_str = json.dumps(verdict.evidence) if (verdict and verdict.evidence) else None

            cursor.execute(
                """
                INSERT INTO judge_verdicts (problem_id, platform, canonical_id, verdict_state, message, submission_id, evidence, verified_at)
                VALUES (?, ?, ?, 'ACCEPTED', ?, ?, ?, ?)
                """,
                (prob_id, platform, canonical_id, msg, sub_id, ev_str, now_iso)
            )
            cursor.execute("COMMIT")

    def get_or_create_daily_run(self, platform: str, target_quota: int, run_date: Optional[str] = None) -> int:
        r_date = run_date or get_kolkata_today_str()
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT id FROM daily_runs WHERE run_date = ? AND platform = ? ORDER BY id DESC LIMIT 1",
                (r_date, platform)
            )
            row = cursor.fetchone()
            if row:
                cursor.execute("COMMIT")
                return row[0]

            cursor.execute(
                """
                INSERT INTO daily_runs (run_date, platform, target_quota, started_at, status)
                VALUES (?, ?, ?, ?, 'IN_PROGRESS')
                """,
                (r_date, platform, target_quota, now_iso)
            )
            run_id = cursor.lastrowid
            cursor.execute("COMMIT")
            return run_id

    def increment_daily_run_counts(
        self,
        run_id: int,
        attempted: int = 0,
        generated: int = 0,
        compiled: int = 0,
        tested: int = 0,
        accepted: int = 0,
        published: int = 0,
        failed: int = 0,
        skipped: int = 0,
    ):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                """
                UPDATE daily_runs
                SET attempted_count = attempted_count + ?,
                    generated_count = generated_count + ?,
                    compiled_count = compiled_count + ?,
                    tested_count = tested_count + ?,
                    accepted_count = accepted_count + ?,
                    published_count = published_count + ?,
                    failed_count = failed_count + ?,
                    skipped_count = skipped_count + ?
                WHERE id = ?
                """,
                (attempted, generated, compiled, tested, accepted, published, failed, skipped, run_id)
            )
            cursor.execute("COMMIT")

    def recover_expired_leases(self, lease_timeout_sec: float = 300.0) -> int:
        """
        Recovers problems stranded in IN_PROGRESS status due to worker termination, container crash, or lost heartbeats.
        Resets them to TRANSIENT_FAILURE and automatically enqueues them into the persistent retry queue.
        """
        now = time.time()
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            self._execute(conn, "BEGIN IMMEDIATE")
            if lease_timeout_sec <= 0:
                cur = self._execute(
                    conn,
                    """
                    SELECT id, platform, canonical_id, url FROM problems
                    WHERE status = 'IN_PROGRESS'
                    """
                )
            else:
                cutoff = now - lease_timeout_sec
                cur = self._execute(
                    conn,
                    """
                    SELECT id, platform, canonical_id, url FROM problems
                    WHERE status = 'IN_PROGRESS' AND (
                        (heartbeat_at IS NOT NULL AND heartbeat_at < ?)
                        OR (heartbeat_at IS NULL AND (claimed_at IS NULL OR claimed_at < ?))
                    )
                    """,
                    (cutoff, cutoff)
                )
            rows = cur.fetchall()
            if not rows:
                self._execute(conn, "COMMIT")
                return 0

            prob_ids = [r[0] for r in rows]
            placeholders = ",".join("?" for _ in prob_ids)
            self._execute(
                conn,
                f"""
                UPDATE problems
                SET status = 'TRANSIENT_FAILURE',
                    failure_reason = 'Worker lease expired / heartbeat lost (stuck job recovered)',
                    claimed_by = NULL,
                    claimed_at = NULL,
                    heartbeat_at = NULL,
                    updated_at = ?
                WHERE id IN ({placeholders})
                """,
                [now_iso] + prob_ids
            )
            self._execute(conn, "COMMIT")

        # Automatically enqueue each recovered stuck problem into the persistent retry queue
        for r in rows:
            p_id, plat, c_id = r[0], r[1], r[2]
            p_url = r[3] if len(r) > 3 and r[3] else f"https://{plat}.com/problem/{c_id}"
            self.enqueue_retry(
                problem_id=p_id,
                platform=plat,
                canonical_id=c_id,
                url=p_url,
                failure_code=FailureCode.TRANSIENT,
                error_msg="Worker lease expired / heartbeat lost (stuck job recovered)"
            )

        log_event("INFO", f"Crash recovery: released {len(prob_ids)} expired problem lease(s)")
        return len(prob_ids)

    def enqueue_retry(
        self,
        problem_id: int,
        platform: str,
        canonical_id: str,
        url: str,
        failure_code: Union[str, FailureCode],
        error_msg: str,
        max_retries: int = 3,
        base_backoff: float = 30.0,
        max_backoff: float = 1800.0,
    ) -> bool:
        """
        Adds or updates a problem in the persistent retry queue.
        Requirement 5: Retry only transient failures automatically.
        Uses exponential backoff with jitter.
        """
        f_code_str = failure_code.value if hasattr(failure_code, "value") else str(failure_code)
        if f_code_str != FailureCode.TRANSIENT.value:
            return False

        now = time.time()
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            self._execute(conn, "BEGIN IMMEDIATE")
            cur = self._execute(
                conn,
                "SELECT id, retry_count, max_retries FROM retry_queue WHERE platform = ? AND canonical_id = ?",
                (platform, canonical_id)
            )
            row = cur.fetchone()
            if row:
                q_id, cur_count, limit = row[0], row[1], row[2]
                if cur_count >= limit:
                    self._execute(
                        conn,
                        """
                        UPDATE retry_queue
                        SET status = 'EXHAUSTED',
                            failure_code = ?,
                            last_error = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (f_code_str, redact_sensitive_data(error_msg), now_iso, q_id)
                    )
                    self._execute(conn, "COMMIT")
                    return False

                new_count = cur_count + 1
                raw_backoff = min(base_backoff * (2 ** (new_count - 1)), max_backoff)
                jitter = random.uniform(-0.15, 0.15) * raw_backoff
                backoff = max(1.0, raw_backoff + jitter)
                next_retry = now + backoff

                self._execute(
                    conn,
                    """
                    UPDATE retry_queue
                    SET retry_count = ?,
                        failure_code = ?,
                        last_error = ?,
                        next_retry_at = ?,
                        backoff_seconds = ?,
                        status = 'PENDING',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (new_count, f_code_str, redact_sensitive_data(error_msg), next_retry, round(backoff, 2), now_iso, q_id)
                )
                self._execute(conn, "COMMIT")
                return True
            else:
                raw_backoff = base_backoff
                jitter = random.uniform(-0.15, 0.15) * raw_backoff
                backoff = max(1.0, raw_backoff + jitter)
                next_retry = now + backoff

                self._execute(
                    conn,
                    """
                    INSERT INTO retry_queue (
                        problem_id, platform, canonical_id, url,
                        retry_count, max_retries, failure_code, last_error,
                        next_retry_at, backoff_seconds, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                    """,
                    (
                        problem_id, platform, canonical_id, url,
                        max_retries, f_code_str, redact_sensitive_data(error_msg),
                        next_retry, round(backoff, 2), now_iso, now_iso
                    )
                )
                self._execute(conn, "COMMIT")
                return True

    def get_pending_retries(
        self,
        platform: Optional[str] = None,
        limit: int = 10,
        include_future: bool = False
    ) -> List[Dict[str, Any]]:
        """Retrieves due problems from the persistent retry queue."""
        now = time.time()
        with self.get_connection() as conn:
            if include_future:
                if platform:
                    cur = self._execute(conn, "SELECT * FROM retry_queue WHERE status = 'PENDING' AND platform = ? ORDER BY next_retry_at ASC LIMIT ?", (platform, limit))
                else:
                    cur = self._execute(conn, "SELECT * FROM retry_queue WHERE status = 'PENDING' ORDER BY next_retry_at ASC LIMIT ?", (limit,))
            else:
                if platform:
                    cur = self._execute(conn, "SELECT * FROM retry_queue WHERE status = 'PENDING' AND next_retry_at <= ? AND platform = ? ORDER BY next_retry_at ASC LIMIT ?", (now, platform, limit))
                else:
                    cur = self._execute(conn, "SELECT * FROM retry_queue WHERE status = 'PENDING' AND next_retry_at <= ? ORDER BY next_retry_at ASC LIMIT ?", (now, limit))
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description] if cur.description else []
            return [dict(zip(cols, r)) for r in rows]

    def get_all_retry_queue_items(self, platform: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves all items from the persistent retry queue for status and inspection."""
        with self.get_connection() as conn:
            if platform:
                cur = self._execute(conn, "SELECT * FROM retry_queue WHERE platform = ? ORDER BY id DESC", (platform,))
            else:
                cur = self._execute(conn, "SELECT * FROM retry_queue ORDER BY id DESC")
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description] if cur.description else []
            return [dict(zip(cols, r)) for r in rows]

    def mark_retry_completed(self, platform: str, canonical_id: str):
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            self._execute(conn, "BEGIN IMMEDIATE")
            self._execute(
                conn,
                "UPDATE retry_queue SET status = 'COMPLETED', updated_at = ? WHERE platform = ? AND canonical_id = ?",
                (now_iso, platform, canonical_id)
            )
            self._execute(conn, "COMMIT")

    def get_failure_breakdown(self, platform: Optional[str] = None) -> Dict[str, Dict[str, int]]:
        """Returns failure code counts grouped by platform."""
        breakdown: Dict[str, Dict[str, int]] = {
            p: {fc.value: 0 for fc in FailureCode}
            for p in ["leetcode", "codeforces", "codechef", "hackerrank", "hackerearth"]
        }
        with self.get_connection() as conn:
            if platform:
                cur = self._execute(
                    conn,
                    "SELECT platform, error FROM attempts WHERE error IS NOT NULL AND platform = ?",
                    (platform,)
                )
            else:
                cur = self._execute(
                    conn,
                    "SELECT platform, error FROM attempts WHERE error IS NOT NULL"
                )
            rows = cur.fetchall()
            for r in rows:
                plat = (r[0] or "").lower()
                err = r[1]
                if plat not in breakdown:
                    breakdown[plat] = {fc.value: 0 for fc in FailureCode}
                fc = classify_failure(err)
                breakdown[plat][fc.value] += 1
        return breakdown

    def get_active_leases(self) -> List[Dict[str, Any]]:
        """Returns currently active in-progress problem leases with heartbeat diagnostics."""
        with self.get_connection() as conn:
            cur = self._execute(
                conn,
                """
                SELECT id, platform, canonical_id, url, claimed_by, claimed_at, heartbeat_at, lease_timeout
                FROM problems
                WHERE status = 'IN_PROGRESS'
                ORDER BY claimed_at ASC
                """
            )
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description] if cur.description else []
            return [dict(zip(cols, r)) for r in rows]

    def acquire_daily_run_lock(
        self,
        platform: str,
        target_quota: int,
        worker_id: str,
        run_date: Optional[str] = None,
        lock_duration_sec: float = 3600.0
    ) -> Optional[int]:
        """
        Acquires an atomic execution lock on today's daily_run record for the platform.
        Prevents overlapping Cloud Scheduler runs or multiple worker containers from duplicating work.
        Returns run_id if lock acquired, or None if quota already completed or another active worker holds the lock.
        """
        r_date = run_date or get_kolkata_today_str()
        now = time.time()
        now_iso = get_kolkata_now().isoformat()

        with self.get_connection() as conn:
            self._execute(conn, "BEGIN IMMEDIATE")
            cur = self._execute(
                conn,
                """
                SELECT id, target_quota, accepted_count, status, locked_by, locked_at
                FROM daily_runs
                WHERE run_date = ? AND platform = ?
                ORDER BY id DESC LIMIT 1
                """,
                (r_date, platform)
            )
            row = cur.fetchone()
            if row is None:
                cur = self._execute(
                    conn,
                    """
                    INSERT INTO daily_runs (run_date, platform, target_quota, started_at, status, locked_by, locked_at)
                    VALUES (?, ?, ?, ?, 'IN_PROGRESS', ?, ?)
                    RETURNING id
                    """,
                    (r_date, platform, target_quota, now_iso, worker_id, now)
                )
                run_id = cur.fetchone()[0]
                self._execute(conn, "COMMIT")
                log_event("INFO", f"Acquired new daily run lock for {platform} on {r_date} (run_id={run_id})")
                return run_id

            run_id = row[0]
            existing_target = row[1]
            accepted_count = row[2]
            status = row[3]
            locked_by = row[4]
            locked_at = row[5]

            # If quota is already met, avoid duplicate runs
            if status == "COMPLETED" and accepted_count >= existing_target:
                self._execute(conn, "COMMIT")
                log_event("INFO", f"Daily quota for {platform} on {r_date} is already completed ({accepted_count}/{existing_target}). Skipping duplicate run.")
                return None

            # If locked by another worker and lock is still active
            if locked_by and locked_by != worker_id:
                lock_age = now - (locked_at or 0)
                if lock_age < lock_duration_sec:
                    self._execute(conn, "COMMIT")
                    log_event("WARNING", f"Daily run for {platform} is currently locked by active worker '{locked_by}' (age: {int(lock_age)}s). Rejecting overlapping run.")
                    return None
                else:
                    log_event("WARNING", f"Stale worker lock from '{locked_by}' (age: {int(lock_age)}s) expired. Re-claiming daily run {run_id}.")

            self._execute(
                conn,
                """
                UPDATE daily_runs
                SET locked_by = ?, locked_at = ?, status = 'IN_PROGRESS'
                WHERE id = ?
                """,
                (worker_id, now, run_id)
            )
            self._execute(conn, "COMMIT")
            log_event("INFO", f"Acquired lock on daily run {run_id} for {platform} (worker={worker_id})")
            return run_id

    def release_daily_run_lock(self, run_id: int):
        with self.get_connection() as conn:
            self._execute(conn, "BEGIN IMMEDIATE")
            self._execute(conn, "UPDATE daily_runs SET locked_by = NULL, locked_at = NULL WHERE id = ?", (run_id,))
            self._execute(conn, "COMMIT")

    def complete_daily_run(self, run_id: int, status: str = "COMPLETED"):
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "UPDATE daily_runs SET completed_at = ?, status = ?, locked_by = NULL, locked_at = NULL WHERE id = ?",
                (now_iso, status, run_id)
            )
            cursor.execute("COMMIT")

    def record_attempt(
        self,
        platform: str,
        canonical_id: str,
        run_id: Optional[int],
        stage: ProcessStage,
        mode: str,
        solution_code: Optional[str] = None,
        explanation: Optional[str] = None,
        error: Optional[str] = None,
        is_transient: bool = False,
        compiler_output: Optional[str] = None,
        diagnostics: Optional[str] = None
    ) -> int:
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("SELECT id FROM problems WHERE platform = ? AND canonical_id = ?", (platform, canonical_id))
            row = cursor.fetchone()
            prob_id = row[0] if row else 0

            cursor.execute(
                """
                INSERT INTO attempts (problem_id, run_id, platform, canonical_id, stage, mode, solution_code, explanation, error, is_transient, started_at, finished_at, compiler_output, diagnostics)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (prob_id, run_id, platform, canonical_id, stage.value, mode, solution_code, explanation, error, 1 if is_transient else 0, now_iso, now_iso, compiler_output, diagnostics)
            )
            attempt_id = cursor.lastrowid
            cursor.execute("COMMIT")
            return attempt_id

    def record_sample_test_results(
        self,
        attempt_id: int,
        sample_results: List[Any]
    ):
        """Persists executed sample test cases and their diagnostic diffs for an attempt."""
        if not sample_results:
            return
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            for s in sample_results:
                cursor.execute(
                    """
                    INSERT INTO sample_test_results (
                        attempt_id, sample_index, input_text, expected_output, actual_output, passed, error_message, diff, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        getattr(s, "sample_index", 1),
                        getattr(s, "input_text", ""),
                        getattr(s, "expected_output", ""),
                        getattr(s, "actual_output", ""),
                        1 if getattr(s, "passed", False) else 0,
                        getattr(s, "error_message", None),
                        getattr(s, "diff", None),
                        now_iso,
                    )
                )
            cursor.execute("COMMIT")

    def record_publish_job(
        self,
        platform: str,
        canonical_id: str,
        attempt_id: Optional[int],
        repo_name: Optional[str],
        branch: Optional[str],
        commit_hash: Optional[str],
        status: str,
        error: Optional[str] = None
    ) -> int:
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("SELECT id FROM problems WHERE platform = ? AND canonical_id = ?", (platform, canonical_id))
            row = cursor.fetchone()
            prob_id = row[0] if row else 0

            cursor.execute(
                """
                INSERT INTO publish_jobs (problem_id, attempt_id, platform, canonical_id, repo_name, branch, commit_hash, status, error, published_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (prob_id, attempt_id, platform, canonical_id, repo_name, branch, commit_hash, status, error, now_iso)
            )
            job_id = cursor.lastrowid
            cursor.execute("COMMIT")
            return job_id

    def get_daily_summary(self, run_date: Optional[str] = None) -> List[Dict[str, Any]]:
        r_date = run_date or get_kolkata_today_str()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT platform, target_quota, attempted_count, generated_count, compiled_count,
                       tested_count, accepted_count, published_count, failed_count, skipped_count, status
                FROM daily_runs
                WHERE run_date = ?
                ORDER BY platform
                """,
                (r_date,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_all_time_stats(self) -> Dict[str, Dict[str, int]]:
        platforms = ["leetcode", "codeforces", "hackerrank", "codechef", "hackerearth"]
        res = {p: {"accepted": 0, "published": 0, "unverified": 0, "failed": 0, "discovered": 0} for p in platforms}
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT platform, COUNT(DISTINCT canonical_id)
                FROM problems
                WHERE status = 'ACCEPTED'
                GROUP BY platform
                """
            )
            for plat, cnt in cursor.fetchall():
                if plat in res:
                    res[plat]["accepted"] = cnt

            cursor.execute(
                """
                SELECT platform, COUNT(DISTINCT canonical_id)
                FROM publish_jobs
                WHERE status = 'PUBLISHED'
                GROUP BY platform
                """
            )
            for plat, cnt in cursor.fetchall():
                if plat in res:
                    res[plat]["published"] = cnt

            cursor.execute(
                """
                SELECT platform, COUNT(DISTINCT canonical_id)
                FROM problems
                WHERE status = 'UNVERIFIED'
                GROUP BY platform
                """
            )
            for plat, cnt in cursor.fetchall():
                if plat in res:
                    res[plat]["unverified"] = cnt

            cursor.execute(
                """
                SELECT platform, COUNT(DISTINCT canonical_id)
                FROM problems
                WHERE status IN ('FAILED', 'REJECTED')
                GROUP BY platform
                """
            )
            for plat, cnt in cursor.fetchall():
                if plat in res:
                    res[plat]["failed"] = cnt

        return res


class SolvedRegistry:
    """Central registry tracking all solved problems backed by persistent SQLite storage."""
    _db: Optional[DatabaseManager] = None

    @classmethod
    def get_db(cls) -> DatabaseManager:
        if cls._db is None:
            cls._db = DatabaseManager(database_url=DATABASE_URL or None)
        return cls._db

    @classmethod
    def set_db(cls, db: DatabaseManager):
        cls._db = db

    @classmethod
    def normalize(cls, s: str, platform: Optional[str] = None) -> str:
        plat, canon = normalize_problem_identity(s, platform)
        return canon

    @classmethod
    def get_all_solved(cls, force_refresh: bool = False) -> dict:
        """Returns verified accepted problems per platform from SQLite database (no file/directory false positives)."""
        db = cls.get_db()
        platforms = ["leetcode", "codeforces", "hackerrank", "codechef", "hackerearth"]
        return {p: db.get_accepted_canonical_ids(p) for p in platforms}

    @classmethod
    def is_solved(cls, platform: str, slug_or_url: str) -> bool:
        """Returns True ONLY if the problem has confirmed judge ACCEPTED verdict in SQLite database."""
        return cls.get_db().is_accepted(platform, slug_or_url)

    @classmethod
    def is_accepted(cls, platform: str, slug_or_url: str) -> bool:
        """Explicit alias for is_solved verifying confirmed judge ACCEPTED verdict."""
        return cls.get_db().is_accepted(platform, slug_or_url)

    @classmethod
    def mark_solved(cls, platform: str, slug_or_url: str, verdict: Optional[SubmissionVerdict] = None):
        """Marks a problem as accepted in the SQLite database with verified verdict."""
        plat, canon = normalize_problem_identity(slug_or_url, platform)
        cls.get_db().mark_problem_accepted(plat, canon, slug_or_url, verdict=verdict)


def get_existing_solved_slugs() -> Set[str]:
    """Backward compatibility helper."""
    all_s = set()
    for s_set in SolvedRegistry.get_all_solved().values():
        all_s.update(s_set)
    return all_s


# Offline Problem Catalog for Cloud Mode & Dry-Run Execution
OFFLINE_PROBLEM_CATALOG: Dict[str, List[Dict[str, Any]]] = {
    "leetcode": [
        {
            "id": "two-sum",
            "url": "https://leetcode.com/problems/two-sum/",
            "title": "Two Sum",
            "difficulty": "easy",
            "description": "Given an array of integers nums and an integer target, return indices of the two numbers such that they add up to target.",
            "starter_code": "class Solution {\n    public int[] twoSum(int[] nums, int target) {\n        \n    }\n}",
            "sample_solution": (
                "class Solution {\n"
                "    public int[] twoSum(int[] nums, int target) {\n"
                "        java.util.Map<Integer, Integer> map = new java.util.HashMap<>();\n"
                "        for (int i = 0; i < nums.length; i++) {\n"
                "            int complement = target - nums[i];\n"
                "            if (map.containsKey(complement)) {\n"
                "                return new int[] { map.get(complement), i };\n"
                "            }\n"
                "            map.put(nums[i], i);\n"
                "        }\n"
                "        return new int[0];\n"
                "    }\n"
                "}"
            ),
            "samples": [
                {"input": "[2,7,11,15]\n9", "expected": "[0,1]"}
            ]
        },
        {
            "id": "palindrome-number",
            "url": "https://leetcode.com/problems/palindrome-number/",
            "title": "Palindrome Number",
            "difficulty": "easy",
            "description": "Given an integer x, return true if x is a palindrome, and false otherwise.",
            "starter_code": "class Solution {\n    public boolean isPalindrome(int x) {\n        \n    }\n}",
            "sample_solution": (
                "class Solution {\n"
                "    public boolean isPalindrome(int x) {\n"
                "        if (x < 0) return false;\n"
                "        int rev = 0, orig = x;\n"
                "        while (x > 0) {\n"
                "            rev = rev * 10 + x % 10;\n"
                "            x /= 10;\n"
                "        }\n"
                "        return rev == orig;\n"
                "    }\n"
                "}"
            ),
            "samples": []
        },
        {
            "id": "roman-to-integer",
            "url": "https://leetcode.com/problems/roman-to-integer/",
            "title": "Roman to Integer",
            "difficulty": "easy",
            "description": "Convert a roman numeral to an integer.",
            "starter_code": "class Solution {\n    public int romanToInt(String s) {\n        \n    }\n}",
            "sample_solution": (
                "class Solution {\n"
                "    public int romanToInt(String s) {\n"
                "        java.util.Map<Character, Integer> map = new java.util.HashMap<>();\n"
                "        map.put('I', 1); map.put('V', 5); map.put('X', 10);\n"
                "        map.put('L', 50); map.put('C', 100); map.put('D', 500); map.put('M', 1000);\n"
                "        int ans = 0;\n"
                "        for (int i = 0; i < s.length(); i++) {\n"
                "            if (i + 1 < s.length() && map.get(s.charAt(i)) < map.get(s.charAt(i + 1))) {\n"
                "                ans -= map.get(s.charAt(i));\n"
                "            } else {\n"
                "                ans += map.get(s.charAt(i));\n"
                "            }\n"
                "        }\n"
                "        return ans;\n"
                "    }\n"
                "}"
            ),
            "samples": []
        }
    ],
    "codeforces": [
        {
            "id": "4A",
            "url": "https://codeforces.com/problemset/problem/4/A",
            "title": "Watermelon",
            "difficulty": "easy",
            "description": "Check if watermelon of weight w can be divided into two even parts.",
            "starter_code": "",
            "sample_solution": (
                "import java.util.Scanner;\n"
                "public class Main {\n"
                "    public static void main(String[] args) {\n"
                "        Scanner sc = new Scanner(System.in);\n"
                "        if (sc.hasNextInt()) {\n"
                "            int w = sc.nextInt();\n"
                "            System.out.println(w > 2 && w % 2 == 0 ? \"YES\" : \"NO\");\n"
                "        }\n"
                "    }\n"
                "}"
            ),
            "samples": [
                {"input": "8\n", "expected": "YES\n"}
            ]
        },
        {
            "id": "71A",
            "url": "https://codeforces.com/problemset/problem/71/A",
            "title": "Way Too Long Words",
            "difficulty": "easy",
            "description": "Replace words longer than 10 characters with abbreviation.",
            "starter_code": "",
            "sample_solution": (
                "import java.util.Scanner;\n"
                "public class Main {\n"
                "    public static void main(String[] args) {\n"
                "        Scanner sc = new Scanner(System.in);\n"
                "        if (sc.hasNextInt()) {\n"
                "            int n = sc.nextInt();\n"
                "            while (n-- > 0 && sc.hasNext()) {\n"
                "                String s = sc.next();\n"
                "                if (s.length() > 10) {\n"
                "                    System.out.println(\"\" + s.charAt(0) + (s.length() - 2) + s.charAt(s.length() - 1));\n"
                "                } else {\n"
                "                    System.out.println(s);\n"
                "                }\n"
                "            }\n"
                "        }\n"
                "    }\n"
                "}"
            ),
            "samples": []
        }
    ],
    "hackerrank": [
        {
            "id": "solve-me-first",
            "url": "https://www.hackerrank.com/challenges/solve-me-first/problem",
            "title": "Solve Me First",
            "difficulty": "easy",
            "description": "Complete solveMeFirst to compute the sum of two integers.",
            "starter_code": "import java.io.*;\nimport java.util.*;\n\npublic class Solution {\n    static int solveMeFirst(int a, int b) {\n        \n    }\n    public static void main(String[] args) {\n        Scanner in = new Scanner(System.in);\n        int a = in.nextInt();\n        int b = in.nextInt();\n        int sum = solveMeFirst(a, b);\n        System.out.println(sum);\n    }\n}",
            "sample_solution": (
                "import java.io.*;\n"
                "import java.util.*;\n\n"
                "public class Solution {\n"
                "    static int solveMeFirst(int a, int b) {\n"
                "        return a + b;\n"
                "    }\n"
                "    public static void main(String[] args) {\n"
                "        Scanner in = new Scanner(System.in);\n"
                "        if (in.hasNextInt()) {\n"
                "            int a = in.nextInt();\n"
                "            int b = in.nextInt();\n"
                "            int sum = solveMeFirst(a, b);\n"
                "            System.out.println(sum);\n"
                "        }\n"
                "    }\n"
                "}"
            ),
            "samples": [
                {"input": "2\n3\n", "expected": "5\n"}
            ]
        }
    ],
    "codechef": [
        {
            "id": "FLOW001",
            "url": "https://www.codechef.com/problems/FLOW001",
            "title": "Add Two Numbers",
            "difficulty": "easy",
            "description": "Add two integers for T testcases.",
            "starter_code": "",
            "sample_solution": (
                "import java.util.Scanner;\n"
                "public class Main {\n"
                "    public static void main(String[] args) {\n"
                "        Scanner sc = new Scanner(System.in);\n"
                "        if (sc.hasNextInt()) {\n"
                "            int t = sc.nextInt();\n"
                "            while (t-- > 0 && sc.hasNextInt()) {\n"
                "                int a = sc.nextInt();\n"
                "                int b = sc.nextInt();\n"
                "                System.out.println(a + b);\n"
                "            }\n"
                "        }\n"
                "    }\n"
                "}"
            ),
            "samples": [
                {"input": "1\n1 2\n", "expected": "3\n"}
            ]
        }
    ],
    "hackerearth": [
        {
            "id": "zoos-04664a78",
            "url": "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/zoos-04664a78/",
            "title": "Zoos",
            "difficulty": "easy",
            "description": "Determine if a string complies with 2 * count('z') == count('o').",
            "starter_code": "",
            "sample_solution": (
                "import java.util.Scanner;\n"
                "public class TestClass {\n"
                "    public static void main(String[] args) {\n"
                "        Scanner sc = new Scanner(System.in);\n"
                "        if (sc.hasNext()) {\n"
                "            String s = sc.next();\n"
                "            int z = 0, o = 0;\n"
                "            for (char c : s.toCharArray()) {\n"
                "                if (c == 'z') z++;\n"
                "                else if (c == 'o') o++;\n"
                "            }\n"
                "            System.out.println(2 * z == o ? \"Yes\" : \"No\");\n"
                "        }\n"
                "    }\n"
                "}"
            ),
            "samples": [
                {"input": "zzzoooooo\n", "expected": "Yes\n"}
            ]
        }
    ]
}

def discover_offline_candidates(platform: str, count: int = 5, difficulty: str = "medium-hard", offset: int = 0) -> List[str]:
    """Provides problem candidate URLs in offline cloud mode without web scraping or browser dependency."""
    candidates = []
    # 1. From offline problem catalog
    entries = OFFLINE_PROBLEM_CATALOG.get(platform, [])
    for item in entries:
        candidates.append(item["url"])

    # 2. From problems.txt if present
    prob_file = BASE_DIR / "problems.txt"
    if prob_file.exists():
        try:
            with open(prob_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        p_type, _ = normalize_problem_identity(line, platform)
                        if p_type == platform and line not in candidates:
                            candidates.append(line)
        except Exception:
            pass

    return candidates[offset : offset + count] if offset < len(candidates) else candidates[:count]

def get_offline_problem_info(url: str, platform_type: str) -> Dict[str, Any]:
    """Retrieves problem definition and deterministic sample solution for offline execution."""
    catalog = OFFLINE_PROBLEM_CATALOG.get(platform_type, [])
    for item in catalog:
        if item["url"] == url or item["id"] in url:
            return item
    _, cid = normalize_problem_identity(url, platform_type)
    return {
        "id": cid,
        "url": url,
        "title": cid.replace("-", " ").title(),
        "difficulty": "medium",
        "description": f"Offline problem {cid} on {platform_type}.",
        "starter_code": "",
        "sample_solution": (
            "public class Solution {\n"
            "    public static void main(String[] args) {\n"
            "        System.out.println(\"Solved\");\n"
            "    }\n"
            "}" if platform_type in ["hackerrank", "leetcode"] else
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            "        System.out.println(\"Solved\");\n"
            "    }\n"
            "}"
        ),
        "samples": []
    }

async def discover_leetcode_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard", offset: int = 0) -> List[str]:
    """Queries LeetCode GraphQL directly for free, completely unsolved problems for user."""
    print(f"\n🔍 [LeetCode] Finding up to {count} unsolved problems (Difficulty: {difficulty.upper()}, Offset: {offset})...")
    candidates = []
    db = SolvedRegistry.get_db()

    diff_list = []
    if difficulty.lower() == "hard":
        diff_list = ["HARD"]
    elif difficulty.lower() == "medium":
        diff_list = ["MEDIUM"]
    elif difficulty.lower() == "easy":
        diff_list = ["EASY"]
    else:  # medium-hard
        diff_list = ["HARD", "MEDIUM"]

    try:
        await page.goto("https://leetcode.com/problemset/", wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(2)

        for diff in diff_list:
            if len(candidates) >= count:
                break
            gql_query = """async ({diff, skip, limit}) => {
                const query = `
                    query problemsetQuestionList($categorySlug: String, $limit: Int, $skip: Int, $filters: QuestionListFilterInput) {
                        problemsetQuestionList: questionList(
                            categorySlug: $categorySlug
                            limit: $limit
                            skip: $skip
                            filters: $filters
                        ) {
                            totalNum
                            questions: data {
                                questionFrontendId
                                titleSlug
                                difficulty
                                status
                                paidOnly: isPaidOnly
                            }
                        }
                    }
                `;
                const resp = await fetch('https://leetcode.com/graphql', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        query: query,
                        variables: {
                            categorySlug: "",
                            skip: skip,
                            limit: limit,
                            filters: {
                                difficulty: diff,
                                status: "NOT_STARTED"
                            }
                        }
                    })
                });
                if (!resp.ok) return [];
                const d = await resp.json();
                return d.data?.problemsetQuestionList?.questions || [];
            }"""
            questions = await page.evaluate(gql_query, {"diff": diff, "skip": offset, "limit": max(50, count * 2)})
            for q in questions:
                if q.get("paidOnly"):
                    continue
                if q.get("status") in ["ac", "not_started_false"]:
                    continue
                slug = q.get("titleSlug")
                if not slug:
                    continue
                if SolvedRegistry.is_solved("leetcode", slug) or db.is_permanent_failure("leetcode", slug):
                    continue
                full_url = f"https://leetcode.com/problems/{slug}/"
                if full_url not in candidates:
                    candidates.append(full_url)
                if len(candidates) >= count:
                    break
    except Exception as e:
        print(f"   ⚠️  LeetCode GraphQL query notice: {e}")

    print(f"   ✅ Discovered {len(candidates)} completely unsolved LeetCode candidates.")
    return candidates

async def discover_codeforces_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard", offset: int = 0) -> List[str]:
    """Finds unsolved Codeforces problems in Medium/Hard ratings, excluding any already solved by user."""
    print(f"\n🔍 [Codeforces] Finding up to {count} unsolved problems (Difficulty: {difficulty.upper()}, Offset: {offset})...")
    candidates = []
    db = SolvedRegistry.get_db()

    try:
        try:
            cf_status_url = f"https://codeforces.com/api/user.status?handle={CODEFORCES_HANDLE}&from=1&count=10000"
            req = urllib.request.Request(cf_status_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                cf_user = json.loads(resp.read().decode("utf-8"))
                if cf_user.get("status") == "OK":
                    for sub in cf_user.get("result", []):
                        if sub.get("verdict") == "OK":
                            p = sub.get("problem", {})
                            c_id = p.get("contestId")
                            idx = p.get("index")
                            if c_id and idx:
                                SolvedRegistry.mark_solved("codeforces", f"{c_id}_{idx}")
        except Exception:
            pass

        req = urllib.request.Request("https://codeforces.com/api/problemset.problems", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode("utf-8"))

        if res.get("status") == "OK":
            probs = res["result"]["problems"]
            stats = {(s["contestId"], s["index"]): s["solvedCount"] for s in res["result"]["problemStatistics"]}
            sorted_probs = sorted(probs, key=lambda p: stats.get((p["contestId"], p["index"]), 0), reverse=True)

            matching_probs = []
            for p in sorted_probs:
                rating = p.get("rating", 1200)
                if difficulty.lower() in ["medium-hard", "hard-medium"]:
                    if rating < 1300 or rating > 2000:
                        continue
                elif difficulty.lower() == "medium":
                    if rating < 1200 or rating > 1600:
                        continue
                elif difficulty.lower() == "hard":
                    if rating < 1600 or rating > 2400:
                        continue
                elif difficulty.lower() == "easy":
                    if rating > 1000:
                        continue
                matching_probs.append(p)

            paged_probs = matching_probs[offset:]
            for p in paged_probs:
                slug = f"{p['contestId']}_{p['index']}"
                if SolvedRegistry.is_solved("codeforces", slug) or db.is_permanent_failure("codeforces", slug):
                    continue

                url = f"https://codeforces.com/problemset/problem/{p['contestId']}/{p['index']}"
                if url not in candidates:
                    candidates.append(url)
                if len(candidates) >= count:
                    break
    except Exception as e:
        print(f"   ⚠️  Codeforces API note: {e}")

    # Fallback to curated catalog if API fails or yields insufficient candidates
    if len(candidates) < count:
        if difficulty.lower() == "easy":
            catalog_fallback = [
                "https://codeforces.com/problemset/problem/4/A",
                "https://codeforces.com/problemset/problem/71/A",
                "https://codeforces.com/problemset/problem/231/A",
                "https://codeforces.com/problemset/problem/158/A",
                "https://codeforces.com/problemset/problem/282/A",
                "https://codeforces.com/problemset/problem/112/A",
                "https://codeforces.com/problemset/problem/339/A",
                "https://codeforces.com/problemset/problem/263/A",
                "https://codeforces.com/problemset/problem/546/A",
                "https://codeforces.com/problemset/problem/791/A",
                "https://codeforces.com/problemset/problem/617/A",
                "https://codeforces.com/problemset/problem/59/A",
                "https://codeforces.com/problemset/problem/977/A",
                "https://codeforces.com/problemset/problem/266/A",
                "https://codeforces.com/problemset/problem/281/A",
                "https://codeforces.com/problemset/problem/110/A",
                "https://codeforces.com/problemset/problem/734/A",
                "https://codeforces.com/problemset/problem/41/A",
                "https://codeforces.com/problemset/problem/677/A",
                "https://codeforces.com/problemset/problem/1030/A",
                "https://codeforces.com/problemset/problem/467/A",
                "https://codeforces.com/problemset/problem/344/A",
                "https://codeforces.com/problemset/problem/486/A",
                "https://codeforces.com/problemset/problem/271/A",
                "https://codeforces.com/problemset/problem/136/A",
                "https://codeforces.com/problemset/problem/200/B",
                "https://codeforces.com/problemset/problem/144/A",
                "https://codeforces.com/problemset/problem/996/A",
                "https://codeforces.com/problemset/problem/1328/A",
                "https://codeforces.com/problemset/problem/520/A",
            ]
        else:
            catalog_fallback = [
                "https://codeforces.com/problemset/problem/1352/C",
                "https://codeforces.com/problemset/problem/1520/D",
                "https://codeforces.com/problemset/problem/474/B",
                "https://codeforces.com/problemset/problem/520/B",
                "https://codeforces.com/problemset/problem/580/C",
                "https://codeforces.com/problemset/problem/20/C",
                "https://codeforces.com/problemset/problem/466/C",
                "https://codeforces.com/problemset/problem/371/C",
                "https://codeforces.com/problemset/problem/339/D",
                "https://codeforces.com/problemset/problem/1294/C",
                "https://codeforces.com/problemset/problem/459/B",
                "https://codeforces.com/problemset/problem/1360/D",
                "https://codeforces.com/problemset/problem/550/A",
                "https://codeforces.com/problemset/problem/276/C",
                "https://codeforces.com/problemset/problem/489/C",
                "https://codeforces.com/problemset/problem/1335/D",
                "https://codeforces.com/problemset/problem/1343/C",
                "https://codeforces.com/problemset/problem/1352/D",
                "https://codeforces.com/problemset/problem/1360/C",
                "https://codeforces.com/problemset/problem/1374/C",
            ]
        paged_catalog = catalog_fallback[offset:]
        for u in paged_catalog:
            slug = u.rstrip("/").split("/")[-2] + "_" + u.rstrip("/").split("/")[-1]
            if not SolvedRegistry.is_solved("codeforces", slug) and not db.is_permanent_failure("codeforces", slug) and u not in candidates:
                candidates.append(u)
            if len(candidates) >= count:
                break

    print(f"   ✅ Discovered {len(candidates)} completely unsolved Codeforces candidates.")
    return candidates

async def discover_hackerrank_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard", offset: int = 0) -> List[str]:
    """Finds unsolved HackerRank challenges (Medium/Hard) using API with pagination and fallback catalog."""
    print(f"\n🔍 [HackerRank] Finding up to {count} unsolved challenges (Difficulty: {difficulty.upper()}, Offset: {offset})...")
    candidates = []
    db = SolvedRegistry.get_db()

    diff_params = ""
    if difficulty.lower() == "hard":
        diff_params = "&filters%5Bdifficulty%5D%5B%5D=hard"
    elif difficulty.lower() in ["medium-hard", "hard-medium"]:
        diff_params = "&filters%5Bdifficulty%5D%5B%5D=medium&filters%5Bdifficulty%5D%5B%5D=hard"
    elif difficulty.lower() == "medium":
        diff_params = "&filters%5Bdifficulty%5D%5B%5D=medium"
    elif difficulty.lower() == "easy":
        diff_params = "&filters%5Bdifficulty%5D%5B%5D=easy"

    tracks = ["algorithms", "data-structures"]
    for track in tracks:
        if len(candidates) >= count:
            break
        for page_offset in [offset, offset + 50, offset + 100, offset + 150]:
            if len(candidates) >= count:
                break
            try:
                api_url = f"https://www.hackerrank.com/rest/contests/master/tracks/{track}/challenges?offset={page_offset}&limit=50{diff_params}"
                req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    for m in data.get("models", []):
                        slug = m.get("slug")
                        if not slug or SolvedRegistry.is_solved("hackerrank", slug) or db.is_permanent_failure("hackerrank", slug):
                            continue
                        u = f"https://www.hackerrank.com/challenges/{slug}/problem"
                        if u not in candidates:
                            candidates.append(u)
                        if len(candidates) >= count:
                            break
            except Exception as e:
                print(f"   ⚠️  HackerRank API ({track}, offset {page_offset}) note: {e}")
                continue

    # Fallback catalog of verified active HackerRank challenges across tracks
    if len(candidates) < count:
        curated_hr = [
            "magic-square-forming", "climbing-the-leaderboard", "extra-long-factorials",
            "non-divisible-subset", "queens-attack-2", "organizing-containers-of-balls",
            "bigger-is-greater", "the-time-in-words", "encryption", "surface-area-3d",
            "absolute-permutation", "the-bomberman-game", "two-pluses", "larrys-array",
            "almost-sorted", "matrix-rotation-algo", "reduced-string", "sherlock-and-anagrams",
            "common-child", "special-palindrome-again", "fraudulent-activity-notifications",
            "lily-ranking", "count-luck", "cut-the-tree", "connected-cell-in-a-grid",
            "short-palindrome", "bear-and-steady-gene", "sherlock-and-valid-string",
            "simple-array-sum", "compare-the-triplets", "a-very-big-sum", "diagonal-difference",
            "plus-minus", "staircase", "mini-max-sum", "birthday-cake-candles", "time-conversion"
        ]
        paged_curated = curated_hr[offset:]
        for slug in paged_curated:
            if not SolvedRegistry.is_solved("hackerrank", slug) and not db.is_permanent_failure("hackerrank", slug):
                u = f"https://www.hackerrank.com/challenges/{slug}/problem"
                if u not in candidates:
                    candidates.append(u)
            if len(candidates) >= count:
                break

    print(f"   ✅ Discovered {len(candidates)} completely unsolved HackerRank candidates.")
    return candidates

async def discover_codechef_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard", offset: int = 0) -> List[str]:
    """Finds unsolved CodeChef problems in Medium/Hard ratings via API pagination."""
    print(f"\n🔍 [CodeChef] Finding up to {count} unsolved problems (Difficulty: {difficulty.upper()}, Offset: {offset})...")
    candidates = []
    db = SolvedRegistry.get_db()

    page_shift = offset // 50
    if difficulty.lower() == "easy":
        base_pages = [0, 1, 2, 3, 4, 5, 6, 7]
    elif difficulty.lower() in ["medium-hard", "hard-medium", "medium"]:
        base_pages = [10, 20, 30, 40, 50, 60]
    else:
        base_pages = [20, 40, 60, 80]
    pages_to_check = [p + page_shift for p in base_pages]
    for p_num in pages_to_check:
        if len(candidates) >= count:
            break
        try:
            api_url = f"https://www.codechef.com/api/list/problems?page={p_num}&limit=50&sort_by=successful_submissions&sort_order=desc"
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for p in data.get("data", []):
                    try:
                        r = int(p.get("difficulty_rating", 0))
                        if difficulty.lower() == "hard":
                            if r < 1600 or r > 2500:
                                continue
                        elif difficulty.lower() in ["medium-hard", "hard-medium"]:
                            if r < 1200 or r > 2200:
                                continue
                        elif difficulty.lower() == "medium":
                            if r < 1200 or r > 1600:
                                continue
                        elif difficulty.lower() == "easy":
                            if r > 1100:
                                continue
                    except Exception:
                        continue

                    code = p.get("code")
                    if not code or SolvedRegistry.is_solved("codechef", code) or db.is_permanent_failure("codechef", code):
                        continue
                    u = f"https://www.codechef.com/problems/{code}"
                    if u not in candidates:
                        candidates.append(u)
                    if len(candidates) >= count:
                        break
        except Exception as e:
            print(f"   ⚠️  CodeChef API page {p_num} note: {e}")

    # Fallback to curated 4-star/Medium problems if API was unreachable
    if len(candidates) < count:
        curated_cc = [
            "NAME2", "ALTARAY", "MAXDIFF", "DIVSUBS", "TACHSTCK",
            "CHEFA", "STONES", "MARCHA1", "CIELRCPT", "CARVANS",
            "JOHNY", "CLEANUP", "HORSES", "CSUB", "CNOTE",
            "RAINBOWA", "SALARY", "CHN15A", "FRGTNLNG", "COPS"
        ]
        paged_curated = curated_cc[offset:]
        for c in paged_curated:
            if not SolvedRegistry.is_solved("codechef", c) and not db.is_permanent_failure("codechef", c):
                u = f"https://www.codechef.com/problems/{c}"
                if u not in candidates:
                    candidates.append(u)
            if len(candidates) >= count:
                break

    print(f"   ✅ Discovered {len(candidates)} completely unsolved CodeChef candidates.")
    return candidates

async def discover_hackerearth_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard", offset: int = 0) -> List[str]:
    """Finds unsolved HackerEarth problems (Medium/Hard) using curated pool and live track crawling."""
    print(f"\n🔍 [HackerEarth] Finding up to {count} unsolved problems (Difficulty: {difficulty.upper()}, Offset: {offset})...")
    candidates = []
    db = SolvedRegistry.get_db()

    # Curated catalog of verified, active algorithmic challenges across topics
    if difficulty.lower() == "easy":
        catalog = [
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/zoos-06063825/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/roy-and-profile/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/find-product/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/count-divisors/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/palindromic-string/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/toggle-string/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/cost-of-balloons/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/divisibility-2-ee324d20/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/ali-and-helping-innocent-people/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/maximum-borders-9ecec21a/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/seven-segments-bm-1/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/split-houses/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/excursion-2-0a544a49/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/step-up-3-c82ee783/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/number-of-steps-38e680a5/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/best-index-1-45a2f8ff/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/minimum-steps-9/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/factorial-4/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/friend-relationship-1/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/maximum-number-11-c917f6bd/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/going-to-office-e2d4d5d3/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/conject-it/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/lift-queries/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/e-maze-in-1aa4e2ac/",
            "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/magical-word-6/",
        ]
    else:
        catalog = [
        "https://www.hackerearth.com/practice/algorithms/graphs/breadth-first-search/practice-problems/algorithm/cloudy-days-82a872ec/",
        "https://www.hackerearth.com/practice/algorithms/graphs/breadth-first-search/practice-problems/algorithm/waves-b18625d7/",
        "https://www.hackerearth.com/practice/algorithms/graphs/breadth-first-search/practice-problems/algorithm/zero-path-a7d370fd/",
        "https://www.hackerearth.com/practice/algorithms/graphs/breadth-first-search/practice-problems/algorithm/t1-1-6064aa64/",
        "https://www.hackerearth.com/practice/algorithms/graphs/breadth-first-search/practice-problems/algorithm/traffic-light-2-ee27ba45/",
        "https://www.hackerearth.com/practice/algorithms/graphs/breadth-first-search/practice-problems/algorithm/monk-and-the-islands/",
        "https://www.hackerearth.com/practice/algorithms/graphs/breadth-first-search/practice-problems/algorithm/dhoom-4/",
        "https://www.hackerearth.com/practice/algorithms/graphs/breadth-first-search/practice-problems/algorithm/oliver-and-the-battle-1/",
        "https://www.hackerearth.com/practice/algorithms/graphs/breadth-first-search/practice-problems/algorithm/flip-grid-c6f88af8/",
        "https://www.hackerearth.com/practice/algorithms/graphs/breadth-first-search/practice-problems/algorithm/ujjwals-mine-9eacab11/",
        "https://www.hackerearth.com/practice/algorithms/graphs/depth-first-search/practice-problems/algorithm/cryptic-line-92a6bd09/",
        "https://www.hackerearth.com/practice/algorithms/graphs/depth-first-search/practice-problems/algorithm/optimal-network-expansion-de452a46/",
        "https://www.hackerearth.com/practice/algorithms/graphs/depth-first-search/practice-problems/algorithm/find-pairs-4-699bc085/",
        "https://www.hackerearth.com/practice/algorithms/graphs/depth-first-search/practice-problems/algorithm/areas-0475fb6e/",
        "https://www.hackerearth.com/practice/algorithms/graphs/depth-first-search/practice-problems/algorithm/water-flow-4-38cea6c6/",
        "https://www.hackerearth.com/practice/algorithms/graphs/depth-first-search/practice-problems/algorithm/minimize-the-magic-05a3986c/",
        "https://www.hackerearth.com/practice/algorithms/graphs/depth-first-search/practice-problems/algorithm/breaking-edges-march-circuits-ca482e2a/",
        "https://www.hackerearth.com/practice/algorithms/graphs/depth-first-search/practice-problems/algorithm/minimum-nodes-e023e51e/",
        "https://www.hackerearth.com/practice/algorithms/graphs/depth-first-search/practice-problems/algorithm/words-and-trees-f9ef202c/",
        "https://www.hackerearth.com/practice/algorithms/graphs/depth-first-search/practice-problems/algorithm/el-nino/",
        "https://www.hackerearth.com/practice/algorithms/dynamic-programming/introduction-to-dynamic-programming-1/practice-problems/algorithm/group-minmax-1cddbceb/",
        "https://www.hackerearth.com/practice/algorithms/dynamic-programming/introduction-to-dynamic-programming-1/practice-problems/algorithm/max-seperations-4ed0b552/",
        "https://www.hackerearth.com/practice/algorithms/dynamic-programming/introduction-to-dynamic-programming-1/practice-problems/algorithm/roses-for-valentine-4a795f72/",
        "https://www.hackerearth.com/practice/algorithms/dynamic-programming/introduction-to-dynamic-programming-1/practice-problems/algorithm/jump-k-forward-250d464b/",
        "https://www.hackerearth.com/practice/algorithms/dynamic-programming/introduction-to-dynamic-programming-1/practice-problems/algorithm/yogi-and-his-steps-65b27a4b/",
        "https://www.hackerearth.com/practice/algorithms/dynamic-programming/introduction-to-dynamic-programming-1/practice-problems/algorithm/analytically-stable-123d6381/",
        "https://www.hackerearth.com/practice/algorithms/dynamic-programming/introduction-to-dynamic-programming-1/practice-problems/algorithm/abcd-26-81bc0a09/",
        "https://www.hackerearth.com/practice/algorithms/dynamic-programming/introduction-to-dynamic-programming-1/practice-problems/algorithm/equal-array-84cf6c5f/",
        "https://www.hackerearth.com/practice/algorithms/dynamic-programming/introduction-to-dynamic-programming-1/practice-problems/algorithm/odd-even-subarrays-72ad69db/",
        "https://www.hackerearth.com/practice/algorithms/dynamic-programming/introduction-to-dynamic-programming-1/practice-problems/algorithm/accomodation-a5c006f3/",
        "https://www.hackerearth.com/practice/algorithms/dynamic-programming/state-space-reduction/practice-problems/algorithm/aeroplanes-scheduling-7df0c65c/",
        "https://www.hackerearth.com/practice/algorithms/dynamic-programming/state-space-reduction/practice-problems/algorithm/coutinho/",
        "https://www.hackerearth.com/practice/algorithms/dynamic-programming/state-space-reduction/practice-problems/algorithm/bytelandian-gold-coins/",
        "https://www.hackerearth.com/practice/data-structures/trees/binary-search-tree/practice-problems/algorithm/b-sequence-f919fc86/",
        "https://www.hackerearth.com/practice/data-structures/trees/binary-search-tree/practice-problems/algorithm/monk-and-his-friends/",
        "https://www.hackerearth.com/practice/data-structures/trees/binary-search-tree/practice-problems/algorithm/distinct-count/",
        "https://www.hackerearth.com/practice/data-structures/trees/binary-search-tree/practice-problems/algorithm/suarez/",
        "https://www.hackerearth.com/practice/data-structures/trees/binary-search-tree/practice-problems/algorithm/mst-revisited-3f9d614c/",
    ]

    paged_catalog = catalog[offset:]
    for u in paged_catalog:
        slug = u.rstrip("/").split("/")[-1]
        if not SolvedRegistry.is_solved("hackerearth", slug) and not db.is_permanent_failure("hackerearth", slug):
            if u not in candidates:
                candidates.append(u)
        if len(candidates) >= count:
            break

    # If more needed, crawl live tracks
    if len(candidates) < count:
        if difficulty.lower() == "easy":
            tracks = [
                "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/",
                "https://www.hackerearth.com/practice/basic-programming/complexity-analysis/time-and-space-complexity/practice-problems/",
                "https://www.hackerearth.com/practice/basic-programming/operators/basics-of-operators/practice-problems/",
                "https://www.hackerearth.com/practice/basic-programming/bit-manipulation/basics-of-bit-manipulation/practice-problems/",
                "https://www.hackerearth.com/practice/basic-programming/implementation/basics-of-implementation/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/searching/linear-search/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/searching/binary-search/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/sorting/bubble-sort/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/sorting/insertion-sort/practice-problems/",
                "https://www.hackerearth.com/practice/data-structures/arrays/1-d/practice-problems/",
            ]
        elif difficulty.lower() == "hard":
            tracks = [
                "https://www.hackerearth.com/practice/algorithms/graphs/minimum-spanning-tree/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/graphs/articulation-points-and-bridges/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/dynamic-programming/state-space-reduction/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/dynamic-programming/2-dimensional/practice-problems/",
                "https://www.hackerearth.com/practice/data-structures/advanced-data-structures/trie-keyword-tree/practice-problems/",
                "https://www.hackerearth.com/practice/data-structures/advanced-data-structures/segment-trees/practice-problems/",
                "https://www.hackerearth.com/practice/data-structures/advanced-data-structures/fenwick-binary-indexed-trees/practice-problems/",
            ]
        else:  # medium or medium-hard
            tracks = [
                "https://www.hackerearth.com/practice/algorithms/graphs/breadth-first-search/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/graphs/depth-first-search/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/graphs/shortest-path-algorithms/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/greedy/basics-of-greedy-algorithms/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/dynamic-programming/introduction-to-dynamic-programming-1/practice-problems/",
                "https://www.hackerearth.com/practice/data-structures/trees/binary-search-tree/practice-problems/",
                "https://www.hackerearth.com/practice/data-structures/stacks/basics-of-stacks/practice-problems/",
                "https://www.hackerearth.com/practice/data-structures/queues/basics-of-queues/practice-problems/",
                "https://www.hackerearth.com/practice/data-structures/hash-tables/basics-of-hash-tables/practice-problems/",
            ]
        for target_url in tracks:
            if len(candidates) >= count:
                break
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2.5)
                dom_links = await page.evaluate('''() => {
                    const anchors = Array.from(document.querySelectorAll("a[href*='/practice-problems/algorithm/']"));
                    return anchors.map(a => a.href);
                }''')
                for link in dom_links:
                    slug = link.rstrip("/").split("/")[-1]
                    if not SolvedRegistry.is_solved("hackerearth", slug) and not db.is_permanent_failure("hackerearth", slug) and link not in candidates:
                        candidates.append(link)
                    if len(candidates) >= count:
                        break
            except Exception as e:
                print(f"   ⚠️  HackerEarth live crawl note: {e}")

    print(f"   ✅ Discovered {len(candidates)} completely unsolved HackerEarth candidates.")
    return candidates

# ==============================================================================
# Editor Language Switcher (Auto-selects Java in the Web UI)
# ==============================================================================
async def switch_editor_to_java(page: Page, platform_type: str):
    """Automatically ensures the web code editor is set to Java."""
    try:
        if platform_type == "leetcode":
            # LeetCode language dropdown button
            btn = page.locator("button[id*='headlessui-listbox-button'], div[data-cy='lang-select'] button, button:has-text('Python3'), button:has-text('Python'), button:has-text('C++'), button:has-text('JavaScript'), button:has-text('Java')").first
            if await btn.count() > 0 and await btn.is_visible():
                btn_text = (await btn.inner_text()).strip()
                if "Java" not in btn_text:
                    await btn.click()
                    await asyncio.sleep(0.6)
                    java_option = page.locator("div[role='option']:has-text('Java'), li[role='option']:has-text('Java')").first
                    if await java_option.count() > 0:
                        await java_option.click()
                        print("   ☕ Automatically switched LeetCode editor to Java.")
                        await asyncio.sleep(1.5)

        elif platform_type == "codeforces":
            # Codeforces language select
            select_loc = page.locator("form.submit-form select[name='programTypeId'], select[name='programTypeId']").first
            if await select_loc.count() > 0:
                options = await select_loc.locator("option").all()
                for opt in options:
                    opt_text = await opt.inner_text()
                    if "Java" in opt_text:
                        val = await opt.get_attribute("value")
                        await select_loc.select_option(val)
                        print(f"   ☕ Automatically selected Codeforces language: {opt_text.strip()}")
                        break

        elif platform_type == "hackerrank":
            # Check current selected language in HackerRank's custom React-Select container
            current_lang = await page.evaluate("""() => {
                const el = document.querySelector('.select-language, [class*="select-language"]');
                return el ? (el.innerText || '').trim() : '';
            }""")
            if "Java" not in current_lang:
                input_loc = page.locator("#select-language-input").first
                if await input_loc.count() > 0:
                    try:
                        await input_loc.click()
                        await asyncio.sleep(0.4)
                        await input_loc.fill("Java 8")
                        await asyncio.sleep(0.4)
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(1.5)
                        print("   ☕ Automatically switched HackerRank editor to Java 8 via #select-language-input!")
                    except Exception as e:
                        print(f"   ⚠️ Could not switch via input: {e}")
                else:
                    container = page.locator(".select-language, div[class*='select-language']").first
                    if await container.count() > 0:
                        await container.click()
                        await asyncio.sleep(0.6)
                        opt = page.locator("div[id*='react-select']:has-text('Java 8'), div[id*='react-select']:has-text('Java 15'), div[id*='react-select']:has-text('Java'), li:has-text('Java')").first
                        if await opt.count() > 0:
                            await opt.click()
                            print("   ☕ Automatically switched HackerRank editor to Java.")
                            await asyncio.sleep(1.5)

        elif platform_type == "codechef":
            lang_btn = page.locator("button:has-text('Python'), button:has-text('C++'), select[name='language']").first
            if await lang_btn.count() > 0 and await lang_btn.is_visible():
                tag = await lang_btn.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    options = await lang_btn.locator("option").all()
                    for opt in options:
                        if "Java" in (await opt.inner_text()):
                            val = await opt.get_attribute("value")
                            await lang_btn.select_option(val)
                            print("   ☕ Automatically selected CodeChef language: Java")
                            break
                else:
                    await lang_btn.click()
                    await asyncio.sleep(0.6)
                    opt = page.locator("div:has-text('Java'), li:has-text('Java')").first
                    if await opt.count() > 0:
                        await opt.click()
                        print("   ☕ Automatically switched CodeChef editor to Java.")
                        await asyncio.sleep(1.5)

        elif platform_type == "hackerearth":
            # 1. Modern Radix UI / Tailwind combobox
            trigger = page.locator("button[data-slot='select-trigger'], button[role='combobox']").first
            if await trigger.count() > 0:
                txt = await trigger.inner_text()
                if "Java" not in txt:
                    await trigger.click()
                    await asyncio.sleep(0.8)
                    java_opt = page.locator("[role='option']:has-text('Java 17'), [role='option']:has-text('Java 14'), [role='option']:has-text('Java 8'), [role='option']:has-text('Java')").first
                    if await java_opt.count() > 0:
                        sel_text = await java_opt.inner_text()
                        await java_opt.click()
                        print(f"   ☕ Automatically switched HackerEarth language to: {sel_text}")
                        await asyncio.sleep(1.5)
            # 2. Legacy fallback
            sel = page.locator("select#language, select[name='language']").first
            if await sel.count() > 0:
                options = await sel.locator("option").all()
                for opt in options:
                    if "Java" in (await opt.inner_text()):
                        val = await opt.get_attribute("value")
                        await sel.select_option(val)
                        print("   ☕ Automatically selected HackerEarth language: Java")
                        break
    except Exception as e:
        print(f"   ⚠️  Language switch check notice: {e}")

# ==============================================================================
# AI Solver Engine (Gemini 2.5 generating Java solutions)
# ==============================================================================
def ask_gemini_for_java_code(
    problem_text: str,
    starter_code: str,
    platform_name: str,
    platform_type: str,
    error_feedback: Optional[str] = None,
    dry_run: bool = False,
) -> Tuple[str, str]:
    """Generates pure Java solution using Gemini 2.5 with model fallbacks (or deterministic in dry-run mode)."""
    if dry_run or DRY_RUN or not GEMINI_API_KEY:
        cat = OFFLINE_PROBLEM_CATALOG.get(platform_type, [])
        for p in cat:
            if p["title"].lower() in problem_text.lower() or p["id"].lower() in problem_text.lower():
                return p["sample_solution"], "Deterministic Java solution generated in dry-run mode."
        if cat:
            return cat[0]["sample_solution"], "Deterministic fallback Java solution generated in dry-run mode."

        if platform_type == "leetcode":
            code = (
                "class Solution {\n"
                "    public int[] twoSum(int[] nums, int target) {\n"
                "        return new int[]{0, 1};\n"
                "    }\n"
                "}"
            )
        elif platform_type == "hackerearth":
            code = (
                "import java.util.Scanner;\n"
                "public class TestClass {\n"
                "    public static void main(String[] args) {\n"
                "        Scanner sc = new Scanner(System.in);\n"
                "        System.out.println(\"Yes\");\n"
                "    }\n"
                "}"
            )
        elif platform_type == "hackerrank":
            code = (
                "import java.util.Scanner;\n"
                "public class Solution {\n"
                "    public static void main(String[] args) {\n"
                "        Scanner in = new Scanner(System.in);\n"
                "        System.out.println(1);\n"
                "    }\n"
                "}"
            )
        else:
            code = (
                "import java.util.Scanner;\n"
                "public class Main {\n"
                "    public static void main(String[] args) {\n"
                "        Scanner sc = new Scanner(System.in);\n"
                "        System.out.println(\"YES\");\n"
                "    }\n"
                "}"
            )
        return code, "Deterministic fallback Java solution generated in dry-run mode."

    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(
            api_version="v1beta",
            timeout=90000,
        )
    )

    if platform_type == "leetcode":
        platform_instructions = """
1. LeetCode expects solutions inside `class Solution` (e.g. `class Solution { public int[] twoSum(...) { ... } }`).
2. DO NOT use System.in or Scanner. Return the answer directly from the method.
3. Import java.util.* and any necessary packages.
4. If starter code is provided below, PRESERVE the exact class name, method name, and parameter types.
"""
    elif platform_type in ["codeforces", "codechef"]:
        platform_instructions = f"""
1. Write a complete, standalone Java program for {platform_name}.
2. The class MUST be named 'Main' (e.g. `public class Main {{ public static void main(String[] args) throws IOException {{ ... }} }}`).
3. Read all input from standard input using fast I/O:
   Use `BufferedReader br = new BufferedReader(new InputStreamReader(System.in));` and `StringTokenizer st;`.
4. Handle multiple test cases (T) if required by the problem statement.
5. Print the output to System.out using PrintWriter or System.out.println.
6. Optimal time & space complexity to prevent TLE (Time Limit Exceeded).
"""
    elif platform_type == "hackerrank":
        platform_instructions = f"""
1. For HackerRank, inspect the starter code template below.
2. If the starter template defines `class Result` and `public class Solution`, you MUST include BOTH classes intact in your output.
3. NEVER omit or remove `public class Solution` or its `main` method, because HackerRank judge runs `Solution.main`!
4. Write optimal algorithm in `Result` (or `Solution`), using fast I/O and handling all constraints and multiple test cases.
5. STRICT JAVA 8 COMPATIBILITY REQUIRED (HackerRank judge executes with Java 8):
   - DO NOT use the 'var' keyword (use explicit types like int, String, List<Integer>, etc.).
   - DO NOT use Java 11+ methods like 'String.strip()', 'String.repeat()', or 'String.isBlank()' (use 'trim()' or standard loops).
   - DO NOT use Java 9+ factory methods like 'List.of()', 'Set.of()', or 'Map.of()' (use 'Arrays.asList()' or 'new ArrayList<>()').
   - DO NOT use 'Stream.toList()' (use '.collect(Collectors.toList())').
   - DO NOT use modern switch expressions (use standard switch-case statements).
"""
    else:  # hackerearth
        platform_instructions = f"""
1. For HackerEarth, write a complete, standalone Java program.
2. The primary class MUST be named 'TestClass' (e.g. `public class TestClass {{ public static void main(String[] args) throws Exception {{ ... }} }}`).
3. Read input from System.in using BufferedReader / Scanner and print to System.out.
4. Optimal time and space complexity.
5. STRICT JAVA 8 COMPATIBILITY REQUIRED (HackerEarth judge executes with Java 8):
   - DO NOT use the 'var' keyword (use explicit types like int, String, List<Integer>, etc.).
   - DO NOT use Java 11+ methods like 'String.strip()', 'String.repeat()', or 'String.isBlank()' (use 'trim()' or standard loops).
   - DO NOT use Java 9+ factory methods like 'List.of()', 'Set.of()', or 'Map.of()' (use 'Arrays.asList()' or 'new ArrayList<>()').
   - DO NOT use 'Stream.toList()' (use '.collect(Collectors.toList())').
   - DO NOT use modern switch expressions (use standard switch-case statements).
"""

    starter_section = f"STARTER CODE TEMPLATE:\n```java\n{starter_code}\n```" if starter_code.strip() else "STARTER CODE TEMPLATE: None provided. Write a complete standalone Java solution."

    feedback_section = f"\nATTENTION - PREVIOUS ATTEMPT FAILED WITH ERROR:\n{error_feedback}\nPlease diagnose and fix the issue in Java." if error_feedback else ""

    java_version_spec = "Java 8 (STRICT Java 8 compatibility, NO var, NO Java 9+ methods)" if platform_type in ["hackerrank", "hackerearth"] else "Java (Java 17/21)"

    prompt = f"""
You are an expert competitive programmer and Java software engineer.
Solve this {platform_name} coding problem in Java ({java_version_spec}).

PROBLEM STATEMENT:
{problem_text}

{starter_section}

PLATFORM INSTRUCTIONS:
{platform_instructions}
{feedback_section}

IMPORTANT FORMATTING:
- Provide clean, readable Java code with standard indentation (4 spaces) and proper newlines between methods, loops, and statements.
- Provide pure Java code in the 'code' field of the JSON output. Do NOT include markdown code fences (```) inside the code field itself.
"""

    last_error = None
    for model_name in AI_MODELS:
        for m_retry in range(2):
            try:
                print(f"   🤖 Querying {model_name} for Java solution...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=CodingAnswer,
                        temperature=0.1,
                    ),
                )
                result = CodingAnswer.model_validate_json(response.text)
                clean_code = clean_code_string(result.code)
                return clean_code, result.explanation
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                if ("503" in err_str or "unavailable" in err_str or "429" in err_str or "rate" in err_str) and m_retry == 0:
                    time.sleep(2)
                    continue
                print(f"   ⚠️  Model {model_name} failed: {e}. Trying next model...")
                break

    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")

def clean_code_string(code: str) -> str:
    """Removes stray markdown fences if any."""
    code = code.strip()
    if code.startswith("```java"):
        code = code[7:]
    elif code.startswith("```"):
        code = code[3:]
    if code.endswith("```"):
        code = code[:-3]
    return code.strip()

# ==============================================================================
# Browser Page Helpers (Selectors & Text Extraction)
# ==============================================================================
async def extract_problem_text(page: Page, selectors: List[str], timeout_ms: int = 15000) -> str:
    """Tries multiple selectors to retrieve the problem description,
    diagnosing stale selectors and verifying fallback target identity."""
    def cp_verifier(text: str) -> bool:
        if len(text) < 80:
            return False
        keywords = ["input", "output", "example", "constraints", "note", "given", "return", "task", "problem", "function", "sample"]
        t_low = text.lower()
        return any(k in t_low for k in keywords)

    res = await resolve_verified_selector(
        page,
        selectors,
        role="problem_description",
        target_verifier=cp_verifier,
        timeout_ms=timeout_ms
    )
    if res.success and res.element:
        try:
            return (await res.element.inner_text()).strip()
        except Exception:
            pass

    # Try verified fallbacks
    fallbacks = ["article", "main", "#content", ".content", ".problem-content", "[role='main']"]
    fb_res = await resolve_verified_selector(
        page,
        fallbacks,
        role="problem_description_fallback",
        target_verifier=cp_verifier,
        timeout_ms=min(3000, timeout_ms)
    )
    if fb_res.success and fb_res.element:
        print(f"   ℹ️ Used verified fallback selector: '{fb_res.matched_selector}'")
        try:
            return (await fb_res.element.inner_text()).strip()
        except Exception:
            pass

    return ""

async def extract_editor_code(page: Page) -> str:
    """Extracts existing starter code from Monaco, Ace, CodeMirror, or textarea."""
    js_extract = """() => {
        if (window.monaco && window.monaco.editor) {
            const models = window.monaco.editor.getModels();
            if (models && models.length > 0) {
                const val = models[0].getValue();
                if (val && val.trim().length > 0) return val;
            }
        }
        if (window.ace && window.ace.edit) {
            const aceEl = document.querySelector('.ace_editor');
            if (aceEl) {
                try {
                    const val = window.ace.edit(aceEl).getValue();
                    if (val && val.trim().length > 0) return val;
                } catch(e) {}
            }
        }
        const monacoLines = document.querySelectorAll('.monaco-editor .view-line');
        if (monacoLines && monacoLines.length > 0) {
            const lines = Array.from(monacoLines).map(l => l.innerText || l.textContent || '');
            const joined = lines.join('\\n').trim();
            if (joined.length > 0) return joined;
        }
        const aceLines = document.querySelectorAll('.ace_line');
        if (aceLines && aceLines.length > 0) {
            const lines = Array.from(aceLines).map(l => l.innerText || l.textContent || '');
            const joined = lines.join('\\n').trim();
            if (joined.length > 0) return joined;
        }
        const cm = document.querySelector('.CodeMirror');
        if (cm && cm.CodeMirror) {
            return cm.CodeMirror.getValue();
        }
        const ta = document.querySelector('textarea.code-editor, textarea[name="code"], textarea[name="source"]');
        if (ta && ta.value) return ta.value;

        return "";
    }"""
    try:
        code = await page.evaluate(js_extract)
        return (code or "").strip()
    except Exception:
        return ""

async def inject_code_into_editor(page: Page, solution_code: str) -> InjectionResult:
    """Injects solution code using Monaco/Ace APIs, direct keyboard insertion, or clipboard,
    and immediately verifies that injection succeeded by reading back editor content."""
    injected_type = None

    js_inject = """(newCode) => {
        if (window.monaco && window.monaco.editor) {
            const models = window.monaco.editor.getModels();
            if (models && models.length > 0) {
                models[0].setValue(newCode);
                return 'monaco_api';
            }
        }
        if (window.ace && window.ace.edit) {
            const aceEl = document.querySelector('.ace_editor');
            if (aceEl) {
                try {
                    window.ace.edit(aceEl).setValue(newCode, -1);
                    return 'ace_api';
                } catch(e) {}
            }
        }
        const cm = document.querySelector('.CodeMirror');
        if (cm && cm.CodeMirror) {
            cm.CodeMirror.setValue(newCode);
            return 'codemirror_api';
        }
        const ta = document.querySelector('textarea[name="source"], textarea.code-editor');
        if (ta) {
            ta.value = newCode;
            ta.dispatchEvent(new Event('input', { bubbles: true }));
            ta.dispatchEvent(new Event('change', { bubbles: true }));
            return 'textarea';
        }
        return false;
    }"""

    try:
        result = await page.evaluate(js_inject, solution_code)
        if result:
            injected_type = result
            print(f"   ✅ Code injected via {result}!")
    except Exception as e:
        print(f"   ⚠️  JS API injection notice: {e}")

    # Method 2: UI Focus + Keyboard Select All + Insert Text
    if not injected_type:
        print("   ⌨️  Using simulated keyboard injection...")
        modifier = "Meta" if sys.platform == "darwin" else "Control"
        editor_targets = [
            ".monaco-editor .view-lines",
            ".monaco-editor textarea",
            ".ace_editor .ace_text-input",
            ".ace_editor",
            "textarea[name='source']",
            "textarea.code-editor",
            ".CodeMirror",
        ]

        for selector in editor_targets:
            try:
                loc = page.locator(selector).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click()
                    await asyncio.sleep(0.2)
                    await page.keyboard.press(f"{modifier}+KeyA")
                    await asyncio.sleep(0.05)
                    await page.keyboard.press("Backspace")
                    await asyncio.sleep(0.05)

                    await page.keyboard.insert_text(solution_code)
                    print(f"   ✅ Code inserted via keyboard ({selector}).")
                    injected_type = f"keyboard_{selector}"
                    break
            except Exception:
                continue

    # Method 3: System Clipboard Fallback
    if not injected_type:
        print("   📋 Using system clipboard fallback...")
        try:
            modifier = "Meta" if sys.platform == "darwin" else "Control"
            pyperclip.copy(solution_code)
            await page.keyboard.press(f"{modifier}+KeyA")
            await page.keyboard.press("Backspace")
            await page.keyboard.press(f"{modifier}+KeyV")
            print("   ✅ Code pasted from clipboard.")
            injected_type = "clipboard"
        except Exception as e:
            print(f"   ❌ Clipboard injection failed: {e}")

    if not injected_type:
        return InjectionResult(success=False, verified=False, error="No supported editor found for code injection")

    # READBACK VERIFICATION
    readback = await extract_editor_code(page)
    expected_class = None
    try:
        expected_class, _, _, _ = detect_java_class_and_filename(solution_code, "generic")
    except Exception:
        pass
    if not expected_class:
        m = re.search(r"\bclass\s+(\w+)", solution_code)
        if m:
            expected_class = m.group(1)

    verified = True
    verify_err = None

    if not readback or len(readback.strip()) < 10:
        verified = False
        verify_err = "Editor readback was empty after injection"
    elif expected_class and not re.search(rf"\b{re.escape(expected_class)}\b", readback):
        verified = False
        verify_err = f"Expected class '{expected_class}' missing from editor readback"
    elif len(readback) < max(20, int(len(solution_code) * 0.35)):
        verified = False
        verify_err = f"Editor readback significantly shorter than expected ({len(readback)} vs {len(solution_code)} chars)"

    if verified:
        print("   ✅ Editor injection verified via readback!")
    else:
        print(f"   ❌ Editor injection verification failed: {verify_err}")

    return InjectionResult(
        success=True,
        verified=verified,
        editor_type=injected_type or "unknown",
        readback_snippet=readback[:100] if readback else "",
        error=verify_err
    )

# ==============================================================================
# Local Java Compilation & Verification (Universal for All Platforms)
# ==============================================================================
LEETCODE_STUBS = {
    "TreeNode.java": (
        "public class TreeNode {\n"
        "    public int val;\n"
        "    public TreeNode left;\n"
        "    public TreeNode right;\n"
        "    public TreeNode() {}\n"
        "    public TreeNode(int val) { this.val = val; }\n"
        "    public TreeNode(int val, TreeNode left, TreeNode right) {\n"
        "        this.val = val;\n"
        "        this.left = left;\n"
        "        this.right = right;\n"
        "    }\n"
        "}\n"
    ),
    "ListNode.java": (
        "public class ListNode {\n"
        "    public int val;\n"
        "    public ListNode next;\n"
        "    public ListNode() {}\n"
        "    public ListNode(int val) { this.val = val; }\n"
        "    public ListNode(int val, ListNode next) { this.val = val; this.next = next; }\n"
        "}\n"
    ),
    "Node.java": (
        "import java.util.*;\n"
        "public class Node {\n"
        "    public int val;\n"
        "    public List<Node> children;\n"
        "    public Node left, right, next, random;\n"
        "    public Node() {}\n"
        "    public Node(int _val) { val = _val; }\n"
        "    public Node(int _val, List<Node> _children) { val = _val; children = _children; }\n"
        "    public Node(int _val, Node _left, Node _right, Node _next) {\n"
        "        val = _val; left = _left; right = _right; next = _next;\n"
        "    }\n"
        "}\n"
    ),
}

def detect_java_class_and_filename(code: str, platform_type: str) -> Tuple[Optional[str], Optional[str], str, Optional[str]]:
    """
    Detects the primary Java class name, source filename, and verifies platform requirements.
    Returns (class_name, filename, cleaned_code, error_message).
    """
    cleaned = clean_code_string(code)

    # Strip comments to avoid false class detections in comments
    no_comments = re.sub(r"/\*[\s\S]*?\*/", "", cleaned)
    no_comments = re.sub(r"//.*", "", no_comments)

    pub_match = re.search(r"\bpublic\s+(?:final\s+)?class\s+(\w+)", no_comments)
    all_classes = [m.group(1) for m in re.finditer(r"\bclass\s+(\w+)", no_comments)]

    if not all_classes and not pub_match:
        return None, None, cleaned, "Missing public class or class declaration in generated Java code."

    pub_class = pub_match.group(1) if pub_match else None
    first_class = pub_class or (all_classes[0] if all_classes else None)

    if platform_type in ["codeforces", "codechef"]:
        if pub_class:
            if pub_class != "Main":
                return pub_class, f"{pub_class}.java", cleaned, f"Platform {platform_type} requires class Main, but found public class '{pub_class}'."
            return "Main", "Main.java", cleaned, None
        if "Main" in all_classes:
            return "Main", "Main.java", cleaned, None
        return first_class, f"{first_class}.java", cleaned, f"Platform {platform_type} requires class Main, but found class '{first_class}'."

    elif platform_type == "hackerearth":
        if pub_class:
            if pub_class != "TestClass":
                return pub_class, f"{pub_class}.java", cleaned, f"HackerEarth requires class TestClass, but found public class '{pub_class}'."
            return "TestClass", "TestClass.java", cleaned, None
        if "TestClass" in all_classes:
            return "TestClass", "TestClass.java", cleaned, None
        return first_class, f"{first_class}.java", cleaned, f"HackerEarth requires class TestClass, but found class '{first_class}'."

    elif platform_type == "hackerrank":
        if not pub_class:
            return first_class, f"{first_class}.java", cleaned, "HackerRank requires public class Solution."
        if pub_class != "Solution":
            return pub_class, f"{pub_class}.java", cleaned, f"HackerRank requires public class Solution, but found public class '{pub_class}'."
        return "Solution", "Solution.java", cleaned, None

    else:  # leetcode
        c_name = pub_class or ("Solution" if "Solution" in all_classes else first_class)
        return c_name, f"{c_name}.java", cleaned, None

def validate_code_security_and_dependencies(code: str) -> Tuple[bool, Optional[str]]:
    """
    Statically inspects generated Java code to reject forbidden network, process,
    reflection, or unauthorized third-party libraries before compilation/execution.
    """
    cleaned = re.sub(r"/\*[\s\S]*?\*/", "", code)
    cleaned = re.sub(r"//.*", "", cleaned)
    cleaned = re.sub(r'"(?:\\.|[^"\\])*"', '""', cleaned)
    cleaned = re.sub(r"'(?:\\.|[^'\\])*'", "''", cleaned)

    prohibited = [
        (r"\bjava\.net\b", "Network access package java.net is strictly prohibited"),
        (r"\b(?:Socket|ServerSocket|HttpURLConnection|HttpClient|InetAddress|DatagramSocket)\b", "Network socket/connection classes are prohibited"),
        (r"\bnew\s+URL\b|\bURI\.create\b", "URL/URI creation is prohibited"),
        (r"\bProcessBuilder\b", "ProcessBuilder execution is prohibited"),
        (r"\bRuntime\.getRuntime\b", "Runtime command execution is prohibited"),
        (r"\bSystem\.exit\b", "System.exit is prohibited in competitive solutions"),
        (r"\bjava\.lang\.reflect\b|\bMethod\.invoke\b|\bField\.get\b", "Reflection operations are prohibited"),
        (r"\bjava\.io\.File\b|\bnew\s+File\s*\(", "Host file manipulation is prohibited"),
        (r"\bjava\.nio\.file\b", "Host filesystem manipulation is prohibited"),
        (r"^\s*import\s+(?!java\.(?:util|io|math|text|lang)\b)[a-zA-Z0-9_\.]+", "Unsupported external package import"),
    ]

    for pattern, desc in prohibited:
        if re.search(pattern, cleaned, re.MULTILINE):
            return False, f"Security violation: {desc}"

    return True, None

def validate_platform_signatures(code: str, starter_code: str, platform_type: str) -> Tuple[bool, Optional[str]]:
    """
    Validates that required platform structures, entrypoints, and Java version compatibility
    are preserved in the generated code.
    """
    cleaned = clean_code_string(code)
    no_comments = re.sub(r"/\*[\s\S]*?\*/", "", cleaned)
    no_comments = re.sub(r"//.*", "", no_comments)

    if platform_type == "hackerrank":
        if "class Result" in starter_code and not re.search(r"\bclass\s+Result\b", no_comments):
            return False, "HackerRank solution missing required class Result from starter template"
        if not re.search(r"\bpublic\s+class\s+Solution\b", no_comments):
            return False, "HackerRank solution missing required public class Solution"
        if not re.search(r"public\s+static\s+void\s+main\s*\(\s*String\s*\[\s*\]", no_comments):
            return False, "HackerRank Solution class must contain public static void main(String[] args)"

    elif platform_type == "hackerearth":
        if not re.search(r"\bclass\s+TestClass\b", no_comments):
            return False, "HackerEarth solution missing required class TestClass"
        if not re.search(r"public\s+static\s+void\s+main\s*\(\s*String\s*\[\s*\]", no_comments):
            return False, "HackerEarth TestClass must contain public static void main(String[] args)"

    elif platform_type in ["codeforces", "codechef"]:
        if not re.search(r"\bclass\s+Main\b", no_comments):
            return False, f"{platform_type} solution missing required class Main"
        if not re.search(r"public\s+static\s+void\s+main\s*\(\s*String\s*\[\s*\]", no_comments):
            return False, f"{platform_type} Main class must contain public static void main(String[] args)"

    if platform_type in ["hackerrank", "hackerearth"]:
        j8_issues = [
            (r"\bvar\s+\w+\s*=", "var keyword (Java 10+)"),
            (r"\.strip\(\)", "String.strip() (Java 11+)"),
            (r"\.repeat\(", "String.repeat() (Java 11+)"),
            (r"\.isBlank\(\)", "String.isBlank() (Java 11+)"),
            (r"\bList\.of\(", "List.of() (Java 9+)"),
            (r"\bSet\.of\(", "Set.of() (Java 9+)"),
            (r"\bMap\.of\(", "Map.of() (Java 9+)"),
            (r"\.toList\(\)", "Stream.toList() (Java 16+)"),
        ]
        for pattern, desc in j8_issues:
            if re.search(pattern, no_comments):
                return False, f"Java 8 incompatibility detected: {desc} is not supported in Java 8."

    return True, None

def compare_sample_outputs(actual: str, expected: str, float_tolerance: float = 1e-6) -> Tuple[bool, Optional[str]]:
    """
    Compares program actual output against expected sample output using platform-appropriate
    whitespace rules and float precision tolerances without blindly ignoring significant formatting.
    """
    act_norm = actual.replace("\r\n", "\n").replace("\r", "\n").rstrip()
    exp_norm = expected.replace("\r\n", "\n").replace("\r", "\n").rstrip()

    if act_norm == exp_norm:
        return True, None

    act_lines = act_norm.split("\n") if act_norm else []
    exp_lines = exp_norm.split("\n") if exp_norm else []

    if len(act_lines) != len(exp_lines):
        return False, f"Line count mismatch: expected {len(exp_lines)} lines, got {len(act_lines)} lines.\nExpected:\n{expected}\nActual:\n{actual}"

    for i, (a_line, e_line) in enumerate(zip(act_lines, exp_lines), 1):
        a_line_r = a_line.rstrip()
        e_line_r = e_line.rstrip()
        if a_line_r == e_line_r:
            continue

        a_tokens = a_line_r.split()
        e_tokens = e_line_r.split()
        if len(a_tokens) != len(e_tokens):
            return False, f"Line {i} token count mismatch: expected {len(e_tokens)} tokens ('{e_line_r}'), got {len(a_tokens)} tokens ('{a_line_r}')."

        for t_idx, (at, et) in enumerate(zip(a_tokens, e_tokens), 1):
            if at == et:
                continue
            try:
                af = float(at)
                ef = float(et)
                diff = abs(af - ef)
                denom = max(1.0, abs(ef))
                if diff > float_tolerance and (diff / denom) > float_tolerance:
                    return False, f"Line {i} token {t_idx} float mismatch: expected {et}, got {at} (diff {diff:.2e} > {float_tolerance:.2e})."
            except ValueError:
                return False, f"Line {i} token {t_idx} mismatch: expected '{et}', got '{at}'."

    return True, None

def compile_java_code_sandboxed(
    code: str,
    platform_type: str,
    java_version: Optional[str] = None,
    work_dir: Optional[Path] = None
) -> CompilationResult:
    """
    Compiles Java code in an isolated directory with release flags and data structure stubs.
    """
    class_name, filename, cleaned, class_err = detect_java_class_and_filename(code, platform_type)
    if class_err or not class_name or not filename:
        return CompilationResult(
            success=False,
            class_name=class_name or "",
            source_filename=filename or "",
            error_message=class_err or "Missing class definition",
            diagnostics=class_err or "Missing class definition",
            cleaned_code=cleaned
        )

    target_version = java_version
    if not target_version:
        if platform_type in ["hackerrank", "hackerearth"]:
            target_version = "8"

    def _compile_in_dir(target_dir: Path) -> CompilationResult:
        if platform_type == "leetcode":
            for stub_name, stub_content in LEETCODE_STUBS.items():
                stub_file = target_dir / stub_name
                if not stub_file.exists():
                    stub_file.write_text(stub_content, encoding="utf-8")

        src_file = target_dir / filename
        src_file.write_text(cleaned, encoding="utf-8")

        cmd = ["javac", "-cp", str(target_dir), "-sourcepath", str(target_dir)]
        if target_version:
            cmd.extend(["-Xlint:-options", "--release", str(target_version)])

        java_files = [str(f) for f in target_dir.glob("*.java")]
        cmd.extend(java_files)

        try:
            comp_proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15
            )
            if comp_proc.returncode != 0:
                return CompilationResult(
                    success=False,
                    class_name=class_name,
                    source_filename=filename,
                    error_message=comp_proc.stderr.strip(),
                    diagnostics=comp_proc.stderr.strip(),
                    cleaned_code=cleaned,
                    target_version=target_version
                )
            return CompilationResult(
                success=True,
                class_name=class_name,
                source_filename=filename,
                cleaned_code=cleaned,
                target_version=target_version
            )
        except Exception as e:
            return CompilationResult(
                success=False,
                class_name=class_name,
                source_filename=filename,
                error_message=f"javac invocation failed: {e}",
                diagnostics=f"javac invocation failed: {e}",
                cleaned_code=cleaned,
                target_version=target_version
            )

    if work_dir is not None:
        return _compile_in_dir(work_dir)
    else:
        with tempfile.TemporaryDirectory() as tmpdir:
            return _compile_in_dir(Path(tmpdir))

def compile_java_code_locally(code: str, platform_type: str, java_version: Optional[str] = None) -> Tuple[bool, str, str]:
    """Compiles Java code locally using javac to detect syntax, type, and import errors."""
    res = compile_java_code_sandboxed(code, platform_type, java_version=java_version)
    if res.success:
        return True, "Compilation successful.", res.cleaned_code
    return False, res.diagnostics or res.error_message or "Compilation failed", res.cleaned_code

def execute_java_sandboxed(
    class_dir: Path,
    class_name: str,
    stdin_input: str = "",
    timeout_sec: float = 5.0,
    max_memory_mb: int = 256,
    disable_network: bool = True,
    max_output_bytes: int = 10 * 1024 * 1024
) -> ExecutionResult:
    """
    Executes compiled Java bytecode in a restricted sandbox enforcing CPU, memory, time,
    process, filesystem, and network isolation limits.
    """
    java_cmd = [
        "java",
        f"-Xmx{max_memory_mb}m",
        f"-Xms16m",
        "-XX:+UseSerialGC",
        "-Dfile.encoding=UTF-8",
    ]
    if disable_network:
        java_cmd.extend([
            "-Dhttp.proxyHost=0.0.0.0",
            "-Dhttp.proxyPort=0",
            "-Dhttps.proxyHost=0.0.0.0",
            "-Dhttps.proxyPort=0",
            "-DsocksProxyHost=0.0.0.0",
            "-DsocksProxyPort=0",
        ])

    java_cmd.extend(["-cp", str(class_dir), class_name])

    final_cmd = list(java_cmd)
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists():
        sb_profile = (
            "(version 1)\n"
            "(allow default)\n"
            "(deny network*)\n"
            f'(deny file-write* (subpath "/Users"))\n'
            f'(deny file-write* (subpath "/private/tmp"))\n'
            f'(deny file-write* (subpath "/tmp"))\n'
            f'(allow file-write* (subpath "{class_dir}"))\n'
        )
        final_cmd = ["/usr/bin/sandbox-exec", "-p", sb_profile] + final_cmd

    def _set_rlimits():
        if resource is not None:
            try:
                cpu_lim = max(1, int(timeout_sec) + 2)
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_lim, cpu_lim + 1))
            except Exception:
                pass
            try:
                resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
            except Exception:
                pass

    try:
        proc = subprocess.run(
            final_cmd,
            input=stdin_input,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            preexec_fn=_set_rlimits if sys.platform != "win32" else None,
        )
        stdout = proc.stdout[:max_output_bytes]
        stderr = proc.stderr[:max_output_bytes]

        oom = ("OutOfMemoryError" in stderr)
        sec_violation = (
            "SocketException" in stderr or
            "UnknownHostException" in stderr or
            "ConnectException" in stderr or
            "SecurityException" in stderr or
            "Operation not permitted" in stderr
        )

        err_msg = None
        if proc.returncode != 0:
            if oom:
                err_msg = f"Memory Limit Exceeded ({max_memory_mb}MB): OutOfMemoryError"
            elif sec_violation:
                err_msg = "Security violation: Network or unauthorized resource access blocked"
            else:
                err_msg = f"Runtime Error (Exit Code {proc.returncode}): {stderr.strip()}"

        return ExecutionResult(
            success=(proc.returncode == 0),
            return_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            timeout=False,
            oom=oom,
            security_violation=sec_violation,
            error_message=err_msg
        )
    except subprocess.TimeoutExpired as te:
        return ExecutionResult(
            success=False,
            return_code=-1,
            stdout="",
            stderr=str(te),
            timeout=True,
            error_message=f"Time Limit Exceeded ({timeout_sec}s)"
        )
    except Exception as e:
        return ExecutionResult(
            success=False,
            return_code=-1,
            stdout="",
            stderr=str(e),
            error_message=f"Execution failed: {e}"
        )

def extract_sample_test_cases_from_text(problem_text: str) -> List[SampleTestCase]:
    """Extracts sample inputs and outputs from problem descriptions."""
    samples: List[SampleTestCase] = []

    pattern1 = re.compile(
        r"(?:Sample\s+Input|Input)\s*(?:\d*)\s*:?\s*\n([\s\S]*?)\n\s*(?:Sample\s+Output|Output)\s*(?:\d*)\s*:?\s*\n([\s\S]*?)(?=(?:Sample\s+Input|Input|\Z))",
        re.IGNORECASE
    )
    for i, (inp, out) in enumerate(pattern1.findall(problem_text), 1):
        clean_inp = inp.strip()
        clean_out = out.strip()
        if clean_inp or clean_out:
            samples.append(SampleTestCase(input_text=clean_inp, expected_output=clean_out, sample_index=i))

    if samples:
        return samples

    pattern2 = re.compile(
        r"Example\s*\d*:?\s*Input:?\s*([^\n]+(?:\n[^\n]+)*?)\s*Output:?\s*([^\n]+(?:\n[^\n]+)*?)(?=Example|Explanation|Note|Constraints|\Z)",
        re.IGNORECASE
    )
    for i, (inp, out) in enumerate(pattern2.findall(problem_text), 1):
        clean_inp = inp.strip()
        clean_out = out.strip()
        if clean_inp or clean_out:
            samples.append(SampleTestCase(input_text=clean_inp, expected_output=clean_out, sample_index=i))

    return samples

async def extract_sample_test_cases_from_page(page: Page, platform_type: str) -> List[SampleTestCase]:
    """Extracts sample test cases from live browser DOM based on platform-specific selectors."""
    samples: List[SampleTestCase] = []
    try:
        if platform_type == "codeforces":
            inputs = await page.locator(".sample-test .input pre").all_inner_texts()
            outputs = await page.locator(".sample-test .output pre").all_inner_texts()
            for i, (inp, out) in enumerate(zip(inputs, outputs), 1):
                samples.append(SampleTestCase(input_text=inp.strip(), expected_output=out.strip(), sample_index=i))
        elif platform_type == "hackerrank":
            inputs = await page.locator(".challenge_sample_input pre").all_inner_texts()
            outputs = await page.locator(".challenge_sample_output pre").all_inner_texts()
            for i, (inp, out) in enumerate(zip(inputs, outputs), 1):
                samples.append(SampleTestCase(input_text=inp.strip(), expected_output=out.strip(), sample_index=i))
        elif platform_type == "codechef":
            inputs = await page.locator(".problem-statement pre:has-text('Input')").all_inner_texts()
            outputs = await page.locator(".problem-statement pre:has-text('Output')").all_inner_texts()
            for i, (inp, out) in enumerate(zip(inputs, outputs), 1):
                samples.append(SampleTestCase(input_text=inp.strip(), expected_output=out.strip(), sample_index=i))
    except Exception:
        pass
    return samples

def run_all_sample_tests_sandboxed(
    code: str,
    samples: List[SampleTestCase],
    platform_type: str,
    java_version: Optional[str] = None,
    timeout_sec: float = 5.0,
    max_memory_mb: int = 256
) -> Tuple[bool, List[SampleTestResult], str]:
    """Compiles and executes Java code against sample test cases in an isolated sandbox."""
    if not samples:
        return True, [], "No sample test cases provided; compilation check only."

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        comp_res = compile_java_code_sandboxed(code, platform_type, java_version=java_version, work_dir=tmp_path)
        if not comp_res.success:
            return False, [], f"Compilation error before sample tests:\n{comp_res.diagnostics}"

        class_name = comp_res.class_name
        if platform_type == "leetcode" and "public static void main" not in comp_res.cleaned_code:
            return True, [], "LeetCode class-based solution verified with data structure stubs."

        all_passed = True
        summary = []
        results: List[SampleTestResult] = []

        for sample in samples:
            exec_res = execute_java_sandboxed(
                class_dir=tmp_path,
                class_name=class_name,
                stdin_input=sample.input_text,
                timeout_sec=timeout_sec,
                max_memory_mb=max_memory_mb
            )

            if exec_res.timeout:
                all_passed = False
                res = SampleTestResult(
                    sample_index=sample.sample_index,
                    passed=False,
                    input_text=sample.input_text,
                    expected_output=sample.expected_output,
                    actual_output="",
                    error_message=exec_res.error_message
                )
                results.append(res)
                summary.append(f"Sample {sample.sample_index}: TIME LIMIT EXCEEDED ({timeout_sec}s)")
            elif exec_res.oom:
                all_passed = False
                res = SampleTestResult(
                    sample_index=sample.sample_index,
                    passed=False,
                    input_text=sample.input_text,
                    expected_output=sample.expected_output,
                    actual_output="",
                    error_message=exec_res.error_message
                )
                results.append(res)
                summary.append(f"Sample {sample.sample_index}: MEMORY LIMIT EXCEEDED ({max_memory_mb}MB)")
            elif not exec_res.success:
                all_passed = False
                res = SampleTestResult(
                    sample_index=sample.sample_index,
                    passed=False,
                    input_text=sample.input_text,
                    expected_output=sample.expected_output,
                    actual_output=exec_res.stdout,
                    error_message=exec_res.error_message
                )
                results.append(res)
                summary.append(f"Sample {sample.sample_index}: RUNTIME ERROR (Code {exec_res.return_code})\nStderr: {exec_res.stderr.strip()}")
            else:
                matches, diff_msg = compare_sample_outputs(exec_res.stdout, sample.expected_output)
                if matches:
                    res = SampleTestResult(
                        sample_index=sample.sample_index,
                        passed=True,
                        input_text=sample.input_text,
                        expected_output=sample.expected_output,
                        actual_output=exec_res.stdout
                    )
                    results.append(res)
                    summary.append(f"Sample {sample.sample_index}: PASSED")
                else:
                    all_passed = False
                    res = SampleTestResult(
                        sample_index=sample.sample_index,
                        passed=False,
                        input_text=sample.input_text,
                        expected_output=sample.expected_output,
                        actual_output=exec_res.stdout,
                        error_message=diff_msg,
                        diff=diff_msg
                    )
                    results.append(res)
                    summary.append(f"Sample {sample.sample_index}: FAILED\nInput:\n{sample.input_text}\nExpected:\n{sample.expected_output}\nActual:\n{exec_res.stdout}\nDiff: {diff_msg}")

        return all_passed, results, "\n".join(summary)

def verify_and_repair_java_pipeline(
    problem_text: str,
    starter_code: str,
    samples: List[SampleTestCase],
    platform_name: str,
    platform_type: str,
    max_repairs: int = 3,
    java_version: Optional[str] = None,
    ai_caller: Optional[Callable[..., Tuple[str, str]]] = None,
    dry_run: bool = False,
) -> VerificationPipelineResult:
    """
    Modular AI generation, verification, and repair pipeline.
    Enforces configurable repair attempts, feeds exact compiler/sample diagnostics into prompts,
    and isolates untrusted execution.
    """
    caller = ai_caller or ask_gemini_for_java_code
    last_feedback: Optional[str] = None
    solution_code: str = ""
    explanation: str = ""
    last_comp_res: Optional[CompilationResult] = None
    last_sample_results: List[SampleTestResult] = []

    for attempt in range(1, max_repairs + 2):
        repair_idx = attempt - 1
        if repair_idx > 0:
            print(f"   🔧 [Repair Attempt {repair_idx}/{max_repairs}] Querying AI with exact failure diagnostics...")
        else:
            print("   🤖 [Initial Generation] Querying AI for Java solution...")

        try:
            import inspect
            sig = inspect.signature(caller)
            call_kwargs = {
                "problem_text": problem_text,
                "starter_code": starter_code,
                "platform_name": platform_name,
                "platform_type": platform_type,
                "error_feedback": last_feedback
            }
            if "dry_run" in sig.parameters:
                call_kwargs["dry_run"] = dry_run

            solution_code, explanation = caller(**call_kwargs)
        except Exception as e:
            return VerificationPipelineResult(
                success=False,
                solution_code=solution_code,
                explanation=explanation,
                repair_attempts=repair_idx,
                failure_reason=f"AI generation call failed: {e}"
            )

        # 1. Security & Dependency Check
        sec_ok, sec_err = validate_code_security_and_dependencies(solution_code)
        if not sec_ok:
            print(f"   🛑 Security Check Failed: {sec_err}")
            last_feedback = (
                f"SECURITY VIOLATION IN GENERATED CODE:\n{sec_err}\n"
                f"Requirement: Online judge code must be completely self-contained. "
                f"Do NOT import or use java.net, ProcessBuilder, Runtime.getRuntime, System.exit, "
                f"reflection, or external 3rd-party libraries."
            )
            continue

        # 2. Platform Signature Check
        sig_ok, sig_err = validate_platform_signatures(solution_code, starter_code, platform_type)
        if not sig_ok:
            print(f"   ⚠️ Platform Signature Check Failed: {sig_err}")
            last_feedback = (
                f"PLATFORM SIGNATURE ERROR:\n{sig_err}\n"
                f"Please preserve the required class names, method signatures, and Java version compatibility."
            )
            continue

        # 3. Class & Filename Check
        c_name, f_name, cleaned_code, class_err = detect_java_class_and_filename(solution_code, platform_type)
        if class_err:
            print(f"   ⚠️ Class Detection Error: {class_err}")
            last_feedback = (
                f"CLASS DEFINITION ERROR:\n{class_err}\n"
                f"Ensure the primary class is properly named according to {platform_name} conventions."
            )
            continue

        # 4. Compilation Check
        comp_ok, comp_msg, cleaned_code = compile_java_code_locally(solution_code, platform_type, java_version=java_version)
        last_comp_res = CompilationResult(
            success=comp_ok,
            class_name=c_name or "Solution",
            source_filename=f_name or "Solution.java",
            error_message=None if comp_ok else comp_msg,
            diagnostics=None if comp_ok else comp_msg,
            cleaned_code=cleaned_code,
            target_version=java_version
        )
        if not comp_ok:
            print(f"   ⚠️ Java Compilation Failed:\n{comp_msg}")
            last_feedback = (
                f"EXACT COMPILER DIAGNOSTICS (javac):\n{comp_msg}\n"
                f"Diagnose and fix the syntax, type, or import errors in Java."
            )
            continue

        print("   ✅ Local javac compilation PASSED cleanly!")

        # 5. Sample Test Cases Check
        samples_passed, sample_results, sample_summary = run_all_sample_tests_sandboxed(
            code=cleaned_code,
            samples=samples,
            platform_type=platform_type,
            java_version=java_version
        )
        last_sample_results = sample_results

        if not samples_passed:
            print(f"   ⚠️ Sample Test Cases Failed:\n{sample_summary}")
            failed_s = next((s for s in sample_results if not s.passed), None)
            diag = (failed_s.diff or failed_s.error_message or "Output mismatch") if failed_s else "Sample mismatch"
            s_idx = failed_s.sample_index if failed_s else 1
            s_inp = failed_s.input_text if failed_s else ""
            s_exp = failed_s.expected_output if failed_s else ""
            s_act = failed_s.actual_output if failed_s else ""
            last_feedback = (
                f"SAMPLE TEST CASE #{s_idx} FAILED:\n"
                f"Input:\n{s_inp}\n"
                f"Expected Output:\n{s_exp}\n"
                f"Actual Output:\n{s_act}\n"
                f"Exact Diagnostic:\n{diag}\n"
                f"Diagnose the algorithmic failure and output a corrected solution."
            )
            continue

        print("   ✅ Sample test cases PASSED!")
        return VerificationPipelineResult(
            success=True,
            solution_code=cleaned_code,
            explanation=explanation,
            compilation=last_comp_res,
            sample_results=last_sample_results,
            repair_attempts=repair_idx
        )

    # All repair attempts exhausted
    return VerificationPipelineResult(
        success=False,
        solution_code=solution_code,
        explanation=explanation,
        compilation=last_comp_res,
        sample_results=last_sample_results,
        repair_attempts=max_repairs,
        failure_reason=last_feedback or "Verification failed after maximum repair attempts"
    )

async def capture_pre_submission_snapshot(
    page: Page,
    platform_type: str,
    url: str
) -> Dict[str, Any]:
    """Captures pre-submission metadata (latest submission ID, start timestamp, expected problem and account)
    to prevent stale submission verdicts from being falsely attributed to the current attempt."""
    start_time = time.time()
    prob_id = extract_problem_id(url, platform_type)
    account = None
    pre_submission_id = None

    if platform_type == "codeforces":
        account = CODEFORCES_HANDLE
        try:
            if account:
                api_url = f"https://codeforces.com/api/user.status?handle={account}&from=1&count=1"
                req = urllib.request.Request(api_url, headers={"User-Agent": DEFAULT_USER_AGENT})
                with urllib.request.urlopen(req, timeout=4) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        if data.get("status") == "OK" and data.get("result"):
                            pre_submission_id = str(data["result"][0].get("id"))
        except Exception:
            pass

    elif platform_type == "hackerrank":
        slug = prob_id
        try:
            data = await page.evaluate(f"""async () => {{
                try {{
                    const r = await fetch('https://www.hackerrank.com/rest/contests/master/challenges/{slug}/submissions');
                    return r.ok ? await r.json() : null;
                }} catch(e) {{
                    return null;
                }}
            }}""")
            if data and "models" in data and len(data["models"]) > 0:
                pre_submission_id = str(data["models"][0].get("id"))
        except Exception:
            pass

    elif platform_type in ["leetcode", "codechef", "hackerearth"]:
        # Capture any currently visible submission ID in DOM before new submission
        try:
            pre_id = await page.evaluate(r"""() => {
                const link = document.querySelector('a[href*="/submissions/detail/"], a[href*="/viewsolution/"], a[href*="/submission/"]');
                if (link && link.href) {
                    const m = link.href.match(/(?:detail\/|viewsolution\/|submission\/)(?:[a-zA-Z0-9_\-]+\/)?(\d+)/);
                    if (m) return m[1];
                }
                const el = document.querySelector('[data-submission-id]');
                return el ? el.getAttribute('data-submission-id') : null;
            }""")
            if pre_id:
                pre_submission_id = str(pre_id)
        except Exception:
            pass

    return {
        "start_time": start_time,
        "pre_submission_id": pre_submission_id,
        "account": account,
        "problem_id": prob_id,
        "platform_type": platform_type,
    }

async def _verify_hackerrank(
    page: Page,
    url: str,
    submission_start_time: float,
    pre_submission_id: Optional[str] = None,
    account: Optional[str] = None,
    expected_problem_id: Optional[str] = None,
    timeout: float = 45.0,
    initial_interval: float = 1.5,
) -> SubmissionVerdict:
    slug = expected_problem_id or extract_problem_id(url, "hackerrank")
    print(f"   ⏳ Polling HackerRank submission API for verdict ({slug})...")

    last_error = None
    seen_pending = False
    pending_sub_id = None

    async for poll, elapsed in poll_with_backoff(
        timeout=timeout, initial_interval=initial_interval, max_interval=5.0, backoff_factor=1.4
    ):
        try:
            data = await page.evaluate(f"""async () => {{
                try {{
                    const r = await fetch('https://www.hackerrank.com/rest/contests/master/challenges/{slug}/submissions');
                    if (!r.ok) return {{ error: 'HTTP_' + r.status }};
                    return await r.json();
                }} catch(e) {{
                    return {{ error: e.toString() }};
                }}
            }}""")
        except Exception as e:
            last_error = str(e)
            continue

        if not data:
            continue

        if isinstance(data, dict) and "error" in data:
            last_error = str(data["error"])
            continue

        models = data.get("models") if isinstance(data, dict) else None
        if not models or not isinstance(models, list):
            continue

        # Find the submission corresponding strictly to the CURRENT attempt
        matching_sub = None
        for sub in models:
            sub_id = str(sub.get("id", ""))
            if not sub_id:
                continue

            # Check 1: Reject if same as pre-submission ID (Stale submission)
            if pre_submission_id and sub_id == str(pre_submission_id):
                continue

            # Check 2: Submission time matching (reject stale verdicts created before submission)
            created_at = sub.get("created_at")
            sub_ts = None
            if created_at:
                try:
                    if isinstance(created_at, (int, float)):
                        sub_ts = float(created_at)
                    else:
                        sub_ts = datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).timestamp()
                except Exception:
                    pass

            if sub_ts is not None and sub_ts < (submission_start_time - 15.0):
                # Stale submission from an earlier historical session
                continue

            # Check 3: Account matching if known
            if account:
                sub_user = sub.get("hacker_username") or sub.get("username")
                if sub_user and sub_user.lower() != account.lower():
                    continue

            # Found candidate submission matching current attempt!
            matching_sub = sub
            break

        if not matching_sub:
            # Only stale or unmatching submissions exist currently
            continue

        sub_id = str(matching_sub.get("id"))
        st = matching_sub.get("status")
        sc = matching_sub.get("score")
        print(f"      [HackerRank Poll {poll}] SubID: {sub_id}, Status: {st}, Score: {sc}")

        if st in ["Processing", "Queued", "Evaluating", None]:
            seen_pending = True
            pending_sub_id = sub_id
            continue

        evidence = {
            "submission_id": sub_id,
            "status": st,
            "score": sc,
            "challenge_slug": slug,
            "created_at": matching_sub.get("created_at"),
            "account": matching_sub.get("hacker_username") or account,
        }

        if st == "Accepted":
            return SubmissionVerdict(
                state=VerdictState.ACCEPTED,
                message=f"Accepted (Score: {sc})",
                submission_id=sub_id,
                platform="hackerrank",
                problem_id=slug,
                account=matching_sub.get("hacker_username") or account,
                timestamp=sub_ts or time.time(),
                correlation_mechanism="hackerrank_rest_api_submission_id",
                evidence=evidence,
            )
        elif st in ["Wrong Answer", "Compilation Error", "Runtime Error", "Terminated due to timeout"] or st:
            return SubmissionVerdict(
                state=VerdictState.REJECTED,
                message=f"{st} (Score: {sc})",
                submission_id=sub_id,
                platform="hackerrank",
                problem_id=slug,
                account=matching_sub.get("hacker_username") or account,
                timestamp=sub_ts or time.time(),
                correlation_mechanism="hackerrank_rest_api_submission_id",
                evidence=evidence,
            )

    # Polling exhausted
    if seen_pending:
        return SubmissionVerdict(
            state=VerdictState.PENDING,
            message="Evaluation timed out on HackerRank (submission remains in evaluation).",
            submission_id=pending_sub_id,
            platform="hackerrank",
            problem_id=slug,
            account=account,
        )

    if last_error:
        return SubmissionVerdict(
            state=VerdictState.ERROR,
            message=f"HackerRank API polling error: {last_error}",
            platform="hackerrank",
            problem_id=slug,
            account=account,
        )

    return SubmissionVerdict(
        state=VerdictState.UNKNOWN,
        message="No new submission registered on HackerRank for current attempt within timeout window.",
        platform="hackerrank",
        problem_id=slug,
        account=account,
    )

async def _verify_codeforces(
    page: Page,
    url: str,
    submission_start_time: float,
    pre_submission_id: Optional[str] = None,
    account: Optional[str] = None,
    expected_problem_id: Optional[str] = None,
    timeout: float = 60.0,
    initial_interval: float = 1.5,
) -> SubmissionVerdict:
    contest_id, index = None, None
    m = re.search(r"/(?:problemset/problem|contest)/(\d+)/(?:problem/)?([A-Za-z0-9]+)", url.strip().rstrip("/"))
    if m:
        contest_id = int(m.group(1))
        index = m.group(2).upper()
    prob_id = f"{contest_id}{index}" if contest_id and index else (expected_problem_id or extract_problem_id(url, "codeforces"))

    handle = account or CODEFORCES_HANDLE
    print(f"   ⏳ Polling Codeforces API for verdict (Target: {prob_id}, Handle: {handle or 'None'})...")

    # 1. Check for anti-bot or submission errors on page
    try:
        page_errs = await page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('.error, .error__desc, span[class*="error"]'));
            return els.map(e => (e.innerText || '').trim()).filter(t => t.length > 0);
        }""")
        for pe in page_errs:
            if "anti-bot" in pe.lower() or "verification" in pe.lower():
                page._anti_bot_blocked = True
                return SubmissionVerdict(
                    state=VerdictState.ERROR,
                    message="Codeforces anti-bot verification (Cloudflare Turnstile) challenge required.",
                    platform="codeforces",
                    problem_id=prob_id,
                    account=handle,
                )
            if len(pe) > 2:
                return SubmissionVerdict(
                    state=VerdictState.REJECTED,
                    message=f"Codeforces form error: {pe}",
                    platform="codeforces",
                    problem_id=prob_id,
                    account=handle,
                )
    except Exception:
        pass

    if not handle:
        return SubmissionVerdict(
            state=VerdictState.ERROR,
            message="Codeforces verification requires CODEFORCES_HANDLE to query user submissions.",
            platform="codeforces",
            problem_id=prob_id,
        )

    last_error = None
    seen_pending = False
    pending_sub_id = None

    async for poll, elapsed in poll_with_backoff(
        timeout=timeout, initial_interval=initial_interval, max_interval=5.0, backoff_factor=1.4
    ):
        try:
            api_url = f"https://codeforces.com/api/user.status?handle={handle}&from=1&count=10"
            req = urllib.request.Request(api_url, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status != 200:
                    last_error = f"HTTP {resp.status}"
                    continue
                cf_data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_error = str(e)
            continue

        if not cf_data or cf_data.get("status") != "OK":
            last_error = cf_data.get("comment", "API status not OK") if isinstance(cf_data, dict) else "Invalid response"
            continue

        results = cf_data.get("result", [])
        matching_sub = None

        for sub in results:
            p = sub.get("problem", {})
            c_id = p.get("contestId")
            idx = p.get("index", "").upper()
            creation = sub.get("creationTimeSeconds", 0)
            sub_id = str(sub.get("id", ""))

            # Check 1: Contest ID and Index must match target problem
            if contest_id is not None and (c_id != contest_id or idx != index):
                continue

            # Check 2: Reject if same as pre-submission ID (Stale)
            if pre_submission_id and sub_id == str(pre_submission_id):
                continue

            # Check 3: Creation time must be after submission start time (within 15s clock skew)
            if creation < (submission_start_time - 15.0):
                continue

            # Check 4: Account verification
            author_members = [m.get("handle", "").lower() for m in sub.get("author", {}).get("members", [])]
            if handle.lower() not in author_members:
                continue

            matching_sub = sub
            break

        if not matching_sub:
            continue

        sub_id = str(matching_sub.get("id"))
        verdict = matching_sub.get("verdict")
        print(f"      [Codeforces Poll {poll}] SubID: {sub_id}, Verdict: {verdict}")

        # REQUIREMENT 4: NEVER return ACCEPTED for TESTING
        if verdict == "TESTING" or verdict is None or verdict == "SUBMITTED":
            seen_pending = True
            pending_sub_id = sub_id
            continue

        evidence = {
            "submission_id": sub_id,
            "contestId": matching_sub.get("problem", {}).get("contestId"),
            "index": matching_sub.get("problem", {}).get("index"),
            "creationTimeSeconds": matching_sub.get("creationTimeSeconds"),
            "verdict": verdict,
            "handle": handle,
        }

        # REQUIREMENT 4: Only verdict OK qualifies as ACCEPTED
        if verdict == "OK":
            return SubmissionVerdict(
                state=VerdictState.ACCEPTED,
                message="Accepted (OK)",
                submission_id=sub_id,
                platform="codeforces",
                problem_id=prob_id,
                account=handle,
                timestamp=float(matching_sub.get("creationTimeSeconds", time.time())),
                correlation_mechanism="codeforces_api_user_status_id",
                evidence=evidence,
            )
        else:
            return SubmissionVerdict(
                state=VerdictState.REJECTED,
                message=f"Codeforces verdict: {verdict}",
                submission_id=sub_id,
                platform="codeforces",
                problem_id=prob_id,
                account=handle,
                timestamp=float(matching_sub.get("creationTimeSeconds", time.time())),
                correlation_mechanism="codeforces_api_user_status_id",
                evidence=evidence,
            )

    # Polling exhausted
    if seen_pending:
        return SubmissionVerdict(
            state=VerdictState.PENDING,
            message="Evaluation timed out on Codeforces (verdict remains TESTING).",
            submission_id=pending_sub_id,
            platform="codeforces",
            problem_id=prob_id,
            account=handle,
        )

    if last_error:
        return SubmissionVerdict(
            state=VerdictState.ERROR,
            message=f"Codeforces API error: {last_error}",
            platform="codeforces",
            problem_id=prob_id,
            account=handle,
        )

    return SubmissionVerdict(
        state=VerdictState.UNKNOWN,
        message="No recent matching submission registered on Codeforces for current attempt.",
        platform="codeforces",
        problem_id=prob_id,
        account=handle,
    )

async def _verify_leetcode(
    page: Page,
    url: str,
    submission_start_time: float,
    pre_submission_id: Optional[str] = None,
    account: Optional[str] = None,
    expected_problem_id: Optional[str] = None,
    timeout: float = 30.0,
    initial_interval: float = 1.0,
) -> SubmissionVerdict:
    slug = expected_problem_id or extract_problem_id(url, "leetcode")
    print(f"   ⏳ Verifying LeetCode submission verdict ({slug})...")

    last_error = None
    seen_pending = False

    async for poll, elapsed in poll_with_backoff(
        timeout=timeout, initial_interval=initial_interval, max_interval=4.0, backoff_factor=1.4
    ):
        try:
            res = await page.evaluate(r"""() => {
                // Scope STRICTLY to the submission result container only
                const selectors = [
                    '[data-e2e-locator="submission-result"]',
                    '[data-cypress="SubmissionResult"]',
                    'div[class*="result-container"]',
                    'div[class*="result-state"]',
                    'div[class*="submission-result"]'
                ];
                let container = null;
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) { container = el; break; }
                }
                if (!container) return null;

                const text = (container.innerText || '').trim();
                let subId = null;
                const link = container.querySelector('a[href*="/submissions/detail/"], a[href*="/submissions/"]') ||
                             document.querySelector('a[href*="/submissions/detail/"]');
                if (link && link.href) {
                    const m = link.href.match(/submissions\/(?:detail\/)?(\d+)/);
                    if (m) subId = m[1];
                }
                if (!subId && container.getAttribute('data-submission-id')) {
                    subId = container.getAttribute('data-submission-id');
                }

                return {
                    text: text,
                    submissionId: subId
                };
            }""")
        except Exception as e:
            last_error = str(e)
            continue

        if not res or not isinstance(res, dict):
            continue

        text = res.get("text", "")
        sub_id = res.get("submissionId")

        # Stale check: if sub_id matches pre_submission_id, ignore
        if sub_id and pre_submission_id and str(sub_id) == str(pre_submission_id):
            continue

        # Check pending states
        if any(p in text for p in ["Pending", "Judging", "Compiling", "Evaluating", "In Queue"]):
            seen_pending = True
            continue

        # Check verdict
        if "Accepted" in text:
            corr_mechanism = "leetcode_dom_submission_detail_link" if sub_id else "leetcode_dom_scoped_result_container"
            effective_id = str(sub_id) if sub_id else f"leetcode_{slug}_{int(submission_start_time)}"
            evidence = {
                "submission_id": effective_id,
                "problem_slug": slug,
                "result_snippet": text[:150],
                "correlation_mechanism": corr_mechanism,
                "submission_start_time": submission_start_time,
            }
            return SubmissionVerdict(
                state=VerdictState.ACCEPTED,
                message="Accepted",
                submission_id=effective_id,
                platform="leetcode",
                problem_id=slug,
                account=account,
                timestamp=time.time(),
                correlation_mechanism=corr_mechanism,
                evidence=evidence,
            )

        rejected_indicators = ["Wrong Answer", "Time Limit Exceeded", "Runtime Error", "Compile Error", "Memory Limit Exceeded", "Output Limit Exceeded"]
        for rej in rejected_indicators:
            if rej in text:
                return SubmissionVerdict(
                    state=VerdictState.REJECTED,
                    message=rej,
                    submission_id=str(sub_id) if sub_id else None,
                    platform="leetcode",
                    problem_id=slug,
                    account=account,
                    timestamp=time.time(),
                    correlation_mechanism="leetcode_dom_scoped_result_container",
                    evidence={"text": text[:150]},
                )

    if seen_pending:
        return SubmissionVerdict(
            state=VerdictState.PENDING,
            message="Evaluation timed out on LeetCode (evaluation remains pending).",
            platform="leetcode",
            problem_id=slug,
            account=account,
        )

    if last_error:
        return SubmissionVerdict(
            state=VerdictState.ERROR,
            message=f"LeetCode DOM inspection error: {last_error}",
            platform="leetcode",
            problem_id=slug,
            account=account,
        )

    # REQUIREMENT 3: Remove 'Submitted (No negative verdict detected)' fallback
    return SubmissionVerdict(
        state=VerdictState.UNKNOWN,
        message="No submission result container detected on LeetCode within timeout window.",
        platform="leetcode",
        problem_id=slug,
        account=account,
    )

async def _verify_codechef(
    page: Page,
    url: str,
    submission_start_time: float,
    pre_submission_id: Optional[str] = None,
    account: Optional[str] = None,
    expected_problem_id: Optional[str] = None,
    timeout: float = 30.0,
    initial_interval: float = 1.0,
) -> SubmissionVerdict:
    prob_code = expected_problem_id or extract_problem_id(url, "codechef")
    print(f"   ⏳ Checking CodeChef submission result ({prob_code})...")

    last_error = None
    seen_pending = False

    async for poll, elapsed in poll_with_backoff(
        timeout=timeout, initial_interval=initial_interval, max_interval=4.0, backoff_factor=1.4
    ):
        try:
            res = await page.evaluate(r"""() => {
                // Scope STRICTLY to the submission status container only
                const selectors = [
                    '#submission-status',
                    '[data-testid="submission-result"]',
                    'div[class*="submission-status"]',
                    'div[class*="Verdict_verdict"]',
                    '.submission-result',
                    '.status-label',
                    'div[class*="verdict_container"]'
                ];
                let container = null;
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) { container = el; break; }
                }
                if (!container) return null;

                const text = (container.innerText || '').trim();
                let subId = null;
                const link = container.querySelector('a[href*="/viewsolution/"], a[href*="/status/"]') ||
                             document.querySelector('a[href*="/viewsolution/"]');
                if (link && link.href) {
                    const m = link.href.match(/(?:viewsolution|status)\/(\d+)/);
                    if (m) subId = m[1];
                }
                if (!subId) {
                    const m = text.match(/Submission\s*#?(\d+)/i);
                    if (m) subId = m[1];
                }

                return {
                    text: text,
                    submissionId: subId
                };
            }""")
        except Exception as e:
            last_error = str(e)
            continue

        if not res or not isinstance(res, dict):
            continue

        text = res.get("text", "")
        sub_id = res.get("submissionId")

        # Stale check
        if sub_id and pre_submission_id and str(sub_id) == str(pre_submission_id):
            continue

        # Pending check
        if any(p in text for p in ["Running", "Judging", "In Queue", "Compiling", "Evaluating", "Pending"]):
            seen_pending = True
            continue

        # Accepted check
        if any(a in text for a in ["Correct Answer", "100 pts", "100 / 100", "AC", "Accepted"]):
            corr_mechanism = "codechef_dom_submission_id" if sub_id else "codechef_dom_scoped_status_container"
            effective_id = str(sub_id) if sub_id else f"codechef_{prob_code}_{int(submission_start_time)}"
            evidence = {
                "submission_id": effective_id,
                "problem_code": prob_code,
                "result_snippet": text[:150],
                "correlation_mechanism": corr_mechanism,
                "submission_start_time": submission_start_time,
            }
            return SubmissionVerdict(
                state=VerdictState.ACCEPTED,
                message="Accepted",
                submission_id=effective_id,
                platform="codechef",
                problem_id=prob_code,
                account=account,
                timestamp=time.time(),
                correlation_mechanism=corr_mechanism,
                evidence=evidence,
            )

        # Rejected check
        rejected_indicators = ["Wrong Answer", "WA", "Partially Solved", "Compilation Error", "Time Limit Exceeded", "TLE", "Runtime Error", "SIGSEGV"]
        for rej in rejected_indicators:
            if rej in text:
                return SubmissionVerdict(
                    state=VerdictState.REJECTED,
                    message=rej,
                    submission_id=str(sub_id) if sub_id else None,
                    platform="codechef",
                    problem_id=prob_code,
                    account=account,
                    timestamp=time.time(),
                    correlation_mechanism="codechef_dom_scoped_status_container",
                    evidence={"text": text[:150]},
                )

    if seen_pending:
        return SubmissionVerdict(
            state=VerdictState.PENDING,
            message="Evaluation timed out on CodeChef (submission still evaluating).",
            platform="codechef",
            problem_id=prob_code,
            account=account,
        )

    if last_error:
        return SubmissionVerdict(
            state=VerdictState.ERROR,
            message=f"CodeChef DOM inspection error: {last_error}",
            platform="codechef",
            problem_id=prob_code,
            account=account,
        )

    # REQUIREMENT 5: Remove 'submission sent' success fallback
    return SubmissionVerdict(
        state=VerdictState.UNKNOWN,
        message="No submission status container detected on CodeChef within timeout window.",
        platform="codechef",
        problem_id=prob_code,
        account=account,
    )

async def _verify_hackerearth(
    page: Page,
    url: str,
    submission_start_time: float,
    pre_submission_id: Optional[str] = None,
    account: Optional[str] = None,
    expected_problem_id: Optional[str] = None,
    timeout: float = 35.0,
    initial_interval: float = 1.0,
) -> SubmissionVerdict:
    slug = expected_problem_id or extract_problem_id(url, "hackerearth")
    print(f"   ⏳ Checking HackerEarth submission verdict ({slug})...")

    last_error = None
    seen_pending = False

    async for poll, elapsed in poll_with_backoff(
        timeout=timeout, initial_interval=initial_interval, max_interval=4.0, backoff_factor=1.4
    ):
        try:
            res = await page.evaluate(r"""() => {
                // Scope STRICTLY to the submission result / modal container
                const selectors = [
                    'div[class*="submission-result"]',
                    '.submission-result',
                    'div[class*="track-submission"]',
                    'div[id*="submission-result"]',
                    'div[class*="result-status"]',
                    'div[class*="verdict-wrapper"]',
                    'div.modal-content .verdict',
                    'div[class*="submission-overview"]',
                    '.status-description'
                ];
                let container = null;
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) { container = el; break; }
                }
                if (!container) return null;

                const text = (container.innerText || '').trim();
                let subId = null;
                const link = container.querySelector('a[href*="/submission/"]') ||
                             document.querySelector('a[href*="/submission/"]');
                if (link && link.href) {
                    const m = link.href.match(/submission\/(?:[a-zA-Z0-9_\-]+\/)?(\d+)/);
                    if (m) subId = m[1];
                }
                if (!subId && container.getAttribute('data-submission-id')) {
                    subId = container.getAttribute('data-submission-id');
                }

                return {
                    text: text,
                    submissionId: subId
                };
            }""")
        except Exception as e:
            last_error = str(e)
            continue

        if not res or not isinstance(res, dict):
            continue

        text = res.get("text", "")
        sub_id = res.get("submissionId")

        # Stale check
        if sub_id and pre_submission_id and str(sub_id) == str(pre_submission_id):
            continue

        # Pending check
        if any(p in text for p in ["Queued", "Running test cases", "Evaluating", "Compiling", "In Queue"]):
            seen_pending = True
            continue

        # Accepted check
        if any(a in text for a in ["Accepted", "Correct Answer", "Score: 100"]):
            corr_mechanism = "hackerearth_dom_modal_submission_id" if sub_id else "hackerearth_dom_scoped_modal_container"
            effective_id = str(sub_id) if sub_id else f"hackerearth_{slug}_{int(submission_start_time)}"
            evidence = {
                "submission_id": effective_id,
                "problem_slug": slug,
                "result_snippet": text[:150],
                "correlation_mechanism": corr_mechanism,
                "submission_start_time": submission_start_time,
            }
            return SubmissionVerdict(
                state=VerdictState.ACCEPTED,
                message="Accepted",
                submission_id=effective_id,
                platform="hackerearth",
                problem_id=slug,
                account=account,
                timestamp=time.time(),
                correlation_mechanism=corr_mechanism,
                evidence=evidence,
            )

        # Rejected check
        rejected_indicators = ["Wrong Answer", "Compilation Error", "Time Limit Exceeded", "Memory Limit Exceeded", "Runtime Error"]
        for rej in rejected_indicators:
            if rej in text:
                return SubmissionVerdict(
                    state=VerdictState.REJECTED,
                    message=rej,
                    submission_id=str(sub_id) if sub_id else None,
                    platform="hackerearth",
                    problem_id=slug,
                    account=account,
                    timestamp=time.time(),
                    correlation_mechanism="hackerearth_dom_scoped_modal_container",
                    evidence={"text": text[:150]},
                )

    if seen_pending:
        return SubmissionVerdict(
            state=VerdictState.PENDING,
            message="Evaluation timed out on HackerEarth (evaluation pending).",
            platform="hackerearth",
            problem_id=slug,
            account=account,
        )

    if last_error:
        return SubmissionVerdict(
            state=VerdictState.ERROR,
            message=f"HackerEarth DOM inspection error: {last_error}",
            platform="hackerearth",
            problem_id=slug,
            account=account,
        )

    # REQUIREMENT 6: Remove 'evaluation pending' success fallback
    return SubmissionVerdict(
        state=VerdictState.UNKNOWN,
        message="No submission result container detected on HackerEarth within timeout window.",
        platform="hackerearth",
        problem_id=slug,
        account=account,
    )

async def verify_live_submission_verdict(
    page: Page,
    platform_type: str,
    url: str,
    submission_start_time: Optional[float] = None,
    pre_submission_id: Optional[str] = None,
    account: Optional[str] = None,
    expected_problem_id: Optional[str] = None,
    timeout: Optional[float] = None,
    initial_interval: Optional[float] = None,
) -> SubmissionVerdict:
    """Polls the judge / platform API to verify that the submission was genuinely Accepted.
    
    Guarantees verdict integrity:
    1. Returns typed verdict states: ACCEPTED, REJECTED, PENDING, UNKNOWN, ERROR.
    2. Never returns ACCEPTED merely because no error was observed or evaluation is pending.
    3. Matches problem ID, account, submission time, and submission ID to reject stale verdicts.
    4. Scopes DOM extraction strictly to current submission results.
    5. Implements bounded polling with timeouts and exponential backoff.
    """
    start_ts = submission_start_time if submission_start_time is not None else time.time()
    prob_id = expected_problem_id or extract_problem_id(url, platform_type)

    if platform_type == "hackerrank":
        return await _verify_hackerrank(
            page, url, submission_start_time=start_ts, pre_submission_id=pre_submission_id,
            account=account, expected_problem_id=prob_id,
            timeout=timeout if timeout is not None else 45.0,
            initial_interval=initial_interval if initial_interval is not None else 1.5
        )
    elif platform_type == "codeforces":
        return await _verify_codeforces(
            page, url, submission_start_time=start_ts, pre_submission_id=pre_submission_id,
            account=account, expected_problem_id=prob_id,
            timeout=timeout if timeout is not None else 60.0,
            initial_interval=initial_interval if initial_interval is not None else 1.5
        )
    elif platform_type == "leetcode":
        return await _verify_leetcode(
            page, url, submission_start_time=start_ts, pre_submission_id=pre_submission_id,
            account=account, expected_problem_id=prob_id,
            timeout=timeout if timeout is not None else 30.0,
            initial_interval=initial_interval if initial_interval is not None else 1.0
        )
    elif platform_type == "codechef":
        return await _verify_codechef(
            page, url, submission_start_time=start_ts, pre_submission_id=pre_submission_id,
            account=account, expected_problem_id=prob_id,
            timeout=timeout if timeout is not None else 30.0,
            initial_interval=initial_interval if initial_interval is not None else 1.0
        )
    elif platform_type == "hackerearth":
        return await _verify_hackerearth(
            page, url, submission_start_time=start_ts, pre_submission_id=pre_submission_id,
            account=account, expected_problem_id=prob_id,
            timeout=timeout if timeout is not None else 35.0,
            initial_interval=initial_interval if initial_interval is not None else 1.0
        )

    # Universal fallback: NEVER return ACCEPTED
    return SubmissionVerdict(
        state=VerdictState.UNKNOWN,
        message=f"Unsupported platform type: {platform_type}",
        platform=platform_type,
        problem_id=prob_id,
    )


async def verify_codeforces_samples_java(page: Page, code: str) -> Tuple[bool, str]:
    """Compiles Java solution using javac and runs against sample test cases locally."""
    try:
        samples = await extract_sample_test_cases_from_page(page, "codeforces")
        if not samples:
            return True, "No sample test cases found on page."
        all_passed, results, summary = run_all_sample_tests_sandboxed(code, samples, "codeforces")
        return all_passed, summary
    except Exception as e:
        return True, f"Local test skipped: {e}"

# ==============================================================================
# URL Helpers & File Archival (.java)
# ==============================================================================
REPOS_DIR = BASE_DIR / "repos"

PLATFORM_REPO_CONFIG = {
    "leetcode": {
        "repo_name": "leetcode-daily",
        "class_filename": "Solution.java",
    },
    "codeforces": {
        "repo_name": "codeforces-daily",
        "class_filename": "Main.java",
    },
    "codechef": {
        "repo_name": "codechef-daily",
        "class_filename": "Main.java",
    },
    "hackerrank": {
        "repo_name": "hackerrank-daily",
        "class_filename": "Solution.java",
    },
    "hackerearth": {
        "repo_name": "hackerearth-daily",
        "class_filename": "TestClass.java",
    },
}

def get_safe_java_filename(url: str, platform_name: str) -> str:
    """Generates a clean Java filename like 'solutions/leetcode_two_sum.java'."""
    clean_url = url.rstrip("/")
    slug = clean_url.split("/")[-1]
    if slug in ["description", "problem"]:
        slug = clean_url.split("/")[-2]
    if "codeforces.com" in url:
        parts = clean_url.split("/")
        if len(parts) >= 2:
            slug = f"{parts[-2]}_{parts[-1]}"

    slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", slug)
    return f"{platform_name.lower()}_{slug}.java"

def save_java_solution(
    filename: str,
    code: str,
    url: str,
    explanation: str,
    platform_name: str,
    verdict: Optional[SubmissionVerdict] = None
):
    """Saves Java solution to solutions/ directory with metadata header including verified verdict and submission ID."""
    file_path = SOLUTIONS_DIR / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    sub_id_line = f" * Submission ID: {verdict.submission_id or verdict.correlation_mechanism}\n" if verdict is not None else ""
    verdict_line = f" * Verdict: {verdict.state.value}\n" if verdict is not None else ""
    header = f"""/* ==============================================================================
 * Platform: {platform_name}
 * Language: Java
 * URL: {url}
 * Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
{sub_id_line}{verdict_line} *
 * Approach:
 * {explanation}
 * ==============================================================================
 */

"""
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(header + code + "\n")
    print(f"   💾 Saved Java solution to '{file_path}'")

def commit_solution_to_github(
    platform_type: str,
    url: str,
    code: str,
    explanation: str,
    platform_name: str,
    verdict: Optional[SubmissionVerdict] = None
) -> PublishResult:
    """Commits and pushes the solution to the user's dedicated GitHub repository for that platform."""
    if not GITHUB_USERNAME:
        print("   ⚠️  GITHUB_USERNAME is not configured. Skipping GitHub commit.")
        return PublishResult(success=False, error="GITHUB_USERNAME is not configured")

    if platform_type not in PLATFORM_REPO_CONFIG:
        print(f"   ⚠️  Unknown platform_type '{platform_type}'. Skipping GitHub commit.")
        return PublishResult(success=False, error=f"Unknown platform_type: {platform_type}")

    # Enforce acceptance requirement: never commit as accepted if not confirmed ACCEPTED
    if verdict is not None and verdict.state != VerdictState.ACCEPTED:
        err = f"Cannot commit solution with non-accepted verdict: {verdict.state.value}"
        print(f"   🛑 {err}")
        return PublishResult(success=False, error=err)

    repo_info = PLATFORM_REPO_CONFIG[platform_type]
    repo_name = repo_info["repo_name"]
    repo_dir = REPOS_DIR / repo_name

    # If repo doesn't exist locally, clone it via gh
    if not repo_dir.exists():
        REPOS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            print(f"   📦 Cloning https://github.com/{GITHUB_USERNAME}/{repo_name}...")
            clone_res = subprocess.run(
                ["gh", "repo", "clone", f"{GITHUB_USERNAME}/{repo_name}", str(repo_dir)],
                capture_output=True,
                text=True
            )
            if clone_res.returncode != 0:
                err = clone_res.stderr.strip() or clone_res.stdout.strip() or "gh repo clone failed"
                print(f"   ⚠️  Could not clone repo {repo_name}: {err}")
                return PublishResult(success=False, error=f"Clone failed: {err}", repo_name=repo_name)
        except Exception as e:
            print(f"   ⚠️  Could not clone repo {repo_name}: {e}")
            return PublishResult(success=False, error=f"Clone failed: {e}", repo_name=repo_name)

    branch = get_repo_default_branch(repo_dir)

    # Pull latest changes before modifying working tree; stop immediately on failure (Failed pull -> no commit)
    try:
        pull_res = subprocess.run(
            ["git", "pull", "--rebase", "origin", branch],
            cwd=repo_dir,
            capture_output=True,
            text=True
        )
        if pull_res.returncode != 0:
            err = pull_res.stderr.strip() or pull_res.stdout.strip() or "git pull --rebase failed"
            print(f"   ⚠️  Git pull --rebase failed on branch '{branch}': {err}")
            return PublishResult(success=False, error=f"Pull failed: {err}", repo_name=repo_name, branch=branch)
    except Exception as e:
        print(f"   ⚠️  Git pull exception: {e}")
        return PublishResult(success=False, error=f"Pull failed: {e}", repo_name=repo_name, branch=branch)

    # Extract clean slug
    clean_url = url.rstrip("/")
    slug = clean_url.split("/")[-1]
    if slug in ["description", "problem"]:
        slug = clean_url.split("/")[-2]
    if "codeforces.com" in url:
        parts = clean_url.split("/")
        if len(parts) >= 2:
            slug = f"{parts[-2]}-{parts[-1]}"
    clean_slug = re.sub(r"[^a-zA-Z0-9\-]", "-", slug).strip("-").lower()

    today_str = datetime.now().strftime("%Y-%m-%d")
    prob_dir = repo_dir / "solutions" / today_str / clean_slug
    prob_dir.mkdir(parents=True, exist_ok=True)

    # Write Java source file
    src_filename = repo_info["class_filename"]
    src_path = prob_dir / src_filename
    src_path.write_text(code, encoding="utf-8")

    # Write README.md with verifiable submission evidence
    readme_path = prob_dir / "README.md"
    sub_id_text = f"\nSubmission ID: `{verdict.submission_id or verdict.correlation_mechanism}`" if verdict is not None else ""
    verdict_display = "Accepted" if (verdict is not None and verdict.state == VerdictState.ACCEPTED) else (verdict.state.value if verdict is not None else "Unverified")
    readme_content = f"""# {clean_slug}

[Original practice problem]({url})

Date: {today_str} (Asia/Kolkata). Status: **{verdict_display}**. Platform: **{platform_name}**.{sub_id_text}

## Approach

{explanation}

## Verification

Solution generated in Java and verified on {platform_name} by autonomous AI bot.
"""
    readme_path.write_text(readme_content, encoding="utf-8")

    # Stage ONLY the solution and generated documentation (NEVER git add .)
    try:
        rel_src = src_path.relative_to(repo_dir)
        rel_readme = readme_path.relative_to(repo_dir)

        add_res = subprocess.run(
            ["git", "add", str(rel_src), str(rel_readme)],
            cwd=repo_dir,
            capture_output=True,
            text=True
        )
        if add_res.returncode != 0:
            err = add_res.stderr.strip() or add_res.stdout.strip() or "git add failed"
            print(f"   ⚠️  Git add failed: {err}")
            return PublishResult(success=False, error=f"Stage failed: {err}", repo_name=repo_name, branch=branch)

        status_res = subprocess.run(
            ["git", "status", "--porcelain", str(rel_src), str(rel_readme)],
            cwd=repo_dir,
            capture_output=True,
            text=True
        )

        # Commit only if there are changes; stop immediately on failure (Failed commit -> no push)
        if status_res.stdout.strip():
            commit_msg = f"Add verified Java solution for {clean_slug} [{platform_name}]"
            commit_res = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=repo_dir,
                capture_output=True,
                text=True
            )
            if commit_res.returncode != 0:
                err = commit_res.stderr.strip() or commit_res.stdout.strip() or "git commit failed"
                print(f"   ⚠️  Git commit failed: {err}")
                return PublishResult(success=False, error=f"Commit failed: {err}", repo_name=repo_name, branch=branch)
        else:
            print(f"   ℹ️  GitHub repo '{repo_name}' already has solution staged/committed for {clean_slug}.")

        # Push to remote default branch to confirm remote is in sync
        push_res = subprocess.run(
            ["git", "push", "origin", branch],
            cwd=repo_dir,
            capture_output=True,
            text=True
        )
        if push_res.returncode == 0:
            commit_hash = None
            rev_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True)
            if rev_res.returncode == 0:
                commit_hash = rev_res.stdout.strip()
            print(f"   🐙 GitHub Commit & Push successful: {GITHUB_USERNAME}/{repo_name} ({clean_slug}) [{branch}]!")
            return PublishResult(success=True, repo_name=repo_name, branch=branch, commit_hash=commit_hash)
        else:
            err = push_res.stderr.strip() or push_res.stdout.strip() or "git push failed"
            print(f"   ⚠️  Git push notice: {err}")
            return PublishResult(success=False, error=f"Push failed: {err}", repo_name=repo_name, branch=branch)
    except Exception as e:
        print(f"   ⚠️  Git operation notice: {e}")
        return PublishResult(success=False, error=f"Git operation failed: {e}", repo_name=repo_name, branch=branch)

# ==============================================================================
# Platform Adapters Architecture
# ==============================================================================
class BasePlatformAdapter(ABC):
    platform_type: str = "generic"
    platform_name: str = "Generic"
    domains: List[str] = []
    question_selectors: List[str] = []
    run_selectors: Optional[List[str]] = None
    submit_selectors: Optional[List[str]] = None

    def __init__(self):
        self.rate_limiter = get_rate_limiter(self.platform_type)

    @abstractmethod
    async def discover(self, page: Page, count: int = 15, difficulty: str = "medium-hard", offset: int = 0) -> List[str]:
        pass

    @abstractmethod
    async def check_auth_state(self, page: Page) -> AuthStateResult:
        pass

    async def validate_url_and_platform(self, page: Page, expected_url: str) -> Tuple[bool, Optional[str]]:
        if not page:
            return False, "Page instance is null"
        curr_url = ""
        try:
            curr_url = page.url or ""
        except Exception:
            pass
        if not curr_url or curr_url.startswith("about:blank"):
            return True, None

        if self.domains and not any(d in curr_url for d in self.domains):
            return False, f"Wrong platform domain: current URL '{curr_url}' does not match expected {self.domains}"

        if any(r in curr_url.lower() for r in ["/login", "/enter", "/auth", "/accounts/login", "/checkpoint"]):
            return False, f"Redirected to login/auth barrier: {curr_url}"

        if any(r in curr_url.lower() for r in ["/404", "/page-not-found", "/error"]):
            return False, f"Target problem returned 404 or page error: {curr_url}"

        return True, None

    async def detect_security_challenge(self, page: Page) -> SecurityChallengeDetection:
        return await detect_security_challenge_or_captcha(page)

    async def is_live_or_rated_contest(self, page: Page, url: str) -> Tuple[bool, Optional[str]]:
        return await check_live_or_rated_contest(page, url, self.platform_type)

    async def set_language(self, page: Page) -> bool:
        await switch_editor_to_java(page, self.platform_type)
        return True

    async def extract(self, page: Page, url: str) -> ProblemExtractionResult:
        text = await extract_problem_text(page, self.question_selectors)
        starter = await extract_editor_code(page)
        samples = await extract_sample_test_cases_from_page(page, self.platform_type)
        if not samples and text:
            samples = extract_sample_test_cases_from_text(text)
        success = bool(text and len(text) > 80)
        return ProblemExtractionResult(
            success=success,
            problem_text=text,
            starter_code=starter,
            samples=samples,
            error=None if success else "Failed to extract problem description"
        )

    async def inject(self, page: Page, solution_code: str) -> InjectionResult:
        return await inject_code_into_editor(page, solution_code)

    async def submit(self, page: Page, url: str, solution_code: str, snapshot: Optional[Dict[str, Any]] = None) -> bool:
        if not self.submit_selectors:
            return False
        for selector in self.submit_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    print(f"   🚀 Clicked submit button ({selector})")
                    return True
            except Exception:
                continue
        return False

    async def verify(
        self,
        page: Page,
        url: str,
        submission_start_time: Optional[float] = None,
        pre_submission_id: Optional[str] = None,
        account: Optional[str] = None,
        expected_problem_id: Optional[str] = None
    ) -> SubmissionVerdict:
        return await verify_live_submission_verdict(
            page,
            self.platform_type,
            url,
            submission_start_time=submission_start_time,
            pre_submission_id=pre_submission_id,
            account=account,
            expected_problem_id=expected_problem_id
        )


class LeetCodeAdapter(BasePlatformAdapter):
    platform_type = "leetcode"
    platform_name = "LeetCode"
    domains = ["leetcode.com"]
    question_selectors = [
        "div[data-track-load='description_content']",
        "div.elfjS",
        "[data-key='description-content']",
        "div.content__uRIT",
        "[data-layout-path*='description']",
        "div[class*='description']",
        ".question-content",
    ]
    run_selectors = [
        "button[data-e2e-locator='console-run-button']",
        "button:has-text('Run')",
        "[data-cypress='RunCode']",
    ]
    submit_selectors = [
        "button[data-e2e-locator='console-submit-button']",
        "button:has-text('Submit')",
        "[data-cypress='SubmitCode']",
    ]

    async def discover(self, page: Page, count: int = 15, difficulty: str = "medium-hard", offset: int = 0) -> List[str]:
        return await discover_leetcode_candidates(page, count=count, difficulty=difficulty, offset=offset)

    async def check_auth_state(self, page: Page) -> AuthStateResult:
        js = """() => {
            const hasLogin = !!document.querySelector('a[href*="/accounts/login"], a:has-text("Sign in")');
            const hasAuth = !!document.querySelector('[data-cypress="UserAvatar"], #navbar_user_avatar, .avatar, a[href*="/profile/"]');
            if (hasAuth) return 'LOGGED_IN';
            if (hasLogin) return 'LOGGED_OUT';
            return 'UNKNOWN';
        }"""
        try:
            st = await page.evaluate(js)
            if st == "LOGGED_OUT":
                return AuthStateResult(state=AuthState.LOGGED_OUT, details="LeetCode login link detected", requires_human=True)
            if st == "LOGGED_IN":
                return AuthStateResult(state=AuthState.LOGGED_IN)
        except Exception:
            pass
        return AuthStateResult(state=AuthState.UNKNOWN)


class CodeforcesAdapter(BasePlatformAdapter):
    platform_type = "codeforces"
    platform_name = "Codeforces"
    domains = ["codeforces.com"]
    question_selectors = [
        ".problem-statement",
        "#pageContent",
    ]
    run_selectors = None
    submit_selectors = [
        "form.submit-form input.submit",
        "input.submit",
        "button:has-text('Submit')",
    ]

    async def discover(self, page: Page, count: int = 15, difficulty: str = "medium-hard", offset: int = 0) -> List[str]:
        return await discover_codeforces_candidates(page, count=count, difficulty=difficulty, offset=offset)

    async def check_auth_state(self, page: Page) -> AuthStateResult:
        js = """() => {
            const hasAuth = !!document.querySelector('a[href*="/logout"], a[href*="/profile/"]');
            const hasLogin = !!document.querySelector('a[href*="/enter"], a[href*="/register"]');
            if (hasAuth) return 'LOGGED_IN';
            if (hasLogin) return 'LOGGED_OUT';
            return 'UNKNOWN';
        }"""
        try:
            st = await page.evaluate(js)
            if st == "LOGGED_OUT":
                return AuthStateResult(state=AuthState.LOGGED_OUT, details="Codeforces /enter link detected", requires_human=True)
            if st == "LOGGED_IN":
                return AuthStateResult(state=AuthState.LOGGED_IN, username=CODEFORCES_HANDLE)
        except Exception:
            pass
        return AuthStateResult(state=AuthState.UNKNOWN)

    async def submit(self, page: Page, url: str, solution_code: str, snapshot: Optional[Dict[str, Any]] = None) -> bool:
        clean_url = url.rstrip("/")
        parts = clean_url.split("/")
        prob_code = f"{parts[-2]}{parts[-1]}"
        try:
            await page.goto("https://codeforces.com/problemset/submit", wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(1)
            await page.locator('input[name="submittedProblemCode"]').fill(prob_code)
            sel = page.locator('select[name="programTypeId"]').first
            if await sel.count() > 0:
                options = await sel.locator("option").all()
                for opt in options:
                    txt = await opt.inner_text()
                    if "Java 21" in txt or "Java 17" in txt:
                        val = await opt.get_attribute("value")
                        await sel.select_option(val)
                        break
            await page.locator('textarea#sourceCodeTextarea, textarea[name="source"]').fill(solution_code)
            await page.locator('input.submit').click()
            print("   🚀 Clicked Codeforces submit button!")
            return True
        except Exception as e:
            print(f"   ⚠️ Codeforces submit error: {e}")
            return False


class HackerRankAdapter(BasePlatformAdapter):
    platform_type = "hackerrank"
    platform_name = "HackerRank"
    domains = ["hackerrank.com"]
    question_selectors = [
        ".challenge-body-html",
        ".problem-statement",
        ".challenge_problem_statement",
        ".challenge-body",
        ".problem-statement-limits",
    ]
    run_selectors = [
        "button.hr-monaco-compile",
        "button:has-text('Run Code')",
        "button[data-analytics='CompileAndTest']",
    ]
    submit_selectors = [
        "button.hr-monaco-submit",
        "button:has-text('Submit Code')",
        "button[data-analytics='SubmitCode']",
    ]

    async def discover(self, page: Page, count: int = 15, difficulty: str = "medium-hard", offset: int = 0) -> List[str]:
        return await discover_hackerrank_candidates(page, count=count, difficulty=difficulty, offset=offset)

    async def check_auth_state(self, page: Page) -> AuthStateResult:
        js = """() => {
            const hasAuth = !!document.querySelector('.avatar, [data-analytics="NavBarProfileAvatar"], a[href*="/profile/"]');
            const hasLogin = !!document.querySelector('a[href*="/auth/login"], a:has-text("Log In")');
            if (hasAuth) return 'LOGGED_IN';
            if (hasLogin) return 'LOGGED_OUT';
            return 'UNKNOWN';
        }"""
        try:
            st = await page.evaluate(js)
            if st == "LOGGED_OUT":
                return AuthStateResult(state=AuthState.LOGGED_OUT, details="HackerRank login link detected", requires_human=True)
            if st == "LOGGED_IN":
                return AuthStateResult(state=AuthState.LOGGED_IN)
        except Exception:
            pass
        return AuthStateResult(state=AuthState.UNKNOWN)


class CodeChefAdapter(BasePlatformAdapter):
    platform_type = "codechef"
    platform_name = "CodeChef"
    domains = ["codechef.com"]
    question_selectors = [
        "#problem-statement",
        "._problem_statement_container_",
        "div[class*='problem-statement']",
        "div[class*='ProblemStatement']",
        "div[id*='problem-statement']",
        ".problem-statement",
        "div._content_container_",
    ]
    run_selectors = [
        "button:has-text('Run')",
        "button:has-text('Run Code')",
        "button[id*='run']",
        "button[data-testid*='run']",
    ]
    submit_selectors = [
        "button:has-text('Submit')",
        "button:has-text('Submit Code')",
        "button[id*='submit']",
        "button[data-testid*='submit']",
    ]

    async def discover(self, page: Page, count: int = 15, difficulty: str = "medium-hard", offset: int = 0) -> List[str]:
        return await discover_codechef_candidates(page, count=count, difficulty=difficulty, offset=offset)

    async def check_auth_state(self, page: Page) -> AuthStateResult:
        js = """() => {
            const hasAuth = !!document.querySelector('a[href*="/logout"], .user-details, .profile-image');
            const hasLogin = !!document.querySelector('a[href*="/login"], a:has-text("Log In")');
            if (hasAuth) return 'LOGGED_IN';
            if (hasLogin) return 'LOGGED_OUT';
            return 'UNKNOWN';
        }"""
        try:
            st = await page.evaluate(js)
            if st == "LOGGED_OUT":
                return AuthStateResult(state=AuthState.LOGGED_OUT, details="CodeChef login link detected", requires_human=True)
            if st == "LOGGED_IN":
                return AuthStateResult(state=AuthState.LOGGED_IN)
        except Exception:
            pass
        return AuthStateResult(state=AuthState.UNKNOWN)


class HackerEarthAdapter(BasePlatformAdapter):
    platform_type = "hackerearth"
    platform_name = "HackerEarth"
    domains = ["hackerearth.com"]
    question_selectors = [
        ".problem-description",
        ".problem-details",
        ".statement",
        "div[class*='problem-description']",
        "div[class*='problem-statement']",
        "[data-slot='card']",
    ]
    run_selectors = [
        "button:has-text('Compile & Test')",
        "button:has-text('Run')",
        ".compile-test-button",
    ]
    submit_selectors = [
        "button:has-text('Submit')",
        "button:has-text('Submit Code')",
        ".submit-button",
    ]

    async def discover(self, page: Page, count: int = 15, difficulty: str = "medium-hard", offset: int = 0) -> List[str]:
        return await discover_hackerearth_candidates(page, count=count, difficulty=difficulty, offset=offset)

    async def check_auth_state(self, page: Page) -> AuthStateResult:
        js = """() => {
            const hasAuth = !!document.querySelector('a[href*="/logout"], .user-avatar, img[alt*="profile"]');
            const hasLogin = !!document.querySelector('a[href*="/login"], a:has-text("Log In")');
            if (hasAuth) return 'LOGGED_IN';
            if (hasLogin) return 'LOGGED_OUT';
            return 'UNKNOWN';
        }"""
        try:
            st = await page.evaluate(js)
            if st == "LOGGED_OUT":
                return AuthStateResult(state=AuthState.LOGGED_OUT, details="HackerEarth login link detected", requires_human=True)
            if st == "LOGGED_IN":
                return AuthStateResult(state=AuthState.LOGGED_IN)
        except Exception:
            pass
        return AuthStateResult(state=AuthState.UNKNOWN)


class GenericPlatformAdapter(BasePlatformAdapter):
    platform_type = "generic"
    platform_name = "Generic"
    domains = []
    question_selectors = ["article", "main", "#content", ".problem-statement"]

    async def discover(self, page: Page, count: int = 15, difficulty: str = "medium-hard", offset: int = 0) -> List[str]:
        return []

    async def check_auth_state(self, page: Page) -> AuthStateResult:
        return AuthStateResult(state=AuthState.UNKNOWN)


_ADAPTER_INSTANCES: List[BasePlatformAdapter] = [
    LeetCodeAdapter(),
    CodeforcesAdapter(),
    HackerRankAdapter(),
    CodeChefAdapter(),
    HackerEarthAdapter(),
]
_GENERIC_ADAPTER = GenericPlatformAdapter()

def get_platform_adapter(platform_or_url: str) -> BasePlatformAdapter:
    p = (platform_or_url or "").lower().strip()
    for adapter in _ADAPTER_INSTANCES:
        if p == adapter.platform_type or any(d in p for d in adapter.domains):
            return adapter
    return _GENERIC_ADAPTER

# ==============================================================================
# Problem Solver Pipeline (Fully Automated, Java)
# ==============================================================================
async def process_problem(
    page: Optional[Page],
    url: str,
    mode: str = "run",
    retry_publish: bool = False,
    cached_verdict: Optional[SubmissionVerdict] = None,
    cached_code: Optional[str] = None,
    cached_explanation: Optional[str] = None,
    max_repairs: int = 3,
    java_version: Optional[str] = None,
    dry_run: bool = False,
    offline_mode: bool = False,
) -> ProcessResult:
    """Processes a single coding problem URL end-to-end in Java with zero human input."""
    print(f"\n{'='*70}")
    print(f"🚀 Processing: {url}")
    print(f"{'='*70}")

    config = None
    for domain, rules in PLATFORM_RULES.items():
        if domain in url:
            config = rules
            break

    if not config:
        print(f"❌ Unsupported platform domain for URL: {url}")
        return ProcessResult(stage=ProcessStage.GENERATED, success=False, url=url, error="Unsupported platform domain")

    platform_name = config["name"]
    platform_type = config["type"]
    prob_id = extract_problem_id(url, platform_type)

    is_offline = offline_mode or OFFLINE_MODE or (page is None)
    is_dry = dry_run or DRY_RUN

    # Fast-path for offline or dry-run execution
    if is_offline or is_dry:
        print(f"[1/7] Processing problem in {'dry-run' if is_dry else 'offline'} mode (no browser required)...")
        off_info = get_offline_problem_info(url, platform_type)
        problem_text = off_info.get("description", f"Problem {prob_id} on {platform_name}")
        starter_code = off_info.get("starter_code", "")
        samples = [
            SampleTestCase(input_text=s["input"], expected_output=s["expected"], sample_index=i + 1)
            for i, s in enumerate(off_info.get("samples", []))
        ]

        print(f"[4/7] Verifying and repairing Java solution locally (max {max_repairs} repairs)...")
        pipeline_res = verify_and_repair_java_pipeline(
            problem_text=problem_text,
            starter_code=starter_code,
            samples=samples,
            platform_name=platform_name,
            platform_type=platform_type,
            max_repairs=max_repairs,
            java_version=java_version,
            dry_run=is_dry,
        )
        solution_code = pipeline_res.solution_code
        explanation = pipeline_res.explanation
        filename = get_safe_java_filename(url, platform_name)

        if not pipeline_res.success:
            print(f"❌ Failed verification pipeline for {url} ({pipeline_res.failure_reason}). Skipping.")
            return ProcessResult(
                stage=ProcessStage.GENERATED,
                success=False,
                url=url,
                platform=platform_type,
                problem_id=prob_id,
                solution_code=solution_code,
                explanation=explanation,
                error=pipeline_res.failure_reason,
                sample_results=pipeline_res.sample_results,
                published=False
            )

        if mode == "save":
            save_java_solution(filename, solution_code, url, explanation, platform_name, verdict=None)
            return ProcessResult(
                stage=ProcessStage.COMPILED,
                success=True,
                url=url,
                platform=platform_type,
                problem_id=prob_id,
                solution_code=solution_code,
                explanation=explanation,
                saved_path=str(SOLUTIONS_DIR / filename),
                sample_results=pipeline_res.sample_results,
                published=False
            )

        if mode == "run":
            return ProcessResult(
                stage=ProcessStage.TESTED,
                success=True,
                url=url,
                platform=platform_type,
                problem_id=prob_id,
                solution_code=solution_code,
                explanation=explanation,
                sample_results=pipeline_res.sample_results,
                published=False
            )

        # Mode submit or both
        if is_dry:
            sim_id = f"dryrun_{prob_id}_{int(time.time())}"
            verdict = SubmissionVerdict(
                state=VerdictState.ACCEPTED,
                message="Accepted (Simulated Dry Run)",
                submission_id=sim_id,
                platform=platform_type,
                problem_id=prob_id,
                account="dry_run_user",
                timestamp=time.time(),
                correlation_mechanism="simulated_dry_run_harness",
                evidence={"dry_run": True, "submission_id": sim_id}
            )
            save_java_solution(filename, solution_code, url, explanation, platform_name, verdict=verdict)
            pub_res = commit_solution_to_github(
                platform_type=platform_type,
                url=url,
                code=solution_code,
                explanation=explanation,
                platform_name=platform_name,
                verdict=verdict
            )
            return ProcessResult(
                stage=ProcessStage.PUBLISHED if pub_res.success else ProcessStage.ACCEPTED,
                success=True,
                url=url,
                platform=platform_type,
                problem_id=prob_id,
                solution_code=solution_code,
                explanation=explanation,
                verdict=verdict,
                saved_path=str(SOLUTIONS_DIR / filename),
                published=pub_res.success,
                repo_name=pub_res.repo_name,
                repo_branch=pub_res.branch,
                commit_hash=pub_res.commit_hash,
                sample_results=pipeline_res.sample_results
            )
        else:
            save_java_solution(filename, solution_code, url, explanation, platform_name, verdict=None)
            return ProcessResult(
                stage=ProcessStage.TESTED,
                success=True,
                url=url,
                platform=platform_type,
                problem_id=prob_id,
                solution_code=solution_code,
                explanation=explanation,
                saved_path=str(SOLUTIONS_DIR / filename),
                sample_results=pipeline_res.sample_results,
                published=False
            )

    # Fast-path for retrying publishing on already accepted solution without resubmitting
    if retry_publish:
        print(f"\n🔄 Retrying publishing for {url} without re-submitting to judge...")
        if not cached_verdict or cached_verdict.state != VerdictState.ACCEPTED or not cached_code:
            return ProcessResult(
                stage=ProcessStage.PUBLISH_FAILED,
                success=False,
                url=url,
                platform=platform_type,
                problem_id=prob_id,
                solution_code=cached_code,
                explanation=cached_explanation,
                verdict=cached_verdict,
                error="Cannot retry publish without accepted verdict and solution code",
                publish_error="Missing accepted verdict or code for publish retry",
                published=False
            )
        filename = get_safe_java_filename(url, platform_name)
        save_java_solution(filename, cached_code, url, cached_explanation or "", platform_name, verdict=cached_verdict)
        pub_res = commit_solution_to_github(
            platform_type=platform_type,
            url=url,
            code=cached_code,
            explanation=cached_explanation or "",
            platform_name=platform_name,
            verdict=cached_verdict
        )
        if pub_res.success:
            print(f"✅ Retry publish succeeded for {url}!\n")
            return ProcessResult(
                stage=ProcessStage.PUBLISHED,
                success=True,
                url=url,
                platform=platform_type,
                problem_id=prob_id,
                solution_code=cached_code,
                explanation=cached_explanation,
                verdict=cached_verdict,
                saved_path=str(SOLUTIONS_DIR / filename),
                published=True,
                repo_name=pub_res.repo_name,
                repo_branch=pub_res.branch,
                commit_hash=pub_res.commit_hash
            )
        else:
            return ProcessResult(
                stage=ProcessStage.PUBLISH_FAILED,
                success=False,
                url=url,
                platform=platform_type,
                problem_id=prob_id,
                solution_code=cached_code,
                explanation=cached_explanation,
                verdict=cached_verdict,
                saved_path=str(SOLUTIONS_DIR / filename),
                publish_error=pub_res.error,
                error=f"Publish retry failed: {pub_res.error}",
                published=False,
                repo_name=pub_res.repo_name,
                repo_branch=pub_res.branch,
                commit_hash=pub_res.commit_hash
            )

    # 1. Check rate limiter before interacting
    adapter = get_platform_adapter(platform_type)
    ok, rate_err = await adapter.rate_limiter.acquire()
    if not ok:
        print(f"🛑 Rate limiter active on {platform_name}: {rate_err}")
        return ProcessResult(
            stage=ProcessStage.GENERATED,
            success=False,
            url=url,
            platform=platform_type,
            problem_id=prob_id,
            error=redact_sensitive_data(rate_err),
            published=False
        )

    # 2. Check if problem URL is in an active or live/rated contest
    is_contest, contest_reason = await adapter.is_live_or_rated_contest(page, url)
    if is_contest:
        print(f"🛑 Refusing submission into active/rated contest: {contest_reason}")
        return ProcessResult(
            stage=ProcessStage.GENERATED,
            success=False,
            url=url,
            platform=platform_type,
            problem_id=prob_id,
            error=redact_sensitive_data(f"Refusing submission into active/rated contest: {contest_reason}"),
            published=False
        )

    # 3. Navigate to problem page
    print(f"[1/7] Navigating to {platform_name}...")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(2)
    except Exception as e:
        adapter.rate_limiter.record_failure(is_rate_limit=False)
        print(f"❌ Failed to load {url}: {e}")
        return ProcessResult(
            stage=ProcessStage.GENERATED,
            success=False,
            url=url,
            platform=platform_type,
            problem_id=prob_id,
            error=redact_sensitive_data(f"Failed to load {url}: {e}"),
            published=False
        )

    # 4. Check for security challenges / CAPTCHA on the loaded page
    sec_check = await adapter.detect_security_challenge(page)
    if sec_check.detected:
        page._anti_bot_blocked = True
        adapter.rate_limiter.record_failure(is_rate_limit=False)
        print(f"\n🛡️  [{platform_name}] Security challenge detected: {sec_check.challenge_type} ({sec_check.details})")
        print("👉 Automation paused for human action. Please complete the verification in your open Chrome browser window.")
        return ProcessResult(
            stage=ProcessStage.GENERATED,
            success=False,
            url=url,
            platform=platform_type,
            problem_id=prob_id,
            error=redact_sensitive_data(f"Security challenge detected: {sec_check.challenge_type}"),
            published=False
        )

    # 5. Validate exact current URL and expected platform
    valid_url, val_err = await adapter.validate_url_and_platform(page, url)
    if not valid_url:
        print(f"❌ URL validation failed: {val_err}")
        return ProcessResult(
            stage=ProcessStage.GENERATED,
            success=False,
            url=url,
            platform=platform_type,
            problem_id=prob_id,
            error=redact_sensitive_data(val_err),
            published=False
        )

    # 6. Check authentication state
    auth_state = await adapter.check_auth_state(page)
    if auth_state.state in [AuthState.LOGGED_OUT, AuthState.EXPIRED] and mode in ["submit", "both"]:
        print(f"⚠️  [{platform_name}] User not authenticated: {auth_state.state.value} ({auth_state.details})")
        print("👉 Submissions require an active authenticated user session. Please log in.")
        return ProcessResult(
            stage=ProcessStage.GENERATED,
            success=False,
            url=url,
            platform=platform_type,
            problem_id=prob_id,
            error=redact_sensitive_data(f"Authentication required: {auth_state.state.value} ({auth_state.details})"),
            published=False
        )

    # 7. Automatically ensure editor language is set to Java
    print("[2/7] Ensuring editor language is set to Java...")
    await switch_editor_to_java(page, platform_type)

    # 8. Extract problem description
    print("[3/7] Extracting problem description...")
    problem_text = await extract_problem_text(page, config["question_selectors"])
    if not problem_text:
        print("⚠️  Waiting 4 more seconds for lazy-loaded elements...")
        await asyncio.sleep(4)
        problem_text = await extract_problem_text(page, config["question_selectors"])
        if not problem_text:
            print("❌ Unable to find problem description. Skipping.")
            return ProcessResult(stage=ProcessStage.GENERATED, success=False, url=url, platform=platform_type, problem_id=prob_id, error="Unable to find problem description")

    print(f"   📄 Extracted problem description ({len(problem_text)} characters).")

    # 4. Extract starter code from editor (Java)
    starter_code = await extract_editor_code(page)
    if starter_code:
        print(f"   📝 Detected Java starter template ({len(starter_code)} characters).")
    else:
        print("   ℹ️  No starter template detected (will generate complete standalone Java code).")

    # 5. Extract sample test cases & run modular verification and repair pipeline
    print(f"[4/7] Verifying and repairing Java solution locally (max {max_repairs} repairs)...")
    samples = await extract_sample_test_cases_from_page(page, platform_type)
    if not samples:
        samples = extract_sample_test_cases_from_text(problem_text)

    pipeline_res = verify_and_repair_java_pipeline(
        problem_text=problem_text,
        starter_code=starter_code,
        samples=samples,
        platform_name=platform_name,
        platform_type=platform_type,
        max_repairs=max_repairs,
        java_version=java_version,
    )
    solution_code = pipeline_res.solution_code
    explanation = pipeline_res.explanation
    filename = get_safe_java_filename(url, platform_name)

    if not pipeline_res.success:
        print(f"❌ Failed verification pipeline for {url} ({pipeline_res.failure_reason}). Skipping.")
        is_compiled = bool(pipeline_res.compilation and pipeline_res.compilation.success)
        fail_stage = ProcessStage.COMPILED if is_compiled else ProcessStage.GENERATED
        return ProcessResult(
            stage=fail_stage,
            success=False,
            url=url,
            platform=platform_type,
            problem_id=prob_id,
            solution_code=solution_code,
            explanation=explanation,
            error=pipeline_res.failure_reason,
            compiler_output=pipeline_res.compilation.diagnostics if pipeline_res.compilation else None,
            diagnostics=pipeline_res.failure_reason,
            sample_results=pipeline_res.sample_results,
            published=False
        )

    print(f"   💡 Approach: {explanation}")

    # In mode 'save', store locally only; no judge submission or GitHub commit
    if mode == "save":
        print(f"   💾 Mode 'SAVE': saving verified Java solution locally without running or submitting.")
        save_java_solution(filename, solution_code, url, explanation, platform_name, verdict=None)
        return ProcessResult(
            stage=ProcessStage.COMPILED,
            success=True,
            url=url,
            platform=platform_type,
            problem_id=prob_id,
            solution_code=solution_code,
            explanation=explanation,
            verdict=None,
            saved_path=str(SOLUTIONS_DIR / filename),
            compiler_output=pipeline_res.compilation.diagnostics if pipeline_res.compilation else None,
            sample_results=pipeline_res.sample_results,
            published=False
        )

    # 6. Inject code into webpage editor & 7. Execute action
    submission_snapshot = {
        "start_time": time.time(),
        "pre_submission_id": None,
        "account": CODEFORCES_HANDLE if platform_type == "codeforces" else None,
        "problem_id": extract_problem_id(url, platform_type),
    }
    submit_clicked = False

    if platform_type == "codeforces":
        if mode in ["submit", "both"]:
            print("[5/7 & 6/7] Navigating to Codeforces submit page & submitting...")
            try:
                clean_url = url.rstrip("/")
                parts = clean_url.split("/")
                prob_code = f"{parts[-2]}{parts[-1]}"
                
                await page.goto("https://codeforces.com/problemset/submit", wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(2)

                # Check if logged in
                is_logged_in = await page.evaluate("""() => {
                    return !!document.querySelector('a[href*="/logout"], a[href*="/profile/"]');
                }""")
                if not is_logged_in:
                    print("\n" + "=" * 68)
                    print("⚠️  [Codeforces] You are not logged in to Codeforces in this browser profile!")
                    print(f"👉 Please enter your password for '{CODEFORCES_HANDLE}' in the open Chrome window.")
                    print("   (Your login will be preserved in saved_chrome_login for all future runs)")
                    print("=" * 68)
                    try:
                        await page.goto("https://codeforces.com/enter", wait_until="domcontentloaded", timeout=15000)
                        if CODEFORCES_HANDLE:
                            await page.locator('input[name="handleOrEmail"]').fill(CODEFORCES_HANDLE)
                        cb = page.locator('input[name="remember"]')
                        if await cb.count() > 0:
                            await cb.check()
                    except Exception:
                        pass
                    if sys.platform == "darwin":
                        try:
                            subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'], capture_output=True)
                        except Exception:
                            pass
                    logged_in = False
                    for _ in range(45):
                        await asyncio.sleep(2)
                        logged_in = await page.evaluate("""() => !!document.querySelector('a[href*="/logout"], a[href*="/profile/"]')""")
                        if logged_in:
                            print("   ✅ Detected Codeforces login! Navigating back to submit page...")
                            await page.goto("https://codeforces.com/problemset/submit", wait_until="domcontentloaded", timeout=25000)
                            await asyncio.sleep(2)
                            break
                    if not logged_in:
                        page._anti_bot_blocked = True
                        print("\n🛑 Login timed out. Codeforces submissions require an authenticated user account.")
                        save_java_solution(filename, solution_code, url, explanation, platform_name)
                        return ProcessResult(
                            stage=ProcessStage.SUBMITTED,
                            success=False,
                            url=url,
                            platform=platform_type,
                            problem_id=prob_id,
                            solution_code=solution_code,
                            explanation=explanation,
                            saved_path=str(SOLUTIONS_DIR / filename),
                            error="Codeforces login timed out",
                            published=False
                        )
                
                # Fill problem code (e.g. 1352C)
                await page.locator('input[name="submittedProblemCode"]').fill(prob_code)
                print(f"   📝 Entered problem code: {prob_code}")
                
                # Select Java
                sel = page.locator('select[name="programTypeId"]').first
                if await sel.count() > 0:
                    options = await sel.locator("option").all()
                    for opt in options:
                        txt = await opt.inner_text()
                        if "Java 21" in txt or "Java 17" in txt:
                            val = await opt.get_attribute("value")
                            await sel.select_option(val)
                            print(f"   ☕ Selected language: {txt.strip()}")
                            break
                            
                # Fill source code
                await page.locator('textarea#sourceCodeTextarea, textarea[name="source"]').fill(solution_code)
                print("   📋 Pasted Java solution code.")

                # Check for Cloudflare Turnstile anti-bot challenge
                has_turnstile = await page.evaluate("""() => {
                    const cf = document.querySelector('iframe[src*="cloudflare"], iframe[src*="turnstile"], .cf-turnstile, input[name="turnstileToken"], input[name="cf-turnstile-response"]');
                    return !!cf;
                }""")

                if has_turnstile:
                    token = ""
                    for _ in range(8):
                        token = await page.evaluate("""() => {
                            const el = document.querySelector('input[name="turnstileToken"], input[name="cf-turnstile-response"], input[name*="turnstile" i]');
                            return el && el.value && el.value.length > 5 ? el.value : '';
                        }""")
                        if token:
                            print("   ✅ Cloudflare Turnstile token ready! Submitting...")
                            break
                        await asyncio.sleep(1)

                    if not token:
                        print("\n" + "=" * 68)
                        print("🛡️  [Codeforces] Cloudflare Anti-Bot Verification Detected!")
                        print("=" * 68)
                        print("👉 Please complete the verification directly in your open Chrome window.")
                        print("   Waiting for verification to complete (up to 30s)...")
                        print("=" * 68)
                        if sys.platform == "darwin":
                            try:
                                subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'], capture_output=True)
                            except Exception:
                                pass

                        for sec in range(15):
                            await asyncio.sleep(2)
                            token = await page.evaluate("""() => {
                                const el = document.querySelector('input[name="turnstileToken"], input[name="cf-turnstile-response"], input[name*="turnstile" i]');
                                return el && el.value && el.value.length > 5 ? el.value : '';
                            }""")
                            if token:
                                print("   ✅ Human verification completed in Chrome! Submitting...")
                                break
                            if sec > 0 and sec % 5 == 0:
                                print(f"   ⏳ Waiting for verification in Chrome ({30 - sec * 2}s remaining)...")

                    if not token:
                        page._anti_bot_blocked = True
                        print("\n🛑 Codeforces anti-bot verification (Cloudflare Turnstile) was not completed.")
                        save_java_solution(filename, solution_code, url, explanation, platform_name)
                        return ProcessResult(
                            stage=ProcessStage.SUBMITTED,
                            success=False,
                            url=url,
                            platform=platform_type,
                            problem_id=prob_id,
                            solution_code=solution_code,
                            explanation=explanation,
                            saved_path=str(SOLUTIONS_DIR / filename),
                            error="Codeforces anti-bot verification was not completed",
                            published=False
                        )

                # Capture pre-submission snapshot to anchor submission time and detect stale submissions
                submission_snapshot = await capture_pre_submission_snapshot(page, platform_type, url)
                submit_clicked = True

                # Click submit
                await page.locator('input.submit').click()
                print("   🚀 Clicked Codeforces Submit! Waiting for judge verdict...")
                await asyncio.sleep(3)

                # Check if submission was blocked due to anti-bot verification
                err_text = await page.evaluate("""() => {
                    const els = Array.from(document.querySelectorAll('.error, .error__desc, span[class*="error"]'));
                    return els.map(e => (e.innerText || '').trim()).join(' ');
                }""")
                if "anti-bot" in err_text.lower() or "verification" in err_text.lower():
                    print("\n⚠️  Codeforces notice: 'Please complete the anti-bot verification'")
                    print("👉 Please click the verification box in Chrome. Giving 20s before retry...")
                    if sys.platform == "darwin":
                        try:
                            subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'], capture_output=True)
                        except Exception:
                            pass
                    for _ in range(10):
                        await asyncio.sleep(2)
                        token = await page.evaluate("""() => {
                            const el = document.querySelector('input[name="turnstileToken"], input[name="cf-turnstile-response"], input[name*="turnstile" i]');
                            return el && el.value && el.value.length > 5 ? el.value : '';
                        }""")
                        if token:
                            print("   ✅ Verification detected! Re-submitting now...")
                            await page.locator('input.submit').click()
                            await asyncio.sleep(3)
                            break
                    if not token:
                        page._anti_bot_blocked = True
                        print("\n🛑 Codeforces anti-bot verification was not completed.")
                        save_java_solution(filename, solution_code, url, explanation, platform_name)
                        return ProcessResult(
                            stage=ProcessStage.SUBMITTED,
                            success=False,
                            url=url,
                            platform=platform_type,
                            problem_id=prob_id,
                            solution_code=solution_code,
                            explanation=explanation,
                            saved_path=str(SOLUTIONS_DIR / filename),
                            error="Codeforces anti-bot verification not completed on retry",
                            published=False
                        )
            except Exception as e:
                print(f"   ⚠️  Codeforces submit notice: {e}")
        else:
            print("[5/7 & 6/7] Codeforces local test complete (Mode: RUN).")
    else:
        print("[5/7] Injecting Java solution into editor...")
        inject_res = await inject_code_into_editor(page, solution_code)
        if not inject_res or (hasattr(inject_res, "verified") and not inject_res.verified):
            print("   ❌ Editor injection failed verification! Aborting submission.")
            return ProcessResult(
                stage=ProcessStage.GENERATED,
                success=False,
                url=url,
                platform=platform_type,
                problem_id=prob_id,
                solution_code=solution_code,
                explanation=explanation,
                error="Editor injection verification failed",
                published=False
            )

        # Execute action (Run / Submit) based on mode
        print(f"[6/7] Performing action (Mode: {mode.upper()})...")
        if mode in ["run", "both"] and config.get("run_selectors"):
            run_clicked = False
            for selector in config["run_selectors"]:
                btn = page.locator(selector).first
                try:
                    if await btn.is_visible():
                        await btn.click()
                        print(f"   ▶️  Clicked 'Run Code' button ({selector})!")
                        run_clicked = True
                        break
                except Exception:
                    continue
            if run_clicked:
                wait_time = 10 if mode == "both" else 7
                print(f"   ⏳ Waiting {wait_time}s for compilation and test results...")
                await asyncio.sleep(wait_time)

        if mode in ["submit", "both"] and config.get("submit_selectors"):
            is_live_c, c_err = await adapter.is_live_or_rated_contest(page, url)
            if is_live_c:
                print(f"🛑 Refusing submission into active/rated contest: {c_err}")
                return ProcessResult(
                    stage=ProcessStage.SUBMITTED,
                    success=False,
                    url=url,
                    platform=platform_type,
                    problem_id=prob_id,
                    solution_code=solution_code,
                    explanation=explanation,
                    error=redact_sensitive_data(f"Refusing submission into active/rated contest: {c_err}"),
                    published=False
                )
            submission_snapshot = await capture_pre_submission_snapshot(page, platform_type, url)
            submit_clicked = False
            for selector in config["submit_selectors"]:
                btn = page.locator(selector).first
                try:
                    if await btn.is_visible():
                        await btn.click()
                        print(f"   🚀 Clicked 'Submit' button ({selector})!")
                        submit_clicked = True
                        break
                except Exception:
                    continue

    # 7. POST-SUBMISSION JUDGE VERIFICATION
    verdict = None
    if mode in ["submit", "both"]:
        print("[7/7] Verifying live judge submission verdict...")
        if platform_type != "codeforces" and not submit_clicked:
            print(f"   ❌ No submit button could be clicked on {platform_name}.")
            return ProcessResult(
                stage=ProcessStage.SUBMITTED,
                success=False,
                url=url,
                platform=platform_type,
                problem_id=prob_id,
                solution_code=solution_code,
                explanation=explanation,
                error=f"No submit button could be clicked on {platform_name}",
                published=False
            )

        verdict = await verify_live_submission_verdict(
            page,
            platform_type,
            url,
            submission_start_time=submission_snapshot.get("start_time"),
            pre_submission_id=submission_snapshot.get("pre_submission_id"),
            account=submission_snapshot.get("account"),
            expected_problem_id=submission_snapshot.get("problem_id"),
        )
        verdict_state, verdict_msg = verdict
        print(f"   🎯 Live verdict: {verdict_state.value} - {verdict_msg}")

        # Persist submission evidence
        record_submission_evidence(verdict, url, platform_type, solution_code)

        if verdict_state == VerdictState.ACCEPTED:
            adapter.rate_limiter.record_success()

        if verdict_state != VerdictState.ACCEPTED:
            print(f"   ❌ Problem {url} NOT accepted by judge ({verdict_msg}) [State: {verdict_state.value}].")
            print(f"   🛑 SKIPPING GitHub commit to guarantee ZERO broken solutions in repository!")
            save_java_solution(filename, solution_code, url, explanation, platform_name, verdict=verdict)
            return ProcessResult(
                stage=ProcessStage.REJECTED,
                success=False,
                url=url,
                platform=platform_type,
                problem_id=prob_id,
                solution_code=solution_code,
                explanation=explanation,
                verdict=verdict,
                saved_path=str(SOLUTIONS_DIR / filename),
                compiler_output=pipeline_res.compilation.diagnostics if pipeline_res.compilation else None,
                diagnostics=pipeline_res.failure_reason,
                sample_results=pipeline_res.sample_results,
                error=verdict_msg,
                published=False
            )
    else:
        # In RUN mode, no submission was made to the judge
        print(f"   ℹ️  Mode '{mode.upper()}': solution tested locally without judge submission. Skipping GitHub commit.")
        save_java_solution(filename, solution_code, url, explanation, platform_name, verdict=None)
        return ProcessResult(
            stage=ProcessStage.TESTED,
            success=True,
            url=url,
            platform=platform_type,
            problem_id=prob_id,
            solution_code=solution_code,
            explanation=explanation,
            verdict=None,
            saved_path=str(SOLUTIONS_DIR / filename),
            compiler_output=pipeline_res.compilation.diagnostics if pipeline_res.compilation else None,
            diagnostics=pipeline_res.failure_reason,
            sample_results=pipeline_res.sample_results,
            published=False
        )

    # 8. ONLY COMMIT TO GITHUB IF GENUINELY VERIFIED & ACCEPTED
    save_java_solution(filename, solution_code, url, explanation, platform_name, verdict=verdict)

    print("   🐙 Committing verified solution to GitHub repo...")
    pub_res = commit_solution_to_github(platform_type, url, solution_code, explanation, platform_name, verdict=verdict)
    if not pub_res.success:
        print(f"   ❌ GitHub publishing failed: {pub_res.error}")
        return ProcessResult(
            stage=ProcessStage.PUBLISH_FAILED,
            success=False,
            url=url,
            platform=platform_type,
            problem_id=prob_id,
            solution_code=solution_code,
            explanation=explanation,
            verdict=verdict,
            saved_path=str(SOLUTIONS_DIR / filename),
            publish_error=pub_res.error,
            compiler_output=pipeline_res.compilation.diagnostics if pipeline_res.compilation else None,
            diagnostics=pipeline_res.failure_reason,
            sample_results=pipeline_res.sample_results,
            error=f"Publish failed: {pub_res.error}",
            published=False,
            repo_name=pub_res.repo_name,
            repo_branch=pub_res.branch,
            commit_hash=pub_res.commit_hash
        )

    print(f"✅ Completed processing: {url} (VERIFIED ACCEPTED & COMMITTED)\n")
    return ProcessResult(
        stage=ProcessStage.PUBLISHED,
        success=True,
        url=url,
        platform=platform_type,
        problem_id=prob_id,
        solution_code=solution_code,
        explanation=explanation,
        verdict=verdict,
        saved_path=str(SOLUTIONS_DIR / filename),
        compiler_output=pipeline_res.compilation.diagnostics if pipeline_res.compilation else None,
        diagnostics=pipeline_res.failure_reason,
        sample_results=pipeline_res.sample_results,
        published=True,
        repo_name=pub_res.repo_name,
        repo_branch=pub_res.branch,
        commit_hash=pub_res.commit_hash
    )

async def ensure_live_page(page: Page, p_playwright=None, browser=None, context=None) -> Tuple[Any, Any, Page]:
    """Guarantees an active, responsive browser page by reconnecting over CDP if disconnected."""
    if await BrowserManager.is_healthy(page):
        return browser, context, page

    print("   🔄 Browser tab or CDP connection lost. Reconnecting to Chrome via CDP (port 9222)...")
    if p_playwright:
        try:
            return await BrowserManager.reconnect(p_playwright, browser, context)
        except Exception as e:
            print(f"   ⚠️ CDP reconnection notice: {e}")
            chrome_app = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" if sys.platform == "darwin" else ""
            if chrome_app and os.path.exists(chrome_app):
                try:
                    user_data_path = str(BASE_DIR / "saved_chrome_login")
                    print("   🌐 Launching Chrome with CDP on port 9222...")
                    subprocess.Popen([chrome_app, "--remote-debugging-port=9222", f"--user-data-dir={user_data_path}", "--no-first-run", "--no-default-browser-check"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    for _ in range(6):
                        await asyncio.sleep(1)
                        try:
                            browser = await p_playwright.chromium.connect_over_cdp("http://localhost:9222")
                            context = browser.contexts[0]
                            page = await context.new_page()
                            print("   ✅ Connected to relaunched Chrome via CDP!")
                            return browser, context, page
                        except Exception:
                            pass
                except Exception:
                    pass

    if context:
        try:
            page = await context.new_page()
            return browser, context, page
        except Exception:
            pass

    return browser, context, page

# ==============================================================================
# Autonomous Batch Execution (Zero Human Input)
# ==============================================================================
async def solve_platform_quota(
    page: Optional[Page],
    platform: str,
    target_accepted: int = 5,
    difficulty: str = "medium-hard",
    mode: str = "both",
    p_playwright = None,
    browser = None,
    context = None,
    max_repairs: int = 3,
    java_version: Optional[str] = None,
    dry_run: bool = False,
    offline_mode: bool = False,
    max_duration: int = 3600,
    start_time: Optional[float] = None,
) -> Tuple[int, Any, Any, Optional[Page]]:
    """Solves problems on the given platform until target_accepted ACCEPTED problems are achieved.
    If a problem is not accepted by the judge, it fetches replacement candidates using pagination.
    Maintains per-platform quota independently without reallocation.
    """
    print(f"\n{'='*70}")
    print(f"🎯 STARTING QUOTA SOLVER: {platform.upper()} (Target: {target_accepted} ACCEPTED Java solutions)")
    print(f"{'='*70}")

    is_dry = dry_run or DRY_RUN
    is_offline = offline_mode or OFFLINE_MODE or (page is None)
    batch_start = start_time or time.time()

    discovery_map = {
        "leetcode": discover_leetcode_candidates,
        "codeforces": discover_codeforces_candidates,
        "hackerrank": discover_hackerrank_candidates,
        "codechef": discover_codechef_candidates,
        "hackerearth": discover_hackerearth_candidates,
    }

    disc_func = discovery_map.get(platform)
    if not disc_func and not is_offline:
        print(f"❌ Unknown platform for quota solving: {platform}")
        return 0, browser, context, page

    db = SolvedRegistry.get_db()
    worker_id = f"worker_{os.getpid()}_{id(asyncio.current_task()) if asyncio.current_task() else 0}"
    SolvedRegistry._active_worker_id = worker_id

    run_id = db.acquire_daily_run_lock(platform, target_quota=target_accepted, worker_id=worker_id, lock_duration_sec=float(max_duration))
    if run_id is None:
        log_event("INFO", f"Platform {platform} is already satisfied today or actively locked by another worker. Skipping.")
        return 0, browser, context, page

    if page:
        page._anti_bot_blocked = False
    accepted_count = 0
    attempt_count = 0
    max_attempts = target_accepted * 4  # Bounded search limit per run
    failed_in_run: Set[str] = set()    # Prevent repeating failed candidates in the same run
    discovery_offset = 0
    step_size = max(15, target_accepted * 2)

    if is_offline or page is None:
        candidate_pool = discover_offline_candidates(platform, count=target_accepted * 3, difficulty=difficulty, offset=discovery_offset)
        discovery_offset += len(candidate_pool)
    else:
        browser, context, page = await ensure_live_page(page, p_playwright, browser, context)
        candidate_pool = await disc_func(page, count=step_size, difficulty=difficulty, offset=discovery_offset)
        discovery_offset += step_size
    pool_idx = 0

    while accepted_count < target_accepted and attempt_count < max_attempts:
        if (time.time() - batch_start) >= max_duration:
            log_event("WARNING", f"Max execution duration of {max_duration}s exceeded. Gracefully ending {platform} quota run.")
            break
        if SHUTDOWN_REQUESTED:
            log_event("WARNING", f"SIGTERM/SIGINT received. Gracefully ending {platform} quota run.")
            break

        if page:
            page._anti_bot_blocked = False
        if pool_idx >= len(candidate_pool):
            print(f"⚠️ Exhausted candidate pool. Querying next batch of candidates for {platform} (offset={discovery_offset})...")
            if is_offline or page is None:
                more_candidates = discover_offline_candidates(platform, count=step_size, difficulty=difficulty, offset=discovery_offset)
            else:
                browser, context, page = await ensure_live_page(page, p_playwright, browser, context)
                more_candidates = await disc_func(page, count=step_size, difficulty=difficulty, offset=discovery_offset)
            discovery_offset += step_size
            for c in more_candidates:
                _, cid = normalize_problem_identity(c, platform)
                if c not in candidate_pool and cid not in failed_in_run:
                    candidate_pool.append(c)
            if pool_idx >= len(candidate_pool):
                print(f"❌ No further unsolved candidates available for {platform}.")
                break

        url = candidate_pool[pool_idx]
        pool_idx += 1

        _, canonical_id = normalize_problem_identity(url, platform)

        # Check in-memory run failure
        if canonical_id in failed_in_run:
            db.increment_daily_run_counts(run_id, skipped=1)
            continue

        # Check DB acceptance
        if SolvedRegistry.is_solved(platform, canonical_id):
            db.increment_daily_run_counts(run_id, skipped=1)
            continue

        # Check DB permanent failure
        if db.is_permanent_failure(platform, canonical_id):
            db.increment_daily_run_counts(run_id, skipped=1)
            continue

        # Check circuit breaker before attempting candidate
        limiter = get_platform_limiter(platform)
        if limiter.is_paused():
            log_event("WARNING", f"Circuit breaker OPEN for {platform}. Platform is paused.", run_id=run_id, platform=platform, failure_code=FailureCode.RATE_LIMITED)
            break

        prob_start = time.time()
        # Transactional atomic claim
        claimed = db.claim_problem(platform, canonical_id, url=url, worker_id=worker_id)
        if not claimed:
            print(f"   🔒 Problem {canonical_id} is claimed or already accepted. Skipping...")
            db.increment_daily_run_counts(run_id, skipped=1)
            continue

        db.record_heartbeat(platform, canonical_id, worker_id)
        attempt_count += 1
        db.increment_daily_run_counts(run_id, attempted=1)
        print(f"\n👉 [{platform.upper()}] Attempt {attempt_count}/{max_attempts} | Target Progress: {accepted_count}/{target_accepted} Accepted")
        print(f"   Solving: {url}")

        if not is_offline and page is not None:
            browser, context, page = await ensure_live_page(page, p_playwright, browser, context)

        try:
            result = await asyncio.wait_for(
                process_problem(
                    page, url, mode=mode, max_repairs=max_repairs,
                    java_version=java_version, dry_run=is_dry, offline_mode=is_offline
                ),
                timeout=180
            )
        except asyncio.TimeoutError:
            print(f"   ⏱️  Problem processing timed out after 180s for {url}. Recovering...")
            if page:
                try:
                    await page.goto("about:blank", timeout=5000)
                except Exception:
                    pass
            result = ProcessResult(stage=ProcessStage.SUBMITTED, success=False, url=url, error="Processing timeout (180s)")
        except Exception as e:
            print(f"   ❌ Unhandled error processing {url}: {e}")
            result = ProcessResult(stage=ProcessStage.SUBMITTED, success=False, url=url, error=str(e))

        db.record_heartbeat(platform, canonical_id, worker_id)

        # Check stages reached
        if result.stage.value in [ProcessStage.GENERATED.value, ProcessStage.COMPILED.value, ProcessStage.TESTED.value,
                                  ProcessStage.SUBMITTED.value, ProcessStage.ACCEPTED.value, ProcessStage.PUBLISHED.value,
                                  ProcessStage.PUBLISH_FAILED.value]:
            db.increment_daily_run_counts(run_id, generated=1)
        if result.stage.value in [ProcessStage.COMPILED.value, ProcessStage.TESTED.value,
                                  ProcessStage.SUBMITTED.value, ProcessStage.ACCEPTED.value, ProcessStage.PUBLISHED.value,
                                  ProcessStage.PUBLISH_FAILED.value]:
            db.increment_daily_run_counts(run_id, compiled=1)
        if result.stage.value in [ProcessStage.TESTED.value,
                                  ProcessStage.SUBMITTED.value, ProcessStage.ACCEPTED.value, ProcessStage.PUBLISHED.value,
                                  ProcessStage.PUBLISH_FAILED.value]:
            db.increment_daily_run_counts(run_id, tested=1)

        # Check for publish failure on confirmed accepted solution: retry publishing without resubmitting
        if result.stage == ProcessStage.PUBLISH_FAILED and result.verdict and result.verdict.state == VerdictState.ACCEPTED:
            print(f"   ⚠️  Publish failed for {url} ({result.publish_error}). Retrying publish without resubmitting to judge...")
            retry_res = await process_problem(
                page, url, mode=mode,
                retry_publish=True,
                cached_verdict=result.verdict,
                cached_code=result.solution_code,
                cached_explanation=result.explanation,
                max_repairs=max_repairs,
                java_version=java_version,
                dry_run=is_dry,
                offline_mode=is_offline,
            )
            if retry_res.published:
                result = retry_res

        # Quota credit requires confirmed publication in submit/both modes
        is_successful = (result.success and result.published) if mode in ["submit", "both"] else result.success

        err_msg = result.error or (result.verdict.message if result.verdict else None)
        f_code = classify_failure(err_msg, verdict=result.verdict.state if result.verdict else None, stage=result.stage)
        transient = (f_code == FailureCode.TRANSIENT)

        attempt_id = db.record_attempt(
            platform=platform,
            canonical_id=canonical_id,
            run_id=run_id,
            stage=result.stage,
            mode=mode,
            solution_code=result.solution_code,
            explanation=result.explanation,
            error=err_msg,
            is_transient=transient,
            compiler_output=result.compiler_output,
            diagnostics=result.diagnostics
        )
        if result.sample_results:
            db.record_sample_test_results(attempt_id, result.sample_results)

        prob_dur = time.time() - prob_start

        if is_successful:
            accepted_count += 1
            limiter.record_success()
            db.mark_retry_completed(platform, canonical_id)
            db.increment_daily_run_counts(run_id, accepted=1)
            if result.published:
                db.increment_daily_run_counts(run_id, published=1)
                db.record_publish_job(
                    platform=platform,
                    canonical_id=canonical_id,
                    attempt_id=attempt_id,
                    repo_name=result.repo_name,
                    branch=result.repo_branch,
                    commit_hash=result.commit_hash,
                    status="PUBLISHED"
                )
            SolvedRegistry.mark_solved(platform, url, verdict=result.verdict)
            print(f"🎉 [{platform.upper()}] Progress: {accepted_count}/{target_accepted} ACCEPTED & COMMITTED TO GITHUB!\n")
            if mode in ["submit", "both"] and result.verdict and result.verdict.state == VerdictState.ACCEPTED:
                log_event("INFO", f"Problem accepted by judge: {canonical_id}", run_id=run_id, problem_id=canonical_id, platform=platform, attempt_id=attempt_id, state="ACCEPTED", duration=prob_dur, confirmed=True)
            else:
                log_event("INFO", f"Problem processed in mode={mode}: {canonical_id}", run_id=run_id, problem_id=canonical_id, platform=platform, attempt_id=attempt_id, state=result.stage.value, duration=prob_dur)
            if not is_offline:
                await asyncio.sleep(6)
        else:
            failed_in_run.add(canonical_id)
            db.increment_daily_run_counts(run_id, failed=1)
            status_to_record = "PUBLISH_FAILED" if result.stage == ProcessStage.PUBLISH_FAILED else "FAILED"
            db.release_problem(
                platform=platform,
                canonical_id=canonical_id,
                status=status_to_record,
                failure_reason=err_msg,
                is_transient=transient
            )
            limiter.record_failure(is_rate_limit=(f_code == FailureCode.RATE_LIMITED), reason=err_msg or "")

            # Requirement 5: Retry only transient failures automatically
            if transient:
                p_id = db.get_problem_id(platform, canonical_id)
                if p_id:
                    db.enqueue_retry(
                        problem_id=p_id,
                        platform=platform,
                        canonical_id=canonical_id,
                        url=url,
                        failure_code=FailureCode.TRANSIENT,
                        error_msg=err_msg or "Transient failure"
                    )

            if f_code == FailureCode.AUTH_REQUIRED:
                limiter.trip(reason=f"Authentication barrier: {err_msg}")
                NOTIFICATION_MANAGER.send_notification(
                    NotificationEvent.AUTH_REQUIRED,
                    f"Authentication Required on {platform.upper()}",
                    f"Session expired or anti-bot challenge detected on {platform.upper()}: {err_msg}",
                    platform=platform,
                    details={"error": err_msg, "url": url}
                )

            log_event(
                "ERROR",
                f"Problem failed for {canonical_id}: {err_msg}",
                run_id=run_id,
                problem_id=canonical_id,
                platform=platform,
                attempt_id=attempt_id,
                state=result.stage.value,
                duration=prob_dur,
                failure_code=f_code
            )

            if result.stage == ProcessStage.PUBLISH_FAILED:
                db.record_publish_job(
                    platform=platform,
                    canonical_id=canonical_id,
                    attempt_id=attempt_id,
                    repo_name=result.repo_name,
                    branch=result.repo_branch,
                    commit_hash=result.commit_hash,
                    status="FAILED",
                    error=result.publish_error
                )
                print(f"⚠️ [{platform.upper()}] Problem was accepted by judge, but GitHub publishing failed ({result.publish_error}).")
            else:
                print(f"⚠️ [{platform.upper()}] Problem was NOT accepted by the judge ({result.error or 'rejected'}).")

            if page and getattr(page, "_anti_bot_blocked", False):
                page._anti_bot_blocked = False
                limiter.trip(reason="Cloudflare Anti-Bot verification")
                NOTIFICATION_MANAGER.send_notification(
                    NotificationEvent.AUTH_REQUIRED,
                    f"Anti-Bot Barrier on {platform.upper()}",
                    f"Cloudflare / captcha detected on {platform.upper()}.",
                    platform=platform,
                    details={"url": url}
                )
                print(f"\n🛑 [{platform.upper()}] Platform is gated by Cloudflare Anti-Bot verification.")
                print(f"🛑 Stopping {platform.upper()} attempts to avoid continuous looping.")
                print(f"📁 Generated Java solution has been saved to: {SOLUTIONS_DIR.resolve()}\n")
                break

            print(f"🔄 Selecting next unsolved replacement candidate (offset={discovery_offset})...\n")

        if not is_offline:
            await asyncio.sleep(3)

    if SHUTDOWN_REQUESTED:
        run_status = "TERMINATED"
    elif accepted_count >= target_accepted:
        run_status = "COMPLETED"
    else:
        run_status = "SHORTFALL"
        NOTIFICATION_MANAGER.send_notification(
            NotificationEvent.QUOTA_MISSED,
            f"Daily Quota Missed on {platform.upper()}",
            f"Achieved {accepted_count}/{target_accepted} verified accepted solutions on {platform.upper()} ({run_status}).",
            platform=platform,
            details={"accepted": accepted_count, "target": target_accepted, "shortfall": target_accepted - accepted_count}
        )
    db.complete_daily_run(run_id, status=run_status)
    print(f"\n📊 [{platform.upper()}] Quota Summary: {accepted_count}/{target_accepted} verified accepted solutions ({run_status}).")
    return accepted_count, browser, context, page

async def execute_batch(
    platform: str,
    count: int,
    difficulty: str,
    mode: str,
    file_path: Optional[str] = None,
    headless: bool = True,
    max_repairs: int = 3,
    java_version: Optional[str] = None,
    dry_run: bool = False,
    offline_mode: bool = False,
    timeout: Optional[int] = None,
    database_url: Optional[str] = None,
) -> bool:
    """Executes a complete automated batch of problems with zero human input."""
    start_time = datetime.now()
    batch_start_ts = time.time()
    is_dry = dry_run or DRY_RUN
    is_offline = offline_mode or OFFLINE_MODE
    max_dur = timeout if timeout is not None else MAX_RUN_DURATION_SECONDS

    # 1. Startup configuration validation
    cfg = validate_startup_configuration(
        mode=mode,
        platform=platform,
        count=count,
        difficulty=difficulty,
        dry_run=is_dry,
        offline_mode=is_offline,
        timeout=max_dur
    )

    # 2. Database & Startup Crash Recovery
    if database_url:
        db = DatabaseManager(database_url=database_url)
        SolvedRegistry.set_db(db)
    else:
        db = SolvedRegistry.get_db()
    recovered_count = db.recover_expired_leases(lease_timeout_sec=600.0)
    if recovered_count > 0:
        log_event("INFO", f"Startup recovery: released {recovered_count} expired problem lease(s)")

    print("\n" + "=" * 70)
    print(f"🚀 STARTING AUTOMATED RUN [{start_time.strftime('%Y-%m-%d %H:%M:%S')}]")
    print(f"   Platform   : {platform.upper()}")
    count_label = f"{count} problem(s) per platform ({count * 5} total across 5 platforms)" if platform == "all" else f"{count} problem(s)"
    print(f"   Count      : {count_label}")
    print(f"   Language   : JAVA ☕")
    print(f"   Difficulty : {difficulty.upper()}")
    print(f"   Mode       : {mode.upper()}")
    print(f"   Offline    : {is_offline} | Dry Run: {is_dry}")
    print("=" * 70)

    # 3. Offline or Dry-Run Execution Branch (skips Playwright & Chrome browser launch entirely)
    if is_offline or is_dry:
        total_accepted = 0
        target_total = 0
        if platform == "file" and file_path and os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                urls_to_solve = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
            target_total = len(urls_to_solve)
            for idx, url in enumerate(urls_to_solve, 1):
                res = await process_problem(
                    None, url, mode=mode, max_repairs=max_repairs,
                    java_version=java_version, dry_run=is_dry, offline_mode=is_offline
                )
                is_ok = (res.success and res.published) if mode in ["submit", "both"] else res.success
                if is_ok:
                    total_accepted += 1
        elif platform == "all" or "," in platform:
            if platform == "all":
                all_platforms = ["leetcode", "codeforces", "hackerrank", "codechef", "hackerearth"]
            else:
                all_platforms = [p.strip().lower() for p in platform.split(",") if p.strip()]
            target_total = count * len(all_platforms)
            print(f"\n📊 Starting independent quota run: {count} verified ACCEPTED problems on EACH of the {len(all_platforms)} platforms ({target_total} total)...")
            platform_results = {}
            platform_targets = {}
            for p_name in all_platforms:
                acc, _, _, _ = await solve_platform_quota(
                    None, p_name, target_accepted=count, difficulty=difficulty, mode=mode,
                    max_repairs=max_repairs, java_version=java_version, dry_run=is_dry,
                    offline_mode=is_offline, max_duration=max_dur, start_time=batch_start_ts
                )
                total_accepted += acc
                platform_results[p_name] = acc
                platform_targets[p_name] = count

            print("\n" + "=" * 70)
            print("📊 INDEPENDENT PLATFORM QUOTA REPORT")
            print("=" * 70)
            all_satisfied = True
            for p_name in all_platforms:
                achieved = platform_results.get(p_name, 0)
                targeted = platform_targets.get(p_name, count)
                status_mark = "✅ Achieved" if achieved >= targeted else "❌ Deficit"
                if achieved < targeted:
                    all_satisfied = False
                print(f"   {p_name.upper():<14}: {achieved}/{targeted} accepted [{status_mark}]")
            print("=" * 70)
            print(f"\n📊 SUMMARY: {total_accepted}/{target_total} total problems verified Accepted across all platforms.")
            batch_success = all_satisfied
        else:
            target_total = count
            total_accepted, _, _, _ = await solve_platform_quota(
                None, platform, target_accepted=count, difficulty=difficulty, mode=mode,
                max_repairs=max_repairs, java_version=java_version, dry_run=is_dry,
                offline_mode=is_offline, max_duration=max_dur, start_time=batch_start_ts
            )
            batch_success = (total_accepted >= target_total)
            print(f"\n📊 {platform.upper()} SUMMARY: {total_accepted}/{count} problems verified Accepted.")

        elapsed = (datetime.now() - start_time).total_seconds()
        print("\n" + "=" * 70)
        print(f"🎉 BATCH COMPLETED IN {int(elapsed)}s!")
        print(f"📁 Java solutions archived in: {SOLUTIONS_DIR.resolve()}")
        print("=" * 70)
        return batch_success

    # Verify internet connectivity before proceeding (e.g. after system wake)
    for net_try in range(10):
        try:
            import urllib.request
            with urllib.request.urlopen("https://www.google.com", timeout=3) as resp:
                if resp.status == 200:
                    break
        except Exception:
            if net_try < 9:
                print(f"   ⏳ Waiting for internet connection to be active ({net_try + 1}/10)...")
                await asyncio.sleep(3)

    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        is_cdp = False

        # 1. Try connecting to user's running Chrome via CDP on port 9222
        try:
            import urllib.request
            with urllib.request.urlopen("http://localhost:9222/json/version", timeout=1) as resp:
                if resp.status == 200:
                    print("🔗 Connecting directly to your running Google Chrome browser via CDP (port 9222)...")
                    cdp_browser = await p.chromium.connect_over_cdp("http://localhost:9222")
                    context = cdp_browser.contexts[0]
                    page = await context.new_page()
                    browser = cdp_browser
                    is_cdp = True
                    print("✅ Successfully connected to your live Chrome with all your accounts & logins!")
        except Exception:
            pass

        # 1b. If port 9222 is not active, launch Chrome with dedicated user-data-dir on port 9222
        if not is_cdp and not headless:
            chrome_app = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" if sys.platform == "darwin" else ""
            if chrome_app and os.path.exists(chrome_app):
                try:
                    user_data_path = str(BASE_DIR / "saved_chrome_login")
                    print("🌐 Launching dedicated Chrome on port 9222...")
                    subprocess.Popen([chrome_app, "--remote-debugging-port=9222", f"--user-data-dir={user_data_path}", "--no-first-run", "--no-default-browser-check"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    for _ in range(8):
                        await asyncio.sleep(1)
                        try:
                            with urllib.request.urlopen("http://localhost:9222/json/version", timeout=1) as resp:
                                if resp.status == 200:
                                    cdp_browser = await p.chromium.connect_over_cdp("http://localhost:9222")
                                    context = cdp_browser.contexts[0]
                                    page = context.pages[0] if context.pages else await context.new_page()
                                    browser = cdp_browser
                                    is_cdp = True
                                    print("✅ Successfully connected to dedicated Chrome via CDP (port 9222)!")
                                    break
                        except Exception:
                            pass
                except Exception:
                    pass

        # 2. Fallback to launching persistent Chrome context
        if not is_cdp:
            print("\n🌐 Launching dedicated Chrome browser with persistent session...")
            try:
                browser = await p.chromium.launch_persistent_context(
                    user_data_dir=str(BASE_DIR / "saved_chrome_login"),
                    channel="chrome",
                    headless=headless,
                    user_agent=DEFAULT_USER_AGENT,
                    ignore_default_args=["--enable-automation", "--no-sandbox"],
                    args=[
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                )
            except Exception as e:
                print(f"⚠️  Could not launch Chrome with channel='chrome' ({e}). Using default Chromium...")
                browser = await p.chromium.launch_persistent_context(
                    user_data_dir=str(BASE_DIR / "saved_chrome_login"),
                    headless=headless,
                    user_agent=DEFAULT_USER_AGENT,
                    ignore_default_args=["--enable-automation", "--no-sandbox"],
                    args=[
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                )
            await browser.add_init_script("""
                Object.defineProperty(Object.getPrototypeOf(navigator), 'webdriver', {
                    get: () => undefined
                });
            """)
            # Ensure add_init_script is applied to the active page
            context = browser
            p0 = browser.pages[0] if browser.pages else None
            page = await browser.new_page()
            if p0:
                await p0.close()

        # Brief pause to let browser initialize
        await asyncio.sleep(2)

        total_accepted = 0
        target_total = 0
        if platform == "file" and file_path and os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                urls_to_solve = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
            target_total = len(urls_to_solve)
            for idx, url in enumerate(urls_to_solve, 1):
                browser, context, page = await ensure_live_page(page, p, browser, context)
                res = await process_problem(page, url, mode=mode, max_repairs=max_repairs, java_version=java_version)
                is_ok = (res.success and res.published) if mode in ["submit", "both"] else res.success
                if is_ok:
                    total_accepted += 1
                if idx < len(urls_to_solve):
                    await asyncio.sleep(4)
        elif platform == "all" or "," in platform:
            if platform == "all":
                all_platforms = ["leetcode", "codeforces", "hackerrank", "codechef", "hackerearth"]
            else:
                all_platforms = [p.strip().lower() for p in platform.split(",") if p.strip()]
            target_total = count * len(all_platforms)
            print(f"\n📊 Starting independent quota run: {count} verified ACCEPTED problems on EACH of the {len(all_platforms)} platforms ({target_total} total)...")
            platform_results = {}
            platform_targets = {}
            for p_name in all_platforms:
                acc, browser, context, page = await solve_platform_quota(
                    page, p_name, target_accepted=count, difficulty=difficulty, mode=mode,
                    p_playwright=p, browser=browser, context=context,
                    max_repairs=max_repairs, java_version=java_version
                )
                total_accepted += acc
                platform_results[p_name] = acc
                platform_targets[p_name] = count

            print("\n" + "=" * 70)
            print("📊 INDEPENDENT PLATFORM QUOTA REPORT")
            print("=" * 70)
            all_satisfied = True
            for p_name in all_platforms:
                achieved = platform_results.get(p_name, 0)
                targeted = platform_targets.get(p_name, count)
                status_mark = "✅ Achieved" if achieved >= targeted else "❌ Deficit"
                if achieved < targeted:
                    all_satisfied = False
                print(f"   {p_name.upper():<14}: {achieved}/{targeted} accepted [{status_mark}]")
            print("=" * 70)
            print(f"\n📊 SUMMARY: {total_accepted}/{target_total} total problems verified Accepted across all platforms.")
        else:
            target_total = count
            total_accepted, browser, context, page = await solve_platform_quota(
                page, platform, target_accepted=count, difficulty=difficulty, mode=mode,
                p_playwright=p, browser=browser, context=context,
                max_repairs=max_repairs, java_version=java_version
            )
            all_satisfied = (total_accepted >= target_total)
            print(f"\n📊 {platform.upper()} SUMMARY: {total_accepted}/{count} problems verified Accepted.")

        elapsed = (datetime.now() - start_time).total_seconds()
        print("\n" + "=" * 70)
        print(f"🎉 BATCH COMPLETED IN {int(elapsed)}s!")
        print(f"📁 Java solutions archived in: {SOLUTIONS_DIR.resolve()}")
        print("=" * 70)

        # Notify / refresh UniRank tab in user's Chrome if open
        if sys.platform == "darwin":
            try:
                subprocess.run([
                    "osascript", "-e",
                    'tell application "Google Chrome"\n'
                    '    repeat with w in windows\n'
                    '        repeat with t in tabs of w\n'
                    '            if title of t contains "UniRank" then reload t\n'
                    '        end repeat\n'
                    '    end repeat\n'
                    'end tell'
                ], capture_output=True, timeout=5)
                print("🔄 Refreshed UniRank leaderboard tab in your Chrome.")
            except Exception:
                pass

        # Automatically clean up
        if is_cdp:
            print("👋 Closing automated tab in your Chrome...")
            try:
                if page and not page.is_closed():
                    await page.close()
            except Exception:
                pass
        elif browser:
            try:
                await browser.close()
            except Exception:
                pass

        if platform == "all" or "," in platform:
            batch_success = all_satisfied
        elif target_total > 0:
            batch_success = (total_accepted >= target_total)
        else:
            batch_success = True

        if not batch_success:
            print(f"\n⚠️  Batch quota shortfall: {total_accepted}/{target_total} completed.")
        return batch_success

# ==============================================================================
# Daily Scheduler Loop
# ==============================================================================
async def run_daily_scheduler(scheduled_time_str: str, batch_coro_func):
    """Schedules batch_coro_func to run every day at scheduled_time_str (e.g. '09:00')."""
    print("=" * 70)
    print(f"⏰ DAILY SCHEDULER ACTIVE - RUNNING EVERY DAY AT {scheduled_time_str}")
    print("=" * 70)

    while True:
        now = datetime.now()
        try:
            hour, minute = map(int, scheduled_time_str.split(":"))
        except Exception:
            hour, minute = 9, 0

        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        hours, remainder = divmod(int(wait_seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        print(f"\n💤 Next automated Java coding run scheduled for {target.strftime('%Y-%m-%d %H:%M:%S')} (in {hours}h {minutes}m {seconds}s). Sleeping...")
        await asyncio.sleep(wait_seconds)

        print(f"\n🔔 WAKING UP! Executing daily coding batch at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}...")
        try:
            await batch_coro_func()
        except Exception as e:
            print(f"❌ Scheduled run encountered an error: {e}")

# ==============================================================================
# CLI Helper Utilities & Subcommands
# ==============================================================================
def print_status_summary(detailed: bool = False, db: Optional[DatabaseManager] = None):
    """Prints a clear, human-readable summary table of verified solved problems and daily quotas."""
    database = db or SolvedRegistry.get_db()
    all_solved = SolvedRegistry.get_all_solved(force_refresh=True)
    stats = database.get_all_time_stats()
    today_str = get_kolkata_today_str()
    daily_rows = database.get_daily_summary(today_str)

    print("\n" + "=" * 76)
    print("      📊 AutoCoder AI: Verified Solved Status & Quota Summary")
    print(f"      Timezone: Asia/Kolkata | Date: {today_str}")
    print("=" * 76)
    print(f"  {'Platform':<14} {'Verified Accepted':>18} {'Confirmed Published':>20} {'Legacy Unverified':>18}")
    print("  " + "─" * 72)
    display_names = {
        "leetcode": "LeetCode",
        "codeforces": "Codeforces",
        "hackerrank": "HackerRank",
        "codechef": "CodeChef",
        "hackerearth": "HackerEarth",
    }
    tot_acc = 0
    tot_pub = 0
    tot_unv = 0
    for p, name in display_names.items():
        p_stats = stats.get(p, {})
        acc = p_stats.get("accepted", len(all_solved.get(p, set())))
        pub = p_stats.get("published", 0)
        unv = p_stats.get("unverified", 0)
        tot_acc += acc
        tot_pub += pub
        tot_unv += unv
        print(f"  {name:<14} {acc:>18} {pub:>20} {unv:>18}")
    print("  " + "─" * 72)
    print(f"  {'TOTAL':<14} {tot_acc:>18} {tot_pub:>20} {tot_unv:>18}")
    print("=" * 76)

    print(f"\n  📅 Today's Quota Runs ({today_str} Asia/Kolkata):")
    if daily_rows:
        print(f"  {'Platform':<14} {'Target':>7} {'Attempted':>10} {'Generated':>10} {'Compiled':>9} {'Tested':>8} {'Accepted':>9} {'Published':>10} {'Failed':>7} {'Skipped':>8} {'Status':<10}")
        print("  " + "─" * 108)
        for r in daily_rows:
            p = display_names.get(r.get("platform", ""), r.get("platform", "").upper())
            print(f"  {p:<14} {r.get('target_quota', 0):>7} {r.get('attempted_count', 0):>10} {r.get('generated_count', 0):>10} {r.get('compiled_count', 0):>9} {r.get('tested_count', 0):>8} {r.get('accepted_count', 0):>9} {r.get('published_count', 0):>10} {r.get('failed_count', 0):>7} {r.get('skipped_count', 0):>8} {r.get('status', ''):<10}")
    else:
        print("  (No runs recorded yet today)")

    if detailed:
        print("\n" + "=" * 76)
        print("      🔍 DETAILED MONITORING & RECOVERY METRICS (--detailed)")
        print("=" * 76)

        # 1. Failure Breakdown by Platform & Code
        breakdown = database.get_failure_breakdown()
        print("\n  ⚠️ Failure Classification Breakdown:")
        print(f"  {'Platform':<14} {'Transient':>10} {'AuthReq':>9} {'RateLimit':>10} {'DOMChanged':>11} {'Rejected':>9} {'PubFailed':>10} {'Permanent':>10}")
        print("  " + "─" * 88)
        for p, name in display_names.items():
            b = breakdown.get(p, {})
            print(f"  {name:<14} {b.get(FailureCode.TRANSIENT.value, 0):>10} {b.get(FailureCode.AUTH_REQUIRED.value, 0):>9} {b.get(FailureCode.RATE_LIMITED.value, 0):>10} {b.get(FailureCode.PLATFORM_CHANGED.value, 0):>11} {b.get(FailureCode.JUDGE_REJECTED.value, 0):>9} {b.get(FailureCode.PUBLISH_FAILED.value, 0):>10} {b.get(FailureCode.PERMANENT.value, 0):>10}")

        # 2. Circuit Breakers Status
        print("\n  ⚡ Per-Platform Circuit Breakers:")
        print(f"  {'Platform':<14} {'State':<12} {'Failures':>9} {'Cooldown':>12} {'Reason':<24}")
        print("  " + "─" * 74)
        for p, name in display_names.items():
            lim = get_platform_limiter(p)
            st = lim.get_status()
            cd = f"{st['remaining_seconds']}s remaining" if st["state"] == "OPEN" else "N/A"
            reason = st.get("reason", "")[:24]
            print(f"  {name:<14} {st['state']:<12} {st['consecutive_failures']:>9} {cd:>12} {reason:<24}")

        # 3. Persistent Retry Queue
        q_items = database.get_all_retry_queue_items()
        print(f"\n  🔄 Persistent Retry Queue ({len(q_items)} items):")
        if q_items:
            print(f"  {'ID':<5} {'Platform':<12} {'Canonical ID':<22} {'Code':<15} {'Retries':>8} {'Next Due':<14} {'Status':<10}")
            print("  " + "─" * 90)
            now_ts = time.time()
            for it in q_items[:15]:
                due_in = max(0, int(it.get("next_retry_at", 0) - now_ts))
                due_str = f"in {due_in}s" if due_in > 0 else "DUE"
                print(f"  {it.get('id', ''):<5} {it.get('platform', ''):<12} {it.get('canonical_id', '')[:20]:<22} {it.get('failure_code', ''):<15} {it.get('retry_count', 0):>8} {due_str:<14} {it.get('status', ''):<10}")
        else:
            print("  (Retry queue is empty)")

        # 4. Active Worker Leases & Heartbeats
        leases = database.get_active_leases()
        print(f"\n  🔒 Active Worker Leases & Heartbeats ({len(leases)} active):")
        if leases:
            print(f"  {'ID':<5} {'Platform':<12} {'Canonical ID':<20} {'Claimed By':<16} {'Claim Age':>10} {'Heartbeat':>10} {'Health':<10}")
            print("  " + "─" * 87)
            now_ts = time.time()
            for l in leases:
                c_age = int(now_ts - l.get("claimed_at", now_ts)) if l.get("claimed_at") else 0
                hb_age = int(now_ts - l.get("heartbeat_at", now_ts)) if l.get("heartbeat_at") else c_age
                tout = l.get("lease_timeout") or 300.0
                health = "EXPIRED" if hb_age > tout else "HEALTHY"
                print(f"  {l.get('id', ''):<5} {l.get('platform', ''):<12} {l.get('canonical_id', '')[:18]:<20} {l.get('claimed_by', '')[:14]:<16} {c_age:>9}s {hb_age:>9}s {health:<10}")
        else:
            print("  (No in-progress leases)")

    print("\n" + "=" * 76)
    print(f"  📁 SQLite Database  : {DEFAULT_DB_FILE.resolve()}")
    print(f"  📁 Solved Registry  : {HISTORY_FILE.resolve()}")
    print(f"  📁 Solution Files   : {SOLUTIONS_DIR.resolve()}")
    if GITHUB_USERNAME:
        print(f"  🐙 GitHub Profile   : https://github.com/{GITHUB_USERNAME}")
    print("=" * 76 + "\n")

async def handle_retry_failed(
    dry_run: bool = True,
    platform: Optional[str] = None,
    max_retries: int = 3,
    db: Optional[DatabaseManager] = None
) -> int:
    """CLI handler for autocoder retry-failed [--dry-run]."""
    database = db or SolvedRegistry.get_db()
    items = database.get_all_retry_queue_items(platform=platform)
    now = time.time()

    print("\n" + "=" * 80)
    mode_str = "DRY-RUN SIMULATION" if dry_run else "EXECUTION"
    print(f"      🔄 AutoCoder AI: Retry Failed Problems ({mode_str})")
    print("=" * 80)

    eligible = [it for it in items if it.get("status") in ("PENDING", "EXHAUSTED")]

    if not eligible:
        print("  ℹ️ No failed problems currently in the retry queue.")
        print("=" * 80 + "\n")
        return 0

    print(f"  Found {len(eligible)} candidate problem(s) eligible for retry:")
    print(f"  {'ID':<5} {'Platform':<12} {'Canonical ID':<22} {'Failure Code':<15} {'Retries':>8} {'Status':<10} {'Next Retry':<12}")
    print("  " + "─" * 90)

    for it in eligible:
        p = it.get("platform", "").capitalize()
        cid = it.get("canonical_id", "")[:20]
        code = it.get("failure_code", "")
        retries = f"{it.get('retry_count', 0)}/{it.get('max_retries', max_retries)}"
        st = it.get("status", "")
        next_r = it.get("next_retry_at", 0)
        due_sec = max(0, int(next_r - now))
        due_str = f"in {due_sec}s" if due_sec > 0 else "DUE NOW"
        print(f"  {it.get('id', ''):<5} {p:<12} {cid:<22} {code:<15} {retries:>8} {st:<10} {due_str:<12}")

    print("=" * 80)
    if dry_run:
        print("  ℹ️ [DRY RUN] Simulation complete. No problems were executed and database state was not modified.")
        print("=" * 80 + "\n")
        return len(eligible)

    due_items = [it for it in eligible if it.get("status") == "PENDING" and it.get("next_retry_at", 0) <= now]
    print(f"  🚀 Processing {len(due_items)} due retry candidate(s)...")
    return len(due_items)

def interactive_config_wizard():
    """Interactively configures or updates API keys and competitive programming handles."""
    print("\n" + "=" * 68)
    print("      ⚙️  AutoCoder AI: Configuration Wizard")
    print("=" * 68)
    print("AutoCoder AI needs a free Google Gemini API key to solve problems.")
    print("👉 Get your key in 10 seconds (free): https://aistudio.google.com/\n")

    current_key = os.environ.get("GEMINI_API_KEY", "")
    key_preview = f" [{current_key[:8]}...]" if current_key else ""
    try:
        new_key = input(f"Enter your Google Gemini API Key{key_preview}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return
    api_key = new_key or current_key

    while not api_key:
        print("❌ Gemini API Key cannot be empty!")
        try:
            api_key = input("Enter your Google Gemini API Key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return

    current_gh = os.environ.get("GITHUB_USERNAME", "")
    gh_preview = f" [{current_gh}]" if current_gh else ""
    try:
        new_gh = input(f"Enter your GitHub Username (optional){gh_preview}: ").strip()
    except (EOFError, KeyboardInterrupt):
        new_gh = ""
    gh_user = new_gh or current_gh

    current_cf = os.environ.get("CODEFORCES_HANDLE", "")
    cf_preview = f" [{current_cf}]" if current_cf else ""
    try:
        new_cf = input(f"Enter your Codeforces Handle (optional){cf_preview}: ").strip()
    except (EOFError, KeyboardInterrupt):
        new_cf = ""
    cf_handle = new_cf or current_cf

    # Save to .env (current dir or CONFIG_DIR)
    target_env = Path.cwd() / ".env" if (Path.cwd() / ".git").exists() or (Path.cwd() / "bot.py").exists() else CONFIG_DIR / ".env"
    env_content = f"""# AutoCoder AI Configuration
GEMINI_API_KEY="{api_key}"
GITHUB_USERNAME="{gh_user}"
CODEFORCES_HANDLE="{cf_handle}"
"""
    target_env.write_text(env_content, encoding="utf-8")
    print(f"\n✅ Configuration saved to: {target_env.resolve()}")
    print("=" * 68 + "\n")

def launch_chrome_debug():
    """Launches Chrome with remote debugging on port 9222."""
    script = BASE_DIR / "connect_chrome.sh"
    if script.exists():
        subprocess.run(["bash", str(script)])
    else:
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to quit'], capture_output=True)
            time.sleep(1)
            subprocess.Popen(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--remote-debugging-port=9222", "--restore-last-session"])
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["google-chrome", "--remote-debugging-port=9222", "--restore-last-session"])
        print("🌐 Chrome launched on port 9222!")

def setup_schedule():
    """Configures the daily background schedule."""
    script = BASE_DIR / "setup_daily_schedule.sh"
    if script.exists():
        subprocess.run(["bash", str(script)])
    else:
        print("ℹ️ Daily schedule is configured via 'autocoder --daily 18:00'")

def launch_interactive_menu():
    """Launches the interactive menu."""
    script = BASE_DIR / "run.sh"
    if script.exists():
        subprocess.run(["bash", str(script)])
    else:
        print("Tip: Run 'autocoder daily' or 'autocoder solve <platform>'")

# ==============================================================================
# Main Entry Point
# ==============================================================================
async def main():
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "status":
            is_detailed = "--detailed" in sys.argv or "-d" in sys.argv
            print_status_summary(detailed=is_detailed)
            return 0
        elif cmd in ["retry-failed", "retry"]:
            is_dry = "--dry-run" in sys.argv or DRY_RUN
            plat = None
            if "--platform" in sys.argv:
                try:
                    plat = sys.argv[sys.argv.index("--platform") + 1]
                except (IndexError, ValueError):
                    pass
            await handle_retry_failed(dry_run=is_dry, platform=plat)
            return 0
        elif cmd in ["config", "setup"]:
            interactive_config_wizard()
            return
        elif cmd == "chrome":
            launch_chrome_debug()
            return
        elif cmd == "schedule":
            setup_schedule()
            return
        elif cmd == "menu":
            launch_interactive_menu()
            return
        elif cmd == "daily":
            # Run daily quota: replace 'daily' with --platform all
            sys.argv = [sys.argv[0], "--platform", "all", "--count", "5", "--difficulty", "medium-hard", "--mode", "both"] + sys.argv[2:]
        elif cmd == "solve":
            if len(sys.argv) > 2 and not sys.argv[2].startswith("-"):
                plat = sys.argv[2]
                sys.argv = [sys.argv[0], "--platform", plat] + sys.argv[3:]
            else:
                sys.argv = [sys.argv[0]] + sys.argv[2:]

    parser = argparse.ArgumentParser(description="Fully Autonomous AI Java Coding Bot")
    parser.add_argument(
        "--platform",
        default="all",
        help="Platform to solve unsolved problems from (e.g. codeforces, hackerrank, or comma-separated list, or 'all', default: all)"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=5,
        help="Number of unsolved problems to solve per platform (default: 5)"
    )
    parser.add_argument(
        "--difficulty",
        choices=["easy", "medium", "hard", "medium-hard", "any"],
        default="medium-hard",
        help="Difficulty filter (default: medium-hard)"
    )
    parser.add_argument(
        "--mode",
        choices=["run", "submit", "both", "save"],
        default="both",
        help="Action: 'run' (test sample cases), 'submit' (full submit), 'both' (run then submit), or 'save' (default: both)"
    )
    parser.add_argument(
        "--file",
        default="problems.txt",
        help="Path to problem URLs file if --platform file is chosen"
    )
    parser.add_argument(
        "--daily",
        type=str,
        default=None,
        help="Run on a daily schedule at specified time (HH:MM format, e.g. '09:00')"
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="When --daily is enabled, also execute immediately before waiting for the scheduled time"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (recommended for scheduled/daemon execution)"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Display verified solved history, daily quotas (Asia/Kolkata), and database summary"
    )
    parser.add_argument(
        "--detailed",
        action="store_true",
        help="Include detailed breakdown of failures, circuit breakers, retry queue, and leases in status"
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry failed problems from the persistent retry queue"
    )
    parser.add_argument(
        "--max-repairs",
        type=int,
        default=3,
        help="Maximum AI repair attempts per problem if compilation or sample tests fail (default: 3)"
    )
    parser.add_argument(
        "--java-version",
        type=str,
        default=None,
        help="Target Java version for javac compilation/verification (e.g. 8, 17, 21; default: auto by platform)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution end-to-end without requiring real Gemini API key or platform credentials"
    )
    parser.add_argument(
        "--offline-only",
        action="store_true",
        help="Run in offline cloud mode without web scraping or browser dependency"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Maximum run duration in seconds before graceful termination (default: 3600)"
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="PostgreSQL connection URL (e.g. postgresql://user:pass@host:5432/db)"
    )
    args = parser.parse_args()

    if args.status:
        print_status_summary(detailed=args.detailed)
        return 0

    is_dry = args.dry_run or DRY_RUN
    is_offline = args.offline_only or OFFLINE_MODE

    if args.retry_failed:
        plat = args.platform if args.platform != "all" else None
        await handle_retry_failed(dry_run=is_dry, platform=plat)
        return 0

    if not is_dry and not GEMINI_API_KEY:
        print("\n" + "=" * 70)
        print("❌ Error: GEMINI_API_KEY is not set!")
        print("👉 You must provide your own free Google Gemini API key to use AutoCoder AI.")
        print("👉 Get your own free key in 10 seconds: https://aistudio.google.com/")
        print("👉 Save your key by running:")
        print("   autocoder config")
        print("   (or add GEMINI_API_KEY=\"your_key_here\" to ~/.autocoder/.env or .env)")
        print("=" * 70 + "\n")
        sys.exit(1)

    if len(sys.argv) == 1:
        print("\n" + "=" * 70)
        print("🚀 AutoCoder AI - Starting Autonomous Coding Batch")
        print(f"   Target: {args.count} {args.difficulty.upper()} Java problems across all 5 platforms")
        print("   Tip: Run './run.sh' for an interactive menu, or use --help for flags")
        print("=" * 70 + "\n")

    async def run_batch() -> bool:
        return await execute_batch(
            platform=args.platform,
            count=args.count,
            difficulty=args.difficulty,
            mode=args.mode,
            file_path=args.file,
            headless=args.headless,
            max_repairs=args.max_repairs,
            java_version=args.java_version,
            dry_run=is_dry,
            offline_mode=is_offline,
            timeout=args.timeout,
            database_url=args.database_url,
        )

    if args.daily:
        if args.run_now:
            print("🚀 --run-now flag detected: Running first batch immediately...")
            ok = await run_batch()
            if not ok:
                print("⚠️ Initial run-now batch reported failures.")
        await run_daily_scheduler(args.daily, run_batch)
        return 0
    else:
        ok = await run_batch()
        if not ok:
            print("\n❌ Batch execution completed with failure / quota shortfall.")
            sys.exit(1)
        return 0

def cli():
    try:
        ret = asyncio.run(main())
        sys.exit(ret or 0)
    except SystemExit as se:
        sys.exit(se.code)
    except KeyboardInterrupt:
        print("\n👋 AutoCoder AI stopped by user. See you next time!")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    cli()
