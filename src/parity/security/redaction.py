"""Secret and PII redaction.

Applied at the boundary where a payload becomes persistent. Everything captured
is treated as hostile: people paste API keys into prompts, and customer records
into the documents they ask a model to summarise.

Replacement is one-way. A matched value becomes ``[REDACTED:<rule>:<digest>]``
where the digest is the first 8 hex characters of its SHA-256. That is enough to
see that the same secret appeared in two places — useful when reading a diff —
and not enough to recover it.

Patterns are ordered most-specific first, because a private key block would
otherwise be partially eaten by a less specific rule.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from parity.domain.models import (
    Case,
    InteractionInput,
    InteractionOutput,
    Message,
    ToolCall,
)

Category = Literal["credential", "pii"]


@dataclass(frozen=True)
class RedactionRule:
    """One named pattern.

    ``group`` selects which capture group is replaced. It is 1 for rules where
    only part of the match is sensitive — a database URL should keep its host
    and lose only its password.
    """

    name: str
    pattern: re.Pattern[str]
    category: Category
    group: int = 0


def _rule(
    name: str, pattern: str, category: Category, *, group: int = 0, flags: int = 0
) -> RedactionRule:
    return RedactionRule(
        name=name, pattern=re.compile(pattern, flags), category=category, group=group
    )


#: Ordered most-specific first. Order is load-bearing.
DEFAULT_RULES: tuple[RedactionRule, ...] = (
    _rule(
        "private_key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,8192}?-----END [A-Z ]*PRIVATE KEY-----",
        "credential",
    ),
    _rule(
        "jwt",
        r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        "credential",
    ),
    _rule("anthropic_key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}", "credential"),
    _rule("openai_key", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}", "credential"),
    _rule("github_token", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", "credential"),
    # Publishing and model-hub tokens. These reach baselines the same way any
    # other credential does — someone pastes a deploy script or a CI log into a
    # prompt — and each grants write access to a package registry.
    _rule("pypi_token", r"\bpypi-[A-Za-z0-9_\-]{16,}", "credential"),
    _rule("npm_token", r"\bnpm_[A-Za-z0-9]{30,}\b", "credential"),
    _rule("huggingface_token", r"\bhf_[A-Za-z0-9]{30,}\b", "credential"),
    _rule("stripe_key", r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{20,}\b", "credential"),
    _rule(
        "sendgrid_key",
        r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}",
        "credential",
    ),
    _rule("slack_token", r"\bxox[baprse]-[A-Za-z0-9\-]{10,}\b", "credential"),
    # Open-ended lengths on purpose. A canonical Google key is AIza + 35 chars,
    # but pinning the length exactly means a longer variant walks straight
    # through. For a redaction rule, over-matching is the safe failure.
    _rule("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35,}", "credential"),
    _rule(
        "aws_access_key_id",
        r"\b(?:AKIA|ASIA|ABIA|ACCA|A3T[A-Z0-9])[A-Z0-9]{16}\b",
        "credential",
    ),
    _rule(
        "bearer_token",
        r"\b[Bb]earer\s+([A-Za-z0-9._\-]{20,})",
        "credential",
        group=1,
    ),
    _rule(
        "connection_string_password",
        r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^:/\s@]+:([^@\s]{3,})@",
        "credential",
        group=1,
    ),
    _rule(
        "generic_assigned_secret",
        r"(?i)\b(?:api[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token)"
        r"\s*[:=]\s*[\"']?([A-Za-z0-9._\-/+]{12,})[\"']?",
        "credential",
        group=1,
    ),
    _rule("ssn", r"\b\d{3}-\d{2}-\d{4}\b", "pii"),
    _rule(
        "credit_card",
        r"\b(?:\d[ -]?){12,18}\d\b",
        "pii",
    ),
    _rule(
        "email",
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
        "pii",
    ),
)


def _luhn_valid(digits: str) -> bool:
    """Luhn checksum, used to keep the credit-card rule from eating order ids."""
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


@dataclass
class RedactionReport:
    """What was removed, by rule. Values are never recorded."""

    counts: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def touched(self) -> bool:
        return self.total > 0

    def merge(self, other: RedactionReport) -> None:
        self.counts.update(other.counts)

    def summary(self) -> str:
        if not self.touched:
            return "no secrets or personal data matched"
        parts = ", ".join(f"{name} x{count}" for name, count in sorted(self.counts.items()))
        return f"redacted {self.total} value(s): {parts}"


class Redactor:
    """Applies redaction rules to text and to whole domain objects."""

    def __init__(
        self,
        rules: Iterable[RedactionRule] = DEFAULT_RULES,
        *,
        categories: frozenset[Category] = frozenset({"credential", "pii"}),
    ) -> None:
        self._rules = tuple(r for r in rules if r.category in categories)
        self._categories = categories

    @property
    def rule_names(self) -> tuple[str, ...]:
        return tuple(rule.name for rule in self._rules)

    @staticmethod
    def _token(rule_name: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        return f"[REDACTED:{rule_name}:{digest}]"

    def _should_redact(self, rule: RedactionRule, value: str) -> bool:
        if rule.name == "credit_card":
            digits = re.sub(r"[ -]", "", value)
            return len(digits) >= 13 and _luhn_valid(digits)
        return True

    def text(self, value: str) -> tuple[str, RedactionReport]:
        """Redact a string. Returns the cleaned text and what was removed."""
        report = RedactionReport()
        if not value:
            return value, report

        result = value
        for rule in self._rules:

            def replace(match: re.Match[str], _rule: RedactionRule = rule) -> str:
                captured = match.group(_rule.group)
                if captured is None or not self._should_redact(_rule, captured):
                    return match.group(0)
                report.counts[_rule.name] += 1
                token = self._token(_rule.name, captured)
                if _rule.group == 0:
                    return token
                # Preserve the surrounding match, swap only the sensitive group.
                start, end = match.span(_rule.group)
                offset = match.start()
                whole = match.group(0)
                return whole[: start - offset] + token + whole[end - offset :]

            result = rule.pattern.sub(replace, result)
        return result, report

    def value(self, value: Any) -> tuple[Any, RedactionReport]:
        """Recursively redact strings inside arbitrary JSON-like structures."""
        report = RedactionReport()
        if isinstance(value, str):
            cleaned, sub = self.text(value)
            report.merge(sub)
            return cleaned, report
        if isinstance(value, dict):
            cleaned_map: dict[str, Any] = {}
            for key, item in value.items():
                cleaned_item, sub = self.value(item)
                report.merge(sub)
                cleaned_map[key] = cleaned_item
            return cleaned_map, report
        if isinstance(value, list):
            cleaned_list = []
            for item in value:
                cleaned_item, sub = self.value(item)
                report.merge(sub)
                cleaned_list.append(cleaned_item)
            return cleaned_list, report
        return value, report

    def _tool_calls(
        self, calls: tuple[ToolCall, ...]
    ) -> tuple[tuple[ToolCall, ...], RedactionReport]:
        report = RedactionReport()
        cleaned: list[ToolCall] = []
        for call in calls:
            arguments, sub = self.value(call.arguments)
            report.merge(sub)
            cleaned.append(call.model_copy(update={"arguments": arguments}))
        return tuple(cleaned), report

    def message(self, message: Message) -> tuple[Message, RedactionReport]:
        report = RedactionReport()
        content, sub = self.text(message.content)
        report.merge(sub)
        tool_calls, sub = self._tool_calls(message.tool_calls)
        report.merge(sub)
        return message.model_copy(update={"content": content, "tool_calls": tool_calls}), report

    def interaction_input(
        self, value: InteractionInput
    ) -> tuple[InteractionInput, RedactionReport]:
        report = RedactionReport()
        messages: list[Message] = []
        for message in value.messages:
            cleaned, sub = self.message(message)
            report.merge(sub)
            messages.append(cleaned)
        return value.model_copy(update={"messages": tuple(messages)}), report

    def interaction_output(
        self, value: InteractionOutput
    ) -> tuple[InteractionOutput, RedactionReport]:
        report = RedactionReport()
        text, sub = self.text(value.text)
        report.merge(sub)
        tool_calls, sub = self._tool_calls(value.tool_calls)
        report.merge(sub)
        return value.model_copy(update={"text": text, "tool_calls": tool_calls}), report

    def case(self, case: Case) -> tuple[Case, RedactionReport]:
        """Redact a whole case.

        The case id is recomputed only when it was derived from the input, so a
        user-supplied id survives redaction and a derived id stays consistent
        with the redacted content it now identifies.
        """
        report = RedactionReport()
        cleaned_input, sub = self.interaction_input(case.input)
        report.merge(sub)
        cleaned_output, sub = self.interaction_output(case.output)
        report.merge(sub)

        was_derived = case.case_id == case.input.fingerprint()
        case_id = cleaned_input.fingerprint() if was_derived else case.case_id

        return (
            case.model_copy(
                update={
                    "case_id": case_id,
                    "input": cleaned_input,
                    "output": cleaned_output,
                    "redacted": True,
                }
            ),
            report,
        )


def default_redactor() -> Redactor:
    """Redactor with every default rule enabled."""
    return Redactor()
