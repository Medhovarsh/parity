"""JSONL baseline store.

One case per line, append-only. The default because it diffs, greps, and streams
— a baseline is evidence, and evidence you can read in a terminal gets reviewed.

Durability approach: appends are flushed and fsynced, and ``replace_all`` writes
to a sibling temp file then atomically renames over the original. A crash leaves
either the old file or the new one, never a half-written mix.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

from pydantic import ValidationError

from parity.domain.models import Case
from parity.errors import StoreError
from parity.security.limits import (
    DEFAULT_LIMITS,
    Limits,
    ensure_secure_dir,
    guard_file_size,
    guard_line_size,
    guard_record_count,
    harden_permissions,
)


class JsonlBaselineStore:
    """Append-only newline-delimited JSON store of cases."""

    def __init__(self, path: Path | str, *, limits: Limits = DEFAULT_LIMITS) -> None:
        self._path = Path(path)
        self._limits = limits
        self._ids: set[str] | None = None
        ensure_secure_dir(self._path.parent)

    @property
    def location(self) -> str:
        return str(self._path)

    @property
    def path(self) -> Path:
        return self._path

    # -- reading ---------------------------------------------------------

    def _iter_raw(self) -> Iterator[tuple[int, str]]:
        if not self._path.exists():
            return
        guard_file_size(self._path, self._limits)
        with self._path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                guard_line_size(stripped, self._limits)
                yield number, stripped

    def iter_cases(self) -> Iterator[Case]:
        for seen, (number, line) in enumerate(self._iter_raw(), start=1):
            guard_record_count(seen, self._limits)
            try:
                yield Case.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise StoreError(f"{self._path}:{number}: malformed case record: {exc}") from exc

    def _load_ids(self) -> set[str]:
        if self._ids is None:
            # Parsing only the id keeps reopening a large baseline cheap.
            ids: set[str] = set()
            for number, line in self._iter_raw():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise StoreError(f"{self._path}:{number}: invalid JSON: {exc}") from exc
                case_id = record.get("case_id")
                if not isinstance(case_id, str):
                    raise StoreError(f"{self._path}:{number}: record has no string 'case_id'")
                ids.add(case_id)
            self._ids = ids
        return self._ids

    def get(self, case_id: str) -> Case | None:
        for case in self.iter_cases():
            if case.case_id == case_id:
                return case
        return None

    def count(self) -> int:
        return len(self._load_ids())

    # -- writing ---------------------------------------------------------

    def extend(self, cases: Iterable[Case]) -> int:
        known = self._load_ids()
        written = 0
        pending: list[str] = []
        for case in cases:
            if case.case_id in known:
                continue
            known.add(case.case_id)
            pending.append(case.model_dump_json())
            written += 1

        if not pending:
            return 0

        is_new = not self._path.exists()
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(pending) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if is_new:
            harden_permissions(self._path)
        return written

    def replace_all(self, cases: Iterable[Case]) -> int:
        """Rewrite the store atomically."""
        directory = self._path.parent
        ensure_secure_dir(directory)
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed explicitly below
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        written = 0
        try:
            with handle:
                for case in cases:
                    handle.write(case.model_dump_json() + "\n")
                    written += 1
                handle.flush()
                os.fsync(handle.fileno())
            harden_permissions(temp_path)
            temp_path.replace(self._path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        self._ids = None
        return written

    def close(self) -> None:
        # Nothing is held open between calls; present to satisfy the port.
        self._ids = None
