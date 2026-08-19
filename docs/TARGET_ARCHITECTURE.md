# Pivota target architecture — the one-pager

**Status:** Authoritative summary of accepted decisions · **Date:** 2026-08-11
**Sources:** ADR-014 (protocol interoperability), ADR-021 (gateway topology), ADR-012/013/016 (identity, settlement, non-custodial), the 2026-08 two-repo architecture audit.

This page exists because the production narrative and the ADRs drifted apart:
earlier "target architecture" statements claimed capabilities that do not exist
(backend MCP, Google-UCP order schema, TAP fields) and forbade capabilities the
founders had already decided to build (capture, refunds). **When this page and
an ADR disagree, the ADR wins — and this page is a bug.**

## The shape (ADR-021, accepted 2026-08-01)

**PIVOTA-Agent is the protocol gateway. pivota-backend is the execution layer.**

```
outside agents (Claude / ChatGPT / Gemini / UCP platforms)
        │  MCP JSON-RPC · OpenAI-ACP REST · UCP discovery+webhooks
        ▼
PIVOTA-Agent  ──  all public protocol DOORS (read + transact)
  · safety-kernel canonical executor (one money spine for every protocol)
  · unified payment-authorization verifier (ACP token / UCP handler / AP2 mandate)
  · quote-lock, idempotency ledger, charge-once — enforced ONCE
        │  internal service credential → backend money endpoints
        ▼
pivota-backend  ──  commerce index + decision layer + money EXECUTION
  · catalog/serving index, offer verification, promo stacking (quote engine)
  · ACP session service, off-session capture, kill-switch chain, refunds
  · /agent/v1/* is Pivota's INTERNAL lane, not the external contract
```

One deploy per side, one policy surface, one place hostnames point
(`mcp.pivota.cc` / `acp.pivota.cc` / `ucp.pivota.cc` → gateway). The failure
mode this prevents is the one already paid for twice: a second policy
implementation drifting from the first.

## Layer truth table (verified 2026-08-11)

| Layer | Status | Where |
|---|---|---|
| MCP door (JSON-RPC, 13 commerce tools, OAuth RS + public read tier) | **Live in prod** | Gateway |
| OpenAI-ACP REST checkout doors (5 session endpoints + feed, HMAC) | Built, **flag-off** (`AGENT_CHECKOUT_ACP_REST_ENABLED`) | Gateway |
| UCP: business profile + order-webhook receiver (spec `2026-01-23`) | Discovery live; webhook receiver flag-off; **no order-write conformance yet** | Gateway |
| AP2 mandate verification (SD-JWT VC, pinned JWKS) | Wired as a verifier family; routes live (`ENABLE_AP2_ROUTES=true`) | Both |
| x402 / Visa TAP / Mastercard Agent Pay | **Not implemented anywhere.** No placeholder code pretends otherwise | — |
| Decision layer: offer verification, identity (content_key digest-enforced), promo stacking | Live | Backend (+ shared corpus) |
| Money execution: quote-first ACP sessions, off-session capture, refunds | Live, gated (see flag manifest) | Backend |

## Red lines (restated truthfully)

1. **No cardholder data — held, by construction.** The ACP `delegate_payment`
   endpoint is a permanent 501 that never reads the request body; schema-guard
   tests forbid PAN/CVC-shaped columns; the token vault primitive is
   PSP-token-only with a fail-closed PAN detector (and is not wired).
2. **Merchant stays merchant-of-record — held.** Every buyer charge resolves
   the merchant's own runtime PSP key. The gateway holds no PSP keys. The last
   platform-key charge path (`adapters/stripe_adapter` module global) was
   deleted 2026-08-11.
3. **Capture and refunds EXIST — by decision, not by accident.** ADR-013/021
   accepted them; they run behind the kill-switch chain
   (guarded protocol → test/live caps → per-merchant live allowlist →
   `capture_offsession`) and merchant-key-only. Any statement that Pivota "has
   no capture/refund logic" is wrong and must not appear in external material.
4. **No fabricated telemetry.** Every operator- or agent-facing number is
   DB-derived or measured (2026-08-11 fabrication-belt sweeps, both repos).
   New endpoints inherit this as a review requirement.

## Operational invariants

- **Both transact lanes funnel through the same backend gate chain** (gateway
  canonical executor and in-process ACP sessions). ADR-021 asserts it; an
  executable cross-repo invariant test is still owed (Tier 3).
- **Every protocol door is env-gated.** Production readiness is a config
  question: see `docs/agent-checkout/FLAG_MANIFEST.md` in PIVOTA-Agent for the
  authoritative per-environment snapshot.
- **Production-like detection goes through `config/platform.py`, never through
  a raw env var.** Prod sets neither `NODE_ENV` nor `ENVIRONMENT`, so
  `NODE_ENV`-only guards are inert there — and `RAILWAY_ENVIRONMENT`-only
  guards become inert the moment a service moves to Cloud Run, where every
  `RAILWAY_*` is unset. Both failures are silent: the guard evaluates to "not
  production" and runs its dev branch against live traffic. Call
  `is_production()` / `is_staging()` / `is_deployed()` instead. The shim
  resolves `PIVOTA_ENV` → Railway deployment markers → Cloud Run, and FAILS
  CLOSED to `production` on a managed host it cannot resolve; every FastAPI
  entrypoint calls `require_platform_env()` so a revision deployed without
  `PIVOTA_ENV` dies at boot rather than serving on that guess. New guards get
  three-shape parity coverage in `tests/test_platform_guard_parity.py`.
- **Naming:** "ACP" inside pivota-backend is the protocol-TIER label on the
  guarded charge lane (`GUARDED_PROTOCOLS = {acp, ucp, ap2}`); OpenAI-ACP the
  wire protocol lives only at the gateway door. "UCP" is the external Universal
  Commerce Protocol (see ADR-014 Amendment 2026-08-11), not an internal surface.
