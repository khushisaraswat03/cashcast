"""The accuracy report: does this thing work?

The second of the project's two reports, and the one the submission rests on. The
merchant report in `forecast.py` answers *"will the money be there"* for one day and
carries no accuracy figures at all -- a controller deciding whether to pay a supplier
has no use for them. This one answers *"should you believe any of that"* and carries
nothing else.

Five columns, deliberately. `Backtest` computes considerably more, because computing
a number costs one line and cannot break, while printing it costs a column that a
human reads and has to defend under questioning. The rest is behind `--dev`, for use
while the thing is being built. Nothing is thrown away; little is published.

**Never pooled across horizons.** Tomorrow's forecast is nearly free -- the money is
already in the pipe -- and a fortnight's is mostly guesswork. One averaged number
would describe neither, and would let the easy end flatter the hard end. That is the
unearned metric this file exists to avoid, so the table is fourteen rows and there is
no total.

Every metric is printed with the score of a rule that does no work. That comparison
was fixed *before* the results were seen, and all three are printed whether they
flatter or not. Choosing after the fact which metrics to publish is cherry-picking,
which is precisely what the brief's *"one cherry-picked match proves nothing"* rules
out.
"""

from __future__ import annotations

from typing import Sequence

from .backtest import (
    DEFAULT_WARM_UP,
    RECENT_AVERAGE,
    Backtest,
    assert_no_leak,
    run,
    vantage_range,
)
from .forecast import DEFAULT_HORIZON
from .money import Paise, fmt_inr
from .world import EventStore


def _bar(width: int = 78) -> str:
    return "-" * width


def render(bt: Backtest, *, dev: bool = False) -> str:
    rows = bt.by_horizon()
    base = bt.baselines()
    n_windows = len(bt.windows)

    lines = [
        "cashcast -- accuracy report",
        _bar(),
        f"scenario   {bt.scenario.value}",
        f"measured   {n_windows} vantage points x {bt.horizon} horizons "
        f"= {len(bt.predictions)} predictions",
        f"baseline   \"{RECENT_AVERAGE}\" -- mean of the last 14 days' closing "
        f"balance, which sees no events at all",
        "",
        f"  {'h':>3} {'n':>4} {'MAE':>14} {'mean err':>14} {'certain':>8} "
        f"{'baseline':>14}",
    ]
    for r in rows:
        lines.append(
            f"  {r.horizon:>3} {r.n:>4} {fmt_inr(r.mae):>14} "
            f"{fmt_inr(r.mean_error):>14} {r.mean_certain_share:>7.0%} "
            f"{fmt_inr(r.baseline_mae.get(RECENT_AVERAGE, 0)):>14}"
        )

    lines += [
        "",
        f"  worst day named   {bt.trough_accuracy():>4.0%}   "
        f"(trivial rule: {base.trough_lazy:.0%})",
        f"  breach called     {bt.breach_accuracy():>4.0%}   "
        f"(always guessing the commoner answer: {base.breach_majority:.0%})",
    ]

    cal = bt.calibration()
    if cal:
        overall_n = sum(c.n for c in cal)
        overall_hit = sum(c.covered for c in cal) / overall_n
        lines += [
            "",
            f"calibration -- does the {bt.confidence:.0%} band mean {bt.confidence:.0%}?",
            _bar(),
            f"  {'h':>3} {'n':>4} {'inside':>8} {'band width':>14}  verdict",
        ]
        for c in cal:
            lines.append(
                f"  {c.horizon:>3} {c.n:>4} {c.hit_rate:>7.0%} "
                f"{fmt_inr(c.mean_width):>14}  {c.verdict}"
            )
        lines.append(
            f"  overall {overall_hit:.0%} of {overall_n} banded predictions "
            f"landed inside a {bt.confidence:.0%} band"
        )

    lines += ["", *_read_this(bt, rows, base)]

    if dev:
        lines += ["", *_dev(bt, rows, base)]
    return "\n".join(lines)


def _read_this(bt: Backtest, rows, base) -> list[str]:
    """The three sentences someone should take away, generated from the numbers
    rather than written down -- so they cannot go stale when the numbers move."""
    out = ["how to read this", _bar()]

    exact = [r.horizon for r in rows if r.mae == 0]
    if exact:
        out.append(
            f"  Horizon {max(exact)} is exact at every vantage point. Everything "
            f"landing that soon was already captured, so there is nothing left to "
            f"guess -- any error at all would mean the walk itself is wrong."
        )

    beaten = [r for r in rows if r.baseline_mae.get(RECENT_AVERAGE, 0) < r.mae]
    if beaten:
        first = min(r.horizon for r in beaten)
        out.append(
            f"  From horizon {first} onward a rule that reads no events beats this "
            f"forecast. That is the gap the estimated layer has to close, and it is "
            f"the honest reason not to lead with the raw MAE yet."
        )
    else:
        out.append("  The forecast beats the do-nothing rule at every horizon.")

    signs = [r.mean_error for r in rows if r.mean_error != 0]
    if signs and all(e < 0 for e in signs):
        out.append(
            "  Every mean error is negative: the forecast is not noisy, it is "
            "biased low. Averaging never fixes a bias -- something is missing, and "
            "here it is the sales that have not happened yet."
        )
    elif signs and any(e > 0 for e in signs) and any(e < 0 for e in signs):
        flips = [r.horizon for r in rows if r.mean_error < 0]
        out.append(
            f"  The mean error changes sign at horizon {min(flips)}: slightly high "
            f"before it, low after. A bias that reverses is a much smaller problem "
            f"than one that only grows, and it is what the estimated layer was for."
        )
    return out


def _dev(bt: Backtest, rows, base) -> list[str]:
    """Everything computed and not published. For building, not for submitting."""
    out = ["dev view -- computed but not published", _bar(),
           f"  {'h':>3} {'median':>14} {'worst':>14} {'skew':>6}"]
    for r in rows:
        out.append(
            f"  {r.horizon:>3} {fmt_inr(r.median_abs):>14} {fmt_inr(r.worst):>14} "
            f"{r.skew:>6.2f}"
        )
    out += ["", "  rival rules, MAE by horizon:",
            f"  {'rule':>22} " + "".join(f"{'h=' + str(h):>13}" for h in (1, 3, 7, 14))]
    for name, per_h in base.balance_mae.items():
        cells = "".join(f"{fmt_inr(per_h.get(h, 0)):>13}" for h in (1, 3, 7, 14))
        out.append(f"  {name:>22} {cells}")
    ours = {r.horizon: r.mae for r in rows}
    out.append(
        f"  {'ours':>22} "
        + "".join(f"{fmt_inr(ours.get(h, 0)):>13}" for h in (1, 3, 7, 14))
    )
    out += [
        "",
        "  skew is MAE / median. ~1.0 means no day is much worse than typical -- "
        "the",
        "  sale-week outliers are drowned by the systematic bias, so the "
        "hypothesis that",
        "  justified these two columns is unsupported so far. Kept because "
        "computing them",
        "  is free and Bucket 2 may revive it.",
    ]
    return out


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Score the forecast.")
    parser.add_argument("--data", default="data")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--warm-up", type=int, default=DEFAULT_WARM_UP)
    parser.add_argument("--dev", action="store_true",
                        help="also print what is computed but not published")
    parser.add_argument(
        "--certain-only", action="store_true",
        help="score the certain layer alone -- the sales-stop scenario rather "
             "than a forecast. Useful for measuring what the estimated layer is "
             "worth; not a baseline, because it is not trying to be one.",
    )
    parser.add_argument("--no-intervals", action="store_true",
                        help="skip Bucket 3's bands and the calibration check")
    parser.add_argument("--skip-leak-check", action="store_true")
    args = parser.parse_args(argv)

    store = EventStore.load(args.data)
    store.check_two_dates()
    store.check_balance_ties()

    if not args.skip_leak_check:
        days = [store.date_for_day(n)
                for n in vantage_range(store, args.horizon, args.warm_up)]
        assert_no_leak(store, days, args.horizon)

    bt = run(store, args.horizon, args.warm_up, estimated=not args.certain_only,
             intervals=not args.no_intervals)
    print(render(bt, dev=args.dev))
    if not args.skip_leak_check:
        print(f"\nleak test: clean at all {len(bt.windows)} vantage points")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
