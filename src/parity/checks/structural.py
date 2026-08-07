"""Structural checks: shape, schema, tool calls, truncation.

These catch the failure modes that actually break downstream code after a model
swap. They are the reason a deterministic layer exists at all — no judge is
needed to know that JSON stopped parsing.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import jsonschema
from jsonschema.exceptions import SchemaError

from parity.checks.base import CheckContext
from parity.domain.models import CheckResult, Severity

#: Depth limit when walking parsed JSON to enumerate field paths. Guards against
#: pathological nesting in untrusted baselines.
MAX_WALK_DEPTH = 24

#: Finish reasons that mean the model was cut off mid-generation.
TRUNCATION_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})


def field_paths(value: Any, *, prefix: str = "", depth: int = 0) -> set[str]:
    """Enumerate dotted paths present in a parsed JSON value.

    List elements collapse to ``[]`` rather than an index, because "the third
    item lost a field" and "every item lost a field" are the same class of
    regression and indexing would make the check order-sensitive for no gain.
    """
    if depth >= MAX_WALK_DEPTH:
        return set()

    paths: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.add(path)
            paths |= field_paths(child, prefix=path, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            paths |= field_paths(child, prefix=f"{prefix}[]", depth=depth + 1)
    return paths


class EmptyOutputCheck:
    """The candidate returned nothing where the baseline returned something."""

    name = "empty_output"

    def run(self, ctx: CheckContext) -> CheckResult:
        if ctx.baseline.is_empty():
            return CheckResult.skipped(self.name, message="baseline output was already empty")
        if ctx.candidate.is_empty():
            return CheckResult.failed(
                self.name,
                message="candidate returned empty output where baseline produced content",
                baseline_chars=len(ctx.baseline.text),
                candidate_finish_reason=ctx.candidate.finish_reason,
            )
        return CheckResult.passed(self.name)


class JsonParseCheck:
    """The baseline was JSON; the candidate must still be JSON."""

    name = "json_parse"

    def run(self, ctx: CheckContext) -> CheckResult:
        if not ctx.baseline_is_json:
            return CheckResult.skipped(self.name, message="baseline output is not JSON")
        if ctx.candidate_is_json:
            return CheckResult.passed(self.name)
        preview = ctx.candidate.text.strip()[:160]
        return CheckResult.failed(
            self.name,
            message="baseline output parsed as JSON but candidate output did not",
            candidate_preview=preview,
        )


class JsonSchemaCheck:
    """The candidate must satisfy an explicitly declared JSON Schema.

    Only runs when a schema was supplied — on the case's expectations, or via
    ``response_format`` recorded at capture time. Never inferred: an inferred
    schema is a guess, and a guess that blocks deploys is worse than no check.
    """

    name = "json_schema"

    def _schema_for(self, ctx: CheckContext) -> dict[str, Any] | None:
        declared = ctx.case.expectations.json_schema
        if declared is not None:
            return declared
        response_format = ctx.case.input.response_format or {}
        # OpenAI-style: {"type": "json_schema", "json_schema": {"schema": {...}}}
        nested = response_format.get("json_schema")
        if isinstance(nested, dict):
            schema = nested.get("schema")
            if isinstance(schema, dict):
                return schema
        return None

    def run(self, ctx: CheckContext) -> CheckResult:
        schema = self._schema_for(ctx)
        if schema is None:
            return CheckResult.skipped(self.name, message="no JSON Schema declared")
        if not ctx.candidate_is_json:
            return CheckResult.failed(
                self.name,
                message="candidate output is not JSON, so the declared schema cannot hold",
            )
        try:
            jsonschema.validate(instance=ctx.candidate_json[1], schema=schema)
        except SchemaError as exc:
            # The schema itself is broken. That is the user's config problem, not
            # a model regression, so report it as a warning and keep the run alive.
            return CheckResult.failed(
                self.name,
                message=f"declared JSON Schema is itself invalid: {exc.message}",
                severity=Severity.WARNING,
            )
        except jsonschema.ValidationError as exc:
            return CheckResult.failed(
                self.name,
                message=f"candidate violates declared schema at "
                f"{'.'.join(str(p) for p in exc.absolute_path) or '<root>'}: {exc.message}",
                schema_path=list(exc.absolute_schema_path),
            )
        return CheckResult.passed(self.name)


class RequiredFieldsCheck:
    """Every field the baseline produced must still be produced.

    This is the inferred backbone of the gate. It needs no authoring and catches
    the most common real regression: the new model quietly drops a key that a
    downstream consumer depends on.
    """

    name = "required_fields"

    def run(self, ctx: CheckContext) -> CheckResult:
        declared = ctx.case.expectations.required_fields
        if declared is None:
            if not ctx.settings.infer_required_fields:
                return CheckResult.skipped(self.name, message="field inference disabled")
            if not ctx.baseline_is_json:
                return CheckResult.skipped(self.name, message="baseline output is not JSON")
            expected = field_paths(ctx.baseline_json[1])
        else:
            expected = set(declared)

        if not expected:
            return CheckResult.skipped(self.name, message="no fields to require")
        if not ctx.candidate_is_json:
            return CheckResult.failed(
                self.name,
                message="candidate output is not JSON, so required fields cannot be present",
                required_count=len(expected),
            )

        actual = field_paths(ctx.candidate_json[1])
        missing = sorted(expected - actual)
        if missing:
            shown = missing[:10]
            suffix = "" if len(missing) == len(shown) else f" (+{len(missing) - len(shown)} more)"
            return CheckResult.failed(
                self.name,
                message=f"candidate dropped {len(missing)} field(s): {', '.join(shown)}{suffix}",
                missing=missing,
            )

        added = sorted(actual - expected)
        if added:
            return CheckResult.failed(
                self.name,
                message=f"candidate added {len(added)} unexpected field(s): "
                f"{', '.join(added[:10])}",
                severity=Severity.WARNING,
                added=added,
            )
        return CheckResult.passed(self.name)


class ToolCallCheck:
    """The candidate must invoke the same tools the baseline invoked.

    Silently stopping to call a tool is one of the nastiest migration failures:
    nothing errors, the response just becomes ungrounded.
    """

    name = "tool_calls"

    def run(self, ctx: CheckContext) -> CheckResult:
        declared = ctx.case.expectations.must_call_tools
        if declared is None:
            if not ctx.settings.infer_tool_calls:
                return CheckResult.skipped(self.name, message="tool-call inference disabled")
            expected = Counter(tc.name for tc in ctx.baseline.tool_calls)
        else:
            expected = Counter(declared)

        actual = Counter(tc.name for tc in ctx.candidate.tool_calls)
        if not expected and not actual:
            return CheckResult.skipped(self.name, message="no tool calls on either side")

        missing = expected - actual
        if missing:
            return CheckResult.failed(
                self.name,
                message="candidate did not call expected tool(s): "
                + ", ".join(f"{n}x{c}" if c > 1 else n for n, c in sorted(missing.items())),
                expected=dict(expected),
                actual=dict(actual),
            )

        extra = actual - expected
        if extra:
            return CheckResult.failed(
                self.name,
                message="candidate called additional tool(s): "
                + ", ".join(sorted(extra.elements())),
                severity=Severity.WARNING,
                expected=dict(expected),
                actual=dict(actual),
            )

        baseline_shapes = Counter(tc.signature() for tc in ctx.baseline.tool_calls)
        candidate_shapes = Counter(tc.signature() for tc in ctx.candidate.tool_calls)
        if baseline_shapes != candidate_shapes:
            return CheckResult.failed(
                self.name,
                message="same tools called with a different argument shape",
                severity=Severity.WARNING,
                baseline=sorted(baseline_shapes.elements()),
                candidate=sorted(candidate_shapes.elements()),
            )
        return CheckResult.passed(self.name)


class TruncationCheck:
    """The candidate was cut off mid-generation where the baseline completed."""

    name = "truncation"

    def run(self, ctx: CheckContext) -> CheckResult:
        candidate_reason = (ctx.candidate.finish_reason or "").lower()
        if candidate_reason not in TRUNCATION_REASONS:
            return CheckResult.passed(self.name)
        baseline_reason = (ctx.baseline.finish_reason or "").lower()
        if baseline_reason in TRUNCATION_REASONS:
            return CheckResult.skipped(
                self.name, message="baseline was also truncated; not a new regression"
            )
        return CheckResult.failed(
            self.name,
            message=f"candidate stopped on '{candidate_reason}' "
            f"(baseline stopped on '{baseline_reason or 'unspecified'}') — output is truncated",
            baseline_finish_reason=ctx.baseline.finish_reason,
            candidate_finish_reason=ctx.candidate.finish_reason,
        )
