# Reap enrollment continuation and dispatch evidence

## Purpose and non-goals

Card setup and order approval are separate actions. A consumed/expired hosted setup URL, a
purchase clock, and provider `updatedAt` do not prove that an enrollment failed or establish
when ACTIVE first became visible. No callback or webhook contract is assumed here.

This change does not repair, revive, or replace an already terminal purchase. In particular,
the observed 2026-10-04 KraveBeauty attempt remains terminal. No operation on that attempt,
provider account, deployed database, or payment is part of these source changes.

## Contact retention is independent of enrollment reads

The existing 900-second privacy sweep still erases buyer email, shipping address and offer
code. It now stamps `contact_purged_at`. A provider-backed `needs_enrollment` purchase is not
expired from the hosted-page deadline, including after the old enrollment grace. It remains
eligible for non-sensitive enrollment GETs with creation disabled or its pilot scope removed.

ACTIVE is persisted locally, but missing contact pauses the same purchase in a nonterminal
state. Pending and unknown statuses never authorize quoting. A pending enrollment with an
expired/unsafe/omitted hosted action is retained instead of being retired just to mint another
one. Explicit provider terminal statuses retain the existing dead-enrollment semantics.

Creation/reconciliation switches remain separate. Turning reconciliation off stops reads.
Contact data is not returned to the caller in owner views or dispatch events.

## Owner/request/attempt-bound re-entry

`POST /agent/v2/commerce/reap/purchases/{purchase_id}/resume` requires the exact original create
body and its original idempotency key. The canonical request fingerprint, authenticated agent,
authenticated buyer, immutable key mapping and purchase ID must all agree. The current buyer
identity link must still name the purchase's buyer reference. There is no create fallback.

Only contact-paused, nonterminal `resolving`, `needs_enrollment` or `quoting` rows with authoritative
`not_dispatched` evidence can be restored. Merchant eligibility/purchasability, variant identity,
market, quantity and current catalog price are validated again against the immutable attempt.
The existing worker also re-resolves/revalidates before quoting. Changed price or item refuses.
The purchase's click, cart URL, enrollment, consent and identity are not replaced.

The single conditional write checks state, owner, buyer linkage, key/hash, dispatch fence,
contact revision and absence of a worker lease. Terminal or dispatched rows cannot be hydrated.
A successful re-entry increments `contact_revision` and starts a new bounded contact-retention
window. An exact duplicate returns the current view without refreshing that window. Concurrent
re-entries have one winner. A poll/terminal/dispatch race refuses or returns an already accepted
same-attempt view; it never opens a new purchase.

The original contact details must be re-entered exactly under the existing canonicalizer.
Changing an address/email is deliberately outside this endpoint's authority.

## Durable dispatch evidence

Migration 256 adds a tracking boundary initialized to version 1 only by a new purchase INSERT.
Historical rows are left NULL. There is no backfill from missing provider IDs.

Before `create_checkout`, one transaction compare-and-swaps the purchase's dispatch key and
appends a `started` event with its quote/enrollment binding. The immutable journal is protected
against UPDATE and DELETE by database triggers. The network call happens after commit.
Timeout, lost response, crash, unsafe response or lost worker lease preserves a nonterminal
`quoting` hold and the fence, so
another poll cannot silently re-quote and create a replacement checkout. HTTP500, malformed or
oversized HTTP200 and missing/malformed checkout IDs are explicitly uncertain. A valid checkout
ID with no safe hosted action remains in `awaiting_approval`, with no link and an operator-review
classification, so authoritative checkout reads can still establish its result. The unresolved work
appears in the existing needs-human count, and clock/attempt sweeps do not declare it failed.

The only automatic release of a dispatch fence is a correlated, explicit rejection already
supported by the client contract: QUOTE_EXPIRED or ENROLLMENT_NOT_ACTIVE with HTTP400/409, or
CHECKOUT_TEMPORARILY_UNAVAILABLE with HTTP503. The append-only `not_created` receipt remains.
Unknown codes/statuses and a missing checkout ID never supply that authority. A quote/enrollment
pair with a prior started event is never dispatched a second time.

The one local release: the client settles a `DispatchProbe` on every exit of `create_checkout`,
and `not_dispatched` is true only when `_post` never reached the line immediately before the
transport call (the final permission/scope re-check, missing configuration, an unbuildable
request or header). That appends a `not_created` event with provider code
`local_not_dispatched:<reason>` and goes through the same fenced clear. The cause keeps its own
semantics (pause, reconciliation release, or hold with its own code). If Reap replays the same
quote id inside its 240 s quote idempotency bucket, the key is still not dispatched again; the
row holds as `checkout_quote_replayed` past the bucket and re-quotes, without becoming human work.
Any failure after the send line, including an exception from the transport, stays parked.

A 200 whose hosted action is refused (`hosted_url_not_allowed`) certainly created a checkout.
Its `_path_id`-validated id is appended as an `observed` event and the row parks in `quoting` as
`checkout_created_hosted_url_refused` (counted as needs-human, never re-sent). The URL is not
stored or logged anywhere. To work it, read the checkout id from
`reap_checkout_dispatch_events` (`event_type='observed'`) and reconcile it with Reap.

A parked create (`quoting` with a dispatch key, counted in `checkout_needs_human`) is resolved
only by an operator: `services.reap_checkout_recovery.list_parked_dispatches` lists the cohort,
and `resolve_parked_dispatch` records `checkout_found` or `confirmed_not_created` from verified
Reap evidence, no earlier than the settle window after the latest `started`. A
`checkout_created_hosted_url_refused` park is one of them: `checkout_found` takes its `observed`
id and `confirmed_not_created` is refused. The append-only journal gains operator `resolved` and
`superseded` events (migration 257). The steps, and what never to do, are in
docs/runbooks/reap_agentic_purchase.md, Parked checkout create.

Owner view `checkout_dispatch_state` values:
- `not_dispatched`: version1 tracking with no unresolved dispatch and no stored checkout/order
- `dispatch_started`: durable intent exists; the outcome may be unknown
- `dispatched`: a checkout/order identity is stored
- `unknown`: legacy/missing tracking, never negative proof

Owner view `contact_reentry_required` is true only for contact-paused nonterminal precheckout.
The gateway carries these as explicit messages; old/missing/invalid fields remain conservative.

## UI and rollout

Enrollment and order approval use separate append-only, checkout-bound browser markers. Old
`handedOff` markers remain ambiguous payment risk. Opening a card-setup page does not approve
an order. A lost resume response triggers status-only recovery on the original checkout; fresh
explicit no-dispatch/contact-required evidence may allow another same-attempt resume request.

Deployments are not part of this work. Before any authorized rollout, independently review the
exact three candidate heads, run the real PostgreSQL/asyncpg gates, and verify migrations/self-heal.
Old checkout-creating worker binaries must be quiesced/drained before enabling new creates:
a mixed-version old worker can ignore the new dispatch journal on a newly created row. Verify
that no old worker remains and that all new workers enforce the fence. Backend and gateway
additive contracts should precede the UI. Do not infer runtime success from
source tests, PR401 wording, or an image/revision belonging only to the UI.

## Receipts still needed

1. Exact deployed backend/gateway revisions and configured retention/read controls
2. Authenticated provider request correlation and status-read timeline for the original enrollment
3. Provider explanation of ACTIVE visibility timing, `updatedAt`, consumed/expired hosted URLs,
   and the documented polling/callback contract
4. Independent authoritative checkout-dispatch/outcome reconciliation for the expired attempt;
   empty quote/checkout/order columns do not suffice
5. A reproducible sandbox setup → ACTIVE → same-attempt continuation → fresh quote → separate
   hosted order-approval observation, without automatically approving or charging

Local tests use synthetic fixtures and blocked/fake network. No production recovery is claimed.
