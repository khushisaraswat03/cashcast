"""Settlement timing: working days, cutoffs, and per-method cycles.

Three things push a settlement past a naive "+2 days" and they compound: working
days (a Thursday capture settles Monday), bank holidays, and the daily cutoff --
two payments an hour apart can settle two days apart.

Every constant here is an assumption, kept in one file so replacing them is a
single edit.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

SATURDAY, SUNDAY = 5, 6
WEEKEND = frozenset({SATURDAY, SUNDAY})

# PLACEHOLDER. Indian bank holidays vary by state and are published annually by
# the RBI; replace with the real list. Three entries exercise the logic.
BANK_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2026, 5, 1),  # a Friday in 2026, so it bites
        date(2026, 8, 15),  # a Saturday, so it changes nothing
        date(2026, 10, 2),  # a Friday
    }
)

# Payments captured after this time roll into the next day's batch.
DEFAULT_CUTOFF = time(18, 0)

# ASSUMPTION: T+2 for domestic cards, T+1 for UPI. Two cycles rather than one is
# what makes the near end of the forecast a gradient instead of a cliff.
METHOD_CYCLE_DAYS: dict[str, int] = {
    "card": 2,
    "netbanking": 2,
    "wallet": 2,
    "upi": 1,
}

DEFAULT_CYCLE_DAYS = 2


def is_working_day(day: date, holidays: frozenset[date] = BANK_HOLIDAYS) -> bool:
    return day.weekday() not in WEEKEND and day not in holidays


def next_working_day(day: date, holidays: frozenset[date] = BANK_HOLIDAYS) -> date:
    """`day` itself if it is a working day, else the first working day after it."""
    while not is_working_day(day, holidays):
        day += timedelta(days=1)
    return day


def add_working_days(
    day: date, n: int, holidays: frozenset[date] = BANK_HOLIDAYS
) -> date:
    """Advance `n` working days. Counting starts the day *after* `day`, so
    `add_working_days(friday, 1)` is Monday. `n == 0` returns `day` unchanged."""
    if n < 0:
        raise ValueError("n must be >= 0")
    for _ in range(n):
        day = next_working_day(day + timedelta(days=1), holidays)
    return day


def cycle_days(method: str) -> int:
    return METHOD_CYCLE_DAYS.get(method, DEFAULT_CYCLE_DAYS)


def batch_day(captured_at: datetime, cutoff: time = DEFAULT_CUTOFF) -> date:
    """The 'T' in T+N. A capture at or after the cutoff joins the next day."""
    day = captured_at.date()
    if captured_at.time() >= cutoff:
        day += timedelta(days=1)
    return day


def settlement_date(
    captured_at: datetime,
    method: str,
    *,
    cutoff: time = DEFAULT_CUTOFF,
    holidays: frozenset[date] = BANK_HOLIDAYS,
    cycle_override: int | None = None,
) -> date:
    """The working day this capture is expected to reach the bank."""
    n = cycle_override if cycle_override is not None else cycle_days(method)
    return add_working_days(batch_day(captured_at, cutoff), n, holidays)


def working_days_between(
    start: date, end: date, holidays: frozenset[date] = BANK_HOLIDAYS
) -> int:
    """Working days strictly after `start`, up to and including `end`.

    Used to explain why money landed when it did -- "four calendar days, but only
    two working ones".
    """
    if end < start:
        raise ValueError("end must not precede start")
    count, day = 0, start
    while day < end:
        day += timedelta(days=1)
        if is_working_day(day, holidays):
            count += 1
    return count
