# Ledger design

Ledger is a deterministic, pure-Python billing domain library. It owns the
rules that turn plans, time, and payment results into subscriptions and
invoices; it does not own HTTP, a database, a payment-provider client, or a
job runner. An application supplies persistence, a clock, and payment results.

The central rule is that historical billing records never change. A correction
is a new credit, debit, or credit note, never an edit to a final invoice.

## Core conventions

- All instants are aware `datetime` values. Ledger accepts a named IANA
  `zoneinfo.ZoneInfo` as a subscription's billing zone and rejects naive
  datetimes and offset-only zones for calendar recurrence.
- Time periods are half-open: `[start, end)`. An instant at `end` belongs to
  the next period, never both periods.
- Public monetary values are `Money(amount: int, currency: Currency)`, where
  `amount` is the currency's minor unit. Currencies must match for arithmetic.
  Negative `Money` represents a credit. There are no float-taking APIs.
- IDs, event timestamps, price snapshots, tax inputs, and idempotency keys are
  explicit inputs. The library never calls `datetime.now()` or generates a
  random identifier as hidden business input.
- Plans and invoices store a price/tax snapshot, not a mutable reference whose
  later change could rewrite the past.

## 1. Money and allocation

### Rules

`Money` stores only an integer number of minor units. For example, USD 10.99
is `Money(1099, USD)`, and JPY 10 is `Money(10, JPY)`. Currency metadata
validates an ISO code and its minor-unit exponent for display only; arithmetic
is always on the integer amount.

`allocate_equal(total, recipient_ids)` splits a `Money` total across one or
more recipients. It uses `divmod(total.amount, n)`, gives every recipient the
quotient, and gives one extra minor unit to the first `remainder` recipient
IDs in the caller-supplied, canonical order. Thus 100 cents split three ways
is 34, 33, 33; -5 cents split three ways is -1, -2, -2. The outputs always sum
exactly to the input and differ by at most one minor unit. A weighted variant
uses the same largest-remainder rule with positive integer weights.

The only internal exception is `ExactAmount`, a `fractions.Fraction` measured
in minor units. It is used for time and tax calculations before an amount is
made customer-visible. It cannot escape a finalized invoice as money.

### Correctness traps and defenses

- **Binary floating point turns 0.01 into an approximation.** Floats and
  `Decimal` values constructed from floats are rejected; use integers and
  `Fraction` only.
- **Integer division silently discards pennies.** Allocation has a sum
  invariant and a deterministic remainder order; no caller receives an
  unallocated remainder.
- **Nondeterministic dictionary/database ordering changes who gets a penny.**
  Allocation requires a stable sequence of recipient IDs and breaks all ties
  by that sequence.
- **Adding different currencies produces plausible nonsense.** Every addition,
  comparison, allocation, price change, and invoice requires one currency.

## 2. Billing cycles

### Rules

`BillingCadence` is `MONTHLY` or `YEARLY`. A paid subscription has a
`CycleAnchor`: the original paid-period start's local date, local time, and
billing `ZoneInfo`. Boundary *k* is calculated from that original anchor, not
by repeatedly adding to the previous boundary:

- monthly: add *k* calendar months;
- yearly: add *k* calendar years;
- use `min(anchor.day, last_day_of_target_month)`.

Therefore a subscription whose paid anchor is `2026-01-31 10:00` in its
billing zone has periods ending on `2026-02-28 10:00`, then `2026-03-31
10:00`. It does not drift permanently to the 28th. A yearly anchor of February
29 is February 28 in non-leap years and February 29 in later leap years.

The local wall time is retained across DST. We validate each resulting local
time by round-tripping it through UTC. A nonexistent local time (spring
forward) is moved forward to the first valid local time; an ambiguous time
(fall back) uses the earlier occurrence (`fold=0`). Each `Period` stores aware
start and end instants plus the billing zone.

Trials are not paid cycles. A trial begins at its aware `started_at` and ends
at its explicit aware `trial_ends_at`; the first paid period starts at that
end instant and establishes the paid `CycleAnchor`. This avoids an implicit,
ambiguous rule about whether a 14-day trial should preserve the creation day.

### Correctness traps and defenses

- **Adding 30 days implements neither a month nor a year.** Cycle boundaries
  use calendar-month/year arithmetic and the original anchor.
- **Chaining clamped dates drifts Jan 31 to Mar 28.** Every boundary is derived
  from the original anchor and its index.
- **Inclusive end points double-bill an exact renewal instant.** All cycle and
  entitlement membership uses the half-open interval convention.
- **Naive datetimes and DST change elapsed time.** Inputs are zone-aware;
  recurrence is local-calendar based, while duration is calculated from the
  resulting UTC instants.
- **DST gaps/folds are left to platform quirks.** The stated gap/fold resolution
  is validated and testable.

## 3. Proration

### Rules

An immediate plan change is allowed only while the subscription is paid and
the new plan has the same currency and cadence. A cross-cadence change is
scheduled for the next renewal instead; it is not guessed mid-cycle.

For a change at `changed_at` in current period `[s, e)`, Ledger computes exact
minor-unit amounts using elapsed instants:

```
remaining = (e - changed_at) / (e - s)
credit    = -old_period_price * remaining
charge    =  new_period_price * remaining
net       = credit + charge
```

`changed_at` must be in `[s, e)`. At `s`, the adjustment is the full
old-plan credit and full new-plan charge; at `e`, it is a next-period change,
not a zero-value immediate change. The old and new price snapshots are stored
on the adjustment. The subscription retains the same cycle anchor and renews
on the new plan at the next boundary.

Ledger creates an adjustment invoice at the change instant. A positive net is
collectible; a negative net is a customer credit balance applied to the next
collectible invoice, never an automatic cash refund. A zero net still records
the audited plan transition but creates no payable invoice. Repeating a change
with the same idempotency key returns its original result.

### Correctness traps and defenses

- **Prorating by a presumed 30-day month mischarges February and DST months.**
  The ratio uses actual elapsed instants within the authoritative period.
- **Rounding credit and charge separately manufactures or loses a penny.**
  Both remain exact until invoice finalization.
- **Changing a plan twice applies the first price twice or creates duplicate
  credits.** Each successful change advances the active price snapshot and is
  idempotency-keyed.
- **Changing cadence mid-period makes the renewal anchor undefined.** Such
  changes are explicitly deferred to the boundary.

## 4. Subscriptions

### Rules

`SubscriptionState` is one of `TRIALING`, `ACTIVE`, `PAST_DUE`,
`CANCEL_SCHEDULED`, `CANCELED`, or `INCOMPLETE`. The permitted transitions are
explicit rather than inferred from nullable fields.

- Creation is `TRIALING` when a trial end is supplied, otherwise `INCOMPLETE`
  until the first payment succeeds (or `ACTIVE` when collection is not
  required by the caller's policy).
- Trial expiry creates the first paid-period invoice and moves to `ACTIVE` on
  successful collection or `INCOMPLETE`/dunning according to that invoice's
  collection policy. A trial does not itself grant a paid period.
- `cancel_at_period_end` changes the state to `CANCEL_SCHEDULED`. Entitlement
  and the already-paid period remain through its exclusive end; no renewal
  invoice is created.
- `cancel_immediately` ends entitlement at its supplied instant, changes the
  state to `CANCELED`, and creates no refund by default. The caller may request
  the separate explicit `prorated_credit` policy, which creates the same
  unused-time credit calculation as a downgrade.
- Reactivating `CANCEL_SCHEDULED` before the period end simply removes the
  pending cancellation. Reactivating `CANCELED` starts a new paid cycle at the
  supplied instant: it does not resurrect old entitlement, old anchor, or a
  trial. Reactivation has no free trial by default.

Every transition records its effective instant and source idempotency key.
Entitlement is derived from state plus intervals, not from a mutable boolean.

### Correctness traps and defenses

- **A scheduled cancellation cuts off access today.** The distinct scheduled
  state retains entitlement until the period's exclusive end.
- **Immediate cancellation accidentally gives a refund or leaves access open.**
  Refund/credit policy is explicit and entitlement ends at one exact instant.
- **Reactivation revives an expired trial or old invoice.** It is a new paid
  cycle, with historical records immutable.
- **Retries and webhooks replay a transition.** State transition commands are
  idempotency-keyed and reject impossible source states.

## 5. Invoices, taxes, and rounding

### Rules

An `Invoice` starts as a mutable `DRAFT`, then is atomically finalized to
`OPEN` (or `PAID` when settled immediately). A finalized invoice is immutable;
`VOID` and `UNCOLLECTIBLE` are state changes, and financial corrections use a
new credit note/invoice.

An `InvoiceLine` has a stable ID, description, category (recurring charge,
proration charge, proration credit, discount, tax, credit-balance application),
currency, exact pre-round amount, and tax treatment. Plan prices are
tax-exclusive. Taxes are computed from the exact aggregate taxable base in
each `(jurisdiction, tax_rate, tax_treatment)` bucket, then rounded once per
bucket using **round half up**, in the invoice currency's minor unit.

Non-tax lines are aggregated exactly. Ledger rounds their one invoice subtotal
once, half up. It then materializes integer line amounts with a deterministic
largest-remainder allocation: floor each exact line amount, and distribute the
needed minor units to the largest fractional remainders, breaking ties by line
ID. This works for credits as well as charges, and guarantees that visible
line amounts sum to the rounded subtotal. Tax lines sum to the rounded tax
total. Finally:

```
total = subtotal + tax_total
amount_due = max(total - applied_credit_balance, 0)
```

Any excess credit balance remains on the customer account. `total` is not
rounded a second time.

### Correctness traps and defenses

- **Rounding each prorated line causes totals to disagree with the actual
  charge.** Exact values survive until the invoice subtotal/tax bucket is
  rounded once; visible line rounding is an allocation of that already-rounded
  subtotal.
- **A line-item display does not add up to the invoice total.** Materialized
  amounts have a reconciliation invariant for subtotal, tax total, and total.
- **Taxing rounded fragments or mixing rates creates inconsistent tax.** Tax is
  aggregated and rounded once per explicit rate/jurisdiction bucket.
- **A later plan or tax edit rewrites a receipt.** Finalization snapshots all
  financial inputs and forbids edits.

## 6. Dunning

### Rules

`DunningPolicy` is injected and versioned with the invoice. Ledger's default
policy is one initial collection attempt at the invoice due instant and four
retries at 1, 3, 7, and 14 days after that attempt: at most five attempts over
14 days. Retry times are UTC instants (durations), not local calendar days, so
DST cannot add or remove a retry hour.

A payment adapter reports an idempotent `PaymentAttempt` outcome as `SUCCEEDED`,
`RETRIABLE_FAILURE`, or `TERMINAL_FAILURE`. Ledger itself never decides that a
gateway error is soft or hard. A success marks the invoice `PAID`, closes the
dunning case, and restores a `PAST_DUE` subscription to `ACTIVE` if it still
has a current entitlement period. A retriable failure schedules exactly the
next retry. A terminal failure skips directly to exhaustion.

At exhaustion Ledger marks the invoice `UNCOLLECTIBLE`, closes the dunning
case, and cancels the subscription immediately at that attempt instant. It
does not alter the invoice total or invent a new renewal. A scheduled
period-end cancellation still permits collection of an already-issued invoice,
but no further renewal is generated.

### Correctness traps and defenses

- **A scheduler runs twice and charges twice.** Every attempt has an invoice,
  attempt-number, and gateway idempotency key; only the currently scheduled
  attempt may be submitted.
- **Retry timing follows wall-clock days through DST.** Backoff is expressed as
  UTC durations from the prior attempt/due instant.
- **Hard declines are retried forever, or soft declines are abandoned too
  soon.** The adapter classifies failures; a bounded, versioned policy defines
  exhaustion.
- **A successful late payment revives a different future subscription.** It
  only settles its invoice; entitlement restoration is limited to the original
  still-current period.

## Module layout

```
ledger/
  __init__.py        # deliberately small public API
  money.py           # Currency, Money, ExactAmount, allocation and rounding
  time.py            # Clock protocol, ZoneInfo validation, DST resolution
  periods.py         # Period, CycleAnchor, BillingCadence, boundary functions
  plans.py           # Plan and immutable PlanPriceSnapshot
  subscriptions.py   # state machine, entitlement, transition commands/results
  proration.py       # ProrationQuote and immediate/deferred change decisions
  invoices.py        # draft/final invoice, lines, reconciliation invariants
  tax.py             # tax buckets and tax calculation policy
  payments.py        # payment attempt result value objects and adapter protocol
  dunning.py         # DunningPolicy, DunningCase, retry/exhaustion transitions
  events.py          # immutable domain events and idempotency result records
  errors.py          # domain-specific validation and transition errors
```

The core types are frozen dataclasses and enums. Transition functions take an
entity plus an explicit command/instant and return a `TransitionResult` holding
the replacement entity, newly created immutable records, and domain events.
This makes replay, exhaustive state-machine tests, and persistence adapters
straightforward without binding domain logic to a database.

The application layer may serialize these value objects and schedule work, but
it must preserve the supplied IDs, ordering, clock values, and idempotency
keys. Tests should assert the invariants above—especially money conservation,
period adjacency, invoice reconciliation, immutable finalization, and replay
idempotency—not just example totals.
