"""Offline provider used for tests, demos, and dry runs.

Deterministic by construction: the same input always produces the same output,
derived from a hash of that input. No network, no credentials, no cost.

The ``Mutation`` set exists so that every regression this tool claims to detect
can be reproduced in a test without a real model — you ask the fake provider to
drop a field, refuse, truncate, or emit malformed JSON, and assert that the
classifier notices.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, assert_never

from parity.domain.models import InteractionInput, InteractionOutput, ToolCall, fingerprint
from parity.errors import ProviderError


class Mutation(StrEnum):
    """A deliberate way of degrading the reference output."""

    NONE = "none"
    IDENTICAL = "identical"
    """Echo the scripted output unchanged — used to assert EQUIVALENT."""

    REWORD = "reword"
    """Semantically similar, textually different — used to assert UNVERIFIED."""

    DROP_FIELD = "drop_field"
    BREAK_JSON = "break_json"
    REFUSE = "refuse"
    TRUNCATE = "truncate"
    EMPTY = "empty"
    DROP_TOOL_CALL = "drop_tool_call"
    INFLATE = "inflate"
    RAISE = "raise"
    """Raise a retryable provider error — used to exercise the retry path."""


REFUSAL_TEXT = "I'm sorry, but I can't help with that request."


class FakeProvider:
    """A provider that never touches the network.

    ``scripted`` maps a case id (or input fingerprint) to the output to return.
    Anything not scripted falls back to a deterministic echo of the last user
    message, so a baseline captured from the fake provider replays cleanly
    against itself.
    """

    def __init__(
        self,
        *,
        name: str = "fake",
        scripted: Mapping[str, InteractionOutput] | None = None,
        mutation: Mutation = Mutation.NONE,
        on_request: Callable[[str, InteractionInput], None] | None = None,
        fail_times: int = 0,
    ) -> None:
        self._name = name
        self._scripted = dict(scripted or {})
        self._mutation = mutation
        self._on_request = on_request
        self._fail_times = fail_times
        self.calls: list[tuple[str, InteractionInput]] = []

    @property
    def name(self) -> str:
        return self._name

    def script(self, key: str, output: InteractionOutput) -> None:
        self._scripted[key] = output

    # -- generation ------------------------------------------------------

    @staticmethod
    def _last_user_text(request: InteractionInput) -> str:
        for message in reversed(request.messages):
            if message.role == "user":
                return message.content
        return request.messages[-1].content

    def _default_output(self, model: str, request: InteractionInput) -> InteractionOutput:
        digest = fingerprint(request.model_dump(mode="json"), length=8)
        return InteractionOutput(
            text=f"[fake:{model}] {self._last_user_text(request)} (#{digest})",
            finish_reason="stop",
            model=model,
            usage={"prompt_tokens": 0, "completion_tokens": 0},
        )

    def complete(self, model: str, request: InteractionInput) -> InteractionOutput:
        self.calls.append((model, request))
        if self._on_request is not None:
            self._on_request(model, request)

        if self._fail_times > 0:
            self._fail_times -= 1
            raise ProviderError(
                "synthetic transient failure",
                provider=self._name,
                retryable=True,
                status_code=503,
            )

        key = request.fingerprint()
        base = self._scripted.get(key) or self._default_output(model, request)
        return self._mutate(base, model)

    # -- mutations -------------------------------------------------------

    def _mutate(self, output: InteractionOutput, model: str) -> InteractionOutput:
        mutation = self._mutation
        if mutation in (Mutation.NONE, Mutation.IDENTICAL):
            return output
        if mutation is Mutation.RAISE:
            raise ProviderError("synthetic permanent failure", provider=self._name, retryable=False)
        if mutation is Mutation.EMPTY:
            return output.model_copy(update={"text": "", "tool_calls": ()})
        if mutation is Mutation.REFUSE:
            return output.model_copy(update={"text": REFUSAL_TEXT, "tool_calls": ()})
        if mutation is Mutation.TRUNCATE:
            return output.model_copy(
                update={
                    "text": output.text[: max(1, len(output.text) // 2)],
                    "finish_reason": "length",
                }
            )
        if mutation is Mutation.INFLATE:
            return output.model_copy(update={"text": output.text + " " + ("padding " * 200)})
        if mutation is Mutation.DROP_TOOL_CALL:
            return output.model_copy(update={"tool_calls": output.tool_calls[:-1]})
        if mutation is Mutation.BREAK_JSON:
            return output.model_copy(update={"text": output.text + " <<not json>>"})
        if mutation is Mutation.DROP_FIELD:
            return output.model_copy(update={"text": self._drop_a_field(output.text)})
        if mutation is Mutation.REWORD:
            return output.model_copy(update={"text": f"Rephrased: {output.text}"})
        assert_never(mutation)

    @staticmethod
    def _drop_a_field(text: str) -> str:
        try:
            parsed: Any = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text
        if isinstance(parsed, dict) and parsed:
            reduced = dict(parsed)
            reduced.pop(sorted(reduced)[0])
            return json.dumps(reduced)
        return text

    def close(self) -> None:
        return


def scripted_json_output(payload: Mapping[str, Any], *, model: str = "fake-1") -> InteractionOutput:
    """Helper for building a JSON-producing reference output in tests."""
    return InteractionOutput(
        text=json.dumps(dict(payload), sort_keys=True),
        finish_reason="stop",
        model=model,
    )


def scripted_tool_output(*names: str, model: str = "fake-1") -> InteractionOutput:
    """Helper for building a tool-calling reference output in tests."""
    return InteractionOutput(
        text="",
        tool_calls=tuple(ToolCall(name=name, arguments={"q": name}) for name in names),
        finish_reason="tool_calls",
        model=model,
    )
