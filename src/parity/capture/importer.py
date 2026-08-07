"""Import interaction logs in whatever shape they already exist.

Four shapes are recognised, tried in order of specificity:

1. **Parity native** — a previously exported case.
2. **Request/response pair** — ``{"request": {...}, "response": {...}}``, the
   shape produced by most proxy and gateway logs.
3. **Explicit** — ``{"input": {...}, "output": {...}}`` using Parity's own
   vocabulary.
4. **Flat** — ``{"messages": [...], "completion": "..."}``, the shape people
   write by hand.

Anything unrecognised is reported with its line number rather than silently
dropped. A baseline that quietly lost half its cases is worse than an import
that failed loudly.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from parity.adapters.providers.openai_compat import parse_tool_calls
from parity.domain.models import (
    Case,
    GenerationParams,
    InteractionInput,
    InteractionOutput,
    Message,
    ModelRef,
    Role,
)
from parity.errors import StoreError
from parity.security.limits import (
    DEFAULT_LIMITS,
    Limits,
    guard_file_size,
    guard_line_size,
    guard_payload,
    guard_record_count,
)
from parity.security.redaction import RedactionReport, Redactor

VALID_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})


@dataclass
class ImportResult:
    """Outcome of an import, including everything that did not make it in."""

    cases: list[Case] = field(default_factory=list)
    written: int = 0
    skipped: list[str] = field(default_factory=list)
    redaction: RedactionReport = field(default_factory=RedactionReport)

    @property
    def parsed(self) -> int:
        return len(self.cases)


def _coerce_messages(raw: Any) -> tuple[Message, ...]:
    if not isinstance(raw, list):
        raise ValueError("'messages' must be a list")
    messages: list[Message] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"messages[{index}] must be an object")
        role = item.get("role")
        if role not in VALID_ROLES:
            raise ValueError(f"messages[{index}] has unsupported role {role!r}")
        content = item.get("content")
        if isinstance(content, list):
            # Multimodal content blocks: keep the text, note the rest.
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            content = "\n".join(part for part in parts if part)
        elif content is None:
            content = ""
        elif not isinstance(content, str):
            content = json.dumps(content, sort_keys=True)
        messages.append(
            Message(
                role=cast(Role, role),  # membership checked against VALID_ROLES above
                content=content,
                tool_calls=parse_tool_calls(item.get("tool_calls")),
                name=item.get("name") if isinstance(item.get("name"), str) else None,
                tool_call_id=(
                    item.get("tool_call_id") if isinstance(item.get("tool_call_id"), str) else None
                ),
            )
        )
    if not messages:
        raise ValueError("'messages' is empty")
    return tuple(messages)


def _coerce_params(raw: Any) -> GenerationParams:
    if not isinstance(raw, dict):
        return GenerationParams()
    stop = raw.get("stop")
    if isinstance(stop, str):
        stop_values: tuple[str, ...] = (stop,)
    elif isinstance(stop, list):
        stop_values = tuple(str(s) for s in stop)
    else:
        stop_values = ()
    return GenerationParams(
        temperature=_as_float(raw.get("temperature")),
        top_p=_as_float(raw.get("top_p")),
        max_tokens=_as_int(raw.get("max_tokens") or raw.get("max_completion_tokens")),
        seed=_as_int(raw.get("seed")),
        stop=stop_values,
    )


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _input_from_request(request: dict[str, Any]) -> InteractionInput:
    tools = request.get("tools")
    response_format = request.get("response_format")
    return InteractionInput(
        messages=_coerce_messages(request.get("messages")),
        params=_coerce_params(request),
        tools=tuple(t for t in tools if isinstance(t, dict)) if isinstance(tools, list) else (),
        response_format=response_format if isinstance(response_format, dict) else None,
    )


def _output_from_response(response: dict[str, Any]) -> InteractionOutput:
    """Normalise a Chat Completions or Anthropic Messages response body."""
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        raw_message = choice.get("message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
        content = message.get("content")
        return InteractionOutput(
            text=content if isinstance(content, str) else "",
            tool_calls=parse_tool_calls(message.get("tool_calls")),
            finish_reason=(
                choice.get("finish_reason")
                if isinstance(choice.get("finish_reason"), str)
                else None
            ),
            model=response.get("model") if isinstance(response.get("model"), str) else None,
        )

    content_blocks = response.get("content")
    if isinstance(content_blocks, list):
        texts = [
            block["text"]
            for block in content_blocks
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return InteractionOutput(
            text="".join(texts),
            finish_reason=(
                response.get("stop_reason")
                if isinstance(response.get("stop_reason"), str)
                else None
            ),
            model=response.get("model") if isinstance(response.get("model"), str) else None,
        )

    raise ValueError("response has neither 'choices' nor 'content'")


def _model_ref(record: dict[str, Any], fallback_model: str | None) -> ModelRef:
    explicit = record.get("reference")
    if isinstance(explicit, str):
        return ModelRef.parse(explicit)
    if isinstance(explicit, dict) and "provider" in explicit and "model" in explicit:
        return ModelRef(provider=str(explicit["provider"]), model=str(explicit["model"]))
    provider = record.get("provider")
    model = record.get("model") or fallback_model
    return ModelRef(
        provider=str(provider) if isinstance(provider, str) else "unknown",
        model=str(model) if model else "unknown",
    )


def parse_record(record: dict[str, Any], *, limits: Limits = DEFAULT_LIMITS) -> Case:
    """Normalise one log record into a :class:`Case`.

    Raises ``ValueError`` with an explanatory message when the shape is not
    recognised; the caller attaches the line number.
    """
    # 1. Parity native.
    if {"case_id", "input", "output", "reference"} <= set(record):
        try:
            return Case.model_validate(record)
        except ValidationError as exc:
            raise ValueError(f"native case record is invalid: {exc}") from exc

    # 2. Request/response pair from a proxy or gateway log.
    request = record.get("request")
    response = record.get("response")
    if isinstance(request, dict) and isinstance(response, dict):
        interaction_input = _input_from_request(request)
        interaction_output = _output_from_response(response)
        _guard_case_payloads(interaction_input, interaction_output, limits)
        return Case.create(
            input=interaction_input,
            output=interaction_output,
            reference=_model_ref(record, request.get("model")),
            case_id=record.get("case_id") if isinstance(record.get("case_id"), str) else None,
            tags=_tags(record),
        )

    # 3. Explicit Parity vocabulary.
    raw_input = record.get("input")
    raw_output = record.get("output")
    if isinstance(raw_input, dict) and raw_output is not None:
        interaction_input = (
            InteractionInput.model_validate(raw_input)
            if "messages" in raw_input
            and isinstance(raw_input.get("messages"), list)
            and all(isinstance(m, dict) and "role" in m for m in raw_input["messages"])
            else _input_from_request(raw_input)
        )
        if isinstance(raw_output, str):
            interaction_output = InteractionOutput(text=raw_output)
        elif isinstance(raw_output, dict):
            interaction_output = (
                InteractionOutput.model_validate(raw_output)
                if "text" in raw_output
                else _output_from_response(raw_output)
            )
        else:
            raise ValueError("'output' must be a string or an object")
        _guard_case_payloads(interaction_input, interaction_output, limits)
        return Case.create(
            input=interaction_input,
            output=interaction_output,
            reference=_model_ref(record, interaction_output.model),
            case_id=record.get("case_id") if isinstance(record.get("case_id"), str) else None,
            tags=_tags(record),
        )

    # 4. Flat hand-written shape.
    if isinstance(record.get("messages"), list):
        completion = record.get("completion", record.get("response_text", ""))
        if not isinstance(completion, str):
            raise ValueError("'completion' must be a string")
        interaction_input = _input_from_request(record)
        interaction_output = InteractionOutput(text=completion, finish_reason="stop")
        _guard_case_payloads(interaction_input, interaction_output, limits)
        return Case.create(
            input=interaction_input,
            output=interaction_output,
            reference=_model_ref(record, None),
            case_id=record.get("case_id") if isinstance(record.get("case_id"), str) else None,
            tags=_tags(record),
        )

    raise ValueError(
        "unrecognised record shape; expected one of: a native case, "
        "{'request', 'response'}, {'input', 'output'}, or {'messages', 'completion'}"
    )


def _tags(record: dict[str, Any]) -> tuple[str, ...]:
    tags = record.get("tags")
    if isinstance(tags, list):
        return tuple(str(t) for t in tags)
    if isinstance(tags, str):
        return (tags,)
    return ()


def _guard_case_payloads(
    interaction_input: InteractionInput,
    interaction_output: InteractionOutput,
    limits: Limits,
) -> None:
    for message in interaction_input.messages:
        guard_payload(message.content, limits, what="message content")
    guard_payload(interaction_output.text, limits, what="output text")


def iter_jsonl(path: Path, *, limits: Limits = DEFAULT_LIMITS) -> Iterator[tuple[int, Any]]:
    """Stream a JSONL file, or a single JSON array, yielding ``(line, value)``."""
    guard_file_size(path, limits)
    text_head = path.open("r", encoding="utf-8")
    with text_head as handle:
        first = handle.readline()
        handle.seek(0)
        if first.lstrip().startswith("["):
            try:
                payload = json.load(handle)
            except json.JSONDecodeError as exc:
                raise StoreError(f"{path}: invalid JSON array: {exc}") from exc
            if not isinstance(payload, list):
                raise StoreError(f"{path}: expected a JSON array of records")
            yield from enumerate(payload, start=1)
            return
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            guard_line_size(stripped, limits)
            try:
                yield number, json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise StoreError(f"{path}:{number}: invalid JSON: {exc}") from exc


def import_records(
    records: Iterable[tuple[int, Any]],
    *,
    redactor: Redactor | None,
    limits: Limits = DEFAULT_LIMITS,
    reference_override: ModelRef | None = None,
    tags: tuple[str, ...] = (),
    strict: bool = False,
) -> ImportResult:
    """Parse, redact, and collect cases.

    ``strict`` turns the first unparseable record into an error instead of a
    skip. Off by default because real logs contain junk lines, on when you want
    a guarantee that nothing was lost.
    """
    result = ImportResult()
    for count, (line, record) in enumerate(records, start=1):
        guard_record_count(count, limits)
        if not isinstance(record, dict):
            message = f"line {line}: expected an object, got {type(record).__name__}"
            if strict:
                raise StoreError(message)
            result.skipped.append(message)
            continue
        try:
            case = parse_record(record, limits=limits)
        except ValueError as exc:
            message = f"line {line}: {exc}"
            if strict:
                raise StoreError(message) from exc
            result.skipped.append(message)
            continue

        if reference_override is not None:
            case = case.model_copy(update={"reference": reference_override})
        if tags:
            case = case.model_copy(update={"tags": tuple(dict.fromkeys(case.tags + tags))})
        if redactor is not None:
            case, report = redactor.case(case)
            result.redaction.merge(report)
        result.cases.append(case)
    return result
