"""Replay execution, retry behaviour, and gate policy.

The runner's contract is that one bad case cannot take down a run, and that
concurrency never reorders results. Both are easy to break and expensive to
discover in production, so they are pinned here.
"""

from __future__ import annotations

import pytest

from parity.adapters.clock import FakeClock
from parity.adapters.judges.none_judge import ScriptedJudge
from parity.adapters.providers.fake import FakeProvider, Mutation, scripted_json_output
from parity.checks.registry import build_pipeline
from parity.classify.classifier import Classifier
from parity.domain.models import (
    CaseOutcome,
    InteractionInput,
    InteractionOutput,
    JudgeVerdict,
    ModelRef,
    RunReport,
    Verdict,
)
from parity.domain.policy import CheckSettings, GatePolicy
from parity.errors import ProviderError
from parity.gate import evaluate_gate, worst_verdict
from parity.replay.runner import ReplayRunner, RunProgress
from tests.conftest import make_case, make_output


def build_classifier(judge: object = None) -> Classifier:
    settings = CheckSettings()
    return Classifier(
        pipeline=build_pipeline(settings),
        settings=settings,
        judge=judge,  # type: ignore[arg-type]
    )


def build_runner(provider: object, **kwargs: object) -> ReplayRunner:
    defaults: dict[str, object] = {
        "provider": provider,
        "model": "m1",
        "classifier": build_classifier(),
        "clock": FakeClock(),
        "concurrency": 1,
    }
    defaults.update(kwargs)
    return ReplayRunner(**defaults)  # type: ignore[arg-type]


class TestReplayCase:
    def test_identical_replay_is_equivalent(self) -> None:
        case = make_case(user="hello")
        provider = FakeProvider(scripted={case.input.fingerprint(): case.output})
        outcome = build_runner(provider).replay_case(case)
        assert outcome.verdict is Verdict.EQUIVALENT

    def test_structural_regression_is_broken(self) -> None:
        baseline = scripted_json_output({"a": 1, "b": 2})
        case = make_case(output=baseline)
        provider = FakeProvider(
            scripted={case.input.fingerprint(): baseline}, mutation=Mutation.DROP_FIELD
        )
        outcome = build_runner(provider).replay_case(case)
        assert outcome.verdict is Verdict.BROKEN
        assert any(c.check == "required_fields" for c in outcome.failed_checks)

    def test_refusal_is_broken(self) -> None:
        case = make_case(output=make_output("Here is the answer."))
        provider = FakeProvider(
            scripted={case.input.fingerprint(): case.output}, mutation=Mutation.REFUSE
        )
        assert build_runner(provider).replay_case(case).verdict is Verdict.BROKEN

    def test_permanent_provider_failure_is_error_not_a_crash(self) -> None:
        provider = FakeProvider(mutation=Mutation.RAISE)
        outcome = build_runner(provider).replay_case(make_case())
        assert outcome.verdict is Verdict.ERROR
        assert outcome.error is not None
        assert "synthetic permanent failure" in outcome.error

    def test_unexpected_adapter_exception_is_contained(self) -> None:
        class ExplodingProvider:
            name = "boom"

            def complete(self, model: str, request: InteractionInput) -> InteractionOutput:
                raise ValueError("adapter defect")

            def close(self) -> None:
                return

        outcome = build_runner(ExplodingProvider()).replay_case(make_case())
        assert outcome.verdict is Verdict.ERROR
        assert "ValueError" in (outcome.error or "")

    def test_records_duration_and_tags(self) -> None:
        case = make_case(tags=("t",))
        provider = FakeProvider(scripted={case.input.fingerprint(): case.output})
        outcome = build_runner(provider).replay_case(case)
        assert outcome.tags == ("t",)
        assert outcome.duration_ms >= 0


class TestRetry:
    def test_retries_transient_failures(self) -> None:
        case = make_case()
        provider = FakeProvider(scripted={case.input.fingerprint(): case.output}, fail_times=2)
        clock = FakeClock()
        outcome = build_runner(provider, clock=clock, max_retries=3).replay_case(case)
        assert outcome.verdict is Verdict.EQUIVALENT
        assert len(clock.slept) == 2

    def test_gives_up_after_the_budget(self) -> None:
        provider = FakeProvider(fail_times=10)
        clock = FakeClock()
        outcome = build_runner(provider, clock=clock, max_retries=2).replay_case(make_case())
        assert outcome.verdict is Verdict.ERROR
        assert len(clock.slept) == 2

    def test_does_not_retry_permanent_failures(self) -> None:
        class PermanentProvider:
            name = "perm"

            def complete(self, model: str, request: InteractionInput) -> InteractionOutput:
                raise ProviderError("bad request", provider="perm", retryable=False)

            def close(self) -> None:
                return

        clock = FakeClock()
        outcome = build_runner(PermanentProvider(), clock=clock, max_retries=5).replay_case(
            make_case()
        )
        assert outcome.verdict is Verdict.ERROR
        assert clock.slept == []

    def test_backoff_grows_and_is_capped(self) -> None:
        provider = FakeProvider(fail_times=10)
        clock = FakeClock()
        build_runner(
            provider,
            clock=clock,
            max_retries=6,
            base_delay_seconds=1.0,
            max_delay_seconds=4.0,
        ).replay_case(make_case())
        assert all(delay <= 4.0 for delay in clock.slept)
        assert len(clock.slept) == 6


class TestRun:
    def test_empty_input(self) -> None:
        assert build_runner(FakeProvider()).run([]) == ()

    def test_preserves_order_when_concurrent(self) -> None:
        cases = [make_case(user=f"q{i}") for i in range(25)]
        scripted = {c.input.fingerprint(): c.output for c in cases}
        provider = FakeProvider(scripted=scripted)
        outcomes = build_runner(provider, concurrency=8).run(cases)
        assert [o.case_id for o in outcomes] == [c.case_id for c in cases]

    def test_concurrent_and_serial_agree(self) -> None:
        cases = [make_case(user=f"q{i}") for i in range(10)]
        scripted = {c.input.fingerprint(): c.output for c in cases}
        serial = build_runner(FakeProvider(scripted=scripted), concurrency=1).run(cases)
        concurrent = build_runner(FakeProvider(scripted=scripted), concurrency=4).run(cases)
        assert [o.verdict for o in serial] == [o.verdict for o in concurrent]

    def test_progress_callback_reports_every_case(self) -> None:
        cases = [make_case(user=f"q{i}") for i in range(5)]
        seen: list[RunProgress] = []
        build_runner(FakeProvider(), concurrency=2).run(cases, on_progress=seen.append)
        assert len(seen) == 5
        assert {p.total for p in seen} == {5}
        assert max(p.completed for p in seen) == 5

    def test_one_failure_does_not_stop_the_run(self) -> None:
        cases = [make_case(user=f"q{i}") for i in range(4)]

        class FlakyProvider:
            name = "flaky"

            def __init__(self) -> None:
                self.seen = 0

            def complete(self, model: str, request: InteractionInput) -> InteractionOutput:
                self.seen += 1
                if self.seen == 2:
                    raise ProviderError("nope", provider="flaky", retryable=False)
                return make_output("ok")

            def close(self) -> None:
                return

        outcomes = build_runner(FlakyProvider(), concurrency=1).run(cases)
        assert len(outcomes) == 4
        assert sum(1 for o in outcomes if o.verdict is Verdict.ERROR) == 1

    def test_build_report(self) -> None:
        cases = [make_case(user="a")]
        provider = FakeProvider(scripted={cases[0].input.fingerprint(): cases[0].output})
        runner = build_runner(provider)
        outcomes = runner.run(cases)
        report = runner.build_report(
            parity_version="0.1.0",
            baseline_ref=ModelRef.parse("ref:ref-1"),
            baseline_source="mem",
            outcomes=outcomes,
        )
        assert report.summary.total == 1
        assert str(report.candidate_ref) == "fake:m1"
        assert report.judge == "none"

    def test_judge_is_used_when_present(self) -> None:
        case = make_case(output=make_output("original text"))
        provider = FakeProvider(
            scripted={case.input.fingerprint(): case.output}, mutation=Mutation.REWORD
        )
        judge = ScriptedJudge({case.case_id: JudgeVerdict(verdict="acceptable")})
        runner = build_runner(provider, classifier=build_classifier(judge))
        assert runner.replay_case(case).verdict is Verdict.ACCEPTABLE


def report_with(*verdicts: Verdict) -> RunReport:
    return RunReport.build(
        parity_version="0.1.0",
        baseline_ref=ModelRef.parse("a:b"),
        candidate_ref=ModelRef.parse("c:d"),
        baseline_source="mem",
        judge="none",
        outcomes=tuple(CaseOutcome(case_id=f"c{i}", verdict=v) for i, v in enumerate(verdicts)),
    )


class TestGate:
    def test_passes_on_a_clean_run(self) -> None:
        decision = evaluate_gate(report_with(Verdict.EQUIVALENT, Verdict.ACCEPTABLE), GatePolicy())
        assert decision.passed
        assert "no blocking findings" in decision.summary()

    def test_fails_on_broken(self) -> None:
        decision = evaluate_gate(report_with(Verdict.EQUIVALENT, Verdict.BROKEN), GatePolicy())
        assert not decision.passed
        assert decision.counted_failures == 1

    def test_fails_on_error(self) -> None:
        assert not evaluate_gate(report_with(Verdict.ERROR), GatePolicy()).passed

    def test_unverified_passes_by_default(self) -> None:
        # The default has to be adoptable before a judge exists.
        assert evaluate_gate(report_with(Verdict.UNVERIFIED), GatePolicy()).passed

    def test_unverified_can_be_made_fatal(self) -> None:
        policy = GatePolicy(fail_on_unverified=True)
        assert not evaluate_gate(report_with(Verdict.UNVERIFIED), policy).passed

    def test_failure_budget(self) -> None:
        report = report_with(Verdict.BROKEN, Verdict.BROKEN, Verdict.EQUIVALENT)
        assert evaluate_gate(report, GatePolicy(max_failures=2)).passed
        assert not evaluate_gate(report, GatePolicy(max_failures=1)).passed

    def test_empty_run_fails_the_minimum_case_guard(self) -> None:
        # A truncated or empty baseline must not silently pass.
        decision = evaluate_gate(report_with(), GatePolicy(min_cases=1))
        assert not decision.passed
        assert "below the required minimum" in decision.summary()

    def test_minimum_can_be_waived(self) -> None:
        assert evaluate_gate(report_with(), GatePolicy(min_cases=0)).passed

    def test_reason_names_the_verdicts(self) -> None:
        decision = evaluate_gate(report_with(Verdict.BROKEN, Verdict.ERROR), GatePolicy())
        assert "1 broken" in decision.summary()
        assert "1 error" in decision.summary()


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ((Verdict.EQUIVALENT,), Verdict.EQUIVALENT),
        ((Verdict.EQUIVALENT, Verdict.UNVERIFIED), Verdict.UNVERIFIED),
        ((Verdict.UNVERIFIED, Verdict.BROKEN), Verdict.BROKEN),
        ((Verdict.BROKEN, Verdict.ERROR), Verdict.ERROR),
        ((), Verdict.EQUIVALENT),
    ],
)
def test_worst_verdict(verdicts: tuple[Verdict, ...], expected: Verdict) -> None:
    assert worst_verdict(report_with(*verdicts)) is expected
