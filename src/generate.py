"""The data generator -- part 1: sales, payments, and the actual daily balance.

Builds 120 days of history for one imaginary D2C fashion business. Everything is
seeded, so the same seed produces byte-identical output and a bug found today can
be reproduced tomorrow.

This generator is **omniscient**. It knows the whole window, including the sale week
that has not happened yet and the noise on every future day. The forecaster must
never see any of that -- which is what `world.py` is for. Keeping the two apart is
the difference between a measurement and a rehearsal.

Part 1 (this file) produces:

    orders.csv      what customers bought, and when
    payments.csv    the money, its fee breakdown, and when it settles
    balance.csv     the actual closing balance each day -- the ground truth

Refunds, chargebacks, outflows and promotions are written as empty files and filled
in by part 2.

One thing measured rather than assumed: the config asks for ~25% day-to-day noise,
but what you *get* depends on how order counts and order values interact. The
summary reports the noise that actually materialised, because that number sets a
floor on how accurate any forecaster can possibly be -- and a floor you assumed is
not a floor you can quote.

    python -m src.generate --out data --seed 7
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Sequence

from . import calendar_rules as cal
from .events import (
    EVENT_FILES,
    Chargeback,
    Event,
    Method,
    Order,
    Outflow,
    OutflowKind,
    Payment,
    PaymentStatus,
    Promotion,
    Refund,
)
from .money import Paise, fmt, fmt_inr, split

# --------------------------------------------------------------------------
# The shape of the business
# --------------------------------------------------------------------------

#: Order values, in paise. A D2C fashion brand's price ladder: mostly Rs.500-2,000
#: with a thinner tail up to Rs.4,000. Weighted toward the lower end because that is
#: where the volume is.
PRICE_POINTS: tuple[tuple[Paise, float], ...] = (
    (49_900, 0.10),
    (69_900, 0.13),
    (89_900, 0.14),
    (99_900, 0.14),
    (129_900, 0.12),
    (149_900, 0.11),
    (179_900, 0.08),
    (199_900, 0.07),
    (249_900, 0.05),
    (299_900, 0.03),
    (349_900, 0.02),
    (399_900, 0.01),
)

#: Weekday multipliers on order volume. Monday is 0. Fashion sells at the weekend;
#: midweek is quiet. Strong enough that a weekday average has a real pattern to
#: find, not so strong that it looks invented.
WEEKDAY_MULTIPLIER: dict[int, float] = {
    0: 0.95,  # Mon
    1: 0.75,  # Tue
    2: 0.78,  # Wed
    3: 0.90,  # Thu
    4: 1.10,  # Fri
    5: 1.55,  # Sat
    6: 1.45,  # Sun
}

#: The multipliers above average slightly above 1, so using them raw would mean
#: `orders_per_day = 10` quietly produced 10.7. Normalising keeps the config
#: parameter honest: it says orders per day and it means orders per day.
_WEEKDAY_MEAN = sum(WEEKDAY_MULTIPLIER.values()) / len(WEEKDAY_MULTIPLIER)

#: When people shop. Evening-weighted, which is why the 18:00 settlement cutoff
#: catches a meaningful slice of each day's takings rather than a rounding error.
HOUR_WEIGHTS: tuple[tuple[int, float], ...] = (
    (7, 0.01), (8, 0.02), (9, 0.03), (10, 0.05), (11, 0.06), (12, 0.07),
    (13, 0.06), (14, 0.05), (15, 0.05), (16, 0.06), (17, 0.07), (18, 0.08),
    (19, 0.10), (20, 0.11), (21, 0.10), (22, 0.06), (23, 0.02),
)


@dataclass(frozen=True)
class Config:
    """Everything that shapes the dataset, in one place, all of it seeded."""

    out_dir: Path = Path("data")
    seed: int = 7

    # --- the window ---
    start: date = date(2026, 4, 27)  # a Monday
    days: int = 120
    opening_balance: Paise = 4_00_000_00  # Rs.4 lakh

    # --- volume and value ---
    #: Baseline orders on an average day *before* growth and promotions are applied.
    #: Weekday multipliers are normalised so this figure is honest; growth and the
    #: sale week then deliberately push the realised average above it.
    orders_per_day: float = 10.0
    growth_over_window: float = 0.30  # +30% from first day to last
    daily_noise: float = 0.25  # requested; the realised figure is measured

    # --- the sale ---
    promo_starts_on_day: int = 60  # 1-indexed
    promo_ends_on_day: int = 66
    promo_volume_uplift: float = 3.0
    promo_discount: float = 0.30  # sale orders are 30% cheaper
    promo_declared_days_ahead: int = 15
    # What the merchant declares, and the two halves are known very differently.
    #
    # The discount is a *decision*, not a forecast -- they chose 30% off, so they
    # know it exactly, the same way they know the dates.
    #
    # The volume uplift is a genuine guess about how customers will respond, and
    # promotional lift forecasts are optimistic as a rule. So the merchant plans for
    # 3.3x and gets 3.0x. ASSUMPTION: the 10% over-estimate is plausible rather than
    # measured, and it exists so the declared forecast improves a lot without being
    # handed the answer -- an exactly correct plan would make declaring look better
    # than it can ever be in practice.
    merchant_expected_volume_uplift: float = 3.3
    merchant_expected_discount: float = 0.30
    dip_days: int = 7  # demand pulled forward, so the week after is thin
    dip_multiplier: float = 0.80

    # --- payments ---
    upi_share: float = 0.55
    failure_rate: float = 0.06
    retry_success_rate: float = 0.80
    cutoff: time = cal.DEFAULT_CUTOFF
    holidays: frozenset[date] = cal.BANK_HOLIDAYS

    # --- refunds ---
    #: Fashion returns run high. This is the single most important parameter for the
    #: estimated layer: at 18% of ~1,500 orders you get ~270 refund events, which is
    #: enough to estimate a rate and a lag distribution from. At 1% you would get
    #: fifteen, and the whole middle layer would have nothing to learn.
    refund_rate: float = 0.18
    #: Returned two of three items rather than the whole order.
    partial_refund_share: float = 0.45
    #: Days between the sale and the refund request, as a distribution rather than a
    #: single number -- return shipping and inspection genuinely vary, and the shape
    #: is what lets refunds be spread across future days instead of lumped on one.
    refund_lag_weights: tuple[tuple[int, float], ...] = (
        (5, 0.08), (6, 0.12), (7, 0.18), (8, 0.18), (9, 0.15),
        (10, 0.11), (11, 0.08), (12, 0.05), (13, 0.03), (14, 0.02),
    )
    #: Working days between requesting a refund and it coming off a payout. Short,
    #: but non-zero -- which is what puts a requested refund in the certain layer.
    refund_netting_lag: int = 1

    # --- chargebacks ---
    #: A count, not a rate. Chargebacks are rare enough that a rate would produce
    #: two or three by luck; fixing the count makes the dataset reproducible without
    #: pretending the events are frequent enough to estimate.
    chargeback_count: int = 4
    #: Most disputes concern sales from before this window -- the realistic case, and
    #: the one where nothing local explains the money leaving.
    chargeback_from_window_share: float = 0.25
    chargeback_debit_lag: tuple[int, int] = (3, 8)

    # --- outflows, in paise ---
    #: Sized against inflow rather than picked for flavour. This business nets about
    #: Rs.5.0 lakh a month after fees and refunds, so committed monthly outflows come
    #: to about Rs.4.55 lakh -- roughly 90%. That leaves a thin surplus in a normal
    #: month, which is what makes the sale month hurt. Set these much higher and the
    #: merchant is simply insolvent, which is not a forecasting problem.
    rent: Paise = 45_000_00
    rent_due_day: int = 1
    salary: Paise = 1_80_000_00
    salary_due_day: int = 1
    ads_monthly: Paise = 65_000_00
    ads_due_day: int = 7
    ads_sale_month_extra: Paise = 40_000_00
    supplier_monthly: Paise = 1_30_000_00
    supplier_due_day: int = 20
    #: Stock for the sale, paid for well before any of it sells. This is what turns
    #: a comfortable balance into a squeeze, and it is the most predictable outflow
    #: in the dataset -- which is exactly why missing it is embarrassing.
    sale_inventory: Paise = 1_80_000_00
    sale_inventory_days_before: int = 10
    gst_monthly: Paise = 35_000_00
    gst_due_day: int = 20

    #: How far ahead each kind of outflow is known. A rent cheque is foreseeable
    #: months out; an ad invoice a week. The spread means that at any vantage point
    #: some outflows are certain and others are not yet visible at all.
    commitment_lead_days: int = 21

    #: Outflows and refunds are generated past the end of the window, because a
    #: forecast standing on day 106 looks fourteen days into the future and those
    #: days have to contain something.
    tail_days: int = 30

    @property
    def end(self) -> date:
        return self.start + timedelta(days=self.days - 1)

    def day_of(self, index: int) -> date:
        """Calendar date for a 1-indexed day number."""
        return self.start + timedelta(days=index - 1)

    def index_of(self, day: date) -> int:
        return (day - self.start).days + 1

    @property
    def promo_starts_on(self) -> date:
        return self.day_of(self.promo_starts_on_day)

    @property
    def promo_ends_on(self) -> date:
        return self.day_of(self.promo_ends_on_day)


# --------------------------------------------------------------------------
# The generator
# --------------------------------------------------------------------------


class Generator:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.rng = random.Random(config.seed)

        self.orders: list[Order] = []
        self.payments: list[Payment] = []
        self.refunds: list[Refund] = []
        self.chargebacks: list[Chargeback] = []
        self.outflows: list[Outflow] = []
        self.promotions: list[Promotion] = []

        #: date -> closing balance. The ground truth every forecast is scored on.
        self.balance: dict[date, Paise] = {}

        #: What the sales curve *intended* for each day, kept so the realised noise
        #: can be measured against it rather than guessed at.
        self.expected_orders: dict[date, float] = {}

        self._order_seq = 0
        self._payment_seq = 0

    # -- the sales curve ---------------------------------------------------

    def _growth_factor(self, index: int) -> float:
        """Linear growth across the window. Deliberately linear rather than
        compounding: it is the *bias it creates* in a four-week average that
        matters, and a simple ramp makes that bias easy to reason about."""
        if self.cfg.days <= 1:
            return 1.0
        progress = (index - 1) / (self.cfg.days - 1)
        return 1.0 + self.cfg.growth_over_window * progress

    def _promo_factor(self, index: int) -> float:
        cfg = self.cfg
        if cfg.promo_starts_on_day <= index <= cfg.promo_ends_on_day:
            return cfg.promo_volume_uplift
        dip_start = cfg.promo_ends_on_day + 1
        if dip_start <= index < dip_start + cfg.dip_days:
            return cfg.dip_multiplier
        return 1.0

    def _expected_orders(self, index: int) -> float:
        day = self.cfg.day_of(index)
        return (
            self.cfg.orders_per_day
            * (WEEKDAY_MULTIPLIER[day.weekday()] / _WEEKDAY_MEAN)
            * self._growth_factor(index)
            * self._promo_factor(index)
        )

    def _order_count(self, index: int) -> int:
        """Draw an order count for the day, with noise around the expected level."""
        expected = self._expected_orders(index)
        drawn = self.rng.gauss(expected, expected * self.cfg.daily_noise)
        return max(0, round(drawn))

    def _order_value(self, index: int) -> Paise:
        """One order's value. Sometimes a basket of two.

        During the sale, orders are discounted -- so a 3x jump in volume produces
        roughly 2.1x the revenue, not 3x.

        That gap used to be described here as "plans are not outcomes", which was
        the wrong label for it: the merchant expects 3x volume and gets exactly 3x
        volume, so the plan was correct. The discrepancy was entirely a unit
        conversion the forecaster was not given enough information to do. The
        promotion now declares volume *and* discount, so a forecaster that uses both
        can convert properly.

        Modelling genuine merchant optimism -- planning 3x and getting 2.4x -- would
        be a separate knob and a separate finding. Deliberately not done: it would
        change the actual sales data, and conflating optimism with a unit mismatch
        is what caused the confusion in the first place.
        """
        points = [p for p, _ in PRICE_POINTS]
        weights = [w for _, w in PRICE_POINTS]
        value = self.rng.choices(points, weights=weights)[0]
        if self.rng.random() < 0.25:
            value += self.rng.choices(points, weights=weights)[0]
        if self.cfg.promo_starts_on_day <= index <= self.cfg.promo_ends_on_day:
            value = round(value * (1 - self.cfg.promo_discount))
        return value

    def _placed_at(self, day: date) -> datetime:
        hours = [h for h, _ in HOUR_WEIGHTS]
        weights = [w for _, w in HOUR_WEIGHTS]
        hour = self.rng.choices(hours, weights=weights)[0]
        return datetime.combine(
            day, time(hour, self.rng.randrange(60), self.rng.randrange(60))
        )

    # -- orders and payments ----------------------------------------------

    def _next_order_id(self) -> str:
        self._order_seq += 1
        return f"ord_{self._order_seq:05d}"

    def _next_payment_id(self) -> str:
        self._payment_seq += 1
        return f"pay_{self._payment_seq:05d}"

    def _method(self) -> Method:
        return Method.UPI if self.rng.random() < self.cfg.upi_share else Method.CARD

    def _capture(self, order: Order, at: datetime) -> Payment:
        """A successful payment against an order."""
        fee, gst, net = split(order.amount)
        settles_on = cal.settlement_date(
            at,
            order.method.value,
            cutoff=self.cfg.cutoff,
            holidays=self.cfg.holidays,
        )
        return Payment(
            payment_id=self._next_payment_id(),
            order_id=order.order_id,
            amount=order.amount,
            method=order.method,
            status=PaymentStatus.CAPTURED,
            captured_at=at,
            settles_on=settles_on,
            fee=fee,
            gst=gst,
            net=net,
        )

    def _fail(self, order: Order, at: datetime) -> Payment:
        """A declined attempt. Collects nothing, settles never, still worth keeping
        -- it is the only thing that explains an order with no revenue behind it."""
        return Payment(
            payment_id=self._next_payment_id(),
            order_id=order.order_id,
            amount=order.amount,
            method=order.method,
            status=PaymentStatus.FAILED,
            captured_at=at,
        )

    def _generate_orders_and_payments(self) -> None:
        cfg = self.cfg
        for index in range(1, cfg.days + 1):
            day = cfg.day_of(index)
            self.expected_orders[day] = self._expected_orders(index)

            for _ in range(self._order_count(index)):
                placed_at = self._placed_at(day)
                order = Order(
                    order_id=self._next_order_id(),
                    placed_at=placed_at,
                    amount=self._order_value(index),
                    method=self._method(),
                    customer_id=f"cust_{self.rng.randrange(1, 100_000):05d}",
                )
                self.orders.append(order)

                attempt_at = placed_at + timedelta(seconds=self.rng.randrange(20, 200))
                if self.rng.random() >= cfg.failure_rate:
                    self.payments.append(self._capture(order, attempt_at))
                    continue

                # Declined. Most customers try again with another method.
                self.payments.append(self._fail(order, attempt_at))
                if self.rng.random() < cfg.retry_success_rate:
                    retry_at = attempt_at + timedelta(
                        seconds=self.rng.randrange(40, 900)
                    )
                    self.payments.append(self._capture(order, retry_at))

        self.orders.sort(key=lambda o: (o.placed_at, o.order_id))
        self.payments.sort(key=lambda p: (p.captured_at, p.payment_id))

    # -- refunds -----------------------------------------------------------

    def _refund_lag(self) -> int:
        days = [d for d, _ in self.cfg.refund_lag_weights]
        weights = [w for _, w in self.cfg.refund_lag_weights]
        return self.rng.choices(days, weights=weights)[0]

    def _generate_refunds(self) -> None:
        """Returns, raised days after the sale and netted off a later payout.

        The structure that makes refunds forecastable: a refund is not a fresh
        random event, it is a *consequence of a sale that already happened*. So the
        expected refund outflow on any future day can be computed from sales already
        in the books, spread forward by the lag distribution -- no guessing about
        customers required.

        Two lags, doing different jobs. The 5-14 day request lag is what makes
        refunds predictable in advance. The short netting lag is what puts an
        already-requested refund in the certain layer.
        """
        cfg = self.cfg
        captured = [p for p in self.payments if p.status is PaymentStatus.CAPTURED]
        seq = 0

        for payment in captured:
            if self.rng.random() >= cfg.refund_rate:
                continue

            if self.rng.random() < cfg.partial_refund_share:
                # Part of the order came back. Round to whole rupees -- a refund is
                # issued against line items, not as an arbitrary fraction.
                fraction = self.rng.uniform(0.3, 0.7)
                amount = max(100, round(payment.amount * fraction / 100) * 100)
            else:
                amount = payment.amount

            requested_at = payment.captured_at + timedelta(
                days=self._refund_lag(),
                hours=self.rng.randrange(-4, 8),
            )
            seq += 1
            self.refunds.append(
                Refund(
                    refund_id=f"rfnd_{seq:05d}",
                    order_id=payment.order_id,
                    payment_id=payment.payment_id,
                    amount=min(amount, payment.amount),
                    requested_at=requested_at,
                    nets_off_on=cal.add_working_days(
                        requested_at.date(), cfg.refund_netting_lag, cfg.holidays
                    ),
                )
            )

        self.refunds.sort(key=lambda r: (r.requested_at, r.refund_id))

    # -- chargebacks -------------------------------------------------------

    def _generate_chargebacks(self) -> None:
        """Disputes. Rare, lumpy, and mostly about sales from before this window.

        Deliberately not derived from a rate. Four events in 120 days is realistic
        for a merchant this size, and it is also far too few to estimate anything
        from -- which is the point. Chargebacks are not a forecasting problem, they
        are an uncertainty problem, and pretending otherwise would be the mistake.
        """
        cfg = self.cfg
        captured = [p for p in self.payments if p.status is PaymentStatus.CAPTURED]
        lo, hi = cfg.chargeback_debit_lag
        # Spread them across the window rather than clustering by luck.
        span = cfg.days - 20
        for i in range(cfg.chargeback_count):
            raised_on = cfg.day_of(
                15 + round(span * (i + 0.5) / cfg.chargeback_count)
                + self.rng.randrange(-4, 5)
            )
            raised_at = datetime.combine(
                raised_on, time(self.rng.randrange(10, 17), self.rng.randrange(60))
            )

            local = self.rng.random() < cfg.chargeback_from_window_share
            candidates = [
                p for p in captured if p.captured_at.date() < raised_on - timedelta(days=20)
            ]
            if local and candidates:
                disputed = self.rng.choice(candidates)
                payment_id = disputed.payment_id
                amount = disputed.amount
                original_on = disputed.captured_at.date()
            else:
                # A sale that predates the dataset. There is no local record of it,
                # which is exactly why the money leaving looks unexplainable.
                payment_id = f"pay_pre_{i:03d}"
                points = [p for p, _ in PRICE_POINTS]
                weights = [w for _, w in PRICE_POINTS]
                amount = self.rng.choices(points, weights=weights)[0]
                original_on = cfg.start - timedelta(days=self.rng.randrange(20, 90))

            self.chargebacks.append(
                Chargeback(
                    chargeback_id=f"cb_{i + 1:03d}",
                    payment_id=payment_id,
                    amount=amount,
                    raised_at=raised_at,
                    debited_on=cal.add_working_days(
                        raised_on, self.rng.randrange(lo, hi + 1), cfg.holidays
                    ),
                    original_captured_on=original_on,
                )
            )

        self.chargebacks.sort(key=lambda c: (c.raised_at, c.chargeback_id))

    # -- outflows ----------------------------------------------------------

    def _months_in_window(self) -> list[tuple[int, int]]:
        """(year, month) for every month the window touches, including the tail."""
        cfg = self.cfg
        last = cfg.end + timedelta(days=cfg.tail_days)
        months: list[tuple[int, int]] = []
        year, month = cfg.start.year, cfg.start.month
        while (year, month) <= (last.year, last.month):
            months.append((year, month))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return months

    def _due_on(self, year: int, month: int, day_of_month: int) -> date:
        """The nominal due date, moved to the next working day if it falls on a
        weekend or holiday. Rent due on the 1st is paid on the 3rd if the 1st is a
        Saturday, and a forecast that puts it on the 1st is wrong."""
        try:
            nominal = date(year, month, day_of_month)
        except ValueError:  # e.g. the 31st of a 30-day month
            nominal = date(year, month, 28) + timedelta(days=4)
            nominal = nominal.replace(day=1) - timedelta(days=1)
        return cal.next_working_day(nominal, self.cfg.holidays)

    def _add_outflow(
        self, kind: OutflowKind, amount: Paise, due_on: date, description: str
    ) -> None:
        cfg = self.cfg
        if not (cfg.start <= due_on <= cfg.end + timedelta(days=cfg.tail_days)):
            return
        committed_on = due_on - timedelta(days=cfg.commitment_lead_days)
        self.outflows.append(
            Outflow(
                outflow_id=f"out_{len(self.outflows) + 1:04d}",
                kind=kind,
                amount=amount,
                committed_at=datetime.combine(committed_on, time(10, 0)),
                due_on=due_on,
                description=description,
            )
        )

    def _generate_outflows(self) -> None:
        """Rent, salaries, ad spend, stock, and tax.

        Almost all of it is knowable weeks ahead, and almost all of it is ignored by
        anyone forecasting cash from revenue alone. The sale-stock payment is the
        interesting one: it leaves three weeks before any of that stock sells, so a
        merchant looking at a healthy balance during the sale has already spent the
        proceeds.
        """
        cfg = self.cfg
        sale_month = (cfg.promo_starts_on.year, cfg.promo_starts_on.month)

        for year, month in self._months_in_window():
            label = f"{year}-{month:02d}"
            self._add_outflow(
                OutflowKind.RENT,
                cfg.rent,
                self._due_on(year, month, cfg.rent_due_day),
                f"office rent {label}",
            )
            self._add_outflow(
                OutflowKind.SALARY,
                cfg.salary,
                self._due_on(year, month, cfg.salary_due_day),
                f"salaries {label}",
            )
            ads = cfg.ads_monthly + (
                cfg.ads_sale_month_extra if (year, month) == sale_month else 0
            )
            self._add_outflow(
                OutflowKind.ADS,
                ads,
                self._due_on(year, month, cfg.ads_due_day),
                f"ad spend {label}" + (" (sale campaign)" if ads > cfg.ads_monthly else ""),
            )
            self._add_outflow(
                OutflowKind.SUPPLIER,
                cfg.supplier_monthly,
                self._due_on(year, month, cfg.supplier_due_day),
                f"stock replenishment {label}",
            )
            self._add_outflow(
                OutflowKind.TAX,
                cfg.gst_monthly,
                self._due_on(year, month, cfg.gst_due_day),
                f"GST {label}",
            )

        # The one that does the damage.
        self._add_outflow(
            OutflowKind.SUPPLIER,
            cfg.sale_inventory,
            cal.next_working_day(
                cfg.promo_starts_on - timedelta(days=cfg.sale_inventory_days_before),
                cfg.holidays,
            ),
            "sale stock purchase",
        )

        self.outflows.sort(key=lambda o: (o.due_on, o.outflow_id))

    # -- the promotion -----------------------------------------------------

    def _generate_promotion(self) -> None:
        """The planned sale, as an event the merchant declared in advance.

        Whether the forecaster ever sees this is decided by the wall, not here --
        `declared_at` is a real date and the wall filters on it like anything else.
        Set it before a vantage day and the sale is foreseeable; set it after and it
        is a shock. Same dataset either way.
        """
        cfg = self.cfg
        declared_on = cfg.promo_starts_on - timedelta(days=cfg.promo_declared_days_ahead)
        self.promotions.append(
            Promotion(
                promotion_id="promo_001",
                name="End of Season Sale",
                declared_at=datetime.combine(declared_on, time(11, 0)),
                starts_on=cfg.promo_starts_on,
                ends_on=cfg.promo_ends_on,
                expected_volume_uplift=cfg.merchant_expected_volume_uplift,
                expected_discount=cfg.merchant_expected_discount,
            )
        )

    # -- the balance -------------------------------------------------------

    def all_events(self) -> list[Event]:
        return [
            *self.orders,
            *self.payments,
            *self.refunds,
            *self.chargebacks,
            *self.outflows,
            *self.promotions,
        ]

    def _compute_balance(self) -> None:
        """Roll the balance forward across every day in the window.

        No per-type branching: a day's movement is the sum of `cash_delta` over
        events whose `cash_at` is that day. That is the whole point of the uniform
        event protocol, and it is the same code the forecaster will run over
        *projected* events.
        """
        movement: dict[date, Paise] = {}
        for event in self.all_events():
            if event.cash_at is None or event.cash_delta == 0:
                continue
            movement[event.cash_at] = movement.get(event.cash_at, 0) + event.cash_delta

        balance = self.cfg.opening_balance
        day = self.cfg.start
        while day <= self.cfg.end:
            balance += movement.get(day, 0)
            self.balance[day] = balance
            day += timedelta(days=1)

        #: Cash landing after the window closes -- money genuinely owed but not yet
        #: arrived. Reported rather than dropped, because a forecast standing near
        #: the end of the window depends on it.
        self.beyond_window = {
            d: m for d, m in sorted(movement.items()) if d > self.cfg.end
        }

    # -- orchestration -----------------------------------------------------

    def run(self) -> None:
        # Order matters: refunds and chargebacks are derived from payments, and the
        # balance is derived from everything.
        self._generate_orders_and_payments()
        self._generate_refunds()
        self._generate_chargebacks()
        self._generate_outflows()
        self._generate_promotion()
        self._compute_balance()

    # -- measurement -------------------------------------------------------

    def _paid(self, refund: Refund) -> Paise:
        """The amount originally captured on the payment a refund is against."""
        for p in self.payments:
            if p.payment_id == refund.payment_id:
                return p.amount
        return 0

    def daily_revenue(self) -> dict[date, Paise]:
        """Captured revenue by the day the sale happened (not the day it settles)."""
        revenue: dict[date, Paise] = {}
        for payment in self.payments:
            if payment.status is not PaymentStatus.CAPTURED:
                continue
            day = payment.captured_at.date()
            revenue[day] = revenue.get(day, 0) + payment.amount
        return revenue

    def realised_noise(self) -> dict[str, float]:
        """How noisy the data actually turned out, versus what was asked for.

        The config requests 25% day-to-day noise, but that is applied to the order
        *count*; order values then vary independently, and the two interact. What
        matters downstream is the noise on daily revenue, so it is measured here.

        Promotional and dip days are excluded -- they are a deliberate signal, not
        noise, and leaving them in would inflate the figure and flatter the
        forecaster later by making the floor look higher than it is.
        """
        cfg = self.cfg
        revenue = self.daily_revenue()
        mean_value = self._mean_order_value()
        ratios: list[float] = []
        for index in range(1, cfg.days + 1):
            if self._promo_factor(index) != 1.0:
                continue
            day = cfg.day_of(index)
            expected_value = self.expected_orders[day] * mean_value
            actual = revenue.get(day, 0)
            if expected_value > 0:
                ratios.append(actual / expected_value)

        return {
            "requested_noise_on_order_count": cfg.daily_noise,
            "realised_noise_on_daily_revenue": (
                statistics.stdev(ratios) if len(ratios) > 1 else 0.0
            ),
            "days_measured": len(ratios),
        }

    def _mean_order_value(self) -> float:
        """Mean captured order value on non-promotional days.

        Measured from the data rather than derived from the price ladder and the
        basket probability. Deriving it would duplicate two constants, and the
        duplicate would go stale silently the first time either one changed --
        taking the noise measurement with it.
        """
        cfg = self.cfg
        values = [
            p.amount
            for p in self.payments
            if p.status is PaymentStatus.CAPTURED
            and self._promo_factor(cfg.index_of(p.captured_at.date())) == 1.0
        ]
        return statistics.mean(values) if values else 0.0

    def noise_floor(self, horizon: int = 14) -> dict[str, float | int]:
        """The best any forecaster could do, given how noisy this business is.

        Even a forecaster that knew each day's *expected* revenue exactly would
        still be wrong by roughly the noise. Errors partly cancel over a horizon, so
        the cumulative floor grows with the square root of the number of days.

        Reporting this alongside the achieved error is the difference between "our
        error is Rs.16,000" and "our error is Rs.16,000 against an irreducible
        Rs.13,000" -- the second says how close to optimal the method is.
        """
        revenue = self.daily_revenue()
        noise = self.realised_noise()["realised_noise_on_daily_revenue"]
        mean_daily = statistics.mean(revenue.values()) if revenue else 0.0
        per_day = mean_daily * noise
        return {
            "horizon_days": horizon,
            "mean_daily_revenue_paise": round(mean_daily),
            "per_day_sigma_paise": round(per_day),
            "cumulative_floor_paise": round(per_day * (horizon**0.5)),
        }

    def tightest_days(self, n: int = 5) -> list[tuple[date, Paise]]:
        """The lowest closing balances in the window, worst first.

        This is what the forecast is *for*. A merchant with a comfortable balance
        every day does not need a forecaster; one who comes within a few days of not
        making payroll does. If this list is not uncomfortable, the outflows are
        sized wrong and the whole exercise is answering a question nobody asked.
        """
        return sorted(self.balance.items(), key=lambda kv: kv[1])[:n]

    def squeeze(self) -> dict:
        """Locate the worst stretch and attribute it.

        Deliberately computed rather than asserted. The squeeze is *emergent* -- it
        comes from the sale-stock payment landing before the sale, the post-sale dip
        in revenue, and the refund wave from sale-week orders all overlapping. None
        of those was placed to create it, so where it actually falls has to be
        looked up rather than declared.
        """
        if not self.balance:
            return {}
        worst_day, worst_balance = min(self.balance.items(), key=lambda kv: kv[1])
        window_start = worst_day - timedelta(days=7)
        window_end = worst_day + timedelta(days=7)

        contributors: dict[str, Paise] = {}
        for outflow in self.outflows:
            if window_start <= outflow.due_on <= window_end:
                key = outflow.kind.value
                contributors[key] = contributors.get(key, 0) + outflow.amount
        refunds = sum(
            r.amount for r in self.refunds if window_start <= r.nets_off_on <= window_end
        )
        if refunds:
            contributors["refunds"] = refunds

        return {
            "worst_day": worst_day.isoformat(),
            "worst_day_index": self.cfg.index_of(worst_day),
            "worst_balance_paise": worst_balance,
            "days_from_sale_start": (worst_day - self.cfg.promo_starts_on).days,
            "longest_run_below_2_lakh": self.longest_run_below(2_00_000_00),
            "outflows_within_a_week_paise": dict(
                sorted(contributors.items(), key=lambda kv: -kv[1])
            ),
        }

    def longest_run_below(self, threshold: Paise) -> int:
        """Longest unbroken stretch of days with a closing balance under `threshold`.

        A single tight day is a blip -- visible two days out and requiring no
        forecast. A five-week stretch is a situation, and the merchant needs to know
        how long it lasts and when they come out of it. That is the question a
        14-day horizon exists to answer, so the dataset had better contain one.
        """
        longest = run = 0
        for index in range(1, self.cfg.days + 1):
            if self.balance[self.cfg.day_of(index)] < threshold:
                run += 1
                longest = max(longest, run)
            else:
                run = 0
        return longest

    def summary(self) -> dict:
        cfg = self.cfg
        captured = [p for p in self.payments if p.status is PaymentStatus.CAPTURED]
        failed = [p for p in self.payments if p.status is PaymentStatus.FAILED]
        paid_orders = {p.order_id for p in captured}

        gross = sum(p.amount for p in captured)
        fees = sum(p.fee for p in captured)
        gst = sum(p.gst for p in captured)
        net = sum(p.net for p in captured)

        return {
            "config": {
                "seed": cfg.seed,
                "start": cfg.start.isoformat(),
                "end": cfg.end.isoformat(),
                "days": cfg.days,
                "orders_per_day": cfg.orders_per_day,
                "growth_over_window": cfg.growth_over_window,
                "upi_share": cfg.upi_share,
                "failure_rate": cfg.failure_rate,
                "cutoff": cfg.cutoff.isoformat(),
                "opening_balance_paise": cfg.opening_balance,
                "holidays_in_window": sorted(
                    h.isoformat() for h in cfg.holidays if cfg.start <= h <= cfg.end
                ),
                "promotion": {
                    "runs": [cfg.promo_starts_on.isoformat(), cfg.promo_ends_on.isoformat()],
                    "declared_on": self.promotions[0].known_at.isoformat()
                    if self.promotions
                    else None,
                    "volume_uplift": cfg.promo_volume_uplift,
                    "discount": cfg.promo_discount,
                    "merchant_expected_volume_uplift": cfg.merchant_expected_volume_uplift,
                    "merchant_expected_discount": cfg.merchant_expected_discount,
                },
            },
            "counts": {
                "orders": len(self.orders),
                "orders_paid": len(paid_orders),
                "orders_abandoned": len(self.orders) - len(paid_orders),
                "payments": len(self.payments),
                "payments_captured": len(captured),
                "payments_failed": len(failed),
                "refunds": len(self.refunds),
                "refunds_partial": sum(1 for r in self.refunds if r.amount < self._paid(r)),
                "chargebacks": len(self.chargebacks),
                "chargebacks_predating_window": sum(
                    1 for c in self.chargebacks if c.original_captured_on < cfg.start
                ),
                "outflows": len(self.outflows),
                "days": len(self.balance),
            },
            "money_paise": {
                "gross_captured": gross,
                "fees": fees,
                "gst": gst,
                "net_settled": net,
                "refunds": sum(r.amount for r in self.refunds),
                "chargebacks": sum(c.amount for c in self.chargebacks),
                # Split in two, because outflows are deliberately generated past the
                # end of the window -- a forecast standing on day 106 has to see
                # them. Lumping the tail in with the window makes the business look
                # far worse than the balance actually shows.
                "outflows_in_window": sum(
                    o.amount for o in self.outflows if o.due_on <= cfg.end
                ),
                "outflows_in_tail": sum(
                    o.amount for o in self.outflows if o.due_on > cfg.end
                ),
                "opening_balance": cfg.opening_balance,
                "closing_balance": self.balance[cfg.end] if self.balance else 0,
                "net_movement_after_window": sum(self.beyond_window.values()),
            },
            "cash_position": {
                "tightest_days": [
                    {"date": d.isoformat(), "day": cfg.index_of(d), "balance_paise": b}
                    for d, b in self.tightest_days(5)
                ],
                "days_below_1_lakh": sum(1 for b in self.balance.values() if b < 1_00_000_00),
                "days_negative": sum(1 for b in self.balance.values() if b < 0),
                "squeeze": self.squeeze(),
            },
            "measured": {
                "noise": self.realised_noise(),
                "noise_floor_14d": self.noise_floor(14),
                "mean_order_value_paise": round(gross / len(captured)) if captured else 0,
            },
        }

    # -- output ------------------------------------------------------------

    def write(self) -> None:
        out = self.cfg.out_dir
        out.mkdir(parents=True, exist_ok=True)

        for model, rows in (
            (Order, self.orders),
            (Payment, self.payments),
            (Refund, self.refunds),
            (Chargeback, self.chargebacks),
            (Outflow, self.outflows),
            (Promotion, self.promotions),
        ):
            _write_events(out / EVENT_FILES[model], model, rows)

        self._write_balance(out / "balance.csv")
        (out / "meta.json").write_text(
            json.dumps(self.summary(), indent=2), encoding="utf-8"
        )

    def _write_balance(self, path: Path) -> None:
        """The ground truth: what the bank actually held at the end of each day.

        Inflow and outflow are broken out so a surprising closing balance can be
        explained by looking at one row rather than re-deriving it.
        """
        inflow: dict[date, Paise] = {}
        outflow: dict[date, Paise] = {}
        for event in self.all_events():
            if event.cash_at is None or event.cash_delta == 0:
                continue
            bucket = inflow if event.cash_delta > 0 else outflow
            bucket[event.cash_at] = bucket.get(event.cash_at, 0) + abs(event.cash_delta)

        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["day", "date", "opening", "inflow", "outflow", "closing"])
            opening = self.cfg.opening_balance
            for index in range(1, self.cfg.days + 1):
                day = self.cfg.day_of(index)
                closing = self.balance[day]
                writer.writerow(
                    [
                        index,
                        day.isoformat(),
                        fmt(opening),
                        fmt(inflow.get(day, 0)),
                        fmt(outflow.get(day, 0)),
                        fmt(closing),
                    ]
                )
                opening = closing


def _write_events(path: Path, model: type[Event], rows: Sequence[Event]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=model.csv_fields())
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_csv_row())


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.generate",
        description="Generate synthetic sales history for one D2C merchant.",
    )
    p.add_argument("--out", type=Path, default=Path("data"))
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--start", type=lambda s: date.fromisoformat(s), default=date(2026, 4, 27))
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--orders-per-day", type=float, default=10.0)
    p.add_argument("--noise", type=float, default=0.25, dest="daily_noise")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config(
        out_dir=args.out,
        seed=args.seed,
        start=args.start,
        days=args.days,
        orders_per_day=args.orders_per_day,
        daily_noise=args.daily_noise,
    )
    gen = Generator(cfg)
    gen.run()
    gen.write()

    if args.quiet:
        return 0

    s = gen.summary()
    c, m, meas = s["counts"], s["money_paise"], s["measured"]
    pos = s["cash_position"]
    print(f"wrote {cfg.out_dir}/  seed={cfg.seed}  {cfg.start} .. {cfg.end} ({cfg.days} days)")
    print(
        f"  {c['orders']} orders -> {c['payments']} payment attempts "
        f"({c['payments_captured']} captured, {c['payments_failed']} declined)"
    )
    print(f"  {c['orders_abandoned']} orders never paid for")
    print(
        f"  {c['refunds']} refunds ({c['refunds_partial']} partial), "
        f"{c['chargebacks']} chargebacks ({c['chargebacks_predating_window']} predating the window), "
        f"{c['outflows']} outflows"
    )
    print()
    print(f"  gross captured   {fmt_inr(m['gross_captured']):>14}")
    print(f"  - fees           {fmt_inr(m['fees']):>14}")
    print(f"  - gst            {fmt_inr(m['gst']):>14}")
    print(f"  {'':<16} {'-' * 14}")
    print(f"  = reaches bank   {fmt_inr(m['net_settled']):>14}")
    print(f"  - refunds        {fmt_inr(m['refunds']):>14}")
    print(f"  - chargebacks    {fmt_inr(m['chargebacks']):>14}")
    print(f"  - outflows       {fmt_inr(m['outflows_in_window']):>14}")
    print()
    print(f"  opening balance  {fmt_inr(m['opening_balance']):>14}")
    print(f"  closing balance  {fmt_inr(m['closing_balance']):>14}")
    print(
        f"  already committed beyond the window: "
        f"{fmt_inr(m['outflows_in_tail'])} out, net {fmt_inr(m['net_movement_after_window'])}"
    )
    print()
    sq = pos["squeeze"]
    print(f"  tightest day     {fmt_inr(sq['worst_balance_paise']):>14}   "
          f"on {sq['worst_day']} (day {sq['worst_day_index']}, "
          f"{sq['days_from_sale_start']:+d} days from the sale)")
    print(f"  days under 1L    {pos['days_below_1_lakh']:>14}")
    print(f"  days negative    {pos['days_negative']:>14}")
    print("  what drained it, within a week either side:")
    for kind, amount in sq["outflows_within_a_week_paise"].items():
        print(f"    {kind:<12} {fmt_inr(amount):>14}")
    print()
    noise = meas["noise"]
    floor = meas["noise_floor_14d"]
    print(f"  mean order value {fmt_inr(meas['mean_order_value_paise']):>14}")
    print(
        f"  noise: asked for {noise['requested_noise_on_order_count']:.0%} on order count, "
        f"got {noise['realised_noise_on_daily_revenue']:.1%} on daily revenue "
        f"({noise['days_measured']} non-promo days)"
    )
    print(
        f"  irreducible 14-day error: {fmt_inr(floor['cumulative_floor_paise'])}  "
        f"<-- no forecaster can beat this"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
