"""Judges that need no model."""

from __future__ import annotations

from collections.abc import Mapping

from parity.domain.models import Case, InteractionOutput, JudgeVerdict


class NoJudge:
    """Always abstains.

    The default. Every differing case is reported ``UNVERIFIED``, which is the
    honest answer when nothing has semantically compared the outputs. Used
    explicitly rather than passing ``None`` when a caller wants the judge name
    to appear in the run report.
    """

    @property
    def name(self) -> str:
        return "none"

    def compare(self, case: Case, candidate: InteractionOutput) -> JudgeVerdict | None:
        return None

    def close(self) -> None:
        return


class ScriptedJudge:
    """Returns pre-set verdicts by case id. Test double."""

    def __init__(self, verdicts: Mapping[str, JudgeVerdict], *, name: str = "scripted") -> None:
        self._verdicts = dict(verdicts)
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def compare(self, case: Case, candidate: InteractionOutput) -> JudgeVerdict | None:
        return self._verdicts.get(case.case_id)

    def close(self) -> None:
        return
