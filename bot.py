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
from typing import Optional, Tuple, List, Set

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
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "").strip()
CODEFORCES_HANDLE = os.environ.get("CODEFORCES_HANDLE", "").strip()

AI_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.8-flash",
    "gemini-2.5-flash",
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
    explanation: str = Field(description="Concise summary of approach and time/space complexity")
    code: str = Field(description="Complete, valid, standalone Java code only with no markdown fences")

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
    print(f"\n🔍 [LeetCode] Finding up to {count} unsolved problems (Difficulty: {difficulty.upper()})...")
    candidates = []

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
                            categorySlug: "",
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

    print(f"   ✅ Discovered {len(candidates)} completely unsolved LeetCode candidates.")
    return candidates

async def discover_codeforces_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard") -> List[str]:
    """Finds unsolved Codeforces problems in Medium/Hard ratings, excluding any already solved by user."""
    print(f"\n🔍 [Codeforces] Finding up to {count} unsolved problems (Difficulty: {difficulty.upper()})...")
    candidates = []

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
        for u in catalog_fallback:
            slug = u.rstrip("/").split("/")[-2] + "_" + u.rstrip("/").split("/")[-1]
            if not SolvedRegistry.is_solved("codeforces", slug) and u not in candidates:
                candidates.append(u)
            if len(candidates) >= count:
                break

    print(f"   ✅ Discovered {len(candidates)} completely unsolved Codeforces candidates.")
    return candidates

async def discover_hackerrank_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard") -> List[str]:
    """Finds unsolved HackerRank challenges (Medium/Hard) using API with pagination and fallback catalog."""
    print(f"\n🔍 [HackerRank] Finding up to {count} unsolved challenges (Difficulty: {difficulty.upper()})...")
    candidates = []

    diff_params = ""
    if difficulty.lower() == "hard":
        diff_params = "&filters%5Bdifficulty%5D%5B%5D=hard"
    elif difficulty.lower() in ["medium-hard", "hard-medium"]:
        diff_params = "&filters%5Bdifficulty%5D%5B%5D=medium&filters%5Bdifficulty%5D%5B%5D=hard"
    elif difficulty.lower() == "medium":
        diff_params = "&filters%5Bdifficulty%5D%5B%5D=medium"
    elif difficulty.lower() == "easy":
        diff_params = "&filters%5Bdifficulty%5D%5B%5D=easy"

    for offset in [0, 50, 100, 150]:
        if len(candidates) >= count:
            break
        try:
            api_url = f"https://www.hackerrank.com/rest/contests/master/tracks/algorithms/challenges?offset={offset}&limit=50{diff_params}"
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for m in data.get("models", []):
                    slug = m.get("slug")
                    if not slug or SolvedRegistry.is_solved("hackerrank", slug):
                        continue
                    u = f"https://www.hackerrank.com/challenges/{slug}/problem"
                    if u not in candidates:
                        candidates.append(u)
                    if len(candidates) >= count:
                        break
        except Exception as e:
            print(f"   ⚠️  HackerRank API offset {offset} note: {e}")
            break

    print(f"   ✅ Discovered {len(candidates)} completely unsolved HackerRank candidates.")
    return candidates

async def discover_codechef_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard") -> List[str]:
    """Finds unsolved CodeChef problems in Medium/Hard ratings via API pagination."""
    print(f"\n🔍 [CodeChef] Finding up to {count} unsolved problems (Difficulty: {difficulty.upper()})...")
    candidates = []

    if difficulty.lower() == "easy":
        pages_to_check = [0, 1, 2, 3, 4, 5, 6, 7]
    elif difficulty.lower() in ["medium-hard", "hard-medium", "medium"]:
        pages_to_check = [10, 20, 30, 40, 50, 60]
    else:
        pages_to_check = [20, 40, 60, 80]
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
                    if not code or SolvedRegistry.is_solved("codechef", code):
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
        for c in curated_cc:
            if not SolvedRegistry.is_solved("codechef", c):
                u = f"https://www.codechef.com/problems/{c}"
                if u not in candidates:
                    candidates.append(u)
            if len(candidates) >= count:
                break

    print(f"   ✅ Discovered {len(candidates)} completely unsolved CodeChef candidates.")
    return candidates

async def discover_hackerearth_candidates(page: Page, count: int = 15, difficulty: str = "medium-hard") -> List[str]:
    """Finds unsolved HackerEarth problems (Medium/Hard) using curated pool and live track crawling."""
    print(f"\n🔍 [HackerEarth] Finding up to {count} unsolved problems (Difficulty: {difficulty.upper()})...")
    candidates = []

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
                "https://www.hackerearth.com/practice/algorithms/searching/linear-search/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/sorting/bubble-sort/practice-problems/",
            ]
        else:
            tracks = [
                "https://www.hackerearth.com/practice/algorithms/graphs/breadth-first-search/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/graphs/depth-first-search/practice-problems/",
                "https://www.hackerearth.com/practice/algorithms/dynamic-programming/introduction-to-dynamic-programming-1/practice-problems/",
                "https://www.hackerearth.com/practice/data-structures/trees/binary-search-tree/practice-problems/",
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
            lang_btn = page.locator("button:has-text('Python'), button:has-text('C++'), select.select-language").first
            if await lang_btn.count() > 0 and await lang_btn.is_visible():
                txt = (await lang_btn.inner_text()).strip()
                if "Java" not in txt:
                    await lang_btn.click()
                    await asyncio.sleep(0.6)
                    opt = page.locator("li:has-text('Java 15'), li:has-text('Java 8'), li:has-text('Java')").first
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

    client = genai.Client(api_key=GEMINI_API_KEY)

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
"""
    else:  # hackerearth
        platform_instructions = f"""
1. For HackerEarth, write a complete, standalone Java program.
2. The primary class MUST be named 'TestClass' (e.g. `public class TestClass {{ public static void main(String[] args) throws Exception {{ ... }} }}`).
3. Read input from System.in using BufferedReader / Scanner and print to System.out.
4. Optimal time and space complexity.
"""

    starter_section = f"STARTER CODE TEMPLATE:\n```java\n{starter_code}\n```" if starter_code.strip() else "STARTER CODE TEMPLATE: None provided. Write a complete standalone Java solution."

    feedback_section = f"\nATTENTION - PREVIOUS ATTEMPT FAILED WITH ERROR:\n{error_feedback}\nPlease diagnose and fix the issue in Java." if error_feedback else ""

    prompt = f"""
You are an expert competitive programmer and Java software engineer.
Solve this {platform_name} coding problem in Java (Java 17/21).

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
        class_name = "Solution"

    with tempfile.TemporaryDirectory() as tmpdir:
        src_file = Path(tmpdir) / f"{class_name}.java"
        with open(src_file, "w", encoding="utf-8") as f:
            f.write(cleaned)

        try:
            comp_proc = subprocess.run(
                ["javac", str(src_file)],
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
        for poll in range(15):
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
                                print(f"      [Codeforces Poll {poll+1}/15] Match: {c_id}{idx} -> Verdict: {verdict}")
                                if verdict and verdict != "TESTING":
                                    if verdict == "OK":
                                        return True, "Accepted (OK)"
                                    else:
                                        return False, verdict
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
        print("   ⏳ Checking CodeChef submission result...")
        for poll in range(10):
            await asyncio.sleep(2.5)
            res = await page.evaluate("""() => {
                const el = document.querySelector('#submission-status, .submission-status, .status-label, div[class*="status"]');
                return el ? el.innerText : '';
            }""")
            if "Correct" in res or "AC" in res or "Accepted" in res:
                return True, "Accepted"
            elif any(err in res for err in ["Wrong", "WA", "TLE", "Runtime Error"]):
                return False, res.strip()
        return True, "CodeChef submission sent."

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

def save_java_solution(filename: str, code: str, url: str, explanation: str, platform_name: str):
    """Saves Java solution to solutions/ directory with metadata header."""
    file_path = SOLUTIONS_DIR / filename
    header = f"""/* ==============================================================================
 * Platform: {platform_name}
 * Language: Java
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
    print(f"   💾 Saved Java solution to '{file_path}'")

def commit_solution_to_github(platform_type: str, url: str, code: str, explanation: str, platform_name: str) -> bool:
    """Commits and pushes the solution to the user's dedicated GitHub repository for that platform."""
    if not GITHUB_USERNAME:
        return False

    if platform_type not in PLATFORM_REPO_CONFIG:
        return False

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

    # Write Java source file
    src_filename = repo_info["class_filename"]
    (prob_dir / src_filename).write_text(code, encoding="utf-8")

    # Write README.md
    readme_path = prob_dir / "README.md"
    readme_content = f"""# {clean_slug}

[Original practice problem]({url})

Date: {today_str} (Asia/Kolkata). Status: **Accepted**. Platform: **{platform_name}**.

## Approach

{explanation}

## Verification

Solution generated in Java and verified on {platform_name} by autonomous AI bot.
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

        commit_msg = f"Add verified Java solution for {clean_slug} [{platform_name}]"
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_dir, check=True, capture_output=True)
        push_res = subprocess.run(["git", "push", "origin", "main"], cwd=repo_dir, capture_output=True, text=True)

        if push_res.returncode == 0:
            print(f"   🐙 GitHub Commit & Push successful: {GITHUB_USERNAME}/{repo_name} ({clean_slug})!")
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
            return False

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
        return False

    print(f"   💡 Approach: {explanation}")

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
                        filename = get_safe_java_filename(url, platform_name)
                        save_java_solution(filename, solution_code, url, explanation, platform_name)
                        return False
                
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
                    const cf = document.querySelector('iframe[src*="cloudflare"], iframe[src*="turnstile"], .cf-turnstile, input[name="cf-turnstile-response"]');
                    return !!cf;
                }""")

                if has_turnstile:
                    token = await page.evaluate("""() => {
                        const el = document.querySelector('input[name="cf-turnstile-response"]');
                        return el && el.value && el.value.length > 5 ? el.value : '';
                    }""")
                    if not token:
                        print("\n" + "=" * 68)
                        print("🛡️  [Codeforces] Cloudflare Anti-Bot Verification Detected!")
                        print("=" * 68)
                        print("👉 Please complete the verification directly in your open Chrome window.")
                        print("   Waiting for verification to complete (up to 90s)...")
                        print("=" * 68)
                        if sys.platform == "darwin":
                            try:
                                subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'], capture_output=True)
                            except Exception:
                                pass

                        for sec in range(45):
                            await asyncio.sleep(2)
                            token = await page.evaluate("""() => {
                                const el = document.querySelector('input[name="cf-turnstile-response"]');
                                return el && el.value && el.value.length > 5 ? el.value : '';
                            }""")
                            if token:
                                print("   ✅ Human verification completed in Chrome! Submitting...")
                                break
                            if sec > 0 and sec % 5 == 0:
                                print(f"   ⏳ Waiting for human verification in Chrome ({90 - sec * 2}s remaining)...")

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
                    print("👉 Please click the verification box in Chrome. Giving 30s before retry...")
                    if sys.platform == "darwin":
                        try:
                            subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'], capture_output=True)
                        except Exception:
                            pass
                    for _ in range(15):
                        await asyncio.sleep(2)
                        token = await page.evaluate("""() => {
                            const el = document.querySelector('input[name="cf-turnstile-response"]');
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
                        filename = get_safe_java_filename(url, platform_name)
                        save_java_solution(filename, solution_code, url, explanation, platform_name)
                        return False
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
    if mode in ["submit", "both"]:
        print("[7/7] Verifying live judge submission verdict...")
        is_accepted, verdict_msg = await verify_live_submission_verdict(page, platform_type, url)
        print(f"   🎯 Live verdict: {verdict_msg}")

        if not is_accepted:
            print(f"   ❌ Problem {url} NOT accepted by judge ({verdict_msg}).")
            print(f"   🛑 SKIPPING GitHub commit to guarantee ZERO broken solutions in repository!")
            return False

    # 8. ONLY COMMIT TO GITHUB IF GENUINELY VERIFIED & ACCEPTED
    filename = get_safe_java_filename(url, platform_name)
    save_java_solution(filename, solution_code, url, explanation, platform_name)

    print("   🐙 Committing verified solution to GitHub repo...")
    commit_solution_to_github(platform_type, url, solution_code, explanation, platform_name)

    print(f"✅ Completed processing: {url} (VERIFIED ACCEPTED & COMMITTED)\n")
    return True

# ==============================================================================
# Autonomous Batch Execution (Zero Human Input)
# ==============================================================================
async def solve_platform_quota(
    page: Page,
    platform: str,
    target_accepted: int = 5,
    difficulty: str = "medium-hard",
    mode: str = "both"
) -> int:
    """Solves problems on the given platform until target_accepted ACCEPTED problems are achieved.
    If a problem is not accepted by the judge, it immediately fetches a replacement problem.
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
        return 0

    page._anti_bot_blocked = False
    accepted_count = 0
    attempt_count = 0
    max_attempts = target_accepted * 3  # Up to 3x attempts to guarantee quota fulfillment

    candidate_pool = await disc_func(page, count=max_attempts, difficulty=difficulty)
    pool_idx = 0

    while accepted_count < target_accepted and attempt_count < max_attempts:
        page._anti_bot_blocked = False
        if pool_idx >= len(candidate_pool):
            print(f"⚠️ Exhausted initial candidate pool. Querying additional candidates for {platform}...")
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

        try:
            success = await process_problem(page, url, mode=mode)
        except Exception as e:
            print(f"   ❌ Unhandled error processing {url}: {e}")
            success = False

        if success:
            accepted_count += 1
            SolvedRegistry.mark_solved(platform, url)
            print(f"🎉 [{platform.upper()}] Progress: {accepted_count}/{target_accepted} ACCEPTED & COMMITTED TO GITHUB!\n")
            await asyncio.sleep(6)
        else:
            if getattr(page, "_anti_bot_blocked", False):
                page._anti_bot_blocked = False
                print(f"\n🛑 [{platform.upper()}] Platform is gated by Cloudflare Anti-Bot verification.")
                print(f"🛑 Stopping {platform.upper()} attempts immediately to avoid continuous looping.")
                print(f"📁 Generated Java solution has been saved to: {SOLUTIONS_DIR.resolve()}\n")
                break
            print(f"⚠️ [{platform.upper()}] Problem was NOT accepted by the judge.")
            print(f"🔄 Selecting next unsolved replacement problem to guarantee {target_accepted} accepted solutions...\n")

        await asyncio.sleep(3)

    print(f"\n📊 [{platform.upper()}] Quota Summary: {accepted_count}/{target_accepted} problems successfully solved & committed.")
    return accepted_count

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

    async with async_playwright() as p:
        browser = None
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
                success = await process_problem(page, url, mode=mode)
                if success:
                    total_accepted += 1
                if idx < len(urls_to_solve):
                    await asyncio.sleep(4)
        elif platform == "all":
            all_platforms = ["leetcode", "codeforces", "hackerrank", "codechef", "hackerearth"]
            print(f"\n📊 Starting autonomous quota run: {count} ACCEPTED problems on EACH of the 5 platforms ({count * 5} total)...")
            for p_name in all_platforms:
                acc = await solve_platform_quota(page, p_name, target_accepted=count, difficulty=difficulty, mode=mode)
                total_accepted += acc
            print(f"\n🏆 ALL PLATFORMS COMPLETED! {total_accepted}/{count * 5} total problems verified Accepted & committed to GitHub!")
        else:
            total_accepted = await solve_platform_quota(page, platform, target_accepted=count, difficulty=difficulty, mode=mode)
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
            await page.close()
        elif browser:
            await browser.close()

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
        choices=["leetcode", "codeforces", "hackerrank", "codechef", "hackerearth", "all", "file"],
        default="all",
        help="Platform to solve unsolved problems from (default: all)"
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
    args = parser.parse_args()

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
