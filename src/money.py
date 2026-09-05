"""Money is an integer number of paise.

`Decimal` appears only at the boundary -- parsing a CSV, formatting output -- and
every internal amount, sum and comparison is an `int`. Rounding is ROUND_HALF_UP,
not Python's banker's rounding.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

Paise = int  # 100 paise = 1 rupee

# Assumed rates. Check against Razorpay's pricing page before trusting output.
FEE_RATE = Decimal("0.02")  # of the transaction
GST_RATE = Decimal("0.18")  # of the fee, not of the transaction

_PAISE_PER_RUPEE = Decimal(100)


def round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def rupees_to_paise(value: str | int | float | Decimal) -> Paise:
    # Floats go via str() so that 0.07 means seven paise.
    if isinstance(value, float):
        value = str(value)
    return round_half_up(Decimal(value) * _PAISE_PER_RUPEE)


def paise_to_rupees(paise: Paise) -> Decimal:
    return (Decimal(paise) / _PAISE_PER_RUPEE).quantize(Decimal("0.01"))


def fmt(paise: Paise) -> str:
    """For CSV output. `-1234` -> `-12.34`."""
    return f"{paise_to_rupees(paise):.2f}"


def _group_indian(digits: str) -> str:
    """`1234567` -> `12,34,567`."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def fmt_inr(paise: Paise, *, exact: bool = False) -> str:
    """`18000000` -> `₹1,80,000`. Whole rupees unless `exact` is set."""
    rupees = paise_to_rupees(paise)
    sign = "-" if rupees < 0 else ""
    rupees = abs(rupees)
    if exact:
        whole = int(rupees)
        return f"{sign}₹{_group_indian(str(whole))}.{f'{rupees - whole:.2f}'[2:]}"
    return f"{sign}₹{_group_indian(str(round_half_up(rupees)))}"


def apply_rate(base: Paise, rate: Decimal) -> Paise:
    """`rate` of `base`, rounded half-up to whole paise.

    Call this per transaction, never once on a batch total. The two answers
    diverge, and per transaction is what the gateway does -- so it is what
    actually reaches the bank.
    """
    return round_half_up(Decimal(base) * rate)


def fee_on(amount: Paise, rate: Decimal = FEE_RATE) -> Paise:
    return apply_rate(amount, rate)


def gst_on_fee(fee: Paise, rate: Decimal = GST_RATE) -> Paise:
    """Note the argument: GST is charged on the fee, never on the sale."""
    return apply_rate(fee, rate)


def split(
    amount: Paise,
    fee_rate: Decimal = FEE_RATE,
    gst_rate: Decimal = GST_RATE,
) -> tuple[Paise, Paise, Paise]:
    """Split one transaction into `(fee, gst, net)`, summing exactly to `amount`."""
    fee = fee_on(amount, fee_rate)
    gst = gst_on_fee(fee, gst_rate)
    return fee, gst, amount - fee - gst


def total(amounts: Iterable[Paise]) -> Paise:
    return sum(amounts)
