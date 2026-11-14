"""Controlled Workspace File System Tools for BEL Code."""

from __future__ import annotations

import difflib
import fnmatch
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from bel_code.security.policy import RiskLevel, SecurityPolicy


class FileReadResult(str):
    """Hybrid string/dict result for `read_file` so callers treating the return
    value as a `str` (`"foo" in fs.read_file(...)`) OR as a `dict`
    (`res["content"]`, `res.get("injection_scan")`) both work seamlessly.
    """

    def __new__(cls, content: str, meta: Optional[Dict[str, Any]] = None) -> "FileReadResult":
        obj = super().__new__(cls, content)
        obj._meta = dict(meta or {})
        obj._meta.setdefault("content", content)
        return obj

    def __getitem__(self, key: Union[int, slice, str]) -> Any:  # type: ignore[override]
        if isinstance(key, str):
            return self._meta[key]
        return super().__getitem__(key)

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str) and super().__contains__(item):
            return True
        return item in self._meta

    def get(self, key: str, default: Any = None) -> Any:
        return self._meta.get(key, default)

    def keys(self) -> Any:
        return self._meta.keys()

    def items(self) -> Any:
        return self._meta.items()

    def values(self) -> Any:
        return self._meta.values()

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._meta)


class FileSystemTools:
    """Provides safe, workspace-isolated file operations with unified diff
    generation, prompt-injection scanning on read, and approval gates on
    high-risk mutations (such as file deletion).
    """

    IGNORED_DIRS: Set[str] = {
        ".git",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        ".venv",
        "venv",
        ".bel_code",
        ".mypy_cache",
        ".ruff_cache",
    }

    def __init__(
        self,
        workspace_root: Union[Path, str],
        security: Optional[SecurityPolicy] = None,
    ) -> None:
        self.workspace_root: Path = Path(workspace_root).resolve()
        self.security: SecurityPolicy = security or SecurityPolicy(self.workspace_root, mode="supervised")

    @staticmethod
    def compute_diff(original: str, modified: str, filename: str = "file") -> str:
        """Compute a standard unified diff between `original` and `modified` text."""
        orig_lines = original.splitlines(keepends=True)
        mod_lines = modified.splitlines(keepends=True)
        diff_iter = difflib.unified_diff(
            orig_lines,
            mod_lines,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            lineterm="",
        )
        return "\n".join(line.rstrip("\r\n") for line in diff_iter)

    def _rel_path(self, resolved: Path) -> str:
        try:
            return resolved.relative_to(self.workspace_root).as_posix()
        except ValueError:
            return resolved.as_posix()

    def read_file(
        self,
        path: Union[str, Path],
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Read a file inside workspace_root, optionally slicing a 1-based line range."""
        resolved = self.security.validate_path(path, allow_write=False, allow_delete=False)
        self.security.check_action_permission("read_file", str(resolved), risk=RiskLevel.LOW)

        if not resolved.exists():
            raise FileNotFoundError(f"File not found: '{path}' (resolved to '{resolved}')")
        if resolved.is_dir():
            raise IsADirectoryError(f"Path is a directory, not a file: '{resolved}'")

        raw_text = resolved.read_text(encoding="utf-8", errors="replace")
        all_lines = raw_text.splitlines()

        if start_line is not None or end_line is not None:
            s_idx = max((start_line or 1) - 1, 0)
            e_idx = end_line if end_line is not None else len(all_lines)
            selected_lines = all_lines[s_idx:e_idx]
            content = "\n".join(selected_lines)
        else:
            content = raw_text

        scan_result = self.security.scan_prompt_injection(content, source=self._rel_path(resolved))

        meta = {
            "success": True,
            "path": str(resolved),
            "relative_path": self._rel_path(resolved),
            "content": content,
            "total_lines": len(all_lines),
            "size_bytes": len(raw_text.encode("utf-8")),
            "injection_scan": scan_result,
        }
        return FileReadResult(content, meta=meta)

    def write_file(
        self,
        path: Union[str, Path],
        content: str,
        create_dirs: bool = True,
        approved: bool = False,
        approval_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or overwrite a file inside workspace_root and return its unified diff."""
        resolved = self.security.validate_path(path, allow_write=True, allow_delete=False)
        perm = self.security.check_action_permission(
            "write_file",
            str(resolved),
            risk=RiskLevel.LOW,
            details={"approved": approved, "approval_id": approval_id},
        )
        if not perm["allowed"]:
            return {
                "success": False,
                "written": False,
                "requires_approval": True,
                "approval_id": perm["approval_id"],
                "path": str(resolved),
                "relative_path": self._rel_path(resolved),
                "reason": perm["reason"],
                "diff": "",
            }

        existed = resolved.exists()
        original = resolved.read_text(encoding="utf-8", errors="replace") if existed and resolved.is_file() else ""

        if create_dirs:
            resolved.parent.mkdir(parents=True, exist_ok=True)

        resolved.write_text(content, encoding="utf-8")
        rel = self._rel_path(resolved)
        diff = self.compute_diff(original, content, rel)

        return {
            "success": True,
            "written": True,
            "created": not existed,
            "requires_approval": False,
            "path": str(resolved),
            "relative_path": rel,
            "bytes_written": len(content.encode("utf-8")),
            "diff": diff,
        }

    def edit_file(
        self,
        path: Union[str, Path],
        target: str,
        replacement: str,
        approved: bool = False,
        approval_id: Optional[str] = None,
        count: int = -1,
    ) -> Dict[str, Any]:
        """Perform exact target replacement in an existing file and return the unified diff."""
        resolved = self.security.validate_path(path, allow_write=True, allow_delete=False)
        if not resolved.exists():
            raise FileNotFoundError(f"Cannot edit non-existent file: '{path}'")

        perm = self.security.check_action_permission(
            "edit_file",
            str(resolved),
            risk=RiskLevel.LOW,
            details={"approved": approved, "approval_id": approval_id},
        )
        if not perm["allowed"]:
            return {
                "success": False,
                "edited": False,
                "requires_approval": True,
                "approval_id": perm["approval_id"],
                "path": str(resolved),
                "relative_path": self._rel_path(resolved),
                "reason": perm["reason"],
                "diff": "",
            }

        original = resolved.read_text(encoding="utf-8", errors="replace")
        if not target or target not in original:
            raise ValueError(f"Target snippet not found in '{self._rel_path(resolved)}'.")

        occurrences = original.count(target) if count < 0 else min(count, original.count(target))
        modified = original.replace(target, replacement) if count < 0 else original.replace(target, replacement, count)

        resolved.write_text(modified, encoding="utf-8")
        rel = self._rel_path(resolved)
        diff = self.compute_diff(original, modified, rel)

        return {
            "success": True,
            "edited": True,
            "requires_approval": False,
            "path": str(resolved),
            "relative_path": rel,
            "replacements": occurrences,
            "diff": diff,
        }

    def move_file(
        self,
        src_path: Union[str, Path],
        dest_path: Union[str, Path],
        approved: bool = False,
        approval_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Move or rename a file within workspace_root."""
        resolved_src = self.security.validate_path(src_path, allow_write=True, allow_delete=False)
        resolved_dest = self.security.validate_path(dest_path, allow_write=True, allow_delete=False)

        if not resolved_src.exists():
            raise FileNotFoundError(f"Source file not found: '{src_path}'")

        perm = self.security.check_action_permission(
            "move_file",
            f"{resolved_src} -> {resolved_dest}",
            risk=RiskLevel.MEDIUM,
            details={"approved": approved, "approval_id": approval_id},
        )
        if not perm["allowed"]:
            return {
                "success": False,
                "moved": False,
                "requires_approval": True,
                "approval_id": perm["approval_id"],
                "src": str(resolved_src),
                "dest": str(resolved_dest),
                "reason": perm["reason"],
            }

        resolved_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(resolved_src), str(resolved_dest))

        return {
            "success": True,
            "moved": True,
            "requires_approval": False,
            "src": str(resolved_src),
            "dest": str(resolved_dest),
            "relative_src": self._rel_path(resolved_src),
            "relative_dest": self._rel_path(resolved_dest),
        }

    def delete_file(
        self,
        path: Union[str, Path],
        approved: bool = False,
        approval_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Delete a file inside workspace_root. Always requires explicit approval
        in Ask, Supervised, and Autonomous modes unless `approved=True` or
        a resolved `approval_id` is supplied.
        """
        resolved = self.security.validate_path(path, allow_write=True, allow_delete=True)
        perm = self.security.check_action_permission(
            "delete_file",
            str(resolved),
            risk=RiskLevel.HIGH,
            details={"approved": approved, "approval_id": approval_id},
        )
        if not perm["allowed"]:
            return {
                "success": False,
                "deleted": False,
                "requires_approval": True,
                "approval_id": perm["approval_id"],
                "path": str(resolved),
                "relative_path": self._rel_path(resolved),
                "reason": perm["reason"],
            }

        if not resolved.exists():
            return {
                "success": False,
                "deleted": False,
                "requires_approval": False,
                "path": str(resolved),
                "relative_path": self._rel_path(resolved),
                "reason": "File does not exist.",
            }

        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()

        return {
            "success": True,
            "deleted": True,
            "requires_approval": False,
            "path": str(resolved),
            "relative_path": self._rel_path(resolved),
        }

    def list_files(
        self,
        subdir: Union[str, Path] = ".",
        pattern: str = "*",
        recursive: bool = True,
        include_hidden: bool = False,
    ) -> List[str]:
        """List files inside workspace_root (or a subdirectory) as relative POSIX paths."""
        resolved_dir = self.security.validate_path(subdir, allow_write=False, allow_delete=False)
        self.security.check_action_permission("list_files", str(resolved_dir), risk=RiskLevel.LOW)

        if not resolved_dir.exists():
            return []

        results: List[str] = []
        if not recursive:
            for item in sorted(resolved_dir.iterdir()):
                if item.is_file():
                    if not include_hidden and item.name.startswith("."):
                        continue
                    if fnmatch.fnmatch(item.name, pattern):
                        results.append(self._rel_path(item))
            return results

        for root, dirs, files in os.walk(resolved_dir):
            if not include_hidden:
                dirs[:] = [
                    d for d in sorted(dirs) if d not in self.IGNORED_DIRS and not d.startswith(".")
                ]
            else:
                dirs[:] = [d for d in sorted(dirs) if d not in (".git", "__pycache__")]

            for fname in sorted(files):
                if not include_hidden and fname.startswith("."):
                    continue
                if fnmatch.fnmatch(fname, pattern):
                    full_path = Path(root) / fname
                    results.append(self._rel_path(full_path))

        return sorted(results)
