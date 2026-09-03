"""Tests for the estimated layer.

This is the module with the most domain logic and, until now, no tests at all. The
double-counted refund that broke horizon 1 was caught by an invariant in a different
file -- that was luck rather than coverage, and it is what this file exists to stop
relying on.

Four things are worth testing hard, because each of them fails quietly and shifts
every number in the scoreboard rather than raising anything:

* a weekday average that drops empty days instead of counting them as zero, which
  inflates every forecast
* a refund rate measured over orders too recent to have finished refunding, which
  makes the business look better than it is
* a lag shape weighted by event count rather than by value, which lets a hundred
  ₹200 returns outvote one ₹20,000 one
* predicted sales reaching the bank on the day they are predicted, rather than
  going through the settlement calendar like real ones
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from src.estimate import DEFAULT_WEEKS, MAX_REFUND_LAG, Estimator
from src.events import Method, Order, Payment, PaymentStatus, Refund
from src.money import split
from src.world import BankBalance, EventStore, world_as_of

DATA = Path(__file__).resolve().parents[1] / "data"

#: A Wednesday, so weekday arithmetic in the fixtures is easy to follow.
AS_OF = dt.date(2026, 6, 10)


def at(day: dt.date, hour: int = 10) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, 0))


def days(n: int) -> dt.timedelta:
    return dt.timedelta(days=n)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def an_order(oid: str, placed: dt.date, amount: int) -> Order:
    return Order(order_id=oid, placed_at=at(placed), amount=amount,
                 method=Method.CARD, customer_id="c")


def a_payment(pid: str, oid: str, captured: dt.date, amount: int,
              method=Method.CARD, status=PaymentStatus.CAPTURED) -> Payment:
    if status is PaymentStatus.FAILED:
        return Payment(payment_id=pid, order_id=oid, amount=amount, method=method,
                       status=status, captured_at=at(captured))
    fee, gst, net = split(amount)
    return Payment(payment_id=pid, order_id=oid, amount=amount, method=method,
                   status=status, captured_at=at(captured),
                   settles_on=captured + days(2), fee=fee, gst=gst, net=net)


def a_refund(rid: str, oid: str, requested: dt.date, amount: int,
             netting: int = 1) -> Refund:
    return Refund(refund_id=rid, order_id=oid, payment_id="p", amount=amount,
                  requested_at=at(requested), nets_off_on=requested + days(netting))


def a_store(orders=(), payments=(), refunds=(), as_of=AS_OF) -> EventStore:
    return EventStore(
        orders=tuple(orders), payments=tuple(payments), refunds=tuple(refunds),
        chargebacks=(), outflows=(), promotions=(),
        balances=(BankBalance(day=1, date=as_of, opening=0, inflow=0,
                              outflow=0, closing=0),),
    )


def fitted(**kw) -> Estimator:
    return Estimator.fit(world_as_of(a_store(**kw), AS_OF))


# --------------------------------------------------------------------------
# The weekday sales baseline
# --------------------------------------------------------------------------


def test_weekday_average_uses_the_same_weekday_only():
    """Saturdays take 2.3x what Tuesdays do here, so averaging across weekdays
    would leak a big Saturday into every Tuesday forecast."""
    payments = []
    for weeks_back in range(1, 5):
        wed = AS_OF - days(7 * weeks_back)
        sat = wed + days(3)
        payments.append(a_payment(f"w{weeks_back}", "o", wed, 10_000_00))
        payments.append(a_payment(f"s{weeks_back}", "o", sat, 30_000_00))
    est = fitted(payments=payments)
    assert est.expected_sales(AS_OF + days(7)) == 10_000_00        # a Wednesday
    assert est.expected_sales(AS_OF + days(3)) == 30_000_00        # a Saturday


def test_a_day_with_no_sales_counts_as_zero_not_as_missing():
    """The quiet failure this file exists for. Dropping empty days instead of
    averaging them in would inflate every weekday baseline, and nothing would
    raise -- the forecast would simply be optimistic everywhere."""
    weds = [AS_OF - days(7 * n) for n in range(1, 5)]
    payments = [a_payment(f"p{i}", "o", d, 10_000_00)
                for i, d in enumerate(weds[:2])]        # two of four Wednesdays
    est = fitted(payments=payments)
    assert est.expected_sales(AS_OF + days(7)) == 5_000_00   # not 10,000


def test_only_the_last_n_weekdays_are_averaged():
    """Older weeks must fall out, or the baseline never tracks a changing business."""
    payments = [
        a_payment(f"p{n}", "o", AS_OF - days(7 * n), 100_00 if n > DEFAULT_WEEKS
                  else 10_000_00)
        for n in range(1, DEFAULT_WEEKS + 4)
    ]
    est = fitted(payments=payments)
    assert est.expected_sales(AS_OF + days(7)) == 10_000_00


def test_failed_payments_are_not_sales():
    """Including them would teach the estimator to expect revenue that never
    existed."""
    payments = [
        a_payment(f"ok{n}", "o", AS_OF - days(7 * n), 10_000_00)
        for n in range(1, 5)
    ] + [
        a_payment(f"bad{n}", "o", AS_OF - days(7 * n), 90_000_00,
                  status=PaymentStatus.FAILED)
        for n in range(1, 5)
    ]
    assert fitted(payments=payments).expected_sales(AS_OF + days(7)) == 10_000_00


def test_an_unseen_weekday_expects_nothing_rather_than_guessing():
    assert fitted().expected_sales(AS_OF + days(1)) == 0


# --------------------------------------------------------------------------
# The refund rate
# --------------------------------------------------------------------------


def test_refund_rate_is_value_returned_over_value_ordered():
    old = AS_OF - days(MAX_REFUND_LAG + 5)
    orders = [an_order("o1", old, 1_000_00), an_order("o2", old, 1_000_00)]
    refunds = [a_refund("r1", "o1", old + days(7), 200_00)]
    assert fitted(orders=orders, refunds=refunds).refund_rate == pytest.approx(0.10)


def test_recent_orders_are_excluded_from_the_rate():
    """The subtle one. Last week's orders have not finished refunding, so counting
    them in the denominator understates the rate -- a way of being optimistic that
    looks like arithmetic."""
    old = AS_OF - days(MAX_REFUND_LAG + 5)
    orders = [an_order("o1", old, 1_000_00),
              an_order("o2", AS_OF - days(2), 9_000_00)]   # too recent to count
    refunds = [a_refund("r1", "o1", old + days(7), 200_00)]
    est = fitted(orders=orders, refunds=refunds)
    assert est.refund_rate == pytest.approx(0.20)   # 200/1000, not 200/10000


def test_no_history_means_no_rate_rather_than_a_crash():
    est = fitted()
    assert est.refund_rate == 0.0
    assert est.expected_refunds(AS_OF + days(7)) == 0


# --------------------------------------------------------------------------
# The refund lag shape
# --------------------------------------------------------------------------


def test_lag_shape_is_weighted_by_value_not_by_event_count():
    """One ₹20,000 return matters more to a cash forecast than a hundred ₹200 ones.
    Counting events equally would flatten that away and put the expected outflow on
    the wrong days."""
    old = AS_OF - days(MAX_REFUND_LAG + 20)
    orders = [an_order("o1", old, 1_000_000_00)]
    refunds = [
        a_refund("big", "o1", old + days(5), 20_000_00),
        *[a_refund(f"small{i}", "o1", old + days(9), 200_00) for i in range(10)],
    ]
    shape = fitted(orders=orders, refunds=refunds).lag_shape
    assert shape[5] > shape[9]
    assert sum(shape.values()) == pytest.approx(1.0)


def test_lag_is_measured_from_the_order_not_the_request():
    """The estimator projects forward from orders, so the lag has to start there."""
    old = AS_OF - days(MAX_REFUND_LAG + 10)
    orders = [an_order("o1", old, 1_000_00)]
    refunds = [a_refund("r1", "o1", old + days(8), 100_00)]
    assert list(fitted(orders=orders, refunds=refunds).lag_shape) == [8]


def test_netting_lag_is_the_median_request_to_cash_gap():
    old = AS_OF - days(MAX_REFUND_LAG + 10)
    orders = [an_order("o1", old, 1_000_00)]
    refunds = [
        a_refund("r1", "o1", old + days(6), 100_00, netting=1),
        a_refund("r2", "o1", old + days(7), 100_00, netting=3),
        a_refund("r3", "o1", old + days(8), 100_00, netting=3),
    ]
    assert fitted(orders=orders, refunds=refunds).netting_lag == 3


# --------------------------------------------------------------------------
# Projecting refunds forward -- and not counting them twice
# --------------------------------------------------------------------------


def test_refunds_are_projected_from_orders_already_in_the_books():
    """The whole idea: you never learn which customer, only how much."""
    old = AS_OF - days(MAX_REFUND_LAG + 10)
    orders = [an_order("o1", old, 10_000_00)]
    refunds = [a_refund("r1", "o1", old + days(7), 1_000_00, netting=1)]
    # 10% of value comes back, all of it at a 7-day lag, netting a day later.
    est = fitted(orders=orders, payments=[a_payment("p", "o", AS_OF - days(3),
                                                    50_000_00)], refunds=refunds)
    target = AS_OF - days(3) + days(7) + days(1)
    assert est.expected_refunds(target) == pytest.approx(50_000_00 * 0.1, rel=0.01)


def test_a_refund_already_requested_is_never_estimated_again():
    """The bug that cost horizon 1 its exactness. A refund the customer has already
    asked for is a fact the certain layer is carrying; predicting it as well
    subtracts the same money twice."""
    old = AS_OF - days(MAX_REFUND_LAG + 10)
    orders = [an_order("o1", old, 10_000_00)]
    refunds = [a_refund("r1", "o1", old + days(7), 1_000_00, netting=1)]
    est = fitted(orders=orders, refunds=refunds)
    # netting_lag is 1, so anything landing tomorrow was requested by today.
    assert est.expected_refunds(AS_OF + days(1)) == 0


def test_the_cutoff_is_the_request_date_not_the_cash_date():
    old = AS_OF - days(MAX_REFUND_LAG + 10)
    orders = [an_order("o1", old, 10_000_00)]
    refunds = [a_refund("r1", "o1", old + days(7), 1_000_00, netting=3)]
    est = fitted(orders=orders, refunds=refunds)
    assert est.netting_lag == 3
    # Cash on as_of+3 implies a request on as_of -- already known.
    assert est.expected_refunds(AS_OF + days(3)) == 0
    # Cash on as_of+4 implies a request tomorrow -- not yet known, so estimated.
    assert est.expected_refunds(AS_OF + days(4)) >= 0


def test_forecast_sales_can_be_excluded_from_refund_projection():
    """A6, kept as a flag so the difference is measured rather than argued."""
    old = AS_OF - days(MAX_REFUND_LAG + 10)
    orders = [an_order("o1", old, 10_000_00)]
    refunds = [a_refund("r1", "o1", old + days(5), 1_000_00, netting=1)]
    payments = [a_payment(f"p{n}", "o", AS_OF - days(7 * n), 10_000_00)
                for n in range(1, 5)]
    est = fitted(orders=orders, payments=payments, refunds=refunds)
    # netting lag 1 and a 5-day order-to-request lag, so cash on as_of+13 traces
    # back to a sale on as_of+7 -- a Wednesday, which is the only weekday this
    # fixture gives a baseline to, and a day that has not happened yet.
    far = AS_OF + days(13)
    assert est.expected_refunds(far, refunds_from_forecast=True) > 0
    assert est.expected_refunds(far, refunds_from_forecast=False) == 0


# --------------------------------------------------------------------------
# Predicted sales reaching the bank
# --------------------------------------------------------------------------


def test_predicted_sales_settle_through_the_working_day_calendar():
    """A sale predicted for Tuesday does not arrive on Tuesday. Skipping this would
    make the estimated layer contribute at horizon 1 and destroy the one invariant
    that catches everything else."""
    payments = [a_payment(f"p{n}", "o", AS_OF - days(7 * n), 10_000_00)
                for n in range(1, 5)]
    est = fitted(payments=payments)
    assert est.expected_settlement(AS_OF + days(1)) == 0
    assert est.expected_settlement(AS_OF + days(2)) == 0


def test_settlement_arrives_net_of_fees():
    payments = [a_payment(f"p{n}", "o", AS_OF - days(7 * n), 10_000_00,
                          method=Method.UPI)
                for n in range(1, 5)]
    est = fitted(payments=payments)
    arriving = sum(est.expected_settlement(AS_OF + days(h)) for h in range(1, 15))

    # Only Wednesdays have a baseline here, and only the one at +7 settles inside
    # a 14-day window -- the one at +14 lands after it. Comparing against the full
    # 14 days of predicted sales would count a sale whose money has not arrived
    # yet, which is the mistake the settlement calendar exists to prevent.
    in_window = est.expected_sales(AS_OF + days(7))
    assert 0 < arriving < in_window
    assert arriving == pytest.approx(in_window * 0.9764, rel=0.01)  # 2% + 18% GST


def test_upi_share_is_measured_from_history_not_assumed():
    """The forecaster is not allowed to know how the world was made, so the mix
    comes from the payments it can see."""
    payments = [
        a_payment("u", "o", AS_OF - days(7), 60_000_00, method=Method.UPI),
        a_payment("c", "o", AS_OF - days(7), 40_000_00, method=Method.CARD),
    ]
    assert fitted(payments=payments).upi_share == pytest.approx(0.6)


# --------------------------------------------------------------------------
# Against the generated dataset
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_store() -> EventStore:
    if not (DATA / "balance.csv").exists():
        pytest.skip("no generated data -- run `python -m src.generate`")
    return EventStore.load(DATA)


def test_it_fits_at_every_vantage_point(real_store):
    for n in range(46, 107):
        est = Estimator.fit(world_as_of(real_store, real_store.date_for_day(n)))
        assert est.weekday_sales and 0 < est.refund_rate < 1
        assert est.lag_shape and est.netting_lag >= 0


def test_the_fit_uses_nothing_past_the_vantage_day(real_store):
    """The estimator is refitted at every vantage point rather than once up front.
    Fitting once would let the first vantage point's forecast rest on data only the
    last one could see -- a temporal leak wearing a different hat."""
    as_of = real_store.date_for_day(60)
    full = Estimator.fit(world_as_of(real_store, as_of))
    blind = Estimator.fit(world_as_of(real_store.truncated_to(as_of), as_of))
    assert full == blind


def test_the_measured_rate_and_shape_match_the_dataset(real_store):
    """Sanity-check the fitted numbers against what the generator actually did:
    ~12.7% of order value returned, on a hump from 5 to 15 days peaking near 7."""
    est = Estimator.fit(world_as_of(real_store, real_store.date_for_day(106)))
    assert 0.08 < est.refund_rate < 0.20
    assert 5 <= min(est.lag_shape) <= 7
    assert 12 <= max(est.lag_shape) <= 20
    assert max(est.lag_shape, key=est.lag_shape.get) in range(6, 11)


def test_weekend_sales_are_higher_than_midweek(real_store):
    """The pattern the estimator exists to find: Saturday takes about 2.3x Tuesday."""
    est = Estimator.fit(world_as_of(real_store, real_store.date_for_day(106)))
    assert est.weekday_sales[5] > est.weekday_sales[1]   # Saturday > Tuesday
