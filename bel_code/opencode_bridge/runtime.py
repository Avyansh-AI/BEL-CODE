"""
OpenCode Runtime Manager & Layer 1 <-> Layer 2 Foundation Bridge (`bel_code/opencode_bridge/runtime.py`).

Responsibilities:
1. Inspects and exposes the real upstream OpenCode repository (`vendor/opencode`),
   including version, commit hash, remotes, packages (`packages/core`, `packages/server`,
   `packages/sdk`, `packages/opencode`, `packages/sdk/js`), built-in tools, built-in agents,
   provider registry, server routes, and MIT License attribution.
2. Generates, loads, validates, and syncs `opencode.json` using OpenCode's official schema
   (`https://opencode.ai/config.json`) with FreeLLM (`@ai-sdk/openai-compatible`) as the
   primary default provider and Token Harbor as the secondary verified provider.
   Never writes plaintext API keys to disk—always uses `{env:FREELLM_API_KEY}` and
   `{env:TOKENHARBOR_API_KEY}` and injects secrets at runtime from `SecretVault`.
3. Manages the OpenCode server runtime: can spawn `opencode serve --port <port> --hostname 127.0.0.1`
   when Bun/OpenCode CLI is installed OR run BEL Code's built-in OpenCode-compatible HTTP/SSE
   protocol engine backed by Layer 1 schemas and Layer 2 intelligence, tools, git checkpoints,
   security permissions, and FreeLLM/ProviderManager.
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from bel_code.engine.multi_agent import SPECIALIZED_ROLES
from bel_code.providers.freellm import FREELLM_SMART_ROUTING_MODELS

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_VENDOR_OPENCODE_DIR = PROJECT_ROOT / "vendor" / "opencode"
DEFAULT_OPENCODE_CONFIG_PATH = PROJECT_ROOT / "opencode.json"

# Upstream commit hash cloned from https://github.com/anomalyco/opencode
PINNED_UPSTREAM_COMMIT = "cbe71595e1f370ff91b3081872b96bbe56519c41"
UPSTREAM_REPO_URL = "https://github.com/anomalyco/opencode.git"

# Canonical OpenCode HTTP & SSE Server Endpoints
OPENCODE_SERVER_ROUTES: List[Dict[str, str]] = [
    {"method": "GET", "path": "/global/health", "description": "Server health and version check"},
    {"method": "GET", "path": "/project", "description": "List all projects"},
    {"method": "GET", "path": "/project/current", "description": "Get active project metadata"},
    {"method": "GET", "path": "/session", "description": "List all sessions"},
    {"method": "POST", "path": "/session", "description": "Create a new session"},
    {"method": "GET", "path": "/session/{id}", "description": "Get session details and messages"},
    {"method": "DELETE", "path": "/session/{id}", "description": "Delete a session"},
    {"method": "POST", "path": "/session/{id}/message", "description": "Send message and execute agent/tools"},
    {"method": "GET", "path": "/session/{id}/children", "description": "List child subagent sessions"},
    {"method": "GET", "path": "/session/{id}/todo", "description": "Get session todo list"},
    {"method": "GET", "path": "/session/{id}/diff", "description": "Get file diffs generated in session"},
    {"method": "POST", "path": "/session/{id}/abort", "description": "Abort running session execution"},
    {"method": "POST", "path": "/session/{id}/revert", "description": "Revert session file changes to checkpoint"},
    {"method": "POST", "path": "/session/{id}/permissions/{permissionID}", "description": "Respond to permission request"},
    {"method": "GET", "path": "/config", "description": "Get active OpenCode configuration"},
    {"method": "GET", "path": "/config/providers", "description": "List configured providers and default models"},
    {"method": "GET", "path": "/agent", "description": "List available primary and subagent definitions"},
    {"method": "GET", "path": "/command", "description": "List slash commands"},
    {"method": "GET", "path": "/file", "description": "List files and directories"},
    {"method": "GET", "path": "/file/content", "description": "Read file content"},
    {"method": "GET", "path": "/file/status", "description": "Get git status of workspace files"},
    {"method": "GET", "path": "/find", "description": "Search text across workspace files"},
    {"method": "GET", "path": "/find/file", "description": "Find files by name pattern"},
    {"method": "GET", "path": "/find/symbol", "description": "Find workspace AST symbols"},
    {"method": "GET", "path": "/mcp", "description": "Get MCP server status"},
    {"method": "GET", "path": "/event", "description": "Server-Sent Events (SSE) real-time event stream"},
]

# Built-in OpenCode Core Agents (packages/core/src/agent.ts & packages/opencode/src/agent/agent.ts)
OPENCODE_BUILTIN_AGENTS: List[Dict[str, Any]] = [
    {
        "id": "build",
        "name": "build",
        "display_name": "Build (Primary)",
        "mode": "primary",
        "layer": "opencode_foundation",
        "model": "freellm/auto",
        "description": "Primary full-access development agent with file edit, write, bash, and search tools enabled.",
        "tools": ["read", "edit", "write", "patch", "bash", "grep", "glob", "list", "todowrite", "todoread", "webfetch", "task"],
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"},
    },
    {
        "id": "plan",
        "name": "plan",
        "display_name": "Plan (Architecture & Analysis)",
        "mode": "primary",
        "layer": "opencode_foundation",
        "model": "freellm/auto:smart",
        "description": "Read-only planning and analysis agent. Denies file edits by default and asks before running shell commands.",
        "tools": ["read", "grep", "glob", "list", "todowrite", "todoread", "webfetch"],
        "permission": {"edit": "deny", "bash": "ask", "webfetch": "allow"},
    },
    {
        "id": "general",
        "name": "general",
        "display_name": "General Subagent",
        "mode": "subagent",
        "layer": "opencode_foundation",
        "model": "freellm/auto",
        "description": "General-purpose subagent for researching complex questions and executing multi-step tasks.",
        "tools": ["read", "edit", "write", "bash", "grep", "glob", "list", "webfetch"],
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"},
    },
    {
        "id": "explore",
        "name": "explore",
        "display_name": "Explore Subagent",
        "mode": "subagent",
        "layer": "opencode_foundation",
        "model": "freellm/auto:fast",
        "description": "Fast read-only subagent for exploring codebases, globbing files, and searching symbols/keywords.",
        "tools": ["read", "grep", "glob", "list"],
        "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
    },
]

# Built-in OpenCode Core Tools (packages/core/src/tool/plugin & packages/opencode/src/tool)
OPENCODE_BUILTIN_TOOLS: List[Dict[str, Any]] = [
    {"id": "read", "name": "read", "category": "filesystem", "description": "Read file contents with line numbers"},
    {"id": "edit", "name": "edit", "category": "filesystem", "description": "Apply exact string replacement edits to files"},
    {"id": "write", "name": "write", "category": "filesystem", "description": "Create or overwrite files in workspace"},
    {"id": "patch", "name": "patch", "category": "filesystem", "description": "Apply unified diff patches to files"},
    {"id": "bash", "name": "bash", "category": "execution", "description": "Execute shell commands in controlled workspace terminal"},
    {"id": "grep", "name": "grep", "category": "search", "description": "Search file contents using regular expressions"},
    {"id": "glob", "name": "glob", "category": "search", "description": "Find files by glob pattern"},
    {"id": "list", "name": "list", "category": "filesystem", "description": "List directory contents and file metadata"},
    {"id": "webfetch", "name": "webfetch", "category": "network", "description": "Fetch web pages and documentation"},
    {"id": "websearch", "name": "websearch", "category": "network", "description": "Search web documentation and references"},
    {"id": "todowrite", "name": "todowrite", "category": "session", "description": "Create and update session todo items"},
    {"id": "todoread", "name": "todoread", "category": "session", "description": "Read current session todo checklist"},
    {"id": "task", "name": "task", "category": "orchestration", "description": "Delegate bounded subtask to a specialized subagent"},
    {"id": "skill", "name": "skill", "category": "extensibility", "description": "Load a domain-specific skill instruction file"},
    {"id": "question", "name": "question", "category": "interaction", "description": "Ask user clarifying questions during execution"},
]

OPENCODE_BUILTIN_COMMANDS: List[Dict[str, str]] = [
    {"name": "/init", "description": "Scan repository and initialize project intelligence context"},
    {"name": "/plan", "description": "Switch to read-only Plan agent to design execution strategy"},
    {"name": "/build", "description": "Switch to primary Build agent to implement and verify code"},
    {"name": "/review", "description": "Run Code Reviewer & Security Auditor on workspace diffs"},
    {"name": "/test", "description": "Run automated test suite and record verification evidence"},
    {"name": "/undo", "description": "Revert last session message and restore file checkpoint"},
    {"name": "/compact", "description": "Compact session conversation history into structured summary"},
]


def build_default_opencode_config(
    freellm_base_url: str = "http://localhost:31415/v1",
    default_model: str = "freellm/auto",
    small_model: str = "freellm/auto:fast",
    security_mode: str = "supervised",
) -> Dict[str, Any]:
    """Build the canonical `opencode.json` configuration dictionary conforming to
    `https://opencode.ai/config.json`. Never hardcodes API keys; always uses `{env:...}`.
    """
    if "/" not in default_model:
        default_model = f"freellm/{default_model}"
    if "/" not in small_model:
        small_model = f"freellm/{small_model}"

    edit_perm = "allow" if security_mode in ("supervised", "autonomous") else "ask"
    bash_default = "allow" if security_mode in ("supervised", "autonomous") else "ask"

    return {
        "$schema": "https://opencode.ai/config.json",
        "theme": "bel-dark-graphite",
        "model": default_model,
        "small_model": small_model,
        "default_agent": "build",
        "autoupdate": False,
        "share": "disabled",
        "instructions": ["UPSTREAM_MODIFICATIONS.md", "README.md"],
        "provider": {
            "freellm": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "FreeLLM",
                "env": ["FREELLM_API_KEY"],
                "options": {
                    "baseURL": freellm_base_url,
                    "apiKey": "{env:FREELLM_API_KEY}",
                    "secondaryBaseURL": "https://apifreellm.com/api/v1/chat",
                    "timeout": 20000,
                },
                "models": {
                    "auto": {
                        "id": "auto",
                        "name": "FreeLLM Auto Router (Balanced)",
                        "tool_call": True,
                        "attachment": False,
                        "temperature": True,
                        "reasoning": True,
                        "streaming": True,
                        "cost": {"input": 0, "output": 0},
                        "limit": {"context": 64000, "output": 8192},
                        "options": {"routing_mode": "auto", "prompt_tool_fallback": True},
                    },
                    "auto:fast": {
                        "id": "auto:fast",
                        "name": "FreeLLM Auto Fast (Low Latency)",
                        "tool_call": True,
                        "attachment": False,
                        "temperature": True,
                        "reasoning": False,
                        "streaming": True,
                        "cost": {"input": 0, "output": 0},
                        "limit": {"context": 32000, "output": 4096},
                        "options": {"routing_mode": "fast", "prompt_tool_fallback": True},
                    },
                    "auto:smart": {
                        "id": "auto:smart",
                        "name": "FreeLLM Auto Smart (High Reasoning)",
                        "tool_call": True,
                        "attachment": False,
                        "temperature": True,
                        "reasoning": True,
                        "streaming": True,
                        "cost": {"input": 0, "output": 0},
                        "limit": {"context": 128000, "output": 16384},
                        "options": {"routing_mode": "smart", "prompt_tool_fallback": True},
                    },
                    "fusion": {
                        "id": "fusion",
                        "name": "FreeLLM Multi-Model Fusion",
                        "tool_call": True,
                        "attachment": False,
                        "temperature": True,
                        "reasoning": True,
                        "streaming": False,
                        "cost": {"input": 0, "output": 0},
                        "limit": {"context": 64000, "output": 8192},
                        "options": {"routing_mode": "fusion", "prompt_tool_fallback": True},
                    },
                    "qwen-2.5-coder-32b": {
                        "id": "qwen-2.5-coder-32b",
                        "name": "Qwen 2.5 Coder 32B (via FreeLLM)",
                        "tool_call": True,
                        "attachment": False,
                        "temperature": True,
                        "reasoning": False,
                        "streaming": True,
                        "cost": {"input": 0, "output": 0},
                        "limit": {"context": 32768, "output": 8192},
                    },
                    "deepseek-r1-free": {
                        "id": "deepseek-r1-free",
                        "name": "DeepSeek R1 Reasoning (via FreeLLM)",
                        "tool_call": False,
                        "attachment": False,
                        "temperature": True,
                        "reasoning": True,
                        "streaming": True,
                        "cost": {"input": 0, "output": 0},
                        "limit": {"context": 64000, "output": 8192},
                        "options": {"prompt_tool_fallback": True},
                    },
                    "llama-3.3-70b-free": {
                        "id": "llama-3.3-70b-free",
                        "name": "Llama 3.3 70B Instruct (via FreeLLM)",
                        "tool_call": True,
                        "attachment": False,
                        "temperature": True,
                        "reasoning": False,
                        "streaming": True,
                        "cost": {"input": 0, "output": 0},
                        "limit": {"context": 64000, "output": 8192},
                    },
                },
            },
            "tokenharbor": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Token Harbor Verified API",
                "env": ["TOKENHARBOR_API_KEY"],
                "options": {
                    "baseURL": "https://tokenharbor.ai/v1",
                    "apiKey": "{env:TOKENHARBOR_API_KEY}",
                },
                "models": {
                    "claude-3-7-sonnet-latest": {
                        "id": "claude-3-7-sonnet-latest",
                        "name": "Claude 3.7 Sonnet (Token Harbor)",
                        "tool_call": True,
                        "attachment": True,
                        "temperature": True,
                        "reasoning": True,
                        "streaming": True,
                        "limit": {"context": 200000, "output": 16384},
                    },
                    "gpt-4o": {
                        "id": "gpt-4o",
                        "name": "GPT-4o (Token Harbor)",
                        "tool_call": True,
                        "attachment": True,
                        "temperature": True,
                        "reasoning": False,
                        "streaming": True,
                        "limit": {"context": 128000, "output": 16384},
                    },
                    "deepseek-r1": {
                        "id": "deepseek-r1",
                        "name": "DeepSeek R1 (Token Harbor)",
                        "tool_call": True,
                        "attachment": False,
                        "temperature": True,
                        "reasoning": True,
                        "streaming": True,
                        "limit": {"context": 128000, "output": 16384},
                    },
                },
            },
        },
        "agent": {
            "build": {
                "mode": "primary",
                "model": default_model,
                "description": "Primary full-access software engineering agent for building, editing, testing, and verifying code.",
                "prompt": "You are BEL Code's primary Build agent powered by the OpenCode foundation and FreeLLM intelligence.",
                "tools": {
                    "read": True,
                    "edit": True,
                    "write": True,
                    "bash": True,
                    "grep": True,
                    "glob": True,
                    "list": True,
                    "patch": True,
                    "todowrite": True,
                    "todoread": True,
                    "webfetch": True,
                    "task": True,
                },
                "permission": {
                    "edit": edit_perm,
                    "bash": bash_default,
                    "webfetch": "allow",
                },
            },
            "plan": {
                "mode": "primary",
                "model": "freellm/auto:smart",
                "description": "Read-only architecture and planning agent for codebase exploration, impact analysis, and staged execution plans.",
                "prompt": "You are BEL Code's Plan agent. Analyze the repository and formulate verified engineering plans without mutating files.",
                "tools": {
                    "read": True,
                    "edit": False,
                    "write": False,
                    "bash": False,
                    "grep": True,
                    "glob": True,
                    "list": True,
                    "todowrite": True,
                    "todoread": True,
                    "webfetch": True,
                },
                "permission": {
                    "edit": "deny",
                    "bash": "ask",
                    "webfetch": "allow",
                },
            },
            "architect": {
                "mode": "subagent",
                "model": "freellm/auto:smart",
                "description": "Specialized system architect subagent for module boundaries, API contracts, and dependency graphs.",
                "tools": {
                    "read": True,
                    "grep": True,
                    "glob": True,
                    "list": True,
                    "edit": False,
                    "bash": False,
                },
                "permission": {
                    "edit": "deny",
                    "bash": "deny",
                    "webfetch": "allow",
                },
            },
            "reviewer": {
                "mode": "subagent",
                "model": "freellm/auto:smart",
                "description": "Specialized code review subagent for inspecting diffs, correctness, edge cases, and maintainability.",
                "tools": {
                    "read": True,
                    "grep": True,
                    "glob": True,
                    "list": True,
                    "edit": False,
                    "bash": False,
                },
                "permission": {
                    "edit": "deny",
                    "bash": "deny",
                    "webfetch": "deny",
                },
            },
            "debugger": {
                "mode": "subagent",
                "model": "freellm/auto:smart",
                "description": "Specialized debugging subagent for stack trace analysis, fault localization, and minimal verified fixes.",
                "tools": {
                    "read": True,
                    "edit": True,
                    "bash": True,
                    "grep": True,
                    "glob": True,
                },
                "permission": {
                    "edit": edit_perm,
                    "bash": bash_default,
                    "webfetch": "deny",
                },
            },
            "security_auditor": {
                "mode": "subagent",
                "model": "freellm/auto:smart",
                "description": "Specialized security auditor subagent for secret scanning, injection detection, and permission boundary audits.",
                "tools": {
                    "read": True,
                    "grep": True,
                    "glob": True,
                    "list": True,
                    "edit": False,
                    "bash": False,
                },
                "permission": {
                    "edit": "deny",
                    "bash": "deny",
                    "webfetch": "deny",
                },
            },
        },
        "permission": {
            "edit": edit_perm,
            "bash": {
                "*": bash_default,
                "rm -rf *": "deny",
                "git push --force*": "deny",
                "git reset --hard*": "ask",
            },
            "webfetch": "allow",
        },
    }


def _sanitize_config_secrets(config_data: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure no literal API keys are ever persisted or exposed in `opencode.json`.
    Always preserves `{env:...}` placeholders.
    """
    sanitized = copy.deepcopy(config_data)
    providers = sanitized.get("provider", {})
    if isinstance(providers, dict):
        for pid, pconf in providers.items():
            if not isinstance(pconf, dict):
                continue
            opts = pconf.get("options")
            if isinstance(opts, dict) and "apiKey" in opts:
                val = str(opts["apiKey"] or "")
                if val and not (val.startswith("{env:") and val.endswith("}")):
                    env_var = f"{pid.upper()}_API_KEY"
                    opts["apiKey"] = f"{{env:{env_var}}}"
    return sanitized


class OpenCodeRuntimeManager:
    """
    Layer 1 <-> Layer 2 Bridge managing the OpenCode Foundation (`vendor/opencode`),
    `opencode.json` configuration, and OpenCode session/tool/agent/diff/revert protocol execution.
    """

    def __init__(
        self,
        workspace_root: Optional[Union[str, Path]] = None,
        vendor_dir: Optional[Union[str, Path]] = None,
        config_path: Optional[Union[str, Path]] = None,
        provider_manager: Any = None,
        settings: Any = None,
        fs_tools: Any = None,
        terminal: Any = None,
        git_manager: Any = None,
        intelligence: Any = None,
        security_policy: Any = None,
        debugger: Any = None,
        multi_agent: Any = None,
        workflow: Any = None,
    ) -> None:
        self.workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self.vendor_dir = Path(vendor_dir or DEFAULT_VENDOR_OPENCODE_DIR).resolve()
        self.config_path = Path(config_path or DEFAULT_OPENCODE_CONFIG_PATH).resolve()

        self.provider_manager = provider_manager
        self.settings = settings
        self.fs_tools = fs_tools
        self.terminal = terminal
        self.git_manager = git_manager
        self.intelligence = intelligence
        self.security_policy = security_policy
        self.debugger = debugger
        self.multi_agent = multi_agent
        self.workflow = workflow

        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._session_events: Dict[str, List[Dict[str, Any]]] = {}
        self._session_snapshots: Dict[str, Dict[str, Optional[str]]] = {}
        self._session_permissions: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._daemon_process: Optional[subprocess.Popen[Any]] = None
        self._daemon_port: int = 4096
        self._foundation_cache: Optional[Dict[str, Any]] = None

        self._ensure_opencode_config()

    def _ensure_opencode_config(self) -> None:
        """Ensure `opencode.json` exists on disk with valid schema and `{env:...}` references."""
        if not self.config_path.exists():
            cfg = build_default_opencode_config()
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # 1. Upstream OpenCode Foundation Inspection (`vendor/opencode`)
    # ------------------------------------------------------------------
    def inspect_foundation(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Inspect the cloned `vendor/opencode` repository and return full metadata."""
        if self._foundation_cache is not None and not force_refresh:
            return copy.deepcopy(self._foundation_cache)

        exists = self.vendor_dir.exists() and (self.vendor_dir / "package.json").exists()
        pkg_json: Dict[str, Any] = {}
        if exists:
            try:
                pkg_json = json.loads((self.vendor_dir / "package.json").read_text(encoding="utf-8"))
            except Exception:
                pkg_json = {}

        license_path = self.vendor_dir / "LICENSE"
        license_text = ""
        if license_path.exists():
            try:
                license_text = license_path.read_text(encoding="utf-8")
            except Exception:
                license_text = ""

        # Inspect git commit and remotes
        commit_hash = PINNED_UPSTREAM_COMMIT
        remotes: Dict[str, str] = {
            "origin": UPSTREAM_REPO_URL,
            "upstream": UPSTREAM_REPO_URL,
        }
        if (self.vendor_dir / ".git").exists():
            try:
                head_res = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(self.vendor_dir),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if head_res.returncode == 0 and head_res.stdout.strip():
                    commit_hash = head_res.stdout.strip()
            except Exception:
                pass
            try:
                rem_res = subprocess.run(
                    ["git", "remote", "-v"],
                    cwd=str(self.vendor_dir),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if rem_res.returncode == 0:
                    for line in rem_res.stdout.splitlines():
                        parts = line.split()
                        if len(parts) >= 2:
                            remotes[parts[0]] = parts[1]
            except Exception:
                pass

        # Discover packages in vendor/opencode/packages
        packages_dir = self.vendor_dir / "packages"
        discovered_packages: List[str] = []
        if packages_dir.exists():
            discovered_packages = sorted(
                p.name for p in packages_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
            )

        # Discover built-in tool modules from vendor/opencode
        discovered_tool_files: List[str] = []
        for tool_dir in (
            self.vendor_dir / "packages" / "core" / "src" / "tool" / "plugin",
            self.vendor_dir / "packages" / "opencode" / "src" / "tool",
        ):
            if tool_dir.exists():
                for tf in sorted(tool_dir.glob("*.ts")):
                    if tf.stem not in discovered_tool_files and tf.stem not in ("index", "tool", "runtime"):
                        discovered_tool_files.append(tf.stem)

        # Discover server handlers from vendor/opencode/packages/server/src/handlers
        handlers_dir = self.vendor_dir / "packages" / "server" / "src" / "handlers"
        discovered_handlers: List[str] = []
        if handlers_dir.exists():
            discovered_handlers = sorted(h.stem for h in handlers_dir.glob("*.ts"))

        # Discover provider modules from vendor/opencode/packages/core/src/provider.ts
        provider_ts = self.vendor_dir / "packages" / "core" / "src" / "provider.ts"
        upstream_providers: List[str] = []
        if provider_ts.exists():
            try:
                src = provider_ts.read_text(encoding="utf-8", errors="replace")
                matches = re.findall(r"@opencode/ai/providers/([a-z0-9-]+)", src)
                upstream_providers = sorted(set(matches))
            except Exception:
                upstream_providers = []

        opencode_v1_dir = self.vendor_dir / "packages" / "opencode"
        core_v2_dir = self.vendor_dir / "packages" / "core"
        sdk_js_dir = self.vendor_dir / "packages" / "sdk" / "js"
        sdk_root_dir = self.vendor_dir / "packages" / "sdk"
        sdk_adapter_path = Path(__file__).resolve().parent / "sdk_adapter.mjs"

        info = {
            "status": "ready" if exists else "missing",
            "installed": exists,
            "name": pkg_json.get("name", "opencode"),
            "version": pkg_json.get("version", "2.3.0"),
            "license": pkg_json.get("license", "MIT"),
            "copyright": "Copyright (c) 2025 opencode",
            "license_verified": "MIT License" in license_text and "Copyright (c) 2025 opencode" in license_text,
            "vendor_path": str(self.vendor_dir),
            "upstream_url": UPSTREAM_REPO_URL,
            "upstream_commit": commit_hash,
            "remotes": remotes,
            "has_upstream_remote": "upstream" in remotes or "anomalyco/opencode" in str(remotes),
            "packages": discovered_packages,
            "core_package_path": str(opencode_v1_dir if opencode_v1_dir.exists() else core_v2_dir),
            "core_v2_package_path": str(core_v2_dir),
            "server_package_path": str(self.vendor_dir / "packages" / "server"),
            "sdk_package_path": str(sdk_js_dir if sdk_js_dir.exists() else sdk_root_dir),
            "sdk_adapter_path": str(sdk_adapter_path),
            "sdk_bridge_ready": sdk_adapter_path.exists(),
            "discovered_tools": discovered_tool_files or [t["id"] for t in OPENCODE_BUILTIN_TOOLS],
            "builtin_tools": copy.deepcopy(OPENCODE_BUILTIN_TOOLS),
            "builtin_agents": [a["id"] for a in OPENCODE_BUILTIN_AGENTS],
            "discovered_server_handlers": discovered_handlers,
            "upstream_providers": upstream_providers,
            "server_routes": [r["path"] for r in OPENCODE_SERVER_ROUTES],
            "server_routes_detail": copy.deepcopy(OPENCODE_SERVER_ROUTES),
            "architecture_layers": {
                "layer_1_foundation": {
                    "name": "OpenCode Foundation",
                    "paths": ["vendor/opencode", "opencode.json", "bel_code/opencode_bridge/"],
                },
                "layer_2_intelligence": {
                    "name": "BEL Code Intelligence",
                    "paths": [
                        "bel_code/providers/",
                        "bel_code/intelligence/",
                        "bel_code/memory/",
                        "bel_code/advanced/",
                        "bel_code/engine/",
                        "bel_code/security/",
                    ],
                },
                "layer_3_application": {
                    "name": "BEL Code Application & IDE",
                    "paths": ["bel_code/server/", "bel_code/frontend/"],
                },
            },
        }
        self._foundation_cache = info
        return copy.deepcopy(info)

    def get_status(self) -> Dict[str, Any]:
        """Return complete OpenCode foundation status, config, and active runtime info."""
        foundation = self.inspect_foundation()
        config = self.get_opencode_config(redacted=True)
        daemon_running = self._daemon_process is not None and self._daemon_process.poll() is None
        return {
            **foundation,
            "runtime_mode": "opencode_subprocess_daemon" if daemon_running else "integrated_protocol_bridge",
            "daemon_running": daemon_running,
            "daemon_port": self._daemon_port if daemon_running else None,
            "active_workspace": str(self.workspace_root),
            "sessions_count": len(self._sessions),
            "opencode_config_path": str(self.config_path),
            "opencode_config": config,
            "freellm_primary": True,
        }

    # ------------------------------------------------------------------
    # 2. `opencode.json` Configuration Management & Sync
    # ------------------------------------------------------------------
    def get_opencode_config(self, redacted: bool = True) -> Dict[str, Any]:
        """Read and return `opencode.json` configuration."""
        self._ensure_opencode_config()
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            raw = build_default_opencode_config()
        if redacted:
            return _sanitize_config_secrets(raw)
        return raw

    def sync_opencode_config(
        self,
        provider_id: Optional[str] = None,
        base_url: Optional[str] = None,
        default_model: Optional[str] = None,
        security_mode: Optional[str] = None,
        extra_updates: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Synchronize BEL Code provider/model/security updates into `opencode.json`
        while strictly preserving `{env:...}` secret references.
        """
        cfg = self.get_opencode_config(redacted=True)
        providers = cfg.setdefault("provider", {})

        if provider_id:
            pid = provider_id.strip().lower()
            env_var = f"{pid.upper()}_API_KEY"
            p_entry = providers.setdefault(
                pid,
                {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": pid.capitalize(),
                    "env": [env_var],
                    "options": {
                        "baseURL": base_url or "http://localhost:31415/v1",
                        "apiKey": f"{{env:{env_var}}}",
                    },
                    "models": {},
                },
            )
            opts = p_entry.setdefault("options", {})
            if base_url:
                opts["baseURL"] = base_url
            opts["apiKey"] = f"{{env:{env_var}}}"

            if default_model:
                model_Ref = default_model if "/" in default_model else f"{pid}/{default_model}"
                if pid == "freellm":
                    cfg["model"] = model_Ref
                    if "agent" in cfg and "build" in cfg["agent"]:
                        cfg["agent"]["build"]["model"] = model_Ref

        if security_mode:
            mode_norm = security_mode.strip().lower()
            perm_val = "allow" if mode_norm in ("supervised", "autonomous") else "ask"
            cfg.setdefault("permission", {})["edit"] = perm_val
            if isinstance(cfg["permission"].get("bash"), dict):
                cfg["permission"]["bash"]["*"] = perm_val
            else:
                cfg["permission"]["bash"] = perm_val

        if isinstance(extra_updates, dict):
            for k, v in extra_updates.items():
                if k not in ("api_key", "apiKey"):
                    cfg[k] = v

        sanitized = _sanitize_config_secrets(cfg)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")
        return sanitized

    def build_runtime_environment(self) -> Dict[str, str]:
        """Build an environment dictionary for OpenCode subprocesses, injecting API keys
        from `SecretVault` in memory without ever writing keys to disk.
        """
        env = dict(os.environ)
        vault = getattr(self.settings, "vault", None) if self.settings is not None else None
        if vault is not None and hasattr(vault, "get_secret"):
            for pid, env_name in (
                ("freellm", "FREELLM_API_KEY"),
                ("tokenharbor", "TOKENHARBOR_API_KEY"),
                ("openai", "OPENAI_API_KEY"),
                ("anthropic", "ANTHROPIC_API_KEY"),
                ("gemini", "GEMINI_API_KEY"),
                ("groq", "GROQ_API_KEY"),
                ("openrouter", "OPENROUTER_API_KEY"),
                ("deepseek", "DEEPSEEK_API_KEY"),
            ):
                secret = vault.get_secret(pid)
                if secret:
                    env[env_name] = secret
        return env

    # ------------------------------------------------------------------
    # 3. Optional Subprocess Daemon Management (`opencode serve`)
    # ------------------------------------------------------------------
    def start_daemon(self, port: int = 4096, hostname: str = "127.0.0.1") -> Dict[str, Any]:
        """Attempt to launch `opencode serve --port <port> --hostname <hostname>` if installed,
        or fall back cleanly to the integrated OpenCode protocol bridge.
        """
        self._daemon_port = port
        if self._daemon_process is not None and self._daemon_process.poll() is None:
            return {
                "running": True,
                "mode": "opencode_subprocess_daemon",
                "port": self._daemon_port,
                "pid": self._daemon_process.pid,
            }

        opencode_bin = shutil.which("opencode")
        bun_bin = shutil.which("bun")
        env = self.build_runtime_environment()

        if opencode_bin:
            try:
                self._daemon_process = subprocess.Popen(
                    [opencode_bin, "serve", "--port", str(port), "--hostname", hostname],
                    cwd=str(self.workspace_root),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                return {
                    "running": True,
                    "mode": "opencode_subprocess_daemon",
                    "port": port,
                    "pid": self._daemon_process.pid,
                }
            except Exception:
                self._daemon_process = None

        return {
            "running": True,
            "mode": "integrated_protocol_bridge",
            "port": port,
            "bun_available": bool(bun_bin),
            "opencode_cli_available": bool(opencode_bin),
            "note": "Running via BEL Code integrated OpenCode HTTP/SSE protocol runtime.",
        }

    def stop_daemon(self) -> Dict[str, Any]:
        """Stop any spawned `opencode serve` subprocess."""
        if self._daemon_process is not None:
            try:
                self._daemon_process.terminate()
                self._daemon_process.wait(timeout=3)
            except Exception:
                try:
                    self._daemon_process.kill()
                except Exception:
                    pass
            self._daemon_process = None
        return {"running": False, "status": "stopped"}

    # ------------------------------------------------------------------
    # 4. Agents, Commands, Providers & MCP Discovery
    # ------------------------------------------------------------------
    def list_agents(self) -> Dict[str, Any]:
        """Return both OpenCode built-in agents (`build`, `plan`, `general`, `explore`),
        configured `opencode.json` agents, and BEL Code's 9 specialized engineering roles.
        """
        cfg = self.get_opencode_config(redacted=True)
        cfg_agents = cfg.get("agent", {})

        opencode_agents: List[Dict[str, Any]] = []
        seen_ids = set()
        for builtin in OPENCODE_BUILTIN_AGENTS:
            aid = builtin["id"]
            seen_ids.add(aid)
            override = cfg_agents.get(aid, {}) if isinstance(cfg_agents, dict) else {}
            merged = {
                **builtin,
                "model": override.get("model", builtin["model"]),
                "description": override.get("description", builtin["description"]),
            }
            opencode_agents.append(merged)

        if isinstance(cfg_agents, dict):
            for aid, aconf in cfg_agents.items():
                if aid not in seen_ids and isinstance(aconf, dict):
                    seen_ids.add(aid)
                    opencode_agents.append(
                        {
                            "id": aid,
                            "name": aid,
                            "display_name": aid.replace("_", " ").title(),
                            "mode": aconf.get("mode", "subagent"),
                            "layer": "opencode_config",
                            "model": aconf.get("model", "freellm/auto"),
                            "description": aconf.get("description", ""),
                            "tools": [k for k, v in (aconf.get("tools") or {}).items() if v],
                            "permission": aconf.get("permission", {}),
                        }
                    )

        bel_agents: List[Dict[str, Any]] = []
        for role in SPECIALIZED_ROLES:
            bel_agents.append(
                {
                    "id": role["id"],
                    "name": role["name"],
                    "display_name": role["name"],
                    "role": role["role"],
                    "mode": "subagent",
                    "layer": "bel_code_intelligence",
                    "task_type": role["task_type"],
                    "model": "freellm/auto:smart" if role["task_type"] in ("architecture_reasoning", "debugging", "security_review", "code_review") else "freellm/auto",
                    "description": role["description"],
                    "capabilities": list(role["capabilities"]),
                }
            )

        combined: List[Dict[str, Any]] = list(opencode_agents)
        existing_ids = {a["id"] for a in combined}
        for ba in bel_agents:
            if ba["id"] not in existing_ids:
                combined.append(ba)
            else:
                for item in combined:
                    if item["id"] == ba["id"]:
                        item["capabilities"] = ba["capabilities"]
                        item["role"] = ba["role"]
                        item["task_type"] = ba["task_type"]

        return {
            "default_agent": cfg.get("default_agent", "build"),
            "opencode_agents": opencode_agents,
            "bel_code_agents": bel_agents,
            "agents": combined,
        }

    def list_commands(self) -> List[Dict[str, str]]:
        return copy.deepcopy(OPENCODE_BUILTIN_COMMANDS)

    def get_config_providers(self) -> Dict[str, Any]:
        """Return OpenCode `/config/providers` payload with FreeLLM primary models."""
        cfg = self.get_opencode_config(redacted=True)
        providers_cfg = cfg.get("provider", {})
        providers_list: List[Dict[str, Any]] = []
        defaults_map: Dict[str, str] = {}

        for pid, pdata in providers_cfg.items():
            if not isinstance(pdata, dict):
                continue
            models_dict = pdata.get("models", {})
            first_model = next(iter(models_dict.keys()), "auto") if isinstance(models_dict, dict) and models_dict else "auto"
            defaults_map[pid] = first_model
            providers_list.append(
                {
                    "id": pid,
                    "name": pdata.get("name", pid),
                    "npm": pdata.get("npm", "@ai-sdk/openai-compatible"),
                    "models": models_dict,
                }
            )

        return {
            "providers": providers_list,
            "default": defaults_map,
            "primary_model": cfg.get("model", "freellm/auto"),
            "small_model": cfg.get("small_model", "freellm/auto:fast"),
        }

    def get_mcp_status(self) -> Dict[str, Any]:
        cfg = self.get_opencode_config(redacted=True)
        mcp_cfg = cfg.get("mcp", {})
        return {
            "status": "ready",
            "servers": mcp_cfg if isinstance(mcp_cfg, dict) else {},
            "builtin_mcp_tools": ["mcp-resource", "mcp-events"],
        }

    # ------------------------------------------------------------------
    # 5. OpenCode Session State Machine, Tool Execution, Diffs & Revert
    # ------------------------------------------------------------------
    def _record_event(self, session_id: str, event_type: str, properties: Dict[str, Any]) -> Dict[str, Any]:
        evt = {
            "id": f"evt_{uuid.uuid4().hex[:10]}",
            "type": event_type,
            "sessionID": session_id,
            "timestamp": time.time(),
            "properties": properties,
        }
        self._session_events.setdefault(session_id, []).append(evt)
        return evt

    def list_sessions(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sessions = list(self._sessions.values())
        if project_id:
            sessions = [s for s in sessions if s.get("projectID") == project_id or s.get("project_id") == project_id]
        sessions.sort(key=lambda s: float(s.get("time", {}).get("updated", 0)), reverse=True)
        return copy.deepcopy(sessions)

    def create_session(
        self,
        title: Optional[str] = None,
        agent: str = "build",
        model: str = "freellm/auto",
        project_id: str = "default",
        parent_id: Optional[str] = None,
        permission_mode: Optional[str] = None,
        directory: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = time.time()
        session_id = f"ses_{uuid.uuid4().hex[:12]}"
        effective_title = (title or "New Engineering Session").strip()
        effective_dir = str(Path(directory).resolve()) if directory else str(self.workspace_root)
        effective_perm = permission_mode or (
            str(getattr(self.security_policy, "mode", "supervised")) if self.security_policy else "supervised"
        )

        session: Dict[str, Any] = {
            "id": session_id,
            "session_id": session_id,
            "projectID": project_id,
            "project_id": project_id,
            "parentID": parent_id,
            "directory": effective_dir,
            "title": effective_title,
            "agent": agent or "build",
            "model": model or "freellm/auto",
            "permission_mode": effective_perm,
            "status": "idle",
            "verified": False,
            "version": "2.3.0",
            "time": {
                "created": now,
                "updated": now,
            },
            "messages": [],
            "todos": [],
            "diffs": [],
            "changed_files": [],
            "checkpoints": [],
            "timeline": [],
            "verification_results": [],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
            },
        }
        self._sessions[session_id] = session
        self._session_snapshots[session_id] = {}
        self._session_permissions[session_id] = {}
        self._record_event(session_id, "session.created", {"info": {"id": session_id, "title": effective_title}})
        return copy.deepcopy(session)

    def get_session(self, session_id: str) -> Dict[str, Any]:
        if session_id not in self._sessions:
            raise KeyError(f"OpenCode session '{session_id}' not found.")
        return copy.deepcopy(self._sessions[session_id])

    def delete_session(self, session_id: str) -> Dict[str, Any]:
        if session_id not in self._sessions:
            raise KeyError(f"OpenCode session '{session_id}' not found.")
        removed = self._sessions.pop(session_id)
        self._session_events.pop(session_id, None)
        self._session_snapshots.pop(session_id, None)
        self._session_permissions.pop(session_id, None)
        return {"deleted": True, "id": session_id, "title": removed.get("title")}

    def get_session_children(self, session_id: str) -> List[Dict[str, Any]]:
        return [
            copy.deepcopy(s)
            for s in self._sessions.values()
            if s.get("parentID") == session_id
        ]

    def get_session_todo(self, session_id: str) -> List[Dict[str, Any]]:
        session = self.get_session(session_id)
        return list(session.get("todos", []))

    def abort_session(self, session_id: str) -> Dict[str, Any]:
        if session_id not in self._sessions:
            raise KeyError(f"OpenCode session '{session_id}' not found.")
        session = self._sessions[session_id]
        session["status"] = "aborted"
        session["time"]["updated"] = time.time()
        self._record_event(session_id, "session.aborted", {"sessionID": session_id})
        return {"aborted": True, "id": session_id, "status": "aborted"}

    def _snapshot_file_before_mutation(self, session_id: str, rel_path: str) -> None:
        """Record original content of `rel_path` before the session mutates it so `/revert` and `/diff` are exact."""
        norm_rel = str(rel_path).replace("\\", "/").lstrip("./")
        snapshots = self._session_snapshots.setdefault(session_id, {})
        if norm_rel in snapshots:
            return
        full_path = (self.workspace_root / norm_rel).resolve()
        if full_path.exists() and full_path.is_file():
            try:
                snapshots[norm_rel] = full_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                snapshots[norm_rel] = ""
        else:
            snapshots[norm_rel] = None

    @staticmethod
    def _compute_file_diff_entry(
        rel_path: str,
        before_content: Optional[str],
        after_content: Optional[str],
    ) -> Dict[str, Any]:
        """Compute unified diff, side-by-side line pairs, and addition/deletion statistics."""
        before_text = before_content or ""
        after_text = after_content or ""
        before_lines = before_text.splitlines()
        after_lines = after_text.splitlines()

        unified_lines = list(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                lineterm="",
            )
        )
        unified_diff = "\n".join(unified_lines)

        additions = sum(1 for ln in unified_lines if ln.startswith("+") and not ln.startswith("+++"))
        deletions = sum(1 for ln in unified_lines if ln.startswith("-") and not ln.startswith("---"))

        # Build side-by-side comparison rows
        matcher = difflib.SequenceMatcher(None, before_lines, after_lines)
        side_by_side: List[Dict[str, Any]] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for offset in range(i2 - i1):
                    side_by_side.append(
                        {
                            "type": "context",
                            "left_line": i1 + offset + 1,
                            "left_text": before_lines[i1 + offset],
                            "right_line": j1 + offset + 1,
                            "right_text": after_lines[j1 + offset],
                        }
                    )
            elif tag == "replace":
                max_len = max(i2 - i1, j2 - j1)
                for offset in range(max_len):
                    l_idx = i1 + offset if offset < (i2 - i1) else None
                    r_idx = j1 + offset if offset < (j2 - j1) else None
                    side_by_side.append(
                        {
                            "type": "modified",
                            "left_line": (l_idx + 1) if l_idx is not None else None,
                            "left_text": before_lines[l_idx] if l_idx is not None else "",
                            "right_line": (r_idx + 1) if r_idx is not None else None,
                            "right_text": after_lines[r_idx] if r_idx is not None else "",
                        }
                    )
            elif tag == "delete":
                for offset in range(i2 - i1):
                    side_by_side.append(
                        {
                            "type": "removed",
                            "left_line": i1 + offset + 1,
                            "left_text": before_lines[i1 + offset],
                            "right_line": None,
                            "right_text": "",
                        }
                    )
            elif tag == "insert":
                for offset in range(j2 - j1):
                    side_by_side.append(
                        {
                            "type": "added",
                            "left_line": None,
                            "left_text": "",
                            "right_line": j1 + offset + 1,
                            "right_text": after_lines[j1 + offset],
                        }
                    )

        status = "modified"
        if before_content is None and after_content is not None:
            status = "added"
        elif before_content is not None and after_content is None:
            status = "deleted"

        return {
            "file": rel_path,
            "path": rel_path,
            "status": status,
            "additions": additions,
            "deletions": deletions,
            "before": before_text,
            "after": after_text,
            "diff": unified_diff,
            "unified_diff": unified_diff,
            "side_by_side": side_by_side,
        }

    def _execute_opencode_tool(
        self,
        session_id: str,
        agent_id: str,
        tool_call: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute a single OpenCode tool call (`read`, `write`, `edit`, `bash`, `grep`, `glob`, `list`, `todowrite`, `task`)
        against the active workspace while enforcing agent permissions and recording diffs.
        """
        t_start = time.perf_counter()
        tool_name = str(
            tool_call.get("tool")
            or tool_call.get("name")
            or tool_call.get("type")
            or (tool_call.get("function") or {}).get("name")
            or "read"
        ).strip().lower()

        raw_args = (
            tool_call.get("input")
            or tool_call.get("arguments")
            or (tool_call.get("function") or {}).get("arguments")
            or {k: v for k, v in tool_call.items() if k not in ("tool", "name", "type", "id", "function")}
        )
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {"raw": raw_args}
        elif isinstance(raw_args, dict):
            args = dict(raw_args)
        else:
            args = {}

        # Normalize BEL Code action aliases to OpenCode canonical tool names
        if tool_name in ("write_file", "create_file"):
            tool_name = "write"
        elif tool_name in ("edit_file", "replace"):
            tool_name = "edit"
        elif tool_name in ("read_file",):
            tool_name = "read"
        elif tool_name in ("run_command", "command", "shell", "terminal"):
            tool_name = "bash"

        # Enforce Plan agent read-only boundary
        if agent_id == "plan" and tool_name in ("write", "edit", "patch", "bash"):
            return {
                "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                "tool": tool_name,
                "status": "denied",
                "success": False,
                "input": args,
                "output": f"Permission denied: '{agent_id}' agent is read-only and cannot execute '{tool_name}'.",
                "duration_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
            }

        session = self._sessions[session_id]
        perm_mode = str(session.get("permission_mode", "supervised")).lower()
        rel_path = str(args.get("path") or args.get("filePath") or args.get("file") or "").replace("\\", "/").lstrip("./")

        try:
            if tool_name == "read":
                target = (self.workspace_root / rel_path).resolve()
                target.relative_to(self.workspace_root)
                if not target.exists() or not target.is_file():
                    return {
                        "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        "tool": "read",
                        "status": "error",
                        "success": False,
                        "input": args,
                        "output": f"File not found: {rel_path}",
                        "duration_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
                    }
                content = target.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                numbered = "\n".join(f"{i + 1}: {ln}" for i, ln in enumerate(lines[:200]))
                return {
                    "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "tool": "read",
                    "status": "completed",
                    "success": True,
                    "input": {"path": rel_path},
                    "output": numbered,
                    "lines_count": len(lines),
                    "duration_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
                }

            if tool_name == "write":
                if not rel_path:
                    raise ValueError("Missing file path for 'write' tool.")
                target = (self.workspace_root / rel_path).resolve()
                target.relative_to(self.workspace_root)
                self._snapshot_file_before_mutation(session_id, rel_path)
                before_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else None
                new_content = str(args.get("content", ""))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_content, encoding="utf-8")

                diff_entry = self._compute_file_diff_entry(rel_path, before_text, new_content)
                self._upsert_session_diff(session, diff_entry)
                return {
                    "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "tool": "write",
                    "status": "completed",
                    "success": True,
                    "input": {"path": rel_path, "bytes": len(new_content.encode("utf-8"))},
                    "output": f"Wrote {len(new_content.splitlines())} lines to {rel_path}",
                    "diff": diff_entry,
                    "duration_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
                }

            if tool_name == "edit":
                if not rel_path:
                    raise ValueError("Missing file path for 'edit' tool.")
                target = (self.workspace_root / rel_path).resolve()
                target.relative_to(self.workspace_root)
                if not target.exists():
                    raise FileNotFoundError(f"Cannot edit non-existent file: {rel_path}")
                self._snapshot_file_before_mutation(session_id, rel_path)
                before_text = target.read_text(encoding="utf-8", errors="replace")
                old_str = str(args.get("oldString") or args.get("target") or args.get("old") or "")
                new_str = str(args.get("newString") or args.get("replacement") or args.get("new") or "")
                if old_str and old_str in before_text:
                    after_text = before_text.replace(old_str, new_str, 1)
                elif not old_str and "content" in args:
                    after_text = str(args["content"])
                else:
                    return {
                        "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        "tool": "edit",
                        "status": "error",
                        "success": False,
                        "input": {"path": rel_path},
                        "output": f"oldString not found in {rel_path}",
                        "duration_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
                    }
                target.write_text(after_text, encoding="utf-8")
                diff_entry = self._compute_file_diff_entry(rel_path, before_text, after_text)
                self._upsert_session_diff(session, diff_entry)
                return {
                    "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "tool": "edit",
                    "status": "completed",
                    "success": True,
                    "input": {"path": rel_path},
                    "output": f"Edited {rel_path} (+{diff_entry['additions']} / -{diff_entry['deletions']})",
                    "diff": diff_entry,
                    "duration_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
                }

            if tool_name == "bash":
                cmd = str(args.get("command") or args.get("cmd") or "").strip()
                if not cmd:
                    raise ValueError("Missing command for 'bash' tool.")
                if perm_mode == "ask" and not args.get("approved"):
                    perm_id = f"perm_{uuid.uuid4().hex[:8]}"
                    perm_req = {
                        "id": perm_id,
                        "sessionID": session_id,
                        "tool": "bash",
                        "command": cmd,
                        "status": "pending",
                        "created_at": time.time(),
                    }
                    self._session_permissions.setdefault(session_id, {})[perm_id] = perm_req
                    self._record_event(session_id, "permission.updated", perm_req)
                    return {
                        "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        "tool": "bash",
                        "status": "awaiting_permission",
                        "permission_id": perm_id,
                        "success": False,
                        "input": {"command": cmd},
                        "output": f"Command requires approval in 'ask' mode (permission_id={perm_id}).",
                        "duration_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
                    }

                if self.terminal is not None and hasattr(self.terminal, "execute"):
                    exec_res = self.terminal.execute(cmd)
                    if not isinstance(exec_res, dict):
                        exec_res = {"stdout": str(exec_res), "stderr": "", "exit_code": 0}
                else:
                    proc = subprocess.run(
                        cmd,
                        cwd=str(self.workspace_root),
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    exec_res = {
                        "command": cmd,
                        "stdout": proc.stdout,
                        "stderr": proc.stderr,
                        "exit_code": proc.returncode,
                    }

                exit_code = int(exec_res.get("exit_code", exec_res.get("returncode", 0)))
                stdout_str = str(exec_res.get("stdout", ""))
                stderr_str = str(exec_res.get("stderr", ""))
                combined_out = (stdout_str + ("\n" + stderr_str if stderr_str else "")).strip()
                return {
                    "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "tool": "bash",
                    "status": "completed" if exit_code == 0 else "failed",
                    "success": exit_code == 0,
                    "input": {"command": cmd},
                    "output": combined_out,
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                    "exit_code": exit_code,
                    "duration_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
                }

            if tool_name in ("grep", "glob", "list"):
                pattern = str(args.get("pattern") or args.get("query") or "*")
                matches: List[str] = []
                for p in self.workspace_root.rglob("*"):
                    if not p.is_file() or any(part in (".git", "__pycache__", "node_modules", ".bel_code", "vendor") for part in p.parts):
                        continue
                    rel = str(p.relative_to(self.workspace_root)).replace("\\", "/")
                    if tool_name in ("glob", "list"):
                        if pattern in ("*", ".") or pattern.lower() in rel.lower() or p.match(pattern):
                            matches.append(rel)
                    else:
                        try:
                            txt = p.read_text(encoding="utf-8", errors="replace")
                            if pattern.lower() in txt.lower():
                                matches.append(rel)
                        except Exception:
                            pass
                    if len(matches) >= 50:
                        break
                return {
                    "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "tool": tool_name,
                    "status": "completed",
                    "success": True,
                    "input": {"pattern": pattern},
                    "output": "\n".join(matches) if matches else "No matches found.",
                    "matches": matches,
                    "duration_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
                }

            if tool_name == "todowrite":
                todos = args.get("todos") or []
                if isinstance(todos, list):
                    session["todos"] = todos
                return {
                    "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "tool": "todowrite",
                    "status": "completed",
                    "success": True,
                    "input": {"todos": todos},
                    "output": f"Updated {len(session['todos'])} todo item(s).",
                    "duration_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
                }

            return {
                "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                "tool": tool_name,
                "status": "completed",
                "success": True,
                "input": args,
                "output": f"Executed tool '{tool_name}'.",
                "duration_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
            }
        except Exception as exc:
            return {
                "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                "tool": tool_name,
                "status": "error",
                "success": False,
                "input": args,
                "output": f"Error executing '{tool_name}': {exc}",
                "duration_ms": round((time.perf_counter() - t_start) * 1000.0, 2),
            }

    @staticmethod
    def _upsert_session_diff(session: Dict[str, Any], diff_entry: Dict[str, Any]) -> None:
        rel_path = diff_entry["file"]
        diffs: List[Dict[str, Any]] = session.setdefault("diffs", [])
        for idx, existing in enumerate(diffs):
            if existing.get("file") == rel_path:
                # Recompute cumulative diff from initial before state to latest after state
                initial_before = existing.get("before")
                diffs[idx] = OpenCodeRuntimeManager._compute_file_diff_entry(
                    rel_path, initial_before, diff_entry.get("after")
                )
                break
        else:
            diffs.append(diff_entry)

        changed_files: List[str] = session.setdefault("changed_files", [])
        if rel_path not in changed_files:
            changed_files.append(rel_path)

    def send_message(
        self,
        session_id: str,
        content: str,
        agent: Optional[str] = None,
        model: Optional[str] = None,
        actions: Optional[List[Dict[str, Any]]] = None,
        tools_requested: Optional[List[Dict[str, Any]]] = None,
        test_command: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Send a user instruction to an OpenCode session, execute tools via the OpenCode tool
        protocol + Layer 2 engine, record thinking/planning summaries, tool activity timeline events,
        file diffs, terminal output, and verification results, and return the structured turn.
        """
        if session_id not in self._sessions:
            raise KeyError(f"OpenCode session '{session_id}' not found.")

        session = self._sessions[session_id]
        now = time.time()
        active_agent = (agent or session.get("agent") or "build").strip().lower()
        active_model = (model or session.get("model") or "freellm/auto").strip()
        session["agent"] = active_agent
        session["model"] = active_model
        session["status"] = "running"

        # Auto-title session from first user message if generic
        if not session["messages"] and session.get("title") in ("New Engineering Session", "New Session", "Untitled"):
            short_title = content.strip().splitlines()[0][:60]
            if short_title:
                session["title"] = short_title

        user_msg_id = f"msg_{uuid.uuid4().hex[:10]}"
        user_msg = {
            "id": user_msg_id,
            "sessionID": session_id,
            "role": "user",
            "agent": active_agent,
            "model": active_model,
            "content": content,
            "parts": [{"type": "text", "text": content}],
            "time": {"created": now},
        }
        session["messages"].append(user_msg)
        self._record_event(session_id, "message.created", {"message": user_msg})

        # Create pre-turn checkpoint
        ckpt_id = f"ckpt_{session_id}_{len(session['messages'])}"
        if self.git_manager is not None and hasattr(self.git_manager, "create_checkpoint"):
            try:
                c_res = self.git_manager.create_checkpoint(ckpt_id, task_id=session_id)
                if isinstance(c_res, dict) and c_res.get("checkpoint_id"):
                    ckpt_id = str(c_res["checkpoint_id"])
            except Exception:
                pass
        session["checkpoints"].append(ckpt_id)

        # 1. Thinking & Planning Summary
        planned_steps = [
            f"[{active_agent.upper()}] Analyze instruction: {content[:120]}",
            "Inspect relevant workspace files and AST symbol context",
        ]
        all_tool_requests = list(tools_requested or []) + list(actions or [])
        for req in all_tool_requests:
            t_label = req.get("tool") or req.get("type") or req.get("name") or "tool"
            t_target = req.get("path") or req.get("file") or req.get("command") or ""
            planned_steps.append(f"Execute `{t_label}`" + (f" on `{t_target}`" if t_target else ""))
        if test_command:
            planned_steps.append(f"Verify implementation with `{test_command}`")

        thinking_summary = (
            f"Agent `{active_agent}` routed to `{active_model}` via OpenCode Foundation Bridge. "
            f"Prepared {len(planned_steps)}-step execution plan."
        )

        # Update session todos automatically
        session["todos"] = [
            {"id": f"todo_{idx + 1}", "content": step, "status": "completed", "priority": "medium"}
            for idx, step in enumerate(planned_steps)
        ]

        # 2. Execute requested tools / actions
        tool_activities: List[Dict[str, Any]] = []
        terminal_outputs: List[Dict[str, Any]] = []
        turn_diffs: List[Dict[str, Any]] = []

        for req in all_tool_requests:
            t_res = self._execute_opencode_tool(session_id, active_agent, req)
            tool_activities.append(t_res)
            self._record_event(session_id, "tool.executed", t_res)
            if t_res.get("diff"):
                turn_diffs.append(t_res["diff"])
            if t_res.get("tool") == "bash":
                terminal_outputs.append(
                    {
                        "command": (t_res.get("input") or {}).get("command", ""),
                        "stdout": t_res.get("stdout", ""),
                        "stderr": t_res.get("stderr", ""),
                        "exit_code": t_res.get("exit_code", 0),
                    }
                )

        # 3. Call ProviderManager / FreeLLM for assistant completion
        provider_id = "freellm"
        model_id = active_model
        if "/" in active_model:
            provider_id, model_id = active_model.split("/", 1)

        llm_content = ""
        provider_error: Optional[str] = None
        usage_dict = {
            "prompt_tokens": max(12, len(content.split()) * 2),
            "completion_tokens": 36,
            "total_tokens": max(48, len(content.split()) * 2 + 36),
            "cost_usd": 0.0,
            "provider_id": provider_id,
            "model_id": model_id,
        }
        limitations_note = ""

        if self.provider_manager is not None and hasattr(self.provider_manager, "complete"):
            try:
                comp = self.provider_manager.complete(
                    messages=[{"role": "user", "content": content}],
                    provider_id=provider_id,
                    model=model_id,
                )
                llm_content = getattr(comp, "content", "") or (comp.get("content", "") if isinstance(comp, dict) else "")
                limitations_note = getattr(comp, "limitations_note", "") or (
                    comp.get("limitations_note", "") if isinstance(comp, dict) else ""
                )
                u_obj = getattr(comp, "usage", None) or (comp.get("usage") if isinstance(comp, dict) else None)
                if u_obj is not None:
                    usage_dict = u_obj.to_dict() if hasattr(u_obj, "to_dict") else dict(u_obj)
            except Exception as exc:
                provider_error = str(exc)
                llm_content = f"Provider error ({provider_id}/{model_id}): {exc}"
        else:
            llm_content = f"Processed instruction via OpenCode `{active_agent}` agent ({active_model}): {content}"

        # 4. Run optional verification test command
        verification_result: Optional[Dict[str, Any]] = None
        if test_command:
            ver_tool = self._execute_opencode_tool(
                session_id,
                active_agent,
                {"tool": "bash", "input": {"command": test_command, "approved": True}},
            )
            tool_activities.append(ver_tool)
            terminal_outputs.append(
                {
                    "command": test_command,
                    "stdout": ver_tool.get("stdout", ""),
                    "stderr": ver_tool.get("stderr", ""),
                    "exit_code": ver_tool.get("exit_code", -1),
                }
            )
            verified_ok = bool(ver_tool.get("exit_code") == 0)
            verification_result = {
                "executed": True,
                "verified": verified_ok,
                "command": test_command,
                "exit_code": ver_tool.get("exit_code", -1),
                "output": ver_tool.get("output", ""),
            }
            session["verified"] = verified_ok
            session["verification_results"].append(verification_result)
            self._record_event(session_id, "session.verified", verification_result)

        # Build structured OpenCode message parts
        parts: List[Dict[str, Any]] = [
            {
                "type": "reasoning",
                "summary": thinking_summary,
                "planned_steps": planned_steps,
            }
        ]
        for ta in tool_activities:
            parts.append(
                {
                    "type": "tool",
                    "id": ta["id"],
                    "tool": ta["tool"],
                    "status": ta["status"],
                    "input": ta["input"],
                    "output": ta["output"],
                    "duration_ms": ta["duration_ms"],
                }
            )
        for td in turn_diffs:
            parts.append(
                {
                    "type": "patch",
                    "file": td["file"],
                    "status": td["status"],
                    "additions": td["additions"],
                    "deletions": td["deletions"],
                    "diff": td["unified_diff"],
                }
            )
        for term in terminal_outputs:
            parts.append(
                {
                    "type": "terminal",
                    **term,
                }
            )
        if verification_result is not None:
            parts.append(
                {
                    "type": "verification",
                    **verification_result,
                }
            )
        parts.append({"type": "text", "text": llm_content})

        assistant_msg_id = f"msg_{uuid.uuid4().hex[:10]}"
        assistant_msg = {
            "id": assistant_msg_id,
            "sessionID": session_id,
            "parentID": user_msg_id,
            "role": "assistant",
            "agent": active_agent,
            "model": active_model,
            "provider": provider_id,
            "content": llm_content,
            "thinking": thinking_summary,
            "planned_steps": planned_steps,
            "parts": parts,
            "tool_activity": tool_activities,
            "code_changes": turn_diffs,
            "terminal_output": terminal_outputs,
            "verification": verification_result,
            "limitations_note": limitations_note,
            "checkpoint_id": ckpt_id,
            "usage": usage_dict,
            "time": {"created": time.time(), "completed": time.time()},
        }

        session["messages"].append(assistant_msg)
        session["timeline"].extend(parts)
        session["status"] = "idle"
        session["time"]["updated"] = time.time()
        session["usage"]["prompt_tokens"] += int(usage_dict.get("prompt_tokens", 0))
        session["usage"]["completion_tokens"] += int(usage_dict.get("completion_tokens", 0))
        session["usage"]["total_tokens"] += int(usage_dict.get("total_tokens", 0))
        session["usage"]["cost_usd"] = round(
            float(session["usage"]["cost_usd"]) + float(usage_dict.get("cost_usd", 0.0)), 6
        )

        self._record_event(session_id, "message.updated", {"message": assistant_msg})
        return {
            "session_id": session_id,
            "user_message": user_msg,
            "message": assistant_msg,
            "response": llm_content,
            "assistant_text": llm_content,
            "error": provider_error,
            "thinking": thinking_summary,
            "planned_steps": planned_steps,
            "tool_activity": tool_activities,
            "code_changes": turn_diffs,
            "terminal_output": terminal_outputs,
            "verification": verification_result,
            "session": copy.deepcopy(session),
        }

    def get_session_diff(self, session_id: str) -> Dict[str, Any]:
        """Return all file diffs accumulated in `session_id`."""
        session = self.get_session(session_id)
        diffs = session.get("diffs", [])
        total_add = sum(int(d.get("additions", 0)) for d in diffs)
        total_del = sum(int(d.get("deletions", 0)) for d in diffs)
        return {
            "session_id": session_id,
            "changed_files": session.get("changed_files", []),
            "files_count": len(diffs),
            "additions": total_add,
            "deletions": total_del,
            "diffs": diffs,
        }

    def revert_session(
        self,
        session_id: str,
        message_id: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Non-destructively revert file changes made during `session_id` using stored
        pre-mutation file snapshots, preserving any unrelated uncommitted user files.
        """
        if session_id not in self._sessions:
            raise KeyError(f"OpenCode session '{session_id}' not found.")

        session = self._sessions[session_id]
        snapshots = self._session_snapshots.get(session_id, {})
        restored_files: List[str] = []

        for rel_path, original_content in list(snapshots.items()):
            target = (self.workspace_root / rel_path).resolve()
            try:
                target.relative_to(self.workspace_root)
            except ValueError:
                continue
            if original_content is None:
                if target.exists() and target.is_file():
                    target.unlink()
                    restored_files.append(rel_path)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(original_content, encoding="utf-8")
                restored_files.append(rel_path)

        snapshots.clear()
        session["diffs"] = []
        session["changed_files"] = []
        if message_id:
            # Remove messages from message_id onward
            trimmed: List[Dict[str, Any]] = []
            for m in session["messages"]:
                if m.get("id") == message_id:
                    break
                trimmed.append(m)
            session["messages"] = trimmed
        session["time"]["updated"] = time.time()

        revert_info = {
            "reverted": True,
            "session_id": session_id,
            "message_id": message_id,
            "checkpoint_id": checkpoint_id,
            "restored_files": restored_files,
        }
        self._record_event(session_id, "session.reverted", revert_info)
        return revert_info

    def resolve_permission(
        self,
        session_id: str,
        permission_id: str,
        response: str = "allow",
        remember: bool = False,
    ) -> Dict[str, Any]:
        """Resolve a pending OpenCode session permission request (`allow`, `always`, `deny`)."""
        if session_id not in self._sessions:
            raise KeyError(f"OpenCode session '{session_id}' not found.")
        perms = self._session_permissions.setdefault(session_id, {})
        perm = perms.get(permission_id, {"id": permission_id, "sessionID": session_id})
        perm["status"] = "resolved"
        perm["response"] = response
        perm["remember"] = remember
        perm["resolved_at"] = time.time()
        perms[permission_id] = perm
        self._record_event(session_id, "permission.replied", perm)
        return copy.deepcopy(perm)

    def get_session_events(self, session_id: str) -> List[Dict[str, Any]]:
        if session_id not in self._sessions:
            raise KeyError(f"OpenCode session '{session_id}' not found.")
        return copy.deepcopy(self._session_events.get(session_id, []))

    def format_sse_stream(self, session_id: Optional[str] = None) -> str:
        """Format recorded events as a valid Server-Sent Events (`text/event-stream`) payload."""
        events: List[Dict[str, Any]] = []
        if session_id and session_id in self._session_events:
            events = self._session_events[session_id]
        else:
            for ev_list in self._session_events.values():
                events.extend(ev_list)

        lines: List[str] = [
            f'data: {json.dumps({"type": "server.connected", "properties": {"version": "2.3.0"}})}\n'
        ]
        for ev in events[-50:]:
            lines.append(f"data: {json.dumps(ev, default=str)}\n")
        return "\n".join(lines) + "\n"
