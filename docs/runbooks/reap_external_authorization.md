# Runbook — Reap external authorization

`POST /webhooks/reap/authorize`

## What this is

Reap's sandbox project is configured **Program-Funded + External authorization**. In that mode
Reap does not decide card authorizations on its own balance rules alone: on **every**
authorization it stops at the network, POSTs us a signed `CARD_AUTHORIZATION_REQUEST`, and waits
for us to say APPROVE or DECLINE.

This is the **live decision**, and it is the only moment at which we can stop a charge.

It is **not** the record. The record is the `CARD_TRANSACTION_CREATED` webhook that arrives
afterwards on `POST /webhooks/reap`, and it carries our `eventId` back as `triggerEventId` — that
field is the join from decision to record. The two receivers are separate endpoints with separate
registrations at Reap, separate signing secrets, and opposite response-code postures.

Reap runs its own guardrails (card state, spend policies) **before** calling us, so every rule
below is a second opinion, not the only one.

### The 1.6-second budget, and why everything fails closed

Reap gives us **1.6 seconds**. A timeout, an unreachable host, a non-2xx status, or a body it
cannot parse all produce a **decline**, shown to the cardholder as a generic `INTERNAL_ERROR`.

That single fact sets the whole posture: every error path here is already a decline, so every
ambiguity resolves toward one. Both env dials, a bad signature, an unparseable body, and an
unhandled 500 all end the same way — no charge.

| Status | When | Effect at Reap |
|---|---|---|
| `503` | `REAP_EXTERNAL_AUTH_ENABLED` not truthy, or `REAP_AUTH_WEBHOOK_SECRET` unset | decline |
| `413` | body over 64 KiB — refused **before** the HMAC, so an unauthenticated caller cannot choose how much hashing we do inside the 1.6 s budget | decline |
| `401` | signature missing, wrong, or outside the 5-minute window | decline |
| `400` | signed body is not JSON / not a `CARD_AUTHORIZATION_REQUEST` / has no `eventId`+`cardId` | decline |
| `500` | database unavailable mid-decision | decline |
| `200` | a real decision (see the rules table) | approve or decline as answered |

The `500` is a deliberate non-catch. Approving without a ledger row, or declining without one,
would move or refuse money with no record of why.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `REAP_EXTERNAL_AUTH_ENABLED` | **off** | Master switch. Not truthy ⇒ 503 for everything. Turn on only once the Reap project is actually in EXTERNAL authorization mode. |
| `REAP_AUTH_WEBHOOK_SECRET` | unset | The `signingSecret` Reap returns when the REQUEST-mode endpoint is created. |
| `REAP_AUTH_WEBHOOK_SIG_HEADER` | `x-reap-webhook-signature` | Override only if Reap changes the header name. |
| `REAP_EXTERNAL_AUTH_DEADLINE_MS` | `1200` | Past this, an APPROVE is downgraded to `deadline_exceeded`. Floored at 1 ms so a stray `0` cannot mean "approve nothing". |

**The secret is NOT `REAP_WEBHOOK_SECRET`.** That one belongs to the notification receiver on
`/webhooks/reap`. The REQUEST endpoint is registered separately and gets its own secret, and the
handler deliberately does not fall back — a fallback would authenticate live spending decisions
with the notification receiver's key.

The switch is checked **before** the secret, so it alone takes the endpoint out of service
without anyone touching secret storage.

## Registering the endpoint with Reap

`POST /webhooks/` (verified against `docs.reap.global/api-reference/webhooks/create-webhook-endpoint`):

```json
{
  "name": "pivota-external-authorization",
  "url": "https://api.pivota.cc/webhooks/reap/authorize",
  "mode": "REQUEST"
}
```

* `name` — required, 1–100 chars.
* `url` — required, max 500 chars.
* `mode` — optional, defaults to `NOTIFICATION`. **`REQUEST`** is what makes this a synchronous
  authorization endpoint, and it is only accepted for projects whose authorization mode is
  `EXTERNAL`.

The 200 response carries `id`, `name`, `url`, `status`, `mode`, `lastUsedAt`, `createdAt`,
`updatedAt` — and **`signingSecret`**.

> `signingSecret` is **returned exactly once** and cannot be retrieved again. Put it into
> `REAP_AUTH_WEBHOOK_SECRET` from the API response, before you close the terminal. Losing it
> means deleting the endpoint and registering a new one.

Prod host is `api.pivota.cc` — the ops API answers only there.

## Signature verification

Header `X-Reap-Webhook-Signature`, value `t=<unix>,v1=<hex>`:

```
v1 = HMAC-SHA256(signingSecret, "{t}.{raw_body}")     # hex
```

* The HMAC covers the **raw received bytes**. Nothing re-serializes the JSON first — re-ordering
  keys breaks valid signatures, and accepting a re-serialized match would mean accepting bodies
  we cannot re-verify later.
* **5-minute window** on `t`, in both directions. This is what makes a captured authorization
  unreplayable; without it, one observed approval could be re-spent until the card expires.

One implementation, shared with the notification receiver:
`services/reap_webhooks.verify_signature`. It also still accepts the older bare-hex /
`sha256=<hex>` form that endpoint was registered with — not a downgrade path, since that form
signs a different message (`{body}` rather than `{t}.{body}`) and still requires the secret.

## The decision rules

First failing rule wins. Every path writes exactly **one** row to `agent_card_auth_decisions`.
`reason` is Reap's vocabulary (only two values exist); `reason_code` is ours, and it is the
column to query.

| # | Rule | Wire `reason` | `reason_code` | Alarm |
|---|---|---|---|---|
| a | `event_id` already decided | *the stored decision, verbatim* | *stored* | — |
| b | No card with that `cardId` | `TRANSACTION_NOT_ALLOWED` | `unknown_card` | `CARD_AUTH_UNKNOWN_CARD` |
| b | **>1 `issued` card for that `cardId`** | `TRANSACTION_NOT_ALLOWED` | `ambiguous_card` | `CARD_AUTH_AMBIGUOUS_CARD` |
| c | Card status ≠ `issued` | `TRANSACTION_NOT_ALLOWED` | `card_not_live` | — |
| c | `expires_at` in the past (or NULL) | `TRANSACTION_NOT_ALLOWED` | `card_expired` | — |
| d | Single-use card with a prior **spend** APPROVE | `TRANSACTION_NOT_ALLOWED` | `already_authorized` | — |
| e | `channel` ≠ `ECOMMERCE` | `TRANSACTION_NOT_ALLOWED` | `channel_not_allowed` | — |
| f | Neither currency leg is the card's | `TRANSACTION_NOT_ALLOWED` | `currency_mismatch` | `CARD_AUTH_CURRENCY_MISMATCH` |
| f | **Amount is exactly 0** | — (APPROVE) | `zero_amount_verification` | — |
| f | Amount < 0, over-precise, or > `MAX_AMOUNT_MINOR` | `TRANSACTION_NOT_ALLOWED` | `amount_unparseable` | — |
| f | Prior approvals + this amount > `amount_cap_minor` | **`INSUFFICIENT_BALANCE`** | `over_cap` | — |
| g | Domain has pins, none matches | `TRANSACTION_NOT_ALLOWED` | `merchant_mismatch` | `CARD_AUTH_MERCHANT_MISMATCH` |
| h | Everything held | — (APPROVE) | `approved` | — |
| — | **Answer produced after the deadline** | `TRANSACTION_NOT_ALLOWED` | `deadline_exceeded` | — |

`over_cap` is the only rule that answers `INSUFFICIENT_BALANCE`: it is the only decline that
means "this instrument does not carry that much", which is what the cardholder's terminal shows.
Everything else is our own control, and maps to `TRANSACTION_NOT_ALLOWED`.

**`ambiguous_card`.** `issuer_card_ref` carries no unique constraint, so two `issued` rows can
claim one Reap card — with different caps, at different merchants. `find_by_issuer_ref` orders
by `created_at DESC` so the choice is at least deterministic, but deterministic is not correct:
enforcing one instrument's cap on the other's spend is silently wrong. We decline instead. Seeing
this alarm means an issuance path produced a duplicate; find both rows and revoke the wrong one.

**`zero_amount_verification`.** A $0.00 authorization is the routine live-card check merchants
run *before* the real charge, so declining it declines the purchase it precedes. It approves,
records `amount_minor = 0`, **pins no descriptor** (a verification says the card works, not that
this merchant owns it), and — the point — does not reserve the card: rule (d) counts only
approvals with `amount_minor > 0`. Every earlier rule still applies, so a verification on a
revoked or expired card still declines.

**`deadline_exceeded`.** Not a rule, a downgrade. See *Time* below.

Alarms are `logger.error` and carry **ids and the reason code only** — never the merchant
descriptor, city, postcode, amounts, `accountId`, or wallet. Alert on the `code=CARD_AUTH_*`
prefix.

### Notes on individual rules

**(a) Idempotency.** `event_id` is the primary key. A retried request is answered with the
verdict we already gave, never re-evaluated — re-deciding would run rule (d) against the
reservation our own earlier APPROVE created, and decline the authorization we just approved.

**(d) Single use is a reservation, not a counter.** The whole decision runs in one transaction
opened with `pg_advisory_xact_lock` keyed on the Reap card id. Without it, two concurrent
authorizations for one card both see no prior APPROVE and both approve — a cap breached by 100%.
`tests/test_reap_external_auth_postgres.py` reproduces exactly that (2 APPROVE rows) and then
shows the same interleaving yielding 1 with the lock taken. Different cards stay concurrent.

**(f) Which amount.** Reap sends a billing pair (`currency`/`amount`) and the merchant's
presentment pair (`originalCurrency`/`originalAmount`), both as decimal **major-unit** numbers.
The cap is in the card's currency, so we compare whichever pair is denominated in it —
presentment first, because that is the number the merchant actually charged. Neither being in
the card's currency means an FX conversion we did not authorize stands between the charge and
the cap, so the cap is not enforceable and we decline.

Amounts are **refused, never rounded**: `1.005 USD` and `500.5 JPY` decline with
`amount_unparseable`. Every rounding rule picks a direction, and on a spending cap both
directions are wrong. The body is parsed with `parse_float=Decimal`, so a binary float never
touches a cap comparison.

**(g) The merchant registry is learned.** The card network gives us a descriptor
("ACME STORE", "Berlin", "DE"), never a domain, and no descriptor-to-domain mapping exists for us
to consult. So a `merchant_domain` with **no** pins approves its first authorization on the
strength of the other constraints and pins what it saw (`source='authorization'`,
`merchant_verified=false` on that row); every later authorization must match a pin on
`(name_norm, country)`. The exposure that buys is bounded by exactly one authorization, at or
below a cap we set, on a single-use card that expires.

Descriptors normalize by: casefold → split on the first `*` and keep the **longer** side →
punctuation to spaces → collapse whitespace.

| Raw descriptor | `name_norm` |
|---|---|
| `ACME Store, Inc.*1234` | `acme store inc` |
| `SQ *HONEST SHOP` | `honest shop` |
| `PAYPAL *ACME STORE` | `acme store` |
| `SQ *` | `sq` — **not pinnable** |
| `***` | `` — **not pinnable** |

> **A WRONG PIN IS NOT HARMLESS.** An earlier version of this runbook said the `*` handling
> "mis-approves nothing, because a wrong descriptor still has to match a pin". **That was
> false**, and the mechanism is the pinning itself. Under the old prefix-keeping rule
> `SQ *HONEST SHOP` normalized to `sq`; the first authorization for a domain *pins* what it saw,
> so `sq` became the pin — and the next authorization from a **different** merchant behind the
> same acquirer also normalized to `sq`, matched, and was approved `merchant_verified=true`. The
> wrong pin does not merely fail to protect; it manufactures positive evidence for an unrelated
> merchant.
>
> Two guards now. The longer-side split is a **heuristic** (acquirer tags are short, merchant
> names are not) and can still be wrong — so `is_pinnable` refuses to *learn* any descriptor
> carrying fewer than 3 alphanumerics. Such an authorization still approves, at
> `merchant_verified=false`, and the domain simply stays unlearned. That is strictly safer: the
> cap, the single use and the expiry still bound it, and every weak decision is queryable.

**Nothing here touches `agent_issued_cards`.** `apply_auth_approved` is guarded on
`status='issued'` and alarms `AUTH_ON_NON_ISSUED_CARD` otherwise — so a decision that exhausted
the card would make its own `CARD_TRANSACTION_CREATED` webhook alarm falsely, on every single
approval. The card's status moves only when the record arrives.

## Sandbox test loop

1. Set `REAP_EXTERNAL_AUTH_ENABLED=true` and `REAP_AUTH_WEBHOOK_SECRET=<signingSecret>`.
2. Mint a card through the normal agent flow, so `agent_issued_cards` has an `issued` row with
   an `issuer_card_ref`, a cap, a currency, and a `merchant_domain`.
3. Fire a simulated authorization at Reap:

   ```
   POST /simulation/card-transactions/authorization
   { "cardId": "<issuer_card_ref>", "amount": <major units>, "merchant": { ... } }
   ```

   Reap calls our endpoint synchronously and applies our answer.
4. Read back what we decided:

   ```sql
   SELECT event_id, card_id, decision, reason, reason_code, amount_minor, currency,
          merchant_verified, latency_ms, created_at
     FROM agent_card_auth_decisions
    ORDER BY created_at DESC
    LIMIT 20;
   ```
5. Confirm the pin was learned, then re-fire the same authorization and confirm
   `merchant_verified` is now `true` and `seen_count` incremented:

   ```sql
   SELECT * FROM agent_card_merchant_descriptors WHERE merchant_domain = '<domain>';
   ```
6. Re-fire on the same single-use card and confirm `already_authorized`.
7. Confirm the `CARD_TRANSACTION_CREATED` webhook then lands on `/webhooks/reap` with
   `triggerEventId` equal to the `event_id` above, and that it — not the decision — is what moved
   the card to `exhausted`.

## Operating

### Time is a correctness property here

Reap declines at 1.6 s. An answer computed after that is an answer **nobody acted on** — and an
APPROVE recorded then is worse than no answer at all, because on a single-use card it reserves
the instrument against a purchase that was already refused. The buyer's genuine retry then dies
on `already_authorized` and the card is spent without a charge. Three things prevent that:

* **`SET LOCAL lock_timeout = '400ms'`** — `pg_advisory_xact_lock` otherwise blocks
  *indefinitely*.
* **`SET LOCAL statement_timeout = '1200ms'`** — `DB_STATEMENT_TIMEOUT_SECONDS` defaults to `0`,
  i.e. no ceiling at all.
* **The deadline downgrade** — before the row is written, if elapsed wall clock exceeds
  `REAP_EXTERNAL_AUTH_DEADLINE_MS`, an APPROVE is recorded as a `deadline_exceeded` DECLINE.
  Declines are *not* downgraded: a late decline agrees with what Reap did, and rewriting its
  `reason_code` would destroy the evidence of which rule fired.

When a timeout fires the transaction rolls back and the request 500s. Reap declines — the same
outcome for the shopper as a slow success, with no phantom row. **A `deadline_exceeded` row means
we were too slow, not that anything was wrong with the card**; investigate the database, not the
buyer.

**Latency.** `latency_ms` is recorded on every row and a `logger.warning` fires above 800 ms.
That clock does not include either network leg, and the budget is 1.6 s end to end, so treat a
sustained warn rate as an outage in progress, not a nice-to-have.

```sql
SELECT count(*) FILTER (WHERE reason_code = 'deadline_exceeded') AS missed,
       count(*) AS total
  FROM agent_card_auth_decisions
 WHERE created_at > now() - interval '1 hour';
```

```sql
SELECT date_trunc('hour', created_at) AS hour,
       count(*), max(latency_ms), avg(latency_ms)::int
  FROM agent_card_auth_decisions
 GROUP BY 1 ORDER BY 1 DESC LIMIT 24;
```

**Decline mix.** A sudden shift in `reason_code` is the fastest read on what broke:

```sql
SELECT reason_code, count(*)
  FROM agent_card_auth_decisions
 WHERE created_at > now() - interval '1 day'
 GROUP BY 1 ORDER BY 2 DESC;
```

Rough triage: a spike in `unknown_card` means our issuance writes are lagging or someone else's
cards are hitting our endpoint; `merchant_mismatch` means a merchant changed descriptor (see the
recipe below); `currency_mismatch` means a merchant started billing in a currency we did not mint
for; `card_not_live` in bulk usually means a revocation sweep ran; `deadline_exceeded` is a
database problem, not a card problem; `ambiguous_card` means an issuance path produced two live
rows for one Reap card.

### Recipe: `merchant_mismatch` alarms on domain X

The pin is a guess made from one authorization under a 1.6-second budget. When it is wrong —
the merchant changed acquirer or rebranded, or the first authorization carried a descriptor that
normalized badly — **every** later authorization for that domain declines and nothing recovers on
its own. This is an operator action, not a wait.

**1. See what is pinned, and what is actually arriving.**

```sql
SELECT id, name_norm, country, city_norm, source, seen_count, first_seen_at, last_seen_at
  FROM agent_card_merchant_descriptors
 WHERE merchant_domain = 'X';

SELECT merchant_name, merchant_country, count(*), max(created_at)
  FROM agent_card_auth_decisions
 WHERE reason_code = 'merchant_mismatch'
   AND created_at > now() - interval '7 days'
 GROUP BY 1, 2 ORDER BY 3 DESC;
```

The second query gives the **raw** descriptor the declines are carrying. Confirm out of band
that it really is that merchant — a mismatch is also exactly what a card being used somewhere it
should not be looks like, and pinning the attacker's descriptor would be the worst possible
response.

**2a. The old pin is stale — remove it.** The domain re-learns from the next authorization.

```python
from db.agent_card_auth_decisions import unpin_descriptor
await unpin_descriptor("X", "the stale name_norm")     # returns rows removed
```

**2b. You know the correct descriptor — pin it deliberately.** Pass the **raw** descriptor;
`pin_descriptor_manual` normalizes it through the same function the decision path uses, so a
hand-normalized string is the one way to get a pin that can never match.

```python
from db.agent_card_auth_decisions import pin_descriptor_manual
await pin_descriptor_manual("X", "SQ *THE REAL SHOP", "DE")   # source='manual'
```

Re-pinning an existing descriptor promotes it to `source='manual'` rather than failing, so 2a
and 2b are safe in either order. A domain may hold several pins; a match against any one of them
approves.

**3. Confirm.** The next authorization should record `merchant_verified = true` and the pin's
`seen_count` should advance.

**Turning it off.** Set `REAP_EXTERNAL_AUTH_ENABLED=false`. Every authorization then gets a 503
and Reap declines it — this stops all card spending on the program, so it is a
break-glass, not a rollback.

**Rolling back the schema.** `db/migrations/down/207_agent_card_auth_decisions_down.sql`. Dropping
`agent_card_auth_decisions` destroys the single-use reservations, so do it only with the feature
switched off.

## Related

* `routes/reap_webhooks.py` — both receivers.
* `services/reap_external_auth.py` — the rules.
* `db/agent_card_auth_decisions.py` — the ledger and the registry.
* `db/migrations/207_agent_card_auth_decisions.sql`, mirrored in `db/schema_guard.py` because
  production deploys skip `db/migrations/`.
* `tests/test_reap_external_auth.py`, `tests/test_reap_external_auth_postgres.py`.
