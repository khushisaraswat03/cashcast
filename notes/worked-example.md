# Phase 0 — Worked Example

Assumptions used throughout: fee 2% of transaction, GST 18% **of the fee**, fees computed and
rounded per transaction to 2 dp, T+2 **working** days, refunds and chargebacks netted off the
next settlement at **gross** (the gateway does not return the original fee on a refund).
Calendar: 24 Aug 2026 = Monday, 29–30 Aug = weekend, no bank holidays in the window.

---

## 1. The three-worlds model

**World 1** is my system: orders and payments as *I* believe them to be. **World 2** is the
gateway: what Razorpay actually collected, refunded, disputed and settled, with its own IDs,
its own amounts (net of fees) and its own timing. **World 3** is the bank statement: opaque
credit lines with a UTR and a truncated narration.

Reconciliation is proving a World 3 line corresponds to World 1 facts, **using World 2 as the
bridge** — never matching World 1 to World 3 directly, because the amounts and dates there
will never agree.

## 2. The settlement formula (from memory)

```
+ captured payments in this batch
− fees on those payments                (2% per txn, rounded)
− GST on those fees                     (18% of the fee)
− refunds processed since last settlement
− chargebacks / disputes debited
± adjustments (reserves held, holds released)
──────────────────────────────────────────────
= net amount credited to my bank
```

Every reconciliation check I write is a test of this identity. When a number doesn't tie, one
of these six lines is the reason.

---

## Exercise 1 — the base case *(given)*

Three card payments, Mon 24 Aug: ₹1,000, ₹2,000, ₹500.

| Amount | Fee (2%) | GST (18% of fee) | Net |
|---|---|---|---|
| 1,000.00 | 20.00 | 3.60 | 976.40 |
| 2,000.00 | 40.00 | 7.20 | 1,952.80 |
| 500.00 | 10.00 | 1.80 | 488.20 |
| **3,500.00** | **70.00** | **12.60** | **3,417.40** |

Settles Wed 26 Aug. Bank shows one line, `CREDIT 3,417.40` — **three orders, one bank line.**
Many-to-one is the normal case, not the exception.

---

## Exercise 2 — the rounding trap

Three payments of ₹132 each, same day.

**(a) Per-transaction nets**

```
fee = 2% × 132        = 2.64          (exact, no rounding needed)
gst = 18% × 2.64      = 0.4752  →  0.48   (rounded to 2 dp)
net = 132 − 2.64 − 0.48                = 128.88
per-transaction total = 128.88 × 3     = 386.64
```

**(b) Applied to the batch total of ₹396**

```
fee = 2% × 396        = 7.92
gst = 18% × 7.92      = 1.4256  →  1.43
net = 396 − 7.92 − 1.43                = 386.65
```

**(c) Do they agree?** No. **386.64 vs 386.65 — they differ by ₹0.01.**

The cause: each transaction's GST is rounded *up* by ₹0.0048 (0.4752 → 0.48), so per-transaction
rounding over-charges by 3 × 0.0048 = ₹0.0144, which surfaces as a 1-paisa shortfall against the
batch calculation. Neither number is "wrong" — they are answers to two different questions. Only
(a) matches what the bank will actually credit, because the gateway rounds per transaction.

**(d) What this tells me about tolerance**

Exact equality is a bug, not a feature. Worst-case drift is ₹0.005 per transaction, so it grows
with batch size: 200 transactions can drift a full rupee.

> My rule: **tolerance = max(₹1.00, ₹0.01 × transaction_count)**, and it is *always* applied
> against the per-transaction sum, never the batch-level shortcut.

The corollary matters more than the number: a ₹0.01 gap is rounding, a ₹300.00 gap is a missing
component of the formula. The tolerance must stay small enough that it can never swallow a real
refund or chargeback.

---

## Exercise 3 — the weekend

**(a)** Captured **Thu 27 Aug**. T+1 = Fri 28, T+2 = **Mon 31 Aug** (Sat/Sun are not working
days). Settles **Monday 31 August**.

**(b) 4 calendar days** (27 → 31). "T+2" measured in the wrong unit is off by 100%.

**(c)** Captured **Mon 31 Aug**. T+1 = Tue 1 Sep, T+2 = **Wed 2 Sep**.

**(d) Which does a "capture date + 2 calendar days" matcher miss?**

| Captured | +2 calendar days | Actual settlement | Result |
|---|---|---|---|
| Thu 27 Aug | Sat 29 Aug | Mon 31 Aug | ❌ **missed** |
| Mon 31 Aug | Wed 2 Sep | Wed 2 Sep | ✅ found |

It misses the **Thursday** payment. Generalising, the naive rule breaks for every **Thursday and
Friday** capture (Friday: +2 cal = Sun 30, actual = Tue 1 Sep), and every capture before a bank
holiday. It happens to work Mon–Wed, which is exactly what makes it dangerous — it passes a
casual test and fails 40% of the week in production.

**Design consequence:** search a **window**, not a date. I use **T+1 to T+5 calendar days**,
which absorbs one weekend plus one bank holiday, and I rank candidates within the window rather
than requiring a single expected date.

---

## Exercise 4 — a refund lands in a later batch

Timeline:

```
Mon 24 Aug  capture ₹1,000, ₹2,000            → batch settling Wed 26
Wed 26 Aug  settlement lands in bank
Wed 26 Aug  capture ₹1,500                    → batch settling Fri 28
Thu 27 Aug  refund ₹300 against the ₹1,000
Fri 28 Aug  settlement lands in bank
```

**(a) Wednesday's credit** — Monday's two payments only:

```
976.40 + 1,952.80 = 2,929.20
```

The refund does not exist yet on Wednesday, and even once it does it cannot touch a settlement
that has already left the gateway.

**(b) Friday's credit** — Wednesday's payment, minus the refund netted off:

```
₹1,500 → fee 30.00, gst 5.40 → net 1,464.60
less refund R1 (gross)                −300.00
                                    ──────────
                                     1,164.60
```

The refund is deducted at its **gross** ₹300 — the ₹20 fee on the original ₹1,000 is not returned
to me. So a ₹300 refund costs the business ₹300, and the ₹23.60 originally paid in fee and GST is
simply gone.

**(c) What I'd wrongly conclude without knowing about the refund**

I'd see ₹1,464.60 expected against ₹1,164.60 received and reason backwards from the shortfall:
"the effective fee is 22.36%, not 2%", or "the payment was partially captured", or "the gateway
short-paid us." The worst outcome isn't the wrong theory — it's a **fuzzy matcher searching for
something that sums to 1,164.60** and force-fitting an unrelated payment to close the gap,
producing a false match that then corrupts the following day too.

This is the general lesson: **a settlement is not a function of one day's captures.** It carries
debits from events that happened after those captures were taken, against payments from earlier
batches. The refund must be modelled as a first-class entity with its own date and its own
settlement linkage, not as a modification of the original payment.

---

## Exercise 5 — a chargeback from another month

₹450 chargeback raised Thu 27 Aug against a payment captured 12 July and settled in July.

**(a) Which settlement absorbs it?** The **next settlement after the debit date** — here the
**Fri 28 Aug** batch. The chargeback attaches to the settlement cycle in which the gateway
*debited* it, which has nothing to do with when the disputed payment was captured or settled.

**(b) How to explain ₹450 leaving August without breaking the August reconciliation**

By reconciling **per settlement, not per payment-month.** The August recon does not need an
August payment for every rupee — it needs the settlement identity to hold:

```
bank credit = Σ nets of payments in this settlement
              − refunds in this cycle
              − chargebacks in this cycle
              ± adjustments
```

So C1 is recorded as its own entity — `C1, ₹450, debited 28 Aug, disputes payment PJ-0712
(captured 12 Jul, settled 15 Jul)` — and appears in the Friday settlement as a **chargeback
component**, sourced from the gateway's settlement report, not inferred. It is fully explained
and August ties exactly.

The structural requirement: my model needs **two dates on every adjustment** — the date of the
*underlying* transaction and the date it *hit a settlement*. Reconciliation uses the second; the
dispute analytics use the first. Collapsing them into one date is what makes cross-month items
look unexplainable.

**(c) Why I can't "put it back in July"**

Three independent reasons, any one of which is sufficient:

1. **July was true and is proved.** July's bank credit really did arrive in full; no July line
   changes. Restating a closed, tied period to accommodate a later event destroys the invariant
   that a reconciled period stays reconciled and reproducible.
2. **The cash moved in August.** Reconciliation is a statement about cash movement against a bank
   statement, and the bank statement says 28 August. The money physically left the account in
   August; no bookkeeping opinion changes that.
3. **The accounting period is closed.** July is filed — GST returns, management accounts. Reopening
   it to move ₹450 is a restatement, not a correction.

The accrual question ("which month's revenue was really wrong?") is a *separate* and legitimate
question, answered with a provision or a P&L adjustment referencing the July payment. It is not
answered by editing the July reconciliation. **Cash reconciliation is by event date; revenue
attribution is by transaction date. Two different books, deliberately.**

---

## Exercise 6 — a partial settlement

₹10,000 scheduled, live balance supports ₹6,000, so a subset totalling ₹6,000 net is settled.

**(a) What goes wrong if the matcher assumes "this settlement contains all payments from day D"**

A cascade, not a single error:

- It computes an expected ₹10,000 against an actual ₹6,000 and reports a **₹4,000 unexplained
  variance** — a false break, since nothing is actually wrong.
- Worse, if it resolves the variance by marking every day-D payment as settled, it **double-counts
  the ₹4,000 remainder** when that genuinely settles a day or two later. The second settlement
  then looks like an unexpected credit with no payments behind it, and the error propagates.
- A fuzzy matcher may instead force-fit the ₹6,000 to a *different* day's payments that happen to
  sum near ₹6,000 — a plausible, confidently-wrong match that is far harder to find than an
  honest unmatched line.

The root cause is **inferring membership from dates.** Date is a heuristic for generating
candidates; it is never evidence of what a settlement contains.

**(b) Where the list of included payments actually comes from**

From **World 2 — the gateway's own settlement report**, keyed by `settlement_id`: Razorpay's
settlement recon report / `settlement.recon` API returns the exact transaction list, with the fee,
tax and net per line and any refunds/adjustments in that batch. That is authoritative; my inference
is not. This is precisely what "World 2 is the bridge" means operationally: I match
`bank line → settlement_id → payment list → my orders`, and the middle arrow is *looked up*,
never computed.

**(c) What happens to the remaining transactions**

Nothing bad — they simply were not settled. They stay in the gateway's balance as
**captured-but-unsettled** and roll into the next cycle (possibly as another partial settlement).
In my system they must remain in an explicit `captured, awaiting settlement` state, not
`overdue` or `missing`. That state needs an **age**, so a payment unsettled for 2 days is normal
and one unsettled for 15 days is an alert — the same fact, different meaning over time.

---

## Exercise 7 — the capstone

### (a) Which payments each bank line corresponds to

Expected nets:

| ID | Captured | Gross | Fee | GST | Net | Settles (T+2 wd) |
|---|---|---|---|---|---|---|
| P1 | Mon 24 | 1,000 | 20.00 | 3.60 | 976.40 | Wed 26 |
| P2 | Mon 24 | 2,000 | 40.00 | 7.20 | 1,952.80 | Wed 26 |
| P3 | Tue 25 | 500 | 10.00 | 1.80 | 488.20 | Thu 27 |
| P4 | Tue 25 | 1,500 | — | — | — | **never (failed)** |
| P5 | Wed 26 | 800 | 16.00 | 2.88 | 781.12 | Fri 28 |
| P6 | Wed 26 | 800 | 16.00 | 2.88 | 781.12 | Fri 28 |

| Bank line | Amount | Explanation | Ties? |
|---|---|---|---|
| Wed 26, UTR8891 | 2,929.20 | P1 + P2 → 976.40 + 1,952.80 | ✅ exact |
| Thu 27, UTR8934 | 488.20 | P3 alone (one payment, one line — coincidence, not a rule) | ✅ exact |
| Fri 28, UTR9002 | 812.24 | P5 + P6 − R1 − C1 → 1,562.24 − 300.00 − 450.00 | ✅ exact |
| Fri 28, IMPS4471 | 1,200.00 | **not a gateway settlement** — see (c) | n/a |

The Friday line is the one that teaches the lesson: **₹812.24 is smaller than either single
payment in it.** Any matcher that assumes a settlement ≥ its largest constituent payment, or that
tries to find payments summing to 812.24, fails here. The only way through is to compute the full
formula — captures *minus refunds minus chargebacks* — and the only way to know which refunds and
chargebacks belong to this batch is to read the settlement report.

### (b) Accounting for the entire ₹870.36 difference

```
Total gross captured (P1,P2,P3,P5,P6 — P4 excluded, it failed)    5,100.00
Total credited by the gateway (2,929.20 + 488.20 + 812.24)        4,229.64
                                                                ───────────
Difference to explain                                               870.36
```

Line by line, straight down the settlement formula:

| Component | Working | Amount |
|---|---|---|
| Fees (2% per txn) | 20.00 + 40.00 + 10.00 + 16.00 + 16.00 | 102.00 |
| GST (18% of each fee) | 3.60 + 7.20 + 1.80 + 2.88 + 2.88 | 18.36 |
| Refund R1 (gross, against P1) | | 300.00 |
| Chargeback C1 (July payment, debited 27 Aug) | | 450.00 |
| Adjustments / reserves | none this period | 0.00 |
| **Total** | | **870.36** |

**870.36 = 870.36 ✅ ties exactly, to the paisa.**

Two checks worth noting. Fees happen to be exact here (2% of every amount lands on whole paisa)
so there is *no* rounding residue — which is why the tie is exact rather than within tolerance;
Exercise 2's ₹132 case is the one that needs the tolerance. And 102.00 + 18.36 = 120.36 is only
**2.36% of gross** — the fees are the small part. The refund and chargeback are ₹750 of the
₹870.36, i.e. **86% of the gap is business events, not pricing.** If I only modelled fees and GST
I'd explain 14% of the difference and call the rest "unexplained variance."

### (c) The bank line gateway activity cannot explain

**`Fri 28 Aug — IMPS-XXXXXX4471-CUSTOMER PAYMENT — 1,200.00`.**

It's a direct customer bank transfer, not a settlement: `IMPS` rather than `NEFT`, no
`RAZORPAY`, no `SETTL`, no UTR in the settlement format, and — decisively — no settlement in
World 2 for ₹1,200 on any nearby date.

What the system must do: **classify it out of scope and leave it alone.** Concretely:

1. **Classify before matching.** A credit is only a settlement candidate if it passes a gateway
   filter (narration pattern + a matching settlement in World 2). Do this *first*, so
   non-settlement lines never enter the matching pool at all.
2. **Give it a real terminal state** — `out_of_scope: non-gateway credit` — distinct from
   `unmatched`. This is the crux: `unmatched` means "I failed", `out_of_scope` means "correctly
   excluded". Conflating them means either the recon never reaches 100% (and everyone learns to
   ignore the exception report), or I suppress the difference and lose the ability to detect a
   settlement I genuinely missed.
3. **Never force-fit.** Without this state, a fuzzy matcher will hunt for something near ₹1,200 —
   and ₹1,164.60-shaped combinations are easy to manufacture. A false match here is worse than
   no match, because it silently marks real payments as settled.

The honest framing: **not every credit is mine to reconcile, and the ability to say "not mine"
is a feature.** A matcher with no exit hatch has a 100% match rate and zero credibility.

### (d) P6, the duplicate — *does it reconcile? is it correct?*

**Does it reconcile? Yes — perfectly.** P6 is a real captured payment; the gateway really
collected ₹800; ₹781.12 really arrived; the Friday line ties to the paisa. Every check I have
passes. There is no tolerance breach, no unmatched line, no variance. A perfect reconciliation.

**Is it correct? No.** One order, one delivery, one obligation — and the customer was charged
twice. ₹800 of that money isn't revenue, it's a liability: money owed back to a customer who
hasn't noticed yet.

**The gap between those two answers is the whole point.** Reconciliation answers *"is the money
that moved accounted for?"* It cannot answer *"should the money have moved?"* Those are different
classes of control:

| Control | Question | Detects |
|---|---|---|
| Reconciliation | is every rupee that moved explained? | missing money, unexplained credits, formula breaks |
| Validity controls | should this transaction have existed? | duplicates, wrong amounts, fraud, unauthorised charges |

**A fully reconciled ledger can be completely wrong**, as long as it's wrong *consistently* across
all three worlds. Reconciliation is an internal-consistency proof: World 3 agrees with World 2
agrees with World 1. It says nothing about whether World 1 reflects what *should* have happened.
Every error that was faithfully recorded everywhere is invisible to it — duplicates, an agreed
wrong price, a fraudulent-but-genuinely-captured payment. Reconciliation is necessary and nowhere
near sufficient.

So P6 needs a **separate detection layer**, not a better matcher: flag ≥2 successful payments on
the same order (or same customer + same amount within a short window) and route to refund. That
control lives beside reconciliation, keyed on World 1 semantics — order identity — which
reconciliation never looks at, because it only ever compares amounts and dates.

And note the second-order effect: once P6 is refunded, the ₹800 gets netted off a *later*
settlement, making that day's credit unexpectedly small — Exercise 4's shape again. **The
correction is itself a reconciliation event.** Fixing the business error creates the next
anomaly, which is why refunds must be modelled as first-class objects rather than reversals of
the original payment.

### (e) P4 failed — where does it appear?

**Nowhere in World 3, and nowhere in World 2's settlements.** A failed payment never collected
money, so there is nothing to settle. It exists only as an *attempt* in World 1 and World 2.

What the matcher should do:

1. **Exclude it from the expected-settlement universe entirely.** Only `captured` payments can
   settle. If failed payments are in the expected set, ₹1,500 of phantom money is permanently
   "missing" and the recon never balances — note that (b) already ties at ₹5,100 gross precisely
   *because* P4 is excluded. Include it and I'm chasing a ₹1,500 hole that does not exist.
2. **Never mark it unsettled or overdue.** `failed` is terminal, not pending. It should never
   appear in an ageing report or an exceptions queue.
3. **Don't discard it — route it to a different check.** P4 means someone tried to pay ₹1,500 and
   couldn't: the *order* may still be unpaid. That's a real problem, owned by an
   **unpaid/abandoned-order report**, not by reconciliation. Reconciliation's answer is simply
   "not my scope" — the same discipline as the IMPS line in (c).
4. **Assert the inverse.** If a `failed` payment ever *does* appear in a settlement report, that
   is a high-severity alert: either my status is stale (it was authorised and captured after I
   last synced) or the gateway is wrong. Cheap check, and the kind of bug that is otherwise
   invisible. Same for its mirror: a `captured` payment that never settles within the window.

The pattern across (c), (d) and (e) is one idea: **a good reconciliation system is defined as
much by what it deliberately refuses to explain as by what it matches.** Three different kinds of
"not my problem" — a non-gateway credit, a business error that reconciles fine, a payment that
never moved money — each needing its own explicit state rather than being forced into
matched/unmatched.

---

## My five decisions

| # | Decision | Reason |
|---|---|---|
| 1 | Fees are **per transaction**, rounded to 2 dp | It's what the gateway does, so it's what the bank will show; batch-level fee maths is off by ~₹0.005 per txn (Ex. 2) |
| 2 | Tolerance = **max(₹1.00, ₹0.01 × txn_count)** | Absorbs per-transaction rounding drift, which scales with batch size — while staying far too small to ever swallow a real refund or chargeback |
| 3 | Refunds are **netted off** the next settlement, at **gross** | The original fee is not returned, so a ₹300 refund costs ₹300 and the settlement is short by exactly that (Ex. 4) |
| 4 | Chargebacks are **netted off** the settlement in which they were debited, and carry **two dates** (underlying txn + debit) | Lets cross-month disputes be explained without restating a closed, already-tied period (Ex. 5) |
| 5 | Candidate date window = **T+1 to T+5 calendar days**, ranked within the window | T+2 calendar days silently fails for every Thursday and Friday capture; T+5 absorbs a weekend plus one bank holiday (Ex. 3) |

Plus one that isn't on the list but is implied by Exercise 6 and belongs with them:

| 6 | Settlement membership is **read from the gateway's settlement report**, never inferred from dates | Partial settlements make date-inferred membership wrong, and the resulting false matches propagate into the next batch |

---

## Phase 0 self-check

Computing a settlement net by hand, from memory: gross, less 2% per transaction, less 18% of
those fees, less refunds and chargebacks debited in this cycle, plus or minus adjustments — and
the two habits that make it survive real data: **read membership from World 2**, and **give every
line a state, including "not mine."**
