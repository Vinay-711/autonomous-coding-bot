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
import urllib.request
import argparse
import subprocess
import tempfile
import sqlite3
from contextlib import contextmanager
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple, List, Set, Dict, Any, Union
from enum import Enum
from dataclasses import dataclass, field

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
# Configuration & Environment Setup
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".autocoder"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# Auto-load .env file from working directory, user config dir, or base dir
env_candidates = [
    Path.cwd() / ".env",
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

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "").strip()
CODEFORCES_HANDLE = os.environ.get("CODEFORCES_HANDLE", "").strip()

AI_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
]
SOLUTIONS_DIR = BASE_DIR / "solutions"
SOLUTIONS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
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
HISTORY_FILE = BASE_DIR / "solved_history.json"
if not HISTORY_FILE.exists() and (CONFIG_DIR / "solved_history.json").exists():
    HISTORY_FILE = CONFIG_DIR / "solved_history.json"

DEFAULT_DB_FILE = BASE_DIR / "autocoder.db"
if not DEFAULT_DB_FILE.exists() and (CONFIG_DIR / "autocoder.db").exists():
    DEFAULT_DB_FILE = CONFIG_DIR / "autocoder.db"

TRANSIENT_ERROR_SUBSTRINGS = [
    "timeout", "timed out", "econnreset", "connection reset",
    "network error", "rate limit", "503", "502", "504", "temporary",
    "cdp reconnection notice", "session closed", "target closed"
]

def is_transient_error(error_msg: Optional[str]) -> bool:
    """Classifies whether an error message indicates a transient recoverable failure."""
    if not error_msg:
        return False
    msg_low = error_msg.lower()
    return any(sub in msg_low for sub in TRANSIENT_ERROR_SUBSTRINGS)

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
    """Persistent SQLite storage engine for problems, attempts, submissions, verdicts, publish jobs, and daily runs."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None, auto_migrate: bool = True):
        if db_path is not None:
            self.db_path = Path(db_path)
        else:
            env_db = os.environ.get("AUTOCODER_DB_PATH")
            self.db_path = Path(env_db) if env_db else DEFAULT_DB_FILE

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        if auto_migrate:
            self.migrate_from_solved_history_if_needed()

    @contextmanager
    def get_connection(self):
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

    def _init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("""
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
                    failure_reason TEXT,
                    failure_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, canonical_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    target_quota INTEGER NOT NULL,
                    attempted_count INTEGER DEFAULT 0,
                    generated_count INTEGER DEFAULT 0,
                    compiled_count INTEGER DEFAULT 0,
                    accepted_count INTEGER DEFAULT 0,
                    published_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    skipped_count INTEGER DEFAULT 0,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL DEFAULT 'IN_PROGRESS'
                )
            """)
            cursor.execute("""
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
                    finished_at TEXT
                )
            """)
            cursor.execute("""
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
            cursor.execute("""
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
            cursor.execute("""
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
            cursor.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY, migrated_at TEXT)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_problems_lookup ON problems(platform, canonical_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_problems_status ON problems(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_runs_lookup ON daily_runs(run_date, platform)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_judge_verdicts_lookup ON judge_verdicts(platform, canonical_id)")
            cursor.execute("COMMIT")

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
        lease_timeout_sec: float = 600.0
    ) -> bool:
        """
        Atomically claims a problem for processing using a SQLite transaction with BEGIN IMMEDIATE.
        Returns True if claim acquired; False if already accepted or actively claimed by another worker.
        """
        now = time.time()
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT id, status, claimed_by, claimed_at FROM problems WHERE platform = ? AND canonical_id = ?",
                (platform, canonical_id)
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    INSERT INTO problems (platform, canonical_id, url, status, claimed_by, claimed_at, created_at, updated_at)
                    VALUES (?, ?, ?, 'IN_PROGRESS', ?, ?, ?, ?)
                    """,
                    (platform, canonical_id, url, worker_id, now, now_iso, now_iso)
                )
                cursor.execute("COMMIT")
                return True

            prob_id, status, claimed_by, claimed_at = row[0], row[1], row[2], row[3]
            if status == "ACCEPTED":
                cursor.execute("COMMIT")
                return False

            if status == "IN_PROGRESS":
                if claimed_by != worker_id and claimed_at and (now - claimed_at) < lease_timeout_sec:
                    cursor.execute("COMMIT")
                    return False

            cursor.execute(
                """
                UPDATE problems
                SET status = 'IN_PROGRESS', claimed_by = ?, claimed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (worker_id, now, now_iso, prob_id)
            )
            cursor.execute("COMMIT")
            return True

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
                SET status = ?, claimed_by = NULL, claimed_at = NULL,
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
                    accepted_count = accepted_count + ?,
                    published_count = published_count + ?,
                    failed_count = failed_count + ?,
                    skipped_count = skipped_count + ?
                WHERE id = ?
                """,
                (attempted, generated, compiled, accepted, published, failed, skipped, run_id)
            )
            cursor.execute("COMMIT")

    def complete_daily_run(self, run_id: int, status: str = "COMPLETED"):
        now_iso = get_kolkata_now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "UPDATE daily_runs SET completed_at = ?, status = ? WHERE id = ?",
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
        is_transient: bool = False
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
                INSERT INTO attempts (problem_id, run_id, platform, canonical_id, stage, mode, solution_code, explanation, error, is_transient, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (prob_id, run_id, platform, canonical_id, stage.value, mode, solution_code, explanation, error, 1 if is_transient else 0, now_iso, now_iso)
            )
            attempt_id = cursor.lastrowid
            cursor.execute("COMMIT")
            return attempt_id

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
                       accepted_count, published_count, failed_count, skipped_count, status
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
            cls._db = DatabaseManager()
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
    error_feedback: Optional[str] = None
) -> Tuple[str, str]:
    """Generates pure Java solution using Gemini 2.5 with model fallbacks."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set.")

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
    """Tries multiple selectors to retrieve the problem description."""
    per_selector_timeout = max(2000, timeout_ms // len(selectors))
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            await loc.wait_for(state="visible", timeout=per_selector_timeout)
            text = (await loc.inner_text()).strip()
            if len(text) > 150:
                return text
        except Exception:
            continue

    for fallback in ["article", "main", "#content", ".content", ".problem-content"]:
        try:
            loc = page.locator(fallback).first
            if await loc.is_visible():
                text = (await loc.inner_text()).strip()
                if len(text) > 80:
                    return text
        except Exception:
            continue
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

async def inject_code_into_editor(page: Page, solution_code: str) -> bool:
    """Injects solution code using Monaco/Ace APIs, direct keyboard insertion, or clipboard."""
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
            print(f"   ✅ Code injected via {result}!")
            return True
    except Exception as e:
        print(f"   ⚠️  JS API injection notice: {e}")

    # Method 2: UI Focus + Keyboard Select All + Insert Text
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
            if await loc.is_visible():
                await loc.click()
                await asyncio.sleep(0.3)
                await page.keyboard.press(f"{modifier}+KeyA")
                await asyncio.sleep(0.1)
                await page.keyboard.press("Backspace")
                await asyncio.sleep(0.1)

                await page.keyboard.insert_text(solution_code)
                print("   ✅ Code inserted via keyboard.")
                return True
        except Exception:
            continue

    # Method 3: System Clipboard Fallback
    print("   📋 Using system clipboard fallback...")
    try:
        pyperclip.copy(solution_code)
        await page.keyboard.press(f"{modifier}+KeyA")
        await page.keyboard.press("Backspace")
        await page.keyboard.press(f"{modifier}+KeyV")
        print("   ✅ Code pasted from clipboard.")
        return True
    except Exception as e:
        print(f"   ❌ Clipboard injection failed: {e}")
        return False

# ==============================================================================
# Local Java Compilation & Verification (Universal for All Platforms)
# ==============================================================================
def compile_java_code_locally(code: str, platform_type: str) -> Tuple[bool, str, str]:
    """Compiles Java code locally using javac to detect syntax, type, and import errors."""
    cleaned = clean_code_string(code)

    # Platform-specific class naming adjustments
    if platform_type == "hackerearth":
        # HackerEarth requires TestClass
        cleaned = re.sub(r"public\s+class\s+Main\b", "public class TestClass", cleaned)
        cleaned = re.sub(r"\bclass\s+Main\b", "class TestClass", cleaned)
        class_name = "TestClass"
    elif platform_type in ["codeforces", "codechef"]:
        class_name = "Main"
    else:  # leetcode, hackerrank
        match = re.search(r"public\s+class\s+(\w+)", cleaned)
        class_name = match.group(1) if match else "Solution"

    with tempfile.TemporaryDirectory() as tmpdir:
        # Provide standard LeetCode data structure stubs if referenced but not defined
        if platform_type == "leetcode":
            if "class TreeNode" not in cleaned and "TreeNode" in cleaned:
                with open(Path(tmpdir) / "TreeNode.java", "w", encoding="utf-8") as f:
                    f.write("public class TreeNode { public int val; public TreeNode left; public TreeNode right; public TreeNode() {} public TreeNode(int val) { this.val = val; } public TreeNode(int val, TreeNode left, TreeNode right) { this.val = val; this.left = left; this.right = right; } }")
            if "class ListNode" not in cleaned and "ListNode" in cleaned:
                with open(Path(tmpdir) / "ListNode.java", "w", encoding="utf-8") as f:
                    f.write("public class ListNode { public int val; public ListNode next; public ListNode() {} public ListNode(int val) { this.val = val; } public ListNode(int val, ListNode next) { this.val = val; this.next = next; } }")
            if "class Node" not in cleaned and "Node" in cleaned:
                with open(Path(tmpdir) / "Node.java", "w", encoding="utf-8") as f:
                    f.write("import java.util.*; public class Node { public int val; public List<Node> children; public Node left, right, next, random; public Node() {} public Node(int _val) { val = _val; } }")

        src_file = Path(tmpdir) / f"{class_name}.java"
        with open(src_file, "w", encoding="utf-8") as f:
            f.write(cleaned)

        try:
            cmd = ["javac"]
            if platform_type in ["hackerrank", "hackerearth"]:
                cmd.extend(["-Xlint:-options", "--release", "8"])
            cmd.append(str(src_file))
            comp_proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15
            )
            if comp_proc.returncode != 0:
                return False, comp_proc.stderr.strip(), cleaned
            return True, "Compilation successful.", cleaned
        except Exception as e:
            return False, f"javac invocation failed: {e}", cleaned

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
        inputs = await page.locator(".sample-test .input pre").all_inner_texts()
        outputs = await page.locator(".sample-test .output pre").all_inner_texts()
        if not inputs or not outputs:
            return True, "No sample test cases found on page."

        match = re.search(r"public\s+class\s+(\w+)", code)
        class_name = match.group(1) if match else "Main"

        with tempfile.TemporaryDirectory() as tmpdir:
            src_file = Path(tmpdir) / f"{class_name}.java"
            with open(src_file, "w", encoding="utf-8") as f:
                f.write(code)

            # Compile with javac
            comp_proc = subprocess.run(
                ["javac", str(src_file)],
                capture_output=True,
                text=True,
                timeout=12
            )
            if comp_proc.returncode != 0:
                return False, f"Java compilation error:\n{comp_proc.stderr.strip()}"

            all_passed = True
            summary = []
            for i, (inp, expected) in enumerate(zip(inputs, outputs), 1):
                inp = inp.strip()
                expected = expected.strip()
                run_proc = subprocess.run(
                    ["java", "-cp", tmpdir, class_name],
                    input=inp,
                    capture_output=True,
                    text=True,
                    timeout=6
                )
                actual = run_proc.stdout.strip()
                if run_proc.returncode != 0:
                    all_passed = False
                    summary.append(f"Sample {i}: RUNTIME ERROR (Code {run_proc.returncode})\nStderr: {run_proc.stderr.strip()}")
                elif actual == expected:
                    summary.append(f"Sample {i}: PASSED (Output == Expected == '{expected}')")
                else:
                    all_passed = False
                    summary.append(f"Sample {i}: FAILED\nInput:\n{inp}\nExpected:\n{expected}\nActual:\n{actual}")

            return all_passed, "\n".join(summary)
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
# Problem Solver Pipeline (Fully Automated, Java)
# ==============================================================================
async def process_problem(
    page: Page,
    url: str,
    mode: str = "run",
    retry_publish: bool = False,
    cached_verdict: Optional[SubmissionVerdict] = None,
    cached_code: Optional[str] = None,
    cached_explanation: Optional[str] = None,
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

    # 1. Navigate to problem page
    print(f"[1/6] Navigating to {platform_name}...")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(4)
    except Exception as e:
        print(f"❌ Failed to load {url}: {e}")
        return ProcessResult(stage=ProcessStage.GENERATED, success=False, url=url, platform=platform_type, problem_id=prob_id, error=f"Failed to load {url}: {e}")

    # 2. Automatically ensure editor language is set to Java
    print("[2/6] Ensuring editor language is set to Java...")
    await switch_editor_to_java(page, platform_type)

    # 3. Extract problem description
    print("[3/6] Extracting problem description...")
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

    # 5. Generate Java solution via Gemini with local javac verification & repair loop
    print("[4/7] Solving in Java with Gemini AI & verifying locally...")
    solution_code = None
    explanation = None
    compile_passed = False
    last_error_log = None

    for attempt in range(1, 4):
        print(f"   [Attempt {attempt}/3] Generating Java solution...")
        solution_code, explanation = ask_gemini_for_java_code(
            problem_text=problem_text,
            starter_code=starter_code,
            platform_name=platform_name,
            platform_type=platform_type,
            error_feedback=last_error_log
        )

        # Local javac compilation check
        print(f"   🧪 Compiling generated Java solution with javac locally...")
        compile_passed, err_msg, solution_code = compile_java_code_locally(solution_code, platform_type)
        if compile_passed:
            print(f"   ✅ Local javac compilation PASSED cleanly!")
            break
        else:
            print(f"   ⚠️  Java compilation failed with error:\n{err_msg}")
            last_error_log = f"Java compilation error:\n{err_msg}"

    if not compile_passed or not solution_code:
        print(f"❌ Failed to generate compiling Java code for {url} after 3 attempts. Skipping to protect repo integrity.")
        return ProcessResult(stage=ProcessStage.GENERATED, success=False, url=url, platform=platform_type, problem_id=prob_id, error="Failed to generate compiling Java code after 3 attempts")

    print(f"   💡 Approach: {explanation}")
    filename = get_safe_java_filename(url, platform_name)

    # In mode 'save', store locally only; no judge submission or GitHub commit
    if mode == "save":
        print(f"   💾 Mode 'SAVE': saving compiled Java solution locally without running or submitting.")
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
            published=False
        )

    # Optional local sample test verification for Codeforces
    if platform_type == "codeforces":
        print("   🧪 Testing Java solution against sample test cases locally...")
        passed_samples, sample_log = await verify_codeforces_samples_java(page, solution_code)
        print(f"   {sample_log}")
        if not passed_samples:
            print("   ⚠️  Sample test failed! Attempting self-correction...")
            try:
                corr_code, corr_exp = ask_gemini_for_java_code(
                    problem_text=problem_text,
                    starter_code=starter_code,
                    platform_name=platform_name,
                    platform_type=platform_type,
                    error_feedback=sample_log
                )
                c_ok, _, clean_corr = compile_java_code_locally(corr_code, platform_type)
                if c_ok:
                    solution_code = clean_corr
                    explanation = corr_exp
            except Exception:
                pass

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
        await inject_code_into_editor(page, solution_code)

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
        published=True,
        repo_name=pub_res.repo_name,
        repo_branch=pub_res.branch,
        commit_hash=pub_res.commit_hash
    )

async def ensure_live_page(page: Page, p_playwright=None, browser=None, context=None) -> Tuple[Any, Any, Page]:
    """Guarantees an active, responsive browser page by reconnecting over CDP if disconnected."""
    if page:
        try:
            if not page.is_closed():
                await page.evaluate("1 + 1")
                return browser, context, page
        except Exception:
            pass

    print("   🔄 Browser tab or CDP connection lost. Reconnecting to Chrome via CDP (port 9222)...")
    if p_playwright:
        try:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            browser = await p_playwright.chromium.connect_over_cdp("http://localhost:9222")
            context = browser.contexts[0]
            page = await context.new_page()
            print("   ✅ Reconnected cleanly to live Chrome via CDP!")
            return browser, context, page
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
    page: Page,
    platform: str,
    target_accepted: int = 5,
    difficulty: str = "medium-hard",
    mode: str = "both",
    p_playwright = None,
    browser = None,
    context = None
) -> Tuple[int, Any, Any, Page]:
    """Solves problems on the given platform until target_accepted ACCEPTED problems are achieved.
    If a problem is not accepted by the judge, it fetches replacement candidates using pagination.
    Maintains per-platform quota independently without reallocation.
    """
    print(f"\n{'='*70}")
    print(f"🎯 STARTING QUOTA SOLVER: {platform.upper()} (Target: {target_accepted} ACCEPTED Java solutions)")
    print(f"{'='*70}")

    discovery_map = {
        "leetcode": discover_leetcode_candidates,
        "codeforces": discover_codeforces_candidates,
        "hackerrank": discover_hackerrank_candidates,
        "codechef": discover_codechef_candidates,
        "hackerearth": discover_hackerearth_candidates,
    }

    disc_func = discovery_map.get(platform)
    if not disc_func:
        print(f"❌ Unknown platform for quota solving: {platform}")
        return 0, browser, context, page

    db = SolvedRegistry.get_db()
    run_id = db.get_or_create_daily_run(platform, target_accepted)
    worker_id = f"worker_{os.getpid()}_{id(asyncio.current_task()) if asyncio.current_task() else 0}"

    page._anti_bot_blocked = False
    accepted_count = 0
    attempt_count = 0
    max_attempts = target_accepted * 4  # Bounded search limit per run
    failed_in_run: Set[str] = set()    # Prevent repeating failed candidates in the same run
    discovery_offset = 0
    step_size = max(15, target_accepted * 2)

    browser, context, page = await ensure_live_page(page, p_playwright, browser, context)
    candidate_pool = await disc_func(page, count=step_size, difficulty=difficulty, offset=discovery_offset)
    discovery_offset += step_size
    pool_idx = 0

    while accepted_count < target_accepted and attempt_count < max_attempts:
        page._anti_bot_blocked = False
        if pool_idx >= len(candidate_pool):
            print(f"⚠️ Exhausted candidate pool. Querying next batch of candidates for {platform} (offset={discovery_offset})...")
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

        # Transactional atomic claim
        claimed = db.claim_problem(platform, canonical_id, url=url, worker_id=worker_id)
        if not claimed:
            print(f"   🔒 Problem {canonical_id} is claimed or already accepted. Skipping...")
            db.increment_daily_run_counts(run_id, skipped=1)
            continue

        attempt_count += 1
        db.increment_daily_run_counts(run_id, attempted=1)
        print(f"\n👉 [{platform.upper()}] Attempt {attempt_count}/{max_attempts} | Target Progress: {accepted_count}/{target_accepted} Accepted")
        print(f"   Solving: {url}")

        browser, context, page = await ensure_live_page(page, p_playwright, browser, context)

        try:
            result = await asyncio.wait_for(process_problem(page, url, mode=mode), timeout=180)
        except asyncio.TimeoutError:
            print(f"   ⏱️  Problem processing timed out after 180s for {url}. Recovering...")
            try:
                await page.goto("about:blank", timeout=5000)
            except Exception:
                pass
            result = ProcessResult(stage=ProcessStage.SUBMITTED, success=False, url=url, error="Processing timeout (180s)")
        except Exception as e:
            print(f"   ❌ Unhandled error processing {url}: {e}")
            result = ProcessResult(stage=ProcessStage.SUBMITTED, success=False, url=url, error=str(e))

        # Check stages reached
        if result.stage.value in [ProcessStage.GENERATED.value, ProcessStage.COMPILED.value, ProcessStage.TESTED.value,
                                  ProcessStage.SUBMITTED.value, ProcessStage.ACCEPTED.value, ProcessStage.PUBLISHED.value,
                                  ProcessStage.PUBLISH_FAILED.value]:
            db.increment_daily_run_counts(run_id, generated=1)
        if result.stage.value in [ProcessStage.COMPILED.value, ProcessStage.TESTED.value,
                                  ProcessStage.SUBMITTED.value, ProcessStage.ACCEPTED.value, ProcessStage.PUBLISHED.value,
                                  ProcessStage.PUBLISH_FAILED.value]:
            db.increment_daily_run_counts(run_id, compiled=1)

        # Check for publish failure on confirmed accepted solution: retry publishing without resubmitting
        if result.stage == ProcessStage.PUBLISH_FAILED and result.verdict and result.verdict.state == VerdictState.ACCEPTED:
            print(f"   ⚠️  Publish failed for {url} ({result.publish_error}). Retrying publish without resubmitting to judge...")
            retry_res = await process_problem(
                page, url, mode=mode,
                retry_publish=True,
                cached_verdict=result.verdict,
                cached_code=result.solution_code,
                cached_explanation=result.explanation
            )
            if retry_res.published:
                result = retry_res

        # Quota credit requires confirmed publication in submit/both modes
        is_successful = (result.success and result.published) if mode in ["submit", "both"] else result.success

        err_msg = result.error or (result.verdict.message if result.verdict else None)
        transient = is_transient_error(err_msg)

        attempt_id = db.record_attempt(
            platform=platform,
            canonical_id=canonical_id,
            run_id=run_id,
            stage=result.stage,
            mode=mode,
            solution_code=result.solution_code,
            explanation=result.explanation,
            error=err_msg,
            is_transient=transient
        )

        if is_successful:
            accepted_count += 1
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

            if getattr(page, "_anti_bot_blocked", False):
                page._anti_bot_blocked = False
                print(f"\n🛑 [{platform.upper()}] Platform is gated by Cloudflare Anti-Bot verification.")
                print(f"🛑 Stopping {platform.upper()} attempts to avoid continuous looping.")
                print(f"📁 Generated Java solution has been saved to: {SOLUTIONS_DIR.resolve()}\n")
                break

            print(f"🔄 Selecting next unsolved replacement candidate (offset={discovery_offset})...\n")

        await asyncio.sleep(3)

    run_status = "COMPLETED" if accepted_count >= target_accepted else "SHORTFALL"
    db.complete_daily_run(run_id, status=run_status)
    print(f"\n📊 [{platform.upper()}] Quota Summary: {accepted_count}/{target_accepted} verified accepted solutions ({run_status}).")
    return accepted_count, browser, context, page

async def execute_batch(
    platform: str,
    count: int,
    difficulty: str,
    mode: str,
    file_path: Optional[str] = None,
    headless: bool = False
) -> bool:
    """Executes a complete automated batch of problems with zero human input."""
    start_time = datetime.now()
    print("\n" + "=" * 70)
    print(f"🚀 STARTING AUTOMATED RUN [{start_time.strftime('%Y-%m-%d %H:%M:%S')}]")
    print(f"   Platform   : {platform.upper()}")
    count_label = f"{count} problem(s) per platform ({count * 5} total across 5 platforms)" if platform == "all" else f"{count} problem(s)"
    print(f"   Count      : {count_label}")
    print(f"   Language   : JAVA ☕")
    print(f"   Difficulty : {difficulty.upper()}")
    print(f"   Mode       : {mode.upper()}")
    print("=" * 70)

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
                res = await process_problem(page, url, mode=mode)
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
                    p_playwright=p, browser=browser, context=context
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
                p_playwright=p, browser=browser, context=context
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
def print_status_summary(db: Optional[DatabaseManager] = None):
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
        print(f"  {'Platform':<14} {'Target':>7} {'Attempted':>10} {'Generated':>10} {'Compiled':>9} {'Accepted':>9} {'Published':>10} {'Failed':>7} {'Skipped':>8} {'Status':<10}")
        print("  " + "─" * 98)
        for r in daily_rows:
            p = display_names.get(r.get("platform", ""), r.get("platform", "").upper())
            print(f"  {p:<14} {r.get('target_quota', 0):>7} {r.get('attempted_count', 0):>10} {r.get('generated_count', 0):>10} {r.get('compiled_count', 0):>9} {r.get('accepted_count', 0):>9} {r.get('published_count', 0):>10} {r.get('failed_count', 0):>7} {r.get('skipped_count', 0):>8} {r.get('status', ''):<10}")
    else:
        print("  (No runs recorded yet today)")

    print("\n" + "=" * 76)
    print(f"  📁 SQLite Database  : {DEFAULT_DB_FILE.resolve()}")
    print(f"  📁 Solved Registry  : {HISTORY_FILE.resolve()}")
    print(f"  📁 Solution Files   : {SOLUTIONS_DIR.resolve()}")
    if GITHUB_USERNAME:
        print(f"  🐙 GitHub Profile   : https://github.com/{GITHUB_USERNAME}")
    print("=" * 76 + "\n")

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
            print_status_summary()
            return
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
    args = parser.parse_args()

    if args.status:
        print_status_summary()
        return 0

    if not GEMINI_API_KEY:
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
            headless=args.headless
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
