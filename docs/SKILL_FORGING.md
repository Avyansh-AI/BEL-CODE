# Skills, Skill Forging & Workspace Analyzers

BEL CODE includes an extensibility and automation layer in `bel_code/advanced/capabilities.py` (`SkillsAndCapabilities`), accessible from **Skills & Customizations** (`Skills & Forge`) in the UI and via `/api/advanced/*` endpoints.

---

## 1. Built-in Skills

BEL CODE ships with four built-in reusable skill workflows:

| Skill Name | Purpose |
| :--- | :--- |
| `security_audit` | Scan source files for hardcoded credentials, SQL injection patterns, and unsafe command/deserialization boundaries |
| `api_endpoint_scaffold` | Design Pydantic request/response models, route handler, router registration, and test cases |
| `legacy_modernization` | Upgrade legacy Python or frontend components with AST compatibility checks and regression verification |
| `pr_readiness_review` | Run hooks, execute unit tests, scan diffs for secrets, and produce a structured PR readiness summary |

---

## 2. Forging Custom Skills

You can forge (register) new reusable skills either in the UI or via HTTP API:

### Via the Web UI
1. Click **Skills & Customizations** in the left sidebar to open **Skills & Forge**.
2. Enter a **Skill name** (e.g., `release_checklist`) and **Description**.
3. Click **Forge Skill** to register it, then click **Run** beside any skill to execute its workflow steps.

### Via API (`POST /api/advanced/skills`)

Register a skill:

```json
{
  "action": "register",
  "name": "release_checklist",
  "description": "Verify tests, secret hygiene, and changelog before tagging a release.",
  "steps": [
    "Run pytest -v",
    "Scan repository for accidental credentials",
    "Verify CHANGELOG.md is updated"
  ],
  "prompt_template": "Run release verification for {target}."
}
```

Execute a skill:

```json
{
  "action": "execute",
  "name": "release_checklist",
  "context": {
    "target": "v2.1.0"
  }
}
```

Skills trigger `pre_skill` and `post_skill` lifecycle hooks if registered via `register_hook`.

---

## 3. Project Scaffolding Templates & MCP Registry

`SkillsAndCapabilities` also provides:
- **Built-in Templates** (`action: "scaffold"` on `POST /api/advanced/skills`):
  - `fastapi_service`
  - `python_cli`
  - `react_component`
  - `filesystem_mcp`
  - `postgres_mcp`
- **MCP Server Registration** (`action: "register_mcp"` on `POST /api/advanced/skills`): Register stdio/HTTP MCP servers and inspect active integrations via `GET /api/advanced/skills` or `GET /api/opencode/mcp`.

---

## 4. Workspace Analyzers (`/api/advanced/*`)

The **Workspace Analyzers** bar inside **Skills & Forge** exposes five on-demand engineering analyzers implemented in `bel_code/advanced/capabilities.py`:

| Analyzer | Endpoint | What It Inspects |
| :--- | :--- | :--- |
| **Security Scan** | `POST /api/advanced/security-scan` | Static SAST scan for hardcoded secrets, `eval`/`exec`, `shell=True`, SQL string interpolation, and unsafe patterns |
| **Database** | `POST /api/advanced/database` | Inspects SQL/ORM models, schema migrations, and missing index candidates |
| **Profile** | `POST /api/advanced/profile` | Analyzes CPU/complexity hotspots and potential performance bottlenecks |
| **CI/CD** | `POST /api/advanced/cicd` | Generates CI/CD workflow templates tailored to the detected repository stack |
| **Self-Evaluate** | `POST /api/advanced/self-evaluate` | Computes measured evaluation metrics from recorded task/verification history |

Additional API endpoints implemented in `bel_code/server/app.py`:
- `POST /api/advanced/migrate` — Legacy codebase migration helper (e.g., Python 2 -> Python 3)
- `GET /api/advanced/openapi` — OpenAPI 3.0 specification and client SDK generator
- `POST /api/advanced/image-to-ui` — Wireframe/mockup specification to HTML/Tailwind UI generator
