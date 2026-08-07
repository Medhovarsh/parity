"""Classification order and honesty.

The decision order is the product's core logic, and the ``UNVERIFIED`` verdict is
the part most likely to be "simplified" away by a future change. These tests
pin both.
"""

from __future__ import annotations

from parity.adapters.judges.none_judge import ScriptedJudge
from parity.checks.registry import build_pipeline
from parity.classify.classifier import Classifier, outputs_equivalent
from parity.domain.models import (
    Case,
    Expectations,
    InteractionOutput,
    JudgeVerdict,
    Severity,
    ToolCall,
    Verdict,
)
from parity.domain.policy import CheckSettings
from parity.errors import JudgeError
from tests.conftest import json_output, make_case, make_output


def classifier(judge: object = None, **settings_kwargs: object) -> Classifier:
    settings = CheckSettings(**settings_kwargs)  # type: ignore[arg-type]
    return Classifier(
        pipeline=build_pipeline(settings),
        settings=settings,
        judge=judge,  # type: ignore[arg-type]
    )


class TestVerdictOrder:
    def test_identical_output_is_equivalent(self) -> None:
        case = make_case(output=make_output("same"))
        assert classifier().classify(case, make_output("same")).verdict is Verdict.EQUIVALENT

    def test_whitespace_only_difference_is_equivalent(self) -> None:
        case = make_case(output=make_output("a b"))
        assert classifier().classify(case, make_output(" a   b ")).verdict is Verdict.EQUIVALENT

    def test_differing_output_without_judge_is_unverified(self) -> None:
        # Not a pass and not a fail. Reporting "nobody looked" honestly is what
        # makes the tool adoptable before a judge exists.
        case = make_case(output=make_output("original wording here"))
        result = classifier().classify(case, make_output("different wording here"))
        assert result.verdict is Verdict.UNVERIFIED

    def test_blocking_check_failure_is_broken(self) -> None:
        case = make_case(output=json_output({"a": 1}))
        assert classifier().classify(case, make_output("not json")).verdict is Verdict.BROKEN

    def test_blocking_failure_short_circuits_the_judge(self) -> None:
        case = make_case(output=json_output({"a": 1}))
        judge = ScriptedJudge({case.case_id: JudgeVerdict(verdict="equivalent")})
        # Even a judge saying "equivalent" cannot rescue output that stopped parsing.
        assert classifier(judge).classify(case, make_output("nope")).verdict is Verdict.BROKEN

    def test_judge_can_accept_a_difference(self) -> None:
        case = make_case(output=make_output("The order shipped."))
        judge = ScriptedJudge(
            {case.case_id: JudgeVerdict(verdict="acceptable", rationale="same meaning")}
        )
        result = classifier(judge).classify(case, make_output("Your order has shipped."))
        assert result.verdict is Verdict.ACCEPTABLE
        assert result.judge_rationale == "same meaning"

    def test_judge_can_reject_a_difference(self) -> None:
        case = make_case(output=make_output("The order shipped."))
        judge = ScriptedJudge({case.case_id: JudgeVerdict(verdict="broken")})
        assert classifier(judge).classify(case, make_output("No idea.")).verdict is Verdict.BROKEN

    def test_abstaining_judge_yields_unverified(self) -> None:
        case = make_case(output=make_output("original"))
        judge = ScriptedJudge({})  # knows nothing about this case
        result = classifier(judge).classify(case, make_output("changed"))
        assert result.verdict is Verdict.UNVERIFIED
        assert result.judge_rationale == "judge abstained"

    def test_failing_judge_yields_unverified_not_error(self) -> None:
        class BrokenJudge:
            name = "broken"

            def compare(self, case: Case, candidate: InteractionOutput) -> JudgeVerdict | None:
                raise JudgeError("model unreachable")

            def close(self) -> None:
                return

        case = make_case(output=make_output("original"))
        result = classifier(BrokenJudge()).classify(case, make_output("changed"))
        assert result.verdict is Verdict.UNVERIFIED
        assert "judge unavailable" in (result.judge_rationale or "")


class TestSeverityPolicy:
    def test_warning_alone_does_not_break(self) -> None:
        case = make_case(output=make_output("a" * 100))
        result = classifier(max_length_delta_ratio=0.1).classify(case, make_output("a" * 500))
        assert result.verdict is Verdict.UNVERIFIED
        failures = [c for c in result.checks if c.status.value == "fail"]
        assert any(c.severity is Severity.WARNING for c in failures)

    def test_warnings_can_be_promoted(self) -> None:
        case = make_case(output=make_output("a" * 100))
        result = classifier(max_length_delta_ratio=0.1, treat_warnings_as_broken=True).classify(
            case, make_output("a" * 500)
        )
        assert result.verdict is Verdict.BROKEN


class TestCheckIsolation:
    def test_a_raising_check_does_not_abort_classification(self) -> None:
        class ExplodingCheck:
            name = "exploding"

            def run(self, ctx: object) -> object:
                raise RuntimeError("boom")

        settings = CheckSettings()
        engine = Classifier(pipeline=[ExplodingCheck()], settings=settings)  # type: ignore[list-item]
        result = engine.classify(make_case(output=make_output("x")), make_output("x"))
        # Downgraded to a warning so one broken check cannot fail every case.
        assert result.verdict is Verdict.EQUIVALENT
        assert "RuntimeError" in result.checks[0].message

    def test_check_error_is_reported_as_warning(self) -> None:
        case = make_case(
            output=make_output("x"), expectations=Expectations(format_regex="[unclosed")
        )
        result = classifier().classify(case, make_output("x"))
        failed = [c for c in result.checks if c.check == "format_regex"]
        assert failed and failed[0].severity is Severity.WARNING


class TestOutputsEquivalent:
    settings = CheckSettings()

    def test_tool_call_ids_are_ignored(self) -> None:
        # Call ids are random per request; comparing them would make every
        # tool-using case differ.
        baseline = make_output(
            "", tool_calls=(ToolCall(name="t", arguments={"a": 1}, call_id="1"),)
        )
        candidate = make_output(
            "", tool_calls=(ToolCall(name="t", arguments={"a": 1}, call_id="2"),)
        )
        assert outputs_equivalent(baseline, candidate, self.settings)

    def test_tool_call_arguments_matter(self) -> None:
        baseline = make_output("", tool_calls=(ToolCall(name="t", arguments={"a": 1}),))
        candidate = make_output("", tool_calls=(ToolCall(name="t", arguments={"a": 2}),))
        assert not outputs_equivalent(baseline, candidate, self.settings)

    def test_tool_call_order_is_ignored(self) -> None:
        baseline = make_output("", tool_calls=(ToolCall(name="a"), ToolCall(name="b")))
        candidate = make_output("", tool_calls=(ToolCall(name="b"), ToolCall(name="a")))
        assert outputs_equivalent(baseline, candidate, self.settings)


def test_classify_to_outcome_carries_metadata() -> None:
    case = make_case(output=make_output("x"), tags=("t1",))
    outcome = classifier().classify_to_outcome(case, make_output("x"))
    assert outcome.case_id == case.case_id
    assert outcome.tags == ("t1",)
    assert outcome.candidate is not None
    assert outcome.duration_ms >= 0


def test_judge_name_reported() -> None:
    assert classifier().judge_name == "none"
    assert classifier(ScriptedJudge({})).judge_name == "scripted"
