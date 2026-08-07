"""Logging behaviour, and the promise that payloads never reach a log.

``SECURITY.md`` claims credentials are never logged. These tests are what make
that claim true rather than aspirational: the scrubber is exercised against the
same credential shapes the redaction layer handles, so a careless log call added
later leaks a redaction token instead of a key.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from parity.observability import (
    LOGGER_NAME,
    HumanFormatter,
    JsonFormatter,
    SecretScrubber,
    configure_logging,
    get_logger,
    log_duration,
)


@pytest.fixture(autouse=True)
def reset_logging():
    """Leave the global logger as we found it."""
    yield
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.NOTSET)


def capture(**kwargs: object) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = configure_logging(stream=stream, **kwargs)  # type: ignore[arg-type]
    return logger, stream


class TestConfiguration:
    def test_quiet_by_default(self) -> None:
        logger, stream = capture()
        logger.info("routine detail")
        assert stream.getvalue() == ""

    def test_verbose_enables_debug(self) -> None:
        logger, stream = capture(verbose=True)
        logger.debug("a detail")
        assert "a detail" in stream.getvalue()

    def test_warnings_surface_even_when_quiet(self) -> None:
        logger, stream = capture()
        logger.warning("something is off")
        assert "something is off" in stream.getvalue()

    def test_is_idempotent(self) -> None:
        # Repeated configuration must not stack handlers and duplicate lines.
        stream = io.StringIO()
        for _ in range(3):
            logger = configure_logging(verbose=True, stream=stream)
        logger.warning("once")
        assert stream.getvalue().count("once") == 1

    def test_does_not_hijack_the_root_logger(self) -> None:
        # Parity is a library as well as a CLI.
        logger, _ = capture(verbose=True)
        assert logger.propagate is False

    def test_defaults_to_stderr_not_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        # stdout belongs to the report when a machine-readable format is used.
        logger = configure_logging(verbose=True)
        logger.warning("diagnostic")
        captured = capsys.readouterr()
        assert "diagnostic" in captured.err
        assert captured.out == ""

    def test_get_logger_namespaces(self) -> None:
        assert get_logger("replay").name == "parity.replay"
        assert get_logger().name == "parity"


class TestSecretScrubbing:
    @pytest.mark.parametrize(
        "secret",
        [
            "sk-abcdefghijklmnopqrstuvwxyz012345",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        ],
    )
    def test_credentials_never_reach_the_stream(self, secret: str) -> None:
        logger, stream = capture(verbose=True)
        logger.warning("request failed with key %s", secret)
        output = stream.getvalue()
        assert secret not in output
        assert "REDACTED" in output

    def test_scrubs_structured_context_too(self) -> None:
        logger, stream = capture(verbose=True)
        logger.warning("failed", extra={"detail": "token ghp_" + "a" * 36})
        assert "ghp_aaa" not in stream.getvalue()

    def test_leaves_ordinary_messages_untouched(self) -> None:
        logger, stream = capture(verbose=True)
        logger.info("replay started")
        assert "replay started" in stream.getvalue()
        assert "REDACTED" not in stream.getvalue()

    def test_scrubber_returns_true_so_records_still_emit(self) -> None:
        record = logging.LogRecord("parity", logging.INFO, __file__, 1, "plain message", None, None)
        assert SecretScrubber().filter(record) is True


class TestFormatters:
    def build_record(self, **extra: object) -> logging.LogRecord:
        record = logging.LogRecord(
            "parity.replay", logging.INFO, __file__, 10, "replay started", None, None
        )
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_json_formatter_emits_one_object(self) -> None:
        payload = json.loads(JsonFormatter().format(self.build_record(cases=5)))
        assert payload["message"] == "replay started"
        assert payload["level"] == "info"
        assert payload["logger"] == "parity.replay"
        assert payload["cases"] == 5
        assert payload["timestamp"].endswith("Z")

    def test_json_formatter_survives_unserialisable_context(self) -> None:
        payload = json.loads(JsonFormatter().format(self.build_record(obj=object())))
        assert "obj" in payload

    def test_human_formatter_appends_context(self) -> None:
        line = HumanFormatter().format(self.build_record(cases=5, candidate="fake:m"))
        assert "replay started" in line
        assert "cases=5" in line
        assert "candidate=fake:m" in line

    def test_json_formatter_includes_exceptions(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = self.build_record()
            record.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(record))
        assert "ValueError" in payload["exception"]


def test_log_duration_reports_elapsed_time() -> None:
    logger, stream = capture(verbose=True, json_format=True)
    with log_duration(logger, "did a thing", case_id="abc"):
        pass
    payload = json.loads(stream.getvalue().strip())
    assert payload["message"] == "did a thing"
    assert payload["case_id"] == "abc"
    assert payload["duration_ms"] >= 0


def test_log_duration_still_logs_when_the_block_raises() -> None:
    logger, stream = capture(verbose=True)
    with pytest.raises(RuntimeError), log_duration(logger, "failed thing"):
        raise RuntimeError("boom")
    assert "failed thing" in stream.getvalue()
