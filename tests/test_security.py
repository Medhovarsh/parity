"""Redaction and resource limits.

Redaction runs on real production payloads before they are persisted. A miss
here leaks a credential into a file people commit and paste into tickets, so
these tests are deliberately exhaustive about what must be caught and what must
be left alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from parity.domain.models import Message, ToolCall
from parity.errors import SecurityLimitExceeded
from parity.security.limits import (
    Limits,
    guard_depth,
    guard_file_size,
    guard_line_size,
    guard_payload,
    guard_record_count,
    harden_permissions,
)
from parity.security.redaction import Redactor, default_redactor
from tests.conftest import make_case, make_input, make_output


class TestCredentialRedaction:
    @pytest.mark.parametrize(
        ("label", "secret"),
        [
            ("openai", "sk-abcdefghijklmnopqrstuvwxyz012345"),
            ("openai_proj", "sk-proj-abcdefghijklmnopqrstuvwxyz012345"),
            ("anthropic", "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"),
            ("github", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
            ("google", "AIzaSyA1234567890abcdefghijklmnopqrstuvw"),
            ("aws", "AKIAIOSFODNN7EXAMPLE"),
            ("slack", "xoxb-123456789012-abcdefghijkl"),
            (
                "jwt",
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
            ),
        ],
    )
    def test_removes_known_key_formats(self, label: str, secret: str) -> None:
        cleaned, report = default_redactor().text(f"my key is {secret} ok")
        assert secret not in cleaned, f"{label} key survived redaction"
        assert report.touched

    def test_removes_private_key_block(self) -> None:
        blob = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA1234567890\n"
            "-----END RSA PRIVATE KEY-----"
        )
        cleaned, report = default_redactor().text(f"here: {blob}")
        assert "MIIEowIBAAKCAQEA" not in cleaned
        assert report.counts["private_key"] == 1

    def test_strips_only_the_password_from_a_connection_string(self) -> None:
        cleaned, _ = default_redactor().text("postgres://admin:hunter2@db.internal:5432/app")
        assert "hunter2" not in cleaned
        # Host and user survive: they are diagnostic, not secret.
        assert "db.internal" in cleaned
        assert "admin" in cleaned

    def test_strips_only_the_token_from_a_bearer_header(self) -> None:
        cleaned, _ = default_redactor().text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
        assert "abcdefghijklmnopqrstuvwxyz" not in cleaned
        assert "Bearer" in cleaned

    def test_catches_assigned_secrets(self) -> None:
        cleaned, _ = default_redactor().text('api_key = "s3cret-value-1234567"')
        assert "s3cret-value-1234567" not in cleaned


class TestPiiRedaction:
    def test_removes_email(self) -> None:
        cleaned, _ = default_redactor().text("write to ada@example.com please")
        assert "ada@example.com" not in cleaned

    def test_removes_ssn(self) -> None:
        cleaned, _ = default_redactor().text("SSN 123-45-6789")
        assert "123-45-6789" not in cleaned

    def test_removes_valid_card_number(self) -> None:
        cleaned, report = default_redactor().text("card 4111 1111 1111 1111 on file")
        assert "4111 1111 1111 1111" not in cleaned
        assert report.counts["credit_card"] == 1

    def test_leaves_invalid_card_like_digits_alone(self) -> None:
        # An order id must not be mangled just for being long. Luhn is the filter.
        cleaned, report = default_redactor().text("order 1234567890123456 shipped")
        assert "1234567890123456" in cleaned
        assert report.counts["credit_card"] == 0

    def test_categories_can_be_narrowed(self) -> None:
        redactor = Redactor(categories=frozenset({"credential"}))
        cleaned, _ = redactor.text("ada@example.com and sk-abcdefghijklmnopqrstuvwxyz01")
        assert "ada@example.com" in cleaned
        assert "sk-abcdefghijklmnopqrstuvwxyz01" not in cleaned


class TestRedactionProperties:
    def test_leaves_clean_text_untouched(self) -> None:
        text = "Summarise this support thread in two sentences."
        cleaned, report = default_redactor().text(text)
        assert cleaned == text
        assert not report.touched

    def test_same_secret_yields_the_same_token(self) -> None:
        # Lets a reviewer see that two occurrences were the same value.
        cleaned, _ = default_redactor().text(
            "sk-abcdefghijklmnopqrstuvwxyz01 and sk-abcdefghijklmnopqrstuvwxyz01"
        )
        tokens = [part for part in cleaned.split() if part.startswith("[REDACTED")]
        assert len(tokens) == 2
        assert tokens[0] == tokens[1]

    def test_different_secrets_yield_different_tokens(self) -> None:
        a, _ = default_redactor().text("sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        b, _ = default_redactor().text("sk-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        assert a != b

    def test_token_does_not_contain_the_secret(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz01"
        cleaned, _ = default_redactor().text(secret)
        assert "abcdefghij" not in cleaned

    def test_is_idempotent(self) -> None:
        once, _ = default_redactor().text("key sk-abcdefghijklmnopqrstuvwxyz01")
        twice, report = default_redactor().text(once)
        assert once == twice
        assert not report.touched

    def test_empty_text(self) -> None:
        cleaned, report = default_redactor().text("")
        assert cleaned == ""
        assert not report.touched


class TestStructuredRedaction:
    def test_walks_nested_values(self) -> None:
        payload = {"outer": {"inner": ["sk-abcdefghijklmnopqrstuvwxyz01"]}}
        cleaned, report = default_redactor().value(payload)
        assert "sk-abcdefghijklmnopqrstuvwxyz01" not in json.dumps(cleaned)
        assert report.touched

    def test_preserves_non_string_types(self) -> None:
        cleaned, _ = default_redactor().value({"n": 1, "f": 1.5, "b": True, "z": None})
        assert cleaned == {"n": 1, "f": 1.5, "b": True, "z": None}

    def test_redacts_tool_call_arguments(self) -> None:
        message = Message(
            role="assistant",
            tool_calls=(ToolCall(name="auth", arguments={"token": "ghp_" + "a" * 36}),),
        )
        cleaned, report = default_redactor().message(message)
        assert "ghp_" not in json.dumps(cleaned.tool_calls[0].arguments)
        assert report.touched

    def test_redacts_a_whole_case_and_marks_it(self) -> None:
        case = make_case(user="my key is sk-abcdefghijklmnopqrstuvwxyz01")
        cleaned, report = default_redactor().case(case)
        assert "sk-abcdefghijklmnopqrstuvwxyz01" not in cleaned.input.messages[-1].content
        assert cleaned.redacted is True
        assert report.touched

    def test_derived_case_id_follows_redacted_content(self) -> None:
        case = make_case(user="key sk-abcdefghijklmnopqrstuvwxyz01")
        cleaned, _ = default_redactor().case(case)
        assert cleaned.case_id == cleaned.input.fingerprint()

    def test_explicit_case_id_survives_redaction(self) -> None:
        from parity.domain.models import Case, ModelRef

        case = Case.create(
            input=make_input("key sk-abcdefghijklmnopqrstuvwxyz01"),
            output=make_output(),
            reference=ModelRef.parse("a:b"),
            case_id="stable-id",
        )
        cleaned, _ = default_redactor().case(case)
        assert cleaned.case_id == "stable-id"

    def test_redacts_output_as_well_as_input(self) -> None:
        case = make_case(output=make_output("token ghp_" + "b" * 36))
        cleaned, _ = default_redactor().case(case)
        assert "ghp_" not in cleaned.output.text


class TestLimits:
    def test_file_size_guard(self, tmp_path: Path) -> None:
        path = tmp_path / "big.jsonl"
        path.write_text("x" * 100, encoding="utf-8")
        assert guard_file_size(path, Limits(max_file_bytes=1000)) == 100
        with pytest.raises(SecurityLimitExceeded, match="max_file_bytes"):
            guard_file_size(path, Limits(max_file_bytes=10))

    def test_record_count_guard(self) -> None:
        guard_record_count(5, Limits(max_records=10))
        with pytest.raises(SecurityLimitExceeded, match="max_records"):
            guard_record_count(11, Limits(max_records=10))

    def test_payload_guard(self) -> None:
        with pytest.raises(SecurityLimitExceeded, match="max_payload_chars"):
            guard_payload("x" * 50, Limits(max_payload_chars=10))

    def test_line_size_guard_counts_bytes_not_characters(self) -> None:
        with pytest.raises(SecurityLimitExceeded, match="max_line_bytes"):
            guard_line_size("é" * 10, Limits(max_line_bytes=15))

    def test_depth_guard_rejects_deep_nesting(self) -> None:
        deep: object = "leaf"
        for _ in range(20):
            deep = {"n": deep}
        guard_depth(deep, Limits(max_json_depth=64))
        with pytest.raises(SecurityLimitExceeded, match="max_json_depth"):
            guard_depth(deep, Limits(max_json_depth=5))

    def test_depth_guard_handles_lists(self) -> None:
        nested: object = [[[["leaf"]]]]
        with pytest.raises(SecurityLimitExceeded):
            guard_depth(nested, Limits(max_json_depth=2))

    def test_harden_permissions_is_safe_on_every_platform(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        path.write_text("x", encoding="utf-8")
        harden_permissions(path)  # no-op on Windows, chmod elsewhere; must not raise
        assert path.read_text(encoding="utf-8") == "x"
