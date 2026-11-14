"""Git & GitHub Integration, Checkpoints, Safe Rollback, Worktree Isolation,
PR Reviewer, Issue-to-Plan Mapper, and Change Register for BEL Code
(Specification Section 9).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from bel_code.security.policy import RiskLevel, SecurityPolicy

# Shared per-workspace state so multiple GitManager instances pointing to the
# same workspace_root share checkpoints, worktrees, and change register entries.
_WORKSPACE_GIT_STATE: Dict[str, Dict[str, Any]] = {}


class GitManager:
    """Provides safe, workspace-isolated Git operations, non-destructive
    checkpoints and rollbacks, parallel worktree management, automated pull
    request review, GitHub issue-to-plan mapping, and an audit-ready change
    register.
    """

    IGNORED_SNAPSHOT_DIRS: Set[str] = {
        ".git",
        ".bel_code",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        ".venv",
        "venv",
        ".mypy_cache",
    }

    PR_SECURITY_RULES: Tuple[Tuple[re.Pattern[str], str, str, str], ...] = (
        (
            re.compile(r"(?:api[_-]?key|secret|password|passwd|token)\s*=\s*['\"][^'\"]{8,}['\"]", re.IGNORECASE),
            "critical",
            "security",
            "Potential hardcoded credential or secret assigned in source code.",
        ),
        (
            re.compile(r"(?:thk_live_|freellmapi-|sk-|ghp_)[A-Za-z0-9_-]{8,}"),
            "critical",
            "security",
            "Hardcoded API key or provider token detected in diff.",
        ),
        (
            re.compile(r"\b(eval|exec)\s*\("),
            "high",
            "security",
            "Dynamic code execution via eval()/exec() introduces arbitrary code execution risk.",
        ),
        (
            re.compile(r"subprocess\.(?:run|Popen|call|check_output)\s*\([^\n]*shell\s*=\s*True"),
            "high",
            "security",
            "Subprocess invoked with shell=True; validate and sanitize command inputs.",
        ),
        (
            re.compile(r"(?:execute|cursor\.execute)\s*\(\s*(?:f['\"]|['\"].*%s|['\"].*\+)"),
            "critical",
            "security",
            "Potential SQL injection via string formatting/concatenation in SQL query.",
        ),
        (
            re.compile(r"\b(?:pickle\.loads|yaml\.load\s*\([^,\n)]*\))\b"),
            "high",
            "security",
            "Unsafe deserialization (pickle.loads or yaml.load without SafeLoader).",
        ),
        (
            re.compile(r"\b(?:innerHTML\s*=|dangerouslySetInnerHTML)\b"),
            "high",
            "security",
            "Potential DOM Cross-Site Scripting (XSS) sink via unescaped HTML injection.",
        ),
    )

    PR_SYNTAX_AND_QUALITY_RULES: Tuple[Tuple[re.Pattern[str], str, str, str], ...] = (
        (
            re.compile(r"^\s*except\s*:\s*$"),
            "medium",
            "syntax",
            "Bare 'except:' clause catches SystemExit/KeyboardInterrupt and hides root causes.",
        ),
        (
            re.compile(r"def\s+\w+\s*\([^\n)]*=\s*(\[\]|\{\})"),
            "medium",
            "syntax",
            "Mutable default argument ([] or {}) shared across function invocations.",
        ),
        (
            re.compile(r"\b(?:pdb\.set_trace|breakpoint)\s*\(\s*\)"),
            "medium",
            "maintainability",
            "Leftover interactive debugger breakpoint in committed code.",
        ),
        (
            re.compile(r"\b(TODO|FIXME|HACK|XXX)\b"),
            "low",
            "maintainability",
            "Unresolved TODO/FIXME technical debt marker in diff.",
        ),
    )

    def __init__(
        self,
        workspace_root: Union[Path, str],
        security: Optional[SecurityPolicy] = None,
    ) -> None:
        self.workspace_root: Path = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.security: SecurityPolicy = security or SecurityPolicy(self.workspace_root, mode="supervised")

        ws_key = str(self.workspace_root)
        if ws_key not in _WORKSPACE_GIT_STATE:
            _WORKSPACE_GIT_STATE[ws_key] = {
                "checkpoints": {},
                "worktrees": {},
                "change_register": [],
            }
        self._state = _WORKSPACE_GIT_STATE[ws_key]
        self._checkpoints: Dict[str, Dict[str, Any]] = self._state["checkpoints"]
        self._worktrees: Dict[str, Dict[str, Any]] = self._state["worktrees"]
        self._change_register: List[Dict[str, Any]] = self._state["change_register"]

    # ------------------------------------------------------------------
    # Low-Level Git Subprocess Helper
    # ------------------------------------------------------------------
    def _run_git(self, args: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["LC_ALL"] = "C"
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.workspace_root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    def _ensure_git_identity(self) -> None:
        """Ensure local git user.name and user.email are configured so commits succeed."""
        name_res = self._run_git(["config", "user.name"])
        if name_res.returncode != 0 or not name_res.stdout.strip():
            self._run_git(["config", "user.name", "BEL Code Agent"])
        email_res = self._run_git(["config", "user.email"])
        if email_res.returncode != 0 or not email_res.stdout.strip():
            self._run_git(["config", "user.email", "agent@belcode.local"])

    def _is_git_repo(self) -> bool:
        if (self.workspace_root / ".git").exists():
            return True
        res = self._run_git(["rev-parse", "--is-inside-work-tree"])
        return res.returncode == 0 and res.stdout.strip().lower() == "true"

    # ------------------------------------------------------------------
    # Repository Status, Branches, Commit Log, Diff, Branching & Commit
    # ------------------------------------------------------------------
    def init_or_status(self) -> Dict[str, Any]:
        """Initialize a Git repository in `workspace_root` if not already a repo,
        and return structured repository status.
        """
        was_initialized = self._is_git_repo()
        if not was_initialized:
            self._run_git(["init"])
        self._ensure_git_identity()

        branch_res = self._run_git(["branch", "--show-current"])
        current_branch = branch_res.stdout.strip()
        if not current_branch:
            head_res = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
            current_branch = head_res.stdout.strip() if head_res.returncode == 0 else "main"

        status_res = self._run_git(["status", "--porcelain"])
        staged: List[str] = []
        modified: List[str] = []
        untracked: List[str] = []
        status_lines: List[str] = []

        for raw_line in status_res.stdout.splitlines():
            if not raw_line.strip():
                continue
            status_lines.append(raw_line)
            xy = raw_line[:2]
            filepath = raw_line[3:].strip().strip('"')
            if filepath.startswith(".bel_code"):
                continue
            if xy == "??":
                untracked.append(filepath)
            else:
                if xy[0] not in (" ", "?"):
                    staged.append(filepath)
                if xy[1] not in (" ", "?"):
                    modified.append(filepath)

        head_hash_res = self._run_git(["rev-parse", "HEAD"])
        head_commit = head_hash_res.stdout.strip() if head_hash_res.returncode == 0 else None

        return {
            "initialized": True,
            "newly_initialized": not was_initialized,
            "is_repo": True,
            "workspace_root": str(self.workspace_root),
            "branch": current_branch or "main",
            "head_commit": head_commit,
            "clean": len(staged) == 0 and len(modified) == 0 and len(untracked) == 0,
            "staged": staged,
            "modified": modified,
            "untracked": untracked,
            "status_lines": status_lines,
        }

    status = init_or_status

    def get_branches(self) -> Dict[str, Any]:
        """Return current branch, local branches, and remote branches."""
        self.init_or_status()
        res = self._run_git(["branch", "-a"])
        local_branches: List[str] = []
        remote_branches: List[str] = []
        current_branch = "main"

        for line in res.stdout.splitlines():
            clean = line.strip()
            if not clean:
                continue
            is_current = clean.startswith("* ")
            name = clean[2:].strip() if is_current else clean
            if is_current:
                current_branch = name
            if name.startswith("remotes/"):
                remote_branches.append(name)
            else:
                local_branches.append(name)

        if not local_branches:
            b_res = self._run_git(["branch", "--show-current"])
            current_branch = b_res.stdout.strip() or "main"
            local_branches = [current_branch]

        return {
            "current": current_branch,
            "branches": local_branches,
            "local": local_branches,
            "remote": remote_branches,
        }

    def get_commit_log(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return up to `limit` recent git commits as structured dicts."""
        self.init_or_status()
        res = self._run_git(
            [
                "log",
                f"-n{max(1, int(limit))}",
                "--pretty=format:%H|%h|%an|%ae|%ad|%s",
                "--date=iso",
            ]
        )
        if res.returncode != 0 or not res.stdout.strip():
            return []

        commits: List[Dict[str, Any]] = []
        for line in res.stdout.splitlines():
            parts = line.split("|", 5)
            if len(parts) == 6:
                commits.append(
                    {
                        "hash": parts[0],
                        "short_hash": parts[1],
                        "author": parts[2],
                        "email": parts[3],
                        "date": parts[4],
                        "message": parts[5],
                    }
                )
        return commits

    def get_diff(
        self,
        ref1: Optional[str] = None,
        ref2: Optional[str] = None,
        staged: bool = False,
    ) -> Dict[str, Any]:
        """Return unified git diff and list of changed files."""
        self.init_or_status()
        args = ["diff"]
        if staged:
            args.append("--staged")
        if ref1 and ref2:
            args.extend([ref1, ref2])
        elif ref1:
            args.append(ref1)

        diff_res = self._run_git(args)
        diff_text = diff_res.stdout or ""

        # If working tree has both unstaged and staged changes when no refs passed
        if not ref1 and not ref2 and not staged and not diff_text.strip():
            staged_res = self._run_git(["diff", "--staged"])
            diff_text = staged_res.stdout or ""

        name_args = ["diff", "--name-only"]
        if staged:
            name_args.append("--staged")
        if ref1 and ref2:
            name_args.extend([ref1, ref2])
        elif ref1:
            name_args.append(ref1)

        names_res = self._run_git(name_args)
        files_changed = [f.strip() for f in names_res.stdout.splitlines() if f.strip()]

        return {
            "diff": diff_text,
            "files_changed": files_changed,
            "has_changes": bool(diff_text.strip()),
        }

    def create_task_branch(self, branch_name: str, checkout: bool = True) -> Dict[str, Any]:
        """Create (and optionally switch to) an isolated feature/task branch."""
        self.init_or_status()
        safe_name = re.sub(r"[^A-Za-z0-9._/-]+", "-", branch_name.strip()).strip("-")
        if not safe_name:
            raise ValueError("Branch name cannot be empty.")

        if checkout:
            res = self._run_git(["checkout", "-b", safe_name])
            if res.returncode != 0:
                res = self._run_git(["checkout", safe_name])
        else:
            res = self._run_git(["branch", safe_name])

        return {
            "success": res.returncode == 0,
            "branch": safe_name,
            "checked_out": checkout,
            "stderr": res.stderr.strip(),
        }

    def commit_changes(
        self,
        message: str,
        files: Optional[List[str]] = None,
        task_id: str = "",
    ) -> Dict[str, Any]:
        """Stage specified files (or workspace changes) and create a Git commit."""
        self.init_or_status()
        self._ensure_git_identity()

        if files:
            validated_rels: List[str] = []
            for f in files:
                resolved = self.security.validate_path(f, allow_write=True)
                validated_rels.append(resolved.relative_to(self.workspace_root).as_posix())
            self._run_git(["add", "--", *validated_rels])
        else:
            self._run_git(["add", "-A"])

        commit_msg = f"[{task_id}] {message}" if task_id and f"[{task_id}]" not in message else message
        commit_res = self._run_git(["commit", "-m", commit_msg])

        head_res = self._run_git(["rev-parse", "HEAD"])
        commit_hash = head_res.stdout.strip() if head_res.returncode == 0 else ""

        self.security.log_audit_event(
            action="git_commit",
            target=commit_hash or commit_msg,
            risk=RiskLevel.LOW,
            outcome="allowed" if commit_res.returncode == 0 else "noop",
            details={"message": commit_msg, "task_id": task_id, "files": files},
        )

        return {
            "success": commit_res.returncode == 0,
            "commit_hash": commit_hash,
            "message": commit_msg,
            "task_id": task_id,
            "stdout": commit_res.stdout.strip(),
            "stderr": commit_res.stderr.strip(),
        }

    # ------------------------------------------------------------------
    # Automatic Checkpoints & Non-Destructive Safe Rollback
    # ------------------------------------------------------------------
    def _snapshot_workspace_files(self) -> Dict[str, bytes]:
        """Capture exact byte contents of all workspace files (excluding ignored dirs)."""
        snapshot: Dict[str, bytes] = {}
        for root, dirs, files in os.walk(self.workspace_root):
            dirs[:] = [d for d in dirs if d not in self.IGNORED_SNAPSHOT_DIRS]
            for fname in files:
                full = Path(root) / fname
                try:
                    rel = full.relative_to(self.workspace_root).as_posix()
                    snapshot[rel] = full.read_bytes()
                except Exception:
                    continue
        return snapshot

    def _get_git_tracked_files(self) -> Set[str]:
        res = self._run_git(["ls-files"])
        if res.returncode != 0:
            return set()
        return {line.strip().strip('"') for line in res.stdout.splitlines() if line.strip()}

    def create_checkpoint(self, label: str, task_id: str = "default") -> Dict[str, Any]:
        """Create a point-in-time checkpoint before risky or multi-file edits
        without disturbing uncommitted user changes.
        """
        status_info = self.init_or_status()
        tracked_files = self._get_git_tracked_files()
        snapshot_bytes = self._snapshot_workspace_files()

        checkpoint_id = f"chk_{uuid.uuid4().hex[:10]}"
        record: Dict[str, Any] = {
            "checkpoint_id": checkpoint_id,
            "label": label,
            "task_id": task_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "branch": status_info["branch"],
            "head_commit": status_info["head_commit"],
            "tracked_files": set(tracked_files),
            "untracked_at_checkpoint": set(status_info["untracked"]),
            "snapshot_bytes": snapshot_bytes,
            "files_count": len(snapshot_bytes),
        }
        self._checkpoints[checkpoint_id] = record

        self.security.log_audit_event(
            action="create_checkpoint",
            target=checkpoint_id,
            risk=RiskLevel.LOW,
            outcome="allowed",
            details={"label": label, "task_id": task_id, "files_count": len(snapshot_bytes)},
        )

        return {
            "id": checkpoint_id,
            "checkpoint_id": checkpoint_id,
            "label": label,
            "task_id": task_id,
            "created_at": record["created_at"],
            "branch": record["branch"],
            "head_commit": record["head_commit"],
            "files_snapshotted": len(snapshot_bytes),
        }

    def rollback_to_checkpoint(
        self,
        checkpoint_id: str,
        preserve_uncommitted: bool = True,
        approved: bool = False,
    ) -> Dict[str, Any]:
        """Cleanly restore tracked state to `checkpoint_id` while preserving
        uncommitted user files. Never discards uncommitted user changes without
        explicit approval.
        """
        if checkpoint_id not in self._checkpoints:
            raise KeyError(f"Checkpoint '{checkpoint_id}' not found.")

        if not preserve_uncommitted and not approved:
            perm = self.security.check_action_permission(
                "destructive_rollback",
                checkpoint_id,
                risk=RiskLevel.CRITICAL,
                details={"preserve_uncommitted": False},
            )
            if not perm["allowed"]:
                return {
                    "success": False,
                    "rolled_back": False,
                    "requires_approval": True,
                    "approval_id": perm["approval_id"],
                    "reason": "Discarding uncommitted user files requires explicit user approval.",
                }

        cp = self._checkpoints[checkpoint_id]
        snapshot_bytes: Dict[str, bytes] = cp["snapshot_bytes"]
        tracked_at_cp: Set[str] = cp["tracked_files"]
        head_at_cp: Optional[str] = cp["head_commit"]

        # If git HEAD advanced after the checkpoint, softly move HEAD back to head_at_cp
        if head_at_cp:
            curr_head_res = self._run_git(["rev-parse", "HEAD"])
            curr_head = curr_head_res.stdout.strip() if curr_head_res.returncode == 0 else None
            if curr_head and curr_head != head_at_cp:
                self._run_git(["reset", "--soft", head_at_cp])
                self._run_git(["reset", "HEAD"])

        # Determine which files were tracked (or if no git commits existed yet, all files in snapshot)
        files_to_restore = tracked_at_cp if tracked_at_cp else set(snapshot_bytes.keys())

        restored_files: List[str] = []
        preserved_user_files: List[str] = []

        # 1. Restore tracked files to their exact byte state at checkpoint creation
        for rel_path in sorted(files_to_restore):
            if rel_path in snapshot_bytes:
                target_file = self.workspace_root / rel_path
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_bytes(snapshot_bytes[rel_path])
                restored_files.append(rel_path)

        # 2. Handle files that were untracked at checkpoint time:
        #    If they were deleted since checkpoint, restore them; if they exist on disk, preserve them!
        for rel_path, content_bytes in snapshot_bytes.items():
            if rel_path not in files_to_restore:
                target_file = self.workspace_root / rel_path
                if not target_file.exists():
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    target_file.write_bytes(content_bytes)
                preserved_user_files.append(rel_path)

        # 3. Inspect any new files created after checkpoint
        current_snapshot = self._snapshot_workspace_files()
        current_tracked = self._get_git_tracked_files()
        for rel_path in sorted(current_snapshot.keys()):
            if rel_path not in snapshot_bytes:
                if preserve_uncommitted and rel_path not in current_tracked:
                    # Uncommitted user file created after checkpoint: preserve intact!
                    preserved_user_files.append(rel_path)
                elif not preserve_uncommitted and approved:
                    try:
                        (self.workspace_root / rel_path).unlink()
                    except Exception:
                        pass

        self.security.log_audit_event(
            action="rollback_to_checkpoint",
            target=checkpoint_id,
            risk=RiskLevel.LOW,
            outcome="allowed",
            details={
                "label": cp["label"],
                "preserve_uncommitted": preserve_uncommitted,
                "restored_files": restored_files,
                "preserved_user_files": preserved_user_files,
            },
        )

        return {
            "success": True,
            "rolled_back": True,
            "checkpoint_id": checkpoint_id,
            "label": cp["label"],
            "task_id": cp["task_id"],
            "preserve_uncommitted": preserve_uncommitted,
            "restored_files": restored_files,
            "preserved_user_files": sorted(set(preserved_user_files)),
        }

    def list_checkpoints(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List recorded checkpoints, optionally filtered by `task_id`."""
        items: List[Dict[str, Any]] = []
        for cp in self._checkpoints.values():
            if task_id and cp.get("task_id") != task_id:
                continue
            items.append(
                {
                    "checkpoint_id": cp["checkpoint_id"],
                    "label": cp["label"],
                    "task_id": cp["task_id"],
                    "created_at": cp["created_at"],
                    "branch": cp["branch"],
                    "head_commit": cp["head_commit"],
                    "files_count": cp["files_count"],
                }
            )
        return items

    # ------------------------------------------------------------------
    # Git Worktree Isolation for Parallel Subagents
    # ------------------------------------------------------------------
    def create_worktree(self, name: str, branch: Optional[str] = None) -> Dict[str, Any]:
        """Create an isolated Git worktree for a parallel subagent."""
        self.init_or_status()
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
        branch_name = branch or f"wt-{safe_name}"

        wt_base = Path(tempfile.gettempdir()) / "bel_code_worktrees"
        wt_base.mkdir(parents=True, exist_ok=True)
        ws_hash = hashlib.sha256(str(self.workspace_root).encode("utf-8")).hexdigest()[:8]
        wt_path = (wt_base / f"{ws_hash}_{safe_name}").resolve()

        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)

        res = self._run_git(["worktree", "add", "-b", branch_name, str(wt_path)])
        if res.returncode != 0:
            # Fallback if branch already exists or repo has no initial commit yet
            res2 = self._run_git(["worktree", "add", str(wt_path), branch_name])
            if res2.returncode != 0:
                wt_path.mkdir(parents=True, exist_ok=True)
                for rel, data in self._snapshot_workspace_files().items():
                    dest = wt_path / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)

        record = {
            "name": safe_name,
            "branch": branch_name,
            "path": str(wt_path),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "active": True,
        }
        self._worktrees[safe_name] = record
        return {"success": True, **record}

    def remove_worktree(self, name: str, force: bool = True) -> Dict[str, Any]:
        """Remove an isolated subagent Git worktree and clean up its directory."""
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
        rec = self._worktrees.get(safe_name)
        wt_path = Path(rec["path"]) if rec else None

        if wt_path:
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(str(wt_path))
            self._run_git(args)
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)

        if safe_name in self._worktrees:
            self._worktrees[safe_name]["active"] = False
            self._worktrees.pop(safe_name, None)

        return {"success": True, "name": safe_name, "removed": True}

    def list_worktrees(self) -> List[Dict[str, Any]]:
        """List active subagent worktrees."""
        return list(self._worktrees.values())

    # ------------------------------------------------------------------
    # Automated Pull Request Reviewer & GitHub Issue-to-Plan Mapper
    # ------------------------------------------------------------------
    def review_pull_request(
        self,
        diff_text: Optional[str] = None,
        base_ref: Optional[str] = None,
        head_ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Analyze a unified git diff and return prioritized security, syntax,
        and maintainability findings with exact file and line references plus a
        draft pull request summary.
        """
        if diff_text is None:
            diff_info = self.get_diff(ref1=base_ref, ref2=head_ref)
            diff_text = diff_info["diff"]

        findings: List[Dict[str, Any]] = []
        files_reviewed: List[str] = []
        current_file = "unknown_file"
        current_line = 1

        for raw_line in (diff_text or "").splitlines():
            if raw_line.startswith("+++ b/"):
                current_file = raw_line[6:].strip()
                if current_file not in files_reviewed:
                    files_reviewed.append(current_file)
                current_line = 1
                continue
            if raw_line.startswith("+++ "):
                current_file = raw_line[4:].strip()
                if current_file not in files_reviewed:
                    files_reviewed.append(current_file)
                current_line = 1
                continue

            hunk_match = re.match(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@", raw_line)
            if hunk_match:
                current_line = int(hunk_match.group(1))
                continue

            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                added_code = raw_line[1:]
                self._inspect_diff_line(added_code, current_file, current_line, findings)
                current_line += 1
            elif not raw_line.startswith("-"):
                # Also allow plain code snippets if caller passed raw code without '+' prefix
                if "@@" not in raw_line and "+++" not in diff_text:
                    self._inspect_diff_line(raw_line, current_file, current_line, findings)
                current_line += 1

        severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        findings.sort(key=lambda item: (severity_rank.get(item["severity"], 4), item["file"], item["line"]))

        sec_count = sum(1 for f in findings if f["category"] == "security")
        syn_count = sum(1 for f in findings if f["category"] == "syntax")
        maint_count = sum(1 for f in findings if f["category"] == "maintainability")

        recommendations: List[str] = []
        for f in findings:
            if f["recommendation"] not in recommendations:
                recommendations.append(f["recommendation"])

        status_label = "CHANGES_REQUESTED" if sec_count > 0 or any(f["severity"] in ("critical", "high") for f in findings) else "APPROVED"
        summary_lines = [
            f"## Automated Pull Request Review ({status_label})",
            f"- **Files Reviewed**: {len(files_reviewed) or 1}",
            f"- **Total Findings**: {len(findings)} (Security: {sec_count}, Syntax/Logic: {syn_count}, Maintainability: {maint_count})",
        ]
        if findings:
            summary_lines.append("\n### Prioritized Findings")
            for idx, item in enumerate(findings, 1):
                summary_lines.append(
                    f"{idx}. **[{item['severity'].upper()} / {item['category']}]** `{item['file']}:{item['line']}` — {item['message']}"
                )
        else:
            summary_lines.append("\nNo security, syntax, or maintainability issues detected.")

        draft_summary = "\n".join(summary_lines)

        return {
            "status": status_label,
            "approved": status_label == "APPROVED",
            "findings": findings,
            "total_findings": len(findings),
            "security_findings_count": sec_count,
            "syntax_findings_count": syn_count,
            "maintainability_findings_count": maint_count,
            "files_reviewed": files_reviewed,
            "recommendations": recommendations,
            "draft_pr_summary": draft_summary,
            "summary": draft_summary,
        }

    def _inspect_diff_line(
        self,
        code_line: str,
        filepath: str,
        line_no: int,
        findings: List[Dict[str, Any]],
    ) -> None:
        for pattern, severity, category, message in self.PR_SECURITY_RULES:
            if pattern.search(code_line):
                findings.append(
                    {
                        "file": filepath,
                        "line": line_no,
                        "severity": severity,
                        "category": category,
                        "message": message,
                        "recommendation": f"Remediate {category} issue at {filepath}:{line_no} ({message})",
                        "code": code_line.strip()[:120],
                    }
                )
        for pattern, severity, category, message in self.PR_SYNTAX_AND_QUALITY_RULES:
            if pattern.search(code_line):
                findings.append(
                    {
                        "file": filepath,
                        "line": line_no,
                        "severity": severity,
                        "category": category,
                        "message": message,
                        "recommendation": f"Fix {category} pattern at {filepath}:{line_no} ({message})",
                        "code": code_line.strip()[:120],
                    }
                )

    def issue_to_task_plan(self, issue_data: Union[Dict[str, Any], str]) -> Dict[str, Any]:
        """Map a GitHub issue report (dict or string) into a structured 7-stage
        engineering task plan with target file candidates and verification steps.
        """
        if isinstance(issue_data, str):
            title = issue_data.splitlines()[0][:80] if issue_data.strip() else "Untitled Issue"
            body = issue_data
            issue_id: Union[int, str] = "issue_1"
            labels: List[str] = []
        else:
            title = str(issue_data.get("title") or "Untitled Issue")
            body = str(issue_data.get("body") or issue_data.get("description") or "")
            issue_id = issue_data.get("number") or issue_data.get("id") or "issue_1"
            raw_labels = issue_data.get("labels") or []
            labels = [
                (lbl.get("name") if isinstance(lbl, dict) else str(lbl)).lower()
                for lbl in raw_labels
            ]

        combined_text = f"{title}\n{body}"
        referenced_files = sorted(
            set(re.findall(r"\b[\w./-]+\.(?:py|js|ts|tsx|jsx|html|css|json|toml|sql|go|rs)\b", combined_text))
        )

        low_text = combined_text.lower()
        if any(k in low_text or k in labels for k in ("security", "vulnerability", "cve", "injection", "xss", "auth")):
            category = "security"
            priority = "high"
        elif any(k in low_text or k in labels for k in ("bug", "error", "crash", "exception", "traceback", "fail", "fix")):
            category = "bug_fix"
            priority = "high"
        elif any(k in low_text or k in labels for k in ("refactor", "cleanup", "performance", "optimize")):
            category = "refactor"
            priority = "medium"
        else:
            category = "feature"
            priority = "medium"

        steps = [
            {
                "stage": 1,
                "name": "Understand",
                "description": f"Analyze issue '{title}' and inspect referenced files: {', '.join(referenced_files) or 'workspace modules'}.",
            },
            {
                "stage": 2,
                "name": "Plan",
                "description": f"Design minimal, modular {category} implementation plan and identify impacted tests.",
            },
            {
                "stage": 3,
                "name": "Implement",
                "description": "Create a Git checkpoint and apply scoped code changes within workspace boundaries.",
            },
            {
                "stage": 4,
                "name": "Test",
                "description": "Execute automated unit and integration tests to verify behavior and capture log evidence.",
            },
            {
                "stage": 5,
                "name": "Repair",
                "description": "If any test or linter check fails, diagnose root cause and iterate or roll back to checkpoint.",
            },
            {
                "stage": 6,
                "name": "Review",
                "description": "Run automated PR diff review for security, syntax, and maintainability compliance.",
            },
            {
                "stage": 7,
                "name": "Deliver",
                "description": "Record verified change in Change Register and produce final completion report.",
            },
        ]

        return {
            "issue_id": issue_id,
            "title": title,
            "body": body,
            "category": category,
            "priority": priority,
            "labels": labels,
            "target_files": referenced_files,
            "steps": steps,
            "acceptance_criteria": [
                f"All requirements described in '{title}' are implemented.",
                "Automated test suite executes and exits with code 0.",
                "Zero high/critical findings in automated PR diff review.",
            ],
        }

    # ------------------------------------------------------------------
    # Audit-Ready Change Register
    # ------------------------------------------------------------------
    def record_change_verification(
        self,
        task_id: str = "default",
        files: Optional[List[str]] = None,
        commit_hash: str = "",
        test_result: Optional[Dict[str, Any]] = None,
        description: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Record an audit-ready change register entry linking a task ID,
        edited files, git commit hash, and test verification evidence.
        """
        edited_files = files or kwargs.get("edited_files") or []
        if isinstance(edited_files, str):
            edited_files = [edited_files]

        if not commit_hash:
            head_res = self._run_git(["rev-parse", "HEAD"])
            if head_res.returncode == 0:
                commit_hash = head_res.stdout.strip()

        verification = test_result or kwargs.get("verification_log") or {}
        if isinstance(verification, str):
            verification = {"verified": True, "log": verification}

        entry: Dict[str, Any] = {
            "register_id": f"chg_{uuid.uuid4().hex[:10]}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": str(task_id),
            "files": list(edited_files),
            "commit_hash": commit_hash,
            "verified": bool(verification.get("verified", verification.get("passed", False))),
            "test_result": verification,
            "description": description or kwargs.get("summary", ""),
        }
        self._change_register.append(entry)

        self.security.log_audit_event(
            action="record_change_verification",
            target=str(task_id),
            risk=RiskLevel.LOW,
            outcome="logged",
            details={
                "register_id": entry["register_id"],
                "commit_hash": commit_hash,
                "files": edited_files,
                "verified": entry["verified"],
            },
        )
        return entry

    def get_change_register(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return recorded change register entries, optionally filtered by `task_id`."""
        if task_id:
            return [e for e in self._change_register if e.get("task_id") == str(task_id)]
        return list(self._change_register)
