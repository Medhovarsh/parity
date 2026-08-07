"""Core domain model.

Design notes worth knowing before changing anything here:

* Models are **frozen**. A baseline is evidence; mutating it in place would make
  a run report unreproducible.
* Sequences are tuples, not lists. Combined with ``frozen=True`` this makes the
  models hashable and prevents a check from quietly editing shared state.
* ``extra="forbid"`` everywhere. A typo in a config or an unexpected field in an
  imported file should fail loudly at parse time, not silently do nothing.
* Case identity is a fingerprint of the *input*, not of the output. Two captures
  of the same prompt against different models are the same case observed twice.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_FROZEN = ConfigDict(frozen=True, extra="forbid")

Role = Literal["system", "user", "assistant", "tool"]


def canonical_json(payload: Any) -> str:
    """Serialise deterministically so fingerprints are stable across runs.

    Sorted keys, no insignificant whitespace, non-ASCII preserved. Anything not
    natively serialisable falls back to ``repr`` rather than raising, because a
    fingerprint must never be the thing that breaks a capture.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=repr,
    )


def fingerprint(payload: Any, *, length: int = 16) -> str:
    """Stable short digest of any JSON-serialisable value."""
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return digest[:length]


def utc_now() -> datetime:
    """Timezone-aware UTC now. Naive datetimes are banned in this codebase."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Interaction primitives
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    """A single tool/function invocation requested by the model."""

    model_config = _FROZEN

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = None

    def signature(self) -> str:
        """Name plus argument *shape*, ignoring values.

        Used by the tool-call check to distinguish "called a different tool"
        (structural, blocking) from "called the same tool with different
        arguments" (semantic, judged).
        """
        return f"{self.name}({','.join(sorted(self.arguments))})"


class Message(BaseModel):
    """One turn in a conversation."""

    model_config = _FROZEN

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    name: str | None = None
    tool_call_id: str | None = None

    @field_validator("content", mode="before")
    @classmethod
    def _coerce_none_content(cls, value: object) -> object:
        # Several provider APIs emit null content alongside tool calls. This must
        # run in "before" mode: an "after" validator never sees the None, because
        # pydantic rejects it against the str annotation first.
        return "" if value is None else value


class GenerationParams(BaseModel):
    """Sampling parameters recorded alongside an interaction.

    Captured because a temperature change is a behaviour change, and a diff that
    ignores it will mislead whoever reviews it.
    """

    model_config = _FROZEN

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    stop: tuple[str, ...] = ()
    extra: dict[str, Any] = Field(default_factory=dict)


class InteractionInput(BaseModel):
    """Everything that was sent to the model, minus credentials."""

    model_config = _FROZEN

    messages: tuple[Message, ...]
    params: GenerationParams = Field(default_factory=GenerationParams)
    tools: tuple[dict[str, Any], ...] = ()
    response_format: dict[str, Any] | None = None

    @field_validator("messages")
    @classmethod
    def _require_at_least_one_message(cls, value: tuple[Message, ...]) -> tuple[Message, ...]:
        if not value:
            raise ValueError("interaction input must contain at least one message")
        return value

    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class InteractionOutput(BaseModel):
    """Everything the model returned."""

    model_config = _FROZEN

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    model: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.text.strip() and not self.tool_calls


class ModelRef(BaseModel):
    """A provider/model pair, e.g. ``openai:gpt-4o-mini`` or ``ollama:llama3``."""

    model_config = _FROZEN

    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"

    @classmethod
    def parse(cls, value: str) -> ModelRef:
        """Parse ``provider:model``. The model half may itself contain colons."""
        provider, separator, model = value.partition(":")
        if not separator or not provider.strip() or not model.strip():
            raise ValueError(
                f"invalid model reference {value!r}; expected 'provider:model', "
                "for example 'ollama:llama3.1'"
            )
        return cls(provider=provider.strip(), model=model.strip())


# ---------------------------------------------------------------------------
# Expectations
# ---------------------------------------------------------------------------


class Expectations(BaseModel):
    """Per-case overrides for the check pipeline.

    Every field defaults to ``None``, meaning "infer from the baseline output".
    Inference is the product's whole premise: you should get a useful gate
    without authoring anything. Overrides exist for the cases where inference is
    wrong, not as the primary path.
    """

    model_config = _FROZEN

    json_schema: dict[str, Any] | None = None
    required_fields: tuple[str, ...] | None = None
    format_regex: str | None = None
    must_call_tools: tuple[str, ...] | None = None
    max_length_delta_ratio: float | None = None
    numeric_tolerance: float | None = None
    forbid_refusal: bool | None = None
    exact_match: bool = False
    skip_checks: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


class Case(BaseModel):
    """One captured interaction: an input plus the reference output for it."""

    model_config = _FROZEN

    case_id: str
    input: InteractionInput
    output: InteractionOutput
    reference: ModelRef
    captured_at: datetime = Field(default_factory=utc_now)
    tags: tuple[str, ...] = ()
    expectations: Expectations = Field(default_factory=Expectations)
    redacted: bool = False

    revision: int = 1
    """Bumped each time a candidate output is accepted as the new reference.

    A baseline is meant to be a *living* specification: when a behaviour change
    is intentional, you accept it rather than fighting the gate forever. This
    records that the reference moved, so a reviewer can tell an original capture
    from an approved change.
    """

    accepted_at: datetime | None = None
    previous_reference: ModelRef | None = None
    """What the reference was before the most recent acceptance."""

    @field_validator("captured_at")
    @classmethod
    def _require_tzaware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        return value.astimezone(UTC)

    @classmethod
    def create(
        cls,
        *,
        input: InteractionInput,  # noqa: A002 - mirrors the field name deliberately
        output: InteractionOutput,
        reference: ModelRef,
        case_id: str | None = None,
        tags: tuple[str, ...] = (),
        expectations: Expectations | None = None,
        captured_at: datetime | None = None,
        redacted: bool = False,
    ) -> Case:
        """Build a case, deriving a stable id from the input when none is given."""
        return cls(
            case_id=case_id or input.fingerprint(),
            input=input,
            output=output,
            reference=reference,
            captured_at=captured_at or utc_now(),
            tags=tags,
            expectations=expectations or Expectations(),
            redacted=redacted,
        )

    def accept(
        self,
        candidate: InteractionOutput,
        candidate_reference: ModelRef,
        *,
        at: datetime | None = None,
    ) -> Case:
        """Promote a candidate output to be this case's new reference.

        The case id does not change: it is a fingerprint of the *input*, and the
        input did not move. Only what the model is expected to do with it has.
        """
        return self.model_copy(
            update={
                "output": candidate,
                "previous_reference": self.reference,
                "reference": candidate_reference,
                "revision": self.revision + 1,
                "accepted_at": at or utc_now(),
            }
        )


# ---------------------------------------------------------------------------
# Check and verdict vocabulary
# ---------------------------------------------------------------------------


class CheckStatus(StrEnum):
    PASS = "pass"  # noqa: S105 - a check status, not a credential
    FAIL = "fail"
    SKIP = "skip"


class Severity(StrEnum):
    """How much a failing check should matter.

    ``BLOCKING`` failures decide the verdict on their own and short-circuit the
    judge — there is no point paying for a semantic comparison of output that no
    longer parses.
    """

    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


class CheckResult(BaseModel):
    model_config = _FROZEN

    check: str
    status: CheckStatus
    severity: Severity = Severity.BLOCKING
    message: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)

    @property
    def blocking_failure(self) -> bool:
        return self.status is CheckStatus.FAIL and self.severity is Severity.BLOCKING

    @classmethod
    def passed(cls, check: str, *, message: str = "", **evidence: Any) -> CheckResult:
        return cls(check=check, status=CheckStatus.PASS, message=message, evidence=evidence)

    @classmethod
    def failed(
        cls,
        check: str,
        *,
        message: str,
        severity: Severity = Severity.BLOCKING,
        **evidence: Any,
    ) -> CheckResult:
        return cls(
            check=check,
            status=CheckStatus.FAIL,
            severity=severity,
            message=message,
            evidence=evidence,
        )

    @classmethod
    def skipped(cls, check: str, *, message: str) -> CheckResult:
        return cls(
            check=check,
            status=CheckStatus.SKIP,
            severity=Severity.INFO,
            message=message,
        )


class Verdict(StrEnum):
    """The classification of one case's behaviour change.

    ``UNVERIFIED`` is not a hedge, it is a fact: the deterministic checks passed
    but the outputs differ and no judge was configured to say whether the
    difference matters. Reporting that honestly is better than defaulting it to
    pass and better than defaulting it to fail.
    """

    EQUIVALENT = "equivalent"
    ACCEPTABLE = "acceptable"
    UNVERIFIED = "unverified"
    BROKEN = "broken"
    ERROR = "error"

    @property
    def is_regression(self) -> bool:
        return self in (Verdict.BROKEN, Verdict.ERROR)


class JudgeVerdict(BaseModel):
    """A semantic judge's opinion about one pair of outputs."""

    model_config = _FROZEN

    verdict: Literal["equivalent", "acceptable", "broken"]
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    rationale: str = ""

    def to_verdict(self) -> Verdict:
        return Verdict(self.verdict)


# ---------------------------------------------------------------------------
# Run results
# ---------------------------------------------------------------------------


class CaseOutcome(BaseModel):
    """The result of replaying and classifying a single case."""

    model_config = _FROZEN

    case_id: str
    verdict: Verdict
    checks: tuple[CheckResult, ...] = ()
    candidate: InteractionOutput | None = None
    judge_rationale: str | None = None
    judge_confidence: float | None = None
    error: str | None = None
    duration_ms: int = 0
    tags: tuple[str, ...] = ()

    @property
    def failed_checks(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status is CheckStatus.FAIL)

    @property
    def primary_reason(self) -> str:
        """One-line explanation for a report row."""
        if self.error:
            return self.error
        blocking = [c for c in self.checks if c.blocking_failure]
        if blocking:
            return blocking[0].message
        warnings = self.failed_checks
        if warnings:
            return warnings[0].message
        if self.judge_rationale:
            return self.judge_rationale
        return ""


class RunSummary(BaseModel):
    """Verdict tallies for a run."""

    model_config = _FROZEN

    total: int = 0
    equivalent: int = 0
    acceptable: int = 0
    unverified: int = 0
    broken: int = 0
    error: int = 0

    @classmethod
    def from_outcomes(cls, outcomes: tuple[CaseOutcome, ...]) -> RunSummary:
        counts = dict.fromkeys(Verdict, 0)
        for outcome in outcomes:
            counts[outcome.verdict] += 1
        return cls(
            total=len(outcomes),
            equivalent=counts[Verdict.EQUIVALENT],
            acceptable=counts[Verdict.ACCEPTABLE],
            unverified=counts[Verdict.UNVERIFIED],
            broken=counts[Verdict.BROKEN],
            error=counts[Verdict.ERROR],
        )

    @property
    def regressions(self) -> int:
        return self.broken + self.error


class RunReport(BaseModel):
    """A complete, self-describing record of one replay.

    Self-describing matters: this artefact is the thing a reviewer reads, a CI
    job uploads, and — at the enterprise tier — an auditor asks for. It must be
    interpretable without the config that produced it.
    """

    model_config = _FROZEN

    run_id: str
    created_at: datetime = Field(default_factory=utc_now)
    parity_version: str
    baseline_ref: ModelRef
    candidate_ref: ModelRef
    baseline_source: str
    judge: str
    outcomes: tuple[CaseOutcome, ...] = ()
    summary: RunSummary = Field(default_factory=RunSummary)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        parity_version: str,
        baseline_ref: ModelRef,
        candidate_ref: ModelRef,
        baseline_source: str,
        judge: str,
        outcomes: tuple[CaseOutcome, ...],
        config_snapshot: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        run_id: str | None = None,
    ) -> RunReport:
        stamp = created_at or utc_now()
        return cls(
            run_id=run_id
            or (
                f"{stamp.strftime('%Y%m%dT%H%M%SZ')}-"
                f"{fingerprint([str(candidate_ref), len(outcomes)], length=8)}"
            ),
            created_at=stamp,
            parity_version=parity_version,
            baseline_ref=baseline_ref,
            candidate_ref=candidate_ref,
            baseline_source=baseline_source,
            judge=judge,
            outcomes=outcomes,
            summary=RunSummary.from_outcomes(outcomes),
            config_snapshot=config_snapshot or {},
        )

    def outcomes_with(self, *verdicts: Verdict) -> tuple[CaseOutcome, ...]:
        wanted = set(verdicts)
        return tuple(o for o in self.outcomes if o.verdict in wanted)
