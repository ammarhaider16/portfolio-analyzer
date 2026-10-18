from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from portfolio_analyzer.scheduling import SchedulePolicy, latest_eligible_run, next_session_run


def ny(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("America/New_York"))


def test_opt_in():
    assert SchedulePolicy().enabled is False


def test_holiday_and_latest_only():
    now = ny("2026-07-03T12:00:00")
    assert next_session_run(now) == ny("2026-07-06T16:30:00")
    assert latest_eligible_run(now) == ny("2026-07-02T16:30:00")


def test_early_close_and_exact_boundary():
    now = ny("2026-11-27T12:00:00")
    assert next_session_run(now) == ny("2026-11-27T13:30:00")
    boundary = ny("2026-11-27T13:30:00")
    assert latest_eligible_run(boundary) == boundary
    assert next_session_run(boundary) == ny("2026-11-30T16:30:00")


def test_dst_and_display_timezone():
    before = next_session_run(ny("2026-03-06T12:00:00"), timezone="UTC")
    after = next_session_run(ny("2026-03-09T12:00:00"), timezone="UTC")
    assert before.hour == 21 and after.hour == 20


def test_naive_time_rejected():
    with pytest.raises(ValueError):
        next_session_run(datetime(2026, 1, 1))
