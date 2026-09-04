"""Bucket 2: sales and refunds that have not happened yet.

Three numbers measured from history and refitted at every vantage point: a weekday
sales average, a refund rate, and an order-to-request lag distribution. Averages and
ratios, not a model.

Refunds are the point. A refund attaches to an order that already happened, so the
projection runs forward from orders already in the books -- you never learn which
customer, and you do not need to.

Rationale for the parameter choices is in notes/design-log.md.
"""

from __future__ import annotations

import datetime as dt
import statistics as st
from collections import defaultdict
from dataclasses import dataclass, field

from . import calendar_rules as cal
from .money import Paise, apply_rate, FEE_RATE, GST_RATE
from .world import KnownWorld

#: Same-weekdays averaged for the sales baseline.
DEFAULT_WEEKS = 4

#: Order-to-cash-out lags outside this range are treated as noise rather than shape.
MAX_REFUND_LAG = 40

#: Days after a declared promotion that are also excluded from the baseline.
#: ASSUMPTION: the direction is well established in retail, this duration is not,
#: and one promotion per dataset gives nothing to fit it to. See design-log.
DIP_EXCLUSION_DAYS = 7

#: The four-weekday window's centre of gravity: 7, 14, 21 and 28 days back.
BASELINE_CENTROID_DAYS = 17.5

#: Growth outside this range is treated as an artefact of short or disturbed history
#: and the correction is skipped. Compounding a wrong rate over 17.5 days does more
#: damage than leaving the lag uncorrected.
MAX_DAILY_GROWTH = 0.02


@dataclass(frozen=True)
class Estimator:
    """What history says to expect. Refitted at every vantage point.

    "Refit" is not retraining -- there is nothing to train. It means recomputing an
    average with one more day of data in it.
    """

    as_of: dt.date
    weeks: int

    #: weekday (Mon=0) -> mean gross captured value
    weekday_sales: dict[int, Paise]
    #: fraction of order value that eventually comes back
    refund_rate: float
    #: days from order to *refund request* -> share of refund value at that lag
    lag_shape: dict[int, float]
    #: typical days from request to the cash actually leaving a payout
    netting_lag: int
    #: share of captured value taken on UPI, which settles a day sooner
    upi_share: float
    #: gross captured value per day, as far back as the wall allows
    sales_history: dict[dt.date, Paise] = field(default_factory=dict)
    #: day -> uplift multiplier, from promotions the merchant has *declared*.
    #: Empty when nothing has been announced, which is the whole experiment.
    declared_uplift: dict[dt.date, float] = field(default_factory=dict)
    #: Measured daily growth, applied forward from the baseline's centre of gravity.
    #: 0.0 when history is too short or the measurement looks implausible.
    daily_growth: float = 0.0

    # -- fitting -----------------------------------------------------------

    @classmethod
    def fit(
        cls,
        world: KnownWorld,
        weeks: int = DEFAULT_WEEKS,
        *,
        promotions_visible: bool = True,
        horizon_days: int = 30,
    ) -> "Estimator":
        """Refit from what was knowable on `world.as_of`.

        `promotions_visible=False` models a merchant who does not tell their finance
        system about a sale they have planned. It is the hidden-versus-declared
        experiment.
        """
        sales = world.captured_sales_by_day()
        skip = _unrepresentative_days(world)
        return cls(
            as_of=world.as_of,
            weeks=weeks,
            weekday_sales=_weekday_means(sales, world.as_of, weeks, skip=skip),
            daily_growth=_daily_growth(sales, world.as_of, skip),
            refund_rate=_refund_rate(world, sales),
            lag_shape=_lag_shape(world),
            netting_lag=_netting_lag(world),
            upi_share=world.upi_share(),
            sales_history=sales,
            declared_uplift=(
                _declared_uplift(world, horizon_days) if promotions_visible else {}
            ),
        )

    # -- sales -------------------------------------------------------------

    def expected_sales(self, day: dt.date) -> Paise:
        """Gross captured value expected on `day`. Zero if that weekday is unseen.

        A declared promotion multiplies the baseline by the merchant's stated uplift,
        taken at face value -- the gap between plan and outcome is itself a finding.
        The uplift covers the declared window only; the dip afterwards stays a
        surprise, because a merchant announces a sale and not its aftermath.

        The baseline is then carried forward by measured growth over the window's
        17.5-day centroid, correcting a lag measured at -4.9% of a day's sales.
        """
        base = self.weekday_sales.get(day.weekday(), 0)
        if self.daily_growth:
            base = round(base * (1.0 + self.daily_growth) ** BASELINE_CENTROID_DAYS)
        uplift = self.declared_uplift.get(day)
        return round(base * uplift) if uplift else base

    def expected_settlement(self, day: dt.date, horizon_days: int = 30) -> Paise:
        """Net cash arriving on `day` from sales that have not happened yet.

        The step people skip. A sale predicted for Tuesday does not reach the bank on
        Tuesday -- it goes through the same working-day calendar as a real one, at
        T+1 for UPI and T+2 for cards. Which is why the estimated layer contributes
        exactly nothing at horizons 1 and 2, and why those stay exact.
        """
        total = 0
        for offset in range(1, horizon_days + 1):
            source = self.as_of + dt.timedelta(days=offset)
            if source >= day:
                break
            gross = self.expected_sales(source)
            if not gross:
                continue
            upi = apply_rate(gross, _as_decimal(self.upi_share))
            card = gross - upi
            for amount, method in ((upi, "upi"), (card, "card")):
                if amount and _settles_on(source, method) == day:
                    total += _net_of_fees(amount)
        return total

    # -- refunds -----------------------------------------------------------

    def expected_refunds(
        self, day: dt.date, *, refunds_from_forecast: bool = True
    ) -> Paise:
        """Refund value leaving the bank on `day`, as a positive number.

        Pivots on the *request* date, not the cash date. A refund already asked for
        is a fact the certain layer is carrying; predicting it again subtracts the
        same money twice. Only requests that have not yet happened are estimated.

        `refunds_from_forecast` lets sales that have not happened yet contribute
        their own refunds -- predicting revenue without its attached cost is not
        conservative, it is inconsistent.
        """
        if not self.lag_shape or not self.refund_rate:
            return 0
        request_day = day - dt.timedelta(days=self.netting_lag)
        if request_day <= self.as_of:
            return 0  # already requested if it exists at all -- the certain layer has it
        total = 0.0
        for lag, share in self.lag_shape.items():
            source = request_day - dt.timedelta(days=lag)
            if source <= self.as_of:
                gross = self.sales_history.get(source, 0)
            elif refunds_from_forecast:
                gross = self.expected_sales(source)
            else:
                continue
            total += gross * self.refund_rate * share
        return round(total)


# --------------------------------------------------------------------------
# Fitting helpers
# --------------------------------------------------------------------------


def _unrepresentative_days(world: KnownWorld) -> frozenset[dt.date]:
    """Days that are not "what would have sold anyway", and so are not baseline.

    A promoted day mixes the business the shop would have done regardless with the
    extra the promotion caused, and a four-week rolling window carries that
    distortion for a month.

    Driven by *declared* promotions only, so declaring one helps twice: it predicts
    the sale week and keeps the following month's baseline clean.
    """
    out: set[dt.date] = set()
    for promo in world.promotions:
        day = promo.starts_on
        while day <= promo.ends_on:
            out.add(day)
            day += dt.timedelta(days=1)
        for offset in range(1, DIP_EXCLUSION_DAYS + 1):
            out.add(promo.ends_on + dt.timedelta(days=offset))
    return frozenset(out)


def _daily_growth(
    sales: dict[dt.date, Paise], as_of: dt.date, skip: frozenset[dt.date]
) -> float:
    """Compound daily growth, from the two four-week blocks before the vantage day.

    Two blocks rather than a fitted line: it is arithmetic anyone can check, and it
    spans whole weeks so weekday composition cancels out.

    Promotional days are excluded from both blocks, or a sale reads as growth and
    gets compounded over 17.5 days. Days before the history begins are excluded
    rather than counted as zero -- a day with no sales inside the window is real
    information, a day before the shop existed is not.
    """
    if not sales:
        return 0.0
    earliest = min(sales)

    def block(start_back: int, end_back: int) -> list[Paise]:
        days = [as_of - dt.timedelta(days=d) for d in range(start_back, end_back)]
        return [sales.get(d, 0) for d in days if d >= earliest and d not in skip]

    recent, older = block(1, 29), block(29, 57)
    if len(recent) < 14 or len(older) < 14:
        return 0.0  # not enough clean history to say anything
    a, b = st.mean(older), st.mean(recent)
    if a <= 0 or b <= 0:
        return 0.0
    # Days between the two blocks' midpoints, which is not always 28 once
    # promotional days and pre-history days have been dropped from either side.
    gap = (len(recent) + len(older)) / 2
    growth = (b / a) ** (1 / gap) - 1.0
    return growth if abs(growth) <= MAX_DAILY_GROWTH else 0.0


def _weekday_means(
    sales: dict[dt.date, Paise],
    as_of: dt.date,
    weeks: int,
    *,
    skip: frozenset[dt.date] = frozenset(),
) -> dict[int, Paise]:
    """Mean of the last `weeks` occurrences of each weekday, ending yesterday.

    Quiet days count as zero; dropping them would inflate every average.

    The window ends the day *before* the vantage day. Including today gave the
    weekday you happen to be standing on a different sample window from the other
    six -- a property of the calendar, not the business. Today is also not over yet.

    `skip` days are passed over and the window reaches further back, so the sample
    size stays constant rather than thinning during a promotion.
    """
    buckets: dict[int, list[Paise]] = defaultdict(list)
    last = as_of - dt.timedelta(days=1)
    floor = last - dt.timedelta(days=weeks * 7 + 14 + len(skip))
    for wd in range(7):
        day, seen = last, 0
        while seen < weeks and day > floor:
            if day.weekday() == wd and day not in skip:
                buckets[wd].append(sales.get(day, 0))
                seen += 1
            day -= dt.timedelta(days=1)
    return {wd: round(st.mean(v)) for wd, v in buckets.items() if v}


def _refund_rate(world: KnownWorld, sales: dict[dt.date, Paise]) -> float:
    """Refund value as a share of order value.

    Measured over orders old enough to have finished refunding. Including last
    week's orders would understate the rate, because most of their refunds have not
    happened yet -- a subtle way to be optimistic without noticing.
    """
    cutoff = world.as_of - dt.timedelta(days=MAX_REFUND_LAG)
    ordered = sum(o.amount for o in world.orders if o.placed_at.date() <= cutoff)
    if not ordered:
        return 0.0
    placed = {o.order_id: o.placed_at.date() for o in world.orders}
    returned = sum(
        r.amount for r in world.refunds
        if placed.get(r.order_id, world.as_of) <= cutoff
    )
    return returned / ordered


def _lag_shape(world: KnownWorld) -> dict[int, float]:
    """Days from order to *refund request* -> share of refund value at that lag.

    Weighted by value rather than by count: a ₹5,000 return matters ten times more
    to a cash forecast than a ₹500 one, and counting events equally would flatten
    that away.
    """
    weights: dict[int, float] = defaultdict(float)
    total = 0.0
    for placed_on, requested_on, _cash_on, amount in world.refund_history():
        lag = (requested_on - placed_on).days
        if 0 <= lag <= MAX_REFUND_LAG:
            weights[lag] += amount
            total += amount
    return {lag: w / total for lag, w in weights.items()} if total else {}


def _declared_uplift(world: KnownWorld, horizon_days: int) -> dict[dt.date, float]:
    """Day -> uplift multiplier, for promotions declared on or before the vantage day.

    Uses `expected_revenue_uplift`, never the volume figure: reading volume as if it
    were revenue over-stated a 3x sale at 30% off by 43%, and balances carried that
    error into every day afterwards.

    Overlapping promotions multiply rather than take the maximum.
    """
    out: dict[dt.date, float] = {}
    for offset in range(1, horizon_days + 1):
        day = world.as_of + dt.timedelta(days=offset)
        for promo in world.promotions_covering(day):
            out[day] = out.get(day, 1.0) * promo.expected_revenue_uplift
    return out


def _netting_lag(world: KnownWorld) -> int:
    """Typical days from a refund being requested to the cash leaving a payout.

    A median rather than a distribution: it is 1 to 3 days here against an order-to-
    request lag spanning 5 to 15, so modelling its spread would add a parameter that
    moves nothing.
    """
    lags = [
        (cash_on - requested_on).days
        for _placed, requested_on, cash_on, _amount in world.refund_history()
    ]
    return round(st.median(lags)) if lags else 1


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _as_decimal(share: float):
    from decimal import Decimal

    return Decimal(str(share))


def _settles_on(captured: dt.date, method: str) -> dt.date:
    """Settlement date for a predicted sale, assuming it happens before the cutoff."""
    return cal.settlement_date(
        dt.datetime.combine(captured, dt.time(12, 0)), method
    )


def _net_of_fees(gross: Paise) -> Paise:
    fee = apply_rate(gross, FEE_RATE)
    return gross - fee - apply_rate(fee, GST_RATE)
