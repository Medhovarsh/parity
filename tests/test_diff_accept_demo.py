"""Diff engine, the accept loop, the demo, and the HTML report."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from parity.accept import SAFE_VERDICTS, apply_acceptance, plan_acceptance
from parity.adapters.stores.jsonl_store import JsonlBaselineStore
from parity.cli.main import app
from parity.demo import DEMO_CASES, build_demo
from parity.diff import ChangeKind, diff_json, diff_outputs, diff_tool_calls
from parity.domain.models import (
    CaseOutcome,
    CheckResult,
    ModelRef,
    RunReport,
    ToolCall,
    Verdict,
)
from parity.report.html import render_html
from tests.conftest import json_output, make_case, make_output

runner = CliRunner()


# ---------------------------------------------------------------------------
# diff engine
# ---------------------------------------------------------------------------


class TestJsonDiff:
    def test_detects_removal(self) -> None:
        changes = diff_json({"a": 1, "b": 2}, {"a": 1})
        assert len(changes) == 1
        assert changes[0].kind is ChangeKind.REMOVED
        assert changes[0].path == "b"

    def test_detects_addition(self) -> None:
        changes = diff_json({"a": 1}, {"a": 1, "new": 2})
        assert changes[0].kind is ChangeKind.ADDED

    def test_detects_value_change(self) -> None:
        changes = diff_json({"score": 0.9}, {"score": 0.4})
        assert changes[0].kind is ChangeKind.CHANGED
        assert changes[0].before == 0.9
        assert changes[0].after == 0.4

    def test_reports_nested_paths(self) -> None:
        changes = diff_json({"i": {"tax": 1, "net": 2}}, {"i": {"net": 2}})
        assert changes[0].path == "i.tax"

    def test_indexes_list_elements(self) -> None:
        # Unlike the required_fields check, the reader wants to know *which* one.
        changes = diff_json({"xs": [1, 2]}, {"xs": [1, 99]})
        assert changes[0].path == "xs[1]"

    def test_identical_documents_have_no_changes(self) -> None:
        assert diff_json({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}) == []

    def test_render_is_readable(self) -> None:
        removed = diff_json({"tax_total": 240.0}, {})[0]
        assert removed.render().startswith("- tax_total")


class TestToolCallDiff:
    def test_detects_lost_call(self) -> None:
        changes = diff_tool_calls((ToolCall(name="search"),), ())
        assert changes[0].kind is ChangeKind.REMOVED
        assert "no longer called" in changes[0].render()

    def test_detects_new_call(self) -> None:
        changes = diff_tool_calls((), (ToolCall(name="search"),))
        assert changes[0].kind is ChangeKind.ADDED

    def test_detects_argument_change(self) -> None:
        changes = diff_tool_calls(
            (ToolCall(name="s", arguments={"q": "a"}),),
            (ToolCall(name="s", arguments={"q": "b"}),),
        )
        assert changes[0].kind is ChangeKind.CHANGED

    def test_ignores_call_ids(self) -> None:
        before = (ToolCall(name="s", arguments={"q": 1}, call_id="c1"),)
        after = (ToolCall(name="s", arguments={"q": 1}, call_id="c2"),)
        assert diff_tool_calls(before, after) == []


class TestOutputDiff:
    def test_structured_when_both_sides_are_json(self) -> None:
        diff = diff_outputs(json_output({"a": 1, "b": 2}), json_output({"a": 1}))
        assert diff.structured
        assert diff.summary() == "1 field(s) removed"

    def test_falls_back_to_text(self) -> None:
        diff = diff_outputs(make_output("the cat sat"), make_output("the dog sat"))
        assert not diff.structured
        assert "prose rewritten" in diff.summary()
        assert diff.text_unified()

    def test_reports_finish_reason_change(self) -> None:
        diff = diff_outputs(make_output("whole"), make_output("half", finish_reason="length"))
        assert diff.finish_reason_changed
        assert "finish reason" in diff.summary()

    def test_identical_outputs_are_empty(self) -> None:
        diff = diff_outputs(make_output("same"), make_output("same"))
        assert diff.empty
        assert diff.summary() == "no difference"

    def test_long_prose_is_localised_not_wholesale(self) -> None:
        # Without wrapping, a paragraph is one line and any edit marks it all.
        base = " ".join(f"word{i}" for i in range(200))
        changed = base.replace("word150", "CHANGED")
        diff = diff_outputs(make_output(base), make_output(changed))
        removed = [line for line in diff.text_unified() if line.startswith("-")]
        assert len(removed) < 5, "an edit should not mark the whole text as changed"


# ---------------------------------------------------------------------------
# accept
# ---------------------------------------------------------------------------


def build_report(*outcomes: CaseOutcome, candidate: str = "new:model") -> RunReport:
    return RunReport.build(
        parity_version="0.1.0",
        baseline_ref=ModelRef.parse("old:model"),
        candidate_ref=ModelRef.parse(candidate),
        baseline_source="mem",
        judge="none",
        outcomes=outcomes,
    )


@pytest.fixture
def populated_store(tmp_path: Path) -> Any:
    store = JsonlBaselineStore(tmp_path / "b.jsonl")
    store.extend([make_case(user="q1", output=make_output("old one"))])
    return store


class TestAcceptPlanning:
    def case_and_outcome(self, store: Any, verdict: Verdict, text: str = "new one") -> CaseOutcome:
        case = next(iter(store.iter_cases()))
        return CaseOutcome(case_id=case.case_id, verdict=verdict, candidate=make_output(text))

    def test_accepts_unverified_by_default(self, populated_store: Any) -> None:
        outcome = self.case_and_outcome(populated_store, Verdict.UNVERIFIED)
        plan = plan_acceptance(build_report(outcome), populated_store)
        assert plan.count == 1

    def test_accepts_acceptable_by_default(self, populated_store: Any) -> None:
        outcome = self.case_and_outcome(populated_store, Verdict.ACCEPTABLE)
        assert plan_acceptance(build_report(outcome), populated_store).count == 1

    def test_refuses_broken_by_default(self, populated_store: Any) -> None:
        # The dangerous mistake is blessing a regression that shared a run with
        # an intended change.
        outcome = self.case_and_outcome(populated_store, Verdict.BROKEN)
        plan = plan_acceptance(build_report(outcome), populated_store)
        assert plan.empty
        assert "--force" in plan.refused[0][1]

    def test_naming_a_broken_case_authorises_it(self, populated_store: Any) -> None:
        outcome = self.case_and_outcome(populated_store, Verdict.BROKEN)
        plan = plan_acceptance(
            build_report(outcome), populated_store, case_ids=frozenset({outcome.case_id})
        )
        assert plan.count == 1

    def test_force_accepts_broken(self, populated_store: Any) -> None:
        outcome = self.case_and_outcome(populated_store, Verdict.BROKEN)
        assert plan_acceptance(build_report(outcome), populated_store, force=True).count == 1

    def test_never_accepts_an_error(self, populated_store: Any) -> None:
        case = next(iter(populated_store.iter_cases()))
        outcome = CaseOutcome(case_id=case.case_id, verdict=Verdict.ERROR, error="boom")
        plan = plan_acceptance(
            build_report(outcome), populated_store, case_ids=frozenset({case.case_id})
        )
        assert plan.empty
        assert "no output to accept" in plan.refused[0][1]

    def test_equivalent_cases_are_nothing_to_do(self, populated_store: Any) -> None:
        outcome = self.case_and_outcome(populated_store, Verdict.EQUIVALENT)
        plan = plan_acceptance(build_report(outcome), populated_store)
        assert plan.empty
        assert plan.unchanged == [outcome.case_id]

    def test_reports_cases_missing_from_the_baseline(self, populated_store: Any) -> None:
        outcome = CaseOutcome(
            case_id="ghost", verdict=Verdict.UNVERIFIED, candidate=make_output("x")
        )
        plan = plan_acceptance(build_report(outcome), populated_store)
        assert plan.missing == ["ghost"]

    def test_planning_writes_nothing(self, populated_store: Any) -> None:
        outcome = self.case_and_outcome(populated_store, Verdict.UNVERIFIED)
        plan_acceptance(build_report(outcome), populated_store)
        assert next(iter(populated_store.iter_cases())).output.text == "old one"

    def test_safe_verdicts_excludes_regressions(self) -> None:
        assert Verdict.BROKEN not in SAFE_VERDICTS
        assert Verdict.ERROR not in SAFE_VERDICTS


class TestAcceptApplying:
    def test_promotes_candidate_and_bumps_revision(self, populated_store: Any) -> None:
        case = next(iter(populated_store.iter_cases()))
        outcome = CaseOutcome(
            case_id=case.case_id, verdict=Verdict.UNVERIFIED, candidate=make_output("new one")
        )
        report = build_report(outcome)
        plan = plan_acceptance(report, populated_store)
        assert apply_acceptance(plan, populated_store, report) == 1

        updated = next(iter(populated_store.iter_cases()))
        assert updated.output.text == "new one"
        assert updated.revision == 2
        assert str(updated.reference) == "new:model"
        assert str(updated.previous_reference) == "ref:ref-1"
        assert updated.accepted_at is not None

    def test_case_id_is_stable_across_acceptance(self, populated_store: Any) -> None:
        # The id fingerprints the input, and the input did not move.
        case = next(iter(populated_store.iter_cases()))
        outcome = CaseOutcome(
            case_id=case.case_id, verdict=Verdict.UNVERIFIED, candidate=make_output("new")
        )
        report = build_report(outcome)
        apply_acceptance(plan_acceptance(report, populated_store), populated_store, report)
        assert next(iter(populated_store.iter_cases())).case_id == case.case_id

    def test_untouched_cases_survive(self, tmp_path: Path) -> None:
        store = JsonlBaselineStore(tmp_path / "b.jsonl")
        keep = make_case(user="keep", output=make_output("keep me"))
        change = make_case(user="change", output=make_output("old"))
        store.extend([keep, change])
        outcome = CaseOutcome(
            case_id=change.case_id, verdict=Verdict.UNVERIFIED, candidate=make_output("new")
        )
        report = build_report(outcome)
        apply_acceptance(plan_acceptance(report, store), store, report)

        by_id = {c.case_id: c for c in store.iter_cases()}
        assert by_id[keep.case_id].output.text == "keep me"
        assert by_id[keep.case_id].revision == 1
        assert by_id[change.case_id].output.text == "new"

    def test_empty_plan_writes_nothing(self, populated_store: Any) -> None:
        report = build_report()
        plan = plan_acceptance(report, populated_store)
        assert apply_acceptance(plan, populated_store, report) == 0


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------


class TestDemo:
    def test_every_case_has_a_lesson(self) -> None:
        cases, _, lessons = build_demo()
        assert len(cases) == len(DEMO_CASES)
        assert all(lessons[c.case_id] for c in cases)

    def test_provider_is_scripted_for_every_case(self) -> None:
        cases, provider, _ = build_demo()
        for case in cases:
            assert provider.complete("m", case.input) is not None

    def test_demo_covers_the_regression_classes(self) -> None:
        """The demo is documentation; it must actually demonstrate."""
        from parity.checks.registry import build_pipeline
        from parity.classify.classifier import Classifier
        from parity.domain.policy import CheckSettings
        from parity.replay.runner import ReplayRunner

        cases, provider, _ = build_demo()
        settings = CheckSettings()
        runner = ReplayRunner(
            provider=provider,
            model="demo",
            classifier=Classifier(pipeline=build_pipeline(settings), settings=settings),
            concurrency=1,
        )
        outcomes = runner.run(cases)
        verdicts = {o.verdict for o in outcomes}
        assert Verdict.EQUIVALENT in verdicts
        assert Verdict.BROKEN in verdicts
        assert Verdict.UNVERIFIED in verdicts
        assert Verdict.ERROR not in verdicts

        fired = {c.check for o in outcomes for c in o.checks if c.blocking_failure}
        for expected in ("required_fields", "tool_calls", "refusal", "truncation", "json_parse"):
            assert expected in fired, f"demo no longer demonstrates {expected}"


# ---------------------------------------------------------------------------
# html report
# ---------------------------------------------------------------------------


class TestHtmlReport:
    def report(self) -> RunReport:
        return build_report(
            CaseOutcome(case_id="ok", verdict=Verdict.EQUIVALENT),
            CaseOutcome(
                case_id="bad",
                verdict=Verdict.BROKEN,
                candidate=make_output("changed"),
                checks=(CheckResult.failed("required_fields", message="dropped total"),),
            ),
        )

    def test_is_self_contained(self) -> None:
        html = render_html(self.report())
        assert "<style>" in html
        assert "https://" not in html.split("<style>")[1]
        assert "<script" not in html

    def test_supports_both_colour_schemes(self) -> None:
        assert "prefers-color-scheme: dark" in render_html(self.report())

    def test_escapes_untrusted_model_output(self) -> None:
        # Model output ends up in a file people open in a browser.
        report = build_report(
            CaseOutcome(
                case_id="x",
                verdict=Verdict.BROKEN,
                candidate=make_output("<script>alert(1)</script>"),
                checks=(CheckResult.failed("c", message="<img onerror=alert(1)>"),),
            )
        )
        html = render_html(report, baselines={"x": "<b>hi</b>"})
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
        assert "<img onerror" not in html

    def test_includes_diffs_when_supplied(self) -> None:
        diff = diff_outputs(json_output({"a": 1, "b": 2}), json_output({"a": 1}))
        html = render_html(self.report(), diffs={"bad": diff})
        assert "b" in html
        assert 'class="d"' in html

    def test_says_so_when_nothing_needs_review(self) -> None:
        html = render_html(build_report(CaseOutcome(case_id="ok", verdict=Verdict.EQUIVALENT)))
        assert "No cases need review" in html


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestNewCommands:
    def test_demo_runs_with_no_config_and_no_credentials(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["--no-color", "demo"])
        assert result.exit_code == 0, result.output
        assert "parity replay" in result.output
        assert "what each case demonstrates" in result.output

    def test_demo_writes_html(self, tmp_path: Path) -> None:
        target = tmp_path / "out" / "demo.html"
        result = runner.invoke(app, ["--no-color", "demo", "--html", str(target)])
        assert result.exit_code == 0
        assert target.is_file()
        assert "<style>" in target.read_text(encoding="utf-8")

    def test_diff_requires_an_existing_run(self, tmp_path: Path) -> None:
        runner.invoke(app, ["init", str(tmp_path)])
        result = runner.invoke(app, ["--config", str(tmp_path / "parity.toml"), "diff", "abc"])
        assert result.exit_code != 0

    def test_accept_with_no_run(self, tmp_path: Path) -> None:
        runner.invoke(app, ["init", str(tmp_path)])
        result = runner.invoke(app, ["--config", str(tmp_path / "parity.toml"), "accept"])
        assert result.exit_code != 0


class TestEndToEndAcceptLoop:
    def test_accepting_makes_the_next_run_equivalent(
        self, tmp_path: Path, sample_log: Path
    ) -> None:
        """The whole point of the feature, proven end to end."""
        config = tmp_path / "parity.toml"
        assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
        assert (
            runner.invoke(app, ["--config", str(config), "capture", str(sample_log)]).exit_code == 0
        )

        base = ["--config", str(config), "--no-color"]
        first = runner.invoke(app, [*base, "replay", "--candidate", "fake:f1"])
        assert first.exit_code == 0
        assert "unverified" in first.output

        accepted = runner.invoke(app, [*base, "accept", "--yes"])
        assert accepted.exit_code == 0
        assert "accepted" in accepted.output

        second = runner.invoke(app, [*base, "replay", "--candidate", "fake:f1"])
        # The case that was reported as changed is now the expected behaviour.
        assert "accepted" not in second.output
        assert second.exit_code == 0
