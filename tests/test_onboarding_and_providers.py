"""End-to-end tests for beginner onboarding, provider registry, multi-key config, 429 routing, Ollama, and docs."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

from fastapi.testclient import TestClient

from bel_code.config.settings import SecretVault, SettingsManager
from bel_code.providers.adapters import OllamaAdapter, get_default_provider_configs
from bel_code.providers.base import ProviderError, RateLimitError
from bel_code.providers.registry import get_registry_entry, load_provider_registry
from bel_code.providers.router import ProviderManager
from bel_code.server.app import create_app


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _make_isolated_manager(tmp_path: Path) -> ProviderManager:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    settings = SettingsManager(config_dir=cfg_dir)
    return ProviderManager(settings=settings, backoff_sleep_fn=lambda _: None)


def test_registry_completeness_and_verification_guardrail(tmp_path: Path):
    reg = load_provider_registry()
    providers = {p["provider_id"]: p for p in reg["providers"]}

    expected_ids = {
        "gemini",
        "groq",
        "openrouter",
        "mistral",
        "cloudflare",
        "nvidia",
        "sambanova",
        "cerebras",
        "huggingface",
        "github_models",
        "ollama",
        "freellm",
        "openai",
        "anthropic",
        "custom",
    }
    assert expected_ids.issubset(set(providers.keys()))

    # Trial/paid classifications must not be advertised as permanently free
    assert providers["gemini"]["advertised_free"] is True
    assert providers["groq"]["advertised_free"] is True
    assert providers["ollama"]["advertised_free"] is True
    assert providers["huggingface"]["advertised_free"] is False
    assert providers["huggingface"]["is_trial_credits"] is True
    assert providers["openai"]["advertised_free"] is False
    assert providers["openai"]["is_paid"] is True
    assert providers["anthropic"]["is_paid"] is True

    # OpenRouter defaults strictly to zero-cost openrouter/free
    defaults = get_default_provider_configs()
    assert defaults["openrouter"].default_model == "openrouter/free"
    assert defaults["openrouter"].cost_per_1k_input == 0.0
    assert defaults["openrouter"].cost_per_1k_output == 0.0

    # Verification guardrail rejects unverified entries from being advertised as free
    custom_reg = tmp_path / "registry_test.json"
    custom_reg.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider_id": "unverified_free",
                        "name": "Unverified Cloud",
                        "free_tier_classification": "permanently_free",
                        "verified": False,
                        "last_verified": "",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = load_provider_registry(registry_path=custom_reg)
    assert loaded["providers"][0]["advertised_free"] is False
    assert get_registry_entry("gemini") is not None


def test_env_loading_multi_account_and_redaction(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    env_file = tmp_path / ".env"
    mock_groq = "gsk_" + "mockenvtoken1234567890"
    env_file.write_text(f"GROQ_API_KEY={mock_groq}\n", encoding="utf-8")

    cfg_dir = tmp_path / "cfg"
    settings = SettingsManager(config_dir=cfg_dir)
    settings.load_dotenv(env_file)
    assert settings.vault.has_secret("groq") is True
    assert settings.vault.get_secret("groq") == mock_groq

    # Multi-account keys per provider
    vault = settings.vault
    mock_personal = "AIza" + "mockpersonal1234567890"
    mock_work = "AIza" + "mockworkkey1234567890"
    vault.set_secret("gemini", mock_personal, label="personal")
    vault.set_secret("gemini", mock_work, label="work")
    assert set(vault.list_accounts("gemini")) >= {"personal", "work"}
    assert vault.get_secret("gemini", label="work") == mock_work

    # Pattern + exact-secret redaction
    pm = ProviderManager(settings=settings)
    nvidia_adapter = pm.get_provider("nvidia")
    mock_nv = "nvapi-" + "mocksecret9876543210abcd"
    vault.set_secret("nvidia", mock_nv)
    sanitized = nvidia_adapter._redact_secret_text(f"Upstream error echoing Bearer {mock_nv} and {mock_groq}")
    assert mock_nv not in sanitized
    assert mock_groq not in sanitized
    assert "REDACTED" in sanitized


def test_cloudflare_account_id_and_ollama_no_key_workflow(tmp_path: Path):
    pm = _make_isolated_manager(tmp_path)

    cf_res = pm.configure_provider(
        "cloudflare",
        api_key="cf-mock-token-value",
        account_id="acct1234567890abcdef",
        set_as_primary=True,
    )
    assert "{account_id}" not in cf_res["base_url"]
    assert "acct1234567890abcdef" in cf_res["base_url"]

    # Ollama works cleanly without an API key; unreachable daemon reports helpful setup hint
    ollama = pm.get_provider("ollama")
    assert isinstance(ollama, OllamaAdapter)
    assert ollama.config.requires_api_key is False

    # Override reachability / transport deterministically for offline unit test
    ollama._is_ollama_reachable = lambda: False  # type: ignore[method-assign]
    offline_res = ollama.test_connection(live_network=True)
    assert offline_res["ok"] is False
    assert "ollama serve" in offline_res["message"]

    ollama._is_ollama_reachable = lambda: True  # type: ignore[method-assign]
    ollama.transport_handler = lambda method, url, headers, payload: (
        200,
        {"data": [{"id": "qwen2.5-coder:7b"}, {"id": "llama3.2"}]},
        {},
    )
    online_res = ollama.test_connection(live_network=True)
    assert online_res["ok"] is True
    assert online_res["status_code"] == 200
    discovered_ids = {m["id"] for m in ollama.discover_models()}
    assert "qwen2.5-coder:7b" in discovered_ids


def test_permanent_vs_temporary_429_and_no_paid_escalation(tmp_path: Path):
    pm = _make_isolated_manager(tmp_path)

    sleep_calls = []
    pm._sleep_fn = lambda s: sleep_calls.append(s)
    for adapter in pm.adapters.values():
        adapter.backoff_sleep_fn = pm._sleep_fn

    # Configure Gemini (free $0) as primary and Groq (free $0) as secondary via mocked transports
    pm.configure_provider("gemini", api_key="gemini-mock-key", set_as_primary=True)
    pm.configure_provider("groq", api_key="groq-mock-key", set_as_secondary=True)
    pm.configure_provider("openai", api_key="openai-mock-key")  # paid provider must NEVER auto-activate on failover

    gemini = pm.get_provider("gemini")
    groq = pm.get_provider("groq")

    # 1. Temporary 429 on Groq triggers retry backoff before failing or succeeding
    temp_attempts = {"count": 0}

    def temp_429_then_ok(method, url, headers, payload):
        temp_attempts["count"] += 1
        if temp_attempts["count"] == 1:
            return (
                429,
                {"error": {"message": "Rate limit exceeded, please try again shortly"}},
                {"Retry-After": "1"},
            )
        return (
            200,
            {
                "choices": [{"message": {"content": "Recovered after transient 429."}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            },
            {},
        )

    groq.transport_handler = temp_429_then_ok
    res_retry = groq.complete("hello")
    assert temp_attempts["count"] == 2
    assert len(groq.last_backoff_delays) == 1
    assert "Recovered" in res_retry.content

    # 2. Permanent 429 on Gemini skips backoff retries and immediately fails over to Groq with clear notice
    gemini_calls = {"count": 0}

    def perm_429(method, url, headers, payload):
        gemini_calls["count"] += 1
        return (
            429,
            {"error": {"message": "Daily limit exhausted: insufficient_quota for free tier"}},
            {},
        )

    gemini.transport_handler = perm_429
    groq.transport_handler = lambda method, url, headers, payload: (
        200,
        {
            "choices": [{"message": {"content": "Answered via Groq secondary failover."}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 9},
        },
        {},
    )

    failover_res = pm.complete("Write a hello function", task_type="general")
    assert gemini_calls["count"] == 1  # No wasted retries on permanent quota exhaustion
    assert len(gemini.last_backoff_delays) == 0
    assert failover_res.provider == "groq"
    assert failover_res.fallback_used is True
    assert "Failover active" in failover_res.limitations_note

    # 3. Ensure free provider NEVER silently upgrades to paid OpenAI when allow_expensive_upgrade=False
    assert pm._is_materially_more_expensive("gemini", "openai") is True
    assert pm._is_materially_more_expensive("gemini", "anthropic") is True
    assert pm._is_materially_more_expensive("gemini", "groq") is False


def test_onboarding_and_custom_provider_api_endpoints(tmp_path: Path):
    test_app = create_app(initial_workspace=PROJECT_ROOT, config_dir=tmp_path)
    client = TestClient(test_app)

    reg_res = client.get("/api/providers/registry")
    assert reg_res.status_code == 200
    reg_data = reg_res.json()
    assert reg_data["count"] >= 14
    ids = {p["provider_id"] for p in reg_data["providers"]}
    assert {"gemini", "groq", "openrouter", "cloudflare", "ollama"}.issubset(ids)

    onb = client.get("/api/onboarding/status")
    assert onb.status_code == 200
    assert "onboarding_completed" in onb.json()

    done = client.post("/api/onboarding/complete", json={"completed": True})
    assert done.status_code == 200
    assert done.json()["onboarding_completed"] is True

    # Register a custom OpenAI-compatible gateway cleanly via API without editing code
    custom = client.post(
        "/api/providers/custom",
        json={
            "provider_id": "custom-test-gw",
            "name": "Local Gateway Test",
            "endpoint_mode": "openai",
            "base_url": "http://localhost:4010/v1",
            "default_model": "local-coder",
            "api_key": "custom-mock-secret-value",
        },
    )
    assert custom.status_code == 200
    c_body = custom.json()
    assert c_body["provider_id"] == "custom-test-gw"
    assert "custom-mock-secret-value" not in json.dumps(c_body)

    # Confirm it appears in /api/providers and /api/providers/registry without leaking secret
    after_reg = client.get("/api/providers/registry").json()
    match = [p for p in after_reg["providers"] if p["provider_id"] == "custom-test-gw"]
    assert len(match) == 1
    assert match[0]["live_base_url"] == "http://localhost:4010/v1"
    assert "custom-mock-secret-value" not in json.dumps(after_reg)


def test_frontend_wizard_and_opensource_documentation_complete():
    index_html = (PROJECT_ROOT / "bel_code" / "frontend" / "index.html").read_text(encoding="utf-8")
    app_js = (PROJECT_ROOT / "bel_code" / "frontend" / "app.js").read_text(encoding="utf-8")

    # Welcome screen 3 beginner onboarding options + Connect Your AI wizard modal + model discovery UI
    for marker in (
        "btn-onboard-free-cloud",
        "btn-onboard-custom-api",
        "btn-onboard-local-ai",
        "modal-connect-ai",
        "btn-wizard-fetch-models",
        "provider-models-discovered-select",
        "Connect Your AI",
        "Choose an AI provider and get BEL CODE running in minutes.",
    ):
        assert marker in index_html, f"Missing UI wizard element: {marker}"

    assert "/api/providers/registry" in app_js
    assert "/api/onboarding/status" in app_js

    required_files = [
        PROJECT_ROOT / "LICENSE",
        PROJECT_ROOT / ".env.example",
        PROJECT_ROOT / ".gitignore",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "docs" / "GETTING_STARTED.md",
        PROJECT_ROOT / "docs" / "FREE_AI_PROVIDERS.md",
        PROJECT_ROOT / "docs" / "API_CONFIGURATION.md",
        PROJECT_ROOT / "docs" / "LOCAL_AI.md",
        PROJECT_ROOT / "docs" / "TROUBLESHOOTING.md",
        PROJECT_ROOT / "docs" / "PRIVACY_AND_SECURITY.md",
    ]
    for path in required_files:
        assert path.exists(), f"Missing required open-source file: {path}"
        assert len(path.read_text(encoding="utf-8").strip()) > 100


def test_dynamic_model_discovery_for_api_key_and_persistence(tmp_path: Path):
    pm = _make_isolated_manager(tmp_path)
    pm.configure_provider("groq", api_key="groq-mock-key-for-models")
    pm.configure_provider("gemini", api_key="gemini-mock-key-for-models")

    groq = pm.get_provider("groq")
    groq.transport_handler = lambda method, url, headers, payload: (
        200,
        {
            "data": [
                {"id": "llama-3.3-70b-versatile"},
                {"id": "mixtral-8x7b-32768"},
                {"id": "gemma2-9b-it"},
            ]
        },
        {},
    )
    groq_test = pm.test_connection("groq", live_network=True)
    assert groq_test["ok"] is True
    assert "mixtral-8x7b-32768" in groq_test["models"]
    assert "gemma2-9b-it" in groq.config.models

    gemini = pm.get_provider("gemini")
    gemini.transport_handler = lambda method, url, headers, payload: (
        200,
        {
            "models": [
                {"name": "models/gemini-2.5-flash", "displayName": "Gemini 2.5 Flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemma-3-27b-it", "displayName": "Gemma 3 27B", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/text-embedding-004", "displayName": "Embedding", "supportedGenerationMethods": ["embedContent"]},
            ]
        },
        {},
    )
    discovered_gemini = pm.discover_models("gemini")
    gemini_ids = {m["id"] for m in discovered_gemini}
    assert "gemma-3-27b-it" in gemini_ids
    assert "text-embedding-004" not in gemini_ids
    # Persisted into provider list so composer/wizard show all models for that key
    listed = {p["id"]: p for p in pm.list_providers()}
    assert "gemma-3-27b-it" in listed["gemini"]["models"]
    assert "mixtral-8x7b-32768" in listed["groq"]["models"]
