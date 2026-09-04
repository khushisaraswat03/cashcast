# cashcast

**A 14-day cash-position forecast for a small online merchant.** It separates the
money that is already certain from the money that is a guess, and it measures how
often it is wrong.

Built for Track 04 — AI Finance Controller.

---

## Run it

```bash
python -m venv venv && source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m src
```

That prints the whole result. The dataset generates itself on first run, seeded, so
your numbers will match the ones below exactly.

```bash
pytest                       # 319 tests
streamlit run streamlit_app.py   # the same thing in a browser, with the agent
```

The agent needs a free Groq key in `.env` as `GROQ_API_KEY=...`. Everything else
works without one.

---

## What it gets right

61 vantage points × 14 horizons = **854 forecasts**, each scored against what
actually happened. Never pooled across horizons — tomorrow is nearly free and a
fortnight is mostly guesswork, and one averaged number would describe neither.

| Days ahead | Typical error | Of balance | A rule doing no work | Already committed |
|---|---|---|---|---|
| 1 | **₹0** | 0.0% | ₹48,088 | 100% |
| 3 | ₹7,840 | 3.0% | ₹53,004 | 26% |
| 7 | ₹17,155 | 6.6% | ₹57,038 | 8% |
| 14 | ₹28,685 | 11.1% | ₹64,349 | 8% |

*"A rule doing no work" is the mean of the last 14 closing balances — chosen in
advance as the strongest of five trivial rules, not picked afterwards for being
easy to beat.*

**Tomorrow is exact at all 61 vantage points.** Not because the forecast is clever,
but because everything arriving tomorrow was already captured — there is nothing
left to guess. A non-zero figure there would mean the arithmetic is broken.

### The three questions a merchant actually asks

| | cashcast | The trivial answer |
|---|---|---|
| Will I run short of what I owe? | **93%** correct | 56% (always guess the common case) |
| Which day is tightest? | **74%** correct | 7% guessing · **74%** "day of the biggest bill" |
| How much should I trust this? | **77%** landed inside their own 80% band | — |

On the tightest day, the trivial rule ties it — the tightest day usually *is* the day
of the biggest bill, and both find it. The difference is that only the forecast knows
the **balance** on that day, which is what decides whether the bill can actually be
paid. Reported as a tie because it is one.

---

## The idea

When a customer pays by card, the money does not arrive. It sits with the gateway
for two working days and lands net of fees. UPI arrives a day sooner. Refunds come
off a *later* payout than the sale they reverse. Chargebacks turn up weeks after.

So a merchant who sold ₹95,000 this week does not have ₹95,000. They have some of
it, arriving in pieces, on days that depend on when each sale was captured and how
it was paid for. The question is not *"how much did we sell?"* but **"will there be
enough in the bank on Friday?"**

### Three layers, kept separate

| Layer | Covers | How |
|---|---|---|
| **Certain** | Money captured and in transit; refunds and chargebacks already raised; committed outflows | Arithmetic on known facts. No prediction at all. |
| **Estimated** | Future sales, future refunds | Weekday averages, a refund rate and a lag distribution, measured from history. |
| **Honest** | Everything left | Not predicted — reported as a range, with a calibration check proving the range is not a lie. |

The share of each day that is *certain* falls from 100% tomorrow to 8% at fourteen
days. The error is allowed to rise as it falls. That relationship is the result.

### Two dates on every event

Every event carries **when it became knowable** and **when the cash moves**. A
chargeback raised on the 12th and debited on the 15th is *certain* from the 12th —
no money has moved, but nothing about it is a guess any more.

### The temporal wall

The generator knows all 120 days. The forecaster must not.

Its only access to data is a function that filters on that first date. It is handed
that filtered object and never a file path, so it cannot reach the future even by
accident. Verified by a test that deletes every event after the vantage day and
asserts the forecast comes out byte-identical.

---

## What it could not resolve

267 of 854 forecasts (31%) were wrong by more than ₹22,328 — the point past which
the error stops being ordinary noise. Listing bad days would be an apology.
Attributing them to causes, and saying which have a fix, is a system that knows its
own limits.

| | Cause | Forecasts |
|---|---|---|
| **not fixable** | ordinary daily variation | 126 |
| **fixable** | the demand dip after a promotion | 113 |
| **fixable** | a promotion the forecast could not size | 28 |
| **absent** | a chargeback | 0 — looked for, and found to explain none |

`python -m src.exceptions` prints this with worked examples.

---

## The agent

Five tools over the finished forecast. The model decides *what to look at* and turns
the result into a sentence. It never adds, compares or estimates anything.

Every number in its answer is checked against what the tools actually returned, and
an answer containing a figure no tool produced is **rejected rather than shown**.
Fourteen questions were fixed in advance — seven it should answer, seven it should
refuse.

It refuses questions beyond 14 days, requests to predict a chargeback (unpredictable
in principle, not merely unpredicted), and anything about profit or tax — those are
accrual questions and this forecasts cash. **Refusing well is the point**, not a
fallback.

---

## Deliberate decisions

None of these have a single right answer, which is why they are written down.

| Decision | Reason |
|---|---|
| **Integer paise, never a float** | The product is a claim that numbers tie exactly. `0.1 + 0.2 != 0.3` is a curiosity in most programs and a defect here. |
| **Half-up rounding, not Python's default** | `round(2.5)` gives 2. Defensible for statistics, wrong for an invoice. |
| **Fees applied per transaction, not per batch** | The two answers diverge, and per-transaction is what the gateway actually does — so it is what reaches the bank. |
| **A derived floor, not a chosen one** | "Why ₹1,00,000?" has no good answer. "Because that is what you owe on the 12th" does. |
| **Baselines fixed before measuring** | A baseline picked after seeing the results is a baseline picked for being beatable. |
| **No machine learning** | The forecast is averages, ratios and percentiles. With 120 days of self-generated data a model would be untestable, and in finance a number that cannot be explained cannot be signed off. |
| **The model never does arithmetic** | A language model averaging four numbers produces a plausible wrong answer, which is worse than no answer. |
| **Four runtime dependencies** | No pandas: a single missing value silently promotes an int column to float64, which is precisely the bug this repo claims to avoid. |

---

## What this does not handle

Stated up front, because these limits were known before the build rather than
discovered during it.

- **Sales-prediction skill cannot be measured here.** The sales pattern was chosen
  when the generator was written, so any model that "discovers" it is discovering a
  choice. What *can* be measured is failure behaviour: whether the system notices it
  is wrong, quantifies it, and attributes the cause.
- **Chargebacks are unpredictable in principle.** Too rare for any realistic dataset
  to estimate a rate from. The system treats them as certain once raised and refuses
  to forecast them before.
- **One merchant, one bank account, one currency, one gateway, one promotion.**
- **Bank holidays are a placeholder list**, not the real RBI calendar.
- **Fee assumptions are unverified** — 2% plus 18% GST on the fee. The structure is
  what is modelled, not the rates.

---

## Layout

```
src/
  money.py           integer paise, per-transaction fee and GST
  calendar_rules.py  working days, holidays, cutoffs, settlement cycles
  events.py          six event types, two dates each
  generate.py        120 days of synthetic history
  world.py           the temporal wall -- what was knowable on a given day
  forecast.py        the certain layer, flags, the derived floor
  estimate.py        weekday baselines, refund rate and lag
  intervals.py       rolling uncertainty bands and their calibration
  backtest.py        854 scored forecasts, baselines fixed in advance
  exceptions.py      misses attributed to causes, fixable or not
  report.py          the printed result
  tools.py           five tools the agent may call
  agent.py           the loop, and the guardrail on every number
  evaluate.py        14 questions fixed in advance
tests/               319 tests
notes/design-log.md  what broke, and what it cost to find out
```
