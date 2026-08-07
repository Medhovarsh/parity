"""Ollama provider — the zero-cost path.

Ollama runs models locally with no account and no key, which makes it the
default recommendation for anyone evaluating Parity, and a genuinely usable
semantic judge for teams that will not send payloads to a hosted API.

Targets Ollama's native ``/api/chat`` rather than its OpenAI compatibility
shim, because the native endpoint reports ``done_reason`` — which is what the
truncation check needs.
"""

from __future__ import annotations

from typing import Any

from parity.adapters.providers.http_base import HttpProviderBase
from parity.domain.models import InteractionInput, InteractionOutput, ToolCall
from parity.errors import ProviderError

DEFAULT_BASE_URL = "http://localhost:11434"

#: Local models on modest hardware are slow. A short default timeout here would
#: manufacture failures that look like model regressions.
DEFAULT_TIMEOUT_SECONDS = 600.0


class OllamaProvider(HttpProviderBase):
    """Client for a local Ollama server."""

    def __init__(
        self,
        *,
        name: str = "ollama",
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            base_url=base_url,
            api_key_env=None,
            require_key=False,
            timeout=timeout,
            extra_headers=extra_headers,
        )

    def _build_payload(self, model: str, request: InteractionInput) -> dict[str, Any]:
        options: dict[str, Any] = {}
        params = request.params
        if params.temperature is not None:
            options["temperature"] = params.temperature
        if params.top_p is not None:
            options["top_p"] = params.top_p
        if params.max_tokens is not None:
            options["num_predict"] = params.max_tokens
        if params.seed is not None:
            options["seed"] = params.seed
        if params.stop:
            options["stop"] = list(params.stop)

        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": False,
        }
        if options:
            payload["options"] = options
        if request.tools:
            payload["tools"] = [dict(tool) for tool in request.tools]
        if request.response_format is not None:
            # Ollama accepts "json" or a JSON Schema object under `format`.
            nested = request.response_format.get("json_schema")
            if isinstance(nested, dict) and isinstance(nested.get("schema"), dict):
                payload["format"] = nested["schema"]
            elif request.response_format.get("type") in {"json_object", "json_schema"}:
                payload["format"] = "json"
        payload.update(params.extra)
        return payload

    def complete(self, model: str, request: InteractionInput) -> InteractionOutput:
        body = self._post("/api/chat", self._build_payload(model, request))

        message = body.get("message")
        if not isinstance(message, dict):
            raise ProviderError("response contained no message", provider=self.name, retryable=True)

        tool_calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            for item in raw_calls:
                if not isinstance(item, dict):
                    continue
                function = item.get("function")
                if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                    continue
                arguments = function.get("arguments")
                tool_calls.append(
                    ToolCall(
                        name=function["name"],
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )

        content = message.get("content")
        usage = {
            key: int(body[key])
            for key in ("prompt_eval_count", "eval_count")
            if isinstance(body.get(key), int)
        }

        return InteractionOutput(
            text=content if isinstance(content, str) else "",
            tool_calls=tuple(tool_calls),
            finish_reason=(
                body.get("done_reason") if isinstance(body.get("done_reason"), str) else None
            ),
            model=body.get("model") if isinstance(body.get("model"), str) else model,
            usage=usage,
        )
