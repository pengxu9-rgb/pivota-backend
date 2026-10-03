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
| `resolving` | we have our catalog row, not Reap's variant | `resolve_our_row`; then `get_active_enrollment`; else `get_pending_enrollment` and, if that row has a partner id, `get_enrollment` to **reconcile** it (see *The enrollment lifecycle*); else `upsert_pending_enrollment` + `create_enrollment` | `needs_enrollment`, `quoting`, `refused`, `failed` |
| `needs_enrollment` | buyer has a hosted card page open | `get_active_enrollment`; `get_enrollment_internal` (re-read — a READ, not the old upsert-as-read); `get_enrollment`; `mark_enrollment_active` / `mark_enrollment_dead`. Keeps polling **past** the link's expiry for the enrollment grace | `quoting` (hosted link cleared), `expired` (sweep only), `failed` |
| `quoting` | ready to price and hand the buyer a link | `get_active_enrollment`; `resolve_our_row` **again**; `request_quote`; **`verify_quote`**; `create_checkout` — **all in one step** | `awaiting_approval`, `refused`, `failed` |
| `awaiting_approval` | buyer has the approval page | `get_checkout` | `processing`, `completed`, `failed`, `expired` |
| `processing` | buyer approved; Reap is placing the order | `get_checkout` | `completed`, `failed` |
| `completed` / `failed` / `refused` / `expired` | terminal. `advance` makes no call. | — | — |

**Terminal writes NULL `buyer_email` and `shipping_address`**, stamp `terminal_at` and clear the
claim — in the same UPDATE, so a crash cannot skip the PII half.

**Any transition INTO `quoting` clears `hosted_url` and `hosted_url_expires_at`** (a rule in
`_TRANSITION_SQL`, keyed on the target state; `transition` refuses a caller that passes either
with `to_state='quoting'`). The only link a row can carry into `quoting` is the spent enrollment
page, and its expiry is what the sweep acts on; the approval page is written by `awaiting_approval`.

### The enrollment lifecycle — one pending row per buyer, reconciled before minting

Found on staging 2026-09-30, the first real sandbox card enrollment (all UTC, one demo buyer):

| time | what happened |
|---|---|
| 11:21:34 | purchase A → `needs_enrollment`; our row `re_5fe1…`, Reap enrollment `9041ef1a-…`, link expires **11:36:34** |
| ~11:23 | buyer completes Reap's page ("Reap has received your response") |
| until 11:36:43 | `GET /agentic/enrollments/{id}` still says `REQUIRES_ACTION`, `updatedAt` unchanged |
| **11:36:43** | `ACTIVE`, `paymentMethod.last4` 7847 — **9 s after the link expired** (04:05 showed the same shape) |
| 11:37:02 | the sweep expires A (`hosted_url_expired`); our row stays `pending` |
| 11:37:38 | purchase B: no active row; `upsert_pending_enrollment` returns the SAME pending row (same id = same attempt id), `create_enrollment` replays and returns the OLD dead link; B is swept 54 s later |
| 11:41 | purchase C, the same. **A buyer whose card IS active at Reap could never buy.** |

What the rail does now:

1. **Reap's ACTIVE arrives at or after the hosted session's expiry**, so the link's expiry is not
   the enrollment's end. The expire sweep gives `needs_enrollment` an **enrollment grace**
   (`REAP_AGENTIC_ENROLLMENT_GRACE_SECONDS`, default **180**) past `hosted_url_expires_at`, and
   the poller keeps reading the enrollment during it; ACTIVE in the grace → `quoting`.
   Checkout-backed `awaiting_approval` remains recoverable after either deadline; only an
   authoritative checkout read decides its outcome. Contact retention has its own deadline.
2. **`resolving` reconciles before it mints.** No active row, but `pending` rows that have a
   `reap_enrollment_id` (a link was handed out): **every** such row, **oldest first**, gets one
   guarded `get_enrollment` (`_still_ours` in front of it, like every partner call):

   | Reap says | and | then |
   |---|---|---|
   | `ACTIVE` | — | `mark_enrollment_active` (network, last4) → `quoting`. **Nothing minted.** An ACTIVE row wins over any other pending row |
   | `REQUIRES_ACTION` | the link — Reap's **fresh** `nextAction` from this read when it sends one (allowlist-vetted), else the stored one — has ≥ 60 s left, and is on the allowlist | reuse **that row and that link** → `needs_enrollment` (the oldest such row). Two purchases may wait on one row; both advance on ACTIVE |
   | `REQUIRES_ACTION` | the link is dead or dying, but we are **inside the grace** | **hold**: release in `resolving`, `enrollment_settling`, re-check in 30 s (see *The hold* below). Retiring it now could orphan a card being enrolled at this moment |
   | `REQUIRES_ACTION` past the grace, or `EXPIRED` / `FAILED` / `REVOKED` | — | `mark_enrollment_dead` the row, mint a **NEW** row → a NEW attempt id → a NEW enrollment and link at Reap. The dead link is never replayed |
   | unrecognised | — | release, `unknown_enrollment_status`; nothing reused, retired or minted |
   | transport error | — | release with the doubled backoff |
   | any other failed read | **inside** the grace | release with the doubled backoff and the partner's code; the row may yet settle |
   | 404 / 410 / `AGENTIC_RESOURCE_NOT_FOUND` | **past** the grace | `mark_enrollment_dead` with the partner's code → mint fresh. (Before #2483's review this failed every later purchase of the buyer, for ever) |
   | any other failed read | **past** the grace | `failed` with the partner's code; the row stays **pending** — operator case below |
   | (stored partner id malformed) | — | `failed`, `partner_id_malformed`, no read — operator case below |

   **A link with no expiry is not a live link.** Reap's `expiresAt` is spec-optional and
   `_parse_ts` answers None on a format change; a stored link with no expiry is dated from the
   row's `created_at` + Reap's hosted-session lifetime (`ledger.HOSTED_SESSION_SECONDS` = 900 s,
   measured: every enrollment and approval page seen on staging was created + 15 min). Before the
   review such a link was reused for ever. A reused link's purchase carries that estimate as its
   `hosted_url_expires_at`, so the sweep bounds it by the link's life.

3. **`upsert_pending_enrollment` never hands back a dead link as "the" attempt.** Without an
   `enrollment_id`, if ANY of the buyer's pending rows has a dead link (SERVER clock; the same
   no-expiry rule) it raises `PendingEnrollmentExpired` instead of returning one — it cannot
   retire the row itself (Reap may say ACTIVE; the ledger never calls Reap) and must not mint a
   second pending row. The caller reconciles and calls again. A pending row with **no link**
   (a create whose response was lost) is still returned and replayed, as before. The purchase
   service maps a raise to a release with `enrollment_pending_expired`. With several pending
   rows the **oldest** is the attempt (deterministic; "newest by created_at" was a coin toss
   within one second).
4. **A create that comes back with a dead link** (a replay of an attempt whose first response we
   never saw) is recorded, retired and **not** handed to the buyer (`enrollment_link_expired`);
   the next step mints a new attempt. Nobody saw that link, so retiring it cannot orphan a card.
5. **A create that comes back with an enrollment id another row of ours already holds**
   (`enrollment_id_conflict`; review P1-1). A retired row keeps its `reap_enrollment_id`, and if
   Reap answers a NEW attempt with that same enrollment (de-duplicating by owner, or an open
   session for the owner — not verified against Reap), `uq_reap_agentic_enrollments_reap_id`
   refuses the write. The ledger names it (`EnrollmentIdConflict`, with the holder) instead of a
   raw IntegrityError out of `advance` on every poll. The service retires OUR new attempt, then:

   | the holder is | then |
   |---|---|
   | another buyer's row | `failed`, `enrollment_id_conflict`; nothing of theirs is touched (WARNING logged — worth a human) |
   | this buyer's ACTIVE row | `quoting` on it |
   | this buyer's PENDING row | reconciled like any pending row (ACTIVE → activate, live link → reuse, dying → hold); dead → `failed` |
   | this buyer's DEAD row | `failed`, `enrollment_id_conflict`. **Fail closed**: `mark_enrollment_active` never resurrects a dead row (it may be a revoked card), and minting again gets the same answer |

   **Operator, for a buyer whose purchases keep failing `enrollment_id_conflict`:** find the
   holder (`SELECT id, buyer_ref, status, reap_status FROM reap_agentic_enrollments WHERE
   reap_enrollment_id = '<id>'`), GET it at Reap. If Reap says **ACTIVE** and the card is the
   buyer's, set that row `active` by hand (demote any other active row of the buyer first — the
   one-active index refuses two); if Reap still says REQUIRES_ACTION/EXPIRED and it is not wanted,
   `rc.revoke_enrollment('<id>')` so Reap stops handing it back. Never delete the row.

### One pending row per buyer — migration 252, and what happens before it is applied

"One pending row per buyer" **was** kept by code alone, and code alone cannot keep it across two
workers: two purchases of one buyer in `resolving` in one poll tick, with the old row dead past
the grace, each retired it, each found no pending row, and each INSERTed one — two enrollments,
two links (review P2-1). **Migration 252** adds `uq_reap_agentic_enrollments_one_pending`
(`reap_agentic_enrollments (buyer_ref) WHERE status = 'pending'`; the self-heal builds it too, in
both dialects, each in its own try). With it the second INSERT is refused and the ledger hands
the loser **the winner** — both purchases wait on one attempt, and Reap's idempotency gives them
one enrollment and one link. An INSERT that carried a partner's answer and lost the race raises
`PendingEnrollmentTaken` instead (the service releases with `enrollment_pending_superseded`).
Because the loser is handed the winner's attempt, it can call `create_enrollment` with the SAME
Idempotency-Key while the winner's create is still in flight; Reap answers 409
`IDEMPOTENCY_REQUEST_IN_PROGRESS` (or `IDEMPOTENT_PARAMETER_MISMATCH` — the two bodies differ in
their return URL). That is the designed outcome of the race, so it RELEASES with backoff
(`idempotency_request_in_progress` / `idempotent_parameter_mismatch`), exactly as the quote path
does; the next poll reuses the winner's stored session. It never fails the purchase.

**Before 252 is applied** (production applies migrations by hand; the self-heal creates the index
at startup but SKIPS it if duplicates already exist — and then logs a WARNING from `db.schema_guard`
naming `uq_reap_agentic_enrollments_one_pending` and this census, once per startup) the race can still mint two rows. Nothing is
stranded: the service reconciles **every** pending row oldest-first, an ACTIVE one wins, and
dead ones are retired — the duplicate is resolved on the buyer's next purchase. The index is what
stops it happening at all. **Census before applying 252** (the CREATE fails if this returns rows):

```sql
SELECT buyer_ref, COUNT(*) FROM reap_agentic_enrollments
 WHERE status = 'pending' GROUP BY buyer_ref HAVING COUNT(*) > 1;
```

Resolve each by reconciling with Reap (GET; ACTIVE → `mark_enrollment_active`, otherwise
`mark_enrollment_dead`), never by deleting a row. `get_pending_enrollments(buyer_ref)` is the read.

### The hold — `enrollment_settling`, its cost and its ceiling

A held purchase stays in `resolving` (the owner view shows `state=resolving`, no hosted URL, and
`last_error_code=enrollment_settling`). It re-checks every 30 s for at most
`MIN_LINK_LIFETIME_SECONDS + REAP_AGENTIC_ENROLLMENT_GRACE_SECONDS` (60 + 180 = 240 s by default;
the hold ends when the link's expiry + grace has passed — the row is then retired and a new
attempt minted — or earlier when Reap says ACTIVE).

* **A re-check does not search the catalog again.** `_step_resolving` sees the marker and asks
  only the enrollment question (one `get_enrollment` per pending row); the full step — the
  resolve, its refusals, the decision — runs again only once the answer is no longer "hold".
* **A re-check does not spend the attempt ceiling.** The ledger's claim leaves `attempts` alone
  for a `resolving` row whose `last_error_code` is `enrollment_settling` (the hold is bounded on
  the clock above instead). Without that, a grace of 3600 s (the dial's maximum) meant ~122
  claims against `REAP_AGENTIC_MAX_ATTEMPTS=50`: the purchase failed `attempts_exhausted` before
  the grace ended. Any other release in `resolving` still counts.
* The release is logged at WARNING the **first** time and at INFO on each repeat.

**Operator check** — a buyer stuck behind a pending row:

```sql
SELECT id, reap_enrollment_id, status, reap_status, hosted_url_expires_at, created_at, updated_at
  FROM reap_agentic_enrollments
 WHERE buyer_ref = '<reap_buyer_ref>' ORDER BY created_at ASC;
```

After this change a `pending` row with a partner id reconciles itself on the buyer's next purchase.
Rows stranded **before** it (pending here, ACTIVE at Reap) are healed the same way — no manual
write needed; the next purchase reads Reap and activates. What still needs a human: a row whose
partner id is malformed (`partner_id_malformed`), a row Reap answers with a non-404 error past the
grace (every purchase of the buyer fails with that code until someone GETs it and marks it
`active` or `dead`), and `enrollment_id_conflict` (above).

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
| `REAP_AGENTIC_SIMULATE_CHECKOUT` | **unset = off** | **Sandbox only.** Exactly `COMPLETED` (case-sensitive; anything else is ignored with a WARNING on the `pivota` logger) adds `X-Simulate-Checkout: COMPLETED` to `POST /agentic/checkouts` and to no other request — and only when `REAP_API_BASE_URL`'s host is exactly one of the sandbox hosts `sandbox.api.reap.global`, `sg.sandbox.api.reap.global` or `mx.sandbox.api.reap.global` (`rc.REAP_SANDBOX_HOSTS`; any other host: header withheld, WARNING). See below. |

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

With the rail off, the poller still claims checkout-backed `awaiting_approval` and `processing`
rows and makes **GET-only checkout reads**. Candidate selection and claim UPDATE both enforce
this scope. It does not resolve, enroll, quote or create another checkout. Keep the worker,
correct-host credentials and reconciliation schedule running when rolling back new purchases.
`skipped_disabled=1` means new-purchase work is disabled, not that checkout reads stopped.
A missing credential or a non-sandbox host outside production blocks provider reads; cleanup
still runs. The authenticated owner-scoped purchase-by-ID GET remains available from stored
data with the create flag off, even during credential outages; it makes no provider call.

**Payment uncertainty and contact retention are separate.** Clock/attempt sweeps never terminally
expire/fail a checkout-backed buyer-facing row. The bounded PII scrub clears shipping address,
buyer email and offer code after `REAP_AGENTIC_HOSTED_MAX_AGE_SECONDS` in that state, retaining
checkout/quote/order IDs, amounts/currency, consent and attribution evidence. Neither this scrub
nor a provider outage clears a live claim or marks the row terminal. Provider `FAILED`/`EXPIRED`
responses still terminate unapproved checkouts with ordinary terminal cleanup. Unknown/error
responses remain due for retries and visible to stuck-purchase monitoring.

**Historical recovery is a separate release gate.** This change prevents future clock-sweep
losses; it intentionally does not automatically reopen terminal rows created by old versions.
Before live rollout, use a read-only census of expired checkout-backed rows, reconcile each
candidate with authenticated Reap GET and record purchase/checkout IDs, provider status and
observation time in an operator audit. `last_error_code` alone cannot identify the old sweep
because its COALESCE preserved earlier errors. Do not revive authoritative FAILED/EXPIRED
outcomes. For a provider PROCESSING/COMPLETED contradicting our terminal row, a separately
reviewed, dry-run-first repair must preserve identity/amount/consent evidence, fence updates,
clear only the erroneous terminal marker, and close conversion under the existing unique
merchant/order and click-claim constraints. No bulk reopening or provider writes are authorized
by this code. Never claim “unpaid” solely because a local hosted deadline passed.

No schema migration or new state is required. Deployment must replace all old poller replicas
before relying on this invariant: an old replica still has the unsafe terminal sweep. Rolling
back to the old code reintroduces the loss risk and is unsafe while exposed checkouts remain.

### The poller's dials

All integers, all with code defaults, **all read per run** (except the interval, which an
APScheduler trigger fixes at registration). An invalid or out-of-range value falls back to the
default **with a warning naming the variable** — never a crash, never a silent zero. The minimums
sit at or above the ledger's own floors on purpose: `lease_seconds < 30` and
`max_age_seconds < 60` are `ValueError`s out of the ledger, so an unvalidated dial would not be a
bad setting, it would be an exception out of a scheduled job on every tick.

| variable | default | bounds | effect |
|---|---|---|---|
| `REAP_AGENTIC_ENABLED` | **unset = off** | truthy allowlist | the gate, inside the job, over **step 4 only**. Off ⇒ precheckout frozen, exposed checkout reads continue; independent reconcile flag stops provider I/O |
| `REAP_AGENTIC_POLL_INTERVAL_SECONDS` | 30 | 5–3600 | the `interval` trigger **and** `misfire_grace_time`. Registration-time only — changing it needs a restart |
| `REAP_AGENTIC_CLAIM_BATCH` | 10 | 1–100 | rows claimed per run. Capped at 100 because each row is a serial partner chain |
| `REAP_AGENTIC_LEASE_SECONDS` | 300 | **180**–3600 | what `requeue_stale_claims` measures against. The floor is 180, not the ledger's 30: a lease shorter than one step gets a LIVE worker's row requeued underneath it, and both workers then call the partner |
| `REAP_AGENTIC_HOSTED_MAX_AGE_SECONDS` | 3600 | 60–2592000 | abandoned enrollment/local-hosted expiry bound; not the contact-retention cap |
| `REAP_AGENTIC_CONTACT_MAX_AGE_SECONDS` | 900 | 60–3600 | independent creation-age contact cap; live leases defer cleanup |
| `REAP_AGENTIC_RECONCILE_ENABLED` | 1 | truthy allowlist | off stops all new provider calls while maintenance continues |
| `REAP_AGENTIC_ENROLLMENT_GRACE_SECONDS` | 180 | 0–3600 | how long past `hosted_url_expires_at` the sweep leaves a **`needs_enrollment`** row alone (Reap flips ACTIVE at/after the link dies); also how long `resolving` holds instead of retiring a pending enrollment. `awaiting_approval` never gets it. 0 = Reap's exact expiry. Read by `services.reap_agentic_purchase.enrollment_grace_seconds()` — ONE reader, which the job calls — not a job dial |
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
lock-window property of prod's Postgres under live traffic, not an operator setting) and
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
| 2 | `expire_overdue_purchases(max_age_seconds=REAP_AGENTIC_HOSTED_MAX_AGE_SECONDS, enrollment_grace_seconds=REAP_AGENTIC_ENROLLMENT_GRACE_SECONDS)` | 200 per statement, looped until a partial batch, hard cap 20 iterations |
| 3 | `fail_exhausted_purchases(REAP_AGENTIC_MAX_ATTEMPTS, include_processing=False)` | same |
| 4 | `claim_due_purchases(worker, limit=REAP_AGENTIC_CLAIM_BATCH)` → per row `advance` → `release_claim` | **sequential**, one partner chain at a time, stopped by `REAP_AGENTIC_POLL_BUDGET_SECONDS` |
| 5 | Independent read-only `contact_retention_blocked`, `checkout_needs_human`, and `stuck_over_age` diagnostics, then report | Every completed maintenance tick, including stop/missing credentials. Each diagnostic has its own 10 s timeout; they run concurrently so an unavailable one cannot hide the others. Database cancellation/unwind can exceed that timeout; the 600 s outer job deadline remains the backstop. A timed-out field is `-1`, never zero. Paused precheckout and classified operator-held work are excluded from ordinary stuck. |

**The sweeps run before the claim** because a row held by a dead pod is not claimable until the
requeue frees it — a claim-first run would skip exactly the rows that most need attention, every
tick, forever, on a pod that keeps dying.

**Step 3 never touches `processing`.** A purchase in `processing` has been approved by the buyer
and its payment is in flight with Reap; auto-failing it on a counter writes `failed` over a
charge whose outcome we do not know. **A payment stuck in `processing` is a human decision** —
reconcile the checkout with Reap using the authoritative read step or an audited repair.
Checkout-backed states are excluded even with `include_processing=True`; counters cannot
substitute for provider evidence.

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
| `stuck_over_age` | **read-only, every completed maintenance tick.** Purchases more than 30 minutes past the last moment the rail's own rules let them stay in their state — see [Alerts](#alerts) for the definition. `-1` = **not counted**, never "zero": the count timed out (10 s) or raised, and `errors` is raised with it | **any value ≥ 1 pages** (`prod: Reap purchase stuck over 30 minutes`) |
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

## Alerts

Three policies, provisioned by `infra/gcp/setup_monitoring.sh` (idempotent; **apply it before
arming** — it is not run by any deploy; see [Applying them](#applying-them)). Cloud Monitoring cannot read the ledger, so all
three are log-based metrics over lines the poller writes on the **`worker`** service. Those lines
carry **no severity** on Cloud Run (the report is plain text on stdout; a module logger's
WARNING/ERROR leaves through Python's last-resort handler as the bare message on stderr), so the
filters match text, never `severity>=ERROR`.

| policy | metric | fires when |
|---|---|---|
| `prod: Reap purchase stuck over 30 minutes` | `reap_agentic_poll_stuck` | a report line has `stuck_over_age` ≥ 1 |
| `prod: Reap purchase poller failing` | `reap_agentic_poll_failing` | a report line has `errors` ≥ 1, **or** the job wrote any other `reap_agentic_poll: ` line except `released on cancellation` (they are all WARNING/ERROR), **or** the scheduler cancelled its run at the 600 s deadline |
| `prod: Reap purchase poller went silent` | `reap_agentic_poll_report` | there was a report line in the last 24 h and there has been none for ~20 minutes (15 min of silence + a 5 min duration). Re-sent **hourly** while open |

**What these cannot see — read this before relying on them.**

* **A poller that has been dead for more than 24 hours raises nothing.** `stuck` and `failing`
  read lines only a running poller prints, and `went silent` needs a report in the previous
  24 hours (the longest look-back Cloud Monitoring allows for a log-based metric). It emails
  hourly for those 24 hours; after that the incident closes by itself, fixed or not, and
  `processing` rows sit unread with no alert. An open `went silent` is not something to leave.
* **`stuck_over_age` is not taken while the rail is disarmed** — a disarmed tick stops before
  step 5 and prints no report. On an armed run it is always taken, over-budget ticks included,
  under a 10-second timeout of its own; if it times out or errors the report says
  `stuck_over_age=-1` **and** `errors` goes up, so that tick pages as `poller failing` instead.
  `-1` beside `errors=0` cannot appear on a report line.

**None of them can fire on a rail that has never been armed.** A disarmed tick returns before the
report line and writes nothing, so the first metric has nothing to count and the third has no
series to go missing: its query is "reports earlier `unless` reports now", which is empty when
there were never any. (The second can still fire while dark if a sweep-side line appears — a bad
dial, `processing` rows past the attempt ceiling — and those are real.)

### `prod: Reap purchase stuck over 30 minutes`

**What "stuck" means.** A purchase is counted when it is more than 30 minutes past the last
moment this rail's own rules allow it to still be in its state:

| state | counted when | why |
|---|---|---|
| `quoting`, `processing` | 30 min since `state_entered_at` | our work and Reap's; nothing here waits on a person. **`processing` is the buyer who approved and got nothing** — and the one state no sweep bounds |
| `resolving` | 30 min since `state_entered_at` (+ the enrollment grace while `last_error_code = enrollment_settling`) | the settling hold is a bounded wait on Reap, see [The hold](#the-hold--enrollment_settling-its-cost-and-its-ceiling) |
| `awaiting_approval` | 30 min past `hosted_url_expires_at`, or past `state_entered_at + REAP_AGENTIC_HOSTED_MAX_AGE_SECONDS` | **a buyer on a live hosted page is waiting legitimately and is never counted.** These are exactly the expire sweep's clocks: a row counted here is one the sweep should have expired half an hour ago and did not |
| `needs_enrollment` | the same, with `REAP_AGENTIC_ENROLLMENT_GRACE_SECONDS` added to the hosted expiry | as above |

The count is taken at the end of an **armed** run (step 5), so it is only as fresh as the last
report line. It stays open while any purchase is stuck and closes about an hour after the last
report that counted one.

**What to do.**

```sql
SELECT id, state, state_entered_at, last_error_code, attempts, claimed_by, claimed_at,
       next_poll_at, hosted_url_expires_at, reap_checkout_id
  FROM reap_agentic_purchases
 WHERE state IN ('resolving','needs_enrollment','quoting','awaiting_approval','processing')
 ORDER BY state_entered_at;
```

* `processing` — the payment is in flight. Follow
  [When `processing_over_attempts` will not go down](#when-processing_over_attempts-will-not-go-down):
  ask Reap what the checkout did **before** touching the row. Never fail it on a guess.
* `resolving` / `quoting` — read `last_error_code`. `transport_error:*` is the partner being
  unreachable (the row backs off up to 600 s and is failed by the attempt ceiling eventually);
  `poller_advance_raised:*` is a step that raises — see [When `errors` > 0](#when-errors--0).
  A `claimed_by` that never clears means a worker is dying mid-step.
* `needs_enrollment` / `awaiting_approval` — the expire sweep is not taking rows it should. Check
  `poller failing` and the worker's logs for the run raising before step 4; the buyer's PII is
  being held past its deadline.

### `prod: Reap purchase poller failing`

Read the lines: worker service logs, `textPayload` containing `reap_agentic_poll`. They carry
purchase ids and exception **types** only.

* a report with `errors` ≥ 1, `advance RAISED`, `STILL CLAIMED after the loop`,
  `returned an unhandled outcome` → [When `errors` > 0](#when-errors--0).
* `purchase(s) are in 'processing' at or past max_attempts` → the section of that name below.
  This one repeats every tick until a human resolves the row, so the incident stays open.
* `could not count stuck purchases` → the stuck alert is blind for that tick
  (`stuck_over_age=-1`). `error_type=TimeoutError` means the count did not answer in 10 s — a
  saturated pool or a lock on the table; anything else is a database error. Look at the pool
  and Cloud SQL.
* `… is not an integer` / `is outside …` → a dial is mis-set and the default is in use. Fix the env
  var; it is logged once per process.
* `budget was spent by the sweeps` / `stopped after N rows` → the run is spending its budget
  before the claim; look at `duration_ms` and the sweep counts.
* `scheduler job 'reap_agentic_purchase_poll' exceeded its 600s run deadline` → a wedged run was
  cancelled; `/__scheduler_health` has the run state. Its claims were released on the way out.
* `REAP_AGENTIC_ENABLED is on outside production but REAP_API_BASE_URL is not exactly a Reap
  sandbox host` → staging is armed against a non-sandbox host; step 4 is refusing to run.

Not counted, on purpose — the lines a **deploy or restart that lands mid-step** writes:

* `purchase=<id> released on cancellation` — the job's own line, one per claim the run was
  holding when it was cancelled. The claims are released exactly as a leftover is; the line is a
  WARNING with its own wording because being interrupted is not a bug. (A claim found after a
  run that **finished** its loop still logs `STILL CLAIMED after the loop` at ERROR and still
  counts under `errors` — both of which page.)
* the runner's `ABANDONED as a zombie (wrapper cancelled …)`;
* APScheduler's `Job "run_reap_agentic_purchase_poll …" raised an exception`.

A run cancelled at its **deadline** writes the same `released on cancellation` lines and pages
anyway, through the runner's `exceeded its 600s run deadline` line. A run that raises on **every**
tick prints no report and is the next alert's.

### `prod: Reap purchase poller went silent`

The poller was reporting and stopped: no purchase is being advanced, so a buyer who approves now
waits. In order of likelihood:

1. **The rail was disarmed on purpose** (`REAP_AGENTIC_ENABLED` off). From the logs this is
   indistinguishable from a dead poller — a disarmed tick writes nothing — so the alert opens
   ~20 minutes after the last report and re-sends hourly. **It cannot be closed by hand**: Cloud
   Monitoring refuses to close an incident whose condition is still met (`Unable to close alert
   with active conditions`), and this one stays met until 24 hours after the last report.
   **Snooze the policy** instead (Monitoring → Alerting → Snooze, for this policy only, up to the
   24 hours) — and end the snooze when the rail is re-armed, or a real outage in that window is
   silent. Disarm in the order given under [Stopping it](#stopping-it).
2. **The client lost its configuration** (`REAP_API_BASE_URL` / `REAP_API_KEY`):
   `is_configured()` is false, step 4 is skipped exactly as if the dial were off, and nothing is
   logged. Check the worker revision's env and secrets.
3. **Every run is raising before it reports** — a sweep failing on the database, typically. Worker
   logs: `Job "run_reap_agentic_purchase_poll …" raised an exception`, with the traceback.
4. **The worker is down or its scheduler is wedged.** `GET /__scheduler_health` on the worker:
   `runs.reap_agentic_purchase_poll` shows the last start, outcome and any zombie; `run-now`
   forces a tick.

The condition holds for 24 hours after the last report and then stops being true on its own. An
incident that closed that way was **not fixed**, and nothing will alert again until the poller has
reported at least once more.

### Applying them

`infra/gcp/setup_monitoring.sh` is run by hand, per project; nothing deploys it.

1. **Use the address the live policies already notify.** The script finds its notification
   channel by `ALERT_EMAIL`; a different address makes it create a new channel and move **every**
   policy in the project onto it (prod has three email channels today and all nine policies use
   one of them). Read the current one first — read-only:

   ```bash
   TOKEN="$(gcloud auth print-access-token)"
   curl -s -H "Authorization: Bearer $TOKEN" \
     https://monitoring.googleapis.com/v3/projects/pivota-prod/alertPolicies \
     | python3 -c 'import json,sys; print({c for p in json.load(sys.stdin)["alertPolicies"] for c in p["notificationChannels"]})'
   curl -s -H "Authorization: Bearer $TOKEN" \
     "https://monitoring.googleapis.com/v3/<the channel name printed above>" \
     | python3 -c 'import json,sys; c=json.load(sys.stdin); print(c["labels"]["email_address"], c.get("verificationStatus"))'
   ```
2. `ALERT_EMAIL=<that address> infra/gcp/setup_monitoring.sh prod`
3. **Expect 12 policies afterwards** — the nine that exist today plus the three above. The script
   prints `policies: 12` and each name; to confirm later, read-only:

   ```bash
   curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
     https://monitoring.googleapis.com/v3/projects/pivota-prod/alertPolicies \
     | python3 -c 'import json,sys; ps=json.load(sys.stdin)["alertPolicies"]; print(len(ps)); [print(" -", p["displayName"]) for p in ps]'
   ```
4. **If it ends with `NOT CREATED: prod: Reap …`, re-run the same command in 10 minutes.** The
   first run creates the three log metrics and then the policies over them, and Monitoring can
   take up to 10 minutes to see a new log metric. The script waits for that (up to 10 minutes in
   total, 30 s at a time) and, if it still cannot create a Reap policy, leaves it out, finishes
   everything else and exits 1 naming it. The nine existing policies are written before the Reap
   ones are attempted, and a Reap policy that already exists is replaced only after its
   replacement was accepted, so a deferred run leaves nothing worse than it found it.
5. **The script exits 1 on a successful run today**, with `WARNING: channel is 'UNSET', not
   VERIFIED` and `FAILED: alerts are configured but the channel cannot receive them`: the prod
   email channels report no `verificationStatus`. That is existing behaviour and is not caused by
   these policies; judge the run by the `policies: 12` list, and treat the channel warning as the
   separate open question it already was.

---

## Arming it

The poller runs **only on the worker service**. `_add_job` registers nothing unless
`services.audit_scheduler._queue_worker_enabled()` is true. The claim has no environment filter.
Prod and staging run separate Cloud SQL instances (`infra/gcp/README.md`, verified 2026-09-29),
so a staging worker cannot reach **prod's** rows — but that is NOT enough to keep it off
production purchases: **staging's database is a restored copy of production.** Every production
purchase that was `resolving`, `quoting`, `needs_enrollment`, `awaiting_approval` or `processing`
when the copy was taken is in it, with the buyer's email and address, and an armed staging
poller would claim it and call `create_enrollment` (Reap emails that real buyer),
`request_quote` and `create_checkout`. Arming on staging therefore requires the **Staging
pre-flight** below. The gate itself dates from the Railway era, when prod and staging shared one
Postgres; it still keeps the poller off staging/preview services by default.

Code guard, in addition: outside production (`platform_env()` ≠ production) the poller's step 4
runs only when `REAP_API_BASE_URL`'s host is exactly one of `sandbox.api.reap.global`,
`sg.sandbox.api.reap.global` or `mx.sandbox.api.reap.global` (`rc.is_sandbox_base_url`, reading
`rc.REAP_SANDBOX_HOSTS` — the same set as the simulate header). Any other host —
`prod.api.reap.global`, `sg.prod.api.reap.global`, `mx.prod.api.reap.global`, a suffix like
`x.sandbox.api.reap.global`, a URL with userinfo — reports `skipped_disabled=1` and logs one ERROR on the
`pivota` logger per process; the sweeps still run. It is a backstop, not a substitute for the
pre-flight: the sandbox would still be asked to enroll and quote production buyers' rows.

**Pre-flight before arming any worker:** confirm its `DATABASE_URL` host is its own project's
instance (prod `10.25.0.2`, staging `10.122.0.3`) — compare host and database name only, never
print the URL. A staging worker pointed at prod's URL would bring the poaching hazard back.

> **THE WORKER SERVICE IS DEPLOYED SEPARATELY. The normal backend deploy does NOT ship it.**
> Merging this and deploying the backend changes nothing: the job only exists in a process where
> `_queue_worker_enabled()` is true, and that process has to be deployed on its own. See
> `docs/` on the scheduler lane; this is the same gap that left `catalog_import_drain_tick`
> registered on an undeployed worker.

### Staging pre-flight (MANDATORY before `REAP_AGENTIC_ENABLED=1` on staging, and after EVERY staging restore)

**Re-run it after every restore of the staging database.** A restore brings back whatever was
live in production at that moment — purchases mid-payment, buyers' emails and addresses, live Reap
approval links — and the poller, if armed, claims them on its next tick. A CLEAR from before the
restore says nothing about the database after it. (Only claiming rows created after an arming
timestamp would make this structural; that is not built — it needs a claim-SQL change in both
dialects — so the census after each restore is the control.)

Both steps are ONE program, `scripts/ops/reap_staging_preflight.py`, run as a one-off Cloud Run job
in the **staging** project. The database is on a private VPC address; `run_oneoff_job.sh` is the
way in, and it is run inline (`-c "$(cat …)"`) because the program need not be in any deployed
image yet.

> **`run_oneoff_job.sh` DEFAULTS TO PRODUCTION** — `PROJECT=pivota-prod`, prod's `DATABASE_URL`,
> `PIVOTA_ENV=production`. The staging block below is not optional, and the program does not trust
> it: before it reads or writes a single Reap row it ABORTS (exit 2) unless `PIVOTA_ENV` is
> exactly `staging` (no surrounding whitespace, no other case), the `DATABASE_URL` names ONE host,
> exactly staging's `10.122.0.3`, with no `host` / `hostaddr` / `service` query parameter, and the
> server's `current_database()` is exactly `pivota`. It then connects with `host=10.122.0.3`
> passed explicitly, so nothing in the URL can redirect it. Any doubt — an unreadable identity
> included — is an abort. Those expected values are constants in the program, not flags.

Read the `DATABASE_URL` secretKeyRef off the staging worker rather than trusting the name below:

```
PROJECT=pivota-staging \
ENV_VARS=PIVOTA_ENV=staging,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600 \
SECRETS=DATABASE_URL=<staging worker's DATABASE_URL secret>:latest \
  scripts/ops/run_oneoff_job.sh -c "$(cat scripts/ops/reap_staging_preflight.py)" census
```

**a. Count** — `census`. Read-only (the session is `READ ONLY`). Prints the host, database name and
counts only: non-terminal purchases by state (with how many carry an email and an approval link)
and pending/active enrollments. **The exit code is the verdict:** 0 = CLEAR, 3 = STOP (live rows),
2 = ABORT (not staging, or bad arguments). A non-zero exit from the runner can also be a failed
job — read its output before concluding anything.

**b. Scrub, or STOP.** Either stop here, or — with the rail owner's explicit go, announced before
it runs — run the SAME block with `scrub` (a dry run: the census plus what would change, writes
nothing), then `scrub --apply`. `--apply` runs BOTH updates in ONE transaction, so either every
live row is scrubbed or none is:

* purchases not in a terminal state -> `refused`, `last_error_code = 'staging_preflight_scrub'`,
  with `buyer_email`, `shipping_address`, `offer_code`, **`hosted_url`, `hosted_url_expires_at`**
  (a restored row's link is a live Reap approval URL for a real production checkout), the claim
  and `next_poll_at` nulled, and `terminal_at` stamped — the terminal transition's own scrub;
* `pending`/`active` enrollments -> `dead`, with their `hosted_url` and expiry nulled.

It prints the two row counts and re-runs the census; exit 0 means CLEAR. Never point it at
production — and if you do, it aborts.

**c. Sandbox only.** `REAP_API_BASE_URL` must be exactly one of `https://sandbox.api.reap.global`,
`https://sg.sandbox.api.reap.global` (verified live for cart-link quotes 2026-09-28; the SG demo
merchant) or `https://mx.sandbox.api.reap.global`, and `REAP_API_KEY` a sandbox key. The poller refuses any
other host outside production (above), but check it before arming rather than discovering it in
the ERROR line.

### Steps

1. Deploy the worker service.
2. `AUDIT_WORKER_ENABLED=true` on it (or let the service-name detection decide; the gate is
   fail-safe toward ENABLED, so an unknown platform stays on). **On staging, set it in the same
   command as `SCHEDULER_JOB_ALLOWLIST=reap_agentic_purchase_poll`** — one revision, never two —
   using the exact command in `docs/runbooks/scheduler_job_allowlist.md` ("Arm the worker").
   Outside production a worker with the flag explicitly true and no allowlist starts nothing.
   To undo, set `AUDIT_WORKER_ENABLED=false` (or `REAP_AGENTIC_ENABLED=0`); never remove the
   allowlist while the flag is true.
3. `REAP_API_BASE_URL` + `REAP_API_KEY` — both, or `is_configured()` is false and every run
   returns `skipped_disabled=1`.
4. `REAP_AGENTIC_ENABLED=1`. **This is the arming step.** No redeploy and no scheduler restart:
   the gate lives inside the job, so the next tick picks it up. **Apply the alerts first**
   ([Applying them](#applying-them)): they key on the report line an armed run prints, so they
   are inert until this step and live from it.
5. Run it once by hand and read the report:
   `POST /admin/scheduler/jobs/reap_agentic_purchase_poll/run-now` (admin auth). The response is
   the runner outcome; the counts are in the job's own log line and on
   `GET /admin/scheduler/runs`.
6. Watch `errors` and `lost_claim` for a few ticks.

### Stopping it

Creation pause and provider stop are separate controls. `REAP_AGENTIC_ENABLED=0` freezes
precheckout work while exposed checkout reads, ledger completion and attribution continue.
Set `REAP_AGENTIC_RECONCILE_ENABLED=0` for a true provider-I/O stop (default1). The gate is
checked before claiming/advancing; a request already in flight may finish. Requeue, safe expiry,
attempt maintenance and contact retention continue. Job pause stops all maintenance and should
be brief. Never infer failed/expired payment from a clock, attempt counter or unreadable checkout.

| lever | provider reads/new work | maintenance |
|---|---|---|
| master/create gate off | exposed checkout reads continue; precheckout frozen | runs |
| `REAP_AGENTIC_RECONCILE_ENABLED=0` | no new provider call of any kind | runs |
| scheduler pause/worker disabled | no new tick | stopped, including privacy sweeps |
| cancel-running | cancels current run and releases its claims | later ticks still run |

Every completed tick emits a heartbeat, including missing-credential and provider-stop ticks.
The heartbeat proves maintenance, not arming. Deploying this change can start report-series
history on a previously dark environment; a later stopped job can trigger “poller went silent”.
Paused precheckout rows are excluded from ordinary stuck counts. Contact-expired precheckout
rows are preserved, separately counted as `contact_retention_blocked`, and make no provider
call on resume. Do not solve an uncertain quoting row by minting another checkout: retain the
original request/key/quote/enrollment and obtain an authoritative provider reconciliation first.

`REAP_AGENTIC_CONTACT_MAX_AGE_SECONDS` is an independent creation-based contact cap: default900,
range60–3600. State transitions never reset it. Exposed checkout rows also scrub at explicit
hosted expiry if earlier. A live lease defers the scrub until released/requeued. Contact cleanup
preserves quote/amount/currency/checkout/order/consent/attribution evidence and never declares a
payment outcome. Restoring buyer contact or restarting a blocked attempt requires separate
owner/operator review; this worker does not silently refill PII or create a replacement.

#### The order to disarm in — an operator rule

Preserve owner GET/recovery and exposed reconciliation when pausing new purchases. Use the
independent reconcile stop only when provider calls themselves must cease. Historic gateway
facts below are pinned to their named source revision and require current-source review.

So, for a planned stop. The gateway facts below are the gateway's (PIVOTA-Agent) behaviour as
read at its commit `873b60727`, not something this repository can check — re-read the gateway
before relying on them at a later commit.

1. **Stop NEW purchases with the create-only switches, and leave everything that reads or steps a
   purchase ON.**
   * Cart-link lane: `REAP_AGENTIC_CART_LINK_ENABLED=0` on the **web** service — the create route
     then answers 404 for a cart-link purchase and nothing else changes — and the gateway's
     `REAP_AGENTIC_CART_LINK_LANE_ENABLED=0`, which gates the gateway's create path only. Do not
     set the backend flag on the **worker** for a graceful stop: there it is a kill switch for
     rows in flight (a cart-link row with no quote yet is refused on its next step).
     The gateway flag is **not purely a create gate**: at `873b60727` the gateway enables offer
     codes on every Reap create only when BOTH its lane flag and this cart-link flag are on
     (`ucpReapAgenticLane.js:259-260`), so while it is off, purchases created on the other lane
     lose their discount codes.
   * **These two stop the cart-link lane and nothing else.** The other lane (`item_source =
     reap_variant`; the seller lane, in the gateway's terms) keeps creating purchases, so step 2's
     "no non-terminal purchase" is never reached on their strength alone. **For a FULL stop of
     the rail the next switch is REQUIRED, not optional:** `REAP_AGENTIC_ENABLED=0` on the **WEB
     service only**. (Skip it only when the intent is to stop the cart-link lane and leave the
     rail running — and then do not go on to step 3.) All three routes then answer 404; the
     gateway maps that to a degraded "incomplete, poll again" answer
     rather than an error; and the worker, which has its own env, keeps stepping the rows in
     flight. What that costs, plainly:
     * a purchase that has **not yet handed the buyer a hosted URL** can no longer be paid — the
       URL reaches the buyer only through the status read, which is now 404. It ends `expired` or
       `failed`, uncharged;
     * a buyer **already on Reap's page** can still approve and be charged, and the worker
       completes the row as usual — but the agent sees "incomplete / state unavailable" for that
       purchase until the web service is re-enabled.
   * **NEVER turn off the gateway's `REAP_AGENTIC_LANE_ENABLED` while any purchase is
     non-terminal.** With it off the gateway returns before its status-read branch, so reads for
     in-flight `reap_…` purchases stop reaching this rail: a buyer on Reap's approval page can
     approve and be charged while the agent never learns the outcome — and an agent told the id
     is unknown may create the purchase again.
2. **Wait until the ledger has no non-terminal purchase:**

   ```sql
   SELECT state, COUNT(*) FROM reap_agentic_purchases
    WHERE state IN ('resolving','needs_enrollment','quoting','awaiting_approval','processing')
    GROUP BY state;
   ```
   Zero rows. The waiting states empty themselves within the hosted-page lifetime
   (`REAP_AGENTIC_HOSTED_MAX_AGE_SECONDS` at most), `resolving` and `quoting` move on or fail at
   the attempt ceiling, and `processing` empties when Reap answers.
3. **Only then:** `REAP_AGENTIC_ENABLED=0` on the **worker** (and on web, if step 1 did not already
   do it), snooze `went silent`, and — **last** — the gateway's `REAP_AGENTIC_LANE_ENABLED=0`.

**In an emergency disarm** — the dial has to go off now — list the non-terminal rows with the
query under [Alerts](#alerts) *before or immediately after*, and reconcile every
`awaiting_approval` and `processing` one with Reap by hand (this rail has no webhooks). Any of
them that later reads `expired` with `last_error_code = hosted_url_expired` may be a completed
payment the ledger never saw.

`processing` rows are not expired by anything; a `processing` row left behind is yours to watch
by hand.

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
3. Apply the authoritative outcome through the fenced checkout read step or a reviewed repair.
   Never use an attempt count to decide whether a checkout was paid.

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
5. **Run cleanup separately from checkout outcome reconciliation.**
   * `expire_overdue_purchases(max_age_seconds=…)` — abandoned pre-checkout expiry.
   * `scrub_reconciling_purchase_pii(max_age_seconds=…)` — the independent contact deadline
     for approval/processing rows with a checkout; retains state and evidence for recovery.
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
`enrollment_row_unreadable`, `enrollment_not_activatable` (Reap said ACTIVE but our row was
retired and no other card is active — fail closed), `enrollment_id_conflict` (a create
answered with an enrollment id another row of ours holds — see *The enrollment lifecycle*,
item 5), `partner_id_malformed`, `checkout_no_hosted_action`,
`checkout_failed`, `approval_window_lapsed` (a partner `FAILED` on `awaiting_approval` after the
quote's expiry — the buyer did not approve in time), `checkout_expired`, `checkout_id_missing`,
`quote_id_missing`, `quote_expired`,
`no_active_enrollment`, `completed_without_order_id`, `final_amount_missing`, and
`reap_status_<n>` / `AGENTIC_*` codes passed through from the partner.

Codes on a RELEASE (the row stays in `resolving`, the code is on `last_error_code`):
`enrollment_settling` (the buyer's pending enrollment link is dead or dying but inside the grace —
waiting for Reap to say ACTIVE or for the grace to pass), `enrollment_link_expired` (a create
returned a dead link; retired, a new attempt is minted next step), `enrollment_pending_expired`
(the ledger refused to replay a dead pending row it could not reconcile),
`enrollment_pending_superseded` (our attempt stopped being pending and another pending row of the
buyer exists; the next step reconciles it), and `enrollment_not_activatable` (on `resolving`,
re-reconciled next step). `enrollment_id_conflict` is a `failed` code — see *The enrollment
lifecycle*, item 5.

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
| `reap_agentic_purchase_keys` | immutable lifetime-attempt idempotency, scoped to `(agent_id, agent_user_ref_hash, idempotency_key)`, carrying a hash of the request the key was used for — the same key on a different body is `idempotency_conflict`, not a 202 about somebody else's purchase |

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
(`revokeEnrollment_agentic`, in `tests/fixtures/reap_openapi_agentic_2026_09_28.json`), wrapped as
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
--    It answers `409 merchant_disabled` (NOT `merchant_not_eligible`), on BOTH lanes: the
--    cart-link (Tier B) lane also refuses a merchant with a disabled row here, whatever its daily
--    Tier B verdict says, and the gateway never retries Tier B on `merchant_disabled`. To turn a
--    merchant off that has NO variant-lane row, INSERT a disabled merchant row
--    (product_key '', enabled FALSE) for it in that market.
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
| `tests/test_reap_rail_alerts.py` | SQLite | the three alert metrics: the REAL poller's and runner's lines, on both streams as the worker writes them, through the filters parsed out of `infra/gcp/setup_monitoring.sh`; a dark rail feeding none of them; the three policies' shape |
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


### Checkout reads requiring human reconciliation

Three consecutive permanent-shaped reads (404, unknown checkout status or unsafe hosted URL)
produce `checkout_unresolvable:<count>:<reason>` and a 15-minute retry, capped at count99.
This is an observation category, not evidence of payment failure. These rows remain recoverable
and are counted in `checkout_needs_human`, separately from ordinary `stuck_over_age`; errors
that are not explicitly classified still count as ordinary stuck work. A transient outage does
not clear the human category; a valid provider waiting/processing/terminal outcome does.
The report also exposes `contact_retention_blocked` for preserved precheckout work whose contact
cap elapsed. Both figures need an operator review queue; no new cloud policy is provisioned here.

The reconciliation stop is checked before every worker-scoped HTTP transport operation, including resolver search/details/variant subrequests. An already in-flight request may finish, but its successor must not start. Context is reset after each step; standalone client callers retain their existing contract. Privacy-expired `needs_enrollment` rows also hold without new provider preparation; stored enrollment evidence is retained for operator review.


### Audited manual resolution of classified checkout uncertainty

`services.reap_checkout_recovery.resolve_checkout_manually` is a service-only primitive; there is no admin HTTP route or executable operator CLI. Its default `dry_run=True` preview writes nothing. Applying it requires an explicitly privileged caller, an opaque operator handle, independently authenticated Reap read or verified support evidence, a same-environment provider origin, an exact checkout ID and recognized terminal status, and evidence observed within the last 24 hours and after this purchase was created. The verification attestation is a caller contract, not automatic cryptographic validation; an operator must verify the authentic evidence before setting it.

Only checkout-backed `awaiting_approval`/`processing` rows explicitly classified `checkout_unresolvable:` are eligible. The original state, checkout ID, error classification, `updated_at`, and absent lease are checked again by a conditional claim. Missing reads, 404s, elapsed clocks and a missing checkout ID are never terminal proof. COMPLETED additionally requires a valid order ID, currency and charged total within the existing one-minor-unit quote tolerance. Provider EXPIRED while processing becomes failed under the existing state transition contract. Terminal historical rows cannot be reopened.

Migration 253 and both startup self-heal dialects create `reap_checkout_manual_resolution_audit`. It stores one decision per purchase: checkout, original state/version, terminal status, operator/evidence handles, verified source, observed time/origin and normalized evidence SHA256. It stores no full provider body or buyer contact. The audit append, conditional terminal decision, terminal lease clearing, click claim and attribution edge call share one short database transaction; unexpected failures or suppression roll back that unit. A deterministic existing merchant-channel click claim is legitimate: completion keeps `attribution_closed_by_other_channel`, preserves that claim, writes no Reap edge, and audits `attribution_outcome=closed_by_other_channel`. Other completions audit `edge_closed` only after rereading the durable attribution edge and checking exact merchant, external and synthetic order, amount/currency, click/agent, converted source/state and the purchase/checkout partner provenance. A synthesized close receipt after `ON CONFLICT DO NOTHING` is insufficient: a missing edge or conflicting existing order slot rolls back the terminal outcome and audit without overwriting the edge. Failed/expired decisions audit `not_applicable`. Ancillary commerce event/interaction emission retains its existing best-effort semantics and requires a separate receipt check; this primitive does not promise those receipts exist. Exact-evidence replay is read-only and cannot create another audit or edge. Do not delete the audit table when rolling back runtime code; removing this function leaves classified rows safely unresolved.

`contact_retention_blocked` is a separate owner/operator queue, covering privacy-held resolving, needs_enrollment and quoting work. Resuming flags does not restore discarded contact or mint another checkout. An operator must inspect whether an external checkout may exist and use a separately reviewed recovery/contact-reauthorization procedure; there is no automatic quoting cleanup or blind retry. `checkout_needs_human` is a separate payment uncertainty queue. The existing three metrics match heartbeat, ordinary stuck and errors only: before arming, these two cohorts need an explicitly owned, reviewed alert/runbook. No cloud policy is created or enabled by this source change.


### Enrollment links without provider expiry

If Reap omits optional enrollment expiresAt, the purchase carries a stable estimate from the
originating enrollment attempt created_at plus the existing HOSTED_SESSION_SECONDS policy
(900 seconds). A reused attempt keeps that deadline; reads and reloads never restamp it.
An explicit valid provider expiresAt wins. A malformed supplied expiry is refused, rather
than silently treated as omitted. The originating created_at is available only on internal
enrollment reads, not in public purchase responses.

For legacy needs_enrollment purchases with NULL deadline, owner GET derives the same estimate
from the original enrollment row, after checking enrollment ID, buyer_ref and exact stored
link. No row is changed. Missing/unreadable/wrong-owner provenance or an expired estimate
withholds the hosted action. Owner data passes public_purchase_view before response building;
no identity, buyer contact or enrollment ID is added to the public contract.

### Lost response recovery and durable attempt keys

Use authenticated `POST /agent/v2/commerce/reap/purchases/recover` with the original create body and opaque key while creates are paused. The read-only endpoint checks the original canonical request fingerprint and returns the owner view without merchant freshness checks, provider calls or consent/PII writes. Preserve the explicit original return URL: if a caller omitted it and the default changes, fingerprint conflict keeps the attempt uncertain. Never resolve that conflict by creating with a new key until the original outcome is authoritatively reconciled.

Mappings and refusal tombstones are immutable regardless of age, including completed purchases. The old 24-hour rollover is removed from both lookup and SQL insertion. Existing mappings require no migration; historical overwritten mappings cannot be reconstructed by this fix and remain an operator audit gate. Roll out to every create-serving instance before relying on this guarantee; do not delete old keys as cleanup. Normal enabled create replays retain existing consent-write semantics; use recover when a read-only replay is required.


### Create pause and complete pilot scope

`REAP_AGENTIC_CREATE_ENABLED` is an optional create-only dial. Unset preserves the master dial's existing behavior. `0`, `false`, an empty string or an unrecognized spelling pauses direct creates before buyer identity/consent/purchase writes and limits the worker to exposed checkout reconciliation. Truthy values require the master dial too. Keep host, credentials, worker scheduling, buyer GET and recover available. The queued resolving/needs_enrollment/quoting states do not make new provider side effects while paused; local abandoned-row/PII retention sweeps still apply.

Optional `REAP_AGENTIC_PILOT_SCOPE` JSON must contain exactly five nonempty arrays: `agent_ids`, `merchant_domains`, `markets`, `product_keys`, `quantities`. Use exact authenticated agent IDs, canonical lower-case merchant domains without www, uppercase two-letter markets, exact product keys and integer quantities. Unknown, missing, duplicate, empty or malformed configuration fails closed for new work. Omit the entire variable to retain existing unrestricted behavior; that is not an exclusive pilot. No live pilot IDs are supplied by this PR.

Fresh API creates enforce all five dimensions before identity/consent writes. Precheckout advance checks stored values and fenced-releases outside-scope rows as `pilot_scope_refused` without provider work; it preserves the attempt rather than failing an uncertain outcome. Scope narrowing never blocks exposed awaiting_approval/processing reconciliation or authenticated GET/recover. Valid-scope normal same-key replays keep the existing purchase even after scope narrowing, while a globally paused create uses the read-only recover route. Deploy this capability everywhere before enabling an explicit scope; web and worker require the same reviewed scope and create-pause settings. Environment changes require the platform's normal service restart/revision process; no live setting was changed here.

Staging acceptance must prove one exact allowlisted request succeeds, each of the five wrong dimensions refuses without buyer/key/purchase writes, partial/malformed scope fails closed, queued out-of-scope rows make no provider calls, and an exposed checkout completes through disarming/narrowing while GET/recover remain readable. No schema migration is required.


### Proposed staging cadence acceptance (configuration only)

For the single scoped staging pilot, propose worker `REAP_AGENTIC_POLL_INTERVAL_SECONDS=5`, the existing supported minimum, with the reviewed pilot scope mirrored on web and worker. Keep the global default unchanged. Keep per-row `next_poll_at`, 15-second processing cadence, 30-second approval cadence and transport backoff intact; never claim future-due rows to meet a UI timing target. Service revision/restart is required for the scheduler interval. No live configuration has been changed.

A five-second trigger reduces the extra scheduling quantization of the default 30-second trigger. It does not bound actual lag when serial batches, slow resolution/quote calls, worker downtime or APScheduler max_instances/coalescing skip a trigger. Measure original provider outcome time, actual row claim, advance/release, next_poll_at, backend/gateway read and UI observation timestamps in staging before promising a timing contract. Observe job duration, due-row age, scheduling skips, database statement rate and provider call rate. Increasing empty-tick frequency costs roughly six times the baseline job/DB overhead; provider reads remain governed by per-row due/backoff. Start with one scoped checkout and accept five-second additional trigger lag only while job duration stays below the trigger and the queue stays bounded. If batches exceed five seconds, tune scoped batch volume or isolate expensive precheckout work in a separately reviewed change; do not poll early.
