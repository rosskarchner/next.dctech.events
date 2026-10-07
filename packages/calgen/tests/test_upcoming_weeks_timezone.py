"""get_upcoming_weeks must use the site's local date, not the system clock's.

A Lambda or CodeBuild host runs in UTC, so between 8 PM and midnight Eastern
the system date is already tomorrow. Every other "today" in calgen uses
local_tz; this one used date.today() and could start the week set a day early.
"""
from datetime import date, datetime

import pytz

from calgen.routes import common


def _freeze_now(monkeypatch, utc_moment):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return utc_moment.astimezone(tz) if tz else utc_moment.replace(tzinfo=None)

    class FrozenDate(date):
        @classmethod
        def today(cls):
            # What date.today() returns on a UTC host.
            return utc_moment.date()

    monkeypatch.setattr(common, 'datetime', FrozenDatetime)
    monkeypatch.setattr(common, 'date', FrozenDate)
    monkeypatch.setattr(common, 'get_events', lambda: [])


def test_week_set_starts_from_the_local_date_not_the_utc_date(monkeypatch):
    # Sunday 2026-10-11 22:00 Eastern is already Monday 2026-10-12 UTC, which
    # is the next ISO week. Locally it is still the week of 2026-10-05.
    _freeze_now(monkeypatch, pytz.UTC.localize(datetime(2026, 10, 12, 2, 0)))
    weeks = common.get_upcoming_weeks(1)
    assert weeks == ['2026-W41']
