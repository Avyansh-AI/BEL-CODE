"""
FreeLLM API Primary Intelligence Provider Adapter for BEL Code v2.1 (Spec Section 2.1).

Supports both:
1. FreeLLMAPI OpenAI-compatible gateway endpoints:
   - `POST {base_url}/chat/completions` and `GET {base_url}/models`
   - Default base URL: `http://localhost:3001/v1`
   - Smart routing modes: `"auto"`, `"auto:fast"`, `"auto:smart"`, `"fusion"`, plus explicit model IDs
   - Bearer authentication (`Authorization: Bearer <key>`)
2. Direct FreeLLM REST endpoints:
   - `POST https://apifreellm.com/api/v1/chat` with `Authorization: Bearer <key>` and `{"message": "..."}`

Also provides:
- Dynamic model catalog discovery (`discover_models`)
- Streaming Server-Sent Events (SSE) parser (`stream_complete`, `parse_sse_lines`)
- Native tool calling + automatic structured JSON/XML prompt-engineered tool-calling fallback
  with explicit `limitations_note` documentation
- Rate-limit handling (HTTP 429 retry + exponential backoff)
- Connection testing (`test_connection`)
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple, Union

from bel_code.config.settings import SecretVault
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


FREELLM_SMART_ROUTING_MODELS: List[Dict[str, Any]] = [
    {
        "id": "auto",
        "name": "FreeLLM Auto Router (Balanced)",
        "provider": "freellm",
        "routing_mode": "auto",
        "supports_tools": True,
        "native_tool_calling": True,
        "tool_calling_mode": "native_with_structured_fallback",
        "structured_tool_fallback": True,
        "supports_streaming": True,
        "context_window": 64000,
        "max_output_tokens": 8192,
        "limit": {"context": 64000, "output": 8192},
        "rpm_limit": 30,
        "tpm_limit": 64000,
        "rate_limit": {"rpm": 30, "tpm": 64000, "tier": "free_gateway"},
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
        "description": "Balanced automatic routing across available free LLM backends.",
    },
    {
        "id": "auto:fast",
        "name": "FreeLLM Auto Fast (Low Latency)",
        "provider": "freellm",
        "routing_mode": "fast",
        "supports_tools": True,
        "native_tool_calling": True,
        "tool_calling_mode": "native_with_structured_fallback",
        "structured_tool_fallback": True,
        "supports_streaming": True,
        "context_window": 32000,
        "max_output_tokens": 4096,
        "limit": {"context": 32000, "output": 4096},
        "rpm_limit": 60,
        "tpm_limit": 64000,
        "rate_limit": {"rpm": 60, "tpm": 64000, "tier": "free_fast"},
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
        "description": "Low-latency routing optimized for routine code edits, search, and quick answers.",
    },
    {
        "id": "auto:smart",
        "name": "FreeLLM Auto Smart (High Reasoning)",
        "provider": "freellm",
        "routing_mode": "smart",
        "supports_tools": True,
        "native_tool_calling": True,
        "tool_calling_mode": "native_with_structured_fallback",
        "structured_tool_fallback": True,
        "supports_streaming": True,
        "context_window": 128000,
        "max_output_tokens": 16384,
        "limit": {"context": 128000, "output": 16384},
        "rpm_limit": 20,
        "tpm_limit": 128000,
        "rate_limit": {"rpm": 20, "tpm": 128000, "tier": "free_smart"},
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
        "description": "High-capability reasoning route for architecture, debugging, and security reviews.",
    },
    {
        "id": "fusion",
        "name": "FreeLLM Multi-Model Fusion",
        "provider": "freellm",
        "routing_mode": "fusion",
        "supports_tools": True,
        "native_tool_calling": True,
        "tool_calling_mode": "native_with_structured_fallback",
        "structured_tool_fallback": True,
        "supports_streaming": False,
        "context_window": 64000,
        "max_output_tokens": 8192,
        "limit": {"context": 64000, "output": 8192},
        "rpm_limit": 15,
        "tpm_limit": 64000,
        "rate_limit": {"rpm": 15, "tpm": 64000, "tier": "free_fusion"},
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
        "description": "Aggregates responses across multiple free models for high-consensus reasoning.",
    },
    {
        "id": "qwen-2.5-coder-32b",
        "name": "Qwen 2.5 Coder 32B (via FreeLLM)",
        "provider": "freellm",
        "routing_mode": "explicit",
        "supports_tools": True,
        "native_tool_calling": True,
        "tool_calling_mode": "native_with_structured_fallback",
        "structured_tool_fallback": True,
        "supports_streaming": True,
        "context_window": 32768,
        "max_output_tokens": 8192,
        "limit": {"context": 32768, "output": 8192},
        "rpm_limit": 30,
        "tpm_limit": 64000,
        "rate_limit": {"rpm": 30, "tpm": 64000, "tier": "free_explicit"},
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
        "description": "Specialized open-weights code generation and refactoring model.",
    },
    {
        "id": "deepseek-r1-free",
        "name": "DeepSeek R1 Reasoning (via FreeLLM)",
        "provider": "freellm",
        "routing_mode": "explicit",
        "supports_tools": False,
        "native_tool_calling": False,
        "tool_calling_mode": "structured_fallback",
        "structured_tool_fallback": True,
        "supports_streaming": True,
        "context_window": 64000,
        "max_output_tokens": 8192,
        "limit": {"context": 64000, "output": 8192},
        "rpm_limit": 20,
        "tpm_limit": 64000,
        "rate_limit": {"rpm": 20, "tpm": 64000, "tier": "free_reasoning"},
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
        "description": "Chain-of-thought reasoning model (uses structured XML/JSON prompt tool fallback).",
    },
    {
        "id": "llama-3.3-70b-free",
        "name": "Llama 3.3 70B Instruct (via FreeLLM)",
        "provider": "freellm",
        "routing_mode": "explicit",
        "supports_tools": True,
        "native_tool_calling": True,
        "tool_calling_mode": "native_with_structured_fallback",
        "structured_tool_fallback": True,
        "supports_streaming": True,
        "context_window": 64000,
        "max_output_tokens": 8192,
        "limit": {"context": 64000, "output": 8192},
        "rpm_limit": 30,
        "tpm_limit": 64000,
        "rate_limit": {"rpm": 30, "tpm": 64000, "tier": "free_explicit"},
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
        "description": "General software engineering and code synthesis model.",
    },
    {
        "id": "apifreellm-direct",
        "name": "ApiFreeLLM Direct REST Chat",
        "provider": "freellm",
        "routing_mode": "direct_rest",
        "supports_tools": False,
        "native_tool_calling": False,
        "tool_calling_mode": "structured_fallback",
        "structured_tool_fallback": True,
        "supports_streaming": False,
        "context_window": 16384,
        "max_output_tokens": 4096,
        "limit": {"context": 16384, "output": 4096},
        "rpm_limit": 12,
        "tpm_limit": 16384,
        "rate_limit": {"rpm": 12, "tpm": 16384, "tier": "direct_rest_free"},
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
        "description": "Direct REST endpoint (https://apifreellm.com/api/v1/chat) with prompt-engineered tool fallback.",
    },
]

# Models or modes that require structured JSON/XML prompt-engineered tool calling
NON_NATIVE_TOOL_MODELS = {
    "deepseek-r1-free",
    "apifreellm-direct",
    "free-basic",
    "no-native-tools",
}

DEFAULT_FREELLM_LIMITATIONS = [
    "FreeLLM free tiers enforce stricter rate limits (e.g., ~1 request per 5 seconds on direct apifreellm.com REST endpoints or upstream free quotas).",
    "Some underlying free models do not support native OpenAI `tools`/`tool_choice` parameters; BEL Code automatically applies a structured JSON/XML `<tool_call>` prompt-engineering fallback.",
    "Context window sizes vary across dynamically routed free upstream providers (typically 8K-64K tokens).",
]


LOCAL_GATEWAY_PORTS = (31415, 3001)
DEFAULT_FREELLM_BASE_URL = "http://localhost:31415/v1"


def _is_tcp_port_open(host: str, port: int, timeout: float = 0.15) -> bool:
    """Fast TCP probe used to detect which local FreeLLMAPI gateway port is active."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _is_default_local_gateway(url: str) -> bool:
    lowered = url.lower()
    return any(f"{h}:{p}" in lowered for h in ("localhost", "127.0.0.1") for p in LOCAL_GATEWAY_PORTS)


def default_freellm_config() -> ProviderConfig:
    """Create the default ProviderConfig for the primary FreeLLM API provider."""
    return ProviderConfig(
        provider_id="freellm",
        name="FreeLLM API (Primary Default)",
        base_url=DEFAULT_FREELLM_BASE_URL,
        secondary_base_url="https://apifreellm.com/api/v1/chat",
        default_model="auto",
        enabled=True,
        auth_type="optional_bearer",
        requires_api_key=False,
        endpoint_mode="auto",
        models=[m["id"] for m in FREELLM_SMART_ROUTING_MODELS],
        supports_streaming=True,
        supports_tools=True,
        supports_vision=False,
        rpm_limit=30,
        tpm_limit=64_000,
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
        timeout_seconds=20.0,
        max_retries=2,
        limitations=list(DEFAULT_FREELLM_LIMITATIONS),
    )


class FreeLLMAdapter(BaseProviderAdapter):
    """
    Primary intelligence provider adapter for FreeLLM API (`provider_id="freellm"`).
    Supports both OpenAI-compatible gateway (`/v1/chat/completions`, `/v1/models`)
    and direct FreeLLM REST endpoint (`https://apifreellm.com/api/v1/chat`).
    """

    def __init__(
        self,
        config: Optional[ProviderConfig] = None,
        vault: Optional[SecretVault] = None,
        transport_handler: Optional[TransportCallable] = None,
        backoff_sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        super().__init__(
            config=config or default_freellm_config(),
            vault=vault,
            transport_handler=transport_handler,
            backoff_sleep_fn=backoff_sleep_fn,
        )

    def _should_use_http_transport(self, **kwargs: Any) -> bool:
        """Execute live HTTP transport when super() enables it OR an API key is configured."""
        if super()._should_use_http_transport(**kwargs):
            return True
        if bool(self.get_api_key()):
            return True
        return False

    def _resolve_gateway_base_url(self) -> str:
        """Resolve active local gateway URL (preferring configured port, auto-detecting 31415/3001)."""
        base = self.config.base_url.rstrip("/")
        if self.transport_handler is not None or self.simulated_failure is not None:
            return base
        if not _is_default_local_gateway(base):
            return base
        # Probe configured local port first, then fallback standard local ports
        configured_port = 31415 if ":31415" in base else 3001
        if _is_tcp_port_open("127.0.0.1", configured_port):
            return base
        for candidate_port in LOCAL_GATEWAY_PORTS:
            if candidate_port != configured_port and _is_tcp_port_open("127.0.0.1", candidate_port):
                return f"http://127.0.0.1:{candidate_port}/v1"
        return base

    def _is_direct_rest_endpoint(self, model: str, endpoint_mode: Optional[str] = None) -> bool:
        mode = (endpoint_mode or self.config.endpoint_mode or "auto").lower()
        if mode == "direct_rest":
            return True
        if mode == "openai":
            return False
        base = self.config.base_url.rstrip("/").lower()
        if "apifreellm.com" in base or base.endswith("/api/v1/chat"):
            return True
        if model == "apifreellm-direct":
            return True
        return False

    def _build_headers(self, api_key: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self.config.extra_headers,
        }
        resolved_key = api_key or self.get_api_key()
        if resolved_key:
            headers["Authorization"] = f"Bearer {resolved_key}"
        return headers

    @staticmethod
    def _compile_messages_to_single_prompt(messages: List[ChatMessage]) -> str:
        """Compile multi-turn ChatMessages into a single prompt string for direct REST `/api/v1/chat`."""
        if len(messages) == 1 and messages[0].role == "user":
            return messages[0].content
        parts: List[str] = []
        for msg in messages:
            role_tag = msg.role.upper()
            parts.append(f"[{role_tag}]: {msg.content}")
        return "\n\n".join(parts)

    def _model_supports_native_tools(self, model: str, direct_rest: bool, force_prompt_tools: bool = False) -> bool:
        if force_prompt_tools or direct_rest or not self.config.supports_tools:
            return False
        if model in NON_NATIVE_TOOL_MODELS:
            return False
        return True

    def complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> CompletionResult:
        """
        Execute a chat completion against FreeLLM API (gateway or direct REST).
        Automatically handles:
        - Smart routing model IDs (`auto`, `auto:fast`, `auto:smart`, `fusion`)
        - Native tool calling or structured JSON/XML prompt-engineered fallback
        - HTTP 429 retry with exponential backoff
        - Explicit `limitations_note` reporting
        """
        start_time = time.perf_counter()
        norm_messages = self.normalize_messages(messages)
        norm_tools = self.normalize_tools(tools)
        target_model = model or self.config.default_model or "auto"
        endpoint_mode = kwargs.get("endpoint_mode")
        force_prompt_tools = bool(kwargs.get("force_prompt_tools", False))
        direct_rest = self._is_direct_rest_endpoint(target_model, endpoint_mode=endpoint_mode)

        native_tools_supported = self._model_supports_native_tools(
            target_model, direct_rest=direct_rest, force_prompt_tools=force_prompt_tools
        )
        limitations_notes: List[str] = []

        if norm_tools and not native_tools_supported:
            norm_messages = self.inject_prompt_tools(norm_messages, norm_tools)
            limitations_notes.append(
                f"Model/endpoint '{target_model}' does not support native OpenAI `tools`; "
                "applied structured XML/JSON `<tool_call>` prompt-engineering fallback."
            )

        if direct_rest:
            limitations_notes.append(
                "Using direct FreeLLM REST endpoint (`/api/v1/chat`); multi-turn history is serialized into a single `message` payload and free-tier rate limits apply."
            )

        headers = self._build_headers(api_key=kwargs.get("api_key"))

        if direct_rest:
            url = self.config.base_url.rstrip("/")
            if not url.endswith("/api/v1/chat") and "apifreellm.com" not in url:
                url = self.config.secondary_base_url or "https://apifreellm.com/api/v1/chat"
            payload: Dict[str, Any] = {
                "message": self._compile_messages_to_single_prompt(norm_messages),
                "model": target_model,
            }
        else:
            base = self._resolve_gateway_base_url()
            url = f"{base}/chat/completions"
            payload = {
                "model": target_model,
                "messages": [m.to_dict() for m in norm_messages],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if norm_tools and native_tools_supported:
                payload["tools"] = [t.to_openai_tool() for t in norm_tools]
                payload["tool_choice"] = kwargs.get("tool_choice", "auto")

        if not self._should_use_http_transport(**kwargs):
            note = " ".join(limitations_notes) if limitations_notes else (
                "FreeLLM free tier active ($0.00/token); subject to upstream rate limits and variable context windows."
            )
            return self._synthesize_offline_completion(
                norm_messages,
                model=target_model,
                tools=norm_tools,
                limitations_note=note,
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
            # If native tools caused a 400 error on a free upstream model, automatically retry with prompt-engineered tool fallback!
            if (
                norm_tools
                and native_tools_supported
                and err.status_code == 400
                and not kwargs.get("_retried_prompt_tools")
            ):
                return self.complete(
                    messages=messages,
                    model=target_model,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    force_prompt_tools=True,
                    _retried_prompt_tools=True,
                    **{k: v for k, v in kwargs.items() if k not in ("force_prompt_tools", "_retried_prompt_tools")},
                )

            # Clarify FreeLLMAPI key vs apifreellm.com mismatch or local gateway offline errors
            api_key_val = self.get_api_key() or kwargs.get("api_key") or ""
            if "apifreellm.com" in url and err.status_code in (401, 403):
                if str(api_key_val).startswith("freellmapi-"):
                    raise ProviderError(
                        "API key with prefix 'freellmapi-' is for a local/self-hosted FreeLLMAPI gateway "
                        "(e.g. http://localhost:31415/v1 or http://localhost:3001/v1), not apifreellm.com "
                        f"(HTTP {err.status_code}: {err}). Set Base URL to http://localhost:31415/v1.",
                        provider_id=self.provider_id,
                        status_code=err.status_code,
                    ) from err
                if "1010" in str(err):
                    raise ProviderError(
                        f"Cloudflare Error 1010 blocked the request to {url}. Check User-Agent/origin headers.",
                        provider_id=self.provider_id,
                        status_code=403,
                    ) from err

            # Only synthesize local offline response when running WITHOUT an API key in offline test mode
            is_default_localhost = _is_default_local_gateway(url)
            if (
                is_default_localhost
                and not api_key_val
                and self.transport_handler is None
                and self.simulated_failure is None
                and self.allow_offline_synthesis
                and not kwargs.get("strict_network", False)
                and not kwargs.get("live_network", False)
            ):
                limitations_notes.append(
                    f"Local FreeLLM gateway ({url}) is not currently running; served via built-in FreeLLM local offline engine."
                )
                synth = self._synthesize_offline_completion(
                    norm_messages,
                    model=target_model,
                    tools=norm_tools,
                    limitations_note=" ".join(limitations_notes),
                )
                return synth
            raise

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        content = ""
        tool_calls: List[Dict[str, Any]] = []
        resolved_model = target_model
        finish_reason = "stop"

        if isinstance(data, dict):
            if "choices" in data and isinstance(data["choices"], list) and data["choices"]:
                choice = data["choices"][0]
                msg_obj = choice.get("message", {}) if isinstance(choice, dict) else {}
                content = str(
                    msg_obj.get("content")
                    or msg_obj.get("reasoning_content")
                    or msg_obj.get("reasoning")
                    or ""
                )
                raw_tcs = msg_obj.get("tool_calls") or []
                if isinstance(raw_tcs, list):
                    for tc in raw_tcs:
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
                                    "function": {
                                        "name": fn.get("name", ""),
                                        "arguments": args,
                                    },
                                }
                            )
                resolved_model = str(data.get("model", target_model))
                finish_reason = str(choice.get("finish_reason", "stop"))
            else:
                # Direct REST format (`{"response": "...", "status": "success"}` or `{"reply": "..."}`)
                content = str(
                    data.get("response")
                    or data.get("reply")
                    or data.get("content")
                    or data.get("message")
                    or ""
                )
                resolved_model = str(data.get("model", target_model))

        # Parse structured XML/JSON tool calls if prompt-engineering fallback was used or present in content
        if not tool_calls and content:
            cleaned_content, prompt_tcs = self.extract_prompt_tool_calls(content)
            if prompt_tcs:
                content = cleaned_content
                tool_calls = prompt_tcs
                if not any("prompt-engineering" in n for n in limitations_notes):
                    limitations_notes.append(
                        "Extracted tool calls via structured XML/JSON prompt-engineering fallback."
                    )

        usage_raw = data.get("usage", {}) if isinstance(data, dict) else {}
        prompt_tokens = int(
            usage_raw.get("prompt_tokens")
            or sum(self.estimate_tokens(m.content) for m in norm_messages)
        )
        completion_tokens = int(
            usage_raw.get("completion_tokens") or self.estimate_tokens(content)
        )
        total_tokens = int(usage_raw.get("total_tokens") or (prompt_tokens + completion_tokens))

        if self.last_backoff_delays:
            limitations_notes.append(
                f"Recovered from FreeLLM rate limit / transient error after {len(self.last_backoff_delays)} backoff retry(s)."
            )

        note_str = " ".join(limitations_notes) if limitations_notes else (
            "FreeLLM free tier active ($0.00/token); subject to upstream rate limits and variable context windows."
        )

        usage = UsageMetrics(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=0.0,
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
            limitations_note=note_str,
            finish_reason=finish_reason,
        )

    @staticmethod
    def parse_sse_lines(lines: Iterable[str], default_model: str = "auto") -> Iterator[Dict[str, Any]]:
        """
        Parse Server-Sent Events (SSE) stream lines (`data: {...}` and `data: [DONE]`).
        Yields normalized delta dicts.
        """
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                yield {
                    "delta": "",
                    "model": default_model,
                    "provider": "freellm",
                    "done": True,
                    "finish_reason": "stop",
                }
                break
            try:
                payload = json.loads(data_str)
                choices = payload.get("choices", [])
                delta_text = ""
                finish_reason = None
                if choices and isinstance(choices[0], dict):
                    delta_obj = choices[0].get("delta", {})
                    delta_text = str(delta_obj.get("content") or "")
                    finish_reason = choices[0].get("finish_reason")
                elif "response" in payload or "delta" in payload:
                    delta_text = str(payload.get("delta") or payload.get("response") or "")
                yield {
                    "delta": delta_text,
                    "model": str(payload.get("model", default_model)),
                    "provider": "freellm",
                    "done": bool(finish_reason == "stop"),
                    "finish_reason": finish_reason,
                }
            except json.JSONDecodeError:
                continue

    def stream_complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> Iterator[Dict[str, Any]]:
        """
        Stream completion tokens as SSE events.
        If `sse_lines` is provided in `kwargs` (or returned by `transport_handler`), parses real SSE lines;
        otherwise falls back to chunking `complete()`.
        """
        target_model = model or self.config.default_model or "auto"
        sse_lines = kwargs.get("sse_lines")
        if sse_lines is not None:
            yield from self.parse_sse_lines(sse_lines, default_model=target_model)
            return

        result = self.complete(
            messages=messages,
            model=target_model,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        words = result.content.split(" ")
        for idx, word in enumerate(words):
            piece = word if idx == 0 else f" {word}"
            yield {
                "delta": piece,
                "model": result.model,
                "provider": self.provider_id,
                "done": False,
                "finish_reason": None,
            }
        yield {
            "delta": "",
            "model": result.model,
            "provider": self.provider_id,
            "done": True,
            "finish_reason": result.finish_reason,
            "usage": result.usage.to_dict(),
            "limitations_note": result.limitations_note,
        }

    def discover_models(self) -> List[Dict[str, Any]]:
        """
        Discover available models from the FreeLLM gateway `GET /v1/models` endpoint,
        merging with built-in FreeLLM smart routing modes (`auto`, `auto:fast`, `auto:smart`, `fusion`).
        """
        catalog_by_id: Dict[str, Dict[str, Any]] = {
            m["id"]: dict(m) for m in FREELLM_SMART_ROUTING_MODELS
        }

        if self._should_use_http_transport() and not self._is_direct_rest_endpoint(self.config.default_model):
            base = self._resolve_gateway_base_url()
            url = f"{base}/models"
            headers = self._build_headers()
            try:
                status, data, _ = self._raw_http_call("GET", url, headers, None)
                if status == 200 and isinstance(data, dict) and isinstance(data.get("data"), list):
                    for item in data["data"]:
                        if isinstance(item, dict) and item.get("id"):
                            mid = str(item["id"])
                            native_tools = bool(item.get("supports_tools", mid not in NON_NATIVE_TOOL_MODELS))
                            ctx_win = int(item.get("context_window", 32768))
                            max_out = int(item.get("max_output_tokens", 8192))
                            rpm = int(item.get("rpm_limit", self.config.rpm_limit or 30))
                            tpm = int(item.get("tpm_limit", self.config.tpm_limit or 64000))
                            catalog_by_id[mid] = {
                                "id": mid,
                                "name": str(item.get("name", mid)),
                                "provider": "freellm",
                                "routing_mode": item.get("routing_mode", "dynamic"),
                                "supports_tools": native_tools,
                                "native_tool_calling": native_tools,
                                "tool_calling_mode": (
                                    "native_with_structured_fallback"
                                    if native_tools
                                    else "structured_fallback"
                                ),
                                "structured_tool_fallback": True,
                                "supports_streaming": bool(item.get("supports_streaming", True)),
                                "context_window": ctx_win,
                                "max_output_tokens": max_out,
                                "limit": {"context": ctx_win, "output": max_out},
                                "rpm_limit": rpm,
                                "tpm_limit": tpm,
                                "rate_limit": {"rpm": rpm, "tpm": tpm, "tier": "free_discovered"},
                                "cost_per_1k_input": 0.0,
                                "cost_per_1k_output": 0.0,
                                "description": str(item.get("description", "Discovered from FreeLLM gateway.")),
                            }
            except Exception:
                pass

        return list(catalog_by_id.values())

    def get_model_metadata(self, model_id: Optional[str] = None) -> Dict[str, Any]:
        """Return full capability and tool-calling metadata for a specific FreeLLM model."""
        target = (model_id or self.config.default_model or "auto").strip()
        for m in self.discover_models():
            if m["id"] == target:
                return dict(m)
        native_tools = self._model_supports_native_tools(
            target, direct_rest=self._is_direct_rest_endpoint(target)
        )
        return {
            "id": target,
            "name": target,
            "provider": "freellm",
            "routing_mode": "custom",
            "supports_tools": native_tools,
            "native_tool_calling": native_tools,
            "tool_calling_mode": "native_with_structured_fallback" if native_tools else "structured_fallback",
            "structured_tool_fallback": True,
            "supports_streaming": not self._is_direct_rest_endpoint(target),
            "context_window": 32768,
            "max_output_tokens": 8192,
            "limit": {"context": 32768, "output": 8192},
            "rpm_limit": self.config.rpm_limit or 30,
            "tpm_limit": self.config.tpm_limit or 64000,
            "rate_limit": {
                "rpm": self.config.rpm_limit or 30,
                "tpm": self.config.tpm_limit or 64000,
                "tier": "free_custom",
            },
            "cost_per_1k_input": 0.0,
            "cost_per_1k_output": 0.0,
        }

    def test_connection(self, live_network: bool = False) -> Dict[str, Any]:
        """
        Test connectivity and readiness of the FreeLLM API provider.
        Returns status, endpoint mode, available models count, latency, and documented limitations.
        """
        start = time.perf_counter()
        api_key = self.get_api_key()
        direct_rest = self._is_direct_rest_endpoint(self.config.default_model)
        resolved_base = self._resolve_gateway_base_url()
        endpoint = (
            resolved_base
            if not direct_rest
            else (
                self.config.base_url.rstrip("/")
                if "apifreellm.com" in self.config.base_url.lower()
                else (self.config.secondary_base_url or "https://apifreellm.com/api/v1/chat")
            )
        )

        if self.simulated_failure and self.simulated_failure.get("remaining", 0) > 0:
            code = int(self.simulated_failure.get("status_code", 429))
            msg = str(self.simulated_failure.get("message", "Simulated failure"))
            return {
                "provider_id": self.provider_id,
                "name": self.config.name,
                "ok": False,
                "status": "error",
                "status_code": code,
                "endpoint": endpoint,
                "mode": "direct_rest" if direct_rest else "openai_gateway",
                "has_api_key": bool(api_key),
                "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                "models_available": len(FREELLM_SMART_ROUTING_MODELS),
                "limitations": list(self.config.limitations),
                "limitations_note": " ".join(self.config.limitations),
                "message": f"Connection test failed (HTTP {code}): {msg}",
            }

        if self.transport_handler is not None or live_network:
            try:
                if direct_rest:
                    status, data, _ = self._raw_http_call(
                        "POST",
                        endpoint,
                        self._build_headers(api_key=api_key),
                        {"message": "ping", "model": self.config.default_model},
                    )
                    if status in (200, 201):
                        return {
                            "provider_id": self.provider_id,
                            "name": self.config.name,
                            "ok": True,
                            "status": "connected",
                            "status_code": status,
                            "endpoint": endpoint,
                            "mode": "direct_rest",
                            "has_api_key": bool(api_key),
                            "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                            "models_available": len(FREELLM_SMART_ROUTING_MODELS),
                            "limitations": list(self.config.limitations),
                            "limitations_note": " ".join(self.config.limitations),
                            "message": "FreeLLM direct REST endpoint reachable and verified.",
                        }
                    hint = ""
                    if status in (401, 403) and str(api_key or "").startswith("freellmapi-"):
                        hint = " Note: 'freellmapi-' keys belong to local FreeLLMAPI gateways (http://localhost:31415/v1), not apifreellm.com."
                    err_detail = data.get("error", data) if isinstance(data, dict) else str(data)
                    return {
                        "provider_id": self.provider_id,
                        "name": self.config.name,
                        "ok": False,
                        "status": "error",
                        "status_code": status,
                        "endpoint": endpoint,
                        "mode": "direct_rest",
                        "has_api_key": bool(api_key),
                        "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                        "models_available": len(FREELLM_SMART_ROUTING_MODELS),
                        "limitations": list(self.config.limitations),
                        "limitations_note": " ".join(self.config.limitations),
                        "message": f"Connection test failed (HTTP {status}): {err_detail}.{hint}",
                    }

                models_url = f"{resolved_base}/models"
                status, data, _ = self._raw_http_call(
                    "GET",
                    models_url,
                    self._build_headers(api_key=api_key),
                    None,
                )
                if status != 200:
                    err_detail = data.get("error", data) if isinstance(data, dict) else str(data)
                    return {
                        "provider_id": self.provider_id,
                        "name": self.config.name,
                        "ok": False,
                        "status": "error",
                        "status_code": status,
                        "endpoint": endpoint,
                        "mode": "openai_gateway",
                        "has_api_key": bool(api_key),
                        "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                        "models_available": len(FREELLM_SMART_ROUTING_MODELS),
                        "limitations": list(self.config.limitations),
                        "limitations_note": " ".join(self.config.limitations),
                        "message": f"Gateway returned HTTP {status} at {models_url}: {err_detail}",
                    }

                models = self.discover_models()
                return {
                    "provider_id": self.provider_id,
                    "name": self.config.name,
                    "ok": True,
                    "status": "connected",
                    "status_code": 200,
                    "endpoint": endpoint,
                    "mode": "openai_gateway",
                    "has_api_key": bool(api_key),
                    "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                    "models_available": len(models),
                    "limitations": list(self.config.limitations),
                    "limitations_note": " ".join(self.config.limitations),
                    "message": f"FreeLLM API gateway verified at {endpoint} ({len(models)} models).",
                }
            except Exception as exc:
                return {
                    "provider_id": self.provider_id,
                    "name": self.config.name,
                    "ok": False,
                    "status": "error",
                    "endpoint": endpoint,
                    "mode": "direct_rest" if direct_rest else "openai_gateway",
                    "has_api_key": bool(api_key),
                    "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
                    "models_available": len(FREELLM_SMART_ROUTING_MODELS),
                    "limitations": list(self.config.limitations),
                    "limitations_note": " ".join(self.config.limitations),
                    "message": f"Connection error: {exc}",
                }

        return {
            "provider_id": self.provider_id,
            "name": self.config.name,
            "ok": bool(self.config.enabled),
            "status": "ready" if self.config.enabled else "disabled",
            "status_code": 200 if self.config.enabled else 0,
            "endpoint": endpoint,
            "mode": "direct_rest" if direct_rest else "openai_gateway",
            "has_api_key": bool(api_key),
            "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
            "models_available": len(FREELLM_SMART_ROUTING_MODELS),
            "routing_modes": ["auto", "auto:fast", "auto:smart", "fusion"],
            "limitations": list(self.config.limitations),
            "limitations_note": " ".join(self.config.limitations),
            "message": "FreeLLM API primary provider ready (supports smart routing & prompt-tool fallback).",
        }
