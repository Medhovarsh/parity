"""Semantic judge port.

A judge is consulted only after the deterministic checks pass and the outputs
still differ. That ordering is a cost decision as much as a correctness one:
deterministic checks are free, judges are not, and most differences are settled
before a judge is ever asked.

Returning ``None`` means *abstain*, and produces a ``UNVERIFIED`` verdict. A
judge that cannot form an opinion must say so rather than guess — a fabricated
"equivalent" is the single most damaging thing this tool could do.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from parity.domain.models import Case, InteractionOutput, JudgeVerdict


@runtime_checkable
class SemanticJudge(Protocol):
    """Decides whether two differing outputs mean the same thing."""

    @property
    def name(self) -> str: ...

    def compare(self, case: Case, candidate: InteractionOutput) -> JudgeVerdict | None:
        """Compare candidate output against the case's reference output.

        Returns ``None`` to abstain. Must raise
        :class:`parity.errors.JudgeError` only for genuine failures, not for
        uncertainty — uncertainty is what abstention is for.
        """
        ...

    def close(self) -> None: ...
