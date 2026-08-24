# cashcast

Forecasts a merchant's bank balance 14 days out, separates what's certain from what's
estimated, and measures how often it's wrong.

**Status: day 1 of the build.** Foundations only — see [Status](#status). No results
to report yet, and this README will not claim any until they exist.

---

## The problem

A business can be profitable and still fail to make payroll, because profit and cash
are not the same thing.

When a customer pays by card, the money does not arrive. It sits with the payment
gateway for two working days, and what eventually lands is net of fees. UPI arrives a
day sooner. Refunds are deducted from a _later_ payout than the sale they reverse.
Chargebacks turn up weeks after the fact.

So a merchant who sold ₹95,000 this week does not have ₹95,000. They have some of it,
arriving in pieces, on days that depend on when each sale was captured and how it was
paid for.

The question that actually matters is not _"how much did we sell?"_ but:

> **Will there be enough in the bank on Friday?**

Today most small businesses answer that by looking at the current balance and hoping.
The information needed to answer it properly already exists — it is just scattered
across the merchant's own order records, their gateway account, and their bank
statement, and nobody has the time to assemble it every morning.

## What it does

Given only what the business could know today, it projects the bank balance for each of
the next 14 days. Three layers, deliberately kept separate:

| Layer         | What it covers                                                                                    | How                                                                                                       |
| ------------- | ------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| **Certain**   | Money already captured and in transit, refunds and chargebacks already raised, committed outflows | Arithmetic on known facts. No prediction.                                                                 |
| **Estimated** | Future sales, future refunds                                                                      | Patterns measured from history. Individual events are unknowable; their totals are not.                   |
| **Honest**    | Everything else                                                                                   | Not predicted. Reported as an uncertainty range, with a calibration check proving the range is not a lie. |

Every number is computed in Python. An agent sits on top to decide what to investigate
and to explain what it found — it never performs arithmetic.

The output for each day is not a bare figure but an explained one: opening balance,
what flows in and out, how much of the day is certain versus estimated, and a range.

## How accuracy is measured

This is the part that distinguishes the project, so it comes before the features.

You cannot wait a month to learn whether a forecast was any good. So the dataset is
120 days of synthetic history where every event is known, and the forecaster is tested
against it by **backtesting**:

- Stand on day 46. Forecast days 47–60. Compare each against what actually happened.
- Slide to day 47. Do it again. Continue to day 106.
- **61 vantage points × 14 horizons = 854 measured forecasts**, every one with a known
  right answer.

Results are grouped by horizon, so the headline is not one number but fourteen — error
one day out, two days out, and so on. The forecast should be near-exact tomorrow and
rough in a fortnight, and showing that curve honestly matters more than any single
figure.

### The discipline that makes it mean anything

The generator knows all 120 days. **The forecaster must not.**

Every event carries two dates — when the merchant could first _know_ about it, and when
the cash actually _moves_. A chargeback raised on day 50 and debited on day 53 is
knowable from day 50, so on day 51 it belongs in the certain layer even though no money
has moved.

The forecaster's only access to data is a function that filters on that first date. It
never receives a file path, so it cannot see the future even by accident.

Verified by a test that deletes every event after the vantage day and asserts the
forecast is byte-identical.

### What will be measured

Not one accuracy figure — a set of findings, each a controlled comparison:

1. Certain-layer-only versus certain-plus-estimated: what the statistical layer is worth
2. A planned promotion hidden versus declared: what knowing about it is worth
3. Growth bias in the sales baseline: measure it, correct it, measure the improvement
4. Baseline pollution: one sale day distorts a four-week average for a month
5. Calibration: does the 80% range contain the truth 80% of the time?
6. Distance from the noise floor: how close to the theoretical best is this?
7. Can the forecaster see a cash squeeze two weeks out that only the estimated layer
   can predict?

## Status

```
[x] Money as integer paise, half-up rounding, per-transaction fee arithmetic
[x] Settlement calendar: working days, bank holidays, cutoffs, per-method cycles
[ ] Event model
[ ] Data generator
[ ] The temporal wall + leak test
[ ] Certain layer: deterministic walk-forward
[ ] Backtest harness + error by horizon
[ ] Estimated layer: weekday baseline, refund rate and lag
[ ] Honest layer: rolling intervals + calibration
[ ] Agent
```

## Design decisions

Recorded because "chosen deliberately, and defensible" matters more than "correct" —
none of these have a single right answer.

| Decision                                   | Reason                                                                                                                                                                                                                                 |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Money is integer paise, never a float**  | The product is a claim that numbers tie exactly. `0.1 + 0.2 != 0.3` is a curiosity in most programs and a defect here.                                                                                                                 |
| **Half-up rounding, not Python's default** | `round(2.5)` gives 2. Defensible for statistics, wrong for an invoice.                                                                                                                                                                 |
| **Two dates on every event**               | Separating "when I could know" from "when cash moves" is what lets a raised-but-undebited chargeback count as certain rather than unpredictable.                                                                                       |
| **Sales and payments modelled separately** | Matches how the gateway models it, and refunds attach to an order rather than to a cash movement.                                                                                                                                      |
| **14-day horizon**                         | Long enough that the estimated layer dominates the far end, short enough that the certain layer dominates the near end. The transition between them is the result.                                                                     |
| **No machine learning**                    | The forecast is averages, ratios and percentiles — descriptive statistics, not modelling. With 120 days of self-generated data an ML model would be untestable, and in finance a number that cannot be explained cannot be signed off. |
| **The model never does arithmetic**        | A language model averaging four numbers produces a plausible wrong answer. It decides what to look at and explains the result; Python computes it.                                                                                     |

## What this doesn't handle

Stated up front, because the limits were known before the build started rather than
discovered during it.

- **Sales-prediction skill cannot be measured here.** The sales pattern was chosen when
  the generator was written, so any model that "discovers" it is discovering a choice.
  What _can_ be measured honestly is failure behaviour: whether the system notices when
  it is wrong, quantifies it, and attributes the cause.
- **Chargebacks are unpredictable in principle, not merely unpredicted.** They are rare
  enough that no realistic dataset contains sample enough to estimate a rate from.
- **One merchant, one bank account, one currency, one gateway.** No multi-account
  allocation, no FX, no marketplace payouts.
- **Bank holidays are a placeholder list**, not the real RBI calendar.
- **Fee assumptions are unverified** — 2% plus 18% GST on the fee. The structure is what
  is modelled, not the specific rates.

## Running it

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt
pytest
```

Nothing else to run yet.

## Layout

```
src/
  money.py            integer paise, per-transaction fee and GST
  calendar_rules.py   working days, holidays, cutoffs, settlement cycles
tests/
  test_money.py           fee and rounding arithmetic, worked by hand first
  test_calendar_rules.py  settlement timing, incl. why a calendar-day window fails
notes/
  worked-example.md   the settlement mechanics, worked on paper before any code
```

Coming: `events.py`, `generate.py`, `world.py`, `forecast.py`, `estimate.py`,
`intervals.py`, `backtest.py`, `report.py`, `agent.py`.

---

Built for Track 04 — AI Finance Controller.
