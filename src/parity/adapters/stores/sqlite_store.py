"""SQLite baseline store.

For baselines large enough that streaming a JSONL file per lookup stops being
reasonable. Same interface, different trade-off: indexed lookup by ``case_id``
and cheap counts, at the cost of a file you cannot read in a terminal.

Uses WAL mode so a long replay reading the store does not block a concurrent
capture writing to it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path

from pydantic import ValidationError

from parity.domain.models import Case
from parity.errors import StoreError
from parity.security.limits import (
    DEFAULT_LIMITS,
    Limits,
    ensure_secure_dir,
    guard_record_count,
    harden_permissions,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    position    INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT    NOT NULL UNIQUE,
    captured_at TEXT    NOT NULL,
    payload     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_captured_at ON cases (captured_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SCHEMA_VERSION = "1"


class SqliteBaselineStore:
    """Baseline store backed by a single SQLite file."""

    def __init__(self, path: Path | str, *, limits: Limits = DEFAULT_LIMITS) -> None:
        self._path = Path(path)
        self._limits = limits
        ensure_secure_dir(self._path.parent)
        existed = self._path.exists()
        self._conn = sqlite3.connect(self._path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._initialise()
        if not existed:
            harden_permissions(self._path)

    def _initialise(self) -> None:
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (SCHEMA_VERSION,),
            )
        except sqlite3.Error as exc:
            raise StoreError(f"could not initialise SQLite store at {self._path}: {exc}") from exc

    @property
    def location(self) -> str:
        return str(self._path)

    def _decode(self, payload: str, *, case_id: str) -> Case:
        try:
            return Case.model_validate(json.loads(payload))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise StoreError(f"{self._path}: case {case_id} is malformed: {exc}") from exc

    def extend(self, cases: Iterable[Case]) -> int:
        rows = [
            (case.case_id, case.captured_at.isoformat(), case.model_dump_json()) for case in cases
        ]
        if not rows:
            return 0
        try:
            with self._conn:
                cursor = self._conn.executemany(
                    "INSERT INTO cases (case_id, captured_at, payload) VALUES (?, ?, ?) "
                    "ON CONFLICT(case_id) DO NOTHING",
                    rows,
                )
                return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        except sqlite3.Error as exc:
            raise StoreError(f"could not write cases to {self._path}: {exc}") from exc

    def iter_cases(self) -> Iterator[Case]:
        try:
            cursor = self._conn.execute("SELECT case_id, payload FROM cases ORDER BY position ASC")
        except sqlite3.Error as exc:
            raise StoreError(f"could not read cases from {self._path}: {exc}") from exc
        for seen, row in enumerate(cursor, start=1):
            guard_record_count(seen, self._limits)
            yield self._decode(row["payload"], case_id=row["case_id"])

    def get(self, case_id: str) -> Case | None:
        try:
            row = self._conn.execute(
                "SELECT case_id, payload FROM cases WHERE case_id = ?", (case_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError(f"could not read case {case_id}: {exc}") from exc
        if row is None:
            return None
        return self._decode(row["payload"], case_id=row["case_id"])

    def count(self) -> int:
        try:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()
        except sqlite3.Error as exc:
            raise StoreError(f"could not count cases: {exc}") from exc
        return int(row["n"])

    def replace_all(self, cases: Iterable[Case]) -> int:
        """Swap the contents inside a single transaction."""
        rows = [
            (case.case_id, case.captured_at.isoformat(), case.model_dump_json()) for case in cases
        ]
        try:
            with self._conn:
                self._conn.execute("DELETE FROM cases")
                self._conn.execute("DELETE FROM sqlite_sequence WHERE name = 'cases'")
                self._conn.executemany(
                    "INSERT INTO cases (case_id, captured_at, payload) VALUES (?, ?, ?)",
                    rows,
                )
        except sqlite3.Error as exc:
            raise StoreError(f"could not replace cases in {self._path}: {exc}") from exc
        return len(rows)

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            return
