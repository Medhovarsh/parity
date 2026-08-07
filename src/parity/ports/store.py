"""Storage ports.

Two stores, kept separate because their access patterns differ. Baselines are
append-heavy and streamed; run reports are written once and read whole.

``iter_cases`` returns an iterator rather than a list on purpose: a baseline can
outgrow memory, and the replay runner consumes it lazily.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Protocol, runtime_checkable

from parity.domain.models import Case, RunReport


@runtime_checkable
class BaselineStore(Protocol):
    """Persistent collection of captured cases."""

    @property
    def location(self) -> str:
        """Human-readable description of where this store lives."""
        ...

    def extend(self, cases: Iterable[Case]) -> int:
        """Append cases, skipping any whose ``case_id`` is already present.

        Returns the number actually written. Deduplication is the store's job
        because only the store knows what it already holds.
        """
        ...

    def iter_cases(self) -> Iterator[Case]:
        """Stream every stored case in insertion order."""
        ...

    def get(self, case_id: str) -> Case | None: ...

    def count(self) -> int: ...

    def replace_all(self, cases: Iterable[Case]) -> int:
        """Atomically replace the entire contents.

        Used by ``parity baseline redact``. Must not leave a partially written
        store behind if it fails.
        """
        ...

    def close(self) -> None: ...


@runtime_checkable
class RunStore(Protocol):
    """Persistent collection of run reports."""

    def save(self, report: RunReport) -> str:
        """Persist a report. Returns the location it was written to."""
        ...

    def load(self, run_id: str) -> RunReport | None: ...

    def list_run_ids(self) -> tuple[str, ...]:
        """Newest first."""
        ...
