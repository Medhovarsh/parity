"""Accepting a behaviour change into the baseline.

The loop this completes is what separates a living specification from a one-shot
checker. Without it, an *intentional* change means fighting the gate forever, so
people disable the gate. Jest snapshots, Chromatic, and Percy all won on exactly
this: seeing the change, then approving it in one command.

Two deliberate safety properties:

* **Broken cases are not accepted by default.** The common mistake is blessing a
  regression because it was in the same run as an intended change. Accepting a
  `broken` case requires naming it explicitly, or passing ``--force``.
* **Errors are never acceptable.** A case that failed to replay has no candidate
  output to promote, so there is nothing to accept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from parity.domain.models import Case, CaseOutcome, RunReport, Verdict
from parity.ports.store import BaselineStore

#: Verdicts accepted without an explicit case id or ``--force``. These are
#: changes the checks already found unobjectionable.
SAFE_VERDICTS: frozenset[Verdict] = frozenset({Verdict.ACCEPTABLE, Verdict.UNVERIFIED})


@dataclass
class AcceptPlan:
    """What accepting would do, before anything is written."""

    accepted: list[tuple[Case, CaseOutcome]] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    """``(case_id, reason)`` for cases that will not be accepted."""

    unchanged: list[str] = field(default_factory=list)
    """Cases already equivalent — nothing to promote."""

    missing: list[str] = field(default_factory=list)
    """Case ids in the run that are no longer in the baseline."""

    @property
    def count(self) -> int:
        return len(self.accepted)

    @property
    def empty(self) -> bool:
        return not self.accepted


def plan_acceptance(
    report: RunReport,
    store: BaselineStore,
    *,
    case_ids: frozenset[str] | None = None,
    verdicts: frozenset[Verdict] | None = None,
    force: bool = False,
) -> AcceptPlan:
    """Work out which cases would be updated. Reads only.

    ``case_ids`` narrows to specific cases and, by naming them, authorises
    accepting a ``broken`` one. ``verdicts`` narrows by classification.
    """
    plan = AcceptPlan()
    by_id = {case.case_id: case for case in store.iter_cases()}
    allowed = verdicts if verdicts is not None else SAFE_VERDICTS

    for outcome in report.outcomes:
        if case_ids is not None and outcome.case_id not in case_ids:
            continue

        case = by_id.get(outcome.case_id)
        if case is None:
            plan.missing.append(outcome.case_id)
            continue

        if outcome.verdict is Verdict.ERROR or outcome.candidate is None:
            plan.refused.append(
                (outcome.case_id, "case failed to replay, so there is no output to accept")
            )
            continue

        if outcome.verdict is Verdict.EQUIVALENT:
            plan.unchanged.append(outcome.case_id)
            continue

        explicitly_named = case_ids is not None and outcome.case_id in case_ids
        if outcome.verdict not in allowed and not (force or explicitly_named):
            plan.refused.append(
                (
                    outcome.case_id,
                    f"verdict is '{outcome.verdict.value}'; name the case explicitly "
                    "or pass --force to accept a regression deliberately",
                )
            )
            continue

        plan.accepted.append((case, outcome))

    return plan


def apply_acceptance(
    plan: AcceptPlan,
    store: BaselineStore,
    report: RunReport,
    *,
    at: datetime | None = None,
) -> int:
    """Write the plan to the baseline. Returns how many cases were updated.

    Rewrites the whole store rather than appending, because accepting mutates
    existing cases in place. The store's ``replace_all`` is atomic, so an
    interrupted acceptance leaves the previous baseline intact.
    """
    if plan.empty:
        return 0

    updates = {
        case.case_id: case.accept(outcome.candidate, report.candidate_ref, at=at)
        for case, outcome in plan.accepted
        if outcome.candidate is not None
    }
    merged = [updates.get(case.case_id, case) for case in store.iter_cases()]
    store.replace_all(merged)
    return len(updates)
