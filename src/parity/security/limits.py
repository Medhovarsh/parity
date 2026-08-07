"""Resource limits and file hardening for untrusted input.

A baseline file is untrusted: it may have been produced by a colleague, checked
out from a branch, or corrupted. Every limit here fails closed by raising
:class:`~parity.errors.SecurityLimitExceeded` *before* the expensive allocation,
rather than discovering the problem by running out of memory.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from parity.errors import SecurityLimitExceeded

#: Owner read/write only. Baselines can contain payloads that survived redaction.
SECURE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
SECURE_DIR_MODE = stat.S_IRWXU


class Limits(BaseModel):
    """Bounds applied when reading anything Parity did not just produce."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_file_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    """Largest baseline or import file that will be opened. Default 512 MiB."""

    max_records: int = Field(default=1_000_000, gt=0)
    """Largest number of cases that will be loaded from one source."""

    max_payload_chars: int = Field(default=4 * 1024 * 1024, gt=0)
    """Largest single message or output. Default 4 MiB of characters."""

    max_json_depth: int = Field(default=64, gt=0)
    """Deepest nesting accepted in parsed JSON, to bound recursive walks."""

    max_line_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    """Largest single JSONL line. Default 64 MiB."""


DEFAULT_LIMITS = Limits()


def guard_file_size(path: Path, limits: Limits = DEFAULT_LIMITS) -> int:
    """Reject an oversized file before opening it. Returns its size in bytes."""
    size = path.stat().st_size
    if size > limits.max_file_bytes:
        raise SecurityLimitExceeded("max_file_bytes", limit=limits.max_file_bytes, actual=size)
    return size


def guard_line_size(line: str | bytes, limits: Limits = DEFAULT_LIMITS) -> None:
    length = len(line.encode("utf-8")) if isinstance(line, str) else len(line)
    if length > limits.max_line_bytes:
        raise SecurityLimitExceeded("max_line_bytes", limit=limits.max_line_bytes, actual=length)


def guard_record_count(count: int, limits: Limits = DEFAULT_LIMITS) -> None:
    if count > limits.max_records:
        raise SecurityLimitExceeded("max_records", limit=limits.max_records, actual=count)


def guard_payload(text: str, limits: Limits = DEFAULT_LIMITS, *, what: str = "payload") -> str:
    if len(text) > limits.max_payload_chars:
        raise SecurityLimitExceeded(
            "max_payload_chars", limit=limits.max_payload_chars, actual=f"{len(text)} ({what})"
        )
    return text


def guard_depth(value: Any, limits: Limits = DEFAULT_LIMITS, *, _depth: int = 0) -> None:
    """Walk a parsed JSON value, rejecting nesting deeper than the limit.

    ``json.loads`` already caps recursion, but its limit is the interpreter's and
    is far higher than anything a legitimate model output needs.
    """
    if _depth > limits.max_json_depth:
        raise SecurityLimitExceeded("max_json_depth", limit=limits.max_json_depth, actual=_depth)
    if isinstance(value, dict):
        for item in value.values():
            guard_depth(item, limits, _depth=_depth + 1)
    elif isinstance(value, list):
        for item in value:
            guard_depth(item, limits, _depth=_depth + 1)


def harden_permissions(path: Path) -> None:
    """Restrict a file or directory to its owner.

    POSIX only. On Windows the object inherits the parent directory's ACL and
    ``chmod`` cannot express the same intent, so this is a documented no-op
    there — see ``SECURITY.md``, which tells users to keep ``.parity/`` out of
    shared locations.
    """
    if os.name == "nt":
        return
    try:
        mode = SECURE_DIR_MODE if path.is_dir() else SECURE_FILE_MODE
        os.chmod(path, mode)  # noqa: PTH101 - Path.chmod has the same effect; kept explicit
    except OSError:
        # A filesystem that does not support permissions (a network mount, a
        # container bind) must not break a capture. The data is still written.
        return


def ensure_secure_dir(path: Path) -> Path:
    """Create a directory if needed and restrict it to its owner."""
    path.mkdir(parents=True, exist_ok=True)
    harden_permissions(path)
    return path
