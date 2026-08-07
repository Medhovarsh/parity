"""Gate evaluation: turning a run report into a pass/fail decision.

Kept separate from both the classifier and the CLI so the rule that decides
whether a build is blocked is one small, directly testable function. A gate that
is hard to reason about gets disabled, and a disabled gate protects nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from parity.domain.models import RunReport, Verdict
from parity.domain.policy import GatePolicy


@dataclass(frozen=True)
class GateDecision:
    """Why the gate passed or failed."""

    passed: bool
    reasons: tuple[str, ...]
    counted_failures: int
    total_cases: int

    def summary(self) -> str:
        if self.passed:
            return f"gate passed: {self.total_cases} case(s), no blocking findings"
        return "gate failed: " + "; ".join(self.reasons)


def evaluate_gate(report: RunReport, policy: GatePolicy) -> GateDecision:
    """Apply ``policy`` to ``report``."""
    reasons: list[str] = []
    summary = report.summary

    if summary.total < policy.min_cases:
        reasons.append(
            f"replayed {summary.total} case(s), which is below the required "
            f"minimum of {policy.min_cases} — the baseline may be empty or truncated"
        )

    counted = sum(len(report.outcomes_with(verdict)) for verdict in policy.fail_on)
    if policy.fail_on_unverified:
        counted += summary.unverified

    if counted > policy.max_failures:
        breakdown: list[str] = []
        for verdict in policy.fail_on:
            count = len(report.outcomes_with(verdict))
            if count:
                breakdown.append(f"{count} {verdict.value}")
        if policy.fail_on_unverified and summary.unverified:
            breakdown.append(f"{summary.unverified} unverified")
        reasons.append(
            f"{counted} failing case(s) ({', '.join(breakdown)}) exceeds the "
            f"budget of {policy.max_failures}"
        )

    return GateDecision(
        passed=not reasons,
        reasons=tuple(reasons),
        counted_failures=counted,
        total_cases=summary.total,
    )


def worst_verdict(report: RunReport) -> Verdict:
    """The most severe verdict present, for a one-line status."""
    for verdict in (
        Verdict.ERROR,
        Verdict.BROKEN,
        Verdict.UNVERIFIED,
        Verdict.ACCEPTABLE,
        Verdict.EQUIVALENT,
    ):
        if report.outcomes_with(verdict):
            return verdict
    return Verdict.EQUIVALENT
