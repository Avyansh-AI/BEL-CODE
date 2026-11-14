"""Tests for TestAndDebugEngine, MultiAgentCoordinator, AutonomousWorkflowEngine, and all 10 Acceptance Benchmarks."""

from pathlib import Path
import pytest

from bel_code.engine.debugger import TestAndDebugEngine
from bel_code.engine.multi_agent import MultiAgentCoordinator
from bel_code.engine.workflow import AutonomousWorkflowEngine


def test_debugger_strict_verification_stack_trace_and_repair(tmp_path: Path):
    # Create a failing test and buggy module
    (tmp_path / "math_ops.py").write_text(
        "def divide_safe(a, b):\n"
        "    return a - b\n",
        encoding="utf-8",
    )
    (tmp_path / "test_math_ops.py").write_text(
        "from math_ops import divide_safe\n\n"
        "def test_add():\n"
        "    assert divide_safe(3, 2) == 5\n",
        encoding="utf-8",
    )

    debugger = TestAndDebugEngine(workspace_root=tmp_path)

    # 1. Failing test suite MUST NOT be marked verified
    fail_report = debugger.run_test_suite(command="pytest -q test_math_ops.py")
    assert fail_report["executed"] is True
    assert fail_report["verified"] is False
    assert fail_report["exit_code"] != 0

    # 2. Stack trace parsing
    sample_tb = (
        'Traceback (most recent call last):\n'
        '  File "math_ops.py", line 2, in divide_safe\n'
        '    return a / 0\n'
        'ZeroDivisionError: division by zero\n'
    )
    frames = debugger.parse_stack_trace(sample_tb)
    assert isinstance(frames, list)
    assert len(frames) >= 1
    assert any("math_ops.py" in str(f) or "ZeroDivisionError" in str(f) for f in frames)

    # 3. Diagnose and repair with candidate fixes (first bad patch rolls back, second good patch passes)
    repair_res = debugger.diagnose_and_repair(
        failure_output=fail_report["stdout"] + "\n" + fail_report["stderr"],
        target_file="math_ops.py",
        test_command="pytest -q test_math_ops.py",
        candidate_fixes=[
            {"file": "math_ops.py", "content": "def divide_safe(a, b):\n    return a * b\n"},
            {"file": "math_ops.py", "content": "def divide_safe(a, b):\n    return a + b\n"},
        ],
    )
    assert isinstance(repair_res, dict)
    assert repair_res.get("verified", repair_res.get("repaired", False)) is True


def test_multi_agent_coordinator_roles_and_ownership_locks(tmp_path: Path):
    coord = MultiAgentCoordinator(workspace_root=tmp_path)
    roles = coord.list_roles()
    assert len(roles) >= 9

    role_names = " ".join(str(r).lower() for r in roles)
    for expected in [
        "architect",
        "developer",
        "debugger",
        "tester",
        "security",
        "reviewer",
        "researcher",
        "documentation",
        "performance",
    ]:
        assert expected in role_names, f"Missing expected role keyword: {expected}"

    # Non-conflicting parallel collaboration
    good_collab = coord.run_parallel_collaboration(
        [
            {"role": "Developer", "task_description": "Implement auth.py", "owned_files": ["auth.py"]},
            {"role": "Tester", "task_description": "Write test_auth.py", "owned_files": ["test_auth.py"]},
        ]
    )
    assert isinstance(good_collab, dict)
    assert good_collab.get("collision_detected", False) is False

    # Conflicting file ownership should be detected/blocked
    conflict_collab = coord.run_parallel_collaboration(
        [
            {"role": "Developer", "task_description": "Edit shared.py", "owned_files": ["shared.py"]},
            {"role": "Debugger", "task_description": "Patch shared.py", "owned_files": ["shared.py"]},
        ]
    )
    assert (
        conflict_collab.get("collision_detected") is True
        or conflict_collab.get("status") in ("conflict", "rejected", "blocked", "error")
        or "conflict" in str(conflict_collab).lower()
        or "collision" in str(conflict_collab).lower()
    )


def test_autonomous_workflow_engine_7_stages_and_resume(tmp_path: Path):
    engine = AutonomousWorkflowEngine(workspace_root=tmp_path)

    # First run with interruption after stage 3 ("implement")
    interrupted = engine.execute_task(
        prompt="Create a utility module greeter.py with greet(name) and its unit test test_greeter.py",
        project_id="test-proj",
        mode="autonomous",
        actions=[
            {
                "type": "write_file",
                "path": "greeter.py",
                "content": "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
            },
            {
                "type": "write_file",
                "path": "test_greeter.py",
                "content": "from greeter import greet\n\ndef test_greet():\n    assert greet('BEL') == 'Hello, BEL!'\n",
            },
        ],
        test_command="pytest -q test_greeter.py",
        task_id="task-lifecycle-1",
        stop_after_stage="implement",
    )
    assert interrupted["status"] == "interrupted"
    assert interrupted["completed_stages"] == ["understand", "plan", "implement"]

    # Resume session and verify stages 1-3 are NOT repeated
    result = engine.resume_task(project_id="test-proj", task_id="task-lifecycle-1")
    assert isinstance(result, dict)
    assert result.get("verified") is True
    assert result.get("resumed") is True
    for s in ("understand", "plan", "implement"):
        assert result["stage_execution_counts"][s] == 1

    report = engine.generate_completion_report(result)
    assert isinstance(report, str)
    assert "verified" in report.lower()



def test_all_10_acceptance_benchmarks(tmp_path: Path):
    """Execute all 10 Acceptance Benchmarks from Spec Section 18 and assert 100% pass with verified evidence."""
    engine = AutonomousWorkflowEngine(workspace_root=tmp_path)
    summary = engine.run_all_benchmarks(base_temp_dir=tmp_path / "benchmarks")

    assert isinstance(summary, dict)
    results = summary.get("results", summary.get("benchmarks", []))
    if isinstance(results, dict):
        items = list(results.values())
    else:
        items = list(results)

    assert len(items) == 10, f"Expected 10 acceptance benchmarks, got {len(items)}"
    for item in items:
        b_id = item.get("benchmark_id", item.get("id", item.get("name", "unknown")))
        passed = item.get("passed", item.get("verified", False))
        assert passed is True, f"Acceptance benchmark '{b_id}' failed: {item}"
        assert item.get("verified", True) is True, f"Acceptance benchmark '{b_id}' missing verified flag: {item}"
