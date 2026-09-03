"""Tests for the Bucket 1 walk.

Three things are being checked. That the arithmetic chains -- each day's opening is
the previous day's closing, and a day's closing is its opening plus a plain sum, so
no sign can be flipped silently. That the flags fire on a *crossing* rather than on
every day beyond a threshold, which is the difference between a two-line report and
an unreadable one. And that the empty Bucket 2 and Bucket 3 slots behave sensibly
while they are still empty.

The two flag tests marked BUG were written after the first run produced a breach
warning on a day the balance rose.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from src.events import Method, Outflow, OutflowKind, Payment, PaymentStatus, Refund
from src.forecast import (
    DayProjection,
    Flag,
    Scenario,
    _flags_for,
    derive_floor,
    forecast,
    render,
)
from src.world import BankBalance, EventStore, world_as_of

DATA = Path(__file__).resolve().parents[1] / "data"

DAY0 = dt.date(2026, 6, 11)  # the vantage day


def at(day: dt.date, hour: int = 10) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, 0))


def plus(n: int) -> dt.date:
    return DAY0 + dt.timedelta(days=n)


def a_payment(pid: str, settles: dt.date, amount: int = 10_000_00) -> Payment:
    fee, gst = 200_00, 36_00
    return Payment(
        payment_id=pid, order_id="o", amount=amount, method=Method.CARD,
        status=PaymentStatus.CAPTURED, captured_at=at(DAY0), settles_on=settles,
        fee=fee, gst=gst, net=amount - fee - gst,
    )


def an_outflow(oid: str, due: dt.date, amount: int, kind=OutflowKind.SUPPLIER):
    return Outflow(outflow_id=oid, kind=kind, amount=amount,
                   committed_at=at(DAY0 - dt.timedelta(days=20)), due_on=due)


def store_with(*, payments=(), refunds=(), outflows=(), opening=5_000_00):
    return EventStore(
        orders=(), payments=tuple(payments), refunds=tuple(refunds),
        chargebacks=(), outflows=tuple(outflows), promotions=(),
        balances=(BankBalance(day=1, date=DAY0, opening=opening, inflow=0,
                              outflow=0, closing=opening),),
    )


def a_world(**kw):
    return world_as_of(store_with(**kw), DAY0)


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------


def test_the_path_starts_at_todays_balance():
    f = forecast(a_world(opening=2_42_382_78), horizon=3)
    assert f.opening == 2_42_382_78
    assert f.at(1).opening == 2_42_382_78


def test_each_day_opens_where_the_last_one_closed():
    f = forecast(
        a_world(payments=[a_payment("p1", plus(1)), a_payment("p2", plus(3))]),
        horizon=5,
    )
    for previous, day in zip(f.days, f.days[1:]):
        assert day.opening == previous.closing


def test_closing_is_a_plain_sum_never_a_subtraction():
    """Outflows are stored negative, so no sign can be forgotten at a call site."""
    f = forecast(
        a_world(payments=[a_payment("p1", plus(1))],
                outflows=[an_outflow("o1", plus(1), 3_000_00)]),
        horizon=2,
    )
    d = f.at(1)
    assert d.certain_in > 0 and d.certain_out < 0
    assert d.closing == d.opening + d.certain_in + d.certain_out


def test_a_day_with_nothing_known_carries_the_balance():
    f = forecast(a_world(payments=[a_payment("p1", plus(1))]), horizon=3)
    assert f.at(2).closing == f.at(1).closing
    assert f.at(2).movements == ()


def test_horizon_length_and_dates():
    f = forecast(a_world(), horizon=14)
    assert len(f.days) == 14
    assert f.days[0].date == plus(1) and f.days[-1].date == plus(14)


def test_events_beyond_the_horizon_are_not_counted():
    f = forecast(a_world(outflows=[an_outflow("o1", plus(9), 1_000_00)]), horizon=5)
    assert all(d.certain_out == 0 for d in f.days)


def test_a_zero_horizon_is_refused():
    with pytest.raises(ValueError, match="horizon"):
        forecast(a_world(), horizon=0)


def test_lookups_and_their_errors():
    f = forecast(a_world(), horizon=3)
    assert f.at(2).date == plus(2)
    assert f.on(plus(2)).horizon == 2
    with pytest.raises(ValueError):
        f.at(99)
    with pytest.raises(ValueError):
        f.on(plus(99))


def test_closing_series_is_what_the_backtest_will_score():
    f = forecast(a_world(payments=[a_payment("p1", plus(2))]), horizon=3)
    assert f.closing_series() == {d.date: d.closing for d in f.days}


# --------------------------------------------------------------------------
# The receipts
# --------------------------------------------------------------------------


def test_a_day_keeps_the_events_that_moved_it():
    f = forecast(
        a_world(payments=[a_payment("p1", plus(1)), a_payment("p2", plus(1))]),
        horizon=2,
    )
    assert {p.payment_id for p in f.at(1).movements} == {"p1", "p2"}


def test_the_reason_names_the_bill_not_the_noise():
    f = forecast(
        a_world(
            payments=[a_payment(f"p{i}", plus(1), amount=1_000_00) for i in range(9)],
            outflows=[an_outflow("o1", plus(1), 5_00_000_00)],
        ),
        horizon=2,
    )
    reason = f.at(1).reason()
    assert reason.startswith("supplier")
    assert "smaller" in reason


def test_the_reason_is_capped_at_three_named_movements():
    """Twenty similar settlements: the 80% rule alone would name almost all of
    them, which is the unreadable report this cap exists to prevent."""
    f = forecast(
        a_world(payments=[a_payment(f"p{i}", plus(1), amount=1_000_00)
                          for i in range(20)]),
        horizon=2,
    )
    assert f.at(1).reason().count(",") <= 3
    assert "smaller" in f.at(1).reason()


def test_an_empty_day_says_so_plainly():
    f = forecast(a_world(), horizon=2)
    assert "carried" in f.at(1).reason()


# --------------------------------------------------------------------------
# The floor
# --------------------------------------------------------------------------


def test_the_floor_is_the_largest_commitment_in_the_window():
    w = a_world(outflows=[an_outflow("o1", plus(2), 45_000_00),
                          an_outflow("o2", plus(5), 1_80_000_00),
                          an_outflow("o3", plus(30), 9_00_000_00)])
    assert derive_floor(w, plus(14)) == 1_80_000_00


def test_no_commitments_means_no_floor():
    assert derive_floor(a_world(), plus(14)) == 0


# --------------------------------------------------------------------------
# Flags -- the logic that had two bugs on first run
# --------------------------------------------------------------------------


def test_exactly_one_trough_and_it_is_the_lowest_day():
    f = forecast(
        a_world(outflows=[an_outflow("o1", plus(3), 4_000_00),
                          an_outflow("o2", plus(7), 1_000_00)]),
        horizon=10,
    )
    troughs = [d for d in f.days if Flag.TROUGH in d.flags]
    assert len(troughs) == 1
    assert troughs[0].closing == min(d.closing for d in f.days)


def test_breach_fires_once_on_the_crossing_not_every_day_after():
    f = forecast(
        a_world(opening=5_00_000_00,
                outflows=[an_outflow("o1", plus(2), 4_00_000_00),
                          an_outflow("o2", plus(6), 2_00_000_00)]),
        horizon=10,
    )
    breaches = [d.horizon for d in f.days if Flag.BREACH in d.flags]
    assert breaches == [2]


def test_BUG_no_breach_when_already_below_the_floor_and_rising():
    """Day 57 reported a breach on a day the balance went *up*, because it started
    below the floor and day one was treated as having no predecessor. A standing
    position is not an event."""
    f = forecast(
        a_world(opening=78_336_43,
                payments=[a_payment("p1", plus(1), amount=40_000_00)],
                outflows=[an_outflow("o1", plus(9), 1_80_000_00)]),
        horizon=10,
    )
    assert f.at(1).closing > f.opening
    assert Flag.BREACH not in f.at(1).flags


def test_BUG_the_report_notes_a_standing_position_instead(capsys):
    f = forecast(a_world(opening=10_00, outflows=[an_outflow("o1", plus(2), 5_00_000_00)]),
                 horizon=5)
    assert "already below the floor" in render(f)


def test_breaches_floor_reports_the_path_not_the_endpoint():
    """The whole point of A2: the endpoint can be fine and the path can not be."""
    f = forecast(
        a_world(opening=5_00_000_00,
                payments=[a_payment("p1", plus(6), amount=5_00_000_00)],
                outflows=[an_outflow("o1", plus(2), 4_50_000_00)]),
        horizon=10,
    )
    assert f.days[-1].closing > f.floor      # the endpoint looks fine
    assert f.breaches_floor()                # the path was not


# -- the at-risk flag, which cannot fire until Bucket 3 exists --------------


def test_at_risk_never_fires_without_bands():
    f = forecast(a_world(outflows=[an_outflow("o1", plus(2), 1_00_000_00)]),
                 horizon=10)
    assert all(Flag.AT_RISK not in d.flags for d in f.days)
    assert all(d.band_low is None for d in f.days)


def test_at_risk_fires_when_the_band_dips_but_the_estimate_does_not():
    """The warning no point forecast can produce -- Bucket 3's reason for existing."""
    day = DayProjection(date=plus(1), horizon=1, opening=2_00_000_00,
                        certain_in=0, certain_out=0, band_low=40_000_00,
                        band_high=3_00_000_00)
    flags = _flags_for(day, previous_closing=2_00_000_00, previous_at_risk=False,
                       is_trough=False, floor=1_00_000_00)
    assert flags == frozenset({Flag.AT_RISK})


def test_at_risk_does_not_repeat_while_the_band_stays_low():
    day = DayProjection(date=plus(2), horizon=2, opening=2_00_000_00,
                        band_low=40_000_00, band_high=3_00_000_00)
    flags = _flags_for(day, previous_closing=2_00_000_00, previous_at_risk=True,
                       is_trough=False, floor=1_00_000_00)
    assert flags == frozenset()


def test_a_real_breach_outranks_an_at_risk_warning():
    """If the central estimate itself is below the floor, that is not a risk."""
    day = DayProjection(date=plus(1), horizon=1, opening=2_00_000_00,
                        certain_out=-1_50_000_00, band_low=10_000_00)
    flags = _flags_for(day, previous_closing=2_00_000_00, previous_at_risk=False,
                       is_trough=False, floor=1_00_000_00)
    assert flags == frozenset({Flag.BREACH})


# --------------------------------------------------------------------------
# The empty Bucket 2 slots
# --------------------------------------------------------------------------


def test_bucket_one_only_is_one_hundred_percent_certain():
    f = forecast(a_world(payments=[a_payment("p1", plus(1))]), horizon=3)
    assert f.at(1).certain_share == 1.0
    assert f.scenario is Scenario.SALES_STOP


def test_certain_share_falls_once_estimates_arrive():
    """Sunday's job, checked today so the field is known to work when it is used."""
    d = DayProjection(date=plus(1), horizon=1, opening=0,
                      certain_in=75_00, estimated_in=25_00)
    assert d.certain_share == 0.75


def test_a_still_day_is_trivially_certain():
    assert DayProjection(date=plus(1), horizon=1, opening=0).certain_share == 1.0


# --------------------------------------------------------------------------
# Against the generated dataset
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_store() -> EventStore:
    if not (DATA / "balance.csv").exists():
        pytest.skip("no generated data -- run `python -m src.generate`")
    return EventStore.load(DATA)


def test_it_runs_from_every_vantage_point(real_store):
    for n in range(46, 107):
        w = world_as_of(real_store, real_store.date_for_day(n))
        f = forecast(w, 14)
        assert len(f.days) == 14
        assert f.at(1).opening == w.opening_balance


def test_horizon_1_is_exact_at_every_vantage_point(real_store):
    """The strongest invariant in the project, and the cheapest bug detector.

    Everything that lands tomorrow was captured today or earlier, so it is all
    inside the wall already. The certain layer therefore has *nothing left to guess*
    at horizon 1 and must reproduce tomorrow's actual balance to the paisa, at all
    61 vantage points. Any error at all means the walk itself is wrong -- a missed
    event type, a settlement date off by one, a dropped sign -- rather than the
    forecast merely being incomplete.
    """
    actual = {b.date: b.closing for b in real_store.balances}
    for n in range(46, 107):
        d = forecast(world_as_of(real_store, real_store.date_for_day(n)), 14).at(1)
        assert d.closing == actual[d.date], f"day {n}: off by {d.closing - actual[d.date]}"


def test_horizon_2_is_NOT_exact_and_errs_in_both_directions(real_store):
    """A correction to the design note that called horizons 1 and 2 deterministic.

    That was written assuming cards at T+2. UPI settles at T+1, so a UPI payment
    captured *tomorrow* lands the day after -- and tomorrow's sales do not exist
    yet. One day of UPI leaks into horizon 2.

    The second half was my own wrong assumption, caught by this test: I asserted the
    certain layer "can only under-count". It cannot only under-count, because the
    same one-day blind spot hides *outflows* as well -- a refund requested tomorrow
    nets off the day after. On 10 of 61 vantage points the unseen refunds outweigh
    the unseen UPI and the projection comes out too high.
    """
    actual = {b.date: b.closing for b in real_store.balances}
    errors = {}
    for n in range(46, 107):
        d = forecast(world_as_of(real_store, real_store.date_for_day(n)), 14).at(2)
        errors[n] = d.closing - actual[d.date]

    assert any(e != 0 for e in errors.values()), "UPI T+1 is not being modelled"
    assert any(e < 0 for e in errors.values()), "no day misses an unseen sale"
    assert any(e > 0 for e in errors.values()), "no day misses an unseen refund"


def test_over_counting_at_horizon_2_is_always_unseen_refunds(real_store):
    """The causal claim behind the test above, asserted rather than assumed.

    Whenever horizon 2 comes out too high, it must be because the refunds the
    forecaster could not yet see outweighed the sales it could not yet see. If that
    ever stops being true, the over-count has some other cause and the explanation
    in the write-up is wrong.
    """
    actual = {b.date: b.closing for b in real_store.balances}
    checked = 0
    for n in range(46, 107):
        as_of = real_store.date_for_day(n)
        w = world_as_of(real_store, as_of)
        d = forecast(w, 14).at(2)
        if d.closing <= actual[d.date]:
            continue
        checked += 1
        seen = {e.event_id for e in w.certain_movements(d.date)}
        unseen = [
            e for e in real_store.events
            if e.cash_at == d.date and e.event_id not in seen and e.cash_delta
        ]
        assert sum(e.cash_delta for e in unseen) < 0, f"day {n}: over-count not refunds"
    assert checked > 0, "no over-counting days found -- the premise has changed"


def test_the_certain_layer_is_pessimistic_and_that_is_expected(real_store):
    """Not a defect: two days of inflow against fourteen of outflow. Asserted so
    that if Bucket 2 ever lands and this stays true, the test names why."""
    actual = {b.date: b.closing for b in real_store.balances}
    w = world_as_of(real_store, real_store.date_for_day(46))
    end = forecast(w, 14).days[-1]
    assert end.closing < actual[end.date]


def test_the_certain_layer_runs_out_and_then_flatlines(real_store):
    """Past roughly three weeks nothing known remains, so the projection stops
    moving entirely. A 90-day horizon returns the same closing balance as a 30-day
    one -- which is the certain layer honestly saying "I know nothing about this
    period" rather than quietly extrapolating."""
    w = world_as_of(real_store, real_store.date_for_day(46))
    assert forecast(w, 30).days[-1].closing == forecast(w, 90).days[-1].closing


def test_the_trough_is_the_earliest_day_at_the_worst_level(real_store):
    """Once the projection flatlines, many days share the minimum. Naming the first
    is the useful answer -- that is when the merchant needs to have acted."""
    w = world_as_of(real_store, real_store.date_for_day(46))
    f = forecast(w, 30)
    lowest = min(d.closing for d in f.days)
    assert f.trough().horizon == min(d.horizon for d in f.days if d.closing == lowest)


def test_every_window_flags_one_or_two_days_never_more(real_store):
    """The report stays readable at every vantage point, not just the ones I looked
    at. The trough always fires; a breach sometimes does; nothing else should."""
    for n in range(46, 107):
        f = forecast(world_as_of(real_store, real_store.date_for_day(n)), 14)
        assert 1 <= len(f.flagged()) <= 2, f"day {n}: {len(f.flagged())} flags"


def test_a_weekend_vantage_point_expects_nothing_tomorrow(real_store):
    """Standing on a Saturday, Sunday brings in exactly nothing -- banks do not
    move money. A calendar-day forecaster gets this wrong every weekend."""
    for n in range(46, 107):
        day = real_store.date_for_day(n)
        if day.weekday() == 5:  # Saturday
            f = forecast(world_as_of(real_store, day), 14)
            assert f.at(1).certain_in == 0
            return
    pytest.fail("no Saturday in the vantage range")


def test_it_projects_past_the_end_of_the_data(real_store):
    """Standing on the last day there are no actuals to check against, but the
    forecast must still be produced -- in production every forecast is like this."""
    f = forecast(world_as_of(real_store, real_store.last_day), 14)
    assert len(f.days) == 14
    assert f.days[-1].date > real_store.last_day


def test_the_report_is_short(real_store):
    """The merchant report is a product, not a dump. Fourteen rows, a header, and
    a handful of flags -- if this ever grows past ~25 lines something is being
    flagged that should not be."""
    w = world_as_of(real_store, real_store.date_for_day(57))
    assert len(render(forecast(w, 14)).splitlines()) <= 25
