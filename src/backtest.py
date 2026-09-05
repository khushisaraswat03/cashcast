"""Scoring the forecast against what actually happened.

Stand on day 46, forecast fourteen days, look up the real balance. Slide to day 47
and repeat: 61 vantage points x 14 horizons = 854 predictions, grouped by horizon
and never pooled.

Three claims are scored -- the balance, the trough (did it name the right worst
day), and the breach (does the path fall below what is owed) -- each beside a lazy
baseline. A metric a trivial heuristic already wins is not one to lead with.

`verify_no_leak` forecasts from the full store and from one with every later event
deleted, and asserts the two are identical.
"""

from __future__ import annotations

import datetime as dt
import statistics as st
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .estimate import DEFAULT_WEEKS, Estimator
from .forecast import DEFAULT_HORIZON, Forecast, Scenario, forecast
from .intervals import DEFAULT_CONFIDENCE, Calibration, RollingIntervals
from .money import Paise
from .world import Divergence, EventStore, describe, diff_daily, world_as_of

#: The forecaster is never scored before this day. Not a training cut-off -- there
#: is no model to train -- but the point at which enough Thursdays have been seen
#: for a weekday average to mean anything, and enough refund histories are complete.
#: Scoring earlier would produce bad forecasts you would then be tempted to exclude,
#: which is cherry-picking with extra steps.
DEFAULT_WARM_UP = 45


@dataclass(frozen=True)
class Prediction:
    """One forecast/actual pair. One cell of the 61 x 14 grid."""

    vantage_day: int
    as_of: dt.date
    horizon: int
    target: dt.date
    predicted: Paise
    actual: Paise
    #: How much of this day was known rather than estimated, at the time.
    certain_share: float
    #: The 80% band, or None where there was not yet enough error history.
    band_low: Paise | None = None
    band_high: Paise | None = None

    @property
    def covered(self) -> bool | None:
        """Did the truth land inside the band? None where no band was offered."""
        if self.band_low is None or self.band_high is None:
            return None
        return self.band_low <= self.actual <= self.band_high

    @property
    def band_width(self) -> Paise | None:
        if self.band_low is None or self.band_high is None:
            return None
        return self.band_high - self.band_low

    @property
    def error(self) -> Paise:
        """Signed. Negative means the forecast was too pessimistic."""
        return self.predicted - self.actual

    @property
    def abs_error(self) -> Paise:
        return abs(self.error)


@dataclass(frozen=True)
class WindowResult:
    """One vantage point: fourteen predictions plus the two decisions."""

    vantage_day: int
    as_of: dt.date
    floor: Paise
    predictions: tuple[Prediction, ...]

    predicted_trough: int
    actual_trough: int
    predicted_breach: bool
    actual_breach: bool

    #: What a rule with no forecasting would have said. Never published by default.
    lazy_trough: int | None
    #: Rule name -> its predictions for this window. Only one is published.
    baseline_predictions: dict[str, tuple[Prediction, ...]] = field(
        default_factory=dict
    )

    @property
    def trough_correct(self) -> bool:
        return self.predicted_trough == self.actual_trough

    @property
    def lazy_trough_correct(self) -> bool:
        return self.lazy_trough == self.actual_trough

    @property
    def breach_correct(self) -> bool:
        return self.predicted_breach == self.actual_breach


@dataclass(frozen=True)
class HorizonScore:
    """One row of the accuracy table. Never an average across horizons.

    Carries more than the report prints. Computing a number costs one line and
    cannot break; printing it costs a column a human has to read and defend. So
    everything cheap is computed here and `report.py` decides what to show --
    `median` and `worst` are currently only in the dev view, because the hypothesis
    that justified them (a few sale-week days dragging the mean above the typical
    case) has so far been contradicted by the data.
    """

    horizon: int
    n: int
    mae: Paise
    median_abs: Paise
    worst: Paise
    mean_error: Paise
    mean_certain_share: float
    baseline_mae: dict[str, Paise] = field(default_factory=dict)

    @property
    def skew(self) -> float:
        """MAE divided by the median. Above ~1.2 means a few days are much worse
        than typical -- which would point at the sale week for free. Currently ~1.0
        at every horizon: the systematic bias is large enough to drown it."""
        return float("inf") if self.median_abs == 0 else self.mae / self.median_abs


@dataclass(frozen=True)
class Baselines:
    """What no forecasting at all would score.

    A baseline has to be the *strongest* rule that does no real work, or beating it
    proves nothing -- claiming to be fast because you outran your grandmother. Five
    candidates were tested for the trough and three for the balance; the winners are
    what appear here.
    """

    trough_lazy: float
    breach_majority: float
    #: rule name -> {horizon: MAE}
    balance_mae: dict[str, dict[int, Paise]]


@dataclass(frozen=True)
class Backtest:
    scenario: Scenario
    horizon: int
    windows: tuple[WindowResult, ...]
    confidence: float = DEFAULT_CONFIDENCE

    @property
    def predictions(self) -> tuple[Prediction, ...]:
        return tuple(p for w in self.windows for p in w.predictions)

    def at_horizon(self, h: int) -> tuple[Prediction, ...]:
        return tuple(p for p in self.predictions if p.horizon == h)

    # -- scores ------------------------------------------------------------

    def by_horizon(self) -> tuple[HorizonScore, ...]:
        rows = []
        for h in range(1, self.horizon + 1):
            ps = self.at_horizon(h)
            if not ps:
                continue
            errs = sorted(p.abs_error for p in ps)
            base = {}
            for name in BALANCE_BASELINES:
                vals = [
                    p.abs_error
                    for w in self.windows
                    for p in w.baseline_predictions.get(name, ())
                    if p.horizon == h
                ]
                if vals:
                    base[name] = round(st.mean(vals))
            rows.append(
                HorizonScore(
                    horizon=h,
                    n=len(ps),
                    mae=round(st.mean(errs)),
                    median_abs=round(st.median(errs)),
                    worst=errs[-1],
                    mean_error=round(st.mean([p.error for p in ps])),
                    mean_certain_share=st.mean([p.certain_share for p in ps]),
                    baseline_mae=base,
                )
            )
        return tuple(rows)

    def calibration(self) -> tuple[Calibration, ...]:
        """Did the 80% band contain the truth 80% of the time, per horizon?

        Only predictions that were actually given a band are counted. The early
        vantage points, which had no error history to build one from, are excluded
        rather than scored as misses -- they made no claim, so there is nothing to
        check.
        """
        rows = []
        for h in range(1, self.horizon + 1):
            ps = [p for p in self.at_horizon(h) if p.covered is not None]
            if not ps:
                continue
            rows.append(
                Calibration(
                    horizon=h,
                    n=len(ps),
                    covered=sum(1 for p in ps if p.covered),
                    confidence=self.confidence,
                    mean_width=round(st.mean([p.band_width for p in ps])),
                )
            )
        return tuple(rows)

    def trough_accuracy(self) -> float:
        return _share(w.trough_correct for w in self.windows)

    def breach_accuracy(self) -> float:
        return _share(w.breach_correct for w in self.windows)

    def baselines(self) -> Baselines:
        breaches = _share(w.actual_breach for w in self.windows)
        rows = self.by_horizon()
        return Baselines(
            trough_lazy=_share(w.lazy_trough_correct for w in self.windows),
            #: Always guessing the commoner answer. The bar a yes/no metric must clear.
            breach_majority=max(breaches, 1 - breaches),
            balance_mae={
                name: {r.horizon: r.baseline_mae[name] for r in rows
                       if name in r.baseline_mae}
                for name in BALANCE_BASELINES
            },
        )


def _share(flags: Iterable[bool]) -> float:
    flags = list(flags)
    return sum(flags) / len(flags) if flags else 0.0


# --------------------------------------------------------------------------
# Running it
# --------------------------------------------------------------------------


def vantage_range(
    store: EventStore, horizon: int = DEFAULT_HORIZON, warm_up: int = DEFAULT_WARM_UP
) -> range:
    """Days that have both enough history behind them and a full horizon ahead.

    The upper bound is what keeps every cell of the grid a fair comparison: the last
    vantage point is `N - horizon`, so no forecast is scored on a partial window.
    Including later days would let short windows contribute unevenly to the averages.
    """
    last = len(store.balances) - horizon
    if last < warm_up + 1:
        raise ValueError(
            f"{len(store.balances)} days is too short for a {horizon}-day horizon "
            f"after a {warm_up}-day warm-up"
        )
    return range(warm_up + 1, last + 1)


# --------------------------------------------------------------------------
# Baselines: what you get without forecasting anything
# --------------------------------------------------------------------------
#
# Each rule sees only the closing balances up to the vantage day, from the
# wall-filtered statement -- no settlement dates, fees or events -- so "contains no
# forecasting" is structural rather than a promise.
#
# RECENT_AVERAGE is published because it is the strongest, not the first one tried.
# The trend rules are kept because their failure is instructive: extrapolating a
# slope through a saw-toothed balance amplifies the wobble, and trend-28 is worse
# than doing nothing.

RECENT_AVERAGE = "recent average"
_AVERAGE_WINDOW = 14
_TREND_WINDOW = 28


def _recent_average(history: Sequence[Paise], horizon: int) -> Paise:
    """"You will be around your recent typical level." Ignores today entirely,
    which costs it at horizon 1 and wins it everything past day 3, because the
    balance zigzags around a level rather than trending."""
    window = history[-_AVERAGE_WINDOW:]
    return round(st.mean(window))


def _nothing_changes(history: Sequence[Paise], horizon: int) -> Paise:
    """Persistence: tomorrow is like today. The obvious lazy rule, and the one
    weather forecasters find hard to beat."""
    return history[-1]


def _trend(history: Sequence[Paise], horizon: int) -> Paise:
    window = history[-(_TREND_WINDOW + 1):]
    if len(window) < 2:
        return history[-1]
    slope = (window[-1] - window[0]) / (len(window) - 1)
    return round(window[-1] + slope * horizon)


BALANCE_BASELINES = {
    RECENT_AVERAGE: _recent_average,
    "nothing changes": _nothing_changes,
    f"trend ({_TREND_WINDOW}d)": _trend,
}


def _lazy_trough(f: Forecast) -> int | None:
    """"The worst day is the day of the biggest bill." No forecasting at all.

    Uses only what the merchant can see, so it is a fair opponent rather than a
    straw man -- which is the point. It scores 74%.
    """
    with_out = [d for d in f.days if d.certain_out < 0]
    if not with_out:
        return None
    return min(with_out, key=lambda d: (d.certain_out, d.horizon)).horizon


def run(
    store: EventStore,
    horizon: int = DEFAULT_HORIZON,
    warm_up: int = DEFAULT_WARM_UP,
    *,
    scenario: Scenario = Scenario.SALES_STOP,
    estimated: bool = False,
    weeks: int = DEFAULT_WEEKS,
    refunds_from_forecast: bool = True,
    promotions_visible: bool = True,
    intervals: bool = False,
    confidence: float = DEFAULT_CONFIDENCE,
) -> Backtest:
    """Forecast from every eligible vantage point and score it.

    With `estimated`, an `Estimator` is refitted at each vantage point from that
    day's `KnownWorld` -- never once up front. Fitting once would let the last
    vantage point's estimator have been trained on data the first one could not see,
    which is a temporal leak wearing a different hat.
    """
    # forecast() flips the label when given an estimator; do the same here, or the
    # report prints "sales-stop" over a table of forecast numbers.
    if estimated and scenario is Scenario.SALES_STOP:
        scenario = Scenario.FORECAST

    actual = {b.date: b.closing for b in store.balances}
    windows: list[WindowResult] = []

    # Rolling, and the ordering is the honesty guarantee: bands for vantage point V
    # are built only from errors observed at vantage points before V, and V's own
    # errors are recorded only after it has been scored. Pooling all 854 errors and
    # applying them everywhere would use the future to calibrate the past.
    bands = RollingIntervals(confidence=confidence) if intervals else None

    for n in vantage_range(store, horizon, warm_up):
        as_of = store.date_for_day(n)
        world = world_as_of(store, as_of)
        est = (
            Estimator.fit(
                world, weeks,
                promotions_visible=promotions_visible,
                horizon_days=horizon,
            )
            if estimated
            else None
        )
        f = forecast(
            world, horizon, scenario=scenario, estimator=est,
            refunds_from_forecast=refunds_from_forecast,
            bands=bands.band_fn() if bands else None,
        )

        predictions = tuple(
            Prediction(
                vantage_day=n, as_of=as_of, horizon=d.horizon, target=d.date,
                predicted=d.closing, actual=actual[d.date],
                certain_share=d.certain_share,
                band_low=d.band_low, band_high=d.band_high,
            )
            for d in f.days
        )
        if bands is not None:
            for p in predictions:
                bands.observe(p.horizon, p.error)
        # The baselines see only the wall-filtered statement, so they are held to
        # exactly the same "cannot see the future" rule as the forecaster.
        history = [b.closing for b in world.statement]
        baseline_preds = {
            name: tuple(
                Prediction(
                    vantage_day=n, as_of=as_of, horizon=d.horizon, target=d.date,
                    predicted=rule(history, d.horizon), actual=actual[d.date],
                    certain_share=0.0,
                )
                for d in f.days
            )
            for name, rule in BALANCE_BASELINES.items()
        }

        actual_low = min(f.days, key=lambda d: actual[d.date])
        windows.append(
            WindowResult(
                vantage_day=n,
                as_of=as_of,
                floor=f.floor,
                predictions=predictions,
                predicted_trough=f.trough().horizon,
                actual_trough=actual_low.horizon,
                predicted_breach=f.breaches_floor(),
                actual_breach=actual[actual_low.date] < f.floor,
                lazy_trough=_lazy_trough(f),
                baseline_predictions=baseline_preds,
            )
        )

    return Backtest(
        scenario=scenario, horizon=horizon, windows=tuple(windows),
        confidence=confidence,
    )


# --------------------------------------------------------------------------
# The leak test, in its final form
# --------------------------------------------------------------------------


def verify_no_leak(
    store: EventStore, day: dt.date, horizon: int = DEFAULT_HORIZON
) -> list[Divergence]:
    """Forecast twice -- once with the future present, once with it deleted.

    The wall says the forecaster cannot see past `day`. This proves it: if any
    future event were reachable, physically removing it would change the answer.
    An empty list means the two runs were identical.
    """
    full = forecast(world_as_of(store, day), horizon)
    blind = forecast(world_as_of(store.truncated_to(day), day), horizon)
    return diff_daily(full.closing_series(), blind.closing_series())


def assert_no_leak(store: EventStore, days: Sequence[dt.date], horizon: int) -> None:
    for day in days:
        diffs = verify_no_leak(store, day, horizon)
        if diffs:
            raise AssertionError(f"temporal leak at {day}:\n{describe(diffs)}")
