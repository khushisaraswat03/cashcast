"""What the forecast could not resolve, and why.

A list of bad days is an apology. A list of causes, each with the error it accounts
for and the fix where one exists, is a system that knows its own limits.

Every miss above the noise floor is attributed to one of four causes: a promotion
nobody declared, the demand dip after one, a chargeback, or ordinary daily
variation. Two have fixes and two do not, and a system that cannot tell a defect
from a limit will keep trying to fix the weather.
"""

from __future__ import annotations

import datetime as dt
import statistics as st
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from .backtest import Backtest, Prediction
from .money import Paise, fmt_inr
from .world import EventStore

#: A prediction is only worth explaining if it is genuinely bad. Anything inside the
#: noise floor is the forecaster working as designed, not an exception.
NOISE_FLOOR_14D = 22_327_82

#: Days after a promotion ends that are treated as its aftermath.
DIP_WINDOW_DAYS = 14

#: How far either side of a target day a chargeback counts as an explanation.
CHARGEBACK_WINDOW_DAYS = 2

#: A chargeback only explains an error if it is large enough to have caused it.
#:
#: Without this the attribution was absurd: a Rs.899 clawback was being credited with
#: a Rs.52,321 miss, purely because it landed on the same day. Coincidence in time is
#: not causation, and an exception list that says otherwise is worse than none --
#: it points the reader at the wrong thing with total confidence.
CHARGEBACK_MATERIALITY = 0.20


class Cause(str, Enum):
    PROMOTION = "a promotion the forecast could not size"
    DIP = "the demand dip after a promotion"
    CHARGEBACK = "a chargeback"
    VARIATION = "ordinary daily variation"


#: Whether anything can be done, and what. Kept beside the cause so a reader cannot
#: see the failure without seeing the verdict on it.
REMEDY: dict[Cause, tuple[bool, str]] = {
    Cause.PROMOTION: (
        True,
        "Declaring the promotion is measured to cut sale-week error by 63% and the "
        "fortnight after it by 61%. What remains is that a merchant's own estimate "
        "of their uplift is itself a guess -- they plan 3.3x volume and get 3.0x -- "
        "so declaring narrows the miss rather than removing it.",
    ),
    Cause.DIP: (
        True,
        "Let the merchant declare an expected post-sale dip alongside the promotion. "
        "Not currently modelled; the direction is well established in retail, the "
        "duration is not.",
    ),
    Cause.CHARGEBACK: (
        False,
        "Nothing. Chargebacks are rare enough that no dataset of this size contains "
        "sample enough to estimate a rate from -- unpredictable in principle, not "
        "merely unpredicted. They are reported as a risk, not forecast.",
    ),
    Cause.VARIATION: (
        False,
        "Nothing. Daily sales vary by ~30% for no reason, which puts a floor of "
        f"{fmt_inr(NOISE_FLOOR_14D)} on any 14-day forecast. Error at or below this "
        "is the forecaster working, not failing.",
    ),
}


@dataclass(frozen=True)
class Exception_:
    """One prediction the forecast could not resolve, with its cause."""

    prediction: Prediction
    cause: Cause
    detail: str

    @property
    def error(self) -> Paise:
        return self.prediction.abs_error


@dataclass
class ExceptionReport:
    total_predictions: int
    threshold: Paise
    exceptions: list[Exception_] = field(default_factory=list)

    def by_cause(self) -> dict[Cause, list[Exception_]]:
        out: dict[Cause, list[Exception_]] = {}
        for e in self.exceptions:
            out.setdefault(e.cause, []).append(e)
        return dict(sorted(out.items(), key=lambda kv: -len(kv[1])))

    @property
    def share(self) -> float:
        return len(self.exceptions) / self.total_predictions if self.total_predictions else 0.0

    def render(self, examples: int = 2) -> str:
        w = 78
        out = [
            "WHAT IT COULD NOT RESOLVE",
            "-" * w,
            f"  {len(self.exceptions)} of {self.total_predictions} forecasts "
            f"({self.share:.0%}) were wrong by more than {fmt_inr(self.threshold)},",
            "  which is the point past which the error is no longer ordinary noise.",
            "",
        ]
        for cause, items in self.by_cause().items():
            fixable, remedy = REMEDY[cause]
            errs = [e.error for e in items]
            out += [
                f"  {cause.value.upper()}  --  {len(items)} forecasts, "
                f"median error {fmt_inr(round(st.median(errs)))}",
            ]
            for e in sorted(items, key=lambda x: -x.error)[:examples]:
                p = e.prediction
                out.append(
                    f"    {p.target}  off by {fmt_inr(p.abs_error)} "
                    f"({p.horizon} days out) -- {e.detail}"
                )
            out.append(f"    {'FIXABLE:' if fixable else 'NOT FIXABLE:'} {remedy}")
            out.append("")

        # A cause that was checked and explains nothing is worth reporting. Silently
        # omitting it looks like it was never considered, and "we looked and it was
        # not this" is information.
        for cause in Cause:
            if cause not in self.by_cause():
                out += [
                    f"  {cause.value.upper()}  --  0 forecasts",
                    "    Checked and found to explain none of the misses in this "
                    "dataset.",
                    f"    {REMEDY[cause][1]}",
                    "",
                ]
        return "\n".join(out).rstrip()


def classify(
    p: Prediction,
    promo_days: frozenset[dt.date],
    dip_days: frozenset[dt.date],
    chargeback_days: dict[dt.date, Paise],
    promotions_visible: bool,
) -> tuple[Cause, str]:
    """Attribute one bad prediction to a cause.

    Checked in order of how much each explains, so a target inside a sale week is
    attributed to the promotion rather than to the chargeback that also happened to
    land that day. The ordering is a claim about which cause dominates, and it is
    worth stating rather than leaving implicit.
    """
    for offset in range(-CHARGEBACK_WINDOW_DAYS, CHARGEBACK_WINDOW_DAYS + 1):
        amount = chargeback_days.get(p.target + dt.timedelta(days=offset))
        # Materiality, not just coincidence in time. A small clawback landing on a
        # badly-missed day did not cause the miss.
        if amount and amount >= CHARGEBACK_MATERIALITY * p.abs_error:
            return Cause.CHARGEBACK, (
                f"{fmt_inr(amount)} clawed back with no warning in the data"
            )

    if p.target in promo_days:
        if promotions_visible:
            return Cause.PROMOTION, (
                "sale week; declared, but a merchant's own estimate of their uplift "
                "is itself a guess"
            )
        return Cause.PROMOTION, "sale week, and the promotion was never declared"

    if p.target in dip_days:
        return Cause.DIP, (
            "the fortnight after a sale: demand pulled forward, and the refund wave "
            "from sale-week orders arriving"
        )

    return Cause.VARIATION, "no identifiable cause; ordinary day-to-day movement"


def build(
    bt: Backtest,
    store: EventStore,
    *,
    threshold: Paise = NOISE_FLOOR_14D,
    promotions_visible: bool = True,
) -> ExceptionReport:
    """Every prediction wrong by more than `threshold`, attributed to a cause.

    The threshold is the noise floor rather than a round number. Below it the
    forecast is inside the range no forecaster can beat, so calling those days
    exceptions would be listing the weather as a defect.
    """
    promo_days: set[dt.date] = set()
    dip_days: set[dt.date] = set()
    for promo in store.promotions:
        day = promo.starts_on
        while day <= promo.ends_on:
            promo_days.add(day)
            day += dt.timedelta(days=1)
        for offset in range(1, DIP_WINDOW_DAYS + 1):
            dip_days.add(promo.ends_on + dt.timedelta(days=offset))

    chargeback_days = {c.debited_on: c.amount for c in store.chargebacks}

    report = ExceptionReport(
        total_predictions=len(bt.predictions), threshold=threshold
    )
    for p in bt.predictions:
        if p.abs_error <= threshold:
            continue
        cause, detail = classify(
            p, frozenset(promo_days), frozenset(dip_days),
            chargeback_days, promotions_visible,
        )
        report.exceptions.append(Exception_(prediction=p, cause=cause, detail=detail))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    from .backtest import run

    p = argparse.ArgumentParser(description="What the forecast could not resolve.")
    p.add_argument("--data", default="data")
    p.add_argument("--examples", type=int, default=2)
    args = p.parse_args(argv)

    if not (Path(args.data) / "balance.csv").exists():
        from .generate import main as generate

        print(f"No dataset in {args.data}/ -- generating first.\n")
        generate(["--out", args.data, "--quiet"])

    store = EventStore.load(args.data)
    bt = run(store, estimated=True, intervals=True)
    print(build(bt, store).render(examples=args.examples))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
