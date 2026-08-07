"""Clock port.

Injected so that run ids, timestamps, and retry backoff are deterministic under
test. Reaching for ``datetime.now()`` inside the core makes reports impossible
to snapshot-test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware, UTC."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block for a duration. Fakes make this a no-op."""
        ...
