"""
Specialized Provider Adapters for all 13 Enterprise & Open Providers in BEL Code v2.1 (Spec Section 2.2):

1. `freellm`     - FreeLLM API (Primary Default)
2. `openai`      - OpenAI (`/v1/chat/completions` & `/v1/models`)
3. `anthropic`   - Anthropic Claude (`/v1/messages` with `x-api-key`, `anthropic-version: 2023-06-01`, `tool_use`)
4. `gemini`      - Google Gemini API (`/v1beta/models/{model}:generateContent` & `/v1beta/models`)
5. `groq`        - Groq (`/openai/v1/chat/completions`)
6. `openrouter`  - OpenRouter (`/api/v1/chat/completions`)
7. `deepseek`    - DeepSeek (`/v1/chat/completions`)
8. `mistral`     - Mistral (`/v1/chat/completions`)
9. `together`    - Together AI (`/v1/chat/completions`)
10. `cerebras`   - Cerebras (`/v1/chat/completions`)
11. `fireworks`  - Fireworks AI (`/inference/v1/chat/completions`)
12. `tokenharbor`- Token Harbor Verified API Dual-Endpoint Adapter:
                   - OpenAI-compatible `https://tokenharbor.ai/v1/chat/completions` (`Bearer thk_live_...`)
                   - Claude-compatible `https://tokenharbor.ai/v1/messages` (`x-api-key` or `Authorization`)
                   - Dynamic catalog discovery `GET https://tokenharbor.ai/v1/models`
13. `custom`     - Custom OpenAI / Anthropic compatible endpoint
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Type, Union
from urllib.parse import urlparse

from bel_code.config.settings import SecretVault
from bel_code.providers.base import (
    AuthenticationError,
    BaseProviderAdapter,
    ChatMessage,
    CompletionResult,
    ProviderConfig,
    ProviderError,
    ToolDefinition,
    TransportCallable,
    UsageMetrics,
)
from bel_code.providers.freellm import FreeLLMAdapter, default_freellm_config


class OpenAICompatibleAdapter(BaseProviderAdapter):
    """
    Standard OpenAI-compatible `/chat/completions` and `/models` adapter.
    Used by OpenAI, Groq, OpenRouter, DeepSeek, Mistral, Together, Cerebras, Fireworks,
    Cloudflare, NVIDIA NIM, SambaNova, Hugging Face, GitHub Models, Ollama, and Custom.
    """

    def build_headers(self, api_key: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self.config.extra_headers,
        }
        resolved_key = api_key or self.get_api_key()
        if resolved_key:
            headers["Authorization"] = f"Bearer {resolved_key}"
        return headers

    def get_chat_completions_url(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def get_models_url(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        return f"{base}/models"

    def build_request_payload(
        self,
        messages: List[ChatMessage],
        model: str,
        tools: List[ToolDefinition],
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools and self.config.supports_tools:
            payload["tools"] = [t.to_openai_tool() for t in tools]
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")
        return payload

    def complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> CompletionResult:
        start_time = time.perf_counter()
        norm_messages = self.normalize_messages(messages)
        norm_tools = self.normalize_tools(tools)
        target_model = model or self.config.default_model

        limitations_notes: List[str] = list(self.config.limitations)
        if norm_tools and not self.config.supports_tools:
            norm_messages = self.inject_prompt_tools(norm_messages, norm_tools)
            limitations_notes.append(
                f"Provider '{self.provider_id}' model '{target_model}' using prompt-engineered tool fallback."
            )

        url = self.get_chat_completions_url()
        headers = self.build_headers(api_key=kwargs.get("api_key"))
        payload = self.build_request_payload(
            norm_messages,
            target_model,
            norm_tools,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

        if not self._should_use_http_transport(**kwargs):
            return self._synthesize_offline_completion(
                norm_messages,
                model=target_model,
                tools=norm_tools,
                limitations_note=" ".join(limitations_notes) if limitations_notes else None,
            )

        try:
            _, data, _ = self._execute_http_with_retry(
                method="POST",
                url=url,
                headers=headers,
                payload=payload,
                max_retries=kwargs.get("max_retries"),
            )
        except ProviderError as err:
            if (
                self.transport_handler is None
                and self.simulated_failure is None
                and err.status_code == 503
                and self.allow_offline_synthesis
                and not kwargs.get("strict_network", False)
            ):
                return self._synthesize_offline_completion(
                    norm_messages,
                    model=target_model,
                    tools=norm_tools,
                    limitations_note=" ".join(limitations_notes) if limitations_notes else None,
                )
            raise

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        content = ""
        tool_calls: List[Dict[str, Any]] = []
        finish_reason = "stop"
        resolved_model = target_model

        if isinstance(data, dict):
            resolved_model = str(data.get("model", target_model))
            choices = data.get("choices", [])
            if choices and isinstance(choices[0], dict):
                choice = choices[0]
                finish_reason = str(choice.get("finish_reason", "stop"))
                msg = choice.get("message", {})
                content = str(msg.get("content") or "")
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        args = fn.get("arguments", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                args = {"raw": args}
                        tool_calls.append(
                            {
                                "id": tc.get("id", "call_1"),
                                "type": "function",
                                "function": {"name": fn.get("name", ""), "arguments": args},
                            }
                        )

        if not tool_calls and content:
            cleaned, extracted = self.extract_prompt_tool_calls(content)
            if extracted:
                content = cleaned
                tool_calls = extracted

        usage_raw = data.get("usage", {}) if isinstance(data, dict) else {}
        prompt_tokens = int(
            usage_raw.get("prompt_tokens")
            or sum(self.estimate_tokens(m.content) for m in norm_messages)
        )
        completion_tokens = int(
            usage_raw.get("completion_tokens") or self.estimate_tokens(content)
        )
        total_tokens = int(usage_raw.get("total_tokens") or (prompt_tokens + completion_tokens))
        cost_usd = self.estimate_cost(prompt_tokens, completion_tokens, model=resolved_model)

        usage = UsageMetrics(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            provider_id=self.provider_id,
            model_id=resolved_model,
        )

        return CompletionResult(
            content=content,
            model=resolved_model,
            provider=self.provider_id,
            tool_calls=tool_calls,
            usage=usage,
            raw_response=data if isinstance(data, dict) else {"data": data},
            fallback_used=False,
            limitations_note=" ".join(limitations_notes) if limitations_notes else None,
            finish_reason=finish_reason,
        )

    def stream_complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> Iterator[Dict[str, Any]]:
        result = self.complete(
            messages=messages,
            model=model,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        for idx, token in enumerate(result.content.split(" ")):
            yield {
                "delta": token if idx == 0 else f" {token}",
                "model": result.model,
                "provider": self.provider_id,
                "done": False,
            }
        yield {
            "delta": "",
            "model": result.model,
            "provider": self.provider_id,
            "done": True,
            "usage": result.usage.to_dict(),
        }

    def _build_default_model_catalog(self) -> Dict[str, Dict[str, Any]]:
        return {
            mid: {
                "id": mid,
                "name": mid,
                "provider": self.provider_id,
                "supports_tools": self.config.supports_tools,
                "supports_streaming": self.config.supports_streaming,
                "cost_per_1k_input": self.config.cost_per_1k_input,
                "cost_per_1k_output": self.config.cost_per_1k_output,
                "is_free": (self.config.cost_per_1k_input + self.config.cost_per_1k_output) == 0.0,
            }
            for mid in self.config.models
        }

    def _merge_models_payload(self, catalog: Dict[str, Dict[str, Any]], data: Any) -> None:
        raw_items: List[Any] = []
        if isinstance(data, dict):
            if isinstance(data.get("data"), list):
                raw_items = data["data"]
            elif isinstance(data.get("models"), list):
                raw_items = data["models"]
            elif isinstance(data.get("result"), list):
                raw_items = data["result"]
        elif isinstance(data, list):
            raw_items = data

        for item in raw_items:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id") or item.get("name") or "").strip()
            if not mid:
                continue
            pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
            try:
                cost_in = float(pricing.get("prompt", item.get("cost_per_1k_input", self.config.cost_per_1k_input)))
                cost_out = float(pricing.get("completion", item.get("cost_per_1k_output", self.config.cost_per_1k_output)))
            except (TypeError, ValueError):
                cost_in = self.config.cost_per_1k_input
                cost_out = self.config.cost_per_1k_output
            is_free = (cost_in == 0.0 and cost_out == 0.0) or mid.endswith(":free") or mid == "openrouter/free"
            catalog[mid] = {
                "id": mid,
                "name": str(item.get("name", mid)),
                "provider": self.provider_id,
                "supports_tools": bool(item.get("supports_tools", self.config.supports_tools)),
                "supports_streaming": bool(item.get("supports_streaming", self.config.supports_streaming)),
                "cost_per_1k_input": cost_in,
                "cost_per_1k_output": cost_out,
                "is_free": is_free,
            }

        # Prioritize free models first when catalog contains mixed pricing (e.g. OpenRouter)
        ordered = sorted(catalog.keys(), key=lambda m: (0 if catalog[m].get("is_free") else 1))
        self.config.models = ordered

    def discover_models(self) -> List[Dict[str, Any]]:
        catalog = self._build_default_model_catalog()
        if self.transport_handler is not None or self._should_use_http_transport():
            try:
                status, data, _ = self._raw_http_call(
                    "GET", self.get_models_url(), self.build_headers(), None
                )
                if status == 200:
                    self._merge_models_payload(catalog, data)
            except Exception:
                pass
        return [catalog[k] for k in self.config.models if k in catalog] or list(catalog.values())

    def test_connection(self, live_network: bool = False) -> Dict[str, Any]:
        start = time.perf_counter()
        api_key = self.get_api_key()
        if self.simulated_failure and self.simulated_failure.get("remaining", 0) > 0:
            code = int(self.simulated_failure.get("status_code", 429))
            return {
                "provider_id": self.provider_id,
                "name": self.config.name,
                "ok": False,
                "status": "error",
                "status_code": code,
                "endpoint": self.get_chat_completions_url(),
                "has_api_key": bool(api_key),
                "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                "models_available": len(self.config.models),
                "message": f"Simulated failure (HTTP {code})",
            }

        if self.config.requires_api_key and not api_key and self.transport_handler is None:
            return {
                "provider_id": self.provider_id,
                "name": self.config.name,
                "ok": False,
                "status": "missing_api_key",
                "status_code": 401,
                "endpoint": self.get_chat_completions_url(),
                "has_api_key": False,
                "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                "models_available": len(self.config.models),
                "message": f"API key required for {self.config.name}. Configure in SecretVault.",
            }

        if self.transport_handler is not None or live_network:
            try:
                status, data, _ = self._raw_http_call(
                    "GET",
                    self.get_models_url(),
                    self.build_headers(),
                    None,
                )
                if status != 200:
                    err_detail = self._redact_secret_text(str(data))
                    return {
                        "provider_id": self.provider_id,
                        "name": self.config.name,
                        "ok": False,
                        "status": "error",
                        "status_code": status,
                        "endpoint": self.get_chat_completions_url(),
                        "has_api_key": bool(api_key),
                        "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                        "models_available": len(self.config.models),
                        "message": f"{self.config.name} returned HTTP {status}: {err_detail}",
                    }
                catalog = self._build_default_model_catalog()
                self._merge_models_payload(catalog, data)
                models = [catalog[k] for k in self.config.models if k in catalog] or list(catalog.values())
                return {
                    "provider_id": self.provider_id,
                    "name": self.config.name,
                    "ok": True,
                    "status": "connected",
                    "status_code": 200,
                    "endpoint": self.get_chat_completions_url(),
                    "has_api_key": bool(api_key),
                    "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                    "models_available": len(models),
                    "models": [m["id"] for m in models],
                    "message": f"{self.config.name} verified ({len(models)} models available for key).",
                }
            except Exception as exc:
                return {
                    "provider_id": self.provider_id,
                    "name": self.config.name,
                    "ok": False,
                    "status": "error",
                    "endpoint": self.get_chat_completions_url(),
                    "has_api_key": bool(api_key),
                    "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                    "models_available": len(self.config.models),
                    "message": self._redact_secret_text(str(exc)),
                }

        return {
            "provider_id": self.provider_id,
            "name": self.config.name,
            "ok": True,
            "status": "configured",
            "status_code": 200,
            "endpoint": self.get_chat_completions_url(),
            "has_api_key": bool(api_key),
            "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
            "models_available": len(self.config.models),
            "message": f"{self.config.name} configured and ready.",
        }


class OpenAIAdapter(OpenAICompatibleAdapter):
    """Official OpenAI `/v1/chat/completions` & `/v1/models` adapter."""


class GroqAdapter(OpenAICompatibleAdapter):
    """Groq `/openai/v1/chat/completions` ultra-low latency adapter."""


class OpenRouterAdapter(OpenAICompatibleAdapter):
    """OpenRouter `/api/v1/chat/completions` multi-model gateway adapter."""


class DeepSeekAdapter(OpenAICompatibleAdapter):
    """DeepSeek `/v1/chat/completions` reasoning & code adapter."""


class MistralAdapter(OpenAICompatibleAdapter):
    """Mistral `/v1/chat/completions` & Codestral adapter."""


class TogetherAdapter(OpenAICompatibleAdapter):
    """Together AI `/v1/chat/completions` open-weights adapter."""


class CerebrasAdapter(OpenAICompatibleAdapter):
    """Cerebras `/v1/chat/completions` wafer-scale inference adapter."""


class FireworksAdapter(OpenAICompatibleAdapter):
    """Fireworks AI `/inference/v1/chat/completions` adapter."""


class CloudflareAdapter(OpenAICompatibleAdapter):
    """Cloudflare Workers AI OpenAI-compatible edge inference adapter."""

    def get_chat_completions_url(self) -> str:
        if "{account_id}" in self.config.base_url:
            raise ProviderError(
                "Cloudflare Account ID missing from Base URL. Enter your Account ID in Settings or 'Connect Your AI'.",
                provider_id=self.provider_id,
                status_code=400,
            )
        return super().get_chat_completions_url()


class NvidiaAdapter(OpenAICompatibleAdapter):
    """NVIDIA NIM hosted API (`integrate.api.nvidia.com/v1`) adapter."""


class SambaNovaAdapter(OpenAICompatibleAdapter):
    """SambaNova Cloud (`api.sambanova.ai/v1`) RDU inference adapter."""


class HuggingFaceAdapter(OpenAICompatibleAdapter):
    """Hugging Face Inference Providers (`router.huggingface.co/v1`) adapter."""


class GitHubModelsAdapter(OpenAICompatibleAdapter):
    """GitHub Models (`models.github.ai/inference`) prototyping adapter."""


class OllamaAdapter(OpenAICompatibleAdapter):
    """Local Ollama (`http://localhost:11434/v1`) open-weight inference adapter (no API key required)."""

    def _is_ollama_reachable(self) -> bool:
        try:
            parsed = urlparse(self.config.base_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            return False

    def _should_use_http_transport(self, **kwargs: Any) -> bool:
        if super()._should_use_http_transport(**kwargs):
            return True
        return self._is_ollama_reachable()

    def test_connection(self, live_network: bool = False) -> Dict[str, Any]:
        if (live_network or self.transport_handler is None) and not self._is_ollama_reachable() and self.transport_handler is None:
            return {
                "provider_id": self.provider_id,
                "name": self.config.name,
                "ok": False,
                "status": "offline",
                "status_code": 503,
                "endpoint": self.get_chat_completions_url(),
                "has_api_key": False,
                "latency_ms": 0.0,
                "models_available": len(self.config.models),
                "message": (
                    f"Ollama server not reachable at {self.config.base_url}. "
                    "Start Ollama ('ollama serve') and pull a model (e.g. 'ollama pull qwen2.5-coder:7b')."
                ),
            }
        return super().test_connection(live_network=live_network)


class AnthropicAdapter(BaseProviderAdapter):
    """
    Anthropic Claude `/v1/messages` adapter.
    Uses `x-api-key`, `anthropic-version: 2023-06-01`, and Claude `tool_use` / `input_schema` format.
    """

    ANTHROPIC_VERSION = "2023-06-01"

    def build_headers(self, api_key: Optional[str] = None) -> Dict[str, str]:
        resolved_key = api_key or self.get_api_key()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "anthropic-version": self.ANTHROPIC_VERSION,
            **self.config.extra_headers,
        }
        if resolved_key:
            headers["x-api-key"] = resolved_key
        return headers

    def get_messages_url(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/messages"):
            return base
        return f"{base}/messages"

    def build_messages_payload(
        self,
        messages: List[ChatMessage],
        model: str,
        tools: List[ToolDefinition],
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        system_parts: List[str] = []
        claude_messages: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                system_parts.append(msg.content)
            else:
                role = "assistant" if msg.role == "assistant" else "user"
                claude_messages.append({"role": role, "content": msg.content})

        if not claude_messages:
            claude_messages.append({"role": "user", "content": "Hello"})

        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": claude_messages,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if tools and self.config.supports_tools:
            payload["tools"] = [t.to_anthropic_tool() for t in tools]
        return payload

    @staticmethod
    def parse_claude_response(data: Dict[str, Any], default_model: str) -> Tuple[str, List[Dict[str, Any]], str, str]:
        content_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        blocks = data.get("content", [])
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict):
                    btype = block.get("type")
                    if btype == "text":
                        content_parts.append(str(block.get("text", "")))
                    elif btype == "tool_use":
                        tool_calls.append(
                            {
                                "id": str(block.get("id", "toolu_1")),
                                "type": "function",
                                "function": {
                                    "name": str(block.get("name", "")),
                                    "arguments": dict(block.get("input", {})),
                                },
                            }
                        )
        elif isinstance(blocks, str):
            content_parts.append(blocks)

        content = "\n".join(content_parts).strip()
        resolved_model = str(data.get("model", default_model))
        stop_reason = str(data.get("stop_reason", "end_turn"))
        return content, tool_calls, resolved_model, stop_reason

    def complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> CompletionResult:
        start_time = time.perf_counter()
        norm_messages = self.normalize_messages(messages)
        norm_tools = self.normalize_tools(tools)
        target_model = model or self.config.default_model

        url = self.get_messages_url()
        headers = self.build_headers(api_key=kwargs.get("api_key"))
        payload = self.build_messages_payload(
            norm_messages, target_model, norm_tools, temperature=temperature, max_tokens=max_tokens
        )

        if not self._should_use_http_transport(**kwargs):
            return self._synthesize_offline_completion(norm_messages, model=target_model, tools=norm_tools)

        try:
            _, data, _ = self._execute_http_with_retry(
                method="POST",
                url=url,
                headers=headers,
                payload=payload,
                max_retries=kwargs.get("max_retries"),
            )
        except ProviderError as err:
            if (
                self.transport_handler is None
                and self.simulated_failure is None
                and err.status_code == 503
                and self.allow_offline_synthesis
                and not kwargs.get("strict_network", False)
            ):
                return self._synthesize_offline_completion(norm_messages, model=target_model, tools=norm_tools)
            raise

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        content, tool_calls, resolved_model, stop_reason = self.parse_claude_response(
            data if isinstance(data, dict) else {}, target_model
        )

        usage_raw = data.get("usage", {}) if isinstance(data, dict) else {}
        prompt_tokens = int(
            usage_raw.get("input_tokens")
            or usage_raw.get("prompt_tokens")
            or sum(self.estimate_tokens(m.content) for m in norm_messages)
        )
        completion_tokens = int(
            usage_raw.get("output_tokens")
            or usage_raw.get("completion_tokens")
            or self.estimate_tokens(content)
        )
        cost_usd = self.estimate_cost(prompt_tokens, completion_tokens, model=resolved_model)

        usage = UsageMetrics(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            provider_id=self.provider_id,
            model_id=resolved_model,
        )
        return CompletionResult(
            content=content,
            model=resolved_model,
            provider=self.provider_id,
            tool_calls=tool_calls,
            usage=usage,
            raw_response=data if isinstance(data, dict) else {"data": data},
            fallback_used=False,
            finish_reason=stop_reason,
        )

    def stream_complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> Iterator[Dict[str, Any]]:
        result = self.complete(messages, model=model, tools=tools, temperature=temperature, max_tokens=max_tokens, **kwargs)
        for idx, token in enumerate(result.content.split(" ")):
            yield {"delta": token if idx == 0 else f" {token}", "model": result.model, "provider": self.provider_id, "done": False}
        yield {"delta": "", "model": result.model, "provider": self.provider_id, "done": True, "usage": result.usage.to_dict()}

    def discover_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": mid,
                "name": mid,
                "provider": self.provider_id,
                "supports_tools": True,
                "supports_streaming": True,
                "cost_per_1k_input": self.config.cost_per_1k_input,
                "cost_per_1k_output": self.config.cost_per_1k_output,
            }
            for mid in self.config.models
        ]

    def test_connection(self, live_network: bool = False) -> Dict[str, Any]:
        api_key = self.get_api_key()
        if self.simulated_failure and self.simulated_failure.get("remaining", 0) > 0:
            code = int(self.simulated_failure.get("status_code", 429))
            return {
                "provider_id": self.provider_id,
                "name": self.config.name,
                "ok": False,
                "status": "error",
                "status_code": code,
                "endpoint": self.get_messages_url(),
                "has_api_key": bool(api_key),
                "models_available": len(self.config.models),
                "message": f"Simulated failure (HTTP {code})",
            }
        if self.config.requires_api_key and not api_key and self.transport_handler is None:
            return {
                "provider_id": self.provider_id,
                "name": self.config.name,
                "ok": False,
                "status": "missing_api_key",
                "status_code": 401,
                "endpoint": self.get_messages_url(),
                "has_api_key": False,
                "models_available": len(self.config.models),
                "message": "Anthropic x-api-key not configured.",
            }
        return {
            "provider_id": self.provider_id,
            "name": self.config.name,
            "ok": True,
            "status": "configured",
            "status_code": 200,
            "endpoint": self.get_messages_url(),
            "has_api_key": bool(api_key),
            "models_available": len(self.config.models),
            "message": "Anthropic Claude `/v1/messages` adapter ready.",
        }


class GeminiAdapter(BaseProviderAdapter):
    """
    Google Gemini API adapter (`/v1beta/models/{model}:generateContent` & `/v1beta/models`).
    """

    def build_headers(self, api_key: Optional[str] = None) -> Dict[str, str]:
        resolved_key = api_key or self.get_api_key()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self.config.extra_headers,
        }
        if resolved_key:
            headers["x-goog-api-key"] = resolved_key
        return headers

    def get_generate_url(self, model: str) -> str:
        base = self.config.base_url.rstrip("/")
        clean_model = model.replace("models/", "")
        return f"{base}/models/{clean_model}:generateContent"

    def build_gemini_payload(
        self,
        messages: List[ChatMessage],
        tools: List[ToolDefinition],
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        contents: List[Dict[str, Any]] = []
        system_parts: List[str] = []
        for msg in messages:
            if msg.role == "system":
                system_parts.append(msg.content)
            else:
                role = "model" if msg.role == "assistant" else "user"
                contents.append({"role": role, "parts": [{"text": msg.content}]})

        payload: Dict[str, Any] = {
            "contents": contents or [{"role": "user", "parts": [{"text": "Hello"}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        if tools and self.config.supports_tools:
            payload["tools"] = [{"functionDeclarations": [t.to_gemini_tool() for t in tools]}]
        return payload

    def complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> CompletionResult:
        start_time = time.perf_counter()
        norm_messages = self.normalize_messages(messages)
        norm_tools = self.normalize_tools(tools)
        target_model = model or self.config.default_model

        url = self.get_generate_url(target_model)
        headers = self.build_headers(api_key=kwargs.get("api_key"))
        payload = self.build_gemini_payload(
            norm_messages, norm_tools, temperature=temperature, max_tokens=max_tokens
        )

        if not self._should_use_http_transport(**kwargs):
            return self._synthesize_offline_completion(norm_messages, model=target_model, tools=norm_tools)

        try:
            _, data, _ = self._execute_http_with_retry(
                method="POST",
                url=url,
                headers=headers,
                payload=payload,
                max_retries=kwargs.get("max_retries"),
            )
        except ProviderError as err:
            if (
                self.transport_handler is None
                and self.simulated_failure is None
                and err.status_code == 503
                and self.allow_offline_synthesis
                and not kwargs.get("strict_network", False)
            ):
                return self._synthesize_offline_completion(norm_messages, model=target_model, tools=norm_tools)
            raise

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        content_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []

        if isinstance(data, dict):
            candidates = data.get("candidates", [])
            if candidates and isinstance(candidates[0], dict):
                parts = candidates[0].get("content", {}).get("parts", [])
                for idx, part in enumerate(parts):
                    if isinstance(part, dict):
                        if "text" in part:
                            content_parts.append(str(part["text"]))
                        if "functionCall" in part and isinstance(part["functionCall"], dict):
                            fc = part["functionCall"]
                            tool_calls.append(
                                {
                                    "id": f"call_gemini_{idx + 1}",
                                    "type": "function",
                                    "function": {
                                        "name": str(fc.get("name", "")),
                                        "arguments": dict(fc.get("args", {})),
                                    },
                                }
                            )

        content = "\n".join(content_parts).strip()
        usage_meta = data.get("usageMetadata", {}) if isinstance(data, dict) else {}
        prompt_tokens = int(
            usage_meta.get("promptTokenCount")
            or sum(self.estimate_tokens(m.content) for m in norm_messages)
        )
        completion_tokens = int(
            usage_meta.get("candidatesTokenCount") or self.estimate_tokens(content)
        )
        cost_usd = self.estimate_cost(prompt_tokens, completion_tokens, model=target_model)

        usage = UsageMetrics(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            provider_id=self.provider_id,
            model_id=target_model,
        )
        return CompletionResult(
            content=content,
            model=target_model,
            provider=self.provider_id,
            tool_calls=tool_calls,
            usage=usage,
            raw_response=data if isinstance(data, dict) else {"data": data},
        )

    def stream_complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> Iterator[Dict[str, Any]]:
        result = self.complete(messages, model=model, tools=tools, temperature=temperature, max_tokens=max_tokens, **kwargs)
        for idx, token in enumerate(result.content.split(" ")):
            yield {"delta": token if idx == 0 else f" {token}", "model": result.model, "provider": self.provider_id, "done": False}
        yield {"delta": "", "model": result.model, "provider": self.provider_id, "done": True, "usage": result.usage.to_dict()}

    def _merge_gemini_models_payload(self, catalog: Dict[str, Dict[str, Any]], data: Any) -> None:
        if not (isinstance(data, dict) and isinstance(data.get("models"), list)):
            return
        for item in data["models"]:
            if not (isinstance(item, dict) and item.get("name")):
                continue
            methods = item.get("supportedGenerationMethods")
            if isinstance(methods, list) and "generateContent" not in methods:
                continue
            raw_name = str(item["name"])
            mid = raw_name.split("/", 1)[-1] if raw_name.startswith("models/") else raw_name
            catalog[mid] = {
                "id": mid,
                "name": str(item.get("displayName", mid)),
                "provider": self.provider_id,
                "supports_tools": True,
                "supports_streaming": True,
                "cost_per_1k_input": self.config.cost_per_1k_input,
                "cost_per_1k_output": self.config.cost_per_1k_output,
                "is_free": True,
            }
        self.config.models = list(catalog.keys())

    def discover_models(self) -> List[Dict[str, Any]]:
        catalog = {
            mid: {
                "id": mid,
                "name": mid,
                "provider": self.provider_id,
                "supports_tools": True,
                "supports_streaming": True,
                "cost_per_1k_input": self.config.cost_per_1k_input,
                "cost_per_1k_output": self.config.cost_per_1k_output,
                "is_free": True,
            }
            for mid in self.config.models
        }
        api_key = self.get_api_key()
        if (self.transport_handler is not None or self._should_use_http_transport()) and api_key:
            try:
                url = f"{self.config.base_url.rstrip('/')}/models?key={api_key}"
                status, data, _ = self._raw_http_call("GET", url, {"Accept": "application/json"}, None)
                if status == 200:
                    self._merge_gemini_models_payload(catalog, data)
            except Exception:
                pass
        return list(catalog.values())

    def test_connection(self, live_network: bool = False) -> Dict[str, Any]:
        start = time.perf_counter()
        api_key = self.get_api_key()
        if self.config.requires_api_key and not api_key and self.transport_handler is None:
            return {
                "provider_id": self.provider_id,
                "name": self.config.name,
                "ok": False,
                "status": "missing_api_key",
                "status_code": 401,
                "endpoint": self.get_generate_url(self.config.default_model),
                "has_api_key": False,
                "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                "models_available": len(self.config.models),
                "message": "Google Gemini API key not configured.",
            }
        if self.transport_handler is not None or live_network:
            try:
                base = self.config.base_url.rstrip("/")
                probe_url = f"{base}/models?key={api_key}" if api_key else f"{base}/models"
                status, data, _ = self._raw_http_call("GET", probe_url, {"Accept": "application/json"}, None)
                if status != 200:
                    err_detail = self._redact_secret_text(str(data))
                    if api_key:
                        err_detail = err_detail.replace(api_key, "[REDACTED]")
                    return {
                        "provider_id": self.provider_id,
                        "name": self.config.name,
                        "ok": False,
                        "status": "error",
                        "status_code": status,
                        "endpoint": f"{base}/models",
                        "has_api_key": bool(api_key),
                        "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                        "models_available": len(self.config.models),
                        "message": f"Google Gemini API returned HTTP {status}: {err_detail}",
                    }
                catalog = {
                    mid: {
                        "id": mid,
                        "name": mid,
                        "provider": self.provider_id,
                        "supports_tools": True,
                        "supports_streaming": True,
                        "cost_per_1k_input": self.config.cost_per_1k_input,
                        "cost_per_1k_output": self.config.cost_per_1k_output,
                        "is_free": True,
                    }
                    for mid in self.config.models
                }
                self._merge_gemini_models_payload(catalog, data)
                models = list(catalog.values())
                return {
                    "provider_id": self.provider_id,
                    "name": self.config.name,
                    "ok": True,
                    "status": "connected",
                    "status_code": 200,
                    "endpoint": f"{base}/models",
                    "has_api_key": bool(api_key),
                    "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                    "models_available": len(models),
                    "models": [m["id"] for m in models],
                    "message": f"Google Gemini API verified ({len(models)} models available for key).",
                }
            except Exception as exc:
                msg = self._redact_secret_text(str(exc))
                if api_key:
                    msg = msg.replace(api_key, "[REDACTED]")
                return {
                    "provider_id": self.provider_id,
                    "name": self.config.name,
                    "ok": False,
                    "status": "error",
                    "endpoint": f"{self.config.base_url.rstrip('/')}/models",
                    "has_api_key": bool(api_key),
                    "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                    "models_available": len(self.config.models),
                    "message": msg,
                }
        return {
            "provider_id": self.provider_id,
            "name": self.config.name,
            "ok": True,
            "status": "configured",
            "status_code": 200,
            "endpoint": self.get_generate_url(self.config.default_model),
            "has_api_key": bool(api_key),
            "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
            "models_available": len(self.config.models),
            "message": "Google Gemini API adapter ready.",
        }


class TokenHarborAdapter(BaseProviderAdapter):
    """
    Token Harbor Verified API Dual-Endpoint Adapter (`provider_id="tokenharbor"`, Spec Section 2.2):
    Supports:
    1. OpenAI-compatible endpoint:
       `POST https://tokenharbor.ai/v1/chat/completions` with `Authorization: Bearer thk_live_...`
    2. Claude-compatible endpoint:
       `POST https://tokenharbor.ai/v1/messages` with `x-api-key: thk_live_...` or `Authorization: Bearer thk_live_...`
       and `anthropic-version: 2023-06-01`
    3. Dynamic catalog discovery:
       `GET https://tokenharbor.ai/v1/models`
    """

    CHAT_COMPLETIONS_ENDPOINT = "https://tokenharbor.ai/v1/chat/completions"
    MESSAGES_ENDPOINT = "https://tokenharbor.ai/v1/messages"
    MODELS_ENDPOINT = "https://tokenharbor.ai/v1/models"

    def get_endpoint_url(self, endpoint_mode: Optional[str] = None, model: Optional[str] = None) -> str:
        mode = (endpoint_mode or self.config.endpoint_mode or "auto").lower()
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions") or base.endswith("/messages"):
            base = base.rsplit("/", 1)[0]
            if base.endswith("/chat"):
                base = base.rsplit("/", 1)[0]

        if mode in ("anthropic", "messages", "claude"):
            return self.config.secondary_base_url or f"{base}/messages"
        if mode in ("openai", "chat/completions", "chat_completions"):
            return f"{base}/chat/completions"

        # Auto mode: if model is explicitly a Claude messages model and secondary_base_url is requested via mode,
        # default to `/v1/chat/completions` unless `messages` mode is selected.
        return f"{base}/chat/completions"

    def build_headers(
        self,
        endpoint_mode: str = "openai",
        api_key: Optional[str] = None,
        auth_style: str = "dual",
    ) -> Dict[str, str]:
        resolved_key = api_key or self.get_api_key()
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self.config.extra_headers,
        }
        mode = endpoint_mode.lower()
        if mode in ("anthropic", "messages", "claude"):
            headers["anthropic-version"] = "2023-06-01"
            if resolved_key:
                if auth_style == "bearer":
                    headers["Authorization"] = f"Bearer {resolved_key}"
                elif auth_style == "x-api-key":
                    headers["x-api-key"] = resolved_key
                else:
                    # Dual compatibility: include both x-api-key and Authorization Bearer
                    headers["x-api-key"] = resolved_key
                    headers["Authorization"] = f"Bearer {resolved_key}"
        else:
            if resolved_key:
                headers["Authorization"] = f"Bearer {resolved_key}"
        return headers

    def complete_openai(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> CompletionResult:
        """Execute completion specifically against `https://tokenharbor.ai/v1/chat/completions`."""
        return self.complete(
            messages=messages,
            model=model,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            endpoint_mode="openai",
            **kwargs,
        )

    def complete_messages(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> CompletionResult:
        """Execute completion specifically against `https://tokenharbor.ai/v1/messages` (Claude format)."""
        return self.complete(
            messages=messages,
            model=model,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            endpoint_mode="messages",
            **kwargs,
        )

    def complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> CompletionResult:
        start_time = time.perf_counter()
        norm_messages = self.normalize_messages(messages)
        norm_tools = self.normalize_tools(tools)
        target_model = model or self.config.default_model

        endpoint_mode = str(kwargs.get("endpoint_mode") or self.config.endpoint_mode or "openai").lower()
        if kwargs.get("use_messages_endpoint"):
            endpoint_mode = "messages"

        use_messages_api = endpoint_mode in ("anthropic", "messages", "claude")
        url = self.get_endpoint_url(endpoint_mode="messages" if use_messages_api else "openai", model=target_model)
        headers = self.build_headers(
            endpoint_mode="messages" if use_messages_api else "openai",
            api_key=kwargs.get("api_key"),
            auth_style=str(kwargs.get("auth_style", "dual")),
        )

        if use_messages_api:
            system_parts = [m.content for m in norm_messages if m.role == "system"]
            claude_msgs = [
                {"role": "assistant" if m.role == "assistant" else "user", "content": m.content}
                for m in norm_messages
                if m.role != "system"
            ] or [{"role": "user", "content": "Hello"}]
            payload: Dict[str, Any] = {
                "model": target_model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": claude_msgs,
            }
            if system_parts:
                payload["system"] = "\n\n".join(system_parts)
            if norm_tools and self.config.supports_tools:
                payload["tools"] = [t.to_anthropic_tool() for t in norm_tools]
        else:
            payload = {
                "model": target_model,
                "messages": [m.to_dict() for m in norm_messages],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if norm_tools and self.config.supports_tools:
                payload["tools"] = [t.to_openai_tool() for t in norm_tools]
                payload["tool_choice"] = kwargs.get("tool_choice", "auto")

        if not self._should_use_http_transport(**kwargs):
            return self._synthesize_offline_completion(
                norm_messages,
                model=target_model,
                tools=norm_tools,
                limitations_note="Token Harbor verified dual-endpoint responder active.",
            )

        try:
            _, data, _ = self._execute_http_with_retry(
                method="POST",
                url=url,
                headers=headers,
                payload=payload,
                max_retries=kwargs.get("max_retries"),
            )
        except ProviderError as err:
            if (
                self.transport_handler is None
                and self.simulated_failure is None
                and err.status_code == 503
                and self.allow_offline_synthesis
                and not kwargs.get("strict_network", False)
            ):
                return self._synthesize_offline_completion(
                    norm_messages,
                    model=target_model,
                    tools=norm_tools,
                    limitations_note="Token Harbor offline responder active.",
                )
            raise

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        content = ""
        tool_calls: List[Dict[str, Any]] = []
        resolved_model = target_model
        finish_reason = "stop"

        if isinstance(data, dict):
            if use_messages_api or ("content" in data and isinstance(data.get("content"), list)):
                content, tool_calls, resolved_model, finish_reason = AnthropicAdapter.parse_claude_response(
                    data, target_model
                )
            elif "choices" in data and isinstance(data["choices"], list) and data["choices"]:
                choice = data["choices"][0]
                msg = choice.get("message", {})
                content = str(msg.get("content") or "")
                resolved_model = str(data.get("model", target_model))
                finish_reason = str(choice.get("finish_reason", "stop"))
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        args = fn.get("arguments", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                args = {"raw": args}
                        tool_calls.append(
                            {
                                "id": tc.get("id", "call_th_1"),
                                "type": "function",
                                "function": {"name": fn.get("name", ""), "arguments": args},
                            }
                        )

        usage_raw = data.get("usage", {}) if isinstance(data, dict) else {}
        prompt_tokens = int(
            usage_raw.get("prompt_tokens")
            or usage_raw.get("input_tokens")
            or sum(self.estimate_tokens(m.content) for m in norm_messages)
        )
        completion_tokens = int(
            usage_raw.get("completion_tokens")
            or usage_raw.get("output_tokens")
            or self.estimate_tokens(content)
        )
        cost_usd = self.estimate_cost(prompt_tokens, completion_tokens, model=resolved_model)

        usage = UsageMetrics(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            provider_id=self.provider_id,
            model_id=resolved_model,
        )
        return CompletionResult(
            content=content,
            model=resolved_model,
            provider=self.provider_id,
            tool_calls=tool_calls,
            usage=usage,
            raw_response=data if isinstance(data, dict) else {"data": data},
            fallback_used=False,
            finish_reason=finish_reason,
        )

    def stream_complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> Iterator[Dict[str, Any]]:
        result = self.complete(messages, model=model, tools=tools, temperature=temperature, max_tokens=max_tokens, **kwargs)
        for idx, token in enumerate(result.content.split(" ")):
            yield {"delta": token if idx == 0 else f" {token}", "model": result.model, "provider": self.provider_id, "done": False}
        yield {"delta": "", "model": result.model, "provider": self.provider_id, "done": True, "usage": result.usage.to_dict()}

    def discover_models(self) -> List[Dict[str, Any]]:
        """
        Dynamic catalog discovery via `GET https://tokenharbor.ai/v1/models`,
        merged with default Token Harbor verified models.
        """
        catalog: Dict[str, Dict[str, Any]] = {
            mid: {
                "id": mid,
                "name": mid,
                "provider": "tokenharbor",
                "endpoints": ["/v1/chat/completions", "/v1/messages"],
                "supports_tools": True,
                "supports_streaming": True,
                "cost_per_1k_input": self.config.cost_per_1k_input,
                "cost_per_1k_output": self.config.cost_per_1k_output,
            }
            for mid in self.config.models
        }
        if self._should_use_http_transport():
            base = self.config.base_url.rstrip("/")
            if base.endswith("/chat/completions") or base.endswith("/messages"):
                base = base.rsplit("/", 1)[0]
            url = f"{base}/models"
            headers = self.build_headers(endpoint_mode="openai")
            try:
                status, data, _ = self._raw_http_call("GET", url, headers, None)
                if status == 200 and isinstance(data, dict) and isinstance(data.get("data"), list):
                    for item in data["data"]:
                        if isinstance(item, dict) and item.get("id"):
                            mid = str(item["id"])
                            catalog[mid] = {
                                "id": mid,
                                "name": str(item.get("name", mid)),
                                "provider": "tokenharbor",
                                "endpoints": ["/v1/chat/completions", "/v1/messages"],
                                "supports_tools": bool(item.get("supports_tools", True)),
                                "supports_streaming": bool(item.get("supports_streaming", True)),
                                "cost_per_1k_input": float(item.get("cost_per_1k_input", self.config.cost_per_1k_input)),
                                "cost_per_1k_output": float(item.get("cost_per_1k_output", self.config.cost_per_1k_output)),
                            }
            except Exception:
                pass
        return list(catalog.values())

    def test_connection(self, live_network: bool = False) -> Dict[str, Any]:
        start = time.perf_counter()
        api_key = self.get_api_key()
        if self.simulated_failure and self.simulated_failure.get("remaining", 0) > 0:
            code = int(self.simulated_failure.get("status_code", 429))
            return {
                "provider_id": self.provider_id,
                "name": self.config.name,
                "ok": False,
                "status": "error",
                "status_code": code,
                "endpoint": self.get_endpoint_url("openai"),
                "secondary_endpoint": self.get_endpoint_url("messages"),
                "has_api_key": bool(api_key),
                "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                "models_available": len(self.config.models),
                "message": f"Simulated failure (HTTP {code})",
            }

        if self.transport_handler is not None or live_network:
            try:
                models = self.discover_models()
                return {
                    "provider_id": self.provider_id,
                    "name": self.config.name,
                    "ok": True,
                    "status": "connected",
                    "status_code": 200,
                    "endpoint": self.get_endpoint_url("openai"),
                    "secondary_endpoint": self.get_endpoint_url("messages"),
                    "has_api_key": bool(api_key),
                    "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                    "models_available": len(models),
                    "message": "Token Harbor dual-endpoint API connected.",
                }
            except Exception as exc:
                return {
                    "provider_id": self.provider_id,
                    "name": self.config.name,
                    "ok": False,
                    "status": "error",
                    "endpoint": self.get_endpoint_url("openai"),
                    "secondary_endpoint": self.get_endpoint_url("messages"),
                    "has_api_key": bool(api_key),
                    "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                    "models_available": len(self.config.models),
                    "message": str(exc),
                }

        return {
            "provider_id": self.provider_id,
            "name": self.config.name,
            "ok": True,
            "status": "configured" if api_key else "ready_fallback",
            "status_code": 200,
            "endpoint": self.get_endpoint_url("openai"),
            "secondary_endpoint": self.get_endpoint_url("messages"),
            "has_api_key": bool(api_key),
            "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
            "models_available": len(self.config.models),
            "message": "Token Harbor dual-endpoint adapter (`/v1/chat/completions` & `/v1/messages`) ready.",
        }


class CustomProviderAdapter(OpenAICompatibleAdapter):
    """
    Custom OpenAI / Anthropic compatible endpoint adapter (`provider_id="custom"`).
    Delegates to Anthropic format when `endpoint_mode == "anthropic"` or `/messages` URL is used.
    """

    def complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> CompletionResult:
        mode = (kwargs.get("endpoint_mode") or self.config.endpoint_mode or "openai").lower()
        if mode in ("anthropic", "messages") or self.config.base_url.rstrip("/").endswith("/messages"):
            anth = AnthropicAdapter(
                config=self.config,
                vault=self.vault,
                transport_handler=self.transport_handler,
                backoff_sleep_fn=self._sleep,
            )
            anth.simulated_failure = self.simulated_failure
            anth.allow_offline_synthesis = self.allow_offline_synthesis
            return anth.complete(
                messages=messages,
                model=model,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        return super().complete(
            messages=messages,
            model=model,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )


def get_default_provider_configs() -> Dict[str, ProviderConfig]:
    """Return default ProviderConfig definitions for all 13 supported providers in BEL Code v2.1."""
    return {
        "freellm": default_freellm_config(),
        "openai": ProviderConfig(
            provider_id="openai",
            name="OpenAI",
            base_url="https://api.openai.com/v1",
            default_model="gpt-4o",
            enabled=True,
            auth_type="bearer",
            requires_api_key=True,
            models=["gpt-4o", "gpt-4o-mini", "o3-mini", "gpt-4.1", "gpt-4.1-mini"],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=True,
            rpm_limit=500,
            tpm_limit=200_000,
            cost_per_1k_input=0.0025,
            cost_per_1k_output=0.0100,
        ),
        "anthropic": ProviderConfig(
            provider_id="anthropic",
            name="Anthropic Claude",
            base_url="https://api.anthropic.com/v1",
            default_model="claude-3-7-sonnet-20250219",
            enabled=True,
            auth_type="x-api-key",
            requires_api_key=True,
            endpoint_mode="anthropic",
            models=[
                "claude-3-7-sonnet-20250219",
                "claude-3-5-sonnet-20241022",
                "claude-3-5-haiku-20241022",
                "claude-3-opus-20240229",
            ],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=True,
            rpm_limit=100,
            tpm_limit=160_000,
            cost_per_1k_input=0.0030,
            cost_per_1k_output=0.0150,
        ),
        "gemini": ProviderConfig(
            provider_id="gemini",
            name="Google Gemini API",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            default_model="gemini-2.5-flash",
            enabled=True,
            auth_type="gemini",
            requires_api_key=True,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-2.5-pro"],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=True,
            rpm_limit=15,
            tpm_limit=250_000,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
        ),
        "groq": ProviderConfig(
            provider_id="groq",
            name="Groq",
            base_url="https://api.groq.com/openai/v1",
            default_model="llama-3.3-70b-versatile",
            enabled=True,
            auth_type="bearer",
            requires_api_key=True,
            models=["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=False,
            rpm_limit=30,
            tpm_limit=8_000,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
        ),
        "openrouter": ProviderConfig(
            provider_id="openrouter",
            name="OpenRouter",
            base_url="https://openrouter.ai/api/v1",
            default_model="openrouter/free",
            enabled=True,
            auth_type="bearer",
            requires_api_key=True,
            models=["openrouter/free"],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=True,
            rpm_limit=20,
            tpm_limit=100_000,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
            extra_headers={"HTTP-Referer": "https://bel-code.local", "X-Title": "BEL Code v2.1"},
        ),
        "deepseek": ProviderConfig(
            provider_id="deepseek",
            name="DeepSeek",
            base_url="https://api.deepseek.com/v1",
            default_model="deepseek-chat",
            enabled=True,
            auth_type="bearer",
            requires_api_key=True,
            models=["deepseek-chat", "deepseek-reasoner", "deepseek-coder"],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=False,
            rpm_limit=120,
            tpm_limit=150_000,
            cost_per_1k_input=0.00027,
            cost_per_1k_output=0.00110,
        ),
        "mistral": ProviderConfig(
            provider_id="mistral",
            name="Mistral AI",
            base_url="https://api.mistral.ai/v1",
            default_model="codestral-latest",
            enabled=True,
            auth_type="bearer",
            requires_api_key=True,
            models=["codestral-latest", "mistral-small-latest", "mistral-large-latest"],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=False,
            rpm_limit=30,
            tpm_limit=100_000,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
        ),
        "together": ProviderConfig(
            provider_id="together",
            name="Together AI",
            base_url="https://api.together.xyz/v1",
            default_model="Qwen/Qwen2.5-Coder-32B-Instruct",
            enabled=True,
            auth_type="bearer",
            requires_api_key=True,
            models=[
                "Qwen/Qwen2.5-Coder-32B-Instruct",
                "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                "deepseek-ai/DeepSeek-R1",
            ],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=False,
            rpm_limit=120,
            tpm_limit=150_000,
            cost_per_1k_input=0.0008,
            cost_per_1k_output=0.0008,
        ),
        "cerebras": ProviderConfig(
            provider_id="cerebras",
            name="Cerebras Cloud",
            base_url="https://api.cerebras.ai/v1",
            default_model="llama-3.3-70b",
            enabled=True,
            auth_type="bearer",
            requires_api_key=True,
            models=["llama-3.3-70b", "llama3.1-8b", "qwen-3-32b"],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=False,
            rpm_limit=30,
            tpm_limit=60_000,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
        ),
        "cloudflare": ProviderConfig(
            provider_id="cloudflare",
            name="Cloudflare Workers AI",
            base_url="https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
            default_model="@cf/qwen/qwen2.5-coder-32b-instruct",
            enabled=True,
            auth_type="bearer",
            requires_api_key=True,
            models=[
                "@cf/qwen/qwen2.5-coder-32b-instruct",
                "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                "@cf/openai/gpt-oss-20b",
            ],
            supports_streaming=True,
            supports_tools=False,
            supports_vision=False,
            rpm_limit=60,
            tpm_limit=100_000,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
        ),
        "nvidia": ProviderConfig(
            provider_id="nvidia",
            name="NVIDIA NIM",
            base_url="https://integrate.api.nvidia.com/v1",
            default_model="qwen/qwen2.5-coder-32b-instruct",
            enabled=True,
            auth_type="bearer",
            requires_api_key=True,
            models=[
                "qwen/qwen2.5-coder-32b-instruct",
                "meta/llama-3.3-70b-instruct",
                "deepseek-ai/deepseek-r1",
            ],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=False,
            rpm_limit=40,
            tpm_limit=100_000,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
        ),
        "sambanova": ProviderConfig(
            provider_id="sambanova",
            name="SambaNova Cloud",
            base_url="https://api.sambanova.ai/v1",
            default_model="Meta-Llama-3.3-70B-Instruct",
            enabled=True,
            auth_type="bearer",
            requires_api_key=True,
            models=["Meta-Llama-3.3-70B-Instruct", "DeepSeek-V3.1", "gpt-oss-120b"],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=False,
            rpm_limit=20,
            tpm_limit=128_000,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
        ),
        "huggingface": ProviderConfig(
            provider_id="huggingface",
            name="Hugging Face Inference Providers",
            base_url="https://router.huggingface.co/v1",
            default_model="Qwen/Qwen2.5-Coder-32B-Instruct",
            enabled=True,
            auth_type="bearer",
            requires_api_key=True,
            models=[
                "Qwen/Qwen2.5-Coder-32B-Instruct",
                "meta-llama/Llama-3.3-70B-Instruct",
                "deepseek-ai/DeepSeek-R1",
            ],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=False,
            rpm_limit=30,
            tpm_limit=60_000,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
        ),
        "github_models": ProviderConfig(
            provider_id="github_models",
            name="GitHub Models",
            base_url="https://models.github.ai/inference",
            default_model="openai/gpt-4o-mini",
            enabled=True,
            auth_type="bearer",
            requires_api_key=True,
            models=["openai/gpt-4o-mini", "openai/gpt-4o", "deepseek/DeepSeek-R1"],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=False,
            rpm_limit=15,
            tpm_limit=60_000,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
        ),
        "ollama": ProviderConfig(
            provider_id="ollama",
            name="Ollama (Local AI)",
            base_url="http://localhost:11434/v1",
            default_model="qwen2.5-coder:7b",
            enabled=True,
            auth_type="none",
            requires_api_key=False,
            models=["qwen2.5-coder:7b", "llama3.2", "deepseek-r1:7b"],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=False,
            rpm_limit=600,
            tpm_limit=1_000_000,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
        ),
        "fireworks": ProviderConfig(
            provider_id="fireworks",
            name="Fireworks AI",
            base_url="https://api.fireworks.ai/inference/v1",
            default_model="accounts/fireworks/models/qwen2p5-coder-32b-instruct",
            enabled=True,
            auth_type="bearer",
            requires_api_key=True,
            models=[
                "accounts/fireworks/models/qwen2p5-coder-32b-instruct",
                "accounts/fireworks/models/deepseek-r1",
                "accounts/fireworks/models/llama-v3p3-70b-instruct",
            ],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=False,
            rpm_limit=120,
            tpm_limit=150_000,
            cost_per_1k_input=0.0009,
            cost_per_1k_output=0.0009,
        ),
        "tokenharbor": ProviderConfig(
            provider_id="tokenharbor",
            name="Token Harbor Verified API",
            base_url="https://tokenharbor.ai/v1",
            secondary_base_url="https://tokenharbor.ai/v1/messages",
            default_model="th-claude-3-7-sonnet",
            enabled=True,
            auth_type="dual",
            requires_api_key=False,
            endpoint_mode="openai",
            models=[
                "th-claude-3-7-sonnet",
                "th-gpt-4o",
                "th-deepseek-r1",
                "th-qwen-2.5-coder-32b",
                "th-fast-coder",
            ],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=True,
            rpm_limit=240,
            tpm_limit=250_000,
            cost_per_1k_input=0.0002,
            cost_per_1k_output=0.0006,
        ),
        "custom": ProviderConfig(
            provider_id="custom",
            name="Custom OpenAI/Anthropic Endpoint",
            base_url="http://localhost:8080/v1",
            default_model="custom-model",
            enabled=True,
            auth_type="optional_bearer",
            requires_api_key=False,
            endpoint_mode="openai",
            models=["custom-model", "local-coder"],
            supports_streaming=True,
            supports_tools=True,
            supports_vision=False,
            rpm_limit=120,
            tpm_limit=120_000,
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
        ),
    }


PROVIDER_ADAPTER_CLASSES: Dict[str, Type[BaseProviderAdapter]] = {
    "freellm": FreeLLMAdapter,
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "gemini": GeminiAdapter,
    "groq": GroqAdapter,
    "openrouter": OpenRouterAdapter,
    "deepseek": DeepSeekAdapter,
    "mistral": MistralAdapter,
    "together": TogetherAdapter,
    "cerebras": CerebrasAdapter,
    "fireworks": FireworksAdapter,
    "cloudflare": CloudflareAdapter,
    "nvidia": NvidiaAdapter,
    "sambanova": SambaNovaAdapter,
    "huggingface": HuggingFaceAdapter,
    "github_models": GitHubModelsAdapter,
    "ollama": OllamaAdapter,
    "tokenharbor": TokenHarborAdapter,
    "custom": CustomProviderAdapter,
}


def create_provider_adapter(
    provider_id: str,
    config: Optional[ProviderConfig] = None,
    vault: Optional[SecretVault] = None,
    transport_handler: Optional[TransportCallable] = None,
    backoff_sleep_fn: Optional[Callable[[float], None]] = None,
) -> BaseProviderAdapter:
    """Instantiate the appropriate adapter for `provider_id`."""
    pid = provider_id.strip().lower()
    defaults = get_default_provider_configs()
    resolved_config = config or defaults.get(pid)
    if resolved_config is None:
        resolved_config = ProviderConfig(
            provider_id=pid,
            name=pid.title(),
            base_url="http://localhost:8080/v1",
            default_model="custom-model",
        )
    adapter_cls = PROVIDER_ADAPTER_CLASSES.get(pid, CustomProviderAdapter)
    return adapter_cls(
        config=resolved_config,
        vault=vault,
        transport_handler=transport_handler,
        backoff_sleep_fn=backoff_sleep_fn,
    )
