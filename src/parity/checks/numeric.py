"""Numeric agreement between structured outputs.

Extraction, scoring, and classification pipelines put numbers in JSON. A model
swap that shifts those numbers is a silent, high-consequence regression: nothing
fails, the answers are just different.

Only runs when a tolerance is configured. There is no sensible default — the
acceptable drift on a confidence score and on an invoice total are not the same
number, and guessing one would be worse than asking.
"""

from __future__ import annotations

import math
from typing import Any

from parity.checks.base import CheckContext
from parity.checks.structural import MAX_WALK_DEPTH
from parity.domain.models import CheckResult

#: Below this magnitude, relative comparison is meaningless and we fall back to
#: absolute difference against the same tolerance.
ABSOLUTE_FALLBACK_THRESHOLD = 1e-9


def numeric_paths(value: Any, *, prefix: str = "", depth: int = 0) -> dict[str, float]:
    """Map dotted path to numeric value for every number in a parsed JSON value.

    Booleans are excluded. In Python ``bool`` is a subclass of ``int``, and
    treating ``true`` as ``1`` would produce nonsense comparisons.
    """
    if depth >= MAX_WALK_DEPTH:
        return {}

    found: dict[str, float] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.update(numeric_paths(child, prefix=path, depth=depth + 1))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.update(numeric_paths(child, prefix=f"{prefix}[{index}]", depth=depth + 1))
    elif isinstance(value, bool):
        return {}
    elif isinstance(value, (int, float)) and math.isfinite(value):
        found[prefix or "<root>"] = float(value)
    return found


def within_tolerance(baseline: float, candidate: float, tolerance: float) -> bool:
    """Relative comparison, with an absolute fallback near zero."""
    if baseline == candidate:
        return True
    scale = abs(baseline)
    if scale < ABSOLUTE_FALLBACK_THRESHOLD:
        return abs(candidate - baseline) <= tolerance
    return abs(candidate - baseline) / scale <= tolerance


class NumericToleranceCheck:
    """Numbers at matching paths must agree within a relative tolerance."""

    name = "numeric_tolerance"

    def run(self, ctx: CheckContext) -> CheckResult:
        tolerance = ctx.case.expectations.numeric_tolerance
        if tolerance is None:
            tolerance = ctx.settings.numeric_tolerance
        if tolerance is None:
            return CheckResult.skipped(self.name, message="no numeric tolerance configured")
        if tolerance < 0:
            return CheckResult.skipped(self.name, message="negative tolerance ignored")
        if not (ctx.baseline_is_json and ctx.candidate_is_json):
            return CheckResult.skipped(self.name, message="both outputs must be JSON")

        baseline_numbers = numeric_paths(ctx.baseline_json[1])
        if not baseline_numbers:
            return CheckResult.skipped(self.name, message="baseline output contains no numbers")
        candidate_numbers = numeric_paths(ctx.candidate_json[1])

        drifted: list[str] = []
        for path, baseline_value in baseline_numbers.items():
            if path not in candidate_numbers:
                # Absence is the required_fields check's job, not this one.
                continue
            candidate_value = candidate_numbers[path]
            if not within_tolerance(baseline_value, candidate_value, tolerance):
                drifted.append(f"{path}: {baseline_value:g} → {candidate_value:g}")

        if drifted:
            shown = drifted[:8]
            suffix = "" if len(drifted) == len(shown) else f" (+{len(drifted) - len(shown)} more)"
            return CheckResult.failed(
                self.name,
                message=f"{len(drifted)} number(s) moved beyond {tolerance:.1%} tolerance: "
                f"{'; '.join(shown)}{suffix}",
                tolerance=tolerance,
                drifted=drifted,
            )
        return CheckResult.passed(
            self.name, message=f"{len(baseline_numbers)} number(s) within tolerance"
        )
