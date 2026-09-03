"""Tests for settlement timing -- when money captured today reaches the bank.

This is the foundation of the certain layer of the forecast. Everything already
captured has a knowable arrival date, and getting that date wrong puts real money
on the wrong day of the projection: one day too early and the forecast says payroll
clears when it does not.

The headline assertion is `test_calendar_day_arithmetic_fails_thursday_and_friday`:
executable evidence that "capture date + 2 calendar days" is correct Monday to
Wednesday and wrong Thursday and Friday. A 40%-of-the-week failure rate that passes
a casual check is exactly the kind of bug that needs a test rather than a comment.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from src.calendar_rules import (
    add_working_days,
    batch_day,
    cycle_days,
    is_working_day,
    next_working_day,
    settlement_date,
    working_days_between,
)

# August 2026: the 24th is a Monday. Fixed dates rather than relative offsets, so
# a failure names a real day of the week you can reason about.
MON = date(2026, 8, 24)
TUE = date(2026, 8, 25)
WED = date(2026, 8, 26)
THU = date(2026, 8, 27)
FRI = date(2026, 8, 28)
SAT = date(2026, 8, 29)
SUN = date(2026, 8, 30)
NEXT_MON = date(2026, 8, 31)

NO_HOLIDAYS: frozenset[date] = frozenset()


def test_day_of_week_assumptions_hold() -> None:
    """Guards the premise of every other test in this file."""
    assert MON.weekday() == 0 and SAT.weekday() == 5 and SUN.weekday() == 6


class TestWorkingDays:
    def test_weekend_is_not_a_working_day(self) -> None:
        assert is_working_day(FRI, NO_HOLIDAYS)
        assert not is_working_day(SAT, NO_HOLIDAYS)
        assert not is_working_day(SUN, NO_HOLIDAYS)

    def test_holiday_is_not_a_working_day(self) -> None:
        gandhi_jayanti = date(2026, 10, 2)
        assert gandhi_jayanti.weekday() == 4  # a Friday, so the holiday actually bites
        assert is_working_day(gandhi_jayanti, NO_HOLIDAYS)
        assert not is_working_day(gandhi_jayanti, frozenset({gandhi_jayanti}))

    def test_next_working_day_is_idempotent_on_a_working_day(self) -> None:
        assert next_working_day(MON, NO_HOLIDAYS) == MON
        assert next_working_day(SAT, NO_HOLIDAYS) == NEXT_MON

    def test_add_counts_from_the_day_after(self) -> None:
        assert add_working_days(MON, 0, NO_HOLIDAYS) == MON
        assert add_working_days(MON, 1, NO_HOLIDAYS) == TUE
        assert add_working_days(FRI, 1, NO_HOLIDAYS) == NEXT_MON
        assert add_working_days(SAT, 1, NO_HOLIDAYS) == NEXT_MON

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError):
            add_working_days(MON, -1)

    def test_holiday_pushes_further(self) -> None:
        holidays = frozenset({date(2026, 10, 2)})  # Friday
        thu = date(2026, 10, 1)
        # T+1 skips the Friday holiday and the weekend, landing on Monday.
        assert add_working_days(thu, 1, holidays) == date(2026, 10, 5)
        assert add_working_days(thu, 2, holidays) == date(2026, 10, 6)

    def test_working_days_between(self) -> None:
        assert working_days_between(THU, NEXT_MON, NO_HOLIDAYS) == 2
        assert working_days_between(MON, WED, NO_HOLIDAYS) == 2
        assert working_days_between(MON, MON, NO_HOLIDAYS) == 0


class TestTheWeekend:
    """The weekend is why T+2 is not two days."""

    def test_thursday_settles_monday(self) -> None:
        assert add_working_days(THU, 2, NO_HOLIDAYS) == NEXT_MON

    def test_thursday_is_four_calendar_days(self) -> None:
        assert (NEXT_MON - THU).days == 4  # "T+2" is off by 100% in calendar terms

    def test_monday_31_settles_wednesday_2_sep(self) -> None:
        assert add_working_days(NEXT_MON, 2, NO_HOLIDAYS) == date(2026, 9, 2)

    @pytest.mark.parametrize(
        "captured,settles,naive_is_right",
        [
            (MON, WED, True),
            (TUE, THU, True),
            (WED, FRI, True),
            (THU, NEXT_MON, False),  # naive says Sat 29 Aug
            (FRI, date(2026, 9, 1), False),  # naive says Sun 30 Aug
        ],
    )
    def test_calendar_day_arithmetic_fails_thursday_and_friday(
        self, captured: date, settles: date, naive_is_right: bool
    ) -> None:
        """Why settlement dates are computed in working days, not calendar days.

        "+2 calendar days" is correct for Mon/Tue/Wed captures and wrong for
        Thu/Fri, so it passes a casual check and is wrong two days in every five.

        For the forecast, being wrong here is not a rounding issue -- it puts a
        whole day's takings on the wrong row. A Thursday capture lands Monday, not
        Saturday, so a forecast built on calendar arithmetic shows money arriving
        over the weekend that will not be there until Monday morning.
        """
        assert add_working_days(captured, 2, NO_HOLIDAYS) == settles
        naive = captured + timedelta(days=2)
        assert (naive == settles) is naive_is_right

    def test_delay_never_exceeds_five_calendar_days(self) -> None:
        """Bounds the lag: a T+2 capture always lands within 5 calendar days, from
        any day of the week. Not true once a Friday bank holiday is involved --
        that reaches 6, and it is recorded as a known limitation rather than
        silently absorbed."""
        for offset in range(7):
            captured = MON + timedelta(days=offset)
            settles = add_working_days(captured, 2, NO_HOLIDAYS)
            assert 1 <= (settles - captured).days <= 5, captured


class TestCutoff:
    CUTOFF = time(18, 0)

    def test_before_cutoff_stays_in_todays_batch(self) -> None:
        assert batch_day(datetime.combine(MON, time(17, 59)), self.CUTOFF) == MON

    def test_at_or_after_cutoff_rolls_forward(self) -> None:
        assert batch_day(datetime.combine(MON, time(18, 0)), self.CUTOFF) == TUE
        assert batch_day(datetime.combine(MON, time(23, 30)), self.CUTOFF) == TUE

    def test_two_sales_an_hour_apart_settle_a_day_apart(self) -> None:
        """The cutoff, made concrete: 17:30 and 18:30 on the same Monday land in
        different batches and reach the bank a day apart."""
        before = settlement_date(
            datetime.combine(MON, time(17, 30)), "card", cutoff=self.CUTOFF, holidays=NO_HOLIDAYS
        )
        after = settlement_date(
            datetime.combine(MON, time(18, 30)), "card", cutoff=self.CUTOFF, holidays=NO_HOLIDAYS
        )
        assert before == WED
        assert after == THU

    def test_on_a_friday_the_cutoff_does_not_matter(self) -> None:
        """The weekend absorbs the cutoff.

        A Friday 17:00 capture batches on Friday; a Friday 21:00 capture batches on
        Saturday. Both then count T+1 to Monday and T+2 to Tuesday, so they settle
        together -- whereas on a Monday the same one-hour gap splits them across two
        days (see the test above). The interaction is not intuitive, which is the
        argument for computing settlement dates in one tested function rather than
        inline wherever the forecast happens to need them.
        """
        late = settlement_date(
            datetime.combine(FRI, time(21, 0)), "card", cutoff=self.CUTOFF, holidays=NO_HOLIDAYS
        )
        early = settlement_date(
            datetime.combine(FRI, time(17, 0)), "card", cutoff=self.CUTOFF, holidays=NO_HOLIDAYS
        )
        assert early == late == date(2026, 9, 1)
        assert (late - FRI).days == 4


class TestMethodCycles:
    def test_default_is_t_plus_2(self) -> None:
        assert cycle_days("card") == 2
        assert cycle_days("netbanking") == 2
        assert cycle_days("something_new") == 2  # unknown methods must not crash

    def test_upi_is_configured_faster(self) -> None:
        assert cycle_days("upi") == 1

    def test_same_afternoon_two_different_bank_days(self) -> None:
        """Two customers, same afternoon, money landing on different dates."""
        when = datetime.combine(MON, time(15, 0))
        card = settlement_date(when, "card", holidays=NO_HOLIDAYS)
        upi = settlement_date(when, "upi", holidays=NO_HOLIDAYS)
        assert card == WED and upi == TUE

    def test_cycle_override_wins(self) -> None:
        when = datetime.combine(MON, time(15, 0))
        assert settlement_date(when, "upi", holidays=NO_HOLIDAYS, cycle_override=2) == WED
