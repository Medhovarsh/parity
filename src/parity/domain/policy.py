"""Policy: the tunable knobs that decide what counts as a regression.

Separated from the checks themselves so that "what we measure" and "how much we
care" stay independent. A team can tighten the gate without touching detection
logic, and detection logic can be tested without a policy debate.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from parity.domain.models import Severity, Verdict

_FROZEN = ConfigDict(frozen=True, extra="forbid")

#: Conservative refusal markers. Every one is a phrase a model emits when it has
#: declined the task outright. Kept narrow on purpose — a false positive here
#: blocks a deploy, so the cost of over-matching is high. Matched
#: case-insensitively against the first part of the output only.
DEFAULT_REFUSAL_PATTERNS: tuple[str, ...] = (
    r"i can'?t help with that",
    r"i cannot help with that",
    r"i can'?t assist with",
    r"i cannot assist with",
    r"i'?m not able to help",
    r"i am not able to help",
    r"i'?m unable to (?:help|assist|provide)",
    r"i am unable to (?:help|assist|provide)",
    r"i (?:can'?t|cannot) (?:provide|create|generate) that",
    r"i (?:must|have to) decline",
    r"as an ai(?: language)? model,? i (?:can'?t|cannot)",
    r"sorry,? (?:but )?i (?:can'?t|cannot)",
)

#: How far into the output to look for a refusal. A refusal is a preamble; text
#: matching these phrases 4000 characters in is discussing refusal, not
#: performing one.
REFUSAL_SCAN_CHARS = 400


class CheckSettings(BaseModel):
    """Configuration for the deterministic check pipeline."""

    model_config = _FROZEN

    disabled: tuple[str, ...] = ()
    """Check names to skip entirely."""

    infer_required_fields: bool = True
    """When the baseline output is JSON, require the candidate to keep its keys."""

    infer_tool_calls: bool = True
    """When the baseline called tools, require the candidate to call the same ones."""

    max_length_delta_ratio: float = Field(default=0.5, ge=0.0)
    """Allowed relative change in output length before the length check fires."""

    length_severity: Severity = Severity.WARNING
    """Length change is usually a smell, not a break. Raise to BLOCKING to enforce."""

    numeric_tolerance: float | None = None
    """Relative tolerance for numbers in JSON output. ``None`` disables the check."""

    refusal_patterns: tuple[str, ...] = DEFAULT_REFUSAL_PATTERNS

    treat_warnings_as_broken: bool = False
    """Promote every warning-severity failure to a blocking one."""

    ignore_whitespace: bool = True
    """Normalise whitespace before deciding two outputs are byte-equivalent."""

    ignore_case: bool = False
    """Case-insensitive equivalence. Off by default; casing is often meaningful."""


class GatePolicy(BaseModel):
    """Decides the process exit code from a run summary."""

    model_config = _FROZEN

    fail_on: tuple[Verdict, ...] = (Verdict.BROKEN, Verdict.ERROR)
    """Verdicts that count against the budgets below."""

    max_failures: int = Field(default=0, ge=0)
    """How many failing cases are tolerated. Zero means any failure blocks."""

    fail_on_unverified: bool = False
    """Treat 'differs, nobody checked' as a failure. Sensible once a judge exists."""

    min_cases: int = Field(default=1, ge=0)
    """Guard against an empty or truncated baseline silently passing the gate."""
