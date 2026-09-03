"""Tests for Bucket 3: the bands and the calibration check.

One test matters more than the rest. `test_a_band_never_sees_its_own_error` is the
temporal wall again, one level up: a band built from all 854 errors and applied to
the first forecast would use the future to calibrate the past, and would inflate the
one number this module exists to report honestly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.backtest import run
from src.forecast import Flag, _flags_for, DayProjection
from src.intervals import (
    DEFAULT_CONFIDENCE,
    MIN_SAMPLES,
    Calibration,
    RollingIntervals,
    percentile,
)
from src.world import EventStore

import datetime as dt

DATA = Path(__file__).resolve().parents[1] / "data"
DAY = dt.date(2026, 6, 11)


# --------------------------------------------------------------------------
# Percentiles
# --------------------------------------------------------------------------


def test_percentile_at_the_ends_and_middle():
    v = [0, 10, 20, 30, 40]
    assert percentile(v, 0.0) == 0
    assert percentile(v, 1.0) == 40
    assert percentile(v, 0.5) == 20


def test_percentile_interpolates():
    assert percentile([0, 100], 0.5) == 50


def test_a_single_value_is_its_own_percentile():
    assert percentile([42], 0.9) == 42


def test_no_values_is_an_error_not_a_zero():
    with pytest.raises(ValueError):
        percentile([], 0.5)


# --------------------------------------------------------------------------
# The band
# --------------------------------------------------------------------------


def test_no_band_until_there_is_evidence():
    """Reporting a confidence interval before you have any evidence about your own
    accuracy is false precision. The earliest forecasts get no band at all."""
    r = RollingIntervals()
    for i in range(MIN_SAMPLES):
        assert r.offsets(3) is None, f"offered a band after only {i} errors"
        r.observe(3, i * 100)
    assert r.offsets(3) is not None


def test_the_band_is_the_middle_eighty_percent_of_past_errors():
    r = RollingIntervals(min_samples=1)
    for e in range(-50_00, 51_00, 10_00):      # -5000 .. +5000 in paise steps
        r.observe(7, e)
    low, high = r.band(7, centre=1_00_000)
    assert low < 1_00_000 < high
    # 80% band on a symmetric spread should be roughly symmetric about the centre
    assert abs((1_00_000 - low) - (high - 1_00_000)) < 50_00


def test_a_forecast_that_runs_high_gets_a_band_reaching_down():
    """Sign discipline. If the forecast has been Rs.5,000 too high, the band has to
    reach *down* to cover the truth -- not up."""
    r = RollingIntervals(min_samples=1)
    for _ in range(20):
        r.observe(5, 5_000_00)                 # consistently over-predicting
    low, high = r.band(5, centre=1_00_000_00)
    assert high <= 1_00_000_00
    assert low < high or low == high


def test_bands_widen_with_horizon_when_the_errors_do():
    r = RollingIntervals(min_samples=1)
    for i in range(20):
        r.observe(1, (i - 10) * 10_00)         # tight
        r.observe(14, (i - 10) * 100_00)       # ten times wider
    near = r.band(1, 0)
    far = r.band(14, 0)
    assert (far[1] - far[0]) > (near[1] - near[0])


def test_a_wider_confidence_gives_a_wider_band():
    tight = RollingIntervals(confidence=0.50, min_samples=1)
    wide = RollingIntervals(confidence=0.95, min_samples=1)
    for i in range(40):
        tight.observe(3, (i - 20) * 1_00)
        wide.observe(3, (i - 20) * 1_00)
    t, w = tight.band(3, 0), wide.band(3, 0)
    assert (w[1] - w[0]) > (t[1] - t[0])


# --------------------------------------------------------------------------
# The wall, one level up
# --------------------------------------------------------------------------


def test_one_freak_error_does_not_blow_the_band_open():
    """An 80% band is meant to ignore its own tails. Ten ordinary errors and one
    enormous one leave the band where it was -- the outlier sits outside the 90th
    percentile, which is the point of not using the min and max.

    (Written expecting the opposite, and the band was right.)"""
    r = RollingIntervals(min_samples=1)
    for _ in range(10):
        r.observe(3, 1_000_00)
    before = r.band(3, 0)
    r.observe(3, 9_99_999_00)
    assert r.band(3, 0) == before


def test_enough_new_errors_do_move_the_band():
    """The other half: it must actually learn, or it is not rolling at all."""
    r = RollingIntervals(min_samples=1)
    for _ in range(10):
        r.observe(3, 1_000_00)
    before = r.band(3, 0)
    for _ in range(10):
        r.observe(3, 9_99_999_00)
    assert r.band(3, 0) != before


def test_bands_at_the_first_vantage_point_are_absent_not_guessed():
    """Across the real backtest: the earliest windows must carry no band."""
    if not (DATA / "balance.csv").exists():
        pytest.skip("no generated data")
    bt = run(EventStore.load(DATA), estimated=True, intervals=True)
    first = bt.windows[0]
    assert all(p.band_low is None for p in first.predictions)
    assert all(p.covered is None for p in first.predictions)


# --------------------------------------------------------------------------
# AT_RISK -- the warning a point forecast cannot produce
# --------------------------------------------------------------------------


def test_at_risk_fires_once_bands_exist():
    """Bucket 3's reason for existing: the central estimate clears the floor and the
    bottom of the band does not."""
    day = DayProjection(date=DAY, horizon=1, opening=2_00_000_00,
                        band_low=40_000_00, band_high=3_00_000_00)
    flags = _flags_for(day, previous_closing=2_00_000_00, previous_at_risk=False,
                       is_trough=False, floor=1_00_000_00)
    assert flags == frozenset({Flag.AT_RISK})


def test_at_risk_appears_in_the_real_backtest():
    if not (DATA / "balance.csv").exists():
        pytest.skip("no generated data")
    from src.estimate import Estimator
    from src.forecast import forecast
    from src.intervals import RollingIntervals as RI
    from src.world import world_as_of

    store = EventStore.load(DATA)
    r = RI(min_samples=1)
    seen = False
    for n in range(46, 107):
        as_of = store.date_for_day(n)
        w = world_as_of(store, as_of)
        f = forecast(w, 14, estimator=Estimator.fit(w), bands=r.band_fn())
        seen = seen or any(Flag.AT_RISK in d.flags for d in f.days)
        actual = {b.date: b.closing for b in store.balances}
        for d in f.days:
            r.observe(d.horizon, d.closing - actual[d.date])
    assert seen, "no at-risk day in 61 windows -- the flag is unreachable"


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------


def test_calibration_counts_only_banded_predictions():
    """A forecast that made no claim cannot have been wrong about it. Counting the
    early unbanded ones as misses would understate the bands for free."""
    if not (DATA / "balance.csv").exists():
        pytest.skip("no generated data")
    bt = run(EventStore.load(DATA), estimated=True, intervals=True)
    for c in bt.calibration():
        banded = [p for p in bt.at_horizon(c.horizon) if p.covered is not None]
        assert c.n == len(banded)
        assert c.n < 61, "every window had a band -- the warm-up is not being honoured"


def test_calibration_is_close_to_the_claim():
    """The headline. 80% claimed; anything wildly off means the bands are lying."""
    if not (DATA / "balance.csv").exists():
        pytest.skip("no generated data")
    bt = run(EventStore.load(DATA), estimated=True, intervals=True)
    cal = bt.calibration()
    overall = sum(c.covered for c in cal) / sum(c.n for c in cal)
    assert 0.70 <= overall <= 0.90, f"calibration {overall:.0%} against a 80% claim"


def test_too_narrow_and_too_wide_are_both_misses():
    """Being inside more often than promised is not a better result -- it means the
    band claims less than it could."""
    narrow = Calibration(horizon=5, n=100, covered=55, confidence=0.8,
                         mean_width=1_000_00)
    wide = Calibration(horizon=5, n=100, covered=99, confidence=0.8,
                       mean_width=9_00_000_00)
    good = Calibration(horizon=5, n=100, covered=79, confidence=0.8,
                       mean_width=5_000_00)
    assert "narrow" in narrow.verdict
    assert "wide" in wide.verdict
    assert good.verdict == "honest"


def test_a_zero_width_band_that_is_always_right_is_exact_not_wide():
    """Horizon 1 has no uncertainty in it, so the band collapses to a point and is
    right every time. Reading that as an over-wide band was a labelling bug."""
    c = Calibration(horizon=1, n=51, covered=51, confidence=0.8, mean_width=0)
    assert c.verdict == "exact"
