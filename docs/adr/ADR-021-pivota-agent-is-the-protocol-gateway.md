# ADR-021: PIVOTA-Agent is the protocol gateway

**Status:** Accepted (founder decision 2026-08-01) · **Date:** 2026-08-01
**Context docs:** `~/dev/PIVOTA_PROTOCOL_TOPOLOGY_REVIEW_2026-08-01.md` (measured topology + audits + decision record), ADR-014 (agent protocol interoperability), ADR-015 (canonical authorized action), ADR-016 (non-custodial).
**Implementation:** PIVOTA-Agent PR #1894 (UCP business endpoints), pivota-backend PR #1669 (in-process ACP checkout).

## Context

By 2026-08-01 every public protocol hostname — `acp.pivota.cc`, `mcp.pivota.cc`, `ucp.pivota.cc` — routed to PIVOTA-Agent, while the founder's mental model (and four running Railway services in Pivota Infra) still reflected a per-service design: `pivota-acp` (April-era ACP checkout), `ucp-web-production` (UCP business profile + hosted checkout), `ucp-platform-receiver` (order-webhook receiver), `ucp-worker` (webhook delivery). The drift was never decided; it produced two wrong-host debugging incidents in one day (2026-07-31), left live Stripe/Adyen credentials on an unmaintained scanner-probed box, and left the Tier-2 in-chat charge path wired to a service nobody monitored (`agent_checkout_intents.py` → `pivota_acp_client` → pivota-acp), dark behind a deliberately-unset `PLATFORM_ORDERS_ACP_URL`.

A code-path audit sharpened the picture:

- The **read side** (ACP feed, MCP search, UCP discovery) already lived in the gateway behind the shared serving gates — correct, because a second or third policy implementation is the failure mode this project keeps paying for (the Python/Node twins, the seedRouteOk near-miss).
- The **transact side** was split but hollow: the gateway's safety-kernel canonical executor already implements the full protocol checkout lifecycle (quote-backed sessions, an idempotency ledger, positive payment-authorization verification) against pivota-backend's money endpoints — while pivota-acp's "real capture" was itself three HTTP calls back into pivota-backend (quote → order → pay). pivota-acp never talked to a PSP; its Stripe/Adyen adapters were dead stubs. The PSP capture engine (`acp_offsession_capture`, the Tier-2 kill-switch, test/live caps) already lived in pivota-backend.
- The UCP services were broken-by-config: the signing JWK env on `ucp-worker` held a placeholder string, so worker and web fell back to per-process ephemeral keys — outbound webhook signatures could never have verified cross-service. `ucp_checkout_sessions` and `ucp_order_webhook_deliveries` held zero rows; there was no dedicated UCP database (all four services pointed at the main xmr6 Postgres).

## Decision

**PIVOTA-Agent is the protocol gateway.** All public protocol doors — read AND transact — terminate at PIVOTA-Agent. Policy is enforced once, via the shared gates. The intended callers of these doors are OUTSIDE agents speaking MCP/ACP/UCP against Pivota's commerce index, decision layer, and transaction rails; pivota-backend's Agent API (`/agent/v1/quotes/preview` → `/agent/v1/orders/create` → `/agent/v1/payments`) is Pivota's internal test lane, not the durable external contract.

Concretely:

1. **ACP checkout executes in-process in pivota-backend** (PR #1669). The Tier-2 in-chat lane (`POST /agent/v1/checkout/acp`) uses an in-backend session service (`acp_checkout_session_service`, table `acp_checkout_sessions`, migration 191) instead of the pivota-acp HTTP hop; completion (`POST /agent/v1/checkout/acp/{id}/complete`) drives the existing gate chain — `evaluate_tier2_charge` (GUARDED_PROTOCOLS) → test/live capture caps → `capture_offsession` → paid-transition finalize — extracted into `acp_offsession_payment` so the gates live exactly once for the payment SDK route and session completion. The money path is fail-closed by construction: claim-before-charge, attempt-scoped stored PSP idempotency keys, classified capture failures (definitive declines release the claim; ambiguous outcomes hold it for same-key replay), no simulation fallback, success only after the completion row is durably written. `protocol_name` is parameterized so UCP/AP2 reuse the same execution layer.
2. **UCP business endpoints live in the gateway** (PR #1894). `ucp.pivota.cc/.well-known/ucp` (business profile, decoupled from the checkout kill-switch — discovery is read-only; the kill-switch governs money, and dark capabilities are withheld from the profile), signing-key publication via `UCP_BUSINESS_SIGNING_PUBLIC_JWK` (public JWKs only; private material is refused loudly), and the order-webhook receiver (`POST /ucp/order-webhook`: detached-JWS ES256 over exact wire bytes, verification required by default, 32KB cap + rate limit, metadata-only ring buffer).
3. **The four Infra services are retired** (scale-to-zero, then delete after a quiet week) and the Stripe/Adyen keys they held are rotated. Founder steps: `~/dev/PIVOTA_PROTOCOL_GATEWAY_CUTOVER_RUNBOOK_2026-08-01.md`.
4. **`PLATFORM_ORDERS_ACP_URL` stays unset forever** — the fail-closed `acp_url_missing` refusal was the correct interim state, and the port removes the client rather than arming the retired endpoint.

## Consequences

- One deploy, one policy surface, one place hostnames point. Hostname → service ambiguity is gone (routing map: `~/dev/PIVOTA_WORKLIST.md`).
- The two transact lanes are explicit: **external agents → gateway protocol doors → safety-kernel canonical executor → backend money endpoints**, and **in-chat Tier-2 → backend in-process session service → same gates**. Both funnel through the same kill-switch/caps; neither can drift from the other's policy.
- Live payment credentials no longer sit on an unmonitored public box (after rotation).
- A follow-up sweep removes `pivota_acp_client.py` (already unreferenced by production code), and cleans the stale flags `FEATURE_PLATFORM_ORDERS_ACP` and `AGENT_ACP_LIVE_CAPTURE_MERCHANTS` (which allowlists a retired test rig).
- Outbound UCP order-webhook delivery (the retired ucp-worker's job) deliberately has NO home after retirement: zero deliveries ever occurred and the signing setup never worked. When a real platform consumer appears, delivery is built into the gateway — which now owns the business profile and publishes real signing keys — not by reviving the worker.
- Known deferred items (recorded in PR #1669 review): the Adyen adapter must narrow `adyen_refused` to `resultCode == "Refused"` behind a canary before Adyen declines release capture claims; persistent SCA (`requires_action`) sessions currently die at TTL rather than offering a card switch.

## Alternatives considered

**Infra-executes (option b):** finish the per-service wiring — set `PLATFORM_ORDERS_ACP_URL`, route `ucp.pivota.cc` paths to the UCP services, move pivota-acp to europe-west4, monitor the credentialed box. Rejected: it institutionalizes a second policy surface, keeps four services plus a region straggler alive for zero current traffic, and the serving-policy history shows the drift cost gets paid repeatedly.

## Correction (2026-08-21) — two factual premises in this ADR are false

Recorded, not rewritten: the decision stands, but two of the facts cited for it do not, and this ADR
is quoted as authority whenever someone deletes or revives a UCP service.

Measured read-only against prod `Postgres-xMr6` on 2026-08-21, after the three `ucp-*` services were
deleted:

| claim in this ADR | measured |
|---|---|
| "`ucp_checkout_sessions` and `ucp_order_webhook_deliveries` held zero rows" | **56** and **22** rows |
| "zero deliveries ever occurred" | **22 occurred**, 2026-01-14 → 2026-01-16, all `status='sent'`, `attempt_count=1`, `last_error` null, `sent_at` set |

`ucp_checkout_sessions` spans 2026-01-14 → 2026-05-27 (28 `incomplete`, 26 `completed`,
2 `requires_escalation`). The 22 deliveries went to `ucp-platform-receiver` (15),
`ucp-web-production` (4) and three `*.trycloudflare.com` dev tunnels (3) — so **`ucp-platform-receiver`
did serve real requests**, and something answered at the `ucp-web-production` host. Both facts
contradict "never booted" and "0 real requests" as those phrases were used elsewhere.

**What does not change:** every delivery completed, and no row is `pending`, `failed`, or otherwise
retryable, so retiring the worker stranded nothing. Merchant ids are `merch_ucp_smoke`,
`merch_ucp_smoke_local`, `merch_smoke_checkout`, `merch_acceptance`, and one hex id
(`merch_efbc46b4619cfbdf`, 35 sessions) that does not resolve to a `merchants` row — `merchants.id`
is an integer, so these are UCP-scoped identifiers, not customer accounts.

**Why this matters going forward:** the retirement was justified partly by "nothing ever flowed
through here." Something did, in January 2026. The justification that survives measurement is
narrower — *the queue was fully drained before the drainer was removed*. Do not reuse the
zero-rows premise on a queue nobody has counted.
