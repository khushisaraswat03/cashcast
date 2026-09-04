# Scoreboard

One row per layer. Every row is the same 854 predictions, scored the same way, so
rows are comparable to each other and to the rules that do no work at all.

**This is the answer to "are we going in the right direction."** If a layer lands and
its row doesn't improve, that layer didn't earn its place — and the honest response
is to say so in the write-up rather than to keep it because it was hard to build.

Filled in as each layer lands. Nothing here is predicted; every number is measured.

| Layer | MAE @1 | MAE @3 | MAE @7 | MAE @14 | worst day | breach | date |
|---|---|---|---|---|---|---|---|
| **Best rule doing no work** | ₹28,170 | ₹53,003 | ₹57,038 | ₹64,348 | 74% | 56% | — |
| **Bucket 1** — certain layer | **₹0** | **₹13,498** | ₹80,045 | ₹209,914 | 46% | **72%** | 28 Aug |
| **Bucket 1 + 2**, promotion hidden | **₹0** | **₹9,724** | **₹23,610** | **₹43,186** | 69% | **92%** | 29 Aug |
| **Bucket 1 + 2**, promotion declared | **₹0** | **₹8,371** | **₹17,225** | **₹29,088** | 72% | **92%** | 29 Aug |
| Bucket 2, no refunds on forecast sales | ₹0 | — | ₹23,657 | ₹46,297 | — | — | 29 Aug |
| **+ clean baseline + growth fix**, hidden | **₹0** | ₹8,934 | ₹22,707 | ₹38,947 | 74% | 93% | 29 Aug |
| **+ clean baseline + growth fix**, declared | **₹0** | **₹7,840** | **₹17,155** | **₹28,685** | **74%** | **93%** | 29 Aug |

**Where that sits.** At a 14-day horizon the most that can be won is
`₹64,349 (best do-nothing rule) − ₹22,328 (noise floor) = ₹42,021`. The forecast has
won **₹35,664 of it — 84.9% of the closable gap.**

| | MAE @14 | gap closed |
|---|---|---|
| best rule doing no work | ₹64,349 | 0% |
| Bucket 1 alone | ₹209,915 | worse than nothing |
| Bucket 1+2, sale hidden | ₹38,947 | 60% |
| **Bucket 1+2, sale declared** | **₹28,685** | **85%** |
| theoretically perfect | ₹22,328 | 100% |
| + growth-bias fixed | | | | | | | |
| + promotion declared | | | | | | | |
| + Bucket 3 — intervals | n/a | n/a | n/a | n/a | | | |

**Noise floor: ₹22,328 at 14 days.** No forecaster can beat it. We are at ₹42,988 —
**within 1.9× of the theoretical best.**

**Bold** = beats the do-nothing rule.

## How to read it

**Left to right along a row** is the story of the project: near-perfect tomorrow,
progressively less certain out to a fortnight. That decay is the result, not a
weakness — a forecaster that claimed the same accuracy at 14 days as at 1 would be
lying.

**Down a column** is whether the work is paying off. Each layer should move its
column. If it doesn't, that is a finding.

## What Bucket 1 already tells us

It **wins outright at horizons 1–5** and loses from horizon 6 onward. The crossover is
exact: at h=5 it is ₹44,997 against the do-nothing rule's ₹54,930; at h=6 it is
₹62,200 against ₹55,110.

That is not a defect. The certain layer counts fourteen days of committed bills
against roughly two days of incoming money, because sales beyond two days out have
not happened yet and leave no record to count. So it is systematically pessimistic,
and increasingly so with distance.

Which sets Bucket 2's actual target: **it does not need to improve days 1–3 at all.**
Those are already unbeatable. It has to fix everything past day four.

## Targets to beat (write these down before Bucket 2 lands)

Predicting before measuring is the point. If the result lands far from the target,
either the layer under-delivered or the mental model of it was wrong — and both are
worth knowing. Guessing afterwards proves nothing.

Recorded 29 Aug, **before Bucket 2 was written**.

Two predictions were recorded, arrived at by different reasoning, so that whichever
landed closer would say something about the *method* and not just the number.

| | A — interpolated | B — from the mechanism | Reasoning |
|---|---|---|---|
| MAE @14 | **₹137,131** | **~₹55,000** | A: split the difference between Bucket 1 (₹209,914) and the do-nothing rule (₹64,348) · B: the whole ₹209,914 *is* the missing sales, so a working estimator should remove most of it and land near the noise floor plus estimation error |
| Beats the do-nothing rule at 14? | no | just about | ₹137k loses to ₹64,348 · ₹55k wins narrowly |
| MAE @7 | — | ~₹35,000 | half the horizon, less accumulated estimate |
| worst day | — | 60–75% | the path stops falling off a cliff, so the trough lands somewhere real |
| breach | — | roughly flat, 70–80% | already works; less bias may cost as much as it gains |

**What the disagreement is about.** A assumes Bucket 2 removes about a third of the
gap. B assumes it removes most of it, because the ₹209,914 bias *is* the unaccounted
sales — the thing Bucket 2 is built to supply — so what should remain is the noise
floor (₹22,328) plus however badly a four-weekday average estimates a growing,
promotion-disrupted business.

Whichever is closer, something is learned:

- **If it lands near ₹137k**, the estimator is much weaker than its mechanism suggests
  — worth finding out why before adding Bucket 3 on top of it.
- **If it lands near ₹55k**, a crude weekday average was enough, and the remaining
  error is mostly irreducible noise rather than method.
- **If it lands below ₹30k**, be suspicious. That is close to the noise floor, and a
  crude estimator has no business getting there. Check the wall first.

### Result: ₹42,988

**Both predictions were too high.** A by ₹94,143 (3.2x), B by ₹12,011 (1.3x). The
crude estimator worked better than either prediction expected.

What each got wrong:

**A — the method was the problem more than the number.** Averaging two other rules'
scores assumes the answer lies between them, but nothing makes that true; they are
unrelated rules, not the ends of a range. The mechanism had already been measured
(₹18,500 of missing sales per day) and reasoning from it beats interpolating between
landmarks. **The lesson: predict the parts, not the total.** A single number that comes
out wrong teaches nothing; a breakdown says which term was wrong.

**B — and here is that lesson paying off.** The breakdown was ₹25,000 noise + ₹10,500
growth bias + sale week ≈ ₹55,000. The noise term was roughly right. The **growth-bias
term was too big**: a four-weekday average lags a growing business less than estimated,
because the same-weekday spacing puts the average ~11 days back, not ~18. Being wrong
on a *named* term is the point of writing the parts down — the error is diagnosable
rather than just an error.

Neither prediction anticipated that **Bucket 1+2 would beat the do-nothing rule at
every single horizon.** Bucket 1 lost from horizon 6 onward; the estimated layer wins
outright at 1 through 14.

## Finding #2 — what is knowing about a planned sale worth?

The same 854 predictions, twice, identical except whether the merchant told the
system a sale was coming. This answers the objection raised on 24 Aug — that
a sale week we planted ourselves proves nothing about prediction. It doesn't, so this
measures the **value of the information** instead, which is testable on any data.

|  | hidden | declared | change |
|---|---|---|---|
| all 854 predictions | ₹23,920 | ₹17,202 | **−28%** |
| targets in the sale week | ₹36,944 | ₹13,616 | **−63%** |
| targets in the week after | ₹64,457 | ₹25,187 | **−61%** |
| everywhere else | ₹15,942 | ₹16,546 | +4% |
| worst day named | 69% | 72% | |
| breach called | 92% | 92% | |

*(Pooled across horizons, which the accuracy report never does. Defensible only
because both columns are pooled identically and the comparison is like-for-like.)*

**The product recommendation, with a number behind it:** let merchants declare planned
promotions — it cuts sale-week forecast error by 63%, and the fortnight after it by
61%.

Two things worth understanding:

**The week after improves as much as the sale itself.** Balances accumulate, so
getting the sale week's revenue right also fixes the balance entering the following
week — and the refunds flowing out of those sale orders are then correctly scaled too.
The same accumulation that made the units bug so damaging works in reverse here.

**It gets slightly worse everywhere else (+4%).** Small, and honest to report: the
uplift is applied to the declared window, and a declaration that is 10% optimistic
carries that error forward past it.

### The version of this that was too good

The first run of this experiment gave −73% on the sale week and MAE@14 of ₹25,318 —
**1.13× the noise floor**, which tripped the "be suspicious below ₹30,000" rule
written above before any of it was run.

The cause: the merchant's declared plan had been set to exactly the values the
generator uses, so the forecaster was handed the answer rather than an estimate.
Fixed by making the merchant **optimistic about volume but exact about the discount**
— because those are known differently. The discount is a *decision* they made (30%
off, known perfectly, like the dates). The volume response is a *guess* about customer
behaviour, and promotional lift forecasts run optimistic as a rule.

So the merchant plans 3.3× volume and gets 3.0×. Everything else in the dataset is
byte-identical.

That correction cost 10 points of sale-week improvement (73% → 63%) and moved MAE@14
from ₹25,318 to ₹29,088 — **1.30× the floor**, which is believable. It also removed a
result that had briefly looked like a milestone: with the perfect declaration the
worst-day metric beat its trivial baseline for the first time (75% vs 74%). With a
realistic one it does not (72%). **That milestone was an artifact.**

*The 10% over-estimate is an assumption, not a measurement, and should be described
that way.*

## Findings #3 and #4 — two biases pointing opposite ways

Measured before anything was changed, and the sign was a surprise:

```
expected from growth          -6.1%   the four-weekday window looks 17.5 days back
what was actually measured    +7.2%   OVER-predicting
```

**#4 was hiding #3.** The sale week runs seven days, so it covers every weekday, and
once it enters the four-week lookback it inflates all seven baselines by ~19% for a
month — 34 of the 61 vantage points. That +13% swamped the −6% growth lag and
reversed the sign.

You cannot measure the growth bias while the pollution is present. Like standing on
a scale that reads 2 kg light while holding a 4 kg bag: it says you are 2 kg heavy,
and no amount of staring at the number tells you either fact.

**The post-sale dip partly offsets, but nowhere near cancels.** A sale day is 2.06×
normal (+1.06); a dip day is 0.76× (−0.24) — the sale is more than four times further
from normal. And they enter the window a week apart, so a promotion produces a *wave*
in the baseline rather than a bump: sharply up, damped while both are present, then
**below** normal for a week as the sale leaves and the dip lingers, then recovery.

### Results

| | before | after |
|---|---|---|
| sales bias per day | −₹970 (−4.9%) | **+₹224 (+1.1%)** |
| MAE @14, hidden | ₹43,186 | **₹38,947** |
| MAE @14, declared | ₹29,088 | **₹28,685** |
| worst day named | 72% | **74%** — now level with the trivial rule |
| breach called | 92% | **93%** |

**The declared run barely moved, and that is the interesting part.** Its old baseline
was polluted upward by ~13% and lagging downward by ~6%, so two errors were partly
cancelling. Now both are fixed and the residual bias is +1.1% instead of two larger
errors offsetting. Same headline number, right for the right reasons — and the
remaining error is roughly 89% noise rather than bias, so there is little left to
correct.

### The method choice, and the part of it that failed

Four ways to keep promotional days out of the baseline were scored on the same 854
predictions, then re-scored on a **held-out dataset** (5-day sale, 14-day dip) that
the choice was not made on:

| | A: |bias| | A: spread | B: |bias| | B: spread |
|---|---|---|---|---|
| mean, no exclusion | ₹5,639 | 3,251 | ₹5,029 | 2,160 |
| mean, skip sale days | ₹5,243 | 431 | ₹4,996 | 3,433 |
| **mean, skip sale + 7 days** | ₹5,227 | **228** | **₹4,738** | 2,798 |
| median of 4 | **₹5,140** | 1,541 | ₹4,944 | **1,518** |

**The criterion used to choose the method did not survive.** It was picked because
its spread between clean and polluted windows was ₹228 — near-perfect consistency,
and a uniform error is correctable while an erratic one is not. On the held-out set
that spread is **₹2,798**, worse than not excluding anything. The ₹228 was a
coincidence: dataset A's dip is exactly seven days and the exclusion window is
exactly seven days, so the correction happened to be complete.

The median's spread is 1,541 on A and 1,518 on B — never best, and identical on data
it has never seen. **That is what robustness looks like**, and it is not what was
chosen.

Exclusion is kept for the one reason that did survive: on windows with no promotion
nearby it excludes nothing, so the growth bias passes through unchanged and stays
measurable. The median adds its own downward bias to clean windows (−₹1,814 on A),
which would have contaminated the growth correction. On absolute error the four
methods are within ~6% of each other and trade places — there is no decisive winner
and claiming one would be reading noise.

*Not overfitting the method. Overfitting the argument for choosing it.*

## Finding #7 — can it see the squeeze coming?

The squeeze around days 60–86 was never placed. It emerges from the post-sale dip,
the refund wave from sale-week orders, and the supplier payment for the sale stock.
The actual trough is **day 66 at ₹113,949**, and 19 days in that stretch sit under ₹2L.

Standing **mid-sale on day 64**, holding ₹1.24L while selling triple, forecasting the
next fortnight:

| layer | trough predicted | actual | miss | trough day |
|---|---|---|---|---|
| certain only | −₹71,158 below | ₹113,949 | **−₹71,158** | 07-07 ✗ |
| + sales, no refunds | ₹120,319 | ₹113,949 | +₹6,370 | **07-01 ✓** |
| + sales + refunds | ₹117,185 | ₹113,949 | **+₹3,236** | **07-01 ✓** |

**From two days into the sale onward, the estimated layer names the right trough day
and gets the level within ₹3,000–17,000. The certain layer never does** — it puts the
trough on 07-07 at every vantage point, because its balance only ever falls and it is
naming the end of its own slide rather than a real dip.

### And the stated mechanism was wrong

The plan said (design-log §9, 24 Aug):

> *"Bucket 1 cannot. Bucket 2 should, because the refund wave is predictable from
> sales already in the books."*

Measured, standing on day 64 over the next 14 days:

```
refunds already requested (the certain layer sees these)      ₹3,228
refunds the estimated layer projects on top                  ₹63,681
settlements from sales that have not happened yet           ₹239,887
```

**The sales half does roughly three quarters of the work, not the refund wave.**
Adding refunds takes the miss from ₹6,370 to ₹3,236 — it halves the residual, which
is a real contribution, but the correction from −₹71,158 to +₹6,370 is almost
entirely "the shop keeps selling."

The refund half is still the more interesting *idea* — 95% of that fortnight's refund
outflow (₹60,453 of ₹63,681) is invisible to the certain layer, because those refunds
have not been requested yet. It is just not what rescues the number.

*Claimed since 24 Aug, measured on 30 Aug, and the claim was three-quarters wrong.*

## Bucket 3 — calibration, 30 Aug

Bucket 3 does not move MAE. It adds a range around the same number, and then checks
that the range is honest.

**Claimed 80%. Measured 77% across 714 banded predictions.** Three points off, which
is about as close as this can get with 51 samples per horizon.

| h | inside | band width | verdict |
|---|---|---|---|
| 1 | 100% | ₹0 | exact — no uncertainty to report |
| 2 | 65% | ₹12,246 | too narrow |
| 3 | 78% | ₹23,554 | honest |
| 5 | 80% | ₹39,795 | honest |
| 7 | 78% | ₹51,619 | honest |
| 10 | 76% | ₹71,558 | honest |
| 12 | 73% | ₹76,666 | too narrow |
| 14 | 69% | ₹79,235 | too narrow |

**The bands are honest in the middle and slightly too narrow at both ends.** Being
too narrow means overconfident — the forecast promises more precision than it has.
At 14 days the truth lands inside 69% of the time against a claimed 80%.

That is the *expected* failure mode and the one predicted in advance. It happens
because the far horizons have the fewest independent errors to learn from: 51
samples, and the errors at 14 days are much more spread out than at 3.

**No band is offered until 10 past vantage points exist.** The first ten windows get
nothing rather than a guess — reporting a confidence interval before you have any
evidence about your own accuracy is false precision, and it would also have inflated
this calibration number for free.

**Rolling, never pooled.** The band at vantage point V uses only errors from vantage
points before V. Computing bands from all 854 errors and applying them to the first
forecast would use the future to calibrate the past, which is the wall again one
level up.

### Two parameters, set rather than derived

- **80% band.** Easier to calibrate credibly than 95%, and it fails visibly — a 95%
  band is breached so rarely that 61 vantage points cannot tell a good one from a bad
  one.
- **10 vantage points minimum.** Enough for the 10th and 90th percentile to sit
  between real observations rather than being the two most extreme errors seen so far.
