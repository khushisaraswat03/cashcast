"""The temporal wall.

The generator knows all 120 days. The forecaster must not. "I will remember not to
look" is not a design, so the separation is structural: `world_as_of(day)` filters
every record on `known_at <= day` and returns a `KnownWorld`, and that object is the
only thing the forecaster is ever given. It holds no path, no directory and no
handle on the store it came from, so there is nothing to reach through.

Three things live here.

**The wall itself.** `world_as_of` and `KnownWorld`.

**The bank statement.** `BankBalance` is not an `Event` -- it reports money that
already moved rather than moving any -- but it passes through the same filter, so a
`KnownWorld` for day 46 holds one balance figure and cannot see day 47's. That is how
a merchant knows their balance: they read it, rather than re-adding a thousand
transactions.

**The audit.** `check_balance_ties` re-derives that balance by summing `cash_delta`
and asserts the two agree. It lives on the store, on the omniscient side of the wall,
and tests the claim the forecast rests on: that adding up what moves reproduces the
balance. A wrong sign anywhere would shift every forecast by a constant and leave
every other test passing.

`diff_daily` reports *which day diverged and by how much*, so a leak test failure
names the problem.
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .events import (
    EVENT_FILES,
    AnyEvent,
    Chargeback,
    CsvModel,
    Method,
    Order,
    Outflow,
    Payment,
    PaymentStatus,
    Promotion,
    Refund,
)
from .money import Paise, fmt_inr


# --------------------------------------------------------------------------
# The bank statement
# --------------------------------------------------------------------------


class BankBalance(CsvModel):
    """One day of the merchant's bank statement.

    Not an `Event`: it does not move money, it observes it. But it carries a date
    and is filtered by the wall exactly like an event, which is what lets the
    forecaster be handed a concrete opening figure without being handed the file
    that also contains every day after it.
    """

    day: int
    date: dt.date
    opening: Paise
    inflow: Paise
    outflow: Paise
    closing: Paise

    MONEY_FIELDS = ("opening", "inflow", "outflow", "closing")

    @property
    def known_at(self) -> dt.date:
        """A day's closing balance is knowable at the end of that day."""
        return self.date


# --------------------------------------------------------------------------
# The omniscient side
# --------------------------------------------------------------------------

#: The generator writes `EVENT_FILES`; this module reads them back. Deliberately
#: imported rather than restated -- two copies of a filename map is the same drift
#: the named queries below exist to prevent.
BALANCE_FILE = "balance.csv"


def _read(path: Path, model: type[CsvModel]) -> list:
    if not path.exists():
        raise FileNotFoundError(f"{path} -- run `python -m src.generate` first")
    with path.open(newline="", encoding="utf-8") as fh:
        return [model.from_csv_row(row) for row in csv.DictReader(fh)]


@dataclass(frozen=True)
class EventStore:
    """Every record, unfiltered. The generator's side of the wall.

    Loaded once and filtered per vantage point: 61 vantage points re-reading eight
    files buys nothing, and the leak test uses `truncated_to` rather than a reload.
    """

    orders: tuple[Order, ...]
    payments: tuple[Payment, ...]
    refunds: tuple[Refund, ...]
    chargebacks: tuple[Chargeback, ...]
    outflows: tuple[Outflow, ...]
    promotions: tuple[Promotion, ...]
    balances: tuple[BankBalance, ...]

    # -- construction ------------------------------------------------------

    @classmethod
    def load(cls, data_dir: str | Path) -> "EventStore":
        d = Path(data_dir)
        rows = {model: _read(d / name, model) for model, name in EVENT_FILES.items()}
        balances = _read(d / BALANCE_FILE, BankBalance)
        balances.sort(key=lambda b: b.date)
        return cls(
            orders=tuple(rows[Order]),
            payments=tuple(rows[Payment]),
            refunds=tuple(rows[Refund]),
            chargebacks=tuple(rows[Chargeback]),
            outflows=tuple(rows[Outflow]),
            promotions=tuple(rows[Promotion]),
            balances=tuple(balances),
        )

    # -- day/date translation ---------------------------------------------

    @property
    def first_day(self) -> dt.date:
        return self.balances[0].date

    @property
    def last_day(self) -> dt.date:
        return self.balances[-1].date

    def date_for_day(self, n: int) -> dt.date:
        """Day 1 is the first day of the dataset."""
        if not 1 <= n <= len(self.balances):
            raise ValueError(f"day {n} outside 1..{len(self.balances)}")
        return self.balances[n - 1].date

    def day_for_date(self, day: dt.date) -> int:
        return (day - self.first_day).days + 1

    # -- everything, uniformly --------------------------------------------

    @property
    def events(self) -> tuple[AnyEvent, ...]:
        return (
            self.orders
            + self.payments
            + self.refunds
            + self.chargebacks
            + self.outflows
            + self.promotions
        )

    # -- the audit ---------------------------------------------------------

    def check_two_dates(self) -> None:
        """No event may move cash before the merchant could know it exists.

        If this ever fails the two-date model is broken and nothing downstream can
        be trusted: the forecaster would be treating a knowable outflow as a
        surprise, or worse, spending money it has not been told about.
        """
        for event in self.events:
            if event.cash_at is not None and event.cash_at < event.known_at:
                raise AssertionError(
                    f"{type(event).__name__} {event.event_id}: "
                    f"cash_at {event.cash_at} precedes known_at {event.known_at}"
                )

    def derived_closing(self, day: dt.date) -> Paise:
        """Closing balance re-derived from events alone.

        Opening balance of day one, plus every `cash_delta` that has landed by
        `day`. This is the forecast's own arithmetic model of the world, applied to
        the past -- which is exactly why it is worth checking against the statement.
        """
        moved = sum(
            e.cash_delta
            for e in self.events
            if e.cash_at is not None and e.cash_at <= day
        )
        return self.balances[0].opening + moved

    def check_balance_ties(self, day: dt.date | None = None) -> None:
        """Assert the statement and the events agree, on `day` or on every day."""
        days = self.balances if day is None else [self.balance_on(day)]
        for row in days:
            derived = self.derived_closing(row.date)
            if derived != row.closing:
                raise AssertionError(
                    f"day {row.day} ({row.date}): statement says "
                    f"{fmt_inr(row.closing)}, events sum to {fmt_inr(derived)}, "
                    f"off by {fmt_inr(derived - row.closing)}"
                )

    def balance_on(self, day: dt.date) -> BankBalance:
        for row in self.balances:
            if row.date == day:
                return row
        raise ValueError(f"no balance row for {day}")

    # -- the leak test's other half ---------------------------------------

    def truncated_to(self, day: dt.date) -> "EventStore":
        """A store with the future physically removed.

        The leak test builds a `KnownWorld` from this and from the full store and
        asserts the two are identical. If they differ, something read past the wall.
        """
        keep = lambda rows: tuple(r for r in rows if r.known_at <= day)
        return EventStore(
            orders=keep(self.orders),
            payments=keep(self.payments),
            refunds=keep(self.refunds),
            chargebacks=keep(self.chargebacks),
            outflows=keep(self.outflows),
            promotions=keep(self.promotions),
            balances=keep(self.balances),
        )


# --------------------------------------------------------------------------
# The wall
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class KnownWorld:
    """Everything the merchant could know on `as_of`, and nothing else.

    The forecaster receives one of these and never anything else -- no path, no
    store, no way back to the unfiltered data.

    The accessors are named queries rather than raw lists on purpose. "Captured but
    not yet in the bank" is needed by the forecaster to project it, by the report to
    explain it, and by the agent to answer questions about it. Written three times
    it drifts -- one copy forgets to exclude failed payments, another counts today's
    settlement twice -- and then two parts of the system quote different numbers
    while both look correct. One definition, tested once.
    """

    as_of: dt.date
    orders: tuple[Order, ...]
    payments: tuple[Payment, ...]
    refunds: tuple[Refund, ...]
    chargebacks: tuple[Chargeback, ...]
    outflows: tuple[Outflow, ...]
    promotions: tuple[Promotion, ...]
    statement: tuple[BankBalance, ...]

    # -- where the forecast starts ----------------------------------------

    @property
    def opening_balance(self) -> Paise:
        """The bank balance as of the vantage day: what the merchant sees today."""
        if not self.statement:
            raise ValueError(f"no bank statement on or before {self.as_of}")
        return self.statement[-1].closing

    # -- Bucket 1: money already in motion --------------------------------

    def payments_in_flight(self) -> tuple[Payment, ...]:
        """Captured, fee already deducted, not yet settled.

        `settles_on > as_of`, not `>=`: money landing today has landed, and is
        already inside `opening_balance`. Counting it again is the classic
        double-count.
        """
        return tuple(
            p
            for p in self.payments
            if p.status is PaymentStatus.CAPTURED
            and p.settles_on is not None
            and p.settles_on > self.as_of
        )

    def refunds_pending(self) -> tuple[Refund, ...]:
        """Requested, not yet netted off a payout. Certain, and not yet paid."""
        return tuple(r for r in self.refunds if r.nets_off_on > self.as_of)

    def chargebacks_pending(self) -> tuple[Chargeback, ...]:
        """Raised, not yet debited.

        The reason the two-date model exists. A chargeback raised yesterday is
        visible in the dashboard and belongs in the certain layer, even though it
        feels like the least predictable thing in the dataset.
        """
        return tuple(c for c in self.chargebacks if c.debited_on > self.as_of)

    def committed_outflows(self, through: dt.date) -> tuple[Outflow, ...]:
        """Rent, salaries, suppliers, tax: due after today, up to and including
        `through`. The most predictable money in the dataset, and the most often
        left out by anyone forecasting from revenue alone."""
        return tuple(o for o in self.outflows if self.as_of < o.due_on <= through)

    # -- history, for the estimators --------------------------------------

    def captured_sales_by_day(self) -> dict[dt.date, Paise]:
        """Gross captured value per capture date. What the sales estimator learns.

        Gross rather than net: the estimator predicts *sales*, and the fee is applied
        later when a predicted sale is turned into cash. Netting here would apply the
        fee twice.

        Failed payments are excluded -- they move no money, so including them would
        teach the estimator to expect revenue that never existed.
        """
        out: dict[dt.date, Paise] = {}
        for p in self.payments:
            if p.status is PaymentStatus.CAPTURED:
                day = p.captured_at.date()
                out[day] = out.get(day, 0) + p.amount
        return out

    def refund_history(self) -> tuple[tuple[dt.date, dt.date, dt.date, Paise], ...]:
        """`(order date, request date, cash-out date, amount)` for known refunds.

        All three dates matter and they do different jobs. The **order date** is what
        the estimator projects *from* -- an order carries a probability of money
        leaving later, knowable the moment it exists. The **request date** is the
        pivot that stops double-counting: once a customer has asked, the refund is a
        *fact* in the certain layer, so the estimator must not predict it again. The
        **cash-out date** is when the money actually goes.
        """
        placed = {o.order_id: o.placed_at.date() for o in self.orders}
        return tuple(
            (placed[r.order_id], r.requested_at.date(), r.nets_off_on, r.amount)
            for r in self.refunds
            if r.order_id in placed
        )

    def upi_share(self) -> float:
        """Fraction of captured value taken on UPI, which settles a day sooner.

        Measured from history rather than read from the generator's config -- the
        forecaster is not allowed to know how the world was made.
        """
        total = upi = 0
        for p in self.payments:
            if p.status is PaymentStatus.CAPTURED:
                total += p.amount
                if p.method is Method.UPI:
                    upi += p.amount
        return upi / total if total else 0.0

    # -- what changes expectations ----------------------------------------

    def promotions_covering(self, day: dt.date) -> tuple[Promotion, ...]:
        """Declared promotions running on `day`.

        Empty when the promotion has not been declared yet -- which is the whole
        hidden-versus-declared experiment, done by the wall rather than by a
        special case here.
        """
        return tuple(p for p in self.promotions if p.covers(day))

    # -- provenance --------------------------------------------------------

    def certain_movements(self, through: dt.date) -> tuple[AnyEvent, ...]:
        """Every known event whose cash lands after today and by `through`.

        The complete certain layer, in one call, for a forecaster that wants to walk
        it rather than ask category by category.
        """
        known: tuple[AnyEvent, ...] = (
            self.payments_in_flight()
            + self.refunds_pending()
            + self.chargebacks_pending()
            + self.committed_outflows(through)
        )
        return tuple(
            e for e in known if e.cash_at is not None and e.cash_at <= through
        )


def world_as_of(store: EventStore, day: dt.date) -> KnownWorld:
    """Everything knowable on `day`. The only door through the wall."""
    keep = lambda rows: tuple(r for r in rows if r.known_at <= day)
    return KnownWorld(
        as_of=day,
        orders=keep(store.orders),
        payments=keep(store.payments),
        refunds=keep(store.refunds),
        chargebacks=keep(store.chargebacks),
        outflows=keep(store.outflows),
        promotions=keep(store.promotions),
        statement=keep(store.balances),
    )


# --------------------------------------------------------------------------
# Divergence reporting -- for the leak test, and later for forecasts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Divergence:
    """One day where two runs disagreed, and by how much."""

    day: dt.date
    label: str
    left: Paise
    right: Paise

    @property
    def delta(self) -> Paise:
        return self.left - self.right

    def __str__(self) -> str:
        return (
            f"{self.day} {self.label}: {fmt_inr(self.left)} vs "
            f"{fmt_inr(self.right)}  (off by {fmt_inr(self.delta)})"
        )


def diff_daily(
    left: Mapping[dt.date, Paise],
    right: Mapping[dt.date, Paise],
    label: str = "balance",
) -> list[Divergence]:
    """Days where two daily series disagree, in date order.

    A missing day counts as a divergence against zero rather than being skipped --
    a forecast that stopped early is a failure, not a match.
    """
    out: list[Divergence] = []
    for day in sorted(set(left) | set(right)):
        a, b = left.get(day, 0), right.get(day, 0)
        if a != b:
            out.append(Divergence(day=day, label=label, left=a, right=b))
    return out


def describe(diffs: Sequence[Divergence]) -> str:
    if not diffs:
        return "identical"
    lines = [f"{len(diffs)} day(s) diverged:"]
    lines += [f"  {d}" for d in diffs]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Print what the forecaster would see on a given day. For poking at by hand."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default="data")
    parser.add_argument("--day", type=int, default=46, help="vantage day, 1-based")
    parser.add_argument("--horizon", type=int, default=14)
    args = parser.parse_args(argv)

    store = EventStore.load(args.data)
    store.check_two_dates()

    as_of = store.date_for_day(args.day)
    through = as_of + dt.timedelta(days=args.horizon)
    store.check_balance_ties(as_of)

    w = world_as_of(store, as_of)
    in_flight = w.payments_in_flight()
    refunds = w.refunds_pending()
    cbs = w.chargebacks_pending()
    outs = w.committed_outflows(through)

    print(f"Standing on day {args.day} ({as_of}), horizon {args.horizon} days")
    print(f"  opening balance          {fmt_inr(w.opening_balance):>14}")
    print(f"  payments in flight       {fmt_inr(sum(p.net for p in in_flight)):>14}"
          f"   ({len(in_flight)} payments)")
    print(f"  refunds pending          {fmt_inr(-sum(r.amount for r in refunds)):>14}"
          f"   ({len(refunds)})")
    print(f"  chargebacks pending      {fmt_inr(-sum(c.amount for c in cbs)):>14}"
          f"   ({len(cbs)})")
    print(f"  committed outflows       {fmt_inr(-sum(o.amount for o in outs)):>14}"
          f"   ({len(outs)})")
    certain = sum(e.cash_delta for e in w.certain_movements(through))
    print(f"  {'':24}{'':>14}")
    print(f"  certain net movement     {fmt_inr(certain):>14}")
    print(f"  balance if nothing else  {fmt_inr(w.opening_balance + certain):>14}")
    print()
    print(f"  events visible: {len(w.orders)} orders, {len(w.payments)} payments, "
          f"{len(w.refunds)} refunds, {len(w.chargebacks)} chargebacks, "
          f"{len(w.outflows)} outflows, {len(w.promotions)} promotions")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
