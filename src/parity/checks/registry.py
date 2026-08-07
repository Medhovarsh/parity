"""Check registry and pipeline assembly.

Order matters. Cheap structural checks run first so that an output which no
longer parses is reported as such, rather than as a length anomaly. The
classifier stops at the first blocking failure when reporting a reason, so the
most explanatory check should come first.
"""

from __future__ import annotations

from collections.abc import Sequence

from parity.checks.base import Check
from parity.checks.numeric import NumericToleranceCheck
from parity.checks.structural import (
    EmptyOutputCheck,
    JsonParseCheck,
    JsonSchemaCheck,
    RequiredFieldsCheck,
    ToolCallCheck,
    TruncationCheck,
)
from parity.checks.textual import (
    ExactMatchCheck,
    FormatRegexCheck,
    LengthDeltaCheck,
    RefusalCheck,
)
from parity.domain.models import Case
from parity.domain.policy import CheckSettings
from parity.errors import ConfigError

#: Every available check, in evaluation order.
ALL_CHECKS: tuple[Check, ...] = (
    EmptyOutputCheck(),
    TruncationCheck(),
    RefusalCheck(),
    JsonParseCheck(),
    JsonSchemaCheck(),
    RequiredFieldsCheck(),
    ToolCallCheck(),
    ExactMatchCheck(),
    FormatRegexCheck(),
    NumericToleranceCheck(),
    LengthDeltaCheck(),
)


def check_names() -> tuple[str, ...]:
    return tuple(check.name for check in ALL_CHECKS)


def build_pipeline(
    settings: CheckSettings,
    *,
    available: Sequence[Check] = ALL_CHECKS,
) -> tuple[Check, ...]:
    """Return the checks enabled by ``settings``, preserving canonical order.

    Raises rather than ignoring an unknown name in ``disabled`` — a typo there
    silently weakens the gate, which is exactly the failure this tool exists to
    prevent.
    """
    known = {check.name for check in available}
    unknown = sorted(set(settings.disabled) - known)
    if unknown:
        raise ConfigError(
            f"unknown check name(s) in disabled list: {', '.join(unknown)}. "
            f"Available checks: {', '.join(sorted(known))}"
        )
    return tuple(check for check in available if check.name not in settings.disabled)


def pipeline_for_case(pipeline: Sequence[Check], case: Case) -> tuple[Check, ...]:
    """Apply per-case ``skip_checks`` on top of the global pipeline."""
    skipped = set(case.expectations.skip_checks)
    if not skipped:
        return tuple(pipeline)
    return tuple(check for check in pipeline if check.name not in skipped)
