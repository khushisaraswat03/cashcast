"""Tests for the event model.

Two things are being checked. First, that the `known_at` / `cash_at` split actually
holds -- because if a knowable-but-unmoved event reports the wrong date, the certain
layer silently becomes a guess. Second, that the invariants refuse bad data rather
than passing it downstream, since a forecast built on an event whose components do
not add up is wrong in a way no later test would catch.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.events import (
    EVENT_FILES,
    Chargeback,
    CsvModel,
    Method,
    Order,
    Outflow,
    OutflowKind,
    Payment,
    PaymentStatus,
    Promotion,
    Refund,
)
from src.money import split

MON = dt.date(2026, 8, 24)
WED = dt.date(2026, 8, 26)
NOON = dt.time(12, 0)


def at(day: dt.date, hour: int = 12) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, 0))


# --------------------------------------------------------------------------
# Builders -- keep the tests about behaviour rather than constructor noise
# --------------------------------------------------------------------------


def an_order(**kw) -> Order:
    return Order(
        **{
            "order_id": "ord_1",
            "placed_at": at(MON),
            "amount": 140_000,
            "method": Method.CARD,
            "customer_id": "cust_1",
            **kw,
        }
    )


def a_payment(amount: int = 140_000, **kw) -> Payment:
    fee, gst, net = split(amount)
    return Payment(
        **{
            "payment_id": "pay_1",
            "order_id": "ord_1",
            "amount": amount,
            "method": Method.CARD,
            "status": PaymentStatus.CAPTURED,
            "captured_at": at(MON),
            "settles_on": WED,
            "fee": fee,
            "gst": gst,
            "net": net,
            **kw,
        }
    )


def a_refund(**kw) -> Refund:
    return Refund(
        **{
            "refund_id": "rfnd_1",
            "order_id": "ord_1",
            "payment_id": "pay_1",
            "amount": 50_000,
            "requested_at": at(WED),
            "nets_off_on": dt.date(2026, 9, 2),
            **kw,
        }
    )


def a_chargeback(**kw) -> Chargeback:
    return Chargeback(
        **{
            "chargeback_id": "cb_1",
            "payment_id": "pay_old",
            "amount": 45_000,
            "raised_at": at(MON),
            "debited_on": dt.date(2026, 8, 28),
            "original_captured_on": dt.date(2026, 6, 10),
            **kw,
        }
    )


def an_outflow(**kw) -> Outflow:
    return Outflow(
        **{
            "outflow_id": "out_1",
            "kind": OutflowKind.RENT,
            "amount": 1_500_000,
            "committed_at": at(dt.date(2026, 8, 1)),
            "due_on": dt.date(2026, 9, 1),
            "description": "office rent",
            **kw,
        }
    )


def a_promotion(**kw) -> Promotion:
    return Promotion(
        **{
            "promotion_id": "promo_1",
            "name": "end of season",
            "declared_at": at(dt.date(2026, 8, 10)),
            "starts_on": dt.date(2026, 9, 1),
            "ends_on": dt.date(2026, 9, 7),
            "expected_volume_uplift": 3.0,
            **kw,
        }
    )


ALL_BUILDERS = [an_order, a_payment, a_refund, a_chargeback, an_outflow, a_promotion]


# --------------------------------------------------------------------------
# The two-date protocol
# --------------------------------------------------------------------------


class TestKnownAt:
    """Every event must report when it became knowable. The wall filters on it, so
    a wrong value here leaks the future or hides the present."""

    @pytest.mark.parametrize("build", ALL_BUILDERS)
    def test_every_event_has_a_known_at(self, build) -> None:
        assert isinstance(build().known_at, dt.date)

    @pytest.mark.parametrize("build", ALL_BUILDERS)
    def test_every_event_has_an_id(self, build) -> None:
        assert build().event_id

    def test_known_at_is_the_business_moment_not_the_cash_moment(self) -> None:
        payment = a_payment()
        assert payment.known_at == MON  # captured Monday
        assert payment.cash_at == WED  # money arrives Wednesday
        assert payment.known_at < payment.cash_at


class TestCashMovement:
    """Four types move money; two do not. The forecaster relies on that split, so
    it is asserted rather than assumed."""

    def test_orders_and_promotions_move_no_money(self) -> None:
        for build in (an_order, a_promotion):
            event = build()
            assert event.cash_at is None
            assert event.cash_delta == 0
            assert not event.moves_cash

    def test_payments_bring_money_in(self) -> None:
        payment = a_payment(amount=140_000)
        assert payment.cash_delta > 0
        # What arrives is net of fee and GST, not the sale value.
        assert payment.cash_delta == 136_696
        assert payment.cash_delta < payment.amount
        assert payment.moves_cash

    def test_refunds_chargebacks_and_outflows_take_money_out(self) -> None:
        for build in (a_refund, a_chargeback, an_outflow):
            event = build()
            assert event.cash_delta < 0, build.__name__
            assert event.moves_cash

    def test_a_refund_costs_its_full_face_value(self) -> None:
        """The gateway does not return the fee it took on the original sale, so a
        Rs.500 refund costs Rs.500 -- not Rs.500 less 2%."""
        assert a_refund(amount=50_000).cash_delta == -50_000

    def test_balance_change_is_just_a_sum(self) -> None:
        """The point of the uniform protocol: no per-type branching. A day's
        movement is the sum of cash_delta over events landing that day."""
        day = dt.date(2026, 9, 1)
        events = [
            a_payment(payment_id="p1", amount=100_000, settles_on=day),
            a_payment(payment_id="p2", amount=200_000, settles_on=day),
            a_refund(amount=30_000, nets_off_on=day),
            an_outflow(amount=150_000, due_on=day),
            an_order(),  # no cash effect
            a_promotion(),  # no cash effect
        ]
        landing = [e for e in events if e.cash_at == day]
        assert len(landing) == 4
        # 97,640 + 195,280 - 30,000 - 150,000
        assert sum(e.cash_delta for e in landing) == 112_920


# --------------------------------------------------------------------------
# Failed payments
# --------------------------------------------------------------------------


class TestFailedPayments:
    """A failed payment collected nothing, so it can never move money. It is kept
    because it explains why an order produced no revenue."""

    def test_failed_payment_moves_no_cash(self) -> None:
        failed = a_payment(status=PaymentStatus.FAILED, settles_on=None, fee=0, gst=0, net=0)
        assert failed.cash_at is None
        assert failed.cash_delta == 0
        assert not failed.moves_cash
        assert failed.known_at == MON  # still knowable, still explains something

    def test_failed_payment_may_not_have_a_settlement_date(self) -> None:
        with pytest.raises(ValidationError, match="cannot have a settlement date"):
            a_payment(status=PaymentStatus.FAILED, fee=0, gst=0, net=0)

    def test_failed_payment_may_not_have_a_fee(self) -> None:
        with pytest.raises(ValidationError, match="no fee, gst or net"):
            a_payment(status=PaymentStatus.FAILED, settles_on=None)

    def test_captured_payment_must_have_a_settlement_date(self) -> None:
        with pytest.raises(ValidationError, match="must have a settlement date"):
            a_payment(settles_on=None)


class TestComponentsMustTie:
    """fee + gst + net == amount, enforced at construction.

    Without this, a rounding mistake in the generator produces a payment whose
    parts do not add up, the forecast quietly inherits it, and nothing downstream
    ever notices.
    """

    def test_components_that_do_not_tie_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="expected 140000"):
            a_payment(fee=2_800, gst=504, net=100_000)

    @pytest.mark.parametrize("amount", [100, 13_200, 140_000, 999_999, 1])
    def test_split_always_ties(self, amount: int) -> None:
        payment = a_payment(payment_id=f"pay_{amount}", amount=amount)
        assert payment.fee + payment.gst + payment.net == amount


# --------------------------------------------------------------------------
# Promotions
# --------------------------------------------------------------------------


class TestPromotion:
    def test_declared_at_is_independent_of_when_the_sale_runs(self) -> None:
        """This is the hidden-versus-declared experiment in one assertion.

        The same sale, declared before or after a vantage day, is visible or
        invisible to the forecaster. Nothing else about the data changes.
        """
        sale_starts = dt.date(2026, 9, 1)
        declared_early = a_promotion(declared_at=at(dt.date(2026, 8, 10)))
        declared_late = a_promotion(declared_at=at(dt.date(2026, 9, 1)))

        vantage = dt.date(2026, 8, 20)
        assert declared_early.known_at <= vantage  # forecaster sees it coming
        assert declared_late.known_at > vantage  # forecaster is blindsided
        assert declared_early.starts_on == declared_late.starts_on == sale_starts

    def test_covers_is_inclusive_at_both_ends(self) -> None:
        promo = a_promotion(starts_on=dt.date(2026, 9, 1), ends_on=dt.date(2026, 9, 7))
        assert promo.covers(dt.date(2026, 9, 1))
        assert promo.covers(dt.date(2026, 9, 7))
        assert not promo.covers(dt.date(2026, 8, 31))
        assert not promo.covers(dt.date(2026, 9, 8))


class TestChargeback:
    def test_the_disputed_sale_may_predate_the_window(self) -> None:
        """Money leaves in August against a sale from June. Nothing local explains
        it, which is exactly why it belongs in the honest layer."""
        cb = a_chargeback()
        assert cb.original_captured_on < cb.known_at
        assert (cb.known_at - cb.original_captured_on).days > 30
        assert cb.known_at < cb.cash_at  # raised before it is debited


# --------------------------------------------------------------------------
# CSV round trip
# --------------------------------------------------------------------------


class TestCsvRoundTrip:
    """The generator writes these files and the forecaster reads them back. A
    column that does not survive the trip breaks the wall test, not this one -- so
    it is cheaper to catch it here."""

    @pytest.mark.parametrize("build", ALL_BUILDERS)
    def test_survives_the_round_trip(self, build, tmp_path: Path) -> None:
        original = build()
        model = type(original)
        path = tmp_path / EVENT_FILES[model]

        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=model.csv_fields())
            writer.writeheader()
            writer.writerow(original.to_csv_row())

        with path.open(newline="", encoding="utf-8") as fh:
            restored = model.from_csv_row(next(csv.DictReader(fh)))

        assert restored == original

    def test_money_survives_as_exact_paise(self, tmp_path: Path) -> None:
        payment = a_payment(amount=13_200)  # the Rs.132 case, where GST rounds
        restored = Payment.from_csv_row(payment.to_csv_row())
        assert restored.amount == 13_200
        assert restored.fee == 264
        assert restored.gst == 48
        assert restored.net == 12_888

    def test_optional_dates_survive_as_none(self) -> None:
        failed = a_payment(status=PaymentStatus.FAILED, settles_on=None, fee=0, gst=0, net=0)
        row = failed.to_csv_row()
        assert row["settles_on"] == ""
        assert Payment.from_csv_row(row).settles_on is None

    def test_every_event_type_has_a_filename(self) -> None:
        for build in ALL_BUILDERS:
            assert type(build()) in EVENT_FILES

    def test_unknown_columns_are_rejected(self) -> None:
        """A misspelt column must fail loudly rather than being silently dropped.

        The dangerous version is a typo in an *optional* column: the field falls
        back to its default, the data looks fine, and the forecast is quietly built
        on a missing value.
        """
        row = an_order().to_csv_row() | {"amont": "100.00"}
        with pytest.raises(ValueError, match="unknown column"):
            Order.from_csv_row(row)

    def test_missing_required_column_names_the_field(self) -> None:
        row = an_order().to_csv_row()
        del row["amount"]
        with pytest.raises(ValidationError, match="amount"):
            Order.from_csv_row(row)

    def test_trailing_empty_column_is_tolerated(self) -> None:
        """A trailing comma in a hand-edited CSV yields an empty key. That is
        sloppiness, not corruption, so it should not stop the read."""
        row = an_order().to_csv_row() | {"": ""}
        assert Order.from_csv_row(row) == an_order()
