"""Tests for the generator.

The generator produces the ground truth. If its own arithmetic is wrong, every
accuracy figure measured against it later is meaningless -- so these tests are less
a unit suite than a proof that the answer key is worth believing.

The invariants, roughly in order of how badly a failure would hurt:

1. The balance chains: each day's closing is the previous closing plus that day's
   movement, and movement is the sum of `cash_delta` over events landing that day.
2. Money is conserved: gross captured == fees + GST + what reaches the bank.
3. Settlement dates follow the working-day rules, and no money arrives before it
   was collected.
4. Failed payments move nothing, ever.
5. The sales curve actually contains the patterns the estimators will look for --
   a weekend peak, growth, a sale week, a dip after it.
6. The same seed reproduces the same dataset.
"""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from src import calendar_rules as cal
from src.events import EVENT_FILES, Method, Order, Payment, PaymentStatus, Promotion
from src.generate import Config, Generator
from src.money import split

#: Small enough to stay fast, long enough to contain the sale week and its dip.
SMALL = dict(days=90, orders_per_day=8.0, promo_starts_on_day=40, promo_ends_on_day=46)


def build(**overrides) -> Generator:
    gen = Generator(Config(**{**SMALL, **overrides}))
    gen.run()
    return gen


@pytest.fixture(scope="module")
def gen() -> Generator:
    return build(seed=7)


@pytest.fixture(scope="module")
def written(tmp_path_factory) -> tuple[Generator, Path]:
    """A dataset round-tripped through the real CSV files."""
    out = tmp_path_factory.mktemp("data")
    g = build(seed=11, out_dir=out)
    g.write()
    return g, out


# --------------------------------------------------------------------------
# 1. The balance
# --------------------------------------------------------------------------


class TestBalance:
    def test_balance_chains_day_by_day(self, gen: Generator) -> None:
        """Each closing balance is the previous one plus that day's net movement.

        Computed here independently of the generator, from the events, so a bug in
        `_compute_balance` cannot hide behind itself.
        """
        movement: dict[date, int] = {}
        for event in gen.all_events():
            if event.cash_at is None or event.cash_delta == 0:
                continue
            movement[event.cash_at] = movement.get(event.cash_at, 0) + event.cash_delta

        expected = gen.cfg.opening_balance
        for index in range(1, gen.cfg.days + 1):
            day = gen.cfg.day_of(index)
            expected += movement.get(day, 0)
            assert gen.balance[day] == expected, day

    def test_every_day_in_the_window_has_a_balance(self, gen: Generator) -> None:
        assert len(gen.balance) == gen.cfg.days
        assert min(gen.balance) == gen.cfg.start
        assert max(gen.balance) == gen.cfg.end

    def test_balance_only_moves_on_days_something_lands(self, gen: Generator) -> None:
        landing = {e.cash_at for e in gen.all_events() if e.moves_cash}
        previous = gen.cfg.opening_balance
        for index in range(1, gen.cfg.days + 1):
            day = gen.cfg.day_of(index)
            if day not in landing:
                assert gen.balance[day] == previous, day
            previous = gen.balance[day]

    def test_weekends_receive_nothing(self, gen: Generator) -> None:
        """Banks do not settle at the weekend. If money lands on a Saturday, the
        settlement calendar is being bypassed somewhere."""
        for event in gen.all_events():
            if not event.moves_cash:
                continue
            assert cal.is_working_day(event.cash_at, gen.cfg.holidays), (
                f"{event.event_id} lands on a non-working day {event.cash_at}"
            )

    def test_money_still_in_transit_is_reported(self, gen: Generator) -> None:
        """Sales captured near the end settle after the window closes, and outflows
        are committed past it too. Both are reported rather than quietly dropped --
        a forecast standing on the last vantage day depends on them.

        The *net* of the tail can be either sign, so it is deliberately not asserted
        as positive; what matters is that money is known to be moving out there.
        """
        assert gen.beyond_window
        assert all(d > gen.cfg.end for d in gen.beyond_window)
        settling_late = [
            p for p in gen.payments if p.settles_on and p.settles_on > gen.cfg.end
        ]
        assert settling_late
        assert sum(p.net for p in settling_late) > 0


# --------------------------------------------------------------------------
# 2. Money is conserved
# --------------------------------------------------------------------------


class TestMoneyConservation:
    def test_gross_equals_fees_plus_gst_plus_net(self, gen: Generator) -> None:
        captured = [p for p in gen.payments if p.status is PaymentStatus.CAPTURED]
        gross = sum(p.amount for p in captured)
        assert gross == sum(p.fee for p in captured) + sum(p.gst for p in captured) + sum(
            p.net for p in captured
        )

    def test_each_payment_uses_the_shared_fee_arithmetic(self, gen: Generator) -> None:
        """The generator must not have its own copy of the fee rules."""
        for p in gen.payments:
            if p.status is PaymentStatus.CAPTURED:
                assert (p.fee, p.gst, p.net) == split(p.amount), p.payment_id

    def test_what_reaches_the_bank_is_less_than_what_was_sold(self, gen: Generator) -> None:
        captured = [p for p in gen.payments if p.status is PaymentStatus.CAPTURED]
        assert 0 < sum(p.net for p in captured) < sum(p.amount for p in captured)

    def test_closing_balance_matches_the_events(self, gen: Generator) -> None:
        total = sum(e.cash_delta for e in gen.all_events() if e.cash_at and e.cash_at <= gen.cfg.end)
        assert gen.balance[gen.cfg.end] == gen.cfg.opening_balance + total


# --------------------------------------------------------------------------
# 3. Settlement timing
# --------------------------------------------------------------------------


class TestSettlementTiming:
    def test_money_never_arrives_before_it_was_collected(self, gen: Generator) -> None:
        for p in gen.payments:
            if p.settles_on is not None:
                assert p.settles_on > p.captured_at.date(), p.payment_id

    def test_settlement_dates_follow_the_calendar_rules(self, gen: Generator) -> None:
        """Recomputed from the rules rather than trusted from the field."""
        for p in gen.payments:
            if p.status is not PaymentStatus.CAPTURED:
                continue
            assert p.settles_on == cal.settlement_date(
                p.captured_at,
                p.method.value,
                cutoff=gen.cfg.cutoff,
                holidays=gen.cfg.holidays,
            ), p.payment_id

    def test_upi_arrives_sooner_than_cards(self, gen: Generator) -> None:
        """Two cycles is what makes the near end of the forecast a gradient rather
        than a cliff, so the data has to actually contain both."""
        lags = {Method.UPI: [], Method.CARD: []}
        for p in gen.payments:
            if p.status is PaymentStatus.CAPTURED:
                lags[p.method].append((p.settles_on - p.captured_at.date()).days)
        assert lags[Method.UPI] and lags[Method.CARD]
        assert statistics.mean(lags[Method.UPI]) < statistics.mean(lags[Method.CARD])

    def test_the_holiday_in_the_window_delays_something(self, gen: Generator) -> None:
        """A bank holiday that never affects a settlement date is a holiday that is
        not being applied. 1 May 2026 is a Friday, so it should push money to the
        following Monday or later."""
        holidays = [h for h in gen.cfg.holidays if gen.cfg.start <= h <= gen.cfg.end]
        assert holidays, "the window must contain a holiday for this to mean anything"
        weekday_holidays = [h for h in holidays if h.weekday() < 5]
        assert weekday_holidays
        for h in weekday_holidays:
            assert not any(p.settles_on == h for p in gen.payments)

    def test_the_cutoff_splits_some_days(self, gen: Generator) -> None:
        """Late-evening sales roll into the next day's batch. If nothing ever does,
        the cutoff is decorative."""
        rolled = [
            p
            for p in gen.payments
            if p.status is PaymentStatus.CAPTURED
            and cal.batch_day(p.captured_at, gen.cfg.cutoff) != p.captured_at.date()
        ]
        assert rolled, "no sale was ever captured after the cutoff"
        assert len(rolled) / len(gen.payments) > 0.05


# --------------------------------------------------------------------------
# 4. Failed payments
# --------------------------------------------------------------------------


class TestFailures:
    def test_failures_exist_and_move_no_money(self, gen: Generator) -> None:
        failed = [p for p in gen.payments if p.status is PaymentStatus.FAILED]
        assert failed
        for p in failed:
            assert p.settles_on is None
            assert p.cash_delta == 0
            assert not p.moves_cash

    def test_most_failures_are_followed_by_a_successful_retry(self, gen: Generator) -> None:
        by_order: dict[str, list[Payment]] = {}
        for p in gen.payments:
            by_order.setdefault(p.order_id, []).append(p)

        failed_orders = [
            attempts
            for attempts in by_order.values()
            if any(p.status is PaymentStatus.FAILED for p in attempts)
        ]
        assert failed_orders
        recovered = [
            a for a in failed_orders if any(p.status is PaymentStatus.CAPTURED for p in a)
        ]
        assert 0.6 < len(recovered) / len(failed_orders) < 0.95

    def test_some_orders_are_never_paid_for(self, gen: Generator) -> None:
        """This is why orders and payments are separate. An abandoned order is real
        revenue that did not happen, and it explains a gap the sales curve alone
        cannot."""
        paid = {p.order_id for p in gen.payments if p.status is PaymentStatus.CAPTURED}
        abandoned = [o for o in gen.orders if o.order_id not in paid]
        assert abandoned

    def test_a_retry_keeps_the_original_amount_and_order(self, gen: Generator) -> None:
        orders = {o.order_id: o for o in gen.orders}
        for p in gen.payments:
            assert p.order_id in orders, p.payment_id
            assert p.amount == orders[p.order_id].amount
            assert p.method == orders[p.order_id].method

    def test_payment_ids_and_order_ids_are_unique(self, gen: Generator) -> None:
        assert len({p.payment_id for p in gen.payments}) == len(gen.payments)
        assert len({o.order_id for o in gen.orders}) == len(gen.orders)


# --------------------------------------------------------------------------
# 5. The sales curve contains what the estimators will look for
# --------------------------------------------------------------------------


class TestSalesCurve:
    def _revenue_by_weekday(self, gen: Generator) -> dict[int, float]:
        totals: dict[int, list[int]] = {d: [] for d in range(7)}
        for day, amount in gen.daily_revenue().items():
            index = gen.cfg.index_of(day)
            if gen._promo_factor(index) == 1.0:  # exclude the sale and the dip
                totals[day.weekday()].append(amount)
        return {d: statistics.mean(v) for d, v in totals.items() if v}

    def test_weekends_outsell_midweek(self, gen: Generator) -> None:
        """The pattern the weekday baseline exists to find. If it is not here, the
        estimator has nothing to learn and its later accuracy means nothing."""
        by_weekday = self._revenue_by_weekday(gen)
        weekend = statistics.mean([by_weekday[5], by_weekday[6]])
        midweek = statistics.mean([by_weekday[1], by_weekday[2]])
        assert weekend > midweek * 1.4

    def test_the_business_grows(self, gen: Generator) -> None:
        """Growth is what biases a four-week average downward. It has to be visible
        in the data before the bias can be measured, let alone corrected."""
        revenue = gen.daily_revenue()
        days = sorted(revenue)
        first_third = [revenue[d] for d in days[: len(days) // 3]]
        last_third = [revenue[d] for d in days[-len(days) // 3 :]]
        assert statistics.mean(last_third) > statistics.mean(first_third)

    def test_the_sale_week_is_the_busiest_stretch(self, gen: Generator) -> None:
        cfg = gen.cfg
        revenue = gen.daily_revenue()
        sale = [
            revenue.get(cfg.day_of(i), 0)
            for i in range(cfg.promo_starts_on_day, cfg.promo_ends_on_day + 1)
        ]
        normal = [
            revenue.get(cfg.day_of(i), 0)
            for i in range(1, cfg.days + 1)
            if gen._promo_factor(i) == 1.0
        ]
        assert statistics.mean(sale) > statistics.mean(normal) * 1.5

    def test_revenue_rises_less_than_volume_during_the_sale(self, gen: Generator) -> None:
        """Plans are not outcomes.

        The merchant's promotion declares a 3x uplift, and volume does roughly
        triple -- but sale orders are discounted, so revenue rises by noticeably
        less. A forecaster that takes the declared uplift at face value should
        therefore be wrong, which is the point of storing the plan rather than the
        outcome on the `Promotion`.
        """
        cfg = gen.cfg
        sale_days = range(cfg.promo_starts_on_day, cfg.promo_ends_on_day + 1)
        revenue = gen.daily_revenue()
        orders_per_day: Counter[date] = Counter(o.placed_at.date() for o in gen.orders)

        def mean_over(indices, source) -> float:
            values = [source.get(cfg.day_of(i), 0) for i in indices]
            return statistics.mean(values) if values else 0.0

        normal_days = [i for i in range(1, cfg.days + 1) if gen._promo_factor(i) == 1.0]
        volume_uplift = mean_over(sale_days, orders_per_day) / mean_over(
            normal_days, orders_per_day
        )
        revenue_uplift = mean_over(sale_days, revenue) / mean_over(normal_days, revenue)

        promo = gen.promotions[0]
        assert volume_uplift > 2.0
        assert revenue_uplift < volume_uplift * 0.85

        # The volume figure alone over-states revenue, which is why the promotion
        # declares the discount alongside it. Reading the volume number as revenue
        # inflated a declared sale week by 43%, and balances carried that error
        # forward into every later prediction.
        assert revenue_uplift < promo.expected_volume_uplift
        assert revenue_uplift == pytest.approx(promo.expected_revenue_uplift, rel=0.15)

    def test_the_week_after_the_sale_dips(self, gen: Generator) -> None:
        """Demand pulled forward. This is the second, subtler failure -- the
        four-week average is freshly inflated by the sale just as the real level
        drops, so the forecaster is wrong in the opposite direction."""
        cfg = gen.cfg
        revenue = gen.daily_revenue()
        dip_start = cfg.promo_ends_on_day + 1
        dip = [
            revenue.get(cfg.day_of(i), 0)
            for i in range(dip_start, dip_start + cfg.dip_days)
        ]
        normal = [
            revenue.get(cfg.day_of(i), 0)
            for i in range(1, cfg.days + 1)
            if gen._promo_factor(i) == 1.0
        ]
        assert statistics.mean(dip) < statistics.mean(normal)

    def test_orders_per_day_means_orders_per_day(self, gen: Generator) -> None:
        """Weekday multipliers are normalised, so the config figure is honest for a
        flat window. Growth and the sale deliberately push the realised average
        above it -- so the check is against the *first* two weeks, before growth has
        accumulated."""
        cfg = gen.cfg
        early = [
            o
            for o in gen.orders
            if cfg.index_of(o.placed_at.date()) <= 14
        ]
        per_day = len(early) / 14
        assert cfg.orders_per_day * 0.75 < per_day < cfg.orders_per_day * 1.25


# --------------------------------------------------------------------------
# The promotion
# --------------------------------------------------------------------------


class TestPromotion:
    def test_declared_before_it_starts(self, gen: Generator) -> None:
        promo = gen.promotions[0]
        assert promo.known_at < promo.starts_on
        assert (promo.starts_on - promo.known_at).days == gen.cfg.promo_declared_days_ahead

    def test_declaration_date_is_what_the_wall_will_filter_on(self, gen: Generator) -> None:
        """The hidden-versus-declared experiment needs the declaration to fall
        strictly between two plausible vantage days, or there is nothing to compare."""
        promo = gen.promotions[0]
        assert gen.cfg.start < promo.known_at < gen.cfg.end
        assert promo.covers(promo.starts_on) and promo.covers(promo.ends_on)


# --------------------------------------------------------------------------
# 6. Reproducibility, measurement, and the files
# --------------------------------------------------------------------------


class TestDeterminism:
    def test_same_seed_same_dataset(self) -> None:
        a, b = build(seed=42), build(seed=42)
        assert [p.model_dump() for p in a.payments] == [p.model_dump() for p in b.payments]
        assert a.balance == b.balance

    def test_different_seed_different_dataset(self) -> None:
        a, b = build(seed=1), build(seed=2)
        assert a.balance != b.balance


class TestMeasurement:
    def test_realised_noise_is_measured_not_assumed(self, gen: Generator) -> None:
        """The config asks for noise on the order *count*; order values then vary
        independently. What matters downstream is the noise on daily revenue, which
        is larger -- and reporting the requested figure instead would understate the
        floor and flatter the forecaster."""
        noise = gen.realised_noise()
        assert noise["days_measured"] > 30
        realised = noise["realised_noise_on_daily_revenue"]
        assert realised > noise["requested_noise_on_order_count"]
        assert 0.15 < realised < 0.60

    def test_noise_floor_grows_with_the_horizon(self, gen: Generator) -> None:
        """Errors partly cancel, so the floor grows with the square root of the
        horizon rather than linearly."""
        one = gen.noise_floor(1)["cumulative_floor_paise"]
        four = gen.noise_floor(4)["cumulative_floor_paise"]
        sixteen = gen.noise_floor(16)["cumulative_floor_paise"]
        assert one > 0
        assert four == pytest.approx(one * 2, rel=0.01)
        assert sixteen == pytest.approx(one * 4, rel=0.01)

    def test_noise_floor_is_a_meaningful_fraction_of_daily_revenue(self, gen: Generator) -> None:
        """Sanity band. Far below this and forecasting is trivial; far above and no
        method is distinguishable from any other."""
        floor = gen.noise_floor(14)
        ratio = floor["cumulative_floor_paise"] / floor["mean_daily_revenue_paise"]
        assert 0.4 < ratio < 3.0


class TestFiles:
    def test_all_files_written(self, written) -> None:
        _, out = written
        expected = set(EVENT_FILES.values()) | {"balance.csv", "meta.json"}
        assert expected <= {p.name for p in out.iterdir()}

    def test_events_survive_the_round_trip(self, written) -> None:
        gen, out = written
        with (out / EVENT_FILES[Payment]).open(newline="", encoding="utf-8") as fh:
            restored = [Payment.from_csv_row(row) for row in csv.DictReader(fh)]
        assert restored == gen.payments

    def test_every_file_has_a_header_and_rows(self, written) -> None:
        """Every event type is populated. An empty file would mean a whole class of
        behaviour silently absent from the dataset."""
        _, out = written
        for name in EVENT_FILES.values():
            lines = (out / name).read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) >= 2, f"{name} has a header and nothing else"

    def test_balance_csv_rows_tie(self, written) -> None:
        """opening + inflow - outflow == closing on every row, and each row's
        opening is the previous row's closing."""
        from src.money import rupees_to_paise

        gen, out = written
        with (out / "balance.csv").open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

        assert len(rows) == gen.cfg.days
        previous: int | None = None
        for row in rows:
            opening = rupees_to_paise(row["opening"])
            inflow = rupees_to_paise(row["inflow"])
            outflow = rupees_to_paise(row["outflow"])
            closing = rupees_to_paise(row["closing"])
            assert opening + inflow - outflow == closing, row["date"]
            if previous is not None:
                assert opening == previous, row["date"]
            previous = closing

        assert rupees_to_paise(rows[0]["opening"]) == gen.cfg.opening_balance
        assert rupees_to_paise(rows[-1]["closing"]) == gen.balance[gen.cfg.end]

    def test_meta_json_is_self_consistent(self, written) -> None:
        gen, out = written
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        assert meta["counts"]["orders"] == len(gen.orders)
        assert meta["counts"]["payments"] == len(gen.payments)
        assert meta["config"]["seed"] == gen.cfg.seed
        assert meta["money_paise"]["closing_balance"] == gen.balance[gen.cfg.end]
        assert meta["measured"]["noise"]["days_measured"] > 0
