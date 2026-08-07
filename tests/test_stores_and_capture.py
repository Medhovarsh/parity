"""Stores, importing, and live capture.

Durability and deduplication matter here: a baseline is evidence. Losing half of
it to a crash, or double-counting cases, both corrupt the thing the gate depends
on.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from parity.adapters.providers.fake import FakeProvider
from parity.adapters.stores.jsonl_store import JsonlBaselineStore
from parity.adapters.stores.registry import open_baseline_store
from parity.adapters.stores.run_store import FileRunStore
from parity.adapters.stores.sqlite_store import SqliteBaselineStore
from parity.capture.importer import ImportResult, import_records, iter_jsonl, parse_record
from parity.capture.recorder import Recorder
from parity.domain.models import CaseOutcome, ModelRef, RunReport, Verdict
from parity.errors import ConfigError, StoreError
from parity.ports.store import BaselineStore
from parity.security.redaction import default_redactor
from tests.conftest import make_case, make_input


def store_kinds(tmp_path: Path) -> list[BaselineStore]:
    return [
        JsonlBaselineStore(tmp_path / "baseline.jsonl"),
        SqliteBaselineStore(tmp_path / "baseline.db"),
    ]


class TestBaselineStores:
    """Both stores must be interchangeable. Every test runs against both."""

    @pytest.fixture(params=["jsonl", "sqlite"])
    def store(self, request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[BaselineStore]:
        suffix = "jsonl" if request.param == "jsonl" else "db"
        built = open_baseline_store(request.param, tmp_path / f"baseline.{suffix}")
        yield built
        built.close()

    def test_starts_empty(self, store: BaselineStore) -> None:
        assert store.count() == 0
        assert list(store.iter_cases()) == []

    def test_roundtrip(self, store: BaselineStore) -> None:
        case = make_case(user="hello")
        assert store.extend([case]) == 1
        assert store.count() == 1
        loaded = list(store.iter_cases())
        assert loaded[0].case_id == case.case_id
        assert loaded[0].input.messages[-1].content == "hello"

    def test_deduplicates_by_case_id(self, store: BaselineStore) -> None:
        case = make_case(user="same")
        assert store.extend([case]) == 1
        assert store.extend([case]) == 0
        assert store.count() == 1

    def test_preserves_insertion_order(self, store: BaselineStore) -> None:
        cases = [make_case(user=f"q{i}") for i in range(5)]
        store.extend(cases)
        assert [c.case_id for c in store.iter_cases()] == [c.case_id for c in cases]

    def test_get_by_id(self, store: BaselineStore) -> None:
        case = make_case(user="findme")
        store.extend([case])
        assert store.get(case.case_id) is not None
        assert store.get("nope") is None

    def test_replace_all(self, store: BaselineStore) -> None:
        store.extend([make_case(user="old")])
        replacement = [make_case(user="new1"), make_case(user="new2")]
        assert store.replace_all(replacement) == 2
        assert store.count() == 2
        assert {c.input.messages[-1].content for c in store.iter_cases()} == {"new1", "new2"}

    def test_extend_with_nothing(self, store: BaselineStore) -> None:
        assert store.extend([]) == 0

    def test_survives_reopen(self, store: BaselineStore, tmp_path: Path) -> None:
        case = make_case(user="persisted")
        store.extend([case])
        store.close()
        reopened = open_baseline_store(
            "jsonl" if str(store.location).endswith(".jsonl") else "sqlite", store.location
        )
        try:
            assert reopened.get(case.case_id) is not None
        finally:
            reopened.close()


class TestJsonlSpecifics:
    def test_rejects_malformed_line(self, tmp_path: Path) -> None:
        path = tmp_path / "b.jsonl"
        path.write_text('{"case_id": "a", "not": "a case"}\n', encoding="utf-8")
        with pytest.raises(StoreError, match="malformed case record"):
            list(JsonlBaselineStore(path).iter_cases())

    def test_rejects_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "b.jsonl"
        path.write_text("{not json\n", encoding="utf-8")
        with pytest.raises(StoreError, match="invalid JSON"):
            JsonlBaselineStore(path).count()

    def test_ignores_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "b.jsonl"
        store = JsonlBaselineStore(path)
        store.extend([make_case(user="a")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n\n")
        assert store.count() == 1

    def test_replace_all_leaves_no_temp_files_behind(self, tmp_path: Path) -> None:
        store = JsonlBaselineStore(tmp_path / "b.jsonl")
        store.extend([make_case(user="a")])
        store.replace_all([make_case(user="b")])
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        store = JsonlBaselineStore(tmp_path / "nested" / "deep" / "b.jsonl")
        assert store.extend([make_case()]) == 1


def test_unknown_store_kind_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown baseline store kind"):
        open_baseline_store("mongodb", tmp_path / "x")


class TestRunStore:
    def build_report(self, run_id: str = "run-1") -> RunReport:
        return RunReport.build(
            parity_version="0.1.0",
            baseline_ref=ModelRef.parse("a:b"),
            candidate_ref=ModelRef.parse("c:d"),
            baseline_source="mem",
            judge="none",
            outcomes=(CaseOutcome(case_id="x", verdict=Verdict.EQUIVALENT),),
            run_id=run_id,
        )

    def test_save_and_load(self, tmp_path: Path) -> None:
        store = FileRunStore(tmp_path)
        report = self.build_report()
        store.save(report)
        loaded = store.load("run-1")
        assert loaded is not None
        assert loaded.run_id == "run-1"
        assert loaded.summary.total == 1

    def test_missing_run(self, tmp_path: Path) -> None:
        assert FileRunStore(tmp_path).load("nope") is None
        assert FileRunStore(tmp_path).load_latest() is None

    def test_lists_ids(self, tmp_path: Path) -> None:
        store = FileRunStore(tmp_path)
        store.save(self.build_report("run-a"))
        store.save(self.build_report("run-b"))
        assert set(store.list_run_ids()) == {"run-a", "run-b"}

    @pytest.mark.parametrize("run_id", ["../escape", "a/b", "a\\b", ".hidden", ""])
    def test_rejects_path_traversal(self, tmp_path: Path, run_id: str) -> None:
        # run ids can reach this store from the command line.
        with pytest.raises(StoreError, match="invalid run id"):
            FileRunStore(tmp_path).load(run_id)

    def test_rejects_malformed_report(self, tmp_path: Path) -> None:
        (tmp_path / "bad.run.json").write_text('{"nope": 1}', encoding="utf-8")
        with pytest.raises(StoreError, match="malformed run report"):
            FileRunStore(tmp_path).load("bad")


class TestImporter:
    def test_proxy_shape(self) -> None:
        record = {
            "request": {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            "response": {
                "model": "m",
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "yo"}}
                ],
            },
        }
        case = parse_record(record)
        assert case.output.text == "yo"
        assert case.reference.model == "m"

    def test_anthropic_response_shape(self) -> None:
        record = {
            "request": {"model": "claude", "messages": [{"role": "user", "content": "hi"}]},
            "response": {
                "model": "claude",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "hello there"}],
            },
        }
        case = parse_record(record)
        assert case.output.text == "hello there"
        assert case.output.finish_reason == "end_turn"

    def test_flat_shape(self) -> None:
        case = parse_record({"messages": [{"role": "user", "content": "q"}], "completion": "a"})
        assert case.output.text == "a"

    def test_explicit_shape_with_string_output(self) -> None:
        case = parse_record(
            {"input": {"messages": [{"role": "user", "content": "q"}]}, "output": "a"}
        )
        assert case.output.text == "a"

    def test_native_case_roundtrip(self) -> None:
        original = make_case(user="native")
        reparsed = parse_record(json.loads(original.model_dump_json()))
        assert reparsed.case_id == original.case_id

    def test_parses_tool_calls_from_a_log(self) -> None:
        record = {
            "request": {"model": "m", "messages": [{"role": "user", "content": "search"}]},
            "response": {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"q": "x"}',
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        }
        case = parse_record(record)
        assert case.output.tool_calls[0].name == "search"
        assert case.output.tool_calls[0].arguments == {"q": "x"}

    def test_tolerates_unparseable_tool_arguments(self) -> None:
        # A model emitting broken argument JSON is a finding, not a crash.
        record = {
            "request": {"model": "m", "messages": [{"role": "user", "content": "x"}]},
            "response": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [{"function": {"name": "t", "arguments": "{broken"}}],
                        }
                    }
                ]
            },
        }
        case = parse_record(record)
        assert case.output.tool_calls[0].arguments == {"_raw": "{broken"}

    def test_flattens_multimodal_text_blocks(self) -> None:
        record = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {"type": "image_url", "image_url": {"url": "http://x"}},
                    ],
                }
            ],
            "completion": "a picture",
        }
        case = parse_record(record)
        assert case.input.messages[0].content == "describe"

    def test_rejects_unknown_shape(self) -> None:
        with pytest.raises(ValueError, match="unrecognised record shape"):
            parse_record({"nothing": "useful"})

    def test_rejects_bad_role(self) -> None:
        with pytest.raises(ValueError, match="unsupported role"):
            parse_record({"messages": [{"role": "wizard", "content": "x"}], "completion": "y"})


class TestImportRecords:
    def records(self, *values: object) -> list[tuple[int, object]]:
        return list(enumerate(values, start=1))

    def test_skips_bad_records_by_default(self) -> None:
        result = import_records(
            self.records(
                {"messages": [{"role": "user", "content": "q"}], "completion": "a"},
                {"garbage": True},
            ),
            redactor=None,
        )
        assert result.parsed == 1
        assert len(result.skipped) == 1

    def test_strict_mode_raises(self) -> None:
        with pytest.raises(StoreError):
            import_records(self.records({"garbage": True}), redactor=None, strict=True)

    def test_applies_redaction(self) -> None:
        result = import_records(
            self.records(
                {
                    "messages": [
                        {"role": "user", "content": "key sk-abcdefghijklmnopqrstuvwxyz01"}
                    ],
                    "completion": "ok",
                }
            ),
            redactor=default_redactor(),
        )
        assert result.redaction.touched
        assert "sk-abcdefghij" not in result.cases[0].input.messages[0].content
        assert result.cases[0].redacted

    def test_reference_override_and_tags(self) -> None:
        result = import_records(
            self.records({"messages": [{"role": "user", "content": "q"}], "completion": "a"}),
            redactor=None,
            reference_override=ModelRef.parse("prod:v2"),
            tags=("smoke",),
        )
        assert str(result.cases[0].reference) == "prod:v2"
        assert result.cases[0].tags == ("smoke",)

    def test_non_object_record_is_skipped(self) -> None:
        result = import_records(self.records("just a string"), redactor=None)
        assert result.parsed == 0
        assert "expected an object" in result.skipped[0]


class TestIterJsonl:
    def test_reads_jsonl(self, sample_log: Path) -> None:
        assert len(list(iter_jsonl(sample_log))) == 2

    def test_reads_a_json_array(self, tmp_path: Path) -> None:
        path = tmp_path / "arr.json"
        path.write_text(json.dumps([{"a": 1}, {"b": 2}]), encoding="utf-8")
        assert [value for _, value in iter_jsonl(path)] == [{"a": 1}, {"b": 2}]

    def test_rejects_invalid_json_line(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text("{broken\n", encoding="utf-8")
        with pytest.raises(StoreError, match="invalid JSON"):
            list(iter_jsonl(path))


class TestRecorder:
    def test_captures_completions(self, tmp_path: Path) -> None:
        store = JsonlBaselineStore(tmp_path / "b.jsonl")
        with Recorder(FakeProvider(), store=store, buffer_size=1) as recorder:
            recorder.complete("m1", make_input("hello"))
        assert store.count() == 1
        assert recorder.captured == 1

    def test_buffers_until_flush(self, tmp_path: Path) -> None:
        store = JsonlBaselineStore(tmp_path / "b.jsonl")
        recorder = Recorder(FakeProvider(), store=store, buffer_size=10)
        recorder.complete("m1", make_input("a"))
        assert store.count() == 0
        recorder.flush()
        assert store.count() == 1

    def test_redacts_before_persisting(self, tmp_path: Path) -> None:
        store = JsonlBaselineStore(tmp_path / "b.jsonl")
        with Recorder(
            FakeProvider(), store=store, redactor=default_redactor(), buffer_size=1
        ) as recorder:
            recorder.complete("m1", make_input("key sk-abcdefghijklmnopqrstuvwxyz01"))
        stored = next(iter(store.iter_cases()))
        assert "sk-abcdefghij" not in stored.input.messages[-1].content
        assert recorder.redaction.touched

    def test_can_be_disabled(self, tmp_path: Path) -> None:
        store = JsonlBaselineStore(tmp_path / "b.jsonl")
        with Recorder(FakeProvider(), store=store, enabled=False, buffer_size=1) as recorder:
            recorder.complete("m1", make_input("x"))
        assert store.count() == 0

    def test_flushes_on_the_error_path(self, tmp_path: Path) -> None:
        # Cases captured before a failure are still valid evidence.
        store = JsonlBaselineStore(tmp_path / "b.jsonl")
        with (
            pytest.raises(RuntimeError),
            Recorder(FakeProvider(), store=store, buffer_size=100) as recorder,
        ):
            recorder.complete("m1", make_input("captured"))
            raise RuntimeError("caller blew up")
        assert store.count() == 1

    def test_reports_provider_name(self, tmp_path: Path) -> None:
        store = JsonlBaselineStore(tmp_path / "b.jsonl")
        recorder = Recorder(FakeProvider(name="myprov"), store=store)
        assert recorder.name == "myprov"
        recorder.close()


def test_import_result_defaults() -> None:
    result = ImportResult()
    assert result.parsed == 0
    assert not result.redaction.touched
    assert result.redaction.summary() == "no secrets or personal data matched"
