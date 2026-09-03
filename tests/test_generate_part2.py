"""Tests for the second half of the generator: refunds, chargebacks, outflows.

The first half proved the arithmetic. This half proves the *scenario* -- that the
data contains the structures the forecaster is supposed to exploit, and the squeeze
it is supposed to see coming.

Two of these tests are the ones I would point at if asked what the dataset is for:

* `test_the_squeeze_is_sustained_not_a_blip` -- a single tight day needs no
  forecast; five tight weeks do.
* `test_sale_revenue_arrives_after_the_sale_is_over` -- profit is not cash, made
  measurable. The merchant sells triple for a week and holds a fifth of their
  starting balance while doing it.
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import date, timedelta

import pytest

from src import calendar_rules as cal
from src.events import OutflowKind, PaymentStatus
from src.generate import Config, Generator

SMALL = dict(days=90, orders_per_day=8.0, promo_starts_on_day=40, promo_ends_on_day=46)


def build(**overrides) -> Generator:
    gen = Generator(Config(**{**SMALL, **overrides}))
    gen.run()
    return gen


@pytest.fixture(scope="module")
def gen() -> Generator:
    return build(seed=7)


@pytest.fixture(scope="module")
def full() -> Generator:
    """The real 120-day dataset. The squeeze needs the full window to develop."""
    g = Generator(Config(seed=7))
    g.run()
    return g


# --------------------------------------------------------------------------
# Refunds
# --------------------------------------------------------------------------


class TestRefunds:
    def test_enough_refunds_to_estimate_from(self, gen: Generator) -> None:
        """The single most important property of this dataset.

        The estimated layer learns a refund rate and a lag distribution from
        history. Thirty events is roughly the floor for that to mean anything; a
        low-refund business would leave the whole layer with nothing to learn and
        make its later accuracy meaningless.
        """
        assert len(gen.refunds) > 30

    def test_refund_rate_is_close_to_the_configured_rate(self, gen: Generator) -> None:
        captured = [p for p in gen.payments if p.status is PaymentStatus.CAPTURED]
        rate = len(gen.refunds) / len(captured)
        assert gen.cfg.refund_rate * 0.8 < rate < gen.cfg.refund_rate * 1.2

    def test_a_refund_never_exceeds_what_was_paid(self, gen: Generator) -> None:
        paid = {p.payment_id: p.amount for p in gen.payments}
        for r in gen.refunds:
            assert r.payment_id in paid, r.refund_id
            assert 0 < r.amount <= paid[r.payment_id], r.refund_id

    def test_partial_and_full_refunds_both_occur(self, gen: Generator) -> None:
        """Fashion returns are often part of an order -- two of three items back."""
        paid = {p.payment_id: p.amount for p in gen.payments}
        partial = [r for r in gen.refunds if r.amount < paid[r.payment_id]]
        full = [r for r in gen.refunds if r.amount == paid[r.payment_id]]
        assert partial and full
        assert 0.25 < len(partial) / len(gen.refunds) < 0.65

    def test_partial_refunds_are_whole_rupees(self, gen: Generator) -> None:
        """A refund is issued against line items, not as an arbitrary fraction."""
        paid = {p.payment_id: p.amount for p in gen.payments}
        for r in gen.refunds:
            if r.amount < paid[r.payment_id]:
                assert r.amount % 100 == 0, r.refund_id

    def test_refunds_reference_the_order_as_well_as_the_payment(self, gen: Generator) -> None:
        """The business fact is about the order; the money comes out of a payment.
        Both links have to hold, which is why orders and payments are separate."""
        payments = {p.payment_id: p for p in gen.payments}
        orders = {o.order_id for o in gen.orders}
        for r in gen.refunds:
            assert r.order_id in orders
            assert payments[r.payment_id].order_id == r.order_id

    def test_the_request_lag_follows_the_configured_distribution(self, gen: Generator) -> None:
        """The lag is a distribution, not a constant -- that shape is what lets
        expected refunds be spread across future days instead of dumped on one. If
        the sampling is wrong the estimator learns the wrong shape."""
        captured = {p.payment_id: p.captured_at for p in gen.payments}
        lags = Counter(
            (r.requested_at.date() - captured[r.payment_id].date()).days
            for r in gen.refunds
        )
        configured = {d for d, _ in gen.cfg.refund_lag_weights}
        # Hours jitter can nudge a lag one day either side of the drawn value.
        assert set(lags) <= {d + off for d in configured for off in (-1, 0, 1)}

        heaviest = max(gen.cfg.refund_lag_weights, key=lambda dw: dw[1])[0]
        observed_mode = lags.most_common(1)[0][0]
        assert abs(observed_mode - heaviest) <= 2

    def test_money_leaves_after_the_refund_is_requested(self, gen: Generator) -> None:
        """The gap is short but non-zero, and it is what puts an already-requested
        refund in the certain layer rather than the estimated one."""
        for r in gen.refunds:
            assert r.nets_off_on > r.requested_at.date(), r.refund_id
            assert cal.is_working_day(r.nets_off_on, gen.cfg.holidays)

    def test_refunds_cost_a_plausible_share_of_revenue(self, gen: Generator) -> None:
        captured = [p for p in gen.payments if p.status is PaymentStatus.CAPTURED]
        share = sum(r.amount for r in gen.refunds) / sum(p.amount for p in captured)
        assert 0.06 < share < 0.25


# --------------------------------------------------------------------------
# Chargebacks
# --------------------------------------------------------------------------


class TestChargebacks:
    def test_they_exist_but_are_too_few_to_estimate_from(self, gen: Generator) -> None:
        """Not a gap in the data -- the reason chargebacks belong in the honest
        layer. Any dataset of a realistic size for this merchant contains a handful,
        which is nowhere near enough to fit a rate to."""
        assert 0 < len(gen.chargebacks) < 10
        captured = [p for p in gen.payments if p.status is PaymentStatus.CAPTURED]
        assert len(gen.chargebacks) / len(captured) < 0.01

    def test_most_dispute_sales_from_before_the_window(self, gen: Generator) -> None:
        """The realistic case, and the one that looks unexplainable: money leaves
        this month against a sale there is no local record of."""
        outside = [c for c in gen.chargebacks if c.original_captured_on < gen.cfg.start]
        assert outside
        known = {p.payment_id for p in gen.payments}
        for c in outside:
            assert c.payment_id not in known, c.chargeback_id

    def test_locally_traceable_ones_point_at_real_payments(self, gen: Generator) -> None:
        known = {p.payment_id: p for p in gen.payments}
        for c in gen.chargebacks:
            if c.payment_id in known:
                assert c.amount == known[c.payment_id].amount
                assert c.original_captured_on == known[c.payment_id].captured_at.date()

    def test_debited_after_raised_and_on_a_working_day(self, gen: Generator) -> None:
        lo, hi = gen.cfg.chargeback_debit_lag
        for c in gen.chargebacks:
            assert c.debited_on > c.raised_at.date(), c.chargeback_id
            assert cal.is_working_day(c.debited_on, gen.cfg.holidays)
            gap = cal.working_days_between(c.raised_at.date(), c.debited_on, gen.cfg.holidays)
            assert lo <= gap <= hi, c.chargeback_id

    def test_raised_inside_the_window(self, gen: Generator) -> None:
        for c in gen.chargebacks:
            assert gen.cfg.start <= c.raised_at.date() <= gen.cfg.end


# --------------------------------------------------------------------------
# Outflows
# --------------------------------------------------------------------------


class TestOutflows:
    def test_every_kind_is_present(self, gen: Generator) -> None:
        kinds = {o.kind for o in gen.outflows}
        assert kinds == {
            OutflowKind.RENT,
            OutflowKind.SALARY,
            OutflowKind.ADS,
            OutflowKind.SUPPLIER,
            OutflowKind.TAX,
        }

    def test_monthly_outflows_recur(self, gen: Generator) -> None:
        for kind in (OutflowKind.RENT, OutflowKind.SALARY, OutflowKind.TAX):
            months = {(o.due_on.year, o.due_on.month) for o in gen.outflows if o.kind is kind}
            assert len(months) >= 3, kind

    def test_everything_is_due_on_a_working_day(self, gen: Generator) -> None:
        """Rent due on the 1st is paid on the 3rd when the 1st is a Saturday. A
        forecast that puts it on the 1st is wrong by two days and the wrong side of
        a weekend."""
        for o in gen.outflows:
            assert cal.is_working_day(o.due_on, gen.cfg.holidays), o.outflow_id

    def test_committed_well_before_due(self, gen: Generator) -> None:
        """The lead time is what makes outflows certain rather than estimated. Zero
        lead time would mean the forecaster only learns about rent on the day it
        leaves, which is not how a business works."""
        for o in gen.outflows:
            assert o.known_at < o.due_on, o.outflow_id
            assert (o.due_on - o.known_at).days == gen.cfg.commitment_lead_days

    def test_the_sale_stock_payment_lands_before_the_sale(self, gen: Generator) -> None:
        """The most predictable outflow in the dataset, and the one that does the
        damage: stock is paid for weeks before any of it sells."""
        stock = [o for o in gen.outflows if o.description == "sale stock purchase"]
        assert len(stock) == 1
        assert stock[0].due_on < gen.cfg.promo_starts_on
        assert stock[0].amount == gen.cfg.sale_inventory

    def test_ad_spend_rises_for_the_sale(self, gen: Generator) -> None:
        ads = [o for o in gen.outflows if o.kind is OutflowKind.ADS]
        assert max(o.amount for o in ads) > gen.cfg.ads_monthly

    def test_outflows_extend_past_the_window(self, gen: Generator) -> None:
        """A forecast standing on the last vantage day looks fourteen days ahead.
        Those days have to contain the outflows that are already committed."""
        assert any(o.due_on > gen.cfg.end for o in gen.outflows)

    def test_committed_outflows_do_not_exceed_income(self, full: Generator) -> None:
        """Sized against inflow rather than picked for flavour. A merchant whose
        fixed costs exceed their revenue is insolvent, which is a different problem
        and not one a forecaster solves.

        Checked against the real config rather than the small fixture: the fixture
        cuts revenue (fewer days, fewer orders) without cutting rent or salaries, so
        its ratio is meaningless.
        """
        captured = [p for p in full.payments if p.status is PaymentStatus.CAPTURED]
        net_in = sum(p.net for p in captured) - sum(r.amount for r in full.refunds)
        out_in_window = sum(o.amount for o in full.outflows if o.due_on <= full.cfg.end)
        assert 0.7 < out_in_window / net_in < 1.05


# --------------------------------------------------------------------------
# The squeeze -- emergent, not placed
# --------------------------------------------------------------------------


class TestTheSqueeze:
    def test_the_business_gets_genuinely_tight(self, full: Generator) -> None:
        """If the merchant is comfortable every day, the forecast answers a question
        nobody asked."""
        worst = min(full.balance.values())
        opening = full.cfg.opening_balance
        assert worst < opening * 0.30
        assert worst > 0  # tight, not insolvent

    def test_the_squeeze_is_sustained_not_a_blip(self, full: Generator) -> None:
        """A single tight day is visible two days out and needs no forecasting. A
        sustained stretch is what a 14-day horizon is for -- the merchant needs to
        know how long it lasts and when they come out of it.

        Two thresholds because "sustained" has two parts: a deep stretch of about a
        fortnight below half the opening balance, sitting inside a much longer
        stretch of never quite recovering. The figures describe the scenario the
        generator is tuned to produce, and exist to catch it regressing.
        """
        opening = full.cfg.opening_balance
        assert full.longest_run_below(opening // 2) >= 10
        assert full.longest_run_below(opening * 3 // 4) >= 20

    def test_the_worst_day_is_near_the_sale(self, full: Generator) -> None:
        """Not asserted into place. It falls out of the stock payment, the monthly
        supplier bill and the tax due date overlapping -- so where it lands has to
        be looked up, and it lands next to the sale."""
        squeeze = full.squeeze()
        assert abs(squeeze["days_from_sale_start"]) <= 21

    def test_the_squeeze_is_attributed_to_real_outflows(self, full: Generator) -> None:
        contributors = full.squeeze()["outflows_within_a_week_paise"]
        assert contributors
        assert sum(contributors.values()) > 1_00_000_00

    def test_sale_revenue_arrives_after_the_sale_is_over(self, full: Generator) -> None:
        """Profit is not cash, made measurable.

        Sales made during the sale week settle a day or two later, so a meaningful
        share of the takings lands *after* the sale has ended. The merchant spends
        the week selling triple and holding a fraction of their opening balance.
        """
        cfg = full.cfg
        sale_week = {
            cfg.day_of(i) for i in range(cfg.promo_starts_on_day, cfg.promo_ends_on_day + 1)
        }
        sale_payments = [
            p
            for p in full.payments
            if p.status is PaymentStatus.CAPTURED and p.captured_at.date() in sale_week
        ]
        assert sale_payments

        after = sum(p.net for p in sale_payments if p.settles_on > cfg.promo_ends_on)
        total = sum(p.net for p in sale_payments)
        assert after / total > 0.15

        # And the balance during the sale is well below where it started.
        during = [full.balance[d] for d in sorted(sale_week)]
        assert statistics.mean(during) < cfg.opening_balance * 0.6

    def test_the_refund_wave_follows_the_sale(self, full: Generator) -> None:
        """The second, subtler half of the squeeze -- and the part only the estimated
        layer can see coming, because it is a consequence of sales already made.

        Sale-week orders return 5-14 days later, so refund outflow peaks exactly
        when revenue is dipping.
        """
        cfg = full.cfg
        sale_week = {
            cfg.day_of(i) for i in range(cfg.promo_starts_on_day, cfg.promo_ends_on_day + 1)
        }
        captured = {p.payment_id: p.captured_at.date() for p in full.payments}
        from_sale = [r for r in full.refunds if captured.get(r.payment_id) in sale_week]
        assert from_sale

        # Their money leaves in the fortnight after the sale ends.
        window_end = cfg.promo_ends_on + timedelta(days=16)
        landing_after = [
            r for r in from_sale if cfg.promo_ends_on < r.nets_off_on <= window_end
        ]
        assert len(landing_after) / len(from_sale) > 0.6

    def test_refund_outflow_peaks_after_the_sale(self, full: Generator) -> None:
        cfg = full.cfg
        by_day: dict[date, int] = {}
        for r in full.refunds:
            by_day[r.nets_off_on] = by_day.get(r.nets_off_on, 0) + r.amount

        def mean_over(start_index: int, count: int) -> float:
            days = [cfg.day_of(i) for i in range(start_index, start_index + count)]
            return statistics.mean(by_day.get(d, 0) for d in days)

        after_sale = mean_over(cfg.promo_ends_on_day + 1, 14)
        before_sale = mean_over(cfg.promo_starts_on_day - 21, 14)
        assert after_sale > before_sale
