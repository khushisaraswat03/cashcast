# Your answers — the questions to answer in your own words

Two kinds of question here, and both are yours, not mine.

**Part A** — decisions still to be made. Each one blocks a specific day's work. Answer them
before that day, not during it.

**Part B** — questions you'll be asked about decisions already made. The reasoning is in
[design-log.md](design-log.md), but **do not copy from it.** Write each answer the way you'd
say it out loud to someone who hasn't seen the project. If you can't fill in a "why" line,
that's the signal to stop and ask me — it means the decision isn't actually yours yet.

The test is simple: someone asks *"why 14 days?"* and you answer from memory, not from a
file I wrote.

---

# Part A — decisions still open

## Fri 28 Aug — `world.py`, the temporal wall

Mostly mechanical, no big forks. One worth thinking about:

**A1. When the leak test fails the first time, what do you want it to tell you?**
A bare "outputs differ" is useless at 11pm. Naming which day and which field diverged costs
ten minutes now and saves an hour later.

> **My answer:** ✅ *decided 28 Aug* — name the day that diverged and by how much.
>
> **Why:**

## Sat 29 Aug — Bucket 1 forecaster, backtest, report

**A2. Does the forecast output the balance only, or also a "you breach zero on day X" flag?**
The flag is the thing that changes a decision; a balance alone is a dashboard. It also gives
you asymmetric errors to talk about — a missed breach means bounced payroll, a false alarm
means a wasted phone call.

> **My answer:** ✅ *decided 28 Aug* — neither on its own. The forecaster answers the
> question asked ("what will the balance be on day X") **and walks every day between the
> vantage day and day X, surfacing each critical day with its cause.** So the output is a
> *path with its worst moments named*, not a single endpoint. The answer can be "yes, you'll
> have ₹2.4L on the 60th" while day 57 sits at ₹78,336 — and answering only the endpoint
> would hide that.
>
> **Why:**
>
> *Sub-decision, settled 28 Aug — two kinds of critical day:*
>
> 1. **The trough** — the single worst day in the window, always flagged even if comfortable.
> 2. **At risk** — the uncertainty band's lower edge breaches the floor even though the
>    central estimate doesn't. A warning no point forecast can produce. Needs Bucket 3
>    (Tue 1 Sep), but the report structure gets a slot for it from day one.
>
> *The **floor is derived, not chosen**: the largest committed outflow still due in the
> window — usually payroll or the monthly supplier bill. So the warning reads "your band
> bottoms at ₹40,000 on the 11th and salaries of ₹1.6L are due on the 12th." No arbitrary
> number to defend: "why ₹1L?" has no good answer, "because that's what you owe on the 12th"
> does.*
>
> *A chart is deferred, not rejected: the forecast is emitted as a data structure and the
> text report is a thin renderer over it, so a fan chart becomes a second renderer with no
> rework — Wed 2 Sep, if there's time. Presentation, not measurement.*

**A3. What is your headline error metric — mean absolute error, or % of days predicted within
₹X?** They say different things. MAE is sensitive to a few big misses (which you *will* have,
around the sale week). "Within ₹X" is more intuitive for a controller but you have to justify
X. You can report both, but you have to *lead* with one.

> **My answer:** ✅ *decided 28 Aug* — three columns per horizon, no pooling across horizons:
>
> | | measures | why it's there |
> |---|---|---|
> | **MAE** | average size of the miss | the only one comparable to the ₹22,328 noise floor |
> | **median** | the typical miss | the MAE/median *gap* reveals the sale-week outliers for free |
> | **trough called right** | did we name the worst day correctly | measures what the product actually ships |
>
> "% within ₹X" dropped — it needed an arbitrary threshold to defend, and "trough called
> right" replaces it with one the data sets.
>
> **Why:**

**A4. Do you report error in rupees, or as a % of that day's balance?** Rupees is concrete;
percentage is comparable across a growing business. The business grows ~30% over the window,
so a constant rupee error looks like improving accuracy in percentage terms — and vice versa.

> **My answer:**
>
> **Why:**

## Sun 30 Aug — estimators and Bucket 2

**A5. Average how many same-weekdays — 4, 6, or weighted toward recent?**
Four is the default. More weeks = less noise but more lag behind a growing business. Weighting
recent weeks is the fix for growth bias, but doing it *now* means you never get to show the
before-and-after (finding #3).

> **My answer:**
>
> **Why:**

**A6. Do *predicted* sales also generate predicted refunds, or only actual past sales?**
A genuine modelling choice, defensible either way. Only-past-sales is cleaner and covers most
of a 14-day window because of the refund lag. Including predicted sales compounds one estimate
on top of another — more complete, more error.

> **My answer:**
>
> **Why:**

**A7. How is the certain/estimated split actually reported?** Per day as a percentage? As two
separate rupee lines? A single chart across horizons? This is your best section, so decide how
it looks rather than letting it fall out of the code.

> **My answer:**
>
> **Why:**

## Tue 1 – Wed 2 Sep — Bucket 3

**A8. 80% band or 90%?** 80% is easier to calibrate credibly and fails visibly when wrong.
90% is what finance people expect. Whichever you choose, you're committing to be inside it
that often.

> **My answer:**
>
> **Why:**

**A9. Rolling window or split-sample?** Rolling is the better answer (§8.5) and it's already
the plan — but be able to say why the naive version is *wrong*, not just different: computing
intervals from all 854 errors and applying them to forecast #1 is using the future to
calibrate the past.

> **My answer:**
>
> **Why:**

**A10. Which named what-ifs do you ship?** "A chargeback lands next week." "The big
settlement is delayed a day." "A supplier payment moves forward." Pick two or three that a
real controller would actually ask.

> **My answer:**
>
> **Why:**

## Thu 3 – Fri 4 Sep — the agent

**A11. Which questions must the agent answer?** Fix the list *before* you build it, or you'll
tune the agent to whatever it happens to do well.

> **My answer:**
>
> **Why:**

**A12. What does it refuse, and how does it say so?** **This is where the marks are.** Refusing
well is the direct answer to "verification capacity is the bottleneck." Candidates: anything
requiring arithmetic it wasn't handed, anything beyond the 14-day horizon, anything about a
merchant/account not in the data, anything asking it to predict a chargeback.

> **My answer:**
>
> **Why:**

**A13. What happens when the number-validation guardrail fires?** Silently retry, show the
refusal to the user, or log and fall back to a template answer? Whichever you pick, the count
of firings is a number you report.

> **My answer:**
>
> **Why:**

## Sat 5 Sep — packaging

**A14. What broke and how did you fix it?** *The only question on the submission form.* Pick
your three best entries from the failure log. The strongest ones are: a wrong mental model
that produced correct-looking output · an assumption that turned out false · a number that
looked good for the wrong reason. Not typos.

> **My three:**
> 1.
> 2.
> 3.
>
> **Why these three:**

## Standing decisions, no fixed date

**A15. Repo name** — `cashcast`, `cash-position`, or `forward-cash`? (§15)

> **My answer:**

**A16. Deploy a static results page, a live agent, or nothing?** Decide near the end, but know
the trade: the static page has nothing to break at 2am on the 5th and presents exactly what
the brief asks for.

> **My answer:**
>
> **Why:**

**A17. Marketplace second payout stream — in or out?** Only if genuinely ahead after Sun 30
Aug. Its real argument is that a T+10 payout puts *certain* content at horizon 10, which
cards and UPI cannot.

> **My answer:**
>
> **Why:**

**A18. Should the merchant be able to declare the expected post-sale dip too?**
*Parked 29 Aug — revisit if ahead, or when writing up the promotion finding.*

Decided for now: **no.** A merchant planning a sale announces the sale; they do not
announce "and then it will be quiet for a week." So the declared run should still miss the
week after the promotion, which is honest and gives a second finding rather than a flaw.

The variant would be a third backtest run measuring *"what is knowing about the aftermath
worth?"* — distinct from the promotion experiment, which measures what knowing about the
sale itself is worth.

Worth remembering when it comes up: the post-sale cash trough has **two independent
causes**, and only one of them is behavioural.

- **Demand pull-forward** — customers who would have bought later bought during the sale.
  A well-established retail effect, strongest for goods that satisfy a need for a while
  (clothing, electronics) and weakest for things people rebuy regardless. But the
  generator's specific 0.8× for one week is an assumption, not a measurement, and should
  be described that way.
- **The refund wave** — sale-week orders generate sale-week-sized refunds about a week
  later, arriving exactly when sales are already soft. Purely arithmetic, certain, and
  independent of whether the demand dip exists at all.

**Even with zero demand dip, the cash would still dip.** Only the second cause can be
defended without a citation — and it is the half Bucket 2 can see coming, since it projects
from orders already in the books. That is finding #7.

> **My answer:**
>
> **Why:**

---

# Part B — questions on decisions already made

Answer from memory. Check against [design-log.md](design-log.md) *afterwards*, not before.

### The project

**B1. What are you building, in three sentences, to someone who knows nothing about payments?**

>

**B2. Why the cash forecaster and not reconciliation, Q&A, or tax matching?**

>

**B3. Has this been solved already? What are you doing differently?**
(The honest answer is stronger than a novelty claim. Know who Recko was.)

>

**B4. Who is this for, and what decision does it change for them?**

>

### The method

**B5. How can you forecast a refund when you don't know who'll ask for one?**

>

**B6. What are the three buckets, and why is a raised-but-undebited chargeback in Bucket 1
rather than Bucket 3?**

>

**B7. Why 14 days? What breaks at 7, and what breaks at 60?**

>

**B8. Why ~10 orders a day? Why not 1,000?**

>

**B9. What are 61 vantage points and 854 measured forecasts? Why not just report one number?**

>

**B10. Why is the warm-up 45 days and not 28?**
(Two constraints, and the second one — complete refund histories — is the non-obvious one.)

>

### The architecture

**B11. Why does every event carry two dates?**

>

**B12. What is the wall, and how do you *prove* the forecaster can't see the future?**

>

**B13. Why are horizons 1 and 2 exactly 100% deterministic? Not "mostly" — exactly.**

>

**B14. Why are settlements derived rather than stored? What does that cost you?**

>

**B15. Why does a forecast day carry provenance instead of just a number?**

>

### The judgment calls

**B16. Why no machine learning?**
(Two separate arguments: it would be *untestable* on self-generated data, and an unexplainable
number can't be signed off in finance.)

>

**B17. Why doesn't the model do arithmetic — and how do you enforce that rather than just ask
for it?**

>

**B18. Why is money an integer number of paise?**

>

**B19. Why no pandas?**
(The real reason is specific, not aesthetic.)

>

**B20. You planted the sale week yourself. So what does missing it prove?**
*(This is your own objection, and the answer is the best idea in the project. Know it cold.)*

>

**B21. How much of your accuracy is just money you'd already collected?**
*(The question that evaporates an unearned metric. You should be able to answer it with a
number.)*

>

**B22. What's the irreducible error, and how close are you to it?**

>

**B23. What can this system NOT detect, even in principle?**

>

---

## The two logs to keep from here

**Decisions log** — ten minutes a day, in your words:

```
Day 3 — forecast output shape
Chose: forecast the balance AND a "will you breach zero" flag
Why: the flag is the thing that changes a decision; the balance alone is a dashboard
Rejected: balance only (too passive), full cash-flow statement (out of scope)
```

**Failure log** — separate, and it's the submission. Record the **symptom**, not just the fix:

```
25 Aug — settlement dates off by one
Symptom: money appearing on Saturdays in the forecast
Cause: counted calendar days instead of working days
Found by: the test that asserts a Thursday capture lands Monday
Fix: add_working_days() counts from the day after, skipping weekends and holidays
```

Six entries are already recorded in [design-log.md §11](design-log.md) — those are mine,
written up from what happened. Everything from `world.py` onward is yours to write on the day
it happens, because you will not remember them on the 5th.

Four are near-certain to fire, so watch for them:

- the **leak test** will probably fail the first time
- **calibration** will probably come out too narrow (you say 80%, truth lands inside ~55%)
- the **weekday baseline** will be biased low by growth
- the **sale week** will poison the baseline for a month afterwards

Each one is "here's what broke, here's how I found it, here's what I changed" — with a number
before and after. And the reason you found them is that you built the detector.
