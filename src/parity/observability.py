"""Structured logging.

Off by default. A CLI that chatters is a CLI people pipe to ``/dev/null``, and
the report is the output that matters. ``--verbose`` turns it on; ``--log-json``
makes it machine-readable for a CI log collector.

The rule this module exists to enforce: **payloads and credentials never reach a
log.** ``SECURITY.md`` promises it, so something has to make it true rather than
hope every future call site remembers. Two mechanisms:

* Log records carry identifiers, counts, durations, and verdicts — never message
  content, never model output.
* :class:`SecretScrubber` runs the redaction rules over every formatted record as
  a last line of defence, so a careless ``logger.info(f"... {response}")`` added
  later leaks a redaction token instead of a key.

The scrubber is defence in depth, not permission to log payloads.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from parity.security.redaction import Redactor, default_redactor

LOGGER_NAME = "parity"

#: Fields the JSON formatter emits for every record, in order.
_BASE_FIELDS = ("timestamp", "level", "logger", "message")

#: Attributes the stdlib puts on every LogRecord. Anything else was supplied by
#: the call site and is treated as structured context.
_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class SecretScrubber(logging.Filter):
    """Runs redaction over every formatted record.

    A filter rather than a formatter so it applies regardless of which handler
    or format is configured, including one a user adds themselves.
    """

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor or default_redactor()

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned, report = self._redactor.text(message)
        if report.touched:
            # Collapse args into the already-formatted message; re-formatting
            # would reintroduce the raw values.
            record.msg = cleaned
            record.args = ()
        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_ATTRS or not isinstance(value, str):
                continue
            scrubbed, sub = self._redactor.text(value)
            if sub.touched:
                record.__dict__[key] = scrubbed
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for a log collector."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """Compact single line, with structured context appended as key=value."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.gmtime(record.created))
        context = " ".join(
            f"{key}={value}" for key, value in record.__dict__.items() if key not in _STANDARD_ATTRS
        )
        line = f"{stamp} {record.levelname.lower():<7} {record.getMessage()}"
        if context:
            line = f"{line} {context}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(
    *,
    verbose: bool = False,
    json_format: bool = False,
    stream: Any = None,
    redactor: Redactor | None = None,
) -> logging.Logger:
    """Configure Parity's logger. Idempotent.

    Always writes to stderr, never stdout: with a machine-readable report
    format, stdout belongs to the document.
    """
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter() if json_format else HumanFormatter())
    handler.addFilter(SecretScrubber(redactor))

    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.WARNING)
    # Parity is also a library. Never hijack the root logger of a host app.
    logger.propagate = False
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Logger for a submodule, e.g. ``get_logger("replay")``."""
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")


@contextmanager
def log_duration(logger: logging.Logger, event: str, **context: Any) -> Iterator[None]:
    """Log how long a block took, at debug level.

    Context must be identifiers and counts. Never pass a payload.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        logger.debug(
            event, extra={**context, "duration_ms": int((time.perf_counter() - started) * 1000)}
        )
