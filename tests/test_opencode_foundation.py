"""Tests verifying the OpenCode Foundation (Layer 1), configuration, bridge, and API routes."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from bel_code.server.app import app


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENDOR_OPENCODE = PROJECT_ROOT / "vendor" / "opencode"


def test_vendor_opencode_genuine_clone_and_remotes():
    """Verify vendor/opencode exists, is a genuine clone of anomalyco/opencode, and has upstream remote."""
    assert VENDOR_OPENCODE.exists() and VENDOR_OPENCODE.is_dir(), "vendor/opencode directory must exist"
    assert (VENDOR_OPENCODE / ".git").exists(), "vendor/opencode must be a git repository"

    # Required upstream packages and files (supports upstream v2.3 core/server/sdk and v1 opencode/sdk/js)
    has_v2_core = (VENDOR_OPENCODE / "packages" / "core" / "src").is_dir()
    has_v1_core = (VENDOR_OPENCODE / "packages" / "opencode" / "src").is_dir()
    assert has_v2_core or has_v1_core, "packages/core/src (or packages/opencode/src) must exist"
    assert (VENDOR_OPENCODE / "packages" / "server").is_dir(), "packages/server must exist"
    has_sdk = (VENDOR_OPENCODE / "packages" / "sdk" / "src").is_dir() or (VENDOR_OPENCODE / "packages" / "sdk" / "js").is_dir()
    assert has_sdk, "packages/sdk/src (or packages/sdk/js) must exist"

    license_file = VENDOR_OPENCODE / "LICENSE"
    assert license_file.is_file(), "vendor/opencode/LICENSE must exist"
    license_text = license_file.read_text(encoding="utf-8", errors="replace")
    assert "MIT License" in license_text or "Permission is hereby granted, free of charge" in license_text
    assert "Copyright" in license_text

    # Verify git remote points to anomalyco/opencode (either as origin or upstream)
    proc = subprocess.run(
        ["git", "remote", "-v"],
        cwd=str(VENDOR_OPENCODE),
        capture_output=True,
        text=True,
        check=True,
    )
    remotes_out = proc.stdout
    assert "anomalyco/opencode" in remotes_out, f"Expected anomalyco/opencode in git remotes:\n{remotes_out}"


def test_upstream_modifications_and_third_party_notices():
    """Verify UPSTREAM_MODIFICATIONS.md and THIRD_PARTY_NOTICES.md exist and document the 3-layer boundary."""
    upstream_doc = PROJECT_ROOT / "UPSTREAM_MODIFICATIONS.md"
    notices_doc = PROJECT_ROOT / "THIRD_PARTY_NOTICES.md"

    assert upstream_doc.is_file(), "UPSTREAM_MODIFICATIONS.md must exist at project root"
    assert notices_doc.is_file(), "THIRD_PARTY_NOTICES.md must exist at project root"

    upstream_text = upstream_doc.read_text(encoding="utf-8", errors="replace")
    assert "Layer 1" in upstream_text and "Layer 2" in upstream_text and "Layer 3" in upstream_text
    assert "anomalyco/opencode" in upstream_text

    notices_text = notices_doc.read_text(encoding="utf-8", errors="replace")
    assert "MIT" in notices_text
    assert "anomalyco/opencode" in notices_text or "OpenCode" in notices_text


def test_opencode_json_configuration_and_secret_safety():
    """Verify opencode.json configures freellm and tokenharbor with {env:...} references and zero hardcoded secrets."""
    cfg_path = PROJECT_ROOT / "opencode.json"
    assert cfg_path.is_file(), "opencode.json must exist at project root"

    raw_text = cfg_path.read_text(encoding="utf-8", errors="replace")
    cfg = json.loads(raw_text)

    assert "$schema" in cfg and "opencode.ai" in cfg["$schema"]
    providers = cfg.get("provider", {})
    assert "freellm" in providers, "freellm provider must be defined in opencode.json"
    assert "tokenharbor" in providers, "tokenharbor provider must be defined in opencode.json"

    freellm = providers["freellm"]
    freellm_opts = freellm.get("options", {})
    assert freellm_opts.get("apiKey") == "{env:FREELLM_API_KEY}", "freellm apiKey must use {env:FREELLM_API_KEY}"

    models = freellm.get("models", {})
    for required_model in ("auto", "auto:fast", "auto:smart", "fusion"):
        assert required_model in models, f"freellm model '{required_model}' must be defined in opencode.json"

    tokenharbor = providers["tokenharbor"]
    th_opts = tokenharbor.get("options", {})
    assert th_opts.get("apiKey") == "{env:TOKENHARBOR_API_KEY}", "tokenharbor apiKey must use {env:TOKENHARBOR_API_KEY}"

    # Ensure no live secrets exist in opencode.json
    assert not re.search(r"freellmapi-[A-Za-z0-9]{16,}", raw_text)
    assert not re.search(r"thk_live_[A-Za-z0-9]{16,}", raw_text)
    assert not re.search(r"sk-[A-Za-z0-9]{20,}", raw_text)


def test_opencode_bridge_modules():
    """Verify bel_code/opencode_bridge exports OpenCodeRuntimeManager, OpenCodeClient, and sdk_adapter.mjs."""
    bridge_dir = PROJECT_ROOT / "bel_code" / "opencode_bridge"
    assert bridge_dir.is_dir(), "bel_code/opencode_bridge directory must exist"
    assert (bridge_dir / "sdk_adapter.mjs").is_file(), "sdk_adapter.mjs must exist in bel_code/opencode_bridge"

    sdk_text = (bridge_dir / "sdk_adapter.mjs").read_text(encoding="utf-8", errors="replace")
    assert "@opencode-ai/sdk" in sdk_text or "packages/sdk/js" in sdk_text

    from bel_code.opencode_bridge import OpenCodeClient, OpenCodeRuntimeManager

    mgr = OpenCodeRuntimeManager(workspace_root=PROJECT_ROOT)
    status = mgr.get_status()
    assert isinstance(status, dict)
    assert "vendor/opencode" in str(status) or status.get("available") is True or "commit" in str(status).lower()

    oc_client = OpenCodeClient(runtime=mgr)
    health = oc_client.health()
    assert isinstance(health, dict)
    agents = oc_client.agent.list()
    assert len(agents) > 0


def test_opencode_server_endpoints():
    """Verify /api/opencode/* endpoints: status, sessions, message, diff, revert, agents."""
    client = TestClient(app)

    # 1. GET /api/opencode/status
    st_res = client.get("/api/opencode/status")
    assert st_res.status_code == 200, f"GET /api/opencode/status failed: {st_res.text}"
    st_data = st_res.json()
    assert isinstance(st_data, dict)
    assert "opencode" in str(st_data).lower()

    # 2. GET /api/opencode/agents
    ag_res = client.get("/api/opencode/agents")
    assert ag_res.status_code == 200, f"GET /api/opencode/agents failed: {ag_res.text}"
    ag_data = ag_res.json()
    ag_str = str(ag_data).lower()
    assert "build" in ag_str and "plan" in ag_str

    # 3. POST /api/opencode/sessions & GET /api/opencode/sessions
    create_res = client.post(
        "/api/opencode/sessions",
        json={"title": "QA OpenCode Verification Session", "agent": "build", "model": "freellm/auto"},
    )
    assert create_res.status_code in (200, 201), f"POST /api/opencode/sessions failed: {create_res.text}"
    created = create_res.json()
    session_id = created.get("id") or created.get("session_id") or created.get("session", {}).get("id")
    assert session_id, f"Expected session ID in response: {created}"

    list_res = client.get("/api/opencode/sessions")
    assert list_res.status_code == 200
    assert session_id in str(list_res.json())

    # 4. POST /api/opencode/sessions/{session_id}/message
    msg_res = client.post(
        f"/api/opencode/sessions/{session_id}/message",
        json={
            "message": "Inspect the project structure and summarize key modules",
            "content": "Inspect the project structure and summarize key modules",
            "agent": "build",
            "model": "freellm/auto",
        },
    )
    assert msg_res.status_code == 200, f"POST /api/opencode/sessions/{session_id}/message failed: {msg_res.text}"
    msg_data = msg_res.json()
    assert isinstance(msg_data, dict)

    # 5. GET /api/opencode/sessions/{session_id}/diff
    diff_res = client.get(f"/api/opencode/sessions/{session_id}/diff")
    assert diff_res.status_code == 200, f"GET /api/opencode/sessions/{session_id}/diff failed: {diff_res.text}"

    # 6. POST /api/opencode/sessions/{session_id}/revert
    rev_res = client.post(f"/api/opencode/sessions/{session_id}/revert", json={})
    assert rev_res.status_code == 200, f"POST /api/opencode/sessions/{session_id}/revert failed: {rev_res.text}"
