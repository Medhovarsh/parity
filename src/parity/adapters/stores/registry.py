"""Store selection by name."""

from __future__ import annotations

from pathlib import Path

from parity.adapters.stores.jsonl_store import JsonlBaselineStore
from parity.adapters.stores.sqlite_store import SqliteBaselineStore
from parity.errors import ConfigError
from parity.ports.store import BaselineStore
from parity.security.limits import DEFAULT_LIMITS, Limits

STORE_KINDS = ("jsonl", "sqlite")


def open_baseline_store(
    kind: str,
    path: Path | str,
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> BaselineStore:
    """Open a baseline store of the named kind."""
    normalised = kind.strip().lower()
    if normalised == "jsonl":
        return JsonlBaselineStore(path, limits=limits)
    if normalised == "sqlite":
        return SqliteBaselineStore(path, limits=limits)
    raise ConfigError(
        f"unknown baseline store kind {kind!r}; expected one of {', '.join(STORE_KINDS)}"
    )
