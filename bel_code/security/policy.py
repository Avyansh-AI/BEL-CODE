"""Security Policy, Workspace Isolation, Risk Classification, Approval Gate,
Indirect Prompt Injection Scanner, and Tamper-Evident Audit Logger for BEL Code.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union


class RiskLevel(str, Enum):
    """Granular risk levels for file, terminal, network, and deployment actions."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class OperatingSecurityMode(str, Enum):
    """Operating security modes defined in BEL Code Specification Section 15."""

    ASK = "ask"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


_RISK_ORDER: Dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def _normalize_risk(risk: Union[str, RiskLevel]) -> RiskLevel:
    if isinstance(risk, RiskLevel):
        return risk
    val = str(risk).strip().lower()
    for member in RiskLevel:
        if member.value == val:
            return member
    return RiskLevel.LOW


class SecurityPolicy:
    """Enforces workspace isolation, operating security modes, command risk
    classification, human approval gates, indirect prompt injection scanning,
    and SHA-256 hash-chained tamper-evident audit logging.
    """

    VALID_MODES: Tuple[str, ...] = ("ask", "supervised", "autonomous")

    # Sensitive filenames / path segments that must never be overwritten or deleted
    SENSITIVE_PATH_PATTERNS: Tuple[str, ...] = (
        ".git/config",
        ".git/hooks",
        ".ssh/",
        "id_rsa",
        "id_ed25519",
        ".aws/credentials",
        "etc/passwd",
        "etc/shadow",
        "windows/system32",
        "vault.enc",
    )

    # Critical / destructive shell command patterns
    CRITICAL_COMMAND_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|--recursive\s+--force)\b", re.IGNORECASE),
            "Destructive recursive force deletion (rm -rf)",
        ),
        (
            re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(/|/\*|~|\.\.)\b", re.IGNORECASE),
            "Destructive root or parent directory deletion",
        ),
        (
            re.compile(r"\b(del|erase)\s+(/[a-zA-Z]\s*)*(/s|/q)\b", re.IGNORECASE),
            "Destructive Windows file deletion (del /s or /q)",
        ),
        (
            re.compile(r"\b(rmdir|rd)\s+(/[a-zA-Z]\s+)*/s\b", re.IGNORECASE),
            "Destructive Windows directory tree removal (rd /s)",
        ),
        (
            re.compile(r"\bformat\s+[a-zA-Z]:", re.IGNORECASE),
            "Disk format command detected",
        ),
        (
            re.compile(r"\b(mkfs|wipefs|shred|dd\s+if=)\b", re.IGNORECASE),
            "Low-level disk destructive command detected",
        ),
        (
            re.compile(r"\b(DROP\s+TABLE|DROP\s+DATABASE|TRUNCATE\s+TABLE)\b", re.IGNORECASE),
            "Destructive SQL schema/data operation (DROP/TRUNCATE)",
        ),
        (
            re.compile(r"\bgit\s+push\s+([^\n]*\s+)?(--force|-f|--force-with-lease)\b", re.IGNORECASE),
            "Destructive git force push (git push --force)",
        ),
        (
            re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
            "Destructive git working tree reset (git reset --hard)",
        ),
        (
            re.compile(r"\bgit\s+clean\s+-[a-zA-Z]*f[a-zA-Z]*\b", re.IGNORECASE),
            "Destructive git untracked file purge (git clean -f)",
        ),
        (
            re.compile(r"\b(curl|wget)\b[^\n]*\|\s*(sh|bash|zsh|pwsh|powershell|python)\b", re.IGNORECASE),
            "Remote script piped directly to shell interpreter",
        ),
        (
            re.compile(r"\b(cat|type|Get-Content)\b[^\n]*(\.ssh|id_rsa|/etc/shadow|/etc/passwd|\.aws/credentials|vault\.enc)", re.IGNORECASE),
            "Direct credential or sensitive system file access",
        ),
        (
            re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0)\b", re.IGNORECASE),
            "System power/shutdown command",
        ),
    )

    # High-risk command patterns requiring explicit human approval
    HIGH_COMMAND_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(r"\b(npm\s+publish|twine\s+upload|cargo\s+publish|gem\s+push|poetry\s+publish)\b", re.IGNORECASE),
            "Package registry publishing command",
        ),
        (
            re.compile(r"\b(docker\s+push|kubectl\s+(apply|delete)|terraform\s+(apply|destroy)|helm\s+(install|upgrade|uninstall))\b", re.IGNORECASE),
            "Infrastructure or production deployment command",
        ),
        (
            re.compile(r"\bgit\s+push\b", re.IGNORECASE),
            "Remote git repository push",
        ),
        (
            re.compile(r"\b(curl|wget|Invoke-WebRequest|Invoke-RestMethod)\b", re.IGNORECASE),
            "Outbound network request via CLI tool",
        ),
        (
            re.compile(r"\b(ssh|scp|sftp|nc|netcat|ncat|telnet)\b", re.IGNORECASE),
            "Remote network socket or shell connection",
        ),
        (
            re.compile(r"\b(rm|del|Remove-Item)\b", re.IGNORECASE),
            "File deletion command in shell",
        ),
        (
            re.compile(r"\b(sudo|runas|su\s+-)\b", re.IGNORECASE),
            "Privilege escalation command",
        ),
        (
            re.compile(r"\b(env|printenv|set)\b[^\n]*\|\s*(curl|wget|nc)", re.IGNORECASE),
            "Environment variable exfiltration attempt",
        ),
    )

    # Medium-risk local state mutation patterns
    MEDIUM_COMMAND_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(r"\bgit\s+(commit|checkout|switch|merge|rebase|stash|tag|branch\s+-[dD])\b", re.IGNORECASE),
            "Local git repository mutation",
        ),
        (
            re.compile(r"\b(pip\s+install|python\s+-m\s+pip\s+install|npm\s+install|yarn\s+add|pnpm\s+add|cargo\s+add)\b", re.IGNORECASE),
            "Package dependency installation",
        ),
        (
            re.compile(r"\b(mv|cp|mkdir|touch|Move-Item|Copy-Item|New-Item)\b", re.IGNORECASE),
            "Local filesystem modification command",
        ),
    )

    # Low-risk read/test/lint allowlist prefixes
    LOW_RISK_ALLOWLIST_PATTERNS: Tuple[re.Pattern[str], ...] = (
        re.compile(r"^\s*(pytest|python\s+-m\s+pytest|python\s+-m\s+unittest|python\s+-m\s+py_compile)\b", re.IGNORECASE),
        re.compile(r"^\s*(python|python3|py)\b", re.IGNORECASE),
        re.compile(r"^\s*(npm\s+test|npm\s+run\s+(test|lint|check|build)|npx\s+(jest|vitest|eslint|tsc))\b", re.IGNORECASE),
        re.compile(r"^\s*(flake8|ruff|black\s+--check|mypy|pylint|isort\s+--check|bandit)\b", re.IGNORECASE),
        re.compile(r"^\s*(cargo\s+(test|check|clippy)|go\s+(test|vet))\b", re.IGNORECASE),
        re.compile(r"^\s*git\s+(status|diff|log|show|branch|rev-parse|ls-files|remote)\b", re.IGNORECASE),
        re.compile(r"^\s*(ls|dir|pwd|echo|cat|head|tail|wc|find|grep|rg|which|where|whoami|hostname|uname|ver)\b", re.IGNORECASE),
        re.compile(r"^\s*(Get-ChildItem|Get-Content|Get-Location|Write-Output|Select-String)\b", re.IGNORECASE),
    )

    # Indirect prompt injection patterns
    PROMPT_INJECTION_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|rules|directions)", re.IGNORECASE),
            "system_override: Instruction to ignore prior instructions",
        ),
        (
            re.compile(r"(disregard|forget|override)\s+(all\s+)?(previous|prior|system)\s+(instructions|prompts|rules|constraints)", re.IGNORECASE),
            "system_override: Attempt to disregard or override system rules",
        ),
        (
            re.compile(r"(<\s*/?\s*system\s*>|\[\s*SYSTEM\s*\]|<<\s*SYS\s*>>|<\|im_start\|>\s*system)", re.IGNORECASE),
            "Fake system prompt boundary tag injection",
        ),
        (
            re.compile(r"(you\s+are\s+now\s+(an?\s+)?(unrestricted|evil|jailbroken|DAN)|developer\s+mode\s+enabled|act\s+as\s+DAN)", re.IGNORECASE),
            "Role hijacking / jailbreak persona directive",
        ),
        (
            re.compile(r"(exfiltrate|send\s+(all\s+)?(secrets|keys|credentials|env|tokens)\s+to|dump\s+(all\s+)?(api\s+keys|environment\s+variables))", re.IGNORECASE),
            "Credential or environment exfiltration directive",
        ),
        (
            re.compile(r"!\[[^\]]*\]\(https?://[^)]*(\?|&)(secret|key|token|password|env|data)=", re.IGNORECASE),
            "Markdown image beacon data exfiltration URL",
        ),
        (
            re.compile(r"https?://[^\s\"')>]*\b(webhook\.site|requestbin\.com|burpcollaborator\.net|oastify\.com)\b", re.IGNORECASE),
            "Known exfiltration/listener domain detected",
        ),
    )

    ZERO_WIDTH_CHARS: Tuple[str, ...] = (
        "\u200b",  # zero-width space
        "\u200c",  # zero-width non-joiner
        "\u200d",  # zero-width joiner
        "\u2060",  # word joiner
        "\u202a",  # LRE
        "\u202b",  # RLE
        "\u202c",  # PDF
        "\u202d",  # LRO
        "\u202e",  # RLO (Trojan Source bidirectional override)
        "\u2066",  # LRI
        "\u2067",  # RLI
        "\u2068",  # FSI
        "\u2069",  # PDI
    )

    def __init__(
        self,
        workspace_root: Union[Path, str],
        mode: str = "supervised",
        audit_log_path: Optional[Union[Path, str]] = None,
        allowed_domains: Optional[List[str]] = None,
        time_limit_seconds: int = 3600,
        spending_cap_usd: float = 50.0,
    ) -> None:
        self.workspace_root: Path = Path(workspace_root).resolve()
        mode_val = mode.value if isinstance(mode, Enum) else str(mode).strip().lower()
        if mode_val not in self.VALID_MODES:
            raise ValueError(f"Invalid security mode '{mode}'. Must be one of {self.VALID_MODES}.")
        self.mode: str = mode_val

        if audit_log_path is not None:
            self.audit_log_path: Path = Path(audit_log_path).resolve()
        else:
            ws_hash = hashlib.sha256(str(self.workspace_root).encode("utf-8")).hexdigest()[:12]
            default_dir = Path.home() / ".bel_code" / "audit"
            self.audit_log_path = default_dir / f"audit_{ws_hash}_{uuid.uuid4().hex[:6]}.jsonl"

        self.allowed_domains: List[str] = list(
            allowed_domains
            or [
                "localhost",
                "127.0.0.1",
                "0.0.0.0",
                "api.github.com",
                "pypi.org",
                "files.pythonhosted.org",
                "registry.npmjs.org",
            ]
        )
        self.time_limit_seconds: int = time_limit_seconds
        self.spending_cap_usd: float = spending_cap_usd

        self._approvals: Dict[str, Dict[str, Any]] = {}
        self._approved_signatures: Set[Tuple[str, str]] = set()
        self._audit_entries: List[Dict[str, Any]] = []

        # Load existing audit entries if the log file already exists on disk
        if self.audit_log_path.exists():
            try:
                for line in self.audit_log_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        self._audit_entries.append(json.loads(line))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Mode Management & Status
    # ------------------------------------------------------------------
    def set_mode(self, mode: Union[str, OperatingSecurityMode]) -> Dict[str, Any]:
        """Update the operating security mode ('ask', 'supervised', 'autonomous')."""
        mode_val = mode.value if isinstance(mode, Enum) else str(mode).strip().lower()
        if mode_val not in self.VALID_MODES:
            raise ValueError(f"Invalid security mode '{mode}'. Must be one of {self.VALID_MODES}.")
        prev_mode = self.mode
        self.mode = mode_val
        self.log_audit_event(
            action="set_security_mode",
            target=mode_val,
            risk=RiskLevel.LOW,
            outcome="allowed",
            actor="user",
            details={"previous_mode": prev_mode, "new_mode": mode_val},
        )
        return {"mode": self.mode, "previous_mode": prev_mode, "status": "updated"}

    def get_status(self) -> Dict[str, Any]:
        """Return current security policy status and audit health summary."""
        chain_check = self.verify_audit_chain()
        return {
            "mode": self.mode,
            "workspace_root": str(self.workspace_root),
            "audit_log_path": str(self.audit_log_path),
            "pending_approvals_count": len(self.list_pending_approvals(include_resolved=False)),
            "total_approvals_count": len(self._approvals),
            "audit_entries_count": len(self._audit_entries),
            "audit_chain_valid": chain_check["valid"],
            "allowed_domains": self.allowed_domains,
            "time_limit_seconds": self.time_limit_seconds,
            "spending_cap_usd": self.spending_cap_usd,
        }

    # ------------------------------------------------------------------
    # Workspace Isolation & Canonical Path Validation
    # ------------------------------------------------------------------
    def validate_path(
        self,
        target_path: Union[str, Path],
        allow_write: bool = False,
        allow_delete: bool = False,
    ) -> Path:
        """Resolve real/canonical path and raise PermissionError if outside
        workspace_root or targeting sensitive system/credential files.
        """
        raw_str = str(target_path)
        if "\x00" in raw_str:
            self.log_audit_event(
                action="validate_path",
                target=raw_str,
                risk=RiskLevel.CRITICAL,
                outcome="blocked",
                details={"reason": "Null byte in path"},
            )
            raise PermissionError("Invalid path containing null byte.")

        candidate = Path(target_path)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate

        try:
            resolved = candidate.resolve()
        except Exception as exc:
            raise PermissionError(f"Failed to resolve path '{target_path}': {exc}") from exc

        # Verify resolved path is inside workspace_root
        try:
            is_inside = resolved == self.workspace_root or resolved.is_relative_to(self.workspace_root)
        except AttributeError:
            # Fallback for older pathlib if ever needed
            is_inside = resolved == self.workspace_root or self.workspace_root in resolved.parents

        if not is_inside:
            self.log_audit_event(
                action="path_traversal_blocked",
                target=str(target_path),
                risk=RiskLevel.CRITICAL,
                outcome="blocked",
                details={
                    "resolved_path": str(resolved),
                    "workspace_root": str(self.workspace_root),
                    "allow_write": allow_write,
                    "allow_delete": allow_delete,
                },
            )
            raise PermissionError(
                f"Access denied: path '{target_path}' resolves to '{resolved}', "
                f"which is outside workspace root '{self.workspace_root}'."
            )

        if allow_delete and resolved == self.workspace_root:
            self.log_audit_event(
                action="workspace_root_delete_blocked",
                target=str(resolved),
                risk=RiskLevel.CRITICAL,
                outcome="blocked",
            )
            raise PermissionError("Cannot delete the workspace root directory itself.")

        # Check sensitive system/credential paths on write or delete (or sensitive OS files on read)
        norm_posix = resolved.as_posix().lower()
        for pattern in self.SENSITIVE_PATH_PATTERNS:
            if pattern in norm_posix:
                if allow_write or allow_delete or pattern in ("etc/passwd", "etc/shadow", ".ssh/", ".aws/credentials", "vault.enc"):
                    self.log_audit_event(
                        action="sensitive_path_blocked",
                        target=str(resolved),
                        risk=RiskLevel.CRITICAL,
                        outcome="blocked",
                        details={"matched_pattern": pattern},
                    )
                    raise PermissionError(
                        f"Access denied: path '{resolved}' matches protected sensitive pattern '{pattern}'."
                    )

        return resolved

    def is_path_allowed(
        self,
        target_path: Union[str, Path],
        allow_write: bool = False,
        allow_delete: bool = False,
    ) -> bool:
        """Non-raising helper to check whether a path is valid within workspace boundaries."""
        try:
            self.validate_path(target_path, allow_write=allow_write, allow_delete=allow_delete)
            return True
        except PermissionError:
            return False

    # ------------------------------------------------------------------
    # Command Risk Classification
    # ------------------------------------------------------------------
    def classify_command(self, command: str) -> Dict[str, Any]:
        """Classify a shell command against allowlists/denylists and operating mode.

        Returns:
            {
                "risk": RiskLevel,
                "allowed": bool,
                "requires_approval": bool,
                "reason": str,
                "command": str,
                "matched_rules": List[str],
            }
        """
        cmd_str = (command or "").strip()
        if not cmd_str:
            return {
                "risk": RiskLevel.LOW,
                "allowed": True,
                "requires_approval": False,
                "reason": "Empty command",
                "command": cmd_str,
                "matched_rules": [],
            }

        matched_rules: List[str] = []
        risk = RiskLevel.LOW

        # 1. Check CRITICAL destructive/credential patterns first
        for pattern, description in self.CRITICAL_COMMAND_PATTERNS:
            if pattern.search(cmd_str):
                matched_rules.append(description)
                risk = RiskLevel.CRITICAL

        # 2. Check HIGH risk patterns if not already CRITICAL
        if risk != RiskLevel.CRITICAL:
            for pattern, description in self.HIGH_COMMAND_PATTERNS:
                if pattern.search(cmd_str):
                    # Special check for curl/wget to localhost or allowed domains
                    if "Outbound network request" in description:
                        if self._is_trusted_network_command(cmd_str):
                            matched_rules.append("Network request to trusted/local endpoint")
                            if _RISK_ORDER[risk] < _RISK_ORDER[RiskLevel.MEDIUM]:
                                risk = RiskLevel.MEDIUM
                            continue
                    matched_rules.append(description)
                    if _RISK_ORDER[risk] < _RISK_ORDER[RiskLevel.HIGH]:
                        risk = RiskLevel.HIGH

        # 3. Check MEDIUM risk patterns if still LOW
        if risk == RiskLevel.LOW:
            for pattern, description in self.MEDIUM_COMMAND_PATTERNS:
                if pattern.search(cmd_str):
                    matched_rules.append(description)
                    risk = RiskLevel.MEDIUM

        # 4. Check if command matches safe LOW risk allowlist
        if risk == RiskLevel.LOW:
            is_allowlisted = any(p.search(cmd_str) for p in self.LOW_RISK_ALLOWLIST_PATTERNS)
            if is_allowlisted:
                matched_rules.append("Safe allowlisted development/test/inspection command")
            else:
                matched_rules.append("Unrecognized shell command classified as medium risk")
                risk = RiskLevel.MEDIUM

        # Check if this exact command was already explicitly approved
        if ("terminal_command", cmd_str) in self._approved_signatures or ("command", cmd_str) in self._approved_signatures:
            return {
                "risk": risk,
                "allowed": True,
                "requires_approval": False,
                "reason": "Explicitly approved by human reviewer",
                "command": cmd_str,
                "matched_rules": matched_rules,
            }

        # Determine allowed & requires_approval based on operating security mode
        if self.mode == OperatingSecurityMode.ASK.value:
            allowed = False
            requires_approval = True
            reason = f"Ask Mode requires explicit user approval before running command ({'; '.join(matched_rules)})"
        elif self.mode == OperatingSecurityMode.SUPERVISED.value:
            if risk in (RiskLevel.LOW, RiskLevel.MEDIUM):
                allowed = True
                requires_approval = False
                reason = "; ".join(matched_rules)
            else:
                allowed = False
                requires_approval = True
                reason = f"Supervised Mode requires user approval for {risk.value}-risk command: {'; '.join(matched_rules)}"
        else:  # autonomous mode
            if risk in (RiskLevel.LOW, RiskLevel.MEDIUM):
                allowed = True
                requires_approval = False
                reason = "; ".join(matched_rules)
            else:
                allowed = False
                requires_approval = True
                reason = f"Autonomous Mode blocks {risk.value}-risk/destructive command without approval: {'; '.join(matched_rules)}"

        return {
            "risk": risk,
            "allowed": allowed,
            "requires_approval": requires_approval,
            "reason": reason,
            "command": cmd_str,
            "matched_rules": matched_rules,
        }

    def _is_trusted_network_command(self, cmd_str: str) -> bool:
        """Check whether all URLs in a curl/wget command target localhost or allowed_domains."""
        urls = re.findall(r"https?://([^\s/:]+)", cmd_str, flags=re.IGNORECASE)
        if not urls:
            return False
        for host in urls:
            host_low = host.lower()
            if not any(host_low == dom.lower() or host_low.endswith("." + dom.lower()) for dom in self.allowed_domains):
                return False
        return True

    # ------------------------------------------------------------------
    # Human Approval Gate
    # ------------------------------------------------------------------
    def check_action_permission(
        self,
        action_type: str,
        target: str,
        risk: Union[str, RiskLevel] = "low",
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Evaluate whether an action is permitted under the active security mode,
        creating an approval request automatically when human sign-off is required.
        """
        details = details or {}
        action_norm = (action_type or "").strip().lower()
        target_str = str(target)
        risk_enum = _normalize_risk(risk)

        # Elevate risk automatically for inherently high-risk action types
        if action_norm in ("delete_file", "deploy", "publish", "force_push", "git_force_push"):
            if _RISK_ORDER[risk_enum] < _RISK_ORDER[RiskLevel.HIGH]:
                risk_enum = RiskLevel.HIGH

        # Check if explicitly approved via details or resolved approval ID
        explicitly_approved = bool(details.get("approved") is True)
        approval_id = details.get("approval_id")
        if approval_id and approval_id in self._approvals:
            if self._approvals[approval_id].get("status") == "approved":
                explicitly_approved = True

        if (action_norm, target_str) in self._approved_signatures:
            explicitly_approved = True

        if explicitly_approved:
            self.log_audit_event(
                action=action_norm,
                target=target_str,
                risk=risk_enum,
                outcome="allowed",
                details={"reason": "Explicitly approved", **details},
            )
            return {
                "allowed": True,
                "requires_approval": False,
                "mode": self.mode,
                "action_type": action_norm,
                "target": target_str,
                "risk": risk_enum,
                "reason": "Action explicitly approved by user.",
                "approval_id": approval_id,
            }

        # Read-only safe inspection actions are allowed in all modes (within workspace)
        read_only_actions = {
            "read_file",
            "list_files",
            "git_status",
            "git_diff",
            "git_log",
            "scan_project",
            "inspect_html",
        }
        if action_norm in read_only_actions and risk_enum == RiskLevel.LOW:
            self.log_audit_event(
                action=action_norm,
                target=target_str,
                risk=risk_enum,
                outcome="allowed",
                details=details,
            )
            return {
                "allowed": True,
                "requires_approval": False,
                "mode": self.mode,
                "action_type": action_norm,
                "target": target_str,
                "risk": risk_enum,
                "reason": "Read-only inspection allowed within workspace.",
                "approval_id": None,
            }

        # File deletion ALWAYS requires explicit approval in Ask/Supervised/Autonomous unless approved
        if action_norm == "delete_file":
            req = self.create_approval_request(
                action_type=action_norm,
                target=target_str,
                risk=risk_enum,
                reason=f"File deletion requires explicit approval in {self.mode} mode.",
                details=details,
            )
            return {
                "allowed": False,
                "requires_approval": True,
                "mode": self.mode,
                "action_type": action_norm,
                "target": target_str,
                "risk": risk_enum,
                "reason": req["reason"],
                "approval_id": req["approval_id"],
            }

        # Evaluate mode-specific rules
        if self.mode == OperatingSecurityMode.ASK.value:
            req = self.create_approval_request(
                action_type=action_norm,
                target=target_str,
                risk=risk_enum,
                reason=f"Ask Mode requires user approval for '{action_norm}' on '{target_str}'.",
                details=details,
            )
            return {
                "allowed": False,
                "requires_approval": True,
                "mode": self.mode,
                "action_type": action_norm,
                "target": target_str,
                "risk": risk_enum,
                "reason": req["reason"],
                "approval_id": req["approval_id"],
            }

        if self.mode == OperatingSecurityMode.SUPERVISED.value:
            if risk_enum in (RiskLevel.LOW, RiskLevel.MEDIUM):
                self.log_audit_event(
                    action=action_norm,
                    target=target_str,
                    risk=risk_enum,
                    outcome="allowed",
                    details=details,
                )
                return {
                    "allowed": True,
                    "requires_approval": False,
                    "mode": self.mode,
                    "action_type": action_norm,
                    "target": target_str,
                    "risk": risk_enum,
                    "reason": f"Auto-approved {risk_enum.value}-risk action in Supervised Mode.",
                    "approval_id": None,
                }
            req = self.create_approval_request(
                action_type=action_norm,
                target=target_str,
                risk=risk_enum,
                reason=f"Supervised Mode requires approval for {risk_enum.value}-risk action '{action_norm}'.",
                details=details,
            )
            return {
                "allowed": False,
                "requires_approval": True,
                "mode": self.mode,
                "action_type": action_norm,
                "target": target_str,
                "risk": risk_enum,
                "reason": req["reason"],
                "approval_id": req["approval_id"],
            }

        # Autonomous Mode: executes low/medium risk within workspace independently; pauses on high/critical
        if risk_enum in (RiskLevel.LOW, RiskLevel.MEDIUM):
            self.log_audit_event(
                action=action_norm,
                target=target_str,
                risk=risk_enum,
                outcome="allowed",
                details=details,
            )
            return {
                "allowed": True,
                "requires_approval": False,
                "mode": self.mode,
                "action_type": action_norm,
                "target": target_str,
                "risk": risk_enum,
                "reason": f"Allowed in Autonomous Mode within workspace boundaries ({risk_enum.value} risk).",
                "approval_id": None,
            }

        req = self.create_approval_request(
            action_type=action_norm,
            target=target_str,
            risk=risk_enum,
            reason=f"Autonomous Mode requires human sign-off for {risk_enum.value}-risk action '{action_norm}'.",
            details=details,
        )
        return {
            "allowed": False,
            "requires_approval": True,
            "mode": self.mode,
            "action_type": action_norm,
            "target": target_str,
            "risk": risk_enum,
            "reason": req["reason"],
            "approval_id": req["approval_id"],
        }

    def create_approval_request(
        self,
        action_type: str,
        target: str,
        risk: Union[str, RiskLevel] = "high",
        reason: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create and register a pending human approval request."""
        risk_enum = _normalize_risk(risk)
        approval_id = f"apr_{uuid.uuid4().hex[:10]}"
        record: Dict[str, Any] = {
            "id": approval_id,
            "approval_id": approval_id,
            "action_type": (action_type or "").strip().lower(),
            "target": str(target),
            "risk": risk_enum,
            "reason": reason or f"Approval required for {action_type} on {target}",
            "details": details or {},
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "resolved_at": None,
            "reviewer_note": "",
        }
        self._approvals[approval_id] = record
        self.log_audit_event(
            action="create_approval_request",
            target=str(target),
            risk=risk_enum,
            outcome="requires_approval",
            details={"approval_id": approval_id, "action_type": action_type, "reason": record["reason"]},
        )
        return record

    def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        reviewer_note: str = "",
    ) -> Dict[str, Any]:
        """Approve or reject a pending approval request by ID."""
        if approval_id not in self._approvals:
            raise KeyError(f"Approval request '{approval_id}' not found.")

        record = self._approvals[approval_id]
        record["status"] = "approved" if approved else "rejected"
        record["approved"] = bool(approved)
        record["resolved_at"] = datetime.now(timezone.utc).isoformat()
        record["reviewer_note"] = reviewer_note

        if approved:
            self._approved_signatures.add((record["action_type"], record["target"]))

        self.log_audit_event(
            action="resolve_approval",
            target=record["target"],
            risk=record["risk"],
            outcome="approved" if approved else "rejected",
            actor="user",
            details={
                "approval_id": approval_id,
                "action_type": record["action_type"],
                "approved": approved,
                "reviewer_note": reviewer_note,
            },
        )
        return record

    def list_pending_approvals(self, include_resolved: bool = False) -> List[Dict[str, Any]]:
        """Return all pending (or all) human approval requests."""
        if include_resolved:
            return list(self._approvals.values())
        return [req for req in self._approvals.values() if req.get("status") == "pending"]

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a specific approval request by ID."""
        return self._approvals.get(approval_id)

    def is_approved(self, action_type: str, target: str) -> bool:
        """Check if a specific (action_type, target) pair has been approved."""
        return ((action_type or "").strip().lower(), str(target)) in self._approved_signatures

    # ------------------------------------------------------------------
    # Indirect Prompt Injection Scanner
    # ------------------------------------------------------------------
    def scan_prompt_injection(self, text: str, source: str = "external") -> Dict[str, Any]:
        """Scan untrusted text from repository files, issues, web pages, or tool
        outputs for embedded prompt overrides, hidden payloads, and exfiltration URLs.

        Returns:
            {"safe": bool, "threats": List[str], "sanitized_text": str, "source": str}
        """
        if not text:
            return {
                "safe": True,
                "threats": [],
                "sanitized_text": "",
                "source": source,
                "risk": RiskLevel.LOW,
            }

        threats: List[str] = []
        sanitized = text

        # 1. Check zero-width / bidirectional unicode obfuscation
        found_zw = [ch for ch in self.ZERO_WIDTH_CHARS if ch in sanitized]
        if found_zw:
            threats.append("Hidden zero-width or bidirectional Unicode control characters detected")
            for ch in self.ZERO_WIDTH_CHARS:
                sanitized = sanitized.replace(ch, "")

        # 2. Check regex patterns for prompt overrides, fake system tags, exfiltration
        for pattern, description in self.PROMPT_INJECTION_PATTERNS:
            if pattern.search(sanitized):
                threats.append(description)
                sanitized = pattern.sub("[REDACTED_PROMPT_INJECTION]", sanitized)

        # 3. Check suspicious Base64 encoded instruction payloads
        b64_candidates = re.findall(r"\b[A-Za-z0-9+/]{24,}={0,2}\b", sanitized)
        suspicious_b64_keywords = (
            "ignore previous",
            "ignore all previous",
            "<system>",
            "rm -rf",
            "curl ",
            "wget ",
            "/etc/passwd",
            "exfiltrate",
            "system prompt",
        )
        for candidate in b64_candidates:
            try:
                decoded = base64.b64decode(candidate, validate=True).decode("utf-8", errors="ignore").lower()
                if any(kw in decoded for kw in suspicious_b64_keywords):
                    threats.append("Hidden Base64-encoded prompt injection or command payload detected")
                    sanitized = sanitized.replace(candidate, "[REDACTED_BASE64_PAYLOAD]")
            except Exception:
                continue

        is_safe = len(threats) == 0
        if not is_safe:
            self.log_audit_event(
                action="scan_prompt_injection",
                target=source,
                risk=RiskLevel.CRITICAL,
                outcome="flagged",
                details={"threats": threats, "source": source},
            )

        return {
            "safe": is_safe,
            "threats": threats,
            "sanitized_text": sanitized,
            "source": source,
            "risk": RiskLevel.LOW if is_safe else RiskLevel.CRITICAL,
        }

    # ------------------------------------------------------------------
    # Tamper-Evident SHA-256 Hash-Chained Audit Logger
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_entry_hash(entry_without_hash: Dict[str, Any]) -> str:
        canonical = json.dumps(
            {
                "index": entry_without_hash["index"],
                "timestamp": entry_without_hash["timestamp"],
                "action": entry_without_hash["action"],
                "actor": entry_without_hash["actor"],
                "target": entry_without_hash["target"],
                "risk": entry_without_hash["risk"],
                "outcome": entry_without_hash["outcome"],
                "details": entry_without_hash.get("details", {}),
                "prev_hash": entry_without_hash["prev_hash"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def log_audit_event(
        self,
        action: str,
        target: str,
        risk: Union[str, RiskLevel] = "low",
        outcome: str = "allowed",
        actor: str = "bel_code",
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Append a cryptographically hash-chained entry to the JSONL audit log."""
        risk_val = _normalize_risk(risk).value
        prev_hash = self._audit_entries[-1]["entry_hash"] if self._audit_entries else ("0" * 64)
        idx = len(self._audit_entries)

        entry: Dict[str, Any] = {
            "index": idx,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": str(action),
            "actor": str(actor),
            "target": str(target),
            "risk": risk_val,
            "outcome": str(outcome),
            "details": details or {},
            "prev_hash": prev_hash,
        }
        entry["entry_hash"] = self._compute_entry_hash(entry)
        self._audit_entries.append(entry)

        try:
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        except Exception:
            pass

        return entry

    def get_audit_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return up to `limit` most recent audit log entries."""
        entries = self._read_disk_or_memory_entries()
        if limit <= 0:
            return []
        return entries[-limit:]

    def _read_disk_or_memory_entries(self) -> List[Dict[str, Any]]:
        if self.audit_log_path.exists():
            disk_entries: List[Dict[str, Any]] = []
            for raw_line in self.audit_log_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if line:
                    disk_entries.append(json.loads(line))
            return disk_entries
        return list(self._audit_entries)

    def verify_audit_chain(self) -> Dict[str, Any]:
        """Verify the cryptographic integrity of the entire SHA-256 hash-chained audit log."""
        try:
            entries = self._read_disk_or_memory_entries()
        except Exception as exc:
            return {
                "valid": False,
                "entries_checked": 0,
                "tampered_at_index": 0,
                "error": f"Failed to parse audit log JSONL: {exc}",
            }

        expected_prev = "0" * 64
        for idx, entry in enumerate(entries):
            if entry.get("prev_hash") != expected_prev:
                return {
                    "valid": False,
                    "entries_checked": idx + 1,
                    "tampered_at_index": idx,
                    "error": f"Broken prev_hash link at entry {idx}",
                }
            computed = self._compute_entry_hash(entry)
            if entry.get("entry_hash") != computed:
                return {
                    "valid": False,
                    "entries_checked": idx + 1,
                    "tampered_at_index": idx,
                    "error": f"Entry hash mismatch at entry {idx} (tampering detected)",
                }
            expected_prev = computed

        return {
            "valid": True,
            "entries_checked": len(entries),
            "tampered_at_index": None,
            "head_hash": expected_prev,
        }
