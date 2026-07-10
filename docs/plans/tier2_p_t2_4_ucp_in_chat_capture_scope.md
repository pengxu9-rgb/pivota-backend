# P-T2.4 — UCP in-chat capture lane (scope)

Bring **UCP** (Universal Commerce Protocol) up to the same in-chat capture
capability we proved for ACP (P-T2.3.3 → P-T2.3.6), reusing the already-proven
backend money path. Tracking issue: **pivota-acp #25**. Sibling of the ACP lane
([tier2_p_t2_3_4_pivota_acp_complete_wiring.md](./tier2_p_t2_3_4_pivota_acp_complete_wiring.md)).

Verified against `main` on both repos, 2026-07-10 (backend `7d9eaa0d`, pivota-acp
`8b1bfd6`). File:line refs below are anchors for implementers to re-verify.

## TL;DR

- The **backend money path is already protocol-agnostic.** The kill-switch
  `GUARDED_PROTOCOLS` already includes `"ucp"`; `create_payment` drives the whole
  test/live off-session capture off the order's `metadata.protocol_name`, not a
  hard-coded `"acp"`. **A UCP order with `protocol_name="ucp"` flows through the
  exact same off-session Stripe capture, kill-switch, caps, and attribution — no
  backend charge-path change required.**
- The work is almost entirely in the **UCP web app** (`pivota_infra_main`), which
  today is a **hosted-escalation MVP**: its `/complete` returns
  `requires_escalation` + a `continue_url`, the buyer pays on a hosted page, and a
  `_link-order` callback attaches the Pivota order afterward. It has **no
  connector layer, no delegated payment, and no in-band payment token.**
- The one **genuinely new** design item is **buyer payment-token intake for UCP**
  (item #4). Everything else is plumbing that mirrors the proven ACP lane.

## Current state

### UCP web app — hosted-escalation MVP (redirect-shaped)

Same repo as ACP (`pengxu9-rgb/pivota-acp`) but a **separate top-level source
tree + Railway service**. Entrypoint `pivota_infra_main/ucp_web.py` (FastAPI),
served under `/ucp/v1/…` + `/.well-known/ucp`. Checkout lifecycle in
`pivota_infra_main/routes/ucp_business_proxy_routes.py`:

| Route | Line |
|---|---|
| `POST /checkout-sessions` | 1284 |
| `GET /checkout-sessions/{id}` | 1471 |
| `PUT /checkout-sessions/{id}` | 1538 |
| `POST /checkout-sessions/{id}/cancel` | 1564 |
| `POST /checkout-sessions/{id}/complete` | 1580 |
| `POST /checkout-sessions/{id}/_link-order` | 1610 |
| `POST /checkout-sessions/{id}/_mark-failure` | 1687 |

Behavior:
- `POST /checkout-sessions` (1284) creates an `incomplete` row and returns
  `status="requires_escalation"` + `continue_url` to the hosted UI
  (`_build_checkout_response`, ~1458-1468). Docstring: *"Does not take payment in
  this API call (payment happens in the hosted flow)"* (1297). Response payload
  carries `payment: {handlers: []}` (413) — **no in-band payment.**
- `POST /complete` (1580): if an `order_id` is already attached → mark
  `completed`; otherwise **re-returns `requires_escalation` + `continue_url`**
  (1602-1607). It **never charges.**
- The buyer pays in the downstream hosted UI (`ucp_checkout_ui_routes.py` `/order`
  redirects to `UCP_CHECKOUT_UI_BASE_URL`); that UI creates + charges the Pivota
  order out-of-band.
- `POST /_link-order` (1610) is the internal callback the hosted UI hits after
  payment: validates an internal secret (1615), looks up the created order (1640),
  links it to the session, flips status `completed` (1647-1658), best-effort
  delivers the platform order webhook (1671-1683).

**No connector abstraction.** Unlike ACP (`pivota_infra/src/connectors/`:
`registry.py`, `base.py::PlatformConnector`, `shopify.py`, `wix.py`, shared
`_real_capture.py::real_capture_via_backend`), the UCP app has none. Its checkout
routes contain **zero** references to `real_capture`, `/agent/v1/payments`,
`/agent/v1/orders/create`, or `protocol_name`, and hold **no** backend Agent-API
credentials today.

### Backend — already protocol-agnostic (the good news)

- `services/agent_checkout_kill_switch.py:37` — `GUARDED_PROTOCOLS =
  frozenset({"acp", "ucp", "ap2"})`. `evaluate_tier2_charge`,
  `resolve_acp_test_capture`, `resolve_acp_live_capture` all gate on
  `is_guarded_protocol(protocol)` → **a `"ucp"` order is guarded + capture-capped
  identically to ACP.**
- `routes/agent_payment_sdk.py::create_payment` reads
  `order_metadata.get("protocol_name")` and threads it generically into
  `evaluate_tier2_charge`, `resolve_acp_test_capture`, `resolve_acp_live_capture`,
  then `capture_offsession`. **Nothing is hard-coded to `"acp"`.**
- `services/platform_capabilities.py:136` defines `PROTOCOL_UCP = "ucp"`.

**Backend gaps (only matter if the *backend serving/decision* layer should
advertise UCP — not needed if the UCP web app drives capture directly):**
1. `_PLATFORM_PROTOCOLS` (`platform_capabilities.py:148`) lists only Shopify+ACP —
   UCP is advertised for no platform.
2. `services/tier2_acp_lane.py::resolve_acp_lane_decision` hard-codes
   `PROTOCOL_ACP` — the decision brain only knows the ACP lane.
3. Canary flags are ACP-branded (`AGENT_ACP_TEST_CAPTURE`,
   `AGENT_ACP_ALLOW_LIVE_CAPTURE`, `AGENT_ACP_*_MERCHANTS`) but functionally
   protocol-agnostic (they gate on `is_guarded_protocol`), so they already govern
   a UCP charge as-is.

**What a UCP order must look like to reuse the money path unchanged:** created via
`/agent/v1/orders/create` with `metadata.protocol_name="ucp"` (+ optional
`pvt_click_id`/`pvt_surface`/`checkout_session_id`), then `/agent/v1/payments`
with `payment_method` (`{"type":"card","token":<pm_>}` live, omit token for the
test lane). This is exactly `_real_capture.real_capture_via_backend` with `"acp"`
→ `"ucp"` at `_real_capture.py:89`.

### Deployment reality

Same repo, different Dockerfiles / Railway services:

| App | Build | Start | Serves |
|---|---|---|---|
| **ACP** | root `railway.json` → `./Dockerfile` | `src.main:app` | `/checkout_sessions/…` (`pivota-acp-production.up.railway.app`) |
| **UCP web** | root `railway.toml` → `Dockerfile.ucp-web` | `uvicorn ucp_web:app --app-dir ./pivota_infra_main` | `/ucp/v1/*`, `/.well-known/ucp` |
| **Backend (charges)** | repo `pivota-backend` | — | `/agent/v1/*`, `create_payment` |

**Import blocker:** `Dockerfile.ucp-web` copies only `pivota_infra_main` (+`scripts`)
and sets `PYTHONPATH=/app/pivota_infra_main:/app` — it does **not** copy
`pivota_infra/`. So the UCP image **cannot `import` `_real_capture` as-is.** Also
present: `Dockerfile.ucp-worker`, `Dockerfile.ucp-platform-receiver` (UCP-family
webhook worker + receiver). `pivota_infra_main/railway.json` is a stale third
config, unused.

## The one hard problem: buyer payment-token intake

ACP carries `payment_data.token` (a buyer `pm_` or a `vt_` delegate token) into
`/complete`, and has a real `POST /agentic_commerce/delegate_payment` route
(`pivota_infra/src/acp/router.py:535`). **UCP has neither** — `payment.handlers`
is always empty. So:

- **Test lane is reachable now** (backend uses its test PM when no token is
  forwarded) → we can prove the UCP money path end-to-end in test mode without
  solving token intake.
- **Live money needs a UCP buyer-token model.** Options to decide with the
  founder (see Open questions): (a) mirror ACP — add a `payment_data.token` field
  to the UCP `/complete` contract + a UCP delegate-payment route; (b) keep UCP
  hosted for the payment leg but capture through the backend (hybrid); (c) adopt
  whatever token mechanism the UCP spec/partner mandates. This is the only item
  that can't be copied wholesale from ACP.

## Work breakdown (dependency-ordered)

1. **[backend, tiny] Parameterize `protocol_name`** in
   `real_capture_via_backend` (`pivota_infra/src/connectors/_real_capture.py:89`,
   currently hard-coded `"acp"`) so a caller can emit `"ucp"`. Backend charge path
   already accepts it; kill-switch already guards it — **no other backend change
   required for capture.**
2. **[UCP build] Make the shared money path reachable in the UCP image** — either
   add `pivota_infra` to `Dockerfile.ucp-web` COPY + PYTHONPATH, or lift
   `_real_capture` into a shared importable module, or reimplement the 3 backend
   calls UCP-side. Give the UCP service `PIVOTA_BACKEND_BASE_URL` +
   `PIVOTA_AGENT_API_KEY` env.
3. **[UCP] Add `UCP_ENABLE_REAL_CAPTURE` flag** (mirror `ACP_ENABLE_REAL_CAPTURE`
   at `_real_capture.py:21`), default **OFF** — hosted-escalation stays the
   default so nothing live breaks.
4. **[UCP] Define buyer-token intake** — the genuinely new design work (see above).
   Test-lane can proceed without this; live cannot.
5. **[UCP] Rewrite `/complete`** (`ucp_business_proxy_routes.py:1580`) to, when the
   flag is on (+ token present for live), drive quote→order(`protocol_name="ucp"`)
   →pay inline and reuse the existing `_link-order` linking/webhook logic
   (1647-1683), instead of returning `requires_escalation`. Keep hosted-escalation
   as the default fallback.
6. **[backend, optional] Advertise UCP in the serving/decision layer** — add UCP
   to `_PLATFORM_PROTOCOLS` (`platform_capabilities.py:148`) and generalize
   `tier2_acp_lane.resolve_acp_lane_decision` (or add a parallel UCP lane). **Only
   needed if the backend should route/offer UCP** — not needed if the UCP web app
   drives capture directly (the pivota-acp model).
7. **[ops] Canary flip** — reuse the protocol-agnostic
   `AGENT_ACP_TEST_CAPTURE` / `SUBMIT_PAYMENT_MERCHANTS` to gate a single-merchant
   UCP **test**-capture, then `AGENT_ACP_ALLOW_LIVE_CAPTURE` + live allowlist for
   live money — exactly as ACP P-T2.3.5. (Consider UCP-scoped flag aliases later
   for clarity; functionally unnecessary.)

**Recommended phasing** (mirrors ACP): items 1–3 + 5 (test-lane only) →
**UCP test-mode canary** proving the money path → item 4 (buyer token) →
**UCP live canary** → item 6 if/when the backend serving layer should advertise
UCP.

## Open questions (founder)

1. **UCP buyer-payment model** — do we add an in-band token to the UCP contract
   (mirror ACP delegate-payment), keep the payment leg hosted (hybrid capture), or
   follow a specific UCP-spec/partner token mechanism? Gates all live UCP money.
2. **Who drives the UCP lane** — the UCP web app calling the backend directly
   (pivota-acp model, minimal backend change), or the backend serving/decision
   layer advertising + routing UCP (item 6)? Recommend the former for v1 (matches
   ACP, smaller surface).
3. **Reuse vs. reimplement `_real_capture`** — ship `pivota_infra` into the UCP
   image, lift the flow to a shared module, or reimplement 3 calls. Recommend
   lifting to a shared module (one money path, no cross-tree Docker coupling).

## Non-negotiables (inherit from ACP)

- Default **OFF** / dark; fail-closed kill-switch governs every charge.
- Per-merchant canary allowlist; test lane refuses live keys and vice-versa.
- Attribution parity (`protocol_name="ucp"` + `pvt_click_id` → attributed edge).
- Merchant-of-record + post-hoc GMV; no money-topology change.
