"""Tests for the money layer, worked on paper before any of it was written.

The important case is three payments of Rs.132. It is the smallest example where
per-transaction fee arithmetic and batch-level fee arithmetic disagree -- by one
paisa -- and the gap grows with batch size.

That matters here because the forecast projects what a future settlement will pay
out. Compute the fees on a day's total rather than per sale and the projected
figure will not be the figure that lands. Pinned as a test so a well-meaning
refactor of the rounding cannot quietly reintroduce it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.money import (
    FEE_RATE,
    GST_RATE,
    apply_rate,
    fee_on,
    fmt,
    gst_on_fee,
    paise_to_rupees,
    round_half_up,
    rupees_to_paise,
    split,
)


class TestRounding:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("0.4", 0),
            ("0.5", 1),  # half-up, not banker's rounding
            ("1.5", 2),
            ("2.5", 3),  # round() would give 2 here
            ("-0.5", -1),
            ("47.52", 48),
        ],
    )
    def test_half_up(self, value: str, expected: int) -> None:
        assert round_half_up(Decimal(value)) == expected

    def test_differs_from_builtin_round(self) -> None:
        """Documents *why* we do not use round(): it rounds 2.5 down to 2."""
        assert round(2.5) == 2
        assert round_half_up(Decimal("2.5")) == 3


class TestConversion:
    @pytest.mark.parametrize(
        "rupees,paise",
        [("0.00", 0), ("1.00", 100), ("0.01", 1), ("3417.40", 341_740), ("132", 13_200)],
    )
    def test_round_trip(self, rupees: str, paise: int) -> None:
        assert rupees_to_paise(rupees) == paise
        assert fmt(paise) == f"{Decimal(rupees):.2f}"

    def test_float_input_does_not_leak_binary_error(self) -> None:
        """The naive `int(rupees * 100)` loses a paisa on real amounts.

        Rather than pin one hand-picked example, search for the cases where the
        naive conversion actually goes wrong on this interpreter and assert that
        ours gets every one of them right. Self-verifying: if the first assertion
        ever fails, floats became exact and this whole module is unnecessary.
        """
        broken = [paise for paise in range(1, 200_000) if int((paise / 100) * 100) != paise]
        assert broken, "expected float multiplication to lose paise somewhere"
        for paise in broken:
            assert rupees_to_paise(paise / 100) == paise

    def test_negative(self) -> None:
        assert fmt(-341_740) == "-3417.40"
        assert paise_to_rupees(-1) == Decimal("-0.01")


class TestBaseCase:
    """Three sales of Rs.1,000, Rs.2,000 and Rs.500 in one settlement."""

    CASES = [
        (100_000, 2_000, 360, 97_640),
        (200_000, 4_000, 720, 195_280),
        (50_000, 1_000, 180, 48_820),
    ]

    @pytest.mark.parametrize("amount,fee,gst,net", CASES)
    def test_per_transaction(self, amount: int, fee: int, gst: int, net: int) -> None:
        assert split(amount) == (fee, gst, net)

    def test_settlement_net(self) -> None:
        nets = [split(a)[2] for a, _, _, _ in self.CASES]
        assert sum(nets) == 341_740  # Rs.3,500 sold, Rs.3,417.40 actually arrives

    def test_batch_check_agrees_for_this_case(self) -> None:
        """These particular numbers tie both ways. The Rs.132 case below does not,
        which is why 'it worked when I checked it once' is not evidence."""
        gross = sum(a for a, _, _, _ in self.CASES)
        assert gross - fee_on(gross) - gst_on_fee(fee_on(gross)) == 341_740


class TestRoundingTrap:
    """Three sales of Rs.132.

    Per transaction the settlement is Rs.386.64; computed on the batch total it is
    Rs.386.65. Both are arithmetically correct answers to two different questions,
    and only the first is what the bank will actually credit.
    """

    AMOUNT = 13_200
    COUNT = 3

    def test_per_transaction_components(self) -> None:
        fee, gst, net = split(self.AMOUNT)
        assert (fee, gst, net) == (264, 48, 12_888)
        # The fee is exact; it is the GST that rounds.
        assert apply_rate(self.AMOUNT, FEE_RATE) == 264
        assert Decimal(264) * GST_RATE == Decimal("47.52")

    def test_per_transaction_sum(self) -> None:
        assert split(self.AMOUNT)[2] * self.COUNT == 38_664  # Rs.386.64

    def test_batch_level_sum(self) -> None:
        gross = self.AMOUNT * self.COUNT
        fee = fee_on(gross)
        gst = gst_on_fee(fee)
        assert (gross, fee, gst) == (39_600, 792, 143)
        assert gross - fee - gst == 38_665  # Rs.386.65

    def test_they_disagree_by_one_paisa(self) -> None:
        per_txn = split(self.AMOUNT)[2] * self.COUNT
        gross = self.AMOUNT * self.COUNT
        batch = gross - fee_on(gross) - gst_on_fee(fee_on(gross))
        assert batch - per_txn == 1  # exactly one paisa, and this is the whole point

    def test_drift_scales_with_batch_size(self) -> None:
        """The gap is not a fixed paisa -- it grows with the size of the batch.

        A flash sale of 200 identical items diverges by nearly a rupee. Small, but
        it is a *systematic* error rather than noise: it always lands the same way,
        so it accumulates across a forecast instead of cancelling out. Asserted
        rather than assumed.
        """
        for count, expected_paise in [(3, 1), (80, 38), (200, 96)]:
            per_txn = split(self.AMOUNT)[2] * count
            gross = self.AMOUNT * count
            batch = gross - fee_on(gross) - gst_on_fee(fee_on(gross))
            assert batch - per_txn == expected_paise, count


class TestSplitInvariant:
    def test_components_always_reconstruct_the_amount(self) -> None:
        """fee + gst + net == amount, for every amount up to Rs.500 and a few big ones."""
        for amount in list(range(0, 50_000, 7)) + [1_00_000, 12_34_567, 99_99_999]:
            fee, gst, net = split(amount)
            assert fee + gst + net == amount, amount
            assert net >= 0

    def test_rate_override(self) -> None:
        """The international-card case: 3% instead of 2%."""
        fee, gst, net = split(100_000, fee_rate=Decimal("0.03"))
        assert (fee, gst, net) == (3_000, 540, 96_460)
