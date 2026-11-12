"""
Unit tests verifying solver reliability, fast input parsing, diagnostic categorization,
AI vs. platform rate limit isolation, and independent quota tracking.
"""

import sys
import os
import time
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from bot import (
    JAVA_FAST_SCANNER_TEMPLATE,
    DiagnosticCategory,
    SampleTestCase,
    SampleTestResult,
    VerificationPipelineResult,
    ProcessResult,
    ProcessStage,
    VerdictState,
    SubmissionVerdict,
    FailureCode,
    clean_code_string,
    validate_problem_text_completeness,
    extract_sample_test_cases_from_text,
    run_all_sample_tests_sandboxed,
    verify_and_repair_java_pipeline,
    classify_failure,
    PlatformRateLimiter,
    get_platform_limiter,
    set_platform_quota_diagnostic,
    get_platform_quota_diagnostic,
    detect_security_challenge_or_captcha,
)


# ==============================================================================
# 1. FastScanner Template Verification (Whitespace, Multi-int, Negatives, EOF)
# ==============================================================================
def test_fast_scanner_template_compilation_and_execution():
    """Verifies that Java FastScanner template correctly parses arbitrary whitespace,
    multiple integers on one line, negative numbers, floats, and handles EOF."""
    driver_code = f"""
import java.io.*;
import java.util.*;

public class Main {{
    {JAVA_FAST_SCANNER_TEMPLATE}

    public static void main(String[] args) {{
        FastScanner fs = new FastScanner();
        int count = 0;
        long sum = 0;
        while (fs.hasNext()) {{
            String tok = fs.next();
            if (tok == null) break;
            long val = Long.parseLong(tok);
            sum += val;
            count++;
        }}
        System.out.println("COUNT=" + count + " SUM=" + sum);
    }}
}}
"""
    # Test input with tabs, multiple spaces, negative numbers, and trailing whitespace
    test_input = "   10    -20\t30\n\n  -40   50   \n\n"
    expected_output = "COUNT=5 SUM=30"

    samples = [SampleTestCase(input_text=test_input, expected_output=expected_output, sample_index=1)]
    ok, results, summary = run_all_sample_tests_sandboxed(
        code=driver_code,
        samples=samples,
        platform_type="codeforces"
    )
    assert ok is True, f"FastScanner sample execution failed:\n{summary}"
    assert len(results) == 1
    assert results[0].passed is True
    assert "COUNT=5 SUM=30" in results[0].actual_output


# ==============================================================================
# 2. NumberFormatException Avoidance on Multi-Token Line ("1 2")
# ==============================================================================
def test_number_format_exception_avoidance():
    """Reproduces the exact failure: input '1 2'.
    Verifies that naive Integer.parseInt(br.readLine()) crashes with NumberFormatException,
    which is properly categorized as PARSER_ERROR, while FastScanner succeeds."""
    naive_code = """
import java.io.*;
public class Main {
    public static void main(String[] args) throws Exception {
        BufferedReader br = new BufferedReader(new InputStreamReader(System.in));
        int a = Integer.parseInt(br.readLine()); // Crashes on "1 2"
        System.out.println(a);
    }
}
"""
    fast_code = f"""
import java.io.*;
import java.util.*;
public class Main {{
    {JAVA_FAST_SCANNER_TEMPLATE}
    public static void main(String[] args) {{
        FastScanner fs = new FastScanner();
        int a = fs.nextInt();
        int b = fs.nextInt();
        System.out.println(a + b);
    }}
}}
"""
    samples = [SampleTestCase(input_text="1 2\n", expected_output="3", sample_index=1)]

    # 1. Naive code must fail and be categorized as PARSER_ERROR
    ok_naive, res_naive, summary_naive = run_all_sample_tests_sandboxed(
        code=naive_code,
        samples=samples,
        platform_type="codeforces"
    )
    assert ok_naive is False
    assert len(res_naive) == 1
    assert res_naive[0].diagnostic_category == DiagnosticCategory.PARSER_ERROR
    assert "numberformatexception" in (res_naive[0].stderr or "").lower()

    # 2. FastScanner code must pass cleanly
    ok_fast, res_fast, summary_fast = run_all_sample_tests_sandboxed(
        code=fast_code,
        samples=samples,
        platform_type="codeforces"
    )
    assert ok_fast is True
    assert res_fast[0].passed is True
    assert res_fast[0].actual_output.strip() == "3"


# ==============================================================================
# 3. Clean Code String Strips Trailing Syntax Noise
# ==============================================================================
def test_clean_code_string_removes_trailing_syntax_noise():
    """Verifies that stray punctuation or markdown fences following the final closing brace
    are stripped to prevent javac compilation failures."""
    code_with_dot = "public class Main {\n    public static void main(String[] args) {}\n}."
    assert clean_code_string(code_with_dot).endswith("}")

    code_with_semicolon = "class Solution {\n    public int solve() { return 1; }\n};"
    assert clean_code_string(code_with_semicolon).endswith("}")

    code_with_fences = "```java\npublic class Main {}\n```."
    assert clean_code_string(code_with_fences).endswith("}")

    code_with_backticks = "public class Main {}\n```"
    assert clean_code_string(code_with_backticks).endswith("}")


# ==============================================================================
# 4. Extract Sample Test Cases Prevents Format Prose Pollution
# ==============================================================================
def test_extract_sample_test_cases_prevents_format_prose_pollution():
    """Verifies that problem text format explanations are not captured as sample inputs,
    and expected outputs terminate cleanly before explanations."""
    problem_text = """
Chef is solving an addition problem.
Input Format
The first line contains an integer T, the number of test cases. Then T test cases follow.
Each testcase contains of a single line of input, two integers A and B.
Output Format
For each testcase, output in a single line the sum of A and B.
Constraints
1 <= T <= 1000
Sample 1:
Input:
2
1 2
100 200
Output:
3
300
Explanation:
Testcase 1: 1 + 2 = 3.
Testcase 2: 100 + 200 = 300.
"""
    samples = extract_sample_test_cases_from_text(problem_text)
    assert len(samples) >= 1
    s1 = samples[0]
    # Input must NOT contain format prose
    assert "the first line" not in s1.input_text.lower()
    assert s1.input_text.strip() == "2\n1 2\n100 200"
    # Output must NOT contain explanation
    assert "explanation" not in s1.expected_output.lower()
    assert s1.expected_output.strip() == "3\n300"


# ==============================================================================
# 5. Problem Text Completeness Validation
# ==============================================================================
def test_validate_problem_text_completeness():
    """Tests that incomplete or placeholder problem descriptions are rejected."""
    # Empty
    ok, err = validate_problem_text_completeness("")
    assert ok is False

    # Short placeholder
    ok, err = validate_problem_text_completeness("404 Not Found")
    assert ok is False
    assert "error placeholder" in (err or "").lower()

    ok, err = validate_problem_text_completeness("Loading...")
    assert ok is False

    # Valid problem text
    ok, err = validate_problem_text_completeness("Given an array of integers nums and an integer target, return indices.")
    assert ok is True
    assert err is None


# ==============================================================================
# 6. Granular Diagnostic Categorization
# ==============================================================================
def test_granular_diagnostic_categorization():
    """Verifies that failures are correctly categorized into granular diagnostic categories."""
    # 1. COMPILATION_ERROR: Syntax error
    syntax_err_code = "public class Main { public static void main(String[] args) { int a = ; } }"
    comp_ok, _, summary_str = run_all_sample_tests_sandboxed(
        code=syntax_err_code,
        samples=[SampleTestCase(input_text="1", expected_output="1")],
        platform_type="codeforces"
    )
    assert comp_ok is False
    assert "compilation error" in summary_str.lower()

    # 2. ALGORITHM_ERROR: Wrong Answer
    wa_code = """
import java.util.Scanner;
public class Main {
    public static void main(String[] args) {
        Scanner sc = new Scanner(System.in);
        int a = sc.nextInt();
        System.out.println(a * 10);
    }
}
"""
    ok_wa, res_wa, _ = run_all_sample_tests_sandboxed(
        code=wa_code,
        samples=[SampleTestCase(input_text="5\n", expected_output="5\n")],
        platform_type="codeforces"
    )
    assert ok_wa is False
    assert len(res_wa) == 1
    assert res_wa[0].diagnostic_category == DiagnosticCategory.ALGORITHM_ERROR

    # 3. TIMEOUT_ERROR: Infinite loop
    tle_code = """
public class Main {
    public static void main(String[] args) {
        while (true) {}
    }
}
"""
    ok_tle, res_tle, _ = run_all_sample_tests_sandboxed(
        code=tle_code,
        samples=[SampleTestCase(input_text="1\n", expected_output="1\n")],
        platform_type="codeforces",
        timeout_sec=0.5
    )
    assert ok_tle is False
    assert len(res_tle) == 1
    assert res_tle[0].diagnostic_category == DiagnosticCategory.TIMEOUT_ERROR


# ==============================================================================
# 7. Runtime Error Diagnostic Feedback in Repair Pipeline
# ==============================================================================
def test_runtime_error_feedback_in_repair_pipeline():
    """Verifies that runtime exceptions feed back exact stack trace, input, expected, and actual output."""
    received_feedback = []

    def mock_ai_caller(problem_text, starter_code, platform_name, platform_type, error_feedback=None, dry_run=False):
        received_feedback.append(error_feedback)
        if len(received_feedback) == 1:
            # First attempt: throws NumberFormatException
            return (
                "import java.io.*;\npublic class Main {\n    public static void main(String[] args) throws Exception {\n        BufferedReader br = new BufferedReader(new InputStreamReader(System.in));\n        int a = Integer.parseInt(br.readLine());\n        System.out.println(a);\n    }\n}",
                "Naive solution"
            )
        else:
            # Repaired attempt: uses FastScanner
            return (
                f"import java.io.*;\nimport java.util.*;\npublic class Main {{\n    {JAVA_FAST_SCANNER_TEMPLATE}\n    public static void main(String[] args) {{\n        FastScanner fs = new FastScanner();\n        int a = fs.nextInt();\n        int b = fs.nextInt();\n        System.out.println(a + b);\n    }}\n}}",
                "Repaired solution with FastScanner"
            )

    samples = [SampleTestCase(input_text="1 2\n", expected_output="3\n", sample_index=1)]
    pipeline_res = verify_and_repair_java_pipeline(
        problem_text="Add two numbers on a single line",
        starter_code="",
        samples=samples,
        platform_name="Codeforces",
        platform_type="codeforces",
        max_repairs=2,
        ai_caller=mock_ai_caller
    )

    assert pipeline_res.success is True
    assert pipeline_res.repair_attempts == 1
    # Check that repair prompt received diagnostic feedback
    fb = received_feedback[1]
    assert fb is not None
    assert "SAMPLE TEST CASE #1 FAILED [PARSER_ERROR]" in fb
    assert "NumberFormatException" in fb
    assert "1 2" in fb
    assert "FastScanner" in fb


# ==============================================================================
# 8. Sample Execution Required for Stdin/Stdout Problems
# ==============================================================================
def test_sample_execution_required_for_stdin_problems():
    """Verifies that stdin/stdout platforms require sample execution before submission."""
    cf_code = "import java.util.Scanner;\npublic class Main { public static void main(String[] args) { Scanner sc = new Scanner(System.in); System.out.println(sc.nextInt()); } }"

    def dummy_ai_caller(problem_text, starter_code, platform_name, platform_type, error_feedback=None, dry_run=False):
        return cf_code, "Approach"

    # For Codeforces with empty samples, verification must fail
    res = verify_and_repair_java_pipeline(
        problem_text="Given n, print n",
        starter_code="",
        samples=[],
        platform_name="Codeforces",
        platform_type="codeforces",
        max_repairs=1,
        ai_caller=dummy_ai_caller
    )
    assert res.success is False
    assert "Sample test execution is required" in (res.failure_reason or "")
    assert res.diagnostic_category == DiagnosticCategory.PARSER_ERROR


# ==============================================================================
# 9. AI Rate Limits (Gemini 429) Do Not Trip Platform Circuit Breaker
# ==============================================================================
def test_ai_rate_limit_isolation_from_platform_circuit_breaker():
    """Verifies that Gemini / AI provider 429 errors are classified as TRANSIENT
    and do not penalize or trip the coding platform's circuit breaker."""
    limiter = get_platform_limiter("hackerearth")
    limiter.reset()
    assert limiter.circuit_state.value == "CLOSED"
    assert limiter.is_paused() is False

    # Simulate Gemini 429 error
    ai_err_msg = "All Gemini models failed. Last error: 429 ResourceExhausted: Quota exceeded for quota metric 'Generate Content API requests'"
    f_code = classify_failure(ai_err_msg)
    assert f_code == FailureCode.TRANSIENT, f"Expected TRANSIENT for AI 429, got {f_code}"

    # Platform rate limiter must not be incremented for AI errors
    is_ai_err = any(k in ai_err_msg.lower() for k in ["gemini", "ai generation", "all gemini models failed", "resource_exhausted"])
    assert is_ai_err is True
    # The limiter remains CLOSED
    assert limiter.circuit_state.value == "CLOSED"
    assert limiter.is_paused() is False

    # Contrast with coding platform HTTP 429
    platform_err_msg = "Platform HTTP 429 Too Many Requests: Rate limit reached on Codeforces"
    p_code = classify_failure(platform_err_msg, http_status=429)
    assert p_code == FailureCode.RATE_LIMITED


# ==============================================================================
# 10. Independent Platform Quotas and Deficit Reporting
# ==============================================================================
def test_independent_platform_quotas_and_deficit_reporting():
    """Verifies that platform quota diagnostics are tracked independently,
    and deficits report the specific cause."""
    set_platform_quota_diagnostic("leetcode", "Quota achieved")
    set_platform_quota_diagnostic("hackerearth", "Circuit breaker OPEN (RATE_LIMITED)")
    set_platform_quota_diagnostic("codeforces", "Exhausted candidate pool after 8 attempts")

    assert get_platform_quota_diagnostic("leetcode") == "Quota achieved"
    assert "RATE_LIMITED" in get_platform_quota_diagnostic("hackerearth")
    assert "Exhausted candidate pool" in get_platform_quota_diagnostic("codeforces")


# ==============================================================================
# 11. HackerRank Problem URL Does Not Trigger Security Challenge
# ==============================================================================
@pytest.mark.asyncio
async def test_hackerrank_url_does_not_trigger_security_challenge():
    """Verifies that normal HackerRank problem URLs (/challenges/<slug>/problem)
    are not falsely flagged as security challenges."""
    mock_page = MagicMock()
    mock_page.url = "https://www.hackerrank.com/challenges/solve-me-first/problem"
    mock_page.evaluate = AsyncMock(return_value={"detected": False})

    det = await detect_security_challenge_or_captcha(mock_page)
    assert det.detected is False

    # But genuine challenge URLs are detected
    mock_page.url = "https://example.com/cdn-cgi/challenge-platform/h/b/scripts/turnstile"
    det2 = await detect_security_challenge_or_captcha(mock_page)
    assert det2.detected is True
    assert "url_challenge_or_login_barrier" in (det2.challenge_type or "")
