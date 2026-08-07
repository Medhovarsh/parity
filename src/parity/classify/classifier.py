"""The classifier: one case in, one verdict out.

The decision order encodes the product's cost model and its honesty policy.

1. Run the deterministic checks. They are free.
2. Any blocking failure ends it — ``BROKEN``. There is nothing to ask a judge
   about output that no longer parses.
3. Outputs identical after normalisation — ``EQUIVALENT``. Also free.
4. Outputs differ and no judge is configured — ``UNVERIFIED``. Not a pass and
   not a fail. Saying "this changed and nobody looked" is the truthful answer,
   and it is what makes the tool safe to adopt before anyone has set up a judge.
5. Otherwise, ask the judge. An abstaining judge also yields ``UNVERIFIED``.

Steps 1-3 settle the large majority of cases in practice, which is what keeps a
full replay affordable.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from parity.checks.base import Check, CheckContext
from parity.checks.registry import pipeline_for_case
from parity.checks.textual import normalise
from parity.domain.models import (
    Case,
    CaseOutcome,
    CheckResult,
    CheckStatus,
    InteractionOutput,
    Severity,
    Verdict,
    canonical_json,
)
from parity.domain.policy import CheckSettings
from parity.errors import CheckError, JudgeError
from parity.ports.judge import SemanticJudge


def outputs_equivalent(
    baseline: InteractionOutput,
    candidate: InteractionOutput,
    settings: CheckSettings,
) -> bool:
    """True when the two outputs are indistinguishable after normalisation.

    Tool calls are compared by name and arguments, not by provider-assigned call
    id — those ids are random per request and would make every case differ.
    """
    if normalise(
        baseline.text,
        ignore_whitespace=settings.ignore_whitespace,
        ignore_case=settings.ignore_case,
    ) != normalise(
        candidate.text,
        ignore_whitespace=settings.ignore_whitespace,
        ignore_case=settings.ignore_case,
    ):
        return False

    def signature(output: InteractionOutput) -> list[tuple[str, str]]:
        return sorted((tc.name, canonical_json(tc.arguments)) for tc in output.tool_calls)

    return signature(baseline) == signature(candidate)


@dataclass(frozen=True)
class Classification:
    """Internal result of classifying one case, before timing is attached."""

    verdict: Verdict
    checks: tuple[CheckResult, ...]
    judge_rationale: str | None = None
    judge_confidence: float | None = None
    error: str | None = None


class Classifier:
    """Applies the check pipeline and, when needed, the judge."""

    def __init__(
        self,
        *,
        pipeline: Sequence[Check],
        settings: CheckSettings,
        judge: SemanticJudge | None = None,
    ) -> None:
        self._pipeline = tuple(pipeline)
        self._settings = settings
        self._judge = judge

    @property
    def judge_name(self) -> str:
        return self._judge.name if self._judge is not None else "none"

    def run_checks(self, case: Case, candidate: InteractionOutput) -> tuple[CheckResult, ...]:
        """Evaluate every applicable check, isolating failures inside checks."""
        ctx = CheckContext(case=case, candidate=candidate, settings=self._settings)
        results: list[CheckResult] = []
        for check in pipeline_for_case(self._pipeline, case):
            try:
                result = check.run(ctx)
            except CheckError as exc:
                # A misconfigured check should not take down the whole run, but it
                # must be visible rather than silently absent.
                result = CheckResult.failed(
                    check.name,
                    message=f"check could not be evaluated: {exc}",
                    severity=Severity.WARNING,
                )
            except Exception as exc:
                result = CheckResult.failed(
                    check.name,
                    message=f"check raised {type(exc).__name__}: {exc}",
                    severity=Severity.WARNING,
                )
            results.append(self._apply_severity_policy(result))
        return tuple(results)

    def _apply_severity_policy(self, result: CheckResult) -> CheckResult:
        if (
            self._settings.treat_warnings_as_broken
            and result.status is CheckStatus.FAIL
            and result.severity is Severity.WARNING
        ):
            return result.model_copy(update={"severity": Severity.BLOCKING})
        return result

    def classify(self, case: Case, candidate: InteractionOutput) -> Classification:
        checks = self.run_checks(case, candidate)

        if any(result.blocking_failure for result in checks):
            return Classification(verdict=Verdict.BROKEN, checks=checks)

        if outputs_equivalent(case.output, candidate, self._settings):
            return Classification(verdict=Verdict.EQUIVALENT, checks=checks)

        if self._judge is None:
            return Classification(verdict=Verdict.UNVERIFIED, checks=checks)

        try:
            opinion = self._judge.compare(case, candidate)
        except JudgeError as exc:
            return Classification(
                verdict=Verdict.UNVERIFIED,
                checks=checks,
                judge_rationale=f"judge unavailable: {exc}",
            )

        if opinion is None:
            return Classification(
                verdict=Verdict.UNVERIFIED,
                checks=checks,
                judge_rationale="judge abstained",
            )

        return Classification(
            verdict=opinion.to_verdict(),
            checks=checks,
            judge_rationale=opinion.rationale or None,
            judge_confidence=opinion.confidence,
        )

    def classify_to_outcome(
        self,
        case: Case,
        candidate: InteractionOutput,
        *,
        duration_ms: int | None = None,
    ) -> CaseOutcome:
        """Convenience wrapper producing a persistable outcome."""
        started = time.perf_counter()
        classification = self.classify(case, candidate)
        elapsed = (
            duration_ms if duration_ms is not None else int((time.perf_counter() - started) * 1000)
        )
        return CaseOutcome(
            case_id=case.case_id,
            verdict=classification.verdict,
            checks=classification.checks,
            candidate=candidate,
            judge_rationale=classification.judge_rationale,
            judge_confidence=classification.judge_confidence,
            error=classification.error,
            duration_ms=elapsed,
            tags=case.tags,
        )
