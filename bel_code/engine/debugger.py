"""Debugging, Diagnosis, Repair, and Test Verification Engine for BEL Code.

Implements Specification Sections 6 (Debugging, Diagnosis & Repair) and
11 (Testing, Quality Assurance & Verification).
"""

from __future__ import annotations

import ast
import copy
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


class TestAndDebugEngine:
    """Executes verification test suites, parses stack traces, and performs
    automated root-cause diagnosis and checkpoint-guarded repair.
    """

    __test__ = False

    def __init__(
        self,
        workspace_root: Union[str, Path],
        terminal: Any = None,
        fs: Any = None,
        git: Any = None,
        provider_manager: Any = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.terminal = terminal
        self.fs = fs
        self.git = git
        self.provider_manager = provider_manager
        self._diagnostic_history: List[Dict[str, Any]] = []

    def _get_git(self) -> Any:
        if self.git is not None:
            return self.git
        try:
            from bel_code.git_ops.manager import GitManager

            self.git = GitManager(self.workspace_root)
        except Exception:
            self.git = None
        return self.git

    def _get_fs(self) -> Any:
        if self.fs is not None:
            return self.fs
        try:
            from bel_code.tools.filesystem import FileSystemTools

            self.fs = FileSystemTools(self.workspace_root)
        except Exception:
            self.fs = None
        return self.fs

    def detect_test_command(self, target: Optional[str] = None) -> str:
        """Auto-detect the appropriate test runner command for the workspace."""
        py_exe = f'"{sys.executable}"' if " " in sys.executable else sys.executable

        if target:
            if target.endswith(".js") or target.endswith(".ts"):
                return f"npm test -- {target}"
            return f"{py_exe} -m pytest {target}"

        has_py_tests = any(
            p.name.startswith("test_") or p.name.endswith("_test.py")
            for p in self.workspace_root.rglob("*.py")
            if ".git" not in p.parts and ".venv" not in p.parts and "node_modules" not in p.parts
        )
        if (
            (self.workspace_root / "pytest.ini").exists()
            or (self.workspace_root / "pyproject.toml").exists()
            or has_py_tests
        ):
            return f"{py_exe} -m pytest"

        if (self.workspace_root / "package.json").exists():
            return "npm test"

        return f"{py_exe} -m unittest discover"

    def _execute_command(self, command: str, timeout: int = 120) -> Dict[str, Any]:
        """Execute a command via configured TerminalExecutor or subprocess fallback."""
        start_time = time.perf_counter()

        if self.terminal is not None:
            for method_name in ("execute", "run_command", "run"):
                method = getattr(self.terminal, method_name, None)
                if callable(method):
                    try:
                        res = method(command, timeout=timeout)
                    except TypeError:
                        res = method(command)
                    if isinstance(res, dict):
                        executed = bool(res.get("executed", True))
                        if res.get("status") in ("blocked", "denied", "awaiting_approval", "not_executed"):
                            executed = False
                        if res.get("allowed") is False:
                            executed = False
                        return {
                            "executed": executed,
                            "exit_code": int(res.get("exit_code", -1 if not executed else 0)),
                            "stdout": str(res.get("stdout", "")),
                            "stderr": str(res.get("stderr", "")),
                            "duration_ms": float(
                                res.get("duration_ms", (time.perf_counter() - start_time) * 1000.0)
                            ),
                        }
                    return {
                        "executed": bool(getattr(res, "executed", True)),
                        "exit_code": int(getattr(res, "exit_code", getattr(res, "returncode", -1))),
                        "stdout": str(getattr(res, "stdout", "")),
                        "stderr": str(getattr(res, "stderr", "")),
                        "duration_ms": float(
                            getattr(res, "duration_ms", (time.perf_counter() - start_time) * 1000.0)
                        ),
                    }

        if not self.workspace_root.exists():
            return {
                "executed": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Workspace root does not exist: {self.workspace_root}",
                "duration_ms": 0.0,
            }

        try:
            proc = subprocess.run(
                command,
                cwd=str(self.workspace_root),
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            return {
                "executed": True,
                "exit_code": int(proc.returncode),
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "",
                "duration_ms": duration_ms,
            }
        except subprocess.TimeoutExpired as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            return {
                "executed": True,
                "exit_code": 124,
                "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
                "stderr": f"Command timed out after {timeout}s",
                "duration_ms": duration_ms,
            }
        except Exception as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            return {
                "executed": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "duration_ms": duration_ms,
            }

    @staticmethod
    def _parse_test_counts(combined_output: str, exit_code: int, executed: bool) -> Tuple[int, int, int, Optional[float]]:
        """Extract passed, failed, skipped counts and coverage percentage from test output."""
        if not executed:
            return 0, 0, 0, None

        passed = 0
        failed = 0
        skipped = 0
        coverage_pct: Optional[float] = None

        # Pytest summary patterns: e.g. "3 passed, 1 failed, 2 skipped in 0.12s"
        m_passed = re.findall(r"(\d+)\s+passed", combined_output)
        m_failed = re.findall(r"(\d+)\s+failed", combined_output)
        m_errors = re.findall(r"(\d+)\s+errors?\b", combined_output)
        m_skipped = re.findall(r"(\d+)\s+skipped", combined_output)

        if m_passed:
            passed = int(m_passed[-1])
        if m_failed:
            failed = int(m_failed[-1])
        if m_errors and "=== ERRORS ===" in combined_output or " error in " in combined_output or " errors in " in combined_output:
            failed += int(m_errors[-1])
        if m_skipped:
            skipped = int(m_skipped[-1])

        # Unittest summary pattern: "Ran 4 tests in 0.002s"
        if passed == 0 and failed == 0:
            m_ran = re.search(r"Ran\s+(\d+)\s+tests?\s+in\s+[\d.]+s", combined_output)
            if m_ran:
                total_ran = int(m_ran.group(1))
                m_fail_u = re.search(r"failures=(\d+)", combined_output)
                m_err_u = re.search(r"errors=(\d+)", combined_output)
                m_skip_u = re.search(r"skipped=(\d+)", combined_output)
                u_fails = (int(m_fail_u.group(1)) if m_fail_u else 0) + (int(m_err_u.group(1)) if m_err_u else 0)
                skipped = int(m_skip_u.group(1)) if m_skip_u else 0
                if exit_code == 0 and u_fails == 0:
                    passed = max(0, total_ran - skipped)
                else:
                    failed = max(1, u_fails)
                    passed = max(0, total_ran - failed - skipped)

        # Coverage pattern: "TOTAL ... 92%" or "coverage: 88.5%"
        m_cov = re.search(r"TOTAL\s+\d+\s+\d+(?:\s+\d+\s+\d+)?\s+(\d+(?:\.\d+)?)%", combined_output)
        if not m_cov:
            m_cov = re.search(r"coverage[:\s]+(\d+(?:\.\d+)?)%", combined_output, re.IGNORECASE)
        if m_cov:
            try:
                coverage_pct = float(m_cov.group(1))
            except ValueError:
                coverage_pct = None

        has_output = bool(combined_output.strip())
        if exit_code == 0 and passed == 0 and failed == 0 and has_output:
            passed = 1
        elif exit_code != 0 and failed == 0:
            failed = 1

        return passed, failed, skipped, coverage_pct

    def run_test_suite(
        self,
        command: Optional[str] = None,
        target: Optional[str] = None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """Run the test suite, static type check, or linter inside workspace_root.

        STRICT VERIFICATION RULE:
        `verified` is True ONLY if the command was actually executed in the environment
        AND `exit_code == 0` with captured stdout/stderr log evidence.
        Never reports unexecuted or failing tests as verified.
        """
        if command is not None and not str(command).strip():
            return {
                "executed": False,
                "verified": False,
                "command": "",
                "exit_code": -1,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "coverage_pct": None,
                "stdout": "",
                "stderr": "No command provided.",
                "duration_ms": 0.0,
                "status": "not_executed",
            }

        cmd_to_run = command.strip() if command else self.detect_test_command(target=target)
        if target and command and target not in cmd_to_run:
            cmd_to_run = f"{cmd_to_run} {target}"

        # Normalize bare 'pytest' to run with current Python interpreter so virtualenvs on Windows work reliably
        if cmd_to_run == "pytest" or cmd_to_run.startswith("pytest "):
            py_exe = f'"{sys.executable}"' if " " in sys.executable else sys.executable
            cmd_to_run = f"{py_exe} -m {cmd_to_run}"

        exec_res = self._execute_command(cmd_to_run, timeout=timeout)
        executed = bool(exec_res.get("executed", False))
        exit_code = int(exec_res.get("exit_code", -1))
        stdout = str(exec_res.get("stdout", ""))
        stderr = str(exec_res.get("stderr", ""))
        duration_ms = float(exec_res.get("duration_ms", 0.0))

        combined = f"{stdout}\n{stderr}".strip()
        passed, failed, skipped, coverage_pct = self._parse_test_counts(combined, exit_code, executed)

        # STRICT VERIFICATION RULE:
        # Must be executed, exit_code == 0, failed == 0, and have non-empty captured stdout/stderr evidence
        has_log_evidence = bool(stdout.strip() or stderr.strip())
        verified = bool(executed and exit_code == 0 and failed == 0 and has_log_evidence)

        if not executed:
            status = "not_executed"
        elif verified:
            status = "passed"
        else:
            status = "failed"

        return {
            "executed": executed,
            "verified": verified,
            "command": cmd_to_run,
            "exit_code": exit_code,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "coverage_pct": coverage_pct,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": round(duration_ms, 2),
            "status": status,
        }

    def _read_context_around_line(self, file_path: str, line_no: int, window: int = 2) -> str:
        """Read source lines around `line_no` if file exists in workspace."""
        try:
            candidate = Path(file_path)
            if not candidate.is_absolute():
                candidate = (self.workspace_root / candidate).resolve()
            if candidate.exists() and candidate.is_file():
                lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
                if 1 <= line_no <= len(lines):
                    start = max(0, line_no - 1 - window)
                    end = min(len(lines), line_no + window)
                    return "\n".join(lines[start:end]).strip()
        except Exception:
            pass
        return ""

    def parse_stack_trace(self, error_output: str) -> List[Dict[str, Any]]:
        """Parse Python tracebacks, syntax/compiler errors, pytest failures, and JS/TS stack traces
        into structured frames (`file`, `line`, `function`, `error_type`, `error_message`, `code_context`).
        """
        if not error_output or not error_output.strip():
            return []

        frames: List[Dict[str, Any]] = []
        lines = error_output.splitlines()

        # Find the primary error type & message (from Python traceback or pytest `E   ErrorType: msg` or JS error)
        global_error_type = "RuntimeError"
        global_error_message = ""

        # Check pytest `E   ErrorType: msg` lines first or standard final exception line
        exc_pattern = re.compile(
            r"^(?:E\s+)?([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning|Failure|Exit|Interrupt)):\s*(.*)$"
        )
        for line in reversed(lines):
            stripped = line.strip()
            m_exc = exc_pattern.match(stripped)
            if m_exc:
                global_error_type = m_exc.group(1)
                global_error_message = m_exc.group(2).strip()
                break

        # 1. Standard Python traceback frames: File "path", line 123, in func_name
        py_frame_re = re.compile(r'^\s*File\s+"([^"]+)",\s+line\s+(\d+)(?:,\s+in\s+([^\s]+))?')
        i = 0
        while i < len(lines):
            m_py = py_frame_re.match(lines[i])
            if m_py:
                file_path = m_py.group(1)
                line_num = int(m_py.group(2))
                func_name = m_py.group(3) or "<module>"
                code_ctx = ""
                if i + 1 < len(lines) and not py_frame_re.match(lines[i + 1]):
                    next_line = lines[i + 1].strip()
                    if not exc_pattern.match(next_line):
                        code_ctx = next_line
                if not code_ctx:
                    code_ctx = self._read_context_around_line(file_path, line_num)

                frames.append(
                    {
                        "file": file_path,
                        "line": line_num,
                        "function": func_name,
                        "error_type": global_error_type,
                        "error_message": global_error_message,
                        "code_context": code_ctx,
                    }
                )
            i += 1

        # 2. Pytest short/concise traceback format:
        # e.g. `test_app.py:14: AssertionError` or `app.py:22: in calculate_total`
        pytest_loc_re = re.compile(r"^([^\s:]+\.py):(\d+):\s*(?:in\s+([A-Za-z0-9_<>]+)|([A-Za-z0-9_.]*Error.*)?)$")
        for idx, line in enumerate(lines):
            stripped = line.strip()
            m_pt = pytest_loc_re.match(stripped)
            if m_pt:
                file_path = m_pt.group(1)
                line_num = int(m_pt.group(2))
                func_name = m_pt.group(3) or "<test>"
                inline_err = (m_pt.group(4) or "").strip()

                err_type = global_error_type
                err_msg = global_error_message
                if inline_err:
                    if ":" in inline_err:
                        err_type, err_msg = [p.strip() for p in inline_err.split(":", 1)]
                    else:
                        err_type = inline_err

                # Look backwards or forwards for code context / `E   ...` lines
                code_ctx_parts: List[str] = []
                for look_idx in range(max(0, idx - 4), min(len(lines), idx + 4)):
                    l_str = lines[look_idx].strip()
                    if l_str.startswith(">"):
                        code_ctx_parts.append(l_str.lstrip("> ").strip())
                    elif l_str.startswith("E "):
                        m_e = exc_pattern.match(l_str)
                        if m_e:
                            err_type = m_e.group(1)
                            err_msg = m_e.group(2).strip()
                code_ctx = " | ".join(code_ctx_parts) if code_ctx_parts else self._read_context_around_line(file_path, line_num)

                if not any(f["file"].endswith(file_path) and f["line"] == line_num for f in frames):
                    frames.append(
                        {
                            "file": file_path,
                            "line": line_num,
                            "function": func_name,
                            "error_type": err_type,
                            "error_message": err_msg,
                            "code_context": code_ctx,
                        }
                    )

        # 3. JavaScript / TypeScript stack traces:
        # e.g. `at funcName (/path/to/file.ts:45:12)` or `at /path/to/file.js:10:5`
        js_frame_re = re.compile(
            r"^\s*at\s+(?:([^\s(]+)\s+\()?([^\s()]+\.(?:js|jsx|ts|tsx|mjs|cjs)):(\d+)(?::(\d+))?\)?"
        )
        for line in lines:
            m_js = js_frame_re.match(line)
            if m_js:
                func_name = m_js.group(1) or "<anonymous>"
                file_path = m_js.group(2)
                line_num = int(m_js.group(3))
                code_ctx = self._read_context_around_line(file_path, line_num)
                frames.append(
                    {
                        "file": file_path,
                        "line": line_num,
                        "function": func_name,
                        "error_type": global_error_type,
                        "error_message": global_error_message,
                        "code_context": code_ctx,
                    }
                )

        return frames

    def _resolve_target_file(self, target_file: Optional[str], frames: List[Dict[str, Any]]) -> Optional[Path]:
        """Identify the primary source file to repair from target_file or parsed stack frames."""
        if target_file:
            p = Path(target_file)
            if not p.is_absolute():
                p = (self.workspace_root / p).resolve()
            if p.exists() and p.is_file():
                return p

        # Prefer non-test source files inside workspace_root from the innermost stack frame outward
        non_test_candidates: List[Path] = []
        test_candidates: List[Path] = []

        for frame in reversed(frames):
            raw_file = frame.get("file", "")
            if not raw_file:
                continue
            p = Path(raw_file)
            if not p.is_absolute():
                p = (self.workspace_root / p).resolve()
            if p.exists() and p.is_file():
                try:
                    p.relative_to(self.workspace_root)
                except ValueError:
                    continue
                if p.name.startswith("test_") or p.name.endswith("_test.py"):
                    test_candidates.append(p)
                else:
                    non_test_candidates.append(p)

        if non_test_candidates:
            return non_test_candidates[0]

        # If only test files appeared in the traceback (e.g. assertion failure on return value),
        # look for non-test .py files in workspace_root imported by the test file
        if test_candidates:
            test_src = test_candidates[0].read_text(encoding="utf-8", errors="replace")
            for m_imp in re.finditer(r"^(?:from|import)\s+([A-Za-z0-9_.]+)", test_src, re.MULTILINE):
                mod_name = m_imp.group(1).split(".")[0]
                candidate_py = (self.workspace_root / f"{mod_name}.py").resolve()
                if candidate_py.exists():
                    return candidate_py

        # Fallback: first non-test .py file in workspace_root
        for py_file in sorted(self.workspace_root.rglob("*.py")):
            if ".git" in py_file.parts or ".venv" in py_file.parts or "__pycache__" in py_file.parts:
                continue
            if not py_file.name.startswith("test_") and not py_file.name.endswith("_test.py") and py_file.name != "__init__.py":
                return py_file.resolve()

        return test_candidates[0] if test_candidates else None

    def _generate_heuristic_fixes(
        self,
        source_file: Path,
        source_text: str,
        failure_output: str,
        frames: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Deterministic AST & heuristic repair analyzers for common software bug patterns."""
        candidates: List[Dict[str, Any]] = []
        lines = source_text.splitlines()

        # Find suspected line numbers in source_file
        suspected_lines: List[int] = []
        for f in reversed(frames):
            try:
                fp = Path(f["file"])
                if not fp.is_absolute():
                    fp = (self.workspace_root / fp).resolve()
                if fp.resolve() == source_file.resolve():
                    suspected_lines.append(int(f["line"]))
            except Exception:
                pass

        # 1. SyntaxError repair (missing colon, Python 2 print, unclosed paren)
        if "SyntaxError" in failure_output:
            for ln in suspected_lines or range(1, len(lines) + 1):
                if 1 <= ln <= len(lines):
                    orig_line = lines[ln - 1]
                    stripped = orig_line.strip()
                    # Missing colon on def/if/elif/else/for/while/class/try/except
                    if re.match(r"^(def |class |if |elif |else|for |while |try|except|finally)\b", stripped) and not stripped.endswith(":"):
                        new_lines = list(lines)
                        new_lines[ln - 1] = orig_line.rstrip() + ":"
                        candidates.append(
                            {
                                "file": str(source_file),
                                "content": "\n".join(new_lines) + ("\n" if source_text.endswith("\n") else ""),
                                "description": f"Add missing colon on line {ln}",
                            }
                        )
                    # Python 2 print statement
                    m_print = re.match(r"^(\s*)print\s+([^(].*)$", orig_line)
                    if m_print:
                        new_lines = list(lines)
                        new_lines[ln - 1] = f"{m_print.group(1)}print({m_print.group(2).strip()})"
                        candidates.append(
                            {
                                "file": str(source_file),
                                "content": "\n".join(new_lines) + ("\n" if source_text.endswith("\n") else ""),
                                "description": f"Convert Python 2 print statement to print() on line {ln}",
                            }
                        )

        # 2. Missing import (NameError: name 'X' is not defined)
        m_name = re.search(r"NameError:\s+name\s+'([A-Za-z0-9_]+)'\s+is\s+not\s+defined", failure_output)
        if m_name:
            missing_sym = m_name.group(1)
            stdlib_mods = {"math", "os", "sys", "re", "json", "time", "datetime", "pathlib", "collections", "itertools", "functools", "hashlib"}
            if missing_sym in stdlib_mods:
                candidates.append(
                    {
                        "file": str(source_file),
                        "content": f"import {missing_sym}\n" + source_text,
                        "description": f"Add missing `import {missing_sym}`",
                    }
                )
            elif missing_sym in {"Path"}:
                candidates.append(
                    {
                        "file": str(source_file),
                        "content": "from pathlib import Path\n" + source_text,
                        "description": "Add missing `from pathlib import Path`",
                    }
                )

        # 3. ZeroDivisionError repair
        if "ZeroDivisionError" in failure_output:
            for ln in suspected_lines or range(1, len(lines) + 1):
                if 1 <= ln <= len(lines):
                    orig_line = lines[ln - 1]
                    if "/" in orig_line and "//" not in orig_line:
                        indent = re.match(r"^(\s*)", orig_line).group(1)
                        m_div = re.search(r"/\s*([A-Za-z_][A-Za-z0-9_.]*|len\([^)]+\))", orig_line)
                        if m_div:
                            denom = m_div.group(1)
                            new_lines = list(lines)
                            new_lines.insert(ln - 1, f"{indent}if {denom} == 0:\n{indent}    return 0")
                            candidates.append(
                                {
                                    "file": str(source_file),
                                    "content": "\n".join(new_lines) + ("\n" if source_text.endswith("\n") else ""),
                                    "description": f"Guard against ZeroDivisionError when `{denom} == 0` on line {ln}",
                                }
                            )

        # 4. Off-by-one IndexError in range(len(...) + 1)
        if "IndexError" in failure_output:
            for idx, line in enumerate(lines):
                if "range(len(" in line and "+ 1)" in line:
                    new_lines = list(lines)
                    new_lines[idx] = line.replace("+ 1)", ")")
                    candidates.append(
                        {
                            "file": str(source_file),
                            "content": "\n".join(new_lines) + ("\n" if source_text.endswith("\n") else ""),
                            "description": f"Fix off-by-one range bound on line {idx + 1}",
                        }
                    )

        # 5. Targeted operator / boundary mutations on suspected function or return statements
        target_line_indices: List[int] = [ln - 1 for ln in suspected_lines if 1 <= ln <= len(lines)]
        if not target_line_indices:
            target_line_indices = list(range(len(lines)))

        # Prioritized operator swaps on return / assignment lines
        op_swaps = [
            (" - ", " + ", "Replace subtraction with addition"),
            (" + ", " - ", "Replace addition with subtraction"),
            (" * ", " / ", "Replace multiplication with division"),
            (" / ", " * ", "Replace division with multiplication"),
            (" + 1", "", "Remove off-by-one + 1"),
            (" - 1", "", "Remove off-by-one - 1"),
            (" <= ", " < ", "Replace <= with <"),
            (" < ", " <= ", "Replace < with <="),
            (" >= ", " > ", "Replace >= with >"),
            (" > ", " >= ", "Replace > with >="),
            (" == ", " != ", "Replace == with !="),
            (" != ", " == ", "Replace != with =="),
        ]

        # Focus first on return lines or arithmetic assignment lines
        for idx in range(len(lines)):
            line = lines[idx]
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("def ") or stripped.startswith("class "):
                continue
            for old_op, new_op, desc in op_swaps:
                if old_op in line:
                    new_lines = list(lines)
                    new_lines[idx] = line.replace(old_op, new_op, 1)
                    candidates.append(
                        {
                            "file": str(source_file),
                            "content": "\n".join(new_lines) + ("\n" if source_text.endswith("\n") else ""),
                            "description": f"{desc} on line {idx + 1}",
                        }
                    )

        return candidates

    def diagnose_and_repair(
        self,
        failure_output: str,
        target_file: Optional[str] = None,
        test_command: Optional[str] = None,
        candidate_fixes: Optional[List[Dict[str, str]]] = None,
        max_attempts: int = 3,
    ) -> Dict[str, Any]:
        """Trace failures to root causes, generate candidate fixes, create a checkpoint before
        each patch, re-run the test suite, and roll back cleanly if a patch fails or regresses.
        """
        frames = self.parse_stack_trace(failure_output)
        resolved_file = self._resolve_target_file(target_file, frames)

        error_type = frames[-1]["error_type"] if frames else "TestFailure"
        error_msg = frames[-1]["error_message"] if frames else failure_output.strip().splitlines()[-1] if failure_output.strip() else "Unknown failure"

        diagnostic_notes: List[str] = [
            f"Parsed {len(frames)} stack frame(s) from failure output.",
            f"Identified primary error: {error_type}: {error_msg}",
        ]
        if resolved_file:
            diagnostic_notes.append(f"Targeted file for root-cause inspection: {resolved_file.name}")

        # Build combined list of candidate fixes: caller-supplied first, then heuristic/AST fixes
        all_candidates: List[Dict[str, Any]] = []
        if candidate_fixes:
            for cf in candidate_fixes:
                all_candidates.append(dict(cf))

        if resolved_file and resolved_file.exists():
            orig_source = resolved_file.read_text(encoding="utf-8", errors="replace")
            heuristic = self._generate_heuristic_fixes(resolved_file, orig_source, failure_output, frames)
            all_candidates.extend(heuristic)

        # Snapshot original state of all non-git files in workspace so rollback is 100% lossless
        original_file_snapshots: Dict[Path, bytes] = {}
        for p in self.workspace_root.rglob("*"):
            if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts and ".venv" not in p.parts:
                try:
                    original_file_snapshots[p.resolve()] = p.read_bytes()
                except Exception:
                    pass

        git_mgr = self._get_git()
        checkpoints: List[str] = []
        attempts_log: List[Dict[str, Any]] = []
        rollbacks_performed = 0

        # If candidate_fixes was not explicitly passed, allow scanning through our generated AST mutations
        # while capping recorded repair attempts or evaluating up to max(max_attempts, len(heuristic)) when needed
        effective_candidates = all_candidates[: max(max_attempts, 12 if candidate_fixes is None else max_attempts)]
        attempt_count = 0

        for idx, fix in enumerate(effective_candidates):
            if candidate_fixes is not None and attempt_count >= max_attempts:
                break
            if candidate_fixes is None and attempt_count >= max(max_attempts, len(effective_candidates)):
                break

            fix_file_raw = fix.get("file") or fix.get("path") or (str(resolved_file) if resolved_file else None)
            if not fix_file_raw:
                continue
            fix_path = Path(fix_file_raw)
            if not fix_path.is_absolute():
                fix_path = (self.workspace_root / fix_path).resolve()
            if not fix_path.exists():
                continue

            pre_attempt_bytes = fix_path.read_bytes()
            pre_attempt_text = pre_attempt_bytes.decode("utf-8", errors="replace")

            # Compute new content
            new_text: Optional[str] = None
            if "content" in fix or "new_content" in fix:
                new_text = fix.get("content") or fix.get("new_content")
            else:
                target_str = fix.get("target") or fix.get("old") or fix.get("search")
                repl_str = fix.get("replacement") if "replacement" in fix else (fix.get("new") if "new" in fix else fix.get("replace"))
                if target_str is not None and repl_str is not None and target_str in pre_attempt_text:
                    new_text = pre_attempt_text.replace(target_str, repl_str, 1)

            if new_text is None or new_text == pre_attempt_text:
                continue

            attempt_count += 1
            ckpt_label = f"repair_attempt_{attempt_count}"
            ckpt_id = ckpt_label
            if git_mgr is not None and hasattr(git_mgr, "create_checkpoint"):
                try:
                    ckpt_res = git_mgr.create_checkpoint(ckpt_label, task_id="repair")
                    ckpt_id = ckpt_res.get("checkpoint_id", ckpt_label) if isinstance(ckpt_res, dict) else str(ckpt_res)
                except Exception:
                    ckpt_id = ckpt_label
            checkpoints.append(ckpt_id)

            # Apply candidate patch
            fix_path.write_text(new_text, encoding="utf-8")
            desc = fix.get("description", f"Candidate fix #{attempt_count} on {fix_path.name}")
            diagnostic_notes.append(f"Attempt {attempt_count}: Applied '{desc}' (checkpoint: {ckpt_id}).")

            # Re-run test suite to verify fix
            test_res = self.run_test_suite(command=test_command)
            if test_res.get("verified") and test_res.get("exit_code") == 0:
                attempts_log.append(
                    {
                        "attempt": attempt_count,
                        "description": desc,
                        "file": str(fix_path),
                        "checkpoint_id": ckpt_id,
                        "verified": True,
                        "rolled_back": False,
                        "test_result": test_res,
                    }
                )
                diagnostic_notes.append(
                    f"Attempt {attempt_count} PASSED verification ({test_res.get('passed')} passed, exit_code=0)."
                )
                result = {
                    "repaired": True,
                    "verified": True,
                    "status": "repaired",
                    "root_cause": f"{error_type}: {error_msg}",
                    "suspected_file": str(fix_path),
                    "frames": frames,
                    "attempts": len(attempts_log),
                    "attempts_detail": attempts_log,
                    "max_attempts": max_attempts,
                    "applied_fix": {
                        "description": desc,
                        "file": str(fix_path),
                        "checkpoint_id": ckpt_id,
                    },
                    "test_result": test_res,
                    "checkpoints": checkpoints,
                    "rollbacks_performed": rollbacks_performed,
                    "diagnostic_notes": diagnostic_notes,
                    "escalation_required": False,
                }
                self._diagnostic_history.append(result)
                return result

            # Patch failed or introduced regression -> Roll back cleanly to checkpoint!
            if git_mgr is not None and hasattr(git_mgr, "rollback_to_checkpoint"):
                try:
                    git_mgr.rollback_to_checkpoint(ckpt_id, preserve_uncommitted=True)
                except Exception:
                    pass
            # Always guarantee exact byte-for-byte restoration of the patched file
            fix_path.write_bytes(pre_attempt_bytes)
            rollbacks_performed += 1
            diagnostic_notes.append(
                f"Attempt {attempt_count} failed verification (exit_code={test_res.get('exit_code')}); rolled back to checkpoint {ckpt_id}."
            )
            attempts_log.append(
                {
                    "attempt": attempt_count,
                    "description": desc,
                    "file": str(fix_path),
                    "checkpoint_id": ckpt_id,
                    "verified": False,
                    "rolled_back": True,
                    "test_result": test_res,
                }
            )

            if attempt_count >= max_attempts and candidate_fixes is not None:
                break

        # Ensure all original files are restored cleanly if repair did not succeed
        for orig_path, orig_bytes in original_file_snapshots.items():
            if orig_path.exists() and orig_path.read_bytes() != orig_bytes:
                orig_path.write_bytes(orig_bytes)

        diagnostic_notes.append(
            "Automated repair exhausted bounded attempts without green verification; escalating for human architectural input."
        )
        result = {
            "repaired": False,
            "verified": False,
            "status": "escalated",
            "root_cause": f"{error_type}: {error_msg}",
            "suspected_file": str(resolved_file) if resolved_file else None,
            "frames": frames,
            "attempts": len(attempts_log),
            "attempts_detail": attempts_log,
            "max_attempts": max_attempts,
            "applied_fix": None,
            "test_result": attempts_log[-1]["test_result"] if attempts_log else None,
            "checkpoints": checkpoints,
            "rollbacks_performed": rollbacks_performed,
            "diagnostic_notes": diagnostic_notes,
            "escalation_required": True,
            "escalation_reason": "Exhausted automated repair attempts; requires human review.",
        }
        self._diagnostic_history.append(result)
        return result
