"""Bucket 2: the things that have not happened yet.

Three numbers, all measured from history, none of them a model. No weights, no
fitting loop, no training artifact -- they are recomputed from scratch at every
vantage point, which is what a real system does every morning.

**Sales.** Average the last four same-weekdays. Grouped by weekday because Saturday
takes 2.3x what Tuesday does in this business, so averaging across days would leak a
big Saturday into a Tuesday forecast.

**Refunds.** The interesting one, and the reason this bucket exists. A refund is not a
new random event -- it attaches to an order that already happened. So the projection
runs forward from orders already in the books: measure what fraction of order value
comes back (12.7% here) and how long it takes (a hump from 6 to 18 days, peaking
around 8), then spread that shape forward. You never learn which customer. You do not
need to.

Deliberately crude, and that is defensible: with 120 days of data whose sales pattern
was chosen by the generator, anything cleverer would be measuring the generator's
random-number choices. A weekday average is auditable; a fitted model is not, and in
finance a number nobody can explain is a number nobody can sign off.

Two decisions live here, both Garvita's:

* **Four weekdays, unweighted.** Not because it is best -- it lags a growing business
  and will under-predict -- but because that bias is a finding. Measure it, fix it,
  measure the improvement. Starting with the fix means never showing it was worth
  anything.
* **Predicted sales generate predicted refunds** (`refunds_from_forecast`). Predicting
  revenue without predicting the cost attached to it is not conservative, it is
  inconsistent -- it swaps one bias for another. Kept as a flag so the difference is
  measured rather than argued.
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

#: Days after a declared promotion ends that are also treated as unrepresentative.
#:
#: ASSUMPTION, and the honest description of it is that the *direction* is
#: well-established -- demand pulled forward by a sale leaves the following period
#: thin -- while this specific length is a choice. It cannot be validated here:
#: there is one promotion per dataset, so there is nothing to fit it to.
#:
#: Chosen on a dataset whose dip happened to be exactly seven days, where it made
#: the residual bias almost perfectly uniform (spread of Rs.228 between clean and
#: polluted windows). On a held-out dataset with a fourteen-day dip that spread was
#: Rs.2,798 -- worse than not excluding anything. So the *consistency* argument for
#: this value was an artifact of the data it was chosen on.
#:
#: It is kept anyway, for a reason that did survive: on windows with no promotion
#: nearby it excludes nothing, so the underlying growth bias passes through
#: unchanged and stays measurable. A median baseline scores similarly on absolute
#: error but adds its own downward bias to clean windows, which would contaminate
#: the growth correction below.
DIP_EXCLUSION_DAYS = 7

#: The four-weekday window's centre of gravity: 7, 14, 21 and 28 days back.
BASELINE_CENTROID_DAYS = 17.5

#: A measured daily growth rate outside this range is treated as a artefact of a
#: short or disturbed history rather than a fact about the business, and the
#: correction is skipped. Compounding a wrong growth rate over 17.5 days does more
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

        `promotions_visible` is the hidden-versus-declared experiment, and it is a
        switch here rather than in the data because the wall already does the real
        work: a promotion the merchant has not announced yet has a `declared_at`
        after the vantage day, so `world.promotions_covering` cannot see it either
        way. Turning this off models a merchant who declines to tell their finance
        system about a sale they have planned.
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

        A declared promotion multiplies the weekday baseline by the uplift the
        merchant stated. Taken at face value deliberately: they said 3x, so the
        forecast says 3x. Damping it would mean inventing a correction factor to
        defend, and would hide the more interesting result -- the gap between what
        a merchant plans and what happens is itself a finding, and it only shows up
        if the plan is used as given.

        The uplift applies to the declared window only. A merchant announces a sale;
        they do not announce the quiet week afterwards, so the dip stays a surprise.

        **Growth correction.** The four-weekday window looks 7, 14, 21 and 28 days
        back, so its centre of gravity sits 17.5 days in the past -- it describes a
        smaller business than the one that exists today, and under-predicts by a
        little, every single day. Measured at Rs.970/day, or -4.9% of a day's sales,
        against -6.1% predicted from the growth rate and the centroid.

        So the baseline is carried forward by the measured growth over that distance.
        Both numbers are measured rather than tuned, which is why this is a
        derivation rather than a knob.
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
        """Refund value leaving the bank on `day`, in paise, as a positive number.

        Every past order carries a probability of a customer asking for money back
        later. Sum that across all orders and you get an expected outflow -- without
        knowing a single customer.

        **Pivots on the request date, not the cash date.** A refund a customer has
        already asked for is a fact, and the certain layer is already carrying it;
        predicting it again subtracts the same money twice. So only requests that
        have *not yet happened* are estimated -- `request_day > as_of`. Getting this
        wrong cost horizon 1 its exactness, which is precisely the invariant that
        exists to catch it.

        With `refunds_from_forecast`, orders that have not happened yet contribute
        too: a sale predicted for tomorrow can have its refund land inside a 14-day
        window, and predicting revenue without the cost attached to it is not
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

    A promoted day contains two things mixed together: the business the shop would
    have done regardless, and the extra the promotion caused. Averaging it in
    estimates the baseline from data that is not baseline -- and because a rolling
    window is four weeks wide, one sale day distorts the estimate for a month.

    Driven entirely by *declared* promotions, so a sale the merchant never mentioned
    cannot be excluded. That is not a limitation to apologise for: it means declaring
    a promotion helps twice, once by predicting the sale week and again by keeping
    the following month's baseline clean.
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

    Two blocks rather than a fitted line: comparing four weeks against the four
    before it is arithmetic anyone can check, and it spans whole weeks so weekday
    composition cancels out rather than needing to be modelled.

    Promotional days are excluded from both blocks. Leaving them in would read a
    sale as growth and then compound that over 17.5 days -- turning a correction
    into a much larger error than the one it was fixing.

    Days before the history begins are excluded, not counted as zero. Reading them
    as quiet trading days made the older block look far smaller than it was and the
    growth rate enormous -- up to 1.9%/day against a true 0.26%, which compounds to
    a +39% "correction" at the earliest vantage points. A day with no sales inside
    the window is real information; a day before the shop existed is not.
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

    Days with no sales at all still count as zero -- dropping them would quietly
    inflate every weekday average by ignoring the quiet days.

    The window ends the day *before* the vantage day, never on it. Counting from
    `as_of` gave the weekday you happen to be standing on a different sample window
    from the other six: today plus three previous, against four previous. A
    Wednesday forecast made on a Wednesday would then be built from partly
    different history than the same forecast made on a Thursday, which is a
    property of the calendar rather than of the business. Today is also, in any
    real deployment, a day that is not over yet.

    Days in `skip` are passed over and the window reaches further back for a clean
    replacement, so the sample size stays at `weeks`. Dropping them instead would
    leave three samples instead of four for a month -- noisier precisely when
    everything else is unusual too.
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
    """Day -> uplift multiplier, for promotions announced on or before the vantage day.

    Reads through `promotions_covering`, so the wall decides visibility. Nothing here
    knows whether a promotion exists until the merchant has declared it.

    Uses `expected_revenue_uplift`, never the volume figure. A cash forecast needs
    revenue, and volume x discount is the only thing that gives it -- reading the
    volume number as if it were revenue over-stated a 3x sale at 30% off by 43%, and
    because balances accumulate that error leaked into every day after the sale.

    Overlapping promotions multiply rather than take the maximum: two independent
    reasons for people to buy compound, and taking the larger would silently discard
    one of them.
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
