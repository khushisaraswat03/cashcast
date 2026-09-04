"""Bucket 3: how wrong is this likely to be?

Buckets 1 and 2 produce a number. This produces a range around it, then checks the
range is honest.

**No probability theory.** The band comes from the forecaster's own past mistakes:
take every 3-days-ahead error made so far, sort them, read off the 10th and 90th
percentile. Bands widen with horizon on their own, because the errors did.

**Rolling, not pooled.** At vantage point V the band uses only errors from before V.
Computing bands from all 854 errors and applying them to the first forecast would use
the future to calibrate the past, inflating the one number this file exists to
produce honestly. The earliest forecasts therefore get **no band at all** rather than
a guessed one.

**Calibration is the point.** "80% confident" is a checkable claim: the truth should
land inside the band about 80 times in 100. Land it 55 times and the bands are too
narrow; land it 99 and they are too wide to say anything.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from .money import Paise

#: Width of the reported band. 80% is easier to calibrate credibly than 95% and
#: fails visibly when it is wrong -- a 95% band is breached so rarely that 61
#: vantage points cannot tell a good one from a bad one.
DEFAULT_CONFIDENCE = 0.80

#: Past vantage points needed before any band is offered. Ten gives the 10th and
#: 90th percentile something to sit between; fewer and the band is just the two most
#: extreme errors seen so far, which moves wildly and reads as precision that is not
#: there.
MIN_SAMPLES = 10


def percentile(sorted_values: list[Paise], q: float) -> Paise:
    """Linear-interpolated percentile of an already-sorted list.

    Written out rather than taken from `statistics.quantiles`, which needs at least
    n>1 and reports slightly different conventions at the tails. At these sample
    sizes the convention matters, and a calibration number is worth being able to
    check by hand.
    """
    if not sorted_values:
        raise ValueError("no values")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    frac = pos - low
    return round(sorted_values[low] * (1 - frac) + sorted_values[high] * frac)


@dataclass
class RollingIntervals:
    """Bands from errors seen so far, kept per horizon.

    Deliberately mutable and order-dependent: `observe` is called only after a
    vantage point has been scored, so `band` can never see its own error. The
    ordering *is* the honesty guarantee, which is why it is a stateful object rather
    than a function over the whole error set.
    """

    confidence: float = DEFAULT_CONFIDENCE
    min_samples: int = MIN_SAMPLES
    #: horizon -> sorted errors (forecast minus actual) seen at that horizon
    _errors: dict[int, list[Paise]] = field(default_factory=dict)

    @property
    def _tails(self) -> tuple[float, float]:
        tail = (1.0 - self.confidence) / 2.0
        return tail, 1.0 - tail

    def observe(self, horizon: int, error: Paise) -> None:
        """Record one scored forecast. Kept sorted so percentiles are a lookup."""
        bisect.insort(self._errors.setdefault(horizon, []), error)

    def samples(self, horizon: int) -> int:
        return len(self._errors.get(horizon, ()))

    def offsets(self, horizon: int) -> tuple[Paise, Paise] | None:
        """How far below and above the forecast the band should reach.

        Returns `None` until enough history exists. The offsets are the error
        percentiles with the sign flipped: if the forecast has been running Rs.5,000
        *high*, the band has to reach further *down* to cover the truth.
        """
        errs = self._errors.get(horizon, [])
        if len(errs) < self.min_samples:
            return None
        lo_q, hi_q = self._tails
        return -percentile(errs, hi_q), -percentile(errs, lo_q)

    def band(self, horizon: int, centre: Paise) -> tuple[Paise, Paise] | None:
        off = self.offsets(horizon)
        return None if off is None else (centre + off[0], centre + off[1])

    def band_fn(self):
        """Adapter for `forecast(bands=...)`, so bands are present before flags are
        computed rather than bolted on afterwards -- `AT_RISK` depends on them."""
        return lambda horizon, centre: self.band(horizon, centre)


@dataclass(frozen=True)
class Calibration:
    """Did the band mean what it said?"""

    horizon: int
    n: int
    covered: int
    confidence: float
    mean_width: Paise

    @property
    def hit_rate(self) -> float:
        return self.covered / self.n if self.n else 0.0

    @property
    def verdict(self) -> str:
        """Plain words, because a calibration number invites the wrong reading.

        Being *inside* the band more often than promised is not a better result --
        it means the band is wider than it needs to be and is claiming less than it
        could. Both directions are misses.
        """
        if self.mean_width == 0:
            # Horizon 1 is exact at every vantage point, so every past error is
            # zero and the band collapses to a point -- which is then right every
            # time. That is not an over-wide band, it is the correct answer to a
            # question with no uncertainty in it.
            return "exact" if self.hit_rate == 1.0 else "point band, missed"
        gap = self.hit_rate - self.confidence
        if abs(gap) <= 0.05:
            return "honest"
        return "too narrow (overconfident)" if gap < 0 else "too wide (says little)"
