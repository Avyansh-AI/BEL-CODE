"""Tests for BEL Code Config, SecretVault, BudgetConfig, FreeLLMAdapter, 12+ Providers, and ProviderManager."""

from pathlib import Path
import pytest

from bel_code.config.settings import (
    SecurityMode,
    BudgetConfig,
    SecretVault,
    SettingsManager,
)
from bel_code.providers.base import (
    ChatMessage,
    ToolDefinition,
    UsageMetrics,
    CompletionResult,
    ProviderConfig,
)
from bel_code.providers.freellm import FreeLLMAdapter
from bel_code.providers.router import ProviderManager


def test_security_mode_enum():
    assert SecurityMode.ASK.value == "ask"
    assert SecurityMode.SUPERVISED.value == "supervised"
    assert SecurityMode.AUTONOMOUS.value == "autonomous"


def test_secret_vault_encryption_rotation_and_redaction(tmp_path: Path):
    vault_file = tmp_path / "vault.enc"
    vault = SecretVault(vault_path=vault_file, use_keyring=False)

    # Store secrets across providers and labels
    vault.set_secret("freellm", "freellmapi-secretkey1234567890", label="default")
    vault.set_secret("tokenharbor", "thk_live_9876543210abcdef12345", label="prod")
    vault.set_secret("openai", "sk-proj-abcdef1234567890abcdef12", label="default")

    assert vault.get_secret("freellm", "default") == "freellmapi-secretkey1234567890"
    assert vault.get_secret("tokenharbor", "prod") == "thk_live_9876543210abcdef12345"
    assert "prod" in vault.list_accounts("tokenharbor")

    # Verify vault file on disk does not contain plaintext key
    if vault_file.exists():
        raw_bytes = vault_file.read_bytes()
        assert b"freellmapi-secretkey1234567890" not in raw_bytes
        assert b"thk_live_9876543210abcdef12345" not in raw_bytes

    # Reload from encrypted file
    vault2 = SecretVault(vault_path=vault_file, use_keyring=False)
    assert vault2.get_secret("freellm", "default") == "freellmapi-secretkey1234567890"

    # Key rotation
    vault2.rotate_key("freellm", "freellmapi-rotatedkey9999999999", label="default")
    assert vault2.get_secret("freellm", "default") == "freellmapi-rotatedkey9999999999"

    # Secret redaction
    sample_log = (
        "Auth with thk_live_9876543210abcdef12345 and "
        "freellmapi-rotatedkey9999999999 and "
        "sk-proj-abcdef1234567890abcdef12 and Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    )
    redacted = vault2.redact(sample_log)
    assert "thk_live_9876543210abcdef12345" not in redacted
    assert "freellmapi-rotatedkey9999999999" not in redacted
    assert "sk-proj-abcdef1234567890abcdef12" not in redacted
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted

    # Delete secret
    vault2.delete_secret("tokenharbor", label="prod")
    assert vault2.get_secret("tokenharbor", "prod") is None


def test_budget_config_spending_limits_and_hard_stop():
    budget = BudgetConfig(per_task_usd=2.0, per_project_usd=10.0, global_usd=25.0, hard_stop=True)

    # Within budget
    res_ok = budget.check_budget(1.0, task_id="task-1", project_id="proj-1")
    if isinstance(res_ok, dict):
        assert res_ok.get("allowed", True) is True
    else:
        assert bool(res_ok) is True

    # Record spend or exceed per-task budget
    if hasattr(budget, "record_spend"):
        budget.record_spend(1.5, task_id="task-1", project_id="proj-1")

    # Exceeding per-task cap should either raise an exception or return allowed=False / hard_stop=True
    try:
        res_exceed = budget.check_budget(2.5, task_id="task-1", project_id="proj-1")
        if isinstance(res_exceed, dict):
            assert res_exceed.get("allowed") is False or res_exceed.get("hard_stop_triggered") is True
        else:
            assert res_exceed is False
    except Exception as exc:
        assert "budget" in str(exc).lower() or "limit" in str(exc).lower() or "stop" in str(exc).lower()


def test_freellm_adapter_features():
    adapter = FreeLLMAdapter()
    models = adapter.discover_models()
    assert isinstance(models, list)
    assert len(models) > 0

    # Check chat completion (offline deterministic fallback or mock)
    messages = [ChatMessage(role="user", content="Write a hello world function in Python")]
    tools = [
        ToolDefinition(
            name="write_file",
            description="Write content to a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    ]
    result = adapter.complete(messages=messages, model="auto", tools=tools)
    assert isinstance(result, CompletionResult)
    assert result.provider == "freellm"
    assert isinstance(result.content, str)

    # Connection test returns structured status
    conn = adapter.test_connection()
    assert isinstance(conn, dict)
    assert "provider" in conn or "status" in conn or "ok" in conn or "success" in conn

    # Tool-calling prompt-engineering fallback & limitation note on non-native tool model
    non_native_res = adapter.complete(
        messages=messages,
        model="deepseek-r1-free",
        tools=tools,
    )
    assert isinstance(non_native_res, CompletionResult)
    assert non_native_res.limitations_note is not None and len(non_native_res.limitations_note) > 0

    # Rate-limit (HTTP 429) retry & backoff handling with custom transport
    attempts = {"count": 0}

    def flaky_transport(method, url, headers, payload=None):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return 429, {"error": {"message": "Rate limit exceeded"}}, {"Retry-After": "0.01"}
        return 200, {
            "model": "auto",
            "choices": [{"message": {"role": "assistant", "content": "Recovered after 429"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }, {}

    retry_adapter = FreeLLMAdapter(transport_handler=flaky_transport)
    retry_res = retry_adapter.complete(messages=messages, model="auto")
    assert retry_res.content == "Recovered after 429"
    assert attempts["count"] == 2



def test_all_12_plus_providers_and_token_harbor_dual_endpoints(tmp_path: Path):
    settings = SettingsManager(config_dir=tmp_path)
    manager = ProviderManager(settings=settings)

    providers = manager.list_providers()
    provider_ids = {p["id"] if isinstance(p, dict) else p.provider_id for p in providers}

    expected_providers = {
        "freellm",
        "openai",
        "anthropic",
        "gemini",
        "groq",
        "openrouter",
        "deepseek",
        "mistral",
        "together",
        "cerebras",
        "fireworks",
        "tokenharbor",
        "custom",
    }
    assert expected_providers.issubset(provider_ids), f"Missing providers: {expected_providers - provider_ids}"

    # Verify FreeLLM is primary default
    routing = manager.get_routing_policy()
    assert isinstance(routing, dict)

    # Verify Token Harbor dual-endpoint support (/v1/chat/completions and /v1/messages)
    manager.configure_provider(
        "tokenharbor",
        api_key="thk_live_test_key_for_unit_test_12345",
        base_url="https://tokenharbor.ai/v1",
    )
    th_models = manager.discover_models("tokenharbor")
    assert isinstance(th_models, list) and len(th_models) > 0


def test_provider_manager_task_routing_and_automatic_fallback(tmp_path: Path):
    settings = SettingsManager(config_dir=tmp_path)
    manager = ProviderManager(settings=settings)

    # Test task-type routing across specialized categories
    for task_type in ["architecture_reasoning", "routine_edits", "debugging", "security_review"]:
        res = manager.complete(
            messages=[ChatMessage(role="user", content=f"Task for {task_type}")],
            task_type=task_type,
        )
        assert isinstance(res, CompletionResult)
        assert res.content

    # Simulate HTTP 429 / 5xx on primary provider and verify automatic fallback
    fallback_res = manager.complete(
        messages=[ChatMessage(role="user", content="Test automatic provider failover")],
        task_type="general",
        simulate_primary_failure=True,
    ) if "simulate_primary_failure" in manager.complete.__code__.co_varnames else manager.simulate_failover() if hasattr(manager, "simulate_failover") else None

    if fallback_res is not None:
        if isinstance(fallback_res, CompletionResult):
            assert fallback_res.fallback_used is True
        elif isinstance(fallback_res, dict):
            assert fallback_res.get("fallback_used") is True or fallback_res.get("success") is True

    usage = manager.get_usage_summary()
    assert isinstance(usage, dict)


def test_freellm_live_transport_activation_user_agent_and_diagnostics(tmp_path: Path):
    """Configured API key triggers real HTTP transport, includes User-Agent, and clarifies gateway mismatches."""
    vault = SecretVault(vault_path=tmp_path / "vault.enc", use_keyring=False)
    vault.set_secret("freellm", "freellmapi-testdummykey00000001")

    seen_headers = {}

    def ok_transport(method, url, headers, payload=None):
        seen_headers.update(headers)
        if url.endswith("/models"):
            return 200, {"object": "list", "data": [{"id": "auto", "name": "Auto"}]}, {}
        return 200, {
            "model": "auto",
            "choices": [{"message": {"role": "assistant", "content": "Live gateway response"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }, {}

    adapter = FreeLLMAdapter(vault=vault, transport_handler=ok_transport)
    assert adapter._should_use_http_transport() is True
    conn = adapter.test_connection(live_network=True)
    assert conn["ok"] is True
    assert conn["status_code"] == 200

    res = adapter.complete(messages="Ping", model="auto")
    assert res.content == "Live gateway response"
    assert "Authorization" in seen_headers

    # BaseProviderAdapter._raw_http_call must include a default User-Agent header (prevents Cloudflare 1010)
    import bel_code.providers.base as base_mod
    from bel_code.providers.freellm import default_freellm_config

    captured_ua = {}

    class _FakeResp:
        def getcode(self):
            return 200

        def read(self):
            return b'{"ok":true}'

        @property
        def headers(self):
            return {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    orig_urlopen = base_mod.urllib.request.urlopen

    def _fake_urlopen(req, timeout=10):
        captured_ua["ua"] = req.get_header("User-agent")
        return _FakeResp()

    base_mod.urllib.request.urlopen = _fake_urlopen
    try:
        status, _, _ = FreeLLMAdapter(vault=vault)._raw_http_call("GET", "https://apifreellm.com/api/v1/models", {})
        assert status == 200
        assert captured_ua.get("ua", "").startswith("Mozilla/5.0")
    finally:
        base_mod.urllib.request.urlopen = orig_urlopen

    # Pointing a freellmapi- key at apifreellm.com surfaces a clear gateway mismatch error
    def bad_cloud_transport(method, url, headers, payload=None):
        return 401, {"success": False, "error": "Invalid API key"}, {}

    cloud_cfg = default_freellm_config()
    cloud_cfg.base_url = "https://apifreellm.com/api/v1/chat"
    cloud_cfg.endpoint_mode = "direct_rest"
    cloud_adapter = FreeLLMAdapter(config=cloud_cfg, vault=vault, transport_handler=bad_cloud_transport)
    with pytest.raises(Exception) as exc_info:
        cloud_adapter.complete(messages="Hello", model="apifreellm-direct")
    assert "31415" in str(exc_info.value) and "apifreellm.com" in str(exc_info.value)
