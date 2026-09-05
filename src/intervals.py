"""Bucket 3: a range around the forecast, and a check that the range is honest.

The band comes from the forecaster's own past errors -- take every 3-days-ahead
error made so far, sort them, read off the 10th and 90th percentile. Rolling, not
pooled: at vantage point V the band uses only errors from before V, so the earliest
forecasts get no band rather than one calibrated on their own future.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from .money import Paise

#: 80% is easier to calibrate credibly than 95%, which is breached too rarely for
#: 61 vantage points to tell a good band from a bad one.
DEFAULT_CONFIDENCE = 0.80

#: Fewer than this and the band is just the two most extreme errors seen so far.
MIN_SAMPLES = 10


def percentile(sorted_values: list[Paise], q: float) -> Paise:
    """Linear-interpolated percentile of an already-sorted list.

    Written out rather than using `statistics.quantiles`, whose tail conventions
    differ and matter at these sample sizes.
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

    Mutable and order-dependent on purpose: `observe` is called only after a
    vantage point has been scored, so `band` can never see its own error.
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
        """How far below and above the forecast the band reaches, or None.

        The offsets are the error percentiles with the sign flipped: a forecast
        running high has to reach further down to cover the truth.
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
        """Adapter for `forecast(bands=...)`. AT_RISK depends on the band, so it
        has to exist before flags are computed."""
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
        """Both directions are misses: a band wider than promised claims less
        than it could."""
        if self.mean_width == 0:
            # Horizon 1 is exact everywhere, so every past error is zero and the
            # band collapses to a point -- correct, not over-wide.
            return "exact" if self.hit_rate == 1.0 else "point band, missed"
        gap = self.hit_rate - self.confidence
        if abs(gap) <= 0.05:
            return "honest"
        return "too narrow (overconfident)" if gap < 0 else "too wide (says little)"
