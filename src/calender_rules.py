"""Settlement timing: working days, cutoffs, and per-method cycles.

Three separate mechanisms push a settlement later than a naive "+2 days":

1. **Working days.** Weekends do not count, so a Thursday capture settles Monday --
   four calendar days, not two.
2. **Bank holidays.** Same effect, on an irregular calendar you must supply.
3. **The daily cutoff.** A payment captured after the cutoff belongs to the *next*
   day's batch, so two payments an hour apart can settle two days apart.

Every constant below is an assumption, not a fact. They are gathered here, in one
file, precisely so that replacing them with real values is a single edit rather
than a hunt through the forecast code.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

SATURDAY, SUNDAY = 5, 6
WEEKEND = frozenset({SATURDAY, SUNDAY})

# --- PLACEHOLDER bank-holiday calendar -------------------------------------
# Indian bank holidays vary by state and are published annually by the RBI.
# Replace this set with the real list for the years you care about; the code
# does not care how long it is. Two entries are enough to exercise the logic.
BANK_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2026, 5, 1),  # May Day / Maharashtra Day -- a Friday in 2026, so it bites
        date(2026, 8, 15),  # Independence Day -- a Saturday in 2026, so it changes nothing
        date(2026, 10, 2),  # Gandhi Jayanti -- a Friday
    }
)

# Payments captured after this time roll into the next day's batch.
DEFAULT_CUTOFF = time(18, 0)

# --- ASSUMPTION: settlement cycle in working days, per payment method.
# Razorpay documents T+2 as standard for domestic card payments and T+1 for UPI.
# Two cycles rather than one is what makes the near end of the forecast a gradient
# instead of a cliff: a sale today reaches the bank tomorrow if it was UPI and the
# day after if it was a card. Verify against your own dashboard before quoting
# these numbers to anyone.
METHOD_CYCLE_DAYS: dict[str, int] = {
    "card": 2,
    "netbanking": 2,
    "wallet": 2,
    "upi": 1,
}

DEFAULT_CYCLE_DAYS = 2


def is_working_day(day: date, holidays: frozenset[date] = BANK_HOLIDAYS) -> bool:
    """A day the bank moves money on."""
    return day.weekday() not in WEEKEND and day not in holidays


def next_working_day(day: date, holidays: frozenset[date] = BANK_HOLIDAYS) -> date:
    """`day` itself if it is a working day, else the first working day after it."""
    while not is_working_day(day, holidays):
        day += timedelta(days=1)
    return day


def add_working_days(
    day: date, n: int, holidays: frozenset[date] = BANK_HOLIDAYS
) -> date:
    """Advance `n` working days from `day`.

    Counting starts the day *after* `day`, so `add_working_days(friday, 1)` is
    Monday. `n == 0` returns `day` unchanged even if it is a weekend -- callers
    that need "the batch day" should pass through `next_working_day` themselves.
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    for _ in range(n):
        day = next_working_day(day + timedelta(days=1), holidays)
    return day


def cycle_days(method: str) -> int:
    """Settlement cycle for a payment method, in working days."""
    return METHOD_CYCLE_DAYS.get(method, DEFAULT_CYCLE_DAYS)


def batch_day(captured_at: datetime, cutoff: time = DEFAULT_CUTOFF) -> date:
    """The 'T' in T+N for a capture: which day's batch this payment falls into.

    A capture at or after the cutoff belongs to the next calendar day's batch.
    """
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
    """Count of working days strictly after `start` up to and including `end`.

    Used to explain *why* money landed on the day it did -- "four calendar days,
    but only two working ones" -- which is the kind of thing the agent needs to say
    when a merchant asks where their Friday takings went.
    """
    if end < start:
        raise ValueError("end must not precede start")
    count, day = 0, start
    while day < end:
        day += timedelta(days=1)
        if is_working_day(day, holidays):
            count += 1
    return count
