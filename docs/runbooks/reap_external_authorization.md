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
| c | Card status ≠ `issued` | `TRANSACTION_NOT_ALLOWED` | `card_not_live` | — |
| c | `expires_at` in the past (or NULL) | `TRANSACTION_NOT_ALLOWED` | `card_expired` | — |
| d | Single-use card with a prior APPROVE | `TRANSACTION_NOT_ALLOWED` | `already_authorized` | — |
| e | `channel` ≠ `ECOMMERCE` | `TRANSACTION_NOT_ALLOWED` | `channel_not_allowed` | — |
| f | Neither currency leg is the card's | `TRANSACTION_NOT_ALLOWED` | `currency_mismatch` | `CARD_AUTH_CURRENCY_MISMATCH` |
| f | Amount ≤ 0, or more decimals than the currency has | `TRANSACTION_NOT_ALLOWED` | `amount_unparseable` | — |
| f | Amount > `amount_cap_minor` | **`INSUFFICIENT_BALANCE`** | `over_cap` | — |
| g | Domain has pins, none matches | `TRANSACTION_NOT_ALLOWED` | `merchant_mismatch` | `CARD_AUTH_MERCHANT_MISMATCH` |
| h | Everything held | — (APPROVE) | `approved` | — |

`over_cap` is the only rule that answers `INSUFFICIENT_BALANCE`: it is the only decline that
means "this instrument does not carry that much", which is what the cardholder's terminal shows.
Everything else is our own control, and maps to `TRANSACTION_NOT_ALLOWED`.

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

Descriptors normalize by: casefold → cut at the first `*` → punctuation to spaces → collapse
whitespace. `"ACME Store, Inc.*1234"` → `acme store inc`.

> **Known gap:** the `*` cut is a *suffix* rule. Acquirers that prefix (`SQ *ACME`,
> `PAYPAL *ACME`) normalize to `sq` / `paypal`. That mis-approves nothing — a wrong descriptor
> still has to match a pin — but it pins a useless value on a first authorization at such a
> merchant. Splitting on which side of the `*` carries the merchant needs an acquirer-prefix
> list we do not have yet.

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

**Latency.** `latency_ms` is recorded on every row and a `logger.warning` fires above 800 ms.
That clock does not include either network leg, and the budget is 1.6 s end to end, so treat a
sustained warn rate as an outage in progress, not a nice-to-have.

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
cards are hitting our endpoint; `merchant_mismatch` means a merchant changed descriptor (pin the
new one manually with `source='manual'` after confirming it); `currency_mismatch` means a
merchant started billing in a currency we did not mint for; `card_not_live` in bulk usually means
a revocation sweep ran.

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
