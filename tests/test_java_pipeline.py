import pytest
import tempfile
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from bot import (
    VerdictState,
    SubmissionVerdict,
    ProcessStage,
    ProcessResult,
    SampleTestCase,
    CompilationResult,
    ExecutionResult,
    SampleTestResult,
    VerificationPipelineResult,
    detect_java_class_and_filename,
    validate_code_security_and_dependencies,
    validate_platform_signatures,
    compare_sample_outputs,
    compile_java_code_sandboxed,
    compile_java_code_locally,
    execute_java_sandboxed,
    run_all_sample_tests_sandboxed,
    verify_and_repair_java_pipeline,
    extract_sample_test_cases_from_text,
    process_problem,
    DatabaseManager,
)


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_pipeline.db"
    return DatabaseManager(db_file, auto_migrate=False)


# ==============================================================================
# 1. Valid Java Compilation
# ==============================================================================
def test_valid_java_compilation():
    code_cf = """
    import java.util.Scanner;
    public class Main {
        public static void main(String[] args) {
            Scanner sc = new Scanner(System.in);
            if (sc.hasNextInt()) {
                System.out.println(sc.nextInt() * 2);
            }
        }
    }
    """
    res_cf = compile_java_code_sandboxed(code_cf, "codeforces")
    assert res_cf.success is True
    assert res_cf.class_name == "Main"
    assert res_cf.source_filename == "Main.java"
    assert res_cf.diagnostics is None

    code_he = """
    public class TestClass {
        public static void main(String[] args) {
            System.out.println("HackerEarth Ready");
        }
    }
    """
    res_he = compile_java_code_sandboxed(code_he, "hackerearth")
    assert res_he.success is True
    assert res_he.class_name == "TestClass"
    assert res_he.source_filename == "TestClass.java"


# ==============================================================================
# 2. Syntax Failure and Repair Loop
# ==============================================================================
def test_syntax_failure_and_repair():
    call_count = 0
    received_feedback = []

    def mock_ai_caller(problem_text, starter_code, platform_name, platform_type, error_feedback=None):
        nonlocal call_count
        call_count += 1
        received_feedback.append(error_feedback)
        if call_count == 1:
            # Syntax error: missing closing parenthesis and semicolon
            return "public class Main { public static void main(String[] args) { System.out.println(\"hello\" } }", "Broken"
        else:
            # Repaired valid code
            return "public class Main { public static void main(String[] args) { System.out.println(\"hello\"); } }", "Repaired"

    samples = [SampleTestCase(input_text="", expected_output="hello", sample_index=1)]
    res = verify_and_repair_java_pipeline(
        problem_text="Print hello",
        starter_code="",
        samples=samples,
        platform_name="Codeforces",
        platform_type="codeforces",
        max_repairs=3,
        ai_caller=mock_ai_caller
    )

    assert res.success is True
    assert res.repair_attempts == 1
    assert call_count == 2
    # Verify exact compiler diagnostics were fed into the repair attempt
    assert received_feedback[1] is not None
    assert "EXACT COMPILER DIAGNOSTICS (javac):" in received_feedback[1]


# ==============================================================================
# 3. Wrong Sample Output & Non-Submission Enforcement
# ==============================================================================
def test_wrong_sample_output():
    # Code outputs multiplication instead of addition
    buggy_code = """
    import java.util.Scanner;
    public class Main {
        public static void main(String[] args) {
            Scanner sc = new Scanner(System.in);
            int a = sc.nextInt();
            int b = sc.nextInt();
            System.out.println(a * b);
        }
    }
    """
    samples = [
        SampleTestCase(input_text="2 3", expected_output="5", sample_index=1)
    ]

    ok, sample_results, summary = run_all_sample_tests_sandboxed(
        code=buggy_code,
        samples=samples,
        platform_type="codeforces"
    )
    assert ok is False
    assert len(sample_results) == 1
    assert sample_results[0].passed is False
    assert sample_results[0].actual_output.strip() == "6"
    assert sample_results[0].expected_output.strip() == "5"
    assert "FAILED" in summary

    # Verify pipeline fails when AI cannot fix the wrong sample output
    def persistent_buggy_caller(problem_text, starter_code, platform_name, platform_type, error_feedback=None):
        return buggy_code, "Multiplies numbers"

    pipeline_res = verify_and_repair_java_pipeline(
        problem_text="Add two numbers",
        starter_code="",
        samples=samples,
        platform_name="Codeforces",
        platform_type="codeforces",
        max_repairs=2,
        ai_caller=persistent_buggy_caller
    )
    assert pipeline_res.success is False
    assert pipeline_res.repair_attempts == 2
    assert "SAMPLE TEST CASE #1 FAILED" in (pipeline_res.failure_reason or "")


# ==============================================================================
# 4. Infinite Loop Timeout
# ==============================================================================
def test_infinite_loop_timeout():
    infinite_loop_code = """
    public class Main {
        public static void main(String[] args) {
            int i = 0;
            while (true) {
                i++;
            }
        }
    }
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        comp = compile_java_code_sandboxed(infinite_loop_code, "codeforces", work_dir=tmp_path)
        assert comp.success is True

        exec_res = execute_java_sandboxed(
            class_dir=tmp_path,
            class_name="Main",
            timeout_sec=1.0
        )
        assert exec_res.success is False
        assert exec_res.timeout is True
        assert "Time Limit Exceeded" in (exec_res.error_message or "")


# ==============================================================================
# 5. Excessive Memory Usage
# ==============================================================================
def test_excessive_memory_usage():
    oom_code = """
    import java.util.ArrayList;
    public class Main {
        public static void main(String[] args) {
            ArrayList<byte[]> list = new ArrayList<>();
            while (true) {
                list.add(new byte[10 * 1024 * 1024]);
            }
        }
    }
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        comp = compile_java_code_sandboxed(oom_code, "codeforces", work_dir=tmp_path)
        assert comp.success is True

        exec_res = execute_java_sandboxed(
            class_dir=tmp_path,
            class_name="Main",
            timeout_sec=5.0,
            max_memory_mb=32
        )
        assert exec_res.success is False
        assert exec_res.oom is True
        assert "Memory Limit Exceeded" in (exec_res.error_message or "")


# ==============================================================================
# 6. Missing Public Class Detection
# ==============================================================================
def test_missing_public_class():
    # Snippet with only methods, no class declaration
    code_no_class = """
    public int solve(int a, int b) {
        return a + b;
    }
    """
    c_name, f_name, cleaned, err = detect_java_class_and_filename(code_no_class, "codeforces")
    assert err is not None
    assert "Missing public class" in err

    comp_res = compile_java_code_sandboxed(code_no_class, "codeforces")
    assert comp_res.success is False
    assert "Missing public class" in (comp_res.diagnostics or comp_res.error_message or "")

    # Codeforces requires class Main, not class Solver
    code_wrong_class = """
    public class Solver {
        public static void main(String[] args) {
            System.out.println("Hello");
        }
    }
    """
    c_name2, f_name2, cleaned2, err2 = detect_java_class_and_filename(code_wrong_class, "codeforces")
    assert err2 is not None
    assert "requires class Main" in err2


# ==============================================================================
# 7. LeetCode TreeNode and ListNode Handling
# ==============================================================================
def test_leetcode_treenode_listnode_handling():
    leetcode_code = """
    import java.util.*;

    class Solution {
        public int maxDepth(TreeNode root) {
            if (root == null) return 0;
            return 1 + Math.max(maxDepth(root.left), maxDepth(root.right));
        }

        public ListNode reverseList(ListNode head) {
            ListNode prev = null;
            ListNode curr = head;
            while (curr != null) {
                ListNode next = curr.next;
                curr.next = prev;
                prev = curr;
                curr = next;
            }
            return prev;
        }

        public Node copyRandomList(Node head) {
            if (head == null) return null;
            return new Node(head.val);
        }
    }
    """
    comp_res = compile_java_code_sandboxed(leetcode_code, "leetcode")
    assert comp_res.success is True
    assert comp_res.class_name == "Solution"
    assert comp_res.source_filename == "Solution.java"

    # Verify method-based solution sample testing returns verified stubs
    ok, results, summary = run_all_sample_tests_sandboxed(leetcode_code, [], "leetcode")
    assert ok is True
    assert "verified" in summary.lower() or "check only" in summary.lower()


# ==============================================================================
# 8. Correct Stdin / Stdout Execution
# ==============================================================================
def test_correct_stdin_stdout_execution():
    sum_code = """
    import java.util.Scanner;
    public class Main {
        public static void main(String[] args) {
            Scanner sc = new Scanner(System.in);
            while (sc.hasNextInt()) {
                int a = sc.nextInt();
                int b = sc.nextInt();
                System.out.println(a + b);
            }
        }
    }
    """
    samples = [
        SampleTestCase(input_text="10 20\n", expected_output="30\n", sample_index=1),
        SampleTestCase(input_text="100 250\n", expected_output="350\n", sample_index=2),
        SampleTestCase(input_text="-5 15\n", expected_output="10\n", sample_index=3),
    ]

    ok, results, summary = run_all_sample_tests_sandboxed(
        code=sum_code,
        samples=samples,
        platform_type="codeforces"
    )
    assert ok is True
    assert len(results) == 3
    assert all(r.passed for r in results)
    assert "PASSED" in summary


# ==============================================================================
# 9. Generated Code Attempting Network Access
# ==============================================================================
def test_generated_code_attempting_network_access():
    network_code = """
    import java.net.Socket;
    public class Main {
        public static void main(String[] args) throws Exception {
            Socket s = new Socket("1.1.1.1", 80);
            System.out.println("Connected");
        }
    }
    """
    # 1. Static security check must catch java.net and Socket
    sec_ok, sec_err = validate_code_security_and_dependencies(network_code)
    assert sec_ok is False
    assert "Network access" in sec_err or "Network classes" in sec_err

    # 2. Runtime sandbox isolation must block network operations
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        src = tmp_path / "Main.java"
        src.write_text("""
        public class Main {
            public static void main(String[] args) {
                try {
                    java.net.Socket s = new java.net.Socket("1.1.1.1", 80);
                    System.out.println("Connected");
                } catch (Exception e) {
                    System.err.println("Network Error: " + e.getMessage());
                }
            }
        }
        """)
        import subprocess
        subprocess.run(["javac", str(src)], check=True)

        exec_res = execute_java_sandboxed(
            class_dir=tmp_path,
            class_name="Main",
            disable_network=True
        )
        # Sandbox must block network access: stdout must NOT contain "Connected"
        assert "Connected" not in exec_res.stdout
        # And stderr or security violation must record the block
        assert exec_res.security_violation is True or "Network Error" in exec_res.stderr


# ==============================================================================
# 10. Platform Whitespace & Float Tolerance Comparison
# ==============================================================================
def test_whitespace_and_float_comparison():
    # Trailing line whitespace ignored
    assert compare_sample_outputs("1 2 3   \n4 5\n", "1 2 3\n4 5")[0] is True

    # Float tolerance within 1e-6 accepted
    assert compare_sample_outputs("3.14159265", "3.141593")[0] is True

    # Float tolerance exceeded rejected
    assert compare_sample_outputs("3.140", "3.145")[0] is False

    # Line count mismatch rejected (significant formatting)
    ok, err = compare_sample_outputs("1 2 3 4", "1 2\n3 4")
    assert ok is False
    assert "Line count mismatch" in (err or "")

    # Token mismatch rejected
    ok2, err2 = compare_sample_outputs("YES", "NO")
    assert ok2 is False
    assert "mismatch" in (err2 or "")


# ==============================================================================
# 11. Database Persistence of Diagnostics & Sample Test Cases
# ==============================================================================
def test_database_persistence_of_compiler_and_sample_diagnostics(temp_db):
    temp_db.claim_problem("codeforces", "4A", "https://codeforces.com/problemset/problem/4/A", "worker1")

    sample_results = [
        SampleTestResult(
            sample_index=1,
            passed=True,
            input_text="8",
            expected_output="YES",
            actual_output="YES"
        ),
        SampleTestResult(
            sample_index=2,
            passed=False,
            input_text="2",
            expected_output="NO",
            actual_output="YES",
            error_message="Mismatch on line 1",
            diff="Line 1 mismatch: expected 'NO', got 'YES'"
        )
    ]

    attempt_id = temp_db.record_attempt(
        platform="codeforces",
        canonical_id="4A",
        run_id=None,
        stage=ProcessStage.COMPILED,
        mode="both",
        solution_code="public class Main {}",
        explanation="Attempted solution",
        error="Sample 2 failed",
        compiler_output="javac diagnostics log",
        diagnostics="Exact diagnostic diff"
    )

    temp_db.record_sample_test_results(attempt_id, sample_results)

    with temp_db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT compiler_output, diagnostics FROM attempts WHERE id = ?", (attempt_id,))
        att_row = cursor.fetchone()
        assert att_row["compiler_output"] == "javac diagnostics log"
        assert att_row["diagnostics"] == "Exact diagnostic diff"

        cursor.execute("SELECT sample_index, passed, diff FROM sample_test_results WHERE attempt_id = ? ORDER BY sample_index", (attempt_id,))
        rows = cursor.fetchall()
        assert len(rows) == 2
        assert rows[0]["passed"] == 1
        assert rows[1]["passed"] == 0
        assert "Line 1 mismatch" in rows[1]["diff"]
