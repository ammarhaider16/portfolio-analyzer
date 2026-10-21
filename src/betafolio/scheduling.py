"""Exchange-session scheduling; callers persist run identifiers and deduplicate."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator


class SchedulePolicy(BaseModel):
    enabled: bool = False
    delay_minutes: int = Field(default=30, ge=0, le=1440)
    timezone: str = "America/New_York"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value


def _runs(now: datetime, delay_minutes: int, timezone: str) -> list[datetime]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Scheduling requires a timezone-aware datetime")
    policy = SchedulePolicy(delay_minutes=delay_minutes, timezone=timezone)
    import exchange_calendars as xcals

    local = now.astimezone(ZoneInfo(policy.timezone))
    start = local.date() - timedelta(days=45)
    end = local.date() + timedelta(days=45)
    calendar = xcals.get_calendar("XNYS", start=start.isoformat(), end=end.isoformat())
    return [
        (calendar.session_close(session).to_pydatetime() + timedelta(minutes=delay_minutes)).astimezone(ZoneInfo(timezone))
        for session in calendar.sessions_in_range(start.isoformat(), end.isoformat())
    ]


def next_session_run(now: datetime, delay_minutes: int = 30, timezone: str = "America/New_York") -> datetime:
    """Next strictly future close plus delay, including early market closes."""
    return next(run for run in _runs(now, delay_minutes, timezone) if run > now)


def latest_eligible_run(now: datetime, delay_minutes: int = 30, timezone: str = "America/New_York") -> datetime:
    """Latest eligible run only, so restart recovery never queues every missed day."""
    return max(run for run in _runs(now, delay_minutes, timezone) if run <= now)
