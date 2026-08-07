"""Check behaviour.

Each test states the regression the check exists to catch, and asserts both that
it fires when it should and stays quiet when it should not. False positives here
block deploys, so the negative cases matter as much as the positive ones.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from parity.checks.base import CheckContext, try_parse_json
from parity.checks.numeric import NumericToleranceCheck, numeric_paths, within_tolerance
from parity.checks.registry import ALL_CHECKS, build_pipeline, check_names, pipeline_for_case
from parity.checks.structural import (
    EmptyOutputCheck,
    JsonParseCheck,
    JsonSchemaCheck,
    RequiredFieldsCheck,
    ToolCallCheck,
    TruncationCheck,
    field_paths,
)
from parity.checks.textual import (
    ExactMatchCheck,
    FormatRegexCheck,
    LengthDeltaCheck,
    RefusalCheck,
    normalise,
)
from parity.domain.models import (
    Case,
    CheckStatus,
    Expectations,
    InteractionOutput,
    Severity,
    ToolCall,
)
from parity.domain.policy import CheckSettings
from parity.errors import CheckError, ConfigError
from tests.conftest import json_output, make_case, make_output


def ctx(
    case_output: InteractionOutput, candidate: InteractionOutput, **kwargs: object
) -> CheckContext:
    settings = CheckSettings(**kwargs)  # type: ignore[arg-type]
    return CheckContext(case=make_case(output=case_output), candidate=candidate, settings=settings)


def ctx_for(case: Case, candidate: InteractionOutput, **kwargs: object) -> CheckContext:
    settings = CheckSettings(**kwargs)  # type: ignore[arg-type]
    return CheckContext(case=case, candidate=candidate, settings=settings)


class TestTryParseJson:
    def test_plain_json(self) -> None:
        assert try_parse_json('{"a": 1}') == (True, {"a": 1})

    def test_unwraps_fenced_block(self) -> None:
        ok, value = try_parse_json('```json\n{"a": 1}\n```')
        assert ok and value == {"a": 1}

    def test_unwraps_bare_fence(self) -> None:
        ok, value = try_parse_json("```\n[1, 2]\n```")
        assert ok and value == [1, 2]

    @pytest.mark.parametrize("text", ["", "   ", "not json", "{unclosed"])
    def test_rejects_non_json(self, text: str) -> None:
        assert try_parse_json(text) == (False, None)


class TestEmptyOutput:
    def test_fires_when_candidate_is_empty(self) -> None:
        result = EmptyOutputCheck().run(ctx(make_output("content"), make_output("")))
        assert result.status is CheckStatus.FAIL
        assert result.severity is Severity.BLOCKING

    def test_skips_when_baseline_was_already_empty(self) -> None:
        result = EmptyOutputCheck().run(ctx(make_output(""), make_output("")))
        assert result.status is CheckStatus.SKIP

    def test_tool_calls_count_as_content(self) -> None:
        candidate = make_output("", tool_calls=(ToolCall(name="t"),))
        assert EmptyOutputCheck().run(ctx(make_output("x"), candidate)).status is CheckStatus.PASS


class TestTruncation:
    def test_fires_on_new_truncation(self) -> None:
        candidate = make_output("half", finish_reason="length")
        result = TruncationCheck().run(ctx(make_output("whole"), candidate))
        assert result.status is CheckStatus.FAIL

    def test_skips_when_baseline_was_also_truncated(self) -> None:
        baseline = make_output("half", finish_reason="length")
        candidate = make_output("half", finish_reason="length")
        assert TruncationCheck().run(ctx(baseline, candidate)).status is CheckStatus.SKIP

    def test_passes_on_normal_stop(self) -> None:
        assert (
            TruncationCheck().run(ctx(make_output("a"), make_output("b"))).status
            is CheckStatus.PASS
        )


class TestRefusal:
    @pytest.mark.parametrize(
        "text",
        [
            "I can't help with that.",
            "I'm sorry, but I cannot provide that information.",
            "As an AI language model, I cannot do this.",
            "I'm unable to assist with this request.",
        ],
    )
    def test_detects_refusals(self, text: str) -> None:
        result = RefusalCheck().run(ctx(make_output("Sure, here you go."), make_output(text)))
        assert result.status is CheckStatus.FAIL

    def test_skips_when_baseline_also_refused(self) -> None:
        baseline = make_output("I can't help with that.")
        candidate = make_output("I cannot help with that.")
        assert RefusalCheck().run(ctx(baseline, candidate)).status is CheckStatus.SKIP

    def test_ignores_discussion_of_refusal_deep_in_text(self) -> None:
        # The phrase far into a long answer is discussing refusal, not performing it.
        candidate = make_output(("padding. " * 200) + "I can't help with that.")
        assert RefusalCheck().run(ctx(make_output("answer"), candidate)).status is CheckStatus.PASS

    def test_can_be_disabled_per_case(self) -> None:
        case = make_case(
            output=make_output("answer"), expectations=Expectations(forbid_refusal=False)
        )
        result = RefusalCheck().run(ctx_for(case, make_output("I can't help with that.")))
        assert result.status is CheckStatus.SKIP


class TestJsonParse:
    def test_fires_when_candidate_stops_parsing(self) -> None:
        result = JsonParseCheck().run(ctx(json_output({"a": 1}), make_output("plain text")))
        assert result.status is CheckStatus.FAIL

    def test_skips_when_baseline_was_not_json(self) -> None:
        assert (
            JsonParseCheck().run(ctx(make_output("prose"), make_output("other prose"))).status
            is CheckStatus.SKIP
        )

    def test_passes_when_both_parse(self) -> None:
        assert (
            JsonParseCheck().run(ctx(json_output({"a": 1}), json_output({"a": 2}))).status
            is CheckStatus.PASS
        )


class TestRequiredFields:
    def test_detects_dropped_field(self) -> None:
        result = RequiredFieldsCheck().run(
            ctx(json_output({"a": 1, "b": 2}), json_output({"a": 1}))
        )
        assert result.status is CheckStatus.FAIL
        assert "b" in result.message
        assert result.severity is Severity.BLOCKING

    def test_added_field_is_only_a_warning(self) -> None:
        result = RequiredFieldsCheck().run(
            ctx(json_output({"a": 1}), json_output({"a": 1, "extra": 2}))
        )
        assert result.status is CheckStatus.FAIL
        assert result.severity is Severity.WARNING

    def test_detects_nested_drop(self) -> None:
        result = RequiredFieldsCheck().run(
            ctx(
                json_output({"invoice": {"total": 1, "tax": 2}}),
                json_output({"invoice": {"total": 1}}),
            )
        )
        assert result.status is CheckStatus.FAIL
        assert "invoice.tax" in result.message

    def test_value_changes_are_not_a_field_drop(self) -> None:
        assert (
            RequiredFieldsCheck().run(ctx(json_output({"a": 1}), json_output({"a": 999}))).status
            is CheckStatus.PASS
        )

    def test_non_json_candidate_fails(self) -> None:
        result = RequiredFieldsCheck().run(ctx(json_output({"a": 1}), make_output("nope")))
        assert result.status is CheckStatus.FAIL

    def test_honours_explicit_expectations(self) -> None:
        case = make_case(
            output=make_output("prose"),
            expectations=Expectations(required_fields=("id",)),
        )
        result = RequiredFieldsCheck().run(ctx_for(case, json_output({"other": 1})))
        assert result.status is CheckStatus.FAIL

    def test_inference_can_be_disabled(self) -> None:
        result = RequiredFieldsCheck().run(
            ctx(json_output({"a": 1}), json_output({}), infer_required_fields=False)
        )
        assert result.status is CheckStatus.SKIP

    def test_field_paths_collapse_list_indices(self) -> None:
        assert field_paths({"items": [{"id": 1}, {"id": 2}]}) == {"items", "items[].id"}


class TestJsonSchema:
    SCHEMA: ClassVar[dict[str, object]] = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }

    def test_skips_without_a_declared_schema(self) -> None:
        assert (
            JsonSchemaCheck().run(ctx(json_output({"id": "1"}), json_output({}))).status
            is CheckStatus.SKIP
        )

    def test_validates_against_case_expectations(self) -> None:
        case = make_case(
            output=json_output({"id": "1"}), expectations=Expectations(json_schema=self.SCHEMA)
        )
        assert (
            JsonSchemaCheck().run(ctx_for(case, json_output({"id": "2"}))).status
            is CheckStatus.PASS
        )
        assert (
            JsonSchemaCheck().run(ctx_for(case, json_output({"nope": 1}))).status
            is CheckStatus.FAIL
        )

    def test_reads_schema_from_response_format(self) -> None:
        case = make_case(
            output=json_output({"id": "1"}),
            response_format={"type": "json_schema", "json_schema": {"schema": self.SCHEMA}},
        )
        result = JsonSchemaCheck().run(ctx_for(case, json_output({"wrong": 1})))
        assert result.status is CheckStatus.FAIL

    def test_broken_schema_is_a_warning_not_a_block(self) -> None:
        case = make_case(
            output=json_output({"id": "1"}),
            expectations=Expectations(json_schema={"type": "not-a-type"}),
        )
        result = JsonSchemaCheck().run(ctx_for(case, json_output({"id": "2"})))
        assert result.status is CheckStatus.FAIL
        assert result.severity is Severity.WARNING


class TestToolCalls:
    def test_detects_missing_tool_call(self) -> None:
        baseline = make_output("", tool_calls=(ToolCall(name="search"),))
        result = ToolCallCheck().run(ctx(baseline, make_output("I'll answer directly")))
        assert result.status is CheckStatus.FAIL
        assert "search" in result.message

    def test_extra_tool_call_is_a_warning(self) -> None:
        baseline = make_output("", tool_calls=(ToolCall(name="search"),))
        candidate = make_output("", tool_calls=(ToolCall(name="search"), ToolCall(name="lookup")))
        result = ToolCallCheck().run(ctx(baseline, candidate))
        assert result.severity is Severity.WARNING

    def test_argument_shape_change_is_a_warning(self) -> None:
        baseline = make_output("", tool_calls=(ToolCall(name="s", arguments={"q": 1}),))
        candidate = make_output("", tool_calls=(ToolCall(name="s", arguments={"query": 1}),))
        result = ToolCallCheck().run(ctx(baseline, candidate))
        assert result.status is CheckStatus.FAIL
        assert result.severity is Severity.WARNING

    def test_argument_value_change_passes(self) -> None:
        baseline = make_output("", tool_calls=(ToolCall(name="s", arguments={"q": "a"}),))
        candidate = make_output("", tool_calls=(ToolCall(name="s", arguments={"q": "b"}),))
        assert ToolCallCheck().run(ctx(baseline, candidate)).status is CheckStatus.PASS

    def test_skips_when_no_tools_involved(self) -> None:
        assert (
            ToolCallCheck().run(ctx(make_output("a"), make_output("b"))).status is CheckStatus.SKIP
        )

    def test_honours_explicit_expectations(self) -> None:
        case = make_case(
            output=make_output("x"), expectations=Expectations(must_call_tools=("required",))
        )
        assert ToolCallCheck().run(ctx_for(case, make_output("y"))).status is CheckStatus.FAIL


class TestLengthDelta:
    def test_fires_beyond_tolerance(self) -> None:
        result = LengthDeltaCheck().run(
            ctx(make_output("a" * 100), make_output("a" * 300), max_length_delta_ratio=0.5)
        )
        assert result.status is CheckStatus.FAIL
        assert result.severity is Severity.WARNING

    def test_passes_within_tolerance(self) -> None:
        assert (
            LengthDeltaCheck()
            .run(ctx(make_output("a" * 100), make_output("a" * 120), max_length_delta_ratio=0.5))
            .status
            is CheckStatus.PASS
        )

    def test_severity_is_configurable(self) -> None:
        result = LengthDeltaCheck().run(
            ctx(
                make_output("a" * 10),
                make_output("a" * 100),
                max_length_delta_ratio=0.1,
                length_severity=Severity.BLOCKING,
            )
        )
        assert result.blocking_failure

    def test_disabled_by_zero_tolerance(self) -> None:
        assert (
            LengthDeltaCheck()
            .run(ctx(make_output("a"), make_output("b" * 500), max_length_delta_ratio=0.0))
            .status
            is CheckStatus.SKIP
        )


class TestExactMatch:
    def test_skips_unless_requested(self) -> None:
        assert (
            ExactMatchCheck().run(ctx(make_output("a"), make_output("b"))).status
            is CheckStatus.SKIP
        )

    def test_fires_on_difference(self) -> None:
        case = make_case(output=make_output("a"), expectations=Expectations(exact_match=True))
        assert ExactMatchCheck().run(ctx_for(case, make_output("b"))).status is CheckStatus.FAIL

    def test_whitespace_normalisation_applies(self) -> None:
        case = make_case(output=make_output("a  b"), expectations=Expectations(exact_match=True))
        assert ExactMatchCheck().run(ctx_for(case, make_output("a b"))).status is CheckStatus.PASS


class TestFormatRegex:
    def test_skips_without_a_pattern(self) -> None:
        assert (
            FormatRegexCheck().run(ctx(make_output("a"), make_output("b"))).status
            is CheckStatus.SKIP
        )

    def test_enforces_pattern(self) -> None:
        case = make_case(
            output=make_output("ID-1"), expectations=Expectations(format_regex=r"^ID-\d+$")
        )
        assert FormatRegexCheck().run(ctx_for(case, make_output("ID-2"))).status is CheckStatus.PASS
        assert FormatRegexCheck().run(ctx_for(case, make_output("oops"))).status is CheckStatus.FAIL

    def test_invalid_pattern_raises_check_error(self) -> None:
        case = make_case(output=make_output("x"), expectations=Expectations(format_regex="[bad"))
        with pytest.raises(CheckError, match="invalid format_regex"):
            FormatRegexCheck().run(ctx_for(case, make_output("x")))


class TestNumericTolerance:
    def test_skips_without_configured_tolerance(self) -> None:
        assert (
            NumericToleranceCheck().run(ctx(json_output({"n": 1}), json_output({"n": 99}))).status
            is CheckStatus.SKIP
        )

    def test_detects_drift(self) -> None:
        result = NumericToleranceCheck().run(
            ctx(json_output({"score": 0.9}), json_output({"score": 0.5}), numeric_tolerance=0.01)
        )
        assert result.status is CheckStatus.FAIL
        assert "score" in result.message

    def test_allows_small_movement(self) -> None:
        assert (
            NumericToleranceCheck()
            .run(ctx(json_output({"n": 100.0}), json_output({"n": 100.5}), numeric_tolerance=0.01))
            .status
            is CheckStatus.PASS
        )

    def test_missing_path_is_left_to_required_fields(self) -> None:
        assert (
            NumericToleranceCheck()
            .run(ctx(json_output({"a": 1.0}), json_output({}), numeric_tolerance=0.01))
            .status
            is CheckStatus.PASS
        )

    def test_booleans_are_not_numbers(self) -> None:
        assert numeric_paths({"flag": True}) == {}

    def test_numeric_paths_index_lists(self) -> None:
        assert numeric_paths({"xs": [1, 2]}) == {"xs[0]": 1.0, "xs[1]": 2.0}

    @pytest.mark.parametrize(
        ("baseline", "candidate", "tolerance", "expected"),
        [
            (100.0, 101.0, 0.02, True),
            (100.0, 110.0, 0.02, False),
            (0.0, 0.005, 0.01, True),
            (0.0, 0.5, 0.01, False),
            (5.0, 5.0, 0.0, True),
        ],
    )
    def test_within_tolerance(
        self, baseline: float, candidate: float, tolerance: float, expected: bool
    ) -> None:
        assert within_tolerance(baseline, candidate, tolerance) is expected


class TestNormalise:
    def test_collapses_whitespace(self) -> None:
        assert normalise("  a \n b ", ignore_whitespace=True, ignore_case=False) == "a b"

    def test_preserves_whitespace_when_asked(self) -> None:
        assert normalise("a  b", ignore_whitespace=False, ignore_case=False) == "a  b"

    def test_case_folding_is_opt_in(self) -> None:
        assert normalise("AB", ignore_whitespace=True, ignore_case=True) == "ab"


class TestRegistry:
    def test_names_are_unique(self) -> None:
        names = check_names()
        assert len(names) == len(set(names))

    def test_pipeline_respects_disabled(self) -> None:
        pipeline = build_pipeline(CheckSettings(disabled=("length_delta",)))
        assert "length_delta" not in {c.name for c in pipeline}
        assert len(pipeline) == len(ALL_CHECKS) - 1

    def test_unknown_disabled_name_is_an_error(self) -> None:
        # A typo here silently weakens the gate, which is the failure this
        # whole project exists to prevent.
        with pytest.raises(ConfigError, match="unknown check name"):
            build_pipeline(CheckSettings(disabled=("no_such_check",)))

    def test_per_case_skip(self) -> None:
        pipeline = build_pipeline(CheckSettings())
        case = make_case(expectations=Expectations(skip_checks=("refusal",)))
        assert "refusal" not in {c.name for c in pipeline_for_case(pipeline, case)}
