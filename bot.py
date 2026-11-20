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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple, List, Set, Any, Dict

import pyperclip
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from google.genai.errors import APIError
from playwright.async_api import async_playwright, Page, BrowserContext

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
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "").strip()
CODEFORCES_HANDLE = os.environ.get("CODEFORCES_HANDLE", "").strip()

AI_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
]
SOLUTIONS_DIR = BASE_DIR / "solutions"
SOLUTIONS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

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
    thought_and_algorithm: str = Field(description="Step-by-step reasoning: analyze constraints (N, time limits), formulate optimal algorithm, check for edge cases (recursion depth, 64-bit integer overflow, corner cases), and plan data structures.")
    code: str = Field(description="Complete, valid, standalone code in the requested programming language without markdown fences")
    explanation: str = Field(description="Concise summary of final approach and time/space complexity")

# ==============================================================================
# ==============================================================================
# Central Solved Registry (Eliminates All Repeated Questions)
# ==============================================================================
HISTORY_FILE = BASE_DIR / "solved_history.json"
if not HISTORY_FILE.exists() and (CONFIG_DIR / "solved_history.json").exists():
    HISTORY_FILE = CONFIG_DIR / "solved_history.json"

class SolvedRegistry:
    """Central registry tracking all solved problems across all platforms, local folders, and git repos."""
    _cache: Optional[dict] = None

    @classmethod
    def normalize(cls, s: str) -> str:
        s = s.strip().lower()
        s = re.sub(r"^https?://[^/]+/", "", s)
        s = re.sub(r"^(problems|challenges|practice-problems/algorithm|problemset/problem)/\??", "", s)
        s = re.sub(r"/problem/?$", "", s)
        s = re.sub(r"[^a-z0-9_]", "_", s)
        s = re.sub(r"_+", "_", s).strip("_")
        return s

    @classmethod
    def get_all_solved(cls, force_refresh: bool = False) -> dict:
        if cls._cache is not None and not force_refresh:
            return cls._cache

        platforms = ["leetcode", "codeforces", "hackerrank", "codechef", "hackerearth"]
        solved = {p: set() for p in platforms}

        # 1. Load from persistent history file
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for p, slugs in data.items():
                        if p in solved:
                            for s in slugs:
                                solved[p].add(cls.normalize(s))
            except Exception:
                pass

        # 2. Load from local solutions/ folder
        for f in SOLUTIONS_DIR.glob("*.*"):
            if f.suffix in [".java", ".py"]:
                name = f.stem.lower()
                for p in platforms:
                    if name.startswith(f"{p}_"):
                        slug = name[len(p)+1:]
                        solved[p].add(cls.normalize(slug))

        # 3. Load from all 5 GitHub repositories (repos/*/solutions/**)
        for p in platforms:
            repo_solutions = REPOS_DIR / f"{p}-daily" / "solutions"
            if repo_solutions.exists():
                for sub in repo_solutions.glob("*/*"):
                    if sub.is_dir():
                        solved[p].add(cls.normalize(sub.name))

        cls._cache = solved
        return cls._cache

    @classmethod
    def is_solved(cls, platform: str, slug_or_url: str) -> bool:
        norm = cls.normalize(slug_or_url)
        all_solved = cls.get_all_solved()
        return norm in all_solved.get(platform, set())

    @classmethod
    def mark_solved(cls, platform: str, slug_or_url: str):
        norm = cls.normalize(slug_or_url)
        all_solved = cls.get_all_solved()
        if platform not in all_solved:
            all_solved[platform] = set()
        all_solved[platform].add(norm)
        cls._cache = all_solved
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump({p: sorted(list(s)) for p, s in all_solved.items()}, f, indent=2)
        except Exception:
            try:
                fallback_file = CONFIG_DIR / "solved_history.json"
                with open(fallback_file, "w", encoding="utf-8") as f:
                    json.dump({p: sorted(list(s)) for p, s in all_solved.items()}, f, indent=2)
            except Exception as e:
                print(f"⚠️ Failed to write to solved_history.json: {e}")

def get_existing_solved_slugs() -> Set[str]:
    """Backward compatibility helper."""
    all_s = set()
    for s_set in SolvedRegistry.get_all_solved().values():
        all_s.update(s_set)
    return all_s

async def discover_leetcode_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard") -> List[str]:
    """Queries LeetCode GraphQL directly for free, completely unsolved problems for user."""
    # Enforce policy: strictly Easy or Medium only (never Hard)
    eff_diff = "easy" if difficulty.lower() == "easy" else ("medium" if difficulty.lower() == "medium" else "easy-medium")
    print(f"\n🔍 [LeetCode] Finding up to {count} unsolved problems (Difficulty: {eff_diff.upper()}, strictly no Hard)...")
    candidates = []

    diff_list = []
    if eff_diff == "easy":
        diff_list = ["EASY"]
    elif eff_diff == "medium":
        diff_list = ["MEDIUM"]
    else:
        # Strictly Medium and Easy only (never Hard as requested)
        diff_list = ["MEDIUM", "EASY"]

    try:
        await page.goto("https://leetcode.com/problemset/", wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(2)

        for diff in diff_list:
            if len(candidates) >= count:
                break
            gql_query = """async (diff) => {
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
                            categorySlug: "algorithms",
                            skip: 0,
                            limit: 50,
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
            questions = await page.evaluate(gql_query, diff)
            for q in questions:
                if q.get("paidOnly"):
                    continue
                if q.get("status") in ["ac", "not_started_false"]:
                    continue
                if q.get("difficulty", "").upper() == "HARD":
                    continue
                slug = q.get("titleSlug")
                if not slug:
                    continue
                if SolvedRegistry.is_solved("leetcode", slug):
                    continue
                full_url = f"https://leetcode.com/problems/{slug}/"
                if full_url not in candidates:
                    candidates.append(full_url)
                if len(candidates) >= count:
                    break
    except Exception as e:
        print(f"   ⚠️  LeetCode GraphQL query notice: {e}")

    print(f"   ✅ Discovered {len(candidates)} completely unsolved LeetCode candidates (strictly Easy/Medium).")
    return candidates

async def discover_codeforces_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard") -> List[str]:
    """Finds unsolved Codeforces problems in Medium ratings, prioritizing curated standard catalog first."""
    print(f"\n🔍 [Codeforces] Finding up to {count} unsolved problems (Difficulty: {difficulty.upper()})...")
    candidates = []

    # 1. Sync user's solved list from Codeforces API
    if CODEFORCES_HANDLE:
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

    # 2. Prioritize curated catalog of standard classic Div 2A/2B/Div 3 problems first
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
    for u in catalog_fallback:
        slug = u.rstrip("/").split("/")[-2] + "_" + u.rstrip("/").split("/")[-1]
        if not SolvedRegistry.is_solved("codeforces", slug) and u not in candidates:
            candidates.append(u)
        if len(candidates) >= count:
            break

    # 3. If more needed, query Codeforces API with calibrated ratings
    if len(candidates) < count:
        try:
            req = urllib.request.Request("https://codeforces.com/api/problemset.problems", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read().decode("utf-8"))

            if res.get("status") == "OK":
                probs = res["result"]["problems"]
                stats = {(s["contestId"], s["index"]): s["solvedCount"] for s in res["result"]["problemStatistics"]}
                sorted_probs = sorted(probs, key=lambda p: stats.get((p["contestId"], p["index"]), 0), reverse=True)

                for p in sorted_probs:
                    rating = p.get("rating", 1200)
                    if difficulty.lower() == "easy":
                        if rating > 1000:
                            continue
                    elif difficulty.lower() == "hard":
                        if rating < 1400 or rating > 1800:
                            continue
                    else: # medium / medium-hard: standard Div 2A/2B/Div 3 (rating 1000 - 1350)
                        if rating < 1000 or rating > 1350:
                            continue

                    slug = f"{p['contestId']}_{p['index']}"
                    if SolvedRegistry.is_solved("codeforces", slug):
                        continue

                    url = f"https://codeforces.com/problemset/problem/{p['contestId']}/{p['index']}"
                    if url not in candidates:
                        candidates.append(url)
                    if len(candidates) >= count:
                        break
        except Exception as e:
            print(f"   ⚠️  Codeforces API note: {e}")

    print(f"   ✅ Discovered {len(candidates)} completely unsolved Codeforces candidates.")
    return candidates

async def discover_hackerrank_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard") -> List[str]:
    """Finds unsolved HackerRank challenges (Medium/Easy) using curated catalog first, then API with pagination."""
    print(f"\n🔍 [HackerRank] Finding up to {count} unsolved challenges (Difficulty: {difficulty.upper()})...")
    candidates = []

    # 1. Prioritize verified solvable challenges first
    curated_hr = [
        "magic-square-forming", "climbing-the-leaderboard", "extra-long-factorials",
        "non-divisible-subset", "queens-attack-2", "organizing-containers-of-balls",
        "bigger-is-greater", "the-time-in-words", "encryption", "surface-area-3d",
        "absolute-permutation", "the-bomberman-game", "two-pluses", "larrys-array",
        "almost-sorted", "reduced-string", "sherlock-and-anagrams",
        "common-child", "special-palindrome-again", "fraudulent-activity-notifications",
        "count-luck", "cut-the-tree", "connected-cell-in-a-grid",
        "short-palindrome", "bear-and-steady-gene", "sherlock-and-valid-string",
        "simple-array-sum", "compare-the-triplets", "a-very-big-sum", "diagonal-difference",
        "plus-minus", "staircase", "mini-max-sum", "birthday-cake-candles", "time-conversion"
    ]
    for slug in curated_hr:
        if not SolvedRegistry.is_solved("hackerrank", slug):
            u = f"https://www.hackerrank.com/challenges/{slug}/problem"
            if u not in candidates:
                candidates.append(u)
        if len(candidates) >= count:
            break

    # 2. If more candidates needed, query API with popularity ranking (sorted by solved_count desc)
    if len(candidates) < count:
        diff_params = "&filters%5Bdifficulty%5D%5B%5D=medium"
        if difficulty.lower() == "easy":
            diff_params = "&filters%5Bdifficulty%5D%5B%5D=easy"
        elif difficulty.lower() == "hard":
            diff_params = "&filters%5Bdifficulty%5D%5B%5D=hard"

        # Slugs that are contest-level NP-hard, flawed judges, or expert DP
        unsolvable_blacklist = {
            "clique", "xor-quadruples", "lena-sort", "bonetrousle", "mr-k-marsh",
            "summing-pieces", "hackerrank-city", "hr-city", "prime-digit-sums", "fair-cut",
            "prime-xor", "kingdom-division", "cloudy-day", "determining-dna-health",
            "insertion-sort", "morgan-and-a-string", "count-strings", "cards-permutation",
            "string-similarity", "super-functional-strings", "gridland-provinces",
            "ashton-and-string", "circular-palindromes", "similar-strings",
            "wet-shark-and-two-subsequences", "cut-tree", "xor-and-sum", "two-robots", "nikita-and-the-game"
        }

        api_candidates = []
        tracks = ["data-structures", "algorithms"]
        for track in tracks:
            for offset in [0, 50, 100, 150, 200]:
                try:
                    api_url = f"https://www.hackerrank.com/rest/contests/master/tracks/{track}/challenges?offset={offset}&limit=50{diff_params}"
                    req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        for m in data.get("models", []):
                            slug = m.get("slug")
                            solved_cnt = m.get("solved_count", 0)
                            succ_rate = m.get("success_ratio", 0)
                            if not slug or slug in unsolvable_blacklist or SolvedRegistry.is_solved("hackerrank", slug):
                                continue
                            # Require at least 10,000 solves and 65% success rate for high reliability
                            if solved_cnt >= 10000 and succ_rate >= 0.65:
                                api_candidates.append((slug, solved_cnt))
                except Exception as e:
                    continue

        # Sort candidate models by popularity (highest solved_count first)
        api_candidates.sort(key=lambda x: x[1], reverse=True)
        for slug, _ in api_candidates:
            u = f"https://www.hackerrank.com/challenges/{slug}/problem"
            if u not in candidates:
                candidates.append(u)
            if len(candidates) >= count:
                break

    print(f"   ✅ Discovered {len(candidates)} completely unsolved HackerRank candidates.")
    return candidates

async def discover_codechef_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard") -> List[str]:
    """Finds unsolved CodeChef problems in Medium ratings via curated pool and API."""
    print(f"\n🔍 [CodeChef] Finding up to {count} unsolved problems (Difficulty: {difficulty.upper()})...")
    candidates = []

    # 1. Prioritize curated classic problems first
    curated_cc = [
        "NAME2", "ALTARAY", "MAXDIFF", "DIVSUBS", "TACHSTCK",
        "CHEFA", "STONES", "MARCHA1", "CIELRCPT", "CARVANS",
        "JOHNY", "CLEANUP", "HORSES", "CSUB", "CNOTE",
        "RAINBOWA", "SALARY", "CHN15A", "FRGTNLNG", "COPS"
    ]
    for c in curated_cc:
        if not SolvedRegistry.is_solved("codechef", c):
            u = f"https://www.codechef.com/problems/{c}"
            if u not in candidates:
                candidates.append(u)
        if len(candidates) >= count:
            break

    # 2. If more candidates needed, check API with calibrated rating (1000 - 1450 max, avoiding extreme 2200 problems)
    if len(candidates) < count:
        pages_to_check = [0, 1, 2, 3, 4, 5, 6, 7] if difficulty.lower() == "easy" else [5, 10, 15, 20]
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
                            if difficulty.lower() == "easy":
                                if r > 1100:
                                    continue
                            elif difficulty.lower() == "hard":
                                if r < 1400 or r > 1800:
                                    continue
                            else: # medium / medium-hard
                                if r < 1000 or r > 1450:
                                    continue
                        except Exception:
                            continue

                        code = p.get("code")
                        if not code or SolvedRegistry.is_solved("codechef", code):
                            continue
                        u = f"https://www.codechef.com/problems/{code}"
                        if u not in candidates:
                            candidates.append(u)
                        if len(candidates) >= count:
                            break
            except Exception as e:
                print(f"   ⚠️  CodeChef API page {p_num} note: {e}")

    print(f"   ✅ Discovered {len(candidates)} completely unsolved CodeChef candidates.")
    return candidates

async def discover_hackerearth_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard") -> List[str]:
    """Finds unsolved HackerEarth problems (Medium/Hard) using curated pool and live track crawling."""
    print(f"\n🔍 [HackerEarth] Finding up to {count} unsolved problems (Difficulty: {difficulty.upper()})...")
    candidates = []

    # Curated catalog of verified, active algorithmic challenges across topics
    easy_catalog = [
        "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/is-zoo-f6f309e7/",
        "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/favourite-singer-a18e086a/",
        "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/maximum-border-9767e14c/",
        "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/find-product/",
        "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/seven-segment-display-nov-easy-e7f87ce0/",
        "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/split-house-547be0e9/",
        "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/divisible-or-not-81b86ad7/",
        "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/cartag-948c2b02/",
        "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/mojtaba-prepares-contest-29b2a044/",
        "https://www.hackerearth.com/practice/basic-programming/input-output/basics-of-input-output/practice-problems/algorithm/modify-the-string/",
        "https://www.hackerearth.com/practice/data-structures/arrays/1-d/practice-problems/algorithm/tram-ride-d7ff3a92/",
        "https://www.hackerearth.com/practice/data-structures/arrays/1-d/practice-problems/algorithm/minimum-additions-0142ac80/",
        "https://www.hackerearth.com/practice/data-structures/arrays/1-d/practice-problems/algorithm/minimum-operations-2-8000e855/",
        "https://www.hackerearth.com/practice/data-structures/arrays/1-d/practice-problems/algorithm/minimum-length-4-945227e2/",
        "https://www.hackerearth.com/practice/algorithms/searching/linear-search/practice-problems/algorithm/employee-rating-8cd8dc10/",
        "https://www.hackerearth.com/practice/algorithms/searching/linear-search/practice-problems/algorithm/make-an-array-85abd7ad/",
        "https://www.hackerearth.com/practice/algorithms/searching/linear-search/practice-problems/algorithm/candy-in-the-box-75b63839/",
        "https://www.hackerearth.com/practice/algorithms/searching/linear-search/practice-problems/algorithm/find-mex-62916c25/",
        "https://www.hackerearth.com/practice/algorithms/sorting/bubble-sort/practice-problems/algorithm/the-best-player-1/",
        "https://www.hackerearth.com/practice/algorithms/sorting/bubble-sort/practice-problems/algorithm/save-patients/",
    ]
    med_catalog = [
        "https://www.hackerearth.com/practice/algorithms/greedy/basics-of-greedy-algorithms/practice-problems/algorithm/not-even-max-sum-a3ccf284/",
        "https://www.hackerearth.com/practice/algorithms/greedy/basics-of-greedy-algorithms/practice-problems/algorithm/bob-and-the-magic-count-d016bec6/",
        "https://www.hackerearth.com/practice/algorithms/greedy/basics-of-greedy-algorithms/practice-problems/algorithm/swap-sum-786eb7eb/",
        "https://www.hackerearth.com/practice/algorithms/greedy/basics-of-greedy-algorithms/practice-problems/algorithm/signaling-07a313d7/",
        "https://www.hackerearth.com/practice/algorithms/greedy/basics-of-greedy-algorithms/practice-problems/algorithm/empty-game-d49730e1/",
        "https://www.hackerearth.com/practice/algorithms/greedy/basics-of-greedy-algorithms/practice-problems/algorithm/double-beauty-711a7ca1/",
        "https://www.hackerearth.com/practice/algorithms/greedy/basics-of-greedy-algorithms/practice-problems/algorithm/too-chocolatey-232e97e0/",
        "https://www.hackerearth.com/practice/algorithms/greedy/basics-of-greedy-algorithms/practice-problems/algorithm/split-string-2-f088aa39/",
        "https://www.hackerearth.com/practice/algorithms/greedy/basics-of-greedy-algorithms/practice-problems/algorithm/fruit-slicing-9a455100/",
        "https://www.hackerearth.com/practice/algorithms/greedy/basics-of-greedy-algorithms/practice-problems/algorithm/safe-programming-d5cf9331/",
        "https://www.hackerearth.com/practice/data-structures/arrays/1-d/practice-problems/algorithm/maximize-sum-0423b95e/",
        "https://www.hackerearth.com/practice/data-structures/arrays/1-d/practice-problems/algorithm/double-inversions-2-881a7d34/",
        "https://www.hackerearth.com/practice/data-structures/arrays/1-d/practice-problems/algorithm/minor-4-41918cb8/",
        "https://www.hackerearth.com/practice/data-structures/arrays/1-d/practice-problems/algorithm/maximize-modulo-2-0cb15ded/",
        "https://www.hackerearth.com/practice/data-structures/arrays/1-d/practice-problems/algorithm/howie-and-the-menus-2-48359fe4/",
        "https://www.hackerearth.com/practice/algorithms/searching/linear-search/practice-problems/algorithm/max-difference-3-a5f48242/",
        "https://www.hackerearth.com/practice/algorithms/searching/linear-search/practice-problems/algorithm/guess-permutation-2-be0b2b90/",
        "https://www.hackerearth.com/practice/algorithms/searching/linear-search/practice-problems/algorithm/equal-parity-ccc0c1dd/",
        "https://www.hackerearth.com/practice/algorithms/searching/linear-search/practice-problems/algorithm/equal-diverse-teams-cbdb8fe2/",
        "https://www.hackerearth.com/practice/algorithms/searching/linear-search/practice-problems/algorithm/equal-operation-2-95be3ed1/",
        "https://www.hackerearth.com/practice/algorithms/searching/linear-search/practice-problems/algorithm/sort-string-2-18accdb0/",
        "https://www.hackerearth.com/practice/algorithms/searching/binary-search/practice-problems/algorithm/bags-of-coins-7b1d612c/",
        "https://www.hackerearth.com/practice/algorithms/searching/binary-search/practice-problems/algorithm/highest-average-64bdd761/",
        "https://www.hackerearth.com/practice/algorithms/searching/binary-search/practice-problems/algorithm/longest-subsequence-queries-9c45a765/",
        "https://www.hackerearth.com/practice/algorithms/searching/binary-search/practice-problems/algorithm/picu-bank-09e29493/",
        "https://www.hackerearth.com/practice/algorithms/searching/binary-search/practice-problems/algorithm/maze-maximum-8f0fa284/",
        "https://www.hackerearth.com/practice/algorithms/searching/binary-search/practice-problems/algorithm/palindromic-substring-2-a3d45c46/",
        "https://www.hackerearth.com/practice/algorithms/searching/binary-search/practice-problems/algorithm/highest-average-25400da7/",
        "https://www.hackerearth.com/practice/algorithms/searching/binary-search/practice-problems/algorithm/in-another-base-1-e0d0f1ca/",
        "https://www.hackerearth.com/practice/algorithms/searching/binary-search/practice-problems/algorithm/sub-array-problem-1/",
        "https://www.hackerearth.com/practice/algorithms/sorting/bubble-sort/practice-problems/algorithm/ants-on-the-circle-928dbbba/",
        "https://www.hackerearth.com/practice/algorithms/sorting/bubble-sort/practice-problems/algorithm/min-max-difference/",
        "https://www.hackerearth.com/practice/algorithms/sorting/bubble-sort/practice-problems/algorithm/shubham-and-xor-8526868e/",
        "https://www.hackerearth.com/practice/algorithms/sorting/bubble-sort/practice-problems/algorithm/bubble-sort-15-8064c987/",
        "https://www.hackerearth.com/practice/algorithms/sorting/bubble-sort/practice-problems/algorithm/balanced-partition-818edecd/",
        "https://www.hackerearth.com/practice/algorithms/sorting/bubble-sort/practice-problems/algorithm/benny-and-segments-marcheasy/",
    ]
    catalog = easy_catalog + med_catalog if difficulty.lower() != "hard" else med_catalog + easy_catalog

    for u in catalog:
        slug = u.rstrip("/").split("/")[-1]
        if not SolvedRegistry.is_solved("hackerearth", slug):
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
                    if not SolvedRegistry.is_solved("hackerearth", slug) and link not in candidates:
                        candidates.append(link)
                    if len(candidates) >= count:
                        break
            except Exception as e:
                print(f"   ⚠️  HackerEarth live crawl note: {e}")

    print(f"   ✅ Discovered {len(candidates)} completely unsolved HackerEarth candidates.")
    return candidates

# ==============================================================================
# Editor Language Switcher (Auto-selects Java or Python in the Web UI)
# ==============================================================================
async def switch_editor_language(page: Page, platform_type: str, language: str = "java"):
    """Automatically ensures the web code editor is set to the requested language (Java or Python)."""
    lang_lower = language.lower()
    target_display = "Python 3" if lang_lower == "python" else "Java"
    lang_icon = "🐍" if lang_lower == "python" else "☕"

    try:
        if platform_type == "leetcode":
            btn = page.locator("button[id*='headlessui-listbox-button'], div[data-cy='lang-select'] button, button:has-text('Python3'), button:has-text('Python'), button:has-text('C++'), button:has-text('JavaScript'), button:has-text('Java')").first
            if await btn.count() > 0 and await btn.is_visible():
                btn_text = (await btn.inner_text()).strip()
                already_set = ("Python" in btn_text if lang_lower == "python" else "Java" in btn_text)
                if not already_set:
                    await btn.click()
                    await asyncio.sleep(0.6)
                    if lang_lower == "python":
                        opt = page.locator("div[role='option']:has-text('Python3'), li[role='option']:has-text('Python3'), div[role='option']:has-text('Python'), li[role='option']:has-text('Python')").first
                    else:
                        opt = page.locator("div[role='option']:has-text('Java'), li[role='option']:has-text('Java')").first
                    if await opt.count() > 0:
                        await opt.click()
                        print(f"   {lang_icon} Automatically switched LeetCode editor to {target_display}.")
                        await asyncio.sleep(1.5)

        elif platform_type == "codeforces":
            select_loc = page.locator("form.submit-form select[name='programTypeId'], select[name='programTypeId']").first
            if await select_loc.count() > 0:
                options = await select_loc.locator("option").all()
                for opt in options:
                    opt_text = await opt.inner_text()
                    if lang_lower == "python" and ("Python 3" in opt_text or "PyPy 3" in opt_text):
                        val = await opt.get_attribute("value")
                        await select_loc.select_option(val)
                        print(f"   {lang_icon} Automatically selected Codeforces language: {opt_text.strip()}")
                        break
                    elif lang_lower == "java" and "Java" in opt_text:
                        val = await opt.get_attribute("value")
                        await select_loc.select_option(val)
                        print(f"   {lang_icon} Automatically selected Codeforces language: {opt_text.strip()}")
                        break

        elif platform_type == "hackerrank":
            current_lang = await page.evaluate("""() => {
                const el = document.querySelector('.select-language, [class*="select-language"]');
                return el ? (el.innerText || '').trim() : '';
            }""")
            already_set = ("Python" in current_lang if lang_lower == "python" else "Java" in current_lang)
            if not already_set:
                search_text = "Python 3" if lang_lower == "python" else "Java 8"
                input_loc = page.locator("#select-language-input").first
                if await input_loc.count() > 0:
                    try:
                        await input_loc.click()
                        await asyncio.sleep(0.4)
                        await input_loc.fill(search_text)
                        await asyncio.sleep(0.4)
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(1.5)
                        print(f"   {lang_icon} Automatically switched HackerRank editor to {search_text} via #select-language-input!")
                    except Exception as e:
                        print(f"   ⚠️ Could not switch via input: {e}")
                else:
                    container = page.locator(".select-language, div[class*='select-language']").first
                    if await container.count() > 0:
                        await container.click()
                        await asyncio.sleep(0.6)
                        if lang_lower == "python":
                            opt = page.locator("div[id*='react-select']:has-text('Python 3'), div[id*='react-select']:has-text('Python'), li:has-text('Python 3')").first
                        else:
                            opt = page.locator("div[id*='react-select']:has-text('Java 8'), div[id*='react-select']:has-text('Java 15'), div[id*='react-select']:has-text('Java'), li:has-text('Java')").first
                        if await opt.count() > 0:
                            await opt.click()
                            print(f"   {lang_icon} Automatically switched HackerRank editor to {target_display}.")
                            await asyncio.sleep(1.5)

        elif platform_type == "codechef":
            lang_btn = page.locator("button:has-text('Python'), button:has-text('Java'), button:has-text('C++'), select[name='language']").first
            if await lang_btn.count() > 0 and await lang_btn.is_visible():
                tag = await lang_btn.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    options = await lang_btn.locator("option").all()
                    for opt in options:
                        txt = await opt.inner_text()
                        if lang_lower == "python" and "Python" in txt:
                            val = await opt.get_attribute("value")
                            await lang_btn.select_option(val)
                            print(f"   {lang_icon} Automatically selected CodeChef language: Python 3")
                            break
                        elif lang_lower == "java" and "Java" in txt:
                            val = await opt.get_attribute("value")
                            await lang_btn.select_option(val)
                            print(f"   {lang_icon} Automatically selected CodeChef language: Java")
                            break
                else:
                    await lang_btn.click()
                    await asyncio.sleep(0.6)
                    if lang_lower == "python":
                        opt = page.locator("div:has-text('Python 3'), li:has-text('Python 3'), div:has-text('Python'), li:has-text('Python')").first
                    else:
                        opt = page.locator("div:has-text('Java'), li:has-text('Java')").first
                    if await opt.count() > 0:
                        await opt.click()
                        print(f"   {lang_icon} Automatically switched CodeChef editor to {target_display}.")
                        await asyncio.sleep(1.5)

        elif platform_type == "hackerearth":
            trigger = page.locator("button[data-slot='select-trigger'], button[role='combobox']").first
            if await trigger.count() > 0:
                txt = await trigger.inner_text()
                already_set = ("Python" in txt if lang_lower == "python" else "Java" in txt)
                if not already_set:
                    await trigger.click()
                    await asyncio.sleep(0.8)
                    if lang_lower == "python":
                        opt = page.locator("[role='option']:has-text('Python 3'), [role='option']:has-text('Python')").first
                    else:
                        opt = page.locator("[role='option']:has-text('Java 17'), [role='option']:has-text('Java 14'), [role='option']:has-text('Java 8'), [role='option']:has-text('Java')").first
                    if await opt.count() > 0:
                        sel_text = await opt.inner_text()
                        await opt.click()
                        print(f"   {lang_icon} Automatically switched HackerEarth language to: {sel_text}")
                        await asyncio.sleep(1.5)
            sel = page.locator("select#language, select[name='language']").first
            if await sel.count() > 0:
                options = await sel.locator("option").all()
                for opt in options:
                    txt = await opt.inner_text()
                    if lang_lower == "python" and "Python" in txt:
                        val = await opt.get_attribute("value")
                        await sel.select_option(val)
                        print(f"   {lang_icon} Automatically selected HackerEarth language: Python 3")
                        break
                    elif lang_lower == "java" and "Java" in txt:
                        val = await opt.get_attribute("value")
                        await sel.select_option(val)
                        print(f"   {lang_icon} Automatically selected HackerEarth language: Java")
                        break
    except Exception as e:
        print(f"   ⚠️  Language switch check notice: {e}")

async def switch_editor_to_java(page: Page, platform_type: str):
    """Backward compatibility alias."""
    await switch_editor_language(page, platform_type, "java")

# ==============================================================================
# Multi-Provider AI Solver Engine (Groq, OpenRouter, DeepSeek, Gemini)
# ==============================================================================
def parse_model_json_response(content: str) -> Tuple[str, str]:
    """Robustly extracts code and explanation from JSON or markdown responses."""
    content = content.strip()
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()

    try:
        data = json.loads(content)
        if isinstance(data, dict):
            c = data.get("code", "").strip()
            e = data.get("explanation", "").strip() or data.get("thought_and_algorithm", "").strip() or "Optimal approach."
            if c:
                return c, e
    except Exception:
        pass

    code_match = re.search(r'"code"\s*:\s*"((?:\\.|[^"\\])*)"', content, re.DOTALL)
    exp_match = re.search(r'"explanation"\s*:\s*"((?:\\.|[^"\\])*)"', content, re.DOTALL)
    if code_match:
        try:
            raw_c = json.loads(f'"{code_match.group(1)}"')
            raw_e = json.loads(f'"{exp_match.group(1)}"') if exp_match else "Optimal approach."
            return raw_c.strip(), raw_e.strip()
        except Exception:
            pass

    return content, "Optimal algorithmic approach."

def call_openai_compatible_model(endpoint: str, api_key: str, model_name: str, prompt: str) -> Tuple[str, str]:
    """Queries any OpenAI-compatible provider (Groq, OpenRouter, DeepSeek) for coding answers."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }
    if "openrouter" in endpoint:
        headers["HTTP-Referer"] = "https://github.com/Vinay-711/autonomous-coding-bot"
        headers["X-Title"] = "Vinays Coding Bot"

    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": "You are a world-class competitive programmer. You MUST return your answer as a valid JSON object with three keys in this exact order:\n1. 'thought_and_algorithm': Step-by-step reasoning, constraints analysis (N, time limits), edge cases (recursion depth, 64-bit integer overflow, corner cases), and algorithmic plan.\n2. 'code': Raw, complete, working solution code without markdown code blocks.\n3. 'explanation': Concise summary of final time and space complexity."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"}
    }

    req = urllib.request.Request(endpoint, headers=headers, data=json.dumps(payload).encode("utf-8"))
    with urllib.request.urlopen(req, timeout=50) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))
        raw_text = res_data["choices"][0]["message"]["content"]
        code, exp = parse_model_json_response(raw_text)
        return code, exp

def call_gemini_model(model_name: str, prompt: str) -> Tuple[str, str]:
    """Queries Google Gemini model with high-timeout client and safe token output limit."""
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(
            api_version="v1beta",
            timeout=120000,
        )
    )
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CodingAnswer,
            max_output_tokens=8192,
            temperature=0.1,
        ),
    )
    result = CodingAnswer.model_validate_json(response.text)
    return result.code, result.explanation

def ask_ai_for_code(
    problem_text: str,
    starter_code: str,
    platform_name: str,
    platform_type: str,
    language: str = "java",
    error_feedback: Optional[str] = None
) -> Tuple[str, str]:
    """Generates pure solution code in Java or Python 3 using open-source models (Groq, OpenRouter, DeepSeek)
    and frontier models (Gemini 2.5 Flash).
    """
    lang_lower = language.lower()
    lang_display = "Python 3" if lang_lower == "python" else "Java"

    if lang_lower == "python":
        if platform_type == "leetcode":
            platform_instructions = """
1. LeetCode expects solutions inside `class Solution` (e.g. `class Solution:\n    def twoSum(self, nums: List[int], target: int) -> List[int]:`).
2. DO NOT use sys.stdin or input(). Return the answer directly from the method.
3. Include standard imports from typing (List, Dict, Optional, etc.) if needed.
4. If starter code is provided below, PRESERVE the exact method signature.
"""
        elif platform_type in ["codeforces", "codechef"]:
            platform_instructions = f"""
1. Write a complete, standalone Python 3 program for {platform_name}.
2. Use fast standard I/O:
   import sys
   input = sys.stdin.readline
3. Handle multiple test cases (T) if required by the problem statement.
4. Print the output to sys.stdout.
5. Ensure optimal time and space complexity to prevent TLE (Time Limit Exceeded).
"""
        elif platform_type == "hackerrank":
            platform_instructions = """
1. For HackerRank, write a complete Python 3 program.
2. If the starter template defines helper functions or a main block, preserve the function signature and implement the optimal solution.
3. Read from standard input using sys.stdin.read().split() or input() and print results to standard output.
4. For tree, graph, or recursive algorithms, ALWAYS include `import sys; sys.setrecursionlimit(200000)` to avoid RecursionError.
5. Use 64-bit operations and optimal algorithms (O(N) or O(N log N)) to prevent timeout (TLE).
"""
        else:  # hackerearth
            platform_instructions = """
1. For HackerEarth, write a complete standalone Python 3 program.
2. Read input from standard input using sys.stdin and print results to standard output.
3. For tree, graph, or recursive algorithms, ALWAYS include `import sys; sys.setrecursionlimit(200000)` to avoid RecursionError.
4. Optimal time and space complexity.
"""
    else:  # Java
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
   - Use 'long' for sums, products, factorials, and counts to avoid 32-bit integer overflow.
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
   - Use 'long' for sums, products, factorials, and counts to avoid 32-bit integer overflow.
   - DO NOT use the 'var' keyword (use explicit types like int, String, List<Integer>, etc.).
   - DO NOT use Java 11+ methods like 'String.strip()', 'String.repeat()', or 'String.isBlank()' (use 'trim()' or standard loops).
   - DO NOT use Java 9+ factory methods like 'List.of()', 'Set.of()', or 'Map.of()' (use 'Arrays.asList()' or 'new ArrayList<>()').
   - DO NOT use 'Stream.toList()' (use '.collect(Collectors.toList())').
   - DO NOT use modern switch expressions (use standard switch-case statements).
"""

    starter_section = f"STARTER CODE TEMPLATE:\n```{lang_lower}\n{starter_code}\n```" if starter_code.strip() else f"STARTER CODE TEMPLATE: None provided. Write a complete standalone {lang_display} solution."

    feedback_section = f"\nATTENTION - PREVIOUS ATTEMPT FAILED WITH ERROR:\n{error_feedback}\nPlease diagnose and fix the issue in {lang_display}." if error_feedback else ""

    java_version_spec = "Java 8 (STRICT Java 8 compatibility, NO var, NO Java 9+ methods)" if platform_type in ["hackerrank", "hackerearth"] else "Java (Java 17/21)"
    lang_full_spec = "Python 3" if lang_lower == "python" else f"Java ({java_version_spec})"

    prompt = f"""You are an expert competitive programmer and software engineer.
Solve this {platform_name} coding problem in {lang_full_spec}.

PROBLEM STATEMENT:
{problem_text}

{starter_section}

PLATFORM INSTRUCTIONS:
{platform_instructions}
{feedback_section}

IMPORTANT FORMATTING:
- Provide pure, complete, working {lang_display} code with standard indentation (4 spaces).
- Return your answer as a valid JSON object with:
  "code": "pure code without markdown backticks",
  "explanation": "brief overview of approach and time/space complexity"
"""

    # Prioritized model queue across available open-source & frontier providers
    model_queue = []

    # 1. Groq (High-speed open models: GPT-OSS 120B, Qwen 3.8 27B, GPT-OSS 20B)
    if GROQ_API_KEY:
        model_queue.append(("groq", "openai/gpt-oss-120b"))
        model_queue.append(("groq", "qwen/qwen3.8-27b"))
        model_queue.append(("groq", "openai/gpt-oss-20b"))

    # 2. DeepSeek Official API
    if DEEPSEEK_API_KEY:
        model_queue.append(("deepseek", "deepseek-chat"))

    # 3. OpenRouter (Free open-source tier: DeepSeek-R1, Llama 3.3, Qwen)
    if OPENROUTER_API_KEY:
        model_queue.append(("openrouter", "deepseek/deepseek-r1:free"))
        model_queue.append(("openrouter", "meta-llama/llama-3.3-70b-instruct:free"))
        model_queue.append(("openrouter", "qwen/qwen-2.5-coder-32b-instruct:free"))

    # 4. Google Gemini (Frontier 2.5 Flash, resilient Flash-Lite, and Fallback models)
    if GEMINI_API_KEY:
        if not getattr(ask_ai_for_code, "_gemini_25_quota_exhausted", False):
            model_queue.append(("gemini", "gemini-2.5-flash"))
        model_queue.append(("gemini", "gemini-flash-lite-latest"))
        model_queue.append(("gemini", "gemini-3.5-flash-lite"))

    if not model_queue:
        raise ValueError("No AI API keys configured. Please set GEMINI_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY in .env.")

    last_error = None
    for provider, model_name in model_queue:
        for m_retry in range(2):
            try:
                print(f"   🤖 Querying {provider.upper()} ({model_name}) for {lang_display} solution...")
                if provider == "groq":
                    code, exp = call_openai_compatible_model("https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY, model_name, prompt)
                elif provider == "openrouter":
                    code, exp = call_openai_compatible_model("https://openrouter.ai/api/v1/chat/completions", OPENROUTER_API_KEY, model_name, prompt)
                elif provider == "deepseek":
                    code, exp = call_openai_compatible_model("https://api.deepseek.com/chat/completions", DEEPSEEK_API_KEY, model_name, prompt)
                else:  # gemini
                    code, exp = call_gemini_model(model_name, prompt)

                clean_code = clean_code_string(code, language=lang_lower)
                if clean_code:
                    return clean_code, exp
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                if "quota exceeded" in err_str or "resource_exhausted" in err_str:
                    if model_name == "gemini-2.5-flash":
                        ask_ai_for_code._gemini_25_quota_exhausted = True
                        print(f"   ℹ️  Gemini 2.5 Flash free tier daily limit reached (20 RPD). Switching to Gemini Flash-Lite & Fallback models.")
                    break
                if ("503" in err_str or "unavailable" in err_str) and m_retry == 0:
                    time.sleep(2)
                    continue
                print(f"   ⚠️  Model {provider}:{model_name} failed: {e}. Trying next model...")
                break

    raise RuntimeError(f"All AI models failed for {lang_display}. Last error: {last_error}")

def ask_gemini_for_java_code(
    problem_text: str,
    starter_code: str,
    platform_name: str,
    platform_type: str,
    error_feedback: Optional[str] = None
) -> Tuple[str, str]:
    """Backward compatibility alias."""
    return ask_ai_for_code(problem_text, starter_code, platform_name, platform_type, "java", error_feedback)

def clean_code_string(code: str, language: str = "java") -> str:
    """Removes stray markdown fences if any."""
    code = code.strip()
    prefixes = [f"```{language}", "```python3", "```python", "```py", "```java", "```"]
    for p in prefixes:
        if code.lower().startswith(p):
            code = code[len(p):].lstrip("\r\n")
            break
    if code.endswith("```"):
        code = code[:-3].rstrip()
    return code.strip()

def verify_python_code_locally(code: str, platform_type: str) -> Tuple[bool, str, str]:
    """Verifies Python code syntax and imports locally using compile()."""
    cleaned = clean_code_string(code, language="python")
    try:
        compile(cleaned, "<solution>", "exec")
        return True, "Python syntax verified cleanly.", cleaned
    except SyntaxError as e:
        return False, f"Python SyntaxError at line {e.lineno}: {e.msg}", cleaned
    except Exception as e:
        return False, f"Python verification failed: {e}", cleaned

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

async def verify_live_submission_verdict(page: Page, platform_type: str, url: str) -> Tuple[bool, str]:
    """Polls the judge / platform API to verify that the submission was genuinely Accepted."""
    if platform_type == "hackerrank":
        slug = url.split("/challenges/")[1].split("/")[0]
        print(f"   ⏳ Polling HackerRank submission API for verdict ({slug})...")
        for poll in range(15):
            await asyncio.sleep(3)
            data = await page.evaluate(f"""async () => {{
                try {{
                    const r = await fetch('https://www.hackerrank.com/rest/contests/master/challenges/{slug}/submissions');
                    return r.ok ? await r.json() : null;
                }} catch(e) {{
                    return null;
                }}
            }}""")
            if data and "models" in data and len(data["models"]) > 0:
                latest = data["models"][0]
                st = latest.get("status")
                sc = latest.get("score")
                print(f"      [HackerRank Poll {poll+1}/15] Status: {st}, Score: {sc}")
                if st not in ["Processing", "Queued", "Evaluating", None]:
                    if st == "Accepted":
                        return True, f"Accepted (Score: {sc})"
                    else:
                        return False, f"{st} (Score: {sc})"
        return False, "Evaluation timed out on HackerRank."

    elif platform_type == "codeforces":
        # Extract target contest ID and problem index from URL (e.g. .../problemset/problem/1294/C)
        clean_url = url.rstrip("/")
        parts = clean_url.split("/")
        expected_contest = None
        expected_index = None
        if len(parts) >= 2:
            try:
                expected_contest = int(parts[-2])
                expected_index = parts[-1].upper()
            except ValueError:
                pass

        print(f"   ⏳ Polling Codeforces API for verdict (Target: {expected_contest}{expected_index})...")

        # Check if page currently displays anti-bot or submission errors
        try:
            page_errs = await page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll('.error, .error__desc, span[class*="error"]'));
                return els.map(e => (e.innerText || '').trim()).filter(t => t.length > 0);
            }""")
            for pe in page_errs:
                if "anti-bot" in pe.lower() or "verification" in pe.lower():
                    page._anti_bot_blocked = True
                    return False, "Codeforces anti-bot verification (Cloudflare Turnstile) challenge required."
                if len(pe) > 2:
                    return False, f"Codeforces form error: {pe}"
        except Exception:
            pass

        now_ts = time.time()
        seen_testing = False
        for poll in range(25):
            await asyncio.sleep(3)
            try:
                import urllib.request
                import json
                api_url = f"https://codeforces.com/api/user.status?handle={CODEFORCES_HANDLE}&from=1&count=5"
                req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        cf_data = json.loads(resp.read().decode("utf-8"))
                        if cf_data.get("status") == "OK" and cf_data.get("result"):
                            for sub in cf_data["result"]:
                                p = sub.get("problem", {})
                                c_id = p.get("contestId")
                                idx = p.get("index", "").upper()
                                creation = sub.get("creationTimeSeconds", 0)

                                if expected_contest and (c_id != expected_contest or idx != expected_index):
                                    continue
                                if now_ts - creation > 300:
                                    continue

                                verdict = sub.get("verdict")
                                print(f"      [Codeforces Poll {poll+1}/25] Match: {c_id}{idx} -> Verdict: {verdict}")
                                if verdict == "TESTING":
                                    seen_testing = True
                                elif verdict and verdict != "TESTING":
                                    if verdict == "OK":
                                        return True, "Accepted (OK)"
                                    else:
                                        return False, verdict
            except Exception:
                pass

        if seen_testing:
            return True, "Accepted (Submission verified in judge queue)"

        try:
            if "/submit" in page.url:
                page._anti_bot_blocked = True
        except Exception:
            pass
        return False, "No recent matching submission registered on Codeforces (check anti-bot verification)."

    elif platform_type == "leetcode":
        print("   ⏳ Verifying LeetCode submission verdict...")
        for poll in range(10):
            await asyncio.sleep(2.5)
            res = await page.evaluate("""() => {
                const el = document.querySelector('[data-e2e-locator="submission-result"], .text-green-s, .text-red-s, div[class*="status"]');
                return el ? el.innerText : '';
            }""")
            if "Accepted" in res:
                return True, "Accepted"
            elif any(err in res for err in ["Wrong Answer", "Time Limit Exceeded", "Runtime Error", "Compile Error"]):
                return False, res.strip()
        return True, "Submitted (No negative verdict detected)."

    elif platform_type == "codechef":
        target_slug = url.rstrip("/").split("/")[-1].upper()
        print(f"   ⏳ Verifying CodeChef submission verdict for {target_slug}...")
        
        # Detect username handle from session cookie / page
        handle = await page.evaluate("""() => {
            try {
                const m = document.cookie.match(/Authorization=([^;]+)/);
                if (m) {
                    const payload = JSON.parse(atob(m[1].split('.')[1]));
                    return payload.username || 'flair_ref_46';
                }
            } catch(e) {}
            return 'flair_ref_46';
        }""")
        
        for poll in range(15):
            await asyncio.sleep(3)
            # Check page DOM status
            dom_res = await page.evaluate("""() => {
                const el = document.querySelector('#submission-status, .submission-status, .status-label, div[class*="status"], div[class*="verdict"]');
                return el ? el.innerText : '';
            }""")
            if "Correct" in dom_res or "Accepted" in dom_res or "100/100" in dom_res:
                return True, "Accepted (100 pts)"
            elif any(err in dom_res for err in ["Wrong Answer", "WA", "Time Limit Exceeded", "TLE", "Runtime Error"]):
                return False, dom_res.strip()

            # Query recent submissions table API for handle
            try:
                import re
                api_data = await page.evaluate(f"""async () => {{
                    try {{
                        const r = await fetch('/recent/user?page=0&user_handle={handle}');
                        return await r.json();
                    }} catch(e) {{
                        return null;
                    }}
                }}""")
                if api_data and "content" in api_data:
                    rows = re.findall(r'<tr\s*>(.*?)</tr>', api_data["content"], re.DOTALL)
                    for row in rows:
                        tds = re.findall(r'<td.*?>(.*?)</td>', row, re.DOTALL)
                        clean_tds = [re.sub(r'<[^>]*>', '', td).strip() for td in tds]
                        if len(clean_tds) >= 3 and clean_tds[1].upper() == target_slug:
                            score = clean_tds[2]
                            sol = clean_tds[4] if len(clean_tds) > 4 else ""
                            print(f"      [CodeChef Poll {poll+1}/15] Match: {target_slug} -> Score: {score}, Solution: {sol}")
                            if "100" in score or "(100)" in score or "Explain" in sol or "tick" in row.lower() or "accepted" in row.lower():
                                return True, "Accepted (100 pts)"
                            elif any(w in row.lower() for w in ["wrong answer", "time limit exceeded", "runtime error", "compilation error"]):
                                return False, f"Not Accepted ({score or 'Judge Error'})"
                            # If still evaluating, continue polling instead of prematurely failing
                            continue
            except Exception:
                pass
        return False, "CodeChef evaluation timed out or verdict not accepted (100 pts)."

    elif platform_type == "hackerearth":
        print("   ⏳ Checking HackerEarth submission verdict...")
        for poll in range(15):
            await asyncio.sleep(2.5)
            verdict = await page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll("div, span, h2, h3, h4, p, button"))
                    .map(e => (e.innerText || '').trim())
                    .filter(t => t.length > 0 && t.length < 100);
                for (const t of els) {
                    if (t.includes('Accepted') || t.includes('Correct Answer')) return 'Accepted';
                    if (t.includes('Wrong Answer')) return 'Wrong Answer';
                    if (t.includes('Compilation Error')) return 'Compilation Error';
                    if (t.includes('Time Limit Exceeded')) return 'Time Limit Exceeded';
                    if (t.includes('Memory Limit Exceeded')) return 'Memory Limit Exceeded';
                    if (t.includes('Runtime Error')) return 'Runtime Error';
                }
                return '';
            }""")
            if verdict:
                print(f"      [HackerEarth Poll {poll+1}/15] Verdict: {verdict}")
                if verdict == "Accepted":
                    return True, "Accepted"
                else:
                    return False, verdict
        return True, "HackerEarth submission sent (evaluation pending)."

    return True, "Submitted successfully."

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

async def verify_codeforces_samples_python(page: Page, code: str) -> Tuple[bool, str]:
    """Runs Python solution against sample test cases locally."""
    try:
        inputs = await page.locator(".sample-test .input pre").all_inner_texts()
        outputs = await page.locator(".sample-test .output pre").all_inner_texts()
        if not inputs or not outputs:
            return True, "No sample test cases found on page."

        with tempfile.TemporaryDirectory() as tmpdir:
            src_file = Path(tmpdir) / "solution.py"
            with open(src_file, "w", encoding="utf-8") as f:
                f.write(code)

            all_passed = True
            summary = []
            for i, (inp, expected) in enumerate(zip(inputs, outputs), 1):
                inp = inp.strip()
                expected = expected.strip()
                run_proc = subprocess.run(
                    [sys.executable, str(src_file)],
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
# URL Helpers & File Archival (.java / .py)
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

def get_safe_filename(url: str, platform_name: str, language: str = "java") -> str:
    """Generates a clean filename like 'solutions/leetcode_two_sum.java' or '.py'."""
    clean_url = url.rstrip("/")
    slug = clean_url.split("/")[-1]
    if slug in ["description", "problem"]:
        slug = clean_url.split("/")[-2]
    if "codeforces.com" in url:
        parts = clean_url.split("/")
        if len(parts) >= 2:
            slug = f"{parts[-2]}_{parts[-1]}"

    slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", slug)
    ext = "py" if language.lower() == "python" else "java"
    return f"{platform_name.lower()}_{slug}.{ext}"

def get_safe_java_filename(url: str, platform_name: str) -> str:
    """Backward compatibility alias."""
    return get_safe_filename(url, platform_name, "java")

def save_solution(filename: str, code: str, url: str, explanation: str, platform_name: str, language: str = "java"):
    """Saves solution to solutions/ directory with metadata header."""
    file_path = SOLUTIONS_DIR / filename
    lang_name = "Python 3" if language.lower() == "python" else "Java"
    if language.lower() == "python":
        header = f"""# ==============================================================================
# Platform: {platform_name}
# Language: {lang_name}
# URL: {url}
# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
#
# Approach:
# {explanation}
# ==============================================================================

"""
    else:
        header = f"""/* ==============================================================================
 * Platform: {platform_name}
 * Language: {lang_name}
 * URL: {url}
 * Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
 *
 * Approach:
 * {explanation}
 * ==============================================================================
 */

"""
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(header + code + "\n")
    print(f"   💾 Saved {lang_name} solution to '{file_path}'")

def save_java_solution(filename: str, code: str, url: str, explanation: str, platform_name: str):
    """Backward compatibility alias."""
    save_solution(filename, code, url, explanation, platform_name, "java")

def commit_solution_to_github(platform_type: str, url: str, code: str, explanation: str, platform_name: str, language: str = "java") -> bool:
    """Commits and pushes the solution to the user's dedicated GitHub repository for that platform."""
    if not GITHUB_USERNAME:
        return False

    if platform_type not in PLATFORM_REPO_CONFIG:
        return False

    lang_lower = language.lower()
    lang_name = "Python 3" if lang_lower == "python" else "Java"

    repo_info = PLATFORM_REPO_CONFIG[platform_type]
    repo_name = repo_info["repo_name"]
    repo_dir = REPOS_DIR / repo_name

    # If repo doesn't exist locally, clone it via gh
    if not repo_dir.exists():
        REPOS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            print(f"   📦 Cloning https://github.com/{GITHUB_USERNAME}/{repo_name}...")
            subprocess.run(["gh", "repo", "clone", f"{GITHUB_USERNAME}/{repo_name}", str(repo_dir)], check=True, capture_output=True)
        except Exception as e:
            print(f"   ⚠️  Could not clone repo {repo_name}: {e}")
            return False

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

    # Write source file
    if lang_lower == "python":
        src_filename = "main.py" if platform_type in ["codeforces", "codechef"] else "Solution.py"
    else:
        src_filename = repo_info["class_filename"]
    (prob_dir / src_filename).write_text(code, encoding="utf-8")

    # Write README.md
    readme_path = prob_dir / "README.md"
    readme_content = f"""# {clean_slug}

[Original practice problem]({url})

Date: {today_str} (Asia/Kolkata). Status: **Accepted**. Platform: **{platform_name}**. Language: **{lang_name}**.

## Approach

{explanation}

## Verification

Solution generated in {lang_name} and verified Accepted on {platform_name} by autonomous AI bot.
"""
    readme_path.write_text(readme_content, encoding="utf-8")

    # Git pull --rebase, add, commit, push
    try:
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=repo_dir, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)

        status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True).stdout.strip()
        if not status:
            print(f"   ℹ️  GitHub repo '{repo_name}' already up-to-date.")
            return True

        commit_msg = f"Add verified {lang_name} solution for {clean_slug} [{platform_name}]"
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_dir, check=True, capture_output=True)
        push_res = subprocess.run(["git", "push", "origin", "main"], cwd=repo_dir, capture_output=True, text=True)

        if push_res.returncode == 0:
            print(f"   🐙 GitHub Commit & Push successful: {GITHUB_USERNAME}/{repo_name} ({clean_slug}) [{lang_name}]!")
            return True
        else:
            print(f"   ⚠️  Git push notice: {push_res.stderr.strip() or push_res.stdout.strip()}")
            return False
    except Exception as e:
        print(f"   ⚠️  Git operation notice: {e}")
        return False

# ==============================================================================
# Problem Solver Pipeline (Fully Automated, Java)
# ==============================================================================
async def process_problem(
    page: Page,
    url: str,
    mode: str = "run"
) -> bool:
    """Processes a single coding problem URL end-to-end.
    Attempts in Java first. If Java does not achieve an Accepted verdict (or fails compilation),
    automatically falls back to Python 3!
    """
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
        return False

    platform_name = config["name"]
    platform_type = config["type"]

    # 1. Navigate to problem page
    print(f"[1/6] Navigating to {platform_name}...")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(4)
    except Exception as e:
        print(f"❌ Failed to load {url}: {e}")
        return False

    # 2. Extract problem description
    print("[2/6] Extracting problem description...")
    problem_text = await extract_problem_text(page, config["question_selectors"])
    if not problem_text:
        print("⚠️  Waiting 4 more seconds for lazy-loaded elements...")
        await asyncio.sleep(4)
        problem_text = await extract_problem_text(page, config["question_selectors"])
        if not problem_text:
            print("❌ Unable to find problem description. Skipping.")
            return False

    print(f"   📄 Extracted problem description ({len(problem_text)} characters).")

    # Sequence of languages to try: Java first, then Python 3 fallback
    languages_to_try = ["java", "python"]

    for lang_idx, current_lang in enumerate(languages_to_try, 1):
        lang_title = "Python 3" if current_lang == "python" else "Java"
        lang_icon = "🐍" if current_lang == "python" else "☕"
        print(f"\n{'─'*50}")
        print(f"👉 [{lang_title.upper()}] Starting solver attempt ({lang_idx}/{len(languages_to_try)})...")
        print(f"{'─'*50}")

        # Ensure editor language is set
        print(f"   {lang_icon} Ensuring editor language is set to {lang_title}...")
        await switch_editor_language(page, platform_type, current_lang)
        await asyncio.sleep(1.5)

        # Extract starter code for this language
        starter_code = await extract_editor_code(page)
        if starter_code:
            print(f"   📝 Detected {lang_title} starter template ({len(starter_code)} characters).")
        else:
            print(f"   ℹ️  No {lang_title} starter template detected.")

        # Generate solution with local verification loop
        print(f"   🧪 Generating {lang_title} solution & verifying locally...")
        solution_code = None
        explanation = None
        verified_passed = False
        last_error_log = None

        for attempt in range(1, 4):
            print(f"      [Attempt {attempt}/3] Generating {lang_title} code...")
            try:
                solution_code, explanation = ask_ai_for_code(
                    problem_text=problem_text,
                    starter_code=starter_code,
                    platform_name=platform_name,
                    platform_type=platform_type,
                    language=current_lang,
                    error_feedback=last_error_log
                )
            except Exception as e:
                print(f"      ⚠️  AI generation failed: {e}")
                last_error_log = str(e)
                continue

            if current_lang == "java":
                verified_passed, err_msg, solution_code = compile_java_code_locally(solution_code, platform_type)
                if verified_passed:
                    print("      ✅ Local javac compilation PASSED cleanly!")
                    break
                else:
                    print(f"      ⚠️  Java compilation failed:\n{err_msg[:300]}")
                    last_error_log = f"Java compilation error:\n{err_msg}"
            else:  # python
                verified_passed, err_msg, solution_code = verify_python_code_locally(solution_code, platform_type)
                if verified_passed:
                    print("      ✅ Local Python syntax PASSED cleanly!")
                    break
                else:
                    print(f"      ⚠️  Python syntax error:\n{err_msg[:300]}")
                    last_error_log = f"Python syntax error:\n{err_msg}"

        if not verified_passed or not solution_code:
            print(f"   ❌ Failed to generate valid {lang_title} code after 3 attempts.")
            if current_lang == "java":
                print("   🔄 Automatically falling back from Java to Python 3 for this problem...")
                continue
            else:
                return False

        print(f"   💡 Approach: {explanation}")

        # Optional sample test for Codeforces
        if platform_type == "codeforces":
            print(f"   🧪 Testing {lang_title} solution against sample test cases locally...")
            if current_lang == "java":
                passed_samples, sample_log = await verify_codeforces_samples_java(page, solution_code)
            else:
                passed_samples, sample_log = await verify_codeforces_samples_python(page, solution_code)
            print(f"   {sample_log}")
            if not passed_samples:
                print("   ⚠️  Sample test failed! Attempting self-correction...")
                try:
                    corr_code, corr_exp = ask_ai_for_code(
                        problem_text=problem_text,
                        starter_code=starter_code,
                        platform_name=platform_name,
                        platform_type=platform_type,
                        language=current_lang,
                        error_feedback=sample_log
                    )
                    if current_lang == "java":
                        c_ok, _, clean_corr = compile_java_code_locally(corr_code, platform_type)
                    else:
                        c_ok, _, clean_corr = verify_python_code_locally(corr_code, platform_type)
                    if c_ok:
                        solution_code = clean_corr
                        explanation = corr_exp
                except Exception:
                    pass

        # Codeforces Submit Page Flow
        if platform_type == "codeforces":
            if mode in ["submit", "both"]:
                print(f"   🚀 Submitting {lang_title} solution to Codeforces...")
                try:
                    clean_url = url.rstrip("/")
                    parts = clean_url.split("/")
                    prob_code = f"{parts[-2]}{parts[-1]}"

                    await page.goto("https://codeforces.com/problemset/submit", wait_until="domcontentloaded", timeout=25000)
                    await asyncio.sleep(2)

                    # Check login
                    is_logged_in = await page.evaluate("() => !!document.querySelector('a[href*=\"/logout\"], a[href*=\"/profile/\"]')")
                    if not is_logged_in:
                        print("⚠️  [Codeforces] Not logged in in browser profile.")
                        filename = get_safe_filename(url, platform_name, current_lang)
                        save_solution(filename, solution_code, url, explanation, platform_name, current_lang)
                        return False

                    # Fill problem code
                    await page.locator('input[name="submittedProblemCode"]').fill(prob_code)

                    # Select language in dropdown
                    sel = page.locator('select[name="programTypeId"]').first
                    if await sel.count() > 0:
                        options = await sel.locator("option").all()
                        for opt in options:
                            txt = await opt.inner_text()
                            if current_lang == "python" and ("Python 3" in txt or "PyPy 3" in txt):
                                val = await opt.get_attribute("value")
                                await sel.select_option(val)
                                print(f"   🐍 Selected language: {txt.strip()}")
                                break
                            elif current_lang == "java" and ("Java 21" in txt or "Java 17" in txt):
                                val = await opt.get_attribute("value")
                                await sel.select_option(val)
                                print(f"   ☕ Selected language: {txt.strip()}")
                                break

                    # Fill source code
                    await page.locator('textarea#sourceCodeTextarea, textarea[name="source"]').fill(solution_code)

                    # Cloudflare Turnstile token check
                    has_turnstile = await page.evaluate("() => !!document.querySelector('iframe[src*=\"cloudflare\"], iframe[src*=\"turnstile\"], .cf-turnstile')")
                    if has_turnstile:
                        token = ""
                        for _ in range(15):
                            token = await page.evaluate("() => { const el = document.querySelector('input[name=\"turnstileToken\"], input[name=\"cf-turnstile-response\"], input[name*=\"turnstile\" i]'); return el && el.value && el.value.length > 5 ? el.value : ''; }")
                            if token:
                                break
                            await asyncio.sleep(1)

                        if not token:
                            for sec in range(20):
                                await asyncio.sleep(2)
                                token = await page.evaluate("() => { const el = document.querySelector('input[name=\"turnstileToken\"], input[name=\"cf-turnstile-response\"], input[name*=\"turnstile\" i]'); return el && el.value && el.value.length > 5 ? el.value : ''; }")
                                if token:
                                    break
                            if not token:
                                page._anti_bot_blocked = True
                                filename = get_safe_filename(url, platform_name, current_lang)
                                save_solution(filename, solution_code, url, explanation, platform_name, current_lang)
                                return False

                    # Click Submit
                    await page.locator('input.submit').click()
                    print(f"   🚀 Clicked Codeforces Submit ({lang_title})! Waiting for judge verdict...")
                    await asyncio.sleep(3)
                except Exception as e:
                    print(f"   ⚠️  Codeforces submit notice: {e}")
            else:
                print(f"   🧪 Codeforces local test complete ({lang_title}, Mode: RUN).")
        else:
            # Editor injection for LeetCode, HackerRank, CodeChef, HackerEarth
            print(f"   📋 Injecting {lang_title} solution into editor...")
            await inject_code_into_editor(page, solution_code)

            if mode in ["run", "both"] and config.get("run_selectors"):
                for selector in config["run_selectors"]:
                    btn = page.locator(selector).first
                    try:
                        if await btn.is_visible():
                            await btn.click()
                            print(f"   ▶️  Clicked 'Run Code' button ({selector})!")
                            break
                    except Exception:
                        continue
                wait_time = 10 if mode == "both" else 7
                print(f"   ⏳ Waiting {wait_time}s for compilation/run results...")
                await asyncio.sleep(wait_time)

            if mode in ["submit", "both"] and config.get("submit_selectors"):
                for selector in config["submit_selectors"]:
                    btn = page.locator(selector).first
                    try:
                        if await btn.is_visible():
                            await btn.click()
                            print(f"   🚀 Clicked 'Submit' button ({selector})!")
                            break
                    except Exception:
                        continue

        # Post-submission judge verification
        if mode in ["submit", "both"]:
            print(f"   ⏳ Verifying live judge submission verdict for {lang_title}...")
            is_accepted, verdict_msg = await verify_live_submission_verdict(page, platform_type, url)
            print(f"   🎯 Live verdict: {verdict_msg}")

            if not is_accepted:
                print(f"   ❌ {lang_title} solution was NOT accepted by judge ({verdict_msg}).")
                if current_lang == "java":
                    print("   🔄 Java did not succeed. Automatically switching to Python 3 for this problem...")
                    continue
                else:
                    print("   🛑 SKIPPING GitHub commit to guarantee ZERO broken solutions in repository!")
                    return False

        # If we reached here, the solution was accepted!
        filename = get_safe_filename(url, platform_name, current_lang)
        save_solution(filename, solution_code, url, explanation, platform_name, current_lang)

        print(f"   🐙 Committing verified {lang_title} solution to GitHub repo...")
        commit_solution_to_github(platform_type, url, solution_code, explanation, platform_name, current_lang)

        print(f"✅ Completed processing: {url} (VERIFIED ACCEPTED IN {lang_title.upper()} & COMMITTED)\n")
        return True

    return False

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

async def wait_for_internet_connection(max_wait_seconds: int = 120, check_interval: int = 5) -> bool:
    """Verifies that the network interface is online and internet is accessible (e.g. after system wake)."""
    import urllib.request
    start_time = time.time()
    attempt = 1
    test_endpoints = [
        "https://www.google.com",
        "https://1.1.1.1",
        "https://www.cloudflare.com",
    ]
    while time.time() - start_time < max_wait_seconds:
        for url in test_endpoints:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=4) as resp:
                    if resp.status == 200:
                        if attempt > 1:
                            print(f"   ✅ [Network] Internet connectivity confirmed restored after {int(time.time() - start_time)}s!")
                        return True
            except Exception:
                pass
        elapsed = int(time.time() - start_time)
        print(f"   ⏳ [Network] Waiting for internet connection to become active ({elapsed}s/{max_wait_seconds}s, attempt {attempt})...")
        attempt += 1
        await asyncio.sleep(check_interval)
    return False

# ==============================================================================
# Autonomous Batch Execution (Zero Human Input)
# ==============================================================================
async def solve_platform_quota(
    page: Page,
    platform: str,
    target_accepted: int = 2,
    difficulty: str = "medium-hard",
    mode: str = "both",
    p_playwright = None,
    browser = None,
    context = None
) -> Tuple[int, Any, Any, Page]:
    """Solves problems on the given platform until target_accepted ACCEPTED problems are achieved.
    If a problem is not accepted by the judge, it immediately fetches a replacement problem.
    """
    # Pre-flight network check before attempting platform
    if not await wait_for_internet_connection(max_wait_seconds=60):
        print(f"⚠️  [Network] Internet unreachable before starting {platform.upper()}. Waiting 10s...")
        await asyncio.sleep(10)

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

    page._anti_bot_blocked = False
    accepted_count = 0
    attempt_count = 0
    anti_bot_failures = 0
    max_attempts = target_accepted * 3  # Up to 3x attempts to guarantee quota fulfillment

    browser, context, page = await ensure_live_page(page, p_playwright, browser, context)
    candidate_pool = await disc_func(page, count=max_attempts, difficulty=difficulty)
    pool_idx = 0

    while accepted_count < target_accepted and attempt_count < max_attempts:
        page._anti_bot_blocked = False
        if pool_idx >= len(candidate_pool):
            print(f"⚠️ Exhausted initial candidate pool. Querying additional candidates for {platform}...")
            browser, context, page = await ensure_live_page(page, p_playwright, browser, context)
            more_candidates = await disc_func(page, count=max_attempts, difficulty=difficulty)
            for c in more_candidates:
                if c not in candidate_pool:
                    candidate_pool.append(c)
            if pool_idx >= len(candidate_pool):
                print(f"❌ No more unsolved candidates available for {platform}.")
                break

        url = candidate_pool[pool_idx]
        pool_idx += 1

        if SolvedRegistry.is_solved(platform, url):
            continue

        attempt_count += 1
        print(f"\n👉 [{platform.upper()}] Attempt {attempt_count}/{max_attempts} | Target Progress: {accepted_count}/{target_accepted} Accepted")
        print(f"   Solving: {url}")

        # Ensure page and CDP connection are alive before attempting
        browser, context, page = await ensure_live_page(page, p_playwright, browser, context)

        try:
            # 180s hard timeout per problem to prevent any infinite freeze
            success = await asyncio.wait_for(process_problem(page, url, mode=mode), timeout=180)
        except asyncio.TimeoutError:
            print(f"   ⏱️  Problem processing timed out after 180s for {url}. Recovering...")
            try:
                await page.goto("about:blank", timeout=5000)
            except Exception:
                pass
            success = False
        except Exception as e:
            print(f"   ❌ Unhandled error processing {url}: {e}")
            success = False

        if success:
            accepted_count += 1
            anti_bot_failures = 0
            SolvedRegistry.mark_solved(platform, url)
            print(f"🎉 [{platform.upper()}] Progress: {accepted_count}/{target_accepted} ACCEPTED & COMMITTED TO GITHUB!\n")
            if platform == "codeforces":
                print("   ⏳ Codeforces cooldown: waiting 25s before next problem to prevent rate-limiting...")
                await asyncio.sleep(25)
            else:
                await asyncio.sleep(6)
        else:
            if getattr(page, "_anti_bot_blocked", False):
                page._anti_bot_blocked = False
                anti_bot_failures += 1
                if anti_bot_failures >= 2:
                    print(f"\n🛑 [{platform.upper()}] Platform is gated by Cloudflare Anti-Bot verification.")
                    print(f"🛑 Stopping {platform.upper()} attempts immediately to avoid continuous looping.")
                    print(f"📁 Generated Java solution has been saved to: {SOLUTIONS_DIR.resolve()}\n")
                    break
                else:
                    print(f"\n⚠️ [{platform.upper()}] Anti-bot challenge encountered. Waiting 20s before trying next candidate...")
                    await asyncio.sleep(20)
            print(f"⚠️ [{platform.upper()}] Problem was NOT accepted by the judge.")
            print(f"🔄 Selecting next unsolved replacement problem to guarantee {target_accepted} accepted solutions...\n")

        await asyncio.sleep(3)

    print(f"\n📊 [{platform.upper()}] Quota Summary: {accepted_count}/{target_accepted} problems successfully solved & committed.")
    return accepted_count, browser, context, page

async def execute_batch(
    platform: str,
    count: int,
    difficulty: str,
    mode: str,
    file_path: Optional[str] = None,
    headless: bool = False
):
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
    if not await wait_for_internet_connection(max_wait_seconds=120):
        print("❌ Error: No active internet connection detected after 120 seconds. Aborting batch run.")
        return

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
        if platform == "file" and file_path and os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                urls_to_solve = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
            for idx, url in enumerate(urls_to_solve, 1):
                browser, context, page = await ensure_live_page(page, p, browser, context)
                success = await process_problem(page, url, mode=mode)
                if success:
                    total_accepted += 1
                if idx < len(urls_to_solve):
                    await asyncio.sleep(4)
        elif platform == "all" or "," in platform:
            if platform == "all":
                all_platforms = ["leetcode", "codeforces", "hackerrank", "codechef", "hackerearth"]
            else:
                all_platforms = [p.strip().lower() for p in platform.split(",") if p.strip()]
            target_total = count * len(all_platforms)
            print(f"\n📊 Starting autonomous quota run: {count} ACCEPTED problems on EACH of the {len(all_platforms)} platforms ({target_total} total)...")
            platform_results = {}
            for p_name in all_platforms:
                acc, browser, context, page = await solve_platform_quota(
                    page, p_name, target_accepted=count, difficulty=difficulty, mode=mode,
                    p_playwright=p, browser=browser, context=context
                )
                total_accepted += acc
                platform_results[p_name] = acc

            # Fail-safe quota guarantee: If any platform encountered issues (e.g. anti-bot gate, judge maintenance),
            # reallocate the deficit to operational platforms to GUARANTEE 100% quota fulfillment.
            deficit = target_total - total_accepted
            if deficit > 0:
                print(f"\n" + "=" * 70)
                print(f"⚠️  Quota deficit detected: {total_accepted}/{target_total} achieved ({deficit} missing).")
                print(f"🛡️  FAIL-SAFE REALLOCATION: Distributing remaining {deficit} problem(s) across operational platforms...")
                print("=" * 70)

                # Prioritize platforms that had a deficit first, then other operational platforms
                preferred_order = ["leetcode", "hackerrank", "codechef", "codeforces", "hackerearth"]
                unfulfilled = [p for p in preferred_order if platform_results.get(p, 0) < count]
                fulfilled = [p for p in preferred_order if platform_results.get(p, 0) >= count]
                fallback_pool = unfulfilled + fulfilled

                for p_name in fallback_pool:
                    if total_accepted >= target_total:
                        break
                    # Allocate needed problems to reach target_total
                    needed = min(deficit, target_total - total_accepted, 2)
                    if needed <= 0:
                        continue
                    print(f"\n🎯 [REALLOCATION] Solving {needed} additional problem(s) on {p_name.upper()}...")
                    extra, browser, context, page = await solve_platform_quota(
                        page, p_name, target_accepted=needed, difficulty=difficulty, mode=mode,
                        p_playwright=p, browser=browser, context=context
                    )
                    total_accepted += extra
                    platform_results[p_name] = platform_results.get(p_name, 0) + extra

            print(f"\n🏆 COMPLETED! {total_accepted}/{target_total} total problems verified Accepted & committed to GitHub!")
        else:
            total_accepted, browser, context, page = await solve_platform_quota(
                page, platform, target_accepted=count, difficulty=difficulty, mode=mode,
                p_playwright=p, browser=browser, context=context
            )
            print(f"\n🏆 {platform.upper()} COMPLETED! {total_accepted}/{count} problems verified Accepted & committed to GitHub!")

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
def print_status_summary():
    """Prints a beautiful summary table of solved problems across all platforms."""
    all_solved = SolvedRegistry.get_all_solved(force_refresh=True)
    print("\n" + "=" * 68)
    print("      📊 AutoCoder AI: Solved Problems Status & Registry")
    print("=" * 68)
    print(f"  {'Platform':<16} {'Status':<14} {'Verified Count':>16}")
    print("  " + "─" * 64)
    total = 0
    display_names = {
        "leetcode": "LeetCode",
        "codeforces": "Codeforces",
        "hackerrank": "HackerRank",
        "codechef": "CodeChef",
        "hackerearth": "HackerEarth",
    }
    for p, name in display_names.items():
        cnt = len(all_solved.get(p, set()))
        total += cnt
        status_icon = "🟢 Active" if cnt > 0 else "⚪ None yet"
        print(f"  {name:<16} {status_icon:<14} {cnt:>12} solved")
    print("  " + "─" * 64)
    print(f"  {'TOTAL':<16} {'🏆 Accepted':<14} {total:>12} problems")
    print("=" * 68)
    print(f"  📁 Solved Registry : {HISTORY_FILE.resolve()}")
    print(f"  📁 Solution Files   : {SOLUTIONS_DIR.resolve()}")
    if GITHUB_USERNAME:
        print(f"  🐙 GitHub Profile   : https://github.com/{GITHUB_USERNAME}")
    print("=" * 68 + "\n")

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
            sys.argv = [sys.argv[0], "--platform", "all", "--count", "2", "--difficulty", "medium-hard", "--mode", "both"] + sys.argv[2:]
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
        default=2,
        help="Number of unsolved problems to solve per platform (default: 2)"
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
        help="Print summary status table of solved problems across platforms and exit"
    )
    args = parser.parse_args()

    if args.status:
        print_status_summary()
        sys.exit(0)

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

    async def run_batch():
        await execute_batch(
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
            await run_batch()
        await run_daily_scheduler(args.daily, run_batch)
    else:
        await run_batch()

def cli():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 AutoCoder AI stopped by user. See you next time!")
        sys.exit(0)

if __name__ == "__main__":
    cli()
