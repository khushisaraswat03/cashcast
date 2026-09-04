"""Money handling.

One rule: **money is an integer number of paise.** `Decimal` appears only at the
boundary -- parsing a CSV, formatting output -- and every internal amount, sum and
comparison is `int`.

`0.1 + 0.2 != 0.3` is a curiosity in most programs and a defect here, because the
product is a claim that a projected balance can be trusted.

Rounding is ROUND_HALF_UP, not Python's banker's rounding: `round(2.5)` gives 2,
which is defensible for statistics and wrong for an invoice.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

# A monetary amount, in paise. 100 paise = 1 rupee.
Paise = int

# --- Assumptions. Confirm against the live Razorpay pricing page before trusting
# --- any number this repo prints. The *structure* is what is being modelled.
FEE_RATE = Decimal("0.02")  # 2% of the transaction amount
GST_RATE = Decimal("0.18")  # 18% of the FEE, not of the transaction

_PAISE_PER_RUPEE = Decimal(100)


def round_half_up(value: Decimal) -> int:
    """Round a Decimal to the nearest integer, halves away from zero."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def rupees_to_paise(value: str | int | float | Decimal) -> Paise:
    """Parse a rupee amount into paise.

    Accepts a float only as a convenience for tests and literals; it is converted
    via `str()` so that `0.07` means seven paise rather than
    0.070000000000000007. Prefer passing a string or Decimal.
    """
    if isinstance(value, float):
        value = str(value)
    return round_half_up(Decimal(value) * _PAISE_PER_RUPEE)


def paise_to_rupees(paise: Paise) -> Decimal:
    """Exact rupee value of an integer paise amount."""
    return (Decimal(paise) / _PAISE_PER_RUPEE).quantize(Decimal("0.01"))


def fmt(paise: Paise) -> str:
    """Format paise as a plain 2-decimal string, for CSV output. `-1234` -> `-12.34`."""
    return f"{paise_to_rupees(paise):.2f}"


def _group_indian(digits: str) -> str:
    """Indian digit grouping -- last three, then pairs. `1234567` -> `12,34,567`."""
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
    """Format paise for a reader. `18000000` -> `₹1,80,000`.

    Whole rupees by default: paise are noise in a balance projection and a column
    of them is harder to scan. Pass `exact=True` where the paise are the point,
    such as a fee breakdown that has to be seen to add up.
    """
    rupees = paise_to_rupees(paise)
    sign = "-" if rupees < 0 else ""
    rupees = abs(rupees)
    if exact:
        whole = int(rupees)
        return f"{sign}₹{_group_indian(str(whole))}.{f'{rupees - whole:.2f}'[2:]}"
    return f"{sign}₹{_group_indian(str(round_half_up(rupees)))}"


def apply_rate(base: Paise, rate: Decimal) -> Paise:
    """`rate` of `base`, rounded half-up to whole paise.

    This single function is where the rounding trap lives. Called per transaction
    it produces one answer; called once on a batch total it produces another, and
    the two diverge by more the larger the batch. Always call it per transaction --
    that is what the gateway does, so it is what will actually reach the bank, and
    therefore what the forecast has to project.
    """
    return round_half_up(Decimal(base) * rate)


def fee_on(amount: Paise, rate: Decimal = FEE_RATE) -> Paise:
    """Gateway fee on a single transaction."""
    return apply_rate(amount, rate)


def gst_on_fee(fee: Paise, rate: Decimal = GST_RATE) -> Paise:
    """GST on the fee. Note the argument: GST is charged on the fee, never on the sale."""
    return apply_rate(fee, rate)


def split(
    amount: Paise,
    fee_rate: Decimal = FEE_RATE,
    gst_rate: Decimal = GST_RATE,
) -> tuple[Paise, Paise, Paise]:
    """Split one transaction into `(fee, gst, net)`. `fee + gst + net == amount` exactly."""
    fee = fee_on(amount, fee_rate)
    gst = gst_on_fee(fee, gst_rate)
    return fee, gst, amount - fee - gst


def total(amounts: Iterable[Paise]) -> Paise:
    """Sum of paise. Trivial, but named so the intent reads at the call site."""
    return sum(amounts)
