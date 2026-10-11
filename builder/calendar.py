from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .models import FoundationError


def iso_date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise FoundationError("Date must be an explicit ISO YYYY-MM-DD value")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FoundationError("Date is not a real calendar day") from exc


@dataclass(frozen=True)
class BusinessCalendar:
    weekdays: frozenset[int]
    holidays: frozenset[date]

    def __post_init__(self):
        if not self.weekdays or any(type(day) is not int or not 0 <= day <= 6 for day in self.weekdays):
            raise FoundationError("A nonempty explicit set of business weekdays is required")

    @classmethod
    def from_policy(cls, policy):
        try:
            return cls(frozenset(policy.facts["business_weekdays"].value),
                       frozenset(iso_date(value) for value in policy.facts["holidays"].value))
        except KeyError as exc:
            raise FoundationError("Calendar has unresolved source facts") from exc

    def is_business_day(self, day: date) -> bool:
        return day.weekday() in self.weekdays and day not in self.holidays

    def add_business_days(self, today: date, count: int) -> date:
        if type(count) is not int or not 0 <= count <= 3660:
            raise FoundationError("Business-day offset must be an integer in the local resource bound 0..3660")
        day, remaining = today, count
        while remaining:
            day += timedelta(days=1)
            if self.is_business_day(day):
                remaining -= 1
        return day

    def next_business_day(self, day: date) -> date:
        return self.add_business_days(day, 1)

    def on_or_after(self, day: date) -> date:
        while not self.is_business_day(day):
            day += timedelta(days=1)
        return day

    def deadline(self, now: str, days_late: int, bands: list[dict]) -> date:
        if type(days_late) is not int:
            raise FoundationError("days_late must be an integer")
        matches = [band for band in bands if band["min_days_late"] <= days_late <= band["max_days_late"]]
        if len(matches) != 1:
            raise FoundationError("Delinquency bands are missing or overlap")
        try:
            today = datetime.fromisoformat(now).date()
        except (TypeError, ValueError) as exc:
            raise FoundationError("Explicit call now is required; system time is never substituted") from exc
        return self.add_business_days(today, matches[0]["deadline_business_days"])

    def validate_promise_date(self, proposed: str, now: str, deadline: date) -> date:
        day = iso_date(proposed)
        try:
            today = datetime.fromisoformat(now).date()
        except (TypeError, ValueError) as exc:
            raise FoundationError("Invalid call now") from exc
        if not today <= day <= deadline or not self.is_business_day(day):
            raise FoundationError("Promise date is outside the allowed business-day interval")
        return day

    def callback_allowed(self, proposed_date: str, proposed_time: str, now: str, window: dict) -> bool:
        day = iso_date(proposed_date)
        if not re.fullmatch(r"\d{2}:\d{2}", proposed_time):
            raise FoundationError("Callback time must be HH:MM")
        try:
            current = datetime.fromisoformat(now)
            target = datetime.combine(day, datetime.strptime(proposed_time, "%H:%M").time(), tzinfo=current.tzinfo)
        except (TypeError, ValueError) as exc:
            raise FoundationError("Invalid callback time or call now") from exc
        return (day in {current.date(), self.next_business_day(current.date())}
                and window["start"] <= proposed_time <= window["end"] and target > current)


def weekday_in_calendar_week(now: str, weekday: int, following_week: bool) -> date:
    if type(weekday) is not int or not 0 <= weekday <= 6:
        raise FoundationError("Weekday must be 0..6")
    today = datetime.fromisoformat(now).date()
    monday = today - timedelta(days=today.weekday())
    return monday + timedelta(days=weekday + (7 if following_week else 0))
