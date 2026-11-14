"""Controlled Terminal & Process Execution Engine for BEL Code."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from bel_code.security.policy import RiskLevel, SecurityPolicy


class TerminalExecutor:
    """Executes shell commands on Windows (PowerShell/CMD) and Unix (bash/zsh)
    with strict working directory isolation, command risk classification,
    human approval gates, secret redaction, background process lifecycle
    management, and sandboxed/Docker execution helpers.
    """

    SECRET_ENV_KEY_PATTERNS: Tuple[str, ...] = (
        "API_KEY",
        "SECRET",
        "TOKEN",
        "PASSWORD",
        "PASSWD",
        "CREDENTIAL",
        "PRIVATE_KEY",
        "AUTH_KEY",
    )

    SECRET_VALUE_REGEXES: Tuple[re.Pattern[str], ...] = (
        re.compile(r"(?:thk_live_|freellmapi-|sk-|ghp_|gho_|xoxb-|xoxp-)[A-Za-z0-9_.-]{8,}"),
        re.compile(r"Bearer\s+[A-Za-z0-9_.-]{16,}", re.IGNORECASE),
    )

    def __init__(
        self,
        workspace_root: Union[Path, str],
        security: Optional[SecurityPolicy] = None,
        default_timeout: int = 60,
    ) -> None:
        self.workspace_root: Path = Path(workspace_root).resolve()
        self.security: SecurityPolicy = security or SecurityPolicy(self.workspace_root, mode="supervised")
        self.default_timeout: int = default_timeout
        self._processes: Dict[str, Dict[str, Any]] = {}
        self._sandboxes: List[Path] = []

    # ------------------------------------------------------------------
    # Environment Sanitization & Secret Redaction
    # ------------------------------------------------------------------
    def sanitize_environment(
        self,
        extra_env: Optional[Dict[str, str]] = None,
        strip_secrets: bool = False,
    ) -> Tuple[Dict[str, str], Set[str]]:
        """Prepare subprocess environment variables and collect sensitive values
        so they can be redacted from stdout/stderr logs.
        """
        env = os.environ.copy()
        if extra_env:
            for k, v in extra_env.items():
                env[str(k)] = str(v)

        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        sensitive_values: Set[str] = set()
        keys_to_remove: List[str] = []

        for key, val in env.items():
            key_upper = key.upper()
            if any(pat in key_upper for pat in self.SECRET_ENV_KEY_PATTERNS):
                if val and len(val.strip()) >= 6:
                    sensitive_values.add(val.strip())
                if strip_secrets and (not extra_env or key not in extra_env):
                    keys_to_remove.append(key)

        for key in keys_to_remove:
            env.pop(key, None)

        return env, sensitive_values

    def redact_secrets(self, text: str, extra_secrets: Optional[Set[str]] = None) -> str:
        """Redact registered environment secrets and token patterns from command output."""
        if not text:
            return ""
        redacted = text
        if extra_secrets:
            for sec in sorted(extra_secrets, key=len, reverse=True):
                if sec and len(sec) >= 6 and sec in redacted:
                    redacted = redacted.replace(sec, "[REDACTED_SECRET]")

        for regex in self.SECRET_VALUE_REGEXES:
            redacted = regex.sub("[REDACTED_SECRET]", redacted)

        return redacted

    # ------------------------------------------------------------------
    # Foreground Command Execution
    # ------------------------------------------------------------------
    def execute(
        self,
        command: str,
        timeout: Optional[int] = None,
        cwd: Optional[Union[str, Path]] = None,
        env: Optional[Dict[str, str]] = None,
        approved: bool = False,
        approval_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a command inside `workspace_root` (or validated subdirectory)
        after enforcing command risk classification and security mode policies.
        """
        target_cwd = self.security.validate_path(cwd or self.workspace_root, allow_write=False)
        target_cwd.mkdir(parents=True, exist_ok=True)

        classification = self.security.classify_command(command)
        risk: RiskLevel = classification["risk"]

        # Check explicit approval override
        is_explicitly_approved = bool(approved)
        if approval_id:
            req = self.security.get_approval(approval_id)
            if req and req.get("status") == "approved":
                is_explicitly_approved = True
        if self.security.is_approved("terminal_command", command.strip()):
            is_explicitly_approved = True

        if not classification["allowed"] and not is_explicitly_approved:
            approval_req = self.security.create_approval_request(
                action_type="terminal_command",
                target=command.strip(),
                risk=risk,
                reason=classification["reason"],
                details={"cwd": str(target_cwd), "matched_rules": classification.get("matched_rules", [])},
            )
            return {
                "executed": False,
                "allowed": False,
                "requires_approval": True,
                "approval_id": approval_req["approval_id"],
                "risk": risk,
                "reason": classification["reason"],
                "command": command,
                "cwd": str(target_cwd),
                "stdout": "",
                "stderr": (
                    f"Command blocked by security policy (mode={self.security.mode}, "
                    f"risk={risk.value}): {classification['reason']}"
                ),
                "exit_code": -1,
                "duration_ms": 0.0,
                "timed_out": False,
            }

        proc_env, sensitive_values = self.sanitize_environment(env)
        effective_timeout = timeout if timeout is not None else self.default_timeout

        start_ts = time.perf_counter()
        timed_out = False
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(target_cwd),
                env=proc_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
            )
            raw_stdout = completed.stdout or ""
            raw_stderr = completed.stderr or ""
            exit_code = int(completed.returncode)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            raw_stdout = (exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
            raw_stderr = (
                (exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
                + f"\nCommand timed out after {effective_timeout} seconds."
            )
            exit_code = -124
        except Exception as exc:
            raw_stdout = ""
            raw_stderr = f"Subprocess execution error: {exc}"
            exit_code = -1

        duration_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        stdout_clean = self.redact_secrets(raw_stdout, sensitive_values)
        stderr_clean = self.redact_secrets(raw_stderr, sensitive_values)

        injection_scan = self.security.scan_prompt_injection(
            f"{stdout_clean}\n{stderr_clean}",
            source=f"terminal:{command[:40]}",
        )

        self.security.log_audit_event(
            action="terminal_command",
            target=command.strip(),
            risk=risk,
            outcome="allowed" if not timed_out else "timeout",
            details={
                "cwd": str(target_cwd),
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "timed_out": timed_out,
            },
        )

        return {
            "executed": True,
            "allowed": True,
            "requires_approval": False,
            "approval_id": approval_id,
            "risk": risk,
            "reason": classification["reason"],
            "command": command,
            "cwd": str(target_cwd),
            "stdout": stdout_clean,
            "stderr": stderr_clean,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "timed_out": timed_out,
            "injection_scan": injection_scan,
        }

    # Convenient method aliases
    run_command = execute
    run = execute

    # ------------------------------------------------------------------
    # Background Process Manager (Local Dev Servers & Watchers)
    # ------------------------------------------------------------------
    def start_background_process(
        self,
        command: str,
        name: Optional[str] = None,
        cwd: Optional[Union[str, Path]] = None,
        env: Optional[Dict[str, str]] = None,
        approved: bool = False,
    ) -> Dict[str, Any]:
        """Start a long-running background process (e.g., local dev server)
        within `workspace_root` and capture its stdout/stderr to a log file.
        """
        target_cwd = self.security.validate_path(cwd or self.workspace_root, allow_write=False)
        classification = self.security.classify_command(command)

        if not classification["allowed"] and not approved:
            req = self.security.create_approval_request(
                action_type="start_background_process",
                target=command,
                risk=classification["risk"],
                reason=classification["reason"],
            )
            return {
                "started": False,
                "requires_approval": True,
                "approval_id": req["approval_id"],
                "reason": classification["reason"],
                "command": command,
            }

        proc_env, sensitive_values = self.sanitize_environment(env)
        process_id = f"proc_{uuid.uuid4().hex[:8]}"
        log_dir = Path(tempfile.gettempdir()) / "bel_code_procs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / f"{process_id}_stdout.log"
        stderr_path = log_dir / f"{process_id}_stderr.log"

        stdout_fh = stdout_path.open("w", encoding="utf-8")
        stderr_fh = stderr_path.open("w", encoding="utf-8")

        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(target_cwd),
            env=proc_env,
            stdout=stdout_fh,
            stderr=stderr_fh,
            text=True,
        )

        record: Dict[str, Any] = {
            "process_id": process_id,
            "pid": proc.pid,
            "name": name or command[:40],
            "command": command,
            "cwd": str(target_cwd),
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "exit_code": None,
            "_popen": proc,
            "_stdout_fh": stdout_fh,
            "_stderr_fh": stderr_fh,
            "_stdout_path": stdout_path,
            "_stderr_path": stderr_path,
            "_sensitive": sensitive_values,
        }
        self._processes[process_id] = record

        self.security.log_audit_event(
            action="start_background_process",
            target=command,
            risk=classification["risk"],
            outcome="allowed",
            details={"process_id": process_id, "pid": proc.pid},
        )

        return {
            "started": True,
            "process_id": process_id,
            "pid": proc.pid,
            "name": record["name"],
            "command": command,
            "status": "running",
            "started_at": record["started_at"],
        }

    def get_process_logs(self, process_id: str, tail_lines: int = 100) -> Dict[str, Any]:
        """Fetch stdout/stderr logs and live status for a tracked background process."""
        if process_id not in self._processes:
            raise KeyError(f"Background process '{process_id}' not found.")

        rec = self._processes[process_id]
        proc: subprocess.Popen[str] = rec["_popen"]
        poll_rc = proc.poll()
        if poll_rc is not None:
            rec["status"] = "exited"
            rec["exit_code"] = poll_rc

        try:
            rec["_stdout_fh"].flush()
            rec["_stderr_fh"].flush()
        except Exception:
            pass

        stdout_text = rec["_stdout_path"].read_text(encoding="utf-8", errors="replace") if rec["_stdout_path"].exists() else ""
        stderr_text = rec["_stderr_path"].read_text(encoding="utf-8", errors="replace") if rec["_stderr_path"].exists() else ""

        if tail_lines > 0:
            stdout_text = "\n".join(stdout_text.splitlines()[-tail_lines:])
            stderr_text = "\n".join(stderr_text.splitlines()[-tail_lines:])

        return {
            "process_id": process_id,
            "pid": rec["pid"],
            "name": rec["name"],
            "command": rec["command"],
            "status": rec["status"],
            "exit_code": rec["exit_code"],
            "stdout": self.redact_secrets(stdout_text, rec["_sensitive"]),
            "stderr": self.redact_secrets(stderr_text, rec["_sensitive"]),
        }

    def stop_process(self, process_id: str, force: bool = False) -> Dict[str, Any]:
        """Stop a running background process cleanly and close its log handles."""
        if process_id not in self._processes:
            raise KeyError(f"Background process '{process_id}' not found.")

        rec = self._processes[process_id]
        proc: subprocess.Popen[str] = rec["_popen"]

        if proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        f"taskkill /PID {proc.pid} /T /F",
                        shell=True,
                        capture_output=True,
                        timeout=5,
                    )
                else:
                    if force:
                        proc.kill()
                    else:
                        proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        rec["status"] = "stopped"
        rec["exit_code"] = proc.poll()

        for fh_key in ("_stdout_fh", "_stderr_fh"):
            try:
                rec[fh_key].close()
            except Exception:
                pass

        return {
            "process_id": process_id,
            "pid": rec["pid"],
            "stopped": True,
            "status": rec["status"],
            "exit_code": rec["exit_code"],
        }

    def list_processes(self) -> List[Dict[str, Any]]:
        """List all tracked background processes and their current status."""
        items: List[Dict[str, Any]] = []
        for pid_key, rec in self._processes.items():
            proc: subprocess.Popen[str] = rec["_popen"]
            poll_rc = proc.poll()
            if poll_rc is not None and rec["status"] == "running":
                rec["status"] = "exited"
                rec["exit_code"] = poll_rc
            items.append(
                {
                    "process_id": pid_key,
                    "pid": rec["pid"],
                    "name": rec["name"],
                    "command": rec["command"],
                    "cwd": rec["cwd"],
                    "status": rec["status"],
                    "exit_code": rec["exit_code"],
                    "started_at": rec["started_at"],
                }
            )
        return items

    # ------------------------------------------------------------------
    # Sandboxed Workspace & Docker Container Helpers
    # ------------------------------------------------------------------
    def create_sandbox_workspace(
        self,
        prefix: str = "bel_sandbox_",
        copy_workspace: bool = False,
    ) -> Path:
        """Create an isolated temporary directory for untrusted or experimental runs."""
        sandbox_dir = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
        if copy_workspace and self.workspace_root.exists():
            for item in self.workspace_root.iterdir():
                if item.name in (".git", "node_modules", "__pycache__", ".venv", "venv", ".bel_code"):
                    continue
                dest = sandbox_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                elif item.is_file():
                    shutil.copy2(item, dest)
        self._sandboxes.append(sandbox_dir)
        return sandbox_dir

    def run_in_docker_if_available(
        self,
        command: str,
        image: str = "python:3.11-slim",
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """Execute a command inside an ephemeral network-isolated Docker container
        when Docker is available on the host; otherwise fall back cleanly to
        controlled local sandbox execution.
        """
        docker_bin = shutil.which("docker")
        docker_ready = False
        if docker_bin:
            try:
                probe = subprocess.run(
                    [docker_bin, "info"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                docker_ready = probe.returncode == 0
            except Exception:
                docker_ready = False

        if docker_ready:
            docker_cmd = (
                f'docker run --rm --network none -v "{self.workspace_root}:/workspace" '
                f'-w /workspace {image} sh -c "{command}"'
            )
            res = self.execute(docker_cmd, timeout=timeout, approved=True)
            res["sandbox_mode"] = "docker_container"
            res["image"] = image
            return res

        res = self.execute(command, timeout=timeout)
        res["sandbox_mode"] = "local_process_fallback"
        res["image"] = None
        return res
