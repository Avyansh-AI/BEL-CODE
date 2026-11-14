"""Tests verifying the Google Antigravity-inspired AI-first BEL CODE UI and IDE mode + backend APIs."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from bel_code.server.app import app


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "bel_code" / "frontend"


def _read_frontend_bundle() -> tuple[str, str, str, str]:
    index_html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8", errors="replace")
    styles_css = (FRONTEND_DIR / "styles.css").read_text(encoding="utf-8", errors="replace")
    app_js = (FRONTEND_DIR / "app.js").read_text(encoding="utf-8", errors="replace")
    combined = f"{index_html}\n{styles_css}\n{app_js}"
    return index_html, styles_css, app_js, combined


def test_ai_first_default_workspace_and_sidebar():
    """Verify default view is the spacious Antigravity-inspired AI workspace with grouped projects & New Conversation."""
    index_html, styles_css, app_js, _ = _read_frontend_bundle()

    assert 'data-mode="ai"' in index_html
    assert "Welcome to BEL CODE" in index_html
    assert "Your AI development environment. What shall we build today?" in index_html
    assert "New Conversation" in index_html
    assert "Projects" in index_html
    assert "Settings" in index_html
    assert "Open IDE" in index_html
    assert "sidebar-projects-tree" in index_html

    # Sidebar ~275px on desktop & IDE mode hidden by default
    assert "274px" in styles_css or "275px" in styles_css
    assert 'id="ide-mode-container" class="ide-mode-shell hidden"' in index_html
    assert 'setWorkspaceMode("ai")' in app_js


def test_floating_ai_composer_and_controls():
    """Verify floating bottom-center composer includes project, env, attachment, model, reasoning, agent, voice & send."""
    index_html, _, app_js, _ = _read_frontend_bundle()

    assert "Ask BEL CODE to build, modify, debug or improve anything..." in index_html
    for id_marker in (
        "ai-composer-card",
        "composer-project-select",
        "composer-env-select",
        "task-composer-input",
        "btn-open-attach",
        "composer-model-select",
        "composer-reasoning-select",
        "composer-agent-select",
        "composer-permission-mode",
        "btn-voice-input",
        "btn-execute-task",
        "btn-stop-execution",
    ):
        assert id_marker in index_html, f"Missing composer control '#{id_marker}'"

    assert "submitPrompt" in app_js
    assert "exitCode ?? 0" not in app_js


def test_on_demand_ide_mode_and_minimal_modals():
    """Verify full IDE mode (Explorer, Editor, Diff, Terminal, Problems, Git, Debug) and Agents/Skills/Settings modals."""
    index_html, _, _, _ = _read_frontend_bundle()

    for ide_id in (
        "ide-mode-container",
        "ide-file-tree",
        "ide-tabs-strip",
        "editor-textarea",
        "editor-diff-table",
        "terminal-cmd-input",
        "bottom-problems-list",
        "bottom-logs-list",
    ):
        assert ide_id in index_html, f"Missing IDE mode component '#{ide_id}'"

    for modal_id in ("modal-agents", "modal-skills", "modal-settings"):
        assert modal_id in index_html, f"Missing on-demand modal '#{modal_id}'"


def test_restrained_antigravity_palette_and_typography():
    """Verify deep-black canvas (#050507), charcoal chrome (#0e0f12), Inter + JetBrains Mono fonts, zero gradients."""
    _, styles_css, _, combined = _read_frontend_bundle()
    styles_lower = styles_css.lower()
    assert "Inter" in combined and "JetBrains Mono" in combined
    assert "#050507" in styles_lower and "#0e0f12" in styles_lower
    assert "linear-gradient" not in styles_lower
    assert "backdrop-filter" not in styles_lower


def test_workspace_file_search_problems_and_logs_endpoints():
    """Verify backend endpoints remain fully functional alongside redesigned UI."""
    client = TestClient(app)

    assert client.get("/api/files/tree").status_code == 200
    read_res = client.get("/api/files/read", params={"path": "README.md"})
    assert read_res.status_code == 200 and "BEL Code" in str(read_res.json())
    assert "matches" in client.get("/api/files/search", params={"q": "BEL Code"}).json()
    assert client.get("/api/problems").status_code == 200

    logs_data = client.get("/api/logs").json()
    assert "audit_logs" in logs_data and "changelog" in logs_data

    proj_data = client.get("/api/projects/current").json()
    for field in ("git_branch", "git_clean", "modified_count", "recent_projects"):
        assert field in proj_data
