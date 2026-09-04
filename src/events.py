"""The things that happen in a merchant's world.

Six event types, each answering two separate questions:

* **`known_at`** -- when could the merchant first have known this existed?
* **`cash_at`** -- when does the money actually move?

That split is the foundation of the forecast. A chargeback raised on the 12th and
debited on the 15th is knowable from the 12th, so on the 13th it belongs in the
certain layer even though no money has moved. Collapse the dates and a known future
outflow becomes an unpredictable surprise.

Only four types move cash; orders and promotions change what you expect, not what you
have. So a day's balance is the sum of `cash_delta` over events landing that day --
no per-type branching anywhere in the forecaster.

There is deliberately no `Settlement` class. A settlement is a grouping of payments
sharing an arrival date; storing it separately would put the same money in two places
and invite double-counting.

Known simplification: a refund larger than the payout it lands on would carry forward
to the next one. At this volume that never binds, so it is not modelled.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any, ClassVar, Mapping

from pydantic import BaseModel, ConfigDict, model_validator

from .money import Paise, fmt, rupees_to_paise

LIST_SEP = "|"


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class Method(str, Enum):
    """How the customer paid. Determines the settlement cycle, and nothing else.

    Two methods rather than four because cards and UPI settle on different cycles
    (T+2 and T+1) while netbanking and wallets share the card cycle -- they would
    add rows to the data without adding behaviour to the forecast.
    """

    CARD = "card"
    UPI = "upi"


class PaymentStatus(str, Enum):
    CAPTURED = "captured"
    FAILED = "failed"


class OutflowKind(str, Enum):
    """What the money was for. Used to explain a squeeze, not to compute one."""

    RENT = "rent"
    SALARY = "salary"
    SUPPLIER = "supplier"
    ADS = "ads"
    TAX = "tax"
    OTHER = "other"


# --------------------------------------------------------------------------
# CSV boundary
# --------------------------------------------------------------------------


class CsvModel(BaseModel):
    """A record that round-trips through a CSV file.

    The generator writes these files and the forecaster reads them back, which is
    what makes the leak test possible: delete every row after the vantage day and
    re-run. Money columns are written as plain rupee strings with two decimals and
    parsed back to exact paise.
    """

    model_config = ConfigDict(extra="forbid")

    MONEY_FIELDS: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def csv_fields(cls) -> list[str]:
        return list(cls.model_fields)

    def to_csv_row(self) -> dict[str, str]:
        row: dict[str, str] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if name in self.MONEY_FIELDS:
                row[name] = "" if value is None else fmt(value)
            elif value is None:
                row[name] = ""
            elif isinstance(value, Enum):
                row[name] = str(value.value)
            elif isinstance(value, bool):
                row[name] = "1" if value else "0"
            elif isinstance(value, dt.datetime):
                row[name] = value.isoformat(sep=" ", timespec="seconds")
            elif isinstance(value, dt.date):
                row[name] = value.isoformat()
            else:
                row[name] = str(value)
        return row

    @classmethod
    def from_csv_row(cls, row: Mapping[str, Any]):
        """Parse a CSV row back into a model.

        A blank cell means absent, so the key is omitted rather than passed as
        `None`. That way a field's default applies, and a blank in a genuinely
        required column fails as a missing field -- which names the real problem
        instead of a confusing type error.

        Unknown columns are rejected explicitly. `extra="forbid"` cannot catch them
        on its own, because this method only ever *reads* the fields it knows about
        -- so a misspelt optional column would be silently dropped and its default
        used instead. Which is precisely the reader/writer drift this class exists
        to prevent.
        """
        unknown = {
            key
            for key in row
            if isinstance(key, str) and key.strip() and key not in cls.model_fields
        }
        if unknown:
            raise ValueError(
                f"{cls.__name__}: unknown column(s) {sorted(unknown)}; "
                f"expected {cls.csv_fields()}"
            )

        kwargs: dict[str, Any] = {}
        for name in cls.model_fields:
            raw = row.get(name)
            text = "" if raw is None else str(raw).strip()
            if not text:
                continue
            kwargs[name] = rupees_to_paise(text) if name in cls.MONEY_FIELDS else text
        return cls(**kwargs)


# --------------------------------------------------------------------------
# The event protocol
# --------------------------------------------------------------------------


class Event(CsvModel):
    """Base for everything that happens.

    Subclasses keep their own domain-appropriate field names -- `captured_at`,
    `requested_at`, `due_on` -- because those read correctly at the point of use.
    The three properties below give the forecaster a uniform view over all of them,
    so it never needs to know which type it is holding.
    """

    @property
    def event_id(self) -> str:
        raise NotImplementedError

    @property
    def known_at(self) -> dt.date:
        """The first day the merchant could have known this existed.

        The temporal wall filters on exactly this. Anything with a `known_at` later
        than the vantage day is invisible to the forecaster.
        """
        raise NotImplementedError

    @property
    def cash_at(self) -> dt.date | None:
        """The day the money moves, or None if this event moves no money."""
        return None

    @property
    def cash_delta(self) -> Paise:
        """Signed effect on the bank balance. Positive in, negative out."""
        return 0

    @property
    def moves_cash(self) -> bool:
        return self.cash_at is not None and self.cash_delta != 0


# --------------------------------------------------------------------------
# World 1 -- the merchant's own records
# --------------------------------------------------------------------------


class Order(Event):
    """A sale. Moves no money by itself.

    Kept separate from `Payment` because the two genuinely differ: an order can
    have a failed attempt followed by a successful retry, or no successful payment
    at all. And a refund is a decision about an *order* -- the customer sent goods
    back -- even though the cash comes out of a payment.
    """

    order_id: str
    placed_at: dt.datetime
    amount: Paise
    method: Method
    customer_id: str

    MONEY_FIELDS = ("amount",)

    @property
    def event_id(self) -> str:
        return self.order_id

    @property
    def known_at(self) -> dt.date:
        return self.placed_at.date()


class Payment(Event):
    """Money collected against an order.

    `fee`, `gst` and `net` are stored rather than recomputed because the gateway is
    the authority on them -- and because storing them makes the CSV self-describing
    when you open it to check something by hand. The validator below keeps them
    honest.

    A failed payment has no settlement date and moves no cash, ever. It is kept
    because it explains why an order produced less revenue than its value.
    """

    payment_id: str
    order_id: str
    amount: Paise
    method: Method
    status: PaymentStatus
    captured_at: dt.datetime
    settles_on: dt.date | None = None
    fee: Paise = 0
    gst: Paise = 0
    net: Paise = 0

    MONEY_FIELDS = ("amount", "fee", "gst", "net")

    @model_validator(mode="after")
    def _check_components(self) -> "Payment":
        if self.status is PaymentStatus.FAILED:
            if self.settles_on is not None:
                raise ValueError("a failed payment cannot have a settlement date")
            if (self.fee, self.gst, self.net) != (0, 0, 0):
                raise ValueError("a failed payment has no fee, gst or net")
            return self
        if self.settles_on is None:
            raise ValueError("a captured payment must have a settlement date")
        if self.fee + self.gst + self.net != self.amount:
            raise ValueError(
                f"{self.payment_id}: fee + gst + net = "
                f"{self.fee + self.gst + self.net}, expected {self.amount}"
            )
        return self

    @property
    def event_id(self) -> str:
        return self.payment_id

    @property
    def known_at(self) -> dt.date:
        return self.captured_at.date()

    @property
    def cash_at(self) -> dt.date | None:
        return self.settles_on

    @property
    def cash_delta(self) -> Paise:
        return self.net


class Refund(Event):
    """Money returned to a customer.

    Two ids on purpose. It references the `order_id` because that is the business
    fact -- goods came back -- and the `payment_id` because that is where the money
    has to come from.

    The gap between `requested_at` and `nets_off_on` is what makes refunds
    forecastable at all: a refund requested today reduces a payout several days
    from now, so it sits in the certain layer for that entire gap.
    """

    refund_id: str
    order_id: str
    payment_id: str
    amount: Paise
    requested_at: dt.datetime
    nets_off_on: dt.date

    MONEY_FIELDS = ("amount",)

    @property
    def event_id(self) -> str:
        return self.refund_id

    @property
    def known_at(self) -> dt.date:
        return self.requested_at.date()

    @property
    def cash_at(self) -> dt.date | None:
        return self.nets_off_on

    @property
    def cash_delta(self) -> Paise:
        return -self.amount


# --------------------------------------------------------------------------
# World 2 -- the gateway
# --------------------------------------------------------------------------


class Chargeback(Event):
    """A disputed payment, clawed back by the customer's bank.

    `original_captured_on` may fall outside the generated window entirely -- that is
    the realistic case, and the one that makes chargebacks feel unexplainable: money
    leaves this month against a sale from two months ago.

    Rare enough that no dataset of this size contains enough of them to estimate a
    rate from. That is not a gap in the data; it is why chargebacks belong in the
    honest layer rather than the estimated one.
    """

    chargeback_id: str
    payment_id: str
    amount: Paise
    raised_at: dt.datetime
    debited_on: dt.date
    original_captured_on: dt.date

    MONEY_FIELDS = ("amount",)

    @property
    def event_id(self) -> str:
        return self.chargeback_id

    @property
    def known_at(self) -> dt.date:
        return self.raised_at.date()

    @property
    def cash_at(self) -> dt.date | None:
        return self.debited_on

    @property
    def cash_delta(self) -> Paise:
        return -self.amount


# --------------------------------------------------------------------------
# Money going out, and things that change expectations
# --------------------------------------------------------------------------


class Outflow(Event):
    """A payment the business makes: rent, salaries, suppliers, ad spend, tax.

    Almost entirely certain, and almost entirely ignored by people forecasting cash
    from revenue alone. A supplier payment committed three weeks in advance is the
    most predictable thing in the dataset, and it is what turns a comfortable
    balance into a squeeze.
    """

    outflow_id: str
    kind: OutflowKind
    amount: Paise
    committed_at: dt.datetime
    due_on: dt.date
    description: str = ""

    MONEY_FIELDS = ("amount",)

    @property
    def event_id(self) -> str:
        return self.outflow_id

    @property
    def known_at(self) -> dt.date:
        return self.committed_at.date()

    @property
    def cash_at(self) -> dt.date | None:
        return self.due_on

    @property
    def cash_delta(self) -> Paise:
        return -self.amount


class Promotion(Event):
    """A planned sale. Moves no money; changes what sales to expect.

    `declared_at` is the most load-bearing field in this file. It is the entire
    hidden-versus-declared experiment: set it before the vantage day and the
    forecaster knows a sale is coming, set it after and the sale arrives as a
    surprise. Same data, one value changed, and the difference between the two
    backtests is a measurement of what knowing is worth.

    **Two numbers, both with units in their names.** A merchant planning a sale knows
    how much more they expect to sell *and* how much they are taking off the price,
    because they chose the discount themselves. Both are needed, because a cash
    forecast wants revenue and neither one gives it alone:

        revenue uplift = expected_volume_uplift x (1 - expected_discount)

    The field was previously a single unitless `expected_uplift`. The generator set
    it from the volume knob and the estimator read it as revenue, so declaring a
    3x sale at 30% off over-stated income by 43% -- and because balances accumulate,
    that error contaminated every prediction after the promotion rather than just
    the promotion itself. Nothing failed; the number was simply the wrong quantity.
    Hence the units in the names.
    """

    promotion_id: str
    name: str
    declared_at: dt.datetime
    starts_on: dt.date
    ends_on: dt.date
    #: How many more orders the merchant expects. NOT a revenue multiplier.
    expected_volume_uplift: float
    #: Fraction taken off the price during the sale. 0.30 means 30% off.
    expected_discount: float = 0.0

    @property
    def expected_revenue_uplift(self) -> float:
        """What a cash forecast actually needs. Derived, so it cannot drift."""
        return self.expected_volume_uplift * (1.0 - self.expected_discount)

    @property
    def event_id(self) -> str:
        return self.promotion_id

    @property
    def known_at(self) -> dt.date:
        return self.declared_at.date()

    def covers(self, day: dt.date) -> bool:
        return self.starts_on <= day <= self.ends_on


# --------------------------------------------------------------------------
# Convenience
# --------------------------------------------------------------------------

#: Every event type, in the order the generator produces them.
AnyEvent = Order | Payment | Refund | Chargeback | Outflow | Promotion

#: Filename each type is written to and read back from.
EVENT_FILES: dict[type[Event], str] = {
    Order: "orders.csv",
    Payment: "payments.csv",
    Refund: "refunds.csv",
    Chargeback: "chargebacks.csv",
    Outflow: "outflows.csv",
    Promotion: "promotions.csv",
}
