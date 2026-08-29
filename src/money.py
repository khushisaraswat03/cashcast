"""Money handling.

One rule, enforced everywhere: **money is an integer number of paise.**

Floats are banned for monetary values. `0.1 + 0.2 != 0.3` is a curiosity in most
programs and a defect here, because the whole product is a claim about a number:
that a projected balance can be trusted. `Decimal` appears only at the boundary
(parsing a CSV, formatting for output); every internal amount, sum and comparison
is `int` paise.

Rounding is **ROUND_HALF_UP**, not Python's default banker's rounding. Financial
convention rounds 0.5 away from zero, and `round()` does not:

    >>> round(0.5), round(1.5), round(2.5)
    (0, 2, 2)

That is a defensible choice for statistics and the wrong one for an invoice.
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


def fmt_inr(paise: Paise) -> str:
    """Format paise for humans, with thousands separators. `341740` -> `3,417.40`."""
    return f"{paise_to_rupees(paise):,.2f}"


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
