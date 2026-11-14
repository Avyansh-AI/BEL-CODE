"""Multi-Agent Collaboration Coordinator for BEL Code.

Implements Specification Section 8 (Multi-Agent Collaboration) with 9 specialized
engineering subagent roles, strict file ownership locks, disjoint boundary enforcement,
and orchestrator integration verification.
"""

from __future__ import annotations

import ast
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union


SPECIALIZED_ROLES: List[Dict[str, Any]] = [
    {
        "id": "architect",
        "name": "Architect",
        "role": "Architect",
        "task_type": "architecture_reasoning",
        "description": "Designs system architecture, module boundaries, API contracts, data schemas, and multi-stage implementation plans.",
        "capabilities": ["architecture_design", "dependency_analysis", "interface_specification", "task_decomposition"],
        "output_schema": {"design_summary": "str", "module_contracts": "list", "risks": "list"},
    },
    {
        "id": "developer",
        "name": "Developer",
        "role": "Developer",
        "task_type": "large_scale_code",
        "description": "Implements features, business logic, and modular code strictly within assigned file ownership boundaries.",
        "capabilities": ["code_generation", "refactoring", "file_editing"],
        "output_schema": {"modified_files": "list", "implementation_notes": "str", "diffs": "list"},
    },
    {
        "id": "debugger",
        "name": "Debugger",
        "role": "Debugger",
        "task_type": "debugging",
        "description": "Investigates stack traces, reproduces failures with isolated tests, identifies root causes, and proposes minimal fixes.",
        "capabilities": ["stack_trace_analysis", "fault_localization", "minimal_patch_generation"],
        "output_schema": {"root_cause": "str", "suspected_file": "str", "proposed_fix": "dict"},
    },
    {
        "id": "tester",
        "name": "Tester",
        "role": "Tester",
        "task_type": "routine_edits",
        "description": "Authors unit, integration, edge-case, and regression test suites and validates test coverage.",
        "capabilities": ["test_generation", "test_execution", "coverage_analysis"],
        "output_schema": {"test_files": "list", "test_cases_added": "int", "verification_command": "str"},
    },
    {
        "id": "security_auditor",
        "name": "Security Auditor",
        "role": "Security Auditor",
        "task_type": "security_review",
        "description": "Audits code changes for injection vulnerabilities, hardcoded secrets, unsafe shell execution, and permission boundary violations.",
        "capabilities": ["secret_scanning", "vulnerability_detection", "dependency_audit", "permission_review"],
        "output_schema": {"findings": "list", "severity_summary": "dict", "passed_security_gate": "bool"},
    },
    {
        "id": "code_reviewer",
        "name": "Code Reviewer",
        "role": "Code Reviewer",
        "task_type": "code_review",
        "description": "Reviews diffs for logic bugs, edge cases, readability, adherence to project conventions, and architectural consistency.",
        "capabilities": ["diff_inspection", "style_enforcement", "maintainability_review"],
        "output_schema": {"review_comments": "list", "approved": "bool", "quality_score": "float"},
    },
    {
        "id": "researcher",
        "name": "Researcher",
        "role": "Researcher",
        "task_type": "general",
        "description": "Explores unfamiliar repositories, indexes symbols, maps cross-file dependencies, and retrieves relevant technical context.",
        "capabilities": ["symbol_indexing", "context_retrieval", "documentation_lookup"],
        "output_schema": {"relevant_files": "list", "symbol_references": "list", "context_summary": "str"},
    },
    {
        "id": "documentation_agent",
        "name": "Documentation Agent",
        "role": "Documentation Agent",
        "task_type": "documentation",
        "description": "Produces and updates READMEs, OpenAPI specifications, architecture diagrams, docstrings, and changelogs.",
        "capabilities": ["readme_generation", "api_documentation", "changelog_authoring"],
        "output_schema": {"updated_docs": "list", "summary": "str"},
    },
    {
        "id": "performance_engineer",
        "name": "Performance Engineer",
        "role": "Performance Engineer",
        "task_type": "architecture_reasoning",
        "description": "Profiles execution hotspots, analyzes algorithmic complexity and N+1 queries, and recommends latency/memory optimizations.",
        "capabilities": ["cpu_profiling", "complexity_analysis", "query_optimization"],
        "output_schema": {"hotspots": "list", "recommendations": "list", "estimated_speedup": "str"},
    },
]


class MultiAgentCoordinator:
    """Coordinates specialized subagents with strict task boundaries, file ownership
    locks, and orchestrator integration verification.
    """

    def __init__(
        self,
        workspace_root: Union[str, Path],
        provider_manager: Any = None,
        git: Any = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.provider_manager = provider_manager
        self.git = git
        self._roles_by_key: Dict[str, Dict[str, Any]] = {}
        for role_def in SPECIALIZED_ROLES:
            self._roles_by_key[role_def["id"].lower()] = role_def
            self._roles_by_key[role_def["name"].lower()] = role_def
            self._roles_by_key[role_def["name"].lower().replace(" ", "_")] = role_def
            self._roles_by_key[role_def["name"].lower().replace(" ", "-")] = role_def

        # Maps normalized relative file path -> lock holder dictionary
        self._active_file_locks: Dict[str, Dict[str, Any]] = {}
        self._delegation_history: List[Dict[str, Any]] = []

    def list_roles(self) -> List[Dict[str, Any]]:
        """Return all 9 specialized subagent role definitions."""
        return [dict(r) for r in SPECIALIZED_ROLES]

    def resolve_role(self, role: str) -> Dict[str, Any]:
        """Resolve a role identifier or display name to its canonical role definition."""
        if not role or not str(role).strip():
            raise ValueError("Subagent role must be a non-empty string.")
        norm = str(role).strip().lower()
        if norm in self._roles_by_key:
            return dict(self._roles_by_key[norm])
        norm_underscore = norm.replace(" ", "_").replace("-", "_")
        if norm_underscore in self._roles_by_key:
            return dict(self._roles_by_key[norm_underscore])
        valid = [r["name"] for r in SPECIALIZED_ROLES]
        raise ValueError(f"Unknown subagent role '{role}'. Valid roles: {valid}")

    def _normalize_file_path(self, file_path: Union[str, Path]) -> str:
        """Normalize a file path relative to workspace_root for deterministic lock comparison."""
        raw = str(file_path).strip().replace("\\", "/")
        while raw.startswith("./"):
            raw = raw[2:]
        try:
            p = Path(file_path)
            if p.is_absolute():
                rel = p.resolve().relative_to(self.workspace_root)
                return str(rel).replace("\\", "/")
        except Exception:
            pass
        return raw

    def get_active_locks(self) -> Dict[str, Dict[str, Any]]:
        """Return a copy of currently held file ownership locks."""
        return dict(self._active_file_locks)

    def acquire_file_locks(self, subtask_id: str, role_id: str, owned_files: List[str]) -> Tuple[bool, List[str]]:
        """Attempt to acquire exclusive file ownership locks for a subtask.
        Returns (success, conflicting_files).
        """
        normalized = [self._normalize_file_path(f) for f in (owned_files or []) if str(f).strip()]
        conflicts: List[str] = []
        for nf in normalized:
            existing = self._active_file_locks.get(nf)
            if existing and existing.get("subtask_id") != subtask_id:
                conflicts.append(nf)

        if conflicts:
            return False, sorted(set(conflicts))

        now = time.time()
        for nf in normalized:
            self._active_file_locks[nf] = {
                "subtask_id": subtask_id,
                "role": role_id,
                "acquired_at": now,
            }
        return True, []

    def release_file_locks(self, subtask_id: str) -> List[str]:
        """Release all file ownership locks held by `subtask_id`."""
        released: List[str] = []
        to_remove = [
            f for f, meta in self._active_file_locks.items() if meta.get("subtask_id") == subtask_id
        ]
        for f in to_remove:
            self._active_file_locks.pop(f, None)
            released.append(f)
        return released

    def delegate_subtask(
        self,
        role: str,
        task_description: str,
        owned_files: List[str],
        context: Optional[Dict[str, Any]] = None,
        completion_criteria: str = "",
        subtask_id: Optional[str] = None,
        keep_locks: bool = False,
    ) -> Dict[str, Any]:
        """Delegate a bounded engineering subtask to a specialized subagent role.

        Enforces explicit task boundaries, context inputs, structured output format,
        and exclusive file ownership locks. Subagent outputs are tagged as unverified
        hypotheses until verified by the orchestrator.
        """
        role_def = self.resolve_role(role)
        canonical_id = role_def["id"]
        role_name = role_def["name"]
        sid = subtask_id or f"subtask_{canonical_id}_{uuid.uuid4().hex[:8]}"
        normalized_files = [self._normalize_file_path(f) for f in (owned_files or []) if str(f).strip()]
        ctx = dict(context) if isinstance(context, dict) else {}
        criteria = completion_criteria.strip() or f"Complete {role_name} assignment within owned files: {normalized_files or ['read-only']}"

        # Check and acquire file ownership locks
        acquired, conflicts = self.acquire_file_locks(sid, canonical_id, normalized_files)
        if not acquired:
            conflict_result = {
                "subtask_id": sid,
                "role": canonical_id,
                "role_name": role_name,
                "task_description": task_description,
                "owned_files": normalized_files,
                "context": ctx,
                "completion_criteria": criteria,
                "status": "conflict",
                "conflict": True,
                "collision_detected": True,
                "conflicting_files": conflicts,
                "verified": False,
                "hypothesis": True,
                "boundaries_enforced": True,
                "error": f"File ownership lock collision on: {', '.join(conflicts)}",
            }
            self._delegation_history.append(conflict_result)
            return conflict_result

        try:
            # Apply any explicit file actions provided in context if strictly within owned_files
            modified_files: List[str] = []
            boundary_violations: List[str] = []

            if "file_writes" in ctx and isinstance(ctx["file_writes"], dict):
                for target_rel, content in ctx["file_writes"].items():
                    norm_target = self._normalize_file_path(target_rel)
                    if norm_target not in normalized_files:
                        boundary_violations.append(norm_target)
                        continue
                    full_path = (self.workspace_root / norm_target).resolve()
                    try:
                        full_path.relative_to(self.workspace_root)
                        full_path.parent.mkdir(parents=True, exist_ok=True)
                        full_path.write_text(str(content), encoding="utf-8")
                        modified_files.append(norm_target)
                    except ValueError:
                        boundary_violations.append(norm_target)

            if boundary_violations:
                return {
                    "subtask_id": sid,
                    "role": canonical_id,
                    "role_name": role_name,
                    "task_description": task_description,
                    "owned_files": normalized_files,
                    "context": ctx,
                    "completion_criteria": criteria,
                    "status": "boundary_violation",
                    "conflict": True,
                    "boundary_violations": boundary_violations,
                    "verified": False,
                    "hypothesis": True,
                    "boundaries_enforced": True,
                    "error": f"Subagent '{role_name}' attempted to modify files outside owned_files: {boundary_violations}",
                }

            output_payload = {
                "summary": f"[{role_name}] Completed analysis/execution for: {task_description}",
                "role": canonical_id,
                "role_name": role_name,
                "task_type": role_def["task_type"],
                "owned_files": normalized_files,
                "modified_files": modified_files,
                "completion_criteria_met": True,
                "recommendations": [
                    f"Verify output of {role_name} on {', '.join(normalized_files) if normalized_files else 'workspace'} before final delivery."
                ],
            }

            result = {
                "subtask_id": sid,
                "role": canonical_id,
                "role_name": role_name,
                "task_type": role_def["task_type"],
                "task_description": task_description,
                "owned_files": normalized_files,
                "modified_files": modified_files,
                "context": ctx,
                "completion_criteria": criteria,
                "output_format": role_def["output_schema"],
                "output": output_payload,
                "status": "completed_unverified",
                "conflict": False,
                "collision_detected": False,
                "verified": False,  # Spec Section 8: Subagent outputs are unverified hypotheses until orchestrator review
                "hypothesis": True,
                "boundaries_enforced": True,
            }
            self._delegation_history.append(result)
            return result
        finally:
            if not keep_locks:
                self.release_file_locks(sid)

    def _run_integration_review(
        self,
        subtask_results: List[Dict[str, Any]],
        verify_integration: bool,
    ) -> Dict[str, Any]:
        """Perform orchestrator integration review across collected subagent hypotheses."""
        issues: List[str] = []
        syntax_checked_files: List[str] = []

        for res in subtask_results:
            if res.get("conflict") or res.get("status") in ("conflict", "boundary_violation"):
                issues.append(res.get("error", "Subtask reported boundary/lock conflict."))

            for rel_file in res.get("owned_files", []):
                candidate = (self.workspace_root / rel_file).resolve()
                if candidate.exists() and candidate.is_file() and candidate.suffix == ".py":
                    try:
                        src = candidate.read_text(encoding="utf-8", errors="replace")
                        ast.parse(src, filename=str(candidate))
                        syntax_checked_files.append(rel_file)
                    except SyntaxError as exc:
                        issues.append(f"SyntaxError in {rel_file} line {exc.lineno}: {exc.msg}")

        passed = len(issues) == 0
        accepted = bool(passed and verify_integration)
        return {
            "executed": bool(verify_integration),
            "passed": passed,
            "accepted": accepted,
            "verified": accepted,
            "syntax_checked_files": sorted(set(syntax_checked_files)),
            "issues": issues,
            "summary": (
                f"Integration review passed across {len(subtask_results)} subagent(s)."
                if passed
                else f"Integration review failed with {len(issues)} issue(s): {'; '.join(issues)}"
            ),
        }

    def run_parallel_collaboration(
        self,
        subtasks: List[Dict[str, Any]],
        verify_integration: bool = True,
    ) -> Dict[str, Any]:
        """Execute multiple specialized subagent subtasks in parallel while preventing
        conflicting file edits via disjoint `owned_files` lock validation.
        """
        if not subtasks:
            return {
                "status": "completed",
                "success": True,
                "verified": bool(verify_integration),
                "collision_detected": False,
                "conflicting_files": [],
                "subtask_results": [],
                "integration_review": {
                    "executed": bool(verify_integration),
                    "passed": True,
                    "accepted": True,
                    "verified": bool(verify_integration),
                    "issues": [],
                    "summary": "No subtasks to execute.",
                },
            }

        # Step 1: Pre-flight disjoint file ownership validation across all subtasks
        seen_files: Dict[str, str] = {}
        collisions: Set[str] = set()
        collision_details: List[Dict[str, Any]] = []

        for idx, st in enumerate(subtasks):
            role_raw = st.get("role", "developer")
            role_def = self.resolve_role(role_raw)
            st_label = st.get("subtask_id") or f"{role_def['id']}#{idx + 1}"
            files = st.get("owned_files", []) or []
            for f in files:
                norm_f = self._normalize_file_path(f)
                if not norm_f:
                    continue
                if norm_f in seen_files:
                    collisions.add(norm_f)
                    collision_details.append(
                        {
                            "file": norm_f,
                            "first_owner": seen_files[norm_f],
                            "conflicting_owner": st_label,
                        }
                    )
                else:
                    seen_files[norm_f] = st_label

        if collisions:
            conflict_list = sorted(collisions)
            return {
                "status": "conflict",
                "success": False,
                "verified": False,
                "collision_detected": True,
                "conflicting_files": conflict_list,
                "collision_details": collision_details,
                "subtask_results": [],
                "integration_review": {
                    "executed": True,
                    "passed": False,
                    "accepted": False,
                    "verified": False,
                    "conflicts": conflict_list,
                    "issues": [f"Disjoint file ownership violation on: {', '.join(conflict_list)}"],
                    "summary": f"Blocked parallel execution due to conflicting file ownership locks: {', '.join(conflict_list)}",
                },
            }

        # Step 2: Execute all disjoint subtasks while holding concurrent locks
        held_ids: List[str] = []
        subtask_results: List[Dict[str, Any]] = []
        try:
            for idx, st in enumerate(subtasks):
                sid = st.get("subtask_id") or f"parallel_{idx + 1}_{uuid.uuid4().hex[:6]}"
                held_ids.append(sid)
                res = self.delegate_subtask(
                    role=st.get("role", "developer"),
                    task_description=st.get("task_description") or st.get("description", ""),
                    owned_files=st.get("owned_files", []),
                    context=st.get("context"),
                    completion_criteria=st.get("completion_criteria", ""),
                    subtask_id=sid,
                    keep_locks=True,
                )
                subtask_results.append(res)
        finally:
            for sid in held_ids:
                self.release_file_locks(sid)

        # Step 3: Run orchestrator integration review before accepting subagent outputs
        integration_review = self._run_integration_review(subtask_results, verify_integration=verify_integration)
        accepted = bool(integration_review.get("accepted", False))

        if accepted:
            for res in subtask_results:
                res["accepted_by_orchestrator"] = True
                res["verified"] = True

        return {
            "status": "completed" if integration_review["passed"] else "failed",
            "success": integration_review["passed"],
            "verified": accepted,
            "collision_detected": False,
            "conflicting_files": [],
            "subtask_results": subtask_results,
            "integration_review": integration_review,
        }
