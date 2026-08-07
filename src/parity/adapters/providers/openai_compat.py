"""OpenAI-compatible Chat Completions provider.

Covers a large share of the ecosystem with one adapter: OpenAI itself, and any
server exposing ``/chat/completions`` — vLLM, LM Studio, llama.cpp's server,
Ollama's compatibility endpoint, and most hosted gateways.

That breadth is deliberate. It means a user with no budget can point Parity at a
local server and get the full workflow, and a user with an existing gateway does
not need a new adapter.
"""

from __future__ import annotations

import json
from typing import Any

from parity.adapters.providers.http_base import DEFAULT_TIMEOUT_SECONDS, HttpProviderBase
from parity.domain.models import InteractionInput, InteractionOutput, Message, ToolCall
from parity.errors import ProviderError


def message_to_wire(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.name:
        payload["name"] = message.name
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.call_id or f"call_{index}",
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for index, call in enumerate(message.tool_calls)
        ]
    return payload


def parse_tool_calls(raw: Any) -> tuple[ToolCall, ...]:
    """Parse wire-format tool calls, tolerating malformed argument JSON.

    A model emitting unparseable arguments is a finding for the checks to report,
    not a reason to abort the run — so the raw string is preserved under a
    ``_raw`` key rather than raising.
    """
    if not isinstance(raw, list):
        return ()
    calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str):
            continue
        raw_args = function.get("arguments")
        arguments: dict[str, Any]
        if isinstance(raw_args, dict):
            arguments = raw_args
        elif isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args) if raw_args.strip() else {}
                arguments = parsed if isinstance(parsed, dict) else {"_value": parsed}
            except json.JSONDecodeError:
                arguments = {"_raw": raw_args}
        else:
            arguments = {}
        call_id = item.get("id")
        calls.append(
            ToolCall(
                name=name,
                arguments=arguments,
                call_id=call_id if isinstance(call_id, str) else None,
            )
        )
    return tuple(calls)


class OpenAICompatibleProvider(HttpProviderBase):
    """Chat Completions client."""

    def __init__(
        self,
        *,
        name: str = "openai",
        base_url: str = "https://api.openai.com/v1",
        api_key_env: str | None = "OPENAI_API_KEY",
        require_key: bool = True,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            base_url=base_url,
            api_key_env=api_key_env,
            require_key=require_key,
            timeout=timeout,
            extra_headers=extra_headers,
        )

    def _build_payload(self, model: str, request: InteractionInput) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [message_to_wire(m) for m in request.messages],
        }
        params = request.params
        if params.temperature is not None:
            payload["temperature"] = params.temperature
        if params.top_p is not None:
            payload["top_p"] = params.top_p
        if params.max_tokens is not None:
            payload["max_tokens"] = params.max_tokens
        if params.seed is not None:
            payload["seed"] = params.seed
        if params.stop:
            payload["stop"] = list(params.stop)
        if request.tools:
            payload["tools"] = [dict(tool) for tool in request.tools]
        if request.response_format is not None:
            payload["response_format"] = request.response_format
        payload.update(params.extra)
        return payload

    def complete(self, model: str, request: InteractionInput) -> InteractionOutput:
        body = self._post("/chat/completions", self._build_payload(model, request))

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("response contained no choices", provider=self.name, retryable=True)
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderError("malformed choice object", provider=self.name, retryable=True)

        message = choice.get("message")
        message = message if isinstance(message, dict) else {}
        content = message.get("content")
        usage_raw = body.get("usage")
        usage = (
            {k: v for k, v in usage_raw.items() if isinstance(v, int)}
            if isinstance(usage_raw, dict)
            else {}
        )

        return InteractionOutput(
            text=content if isinstance(content, str) else "",
            tool_calls=parse_tool_calls(message.get("tool_calls")),
            finish_reason=(
                choice.get("finish_reason")
                if isinstance(choice.get("finish_reason"), str)
                else None
            ),
            model=body.get("model") if isinstance(body.get("model"), str) else model,
            usage=usage,
        )
