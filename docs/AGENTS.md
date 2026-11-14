# Agents, Roles & Autonomous Workflow

BEL CODE combines **OpenCode Foundation agents** (Layer 1) with **9 specialized engineering roles**, **disjoint file ownership locks**, and a **7-stage autonomous lifecycle** (Layer 2).

---

## 1. OpenCode Foundation Agents (`opencode.json` & `runtime.py`)

In the AI Workspace composer and `GET /api/opencode/agents`, BEL CODE exposes primary and subagent profiles configured in `opencode.json`:

| Agent ID | Mode | Default Permissions | Purpose |
| :--- | :--- | :--- | :--- |
| `build` | Primary | `edit: allow`, `bash: allow`, `webfetch: allow` | Full-access software engineering agent for implementing, editing, testing, and verifying code |
| `plan` | Primary | `edit: deny`, `bash: ask`, `webfetch: allow` | Read-only architecture and planning agent for codebase analysis and staged execution plans |
| `general` | Subagent | `edit: allow`, `bash: allow`, `webfetch: allow` | General-purpose subagent for multi-step research and execution |
| `explore` | Subagent | `edit: deny`, `bash: deny`, `webfetch: deny` | Fast read-only subagent for globbing files, reading code, and searching symbols |
| `architect` | Subagent | `edit: deny`, `bash: deny`, `webfetch: allow` | System architect subagent for module boundaries, API contracts, and dependency graphs |
| `reviewer` | Subagent | `edit: deny`, `bash: deny`, `webfetch: deny` | Code review subagent for inspecting diffs, correctness, edge cases, and maintainability |
| `debugger` | Subagent | `edit: allow`, `bash: allow`, `webfetch: deny` | Debugging subagent for stack trace analysis, fault localization, and minimal verified fixes |
| `security_auditor` | Subagent | `edit: deny`, `bash: deny`, `webfetch: deny` | Security auditor subagent for secret scanning, injection detection, and permission boundary audits |

---

## 2. Specialized Multi-Agent Roles (`bel_code/engine/multi_agent.py`)

`MultiAgentCoordinator` defines **9 specialized roles** (`SPECIALIZED_ROLES`), each mapped to a provider routing `task_type`:

| Role | Task Type | Core Capabilities |
| :--- | :--- | :--- |
| **Architect** | `architecture_reasoning` | Architecture design, dependency analysis, interface specification, task decomposition |
| **Developer** | `large_scale_code` | Code generation, refactoring, file editing within assigned file boundaries |
| **Debugger** | `debugging` | Stack trace analysis, fault localization, minimal patch generation |
| **Tester** | `routine_edits` | Test generation, test execution, coverage analysis |
| **Security Auditor** | `security_review` | Secret scanning, vulnerability detection, dependency audit, permission review |
| **Code Reviewer** | `code_review` | Diff inspection, style enforcement, maintainability review |
| **Researcher** | `general` | Symbol indexing, context retrieval, documentation lookup |
| **Documentation Agent** | `documentation` | README generation, API documentation, changelog authoring |
| **Performance Engineer** | `architecture_reasoning` | CPU profiling, complexity analysis, query optimization |

### File Ownership Locks

To prevent conflicting concurrent edits during multi-agent collaboration, `MultiAgentCoordinator` enforces disjoint `owned_files` locks: a subagent cannot acquire a lock on a file already owned by another active subagent, and mutations are checked against declared ownership boundaries.

---

## 3. Seven-Stage Autonomous Engineering Lifecycle (`bel_code/engine/workflow.py`)

`AutonomousWorkflowEngine` structures engineering tasks through seven canonical stages (`CANONICAL_STAGES`):

1. **`understand`** — Scans the repository, resolves relevant files/symbols, and loads project memory.
2. **`plan`** — Produces a concrete staged execution plan and identifies target files.
3. **`implement`** — Applies bounded file edits and creates git/file checkpoints.
4. **`test`** — Runs real test commands via `TestAndDebugEngine` (`verified=True` only when actual command execution returns exit code `0`).
5. **`repair`** — Localizes stack trace faults and tests minimal candidate patches (rolling back regressions).
6. **`review`** — Performs diff inspection, secret scanning, and code quality checks.
7. **`deliver`** — Records session state, changelog entries, and verified output summary.

Long-running tasks persist checkpointed stage state so interrupted tasks can resume via `POST /api/tasks/{task_id}/resume` without repeating already-verified stages.

---

## 4. Security & Permission Modes

Every action in the workspace is governed by `SecurityPolicy` (`bel_code/security/policy.py`), selectable in the composer or via `POST /api/security/mode`:

- **`ask`**: Requires explicit human approval before file writes or shell commands.
- **`supervised`** *(default)*: Allows low-risk workspace operations while requiring human approval for high/critical-risk commands (`rm -rf`, `git push --force`, etc.).
- **`autonomous`**: Permits pre-authorized workspace edits and tests within workspace path boundaries while still enforcing critical destructive command blocks and workspace path confinement.
