"""
Intelligent Provider Manager, Multi-Provider Router, Fallback Engine, and Cost/Telemetry Tracker
for BEL Code v2.1 (Spec Sections 2 & 17).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import time
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple, Union

from bel_code.config.settings import BudgetConfig, BudgetExceededError, SecretVault, SettingsManager
from bel_code.providers.adapters import (
    PROVIDER_ADAPTER_CLASSES,
    create_provider_adapter,
    get_default_provider_configs,
)
from bel_code.providers.base import (
    BaseProviderAdapter,
    ChatMessage,
    CompletionResult,
    ProviderConfig,
    ProviderError,
    RateLimitError,
    ToolDefinition,
    TransientProviderError,
    TransportCallable,
    UsageMetrics,
)


SUPPORTED_TASK_TYPES = (
    "architecture_reasoning",
    "large_scale_code",
    "routine_edits",
    "debugging",
    "code_review",
    "security_review",
    "documentation",
    "general",
)


DEFAULT_ROUTING_POLICY: Dict[str, Dict[str, Dict[str, str]]] = {
    "architecture_reasoning": {
        "primary": {"provider": "freellm", "model": "auto:smart"},
        "secondary": {"provider": "tokenharbor", "model": "th-claude-3-7-sonnet"},
        "tertiary": {"provider": "anthropic", "model": "claude-3-7-sonnet-20250219"},
    },
    "large_scale_code": {
        "primary": {"provider": "freellm", "model": "qwen-2.5-coder-32b"},
        "secondary": {"provider": "tokenharbor", "model": "th-qwen-2.5-coder-32b"},
        "tertiary": {"provider": "openai", "model": "gpt-4o"},
    },
    "routine_edits": {
        "primary": {"provider": "freellm", "model": "auto:fast"},
        "secondary": {"provider": "tokenharbor", "model": "th-fast-coder"},
        "tertiary": {"provider": "groq", "model": "llama-3.3-70b-versatile"},
    },
    "debugging": {
        "primary": {"provider": "freellm", "model": "auto:smart"},
        "secondary": {"provider": "tokenharbor", "model": "th-deepseek-r1"},
        "tertiary": {"provider": "deepseek", "model": "deepseek-reasoner"},
    },
    "code_review": {
        "primary": {"provider": "freellm", "model": "fusion"},
        "secondary": {"provider": "tokenharbor", "model": "th-claude-3-7-sonnet"},
        "tertiary": {"provider": "openai", "model": "gpt-4o"},
    },
    "security_review": {
        "primary": {"provider": "freellm", "model": "auto:smart"},
        "secondary": {"provider": "tokenharbor", "model": "th-claude-3-7-sonnet"},
        "tertiary": {"provider": "anthropic", "model": "claude-3-7-sonnet-20250219"},
    },
    "documentation": {
        "primary": {"provider": "freellm", "model": "auto:fast"},
        "secondary": {"provider": "tokenharbor", "model": "th-gpt-4o"},
        "tertiary": {"provider": "gemini", "model": "gemini-2.5-flash"},
    },
    "general": {
        "primary": {"provider": "freellm", "model": "auto"},
        "secondary": {"provider": "tokenharbor", "model": "th-claude-3-7-sonnet"},
        "tertiary": {"provider": "openai", "model": "gpt-4o-mini"},
    },
}


class ProviderManager:
    """
    Manages all 13 AI providers, credentials in SecretVault, intelligent task-type model routing,
    automatic failover across primary/secondary/tertiary providers, rate-limit tracking, and budget guardrails.
    """

    EXPENSIVE_COST_THRESHOLD_PER_1K = 0.0020

    def __init__(
        self,
        settings: Optional[SettingsManager] = None,
        config_dir: Optional[Any] = None,
        backoff_sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.settings = settings or SettingsManager(config_dir=config_dir)
        self.vault: SecretVault = self.settings.vault
        self.budget: BudgetConfig = self.settings.budget
        self._sleep_fn = backoff_sleep_fn

        self.adapters: Dict[str, BaseProviderAdapter] = {}
        self._routing_policy: Dict[str, Dict[str, Dict[str, str]]] = {
            k: {tier: dict(target) for tier, target in v.items()}
            for k, v in DEFAULT_ROUTING_POLICY.items()
        }

        # Telemetry & health tracking per provider
        self._health: Dict[str, Dict[str, Any]] = {}
        self._request_timestamps: Dict[str, Deque[Tuple[float, int]]] = {}
        self._task_usage: Dict[str, Dict[str, Any]] = {}
        self._total_requests: int = 0
        self._total_prompt_tokens: int = 0
        self._total_completion_tokens: int = 0
        self._total_cost_usd: float = 0.0
        self._total_fallbacks: int = 0

        self._initialize_providers()

    def _initialize_providers(self) -> None:
        defaults = get_default_provider_configs()
        saved_providers = self.settings.get_settings().get("providers", {})
        saved_routing = self.settings.get_settings().get("routing_policy", {})
        if isinstance(saved_routing, dict) and saved_routing:
            for ttype, tiers in saved_routing.items():
                if isinstance(tiers, dict):
                    self._routing_policy[ttype] = {
                        tier: dict(val) for tier, val in tiers.items() if isinstance(val, dict)
                    }

        for pid, default_cfg in defaults.items():
            merged_dict = default_cfg.to_dict()
            if pid in saved_providers and isinstance(saved_providers[pid], dict):
                merged_dict.update(saved_providers[pid])
            cfg = ProviderConfig.from_dict(merged_dict)
            self.adapters[pid] = create_provider_adapter(
                provider_id=pid,
                config=cfg,
                vault=self.vault,
                backoff_sleep_fn=self._sleep_fn,
            )
            self._init_health_entry(pid)

        # Also load any extra user-registered custom providers
        for pid, pdata in saved_providers.items():
            if pid not in self.adapters and isinstance(pdata, dict):
                pdata_with_id = {"provider_id": pid, **pdata}
                cfg = ProviderConfig.from_dict(pdata_with_id)
                self.adapters[pid] = create_provider_adapter(
                    provider_id=pid,
                    config=cfg,
                    vault=self.vault,
                    backoff_sleep_fn=self._sleep_fn,
                )
                self._init_health_entry(pid)

    def _init_health_entry(self, provider_id: str) -> None:
        if provider_id not in self._health:
            self._health[provider_id] = {
                "provider_id": provider_id,
                "status": "healthy",
                "requests": 0,
                "successes": 0,
                "errors": 0,
                "rate_limit_hits": 0,
                "consecutive_failures": 0,
                "avg_latency_ms": 0.0,
                "last_latency_ms": 0.0,
                "last_error": None,
                "last_used_at": None,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
            }
        if provider_id not in self._request_timestamps:
            self._request_timestamps[provider_id] = deque()

    def get_provider(self, provider_id: str) -> BaseProviderAdapter:
        pid = provider_id.strip().lower()
        if pid not in self.adapters:
            raise KeyError(f"Unknown provider '{provider_id}'. Available: {list(self.adapters.keys())}")
        return self.adapters[pid]

    def list_providers(self) -> List[Dict[str, Any]]:
        """List all registered providers with configuration, key status, rate-limit headers, and live health metrics."""
        settings_map = self.settings.get_settings()
        primary_pid = settings_map.get("primary_provider", "freellm")
        secondary_pid = settings_map.get("secondary_provider")
        fallback_pid = settings_map.get("tertiary_provider")
        result: List[Dict[str, Any]] = []
        for pid, adapter in self.adapters.items():
            cfg = adapter.config
            has_key = self.vault.has_secret(pid)
            accounts = self.vault.list_accounts(pid)
            health = self._health.get(pid, {})
            rpm_cur, tpm_cur = self._get_current_rate_window(pid)
            result.append(
                {
                    **cfg.to_dict(),
                    "has_api_key": has_key,
                    "requires_api_key": cfg.requires_api_key,
                    "accounts": accounts,
                    "is_primary": pid == primary_pid,
                    "is_secondary": pid == secondary_pid,
                    "is_fallback": pid == fallback_pid,
                    "rate_limit_headers": dict(getattr(adapter, "last_rate_limit_headers", {}) or {}),
                    "health": {
                        **health,
                        "rpm_current": rpm_cur,
                        "tpm_current": tpm_cur,
                    },
                }
            )
        return result

    def configure_provider(
        self,
        provider_id: Union[str, Dict[str, Any]],
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_model: Optional[str] = None,
        enabled: Optional[bool] = None,
        account_label: str = "default",
        label: Optional[str] = None,
        endpoint_mode: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Configure or update a provider's settings and securely store its API key in `SecretVault`.
        Supports either `configure_provider("openai", api_key="...", default_model="...")`
        or `configure_provider({"provider_id": "openai", "api_key": "..."})`.
        """
        if label is not None:
            account_label = str(label)
        if isinstance(provider_id, dict):
            payload = dict(provider_id)
            pid = str(payload.pop("provider_id", payload.pop("id", "freellm"))).strip().lower()
            api_key = payload.pop("api_key", api_key)
            base_url = payload.pop("base_url", base_url)
            default_model = payload.pop("default_model", default_model)
            enabled = payload.pop("enabled", enabled)
            account_label = str(payload.pop("account_label", payload.pop("label", account_label)))
            endpoint_mode = payload.pop("endpoint_mode", endpoint_mode)
            kwargs.update(payload)
        else:
            pid = provider_id.strip().lower()

        if pid not in self.adapters:
            cfg = ProviderConfig(
                provider_id=pid,
                name=kwargs.get("name", pid.title()),
                base_url=base_url or "http://localhost:8080/v1",
                default_model=default_model or "auto",
                enabled=True if enabled is None else bool(enabled),
                account_label=account_label,
                endpoint_mode=endpoint_mode or "auto",
            )
            self.adapters[pid] = create_provider_adapter(
                pid, config=cfg, vault=self.vault, backoff_sleep_fn=self._sleep_fn
            )
            self._init_health_entry(pid)

        adapter = self.adapters[pid]
        cfg = adapter.config

        if api_key is not None and str(api_key).strip():
            self.vault.set_secret(pid, str(api_key).strip(), label=account_label)
            cfg.account_label = account_label

        if base_url is not None:
            cfg.base_url = str(base_url).strip()
        if kwargs.get("account_id"):
            acct_id = str(kwargs["account_id"]).strip()
            if "{account_id}" in cfg.base_url:
                cfg.base_url = cfg.base_url.replace("{account_id}", acct_id)
        if default_model is not None:
            cfg.default_model = str(default_model).strip()
            if cfg.default_model not in cfg.models:
                cfg.models.insert(0, cfg.default_model)
        if enabled is not None:
            cfg.enabled = bool(enabled)
        if endpoint_mode is not None:
            cfg.endpoint_mode = str(endpoint_mode).strip()
        if "requires_api_key" in kwargs:
            cfg.requires_api_key = bool(kwargs["requires_api_key"])
        if "secondary_base_url" in kwargs and kwargs["secondary_base_url"] is not None:
            cfg.secondary_base_url = str(kwargs["secondary_base_url"])
        if "models" in kwargs and isinstance(kwargs["models"], list):
            cfg.models = list(kwargs["models"])
        if "cost_per_1k_input" in kwargs:
            cfg.cost_per_1k_input = float(kwargs["cost_per_1k_input"])
        if "cost_per_1k_output" in kwargs:
            cfg.cost_per_1k_output = float(kwargs["cost_per_1k_output"])
        if "max_retries" in kwargs:
            cfg.max_retries = int(kwargs["max_retries"])

        # Persist non-secret provider config
        providers_settings = dict(self.settings.get_settings().get("providers", {}))
        providers_settings[pid] = cfg.to_dict()
        setting_updates: Dict[str, Any] = {"providers": providers_settings}

        role = str(kwargs.get("role") or "").strip().lower()
        if kwargs.get("set_as_primary") or role == "primary":
            setting_updates["primary_provider"] = pid
            self._routing_policy.setdefault("general", {})["primary"] = {"provider": pid, "model": cfg.default_model}
            setting_updates["routing_policy"] = self._routing_policy
        if kwargs.get("set_as_secondary") or role == "secondary":
            setting_updates["secondary_provider"] = pid
            self._routing_policy.setdefault("general", {})["secondary"] = {"provider": pid, "model": cfg.default_model}
            setting_updates["routing_policy"] = self._routing_policy
        if kwargs.get("set_as_fallback") or kwargs.get("set_as_tertiary") or role in ("fallback", "tertiary"):
            setting_updates["tertiary_provider"] = pid
            self._routing_policy.setdefault("general", {})["tertiary"] = {"provider": pid, "model": cfg.default_model}
            setting_updates["routing_policy"] = self._routing_policy

        self.settings.update_settings(setting_updates)

        opencode_sync = self.sync_opencode_config(provider_id=pid)

        return {
            **cfg.to_dict(),
            "has_api_key": self.vault.has_secret(pid),
            "requires_api_key": cfg.requires_api_key,
            "accounts": self.vault.list_accounts(pid),
            "opencode_synced": True,
            "opencode_provider": opencode_sync.get("provider_entry"),
            "status": "configured",
        }

    def register_custom_provider(
        self,
        provider_id: str,
        name: str,
        base_url: str,
        default_model: str,
        api_key: Optional[str] = None,
        endpoint_mode: str = "openai",
        requires_api_key: bool = True,
        account_label: str = "default",
        set_as_primary: bool = False,
        cost_per_1k_input: float = 0.0,
        cost_per_1k_output: float = 0.0,
    ) -> Dict[str, Any]:
        """Register a user-defined OpenAI-compatible or Anthropic-compatible provider without code edits."""
        clean_id = "".join(ch for ch in provider_id.strip().lower() if ch.isalnum() or ch in ("-", "_")) or "custom"
        return self.configure_provider(
            provider_id=clean_id,
            name=name.strip() or clean_id.title(),
            api_key=api_key,
            base_url=base_url.strip(),
            default_model=default_model.strip(),
            enabled=True,
            account_label=account_label,
            endpoint_mode=endpoint_mode,
            requires_api_key=requires_api_key,
            set_as_primary=set_as_primary,
            cost_per_1k_input=cost_per_1k_input,
            cost_per_1k_output=cost_per_1k_output,
        )

    def sync_opencode_config(
        self,
        provider_id: Optional[str] = None,
        target_path: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Synchronize non-secret provider and model metadata into OpenCode configuration format
        (`opencode.json` inside `settings.config_dir` or `target_path`).
        Secrets are NEVER written to disk in `opencode.json`; environment variable placeholders
        (`{env:FREELLM_API_KEY}`, `{env:TOKENHARBOR_API_KEY}`, etc.) are strictly enforced.
        """
        import json
        from pathlib import Path

        cfg_dir = getattr(self.settings, "config_dir", None)
        out_path = Path(target_path) if target_path else (Path(cfg_dir) / "opencode.json" if cfg_dir else None)

        doc: Dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "theme": "bel-dark-graphite",
            "model": f"{self.settings.get_settings().get('primary_provider', 'freellm')}/auto",
            "small_model": "freellm/auto:fast",
            "default_agent": "build",
            "provider": {},
        }
        if out_path and out_path.exists():
            try:
                loaded = json.loads(out_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    doc.update(loaded)
            except Exception:
                pass

        providers_map = doc.setdefault("provider", {})
        pids = [provider_id.strip().lower()] if provider_id else ["freellm", "tokenharbor"]
        last_entry: Dict[str, Any] = {}

        for pid in pids:
            if pid not in self.adapters:
                continue
            adapter = self.adapters[pid]
            cfg = adapter.config
            env_var = f"{pid.upper()}_API_KEY"
            existing_entry = providers_map.get(pid, {}) if isinstance(providers_map.get(pid), dict) else {}
            existing_models = existing_entry.get("models", {}) if isinstance(existing_entry.get("models"), dict) else {}

            models_meta: Dict[str, Any] = dict(existing_models)
            try:
                discovered = adapter.discover_models()
            except Exception:
                discovered = []

            for m in discovered:
                if isinstance(m, dict) and m.get("id"):
                    mid = str(m["id"])
                    ctx_limit = int(m.get("context_window", 32768))
                    out_limit = int(m.get("max_output_tokens", 8192))
                    models_meta[mid] = {
                        "id": mid,
                        "name": str(m.get("name", mid)),
                        "tool_call": bool(m.get("supports_tools", True)),
                        "attachment": False,
                        "temperature": True,
                        "reasoning": bool("smart" in mid or "r1" in mid or "fusion" in mid),
                        "streaming": bool(m.get("supports_streaming", True)),
                        "cost": {
                            "input": float(m.get("cost_per_1k_input", cfg.cost_per_1k_input)),
                            "output": float(m.get("cost_per_1k_output", cfg.cost_per_1k_output)),
                        },
                        "limit": {
                            "context": ctx_limit,
                            "output": out_limit,
                        },
                        "options": {
                            "routing_mode": m.get("routing_mode", "auto"),
                            "tool_calling_mode": m.get(
                                "tool_calling_mode",
                                "native_with_structured_fallback" if m.get("supports_tools", True) else "structured_fallback",
                            ),
                            "prompt_tool_fallback": True,
                        },
                    }

            entry = {
                "npm": existing_entry.get("npm", "@ai-sdk/openai-compatible"),
                "name": existing_entry.get("name", cfg.name),
                "env": [env_var],
                "options": {
                    "baseURL": cfg.base_url,
                    "apiKey": f"{{env:{env_var}}}",
                },
                "models": models_meta,
            }
            if cfg.secondary_base_url:
                entry["options"]["secondaryBaseURL"] = cfg.secondary_base_url
            providers_map[pid] = entry
            last_entry = entry

        if out_path:
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            except Exception:
                pass

        return {
            "synced": True,
            "path": str(out_path) if out_path else None,
            "provider_entry": last_entry,
            "config": doc,
        }

    def disable_provider(self, provider_id: str) -> Dict[str, Any]:
        """Disable a provider so it is skipped during model routing."""
        pid = provider_id.strip().lower()
        adapter = self.get_provider(pid)
        adapter.config.enabled = False
        self._health[pid]["status"] = "disabled"
        providers_settings = dict(self.settings.get_settings().get("providers", {}))
        providers_settings[pid] = adapter.config.to_dict()
        self.settings.update_settings({"providers": providers_settings})
        return {
            "provider_id": pid,
            "enabled": False,
            "status": "disabled",
        }

    def remove_provider(self, provider_id: str, delete_secrets: bool = True) -> Dict[str, Any]:
        """Disable/remove a provider configuration and delete its stored credentials from SecretVault."""
        pid = provider_id.strip().lower()
        if delete_secrets:
            for acc in list(self.vault.list_accounts(pid)):
                self.vault.delete_secret(pid, label=acc["label"])

        if pid in self.adapters:
            self.adapters[pid].config.enabled = False
            self._health[pid]["status"] = "removed"

        providers_settings = dict(self.settings.get_settings().get("providers", {}))
        providers_settings.pop(pid, None)
        self.settings.update_settings({"providers": providers_settings})
        return {
            "provider_id": pid,
            "removed": True,
            "has_api_key": self.vault.has_secret(pid),
        }

    def test_connection(self, provider_id: str = "freellm", live_network: bool = False) -> Dict[str, Any]:
        """Run connection & readiness test for `provider_id` and cache discovered models."""
        adapter = self.get_provider(provider_id)
        result = adapter.test_connection(live_network=live_network)
        if result.get("ok") and isinstance(result.get("models"), list) and result["models"]:
            adapter.config.models = list(dict.fromkeys(str(m) for m in result["models"] if m))
            providers_settings = dict(self.settings.get_settings().get("providers", {}))
            providers_settings[adapter.provider_id] = adapter.config.to_dict()
            self.settings.update_settings({"providers": providers_settings})
        return result

    def discover_models(self, provider_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Discover models for a specific provider or across all enabled providers."""
        if provider_id is not None:
            adapter = self.get_provider(provider_id)
            models = adapter.discover_models()
            discovered_ids = [str(m.get("id")) for m in models if isinstance(m, dict) and m.get("id")]
            if discovered_ids:
                adapter.config.models = list(dict.fromkeys(discovered_ids))
                providers_settings = dict(self.settings.get_settings().get("providers", {}))
                providers_settings[adapter.provider_id] = adapter.config.to_dict()
                self.settings.update_settings({"providers": providers_settings})
            return models

        all_models: List[Dict[str, Any]] = []
        for pid, adapter in self.adapters.items():
            if adapter.config.enabled:
                all_models.extend(adapter.discover_models())
        return all_models

    @staticmethod
    def classify_task_type(prompt: str) -> str:
        """Infer the best engineering task_type from a natural-language task prompt."""
        text = (prompt or "").lower()
        if any(k in text for k in ("security", "vulnerability", "cve", "injection", "xss", "audit", "secret")):
            return "security_review"
        if any(k in text for k in ("debug", "traceback", "exception", "failing test", "bug", "fix", "crash", "error")):
            return "debugging"
        if any(k in text for k in ("architect", "design", "system design", "scalab", "schema", "microservice")):
            return "architecture_reasoning"
        if any(k in text for k in ("review", "pull request", "pr ", "diff")):
            return "code_review"
        if any(k in text for k in ("document", "readme", "docstring", "changelog", "openapi")):
            return "documentation"
        if any(k in text for k in ("migrate", "refactor", "build app", "from scratch", "implement", "feature", "module")):
            return "large_scale_code"
        if any(k in text for k in ("rename", "format", "typo", "quick", "small edit")):
            return "routine_edits"
        return "general"

    def get_routing_policy(self) -> Dict[str, Any]:
        """Return the current task-type routing policy and provider hierarchy."""
        settings_data = self.settings.get_settings()
        return {
            "auto_routing": bool(settings_data.get("auto_routing", True)),
            "primary_provider": settings_data.get("primary_provider", "freellm"),
            "secondary_provider": settings_data.get("secondary_provider", "tokenharbor"),
            "tertiary_provider": settings_data.get("tertiary_provider", "openai"),
            "allow_expensive_upgrade": bool(settings_data.get("allow_expensive_upgrade", False)),
            "task_routes": json_clone(self._routing_policy),
        }

    def set_routing_policy(self, policy_updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update the routing policy or primary/secondary/tertiary provider hierarchy."""
        settings_updates: Dict[str, Any] = {}
        for key in ("auto_routing", "primary_provider", "secondary_provider", "tertiary_provider", "allow_expensive_upgrade"):
            if key in policy_updates:
                settings_updates[key] = policy_updates[key]

        task_routes = policy_updates.get("task_routes") or policy_updates.get("routes")
        if isinstance(task_routes, dict):
            for ttype, tiers in task_routes.items():
                if isinstance(tiers, dict):
                    existing = self._routing_policy.setdefault(ttype, {})
                    for tier_name, target in tiers.items():
                        if isinstance(target, dict) and "provider" in target:
                            existing[tier_name] = {
                                "provider": str(target["provider"]).lower(),
                                "model": str(target.get("model", "auto")),
                            }

        # Also support direct task_type keys passed at top level of policy_updates
        for ttype in SUPPORTED_TASK_TYPES:
            if ttype in policy_updates and isinstance(policy_updates[ttype], dict):
                existing = self._routing_policy.setdefault(ttype, {})
                for tier_name, target in policy_updates[ttype].items():
                    if isinstance(target, dict) and "provider" in target:
                        existing[tier_name] = {
                            "provider": str(target["provider"]).lower(),
                            "model": str(target.get("model", "auto")),
                        }

        settings_updates["routing_policy"] = self._routing_policy
        self.settings.update_settings(settings_updates)
        return self.get_routing_policy()

    def select_route(
        self,
        task_type: str = "general",
        prompt: str = "",
        preferred_provider: Optional[str] = None,
        preferred_model: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """
        Build an ordered list of `[primary, secondary, tertiary, ...]` provider/model candidates
        across distinct providers for the given `task_type`.
        """
        resolved_type = task_type if task_type in self._routing_policy else self.classify_task_type(prompt)
        tier_map = self._routing_policy.get(resolved_type, self._routing_policy["general"])

        candidates: List[Dict[str, str]] = []
        seen: set[Tuple[str, str]] = set()

        def _add(pid: str, mid: Optional[str], tier_label: str) -> None:
            pid_clean = pid.strip().lower()
            if pid_clean not in self.adapters:
                return
            adapter = self.adapters[pid_clean]
            if not adapter.config.enabled:
                return
            model_clean = mid or adapter.config.default_model
            key = (pid_clean, model_clean)
            if key not in seen:
                seen.add(key)
                candidates.append({"provider": pid_clean, "model": model_clean, "tier": tier_label})

        if preferred_provider:
            _add(preferred_provider, preferred_model, "explicit")

        for tier_name in ("primary", "secondary", "tertiary"):
            target = tier_map.get(tier_name)
            if target:
                t_pid = target["provider"]
                t_mid = preferred_model if (tier_name == "primary" and preferred_model and not preferred_provider) else target.get("model")
                _add(t_pid, t_mid, tier_name)

        # Include user-configured secondary/fallback providers if set
        settings_map = self.settings.get_settings()
        for tier_key, tier_label in (("secondary_provider", "secondary"), ("tertiary_provider", "tertiary")):
            cfg_pid = settings_map.get(tier_key)
            if cfg_pid:
                _add(str(cfg_pid), None, tier_label)

        # Ensure Token Harbor and FreeLLM are always available as resilient fallbacks
        _add("tokenharbor", None, "fallback")
        _add("freellm", "auto", "fallback")

        return candidates

    def simulate_provider_failure(
        self,
        provider_id: str,
        status_code: int = 429,
        message: str = "Rate limit exceeded",
        remaining_failures: int = 999,
    ) -> None:
        """Simulate HTTP 429 / 5xx / connection errors on `provider_id` to test automatic failover."""
        adapter = self.get_provider(provider_id)
        adapter.set_simulated_failure(
            status_code=status_code,
            message=message,
            remaining_failures=remaining_failures,
        )

    def clear_simulated_failures(self, provider_id: Optional[str] = None) -> None:
        """Clear simulated failures on one or all providers."""
        if provider_id is not None:
            self.get_provider(provider_id).clear_simulated_failure()
        else:
            for adapter in self.adapters.values():
                adapter.clear_simulated_failure()

    def _is_materially_more_expensive(
        self,
        primary_provider_id: str,
        candidate_provider_id: str,
    ) -> bool:
        """
        Check whether failing over from `primary_provider_id` to `candidate_provider_id` represents
        a silent upgrade from free/zero-cost to a paid provider or to a materially more expensive tier.
        Note: Token Harbor and FreeLLM are pre-approved low-cost/free fallback tiers.
        """
        if candidate_provider_id in ("freellm", "tokenharbor"):
            return False
        prim_cfg = self.adapters[primary_provider_id].config
        cand_cfg = self.adapters[candidate_provider_id].config
        prim_rate = prim_cfg.cost_per_1k_input + prim_cfg.cost_per_1k_output
        cand_rate = cand_cfg.cost_per_1k_input + cand_cfg.cost_per_1k_output
        if prim_rate == 0.0 and cand_rate > 0.0:
            return True
        if cand_rate <= self.EXPENSIVE_COST_THRESHOLD_PER_1K:
            return False
        if prim_rate > 0 and cand_rate > (prim_rate * 5.0):
            return True
        return False

    def complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        task_type: str = "general",
        model: Optional[str] = None,
        provider_id: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        task_id: str = "default",
        project_id: str = "default",
        allow_expensive_upgrade: Optional[bool] = None,
        simulate_primary_failure: bool = False,
        **kwargs: Any,
    ) -> CompletionResult:
        """
        Route and execute a chat completion with automatic retry, multi-provider failover,
        cost protection, and budget enforcement.
        """
        norm_messages = (
            [ChatMessage(role="user", content=messages)]
            if isinstance(messages, str)
            else [ChatMessage.from_any(m) for m in messages]
        )
        prompt_Preview = " ".join(m.content for m in norm_messages)
        est_prompt_tokens = sum(BaseProviderAdapter.estimate_tokens(m.content) for m in norm_messages)

        # Pre-flight budget check
        self.budget.check_budget(
            amount_usd=0.0,
            task_id=task_id,
            project_id=project_id,
            raise_on_exceed=self.budget.hard_stop,
        )

        allow_expensive = (
            bool(allow_expensive_upgrade)
            if allow_expensive_upgrade is not None
            else bool(self.settings.get_settings().get("allow_expensive_upgrade", False))
        )

        candidates = self.select_route(
            task_type=task_type,
            prompt=prompt_Preview,
            preferred_provider=provider_id,
            preferred_model=model,
        )
        if not candidates:
            raise ProviderError("No enabled AI providers available for routing.")

        primary_pid = candidates[0]["provider"]
        if simulate_primary_failure:
            self.simulate_provider_failure(primary_pid, status_code=429, message="Simulated primary rate limit (HTTP 429)", remaining_failures=3)

        attempt_history: List[Dict[str, Any]] = []
        last_error: Optional[Exception] = None

        try:
            for idx, candidate in enumerate(candidates):
                cand_pid = candidate["provider"]
                cand_model = candidate["model"]
                adapter = self.adapters[cand_pid]

                # Cost guardrail on failover: do not silently switch to a materially more expensive provider
                if idx > 0 and not allow_expensive and self._is_materially_more_expensive(primary_pid, cand_pid):
                    attempt_history.append(
                        {
                            "provider": cand_pid,
                            "model": cand_model,
                            "status": "skipped_expensive_upgrade",
                            "reason": f"Skipped expensive failover to '{cand_pid}' without allow_expensive_upgrade=True.",
                        }
                    )
                    continue

                # Estimate candidate request cost and verify against budget before calling
                est_cost = adapter.estimate_cost(est_prompt_tokens, min(max_tokens, 512), model=cand_model)
                budget_check = self.budget.check_budget(
                    amount_usd=est_cost,
                    task_id=task_id,
                    project_id=project_id,
                    raise_on_exceed=False,
                )
                if budget_check.exceeded and self.budget.hard_stop:
                    if idx == 0 and est_cost > 0:
                        # Try a zero-cost fallback if available, or raise if budget is already at limit
                        zero_check = self.budget.check_budget(0.000001, task_id=task_id, project_id=project_id)
                        if zero_check.exceeded:
                            raise BudgetExceededError(budget_check.reason, scope=budget_check.scope or "task")
                    elif est_cost > 0:
                        attempt_history.append(
                            {
                                "provider": cand_pid,
                                "model": cand_model,
                                "status": "skipped_budget_limit",
                                "reason": budget_check.reason,
                            }
                        )
                        continue

                try:
                    result = adapter.complete(
                        messages=norm_messages,
                        model=cand_model,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        **kwargs,
                    )
                    result.fallback_used = idx > 0
                    attempt_history.append(
                        {
                            "provider": cand_pid,
                            "model": result.model,
                            "status": "success",
                            "tier": candidate["tier"],
                            "latency_ms": result.usage.latency_ms,
                        }
                    )
                    result.attempt_history = attempt_history
                    if idx > 0:
                        self._total_fallbacks += 1
                        first_fail = next(
                            (a for a in attempt_history if a.get("status") not in ("success",)),
                            None,
                        )
                        fail_reason = (first_fail or {}).get("error") or (first_fail or {}).get("reason") or "unavailable"
                        notice = (
                            f"Failover active: switched from '{primary_pid}' to '{cand_pid}/{result.model}' "
                            f"because: {fail_reason}"
                        )
                        result.limitations_note = f"{result.limitations_note} {notice}".strip() if result.limitations_note else notice

                    self._record_success(
                        provider_id=cand_pid,
                        usage=result.usage,
                        task_id=task_id,
                        project_id=project_id,
                    )
                    return result

                except Exception as exc:
                    last_error = exc
                    status_code = getattr(exc, "status_code", None)
                    self._record_failure(cand_pid, exc, status_code=status_code)
                    attempt_history.append(
                        {
                            "provider": cand_pid,
                            "model": cand_model,
                            "status": "failed",
                            "status_code": status_code,
                            "error": str(exc),
                        }
                    )
                    continue
        finally:
            if simulate_primary_failure:
                self.clear_simulated_failures(primary_pid)

        if last_error is not None:
            raise last_error
        raise ProviderError("All configured providers failed to complete the request.")

    def simulate_failover(
        self,
        prompt: str = "Verify automatic provider failover",
        task_type: str = "general",
    ) -> CompletionResult:
        """Simulate a primary provider failure and execute automatic failover to secondary provider."""
        return self.complete(messages=prompt, task_type=task_type, simulate_primary_failure=True)

    def chat(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        **kwargs: Any,
    ) -> CompletionResult:
        """Convenience alias for `complete()`."""
        return self.complete(messages=messages, **kwargs)

    def stream_complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        task_type: str = "general",
        model: Optional[str] = None,
        provider_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Iterator[Dict[str, Any]]:
        """Stream completion events from the primary or routed provider."""
        candidates = self.select_route(
            task_type=task_type,
            preferred_provider=provider_id,
            preferred_model=model,
        )
        target = candidates[0]
        adapter = self.adapters[target["provider"]]
        yield from adapter.stream_complete(messages=messages, model=target["model"], **kwargs)

    def _record_success(
        self,
        provider_id: str,
        usage: UsageMetrics,
        task_id: str = "default",
        project_id: str = "default",
    ) -> None:
        self._init_health_entry(provider_id)
        h = self._health[provider_id]
        h["requests"] += 1
        h["successes"] += 1
        h["consecutive_failures"] = 0
        h["status"] = "healthy"
        h["last_latency_ms"] = usage.latency_ms
        prev_avg = float(h["avg_latency_ms"])
        succ = int(h["successes"])
        h["avg_latency_ms"] = round(((prev_avg * (succ - 1)) + usage.latency_ms) / succ, 2)
        h["last_used_at"] = datetime.now(timezone.utc).isoformat()
        h["prompt_tokens"] += usage.prompt_tokens
        h["completion_tokens"] += usage.completion_tokens
        h["total_tokens"] += usage.total_tokens
        h["cost_usd"] = round(float(h["cost_usd"]) + usage.cost_usd, 6)

        now_ts = time.time()
        self._request_timestamps[provider_id].append((now_ts, usage.total_tokens))
        self._prune_rate_window(provider_id, now_ts)

        self._total_requests += 1
        self._total_prompt_tokens += usage.prompt_tokens
        self._total_completion_tokens += usage.completion_tokens
        self._total_cost_usd = round(self._total_cost_usd + usage.cost_usd, 6)

        t_entry = self._task_usage.setdefault(
            task_id,
            {
                "task_id": task_id,
                "project_id": project_id,
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        t_entry["requests"] += 1
        t_entry["prompt_tokens"] += usage.prompt_tokens
        t_entry["completion_tokens"] += usage.completion_tokens
        t_entry["total_tokens"] += usage.total_tokens
        t_entry["cost_usd"] = round(float(t_entry["cost_usd"]) + usage.cost_usd, 6)

        if usage.cost_usd > 0:
            self.budget.record_spend(
                amount_usd=usage.cost_usd,
                task_id=task_id,
                project_id=project_id,
                provider_id=provider_id,
                model_id=usage.model_id,
            )
            self.settings.save()

    def _record_failure(
        self,
        provider_id: str,
        exc: Exception,
        status_code: Optional[int] = None,
    ) -> None:
        self._init_health_entry(provider_id)
        h = self._health[provider_id]
        h["requests"] += 1
        h["errors"] += 1
        h["consecutive_failures"] += 1
        if status_code == 429 or isinstance(exc, RateLimitError):
            h["rate_limit_hits"] += 1
        h["last_error"] = str(exc)
        h["status"] = "degraded" if h["consecutive_failures"] < 3 else "unhealthy"
        self._total_requests += 1

    def _prune_rate_window(self, provider_id: str, now_ts: Optional[float] = None) -> None:
        now = now_ts or time.time()
        window = self._request_timestamps.get(provider_id)
        if not window:
            return
        while window and (now - window[0][0]) > 60.0:
            window.popleft()

    def _get_current_rate_window(self, provider_id: str) -> Tuple[int, int]:
        self._prune_rate_window(provider_id)
        window = self._request_timestamps.get(provider_id, deque())
        rpm = len(window)
        tpm = sum(tokens for _, tokens in window)
        return rpm, tpm

    def get_provider_health(self, provider_id: Optional[str] = None) -> Dict[str, Any]:
        if provider_id is not None:
            pid = provider_id.strip().lower()
            self._init_health_entry(pid)
            rpm, tpm = self._get_current_rate_window(pid)
            return {**self._health[pid], "rpm_current": rpm, "tpm_current": tpm}
        return {
            pid: {**h, "rpm_current": self._get_current_rate_window(pid)[0], "tpm_current": self._get_current_rate_window(pid)[1]}
            for pid, h in self._health.items()
        }

    def get_usage_summary(self) -> Dict[str, Any]:
        """Return comprehensive token usage, cost, budget status, and provider health telemetry."""
        return {
            "total_requests": self._total_requests,
            "total_prompt_tokens": self._total_prompt_tokens,
            "total_completion_tokens": self._total_completion_tokens,
            "total_tokens": self._total_prompt_tokens + self._total_completion_tokens,
            "total_cost_usd": round(self._total_cost_usd, 6),
            "total_fallbacks": self._total_fallbacks,
            "budget": self.budget.get_summary(),
            "providers": self.get_provider_health(),
            "tasks": dict(self._task_usage),
        }


def json_clone(obj: Any) -> Any:
    import json
    return json.loads(json.dumps(obj))
