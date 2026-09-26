# Reap agentic purchase — the state machine (WP2b), the poller (WP3) + the routes (WP4)

`services/reap_agentic_purchase.py` (the state machine) and
`jobs/reap_agentic_purchase_poll.py` (the poller that drives it). Buyer-funded purchases over
Reap's agentic rail: the buyer enrols **their own card** once on Reap's hosted page, and approves
each purchase on another hosted page. **Pivota never holds or moves money and never sees card
data.**

This rail is **dark**. Its routes exist but answer 404, the dial is off, and the scheduler job is
registered but inert — and the worker service that would run it **is deployed separately from the normal
backend deploy**. Nothing here has ever talked to a `reap.global` or `prava.space` host.

---

## States

`db/reap_agentic_ledger.ALLOWED_TRANSITIONS` is the map; migration 224's `CHECK` is the
vocabulary. **There are no self-edges** — "wait and try again" is `release_claim`, not a
transition.

| state | what it means | this package's step calls | → |
|---|---|---|---|
| `resolving` | we have our catalog row, not Reap's variant | `resolve_our_row`; then either `get_active_enrollment` or `upsert_pending_enrollment` + `create_enrollment` | `needs_enrollment`, `quoting`, `refused`, `failed` |
| `needs_enrollment` | buyer has a hosted card page open | `get_active_enrollment`; `get_enrollment_internal` (re-read — a READ, not the old upsert-as-read); `get_enrollment`; `mark_enrollment_active` / `mark_enrollment_dead` | `quoting`, `expired` (sweep only), `failed` |
| `quoting` | ready to price and hand the buyer a link | `get_active_enrollment`; `resolve_our_row` **again**; `request_quote`; **`verify_quote`**; `create_checkout` — **all in one step** | `awaiting_approval`, `refused`, `failed` |
| `awaiting_approval` | buyer has the approval page | `get_checkout` | `processing`, `completed`, `failed`, `expired` |
| `processing` | buyer approved; Reap is placing the order | `get_checkout` | `completed`, `failed` |
| `completed` / `failed` / `refused` / `expired` | terminal. `advance` makes no call. | — | — |

**Terminal writes NULL `buyer_email` and `shipping_address`**, stamp `terminal_at` and clear the
claim — in the same UPDATE, so a crash cannot skip the PII half.

### Why the quote and the checkout are one step
A quote expires in ~5 minutes; a poll cycle is not guaranteed to be shorter, and there is no
legal state for the row to sit in between the two (`quoting` → `quoting` is not an edge).

### The lifecycle of an unapproved checkout — the quote TTL is the approval window

**Measured 2026-09-25 in the Reap sandbox**, on two checkouts (one with and one without the
`X-Simulate-Checkout` header, neither approved): `POST /agentic/checkouts` answers
`REQUIRES_ACTION` with `nextAction.expiresAt` = created + **15 min**, but the checkout flips to
**`FAILED` — not `EXPIRED` — 1–10 s after the QUOTE's `expiresAt`** (created + **5 min**), and
never passes `PROCESSING`. The hosted page's expiry is therefore not the buyer's deadline; the
quote's is. Consequences in this package:

* the owner view carries **`approval_deadline`** on `awaiting_approval` = the earlier of
  `reap_quote_expires_at` and `hosted_url_expires_at`, and drops `hosted_url` once *that* has
  passed (`routes/agent_commerce_reap.approval_deadline`);
* a partner `FAILED` read on an `awaiting_approval` row whose `reap_quote_expires_at` is already
  in the past at read time is written as `failed` with **`last_error_code = approval_window_lapsed`**
  rather than `checkout_failed` (`_checkout_failed_code`). The target state is unchanged; a
  `FAILED` on `processing` — after approval — is still `checkout_failed` whatever the quote says;
* a partner `EXPIRED` still takes the `expired` edge with `checkout_expired`. In the sandbox it
  was never observed for an unapproved checkout, but the mapping is kept for a partner that one
  day sends it.

A spike in `approval_window_lapsed` is **most likely** buyers not reaching the approval page
inside five minutes — a door showing the link late, or not at all. It is a heuristic, not a
diagnosis: Reap's `FAILED` payload carries no reason, and a buyer who approves inside the last
poll interval before the quote expires, whose checkout then fails at the merchant after it, gets
the same code (the machine admits `PROCESSING` can be skipped between two polls). The ambiguity
is one poll interval wide (30 s, doubled per transport failure). Read a spike against
`checkout_failed` on `processing` rows from the same window before calling it a door problem.

### What the quote is checked against

The unit price is not the charge, so the quote is verified in **integer minor units** before
anything is created. All four checks must pass:

| check | rule | on failure |
|---|---|---|
| **items echo** | `items` is exactly one line, carrying the `variantId` we sent and the quantity we sent | `price_unverifiable` / `quote_items_mismatch` |
| readable | `finalAmount`, `itemsSubtotal`, `shipping` and `tax.amount` all present and parsable as exact decimals (a JSON number **or** a decimal string) | `price_unverifiable` / `quote_amounts_unreadable` |
| currency | every one of those four names the **row's** currency | `price_changed` / `quote_currency_mismatch` |
| subtotal | `itemsSubtotal == quantity × our_price_minor`, **exactly**, no tolerance | `price_changed` / `quote_items_subtotal_mismatch` |
| reconciles | `finalAmount == itemsSubtotal + shipping + tax`, within **±1 minor unit** | `price_changed` / `quote_total_not_reconciled` |

**The items echo is the only check that looks at *what* is being bought.** Reap returns 200 for a
**substituted variant**, and every other check here looks at what the quote *costs* — a
substitution whose price happens to match sails through all of them. `MAX_QUANTITY` is re-applied
inside `verify_quote` as well as at `start_purchase`, because the subtotal check multiplies by
that number and it is the last arithmetic before a card is charged.

A quote that does not say what it priced — `items` absent, not a list, two lines, a bool or
string quantity — is `quote_items_mismatch`, same as a mismatch. A `None` is **never** COALESCEd
into "fine": an amount we cannot state exactly is one we do not
buy against. `shipping` and `tax` may be exactly `0.00` (free shipping is the commonest quote
there is) and are read through a sibling converter for that reason; a **negative** component is
still refused. A quote whose `expiresAt` has already passed is not turned into a checkout —
release with `quote_expired` and re-quote next step.

### Resolution hints

`accept_variant_labels`, `also_accept_domains` and `market_country` are **persisted** (migration
225) and read back by `_resolution_inputs`, so a hint survives the process boundary between
`start_purchase` and `advance`. They used to be refused (`resolution_hints_not_persistable`) or
silently dropped, because there were no columns for them.

* The **ledger** owns their shape: at most 32 labels of 128 characters, no control characters;
  domains lowercased and hostname-shaped (no scheme, path or port); `market_country` two
  UPPERCASE letters. It **raises rather than truncating** — a silently shortened alias names a
  different object — and `start_purchase` maps that to `PurchaseRefused("invalid_request")`, so a
  route has one exception type to catch and no row is left behind.
* **This module uppercases `market_country`** before the ledger sees it. The column's guard is
  `^[A-Z]{2}`, so an ordinary lowercase `us` would otherwise come back as a refusal of a value
  the caller would reasonably think was fine.
* They are **not PII** and are **not** nulled on a terminal transition: a completed purchase that
  kept the alias it resolved through is the record you want when that alias is questioned later.
  They are also not in the owner-facing view.

### Why the quote step re-resolves
`reap_variant_id` / `reap_product_id` on the row are **evidence, never identity**: five searches
for the same product on one day returned five different `prd_` ids. Quoting against a stored
handle is quoting against something nobody has checked since.

---

## Dials

| variable | default | effect |
|---|---|---|
| `REAP_AGENTIC_ENABLED` | **unset = off** | `start_purchase` refuses `rail_disabled`. Truthy spellings: `1`, `true`, `on`, `yes` (case/space-insensitive). An allowlist, so a typo cannot arm the rail. |
| `REAP_API_BASE_URL` + `REAP_API_KEY` | unset | both required; otherwise `start_purchase` refuses `rail_unconfigured`. |
| `REAP_RETURN_URL_HOSTS` | `api.pivota.cc,agent.pivota.cc` | host allowlist for our own `returnUrl`. Empty/unset means the default, not "no hosts". **Order matters:** the first host is the default return URL's host. |
| `REAP_AGENTIC_RETURN_URL` | unset | the return URL the routes use when the caller sends none. Unset ⇒ `https://<first REAP_RETURN_URL_HOSTS host>/reap/return`. Validated like a caller's (https, allowlisted host, no userinfo). |
| `REAP_AGENTIC_SIMULATE_CHECKOUT` | **unset = off** | **Sandbox only.** Exactly `COMPLETED` (case-sensitive; anything else is ignored with a WARNING on the `pivota` logger) adds `X-Simulate-Checkout: COMPLETED` to `POST /agentic/checkouts` and to no other request — and only when `REAP_API_BASE_URL`'s host is exactly `sandbox.api.reap.global` or `mx.sandbox.api.reap.global` (any other host: header withheld, WARNING). See below. |

**`REAP_AGENTIC_SIMULATE_CHECKOUT`, measured 25 Sep in the sandbox.** It does not skip the buyer:
the checkout is still created `REQUIRES_ACTION` with a hosted approval URL, and a human must approve
it within the **quote's** ~5-minute TTL — an unapproved checkout goes `FAILED` (not `EXPIRED`) when
the quote expires. After approval it goes `PROCESSING` → `COMPLETED` with an `orderId` in about
70 s, where the un-simulated sandbox ends `FAILED`. When sent, the header is part of the checkout's
idempotency material, so a simulated and an unsimulated checkout on one quote are different
requests; with the dial unset the key is byte-identical to before. Reap's spec says the header is
rejected in production, so the host guard is belt and braces, not the only lock.

### The return URL and its landing page

With neither variable above set, Reap sends the buyer's browser back to
`https://api.pivota.cc/reap/return` — a static page this backend serves (`routes/reap_return.py`,
`web` service, public, no auth, not behind `REAP_AGENTIC_ENABLED`), titled "Back to your
assistant". Buyers reach it after BOTH hosted steps — the enrollment (card saved) and the checkout
approval — and it reads no input, so its copy is stage-neutral: Reap has their response, the
assistant will confirm the next step once it is settled, nothing is charged without their approval
on Reap's page. **Landing there proves nothing about the order:** the buyer's approval on Reap's page authorises the charge, arriving at the URL only means
a browser followed a redirect, and the outcome is known only when the poller reads
`GET /agentic/checkouts/{id}`. The page reads no parameter, writes nothing and logs nothing of its
own, but the click id in its query string does appear in uvicorn's access line (the redaction
filter rewrites path secrets only). To send buyers elsewhere, set `REAP_AGENTIC_RETURN_URL` to a
full https URL on an allowlisted host, or put a different host first in `REAP_RETURN_URL_HOSTS`
(that host must then serve `/reap/return` itself).

`REAP_AGENTIC_ENABLED` gates `start_purchase` only. `advance` does **not** re-check it: a
purchase already in flight must be allowed to finish (or fail cleanly) after the dial is turned
off — stranding a row in `awaiting_approval` after the buyer has paid is worse than finishing it.

> **The poller re-checks it, but ONLY for the partner-facing half.**
> `run_reap_agentic_purchase_poll` gates **step 4 only** (claim + `advance`). With the rail off
> it returns `skipped_disabled=1`, takes no claim and makes no partner call — and **the three
> sweeps above it run anyway**, including the PII deadline.
>
> The first cut gated the whole run, and that was a measured retention bug: with the rail off, an
> `awaiting_approval` row 99,999 s old kept `buyer_email` and `shipping_address` across three
> consecutive ticks. Because the gate is `is_enabled() **and** `is_configured()`, one unset
> credential had the same effect as an operator switching the feature off.
>
> The rule is: **the dial stops us talking to a partner. It is not permission to stop forgetting
> people.** That is safe because none of steps 1–3 calls a partner (they are three UPDATEs in
> `db/reap_agentic_ledger.py`) and none can touch a row whose payment is in flight —
> `expire_overdue_purchases` names only the two waiting states and `fail_exhausted_purchases`
> runs `include_processing=False`, both parsed out of the SQL that enforces them.

### The poller's dials

All integers, all with code defaults, **all read per run** (except the interval, which an
APScheduler trigger fixes at registration). An invalid or out-of-range value falls back to the
default **with a warning naming the variable** — never a crash, never a silent zero. The minimums
sit at or above the ledger's own floors on purpose: `lease_seconds < 30` and
`max_age_seconds < 60` are `ValueError`s out of the ledger, so an unvalidated dial would not be a
bad setting, it would be an exception out of a scheduled job on every tick.

| variable | default | bounds | effect |
|---|---|---|---|
| `REAP_AGENTIC_ENABLED` | **unset = off** | truthy allowlist | the gate, inside the job, over **step 4 only**. Off ⇒ `skipped_disabled=1`, no claim, no partner call — the sweeps still run |
| `REAP_AGENTIC_POLL_INTERVAL_SECONDS` | 30 | 5–3600 | the `interval` trigger **and** `misfire_grace_time`. Registration-time only — changing it needs a restart |
| `REAP_AGENTIC_CLAIM_BATCH` | 10 | 1–100 | rows claimed per run. Capped at 100 because each row is a serial partner chain |
| `REAP_AGENTIC_LEASE_SECONDS` | 300 | **180**–3600 | what `requeue_stale_claims` measures against. The floor is 180, not the ledger's 30: a lease shorter than one step gets a LIVE worker's row requeued underneath it, and both workers then call the partner |
| `REAP_AGENTIC_HOSTED_MAX_AGE_SECONDS` | 3600 | 60–2592000 | the absolute PII deadline in `expire_overdue_purchases`, measured from `state_entered_at` |
| `REAP_AGENTIC_MAX_ATTEMPTS` | 50 | 1–10000 | attempts ceiling. `attempts` counts **claims**, and only in `resolving`/`quoting`/`processing` |
| `REAP_AGENTIC_ERROR_BACKOFF_SECONDS` | 120 | 1–3600 | how long a row waits after `advance` **raised**. Not the state machine's table — this is the path where it did not get to choose |
| `REAP_AGENTIC_POLL_BUDGET_SECONDS` | 240 | 10–3600 | wall-clock budget. It stops the job **starting** work — a row already in flight finishes — so the run deadline is sized as budget + one whole step |

### How slow one step really is

The `~40 s for quoting` figure that used to live here was inherited and is **wrong** for
`_step_quoting` as it now stands. Re-derived from `services/reap_agentic_client`'s per-path read
timeouts:

| call | bound | note |
|---|---|---|
| `resolve_our_row` | up to 4 × 25 s = **100 s** | `MAX_SEARCH_ATTEMPTS` (3) `products/search` plus one `products/variant` |
| `request_quote` | **35 s** | 13–16 s measured across nine merchants |
| `create_checkout` | **35 s** | |
| | **170 s worst realistic** | |

Everything else is sized from that one number: the lease floor (180), the run deadline
(600 = 240 budget + 170 step + margin for the sweeps), and the standing caveat below.

> **Even 600 s is not a guarantee.** The client hands httpx a *bare float* timeout, and httpx
> spreads a bare float across connect, read, write **and** pool as that value **each** — so the
> strict bound on one `quoting` step is roughly 4 × 170 s. The run deadline is a backstop against
> a **wedge**, not a promise that a batch completes. That is safe because the job wraps its row
> loop in a `try/finally` that releases every claim on `CancelledError`, so a cut run strands no
> leases and the next tick simply re-claims what it did not reach.

Two constants are deliberately **not** dials: `SWEEP_BATCH` (200 rows per sweep statement — a
lock-window property of a Postgres prod and staging share, not an operator setting) and
`MAX_SWEEP_ITERATIONS` (20 — the cap that stops a sweep whose predicate a future migration makes
permanently true becoming an infinite loop inside a scheduled job).

---

## The poller — `jobs/reap_agentic_purchase_poll.py` (WP3)

`async def run_reap_agentic_purchase_poll(*, worker_id=None, now=None) -> PollReport`

Registered in `services/audit_scheduler.py` as **`reap_agentic_purchase_poll`**, an `interval`
job every `REAP_AGENTIC_POLL_INTERVAL_SECONDS` (default 30), `max_instances=1`, `coalesce=True`,
run deadline **600 s** in `_JOB_RUN_DEADLINES` (see the derivation above).

### Run order (each step bounded)

| # | step | bound |
|---|---|---|
| 1 | `requeue_stale_claims(lease_seconds=REAP_AGENTIC_LEASE_SECONDS)` | one batch of 200 per run |
| 2 | `expire_overdue_purchases(max_age_seconds=REAP_AGENTIC_HOSTED_MAX_AGE_SECONDS)` | 200 per statement, looped until a partial batch, hard cap 20 iterations |
| 3 | `fail_exhausted_purchases(REAP_AGENTIC_MAX_ATTEMPTS, include_processing=False)` | same |
| 4 | `claim_due_purchases(worker, limit=REAP_AGENTIC_CLAIM_BATCH)` → per row `advance` → `release_claim` | **sequential**, one partner chain at a time, stopped by `REAP_AGENTIC_POLL_BUDGET_SECONDS` |

**The sweeps run before the claim** because a row held by a dead pod is not claimable until the
requeue frees it — a claim-first run would skip exactly the rows that most need attention, every
tick, forever, on a pod that keeps dying.

**Step 3 never touches `processing`.** A purchase in `processing` has been approved by the buyer
and its payment is in flight with Reap; auto-failing it on a counter writes `failed` over a
charge whose outcome we do not know. **A payment stuck in `processing` is a human decision** —
reconcile the checkout with Reap by hand, then either transition the row or call
`fail_exhausted_purchases(..., include_processing=True)` yourself. There is deliberately no env
var for it: a dial would let somebody arm it once and forget.

**After the loop this worker holds no claims**, and that is *checked with a query*, not asserted.
Anything left over is released and counted under `errors`.

### The counts on `PollReport`

Counts and a duration only — **no ids, no PII**. Purchase ids appear in DEBUG logs and in the two
ERROR lines an operator must act on.

| count | meaning | action if it is high |
|---|---|---|
| `skipped_disabled` | 1 = **step 4** was skipped (dial off, or client unconfigured). NOT "the run did nothing" — the sweeps above it ran and their counts are real | expected while dark |
| `requeued` | stale leases freed | steadily > 0 means workers are dying mid-step, or `REAP_AGENTIC_LEASE_SECONDS` is below the slowest step |
| `expired` | purchases the buyer abandoned; PII NULLed | normal; a spike means hosted pages are expiring before buyers use them |
| `failed_exhausted` | rows that hit the attempt ceiling | look at `last_error_code` on those rows — the rail is failing the same way repeatedly |
| `processing_over_attempts` | **read-only.** Purchases in `processing` at or past `max_attempts` — the exact set the fail sweep refuses to terminate because a payment is in flight | **any non-zero value that does not fall needs a human.** Reconcile the checkout with Reap; the counter will never resolve these. See below |
| `claimed` | leases taken this run | `== REAP_AGENTIC_CLAIM_BATCH` every tick means the backlog is growing; raise the batch or the interval |
| `advanced` | rows that changed state | — |
| `released` | rows that made no progress because **the partner was not ready** | — |
| `abandoned_budget` | rows claimed and handed straight back because **the run ran out of time**. Kept separate from `released` on purpose: they are different facts and only one of them means "raise the budget" | persistently > 0 ⇒ lower `REAP_AGENTIC_CLAIM_BATCH` or raise `REAP_AGENTIC_POLL_BUDGET_SECONDS` |
| `lost_claim` | a fenced write answered None — a sweep or another worker got there first | **never an error**; steadily high means two workers are fighting, i.e. more than one worker service is armed |
| `terminal` | the row was already terminal when the step read it | a few are normal (a sweep landed between claim and step) |
| `errors` | **the only count that should page anyone** | see below |
| `duration_ms` | wall clock | approaching `REAP_AGENTIC_POLL_BUDGET_SECONDS` every tick means the batch cannot be serviced at this cadence |

### When `errors` > 0

`errors` means one of three things, all of them bugs, none of them backpressure:

1. **`advance` raised.** The log line is
   `advance RAISED for purchase=<id> error_type=<Type>; releasing with a <N>s backoff` — the
   exception TYPE and the purchase id, never the message (a driver error's message carries row
   content, and on this table that content is the buyer's email and address). The known case is
   `UniqueViolationError` on `uq_reap_agentic_purchases_checkout`: the partner handed back a
   `checkout.id` we already stored, which means two of our rows believe they own one charge.
   **Do not retry it.** Find both rows (`SELECT id, state FROM reap_agentic_purchases WHERE
   reap_checkout_id = '<id>'`), reconcile the checkout with Reap, and resolve by hand. The row
   itself is safe meanwhile: it was released with `REAP_AGENTIC_ERROR_BACKOFF_SECONDS`.
2. **A claim survived the loop.** The log line says so and names the purchase id. It has already
   been released. This is a bug in the poller — open an issue with the surrounding run's report.
3. **`advance` returned an outcome the poller does not know** (`returned an unhandled outcome
   '<x>'`). Today that is only `missing` — a row claimed a moment ago and unreadable now, which
   should be impossible — or a new outcome added to `services/reap_agentic_purchase.py` without
   updating the poller's list. Counting it rather than dropping it is what keeps a whole new
   outcome class from being invisible in the one report an operator reads.

Everything else is a count, not an error. In particular `lost_claim` is the designed answer to
the unfenced bulk sweeps and must never be alerted on.

---

## Arming it

The poller runs **only on the worker service**. `_add_job` registers nothing unless
`services.audit_scheduler._queue_worker_enabled()` is true, because prod and staging share one
Postgres and the claim has no environment filter — a staging service would poach production
purchases and spend a buyer's card with staging code.

> **THE WORKER SERVICE IS DEPLOYED SEPARATELY. The normal backend deploy does NOT ship it.**
> Merging this and deploying the backend changes nothing: the job only exists in a process where
> `_queue_worker_enabled()` is true, and that process has to be deployed on its own. See
> `docs/` on the scheduler lane; this is the same gap that left `catalog_import_drain_tick`
> registered on an undeployed worker.

1. Deploy the worker service.
2. `AUDIT_WORKER_ENABLED=true` on it (or let the service-name detection decide; the gate is
   fail-safe toward ENABLED, so an unknown platform stays on).
3. `REAP_API_BASE_URL` + `REAP_API_KEY` — both, or `is_configured()` is false and every run
   returns `skipped_disabled=1`.
4. `REAP_AGENTIC_ENABLED=1`. **This is the arming step.** No redeploy and no scheduler restart:
   the gate lives inside the job, so the next tick picks it up.
5. Run it once by hand and read the report:
   `POST /admin/scheduler/jobs/reap_agentic_purchase_poll/run-now` (admin auth). The response is
   the runner outcome; the counts are in the job's own log line and on
   `GET /admin/scheduler/runs`.
6. Watch `errors` and `lost_claim` for a few ticks.

### Stopping it

**Pause and dial-off are not interchangeable, and the difference is the PII deadline.** A paused
job never fires, so pausing stops *everything* — including the sweep that forgets abandoned
buyers. Turning the dial off stops only the partner-facing half.

| lever | stops partner calls | sweeps keep running | use when |
|---|---|---|---|
| `REAP_AGENTIC_ENABLED` off | **yes** | **yes** — requeue, expire (the PII deadline) and fail all still run | **the normal stop.** Reach for this first. |
| `POST .../reap_agentic_purchase_poll/pause` | yes | **NO — nothing runs at all** | only briefly: the database is in trouble, or the job itself is misbehaving. **Resume it, or the PII deadline stays off.** |
| `POST .../cancel-running` | frees a wedged in-flight run only | yes | a run has wedged; it never starts work |
| redeploy the worker without the env var | yes | yes | equivalent to the dial, plus it abandons the in-flight run |

With the dial off, purchases already in flight **stall** — nothing advances them — but they are
still expired on the clock and still have their PII nulled, and their claims are still recovered.
That is the intended resting state for a disarmed rail.

**If you must pause:** set a reminder to resume. A paused poller is a rail that has stopped
forgetting people, and nothing else in the system will do it for you.

### When `processing_over_attempts` will not go down

These rows are the deliberate blind spot. The buyer approved, Reap took the payment, and our poll
has asked about it `max_attempts` times without getting a terminal answer. The counter will
**never** fail them — `fail_exhausted_purchases` runs `include_processing=False` so our ledger
cannot say `failed` over a charge whose outcome we do not know.

To resolve one:

1. `SELECT id, reap_checkout_id, attempts, state_entered_at FROM reap_agentic_purchases
   WHERE state = 'processing' AND attempts >= <max_attempts>;`
2. Ask Reap what that checkout did. This rail has **no webhooks**, so this is a manual lookup.
3. Then, and only then, transition the row by hand — or, if you have confirmed the charge did not
   happen, call `fail_exhausted_purchases(..., include_processing=True)` yourself. There is no
   env var for that flag on purpose: a dial would let somebody arm it once and forget, which is
   the same as not having decided.

Both `run-now` and `pause`/`resume` are **allowlists** in `routes/admin_scheduler_jobs.py`
(`_RUNNABLE_JOB_IDS`, `_MANAGEABLE_JOB_IDS`); `reap_agentic_purchase_poll` was added to both, and
`tests/test_reap_agentic_purchase_poll.py` pins that so a rename cannot silently take the levers
away.

---

## What a poller must do (the obligations WP3 implements)

```python
for row in await ledger.claim_due_purchases(worker_id, limit=N):
    try:
        result = await advance(row["id"], worker_id)
    except Exception:
        logger.exception(...)          # see "raises", below
    finally:
        await ledger.release_claim(row["id"], worker_id)   # fenced no-op if already released
```

Obligations, in order of how expensive they are to get wrong:

1. **Claim before `advance`, always.** `advance` does not claim. Two different guards then
   apply, and it is worth knowing which protects what:
   * every write to **our database** is fenced on `claimed_by = worker_id` inside the UPDATE —
     authoritative, nothing can land between the check and the write;
   * every **partner call and enrollment-table write** is preceded by a re-read
     (`_still_ours`) requiring that we still hold the claim and the state has not moved. This is
     read-then-act, so it **narrows but does not close** the window; what it guarantees is that
     the ordinary lost race costs nothing at Reap. A step run on a row this worker does not hold
     makes **no partner call at all** and returns `outcome="lost_claim"`.

   Residual, and not closed here: a claim that moves between the re-read and the call two lines
   later is not caught, and the enrollment-table writes take no holder. Closing it needs a holder
   conjunct on those writes or an outbox — both ledger changes. (The enrollment *read* is no
   longer a write: `get_enrollment_internal` replaced the upsert-as-read, so polling a pending
   enrollment no longer touches `updated_at` or risks minting a stray row.)
2. **`lost_claim` means re-read, never retry.** Somebody else owns the row, *or* an unfenced bulk
   sweep terminated it. Retrying the same write cannot succeed.
3. **Wrap each row's step in its own `try`.** `advance` does **not** catch driver exceptions. The
   known case is `uq_reap_agentic_purchases_checkout`: a partner that hands back a `checkout.id`
   we have already stored raises a `UniqueViolationError` out of the step. That is deliberate —
   two of our rows believing they own one charge is the worst outcome on this rail — but one such
   row must not stop the batch.
4. **Expect the backoff to grow.** A transport failure schedules the state's interval doubled,
   and doubled again for each consecutive failure, up to `MAX_BACKOFF_SECONDS` (600). The counter
   is the ledger's `attempts`, which is exempt in the two human-wait states — so those stay at
   interval × 2 and are bounded by `expire_overdue_purchases` on a clock instead.
5. **Run the two sweeps.** They are the only bounds on the waiting states:
   * `expire_overdue_purchases(max_age_seconds=…)` — the PII deadline. `attempts` is exempt in
     `needs_enrollment` and `awaiting_approval`, so without this a buyer who walks away keeps
     their address and email on the row indefinitely.
   * `fail_exhausted_purchases(max_attempts=…)` — bounds `resolving` / `quoting` / `processing`.
     `include_processing` defaults **False** on purpose: a purchase in `processing` has been
     approved and its payment is in flight, and auto-failing it writes a terminal state over a
     charge whose outcome we do not know.
   * `requeue_stale_claims(lease_seconds=…)` — recovers a row whose worker died mid-step.
6. **`lease_seconds` must exceed the longest step.** The `~40 s` this used to say was written
   before the quote-verification round and is wrong — see **How slow one step really is** above:
   the worst realistic `quoting` step is **170 s**. The ledger's floor is 30 s, WP3's own floor is
   **180 s**, and 300 s (the default) is sane.
7. **Never call the unfenced `ledger.transition` from a worker.** Use `transition_as_holder`.

---

## What the owner view exposes

`ledger.get_purchase_for_owner` / `list_purchases_for_owner`, conjunct on
`(agent_id, agent_user_ref_hash)` **in SQL**, redacted by default through
`PUBLIC_PURCHASE_COLUMNS` — an allowlist.

Visible: state, our product identity, quantity, currency, `our_price_minor`, the quote/final
totals, `hosted_url` + `hosted_url_expires_at` (the link to show the buyer),
`reap_quote_expires_at`, `approval_deadline` (computed, `awaiting_approval` only: the earlier of
the quote's and the page's expiry — see the lifecycle note above), `reap_order_id`,
`refusal_reason`, `last_error_code`, timestamps.

**Not visible, and each absence is deliberate:** `buyer_email`, `shipping_address` (PII);
`buyer_ref`, `agent_id`, `agent_user_ref_hash` (identity we minted); `reap_product_id`,
`reap_variant_id`, `reap_quote_id`, `reap_checkout_id` (evidence — handing these out would read
as identity downstream); `enrollment_id`, `click_id`, `return_url`, `queries_tried`, and the
poller's bookkeeping.

The only partner-supplied URL that ever reaches the row is the one `rc.hosted_action` vouched for
(https, exact-or-dot-suffix on `prava.space`/`reap.global`, no userinfo, port 443). If it will
not vouch for one, the purchase **fails** rather than entering a state whose whole content is a
link. Nothing from Reap's product-media fields is ever stored or forwarded.

---

## Refusal and error vocabulary

`refusal_reason` (ours, on `refused`): the client's own resolve reason verbatim (e.g.
`search:merchant_not_in_results`, `options:sole_label_differs:<axis>`), `price_changed`,
`price_unverifiable`, `unverified_single_variant`, `merchant_not_completable` (503 on the quote).

`last_error_code` (on `failed`, or alongside a refusal): the four quote-check codes in the table
above, plus `ENROLLMENT_NOT_ACTIVE`, `enrollment_dead`, `enrollment_no_hosted_action`,
`enrollment_row_unreadable`, `partner_id_malformed`, `checkout_no_hosted_action`,
`checkout_failed`, `approval_window_lapsed` (a partner `FAILED` on `awaiting_approval` after the
quote's expiry — the buyer did not approve in time), `checkout_expired`, `checkout_id_missing`,
`quote_id_missing`, `quote_expired`,
`no_active_enrollment`, `completed_without_order_id`, `final_amount_missing`, and
`reap_status_<n>` / `AGENTIC_*` codes passed through from the partner.

### The three codes that suppress the attribution edge

A COMPLETED checkout **always completes the row**. The edge is a separate decision, and three
things suppress it. The edge is idempotent on `(merchant_id, external_order_id)` via
`ON CONFLICT DO NOTHING`, so **any** edge written here is permanent and a later correct close is
silently dropped — which makes "write something approximate now" strictly worse than "write
nothing and leave the slot free". In all three cases `reap_checkout_id` is stored, and that is
what a reconciliation re-reads.

| code | meaning |
|---|---|
| `completed_without_order_id` | no usable `orderId` (absent, blank, wrong charset, **or not a string** — `orderId: true` used to become `"True"` and collide every order at that merchant onto one key) |
| `final_amount_missing` | `finalAmount` absent, unparsable, or in another currency |
| `charged_total_differs` | the amount **charged** is not the amount **quoted** (the buyer approved a page showing the quote), outside ±1 minor unit — or there is no stored quote to compare against |

`completed_without_order_id` **completes the row; it does not park it.** An earlier version sent
it to `processing` so a human would see it, which was wrong for a measured reason: **`processing`
has no PII deadline.** `expire_overdue_purchases` sweeps only `needs_enrollment` and
`awaiting_approval`, and `fail_exhausted_purchases` skips `processing` by default, so the row kept
`buyer_email` and `shipping_address` against every default sweep, indefinitely — for a purchase
whose charge had already succeeded. The terminal write is the one statement that nulls the PII,
and nothing is in flight once the partner says COMPLETED.

A genuinely **PENDING/PROCESSING** partner status is a different thing and still belongs in
`processing`; bounding that is the poller's attempts counter, not this module's job.

`last_error_code` is **lower-cased** at the single place that writes it. The column is fed by
three vocabularies that disagree on case — ours (`quote_expired`), the client's transport codes
(`transport_error:ReadTimeout`) and the partner's, which its own `^[A-Z_]{3,64}$` check pins
UPPERCASE (`ENROLLMENT_NOT_ACTIVE`) — and a column holding both cannot be grouped or alerted on
without every consumer carrying its own fold. `refusal_reason`, `reap_status` and `card_network`
are **not** folded: those carry somebody else's vocabulary verbatim.

`partner_id_malformed` — a partner id we will not store, because it cannot survive the client's
own path-parameter rule. Storing one put the row in `awaiting_approval` and made every later poll
raise out of `advance`, unbounded, since that state is exempt from the attempts counter.

`price_changed` is **fail closed and final**: we do not re-offer at the new price. The owner
starts a new purchase. Same for `ENROLLMENT_NOT_ACTIVE` — `quoting` → `needs_enrollment` is not a
legal edge, so there is no way back to the card page on that purchase.

**A transport failure now records its reason.** `release_claim` takes `last_error_code`
(migration 225's sibling change), so a stalled row says *why* rather than only *when to look
again*. The ledger **validates** the code against `^[a-z0-9_:.-]{1,64}` and refuses rather than
folding, which is why `_error_code` folds at the single place that writes the column — this
module builds `transport_error:ReadTimeout` out of an httpx exception type name, and unfolded
that would raise out of every transport release.

`None` on a release means *do not write one*: the release statement COALESCEs, so a buyer still
looking at the hosted page does not erase the transport failure that preceded them.

**A terminal transition writes `last_error_code` without COALESCE.** So a clean completion after
an earlier transport failure **clears** the stale code — a terminal row's code is read as "why
this ended", and a stale one says the purchase failed when it did not. An explicit code on a
terminal write still wins.

---

## Not handled

* **Refunds, cancellations, after-sales.** Nothing in this package can reverse a completed
  purchase. Reap's agentic rail has **no webhooks**, so there is also nothing that will tell us.
* **Recurring / subscription purchases.** One enrollment, many purchases — but each purchase is
  independent and each needs its own buyer approval.
* **Non-Shopify merchants.** A quote 503 (`AGENTIC_SERVICE_UNAVAILABLE`) correlates with
  non-UCP merchants on n=2 measured merchants. Recorded per purchase as
  `merchant_not_completable`; **never** used to suppress a merchant.
* **Reap's same-name-per-colour products.** Reap lists some merchants' products once per colour
  under the same name. `match_product` requires an exact name match and refuses an ambiguous set;
  the remedy is `accept_variant_labels`, **which this package refuses** (see below).
* **Three-decimal currencies** (KWD, BHD, JOD, OMR, TND, LYD, IQD). `start_purchase` refuses them
  with `currency_unsupported`. `_exponent` assumes two decimal places for anything outside the
  repo's zero-decimal list, so 1.234 KWD would be stored as 123 fils rather than 1234 — a tenfold
  error in the partner's favour that stays self-consistent all the way to the charge. Handling
  them means a third exponent through `major_to_minor`, which is the repo's one rounding policy
  and not this package's to widen.
* **Re-quoting after a shipping-option change.** `select_shipping_option` exists on the client and
  is not used here; the quote's default option is taken.
* **Orphaned checkouts.** A crash between `create_checkout` returning 200 and the transition that
  records it leaves a checkout at Reap that no row of ours references. It cannot be recovered by
  replaying the create — the client's idempotency key is derived from `(quoteId, enrollmentId)`,
  and the next step re-resolves and re-quotes, so it arrives with a new quote id. There is no
  double-charge risk: the buyer only ever receives the hosted URL through a row the fence agreed
  to write, and an unapproved checkout expires. The ledger offers no fenced field-only write, so
  the id cannot be persisted before the transition; it is written to the log at INFO
  (`checkout created purchase=… checkout=… quote=…`) so the orphan is at least findable.


---

## The routes — `routes/agent_commerce_reap.py` (WP4)

Three, under `/agent/v2/commerce/reap`, registered in `main.py` next to `agent_commerce_router`.
**The wire contract — every field, every refusal code, worked JSON — is
`docs/reap_agentic_routes.md`.** This section is the operational half: what an operator turns on,
and what the routes decide.

| route | what it does |
|---|---|
| `POST /purchases` | opens a purchase and returns `202` at once. **Makes no partner call.** |
| `GET /purchases/{id}` | the owner's read: state, totals, the current hosted URL, the order reference |
| `GET /purchases?limit=` | the same, for this buyer's recent purchases |

### The dial makes them 404, not 503

While `is_enabled()` is false **or** `rc.is_configured()` is false, all three answer
**404 `not_available_on_this_rail`**. That is not a bug report, it is the design: the agent door's
job on a 404 is to fall back to another rail, and a 503 would read as "this rail is the answer,
retry shortly" and stall a buyer behind a feature nobody has armed. The check is the first
statement of each handler — one per route, not a router-level dependency, so a mutation that
deletes one is killed by its own test rather than by all three at once.

The gate is read **per request**, so arming the rail is an env change and not a redeploy.

### What the routes decide, and what they refuse to trust

| the route will not trust | what it does instead |
|---|---|
| **the price** | reads `catalog_products` → `catalog_skus` → `catalog_offers` for `(merchant_domain, product_key, variant_key)` **and the eligible merchant's own `o.merchant_id`**, with `coalesce(merchant_effective_price, estimated_best_price, list_price)` — the same precedence every other surface in the repo uses. A price in the request body is ignored. |
| **another seller's price** | `catalog_offers.merchant_id` is the offer SELLER and is not `catalog_products.merchant_id`. One sku carries offers from several sellers, and the crawl-mirror and merchant-sync lanes can both hold a copy. Only this merchant's own offer is read; no offer of its own ⇒ `row_unpriced`. |
| **the currency** | the offer must be priced in the currency the buyer's market uses (`US` → `USD`). Otherwise `row_currency_mismatch` — the divergence would otherwise surface at the quote as `price_changed`, naming the wrong cause, after the buyer has entered a card. |
| **the merchant** | `reap_agentic_eligibility` is an allowlist. No enabled row for this domain **in the buyer's market** ⇒ `merchant_not_eligible`. |
| **the buyer** | resolved from `buyer_identity_links` on `(agent_id, hash(agent_user_ref))`. No link ⇒ one is **created** (see below). The agent cannot name a buyer: there is no field for it, and the id minted is random, never derived from `buyer.email`. |
| **the consent** | `buyer.consent_version` is required; missing or malformed ⇒ `consent_required`, decided after the dial and before eligibility, the catalog read and every write. Recorded against the buyer, latest wins. |
| **the variant key** | matched exactly against `catalog_skus.sku_key`, never re-derived: this repo has three live spellings of a variant sku key and they collide. |
| **the storefront** | `catalog_products.platform` must be `shopify`. `external_seed` rows are refused `row_not_shopify` even though most of that cohort really is Shopify — that normalisation needs seed-snapshot evidence the catalog tables do not carry, and this is a charge, not a display. |

The Tier B **cart-link** lane is narrower in a different way: a mirrored `external_seed` is
buyable only when its active market-matched seed is attached to that exact catalog product and
its storefront snapshot has exactly one stamped variant and a fresh, URL-bound `.js` proof
that the live Shopify product itself had exactly one variant. Older stamps without that proof
fail closed until a backfill refresh. An operator-entered numeric
`attached_variant_id` is not proof; if it conflicts with the stamp, the route refuses. The
variant lane described in the table above still refuses external seeds entirely.

**Why a missing buyer link is now a sign-up and not a refusal.** Owner decision, 2026-09-18. Until
WP4b the routes refused `buyer_unlinked`, on the argument that only a buyer-authenticated sign-in
should bind an agent's opaque user ref to an account. The consequence was that **every** agent-only
buyer was refused, which made the rail unarmable for the door it was built for.

The first purchase now creates the link, and with it a **random** buyer id — not an account row,
and nothing derived from `buyer.email`. That last part is load-bearing:
`db/accounts.create_or_get_shop_user` is *create-or-get*, so minting through it would hand an
agent that asserted a stranger's email that stranger's real buyer id, and with it their saved
email and default shipping address through the checkout-intent prefill. An agent-asserted identity
is unverified and gets its own id space.

An existing link always wins — the insert cannot overwrite one, and the route re-reads rather than
trusting what it minted, so two concurrent first purchases yield one buyer, one link, one ref.

**The one cost, and it is now the only one.** If the same human later signs in through the hosted
checkout, that surface repoints the link to their real account, and because
`reap_agentic_buyer_refs` is keyed on `buyer_id` their next purchase mints a fresh ref — so **Reap
asks for the card once more**. One re-enrollment after a sign-in. That is the trade; WP4 avoided it
by refusing those buyers forever.

Since **WP4c** the repoint also cleans up after itself: the stranded enrollment is marked dead and
the stale `reap_agentic_buyer_refs` row is deleted, automatically, at the moment of the repoint.
See "One thing to expect in support" below.

### Storage the routes own — migrations 226 and 227

Three tables, none of them touched by the state machine or the poller. Also in
`db/schema_guard.ensure_required_schema_light` in **both** dialect branches, in their own
try/except, because production deploys skip `db/migrations/`.

| table | what it is |
|---|---|
| `reap_agentic_eligibility` | the allowlist. `(merchant_domain, market_country, product_key, variant_key)` |
| `reap_agentic_buyer_refs` | `buyer_id` → the opaque `owner.id` we send Reap. Minted once, never exposed. Migration **227** adds `consent_version VARCHAR(32)` and `consented_at TIMESTAMPTZ` — the terms the buyer's enrollment was established under, rewritten on every purchase so the pair is always the latest. Nullable only because rows minted before 227 exist; nothing written from now on can be NULL, because the route refuses `consent_required` before it writes. |
| `reap_agentic_purchase_keys` | idempotency, 24 h, scoped to `(agent_id, agent_user_ref_hash, idempotency_key)`, carrying a hash of the request the key was used for — the same key on a different body is `idempotency_conflict`, not a 202 about somebody else's purchase |

`reap_buyer_ref` is a **third** identifier, not the global buyer id and not
`buyer_agent_links.agent_scoped_buyer_ref`. An enrollment is a CARD: an agent-scoped ref would
give one human two refs, two enrollments and two cards, and migration 224's "at most one active
enrollment per buyer_ref" would then hold twice, per agent, which is not the invariant anybody
wanted.

---

## Before arming

Three things are true today, and each one will otherwise be discovered as a mystery refusal.

### 1. The door must send `buyer.consent_version`, or every purchase is refused

This replaces the old item 1, "every agent-only buyer answers `buyer_unlinked`". That is no longer
true: since WP4b the first purchase creates the buyer identity, so an agent with **zero** rows in
`buyer_identity_links` is now perfectly armable. The blocker moved.

**The new blocker is the door.** `buyer.consent_version` is required on every `POST /purchases`,
and a door that does not send it gets `400 consent_required` on every single request — which looks
exactly like a broken rail. Confirm the door sends it **before** you arm anything.

After arming, this is the query that tells you whether consent is actually arriving:

```sql
SELECT consent_version, COUNT(*), MAX(consented_at)
  FROM reap_agentic_buyer_refs
 GROUP BY consent_version
 ORDER BY 2 DESC;
```

A `NULL` group is rows minted before migration 227 — expected, and only for buyers linked by the
hosted checkout before this shipped. A *growing* NULL group is impossible unless the consent write
has been broken; investigate rather than waiting.

To see the identities the rail is creating for an agent:

```sql
SELECT COUNT(*) FROM buyer_identity_links WHERE agent_id = '<agent_id>';
```

Zero no longer means "arming will change nothing" — it means no purchase has been made yet. The
count should climb by one per new end user.

**One thing to expect in support. The cleanup is automatic (WP4c).** A buyer whose identity this
rail created, who *later* signs in through the hosted checkout, gets their link repointed to their
real account by `POST /buyer/save_from_checkout`. Because `reap_agentic_buyer_refs` is keyed on
`buyer_id`, their next purchase mints a fresh ref and asks them to enter the card once more. That
is correct and happens at most once per buyer — **it is not a bug report.**

**Since WP4c the repoint cleans up behind itself.** `routes/buyer_api._upsert_buyer_identity_link`
— the surface that does the repointing — calls
`db.reap_agentic_ledger.retire_buyer_refs_for_buyer(old_buyer_id, reason="buyer_link_repointed")`
inline, and that call:

* marks every **non-dead** enrollment on the old `reap_buyer_ref` `status = 'dead'` with
  `reap_status = 'buyer_link_repointed'`, clearing `hosted_url` — *pending* rows included, because
  a pending row's hosted page is a page a card can still be entered on;
* then **deletes** the old `reap_agentic_buyer_refs` row. A consent tag on a buyer id no link
  mentions is a record nobody can find; the live account records a fresh consent at its next
  purchase. **This does not cost the consent evidence** — since migration **233** every
  `reap_agentic_purchases` row carries its own `consent_version` / `consented_at`, the tag that
  was in force *when that purchase was opened*, and the sweep does not touch that table. The
  refs row's pair is only the **latest** consent, kept for re-use on the next purchase and for
  the cart-link lane's identity check;
* leaves **purchases alone** — a purchase is owned by `(agent_id, agent_user_ref_hash)` on its own
  row, which the repoint does not change.

It is idempotent (a second run reports zeros), it is not transactional by design (ordered
autocommit statements — the `databases` shared-connection rule forbids a transaction here, and the
order is chosen so a crash between statements leaves a state the next run heals), and it cannot
fail or slow the checkout: the hook is wrapped, never re-raises, and makes **no partner call**.

Two log lines, carrying counts and no identifiers:

```
event=reap_buyer_link_repointed refs=1 enrollments=1
event=reap_buyer_link_repointed_kept
```

The `_kept` line is the case the hook **declines**: the old buyer still has links through other
agents, and `reap_agentic_buyer_refs` is keyed on the buyer id rather than on `(agent, ref)`, so
retiring there would kill a card that buyer is actively using elsewhere. Those are left for the
audit below.

**The enrollment at Reap is still a separate step.** We stop using it; the partner is not told to
stop honouring it. Reap *does* offer `POST /agentic/enrollments/{id}/revoke`
(`revokeEnrollment_agentic`, in `tests/fixtures/reap_openapi_agentic_2026_09_25.json`), wrapped as
`services.reap_agentic_client.revoke_enrollment(<reap_enrollment_id>)`. It is deliberately **not**
called from the repoint hook: that hook runs on the hosted checkout's save path with a human
waiting, and a partner POST can take up to the client's 25-second timeout. Run it from a shell
against the ids the audit below turns up.

> **Until `revoke_enrollment` is run, the card is still live at Reap.** Say this plainly to anyone
> asking whether a repointed buyer's old card is "gone": on our side, yes — the enrollment is
> `dead`, its hosted page is cleared, and nothing will quote against it again. At Reap, **no**. The
> partner has not been told to stop honouring it, and nothing in this repo tells them
> automatically. That step is an operator's, from the ids the audit query below turns up, and it
> stays outstanding until somebody runs it.

**The audit query — now the BACKSTOP rather than the containment.** It finds refs whose buyer is no
longer linked and the enrollments hanging off them. After WP4c a healthy database returns
**nothing** from it for repoints that fired. What it still finds, and what it is now *for*:

* the **`_kept`** cases — the old buyer still has links through other agents, so the hook declined
  on purpose. If such a buyer later loses those links too, this query is what notices;
* rows **stranded before WP4c** shipped;
* a repoint whose hook **failed** — the hook swallows everything (`event=reap_buyer_link_repoint_failed`
  in the logs), by design, so a failure leaves exactly the orphan this query describes;
* a repoint through the **fallback write arm**. On Postgres that arm reports no rowcount for an
  UPDATE that landed, so it can repoint the link and return `None` **without firing the hook**. It
  is not reached on a healthy database — the `ON CONFLICT` arm is — but a *transient* failure of
  the primary write (a dropped connection, a statement timeout, a serialization error, pool
  exhaustion) falls through to it on an otherwise fine database. Pinned by
  `tests/test_reap_buyer_link_repoint_postgres.py::test_the_fallback_arm_is_unreachable_on_postgres`;
* a request **cancelled between the upsert and the retire**. `asyncio.CancelledError` is a
  `BaseException`, so the hook's `except Exception` does not catch it — deliberately: a cancelled
  request must not be turned into a completed one. The write landed, the sweep did not run.

So: run it periodically, not only when somebody complains. It is cheap and it is the only thing
watching those last three paths.

```sql
SELECT r.buyer_id, r.reap_buyer_ref, r.consent_version, r.created_at,
       e.id AS enrollment_id, e.status, e.card_network, e.card_last4
  FROM reap_agentic_buyer_refs r
  LEFT JOIN reap_agentic_enrollments e ON e.buyer_ref = r.reap_buyer_ref
 WHERE NOT EXISTS (
         SELECT 1 FROM buyer_identity_links l WHERE l.buyer_id = r.buyer_id
       )
 ORDER BY r.created_at;
```

Retire what that turns up through the ledger, **not** with a hand-written UPDATE. For a whole
stranded identity, use the WP4c sweep — it is the same code the hook runs, it is idempotent, and it
handles the enrollments and the refs row in the safe order:

```python
from db.reap_agentic_ledger import retire_buyer_refs_for_buyer
report = await retire_buyer_refs_for_buyer("<buyer_id>", reason="orphaned_by_buyer_repoint")
print(report.refs_retired, report.enrollments_marked_dead)   # ints only, by design
```

`reason` is written into `reap_agentic_enrollments.reap_status` and must match
`^[a-z0-9_:.-]{1,64}\Z` — it is refused rather than folded or truncated, because a truncated reason
names a different reason.

> **`\Z`, not `$`, and copy it that way.** Without `re.MULTILINE`, Python's `$` also matches just
> *before* a newline that ends the string, so `…{1,64}$` **accepts** `"buyer_link_repointed\n"`
> while `\Z` refuses it. A reason ending in a newline reaches `reap_status` and every log line and
> report that column feeds — it is the forged-log-line primitive. An ad hoc script that re-spelled
> this check with `$` would be subtly wrong; the ledger uses `\Z` and so does this page. Use `orphaned_by_buyer_repoint` for a hand-run sweep so it is
distinguishable from the hook's own `buyer_link_repointed`.

For ONE enrollment and nothing else, `mark_enrollment_dead` is still the right call — idempotent,
keeps the status vocabulary's `CHECK` honest, returns `None` when the row was already dead:

```python
from db.reap_agentic_ledger import mark_enrollment_dead
await mark_enrollment_dead("<enrollment_id>", reap_status="orphaned_by_buyer_repoint")
```

Then, optionally, tell Reap:

```python
from services import reap_agentic_client as rc
result = await rc.revoke_enrollment("<reap_enrollment_id>")   # the PARTNER's id, a uuid
```

**Note what changed in WP4c and read it before reaching for an older copy of this page.** This
section used to say "leave the `reap_agentic_buyer_refs` row alone — it is the consent record".
WP4c reverses that on the owner's decision (2026-09-22): the row is deleted, because a consent tag
on a buyer id no link mentions is not a record anybody can find, and the account the buyer actually
uses always carries a current one.

**Older copies of this page said the delete costs the consent evidence. Migration 233 removed that
cost** — the evidence lives on the purchase row now, and the refs row's tag is only the latest,
for re-use. To read what a given purchase was opened under:

```sql
SELECT id, state, merchant_domain, product_name,
       consent_version, consented_at, created_at, terminal_at
  FROM reap_agentic_purchases
 WHERE id = '<rp_…>';
```

`consent_version` is `NULL` **only** on rows opened before 233 — nothing the rail opens now can
have one, because `services/reap_agentic_purchase.start_purchase` refuses `consent_required` on
both lanes before the `INSERT`. `consented_at` is aware UTC on both dialects. Neither column is
ever rewritten: they are absent from the transition statement's field list, so no poller step can
revise them, and the terminal write that `NULL`s `shipping_address` and `buyer_email` leaves them
alone. A completed purchase therefore keeps its consent and none of the buyer's PII. To census the
rail:

```sql
SELECT consent_version, COUNT(*), MIN(created_at), MAX(created_at)
  FROM reap_agentic_purchases
 GROUP BY consent_version
 ORDER BY 2 DESC;
```

A **growing** `NULL` group here means the rail is opening purchases with no consent evidence,
which the service is supposed to make impossible — treat it the way you would treat a growing
`NULL` group on the buyer-refs census above.

### 2. The merchant must have an offer of its own, in the market's currency

The route reads only the eligible merchant's own `catalog_offers` row (`o.merchant_id =
p.merchant_id`), because `catalog_offers.merchant_id` is the offer SELLER and one sku can carry
several. Verify before arming:

```sql
SELECT o.merchant_id, o.currency,
       coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price) AS price
  FROM catalog_products p
  JOIN catalog_skus s   ON s.product_key = p.product_key
  JOIN catalog_offers o ON o.sku_key = s.sku_key
 WHERE lower(p.source_domain) = '<domain>'
 ORDER BY o.merchant_id;
```

Rows whose `merchant_id` is not the product's own are other sellers and are invisible to this
route; if the product's own merchant has none, every purchase answers `row_unpriced`.

The currency must be the one the market uses — `US` → `USD` — or the answer is
`row_currency_mismatch`. **The map is in code**, `_MARKET_CURRENCY` in
`routes/agent_commerce_reap.py`, and it **fails closed**: a market that is not in it refuses every
purchase. Adding one is a one-line change.

> **The SG case specifically.** The curated lane writes USD rows. An SG merchant whose offers are
> denominated in USD will refuse `row_currency_mismatch`, and that refusal is CORRECT — an SGD
> storefront quoted in USD is exactly the divergence the check exists for — but it will look like
> a bug. Fix the catalogue, not the check.

### 3. The domain must name the same merchant as `catalog_products.source_domain`

Matched **canonically**: lower case, **one** leading `www.` removed, on both sides. The Shopify sync
writes `source_domain` as Shopify's `shop_domain` — `www.Brand.com` in production — and the
eligibility row, the request and the catalog row are all folded to `brand.com` before they are
compared. `wwwbrand.com` is a different store (no dot after the prefix), and `www.www.brand.com`
folds to `www.brand.com`, never to `brand.com`. A domain that still does not match answers
`row_not_found` for every product on it, which reads as "we do not have this merchant" rather than
"the eligibility row is wrong". A request `merchant_domain` that is not a bare host name
(`https://…`, a port, a path, userinfo, an IP, one label) is refused `invalid_request` before any
read.

### Before arming: eligibility rows need a fresh positive purchasability fact

With `MERCHANT_PURCHASABILITY_ENFORCE` on, an enabled `reap_agentic_eligibility` row — or a Tier B
`ELIGIBLE` verdict — is **necessary but not sufficient**. `POST /agent/v2/commerce/reap/purchases`
additionally refuses **`merchant_not_purchasable` (409)** unless
`db.merchant_purchasability.is_purchasable(domain, market)` is true, and that needs a **fresh
positive fact FROM THE BUYER VANTAGE**: a checkout we actually rendered whose own accept-list named
a card gateway, at the price we hold, gathered from the egress named by
`MERCHANT_PURCHASABILITY_BUYER_VANTAGE` and not yet aged out. The check runs **before** either
lane's eligibility, because it is the broader refusal — both allowlists say a merchant is
*permitted*, and neither says its checkout can be *paid*; it is a separate refusal code from
`merchant_not_eligible` so an operator can tell "nobody listed this merchant" from "this merchant
is listed and we cannot prove it can be paid". Note that arming that dial arms the gathering and
the enforcement at the same time, so every merchant answers `merchant_not_purchasable` until the
first sweep tick lands its fact. See **docs/runbooks/merchant_purchasability.md** for the rule, the
vantage, and the rollback, and read a merchant's current state with
`GET /ops/merchant-purchasability?domain=&market=` (admin auth) — its `tier` field is computed
through the same `is_purchasable` this route calls.

---

## Enabling a merchant

There is **no admin route** for this in WP4 — deliberately. Arming a domain is the step that lets
a buyer's own card be spent at that merchant, and it should leave a row somebody wrote on purpose.

```sql
-- 1. THE MERCHANT ROW. This is the row eligibility is decided against. product_key and
--    variant_key are '' (the sentinel for "the whole merchant" — NOT NULL, which does not behave
--    the same on both dialects inside a primary key).
INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key,
                                      market_country, enabled)
VALUES ('brand.example', '', '', 'US', TRUE)
ON CONFLICT (merchant_domain, market_country, product_key, variant_key)
DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now();
```

* `merchant_domain` is written **canonical**: lower case, bare host, **no leading `www.`** —
  `brand.example`, never `www.Brand.example`. The route folds the stored column as well, so a
  `www.` row still matches, but the canonical spelling is the one every runbook query below and
  the purchasability sweep's population agree on, and it is the only way to have exactly one row
  per merchant. (The one exception: a storefront whose host itself begins `www.www.` is written as
  observed, because the fold strips only one `www.`.) Check it names the same merchant as the
  catalog first:
  `SELECT DISTINCT source_domain, platform FROM catalog_products WHERE merchant_id = '<id>';`
  `www.Brand.example` there and `brand.example` here are the same merchant. A domain that does not
  match answers `row_not_found` for every product on it. Run the offer and currency check under
  **Before arming** at the same time.
* `market_country` is ISO-3166-1 alpha-2, **uppercase**, and the match is EQUALITY against the
  buyer's `shipping_address.country`. **Domestic only.** A merchant that sells into two markets
  gets two rows.
* `enabled` defaults to `FALSE`. A row somebody created and did not finish thinking about is not
  an authorization to spend.

```sql
-- 2. OPTIONAL: per-product resolution aliases. These do NOT grant eligibility — `enabled` is
--    read from the merchant row only — they only widen what the resolver will accept as naming
--    the same object. Leave variant_key '' to apply to every variant of the product.
INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key,
                                      market_country, enabled,
                                      accept_variant_labels, also_accept_domains)
VALUES ('brand.example', 'prod::m_brand::shopify::1001', '', 'US', FALSE,
        '["Standard 50ml"]'::jsonb, '["shop.brand.example"]'::jsonb);
```

The ledger bounds these: at most 32 entries of 128 characters, no control characters, domains
lowercased and hostname-shaped (no scheme, path or port). It **raises rather than truncating**, and
the route maps that to `invalid_request` — so a malformed operator row refuses the purchase rather
than quietly resolving without the alias it was created to supply.

```sql
-- 3. TURNING A MERCHANT OFF. Purchases already in flight are NOT affected: eligibility is read
--    once, at POST. They continue, and the poller finishes them. Folded, so a `www.` twin row is
--    turned off too — and a disabled twin is enough on its own: the route refuses a merchant if
--    ANY of its merchant rows in that market is disabled.
UPDATE reap_agentic_eligibility
   SET enabled = FALSE, updated_at = now()
 WHERE CASE WHEN lower(merchant_domain) LIKE 'www.%'
            THEN substr(lower(merchant_domain), 5)
            ELSE lower(merchant_domain) END = 'brand.example';

-- 4. WHAT IS ARMED RIGHT NOW.
SELECT merchant_domain, market_country, enabled, updated_at
  FROM reap_agentic_eligibility
 WHERE product_key = '' ORDER BY merchant_domain;
```

### One-off: canonicalise rows written before canonical matching

Rows are operator-entered, so there is no migration for this; run it once, by hand, on each
environment that has eligibility rows. The route matches canonically either way — this is for the
"one row per merchant" property the runbook queries and the sweep population rely on.

**Order: (a) census, (b) merge every twin group BY HAND, (c) canonicalise.** A twin group is two
or more rows that fold to the same canonical key — `brand.example` + `www.brand.example`, but
also two non-canonical spellings with NO bare row at all (`www.dup.example` + `WWW.Dup.example`).
The UPDATE in (c) would try to give both the same primary key and abort the whole statement, so
(c) refuses to touch any member of a twin group and must run only after (a) shows none.

```sql
-- a. CENSUS, GROUPED BY THE CANONICAL FORM. Every group that is either a twin group
--    (spellings > 1) or holds a non-canonical spelling. A disabled member turns the merchant
--    off at the route (every merchant row must be enabled), so read `enabled_flags` first.
SELECT canonical, market_country, product_key, variant_key,
       count(*)                                          AS spellings,
       string_agg(merchant_domain, ', ' ORDER BY merchant_domain) AS stored_as,
       string_agg(enabled::text, ', ' ORDER BY merchant_domain)   AS enabled_flags
  FROM (SELECT e.*,
               CASE WHEN lower(e.merchant_domain) LIKE 'www.%'
                    THEN substr(lower(e.merchant_domain), 5)
                    ELSE lower(e.merchant_domain) END AS canonical
          FROM reap_agentic_eligibility e) f
 GROUP BY canonical, market_country, product_key, variant_key
HAVING count(*) > 1 OR bool_or(merchant_domain <> canonical)
 ORDER BY spellings DESC, canonical, market_country;

-- b. MERGE EVERY GROUP WITH spellings > 1 BY HAND: decide the one `enabled` and the one set of
--    aliases that are right, UPDATE one member to carry them, DELETE the others. Re-run (a) until
--    every remaining row has spellings = 1.

-- c. CANONICALISE, in one transaction. Refuses any row that still has a twin (any OTHER row
--    folding to the same key) and any `www.www.` host (its fold is not a fixed point; it stays
--    as observed). Expect the UPDATE count to equal (a)'s rows with spellings = 1 that do not
--    begin `www.www.`.
BEGIN;
UPDATE reap_agentic_eligibility e
   SET merchant_domain = CASE WHEN lower(e.merchant_domain) LIKE 'www.%'
                              THEN substr(lower(e.merchant_domain), 5)
                              ELSE lower(e.merchant_domain) END,
       updated_at = now()
 WHERE e.merchant_domain <> CASE WHEN lower(e.merchant_domain) LIKE 'www.%'
                                 THEN substr(lower(e.merchant_domain), 5)
                                 ELSE lower(e.merchant_domain) END
   AND lower(e.merchant_domain) NOT LIKE 'www.www.%'
   AND NOT EXISTS (
       SELECT 1 FROM reap_agentic_eligibility t
        WHERE t.merchant_domain <> e.merchant_domain
          AND CASE WHEN lower(t.merchant_domain) LIKE 'www.%'
                   THEN substr(lower(t.merchant_domain), 5)
                   ELSE lower(t.merchant_domain) END
            = CASE WHEN lower(e.merchant_domain) LIKE 'www.%'
                   THEN substr(lower(e.merchant_domain), 5)
                   ELSE lower(e.merchant_domain) END
          AND t.market_country = e.market_country
          AND t.product_key = e.product_key
          AND t.variant_key = e.variant_key
   );
-- re-run (a): it must return only `www.www.` rows you chose to leave. Then:
COMMIT;
```

The sweep reports allowlist rows it cannot use (a URL, a port, a trailing dot) as a COUNT,
`population_skipped_unusable`, with one warning per run and no domains. The route can never admit
such a row. To name them (a coarse shape check; the authority is
`services.tierb_cart_link_merchants.canonical_merchant_domain`):

```sql
SELECT merchant_domain, market_country, product_key, enabled
  FROM reap_agentic_eligibility
 WHERE merchant_domain ~ '[^A-Za-z0-9.-]'      -- scheme, path, port, userinfo, whitespace
    OR merchant_domain LIKE '%.'               -- trailing dot
    OR merchant_domain LIKE '%..%'
    OR merchant_domain NOT LIKE '%.%'          -- a single label
 ORDER BY merchant_domain;
```

> **Never `DELETE FROM reap_agentic_buyer_refs`.** It is the only record of which opaque owner id
> Reap knows a buyer by. Dropping a row strands that buyer's enrollment at Reap and asks somebody
> who has already given us a card to enter it again.

---

## Cart-link attribution safety (migration 230)

One cart-link sale can be reported under a Reap order ID and a Shopify order ID. A permanent
`conversion_click_claims` row gives the click to the first closer; **never delete or release a
claim to retry**. Releasing one can let the other channel write a second edge. The Reap close
uses the click's recorded `seller_ref` as its merchant identity when present, after checking that
the recorded click destination is the stored cart URL's shop. Legacy clicks retain their prior
domain-based close. Reap provenance is stored under `metadata.partner_provenance`, not accepted
from Shopify order data.

If the merchant webhook cannot read the cart-link scope or take the claim, it still acknowledges
the paid order, but **defers attribution**. The read_orders poller holds its watermark when it
sees that claim failure, and retries the window on its next run. This protects against two GMV
edges during a transient claim-table failure. Monitor the poller's `claim_unavailable` and
`watermark_held` signals; if the poller is not running, arrange an operator replay of the paid
order after the claim store recovers.

A Reap purchase can complete but fail to write its attribution edge (for example, if the process
dies after the terminal write). The claim remains held, so the merchant channel cannot repair it.
Set `DATABASE_URL` explicitly to the intended database, then run
`python -m scripts.reconcile_reap_cart_link_claims --limit 100`. This is read-only and returns a
`ready` row only for a unique completed cart-link purchase with the same claimed click/order,
an intact URL/shop/quantity, a seller-keyed click on that shop, a charged amount within one minor
unit of the quote, and no OTHER
edge on that click. Investigate every `skipped` row; never delete or reassign a claim. After
confirming the partner order independently, run the same command with `--apply`. It rechecks the
claim and existing edges before the idempotent close and reports `repaired` only when the edge
can be read back. This procedure does not call Reap or change a charge. The merchant-owned claim
path remains with webhook/poller replay, not this repair command. Do not treat this repair as
proof of the Reap cart-link quote contract or as authorization for a paid canary.

## Local end-to-end run against the sandbox

`scripts/ops/reap_local_e2e.py` runs the whole machine on a laptop — purchase route → ledger →
poller → Reap **sandbox** → `completed` + attribution edge — against a LOCAL database, with a human
approving on Reap's hosted page. Nothing is deployed, nothing touches the prod database, and
**nothing is charged**: it is the sandbox, and with `X-Simulate-Checkout: COMPLETED` the merchant
order is simulated. Pivota never sees card data; the script prints hosted URLs and never opens them.

What it refuses, before anything is built or sent:

* a `DATABASE_URL` that is not a SQLite **file** or a Postgres on exactly `localhost` /
  `127.0.0.1` / `::1` (dotted hosts, private IPs, `user@remote`, more than one `@`,
  `?host=`/`?hostaddr=`/`?service=` overrides, multi-host lists and host-less URLs all refuse).
  The full table is `REFUSED_DATABASE_URLS` in the script. The check is repeated as the FIRST
  statement of every writer (`build_schema`, `seed_rows`, the in-process purchase, the poll loop),
  against the URL `db.database` actually bound;
* a Reap base URL — from `--reap-base-url`, your shell's `REAP_API_BASE_URL`, or the env file —
  that is not exactly `https://sandbox.api.reap.global`;
* egress from the poll process to any host but the sandbox;
* **any command at all while `REAP_API_KEY` is exported in your shell.** A shell key has no
  provenance (it may be a production key), so it is refused rather than ignored: unset it.

The Reap key is **loaded at runtime, by `poll`/`run` only, and only from the env file**
`~/.config/pivota/reap_sandbox.env` (or `$REAP_SANDBOX_ENV`). If that file names a
`REAP_API_BASE_URL`/`REAP_API_BASE` that is not the sandbox, the key beside it is refused. `serve`
**never** holds the real key: it makes no Reap call (the routes only write the ledger and the
scheduler registers no jobs), so it gets a placeholder that is just enough to arm the route. The
key is never printed, never written to the state file, and `Authorization` is redacted in the
call log. `serve` and the harness process both run on an **allowlisted** environment
(PATH/HOME/locale/proxy/CA vars plus the keys below; the harness's own `os.environ` is cleared
first), so a shell exporting a production `DATABASE_URL`, `REDIS_URL`, `SENTRY_DSN`, a Cloud Run
marker, or libpq's `PGHOST`/`PGHOSTADDR`/`PGSERVICE`/`PGPASSFILE` (psycopg2 honours `PGHOSTADDR`
and `PGSERVICE` even beside `host=localhost`) leaks nothing into the run.

### The commands, in order

```
PY=.venv/bin/python
# 1. schema + rows + local agent key + buyer JWT (printed: LOCAL test credentials only)
$PY scripts/ops/reap_local_e2e.py seed --reset --seed-enrollment <reap enrollment uuid>
#    (omit --seed-enrollment to go through card entry instead; see below)
# 2. terminal 1 — the app on 127.0.0.1:8765; leave it running
$PY scripts/ops/reap_local_e2e.py serve
# 3. terminal 2 — POST the purchase over HTTP, then drive the poller in-process
$PY scripts/ops/reap_local_e2e.py run            # = `purchase` then `poll`
```

State lives in `$TMPDIR/pivota-reap-local-e2e/` (`--state-dir` to move it; with `TMPDIR` unset the
script refuses rather than fall back to a shared `/tmp`). The directory is created **0700** and
must be yours: an existing one that is a symlink, someone else's, or has group/other bits is
refused. Every file in it — `local.db` (created 0600 before anything writes it), `state.json`,
`jwks.json`, `signing_key.pem` — is 0600, written to a temp file and renamed into place. Relative
`--state-dir`, `--reap-log` and SQLite paths are resolved against YOUR cwd before the script
changes into the repo. Every Reap call is logged, redacted, to `./reap_local_e2e_<ts>.json` in
the directory you ran from (0600, git-ignored; `--reap-log` to choose). The bodies contain the test
buyer's address and email.

**A local Postgres must be a THROWAWAY database** (`--database-url postgresql://localhost/<db>` on
`seed`; the later commands read it back from `state.json`). `seed` runs `metadata.create_all` over
every table `main` registers and then `ensure_required_schema_light`, which issues `CREATE` and
`ALTER TABLE` statements, and it **deletes rows by key** before inserting its own:

| table | rows deleted |
|---|---|
| `catalog_offers` | `offer_id = off_sku::prod::<merchant-id>::shopify::<source-product-id>::v1` |
| `catalog_skus` | `sku_key = sku::prod::<merchant-id>::shopify::<source-product-id>::v1` |
| `catalog_products` | `product_key = prod::<merchant-id>::shopify::<source-product-id>` |
| `catalog_merchants` | `merchant_id = <merchant-id>` (default `m_local_fashionnova`) |
| `reap_agentic_eligibility` | `merchant_domain = <domain> AND market_country = 'US'` |
| `reap_agentic_buyer_refs` (with `--seed-enrollment`) | the local buyer's row, and ANY row whose `reap_buyer_ref` is `--buyer-ref` |
| `reap_agentic_enrollments` (with `--seed-enrollment`) | the row whose `reap_enrollment_id` is the one given |

Never point it at a database whose contents you want to keep — the host check stops a remote
database, not a local copy of one.

`seed` defaults are the product measured quotable in the sandbox on 2026-09-25 (its index holds
fashion merchants only): `fashionnova.com`, "Maven Lipstick - Snatched", variant `OS`, $1.98.
`--product-title`, `--variant-title` (must equal Reap's option label **exactly**; `''` for a
product with no variants), `--price`, `--brand`, `--category` override them.

`serve` sets exactly: `DATABASE_URL` (local), `PIVOTA_ENV=development`, `REAP_AGENTIC_ENABLED=1`,
`REAP_API_BASE_URL=https://sandbox.api.reap.global`, `REAP_API_KEY` (a **placeholder**, never the
real key), `serve` refuses any `--host` but loopback,
`REAP_AGENTIC_SIMULATE_CHECKOUT=COMPLETED`, `AUDIT_WORKER_ENABLED=false` (the scheduler registers
**no** jobs — the poller is driven by hand), `SKIP_HEAVY_STARTUP_INIT=true`,
`AGENT_USER_JWKS_FILE` / `AGENT_USER_JWT_ISSUER` / `AGENT_USER_JWT_AUDIENCE` (the local buyer-JWT
issuer `seed` created), `NO_PROXY`/`no_proxy` (this Mac's proxy drops `reap.global`), and
`MVP_EVENTS_FILE`. `MERCHANT_PURCHASABILITY_ENFORCE` is left unset. On SQLite the boot prints a
best-effort `no such table: webhook_events` traceback; it is harmless.

### Enrollment: reuse, or card entry

* **`--seed-enrollment <uuid>` (preferred).** `seed` writes the chain the route and the poller
  walk: `buyer_identity_links` (agent, hash(`<iss>:<sub>`)) → a buyer id minted by the route's own
  `_buyer_id_for`; `reap_agentic_buyer_refs` buyer → `pivota-probe-buyer-001` (`--buyer-ref`), the
  owner the sandbox enrollment already belongs to; and an **active** `reap_agentic_enrollments`
  row via `upsert_pending_enrollment` + `mark_enrollment_active`, bound to that Reap enrollment id.
  The purchase then goes `resolving → quoting` with no card page, and the checkout is created
  against that enrollment. The id must be the full UUID of an enrollment that is ACTIVE at Reap
  (the two known ones start `5a3637e1` and `a5166ce3`).
* **No flag.** The route mints a fresh buyer ref, the poller creates an enrollment, and `poll`
  prints a **CARD ENTRY** URL. A human opens it and types their own (sandbox test) card; the next
  `needs_enrollment` poll (every 30 s) sees ACTIVE and moves on.

### What the human does, and the timings to expect

`poll` narrates each transition. With a seeded enrollment, on the real cadence:

| transition | when | why |
|---|---|---|
| `resolving → quoting` | first tick (≤ 5 s) | search → details → variant (each search up to ~9 s) |
| `quoting → awaiting_approval` | next tick | quote (13–16 s) and checkout created **in the same step** |
| — human — | **within 5 minutes** | open the printed **APPROVE** URL and approve. `poll` prints the quote total ($8.97 = 1.98 + 6.99 shipping), the quote expiry, the page expiry and **APPROVE BEFORE**: the quote's expiry, not the page's. An unapproved checkout goes FAILED 1–10 s after the quote expires (`last_error_code=approval_window_lapsed`) |
| `awaiting_approval → processing` | next 30 s poll after approval | |
| `processing → completed` | ~70 s after approval (polled every 15 s) | the sandbox places the simulated order and returns an `orderId` |

`poll` stops at a terminal state or `--timeout` (default 900 s). A timed-out row is left as is;
`poll` again resumes it (`--purchase-id` to pick one).

### Reading the attribution edge

On `completed`, `poll` prints the ledger row's public columns (`PUBLIC_PURCHASE_COLUMNS`), the
order reference (Reap's `orderId`), and every `commerce_attribution_edges` row with that
`external_order_id`: `merchant_id` = the canonical merchant (`fashionnova.com`), `agent_id` = the
local agent (`agent_source: partner_purchase`), `gross_attributed_gmv_cents` = the final total
(897), `metadata.partner_provenance` = the purchase id and Reap checkout id.

**On SQLite there is no edge, and that is expected.** `_CLOSE_EXTERNAL_CONVERSION_SQL` is
Postgres-only (`'[]'::jsonb`), so `_close_attribution` logs `error_type=OperationalError` and the
purchase still completes. Seed with `--database-url postgresql://localhost/<db>` to see the edge.
On Postgres, a completed row with no edge names the reason in `last_error_code` (see
["The three codes that suppress the attribution edge"](#the-three-codes-that-suppress-the-attribution-edge)).

### Rehearsing without the key

`--dry-run` on `purchase` / `poll` / `run` replaces Reap with an in-process fake that sits
**under** the real client (search, option matching, quote verification, the simulate header and
the hosted-URL allowlist all run), simulates the human (card entry on the 2nd enrollment read,
approval on the 2nd checkout read), and posts the purchase through the real app in-process with
the real agent-key and JWT auth. No network, no key; `--fast` (dry run only) shrinks the per-state
poll intervals to 1 s. Because `serve` never holds the real key, the HTTP path is rehearsed
without it as `serve`, then `purchase`, then `poll --dry-run`.

## Tests

| file | dialect | what it is for |
|---|---|---|
| `tests/test_reap_agentic_purchase.py` | SQLite | every state, every refusal, the fence under interleaving, PII, the backoff table |
| `tests/test_reap_agentic_purchase_postgres.py` | Postgres (dialect gate) | the fence across **two backend connections**, jsonb-as-text, the server-side clock, the partial unique index, PREPARE |
| `tests/test_reap_agentic_purchase_poll.py` | SQLite | the poller: the gate (step 4 only), the run order, the counts, the dials and their bounds, the budget, the leftover-claims invariant, cancellation, registration |
| `tests/test_reap_agentic_purchase_poll_postgres.py` | Postgres (dialect gate) | the poller across **two real backend connections**, its SQL constants under PREPARE, the error backoff against the server clock, `include_processing=False` on the real statement, the PII deadline with the rail off, claim release on cancellation |
| `tests/test_agent_commerce_reap_routes.py` | SQLite | the three routes over the real app: the router is MOUNTED, the 404 on all three while dark **and for every shape of malformed input**, the ownership conjuncts, eligibility and the market, the price coming from our catalog and from THIS merchant's own offer, the market-currency rule, the buyer ref, idempotency including the request-hash conflict, unprintable identifiers, the hosted-URL vetting, the per-statement self-heal, and that no response or log line carries the buyer; **WP4b**: the first purchase minting exactly one buyer/link/ref, the second reusing them, a hosted-checkout link never being re-minted, two agents sharing a user ref getting two buyers, a link racing in at the write seam winning, and consent being required, ordered after the dial, and stored |
| `tests/test_agent_commerce_reap_routes_postgres.py` | Postgres (dialect gate) | migrations 226+227 vs the self-heal through the **catalog** (columns, `indexdef`, `pg_get_constraintdef`), the `numeric`→`Decimal` price path the `CAST` exists for, the `market_country` regex CHECK, **a NUL byte in an identifier being a refusal and not a 500** (asyncpg raises where SQLite stores it happily, so only this arm can see it), and every security-relevant refusal re-run on the production dialect; **WP4b**: the mint against the REAL unique constraint (`ON CONFLICT DO NOTHING` as Postgres implements it), the `VARCHAR(32)` consent cap refusing rather than truncating, and `consented_at` being a real aware `timestamptz` |

| `tests/test_reap_local_e2e_harness.py` | SQLite | the local e2e harness: both safety guards (local DB, sandbox-only base), the allowlisted env, call-log redaction, sandbox-only egress, `seed` read back through the route's own catalog SQL, and `run --dry-run` driven to `completed` with and without a seeded enrollment |

All four drive the **real** ledger and the client's **real** pure helpers; only the client's six
transport functions are faked, and an autouse fixture makes an unpatched `httpx.AsyncClient`
raise so a step that reached the network fails rather than hangs.

```
.venv/bin/python -m pytest tests/test_reap_agentic_purchase.py \
                          tests/test_reap_agentic_purchase_poll.py
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_reap_wp2b_test \
    .venv/bin/python -m pytest tests/test_reap_agentic_purchase_postgres.py
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_reap_wp3_test \
    .venv/bin/python -m pytest tests/test_reap_agentic_purchase_poll_postgres.py

.venv/bin/python -m pytest tests/test_agent_commerce_reap_routes.py
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_reap_wp4b_test \
    .venv/bin/python -m pytest tests/test_agent_commerce_reap_routes_postgres.py
```
