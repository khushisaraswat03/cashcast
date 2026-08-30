"""The forecast: a path, not a number.

The certain layer only. Start from the balance the merchant can see today, step
forward one day at a time, and on each day add what is already known to be arriving
and subtract what is already known to be leaving. No prediction anywhere -- every
figure comes from an event that already exists.

**This is deliberately not a forecast, and the code says so.** Card money settles in
two working days, so only a day or two of sales is ever in flight, while rent,
salaries and supplier bills are committed weeks ahead. The certain layer therefore
sees two days of income against fourteen days of expenditure and trends sharply
negative. What it honestly answers is *"what happens if I never sell anything
again, starting now"* -- a real question, and a useful worst case, but not a
prediction. `Forecast.scenario` names it.

Two structural choices, both to stop later work from becoming a rewrite:

**Every day carries its receipts.** A `DayProjection` holds the actual events that
moved its cash, not just a total. That is what makes "day 57 is the trough because
the supplier bill and the tax payment landed the same week" a read rather than a
second search, and the merchant report promises exactly that reason.

**The empty slots exist from day one.** `estimated_in`, `estimated_out`, `band_low`
and `band_high` are here and unused. Bucket 2 fills the first two on Sunday and
Bucket 3 the last two on Tuesday, by filling fields rather than reshaping the
object -- so the report, the backtest and the agent are written once.

Sign convention, stated once because mixing it is exactly the class of bug this
project claims to eliminate: **inflows are positive, outflows are negative.** A day's
net movement is a plain sum, never a subtraction.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Sequence

from .estimate import Estimator
from .events import AnyEvent, Outflow
from .money import Paise, fmt_inr
from .world import EventStore, KnownWorld, world_as_of

DEFAULT_HORIZON = 14


class Flag(str, Enum):
    """Why a day is worth the merchant's attention."""

    #: The lowest point in the window. Always exactly one, flagged even when
    #: comfortable -- knowing where the bottom is is useful either way.
    TROUGH = "trough"

    #: The projected balance itself falls below what is owed.
    BREACH = "breach"

    #: The projection clears the floor but the *bottom of the uncertainty band*
    #: does not. A warning no point forecast can produce, and the reason Bucket 3
    #: is not decoration. Never fires until bands exist.
    AT_RISK = "at_risk"


class Scenario(str, Enum):
    """What question a forecast actually answers. Printed, not implied."""

    #: Certain layer only. "What if sales stopped today?"
    SALES_STOP = "sales-stop (certain layer only)"
    #: Certain + estimated. An actual forecast.
    FORECAST = "forecast"


# --------------------------------------------------------------------------
# One day
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DayProjection:
    """One day of the path, with its workings.

    `certain_out` and `estimated_out` are **negative**. `net` is a sum.
    """

    date: dt.date
    horizon: int
    opening: Paise

    certain_in: Paise = 0
    certain_out: Paise = 0
    estimated_in: Paise = 0
    estimated_out: Paise = 0

    band_low: Paise | None = None
    band_high: Paise | None = None

    #: The events that moved this day's cash. The receipts.
    movements: tuple[AnyEvent, ...] = ()
    flags: frozenset[Flag] = field(default_factory=frozenset)

    @property
    def net(self) -> Paise:
        return self.certain_in + self.certain_out + self.estimated_in + self.estimated_out

    @property
    def closing(self) -> Paise:
        return self.opening + self.net

    @property
    def certain_share(self) -> float:
        """Fraction of the day's gross movement that is known rather than estimated.

        The number that stops an accuracy claim being unearned: at horizon 1 it is
        1.0 and the forecast is nearly exact; at horizon 14 it falls, and the error
        should be allowed to rise with it. A day with no movement is trivially
        certain.
        """
        known = abs(self.certain_in) + abs(self.certain_out)
        guessed = abs(self.estimated_in) + abs(self.estimated_out)
        return 1.0 if known + guessed == 0 else known / (known + guessed)

    def largest_movements(self, n: int = 2) -> tuple[AnyEvent, ...]:
        """The events that best explain this day, biggest absolute effect first."""
        return tuple(
            sorted(self.movements, key=lambda e: abs(e.cash_delta), reverse=True)[:n]
        )

    def reason(self) -> str:
        """Why this day looks the way it does, from its own receipts.

        Names only the movements that actually explain it -- biggest first, until
        80% of the day's gross flow is accounted for, and never more than three.
        Listing the other nine payments of ₹300 each is noise, and a report nobody
        reads explains nothing.

        The cap matters as much as the 80% rule: on a day made up of twenty similar
        small settlements no single one dominates, so the share test alone names
        almost all of them. When that happens the honest summary is "a lot of
        ordinary settlements", which is what the "+N smaller" tail says.
        """
        if not self.movements:
            return "no movement; the balance is carried from the day before"
        gross = sum(abs(e.cash_delta) for e in self.movements)
        parts, covered = [], 0
        for e in self.largest_movements(len(self.movements)):
            if parts and (covered >= 0.8 * gross or len(parts) >= 3):
                break
            kind = getattr(e, "kind", None)
            label = kind.value if kind is not None else type(e).__name__.lower()
            parts.append(f"{label} {fmt_inr(abs(e.cash_delta))}")
            covered += abs(e.cash_delta)
        rest = len(self.movements) - len(parts)
        if rest > 0:
            parts.append(f"+{rest} smaller")
        return ", ".join(parts)


# --------------------------------------------------------------------------
# The path
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Forecast:
    """A projected path with its worst moments named.

    The merchant's question is "will I have enough on day X". The answer is this
    whole object, not `at(X)` -- because the balance can clear the target day and
    still fall through the floor on the way there, and reporting only the endpoint
    would hide that.
    """

    as_of: dt.date
    scenario: Scenario
    opening: Paise
    floor: Paise
    days: tuple[DayProjection, ...]

    # -- lookups -----------------------------------------------------------

    def at(self, horizon: int) -> DayProjection:
        for d in self.days:
            if d.horizon == horizon:
                return d
        raise ValueError(f"horizon {horizon} outside 1..{len(self.days)}")

    def on(self, day: dt.date) -> DayProjection:
        for d in self.days:
            if d.date == day:
                return d
        raise ValueError(f"{day} is not in this forecast")

    def closing_series(self) -> dict[dt.date, Paise]:
        """Day -> projected closing balance. What the backtest scores, and what
        `diff_daily` compares when the leak test runs."""
        return {d.date: d.closing for d in self.days}

    # -- the critical days -------------------------------------------------

    def trough(self) -> DayProjection:
        """The lowest point in the window. The single most useful day to name."""
        return min(self.days, key=lambda d: d.closing)

    def flagged(self) -> tuple[DayProjection, ...]:
        return tuple(d for d in self.days if d.flags)

    def breaches_floor(self) -> bool:
        """Does the path fall below what is owed at any point? The binary the
        merchant actually acts on, and the one the backtest scores."""
        return self.trough().closing < self.floor


# --------------------------------------------------------------------------
# Building it
# --------------------------------------------------------------------------


def derive_floor(world: KnownWorld, through: dt.date) -> Paise:
    """The largest committed outflow still due in the window.

    A derived floor rather than a chosen one. "Why ₹1,00,000?" has no good answer;
    "because that is what you owe on the 12th" does. It also happens to keep the
    breach question near a 50/50 split -- a fixed ₹1,00,000 floor would be breached
    on 1 day in 120, and a system that always answered "you are fine" would score
    99%.
    """
    return max((o.amount for o in world.committed_outflows(through)), default=0)


def _flags_for(
    day: DayProjection,
    previous_closing: Paise,
    previous_at_risk: bool,
    is_trough: bool,
    floor: Paise,
) -> frozenset[Flag]:
    """Flag the day a threshold is *crossed*, not every day spent beyond it.

    A fortnight below the floor is one situation, not fourteen. Flagging each day
    produced a report where eleven of fourteen days were marked and seven of the
    warnings read "no movement" -- true, useless, and the reason nobody reads to
    the end. The crossing is the moment something happened, and the only moment the
    merchant can act on.

    `previous_closing` for the first day is the merchant's balance *today*, not
    nothing. Treating day one as having no predecessor reported a breach on a day
    the balance rose, purely because it started below the floor -- which is a
    standing condition, not an event.
    """
    flags: set[Flag] = set()
    if is_trough:
        flags.add(Flag.TROUGH)

    if day.closing < floor:
        if previous_closing >= floor:
            flags.add(Flag.BREACH)
    elif day.band_low is not None and day.band_low < floor and not previous_at_risk:
        flags.add(Flag.AT_RISK)
    return frozenset(flags)


def forecast(
    world: KnownWorld,
    horizon: int = DEFAULT_HORIZON,
    *,
    scenario: Scenario = Scenario.SALES_STOP,
    estimator: "Estimator | None" = None,
    refunds_from_forecast: bool = True,
    bands: "Callable[[int, Paise], tuple[Paise, Paise] | None] | None" = None,
) -> Forecast:
    """Walk forward from `world.as_of`.

    With no `estimator` this is the certain layer alone -- the sales-stop scenario.
    Pass one and the estimated layer is added on top, filling `estimated_in` and
    `estimated_out` on each day, which is what turns it into an actual forecast.

    Takes a `KnownWorld` and nothing else -- no store, no path, no data directory.
    The wall is what makes every number downstream trustworthy, and it holds here
    because there is physically nothing else to read.
    """
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    if estimator is not None and scenario is Scenario.SALES_STOP:
        scenario = Scenario.FORECAST

    through = world.as_of + dt.timedelta(days=horizon)
    floor = derive_floor(world, through)

    by_day: dict[dt.date, list[AnyEvent]] = {}
    for event in world.certain_movements(through):
        assert event.cash_at is not None  # certain_movements guarantees it
        by_day.setdefault(event.cash_at, []).append(event)

    days: list[DayProjection] = []
    balance = world.opening_balance
    for h in range(1, horizon + 1):
        day = world.as_of + dt.timedelta(days=h)
        moved = tuple(by_day.get(day, ()))
        est_in = est_out = 0
        if estimator is not None:
            est_in = estimator.expected_settlement(day, horizon)
            est_out = -estimator.expected_refunds(
                day, refunds_from_forecast=refunds_from_forecast
            )
        projection = DayProjection(
            date=day,
            horizon=h,
            opening=balance,
            certain_in=sum(e.cash_delta for e in moved if e.cash_delta > 0),
            certain_out=sum(e.cash_delta for e in moved if e.cash_delta < 0),
            estimated_in=est_in,
            estimated_out=est_out,
            movements=moved,
        )
        # Bands are attached here, not afterwards, because AT_RISK is decided from
        # them -- a band bolted on after the flags would never fire the one warning
        # that a point forecast cannot produce.
        if bands is not None:
            edges = bands(h, projection.closing)
            if edges is not None:
                projection = replace(
                    projection, band_low=edges[0], band_high=edges[1]
                )
        balance = projection.closing
        days.append(projection)

    # Flags need the whole path and each day's predecessor, so they are a second
    # pass rather than inline.
    trough_date = min(days, key=lambda d: d.closing).date
    flagged: list[DayProjection] = []
    previous_closing = world.opening_balance
    previous_at_risk = False
    for d in days:
        marks = _flags_for(
            d, previous_closing, previous_at_risk, d.date == trough_date, floor
        )
        flagged.append(replace(d, flags=marks))
        previous_closing = d.closing
        previous_at_risk = d.band_low is not None and d.band_low < floor
    days = flagged

    return Forecast(
        as_of=world.as_of,
        scenario=scenario,
        opening=world.opening_balance,
        floor=floor,
        days=tuple(days),
    )


# --------------------------------------------------------------------------
# The merchant report
# --------------------------------------------------------------------------

_MARK = {Flag.TROUGH: "▼", Flag.BREACH: "!", Flag.AT_RISK: "?"}


def render(f: Forecast, question_day: dt.date | None = None) -> str:
    """The merchant-facing report: the path, and its critical days with reasons.

    Deliberately small. No baselines, no accuracy figures -- a controller deciding
    whether to pay a supplier has no use for either. Those belong in the accuracy
    report, which answers a different question for a different reader.
    """
    lines = [
        f"Standing on {f.as_of}  ·  balance {fmt_inr(f.opening)}",
        f"Scenario: {f.scenario.value}",
        f"Floor: {fmt_inr(f.floor)} (largest commitment due in the window)",
    ]
    if f.opening < f.floor:
        lines.append(
            "  NOTE: already below the floor today. That is a standing position,"
            " not an event, so no day is flagged for it."
        )
    lines += [
        "",
        f"  {'':4} {'date':<12}{'in':>13}{'out':>13}{'balance':>15}  certain",
    ]
    for d in f.days:
        mark = "".join(_MARK[x] for x in sorted(d.flags, key=lambda x: x.value))
        band = (
            f"  {fmt_inr(d.band_low)} to {fmt_inr(d.band_high)}"
            if d.band_low is not None
            else ""
        )
        # Total flow, not just the certain part. Showing certain_in alone made days
        # whose movement was entirely estimated read as "0.00 in, 0.00 out" while
        # the balance moved by tens of thousands. The `certain` column already says
        # how much of it was known rather than guessed.
        lines.append(
            f"  {'+' + str(d.horizon):<4} {d.date}  "
            f"{fmt_inr(d.certain_in + d.estimated_in):>12}"
            f"{fmt_inr(d.certain_out + d.estimated_out):>13}"
            f"{fmt_inr(d.closing):>15}"
            f"{d.certain_share:>8.0%} {mark:<3}{band}"
        )

    if question_day is not None:
        asked = f.on(question_day)
        lines += ["", f"Asked about {question_day}: {fmt_inr(asked.closing)}"]

    lines.append("")
    for d in f.flagged():
        mark = "".join(_MARK[x] for x in sorted(d.flags, key=lambda x: x.value))
        names = " + ".join(sorted(x.value for x in d.flags))
        lines.append(
            f"  {mark:<3} +{d.horizon} ({d.date})  {fmt_inr(d.closing)}   [{names}]"
        )
        lines.append(f"        {d.reason()}")
    if not f.flagged():
        lines.append("  no critical days in this window")
    return "\n".join(lines)


def _intervals_up_to(store: EventStore, as_of: dt.date, horizon: int):
    """Replay every earlier vantage point to rebuild the error history.

    A band is a statement about how wrong this forecaster has been before, so
    producing one for a single day still requires having run the earlier days. Only
    vantage points strictly before `as_of` are replayed -- the same rule the backtest
    enforces, applied here so the two cannot disagree.
    """
    from .intervals import RollingIntervals

    rolling = RollingIntervals()
    actual = {b.date: b.closing for b in store.balances}
    day = store.date_for_day(1) + dt.timedelta(days=45)
    while day < as_of:
        w = world_as_of(store, day)
        f = forecast(w, horizon, estimator=Estimator.fit(w, horizon_days=horizon))
        for d in f.days:
            if d.date in actual:
                rolling.observe(d.horizon, d.closing - actual[d.date])
        day += dt.timedelta(days=1)
    return rolling


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Forecast a merchant's cash path.")
    parser.add_argument("--data", default="data")
    parser.add_argument("--day", type=int, default=46, help="vantage day, 1-based")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--ask", type=int, default=None,
                        help="horizon the merchant actually asked about")
    parser.add_argument("--certain-only", action="store_true",
                        help="the sales-stop scenario: no estimates, no bands")
    args = parser.parse_args(argv)

    store = EventStore.load(args.data)
    as_of = store.date_for_day(args.day)
    world = world_as_of(store, as_of)

    estimator = bands = None
    if not args.certain_only:
        estimator = Estimator.fit(world, horizon_days=args.horizon)
        # Bands come from how wrong this forecaster has been at earlier vantage
        # points, so they have to be rebuilt by replaying them. Only days strictly
        # before today are replayed -- the same rule the backtest enforces.
        bands = _intervals_up_to(store, as_of, args.horizon).band_fn()

    f = forecast(world, args.horizon, estimator=estimator, bands=bands)
    asked = as_of + dt.timedelta(days=args.ask) if args.ask else None
    print(render(f, asked))

    actual = {b.date: b.closing for b in store.balances}
    end = f.days[-1]
    if end.date in actual:
        print()
        print(f"  actual on {end.date}: {fmt_inr(actual[end.date])}")
        print(f"  projected:            {fmt_inr(end.closing)}")
        print(f"  gap (Bucket 2's job): {fmt_inr(end.closing - actual[end.date])}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
