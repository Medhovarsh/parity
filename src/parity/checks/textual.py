"""Textual checks: refusals, declared formats, length drift, exact match."""

from __future__ import annotations

import re

from parity.checks.base import CheckContext
from parity.domain.models import CheckResult
from parity.domain.policy import REFUSAL_SCAN_CHARS
from parity.errors import CheckError


def normalise(text: str, *, ignore_whitespace: bool, ignore_case: bool) -> str:
    """Canonical form used for equivalence comparisons."""
    result = text.strip()
    if ignore_whitespace:
        result = re.sub(r"\s+", " ", result)
    if ignore_case:
        result = result.casefold()
    return result


class RefusalCheck:
    """The candidate declined a task the baseline performed.

    A model swap turning working prompts into refusals is a top-three migration
    failure and is invisible to a naive string diff, which just reports "text
    changed".
    """

    name = "refusal"

    def _looks_like_refusal(self, text: str, patterns: tuple[str, ...]) -> str | None:
        head = text.strip()[:REFUSAL_SCAN_CHARS]
        if not head:
            return None
        for pattern in patterns:
            if re.search(pattern, head, flags=re.IGNORECASE):
                return pattern
        return None

    def run(self, ctx: CheckContext) -> CheckResult:
        if ctx.case.expectations.forbid_refusal is False:
            return CheckResult.skipped(self.name, message="refusal check disabled for this case")

        patterns = ctx.settings.refusal_patterns
        candidate_hit = self._looks_like_refusal(ctx.candidate.text, patterns)
        if candidate_hit is None:
            return CheckResult.passed(self.name)

        baseline_hit = self._looks_like_refusal(ctx.baseline.text, patterns)
        if baseline_hit is not None:
            return CheckResult.skipped(
                self.name, message="baseline also refused; not a new regression"
            )
        return CheckResult.failed(
            self.name,
            message="candidate refused a request the baseline completed",
            matched_pattern=candidate_hit,
            candidate_preview=ctx.candidate.text.strip()[:200],
        )


class FormatRegexCheck:
    """The candidate must match an explicitly declared format."""

    name = "format_regex"

    def run(self, ctx: CheckContext) -> CheckResult:
        pattern = ctx.case.expectations.format_regex
        if pattern is None:
            return CheckResult.skipped(self.name, message="no format regex declared")
        try:
            compiled = re.compile(pattern, flags=re.DOTALL)
        except re.error as exc:
            raise CheckError(f"invalid format_regex {pattern!r}: {exc}") from exc
        if compiled.search(ctx.candidate.text):
            return CheckResult.passed(self.name)
        return CheckResult.failed(
            self.name,
            message=f"candidate output does not match declared format /{pattern}/",
            candidate_preview=ctx.candidate.text.strip()[:200],
        )


class LengthDeltaCheck:
    """Output length moved further than the configured tolerance.

    Warning severity by default. A large swing usually signals verbosity drift
    or a silently dropped section, both worth a human glance, neither
    self-evidently a break.
    """

    name = "length_delta"

    def run(self, ctx: CheckContext) -> CheckResult:
        limit = ctx.case.expectations.max_length_delta_ratio
        if limit is None:
            limit = ctx.settings.max_length_delta_ratio
        if limit <= 0:
            return CheckResult.skipped(self.name, message="length check disabled")

        baseline_len = len(ctx.baseline.text.strip())
        candidate_len = len(ctx.candidate.text.strip())
        if baseline_len == 0:
            return CheckResult.skipped(self.name, message="baseline text is empty")

        ratio = abs(candidate_len - baseline_len) / baseline_len
        if ratio <= limit:
            return CheckResult.passed(
                self.name, message=f"length changed by {ratio:.0%}", ratio=round(ratio, 4)
            )
        direction = "longer" if candidate_len > baseline_len else "shorter"
        return CheckResult.failed(
            self.name,
            message=f"candidate is {ratio:.0%} {direction} than baseline "
            f"({baseline_len} → {candidate_len} chars, tolerance {limit:.0%})",
            severity=ctx.settings.length_severity,
            ratio=round(ratio, 4),
            baseline_chars=baseline_len,
            candidate_chars=candidate_len,
        )


class ExactMatchCheck:
    """Byte-for-byte agreement, for cases where nothing may move at all."""

    name = "exact_match"

    def run(self, ctx: CheckContext) -> CheckResult:
        if not ctx.case.expectations.exact_match:
            return CheckResult.skipped(self.name, message="exact match not required")
        baseline = normalise(
            ctx.baseline.text,
            ignore_whitespace=ctx.settings.ignore_whitespace,
            ignore_case=ctx.settings.ignore_case,
        )
        candidate = normalise(
            ctx.candidate.text,
            ignore_whitespace=ctx.settings.ignore_whitespace,
            ignore_case=ctx.settings.ignore_case,
        )
        if baseline == candidate:
            return CheckResult.passed(self.name)
        return CheckResult.failed(
            self.name,
            message="candidate does not exactly match baseline",
            baseline_preview=baseline[:200],
            candidate_preview=candidate[:200],
        )
