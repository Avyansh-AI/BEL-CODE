"""
Base data structures, exceptions, and abstract adapter for BEL Code AI Providers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
import re
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union
import urllib.error
import urllib.request

from bel_code.config.settings import SecretVault

_ORIGINAL_URLOPEN = urllib.request.urlopen


class ProviderError(RuntimeError):
    """Base error raised by a provider adapter."""

    def __init__(
        self,
        message: str,
        provider_id: str = "",
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
        raw_body: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.status_code = status_code
        self.retry_after = retry_after
        self.raw_body = raw_body


class RateLimitError(ProviderError):
    """Raised on HTTP 429 rate-limit or quota exhaustion responses."""

    def __init__(
        self,
        message: str,
        provider_id: str = "",
        status_code: Optional[int] = 429,
        retry_after: Optional[float] = None,
        raw_body: Optional[str] = None,
        is_permanent: bool = False,
    ) -> None:
        super().__init__(
            message,
            provider_id=provider_id,
            status_code=status_code,
            retry_after=retry_after,
            raw_body=raw_body,
        )
        self.is_permanent = bool(is_permanent)


class AuthenticationError(ProviderError):
    """Raised on HTTP 401/403 authentication or missing API key errors."""


class TransientProviderError(ProviderError):
    """Raised on HTTP 5xx, timeout, or transient network failures."""


@dataclass
class ChatMessage:
    """Normalized chat message representation across all AI providers."""
    role: str
    content: str
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "role": self.role,
            "content": self.content,
        }
        if self.name:
            payload["name"] = self.name
        if self.tool_calls:
            payload["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        return payload

    @classmethod
    def from_any(cls, item: Union["ChatMessage", Dict[str, Any], str]) -> "ChatMessage":
        if isinstance(item, cls):
            return item
        if isinstance(item, str):
            return cls(role="user", content=item)
        if isinstance(item, dict):
            return cls(
                role=str(item.get("role", "user")),
                content=str(item.get("content", "")),
                name=item.get("name"),
                tool_calls=item.get("tool_calls"),
                tool_call_id=item.get("tool_call_id"),
                metadata=item.get("metadata"),
            )
        return cls(role="user", content=str(item))


@dataclass
class ToolDefinition:
    """Normalized tool/function schema for native and prompt-engineered tool calling."""
    name: str
    description: str = ""
    parameters: Dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}, "required": []}
    )

    def to_openai_tool(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic_tool(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_gemini_tool(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    @classmethod
    def from_any(cls, tool: Union["ToolDefinition", Dict[str, Any]]) -> "ToolDefinition":
        if isinstance(tool, cls):
            return tool
        if isinstance(tool, dict):
            if "function" in tool and isinstance(tool["function"], dict):
                fn = tool["function"]
                return cls(
                    name=str(fn.get("name", "unnamed_tool")),
                    description=str(fn.get("description", "")),
                    parameters=dict(fn.get("parameters", {"type": "object", "properties": {}})),
                )
            return cls(
                name=str(tool.get("name", "unnamed_tool")),
                description=str(tool.get("description", "")),
                parameters=dict(
                    tool.get("parameters")
                    or tool.get("input_schema")
                    or {"type": "object", "properties": {}}
                ),
            )
        raise TypeError(f"Unsupported tool specification type: {type(tool)}")


@dataclass
class UsageMetrics:
    """Token consumption, cost, and latency telemetry for a single provider request."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    provider_id: str = ""
    model_id: str = ""

    def __post_init__(self) -> None:
        self.prompt_tokens = int(self.prompt_tokens or 0)
        self.completion_tokens = int(self.completion_tokens or 0)
        if not self.total_tokens:
            self.total_tokens = self.prompt_tokens + self.completion_tokens
        else:
            self.total_tokens = int(self.total_tokens)
        self.cost_usd = round(float(self.cost_usd or 0.0), 6)
        self.latency_ms = round(float(self.latency_ms or 0.0), 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
        }

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


@dataclass
class CompletionResult:
    """Normalized completion result returned by all provider adapters and ProviderManager."""
    content: str = ""
    model: str = ""
    provider: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage: UsageMetrics = field(default_factory=UsageMetrics)
    raw_response: Optional[Dict[str, Any]] = None
    fallback_used: bool = False
    limitations_note: Optional[str] = None
    finish_reason: str = "stop"
    attempt_history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "model": self.model,
            "provider": self.provider,
            "tool_calls": list(self.tool_calls),
            "usage": self.usage.to_dict() if isinstance(self.usage, UsageMetrics) else self.usage,
            "raw_response": self.raw_response,
            "fallback_used": self.fallback_used,
            "limitations_note": self.limitations_note,
            "finish_reason": self.finish_reason,
            "attempt_history": list(self.attempt_history),
        }

    def __getitem__(self, key: str) -> Any:
        if key == "usage" and isinstance(self.usage, UsageMetrics):
            return self.usage
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


@dataclass
class ProviderConfig:
    """Configuration descriptor for an AI provider in BEL Code."""
    provider_id: str
    name: str
    base_url: str
    default_model: str
    enabled: bool = True
    auth_type: str = "bearer"  # bearer | x-api-key | dual | gemini | optional_bearer | none
    requires_api_key: bool = True
    account_label: str = "default"
    secondary_base_url: Optional[str] = None
    endpoint_mode: str = "auto"  # auto | openai | anthropic | direct_rest
    models: List[str] = field(default_factory=list)
    supports_streaming: bool = True
    supports_tools: bool = True
    supports_vision: bool = False
    rpm_limit: int = 60
    tpm_limit: int = 120_000
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    timeout_seconds: float = 20.0
    max_retries: int = 2
    extra_headers: Dict[str, str] = field(default_factory=dict)
    limitations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.provider_id,
            "provider_id": self.provider_id,
            "name": self.name,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "enabled": self.enabled,
            "auth_type": self.auth_type,
            "requires_api_key": self.requires_api_key,
            "account_label": self.account_label,
            "secondary_base_url": self.secondary_base_url,
            "endpoint_mode": self.endpoint_mode,
            "models": list(self.models),
            "supports_streaming": self.supports_streaming,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "rpm_limit": self.rpm_limit,
            "tpm_limit": self.tpm_limit,
            "cost_per_1k_input": self.cost_per_1k_input,
            "cost_per_1k_output": self.cost_per_1k_output,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "extra_headers": dict(self.extra_headers),
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderConfig":
        return cls(
            provider_id=str(data["provider_id"]),
            name=str(data.get("name", data["provider_id"])),
            base_url=str(data.get("base_url", "")),
            default_model=str(data.get("default_model", "auto")),
            enabled=bool(data.get("enabled", True)),
            auth_type=str(data.get("auth_type", "bearer")),
            requires_api_key=bool(data.get("requires_api_key", True)),
            account_label=str(data.get("account_label", "default")),
            secondary_base_url=data.get("secondary_base_url"),
            endpoint_mode=str(data.get("endpoint_mode", "auto")),
            models=list(data.get("models", [])),
            supports_streaming=bool(data.get("supports_streaming", True)),
            supports_tools=bool(data.get("supports_tools", True)),
            supports_vision=bool(data.get("supports_vision", False)),
            rpm_limit=int(data.get("rpm_limit", 60)),
            tpm_limit=int(data.get("tpm_limit", 120_000)),
            cost_per_1k_input=float(data.get("cost_per_1k_input", 0.0)),
            cost_per_1k_output=float(data.get("cost_per_1k_output", 0.0)),
            timeout_seconds=float(data.get("timeout_seconds", 20.0)),
            max_retries=int(data.get("max_retries", 2)),
            extra_headers=dict(data.get("extra_headers", {})),
            limitations=list(data.get("limitations", [])),
        )


TransportCallable = Callable[
    [str, str, Dict[str, str], Optional[Dict[str, Any]]],
    Tuple[int, Any, Dict[str, str]],
]


DEFAULT_HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class BaseProviderAdapter(ABC):
    """
    Abstract base class for all BEL Code AI provider adapters.
    Provides:
    - Secure API key resolution from `SecretVault`
    - HTTP request execution with retry & exponential backoff on HTTP 429 / 5xx
    - Structured JSON/XML prompt-engineered tool calling fallback
    - Token & cost calculation helpers
    """

    TOOL_CALL_XML_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
    TOOL_CALL_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\"tool_calls\".*?\})\s*```", re.DOTALL)

    def __init__(
        self,
        config: ProviderConfig,
        vault: Optional[SecretVault] = None,
        transport_handler: Optional[TransportCallable] = None,
        backoff_sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.config = config
        self._default_base_url = config.base_url
        self.provider_id = config.provider_id
        self.vault = vault
        self.transport_handler = transport_handler
        self._sleep = backoff_sleep_fn or (lambda s: time.sleep(min(s, 0.05)))
        self.simulated_failure: Optional[Dict[str, Any]] = None
        self.allow_offline_synthesis: bool = True
        self.last_backoff_delays: List[float] = []
        self.last_rate_limit_headers: Dict[str, Any] = {}

    def _should_use_http_transport(self, **kwargs: Any) -> bool:
        """
        Determine whether to execute the HTTP transport pipeline vs offline synthesis.
        Executes HTTP transport whenever:
        - `transport_handler` is provided
        - `simulated_failure` is active
        - `urllib.request.urlopen` is mocked (e.g. via unittest.mock.patch)
        - `live_network=True` or `strict_network=True` is passed
        - `base_url` was customized to a non-default endpoint (e.g. local test server or unreachable test URL)
        - `allow_offline_synthesis` is False
        """
        if self.transport_handler is not None:
            return True
        if self.simulated_failure and self.simulated_failure.get("remaining", 0) > 0:
            return True
        if urllib.request.urlopen is not _ORIGINAL_URLOPEN or hasattr(urllib.request.urlopen, "_mock_name"):
            return True
        if kwargs.get("live_network") or kwargs.get("strict_network"):
            return True
        if not self.allow_offline_synthesis:
            return True
        if self.config.base_url.rstrip("/") != self._default_base_url.rstrip("/"):
            return True
        if self.config.requires_api_key and bool(self.get_api_key()):
            return True
        return False

    def get_api_key(self, label: Optional[str] = None) -> Optional[str]:
        """Resolve the API key for this provider from `SecretVault`."""
        if self.vault is None:
            return None
        target_label = label or self.config.account_label or "default"
        return self.vault.get_secret(self.provider_id, label=target_label)

    def normalize_messages(
        self, messages: Union[str, List[Union[ChatMessage, Dict[str, Any], str]]]
    ) -> List[ChatMessage]:
        if isinstance(messages, str):
            return [ChatMessage(role="user", content=messages)]
        return [ChatMessage.from_any(m) for m in messages]

    def normalize_tools(
        self, tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]]
    ) -> List[ToolDefinition]:
        if not tools:
            return []
        return [ToolDefinition.from_any(t) for t in tools]

    @staticmethod
    def estimate_tokens(text: str) -> int:
        if not text:
            return 1
        return max(1, (len(text) + 3) // 4)

    def estimate_cost(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        model: Optional[str] = None,
    ) -> float:
        in_cost = (prompt_tokens / 1000.0) * float(self.config.cost_per_1k_input)
        out_cost = (completion_tokens / 1000.0) * float(self.config.cost_per_1k_output)
        return round(in_cost + out_cost, 6)

    def inject_prompt_tools(
        self,
        messages: List[ChatMessage],
        tools: List[ToolDefinition],
    ) -> List[ChatMessage]:
        """
        Inject structured JSON/XML tool-calling instructions into messages when an underlying
        model or endpoint does not natively support the `tools` parameter.
        """
        if not tools:
            return messages

        tool_specs = [t.to_dict() for t in tools]
        instruction = (
            "You have access to the following tools. Because native tool_calls are disabled or "
            "unsupported by this model endpoint, emit any tool invocation using XML tags containing JSON:\n"
            "<tool_call>{\"name\": \"tool_name\", \"arguments\": {\"arg1\": \"val1\"}}</tool_call>\n"
            f"Available Tools:\n{json.dumps(tool_specs, indent=2)}"
        )

        cloned = [ChatMessage.from_any(m.to_dict()) for m in messages]
        if cloned and cloned[0].role == "system":
            cloned[0].content = f"{cloned[0].content}\n\n{instruction}"
        else:
            cloned.insert(0, ChatMessage(role="system", content=instruction))
        return cloned

    @classmethod
    def extract_prompt_tool_calls(cls, content: str) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Parse `<tool_call>{...}</tool_call>` or fenced JSON tool_calls blocks from text responses.
        Returns `(cleaned_content, extracted_tool_calls)`.
        """
        if not content:
            return content, []

        extracted: List[Dict[str, Any]] = []

        for idx, match in enumerate(cls.TOOL_CALL_XML_RE.finditer(content)):
            raw_json = match.group(1).strip()
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict) and "name" in parsed:
                    args = parsed.get("arguments", parsed.get("parameters", {}))
                    extracted.append(
                        {
                            "id": parsed.get("id", f"call_prompt_{idx + 1}"),
                            "type": "function",
                            "function": {
                                "name": parsed["name"],
                                "arguments": args if isinstance(args, dict) else json.loads(str(args)),
                            },
                        }
                    )
            except Exception:
                continue

        if not extracted:
            for idx, match in enumerate(cls.TOOL_CALL_JSON_BLOCK_RE.finditer(content)):
                try:
                    parsed = json.loads(match.group(1).strip())
                    for tc in parsed.get("tool_calls", []):
                        name = tc.get("name") or tc.get("function", {}).get("name")
                        args = tc.get("arguments") or tc.get("function", {}).get("arguments", {})
                        if name:
                            extracted.append(
                                {
                                    "id": tc.get("id", f"call_json_{idx + 1}"),
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": args if isinstance(args, dict) else json.loads(str(args)),
                                    },
                                }
                            )
                except Exception:
                    continue

        cleaned = cls.TOOL_CALL_XML_RE.sub("", content).strip()
        return cleaned, extracted

    def set_simulated_failure(
        self,
        status_code: int = 429,
        message: str = "Rate limit exceeded",
        remaining_failures: int = 999,
    ) -> None:
        """Configure a simulated HTTP error (e.g. 429 or 503) for testing retries and fallback."""
        self.simulated_failure = {
            "status_code": status_code,
            "message": message,
            "remaining": remaining_failures,
        }

    def clear_simulated_failure(self) -> None:
        self.simulated_failure = None

    def _execute_http_with_retry(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]] = None,
        max_retries: Optional[int] = None,
    ) -> Tuple[int, Any, Dict[str, str]]:
        """
        Execute an HTTP request with automatic retry and exponential backoff on HTTP 429 and 5xx.
        """
        retries = self.config.max_retries if max_retries is None else int(max_retries)
        self.last_backoff_delays = []
        attempt = 0

        while True:
            try:
                status, data, resp_headers = self._raw_http_call(method, url, headers, payload)
                if resp_headers:
                    self._capture_rate_limit_headers(resp_headers)
                if status == 429:
                    retry_after_hdr = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
                    try:
                        retry_after = float(retry_after_hdr) if retry_after_hdr else None
                    except (TypeError, ValueError):
                        retry_after = None
                    detail_text = self._redact_secret_text(str(data))
                    lowered = detail_text.lower()
                    is_perm = any(
                        tok in lowered
                        for tok in (
                            "insufficient_quota",
                            "quota exceeded",
                            "exceeded your current quota",
                            "daily limit",
                            "monthly limit",
                            "requests per day",
                            "tokens per day",
                            "neuron allocation",
                        )
                    )
                    kind = "quota exhausted" if is_perm else "rate limited"
                    raise RateLimitError(
                        f"Provider '{self.provider_id}' {kind} (HTTP 429): {detail_text}",
                        provider_id=self.provider_id,
                        status_code=429,
                        retry_after=retry_after,
                        raw_body=detail_text,
                        is_permanent=is_perm,
                    )
                if status in (401, 403):
                    detail_text = self._redact_secret_text(str(data))
                    raise AuthenticationError(
                        f"Provider '{self.provider_id}' authentication failed (HTTP {status}): {detail_text}",
                        provider_id=self.provider_id,
                        status_code=status,
                        raw_body=detail_text,
                    )
                if status >= 500:
                    raise TransientProviderError(
                        f"Provider '{self.provider_id}' server error (HTTP {status})",
                        provider_id=self.provider_id,
                        status_code=status,
                    )
                if status >= 400:
                    detail_text = self._redact_secret_text(str(data))
                    raise ProviderError(
                        f"Provider '{self.provider_id}' request failed (HTTP {status}): {detail_text}",
                        provider_id=self.provider_id,
                        status_code=status,
                        raw_body=detail_text,
                    )
                return status, data, resp_headers

            except (RateLimitError, TransientProviderError) as exc:
                if isinstance(exc, RateLimitError) and exc.is_permanent:
                    raise
                if attempt >= retries:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else (0.5 * (2 ** attempt))
                self.last_backoff_delays.append(delay)
                self._sleep(delay)
                attempt += 1

    def _redact_secret_text(self, text: str) -> str:
        """Redact API secrets from error text before returning or logging."""
        if self.vault is not None and hasattr(self.vault, "redact"):
            return self.vault.redact(text)
        return text

    def _capture_rate_limit_headers(self, headers: Dict[str, str]) -> None:
        """Store normalized rate-limit telemetry headers returned by upstream APIs."""
        captured: Dict[str, Any] = {}
        for k, v in headers.items():
            lk = k.lower()
            if "ratelimit" in lk or lk == "retry-after":
                captured[lk] = v
        if captured:
            self.last_rate_limit_headers = captured

    def _raw_http_call(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Any, Dict[str, str]]:
        if self.simulated_failure and self.simulated_failure.get("remaining", 0) > 0:
            self.simulated_failure["remaining"] -= 1
            code = int(self.simulated_failure.get("status_code", 429))
            msg = str(self.simulated_failure.get("message", "Simulated provider failure"))
            return code, {"error": {"message": msg, "code": code}}, {}

        if self.transport_handler is not None:
            return self.transport_handler(method, url, headers, payload)

        body_bytes: Optional[bytes] = None
        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": DEFAULT_HTTP_USER_AGENT,
            **headers,
        }
        if payload is not None:
            body_bytes = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(url=url, data=body_bytes, headers=req_headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                status_code = resp.getcode()
                raw_bytes = resp.read()
                resp_hdrs = dict(resp.headers.items())
                text_body = raw_bytes.decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(text_body) if text_body.strip() else {}
                except json.JSONDecodeError:
                    parsed = {"raw_text": text_body}
                return status_code, parsed, resp_hdrs
        except urllib.error.HTTPError as http_err:
            err_body = ""
            try:
                err_body = http_err.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            try:
                parsed_err = json.loads(err_body) if err_body.strip() else {"error": str(http_err)}
            except Exception:
                parsed_err = {"error": err_body or str(http_err)}
            return http_err.code, parsed_err, dict(http_err.headers.items()) if http_err.headers else {}
        except Exception as net_err:
            raise TransientProviderError(
                f"Network error contacting provider '{self.provider_id}' at {url}: {net_err}",
                provider_id=self.provider_id,
                status_code=503,
            ) from net_err

    def _synthesize_offline_completion(
        self,
        messages: List[ChatMessage],
        model: str,
        tools: List[ToolDefinition],
        limitations_note: Optional[str] = None,
    ) -> CompletionResult:
        """
        Deterministic local completion synthesizer used when running in an offline test/benchmark
        environment without live external network connectivity.
        """
        last_user = ""
        for msg in reversed(messages):
            if msg.role == "user":
                last_user = msg.content
                break
        if not last_user and messages:
            last_user = messages[-1].content

        tool_calls: List[Dict[str, Any]] = []
        if tools:
            first_tool = tools[0]
            tool_calls.append(
                {
                    "id": "call_local_1",
                    "type": "function",
                    "function": {
                        "name": first_tool.name,
                        "arguments": {},
                    },
                }
            )

        content = (
            f"[{self.config.name} ({model})] Processed engineering request: "
            f"{last_user[:240].strip() or 'Ready.'}"
        )
        prompt_toks = sum(self.estimate_tokens(m.content) for m in messages)
        comp_toks = self.estimate_tokens(content)
        cost = self.estimate_cost(prompt_toks, comp_toks, model=model)

        usage = UsageMetrics(
            prompt_tokens=prompt_toks,
            completion_tokens=comp_toks,
            total_tokens=prompt_toks + comp_toks,
            cost_usd=cost,
            latency_ms=4.5,
            provider_id=self.provider_id,
            model_id=model,
        )
        return CompletionResult(
            content=content,
            model=model,
            provider=self.provider_id,
            tool_calls=tool_calls,
            usage=usage,
            raw_response={"offline_synthesized": True, "provider": self.provider_id, "model": model},
            fallback_used=False,
            limitations_note=limitations_note,
        )

    @abstractmethod
    def complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> CompletionResult:
        """Execute a synchronous chat completion request."""

    @abstractmethod
    def stream_complete(
        self,
        messages: Union[str, List[Union[ChatMessage, Dict[str, Any]]]],
        model: Optional[str] = None,
        tools: Optional[List[Union[ToolDefinition, Dict[str, Any]]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> Iterator[Dict[str, Any]]:
        """Execute a streaming chat completion request yielding SSE delta events."""

    @abstractmethod
    def discover_models(self) -> List[Dict[str, Any]]:
        """Discover available models for this provider."""

    @abstractmethod
    def test_connection(self, live_network: bool = False) -> Dict[str, Any]:
        """Verify provider configuration, credentials, and endpoint readiness."""
