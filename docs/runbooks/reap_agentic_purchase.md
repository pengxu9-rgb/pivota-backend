# Reap agentic purchase — the state machine (WP2b)

`services/reap_agentic_purchase.py`. Buyer-funded purchases over Reap's agentic rail: the buyer
enrols **their own card** once on Reap's hosted page, and approves each purchase on another
hosted page. **Pivota never holds or moves money and never sees card data.**

This package is **dark**. It has no routes and no scheduler, nothing calls it in production, and
the dial is off. Nothing here has ever talked to a `reap.global` or `prava.space` host.

---

## States

`db/reap_agentic_ledger.ALLOWED_TRANSITIONS` is the map; migration 224's `CHECK` is the
vocabulary. **There are no self-edges** — "wait and try again" is `release_claim`, not a
transition.

| state | what it means | this package's step calls | → |
|---|---|---|---|
| `resolving` | we have our catalog row, not Reap's variant | `resolve_our_row`; then either `get_active_enrollment` or `upsert_pending_enrollment` + `create_enrollment` | `needs_enrollment`, `quoting`, `refused`, `failed` |
| `needs_enrollment` | buyer has a hosted card page open | `get_active_enrollment`; `upsert_pending_enrollment` (re-read); `get_enrollment`; `mark_enrollment_active` / `mark_enrollment_dead` | `quoting`, `expired` (sweep only), `failed` |
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
| readable | `finalAmount`, `itemsSubtotal`, `shipping` and `tax.amount` all present and parsable as exact decimals (a JSON number **or** a decimal string) | `price_unverifiable` / `quote_amounts_unreadable` |
| currency | every one of those four — and `rc.quote_total` where it can read the field — names the **row's** currency | `price_changed` / `quote_currency_mismatch` |
| subtotal | `itemsSubtotal == quantity × our_price_minor`, **exactly**, no tolerance | `price_changed` / `quote_items_subtotal_mismatch` |
| reconciles | `finalAmount == itemsSubtotal + shipping + tax`, within **±1 minor unit** | `price_changed` / `quote_total_not_reconciled` |

A `None` is **never** COALESCEd into "fine": an amount we cannot state exactly is one we do not
buy against. `shipping` and `tax` may be exactly `0.00` (free shipping is the commonest quote
there is) and are read through a sibling converter for that reason; a **negative** component is
still refused. A quote whose `expiresAt` has already passed is not turned into a checkout —
release with `quote_expired` and re-quote next step.

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
**To stop the rail: turn the dial off (no new purchases) and let the backlog drain.**

Backoff (`POLL_INTERVALS`, seconds, no progress made): `resolving` 60, `needs_enrollment` 30,
`quoting` 60, `awaiting_approval` 30, `processing` 15. A **transport** failure uses the state's
interval doubled, capped at `MAX_BACKOFF_SECONDS` = 600.

---

## What a poller must do

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
   later is not caught, and `upsert_pending_enrollment` takes no holder. Closing it needs a
   holder conjunct on the enrollment writes or an outbox — both ledger changes.
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
6. **`lease_seconds` must exceed the longest step.** A quote takes 13–19 s and the quote step
   also creates a checkout, so `quoting` can take ~40 s. The ledger's floor is 30 s; 300 s (its
   default) is sane.
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

`completed_without_order_id` — the partner said COMPLETED and gave no usable `orderId`. The row
goes to (or stays in) **`processing`**, never `completed`: `orderId` is the only thing that makes
the order findable, and `close_external_order_conversion` drops an empty one. `processing` is the
state `fail_exhausted_purchases` skips by default, so the row waits for a person rather than
being auto-failed over a charge that succeeded. A later poll that carries the id completes it.

`final_amount_missing` — the order exists and the row **is** completed, but no attribution edge
was written. That edge is idempotent on `(merchant_id, external_order_id)` via
`ON CONFLICT DO NOTHING`, so writing one with a null amount would permanently occupy the slot and
that order would read as zero GMV for ever. Leaving it empty lets a reconciliation close it.

`partner_id_malformed` — a partner id we will not store, because it cannot survive the client's
own path-parameter rule. Storing one put the row in `awaiting_approval` and made every later poll
raise out of `advance`, unbounded, since that state is exempt from the attempts counter.

`price_changed` is **fail closed and final**: we do not re-offer at the new price. The owner
starts a new purchase. Same for `ENROLLMENT_NOT_ACTIVE` — `quoting` → `needs_enrollment` is not a
legal edge, so there is no way back to the card page on that purchase.

**A transport failure records no code in the database.** `release_claim` accepts `next_poll_at`
and nothing else, and there are no self-edges, so on a transport error the code exists only on
the returned `AdvanceResult` and in one log line. Closing that gap needs a ledger change.

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
* **`accept_variant_labels` / `also_accept_domains`.** Migration 224 has no columns for them and
  `advance` runs in another process, so they cannot reach the resolver. `start_purchase` refuses
  a row carrying either (`resolution_hints_not_persistable`) rather than accepting a purchase
  whose stated remedy we will not apply. Lifting this is one migration plus three lines in
  `_resolution_inputs`.
* **`market_country`.** Accepted, validated, and **not persisted** — same reason. Dropping it
  degrades recall only; an out-of-market variant comes back in another currency or at another
  price and is refused as `price_changed`.
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
* **Reading an enrollment row by id.** The ledger exports `get_active_enrollment(buyer_ref)` and
  nothing else, so `needs_enrollment` re-reads its own pending row through
  `upsert_pending_enrollment(enrollment_id=…)`. That bumps `updated_at`, and in a narrow race can
  mint one stray pending row — which the next step turns into a named failure
  (`enrollment_row_unreadable`), not a silent wrong answer. A `get_enrollment_by_id` on the ledger
  retires the workaround.

---

## Tests

| file | dialect | what it is for |
|---|---|---|
| `tests/test_reap_agentic_purchase.py` | SQLite | every state, every refusal, the fence under interleaving, PII, the backoff table |
| `tests/test_reap_agentic_purchase_postgres.py` | Postgres (dialect gate) | the fence across **two backend connections**, jsonb-as-text, the server-side clock, the partial unique index, PREPARE |

Both drive the **real** ledger and the client's **real** pure helpers; only the client's six
transport functions are faked, and an autouse fixture makes an unpatched `httpx.AsyncClient`
raise so a step that reached the network fails rather than hangs.

```
.venv/bin/python -m pytest tests/test_reap_agentic_purchase.py
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_reap_wp2b_test \
    .venv/bin/python -m pytest tests/test_reap_agentic_purchase_postgres.py
```
