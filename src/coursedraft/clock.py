from __future__ import annotations

from datetime import UTC, datetime, timedelta


class Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock(Clock):
    """A clock the tests and the seed script move by hand, so timestamps are reproducible."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now
