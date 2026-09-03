"""Tests for the temporal wall.

The point of this file is that every accuracy number the project reports is
worthless if the forecaster can see the future. So the wall gets tested harder than
anything else here: not just "does it filter", but "does it filter at the boundary",
and "does deleting the future actually change nothing".

Two kinds of test. The synthetic ones build a tiny world by hand, so a boundary
failure names itself. The `real_store` ones run against the generated 120 days,
because a hand-built fixture cannot catch a generator that stopped tying.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from src.events import (
    Chargeback,
    Method,
    Order,
    Outflow,
    OutflowKind,
    Payment,
    PaymentStatus,
    Promotion,
    Refund,
)
from src.world import (
    BankBalance,
    Divergence,
    EventStore,
    describe,
    diff_daily,
    world_as_of,
)

DATA = Path(__file__).resolve().parents[1] / "data"

D1 = dt.date(2026, 6, 10)
D2 = dt.date(2026, 6, 11)  # the vantage day throughout
D3 = dt.date(2026, 6, 12)
D4 = dt.date(2026, 6, 13)


def at(day: dt.date, hour: int = 12) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, 0))


# --------------------------------------------------------------------------
# A hand-built world, small enough to reason about
# --------------------------------------------------------------------------


def a_payment(pid: str, known: dt.date, settles: dt.date | None, amount=100_00,
              status=PaymentStatus.CAPTURED) -> Payment:
    fee, gst = (200, 36) if status is PaymentStatus.CAPTURED else (0, 0)
    return Payment(
        payment_id=pid,
        order_id="ord_" + pid,
        amount=amount,
        method=Method.CARD,
        status=status,
        captured_at=at(known),
        settles_on=settles,
        fee=fee if status is PaymentStatus.CAPTURED else 0,
        gst=gst if status is PaymentStatus.CAPTURED else 0,
        net=amount - fee - gst if status is PaymentStatus.CAPTURED else 0,
    )


def a_balance(n: int, day: dt.date, closing: int) -> BankBalance:
    return BankBalance(
        day=n, date=day, opening=closing, inflow=0, outflow=0, closing=closing
    )


@pytest.fixture
def tiny() -> EventStore:
    """Every record sits either side of the D2 boundary, or exactly on it."""
    return EventStore(
        orders=(
            Order(order_id="ord_before", placed_at=at(D1), amount=100_00,
                  method=Method.CARD, customer_id="c1"),
            Order(order_id="ord_after", placed_at=at(D3), amount=100_00,
                  method=Method.CARD, customer_id="c2"),
        ),
        payments=(
            a_payment("pay_lands_today", known=D1, settles=D2),
            a_payment("pay_in_flight", known=D2, settles=D4),
            a_payment("pay_failed", known=D2, settles=None,
                      status=PaymentStatus.FAILED),
            a_payment("pay_future", known=D3, settles=D4),
        ),
        refunds=(
            Refund(refund_id="rf_today", order_id="o", payment_id="p",
                   amount=50_00, requested_at=at(D1), nets_off_on=D2),
            Refund(refund_id="rf_pending", order_id="o", payment_id="p",
                   amount=60_00, requested_at=at(D2), nets_off_on=D4),
        ),
        chargebacks=(
            Chargeback(chargeback_id="cb_pending", payment_id="p", amount=70_00,
                       raised_at=at(D2), debited_on=D4,
                       original_captured_on=D1),
        ),
        outflows=(
            Outflow(outflow_id="out_today", kind=OutflowKind.RENT, amount=10_00,
                    committed_at=at(D1), due_on=D2),
            Outflow(outflow_id="out_soon", kind=OutflowKind.SALARY, amount=20_00,
                    committed_at=at(D1), due_on=D3),
            Outflow(outflow_id="out_edge", kind=OutflowKind.SUPPLIER, amount=30_00,
                    committed_at=at(D1), due_on=D4),
        ),
        promotions=(
            Promotion(promotion_id="promo_declared", name="early",
                      declared_at=at(D1), starts_on=D3, ends_on=D4,
                      expected_volume_uplift=2.0),
            Promotion(promotion_id="promo_secret", name="late",
                      declared_at=at(D3), starts_on=D3, ends_on=D4,
                      expected_volume_uplift=3.0),
        ),
        balances=(
            a_balance(1, D1, 1_000_00),
            a_balance(2, D2, 1_200_00),
            a_balance(3, D3, 9_999_00),  # the future: must never be visible
            a_balance(4, D4, 8_888_00),
        ),
    )


# --------------------------------------------------------------------------
# The wall filters, and it filters at the boundary
# --------------------------------------------------------------------------


def test_nothing_visible_was_known_later(tiny):
    w = world_as_of(tiny, D2)
    late = [
        e
        for e in (w.orders + w.payments + w.refunds + w.chargebacks
                  + w.outflows + w.promotions)
        if e.known_at > D2
    ]
    assert late == []


def test_events_known_on_the_vantage_day_are_visible(tiny):
    """`known_at <= day`, not `<`. A payment captured this morning is known."""
    w = world_as_of(tiny, D2)
    assert {p.payment_id for p in w.payments} == {
        "pay_lands_today", "pay_in_flight", "pay_failed"
    }


def test_opening_balance_is_the_vantage_days_closing(tiny):
    assert world_as_of(tiny, D2).opening_balance == 1_200_00


def test_opening_balance_cannot_see_tomorrow(tiny):
    """The 9,999 on D3 is bait: reading the wrong statement row would take it."""
    assert world_as_of(tiny, D2).opening_balance != 9_999_00


def test_opening_balance_holds_over_a_gap(tiny):
    """No statement row today -> the most recent one still stands."""
    sparse = EventStore(**{**tiny.__dict__, "balances": (a_balance(1, D1, 1_000_00),)})
    assert world_as_of(sparse, D2).opening_balance == 1_000_00


def test_no_statement_at_all_raises(tiny):
    empty = EventStore(**{**tiny.__dict__, "balances": ()})
    with pytest.raises(ValueError, match="no bank statement"):
        _ = world_as_of(empty, D2).opening_balance


# --------------------------------------------------------------------------
# The named queries -- the definitions that must not drift
# --------------------------------------------------------------------------


def test_in_flight_excludes_money_that_landed_today(tiny):
    """`settles_on > as_of`. Today's settlement is already in the balance;
    counting it again is the double-count this boundary exists to stop."""
    ids = {p.payment_id for p in world_as_of(tiny, D2).payments_in_flight()}
    assert ids == {"pay_in_flight"}
    assert "pay_lands_today" not in ids


def test_in_flight_excludes_failed_payments(tiny):
    assert all(
        p.status is PaymentStatus.CAPTURED
        for p in world_as_of(tiny, D2).payments_in_flight()
    )


def test_refunds_pending_excludes_todays(tiny):
    ids = {r.refund_id for r in world_as_of(tiny, D2).refunds_pending()}
    assert ids == {"rf_pending"}


def test_chargeback_raised_but_not_debited_is_pending(tiny):
    """The case the whole two-date model exists for."""
    cbs = world_as_of(tiny, D2).chargebacks_pending()
    assert [c.chargeback_id for c in cbs] == ["cb_pending"]
    assert cbs[0].known_at <= D2 < cbs[0].cash_at


def test_committed_outflows_window_is_exclusive_then_inclusive(tiny):
    """Due today is spent; due on `through` still counts."""
    ids = {o.outflow_id for o in world_as_of(tiny, D2).committed_outflows(D4)}
    assert ids == {"out_soon", "out_edge"}
    assert {o.outflow_id for o in world_as_of(tiny, D2).committed_outflows(D3)} == {
        "out_soon"
    }


def test_undeclared_promotion_is_invisible(tiny):
    """The hidden-versus-declared experiment, done by the wall alone."""
    covering = world_as_of(tiny, D2).promotions_covering(D3)
    assert [p.promotion_id for p in covering] == ["promo_declared"]


def test_declared_later_becomes_visible_later(tiny):
    covering = world_as_of(tiny, D3).promotions_covering(D3)
    assert {p.promotion_id for p in covering} == {"promo_declared", "promo_secret"}


def test_certain_movements_are_the_sum_of_the_parts(tiny):
    w = world_as_of(tiny, D2)
    expected = (
        sum(p.cash_delta for p in w.payments_in_flight())
        + sum(r.cash_delta for r in w.refunds_pending())
        + sum(c.cash_delta for c in w.chargebacks_pending())
        + sum(o.cash_delta for o in w.committed_outflows(D4))
    )
    assert sum(e.cash_delta for e in w.certain_movements(D4)) == expected


# --------------------------------------------------------------------------
# The leak test
# --------------------------------------------------------------------------


def test_deleting_the_future_changes_nothing(tiny):
    """The five-line test that makes every later number trustworthy."""
    assert world_as_of(tiny, D2) == world_as_of(tiny.truncated_to(D2), D2)


def test_truncation_actually_removed_something(tiny):
    """Otherwise the test above could pass by doing nothing at all."""
    assert len(tiny.truncated_to(D2).events) < len(tiny.events)


# --------------------------------------------------------------------------
# Invariants and the audit
# --------------------------------------------------------------------------


def test_cash_may_not_move_before_it_is_knowable(tiny):
    tiny.check_two_dates()


def test_the_audit_catches_a_statement_that_does_not_tie(tiny):
    """`tiny`'s balances were written by hand and deliberately do not match its
    events, so this is the audit failing on purpose. Without this test the real
    dataset passing `check_balance_ties` would prove nothing -- a check that cannot
    fail is not a check."""
    with pytest.raises(AssertionError) as exc:
        tiny.check_balance_ties(D2)
    message = str(exc.value)
    assert str(D2) in message and "off by" in message


def test_a_backdated_event_is_caught():
    """If this ever passes silently the two-date model is broken."""
    bad = EventStore(
        orders=(), payments=(), refunds=(),
        chargebacks=(
            Chargeback(chargeback_id="cb_bad", payment_id="p", amount=100,
                       raised_at=at(D3), debited_on=D1,
                       original_captured_on=D1),
        ),
        outflows=(), promotions=(), balances=(a_balance(1, D1, 0),),
    )
    with pytest.raises(AssertionError, match="precedes known_at"):
        bad.check_two_dates()


# --------------------------------------------------------------------------
# Divergence reporting -- A1: name the day and the amount
# --------------------------------------------------------------------------


def test_diff_names_the_day_and_the_amount():
    diffs = diff_daily({D1: 100, D2: 200}, {D1: 100, D2: 150})
    assert len(diffs) == 1
    assert diffs[0].day == D2 and diffs[0].delta == 50
    assert "off by" in str(diffs[0])


def test_identical_series_report_nothing():
    assert diff_daily({D1: 100}, {D1: 100}) == []
    assert describe([]) == "identical"


def test_a_short_series_is_a_divergence_not_a_match():
    """A forecast that stopped early has failed, not matched."""
    assert [d.day for d in diff_daily({D1: 100, D2: 200}, {D1: 100})] == [D2]


def test_diffs_come_back_in_date_order():
    diffs = diff_daily({D3: 1, D1: 1, D2: 1}, {})
    assert [d.day for d in diffs] == [D1, D2, D3]


# --------------------------------------------------------------------------
# Against the generated dataset
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_store() -> EventStore:
    if not (DATA / "balance.csv").exists():
        pytest.skip("no generated data -- run `python -m src.generate`")
    return EventStore.load(DATA)


def test_real_data_respects_the_two_date_order(real_store):
    real_store.check_two_dates()


def test_events_reproduce_the_bank_statement(real_store):
    """The audit. Summing `cash_delta` must rebuild `balance.csv` on every one of
    the 120 days -- because that summation *is* the forecast's model of the world,
    and a wrong sign anywhere would shift every forecast by a constant that no
    other test would notice."""
    real_store.check_balance_ties()


def test_the_wall_holds_at_every_vantage_point(real_store):
    """61 vantage points, each compared against a physically truncated store."""
    for n in range(46, 107):
        day = real_store.date_for_day(n)
        assert world_as_of(real_store, day) == world_as_of(
            real_store.truncated_to(day), day
        ), f"leak at day {n} ({day})"


def test_opening_balance_matches_the_statement(real_store):
    for n in (46, 75, 106):
        day = real_store.date_for_day(n)
        assert (
            world_as_of(real_store, day).opening_balance
            == real_store.balance_on(day).closing
        )


def test_the_promotion_is_hidden_before_it_is_declared(real_store):
    """Declared 2026-06-10, runs from the 25th. Standing on the 9th the forecaster
    must not see it; standing on the 10th it must."""
    promo = real_store.promotions[0]
    declared = promo.known_at
    before = world_as_of(real_store, declared - dt.timedelta(days=1))
    on = world_as_of(real_store, declared)
    assert before.promotions_covering(promo.starts_on) == ()
    assert on.promotions_covering(promo.starts_on) == (promo,)


def test_in_flight_money_is_never_negative_or_absurd(real_store):
    """A sanity floor: ~10 orders a day at T+1/T+2 should leave a few thousand
    rupees in flight, never nothing and never a month's revenue."""
    w = world_as_of(real_store, real_store.date_for_day(60))
    in_flight = sum(p.net for p in w.payments_in_flight())
    assert 0 < in_flight < 5_00_000_00
