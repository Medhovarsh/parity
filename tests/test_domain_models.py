"""Domain model invariants."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from parity.domain.models import (
    Case,
    CaseOutcome,
    CheckResult,
    CheckStatus,
    InteractionInput,
    InteractionOutput,
    Message,
    ModelRef,
    RunReport,
    RunSummary,
    Severity,
    Verdict,
    canonical_json,
    fingerprint,
)
from tests.conftest import make_case, make_input, make_output


class TestModelRef:
    def test_roundtrip(self) -> None:
        ref = ModelRef.parse("openai:gpt-4o-mini")
        assert (ref.provider, ref.model) == ("openai", "gpt-4o-mini")
        assert str(ref) == "openai:gpt-4o-mini"

    def test_model_may_contain_colons(self) -> None:
        ref = ModelRef.parse("ollama:llama3.1:8b-instruct")
        assert ref.model == "llama3.1:8b-instruct"

    @pytest.mark.parametrize("value", ["", "openai", "openai:", ":gpt", "  :  "])
    def test_rejects_malformed(self, value: str) -> None:
        with pytest.raises(ValueError, match="invalid model reference"):
            ModelRef.parse(value)


class TestFingerprint:
    def test_is_stable_across_key_order(self) -> None:
        assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})

    def test_distinguishes_different_values(self) -> None:
        assert fingerprint({"a": 1}) != fingerprint({"a": 2})

    def test_canonical_json_is_deterministic(self) -> None:
        assert canonical_json({"b": 1, "a": [3, 2]}) == '{"a":[3,2],"b":1}'

    def test_survives_unserialisable_values(self) -> None:
        # Must never be the thing that breaks a capture.
        assert fingerprint({"when": datetime(2026, 1, 1)})


class TestCase:
    def test_id_derives_from_input(self) -> None:
        case = make_case(user="same")
        assert case.case_id == case.input.fingerprint()

    def test_same_input_yields_same_id(self) -> None:
        assert make_case(user="x").case_id == make_case(user="x").case_id

    def test_different_input_yields_different_id(self) -> None:
        assert make_case(user="x").case_id != make_case(user="y").case_id

    def test_explicit_id_is_preserved(self) -> None:
        case = Case.create(
            input=make_input(),
            output=make_output(),
            reference=ModelRef.parse("a:b"),
            case_id="chosen",
        )
        assert case.case_id == "chosen"

    def test_is_frozen(self) -> None:
        case = make_case()
        with pytest.raises(ValidationError):
            case.case_id = "mutated"

    def test_rejects_naive_timestamp(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            Case(
                case_id="x",
                input=make_input(),
                output=make_output(),
                reference=ModelRef.parse("a:b"),
                captured_at=datetime(2026, 1, 1),
            )

    def test_normalises_timestamp_to_utc(self) -> None:
        case = make_case()
        assert case.captured_at.tzinfo == UTC


class TestInteractionModels:
    def test_input_requires_a_message(self) -> None:
        with pytest.raises(ValidationError, match="at least one message"):
            InteractionInput(messages=())

    def test_message_coerces_null_content(self) -> None:
        # Providers emit null content alongside tool calls.
        assert Message(role="assistant", content=None).content == ""  # type: ignore[arg-type]

    def test_output_emptiness(self) -> None:
        assert InteractionOutput(text="   ").is_empty()
        assert not InteractionOutput(text="x").is_empty()

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InteractionOutput(text="x", unexpected=1)


class TestCheckResult:
    def test_blocking_failure_detection(self) -> None:
        assert CheckResult.failed("c", message="m").blocking_failure
        assert not CheckResult.failed("c", message="m", severity=Severity.WARNING).blocking_failure
        assert not CheckResult.passed("c").blocking_failure

    def test_skip_is_informational(self) -> None:
        result = CheckResult.skipped("c", message="n/a")
        assert result.status is CheckStatus.SKIP
        assert result.severity is Severity.INFO


class TestRunSummary:
    def test_counts_by_verdict(self) -> None:
        outcomes = tuple(
            CaseOutcome(case_id=str(i), verdict=v)
            for i, v in enumerate(
                [
                    Verdict.EQUIVALENT,
                    Verdict.EQUIVALENT,
                    Verdict.BROKEN,
                    Verdict.UNVERIFIED,
                    Verdict.ERROR,
                ]
            )
        )
        summary = RunSummary.from_outcomes(outcomes)
        assert (summary.total, summary.equivalent, summary.broken) == (5, 2, 1)
        assert summary.regressions == 2

    def test_empty(self) -> None:
        assert RunSummary.from_outcomes(()).total == 0


class TestCaseOutcome:
    def test_primary_reason_prefers_error(self) -> None:
        outcome = CaseOutcome(
            case_id="c",
            verdict=Verdict.ERROR,
            error="provider exploded",
            checks=(CheckResult.failed("x", message="also this"),),
        )
        assert outcome.primary_reason == "provider exploded"

    def test_primary_reason_prefers_blocking_over_warning(self) -> None:
        outcome = CaseOutcome(
            case_id="c",
            verdict=Verdict.BROKEN,
            checks=(
                CheckResult.failed("warn", message="warned", severity=Severity.WARNING),
                CheckResult.failed("block", message="blocked"),
            ),
        )
        assert outcome.primary_reason == "blocked"

    def test_primary_reason_falls_back_to_judge(self) -> None:
        outcome = CaseOutcome(
            case_id="c", verdict=Verdict.ACCEPTABLE, judge_rationale="same meaning"
        )
        assert outcome.primary_reason == "same meaning"


class TestRunReport:
    def test_build_computes_summary_and_id(self) -> None:
        outcomes = (
            CaseOutcome(case_id="a", verdict=Verdict.EQUIVALENT),
            CaseOutcome(case_id="b", verdict=Verdict.BROKEN),
        )
        report = RunReport.build(
            parity_version="0.1.0",
            baseline_ref=ModelRef.parse("a:b"),
            candidate_ref=ModelRef.parse("c:d"),
            baseline_source="mem",
            judge="none",
            outcomes=outcomes,
        )
        assert report.summary.total == 2
        assert report.summary.broken == 1
        assert report.run_id

    def test_outcomes_with_filters(self) -> None:
        outcomes = (
            CaseOutcome(case_id="a", verdict=Verdict.EQUIVALENT),
            CaseOutcome(case_id="b", verdict=Verdict.BROKEN),
            CaseOutcome(case_id="c", verdict=Verdict.ERROR),
        )
        report = RunReport.build(
            parity_version="0.1.0",
            baseline_ref=ModelRef.parse("a:b"),
            candidate_ref=ModelRef.parse("c:d"),
            baseline_source="mem",
            judge="none",
            outcomes=outcomes,
        )
        assert len(report.outcomes_with(Verdict.BROKEN, Verdict.ERROR)) == 2


def test_verdict_regression_flags() -> None:
    assert Verdict.BROKEN.is_regression
    assert Verdict.ERROR.is_regression
    assert not Verdict.UNVERIFIED.is_regression
    assert not Verdict.EQUIVALENT.is_regression
