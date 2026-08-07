"""Configuration, reporters, the judge adapter, and the CLI end to end."""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree

import pytest
from typer.testing import CliRunner, Result

from parity.adapters.judges.llm_judge import LLMJudge
from parity.adapters.judges.none_judge import NoJudge
from parity.adapters.providers.fake import FakeProvider
from parity.app import Application
from parity.cli.exit_codes import ExitCode
from parity.cli.main import app
from parity.config import ParityConfig, find_config_file, load_config
from parity.domain.models import (
    CaseOutcome,
    CheckResult,
    InteractionInput,
    InteractionOutput,
    ModelRef,
    RunReport,
    Verdict,
)
from parity.domain.policy import GatePolicy
from parity.errors import ConfigError, JudgeError, ProviderError
from parity.gate import evaluate_gate
from parity.report import render_junit, render_markdown
from tests.conftest import make_case, make_output

runner = CliRunner()


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults_need_no_file(self, tmp_path: Path) -> None:
        config = load_config(start=tmp_path)
        assert config.baseline.store == "jsonl"
        assert config.security.redact is True
        assert config.judge.enabled is False
        # Usable with no credentials at all.
        assert "fake" in config.providers
        assert "ollama" in config.providers

    def test_loads_a_file(self, tmp_path: Path) -> None:
        (tmp_path / "parity.toml").write_text(
            '[baseline]\nstore = "sqlite"\npath = "b.db"\n\n[gate]\nmax_failures = 3\n',
            encoding="utf-8",
        )
        config = load_config(start=tmp_path)
        assert config.baseline.store == "sqlite"
        assert config.gate.max_failures == 3
        assert config.baseline_path() == tmp_path / "b.db"

    def test_discovers_upward(self, tmp_path: Path) -> None:
        (tmp_path / "parity.toml").write_text("[gate]\nmax_failures = 7\n", encoding="utf-8")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert load_config(start=nested).gate.max_failures == 7
        assert find_config_file(nested) == tmp_path / "parity.toml"

    def test_custom_providers_merge_with_defaults(self, tmp_path: Path) -> None:
        (tmp_path / "parity.toml").write_text(
            '[providers.local]\nkind = "openai"\nbase_url = "http://localhost:8000/v1"\n'
            "require_key = false\n",
            encoding="utf-8",
        )
        config = load_config(start=tmp_path)
        assert config.provider("local").base_url == "http://localhost:8000/v1"
        assert config.provider("ollama").kind == "ollama"

    def test_invalid_toml(self, tmp_path: Path) -> None:
        (tmp_path / "parity.toml").write_text("[unclosed\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="invalid TOML"):
            load_config(start=tmp_path)

    def test_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        # A typo that silently does nothing is worse than a loud failure.
        (tmp_path / "parity.toml").write_text("[gate]\nmax_falures = 3\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(start=tmp_path)

    def test_root_cannot_be_set_in_the_file(self, tmp_path: Path) -> None:
        (tmp_path / "parity.toml").write_text('root = "/elsewhere"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="cannot be set in the file"):
            load_config(start=tmp_path)

    def test_unknown_provider_lookup(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="no provider named"):
            load_config(start=tmp_path).provider("nope")

    def test_absolute_paths_are_respected(self, tmp_path: Path) -> None:
        absolute = (tmp_path / "elsewhere.jsonl").resolve()
        (tmp_path / "parity.toml").write_text(
            f'[baseline]\npath = "{absolute.as_posix()}"\n', encoding="utf-8"
        )
        assert load_config(start=tmp_path).baseline_path() == absolute

    def test_snapshot_omits_provider_endpoints(self, tmp_path: Path) -> None:
        # Run reports get shared; base URLs can carry internal hostnames.
        snapshot = load_config(start=tmp_path).snapshot()
        assert snapshot["providers"] == sorted(["fake", "ollama", "openai", "anthropic"])
        assert "root" not in snapshot


class TestApplication:
    def test_judge_defaults_to_none(self, tmp_path: Path) -> None:
        application = Application(ParityConfig(root=tmp_path))
        judge = application.judge()
        assert isinstance(judge, NoJudge)
        # NoJudge is collapsed to None so the classifier skips the branch entirely.
        assert application.classifier(judge)._judge is None

    def test_redactor_can_be_disabled(self, tmp_path: Path) -> None:
        from parity.config import SecurityConfig

        config = ParityConfig(root=tmp_path, security=SecurityConfig(redact=False))
        assert Application(config).redactor() is None

    def test_replay_session_closes_resources(self, tmp_path: Path) -> None:
        application = Application(ParityConfig(root=tmp_path))
        with application.replay_session(ModelRef.parse("fake:m")) as replay_runner:
            assert replay_runner.candidate_ref.provider == "fake"


# ---------------------------------------------------------------------------
# judge adapter
# ---------------------------------------------------------------------------


class TestLLMJudge:
    def judge_for(self, response_text: str) -> LLMJudge:
        class StubProvider:
            name = "stub"

            def complete(self, model: str, request: InteractionInput) -> InteractionOutput:
                return InteractionOutput(text=response_text)

            def close(self) -> None:
                return

        return LLMJudge(StubProvider(), "judge-model")

    def test_parses_a_verdict(self) -> None:
        verdict = self.judge_for(
            '{"verdict": "acceptable", "confidence": 0.9, "rationale": "same meaning"}'
        ).compare(make_case(), make_output("x"))
        assert verdict is not None
        assert verdict.verdict == "acceptable"
        assert verdict.rationale == "same meaning"

    def test_unwraps_a_fenced_reply(self) -> None:
        verdict = self.judge_for('```json\n{"verdict": "equivalent"}\n```').compare(
            make_case(), make_output("x")
        )
        assert verdict is not None and verdict.verdict == "equivalent"

    @pytest.mark.parametrize(
        "reply",
        [
            "I think they are the same, honestly.",
            '{"verdict": "maybe"}',
            "{}",
            "",
            "```\nnot json\n```",
        ],
    )
    def test_abstains_on_unusable_replies(self, reply: str) -> None:
        # A judge that guesses is worse than no judge.
        assert self.judge_for(reply).compare(make_case(), make_output("x")) is None

    def test_abstains_below_the_confidence_floor(self) -> None:
        assert (
            self.judge_for('{"verdict": "broken", "confidence": 0.1}').compare(
                make_case(), make_output("x")
            )
            is None
        )

    def test_clamps_out_of_range_confidence(self) -> None:
        verdict = self.judge_for('{"verdict": "equivalent", "confidence": 5}').compare(
            make_case(), make_output("x")
        )
        assert verdict is not None and verdict.confidence == 1.0

    def test_provider_failure_becomes_judge_error(self) -> None:
        class FailingProvider:
            name = "failing"

            def complete(self, model: str, request: InteractionInput) -> InteractionOutput:
                raise ProviderError("down", provider="failing", retryable=True)

            def close(self) -> None:
                return

        with pytest.raises(JudgeError, match="judge provider failed"):
            LLMJudge(FailingProvider(), "m").compare(make_case(), make_output("x"))

    def test_name_identifies_the_model(self) -> None:
        assert LLMJudge(FakeProvider(), "llama3.1").name == "llm:fake:llama3.1"


# ---------------------------------------------------------------------------
# reporters
# ---------------------------------------------------------------------------


def sample_report() -> RunReport:
    return RunReport.build(
        parity_version="0.1.0",
        baseline_ref=ModelRef.parse("openai:old"),
        candidate_ref=ModelRef.parse("openai:new"),
        baseline_source="mem",
        judge="none",
        outcomes=(
            CaseOutcome(case_id="ok1", verdict=Verdict.EQUIVALENT),
            CaseOutcome(
                case_id="bad1",
                verdict=Verdict.BROKEN,
                checks=(CheckResult.failed("required_fields", message="dropped field: total"),),
            ),
            CaseOutcome(case_id="err1", verdict=Verdict.ERROR, error="provider timed out"),
            CaseOutcome(case_id="unk1", verdict=Verdict.UNVERIFIED),
        ),
    )


class TestMarkdownReport:
    def test_includes_counts_and_details(self) -> None:
        text = render_markdown(sample_report())
        assert "openai:old" in text and "openai:new" in text
        assert "| ❌ broken | 1 |" in text
        assert "dropped field: total" in text
        assert "<details>" in text

    def test_includes_the_gate_decision(self) -> None:
        report = sample_report()
        text = render_markdown(report, evaluate_gate(report, GatePolicy()))
        assert "Parity: failed" in text

    def test_escapes_pipes_so_the_table_survives(self) -> None:
        report = RunReport.build(
            parity_version="0.1.0",
            baseline_ref=ModelRef.parse("a:b"),
            candidate_ref=ModelRef.parse("c:d"),
            baseline_source="mem",
            judge="none",
            outcomes=(
                CaseOutcome(
                    case_id="x",
                    verdict=Verdict.BROKEN,
                    checks=(CheckResult.failed("c", message="a | b\nnewline"),),
                ),
            ),
        )
        import re

        row = next(line for line in render_markdown(report).splitlines() if "`x`" in line)
        assert "\\|" in row
        # The escaped pipe must not open a new cell: splitting on unescaped
        # pipes still yields a 4-column row.
        cells = [c for c in re.split(r"(?<!\\)\|", row) if c.strip()]
        assert len(cells) == 4
        assert "newline" in row and "\n" not in row


class TestJunitReport:
    def test_is_well_formed_xml(self) -> None:
        root = ElementTree.fromstring(render_junit(sample_report()))
        assert root.tag == "testsuites"
        assert root.attrib["tests"] == "4"
        assert root.attrib["failures"] == "1"
        assert root.attrib["errors"] == "1"
        assert root.attrib["skipped"] == "1"

    def test_maps_verdicts_to_junit_elements(self) -> None:
        root = ElementTree.fromstring(render_junit(sample_report()))
        cases = {c.attrib["name"]: c for c in root.iter("testcase")}
        assert cases["bad1"].find("failure") is not None
        assert cases["err1"].find("error") is not None
        assert cases["unk1"].find("skipped") is not None
        assert list(cases["ok1"]) == []

    def test_strips_control_characters(self) -> None:
        # XML 1.0 cannot represent these at all, and model output contains them.
        report = RunReport.build(
            parity_version="0.1.0",
            baseline_ref=ModelRef.parse("a:b"),
            candidate_ref=ModelRef.parse("c:d"),
            baseline_source="mem",
            judge="none",
            outcomes=(
                CaseOutcome(
                    case_id="x",
                    verdict=Verdict.BROKEN,
                    checks=(CheckResult.failed("c", message="bad \x00\x07 char"),),
                ),
            ),
        )
        rendered = render_junit(report)
        assert "\x00" not in rendered
        ElementTree.fromstring(rendered)

    def test_preserves_baseline_order(self) -> None:
        root = ElementTree.fromstring(render_junit(sample_report()))
        assert [c.attrib["name"] for c in root.iter("testcase")] == [
            "ok1",
            "bad1",
            "err1",
            "unk1",
        ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path, sample_log: Path) -> Path:
    """A project directory with a config and a captured baseline."""
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    result = runner.invoke(
        app, ["--config", str(tmp_path / "parity.toml"), "capture", str(sample_log)]
    )
    assert result.exit_code == 0, result.output
    return tmp_path


def run_in(project: Path, *args: str) -> Result:
    return runner.invoke(app, ["--config", str(project / "parity.toml"), *args])


class TestCli:
    def test_version(self) -> None:
        # Assert against the package version rather than a literal, so a release
        # bump does not fail a test that is really about the command working.
        from parity import __version__

        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_checks_lists_every_check(self) -> None:
        result = runner.invoke(app, ["checks"])
        assert result.exit_code == 0
        assert "required_fields" in result.output
        assert "tool_calls" in result.output

    def test_init_writes_config(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["init", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / "parity.toml").is_file()

    def test_init_refuses_to_clobber(self, tmp_path: Path) -> None:
        runner.invoke(app, ["init", str(tmp_path)])
        result = runner.invoke(app, ["init", str(tmp_path)])
        assert result.exit_code == ExitCode.CONFIG_ERROR

    def test_init_force_overwrites(self, tmp_path: Path) -> None:
        runner.invoke(app, ["init", str(tmp_path)])
        assert runner.invoke(app, ["init", str(tmp_path), "--force"]).exit_code == 0

    def test_capture_builds_a_baseline(self, project: Path) -> None:
        result = run_in(project, "baseline", "stats")
        assert result.exit_code == 0
        assert "2 case(s)" in result.output

    def test_capture_is_idempotent(self, project: Path, sample_log: Path) -> None:
        result = run_in(project, "capture", str(sample_log))
        assert result.exit_code == 0
        assert "0 new case(s)" in result.output

    def test_capture_dry_run_writes_nothing(self, tmp_path: Path, sample_log: Path) -> None:
        runner.invoke(app, ["init", str(tmp_path)])
        result = runner.invoke(
            app,
            ["--config", str(tmp_path / "parity.toml"), "capture", str(sample_log), "--dry-run"],
        )
        assert result.exit_code == 0
        assert not (tmp_path / ".parity" / "baseline.jsonl").exists()

    def test_capture_missing_file(self, project: Path) -> None:
        result = run_in(project, "capture", str(project / "nope.jsonl"))
        assert result.exit_code == ExitCode.CONFIG_ERROR

    def test_baseline_list_and_show(self, project: Path) -> None:
        listed = run_in(project, "baseline", "list")
        assert listed.exit_code == 0
        case_id = json.loads(
            (project / ".parity" / "baseline.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )["case_id"]
        shown = run_in(project, "baseline", "show", case_id[:10])
        assert shown.exit_code == 0
        assert case_id in shown.output

    def test_baseline_show_unknown(self, project: Path) -> None:
        assert run_in(project, "baseline", "show", "zzzz").exit_code == ExitCode.STORE_ERROR

    def test_baseline_redact(self, project: Path) -> None:
        result = run_in(project, "baseline", "redact", "--yes")
        assert result.exit_code == 0
        assert "rewrote 2 case(s)" in result.output

    def test_replay_against_fake(self, project: Path) -> None:
        result = run_in(project, "replay", "--candidate", "fake:m1")
        assert result.exit_code == 0
        assert "parity replay" in result.output

    def test_gate_fails_on_regressions(self, project: Path) -> None:
        # The fake provider echoes, so the JSON case necessarily regresses.
        result = run_in(project, "gate", "--candidate", "fake:m1")
        assert result.exit_code == ExitCode.GATE_FAILED

    def test_gate_json_output_is_parseable(self, project: Path) -> None:
        # The contract: with a machine-readable format and no --out, stdout
        # holds the document and nothing else, so `parity gate -f json | jq`
        # works. Status chatter belongs on stderr.
        result = run_in(project, "gate", "--candidate", "fake:m1", "--format", "json")
        payload = json.loads(result.stdout)
        assert payload["summary"]["total"] == 2
        assert "run saved to" in result.stderr

    def test_markdown_output_owns_stdout(self, project: Path) -> None:
        result = run_in(project, "gate", "--candidate", "fake:m1", "--format", "markdown")
        assert result.stdout.lstrip().startswith(("✅", "❌"))

    def test_gate_writes_junit_to_a_file(self, project: Path) -> None:
        target = project / "out" / "parity.xml"
        result = run_in(
            project, "gate", "--candidate", "fake:m1", "--format", "junit", "--out", str(target)
        )
        assert result.exit_code == ExitCode.GATE_FAILED
        ElementTree.fromstring(target.read_text(encoding="utf-8"))

    def test_gate_rejects_unknown_format(self, project: Path) -> None:
        result = run_in(project, "gate", "--candidate", "fake:m1", "--format", "yaml")
        assert result.exit_code == ExitCode.CONFIG_ERROR

    def test_replay_rejects_a_malformed_reference(self, project: Path) -> None:
        result = run_in(project, "replay", "--candidate", "no-colon")
        assert result.exit_code != 0

    def test_replay_on_an_empty_baseline(self, tmp_path: Path) -> None:
        runner.invoke(app, ["init", str(tmp_path)])
        result = runner.invoke(
            app,
            ["--config", str(tmp_path / "parity.toml"), "replay", "--candidate", "fake:m1"],
        )
        assert result.exit_code == ExitCode.STORE_ERROR

    def test_replay_limit_and_tag(self, project: Path) -> None:
        limited = run_in(project, "replay", "--candidate", "fake:m1", "--limit", "1")
        assert "1 case(s)" in limited.output
        tagged = run_in(project, "replay", "--candidate", "fake:m1", "--tag", "extraction")
        assert "1 case(s)" in tagged.output

    def test_report_renders_the_latest_run(self, project: Path) -> None:
        run_in(project, "replay", "--candidate", "fake:m1")
        result = run_in(project, "report", "--format", "markdown")
        assert result.exit_code == 0
        assert "Parity" in result.output

    def test_report_with_no_runs(self, project: Path) -> None:
        assert run_in(project, "report").exit_code == ExitCode.STORE_ERROR

    def test_runs_lists_ids(self, project: Path) -> None:
        run_in(project, "replay", "--candidate", "fake:m1")
        assert run_in(project, "runs").exit_code == 0

    def test_providers_lists_configured_providers(self, project: Path) -> None:
        result = run_in(project, "providers")
        assert result.exit_code == 0
        assert "ollama" in result.output

    def test_doctor_succeeds_without_any_credentials(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = run_in(project, "doctor")
        assert result.exit_code == 0
        assert "no blocking problems found" in result.output
