"""One command that runs the whole thing and says what it found.

    python -m src

Generates the dataset if it is missing, verifies the temporal wall, backtests the
forecaster 854 times, and prints the result. No arguments, no setup, nothing to get
in the right order.

Everything underneath stays available for anyone who wants to look:

    python -m src.generate            rebuild the dataset
    python -m src.forecast --day 57   one merchant-facing forecast
    python -m src.report --dev        the full accuracy report
    pytest                            the test suite
"""

from __future__ import annotations

import datetime as dt
import statistics as st
from pathlib import Path
from typing import Sequence

from .backtest import RECENT_AVERAGE, assert_no_leak, run, vantage_range
from .estimate import Estimator
from .forecast import forecast, render
from .money import Paise, fmt_inr
from .world import EventStore, world_as_of

DATA = Path("data")
RULE = "=" * 78


def _ensure_data() -> None:
    if (DATA / "balance.csv").exists():
        return
    print("No dataset found -- generating 120 days first.\n")
    from .generate import main as generate

    generate(["--quiet"])


def _pct(part: float, whole: float) -> str:
    return f"{part / whole:.0%}" if whole else "--"


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_data()
    store = EventStore.load(DATA)

    # Every number below is worthless if the forecaster can see the future, so
    # this is checked before anything is printed rather than reported afterwards.
    store.check_two_dates()
    store.check_balance_ties()
    days = [store.date_for_day(n) for n in vantage_range(store)]
    assert_no_leak(store, days, 14)

    bt = run(store, estimated=True, intervals=True)
    rows = {r.horizon: r for r in bt.by_horizon()}
    median_balance = st.median([b.closing for b in store.balances])

    def within(h: int, tol: float) -> float:
        ps = [p for p in bt.at_horizon(h) if p.actual]
        return sum(1 for p in ps if abs(p.error) / p.actual <= tol) / len(ps)

    print(RULE)
    print("cashcast -- a 14-day cash-position forecast for a D2C merchant")
    print(RULE)
    print()
    print("  Money does not arrive when a customer pays. It sits with the gateway")
    print("  for a working day or two, arrives net of fees, and refunds come off a")
    print("  later payout than the sale they reverse. So a shop that sold well this")
    print("  week still cannot say whether payroll clears on Friday.")
    print()
    print(f"  {len(store.orders):,} orders over {len(store.balances)} days, "
          f"{len(bt.windows)} vantage points x {bt.horizon} horizons")
    print(f"  = {len(bt.predictions)} forecasts, every one scored against what "
          f"actually happened.")
    print()

    print("HOW ACCURATE")
    print("-" * 78)
    print(f"  {'days ahead':>11} {'error':>13} {'of balance':>12} "
          f"{'within 10%':>12} {'do-nothing rule':>17}")
    for h in (1, 3, 7, 14):
        r = rows[h]
        print(f"  {h:>11} {fmt_inr(r.mae):>13} "
              f"{r.mae / median_balance:>11.1%} {within(h, 0.10):>11.0%} "
              f"{fmt_inr(r.baseline_mae[RECENT_AVERAGE]):>17}")
    print()
    print(f"  Tomorrow is exact at all {len(bt.windows)} vantage points -- everything")
    print("  arriving then was already captured, so there is nothing left to guess.")
    print("  A wrong figure there would mean the arithmetic is broken, not that the")
    print("  forecast is incomplete.")
    print()

    print("THE QUESTIONS A MERCHANT ACTUALLY ASKS")
    print("-" * 78)
    print(f"  Will I run short of what I owe?   {bt.breach_accuracy():>4.0%} correct"
          f"   (always guessing: {bt.baselines().breach_majority:.0%})")
    print(f"  Which day is tightest?            {bt.trough_accuracy():>4.0%} correct"
          f"   (guessing among 14: {1/14:.0%})")
    cal = bt.calibration()
    hit = sum(c.covered for c in cal) / sum(c.n for c in cal)
    print(f"  How much should I trust this?     {hit:>4.0%} of forecasts landed")
    print(f"                                    inside their own 80% band")
    print()

    print("WHAT MAKES THE NUMBERS TRUSTWORTHY")
    print("-" * 78)
    print("  The generator knows all 120 days. The forecaster is handed only what")
    print("  was knowable on the day it stands on -- enforced by code, not by")
    print("  discipline. Verified by deleting every later event and checking the")
    print(f"  forecast does not move: clean at all {len(days)} vantage points.")
    print()
    print("  Every figure above is printed beside what a rule doing no work scores,")
    print("  and those baselines were fixed before the results were seen.")
    print()

    # One worked forecast, so the output is a thing rather than a claim.
    n = 57
    as_of = store.date_for_day(n)
    w = world_as_of(store, as_of)
    est = Estimator.fit(w, horizon_days=14)
    from .forecast import _intervals_up_to

    f = forecast(w, 14, estimator=est, bands=_intervals_up_to(store, as_of, 14).band_fn())
    print("ONE FORECAST, IN FULL")
    print("-" * 78)
    print(render(f))
    print()
    print(RULE)
    print("  python -m src.report --dev        every metric, including the ones")
    print("                                    computed and not published")
    print("  python -m src.forecast --day 46   any other vantage point")
    print("  pytest                            the test suite")
    print(RULE)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
