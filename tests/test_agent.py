"""Tests for the agent's tools, loop and guardrail.

All of it runs against `StubModel` -- no network, no key, deterministic. That is
deliberate rather than a cost measure: a test that calls a live API is slow, rate
limited and flaky, so it stops being run. And with the plumbing proven here, any
failure that appears once a real model is plugged in is definitely the model.

The guardrail tests are the important ones. "Never do arithmetic" in a prompt is a
request; these are what make it enforcement.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from src.agent import (
    Answer,
    GuardrailResult,
    Reply,
    StubModel,
    ToolCall,
    ask,
    check_numbers,
)
from src.estimate import Estimator
from src.forecast import forecast
from src.intervals import RollingIntervals
from src.tools import SCHEMAS, bind, can_i_afford, explain_day, get_forecast, get_tightest_day
from src.world import EventStore, world_as_of

DATA = Path(__file__).resolve().parents[1] / "data"

ACCURACY = {
    "predictions_measured": 854,
    "typical_error_1_day": 0.0,
    "typical_error_7_days": 17155.02,
    "typical_error_14_days": 28685.17,
    "band_confidence": 0.80,
    "band_actually_contained_truth": 0.77,
}


@pytest.fixture(scope="module")
def fc():
    if not (DATA / "balance.csv").exists():
        pytest.skip("no generated data -- run `python -m src.generate`")
    store = EventStore.load(DATA)
    as_of = store.date_for_day(57)
    w = world_as_of(store, as_of)
    rolling = RollingIntervals(min_samples=1)
    for h in range(1, 15):
        rolling.observe(h, h * 1_000_00)
    return forecast(w, 14, estimator=Estimator.fit(w, horizon_days=14),
                    bands=rolling.band_fn())


@pytest.fixture
def tools(fc):
    return bind(fc, ACCURACY)


# --------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------


def test_tools_return_rupees_not_paise(fc):
    """A model shown 15338045 will write about fifteen million. It also forces the
    guardrail to reconcile two representations of the same figure."""
    out = get_forecast(fc, 7)
    assert out["projected_balance"] == round(fc.at(7).closing / 100, 2)
    assert abs(out["projected_balance"]) < 100_000_000


def test_a_horizon_beyond_the_forecast_is_refused_by_name(fc):
    with pytest.raises(ValueError, match="does not reach"):
        get_forecast(fc, 30)


def test_can_i_afford_does_the_comparison_in_python(fc):
    """The whole reason this is a tool: comparison is arithmetic."""
    balance = fc.at(5).closing / 100
    yes = can_i_afford(fc, balance - 1000, 5)
    no = can_i_afford(fc, balance + 1000, 5)
    assert yes["affordable"] is True and yes["margin"] > 0
    assert no["affordable"] is False and no["margin"] < 0


def test_can_i_afford_reports_the_pessimistic_case_separately(fc):
    """Affordable on the central estimate but not at the bottom of the band is a
    real answer, and a different one."""
    out = can_i_afford(fc, fc.at(7).closing / 100, 7)
    assert out["affordable"] is True
    assert out["affordable_at_band_low"] is False


def test_explain_day_returns_the_receipts(fc):
    out = explain_day(fc, 9)
    assert all(m["direction"] in ("in", "out") for m in out["largest_movements"])


def test_the_tightest_day_names_the_floor_it_is_measured_against(fc):
    out = get_tightest_day(fc)
    assert out["floor"] > 0 and out["floor_meaning"]
    assert isinstance(out["falls_below_floor"], bool)


def test_no_tool_hands_the_model_prose_to_interpret(fc):
    """Found on first contact with a real model. `get_tightest_day` used to return
    `reason()` -- the compact human summary "payment 3,317.81, +24 smaller" -- and
    the model described three incoming payments as outgoing and read the "+24
    smaller" tail (24 further movements) as a Rs.24 adjustment.

    Every number was real, so the guardrail passed it: it checks figures, not
    meaning. A tool must return facts the model cannot misread."""
    for out in (get_tightest_day(fc), explain_day(fc, 9)):
        assert "summary" not in out and "reason" not in out
        for m in out["largest_movements"]:
            assert m["direction"] in ("in", "out")
            assert isinstance(m["amount"], float)
        assert isinstance(out["movements_not_listed"], int)


def test_every_schema_has_a_bound_implementation(tools):
    assert {s["name"] for s in SCHEMAS} == set(tools)


# --------------------------------------------------------------------------
# The guardrail
# --------------------------------------------------------------------------


def test_a_number_from_a_tool_passes():
    out = [{"projected_balance": 153380.45}]
    assert check_numbers("You will have Rs.153,380.45 on Thursday.", out).ok


def test_an_invented_number_is_caught():
    """The failure this exists to stop: the model sees a balance and a bill and
    helpfully subtracts them."""
    out = [{"projected_balance": 153380.45, "amount_asked": 200000.0}]
    r = check_numbers("You would be short by Rs.46,619.55.", out)
    assert not r.ok
    assert 46619.55 in r.invented


def test_small_numbers_are_allowed_through():
    """"the next 14 days", "80% confident", "3 payments" are English, not claims
    about money. Rejecting them would fail fluent answers for nothing."""
    assert check_numbers("Over the next 14 days, with 80% confidence.", []).ok


def test_thousands_separators_are_understood():
    out = [{"balance": 153380.45}]
    assert check_numbers("Rs.1,53,380.45 is projected.", out).ok


def test_a_share_may_be_restated_as_a_percentage():
    """`share_already_certain` of 0.08 written as "8%" is a restatement, not an
    invention."""
    assert check_numbers("About 8% of that is certain.", [{"s": 0.08}]).ok


def test_rounding_is_tolerated():
    assert check_numbers("about Rs.153,380", [{"b": 153380.45}]).ok


def test_a_negative_margin_may_be_stated_as_a_shortfall():
    """Found by the first real model call. `can_i_afford` returns a margin of
    -46619.55 and the natural English is "short by Rs.46,620" -- the rounded
    absolute value. Rejecting that was a false positive: the magnitude still had to
    come from a tool, so nothing new gets through."""
    out = [{"margin": -46619.55}]
    assert check_numbers("You are short by Rs.46,620.", out).ok


def test_a_magnitude_no_tool_produced_is_still_caught():
    """The other side of it -- allowing absolute values must not open the door."""
    out = [{"margin": -46619.55}]
    assert not check_numbers("You are short by Rs.91,000.", out).ok


def test_numbers_nested_anywhere_in_a_tool_result_count():
    out = [{"movements": [{"amount": 180000.0}, {"amount": 35000.0}]}]
    assert check_numbers("Salaries of 180,000 and tax of 35,000.", out).ok


def test_the_rejection_message_names_the_offending_figures():
    r = check_numbers("You will have 999,999.00", [{"b": 1.0}])
    assert not r.ok
    assert "999,999" in r.message


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_a_direct_answer_needs_no_tools(tools):
    model = StubModel(script=[Reply(text="I cannot forecast beyond 14 days.")])
    a = ask("What about next year?", model, tools, SCHEMAS)
    assert a.tool_calls == ()
    assert "cannot" in a.shown


def test_a_tool_call_is_executed_and_fed_back(tools):
    model = StubModel(script=[
        Reply(tool_calls=(ToolCall("get_tightest_day", {}),)),
        Reply(text="Your tightest day is coming up."),
    ])
    a = ask("When is it tightest?", model, tools, SCHEMAS)
    assert [c.name for c in a.tool_calls] == ["get_tightest_day"]
    assert a.tool_outputs[0]["date"]
    # the tool result must have reached the model
    assert any(m.get("role") == "tool" for m in model.seen[-1])


def test_the_model_can_chain_tools(tools):
    """What makes this an agent rather than five lookups: the second call is chosen
    because of what the first returned."""
    model = StubModel(script=[
        Reply(tool_calls=(ToolCall("can_i_afford", {"amount_rupees": 900000.0,
                                                    "days_ahead": 7}),)),
        Reply(tool_calls=(ToolCall("explain_day", {"days_ahead": 7}),)),
        Reply(text="No -- salaries land that day."),
    ])
    a = ask("Can I pay 9 lakh on the 7th?", model, tools, SCHEMAS)
    assert [c.name for c in a.tool_calls] == ["can_i_afford", "explain_day"]


def test_a_bad_argument_comes_back_as_an_error_not_a_crash(tools):
    """Asking for day 30 of a 14-day forecast should produce a refusal, not a
    traceback -- the model needs the chance to correct itself."""
    model = StubModel(script=[
        Reply(tool_calls=(ToolCall("get_forecast", {"days_ahead": 30}),)),
        Reply(text="I cannot forecast that far ahead."),
    ])
    a = ask("Balance in a month?", model, tools, SCHEMAS)
    assert "error" in a.tool_outputs[0]
    assert "cannot" in a.shown


def test_an_unknown_tool_is_reported_rather_than_raised(tools):
    model = StubModel(script=[
        Reply(tool_calls=(ToolCall("get_profit", {}),)),
        Reply(text="I only forecast cash, not profit."),
    ])
    a = ask("What's my profit?", model, tools, SCHEMAS)
    assert "no such tool" in a.tool_outputs[0]["error"]


def test_a_runaway_loop_is_stopped(tools):
    model = StubModel(script=[
        Reply(tool_calls=(ToolCall("get_tightest_day", {}),)) for _ in range(20)
    ])
    a = ask("When is it tightest?", model, tools, SCHEMAS, max_steps=3)
    assert len(a.tool_calls) == 3
    assert "could not settle" in a.shown


def test_an_invented_number_is_withheld_from_the_merchant(tools):
    """End to end: the model does arithmetic, and the merchant never sees it."""
    model = StubModel(script=[
        Reply(tool_calls=(ToolCall("get_forecast", {"days_ahead": 3}),)),
        Reply(text="You will have Rs.7,77,777.00 on Thursday."),
    ])
    a = ask("Balance on Thursday?", model, tools, SCHEMAS)
    assert a.rejected
    assert "7,77,777" not in a.shown
    assert "rejected" in a.shown.lower()


def test_a_clean_answer_is_shown_unchanged(tools):
    model = StubModel(script=[
        Reply(tool_calls=(ToolCall("get_tightest_day", {}),)),
        Reply(text="Your tightest day is coming, and you stay above the floor."),
    ])
    a = ask("When is it tightest?", model, tools, SCHEMAS)
    assert not a.rejected
    assert a.shown == a.text
