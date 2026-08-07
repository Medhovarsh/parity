"""Check protocol and the context handed to every check."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Protocol, runtime_checkable

from parity.domain.models import Case, CheckResult, InteractionOutput
from parity.domain.policy import CheckSettings


def try_parse_json(text: str) -> tuple[bool, Any]:
    """Parse ``text`` as JSON, tolerating the fenced-code wrapper models emit.

    Returns ``(ok, value)``. Never raises — a check reporting "this is not JSON"
    is a finding, not an exception.
    """
    stripped = text.strip()
    if not stripped:
        return False, None

    # Unwrap ```json ... ``` and ``` ... ``` fences, which are extremely common
    # and are a formatting difference rather than a structural one.
    if stripped.startswith("```"):
        body = stripped[3:]
        if body[:4].lower() == "json":
            body = body[4:]
        closing = body.rfind("```")
        if closing != -1:
            body = body[:closing]
        stripped = body.strip()

    try:
        return True, json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return False, None


@dataclass(frozen=True)
class CheckContext:
    """Everything a check is allowed to look at.

    Parsed forms are computed lazily and cached, so eight checks inspecting the
    same JSON output parse it once.
    """

    case: Case
    candidate: InteractionOutput
    settings: CheckSettings

    @property
    def baseline(self) -> InteractionOutput:
        return self.case.output

    @cached_property
    def baseline_json(self) -> tuple[bool, Any]:
        return try_parse_json(self.baseline.text)

    @cached_property
    def candidate_json(self) -> tuple[bool, Any]:
        return try_parse_json(self.candidate.text)

    @property
    def baseline_is_json(self) -> bool:
        return self.baseline_json[0]

    @property
    def candidate_is_json(self) -> bool:
        return self.candidate_json[0]


@runtime_checkable
class Check(Protocol):
    """A single deterministic comparison between baseline and candidate."""

    @property
    def name(self) -> str: ...

    def run(self, ctx: CheckContext) -> CheckResult:
        """Evaluate. Return a ``SKIP`` result when the check does not apply.

        Must not raise for ordinary "does not apply" or "found a problem" cases.
        Raising is reserved for genuine defects, and the runner will surface it
        as an ``ERROR`` verdict for that case rather than aborting the run.
        """
        ...
