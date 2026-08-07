"""Anthropic Messages API provider.

Two shape differences from Chat Completions matter here and are handled
explicitly rather than papered over:

* System prompts are a top-level ``system`` parameter, not a message with
  ``role: "system"``.
* Content is a list of typed blocks, so text and tool calls arrive interleaved
  in one array rather than in separate fields.
"""

from __future__ import annotations

from typing import Any

from parity.adapters.providers.http_base import DEFAULT_TIMEOUT_SECONDS, HttpProviderBase
from parity.domain.models import InteractionInput, InteractionOutput, ToolCall
from parity.errors import ProviderError

DEFAULT_API_VERSION = "2023-06-01"

#: Anthropic requires max_tokens. This is used only when the captured
#: interaction did not record one.
FALLBACK_MAX_TOKENS = 4096


class AnthropicProvider(HttpProviderBase):
    """Client for the Anthropic Messages API."""

    def __init__(
        self,
        *,
        name: str = "anthropic",
        base_url: str = "https://api.anthropic.com/v1",
        api_key_env: str | None = "ANTHROPIC_API_KEY",
        require_key: bool = True,
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        headers = {"anthropic-version": api_version}
        headers.update(extra_headers or {})
        super().__init__(
            name=name,
            base_url=base_url,
            api_key_env=api_key_env,
            require_key=require_key,
            timeout=timeout,
            extra_headers=headers,
        )

    def _auth_headers(self) -> dict[str, str]:
        # Anthropic uses x-api-key rather than a bearer token.
        return {"x-api-key": self._api_key} if self._api_key else {}

    def _build_payload(self, model: str, request: InteractionInput) -> dict[str, Any]:
        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "system":
                if message.content:
                    system_parts.append(message.content)
                continue
            role = "assistant" if message.role == "assistant" else "user"
            messages.append({"role": role, "content": message.content})

        params = request.params
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": params.max_tokens or FALLBACK_MAX_TOKENS,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if params.temperature is not None:
            payload["temperature"] = params.temperature
        if params.top_p is not None:
            payload["top_p"] = params.top_p
        if params.stop:
            payload["stop_sequences"] = list(params.stop)
        if request.tools:
            payload["tools"] = [dict(tool) for tool in request.tools]
        payload.update(params.extra)
        return payload

    def complete(self, model: str, request: InteractionInput) -> InteractionOutput:
        body = self._post("/messages", self._build_payload(model, request))

        content = body.get("content")
        if not isinstance(content, list):
            raise ProviderError(
                "response contained no content blocks", provider=self.name, retryable=True
            )

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block_type == "tool_use" and isinstance(block.get("name"), str):
                arguments = block.get("input")
                tool_calls.append(
                    ToolCall(
                        name=block["name"],
                        arguments=arguments if isinstance(arguments, dict) else {},
                        call_id=block.get("id") if isinstance(block.get("id"), str) else None,
                    )
                )

        usage_raw = body.get("usage")
        usage = (
            {k: v for k, v in usage_raw.items() if isinstance(v, int)}
            if isinstance(usage_raw, dict)
            else {}
        )

        return InteractionOutput(
            text="".join(text_parts),
            tool_calls=tuple(tool_calls),
            finish_reason=(
                body.get("stop_reason") if isinstance(body.get("stop_reason"), str) else None
            ),
            model=body.get("model") if isinstance(body.get("model"), str) else model,
            usage=usage,
        )
