"""Shared fixtures and builders.

Every test in this suite runs offline. Nothing here opens a socket, reads a
credential, or costs money — if a test needs a model, it uses ``FakeProvider``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from parity.config import ParityConfig
from parity.domain.models import (
    Case,
    Expectations,
    GenerationParams,
    InteractionInput,
    InteractionOutput,
    Message,
    ModelRef,
    ToolCall,
)
from parity.domain.policy import CheckSettings

FIXED_TIME = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def make_input(
    user: str = "hello",
    *,
    system: str | None = None,
    tools: tuple[dict[str, Any], ...] = (),
    response_format: dict[str, Any] | None = None,
    temperature: float | None = None,
) -> InteractionInput:
    messages: list[Message] = []
    if system is not None:
        messages.append(Message(role="system", content=system))
    messages.append(Message(role="user", content=user))
    return InteractionInput(
        messages=tuple(messages),
        params=GenerationParams(temperature=temperature),
        tools=tools,
        response_format=response_format,
    )


def make_output(
    text: str = "hi",
    *,
    tool_calls: tuple[ToolCall, ...] = (),
    finish_reason: str | None = "stop",
    model: str = "ref-1",
) -> InteractionOutput:
    return InteractionOutput(
        text=text, tool_calls=tool_calls, finish_reason=finish_reason, model=model
    )


def json_output(payload: Mapping[str, Any], **kwargs: Any) -> InteractionOutput:
    return make_output(json.dumps(dict(payload), sort_keys=True), **kwargs)


def make_case(
    *,
    user: str = "hello",
    output: InteractionOutput | None = None,
    expectations: Expectations | None = None,
    tags: tuple[str, ...] = (),
    reference: str = "ref:ref-1",
    response_format: dict[str, Any] | None = None,
    tools: tuple[dict[str, Any], ...] = (),
) -> Case:
    return Case.create(
        input=make_input(user, response_format=response_format, tools=tools),
        output=output or make_output(),
        reference=ModelRef.parse(reference),
        expectations=expectations,
        tags=tags,
        captured_at=FIXED_TIME,
    )


@pytest.fixture
def settings() -> CheckSettings:
    return CheckSettings()


@pytest.fixture
def case() -> Case:
    return make_case()


@pytest.fixture
def tmp_config(tmp_path: Path) -> ParityConfig:
    """Config rooted in a temp directory, so nothing touches the real project."""
    return ParityConfig(root=tmp_path)


@pytest.fixture
def sample_log(tmp_path: Path) -> Path:
    """A small proxy-shaped interaction log."""
    records = [
        {
            "request": {
                "model": "ref-1",
                "messages": [{"role": "user", "content": "extract invoice 1"}],
            },
            "response": {
                "model": "ref-1",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"id": "1", "total": 10.0}',
                        },
                    }
                ],
            },
            "tags": ["extraction"],
        },
        {
            "request": {
                "model": "ref-1",
                "messages": [{"role": "user", "content": "summarise the thread"}],
            },
            "response": {
                "model": "ref-1",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "A short summary."},
                    }
                ],
            },
            "tags": ["summary"],
        },
    ]
    path = tmp_path / "log.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path
