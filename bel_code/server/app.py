"""
BEL Code v2.1.0 — Standalone FastAPI Web Server, OpenCode Gateway & REST/SSE API.

Serves the redesigned 4-Zone BEL Code Software Engineering IDE Workspace at `/` and exposes
comprehensive REST & SSE endpoints under `/api/*` and standard OpenCode paths:
- Layer 1 — OpenCode Foundation Bridge (`/api/opencode/*`, `/global/health`, `/session/*`, `/config/*`, `/agent`, `/command`, `/file/*`, `/find/*`, `/mcp`, `/event`)
- Workspace File Explorer, Code Editor, Diff & Problems (`/api/files/tree`, `/api/files/read`, `/api/files/write`, `/api/files/changed`, `/api/problems`)
- Project Intelligence & Architecture Mapping (`/api/projects/*`)
- AI Provider & Model Routing Management with OpenCode Config Sync (`/api/providers/*`)
- Security Modes, Human Approval Queue, Prompt Injection & Audit Log (`/api/security/*`)
- Git Operations, Checkpoints, Safe Rollback, PR Review & Change Register (`/api/git/*`)
- Autonomous 7-Stage Engineering Tasks & Multi-Agent Collaboration (`/api/tasks/*`)
- Controlled Terminal, Browser/Accessibility QA & Test Verification (`/api/terminal/*`, `/api/qa/*`)
- Persistent Project-Isolated Engineering Memory & Session Recovery (`/api/memory/*`)
- Advanced Capabilities (Skills, Migration, Security Scan, OpenAPI, DB, Profiling, CI/CD, Image-to-UI, Self-Eval) (`/api/advanced/*`)
- 10 End-to-End Acceptance Benchmarks (`/api/benchmarks/*`)
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import inspect
import json
import os
import tempfile
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from bel_code import __product_name__, __version__
from bel_code.git_ops.manager import GitManager as _GitManager
from bel_code.intelligence.indexer import DEFAULT_IGNORED_DIRS as _INDEXER_IGNORED_DIRS

# Ensure workspace scanners and snapshotters skip the upstream vendor/ monorepo clone
_INDEXER_IGNORED_DIRS.add("vendor")
_GitManager.IGNORED_SNAPSHOT_DIRS.add("vendor")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

LANGUAGE_BY_EXT: Dict[str, str] = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript (JSX)",
    ".ts": "TypeScript",
    ".tsx": "TypeScript (TSX)",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".json": "JSON",
    ".md": "Markdown",
    ".toml": "TOML",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".sql": "SQL",
    ".sh": "Shell",
    ".ps1": "PowerShell",
    ".go": "Go",
    ".rs": "Rust",
}

TREE_IGNORED_DIRS: Set[str] = {
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

ACCEPTANCE_BENCHMARK_CATALOG = [
    {
        "id": "build_app_from_scratch",
        "number": 1,
        "title": "Build an Application from Scratch",
        "description": "Scaffold a runnable Python application + test suite from a natural-language specification, execute pytest, and verify exit code 0.",
    },
    {
        "id": "understand_unfamiliar_repo",
        "number": 2,
        "title": "Understand an Unfamiliar Repository",
        "description": "Scan a multi-file repository, build AST symbol index, generate Mermaid architecture map, and cite exact file/line references.",
    },
    {
        "id": "fix_difficult_bug",
        "number": 3,
        "title": "Fix a Difficult Bug",
        "description": "Reproduce a bug with an isolated failing test, trace root cause, apply minimal repair patch, and verify regression suite passes.",
    },
    {
        "id": "implement_complex_feature",
        "number": 4,
        "title": "Implement a Complex Feature",
        "description": "Design and implement a multi-module feature across codebase layers with full automated unit and integration test verification.",
    },
    {
        "id": "migrate_legacy_project",
        "number": 5,
        "title": "Migrate a Legacy Project",
        "description": "Upgrade legacy Python 2 / deprecated syntax to modern Python 3 idioms and pass AST compatibility & regression tests.",
    },
    {
        "id": "review_pull_request",
        "number": 6,
        "title": "Review a Pull Request",
        "description": "Inspect Git diffs to identify security vulnerabilities, logic bugs, and maintainability issues with exact file and line citations.",
    },
    {
        "id": "run_long_running_task",
        "number": 7,
        "title": "Run a Long-Running Multi-Step Task",
        "description": "Execute a staged task with persistent checkpoints, simulate session interruption, and resume cleanly without repeating verified stages.",
    },
    {
        "id": "handle_provider_failure",
        "number": 8,
        "title": "Handle Provider Failure & Rate Limits",
        "description": "Simulate primary provider HTTP 429 / connection failure and verify automatic failover to secondary provider (Token Harbor / FreeLLM) within budget.",
    },
    {
        "id": "recover_from_failed_change",
        "number": 9,
        "title": "Recover from a Failed Change",
        "description": "Create a Git/file checkpoint, apply a broken edit while an uncommitted user file exists, roll back cleanly, and preserve user uncommitted work.",
    },
    {
        "id": "deploy_application",
        "number": 10,
        "title": "Deploy an Application Safely",
        "description": "Gate production deployment behind explicit human approval, execute deployment verification once approved, and generate rollback instructions.",
    },
]


def _to_serializable(obj: Any) -> Any:
    """Convert dataclasses, Enums, Paths, and custom objects into JSON-safe structures."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Path):
        return str(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_serializable(v) for k, v in asdict(obj).items()}
    if hasattr(obj, "value") and isinstance(getattr(obj, "value"), (str, int, float)):
        return obj.value
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        return _to_serializable(obj.model_dump())
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return _to_serializable(obj.to_dict())
    if isinstance(obj, dict):
        return {str(k): _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_serializable(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {
            k: _to_serializable(v)
            for k, v in vars(obj).items()
            if not k.startswith("_")
        }
    return str(obj)


def _compute_project_id(workspace_root: Path) -> str:
    resolved = str(workspace_root.resolve()).lower()
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:10]
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in workspace_root.name) or "workspace"
    return f"{safe_name}_{digest}"


def _build_side_by_side_diff(before_text: str, after_text: str) -> List[Dict[str, Any]]:
    before_lines = (before_text or "").splitlines()
    after_lines = (after_text or "").splitlines()
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines)
    rows: List[Dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append(
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
                rows.append(
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
                rows.append(
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
                rows.append(
                    {
                        "type": "added",
                        "left_line": None,
                        "left_text": "",
                        "right_line": j1 + offset + 1,
                        "right_text": after_lines[j1 + offset],
                    }
                )
    return rows


class ServerState:
    """Manages active workspace and lazy-initialized BEL Code Layer 1 & Layer 2 subsystems."""

    def __init__(self, initial_workspace: Optional[Path] = None, config_dir: Optional[Path] = None) -> None:
        env_ws = os.environ.get("BEL_CODE_WORKSPACE")
        if initial_workspace:
            self.workspace_root = Path(initial_workspace).resolve()
        elif env_ws:
            self.workspace_root = Path(env_ws).resolve()
        else:
            self.workspace_root = Path.cwd().resolve()

        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.project_id = _compute_project_id(self.workspace_root)

        env_cfg = os.environ.get("BEL_CODE_CONFIG_DIR")
        if config_dir:
            self.config_dir = Path(config_dir).resolve()
        elif env_cfg:
            self.config_dir = Path(env_cfg).resolve()
        else:
            self.config_dir = Path(tempfile.gettempdir()) / "bel_code_runtime"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.security_mode: str = os.environ.get("BEL_CODE_SECURITY_MODE", "supervised")
        self.tasks_store: Dict[str, Dict[str, Any]] = {}
        self.recent_file_diffs: Dict[str, Dict[str, Any]] = {}
        self.recent_workspaces: List[Dict[str, str]] = [
            {
                "name": self.workspace_root.name,
                "path": str(self.workspace_root),
                "project_id": self.project_id,
            }
        ]

        self._settings = None
        self._provider_manager = None
        self._security_policy = None
        self._fs_tools = None
        self._terminal = None
        self._browser_qa = None
        self._git_manager = None
        self._intelligence = None
        self._memory = None
        self._advanced = None
        self._debugger = None
        self._multi_agent = None
        self._workflow = None
        self._opencode_runtime = None
        self._opencode_client = None

    def set_workspace(self, new_workspace: Path, project_id: Optional[str] = None) -> Dict[str, Any]:
        resolved = Path(new_workspace).resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        self.workspace_root = resolved
        self.project_id = project_id or _compute_project_id(resolved)
        self._record_recent_workspace(self.workspace_root, self.project_id)
        # Reset workspace-scoped instances
        self._security_policy = None
        self._fs_tools = None
        self._terminal = None
        self._browser_qa = None
        self._git_manager = None
        self._intelligence = None
        self._advanced = None
        self._debugger = None
        self._multi_agent = None
        self._workflow = None
        if self._opencode_runtime is not None:
            self._opencode_runtime.workspace_root = self.workspace_root
            self._opencode_runtime.fs_tools = self.fs_tools
            self._opencode_runtime.terminal = self.terminal
            self._opencode_runtime.git_manager = self.git_manager
            self._opencode_runtime.intelligence = self.intelligence
            self._opencode_runtime.security_policy = self.security_policy
        return {
            "active_workspace": str(self.workspace_root),
            "project_id": self.project_id,
            "name": self.workspace_root.name,
            "exists": self.workspace_root.exists(),
        }

    def _record_recent_workspace(self, ws_path: Path, project_id: str) -> None:
        entry = {"name": ws_path.name, "path": str(ws_path), "project_id": project_id}
        self.recent_workspaces = [entry] + [
            w for w in self.recent_workspaces if w.get("path") != str(ws_path)
        ]
        self.recent_workspaces = self.recent_workspaces[:8]

    @property
    def settings(self) -> Any:
        if self._settings is None:
            from bel_code.config.settings import SettingsManager

            sig = inspect.signature(SettingsManager.__init__)
            if "config_dir" in sig.parameters:
                self._settings = SettingsManager(config_dir=self.config_dir)
            else:
                self._settings = SettingsManager()
        return self._settings

    @property
    def provider_manager(self) -> Any:
        if self._provider_manager is None:
            from bel_code.providers.router import ProviderManager

            sig = inspect.signature(ProviderManager.__init__)
            if "settings" in sig.parameters:
                self._provider_manager = ProviderManager(settings=self.settings)
            else:
                self._provider_manager = ProviderManager()
        return self._provider_manager

    @property
    def security_policy(self) -> Any:
        if self._security_policy is None:
            from bel_code.security.policy import SecurityPolicy

            audit_path = self.config_dir / "audit.jsonl"
            sig = inspect.signature(SecurityPolicy.__init__)
            kwargs: Dict[str, Any] = {"workspace_root": self.workspace_root}
            if "mode" in sig.parameters:
                kwargs["mode"] = self.security_mode
            if "audit_log_path" in sig.parameters:
                kwargs["audit_log_path"] = audit_path
            self._security_policy = SecurityPolicy(**kwargs)
        return self._security_policy

    @property
    def fs_tools(self) -> Any:
        if self._fs_tools is None:
            from bel_code.tools.filesystem import FileSystemTools

            self._fs_tools = FileSystemTools(
                workspace_root=self.workspace_root,
                security=self.security_policy,
            )
        return self._fs_tools

    @property
    def terminal(self) -> Any:
        if self._terminal is None:
            from bel_code.tools.terminal import TerminalExecutor

            self._terminal = TerminalExecutor(
                workspace_root=self.workspace_root,
                security=self.security_policy,
            )
        return self._terminal

    @property
    def browser_qa(self) -> Any:
        if self._browser_qa is None:
            from bel_code.tools.browser_qa import BrowserQATester

            self._browser_qa = BrowserQATester(
                workspace_root=self.workspace_root,
                security=self.security_policy,
            )
        return self._browser_qa

    @property
    def git_manager(self) -> Any:
        if self._git_manager is None:
            from bel_code.git_ops.manager import GitManager

            self._git_manager = GitManager(
                workspace_root=self.workspace_root,
                security=self.security_policy,
            )
        return self._git_manager

    @property
    def intelligence(self) -> Any:
        if self._intelligence is None:
            from bel_code.intelligence.indexer import ProjectIntelligence

            self._intelligence = ProjectIntelligence(workspace_root=self.workspace_root)
        return self._intelligence

    @property
    def memory(self) -> Any:
        if self._memory is None:
            from bel_code.memory.store import EngineeringMemory

            mem_dir = self.config_dir / "memory"
            mem_dir.mkdir(parents=True, exist_ok=True)
            sig = inspect.signature(EngineeringMemory.__init__)
            if "storage_dir" in sig.parameters:
                self._memory = EngineeringMemory(storage_dir=mem_dir)
            else:
                self._memory = EngineeringMemory()
        return self._memory

    @property
    def advanced(self) -> Any:
        if self._advanced is None:
            from bel_code.advanced.capabilities import SkillsAndCapabilities

            self._advanced = SkillsAndCapabilities(workspace_root=self.workspace_root)
        return self._advanced

    @property
    def debugger(self) -> Any:
        if self._debugger is None:
            from bel_code.engine.debugger import TestAndDebugEngine

            self._debugger = TestAndDebugEngine(
                workspace_root=self.workspace_root,
                terminal=self.terminal,
                fs=self.fs_tools,
                git=self.git_manager,
                provider_manager=self.provider_manager,
            )
        return self._debugger

    @property
    def multi_agent(self) -> Any:
        if self._multi_agent is None:
            from bel_code.engine.multi_agent import MultiAgentCoordinator

            self._multi_agent = MultiAgentCoordinator(
                workspace_root=self.workspace_root,
                provider_manager=self.provider_manager,
                git=self.git_manager,
            )
        return self._multi_agent

    @property
    def workflow(self) -> Any:
        if self._workflow is None:
            from bel_code.engine.workflow import AutonomousWorkflowEngine

            self._workflow = AutonomousWorkflowEngine(
                workspace_root=self.workspace_root,
                provider_manager=self.provider_manager,
                settings=self.settings,
                memory=self.memory,
                security=self.security_policy,
            )
        return self._workflow

    @property
    def opencode_runtime(self) -> Any:
        if self._opencode_runtime is None:
            from bel_code.opencode_bridge.runtime import OpenCodeRuntimeManager

            self._opencode_runtime = OpenCodeRuntimeManager(
                workspace_root=self.workspace_root,
                provider_manager=self.provider_manager,
                settings=self.settings,
                fs_tools=self.fs_tools,
                terminal=self.terminal,
                git_manager=self.git_manager,
                intelligence=self.intelligence,
                security_policy=self.security_policy,
                debugger=self.debugger,
                multi_agent=self.multi_agent,
                workflow=self.workflow,
            )
        return self._opencode_runtime

    @property
    def opencode_client(self) -> Any:
        if self._opencode_client is None:
            from bel_code.opencode_bridge.client import OpenCodeClient

            self._opencode_client = OpenCodeClient(
                runtime=self.opencode_runtime,
                directory=self.workspace_root,
            )
        return self._opencode_client


def create_app(initial_workspace: Optional[Path] = None, config_dir: Optional[Path] = None) -> FastAPI:
    state = ServerState(initial_workspace=initial_workspace, config_dir=config_dir)

    app = FastAPI(
        title="BEL Code — OpenCode-Powered Autonomous AI Software Engineering Platform",
        description=(
            "Standalone 3-Layer software engineering IDE and autonomous execution platform. "
            "Layer 1: OpenCode Foundation | Layer 2: BEL Code Intelligence | Layer 3: 4-Zone IDE. "
            "Primary Intelligence Provider: FreeLLM API + 12 enterprise providers."
        ),
        version=__version__,
    )
    app.state.bel_code = state

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if FRONTEND_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    # ------------------------------------------------------------------
    # Frontend Static Delivery
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def serve_index() -> Any:
        index_path = FRONTEND_DIR / "index.html"
        if index_path.exists():
            return HTMLResponse(content=index_path.read_text(encoding="utf-8"), status_code=200)
        return HTMLResponse(
            content=f"<html><body><h1>{__product_name__} v{__version__}</h1></body></html>",
            status_code=200,
        )

    @app.get("/styles.css")
    async def serve_styles() -> Any:
        css_path = FRONTEND_DIR / "styles.css"
        if css_path.exists():
            return FileResponse(str(css_path), media_type="text/css")
        raise HTTPException(status_code=404, detail="styles.css not found")

    @app.get("/app.js")
    async def serve_app_js() -> Any:
        js_path = FRONTEND_DIR / "app.js"
        if js_path.exists():
            return FileResponse(str(js_path), media_type="application/javascript")
        raise HTTPException(status_code=404, detail="app.js not found")

    # ------------------------------------------------------------------
    # 1. Health & Platform Status
    # ------------------------------------------------------------------
    @app.get("/api/health")
    async def api_health() -> Dict[str, Any]:
        providers_list: List[Any] = []
        try:
            providers_list = _to_serializable(state.provider_manager.list_providers())
        except Exception:
            providers_list = []

        pending_approvals: List[Any] = []
        try:
            pending_approvals = _to_serializable(state.security_policy.list_pending_approvals())
        except Exception:
            pending_approvals = []

        return {
            "status": "ok",
            "healthy": True,
            "product": __product_name__,
            "version": __version__,
            "standalone": True,
            "architecture": "3-layer-opencode",
            "active_workspace": str(state.workspace_root),
            "project_id": state.project_id,
            "security_mode": str(getattr(state.security_policy, "mode", state.security_mode)),
            "primary_provider": "freellm",
            "providers_count": len(providers_list) if isinstance(providers_list, list) else 13,
            "provider_status": {
                "primary": "freellm",
                "providers": providers_list,
            },
            "pending_approvals_count": len(pending_approvals) if isinstance(pending_approvals, list) else 0,
            "timestamp": time.time(),
        }

    # ------------------------------------------------------------------
    # 1A. Layer 1: OpenCode Foundation Routes (`/api/opencode/*` & SDK Routes)
    # ------------------------------------------------------------------
    @app.get("/api/opencode/status")
    @app.get("/global/health")
    async def api_opencode_status() -> Dict[str, Any]:
        rt = state.opencode_runtime
        status_data = _to_serializable(rt.get_status())
        if not isinstance(status_data, dict):
            status_data = {"status": "ready"}
        status_data.setdefault("healthy", True)
        status_data.setdefault("repo_path", status_data.get("vendor_path", str(PROJECT_ROOT / "vendor" / "opencode")))
        status_data.setdefault("config", status_data.get("opencode_config", {}))
        status_data.setdefault("tools", status_data.get("discovered_tools", []))
        status_data.setdefault("agents", status_data.get("builtin_agents", []))
        status_data.setdefault("routes", status_data.get("server_routes", []))
        status_data.setdefault(
            "sdk_bridge",
            {
                "status": "ready" if status_data.get("sdk_bridge_ready", True) else "missing",
                "sdk_package": "@opencode-ai/sdk",
                "sdk_package_path": status_data.get("sdk_package_path"),
                "sdk_adapter_path": status_data.get("sdk_adapter_path"),
            },
        )
        return status_data

    @app.get("/api/opencode/config")
    @app.get("/config")
    async def api_opencode_config() -> Dict[str, Any]:
        return _to_serializable(state.opencode_runtime.get_opencode_config(redacted=True))

    @app.get("/api/opencode/providers")
    @app.get("/config/providers")
    async def api_opencode_providers() -> Dict[str, Any]:
        return _to_serializable(state.opencode_runtime.get_config_providers())

    @app.get("/api/opencode/agents")
    @app.get("/agent")
    async def api_opencode_agents() -> Dict[str, Any]:
        return _to_serializable(state.opencode_runtime.list_agents())

    @app.get("/api/opencode/commands")
    @app.get("/command")
    async def api_opencode_commands() -> Any:
        cmds = _to_serializable(state.opencode_runtime.list_commands())
        return cmds

    @app.get("/api/opencode/mcp")
    @app.get("/mcp")
    async def api_opencode_mcp() -> Dict[str, Any]:
        return _to_serializable(state.opencode_runtime.get_mcp_status())

    @app.get("/api/opencode/sessions")
    @app.get("/session")
    async def api_opencode_sessions_list(
        project_id: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        sessions = _to_serializable(state.opencode_runtime.list_sessions(project_id=project_id))
        if not isinstance(sessions, list):
            sessions = []
        return {
            "sessions": sessions,
            "total": len(sessions),
            "active_workspace": str(state.workspace_root),
            "project_id": project_id or state.project_id,
        }

    @app.post("/api/opencode/sessions")
    @app.post("/session")
    async def api_opencode_sessions_create(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        title = payload.get("title") or payload.get("name") or "New Engineering Session"
        agent = str(payload.get("agent") or "build")
        model = str(payload.get("model") or "freellm/auto")
        project_id = str(payload.get("project_id") or payload.get("projectID") or state.project_id)
        parent_id = payload.get("parent_id") or payload.get("parentID")
        permission_mode = payload.get("permission_mode") or payload.get("mode") or state.security_mode
        directory = payload.get("directory") or payload.get("workspace_path") or str(state.workspace_root)

        created = _to_serializable(
            state.opencode_runtime.create_session(
                title=title,
                agent=agent,
                model=model,
                project_id=project_id,
                parent_id=parent_id,
                permission_mode=permission_mode,
                directory=directory,
            )
        )
        return created

    @app.get("/api/opencode/sessions/{session_id}")
    @app.get("/session/{session_id}")
    async def api_opencode_sessions_get(session_id: str) -> Dict[str, Any]:
        try:
            session = _to_serializable(state.opencode_runtime.get_session(session_id))
            events = _to_serializable(state.opencode_runtime.get_session_events(session_id))
            if isinstance(session, dict):
                session.setdefault("events", events)
            return session
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.delete("/api/opencode/sessions/{session_id}")
    @app.delete("/session/{session_id}")
    async def api_opencode_sessions_delete(session_id: str) -> Dict[str, Any]:
        try:
            res = _to_serializable(state.opencode_runtime.delete_session(session_id))
            if isinstance(res, dict):
                res.setdefault("session_id", session_id)
                res.setdefault("status", "deleted")
                return res
            return {"status": "deleted", "session_id": session_id, "deleted": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/api/opencode/sessions/{session_id}/message")
    @app.post("/session/{session_id}/message")
    async def api_opencode_sessions_message(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        rt = state.opencode_runtime
        # Auto-create session if caller posts directly to a non-existent session_id
        if session_id not in rt._sessions:
            rt.create_session(
                title=str(payload.get("title") or "Engineering Session"),
                agent=str(payload.get("agent") or "build"),
                model=str(payload.get("model") or "freellm/auto"),
                project_id=str(payload.get("project_id") or state.project_id),
                permission_mode=str(payload.get("permission_mode") or payload.get("mode") or state.security_mode),
            )
            # Ensure exact requested session_id is registered if different from generated ID
            if session_id not in rt._sessions:
                last_created_id = next(reversed(rt._sessions.keys()))
                sess_obj = rt._sessions.pop(last_created_id)
                sess_obj["id"] = session_id
                sess_obj["session_id"] = session_id
                rt._sessions[session_id] = sess_obj
                rt._session_snapshots[session_id] = rt._session_snapshots.pop(last_created_id, {})
                rt._session_permissions[session_id] = rt._session_permissions.pop(last_created_id, {})
                rt._session_events[session_id] = rt._session_events.pop(last_created_id, [])

        raw_content = (
            payload.get("message")
            or payload.get("prompt")
            or payload.get("content")
            or payload.get("text")
            or ""
        )
        if not raw_content and isinstance(payload.get("parts"), list):
            raw_content = "\n".join(
                str(p.get("text", "")) for p in payload["parts"] if isinstance(p, dict) and p.get("text")
            )
        content = str(raw_content).strip()
        if not content:
            raise HTTPException(status_code=400, detail="Message content or prompt is required")

        agent = payload.get("agent")
        model = payload.get("model")
        actions = payload.get("actions")
        tools_requested = payload.get("tools") or payload.get("tool_calls")
        test_command = payload.get("test_command")

        turn = _to_serializable(
            rt.send_message(
                session_id=session_id,
                content=content,
                agent=agent,
                model=model,
                actions=actions,
                tools_requested=tools_requested,
                test_command=test_command,
            )
        )
        if isinstance(turn, dict):
            turn.setdefault("id", turn.get("message", {}).get("id", f"turn_{int(time.time())}"))
            turn.setdefault("response", turn.get("message", {}).get("content", ""))
            turn.setdefault("assistant_text", turn.get("response", ""))
            turn.setdefault("thinking_summary", turn.get("thinking", ""))
            turn.setdefault("tool_calls", turn.get("tool_activity", []))
            diffs_list = turn.get("code_changes") or []
            combined_diff = "\n\n".join(
                d.get("unified_diff") or d.get("diff") or ""
                for d in diffs_list
                if isinstance(d, dict) and (d.get("unified_diff") or d.get("diff"))
            )
            turn.setdefault("diff", combined_diff)
            for d in diffs_list:
                if isinstance(d, dict) and d.get("file"):
                    state.recent_file_diffs[str(d["file"])] = d
        return turn

    @app.get("/api/opencode/sessions/{session_id}/events")
    @app.get("/session/{session_id}/events")
    async def api_opencode_sessions_events(
        session_id: str,
        request: Request,
        stream: bool = Query(default=False),
        sse: bool = Query(default=False),
    ) -> Any:
        rt = state.opencode_runtime
        if session_id not in rt._sessions:
            raise HTTPException(status_code=404, detail=f"OpenCode session '{session_id}' not found")

        accept_header = (request.headers.get("accept") or "").lower()
        if stream or sse or "text/event-stream" in accept_header:
            sse_payload = rt.format_sse_stream(session_id=session_id)
            return StreamingResponse(iter([sse_payload]), media_type="text/event-stream")

        events = _to_serializable(rt.get_session_events(session_id))
        session = _to_serializable(rt.get_session(session_id))
        return {
            "session_id": session_id,
            "events": events,
            "timeline": session.get("timeline", []) if isinstance(session, dict) else events,
            "sse": rt.format_sse_stream(session_id=session_id),
            "total": len(events) if isinstance(events, list) else 0,
        }

    @app.get("/api/opencode/events")
    @app.get("/event")
    async def api_opencode_global_events(
        request: Request,
        session_id: Optional[str] = Query(default=None),
        stream: bool = Query(default=False),
    ) -> Any:
        rt = state.opencode_runtime
        accept_header = (request.headers.get("accept") or "").lower()
        if stream or "text/event-stream" in accept_header:
            sse_payload = rt.format_sse_stream(session_id=session_id)
            return StreamingResponse(iter([sse_payload]), media_type="text/event-stream")

        events = _to_serializable(rt.get_session_events(session_id)) if session_id and session_id in rt._sessions else []
        return {
            "session_id": session_id,
            "events": events,
            "stream": rt.format_sse_stream(session_id=session_id),
        }

    @app.get("/api/opencode/sessions/{session_id}/diff")
    @app.get("/session/{session_id}/diff")
    async def api_opencode_sessions_diff(session_id: str) -> Dict[str, Any]:
        rt = state.opencode_runtime
        if session_id not in rt._sessions:
            raise HTTPException(status_code=404, detail=f"OpenCode session '{session_id}' not found")
        diff_data = _to_serializable(rt.get_session_diff(session_id))
        if isinstance(diff_data, dict):
            diffs_list = diff_data.get("diffs", [])
            combined = "\n\n".join(
                d.get("unified_diff") or d.get("diff") or ""
                for d in diffs_list
                if isinstance(d, dict) and (d.get("unified_diff") or d.get("diff"))
            )
            diff_data.setdefault("diff", combined)
            diff_data.setdefault("unified_diff", combined)
        return diff_data

    @app.post("/api/opencode/sessions/{session_id}/revert")
    @app.post("/session/{session_id}/revert")
    async def api_opencode_sessions_revert(
        session_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = payload or {}
        rt = state.opencode_runtime
        if session_id not in rt._sessions:
            raise HTTPException(status_code=404, detail=f"OpenCode session '{session_id}' not found")
        message_id = payload.get("messageID") or payload.get("message_id")
        checkpoint_id = payload.get("checkpoint_id") or payload.get("checkpointID")
        res = _to_serializable(
            rt.revert_session(session_id=session_id, message_id=message_id, checkpoint_id=checkpoint_id)
        )
        if isinstance(res, dict):
            res.setdefault("status", "reverted")
        return res

    @app.post("/api/opencode/sessions/{session_id}/abort")
    @app.post("/session/{session_id}/abort")
    async def api_opencode_sessions_abort(session_id: str) -> Dict[str, Any]:
        rt = state.opencode_runtime
        if session_id not in rt._sessions:
            raise HTTPException(status_code=404, detail=f"OpenCode session '{session_id}' not found")
        return _to_serializable(rt.abort_session(session_id))

    @app.get("/api/opencode/sessions/{session_id}/children")
    @app.get("/session/{session_id}/children")
    async def api_opencode_sessions_children(session_id: str) -> List[Dict[str, Any]]:
        rt = state.opencode_runtime
        if session_id not in rt._sessions:
            raise HTTPException(status_code=404, detail=f"OpenCode session '{session_id}' not found")
        return _to_serializable(rt.get_session_children(session_id))

    @app.get("/api/opencode/sessions/{session_id}/todo")
    @app.get("/session/{session_id}/todo")
    async def api_opencode_sessions_todo(session_id: str) -> List[Dict[str, Any]]:
        rt = state.opencode_runtime
        if session_id not in rt._sessions:
            raise HTTPException(status_code=404, detail=f"OpenCode session '{session_id}' not found")
        return _to_serializable(rt.get_session_todo(session_id))

    @app.post("/api/opencode/sessions/{session_id}/permissions/{permission_id}")
    @app.post("/session/{session_id}/permissions/{permission_id}")
    async def api_opencode_sessions_permission(
        session_id: str,
        permission_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = payload or {}
        rt = state.opencode_runtime
        if session_id not in rt._sessions:
            raise HTTPException(status_code=404, detail=f"OpenCode session '{session_id}' not found")
        response_val = str(payload.get("response") or ("allow" if payload.get("approved", True) else "deny"))
        remember = bool(payload.get("remember", False))
        res = _to_serializable(
            rt.resolve_permission(
                session_id=session_id,
                permission_id=permission_id,
                response=response_val,
                remember=remember,
            )
        )
        if isinstance(res, dict):
            res.setdefault("approved", response_val in ("allow", "always"))
        return res

    # Standard OpenCode SDK convenience routes (/project, /file, /find)
    @app.get("/project")
    async def api_opencode_project_list() -> List[Dict[str, Any]]:
        return [
            {
                "id": state.project_id,
                "worktree": str(state.workspace_root),
                "name": state.workspace_root.name,
            }
        ]

    @app.get("/project/current")
    async def api_opencode_project_current() -> Dict[str, Any]:
        return {
            "id": state.project_id,
            "worktree": str(state.workspace_root),
            "name": state.workspace_root.name,
            "vcs": "git" if (state.workspace_root / ".git").exists() else "none",
        }

    @app.get("/file")
    async def api_opencode_file_list(path: str = Query(default=".")) -> Dict[str, Any]:
        return _to_serializable(state.opencode_client._dispatch_local("GET", "/file", params={"path": path}))

    @app.get("/file/content")
    async def api_opencode_file_content(path: str = Query(default="README.md")) -> Dict[str, Any]:
        return _to_serializable(state.opencode_client._dispatch_local("GET", "/file/content", params={"path": path}))

    @app.get("/file/status")
    async def api_opencode_file_status() -> Dict[str, Any]:
        return _to_serializable(state.opencode_client._dispatch_local("GET", "/file/status"))

    @app.get("/find")
    async def api_opencode_find_text(pattern: str = Query(default="")) -> Dict[str, Any]:
        return _to_serializable(state.opencode_client._dispatch_local("GET", "/find", params={"pattern": pattern}))

    @app.get("/find/file")
    async def api_opencode_find_file(query: str = Query(default="*")) -> Dict[str, Any]:
        return _to_serializable(state.opencode_client._dispatch_local("GET", "/find/file", params={"query": query}))

    @app.get("/find/symbol")
    async def api_opencode_find_symbol(query: str = Query(default="")) -> Dict[str, Any]:
        return _to_serializable(state.opencode_client._dispatch_local("GET", "/find/symbol", params={"query": query}))

    # ------------------------------------------------------------------
    # 1B. Workspace File Explorer, Code Editor, Diff & Problems (`/api/files/*`, `/api/problems`)
    # ------------------------------------------------------------------
    @app.get("/api/files/tree")
    async def api_files_tree(
        path: Optional[str] = Query(default=None),
        max_depth: int = Query(default=4, ge=1, le=10),
        include_hidden: bool = Query(default=False),
    ) -> Dict[str, Any]:
        base_dir = state.workspace_root
        if path and path not in (".", "/"):
            base_dir = state.security_policy.validate_path(path, allow_write=False, allow_delete=False)
        if not base_dir.exists() or not base_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"Directory not found: {path}")

        flat_files: List[str] = []
        total_dirs = 0

        def _walk_dir(current: Path, depth: int) -> List[Dict[str, Any]]:
            nonlocal total_dirs
            if depth > max_depth:
                return []
            nodes: List[Dict[str, Any]] = []
            try:
                entries = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return []

            for entry in entries:
                name = entry.name
                if not include_hidden and name.startswith(".") and name not in (".gitignore",):
                    continue
                if entry.is_dir():
                    if name in TREE_IGNORED_DIRS:
                        continue
                    rel_dir = entry.relative_to(state.workspace_root).as_posix()
                    total_dirs += 1
                    # Keep vendor directory compact in tree view so large upstream clones return instantaneously
                    if name == "vendor" and depth == 1:
                        children = [
                            {
                                "name": sub.name,
                                "path": sub.relative_to(state.workspace_root).as_posix(),
                                "type": "directory" if sub.is_dir() else "file",
                                "children": [],
                            }
                            for sub in sorted(entry.iterdir(), key=lambda x: x.name)[:20]
                            if not sub.name.startswith(".")
                        ]
                    else:
                        children = _walk_dir(entry, depth + 1)
                    nodes.append(
                        {
                            "name": name,
                            "path": rel_dir,
                            "type": "directory",
                            "children": children,
                        }
                    )
                elif entry.is_file():
                    rel_file = entry.relative_to(state.workspace_root).as_posix()
                    ext = entry.suffix.lower()
                    lang = LANGUAGE_BY_EXT.get(ext, "Text")
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    flat_files.append(rel_file)
                    nodes.append(
                        {
                            "name": name,
                            "path": rel_file,
                            "type": "file",
                            "extension": ext,
                            "language": lang,
                            "size_bytes": size,
                        }
                    )
            return nodes

        tree_nodes = _walk_dir(base_dir, 1)
        return {
            "active_workspace": str(state.workspace_root),
            "project_id": state.project_id,
            "root": state.workspace_root.name,
            "tree": tree_nodes,
            "nodes": tree_nodes,
            "files": flat_files,
            "total_files": len(flat_files),
            "total_directories": total_dirs,
        }

    @app.get("/api/files/read")
    async def api_files_read(
        path: Optional[str] = Query(default=None),
        file: Optional[str] = Query(default=None),
        start_line: Optional[int] = Query(default=None),
        end_line: Optional[int] = Query(default=None),
    ) -> Dict[str, Any]:
        target_rel = (path or file or "").strip()
        if not target_rel:
            default_readme = state.workspace_root / "README.md"
            if default_readme.exists():
                target_rel = "README.md"
            else:
                raise HTTPException(status_code=400, detail="Query parameter 'path' is required")

        try:
            read_res = state.fs_tools.read_file(target_rel, start_line=start_line, end_line=end_line)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        meta = read_res.to_dict() if hasattr(read_res, "to_dict") else dict(read_res)
        content = str(meta.get("content", ""))
        rel_path = str(meta.get("relative_path", target_rel)).replace("\\", "/")
        ext = Path(rel_path).suffix.lower()
        language = LANGUAGE_BY_EXT.get(ext, "Text")
        raw_lines = content.splitlines()
        start_num = max(1, int(start_line or 1))
        numbered_lines = [
            {
                "line": start_num + idx,
                "number": start_num + idx,
                "text": ln,
                "content": ln,
            }
            for idx, ln in enumerate(raw_lines)
        ]
        numbered_content = "\n".join(f"{item['line']:4d} | {item['text']}" for item in numbered_lines)

        return {
            "success": True,
            "path": rel_path,
            "relative_path": rel_path,
            "absolute_path": meta.get("path", str((state.workspace_root / rel_path).resolve())),
            "name": Path(rel_path).name,
            "extension": ext,
            "language": language,
            "content": content,
            "numbered_content": numbered_content,
            "lines": numbered_lines,
            "line_count": len(raw_lines),
            "total_lines": meta.get("total_lines", len(raw_lines)),
            "size_bytes": meta.get("size_bytes", len(content.encode("utf-8"))),
            "injection_scan": _to_serializable(meta.get("injection_scan")),
        }

    @app.post("/api/files/write")
    async def api_files_write(payload: Dict[str, Any]) -> Dict[str, Any]:
        target_rel = str(payload.get("path") or payload.get("file") or payload.get("relative_path") or "").strip()
        if not target_rel:
            raise HTTPException(status_code=400, detail="'path' is required")

        content = payload.get("content")
        target_snippet = payload.get("target")
        replacement_snippet = payload.get("replacement")

        resolved = state.security_policy.validate_path(target_rel, allow_write=True, allow_delete=False)
        before_text = resolved.read_text(encoding="utf-8", errors="replace") if resolved.exists() and resolved.is_file() else ""

        # Automatic Git checkpoint before mutating file
        ckpt_label = str(payload.get("checkpoint_label") or f"Edit {target_rel}")
        checkpoint_info: Dict[str, Any] = {}
        try:
            checkpoint_info = _to_serializable(
                state.git_manager.create_checkpoint(label=ckpt_label, task_id=str(payload.get("task_id") or "editor"))
            )
        except Exception:
            checkpoint_info = {"checkpoint_id": f"chk_{int(time.time())}"}

        try:
            if target_snippet is not None and replacement_snippet is not None and content is None:
                write_res = _to_serializable(
                    state.fs_tools.edit_file(
                        target_rel,
                        target=str(target_snippet),
                        replacement=str(replacement_snippet),
                        approved=bool(payload.get("approved", False)),
                        approval_id=payload.get("approval_id"),
                    )
                )
                after_text = resolved.read_text(encoding="utf-8", errors="replace") if resolved.exists() else ""
            else:
                after_text = str(content if content is not None else "")
                write_res = _to_serializable(
                    state.fs_tools.write_file(
                        target_rel,
                        content=after_text,
                        approved=bool(payload.get("approved", False)),
                        approval_id=payload.get("approval_id"),
                    )
                )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        rel_posix = str(write_res.get("relative_path") or target_rel).replace("\\", "/")
        diff_str = str(write_res.get("diff") or state.fs_tools.compute_diff(before_text, after_text, rel_posix))
        side_by_side = _build_side_by_side_diff(before_text, after_text)
        diff_lines = diff_str.splitlines()
        additions = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
        deletions = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))

        diff_record = {
            "path": rel_posix,
            "file": rel_posix,
            "status": "added" if write_res.get("created") else "modified",
            "additions": additions,
            "deletions": deletions,
            "diff": diff_str,
            "unified_diff": diff_str,
            "side_by_side": side_by_side,
            "checkpoint_id": checkpoint_info.get("checkpoint_id") if isinstance(checkpoint_info, dict) else str(checkpoint_info),
            "updated_at": time.time(),
        }
        state.recent_file_diffs[rel_posix] = diff_record

        return {
            **write_res,
            "path": rel_posix,
            "relative_path": rel_posix,
            "diff": diff_str,
            "unified_diff": diff_str,
            "side_by_side": side_by_side,
            "additions": additions,
            "deletions": deletions,
            "checkpoint_id": diff_record["checkpoint_id"],
            "checkpoint": checkpoint_info,
        }

    @app.get("/api/files/changed")
    async def api_files_changed() -> Dict[str, Any]:
        git = state.git_manager
        status_info = _to_serializable(git.init_or_status())
        diff_info = _to_serializable(git.get_diff())
        checkpoints = _to_serializable(git.list_checkpoints()) if hasattr(git, "list_checkpoints") else []

        changed_by_path: Dict[str, Dict[str, Any]] = {}

        # 1. Include files tracked in recent_file_diffs and OpenCode sessions
        for rel_p, rec in state.recent_file_diffs.items():
            changed_by_path[rel_p] = dict(rec)

        for sess in state.opencode_runtime.list_sessions():
            for d in sess.get("diffs", []):
                if isinstance(d, dict) and d.get("file"):
                    fp = str(d["file"])
                    changed_by_path[fp] = {
                        "path": fp,
                        "file": fp,
                        "status": d.get("status", "modified"),
                        "additions": int(d.get("additions", 0)),
                        "deletions": int(d.get("deletions", 0)),
                        "diff": d.get("unified_diff") or d.get("diff") or "",
                        "unified_diff": d.get("unified_diff") or d.get("diff") or "",
                        "side_by_side": d.get("side_by_side") or [],
                    }

        # 2. Include working-tree modified/staged/untracked files from Git
        if isinstance(status_info, dict):
            for bucket, status_label in (
                ("modified", "modified"),
                ("staged", "staged"),
                ("untracked", "untracked"),
            ):
                for fp in status_info.get(bucket, []) or []:
                    norm_fp = str(fp).replace("\\", "/")
                    if norm_fp.startswith("vendor/") or norm_fp == "vendor":
                        continue
                    if norm_fp not in changed_by_path:
                        file_diff_res = git._run_git(["diff", "--", norm_fp])
                        f_diff = file_diff_res.stdout or ""
                        if not f_diff and status_label == "untracked":
                            full_f = state.workspace_root / norm_fp
                            if full_f.exists() and full_f.is_file() and full_f.stat().st_size <= 128_000:
                                txt = full_f.read_text(encoding="utf-8", errors="replace")
                                f_diff = state.fs_tools.compute_diff("", txt, norm_fp)
                        d_lines = f_diff.splitlines()
                        add_cnt = sum(1 for ln in d_lines if ln.startswith("+") and not ln.startswith("+++"))
                        del_cnt = sum(1 for ln in d_lines if ln.startswith("-") and not ln.startswith("---"))
                        changed_by_path[norm_fp] = {
                            "path": norm_fp,
                            "file": norm_fp,
                            "status": status_label,
                            "additions": add_cnt,
                            "deletions": del_cnt,
                            "diff": f_diff,
                            "unified_diff": f_diff,
                            "side_by_side": [],
                        }

        changed_list = list(changed_by_path.values())
        combined_diff = (diff_info.get("diff") if isinstance(diff_info, dict) else "") or "\n\n".join(
            item["diff"] for item in changed_list if item.get("diff")
        )

        return {
            "active_workspace": str(state.workspace_root),
            "project_id": state.project_id,
            "branch": status_info.get("branch", "main") if isinstance(status_info, dict) else "main",
            "clean": len(changed_list) == 0,
            "changed_files": changed_list,
            "files": changed_list,
            "total_changed": len(changed_list),
            "diff": combined_diff,
            "combined_diff": combined_diff,
            "checkpoints": checkpoints if isinstance(checkpoints, list) else [],
        }

    @app.get("/api/problems")
    @app.post("/api/problems")
    async def api_workspace_problems() -> Dict[str, Any]:
        problems: List[Dict[str, Any]] = []

        # 1. Fast Python AST syntax diagnostics across workspace files
        for py_file in state.intelligence._iter_workspace_files():
            if py_file.suffix.lower() != ".py":
                continue
            rel = py_file.relative_to(state.workspace_root).as_posix()
            if rel.startswith("vendor/"):
                continue
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
                ast.parse(source, filename=rel)
            except SyntaxError as exc:
                problems.append(
                    {
                        "id": f"ast_{len(problems) + 1}",
                        "file": rel,
                        "path": rel,
                        "line": int(exc.lineno or 1),
                        "column": int(exc.offset or 1),
                        "severity": "error",
                        "category": "syntax",
                        "source": "python_ast",
                        "message": f"SyntaxError: {exc.msg}",
                    }
                )
            except OSError:
                continue

        # 2. SAST & Code Quality Diagnostics from SkillsAndCapabilities
        try:
            sast_res = _to_serializable(state.advanced.run_security_scan())
            findings = sast_res.get("findings", []) if isinstance(sast_res, dict) else []
            for item in findings:
                if not isinstance(item, dict):
                    continue
                f_rel = str(item.get("file") or item.get("path") or "")
                if f_rel.startswith("vendor/"):
                    continue
                raw_sev = str(item.get("severity") or "warning").lower()
                norm_sev = "error" if raw_sev in ("critical", "high", "error") else ("warning" if raw_sev == "medium" else "info")
                problems.append(
                    {
                        "id": f"sast_{len(problems) + 1}",
                        "file": f_rel,
                        "path": f_rel,
                        "line": int(item.get("line") or 1),
                        "column": 1,
                        "severity": norm_sev,
                        "raw_severity": raw_sev,
                        "category": str(item.get("category") or "security"),
                        "source": "bel_code_sast",
                        "message": str(item.get("message") or item.get("description") or "Security finding"),
                    }
                )
        except Exception:
            pass

        err_cnt = sum(1 for p in problems if p["severity"] == "error")
        warn_cnt = sum(1 for p in problems if p["severity"] == "warning")
        info_cnt = sum(1 for p in problems if p["severity"] == "info")

        return {
            "active_workspace": str(state.workspace_root),
            "project_id": state.project_id,
            "problems": problems,
            "findings": problems,
            "total": len(problems),
            "counts": {
                "error": err_cnt,
                "warning": warn_cnt,
                "info": info_cnt,
                "total": len(problems),
            },
            "summary": (
                f"{len(problems)} problem(s) detected ({err_cnt} errors, {warn_cnt} warnings)."
                if problems
                else "0 problems detected across workspace AST syntax & security checks."
            ),
        }

    @app.get("/api/files/search")
    async def api_files_search(
        q: str = Query(default=""),
        limit: int = Query(default=60, ge=1, le=200),
    ) -> Dict[str, Any]:
        query_text = q.strip()
        if not query_text:
            return {"query": "", "matches": [], "total": 0}
        q_lower = query_text.lower()
        matches: List[Dict[str, Any]] = []
        for fpath in state.intelligence._iter_workspace_files():
            try:
                rel = fpath.relative_to(state.workspace_root).as_posix()
            except ValueError:
                continue
            if rel.startswith("vendor/") or any(part in TREE_IGNORED_DIRS for part in Path(rel).parts):
                continue
            try:
                if fpath.stat().st_size > 256_000:
                    continue
                lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for idx, ln in enumerate(lines, start=1):
                if q_lower in ln.lower():
                    matches.append(
                        {
                            "file": rel,
                            "line": idx,
                            "text": ln.strip()[:180],
                        }
                    )
                    if len(matches) >= limit:
                        return {"query": query_text, "matches": matches, "total": len(matches)}
        return {"query": query_text, "matches": matches, "total": len(matches)}

    @app.get("/api/logs")
    async def api_runtime_logs(limit: int = Query(default=80, ge=1, le=500)) -> Dict[str, Any]:
        audit_logs: List[Any] = []
        changelog_entries: List[Any] = []
        try:
            audit_logs = _to_serializable(state.security_policy.get_audit_logs(limit=limit))
        except Exception:
            audit_logs = []
        try:
            changelog_entries = _to_serializable(state.memory.get_changelog(state.project_id))
        except Exception:
            changelog_entries = []
        return {
            "project_id": state.project_id,
            "audit_logs": audit_logs if isinstance(audit_logs, list) else [],
            "changelog": changelog_entries[-limit:] if isinstance(changelog_entries, list) else [],
        }

    # ------------------------------------------------------------------
    # 2. Project Intelligence & Architecture
    # ------------------------------------------------------------------
    @app.get("/api/projects/current")
    async def api_projects_current() -> Dict[str, Any]:
        files_preview: List[str] = []
        try:
            listed = state.fs_tools.list_files()
            if isinstance(listed, dict) and "files" in listed:
                files_preview = [f for f in listed["files"] if not str(f).startswith("vendor/")][:100]
            elif isinstance(listed, list):
                files_preview = [str(x) for x in listed if not str(x).startswith("vendor/")][:100]
        except Exception:
            try:
                files_preview = [
                    str(p.relative_to(state.workspace_root)).replace("\\", "/")
                    for p in state.workspace_root.rglob("*")
                    if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts and "vendor" not in p.parts
                ][:100]
            except Exception:
                files_preview = []

        git_branch = "no-git"
        git_clean = True
        modified_count = 0
        try:
            gst = _to_serializable(state.git_manager.init_or_status())
            if isinstance(gst, dict):
                git_branch = str(gst.get("branch") or "main")
                mod = len(gst.get("modified") or [])
                stg = len(gst.get("staged") or [])
                unt = len(gst.get("untracked") or [])
                modified_count = mod + stg + unt
                git_clean = modified_count == 0
        except Exception:
            pass

        return {
            "active_workspace": str(state.workspace_root),
            "workspace_path": str(state.workspace_root),
            "project_id": state.project_id,
            "name": state.workspace_root.name,
            "exists": state.workspace_root.exists(),
            "security_mode": str(getattr(state.security_policy, "mode", state.security_mode)),
            "git_branch": git_branch,
            "git_clean": git_clean,
            "modified_count": modified_count,
            "recent_projects": state.recent_workspaces,
            "files": files_preview,
        }

    @app.post("/api/projects/select")
    async def api_projects_select(payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_path = (
            payload.get("workspace_path")
            or payload.get("path")
            or payload.get("workspace")
            or payload.get("active_workspace")
        )
        if not raw_path:
            raise HTTPException(status_code=400, detail="workspace_path is required")
        target = Path(str(raw_path)).resolve()
        info = state.set_workspace(target, project_id=payload.get("project_id"))
        return {
            "status": "selected",
            **info,
        }

    @app.get("/api/projects/scan")
    @app.post("/api/projects/scan")
    async def api_projects_scan(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if payload and (payload.get("workspace_path") or payload.get("path")):
            raw_path = payload.get("workspace_path") or payload.get("path")
            state.set_workspace(Path(str(raw_path)).resolve())
        scan_result = _to_serializable(state.intelligence.scan_project())
        if isinstance(scan_result, dict):
            scan_result.setdefault("active_workspace", str(state.workspace_root))
            scan_result.setdefault("project_id", state.project_id)
            return scan_result
        return {
            "active_workspace": str(state.workspace_root),
            "project_id": state.project_id,
            "scan": scan_result,
        }

    @app.get("/api/projects/architecture")
    async def api_projects_architecture() -> Dict[str, Any]:
        arch = _to_serializable(state.intelligence.generate_architecture_map())
        if isinstance(arch, dict):
            if "mermaid" not in arch and "mermaid_diagram" in arch:
                arch["mermaid"] = arch["mermaid_diagram"]
            elif "mermaid_diagram" not in arch and "mermaid" in arch:
                arch["mermaid_diagram"] = arch["mermaid"]
            return arch
        return {"architecture": arch}

    @app.get("/api/projects/symbols")
    async def api_projects_symbols(
        q: Optional[str] = Query(default=None),
        query: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        index_data = _to_serializable(state.intelligence.build_symbol_index())
        search_term = (q or query or "").strip().lower()
        if search_term and isinstance(index_data, dict):
            symbols = index_data.get("symbols", [])
            if isinstance(symbols, list):
                filtered = [
                    s
                    for s in symbols
                    if search_term in str(s.get("name", "")).lower()
                    or search_term in str(s.get("file", "")).lower()
                    or search_term in str(s.get("type", s.get("kind", ""))).lower()
                ]
                return {
                    **index_data,
                    "symbols": filtered,
                    "filtered_count": len(filtered),
                    "query": search_term,
                }
        return index_data if isinstance(index_data, dict) else {"symbols": index_data}

    @app.post("/api/projects/context")
    async def api_projects_context(payload: Dict[str, Any]) -> Dict[str, Any]:
        query_text = str(payload.get("query") or payload.get("prompt") or "")
        top_k = int(payload.get("top_k", 5))
        results = _to_serializable(state.intelligence.retrieve_context(query_text, top_k=top_k))
        if isinstance(results, dict) and "results" in results:
            return results
        return {
            "query": query_text,
            "top_k": top_k,
            "results": results if isinstance(results, list) else [results],
        }

    @app.get("/api/projects/health")
    async def api_projects_health() -> Dict[str, Any]:
        health = _to_serializable(state.intelligence.analyze_code_health())
        return health if isinstance(health, dict) else {"health": health}

    # ------------------------------------------------------------------
    # 3. AI Providers & Model Router (with SecretVault & OpenCode Config Sync)
    # ------------------------------------------------------------------
    @app.get("/api/providers")
    async def api_providers_list() -> Dict[str, Any]:
        settings_map = state.settings.get_settings()
        providers = _to_serializable(state.provider_manager.list_providers())
        routing = _to_serializable(state.provider_manager.get_routing_policy())
        usage = _to_serializable(state.provider_manager.get_usage_summary())
        return {
            "primary_provider": settings_map.get("primary_provider", "freellm"),
            "secondary_provider": settings_map.get("secondary_provider"),
            "tertiary_provider": settings_map.get("tertiary_provider"),
            "providers": providers,
            "routing": routing,
            "usage": usage,
        }

    @app.get("/api/providers/registry")
    async def api_providers_registry() -> Dict[str, Any]:
        """Return verified provider metadata from registry.json merged with live configured status (no secrets)."""
        from bel_code.providers.registry import load_provider_registry

        reg = load_provider_registry()
        entries = reg.get("providers", [])
        live_map = {
            str(p.get("provider_id", "")).lower(): p
            for p in _to_serializable(state.provider_manager.list_providers())
            if isinstance(p, dict)
        }
        enriched: List[Dict[str, Any]] = []
        seen_ids = set()
        for entry in entries:
            pid = str(entry.get("provider_id", "")).lower()
            seen_ids.add(pid)
            live = live_map.get(pid, {})
            live_models = live.get("models") if isinstance(live.get("models"), list) else []
            reg_models = entry.get("free_models") if isinstance(entry.get("free_models"), list) else []
            merged_models = list(dict.fromkeys([*reg_models, *live_models]))
            enriched.append(
                {
                    **entry,
                    "models": merged_models,
                    "has_api_key": bool(live.get("has_api_key", False)),
                    "enabled": bool(live.get("enabled", True)),
                    "is_primary": bool(live.get("is_primary", False)),
                    "is_secondary": bool(live.get("is_secondary", False)),
                    "is_fallback": bool(live.get("is_fallback", False)),
                    "accounts": live.get("accounts", []),
                    "live_base_url": live.get("base_url") or entry.get("api_base_url", ""),
                    "live_default_model": live.get("default_model") or entry.get("default_model", ""),
                }
            )
        # Include any user-registered custom providers not in the static registry
        for pid, live in live_map.items():
            if pid not in seen_ids:
                enriched.append(
                    {
                        "provider_id": pid,
                        "name": live.get("name", pid.title()),
                        "category": "byo_api",
                        "free_tier_classification": "custom",
                        "advertised_free": False,
                        "verified": True,
                        "requires_api_key": bool(live.get("requires_api_key", True)),
                        "credit_card_required": False,
                        "api_base_url": live.get("base_url", ""),
                        "openai_compatible": str(live.get("endpoint_mode", "openai")) != "anthropic",
                        "default_model": live.get("default_model", ""),
                        "free_models": live.get("models", []),
                        "models": live.get("models", []),
                        "free_tier_summary": "User-registered endpoint configured via Connect Your Own API.",
                        "rate_limits_summary": "Determined by your target endpoint.",
                        "context_window_summary": "Endpoint / model dependent.",
                        "data_privacy_summary": "Governed by your configured endpoint.",
                        "known_restrictions": [],
                        "setup_steps": [
                            "Confirm the Base URL and Endpoint Mode (OpenAI or Anthropic compatible).",
                            "Save an API key (if required by your gateway) and click Test Connection.",
                        ],
                        "has_api_key": bool(live.get("has_api_key", False)),
                        "enabled": bool(live.get("enabled", True)),
                        "is_primary": bool(live.get("is_primary", False)),
                        "is_secondary": bool(live.get("is_secondary", False)),
                        "is_fallback": bool(live.get("is_fallback", False)),
                        "accounts": live.get("accounts", []),
                        "live_base_url": live.get("base_url", ""),
                        "live_default_model": live.get("default_model", ""),
                    }
                )
        return {"providers": enriched, "count": len(enriched), "last_updated": reg.get("last_updated", "")}

    @app.post("/api/providers/custom")
    async def api_providers_custom(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Register a custom OpenAI/Anthropic-compatible provider endpoint without editing source code."""
        base_url = str(payload.get("base_url") or "").strip()
        default_model = str(payload.get("default_model") or payload.get("model") or "").strip()
        if not base_url:
            raise HTTPException(status_code=400, detail="base_url is required for custom provider registration.")
        if not default_model:
            raise HTTPException(status_code=400, detail="default_model is required for custom provider registration.")

        name = str(payload.get("name") or "Custom Endpoint").strip()
        raw_id = str(payload.get("provider_id") or name or "custom").strip().lower().replace(" ", "-")
        provider_id = "".join(ch for ch in raw_id if ch.isalnum() or ch in ("-", "_")) or "custom"
        endpoint_mode = str(payload.get("endpoint_mode") or "openai").strip().lower()
        api_key = payload.get("api_key")
        requires_api_key = bool(payload.get("requires_api_key", bool(api_key)))
        account_label = str(payload.get("account_label") or payload.get("label") or "default").strip() or "default"
        set_as_primary = bool(payload.get("set_as_primary", False))

        result = _to_serializable(
            state.provider_manager.register_custom_provider(
                provider_id=provider_id,
                name=name,
                base_url=base_url,
                default_model=default_model,
                api_key=str(api_key).strip() if api_key else None,
                endpoint_mode=endpoint_mode,
                requires_api_key=requires_api_key,
                account_label=account_label,
                set_as_primary=set_as_primary,
                cost_per_1k_input=float(payload.get("cost_per_1k_input", 0.0)),
                cost_per_1k_output=float(payload.get("cost_per_1k_output", 0.0)),
            )
        )
        if isinstance(result, dict) and "api_key" in result and result["api_key"]:
            result["api_key"] = "***REDACTED***"
        return {
            "status": "registered",
            "provider_id": provider_id,
            "config": result,
        }

    @app.post("/api/providers/configure")
    async def api_providers_configure(payload: Dict[str, Any]) -> Dict[str, Any]:
        provider_id = str(payload.get("provider_id") or payload.get("id") or "freellm").strip().lower()
        api_key = payload.get("api_key")
        base_url = payload.get("base_url")
        default_model = payload.get("default_model") or payload.get("model")
        enabled = payload.get("enabled", True)
        label = str(payload.get("label", "default"))
        extra = {
            k: v
            for k, v in payload.items()
            if k not in ("provider_id", "id", "api_key", "base_url", "default_model", "model", "enabled", "label")
        }

        pm = state.provider_manager
        sig = inspect.signature(pm.configure_provider)
        call_kwargs: Dict[str, Any] = {}
        if "provider_id" in sig.parameters:
            call_kwargs["provider_id"] = provider_id
        if "api_key" in sig.parameters and api_key is not None:
            call_kwargs["api_key"] = api_key
        if "base_url" in sig.parameters and base_url is not None:
            call_kwargs["base_url"] = base_url
        if "default_model" in sig.parameters and default_model is not None:
            call_kwargs["default_model"] = default_model
        elif "model" in sig.parameters and default_model is not None:
            call_kwargs["model"] = default_model
        if "enabled" in sig.parameters:
            call_kwargs["enabled"] = bool(enabled)
        if "label" in sig.parameters:
            call_kwargs["label"] = label
        for k, v in extra.items():
            if k in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                call_kwargs[k] = v

        result = _to_serializable(pm.configure_provider(**call_kwargs))
        # Redact any accidental key exposure so secrets never leave SecretVault in plaintext
        if isinstance(result, dict) and "api_key" in result and result["api_key"]:
            result["api_key"] = "***REDACTED***"

        # Sync non-secret provider/model metadata with OpenCode runtime config
        opencode_synced = False
        try:
            state.opencode_runtime.sync_opencode_config(
                provider_id=provider_id,
                base_url=str(base_url).strip() if base_url else None,
                default_model=str(default_model).strip() if default_model else None,
            )
            opencode_synced = True
        except Exception:
            opencode_synced = bool(isinstance(result, dict) and result.get("opencode_synced"))

        return {
            "status": "configured",
            "provider_id": provider_id,
            "opencode_synced": opencode_synced,
            "config": result,
        }

    @app.post("/api/providers/{provider_id}/test")
    async def api_providers_test(provider_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        pm = state.provider_manager
        if any(payload.get(k) for k in ("api_key", "base_url", "account_id", "default_model", "model")):
            await api_providers_configure({"provider_id": provider_id, **payload})
        sig = inspect.signature(pm.test_connection)
        kwargs: Dict[str, Any] = {}
        if "provider_id" in sig.parameters:
            kwargs["provider_id"] = provider_id
        if "live_network" in sig.parameters:
            kwargs["live_network"] = bool(payload.get("live_network", True))
        res = _to_serializable(
            pm.test_connection(provider_id) if not kwargs else pm.test_connection(**kwargs)
        )
        if isinstance(res, dict):
            res.setdefault("provider_id", provider_id)
            return res
        return {"provider_id": provider_id, "result": res}

    @app.get("/api/providers/{provider_id}/models")
    async def api_providers_models(provider_id: str) -> Dict[str, Any]:
        models = _to_serializable(state.provider_manager.discover_models(provider_id))
        if isinstance(models, dict) and "models" in models:
            return models
        return {
            "provider_id": provider_id,
            "models": models if isinstance(models, list) else [models],
        }

    @app.get("/api/providers/routing")
    async def api_providers_get_routing() -> Dict[str, Any]:
        policy = _to_serializable(state.provider_manager.get_routing_policy())
        budget = _to_serializable(getattr(state.settings, "budget", None))
        if isinstance(policy, dict):
            return {**policy, "budget": budget}
        return {"routing": policy, "budget": budget}

    @app.post("/api/providers/routing")
    async def api_providers_set_routing(payload: Dict[str, Any]) -> Dict[str, Any]:
        pm = state.provider_manager
        budget_updates = payload.get("budget")
        if isinstance(budget_updates, dict) and hasattr(state.settings, "budget"):
            budget_obj = state.settings.budget
            for field in ("per_task_usd", "per_project_usd", "global_usd", "hard_stop"):
                if field in budget_updates and hasattr(budget_obj, field):
                    setattr(budget_obj, field, budget_updates[field])

        routing_payload = {k: v for k, v in payload.items() if k != "budget"}
        updated = _to_serializable(pm.set_routing_policy(routing_payload))
        return {
            "status": "updated",
            "routing": updated,
            "budget": _to_serializable(getattr(state.settings, "budget", None)),
        }

    @app.get("/api/providers/usage")
    async def api_providers_usage() -> Dict[str, Any]:
        usage = _to_serializable(state.provider_manager.get_usage_summary())
        budget = _to_serializable(getattr(state.settings, "budget", None))
        if isinstance(usage, dict):
            usage.setdefault("budget", budget)
            return usage
        return {"usage": usage, "budget": budget}

    @app.get("/api/onboarding/status")
    async def api_onboarding_status() -> Dict[str, Any]:
        """Return first-launch onboarding state and configured AI provider summary."""
        settings_map = state.settings.get_settings()
        providers = _to_serializable(state.provider_manager.list_providers())
        configured = [
            p.get("provider_id")
            for p in providers
            if isinstance(p, dict) and p.get("has_api_key")
        ]
        completed = bool(settings_map.get("onboarding_completed", False))
        return {
            "onboarding_completed": completed,
            "needs_onboarding": (not completed) and (len(configured) == 0),
            "configured_providers": configured,
            "primary_provider": settings_map.get("primary_provider", "freellm"),
            "secondary_provider": settings_map.get("secondary_provider"),
            "tertiary_provider": settings_map.get("tertiary_provider"),
        }

    @app.post("/api/onboarding/complete")
    async def api_onboarding_complete(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Mark the first-launch onboarding flow as completed (or reset if completed=False)."""
        payload = payload or {}
        completed = bool(payload.get("completed", True))
        state.settings.update_settings({"onboarding_completed": completed})
        return await api_onboarding_status()

    # ------------------------------------------------------------------
    # 4. Security, Approvals & Audit
    # ------------------------------------------------------------------
    @app.get("/api/security/status")
    async def api_security_status() -> Dict[str, Any]:
        sec = state.security_policy
        pending = _to_serializable(sec.list_pending_approvals())
        chain_check = _to_serializable(sec.verify_audit_chain())
        mode_val = getattr(sec, "mode", state.security_mode)
        if hasattr(mode_val, "value"):
            mode_val = mode_val.value
        return {
            "mode": str(mode_val),
            "workspace_root": str(state.workspace_root),
            "pending_approvals": pending if isinstance(pending, list) else [],
            "pending_count": len(pending) if isinstance(pending, list) else 0,
            "audit_chain": chain_check,
            "available_modes": ["ask", "supervised", "autonomous"],
        }

    @app.post("/api/security/mode")
    async def api_security_set_mode(payload: Dict[str, Any]) -> Dict[str, Any]:
        new_mode = str(payload.get("mode", "")).strip().lower()
        if new_mode not in ("ask", "supervised", "autonomous"):
            raise HTTPException(
                status_code=400,
                detail="Invalid security mode. Must be one of: ask, supervised, autonomous",
            )
        state.security_mode = new_mode
        sec = state.security_policy
        if hasattr(sec, "set_mode") and callable(sec.set_mode):
            sec.set_mode(new_mode)
        else:
            sec.mode = new_mode
        try:
            state.settings.update_settings({"security_mode": new_mode})
        except Exception:
            pass
        try:
            state.opencode_runtime.sync_opencode_config(security_mode=new_mode)
        except Exception:
            pass
        return {
            "status": "updated",
            "mode": new_mode,
            "workspace_root": str(state.workspace_root),
        }

    @app.get("/api/security/approvals")
    async def api_security_approvals() -> Dict[str, Any]:
        sec = state.security_policy
        pending = _to_serializable(sec.list_pending_approvals())
        all_approvals = pending
        if hasattr(sec, "approvals") and isinstance(sec.approvals, dict):
            all_approvals = _to_serializable(list(sec.approvals.values()))
        return {
            "pending": pending if isinstance(pending, list) else [],
            "approvals": all_approvals if isinstance(all_approvals, list) else [],
        }

    @app.post("/api/security/approvals")
    async def api_security_create_approval(payload: Dict[str, Any]) -> Dict[str, Any]:
        sec = state.security_policy
        action_type = str(payload.get("action_type") or payload.get("action") or "custom_action")
        target = str(payload.get("target") or "")
        risk = str(payload.get("risk") or "high")
        details = payload.get("details") or payload.get("reason") or {}
        if hasattr(sec, "create_approval_request"):
            sig = inspect.signature(sec.create_approval_request)
            kwargs: Dict[str, Any] = {}
            if "action_type" in sig.parameters:
                kwargs["action_type"] = action_type
            elif "action" in sig.parameters:
                kwargs["action"] = action_type
            if "target" in sig.parameters:
                kwargs["target"] = target
            if "risk" in sig.parameters:
                kwargs["risk"] = risk
            if "details" in sig.parameters:
                kwargs["details"] = details
            elif "reason" in sig.parameters:
                kwargs["reason"] = str(details)
            req = _to_serializable(sec.create_approval_request(**kwargs))
        else:
            req = _to_serializable(
                sec.check_action_permission(action_type, target, risk=risk, details=details)
            )
        return {"status": "created", "approval": req}

    @app.post("/api/security/approvals/{approval_id}/resolve")
    async def api_security_resolve_approval(approval_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        approved = bool(payload.get("approved", True))
        reviewer_note = str(payload.get("reviewer_note") or payload.get("note") or "")
        sec = state.security_policy
        sig = inspect.signature(sec.resolve_approval)
        kwargs: Dict[str, Any] = {}
        if "approval_id" in sig.parameters:
            kwargs["approval_id"] = approval_id
        if "approved" in sig.parameters:
            kwargs["approved"] = approved
        if "reviewer_note" in sig.parameters:
            kwargs["reviewer_note"] = reviewer_note
        res = _to_serializable(
            sec.resolve_approval(**kwargs)
            if kwargs
            else sec.resolve_approval(approval_id, approved, reviewer_note)
        )
        return {
            "status": "resolved",
            "approval_id": approval_id,
            "approved": approved,
            "result": res,
        }

    @app.get("/api/security/audit")
    async def api_security_audit(limit: int = Query(default=100, ge=1, le=1000)) -> Dict[str, Any]:
        sec = state.security_policy
        logs = _to_serializable(sec.get_audit_logs(limit=limit))
        verification = _to_serializable(sec.verify_audit_chain())
        valid = True
        if isinstance(verification, dict):
            valid = bool(verification.get("valid", verification.get("verified", True)))
        return {
            "logs": logs if isinstance(logs, list) else [],
            "verification": verification,
            "valid": valid,
        }

    @app.post("/api/security/scan-injection")
    async def api_security_scan_injection(payload: Dict[str, Any]) -> Dict[str, Any]:
        text = str(payload.get("text") or payload.get("content") or "")
        source = str(payload.get("source") or "external")
        command = payload.get("command")
        sec = state.security_policy
        scan_res = _to_serializable(sec.scan_prompt_injection(text, source=source))
        if isinstance(scan_res, dict) and command:
            scan_res["command_classification"] = _to_serializable(sec.classify_command(str(command)))
        return scan_res if isinstance(scan_res, dict) else {"result": scan_res}

    # ------------------------------------------------------------------
    # 5. Git, Checkpoints & PR Review
    # ------------------------------------------------------------------
    @app.get("/api/git/status")
    async def api_git_status() -> Dict[str, Any]:
        git = state.git_manager
        status_info = _to_serializable(git.init_or_status())
        branches = _to_serializable(git.get_branches()) if hasattr(git, "get_branches") else []
        commits = _to_serializable(git.get_commit_log(limit=15)) if hasattr(git, "get_commit_log") else []
        checkpoints: List[Any] = []
        if hasattr(git, "list_checkpoints") and callable(git.list_checkpoints):
            checkpoints = _to_serializable(git.list_checkpoints())
        elif hasattr(git, "checkpoints"):
            cp_raw = getattr(git, "checkpoints")
            checkpoints = _to_serializable(list(cp_raw.values()) if isinstance(cp_raw, dict) else cp_raw)

        if isinstance(status_info, dict):
            status_info.setdefault("branches", branches)
            status_info.setdefault("commits", commits)
            status_info.setdefault("checkpoints", checkpoints)
            return status_info
        return {
            "status": status_info,
            "branches": branches,
            "commits": commits,
            "checkpoints": checkpoints,
        }

    @app.get("/api/git/diff")
    async def api_git_diff(
        ref1: Optional[str] = Query(default=None),
        ref2: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        diff_res = _to_serializable(state.git_manager.get_diff(ref1=ref1, ref2=ref2))
        if isinstance(diff_res, dict):
            return diff_res
        return {"diff": str(diff_res or "")}

    @app.post("/api/git/checkpoint")
    async def api_git_checkpoint(payload: Dict[str, Any]) -> Dict[str, Any]:
        label = str(payload.get("label") or payload.get("message") or "Manual Checkpoint")
        task_id = str(payload.get("task_id") or "default")
        cp = _to_serializable(state.git_manager.create_checkpoint(label=label, task_id=task_id))
        if isinstance(cp, dict):
            return cp
        return {"checkpoint_id": str(cp), "label": label, "task_id": task_id}

    @app.post("/api/git/rollback")
    async def api_git_rollback(payload: Dict[str, Any]) -> Dict[str, Any]:
        checkpoint_id = str(payload.get("checkpoint_id") or payload.get("id") or "")
        if not checkpoint_id:
            raise HTTPException(status_code=400, detail="checkpoint_id is required")
        preserve_uncommitted = bool(payload.get("preserve_uncommitted", True))
        res = _to_serializable(
            state.git_manager.rollback_to_checkpoint(
                checkpoint_id=checkpoint_id,
                preserve_uncommitted=preserve_uncommitted,
            )
        )
        return res if isinstance(res, dict) else {"status": "rolled_back", "result": res}

    @app.post("/api/git/review-pr")
    async def api_git_review_pr(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        diff_text = payload.get("diff_text") or payload.get("diff")
        review = _to_serializable(state.git_manager.review_pull_request(diff_text=diff_text))
        if payload.get("issue_data") and hasattr(state.git_manager, "issue_to_task_plan"):
            issue_plan = _to_serializable(state.git_manager.issue_to_task_plan(payload["issue_data"]))
            if isinstance(review, dict):
                review["issue_plan"] = issue_plan
        return review if isinstance(review, dict) else {"review": review}

    @app.get("/api/git/register")
    async def api_git_register() -> Dict[str, Any]:
        reg = _to_serializable(state.git_manager.get_change_register())
        if isinstance(reg, dict) and "entries" in reg:
            return reg
        return {"register": reg if isinstance(reg, list) else [reg]}

    # ------------------------------------------------------------------
    # 6. Autonomous Engineering Tasks (7-Stage Lifecycle)
    # ------------------------------------------------------------------
    @app.post("/api/tasks/execute")
    async def api_tasks_execute(payload: Dict[str, Any]) -> Dict[str, Any]:
        prompt = str(payload.get("prompt") or payload.get("task") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="Task prompt is required")
        project_id = str(payload.get("project_id") or state.project_id)
        mode = str(payload.get("mode") or getattr(state.security_policy, "mode", state.security_mode))
        if hasattr(mode, "value"):
            mode = mode.value
        actions = payload.get("actions")
        test_command = payload.get("test_command")
        task_id = payload.get("task_id")

        wf = state.workflow
        sig = inspect.signature(wf.execute_task)
        kwargs: Dict[str, Any] = {"prompt": prompt}
        if "project_id" in sig.parameters:
            kwargs["project_id"] = project_id
        if "mode" in sig.parameters:
            kwargs["mode"] = mode
        if "actions" in sig.parameters and actions is not None:
            kwargs["actions"] = actions
        if "test_command" in sig.parameters and test_command is not None:
            kwargs["test_command"] = test_command
        if "task_id" in sig.parameters and task_id is not None:
            kwargs["task_id"] = task_id

        result = _to_serializable(wf.execute_task(**kwargs))
        if isinstance(result, dict):
            tid = str(result.get("task_id") or task_id or f"task_{int(time.time())}")
            result["task_id"] = tid
            if "completion_report" not in result and hasattr(wf, "generate_completion_report"):
                try:
                    result["completion_report"] = wf.generate_completion_report(result)
                except Exception:
                    pass
            state.tasks_store[tid] = result
            return result
        return {"status": "completed", "result": result}

    @app.post("/api/tasks/{task_id}/resume")
    async def api_tasks_resume(task_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        project_id = str(payload.get("project_id") or state.project_id)
        wf = state.workflow
        sig = inspect.signature(wf.resume_task)
        if "project_id" in sig.parameters and "task_id" in sig.parameters:
            res = _to_serializable(wf.resume_task(project_id=project_id, task_id=task_id))
        else:
            res = _to_serializable(wf.resume_task(task_id))
        if isinstance(res, dict):
            state.tasks_store[task_id] = res
            return res
        return {"task_id": task_id, "status": "resumed", "result": res}

    @app.get("/api/tasks")
    async def api_tasks_list(project_id: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        pid = project_id or state.project_id
        sessions: List[Any] = []
        try:
            sessions = _to_serializable(state.memory.list_sessions(project_id=pid))
        except Exception:
            sessions = []

        roles: List[Any] = []
        try:
            roles = _to_serializable(state.multi_agent.list_roles())
        except Exception:
            roles = []

        return {
            "project_id": pid,
            "tasks": list(state.tasks_store.values()),
            "sessions": sessions if isinstance(sessions, list) else [],
            "agent_roles": roles if isinstance(roles, list) else [],
        }

    @app.get("/api/tasks/{task_id}")
    async def api_tasks_get(task_id: str, project_id: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        if task_id in state.tasks_store:
            return state.tasks_store[task_id]
        pid = project_id or state.project_id
        try:
            loaded = _to_serializable(state.memory.load_session_state(pid, task_id))
            if loaded:
                return loaded if isinstance(loaded, dict) else {"task_id": task_id, "state": loaded}
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    # ------------------------------------------------------------------
    # 7. Terminal & QA (Browser & Test Suite)
    # ------------------------------------------------------------------
    @app.post("/api/terminal/execute")
    async def api_terminal_execute(payload: Dict[str, Any]) -> Dict[str, Any]:
        command = str(payload.get("command") or "").strip()
        if not command:
            raise HTTPException(status_code=400, detail="command is required")
        timeout = int(payload.get("timeout", 30))
        background = bool(payload.get("background", False))
        term = state.terminal

        if background and hasattr(term, "start_background_process"):
            bg_res = _to_serializable(term.start_background_process(command))
            return bg_res if isinstance(bg_res, dict) else {"background": True, "result": bg_res}

        if hasattr(term, "execute"):
            sig = inspect.signature(term.execute)
            kwargs: Dict[str, Any] = {"command": command}
            if "timeout" in sig.parameters:
                kwargs["timeout"] = timeout
            res = _to_serializable(term.execute(**kwargs))
        elif hasattr(term, "run_command"):
            res = _to_serializable(term.run_command(command, timeout=timeout))
        else:
            res = {"executed": False, "error": "Terminal executor method not found"}

        return res if isinstance(res, dict) else {"result": res}

    @app.post("/api/qa/browser")
    async def api_qa_browser(payload: Dict[str, Any]) -> Dict[str, Any]:
        target = payload.get("target") or payload.get("url") or payload.get("html_content") or ""
        html_content = payload.get("html_content") or (target if "<" in str(target) else "")
        viewports = payload.get("viewports") or ["mobile", "tablet", "desktop"]
        reference_spec = payload.get("reference_spec")

        qa = state.browser_qa
        inspection = _to_serializable(qa.inspect_html_or_url(target or "<div>BEL Code</div>", viewports=viewports))
        a11y = _to_serializable(qa.check_accessibility(html_content or str(target or "<main><h1>App</h1></main>")))
        visual = None
        if reference_spec is not None and hasattr(qa, "compare_visual_layout"):
            visual = _to_serializable(qa.compare_visual_layout(html_content or str(target), reference_spec))

        return {
            "inspection": inspection,
            "accessibility": a11y,
            "visual_comparison": visual,
            "viewports_checked": viewports,
        }

    @app.post("/api/qa/tests")
    async def api_qa_tests(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        command = payload.get("command")
        target = payload.get("target")
        failure_output = payload.get("failure_output")
        diagnose = bool(payload.get("diagnose", False))

        dbg = state.debugger
        if failure_output and not command and not diagnose:
            frames = _to_serializable(dbg.parse_stack_trace(str(failure_output)))
            return {"frames": frames}

        if diagnose and failure_output:
            repair_res = _to_serializable(
                dbg.diagnose_and_repair(
                    failure_output=str(failure_output),
                    target_file=target,
                    test_command=command,
                )
            )
            return repair_res if isinstance(repair_res, dict) else {"repair": repair_res}

        report = _to_serializable(dbg.run_test_suite(command=command, target=target))
        return report if isinstance(report, dict) else {"report": report}

    # ------------------------------------------------------------------
    # 8. Persistent Project-Isolated Engineering Memory
    # ------------------------------------------------------------------
    @app.get("/api/memory")
    async def api_memory_list(
        project_id: Optional[str] = Query(default=None),
        query: str = Query(default=""),
        q: Optional[str] = Query(default=None),
        category: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        pid = project_id or state.project_id
        search_q = q if q is not None else query
        items = _to_serializable(state.memory.search_memory(pid, query=search_q, category=category))
        return {
            "project_id": pid,
            "query": search_q,
            "category": category,
            "memories": items if isinstance(items, list) else [items],
        }

    @app.post("/api/memory")
    async def api_memory_add(payload: Dict[str, Any]) -> Dict[str, Any]:
        pid = str(payload.get("project_id") or state.project_id)
        category = str(payload.get("category") or "rule")
        title = str(payload.get("title") or "Engineering Note")
        content = str(payload.get("content") or "")
        metadata = payload.get("metadata")
        record = _to_serializable(
            state.memory.add_memory(
                project_id=pid,
                category=category,
                title=title,
                content=content,
                metadata=metadata,
            )
        )
        if isinstance(record, dict):
            return {"status": "saved", "memory": record, **record}
        return {"status": "saved", "memory_id": str(record), "project_id": pid}

    @app.delete("/api/memory/{memory_id}")
    async def api_memory_delete(
        memory_id: str,
        project_id: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        pid = project_id or state.project_id
        res = _to_serializable(state.memory.delete_memory(pid, memory_id))
        return {
            "status": "deleted",
            "memory_id": memory_id,
            "project_id": pid,
            "deleted": res,
        }

    @app.get("/api/memory/changelog")
    async def api_memory_changelog(project_id: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        pid = project_id or state.project_id
        entries = _to_serializable(state.memory.get_changelog(pid))
        return {
            "project_id": pid,
            "changelog": entries if isinstance(entries, list) else [],
        }

    @app.post("/api/memory/changelog")
    async def api_memory_add_changelog(payload: Dict[str, Any]) -> Dict[str, Any]:
        pid = str(payload.get("project_id") or state.project_id)
        entry = _to_serializable(state.memory.append_changelog(pid, payload))
        return {"status": "recorded", "project_id": pid, "entry": entry}

    @app.get("/api/memory/export")
    async def api_memory_export(project_id: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        pid = project_id or state.project_id
        exported = _to_serializable(state.memory.export_project_memory(pid))
        if isinstance(exported, dict):
            exported.setdefault("project_id", pid)
            return exported
        return {"project_id": pid, "export": exported}

    # ------------------------------------------------------------------
    # 9. Advanced Capabilities & Extensibility (P1–P3)
    # ------------------------------------------------------------------
    @app.get("/api/advanced/skills")
    async def api_advanced_get_skills() -> Dict[str, Any]:
        adv = state.advanced
        skills = _to_serializable(adv.list_skills()) if hasattr(adv, "list_skills") else []
        templates = _to_serializable(adv.list_templates()) if hasattr(adv, "list_templates") else []
        integrations = _to_serializable(adv.list_integrations()) if hasattr(adv, "list_integrations") else []
        return {
            "skills": skills if isinstance(skills, list) else [],
            "templates": templates if isinstance(templates, list) else [],
            "integrations": integrations if isinstance(integrations, list) else [],
        }

    @app.post("/api/advanced/skills")
    async def api_advanced_post_skills(payload: Dict[str, Any]) -> Dict[str, Any]:
        adv = state.advanced
        action = str(payload.get("action") or "register").lower()
        if action == "execute":
            name = str(payload.get("name") or "")
            ctx = payload.get("context") or {}
            res = _to_serializable(adv.execute_skill(name, context=ctx))
            return {"status": "executed", "result": res}
        if action == "scaffold":
            tpl = str(payload.get("template_name") or payload.get("name") or "fastapi_service")
            target_dir = payload.get("target_dir") or str(state.workspace_root / tpl)
            res = _to_serializable(adv.scaffold_template(tpl, target_dir))
            return {"status": "scaffolded", "result": res}
        if action == "register_mcp":
            name = str(payload.get("name") or "custom_mcp")
            endpoint = str(payload.get("endpoint") or payload.get("endpoint_or_cmd") or "stdio://mcp")
            scopes = payload.get("scopes") or ["read"]
            res = _to_serializable(adv.register_mcp_server(name, endpoint, scopes))
            return {"status": "registered_mcp", "result": res}

        name = str(payload.get("name") or "custom_skill")
        description = str(payload.get("description") or "")
        steps = payload.get("steps") or ["Analyze", "Execute", "Verify"]
        prompt_template = str(payload.get("prompt_template") or "")
        skill = _to_serializable(
            adv.register_skill(
                name=name,
                description=description,
                steps=steps,
                prompt_template=prompt_template,
            )
        )
        return {"status": "registered", "skill": skill}

    @app.post("/api/advanced/security-scan")
    async def api_advanced_security_scan() -> Dict[str, Any]:
        res = _to_serializable(state.advanced.run_security_scan())
        return res if isinstance(res, dict) else {"scan": res}

    @app.post("/api/advanced/migrate")
    async def api_advanced_migrate(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        migration_type = str(payload.get("migration_type") or "python2_to_3")
        target_files = payload.get("target_files")
        dry_run = bool(payload.get("dry_run", False))
        res = _to_serializable(
            state.advanced.migrate_codebase(
                migration_type=migration_type,
                target_files=target_files,
                dry_run=dry_run,
            )
        )
        return res if isinstance(res, dict) else {"migration": res}

    @app.get("/api/advanced/openapi")
    async def api_advanced_openapi() -> Dict[str, Any]:
        res = _to_serializable(state.advanced.generate_openapi_spec())
        return res if isinstance(res, dict) else {"openapi": res}

    @app.post("/api/advanced/database")
    async def api_advanced_database(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        schema = str(
            payload.get("schema_sql_or_models")
            or payload.get("schema")
            or payload.get("sql")
            or "CREATE TABLE users (id INT PRIMARY KEY, email VARCHAR(255), created_at TIMESTAMP);"
        )
        res = _to_serializable(state.advanced.analyze_database_schema(schema))
        return res if isinstance(res, dict) else {"database_analysis": res}

    @app.post("/api/advanced/profile")
    async def api_advanced_profile(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        target_script = payload.get("target_script") or payload.get("file")
        res = _to_serializable(state.advanced.profile_performance(target_script=target_script))
        return res if isinstance(res, dict) else {"profile": res}

    @app.post("/api/advanced/cicd")
    async def api_advanced_cicd(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        platform = str(payload.get("platform") or "github_actions")
        res = _to_serializable(state.advanced.generate_cicd_pipeline(platform=platform))
        return res if isinstance(res, dict) else {"cicd": res}

    @app.post("/api/advanced/image-to-ui")
    async def api_advanced_image_to_ui(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        design_spec = payload.get("design_spec") if isinstance(payload.get("design_spec"), dict) else payload
        res = _to_serializable(state.advanced.image_to_interface(design_spec))
        return res if isinstance(res, dict) else {"ui": res}

    @app.post("/api/advanced/self-evaluate")
    async def api_advanced_self_evaluate(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        history = payload.get("task_history")
        if not isinstance(history, list):
            history = list(state.tasks_store.values())
        res = _to_serializable(state.advanced.evaluate_agent_history(history))
        return res if isinstance(res, dict) else {"evaluation": res}

    # ------------------------------------------------------------------
    # 10. Acceptance Benchmarks (Spec Section 18)
    # ------------------------------------------------------------------
    @app.get("/api/benchmarks")
    async def api_benchmarks_catalog() -> Dict[str, Any]:
        return {
            "total": len(ACCEPTANCE_BENCHMARK_CATALOG),
            "benchmarks": ACCEPTANCE_BENCHMARK_CATALOG,
        }

    @app.post("/api/benchmarks/run")
    async def api_benchmarks_run(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = payload or {}
        benchmark_id = str(payload.get("benchmark_id") or payload.get("id") or "all").strip()
        wf = state.workflow
        if benchmark_id.lower() in ("all", "*"):
            res = _to_serializable(wf.run_all_benchmarks())
        else:
            res = _to_serializable(wf.run_acceptance_benchmark(benchmark_id))
        return res if isinstance(res, dict) else {"benchmark_id": benchmark_id, "result": res}

    return app


app = create_app()
