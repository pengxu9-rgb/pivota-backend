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
| `REAP_RETURN_URL_HOSTS` | `agent.pivota.cc` | host allowlist for our own `returnUrl`. Empty/unset means the default, not "no hosts". |

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
`reap_quote_expires_at`, `reap_order_id`, `refusal_reason`, `last_error_code`, timestamps.

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
`checkout_failed`, `checkout_expired`, `checkout_id_missing`, `quote_id_missing`, `quote_expired`,
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
| **the buyer** | resolved from `buyer_identity_links` on `(agent_id, hash(agent_user_ref))`. No link ⇒ `buyer_unlinked` — the routes never create one (see below). |
| **the variant key** | matched exactly against `catalog_skus.sku_key`, never re-derived: this repo has three live spellings of a variant sku key and they collide. |
| **the storefront** | `catalog_products.platform` must be `shopify`. `external_seed` rows are refused `row_not_shopify` even though most of that cohort really is Shopify — that normalisation needs seed-snapshot evidence the catalog tables do not carry, and this is a charge, not a display. |

**Why a missing buyer link is a refusal and not a sign-up.** The only writer of
`buyer_identity_links` is `routes/buyer_api._upsert_buyer_identity_link`, reached from a
buyer-authenticated surface — the buyer signs in, and that is what binds the agent's opaque user
ref to a real account. Minting a link from an agent's assertion alone would hang a stored card off
a buyer account nothing else knows about, and the same human signing in tomorrow would get a
second account and be asked for their card again.

### Storage the routes own — migration 226

Three tables, none of them touched by the state machine or the poller. Also in
`db/schema_guard.ensure_required_schema_light` in **both** dialect branches, in their own
try/except, because production deploys skip `db/migrations/`.

| table | what it is |
|---|---|
| `reap_agentic_eligibility` | the allowlist. `(merchant_domain, market_country, product_key, variant_key)` |
| `reap_agentic_buyer_refs` | `buyer_id` → the opaque `owner.id` we send Reap. Minted once, never exposed. |
| `reap_agentic_purchase_keys` | idempotency, 24 h, scoped to `(agent_id, agent_user_ref_hash, idempotency_key)`, carrying a hash of the request the key was used for — the same key on a different body is `idempotency_conflict`, not a 202 about somebody else's purchase |

`reap_buyer_ref` is a **third** identifier, not the global buyer id and not
`buyer_agent_links.agent_scoped_buyer_ref`. An enrollment is a CARD: an agent-scoped ref would
give one human two refs, two enrollments and two cards, and migration 224's "at most one active
enrollment per buyer_ref" would then hold twice, per agent, which is not the invariant anybody
wanted.

---

## Before arming

Three things are true today, and each one will otherwise be discovered as a mystery refusal.

### 1. Every agent-only buyer answers `buyer_unlinked`

`buyer_identity_links` has exactly one writer: `routes/buyer_api._upsert_buyer_identity_link`,
reached from `POST /buyer/save_from_checkout` under **buyer authentication**. The link is created
when a human signs in and consents; nothing an agent presents can create one.

**The refusal stays.** A Reap enrollment is a STORED CARD. Minting a link from an agent's bare
assertion would hang that card off a buyer account nothing else in the system knows about, and the
same human signing in tomorrow would get a second account and be asked for their card again.

So: **this rail cannot be armed for Minds buyers until there is a buyer-authenticated enrollment
step** — a point in the flow where the buyer themselves establishes the link, after which the
agent's user token resolves to a real buyer. Until then every `POST /purchases` from an agent-only
session answers `409 buyer_unlinked`, the door falls back, and that is correct behaviour rather
than something to route around. Check before you arm anything:

```sql
SELECT COUNT(*) FROM buyer_identity_links WHERE agent_id = '<agent_id>';
```

Zero means arming the rail for that agent will change nothing at all.

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

### 3. The domain must match `catalog_products.source_domain`

Lowercased, exactly. A domain that does not match answers `row_not_found` for every product on it,
which reads as "we do not have this merchant" rather than "the eligibility row is wrong".

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

* `merchant_domain` must match `catalog_products.source_domain`, **lowercased**. Check it first:
  `SELECT DISTINCT source_domain, platform FROM catalog_products WHERE merchant_id = '<id>';`
  A domain that does not match answers `row_not_found` for every product on it. Run the offer and
  currency check under **Before arming** at the same time.
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
--    once, at POST. They continue, and the poller finishes them.
UPDATE reap_agentic_eligibility
   SET enabled = FALSE, updated_at = now()
 WHERE merchant_domain = 'brand.example';

-- 4. WHAT IS ARMED RIGHT NOW.
SELECT merchant_domain, market_country, enabled, updated_at
  FROM reap_agentic_eligibility
 WHERE product_key = '' ORDER BY merchant_domain;
```

> **Never `DELETE FROM reap_agentic_buyer_refs`.** It is the only record of which opaque owner id
> Reap knows a buyer by. Dropping a row strands that buyer's enrollment at Reap and asks somebody
> who has already given us a card to enter it again.

---

## Tests

| file | dialect | what it is for |
|---|---|---|
| `tests/test_reap_agentic_purchase.py` | SQLite | every state, every refusal, the fence under interleaving, PII, the backoff table |
| `tests/test_reap_agentic_purchase_postgres.py` | Postgres (dialect gate) | the fence across **two backend connections**, jsonb-as-text, the server-side clock, the partial unique index, PREPARE |
| `tests/test_reap_agentic_purchase_poll.py` | SQLite | the poller: the gate (step 4 only), the run order, the counts, the dials and their bounds, the budget, the leftover-claims invariant, cancellation, registration |
| `tests/test_reap_agentic_purchase_poll_postgres.py` | Postgres (dialect gate) | the poller across **two real backend connections**, its SQL constants under PREPARE, the error backoff against the server clock, `include_processing=False` on the real statement, the PII deadline with the rail off, claim release on cancellation |
| `tests/test_agent_commerce_reap_routes.py` | SQLite | the three routes over the real app: the router is MOUNTED, the 404 on all three while dark **and for every shape of malformed input**, the ownership conjuncts, eligibility and the market, the price coming from our catalog and from THIS merchant's own offer, the market-currency rule, the buyer ref, idempotency including the request-hash conflict, unprintable identifiers, the hosted-URL vetting, the per-statement self-heal, and that no response or log line carries the buyer |
| `tests/test_agent_commerce_reap_routes_postgres.py` | Postgres (dialect gate) | migration 226 vs the self-heal through the **catalog** (columns, `indexdef`, `pg_get_constraintdef`), the `numeric`→`Decimal` price path the `CAST` exists for, the `market_country` regex CHECK, **a NUL byte in an identifier being a refusal and not a 500** (asyncpg raises where SQLite stores it happily, so only this arm can see it), and every security-relevant refusal re-run on the production dialect |

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
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_reap_wp4_test \
    .venv/bin/python -m pytest tests/test_agent_commerce_reap_routes_postgres.py
```
