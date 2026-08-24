"""Application-local calendar helpers.

Persisted timestamps stay in UTC.  Calendar cut-offs (deadlines and the
meaning of "today") follow the dashboard's Europe/Rome timezone so a UTC
container does not keep yesterday's records alive until 02:00 local time.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

APP_TIMEZONE = ZoneInfo("Europe/Rome")


def local_today(now: datetime | None = None) -> date:
    """Return the current Europe/Rome calendar date.

    ``now`` exists for deterministic boundary tests.  A naive value is treated
    as UTC because all internal timestamps are persisted in UTC.
    """
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(APP_TIMEZONE).date()
