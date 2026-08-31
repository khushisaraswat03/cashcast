"""What the agent is allowed to look at.

Five tools over the finished forecaster. Between them they answer the questions a
merchant actually asks, and each one returns **numbers computed in Python** with the
text left to the model.

The division is the whole architectural claim of this project. The model decides
*what to look at* and turns the result into a sentence; it never adds, compares or
estimates anything. A language model averaging four numbers produces a plausible
wrong answer, and a cash forecast whose arithmetic does not tie is worse than no
forecast because somebody acts on it.

`can_i_afford` exists for exactly that reason. "Is Rs.2,00,000 more than the balance
on Thursday" is a comparison, and a comparison is arithmetic -- so it happens here,
not in the model's head.

Every tool returns a flat dict of primitives. Two consequences: it serialises
straight into a tool-call response, and the guardrail in `agent.py` can walk it to
collect every number the model was legitimately given.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from .forecast import Flag, Forecast
from .money import Paise, fmt_inr


def _rupees(paise: Paise | None) -> float | None:
    """Tools speak rupees. Internals stay in integer paise; a model shown 15338045
    will write about fifteen million, and the guardrail then has to reconcile two
    representations of the same figure."""
    return None if paise is None else round(paise / 100, 2)


def _day(f: Forecast, days_ahead: int):
    if not 1 <= days_ahead <= len(f.days):
        raise ValueError(
            f"days_ahead must be between 1 and {len(f.days)}; the forecast does not "
            f"reach {days_ahead} days out"
        )
    return f.at(days_ahead)


# --------------------------------------------------------------------------
# The five
# --------------------------------------------------------------------------


def get_forecast(f: Forecast, days_ahead: int) -> dict[str, Any]:
    """Projected balance for one day, with its uncertainty and its provenance."""
    d = _day(f, days_ahead)
    return {
        "date": d.date.isoformat(),
        "days_ahead": d.horizon,
        "projected_balance": _rupees(d.closing),
        "band_low": _rupees(d.band_low),
        "band_high": _rupees(d.band_high),
        "band_confidence": None if d.band_low is None else 0.80,
        "share_already_certain": round(d.certain_share, 3),
        # Given only the certain share, a model asked "how much of this is
        # guesswork" has to compute 1 - x -- and correctly refuses, because it is
        # forbidden from doing arithmetic. A tool that forces the model to choose
        # between breaking a rule and declining an answerable question is the tool's
        # fault. If a question needs the complement, return the complement.
        "share_estimated": round(1.0 - d.certain_share, 3),
        "money_in": _rupees(d.certain_in + d.estimated_in),
        "money_out": _rupees(d.certain_out + d.estimated_out),
        "money_in_already_certain": _rupees(d.certain_in),
        "money_in_estimated": _rupees(d.estimated_in),
    }


def _movements(d, limit: int = 5) -> list[dict[str, Any]]:
    """The day's receipts, as structure rather than prose.

    `DayProjection.reason()` renders a compact human summary -- "payment 3,317.81,
    +24 smaller" -- which is right for the merchant report and wrong as a tool
    result. Handed that string, a real model described three *incoming* payments as
    outgoing and read the "+24 smaller" tail (24 further movements) as a Rs.24
    adjustment. Both numbers were real, so the guardrail passed it: it checks
    figures, not meaning.

    A tool should return facts the model cannot misread. Prose is the model's job,
    not its input.
    """
    return [
        {
            "kind": getattr(e, "kind", None).value
            if getattr(e, "kind", None) is not None
            else type(e).__name__.lower(),
            "amount": _rupees(abs(e.cash_delta)),
            "direction": "in" if e.cash_delta > 0 else "out",
        }
        for e in d.largest_movements(limit)
    ]


def get_tightest_day(f: Forecast) -> dict[str, Any]:
    """The lowest point in the window -- the single most useful day to name."""
    d = f.trough()
    movements = _movements(d)
    return {
        "date": d.date.isoformat(),
        "days_ahead": d.horizon,
        "projected_balance": _rupees(d.closing),
        "band_low": _rupees(d.band_low),
        "band_high": _rupees(d.band_high),
        "largest_movements": movements,
        "movements_not_listed": max(0, len(d.movements) - len(movements)),
        "floor": _rupees(f.floor),
        "floor_meaning": "the largest single commitment due in this window",
        "falls_below_floor": bool(d.closing < f.floor),
    }


def explain_day(f: Forecast, days_ahead: int) -> dict[str, Any]:
    """What actually moved the money on one day.

    Reads the receipts each `DayProjection` carries, which is why "why is the 22nd
    low" is a lookup rather than a second search through the event store.
    """
    d = _day(f, days_ahead)
    movements = _movements(d)
    return {
        "date": d.date.isoformat(),
        "days_ahead": d.horizon,
        "projected_balance": _rupees(d.closing),
        "largest_movements": movements,
        "movements_not_listed": max(0, len(d.movements) - len(movements)),
        "share_already_certain": round(d.certain_share, 3),
        "flags": sorted(x.value for x in d.flags),
    }


def can_i_afford(f: Forecast, amount_rupees: float, days_ahead: int) -> dict[str, Any]:
    """Can the merchant pay `amount_rupees` on that day?

    A comparison, done here rather than by the model. The margin is returned so the
    answer can be specific without the model subtracting anything.
    """
    d = _day(f, days_ahead)
    amount = round(amount_rupees * 100)
    return {
        "date": d.date.isoformat(),
        "days_ahead": d.horizon,
        "amount_asked": _rupees(amount),
        "projected_balance": _rupees(d.closing),
        "affordable": bool(d.closing >= amount),
        "margin": _rupees(d.closing - amount),
        "affordable_at_band_low": (
            None if d.band_low is None else bool(d.band_low >= amount)
        ),
        "band_low": _rupees(d.band_low),
        "share_already_certain": round(d.certain_share, 3),
    }


def get_accuracy(accuracy: dict[str, Any]) -> dict[str, Any]:
    """How wrong this forecaster has been, measured rather than claimed.

    Handed a pre-computed summary rather than running an 854-prediction backtest per
    question. It is the same object the accuracy report prints from, so the agent
    cannot quote a number the report disagrees with.
    """
    return accuracy


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

#: The schemas the model sees. Descriptions are written for the model, not for us --
#: they are the only thing telling it when each tool applies.
SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_forecast",
        "description": (
            "Projected bank balance for one day in the next 14. Returns the "
            "figure, its 80% uncertainty band, how much money moves in and out, "
            "and the split between what is already certain and what is still "
            "estimated. Use for 'what will my balance be on X', and also for 'how "
            "much of that is guesswork', 'how much is estimated', 'how confident "
            "are you about day X' -- share_estimated answers all of those directly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "1 to 14, counting from today",
                }
            },
            "required": ["days_ahead"],
        },
    },
    {
        "name": "get_tightest_day",
        "description": (
            "The lowest projected balance in the next 14 days, why it is low, and "
            "whether it falls below what the merchant owes. Use for 'when is it "
            "tightest', 'when should I worry', 'will I run short'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "explain_day",
        "description": (
            "What moved the money on a given day: the largest payments in and out, "
            "and whether they were known or estimated. Use for 'why is X low', "
            "'what happens on X'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "1 to 14"}
            },
            "required": ["days_ahead"],
        },
    },
    {
        "name": "can_i_afford",
        "description": (
            "Whether a specific amount can be paid on a specific day, with the "
            "margin either way. Always use this rather than comparing numbers "
            "yourself. Use for 'can I pay X on Y', 'can I afford X'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "amount_rupees": {"type": "number"},
                "days_ahead": {"type": "integer", "description": "1 to 14"},
            },
            "required": ["amount_rupees", "days_ahead"],
        },
    },
    {
        "name": "get_accuracy",
        "description": (
            "How accurate this forecaster has been, measured over 854 past "
            "forecasts: typical error by horizon, and how often the truth landed "
            "inside its 80% band. Use for 'how much should I trust this', 'how "
            "accurate are you'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


def bind(f: Forecast, accuracy: dict[str, Any]) -> dict[str, Callable[..., dict]]:
    """Attach the tools to one forecast, so the model passes only its own arguments."""
    return {
        "get_forecast": lambda days_ahead: get_forecast(f, days_ahead),
        "get_tightest_day": lambda: get_tightest_day(f),
        "explain_day": lambda days_ahead: explain_day(f, days_ahead),
        "can_i_afford": lambda amount_rupees, days_ahead: can_i_afford(
            f, amount_rupees, days_ahead
        ),
        "get_accuracy": lambda: get_accuracy(accuracy),
    }
