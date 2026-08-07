"""Clock adapters."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta


class SystemClock:
    """Real time. The default everywhere outside tests."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """Deterministic time for tests.

    ``sleep`` advances the clock instead of blocking, so retry-backoff logic is
    exercised at full speed and the elapsed time is still assertable.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._now += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
