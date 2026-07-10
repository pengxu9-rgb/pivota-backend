# P-T2.3.7 — PSP-agnostic off-session capture (Adyen) — scope

Generalize the in-chat off-session capture beyond Stripe so a merchant on **Adyen**
(highest merchant-reach after Stripe) can be charged in-chat through the same ACP
lane. Tracking issue: **pivota-backend #1305**. Sibling of the platform-agnostic
connector work (P-T2.3.6 Wix) but on the **PSP** axis instead of the platform axis.

Verified against `main` (`7d9eaa0d`), 2026-07-10. File:line refs are anchors.

## TL;DR

- The **only** Stripe-coupled piece is `services/acp_offsession_capture.py`. Every
  other layer of the money path is already provider-generic or already
  Adyen-aware.
- **Adyen is already a first-class PSP** for config, readiness, routing, the buyer
  dropin, and the finalizer — so this is *not* greenfield. The gap is precisely
  the server-side **off-session (MIT) capture**.
- The genuinely-hard prerequisite mirrors UCP's: an Adyen **stored/tokenized
  payment method** (`shopperReference` + `storedPaymentMethodId`) to charge
  off-session. Without it there is no Adyen MIT to run — so a real Adyen canary
  needs both an **Adyen test account** and a **tokenized buyer**.

## Current state

### What is Stripe-coupled (the whole gap)

`services/acp_offsession_capture.py::capture_offsession` is Stripe-only:
- resolves the PSP with a **hard-coded** `provider="stripe"`
  (`acp_offsession_capture.py:122`);
- imports the Stripe SDK and builds a `stripe.StripeClient` (`:142-144`);
- Stripe PaymentIntent semantics — `off_session=True, confirm=True`, customer /
  mandate resolution for SCA (`:154-180`);
- Stripe key-prefix live/test detection — `sk_live_`/`rk_live_` (`:129`);
- result mapping off `PaymentIntent.status` (`succeeded` / `requires_action`)
  (`:190-206`).

Its caller — `routes/agent_payment_sdk.py::create_payment`, the ACP off-session
lane (~`:954`) — is already provider-agnostic in spirit: it calls
`capture_offsession(merchant_id=…, payment_method=request.payment_method.token, …)`
and maps the normalized `OffSessionCaptureResult` into the shared success path.
**Only the capture helper needs to become provider-dispatched.**

### What is already provider-generic / Adyen-aware (reuse as-is)

- **PSP resolution is not Stripe-bound.**
  `merchant_psp_config_service.fetch_active_runtime_merchant_psp(merchant_id,
  provider=None)` (`:560`) already resolves the merchant's active runtime PSP for
  **any** provider; the capture path simply passes `"stripe"`. Drop the hint and
  it returns the merchant's real PSP (row carries `provider`, `api_key`,
  `secret_key`, `environment`, `provider_config`).
- **Adyen config + readiness already exist.** `SUPPORTED_CANONICAL_PSPS =
  {"stripe", "adyen", "checkout"}` (`:19`); `normalize_provider_config` stores
  Adyen `merchant_account` + `client_key` (`:153-167`); `evaluate_psp_readiness`
  has Adyen blockers (merchant account / client key missing) (`:451-455`);
  `normalize_psp_environment` already derives Adyen **live/test from the
  `live_`/`test_` API-key prefix** (`:93-97`) — so generalized live-key detection
  is a one-liner (`normalize_psp_environment(provider, api_key, env) == "live"`).
- **Adyen buyer path exists.** `merchant_payment_initiation_service` builds an
  `adyen_session` / `adyen_dropin` component (`:120-125,228`) — the on-session
  buyer surface. `payment_routing_service` already ranks `stripe > adyen > paypal`
  (`:176,943,1235`). `psp_payment_finalizer` already handles Adyen
  `CAPTURE_FAILED` (`:145`).

## Design — a capture-provider interface

Introduce a thin dispatch so `capture_offsession` selects an adapter by the
merchant's active PSP provider, keeping the **normalized `OffSessionCaptureResult`
contract and every safety guard** (amount cap, idempotency, live/test lane rules,
merchant-of-record) at the shared layer.

```
capture_offsession(merchant_id, amount_cents, currency, idempotency_key,
                   payment_method, metadata, max_cents, allow_live)
  ├─ resolve active runtime PSP (no provider hint) → {provider, key, environment}
  ├─ shared guards: amount>0, ≤cap, live/test lane vs key environment
  └─ dispatch by provider:
       stripe → StripeCaptureAdapter  (current code, extracted verbatim)
       adyen  → AdyenCaptureAdapter   (new; MIT /payments)
       else   → _fail("unsupported_capture_provider")   # honest, fail-closed
```

- The shared layer owns the **lane rules** (test lane refuses a live key + a real
  PM; live lane requires a live key + a real PM). Generalize the key-environment
  check via `normalize_psp_environment` so it holds for Adyen `live_`/`test_` keys,
  not just Stripe prefixes.
- Each adapter maps its provider result → `OffSessionCaptureResult`
  (`succeeded` → success; SCA/redirect → `requires_action`; refusal/error →
  failed). **Never raises** (same contract as today).
- **Default-off + fail-closed unchanged**: same kill-switch, allowlist, caps, and
  `AGENT_ACP_TEST_CAPTURE` / `AGENT_ACP_ALLOW_LIVE_CAPTURE` flags gate the lane; an
  unsupported provider fails closed to the redirect floor upstream.

## Adyen off-session (MIT) specifics

The Adyen analog of Stripe's off-session `confirm` is a **merchant-initiated
transaction against a stored payment method**:

- **Endpoint:** `POST {checkout-base}/v71/payments` with header `X-API-Key: <adyen
  api key>` and `Idempotency-Key: <key>`.
- **Body:** `{ merchantAccount, amount:{value:<minor units>, currency}, reference,
  paymentMethod:{type:"scheme", storedPaymentMethodId:<token>}, shopperReference,
  shopperInteraction:"ContAuth", recurringProcessingModel:"CardOnFile" }`. Minor
  units match Stripe (`amount_cents`).
- **resultCode mapping:** `Authorised` → success; `RedirectShopper` /
  `ChallengeShopper` / `IdentifyShopper` → `requires_action` (SCA — cannot
  complete in-chat off-session; MIT normally carries an SCA exemption, so this
  should be rare); `Refused` / `Error` / `Cancelled` → failed.
- **Two Adyen-specific wrinkles Stripe doesn't have:**
  1. **Live URL prefix.** Adyen's live endpoint is
     `https://{prefix}-checkout-live.adyenpayments.com/checkout/v71` (the
     `{prefix}` is a per-merchant Customer-Area value); test is
     `https://checkout-test.adyen.com/v71`. Unlike Stripe (one base, key selects
     env), Adyen needs the **live prefix stored in `provider_config`** — a config
     field to add for live. Test needs no prefix.
  2. **Capture delay.** If the merchant account is set to **manual/delayed
     capture**, `Authorised` is an *authorization only* — funds require a separate
     `POST /payments/{pspReference}/captures`. For parity with Stripe's
     confirm+capture, either require **immediate capture** accounts for v1 or add
     a follow-up capture call. Scope note, not a blocker for an immediate-capture
     test account.

## The hard prerequisite — a tokenized Adyen buyer

MIT needs `shopperReference` + `storedPaymentMethodId`. These only exist after the
buyer was **tokenized** — an initial Adyen payment (or zero-auth) with
`storePaymentMethod:true` + `recurringProcessingModel` + `shopperReference`
(typically via the existing dropin/sessions flow,
`merchant_payment_initiation_service`). This is the Adyen analog of Stripe's
`SetupIntent{customer, usage:off_session}` → `pm_`, and it is the same
class of "buyer token" problem UCP has. Implication:

- **Test-lane proof needs a stored PM.** Adyen has no universal "test PM" like
  Stripe's `pm_card_visa`; the canary must first tokenize an Adyen **test** card
  (test PAN 4111 1111 1111 1111 via the dropin with `storePaymentMethod`) to get a
  `storedPaymentMethodId` + `shopperReference`, then run the MIT capture. Budget
  for a small tokenization step in the canary runbook.

## Work breakdown (dependency-ordered)

1. **[refactor] Extract the current Stripe body** of `capture_offsession` into a
   `StripeCaptureAdapter` behind a small `CaptureProvider` protocol
   (`capture(...) -> OffSessionCaptureResult`). Byte-equivalent behavior; existing
   Stripe tests must stay green (mirror the P-T2.3.6 extraction discipline).
2. **[core] Provider dispatch** in `capture_offsession`: resolve the active PSP
   without a provider hint; move the lane/cap/idempotency guards to the shared
   layer; generalize live-key detection via `normalize_psp_environment`; dispatch
   to the adapter; unsupported provider → `_fail("unsupported_capture_provider")`.
3. **[adyen] `AdyenCaptureAdapter`** — MIT `POST /payments` (httpx), body above,
   `Idempotency-Key`, resultCode → result mapping, test-vs-live base URL (+ live
   `prefix` from `provider_config`). Read `merchant_account` from the PSP row's
   `provider_config`.
4. **[config] Live-prefix field** — add `provider_config.live_url_prefix` (or
   `endpoint_prefix`) for Adyen live in `normalize_provider_config` + readiness
   (blocker when live but prefix missing). Test lane unaffected.
5. **[caller] No change expected** — `create_payment`'s off-session lane already
   passes provider-generic args; confirm the forwarded token is treated as the
   Adyen `storedPaymentMethodId` when the provider is Adyen (the Stripe adapter
   keeps `pm_` semantics; the Adyen adapter treats the token as a stored-PM id).
6. **[canary] Tokenize + capture** — runbook: tokenize an Adyen test card via the
   dropin (`storePaymentMethod`) → obtain `shopperReference` +
   `storedPaymentMethodId` → arm the existing (protocol-agnostic)
   `AGENT_ACP_TEST_CAPTURE` + allowlist on an Adyen **test** canary merchant → run
   the ACP chain → verify the Adyen test dashboard. Then P-T2.3.5-style live lane.
7. **[optional] Add `checkout`/PayPal adapters** later — the dispatch makes them
   incremental; out of scope for this pass.

**Recommended phasing:** items 1-3 + 5 → Adyen **test** canary (item 6) → item 4
+ live lane. Items 1-2 are pure refactor + dispatch and can land dark ahead of any
Adyen account.

## Open questions (founder / ops)

1. **Adyen test account** — need a canary merchant with an Adyen **test** API key +
   merchant account + client key in `merchant_psps` (the Adyen analog of
   `merch_efbc`'s Stripe test key). **This is the gating prerequisite** — nothing
   can be proven without it. Which merchant / who provisions it?
2. **Buyer tokenization surface** — reuse the existing Adyen dropin to store a PM
   (recommended), or a dedicated zero-auth tokenization step? Gates the canary.
3. **Capture mode** — require immediate-capture Adyen accounts for v1 (simplest),
   or implement the separate `/captures` follow-up for manual-capture accounts?

## Non-negotiables (inherit from ACP)

- Default **OFF** / dark; fail-closed kill-switch + allowlist + caps govern every
  charge; unsupported provider fails closed.
- Merchant-of-record on the merchant's own PSP; no money-topology change.
- Test lane refuses a live key; live lane refuses a test key / test PM — enforced
  at the shared layer for **all** providers, not just Stripe.
- Idempotency-keyed capture so a retry cannot double-charge (Stripe idempotency
  key ↔ Adyen `Idempotency-Key` header).
- Normalized `OffSessionCaptureResult` contract preserved; adapters never raise.
