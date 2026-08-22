# Warm-handoff click lane — rollout constraints

Operational constraints for `OUTBOUND_WARM_HANDOFF_*`
(`services/outbound_warm_handoff.py`, `routes/outbound_links.py::redirect_endpoint`,
flags in `config/settings.py`). Spec: `Pivota_Warm_Handoff_Click_Lane_Spec_2026-07-22.md`
(not in this repo).

Read this **before widening `OUTBOUND_WARM_HANDOFF_BRANDS`**, before raising
`OUTBOUND_WARM_HANDOFF_ROLLOUT_PCT`, and before changing `evaluate_warm_eligibility`.

## State as of 2026-08-22 (verified, not assumed)

| Fact | Value | How verified |
|---|---|---|
| `OUTBOUND_WARM_HANDOFF_ENABLED` | **`true`** | Cloud Run `web`, us-west1, serving revision `web-00022-yew` |
| `OUTBOUND_WARM_HANDOFF_INTERNAL_KEY` | set (Secret Manager `env-OUTBOUND_WARM_HANDOFF_INTERNAL_KEY`) | same |
| `OUTBOUND_WARM_HANDOFF_BRANDS` | `cosrx.com, beautyofjoseon.com, skin1004.com, anua.us, medicube.us, mixsoon.us` | same |
| `outbound_warm_handoff_enabled` code default | `false` | `config/settings.py` |

The **code default is `false` and the deployed value is `true`.** Reading the default and
concluding "the lane is dark" is wrong, and has already been read that way once. Check the
deployed env.

## Constraint 1 — enabling/widening makes `cart_prefilled: false` a false negative

`offers.resolve` returns an execution-spec field `cart_prefilled` on external offers
(`routes/agent_shop_gateway.py::_append_external_offers_from_seed_rows`, PR #1822). It is
**resolve-time** truth: `true` = the signed token's `dest` is a Shopify cart permalink,
`false` = `dest` is a bare PDP. It is computed by the same `resolve_cart_permalink` the
redirect builder uses, so it cannot drift *from the link*. It can, however, be contradicted
*by this lane*.

`evaluate_warm_eligibility` knocks out affiliate hosts, bot user-agents, and non-allowlisted
brands — but it never asks whether `dest` is already a cart. Its effective target population
is therefore precisely the cold PDPs, i.e. exactly the offers we reported as `false`.

The exposure is **one-sided**:

- `cart_prefilled: true` — the lane can only ever *build* a cart, so the claim cannot be
  falsified. No action needed.
- `cart_prefilled: false` — we told the agent "this link lands on a product page, the buyer
  must pick the variant themselves". The agent plans that flow and says so. The buyer lands
  in a prefilled cart. **The answer was already sent and cannot be corrected.**

Why `false` specifically matters: the paired gateway PR (PIVOTA-Agent #2082) made the field a
**tri-state** (`true` / `false` / `null`) because an explicit `false` is itself a positive
claim to a buyer about where they will land, and only an explicit backend `false` licenses
saying it. A `false` that click-time behaviour contradicts is that same defect one layer down.

**Status:** `cart_prefilled` (#1822) is merged to `main` but was **not** on the serving
revision as of 2026-08-22 (prod was one commit behind, at `b5490615`). So this does not
require a flag flip to become live — **it goes live at the next deploy**, for any external
offer whose destination host is one of the six allowlisted brands.

*Unquantified here:* how many external-seed offers on those six domains resolve to
`cart_prefilled: false`. Answering it needs SQL over the seed corpus by domain (prod
Postgres was not reachable from this session). The upper bound is "all cold rows on those
six hosts"; the card-rail audit's finding that a numeric Shopify variant id exists for only
~28% of rows suggests the cold share on Shopify brands is the majority, not the tail.

## Constraint 2 — a widening has a ~7-day tail of already-issued claims

Redirect tokens are minted with a **7-day TTL** (`make_redirect_token`,
`services/outbound_links_service.py:247`), and eligibility is evaluated at *click* time
against *current* env. So widening the allowlist (or raising the rollout pct) retroactively
falsifies `cart_prefilled: false` answers that were sent up to seven days earlier and are
still being clicked. A widening is not a clean cutover; budget the tail.

(Expired tokens are exempt — `redirect_endpoint` returns the cold 302 before the warm lane
for `is_expired`.)

## Rollout checklist

Before flipping `OUTBOUND_WARM_HANDOFF_ENABLED`, adding domains to
`OUTBOUND_WARM_HANDOFF_BRANDS`, or raising `OUTBOUND_WARM_HANDOFF_ROLLOUT_PCT`:

- [ ] Confirm whether the deployed revision serves `cart_prefilled` at all
      (`/health` → `commit_sha`; the field ships in #1822 / `3ea67508`).
- [ ] If it does, resolve the conflict in Constraint 1 **first** — either the flag accounts
      for warm eligibility, or the upgrade is suppressed for offers that reported `false`
      (see below). Shipping both as they stand means knowingly serving a false negative.
- [ ] Account for the ~7-day tail of Constraint 2 in the canary window.
- [ ] Watch the `handoff` / `warm_reason` pair on click-event ctx — that is the
      substitution-rate instrument, and it is also what tells you how many `false` answers
      were actually contradicted (`handoff=warm` on a cold-dest token).

## Preferred fix (decision — NOT yet implemented)

Two candidates:

**(a) `cart_prefilled` accounts for warm-handoff eligibility.**
**(b) The warm upgrade is suppressed for offers that already reported `false`.**

**Preferred: (a), in the form "downgrade the falsifiable `false` to `null`".**

Rationale:

1. Resolve time cannot honestly promise `true` — a warm upgrade depends on a live gateway
   call that may return `None` (timeout, off-brand `continue_url`, non-200), plus a
   click-time user-agent we do not have. So the truthful answer for a warm-eligible cold
   offer is **"unknown"**, which the gateway tri-state already represents as `null`. Emitting
   `null` costs one local check and keeps the invariant that an explicit `false` is always a
   claim we can stand behind.
2. The check is **sound and cheap**. Every eligibility input except the user-agent is
   available at mint time (dest host, flag, internal key, brand allowlist, rollout pct, and
   the token itself — `rollout_bucket` is a stable hash of the token). The one click-time-only
   input, the UA, can only ever *remove* eligibility (bots get the cold redirect). So a
   resolve-time "not eligible" implies a click-time "not eligible": a conservative
   over-approximation, never a missed case. `false` survives wherever it is safe — flag off,
   host not allowlisted / outside the rollout bucket, affiliate destination (never warm-handed).
3. **(b) inverts the priority.** It would deliberately hand the buyer the *worse* landing
   experience to protect a sentence we wrote earlier — and it caps the warm lane at nothing,
   since cold PDPs are the only population it serves. It is also not free to build: nothing
   in the signed token records "we claimed `false`", so (b) requires stamping that claim into
   the token at mint time and honouring it in the click lane — strictly more machinery than
   (a), for a worse outcome.
4. Constraint 2 argues for (a) too: (a) degrades gracefully when env changes between mint and
   click (the worst case is a stale `null`, which claims nothing), whereas (b) would have to
   honour a stamped claim that the current config no longer agrees with.

Residual gap in (a), stated honestly: a token minted while a host was *not* allowlisted
carries `false`, and if that host is added within the token's 7-day TTL the stale `false` is
still falsifiable. (a) shrinks the window to that tail; only (b)'s stamped-claim machinery
closes it entirely. Constraint 2's checklist item is the mitigation.

**Not implemented — this document records the constraint and the decision. Confirm with the
owner before changing handoff behaviour or the field's value.**
