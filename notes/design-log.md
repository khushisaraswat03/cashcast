# cashcast — design log

Every decision in this project, with the reasoning behind it. Started 28 Aug 2026 and
extended as the build continued.

This is the reference document: what was chosen, what was rejected, what was measured,
and what broke along the way.

> **How to use this.** Read it for the reasoning; write your *own* answers in
> [your-answers.md](your-answers.md). A decision you can defend from memory is worth
> more than one you have to look up.

---

## 1. The brief, and what it actually asks for

> Build an **agent** that closes **one** finance-ops loop across a **50+ record** batch of
> synthetic data, reporting its **match rate** and the **exceptions it could not resolve**.
>
> The bar: *"One cherry-picked match proves nothing."*
>
> Why now: *"Verification capacity, not generation speed, is the bottleneck."*

Track 04 — AI Finance Controller. Four things follow from reading it closely:

- **"one" loop, singular.** The four listed directions are headed *EXAMPLE DIRECTIONS* — a
  menu, not a checklist. Attempting all four gets four shallow demos, which is exactly what
  the bar warns against.
- **50+ records is a floor, and a low one.** Scale is explicitly not the challenge.
  "Throughput" here means *runs a whole batch without a human babysitting each row*.
- **It says "agent" twice.** So the LLM can't be a decoration bolted on at the end.
- **"Verification capacity is the bottleneck"** tells you what they mean by agent. Not
  something that writes prose about your money — something that does *checking* work:
  decides what to look at next, gathers evidence, chases the discrepancy, and is straight
  about what it couldn't settle.

The submission form asks exactly one question: **"what broke and how did you fix it."**
No pitch was requested. Deadline **Sat 5 Sep 2026**.

That question is the whole submission, and it filters for something specific: anyone can
produce a demo, but only someone who actually built something can describe what broke. A
project where nothing broke was a project that didn't attempt much. Hence the failure log
(§11) — kept from day one, because you will not remember these on the 5th.

## 2. Has this been solved already?

Yes, thoroughly — and it's better to know that now than in an interview.

- **Razorpay ships it themselves.** Transaction-level settlement reports, fee breakdowns,
  a Single Reconciliation View. They know every trap in this domain.
- **Recko** did payment reconciliation end to end, founded 2017 in Bengaluru, acquired by
  **Stripe in Oct 2021** — their first India acquisition. Customers included Meesho,
  PharmEasy, Myntra.
- **BlackLine, Trintech, Oracle** have done the enterprise version for decades.
- Open-source record linkage (`dedupe`, `splink`, `recordlinkage`) solves the abstract
  matching problem.

So "different" is the wrong target. Nobody expects a two-week project to beat Recko, and
claiming it does is the judgment failure they'd notice. They picked a problem they already
understand completely, which strongly suggests they're assessing **how you think**.

The differentiator is therefore the demonstration, not the product:

- not using an LLM for arithmetic, deliberately
- real measured numbers instead of a screenshot
- optimising for the *right* error and being able to say why
- knowing what the system structurally cannot detect

If you want a genuine gap: **none of those commercial products publish accuracy numbers.**
Nobody can tell you what Recko's precision is. An open, reproducible benchmark doesn't
exist. That's a legitimate "here's what's new" — but it's a choice, not a requirement.

## 3. Why the cash forecaster, out of the four options

|  | Crowdedness | Domain load | Agent fit | Metric obvious? | 12 days |
|---|---|---|---|---|---|
| Multi-source reconciliation | High | Low | Medium | Yes | Safe |
| Settlement Q&A agent | Medium | Low | **High** | No — you define it | Safe |
| **Forward cash forecaster** | **Low** | Medium | Medium | Yes | Safe, more build |
| Tax-line matcher (GST) | **Lowest** | **High** | High | Yes | Tight |

Chosen: **forward cash forecaster.** Reasons, in order of weight:

1. It matches the track title directly — *"run the books **and the cash position**."*
   Cash position is forward-looking; reconciliation isn't.
2. It's the least crowded of the safe options. Most applicants will pick reconciliation.
3. **Backtesting** gives an unusually credible accuracy story — hundreds of measured
   forecast/actual pairs rather than one number.

Note that reconciliation sits *underneath* the other three, which is both why it's the
safest foundation and why it's crowded.

### The risk in this choice, stated honestly

The impressive-looking part is trivial and the genuinely hard part is small and awkward:

- Settlement dates are **rules**, not predictions. The near-term forecast is a lookup table
  plus arithmetic — a competent developer builds it in an afternoon.
- Your accuracy will look great **for the wrong reason**. Days 1–3 are almost entirely money
  you already collected. *"How much of that accuracy is just money already in the pipe?"* is
  the question that evaporates an unearned metric.
- Predicting future sales isn't a finance problem, it's generic time-series — and **you
  generated the data**, so any pattern your forecaster "discovers" is your own random-number
  choices. That circularity is permanent; no cleverer setup fixes it.
- The exception list risks being a page of shrugs rather than diagnosed causes.

**What removes the risk** — and these four are why the project is shaped the way it is:

1. **Separate deterministic from estimated and report them separately.** *"At 2 days out,
   96% of the projected balance is already-captured money and our error is ₹120. At 10 days
   out only 31% is determined and our error is ₹18,000."* That reframing turns the weakness
   into the most honest thing in the room.
2. **Make uncertainty the product.** Not "₹62,758" but "₹58,000–₹67,000 at 80% confidence" —
   then measure whether the truth lands inside the 80% band 80% of the time.
3. **Forecast the decision, not the number.** "Will you breach zero in 14 days" is binary,
   consequential, and has beautifully asymmetric errors: a missed breach means bounced
   payroll, a false alarm means a wasted phone call.
4. **Move the agent upstream** — let it decide what to investigate when the forecast shifts,
   rather than narrating a finished answer.

## 4. The core idea

**You don't forecast events. You forecast amounts.**

You cannot know today that customer #4471 will ask for a ₹5,000 refund on Thursday. You
don't need to. You need to know *how much refunding* to expect — and that is stable and
measurable. (A shop can't name tomorrow's customers and still staffs correctly; insurance
prices accurately without knowing who will crash.)

Everything that moves money sorts into three buckets. This is the whole design.

### Bucket 1 — Certain

Arithmetic on facts you already have. Payments captured but not yet settled, settlements
already scheduled, rent, salaries, autopay mandates — **and refunds and chargebacks already
raised but not yet debited.** Dominates the next 2–3 days.

That last item matters more than it looks. A chargeback has a lifecycle: raised → reviewed →
debited days later. **Once it's raised, it's visible.** So a chunk of what feels like Bucket
3 is actually Bucket 1 if you go looking. Assembling a complete picture of the *present* is
the agent's real job — which is precisely what *"verification capacity is the bottleneck"*
means. The information exists; nobody has the capacity to gather it every morning.

### Bucket 2 — Statistically predictable

**Sales:** average the last four same-weekdays. Grouped by weekday, because averaging
Thu–Sun together lets a big Saturday leak into a Tuesday forecast. Crude, defensible, and
genuinely where production forecasting starts.

**Refunds:** the good one. A refund isn't a new random event — **it attaches to a sale that
already happened.** Measure two things from history: what fraction of sales value comes back
(say 3%), and the lag distribution (say half at +5 days, half at +8). Then for every past
day of sales, spread the expected refund value across the following days:

```
Day 95 sold ₹100  →  ₹1.50 on day 100,  ₹1.50 on day 103
Day 96 sold ₹120  →  ₹1.80 on day 101,  ₹1.80 on day 104
Day 98 sold ₹100  →  ₹1.50 on day 103,  ₹1.50 on day 106
                     ──────────────────────────────────
                     expected refunds day 103 = ₹3.00
```

For the next ten days your refund forecast comes almost entirely from sales already in the
books. You're barely guessing at all.

### Bucket 3 — Honest

Doesn't predict. Does three other things:

1. **Ranges from your own past errors.** Take all your 3-days-ahead errors, sort them, take
   the 10th and 90th percentile. That's your 80% band. No probability theory required.
2. **Calibration.** If you say 80%, the truth should land inside about 80 times in 100.
   Inside 52 times → overconfident, widen. Inside 99 times → so wide it's useless. This
   check is ~15 lines of code and it's the most grown-up thing in the project.
3. **Named what-ifs** instead of predictions. *"You get roughly one chargeback a month,
   around ₹15,000. If one lands next week, Friday goes from ₹62,000 to ₹47,000 — you'd still
   make payroll."*

### Build order — not 1 → 2 → 3

```
1. Generator            events unfolding day by day
2. Bucket 1             deterministic walk-forward
3. Backtest harness     measure Bucket 1 on its own      ← must precede 4
4. Bucket 2             measured as an improvement over 3
5. Bucket 3             intervals derived from 3's residuals
6. Agent                sits on top of all of it
```

Step 3 must come before 4 or there's nothing to compare against, and step 5 literally
consumes step 3's output. What this buys you is **a number at every stage**:

> Deterministic only: ±₹18,000 at 10 days out.
> Add the statistical layer: ±₹9,000.
> Add calibrated intervals: the 80% band contains the truth 78% of the time.

Far more convincing than any single final number, and only available if you build in this
order and measure as you go.

## 5. Who it's for

The **merchant** — the business receiving the money. Sharpened one level: the track is
"AI Finance **Controller**", and a financial controller is a real job title. The brief's
vocabulary ("match rate", "honest exception list") is finance-professional language.

A stall owner wants one sentence: *"you're fine until Thursday."* A controller wants the
workings, the assumptions, and the ability to drill into anything that looks off. **Build for
the controller** — the output is a number *plus its reasoning*.

Decisions it changes: can I make payroll on the 30th · can I pay this supplier today or wait
three days · should I commit to this ad spend · **do I need to pay for instant settlement?**

That last one is worth mentioning out loud: Razorpay sells instant settlement as a paid
product. A forecaster saying *"you're short ₹80,000 on Tuesday"* leads directly to *"so pay
the fee and pull Monday's settlement forward."* That's probably part of why this track exists.

Useful consequence: every data source belongs to the merchant already — their orders, their
gateway account, their bank statement. No third-party feeds, no permissions problem. That's
why it's buildable in twelve days.

## 6. The merchant — locked decisions

| | | Why |
|---|---|---|
| Business | D2C fashion brand, own website | Fashion has the highest return rate in e-commerce, so Bucket 2 has real data to learn from. Apparel returns are also the textbook case for "predict amounts, not events" — you'd be explaining the project using its own data. |
| Not Myntra itself | Marketplace ≠ merchant | Myntra collects from shoppers and pays thousands of sellers; its cash position is about payouts, not its own sales. And 1,200 transactions would be absurd at its scale. |
| Volume | ~10 orders/day → ~1,200 over 120 days | Derived from refund count (see §7). Also believable for a small D2C shop, and hand-verifiable — ~10 payments per settlement batch is few enough to check on paper, which you *will* need to do. |
| Methods | Cards **T+2**, UPI **T+1** | Via a **source-keyed rule table**, not `if/else` — we need two rules anyway, so a table is free, and it leaves the door open for a third source. |
| Fees | 2% of transaction, 18% GST on the fee | Structure is what's modelled; the specific rates are unverified and flagged as such. |
| Returns | 15–25% of sales, **7–14 day lag as a distribution** | A distribution, not a fixed number — a fashion return has a decision window, then shipping, then inspection, so the lag is a smear, not a spike. Partial refunds included (order three shirts, return one). |
| Weekday pattern | Sat/Sun ~1.5× average, Tue/Wed ~0.75× | Strong enough that the weekday estimator has a real pattern to find, not so strong it's cartoonish. |
| Growth | ~30% across 120 days | Enough that a four-Thursday average visibly under-predicts. That bias is a finding (§9.3). |
| Day-to-day noise | ±25% requested (**29.9% realised**) | See §9.6 — this sets the floor on how good *any* forecaster can be. |
| Sale week | Days 60–66 at ~3× | Placed with enough history before it for a baseline, and enough runway after for the dip and refund wave to play out. |
| Post-sale dip | Days 67–73 at ~0.8× | Real sales pull demand forward. Causes a *second*, opposite-direction failure (§9.4). |
| Failed payments | ~6% of orders, most retried | Zero cash effect by definition, but "orders placed" ≠ "revenue captured", which is realistic and gives the agent something true to say. |
| **Cash squeeze** | Days ~67–82, **emergent** | Not placed. Falls out of three overlapping causes: post-sale dip + the refund wave from sale-week sales + a supplier payment for the sale stock. See §9.7. |

**Marketplace second payout stream** (selling on Myntra *and* own site, weekly T+7–T+15
payouts minus commission) was considered and deferred — a stretch goal, decided only if
ahead. Its real argument isn't realism: a Myntra payout arriving in 10 days is *certain*,
so it puts deterministic content at horizon 10, which cards and UPI cannot. That would let
you say *"how much of your long-horizon forecast is knowable depends on your payout mix,
not just on time."*

## 7. Sizing — every number derived, not picked

| | Value |
|---|---|
| Data span | **120 days** |
| Warm-up | days 1–45 (never scored) |
| Vantage points | days 46–106 = **61** |
| Horizon | **14 days** |
| Measured forecasts | 61 × 14 = **854**, no clipping |
| Estimator behaviour | **refits at each vantage point** — standing on day 80 it uses days 1–80 |

**Horizon 14** — cards settle in ~2 working days, so days 1–3 are almost entirely Bucket 1
and days 10–14 almost entirely Bucket 2. **That transition is the headline result.** Seven
days doesn't show enough decay; 30+ means Bucket 1 contributes nothing and you're only
testing sales prediction on data you invented — unmeasurable. It's also the natural business
question: can I make payroll in a fortnight.

**~10 orders/day** — derived from how many refunds Bucket 2 needs:
```
30 refund events ÷ 0.03 refund rate = 1,000 transactions
1,000 ÷ 120 days ≈ 8.3/day → round to 10
```

**120 days** = 45 warm-up + 61 vantage + 14 horizon. Also ~4 months, giving 3–4 month
boundaries (settlements cross month ends; month-end bumps are real). A *year* would start
demanding seasonality and trend modelling — the span deliberately bounds the modelling.

**Warm-up 45** — the weekday baseline needs ≥4 of each weekday (28 days), but the
non-obvious constraint is **complete refund histories**: with a lag up to ~10 days, at day 45
you only have complete refund data for sales up to ~day 35. Also, starting at day 29 with
barely-adequate estimates would produce bad early forecasts you'd then be tempted to exclude
— which is cherry-picking. Start late enough that every scored forecast is a fair one.

**Vantage stops at 106** because 106 + 14 = 120. Day 106 is the last vantage point with a
*complete* horizon, so every cell in the 61×14 matrix is a fair comparison.

**854 measured forecasts.** One vantage point produces 14 predictions, one per horizon:
```
                     days ahead
  standing on   +1   +2   +3   ...  +14
   day 46        ✓    ✓    ✓         ✓
   day 47        ✓    ✓    ✓         ✓
    ...
   day 106       ✓    ✓    ✓         ✓
```
Read **down a column** for "how accurate am I 3 days out" (61 measurements each). Read
**across a row** for how one day's forecast decays. The output is therefore fourteen results
each backed by sixty-one measurements — not one number. What's compared is the **closing bank
balance** on that day, forecast versus actual.

Reported against the brief: *1,200 records in, 854 forecast/actual pairs measured, accuracy
broken out by horizon* — clearing "50+ records" twenty-fold without looking gamed.

**Make all five of these config parameters, not constants** — mainly because "here's what
happens at a 7-day versus 21-day horizon" is a good chart you only get for free if horizon
was a knob from day one.

### What the data volume tells you about the buckets

| Estimate | Requires | So you need |
|---|---|---|
| Weekday sales baseline | ≥4 of each weekday | 28+ days |
| Refund rate + lag | ~25–40 refund events | ~800–1,200 transactions |
| Month-end effects | 2–3 month boundaries | ~90–120 days |
| Chargeback rate | ~30 chargebacks | ~10,000 transactions — **not achievable** |

That last row is a finding, not a flaw: **chargebacks cannot be estimated at any dataset size
you'd plausibly generate.** The data volume itself tells you which bucket each event type
belongs in. Frequent events are estimable; rare ones never are. Worth saying out loud.

## 8. Architecture

```
GENERATOR  (omniscient — knows all 120 days)
    │  writes a timeline of events
    ▼
events on disk ──────────────────────────────────┐
    │                                            │
    │  world_as_of(46)                           │  actuals for
    ▼                                            │  days 47–120
KNOWN WORLD  (only what was knowable by day 46)  │
    │         ← THE WALL                         │
    ├──► estimators: weekday sales, refund rate + lag
    │                                            │
    ▼                                            │
FORECASTER → 14 days, each with a balance,       │
    │        a range, and where the number came from
    ▼                                            ▼
         BACKTEST  ─── compares forecast to actual
              │
              ▼
         REPORT  → error by horizon, certain/estimated split, calibration
              │
              ▼
         AGENT  → answers questions using all of the above as tools
```

### 8.1 Every money event carries two dates

The architectural core. Get it wrong and everything needs rewriting.

A chargeback is **raised** day 50, **debited** day 53. On day 51: does the merchant know?
Yes. Has cash moved? No.

| Event | `known_at` | `cash_at` |
|---|---|---|
| Sale / payment | captured | settles (UPI T+1, card T+2) |
| Refund | customer requests it | netted off a later payout |
| Chargeback | raised | debited from a later payout |
| Scheduled outflow | committed | due date |
| Promotion | declared | — no cash; it only shifts sales |

If you stored one date you couldn't tell "what do I know" from "what has happened", and
you'd treat knowable things as unpredictable.

### 8.2 The wall must be structural, not a promise

`world_as_of(day)` filters every event on `known_at <= day`, and it is the forecaster's
**only** input. It never receives a file path, so it cannot cheat even by accident.

**The leak test:** forecast from day 46. Now physically delete every event after day 46 and
forecast again. Output must be byte-identical. Five lines, and it's what makes every number
you report trustworthy. *(Expect this to fail the first time — temporal leaks are easy to
introduce and invisible without the check. That's a failure-log entry.)*

### 8.3 A forecast is not a list of numbers

```
day 52
  opening balance      ₹48,200
  + settlements due    ₹31,400   ← certain (payments already captured)
  + predicted sales     ₹8,900   ← estimated
  − refunds netted      ₹1,100   ← certain (already requested)
  − predicted refunds     ₹640   ← estimated
  − rent               ₹15,000   ← certain
  = ₹71,760            range ₹66,000–₹78,000
  certain: 87%   estimated: 13%
```

Pays for itself twice: it's what lets you report the certain/estimated split (the thing that
stops your accuracy number being unearned), and it's what the agent reads to explain itself.
A bare number gives the agent nothing to say.

### 8.4 Horizon 1 is exactly deterministic — horizon 2 is not

**Corrected 28 Aug, by measurement.** The original claim here was that horizons 1 *and 2* are
100% deterministic. That was reasoned from cards at T+2 alone and is false once UPI is in the
mix. What the data says:

| horizon | mean error | median | worst |
|---|---|---|---|
| 1 | **₹0.00** | ₹0.00 | ₹0.00 |
| 2 | −₹2,560 | −₹1,294 | −₹20,896 |
| 3 | −₹12,596 | −₹12,305 | −₹57,829 |
| 5 | −₹44,921 | −₹41,352 | −₹135,811 |
| 7 | −₹80,045 | −₹77,799 | −₹201,574 |
| 10 | −₹136,040 | −₹128,946 | −₹255,596 |
| 14 | −₹209,915 | −₹211,583 | −₹310,646 |

**Horizon 1 is exact to the paisa at all 61 vantage points.** Everything landing tomorrow was
captured today or earlier, so it is already inside the wall. That makes it the cheapest bug
detector in the project: any error at all means the walk is wrong — a missed event type, a
settlement date off by one, a dropped sign — rather than the forecast merely being
incomplete. Locked in by `test_horizon_1_is_exact_at_every_vantage_point`.

**Horizon 2 leaks, in both directions.** A UPI payment captured *tomorrow* settles the day
after, and tomorrow's sales do not exist yet. So one day of UPI inflow is invisible — which
was the expected under-count. What was not expected is that the same one-day blind spot hides
**outflows** too: a refund requested tomorrow nets off the day after. On 10 of 61 vantage
points the unseen refunds outweigh the unseen sales and the projection comes out *too high*.

That over-counting clusters in the fortnight after the sale week, which is finding #7 showing
up at horizon 2 before Bucket 2 exists at all.

The error curve above is the headline result, and note its shape: **monotonic and one-sided**.
The certain layer is not noisy, it is *biased* — two days of visible inflow against fourteen
days of committed outflow. See §11 on why that makes it a scenario rather than a baseline.

### 8.5 The intervals have a chicken-and-egg problem

Bucket 3's ranges come from past errors, but at the first vantage point there are none.

- **Rolling** (chosen): at vantage V, build the interval from errors observed at vantage
  points *before* V. Early forecasts get wide or absent intervals — correct, because you
  genuinely didn't know your own accuracy yet.
- **Split** (simpler, rejected): vantage 46–70 collect errors unscored, 71–106 scored.

What you must **never** do: compute intervals from all 854 errors and apply them to forecast
#1. That's using the future to calibrate the past, and it quietly inflates calibration.

### 8.6 Modules and build order

```
money.py          ✓ done    integer paise, fee arithmetic
calendar_rules.py ✓ done    working days, holidays, T+1/T+2, cutoffs
events.py         ✓ done    six event types, two dates on everything
generate.py       ✓ done    the 120-day timeline
world.py          ← NEXT    world_as_of(day) — the wall
forecast.py                 Bucket 1 + Bucket 2 → explained projection
backtest.py                 the vantage-point loop
report.py                   error by horizon, split, calibration
estimate.py                 weekday baseline, refund rate + lag
intervals.py                Bucket 3 — rolling empirical bands
agent.py                    tools + the loop
```

Get `backtest.py` and `report.py` working as soon as `forecast.py` does Bucket 1 only. Then
every later addition is *measured* as an improvement over a real baseline rather than hoped
about. **The agent goes last** — it's a layer over finished machinery, and building it early
means debugging two things at once.

### 8.7 Two implementation decisions inside the event model

- **Only four of the six event types move cash.** Orders and promotions are knowledge-only:
  they change what you *expect*, not the balance. So `cash_delta` is zero for them, and the
  daily balance is simply the sum of `cash_delta` over events whose `cash_at` falls that day.
  Uniform, no special cases, no branching on type.
- **Settlements are derived, not stored.** A settlement is just the payments sharing a
  settlement date. Storing them separately means two places holding the same money and a
  chance to double-count. *Cost:* in reality a refund too large for one payout carries
  forward to the next; at ~₹14,000 settling daily against refunds of ₹500–2,000 that never
  binds. Noted as a limitation.

## 9. The seven things you'll be able to measure

This is the actual differentiator — not one accuracy number, seven findings, each a
controlled comparison.

1. **Bucket 1 alone vs Bucket 1+2** — what the statistical layer is worth, in rupees.
2. **Promotion hidden vs declared** — run the backtest twice with one flag flipped:
   ```
   Sale week, forecast error:
     promotion hidden      ₹74,000 off
     promotion declared     ₹9,000 off
     ─────────────────────────────────
     value of knowing:     88% reduction
   ```
   Becomes a product recommendation with a number behind it: *let the merchant declare
   planned promotions.* **This idea is yours** — it came from you pushing back that a planted
   sale week is still deterministic in the generator (§13).
3. **Growth bias.** A four-weekday average looks back 28 days, so it sits 2–3 weeks behind
   the current level. At ~30% growth over 120 days it under-forecasts consistently. The nice
   property: **invisible in any single forecast, unmistakable across 854.** Measure it, fix
   it (weight recent weeks), measure the improvement.
4. **Baseline pollution.** Standing on day 66, forecasting day 70:
   ```
   day 63:  ₹42,000   ← the sale day, now polluting the average
   day 56:  ₹14,000
   day 49:  ₹13,500
   day 42:  ₹13,000
   ────────────────
   average: ₹20,625      actual: ₹11,000     ← off by 87%, opposite direction
   ```
   One promotion causes **two** failures: under-predicts during the sale (didn't see it
   coming), then over-predicts for four weeks (average polluted, real level dropped). Stays
   wrong until the sale Thursday falls out of the window. Fix: exclude promo days from the
   baseline — another measured improvement.
5. **Calibration.** Does the 80% band contain the truth 80% of the time? *(Expect it to come
   out badly at first — intervals too narrow is the classic result. Failure-log entry.)*
6. **Distance from the noise floor.** ~30% realised daily noise → **irreducible 14-day error
   ₹22,328**. No forecaster can beat that, ever. So: *"the irreducible error for this
   business is ₹22,328 at 14 days; we achieve ₹X."* Hardly anyone reports how close to
   optimal they are, and it's very hard to argue with.
7. **The squeeze test.** Standing mid-sale, holding ₹1.24L while selling triple — can the
   forecaster see the squeeze coming? **Measured 30 Aug: yes from day 62 onward.** The
   estimated layer names the right trough day and gets the level within ₹3,000–17,000; the
   certain layer misses by ₹71,158 and puts the trough on the wrong day at every vantage
   point, because its balance only falls and it is naming the end of its own slide.

   **The stated mechanism was wrong.** This originally read *"because the refund wave is
   predictable from sales already in the books"*. Over the fortnight from day 64 the
   estimated layer projects ₹63,681 of refunds against ₹239,887 of settlements from
   unmade sales — so the **sales half does about three quarters of the work**. Adding
   refunds halves the residual miss (₹6,370 → ₹3,236), which is real but is not what
   rescues the number. The refund half remains the more interesting idea — 95% of that
   fortnight's refund outflow is invisible to the certain layer — it is just not the
   reason the forecast works.

## 10. Technology decisions

**No ML, no training — anywhere.** What sounds like prediction is:

| Sounds like | Actually is |
|---|---|
| Predicting Tuesday's sales | `(35+39+33+37)/4` — an average |
| Predicting the refund rate | `300/10000` — a division |
| Predicting refund timing | counting past refunds by lag |
| Predicting the uncertainty range | sorting past errors, taking percentiles |

Descriptive statistics. No weights, no fitting loop, no training artifact. Two words that
caused confusion and are worth correcting: **"warm-up"** is not a training phase, it means
"wait until you've seen enough Thursdays"; **"refit at each vantage point"** is not
retraining, it means "recompute the average with one more day in it".

ML would earn its place with *years* of real data and interactions to capture (weekday ×
promotion × category × payday cycles). With 120 days you generated yourself it would be worse
than useless — **untestable**, since any pattern it discovers is your own random-number
choice. And the interview line: *deliberately not using ML where an average suffices is a
stronger signal than using it everywhere* — especially in finance, where a number you can't
explain can't be signed off. A weekday average is auditable; a gradient-boosted model isn't.
(If far ahead: quantile regression for the bands instead of raw percentiles is the one
legitimate upgrade. Don't start there.)

**Money is integer paise, never float.** The product is a claim that numbers tie exactly;
`0.1 + 0.2 != 0.3` is a curiosity elsewhere and a defect here. Half-up rounding, not Python's
banker's rounding — `round(2.5)` gives 2, which is defensible for statistics and wrong for an
invoice.

**No pandas.** Not aesthetics: a single missing value in a pandas int column silently
promotes the whole column to `float64` — exactly the bug class this project claims to avoid.
It would be embarrassing to have a float creep in through the convenience layer while
`money.py` insists on exactness. The dataset is tiny; stdlib `statistics` covers it.

**LLM provider: Groq**, free tier, OpenAI-compatible — the most widely documented API
shape, so swapping providers later is a one-file change rather than a rewrite. Pick a
model from Groq's current list and **check it documents tool-calling support** — their
catalog changes often.

**Build the agent against a stub first.** A fake model returning a hardcoded tool call tests
all the plumbing — is the tool called, are the arguments right, is the answer formatted — with
zero API calls. It's also the only way to have *tests* for the agent, since tests that hit a
paid API are tests you'll stop running. Then ~20 real calls: ~10 to confirm it reasons
sensibly, ~10 for the demo. This makes the provider choice a one-file change, and it means
the free-vs-paid question (roughly ₹200 of difference) doesn't have to be decided now.

**The number-validation guardrail — build this regardless of provider.** After the model
responds, extract every number from its answer and assert each appears in the tool output it
was given. If a number appears that the tools never produced, **reject the answer**.

~20 lines, and it does three things: makes provider choice low-stakes (a weaker model can't
slip a computed number past you); gives a real measurement — *"the model attempted arithmetic
in 3 of 50 answers; all 3 were caught and rejected"*; and it's a genuinely good "what broke
and how I fixed it" entry, because the answer is a **mechanism, not a prompt tweak**.
Prompting ("never perform arithmetic") is a request. This is enforcement.

**On deploying** — two very different versions. A **static results page** (error-by-horizon
table, calibration, exception list; GitHub Pages, half a day, no backend, no key, nothing to
break at 2am on the 5th) presents exactly what the brief asks for. A **live agent** needs a
server, a key on it, real cost, and it's the version that breaks on submission day. Lean
static; decide near the end.

**Repo:** private during the build, public on submission day. Flipping takes ten seconds and
exposes the whole commit history at once — which is good, since a visible progression from
generator → backtest → calibration reads better than one giant commit. The API key must never
touch the repo: once in git history it's compromised permanently, and a public repo with a
live key gets scraped within minutes. `.env`, gitignored, created in your editor — not typed
into PowerShell, which saves plaintext history to
`$env:APPDATA\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt`.

## 11. What's built, and what the data says

**Foundations + event model + generator complete. 160 tests passing.**

```
1,547 orders · 1,525 captured payments · 283 refunds (132 partial)
4 chargebacks (3 disputing sales from before the window) · 26 outflows

gross captured    ₹23,91,932
− fees + GST      ₹   56,453
− refunds         ₹ 3,08,691
− chargebacks     ₹    3,996
− outflows        ₹20,40,000
opening ₹4,00,000  →  closing ₹3,34,200

tightest day      ₹78,336  (day 57, three days before the sale)
longest stretch below ₹2L   13 days
days below ₹1L                1
days negative                 0
```

### Three properties worth knowing are in the data

**The squeeze is emergent.** The worst day wasn't placed — it fell out of the sale-stock
payment, the monthly supplier bill and the GST due date landing in the same week. The test
asserts it's *near* the sale, not *on* a specific day, because where it lands is a lookup,
not a decision.

```
 49  2026-06-14   247,450   comfortable
 50  2026-06-15   104,946 * sale stock payment lands (₹1.8L)
 57  2026-06-22    78,336 * monthly supplier + GST
 60  2026-06-25   124,203   SALE STARTS — selling 3x, holding ₹1.2L
 65  2026-06-30   305,404   SALE — takings finally arriving (T+1/T+2)
 66  2026-07-01   113,949 * SALE — rent + salaries
 72  2026-07-07   135,143 * post-sale dip + refund wave
 85  2026-07-20   145,306 * supplier again
```

**Sale takings arrive after the sale.** Over 15% of sale-week revenue settles after the sale
ends; the average balance *during* the sale is under 60% of opening. Selling triple while
holding ₹124k. That's "profit isn't cash" as a measurable fact rather than a slogan.

**The refund wave is the half only Bucket 2 can see.** Over 60% of refunds from sale-week
orders take their money out in the fortnight afterwards — exactly when revenue is dipping.

And the calendar doing its job, visible in `balance.csv` without a test:
```
118,2026-08-22, 2461857.97,     0.00, ...  ← Saturday
119,2026-08-23, 2461857.97,     0.00, ...  ← Sunday
120,2026-08-24, 2461857.97, 49467.28, ...  ← Monday, three days' takings
```

### The failure log so far

*(Yours to maintain from here — this is the submission's only question. Record the
**symptom**, not just the fix; the symptom is what makes the story concrete. The valuable
entries are: a wrong mental model that produced correct-looking output, an assumption that
turned out false, a number that looked good for the wrong reason. Not typos.)*

**1. `extra="forbid"` never saw unknown columns.** `from_csv_row` only ever *reads* the
fields it knows about, so a misspelt **optional** column was silently dropped and its default
used instead — data looks fine, forecast quietly built on a missing value. That's the exact
reader/writer drift the class docstring claimed to prevent. Symptom: nothing crashed, nothing
looked wrong; only the test asserting the promise found it.

**2. `orders_per_day = 10` was silently producing 10.7.** The weekday multipliers averaged
1.069, so the config parameter didn't mean what it said. Nothing failed — the data just
quietly wasn't the size specified. Found by dividing order count by days.

**3. A duplicated constant that would have gone stale.** The noise measurement derived mean
order value from the price ladder × a hardcoded `1.25` for baskets. Correct that day, and
silently wrong the first time either the ladder or the basket probability changed — taking
the noise floor with it. Now measured from the data.

**4. Noise: asked for 25%, got 29.9%.** Order *count* varies by 25% and order *values* vary
independently on top; the two compound. Nothing wrong — but the figure that matters
downstream is the revenue one, and quoting the config value would have understated the floor
and flattered the forecaster later. *This is why the noise floor is measured, not declared.*

**5. The outflows were sized by feel and made the business insolvent.** Not a crash — the
generator ran fine, tests passed, `balance.csv` tied perfectly. The arithmetic was correct and
the *scenario* was nonsense: 86 days negative, closing at −₹4.86L. Found by reading the
printed summary, not by any test. The fix wasn't smaller numbers, it was *computing the
target*: monthly net inflow ≈ ₹5.0L → committed outflows ~₹4.55L. There's now a test
asserting outflows stay between 70% and 105% of net income so it can't drift back. **The best
kind of entry: everything internally consistent, premise wrong.**

**6. Three tests failed for the right reason** when generator part 2 landed — one asserted the
refunds file was empty, one that tail cash movement was positive, one compared outflows
against a fixture whose revenue had shrunk without shrinking rent. Stale premises, caught
immediately.

### 28 Aug — the wall and the certain layer

**7. A duplicated filename map, written ten minutes after arguing against exactly that.**
`world.py` got its own `{filename: model}` dict; `EVENT_FILES` already existed in `events.py`.
Nothing failed — both copies were correct — and they would have diverged the first time a file
was renamed. Found by reading the existing tests, not by anything breaking. Notable because
the D1 discussion *was* about this failure mode, in this session, immediately beforehand.

**8. Every day below the floor was flagged, instead of the day it crossed.** First run of the
merchant report marked 11 of 14 days as a breach, seven of them reading "nothing known lands
or leaves". Symptom: a correct report nobody would read to the end. A fortnight below the
floor is one situation, not fourteen.

**9. A breach reported on a day the balance went up.** Standing on day 57 the merchant was
*already* below the floor. Day one had no predecessor, so "was it below yesterday?" answered
no, and a standing position was reported as an event — on a day the balance rose from ₹78,336
to ₹115,677. Nothing crashed, every test passed, the number was right and the warning was
nonsense. Fixed by treating today's balance as day one's predecessor, and by saying the true
thing instead: *"already below the floor today; that is a position, not an event."*
**The best entry so far: correct arithmetic, wrong meaning.**

**10. An explanation rule that failed on the case it was written for.** `reason()` named
movements "biggest first until 80% of the day's flow is covered". Fine when one bill dominates;
useless when twenty similar settlements share a day, because no single one ever reaches the
threshold — so it listed 18 payments. The share test needed a hard cap of three alongside it.

**11. "Horizons 1 and 2 are 100% deterministic" was wrong, and so was the test I wrote to
check it.** The claim (§8.4) came from reasoning about cards at T+2 and ignored UPI at T+1.
Then, correcting it, I asserted the certain layer *"can only under-count"* — reasoning that
missing sales means missing money. **That test failed too.** The one-day blind spot also hides
outflows: a refund requested tomorrow nets off the day after. On 10 of 61 vantage points the
unseen refunds outweigh the unseen sales and the projection comes out too high, clustered in
the fortnight after the sale week.

Two wrong beliefs in a row, from the same habit — reasoning about one mechanism and forgetting
the symmetric one. The fix was not just to correct the assertion but to add a test that pins
the *explanation*: whenever horizon 2 over-counts, unseen refunds must exceed unseen sales. If
the cause ever changes, the write-up's story fails a test rather than quietly becoming untrue.

### 29 Aug — the estimated layer

**12. The weekday average counted today, so one weekday was measured differently from the
other six.** `_weekday_means` counted back from the vantage day itself. Standing on a
Wednesday, the Wednesday baseline was today plus three previous; every other weekday got four
previous. So the forecast for next Wednesday depended on whether you asked on a Tuesday or a
Wednesday — a property of the calendar, not the business. Found by writing the first tests for
`estimate.py`, where four identical ₹10,000 Wednesdays averaged to ₹7,500.

**13. The report described itself as something else.** `run()` flipped the forecast's scenario
label but handed the *original* to `Backtest`, so the header printed "sales-stop (certain layer
only)" above a table of Bucket 2 numbers. Every number correct, the title wrong. For a project
whose entire claim is honest measurement, a report that misdescribes its own contents is worse
than no report.

**14. A number with no unit, read as two different quantities. The best entry so far.**
`Promotion.expected_uplift` had no unit in its name or docstring. The generator set it from the
*volume* knob (3× as many orders); `estimate.py` multiplied *revenue* by it. During a 30%-off
sale, revenue actually goes to 2.1×, so declaring the promotion over-stated income by 43%.

Nothing failed. No test caught it. What surfaced it was the experiment coming out backwards:
**declaring the sale made the overall forecast worse**, +2%, despite helping the sale week by
15%. The reason it was so damaging is that **balances accumulate** — seven over-predicted days
sat in the projected balance for every one of the 658 predictions that followed, so a gain on
98 predictions was swamped by a loss on 658.

Two further things about this one.

*It was planted deliberately and mislabelled.* `generate.py` documented the gap as "plans are
not outcomes" — but the merchant expected 3× volume and got exactly 3× volume, so the plan was
correct. The discrepancy was entirely a unit conversion the forecaster had not been given
enough information to perform. Optimism and a unit mismatch had been conflated, and the wrong
one was written down.

*The fix nearly created a worse problem.* Making the promotion declare volume **and** discount
fixed the conversion — and dropped MAE@14 to ₹25,318, **1.13× the irreducible noise floor**.
That tripped a suspicion threshold written into the scoreboard before any of it ran. The cause
was that the declared plan had been set to exactly the generator's values, so the forecaster
was handed the answer. Corrected by separating what a merchant *decides* from what they
*guess*: the discount is exact (they chose it), the volume uplift is optimistic (3.3× planned,
3.0× actual). MAE@14 settled at ₹29,088, 1.30× the floor.

**And it removed a milestone.** With the perfect declaration, the worst-day metric beat its
trivial baseline for the first time in the project — 75% against 74%. With a realistic one it
does not (72%). That result had been true for about ten minutes and was entirely an artifact.

**15. Days before the shop existed were counted as days it sold nothing.** The growth
estimator compares the last four weeks against the four before them. At vantage day 46 the
older block reaches back 56 days — ten days before the dataset begins — and
`sales.get(day, 0)` returned zero for each of them. So the older block looked far smaller
than it was, and growth looked enormous: **1.9%/day measured against a true 0.26%**, which
compounds over 17.5 days into a **+39% "correction"** at exactly the vantage points with the
least history to check it against.

Symptom: the growth fix made MAE@14 *worse* on the declared run (₹29,088 → ₹32,380) while
improving it on the hidden one. A correction that helps on average and hurts at the far
horizon is the signature of one that has added more variance than it removed bias — which is
what sent me looking.

Fixed by bounding both blocks at the first day of history. Measured growth went from
0.392%/day (range −0.19 to +1.88) to **0.286%/day (range −0.25 to +0.92)** against a true
0.260%, and the sales bias from −4.9% to **+1.1%**.

*A day with no sales inside the window is real information. A day before the shop existed is
not.* Two things that look identical in a dictionary lookup and mean completely different
things — the same class of error as #14, one layer down.

**16. The criterion for choosing the baseline method was itself overfitted.** Four ways of
keeping promotional days out of the weekday baseline were scored, and "exclude the sale plus
the following seven days" was chosen because its residual bias was almost perfectly uniform
across clean and polluted windows — a spread of ₹228, against 1,541 for the nearest rival.
The argument was that a uniform error is correctable and an erratic one is not.

On a **held-out dataset** built specifically to break the assumption — five-day sale,
fourteen-day dip, exclusion window deliberately left at seven — that spread is **₹2,798**,
worse than excluding nothing at all. The ₹228 existed because dataset A's dip happened to be
exactly as long as the window, so the correction happened to be complete.

Not the method overfitted — **the argument for selecting the method.** The subtler version,
and it would have gone unnoticed without deliberately generating data the choice had not been
made on. The method is kept for a different reason that does survive (it leaves the clean-
window bias untouched, so growth stays measurable), and the failed criterion is reported
rather than quietly replaced.

## 12. What this deliberately doesn't handle

Stated up front, because the limits were known before the build rather than discovered during
it. *(This list should grow — a growing limitations section reads as someone paying attention,
not someone making excuses.)*

- **Sales-prediction skill cannot be measured here.** The pattern was chosen when the
  generator was written, so any model that "discovers" it is discovering a choice. What *can*
  be measured honestly is **failure behaviour**: whether the system notices it's wrong,
  quantifies it, and attributes the cause.
- **Chargebacks are unpredictable in principle**, not merely unpredicted — no realistic
  dataset contains enough of them to estimate a rate.
- **One merchant, one bank account, one currency, one gateway.** No multi-account allocation,
  no FX, no marketplace payouts.
- **Bank holidays are a placeholder list**, not the real RBI calendar.
- **Fee assumptions are unverified** — 2% + 18% GST. The structure is modelled, not the rates.
- **Refund carry-forward is not modelled** (§8.7).
- **Real merchants have near-identical daily batch totals** from a few dominant SKUs, so
  amount collisions happen in production and essentially never here.

## 13. Where your input already changed the design

Keep this list — it's the honest answer to "what was your contribution."

1. **You found the actual brief.** The work had been running off a plan document that pointed
   at reconciliation. Reading the real thing moved the entire project.
2. **You asked whether this was already solved.** That reframed "what's different" — and the
   honest answer (§2) is a much better interview position than a claim of novelty.
3. **You pushed back that the sale week is still deterministic.** You were right. It produced
   the best idea in the project: run the backtest twice, hidden vs declared, and report the
   difference as *the value of knowing*. That turned a staged failure into a measured
   experiment.
4. **You asked whether a second payout stream could be added later.** That's why settlement
   rules are a source-keyed table rather than hardcoded.
5. **You proposed a sustained squeeze instead of one-day dips.** A one-day dip is a blip —
   visible two days out, requires no forecasting. A sustained squeeze is a *situation*, and
   it's where a 14-day horizon earns its keep. It also turned out to be emergent rather than
   hand-placed, which is far more convincing.
6. **You objected that separate sales and payments would be cleaner** for refunds. Accepted —
   they're separate, refunds reference an order, which matches how Razorpay models it.

## 14. Schedule

| Date | Work | Status |
|---|---|---|
| Tue 25 Aug | Event model + generator part 1 | ✓ done |
| Wed 26 – Thu 27 Aug | Generator part 2 (travel days, fragments) | ✓ done |
| **Fri 28 Aug** | Finish generator pt 2 + **the wall + leak test** | ← today, pt 2 already done |
| **Sat 29 Aug** | Bucket 1 + backtest + report ← **MILESTONE: submittable** | |
| **Sun 30 Aug** | Estimators + Bucket 2 + certain/estimated split | |
| Mon 31 Aug | — exam — | |
| Tue 1 Sep | Bucket 3: rolling intervals | |
| Wed 2 Sep | Bucket 3: calibration + growth-bias fix | |
| Thu 3 Sep | Agent: tools + question set | |
| Fri 4 Sep | Agent: loop + refusals · **code freeze** · demo video | |
| **Sat 5 Sep** | README + metrics table · **submit** | |

**Sat 29 and Sun 30 are load-bearing** — together they carry everything that earns marks, and
the Capgemini week (1.5-hour days) cannot absorb a slipped weekend. If you can protect only
two days from everything else, protect those two.

**If you're behind, cut in this order:** agent depth → sales-forecasting cleverness → Bucket 3
scenarios. **Never cut:** the backtest harness, the error-by-horizon table, or the
certain/estimated split. Those three *are* the submission.

And the priority in a straight choice: **a calibrated forecaster with a thin agent beats a
slick agent wrapped around an uncalibrated forecaster.** The brief measures accuracy and
honesty; the agent is how you present them.

## 15. Open decisions

1. **Repo name.** README and this folder say `cashcast`; `pyproject.toml` still says
   `razorpay-recon`. Alternatives considered: `cash-position` (uses the brief's own phrase),
   `forward-cash` (literal but flat). Rejected: `float` — the right finance word, but it
   collides with a Python builtin.
2. **Marketplace second payout stream** — stretch goal, only if ahead (§6).
3. **Deployment** — static results page vs live agent (§10).
4. Everything in [your-answers.md](your-answers.md) that isn't filled in yet.
