"""Tests for the backtest and the accuracy report.

The backtest is what turns "it seems to work" into a number, so the things worth
testing hardest are the ones that would quietly inflate that number: scoring against
the wrong day, letting a short window contribute unevenly, or -- worst -- a baseline
that can see more than the forecaster can.

`test_baselines_cannot_see_the_future` is the important one. A baseline that peeks
would make the forecast look better or worse for a reason that has nothing to do with
the forecast.
"""

from __future__ import annotations

import datetime as dt
import statistics as st
from pathlib import Path

import pytest

from src.backtest import (
    BALANCE_BASELINES,
    DEFAULT_WARM_UP,
    RECENT_AVERAGE,
    Prediction,
    assert_no_leak,
    run,
    vantage_range,
    verify_no_leak,
)
from src.forecast import Scenario, forecast
from src.report import render
from src.world import EventStore, world_as_of

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="module")
def store() -> EventStore:
    if not (DATA / "balance.csv").exists():
        pytest.skip("no generated data -- run `python -m src.generate`")
    return EventStore.load(DATA)


@pytest.fixture(scope="module")
def bt(store):
    return run(store)


# --------------------------------------------------------------------------
# The grid
# --------------------------------------------------------------------------


def test_the_grid_is_complete_and_the_expected_size(bt):
    """61 x 14 with no gaps. The headline count has to be real."""
    assert len(bt.windows) == 61
    assert len(bt.predictions) == 854
    for h in range(1, 15):
        assert len(bt.at_horizon(h)) == 61


def test_no_window_is_scored_on_a_partial_horizon(store):
    """The last vantage point is N - horizon. Including later days would let short
    windows contribute unevenly to the per-horizon averages -- a quiet way to make
    the far end look better than it is."""
    r = vantage_range(store, horizon=14)
    assert store.date_for_day(r.stop - 1) + dt.timedelta(days=14) <= store.last_day


def test_the_warm_up_is_respected(store):
    assert vantage_range(store, 14, DEFAULT_WARM_UP).start == DEFAULT_WARM_UP + 1


def test_too_short_a_dataset_is_refused(store):
    with pytest.raises(ValueError, match="too short"):
        vantage_range(store, horizon=90, warm_up=45)


def test_each_prediction_is_scored_against_its_own_day(bt, store):
    """Off-by-one here would be invisible in the totals and wrong everywhere."""
    actual = {b.date: b.closing for b in store.balances}
    for p in bt.predictions:
        assert p.target == p.as_of + dt.timedelta(days=p.horizon)
        assert p.actual == actual[p.target]


def test_error_signs_mean_what_they_say(bt):
    p = bt.at_horizon(14)[0]
    assert p.error == p.predicted - p.actual
    assert p.abs_error == abs(p.error)


# --------------------------------------------------------------------------
# The baselines
# --------------------------------------------------------------------------


def test_baselines_cannot_see_the_future(store):
    """The one that matters. A baseline is only a fair opponent if it is held to
    the same wall as the forecaster -- so it is fed the filtered statement, never
    the raw balance file. Recomputing it here from the wall proves that."""
    for n in (46, 75, 106):
        as_of = store.date_for_day(n)
        world = world_as_of(store, as_of)
        history = [b.closing for b in world.statement]
        assert max(b.date for b in world.statement) <= as_of
        expected = round(st.mean(history[-14:]))
        w = next(w for w in run(store).windows if w.vantage_day == n)
        assert w.baseline_predictions[RECENT_AVERAGE][0].predicted == expected


def test_every_baseline_produces_a_full_grid(bt):
    for name in BALANCE_BASELINES:
        got = [p for w in bt.windows for p in w.baseline_predictions[name]]
        assert len(got) == 854, name


def test_the_published_baseline_is_the_strongest_one(bt):
    """A baseline must be the best rule that does no work, or beating it proves
    nothing. If a rival ever wins overall, the published one should change -- and
    this test is how you would find out."""
    overall = {}
    for name in BALANCE_BASELINES:
        errs = [p.abs_error for w in bt.windows for p in w.baseline_predictions[name]]
        overall[name] = st.mean(errs)
    assert min(overall, key=overall.get) == RECENT_AVERAGE, overall


def test_the_majority_baseline_is_the_bar_for_a_yes_no_metric(bt):
    """Guessing the commoner answer every time. Any yes/no metric that cannot beat
    this has measured the question's lopsidedness, not any skill."""
    breaches = sum(w.actual_breach for w in bt.windows) / len(bt.windows)
    assert bt.baselines().breach_majority == pytest.approx(max(breaches, 1 - breaches))
    assert bt.breach_accuracy() > bt.baselines().breach_majority


# --------------------------------------------------------------------------
# What the certain layer actually scores
# --------------------------------------------------------------------------


def test_horizon_1_scores_zero(bt):
    assert bt.by_horizon()[0].mae == 0


def test_the_error_grows_with_horizon(bt):
    maes = [r.mae for r in bt.by_horizon()]
    assert maes == sorted(maes), "error should never improve with distance"


def test_the_bias_is_one_directional(bt):
    """Not noise. Every horizon past the first is systematically low, because the
    certain layer counts fourteen days of bills against two days of income."""
    errs = [r.mean_error for r in bt.by_horizon()[1:]]
    assert all(e < 0 for e in errs)


def test_the_certain_layer_loses_to_a_do_nothing_rule_eventually(bt):
    """Recorded so the crossover is a measured fact rather than a claim. If Bucket 2
    ever pushes this past horizon 14, this test fails and that is the good news."""
    rows = bt.by_horizon()
    beaten = [r.horizon for r in rows if r.baseline_mae[RECENT_AVERAGE] < r.mae]
    assert beaten, "the certain layer never loses -- has Bucket 2 landed?"
    assert min(beaten) >= 4, "it should still win the near horizons"


def test_bucket_one_is_one_hundred_percent_certain_everywhere(bt):
    assert all(r.mean_certain_share == 1.0 for r in bt.by_horizon())
    assert bt.scenario is Scenario.SALES_STOP


def test_an_estimated_run_labels_itself_as_a_forecast(store):
    """It briefly did not, and printed "sales-stop (certain layer only)" as a
    heading over a table of Bucket 2 numbers. A report that misdescribes its own
    contents is worse than no report."""
    est = run(store, estimated=True)
    assert est.scenario is Scenario.FORECAST
    assert "sales-stop" not in render(est)


def test_the_estimated_layer_beats_the_do_nothing_rule_everywhere(store):
    """Bucket 1 alone lost from horizon 6. This is the measured claim that the
    estimated layer is what makes the forecast worth having."""
    for r in run(store, estimated=True).by_horizon():
        assert r.mae < r.baseline_mae[RECENT_AVERAGE], f"horizon {r.horizon}"


def test_the_estimated_layer_does_not_touch_horizon_one(store):
    """Predicted sales settle through the working-day calendar, so nothing
    estimated can land tomorrow -- and horizon 1 must stay exact."""
    rows = run(store, estimated=True).by_horizon()
    assert rows[0].mae == 0
    assert rows[0].mean_certain_share == 1.0


# --------------------------------------------------------------------------
# The leak test in its final form
# --------------------------------------------------------------------------


def test_forecasts_are_identical_with_the_future_deleted(store):
    for n in (46, 60, 75, 106):
        assert verify_no_leak(store, store.date_for_day(n), 14) == []


def test_assert_no_leak_covers_every_vantage_point(store):
    days = [store.date_for_day(n) for n in vantage_range(store)]
    assert_no_leak(store, days, 14)


def test_a_leak_would_be_reported_with_the_day_and_the_amount(store):
    """A1: "outputs differ" is useless at eleven at night. Simulated by diffing two
    genuinely different forecasts, since a real leak cannot be manufactured."""
    from src.world import diff_daily

    a = forecast(world_as_of(store, store.date_for_day(46)), 14)
    b = forecast(world_as_of(store, store.date_for_day(47)), 14)
    diffs = diff_daily(a.closing_series(), b.closing_series())
    assert diffs
    assert "off by" in str(diffs[0]) and str(diffs[0].day) in str(diffs[0])


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def test_the_published_report_has_five_columns(bt):
    header = [l for l in render(bt).splitlines() if l.strip().startswith("h ")][0]
    assert header.split() == ["h", "n", "MAE", "mean", "err", "certain", "baseline"]


def test_the_report_never_prints_a_pooled_total(bt):
    """One number across all horizons would let the near end flatter the far end.
    There must be fourteen rows and no total."""
    text = render(bt).lower()
    assert "total" not in text and "overall" not in text


def test_every_metric_is_printed_with_its_baseline(bt):
    text = render(bt)
    assert "trivial rule:" in text
    assert "always guessing the commoner answer:" in text
    assert "baseline" in text


def test_the_summary_is_derived_from_the_numbers(bt):
    """The prose is generated, not written down, so it cannot go stale when the
    numbers move."""
    text = render(bt)
    assert "Horizon 1 is exact" in text
    assert "biased low" in text
    rows = bt.by_horizon()
    first_loss = min(r.horizon for r in rows if r.baseline_mae[RECENT_AVERAGE] < r.mae)
    assert f"From horizon {first_loss} onward" in text


def test_dev_output_is_off_by_default_and_adds_the_rest(bt):
    plain, dev = render(bt), render(bt, dev=True)
    assert "dev view" not in plain
    assert "median" not in plain
    assert "dev view" in dev and "skew" in dev
    for name in BALANCE_BASELINES:
        assert name in dev
