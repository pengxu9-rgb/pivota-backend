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

## Constraint 1 — the click lane can contradict `cart_prefilled: false` (HANDLED)

`offers.resolve` returns an execution-spec field `cart_prefilled` on external offers
(`routes/agent_shop_gateway.py::_append_external_offers_from_seed_rows`, PR #1822). It is
**resolve-time** truth: `true` = the signed token's `dest` is a Shopify cart permalink,
`false` = `dest` is a bare PDP. It is computed by the same `resolve_cart_permalink` the
redirect builder uses, so it cannot drift *from the link*. It can, however, be contradicted
*by this lane*.

`evaluate_warm_eligibility` knocks out, in order: a missing internal key (`no_internal_key`),
an unparseable destination host (`no_dest_host`), affiliate/redirector hosts (`affiliate`),
bot and prefetch user-agents (`bot`), brands outside a non-empty allowlist
(`not_allowlisted`), and tokens outside the percentage bucket (`control`). It never asks
whether `dest` is already a cart. Its effective target population is therefore precisely the
cold PDPs, i.e. exactly the offers we reported as `false`.

The exposure is **one-sided**:

- `cart_prefilled: true` — the lane can only ever *build* a cart, so the claim cannot be
  falsified. No action needed.

  **Know where that guarantee actually lives.** Nothing in *this* repo enforces it:
  `evaluate_warm_eligibility` has no knockout for a `dest` that is already a cart permalink,
  so a `true` offer on an allowlisted host is warm-eligible and will attempt an upgrade, and
  `_validate_continue_url` checks only scheme + host — it would accept a brand homepage or
  PDP as the 302 target. The invariant holds because the gateway builds `continue_url` from a
  UCP `create_cart` response and returns nothing else (`PIVOTA-Agent`
  `src/services/ucpWarmHandoff.js`: cart-build + return continue_url ONLY). A change on THAT
  side could falsify a `true` here with nothing in this repo failing.
- `cart_prefilled: false` — we told the agent "this link lands on a product page, the buyer
  must pick the variant themselves". The agent plans that flow and says so. The buyer lands
  in a prefilled cart. **The answer was already sent and cannot be corrected.**

Why `false` specifically matters: the paired gateway PR (PIVOTA-Agent #2082) made the field a
**tri-state** (`true` / `false` / `null`) because an explicit `false` is itself a positive
claim to a buyer about where they will land, and only an explicit backend `false` licenses
saying it. A `false` that click-time behaviour contradicts is that same defect one layer down.

**Status: FIXED in code** (see "The fix" below). `cart_prefilled` no
longer emits a `false` the warm lane could contradict — it emits `null` there instead.

The window this closed: `cart_prefilled` (#1822) was merged to `main` but **not** on the
serving revision as of 2026-08-22 (prod was one commit behind, at `b5490615`), while the warm
canary was already on. It would have needed no flag flip to go wrong — only the next deploy,
for any external offer whose destination host is one of the six allowlisted brands.

*Unquantified here:* how many external-seed offers on those six domains resolve to
`cart_prefilled: false`. Answering it needs SQL over the seed corpus by domain (prod
Postgres was not reachable from this session). The upper bound is "all cold rows on those
six hosts"; the card-rail audit's finding that a numeric Shopify variant id exists for only
~28% of rows suggests the cold share on Shopify brands is the majority, not the tail.

## Constraint 4 — `execution_spec.rail` has the same defect, and is NOT fixed

PR #1846 shipped a fuller `execution_spec` beside `cart_prefilled`, and one of its fields
carries the identical exposure:

```python
"rail": "shopify_cart" if composed_spec["cart_url"] else "referral",
```

`rail: "referral"` is emitted on exactly the cold population the warm lane targets, so an
agent that hands the buyer off via `affiliate_url` can be told "referral" and have the buyer
land in a Shopify cart — the same already-sent claim, contradicted the same way.

It is deliberately left alone. `rail` is a two-value string vocabulary the gateway consumes;
adding a third state (or a null) is a **contract change across two repos**, not a bug fix,
and doing it silently inside a fix for a different field is how consumers break. Fixing it
means agreeing the vocabulary with the gateway first.

Narrower scope than `cart_prefilled`, worth knowing: `execution_spec.pdp_url` and `cart_url`
are direct URLs that bypass `/r` entirely, so an agent that follows *those* never meets the
warm lane. Only the `affiliate_url` path is exposed. `rail` is ambiguous about which it
describes, which is part of why it needs a decision rather than a patch.

## Constraint 2 — a widening has a ~7-day tail of already-issued claims

Redirect tokens are minted with a **7-day TTL** (`make_redirect_token`,
`services/outbound_links_service.py:247`), and eligibility is evaluated at *click* time
against *current* env. So widening the allowlist (or raising the rollout pct) retroactively
falsifies `cart_prefilled: false` answers that were sent up to seven days earlier and are
still being clicked. A widening is not a clean cutover; budget the tail.

(Expired tokens are exempt — `redirect_endpoint` returns the cold 302 before the warm lane
for `is_expired`.)

## Constraint 3 — an empty allowlist at a full rollout deletes the `false` state

`OUTBOUND_WARM_HANDOFF_BRANDS` empty means the percentage rollout decides. At
`OUTBOUND_WARM_HANDOFF_ROLLOUT_PCT=100` every non-affiliate host with a parseable dest is
warm-eligible, so `cart_prefilled` degenerates to `true`/`null` and **the `false` state
disappears from the API globally** — no code change, no deploy, no alarm. That is correct
behaviour (we genuinely could not promise a PDP landing any more), but it silently removes an
execution-spec signal agents plan on. Widen the allowlist brand by brand rather than emptying
it, or accept the loss deliberately.

## Rollout checklist

Before flipping `OUTBOUND_WARM_HANDOFF_ENABLED`, adding domains to
`OUTBOUND_WARM_HANDOFF_BRANDS`, or raising `OUTBOUND_WARM_HANDOFF_ROLLOUT_PCT`:

- [ ] Confirm whether the deployed revision serves `cart_prefilled` at all
      (`/health` → `commit_sha`; the field ships in #1822 / `3ea67508`).
- [x] Resolve the conflict in Constraint 1 — **done**: `_cart_prefilled_claim` consults
      `could_upgrade_at_click_time` and answers `null` where `false` is not provable. If you
      are widening the allowlist, no extra step is needed; the claim follows the config.
      **But** re-read Constraint 2 — the fix is evaluated at mint time, so it does not cover
      tokens already in flight.
- [ ] Account for the ~7-day tail of Constraint 2 in the canary window.
- [ ] If you are reaching for an empty allowlist + a high rollout pct, read Constraint 3
      first — that combination removes the `false` state from the API entirely.
- [ ] Watch the `handoff` / `warm_reason` pair on click-event ctx — that is the
      substitution-rate instrument, and it is also what tells you how many `false` answers
      were actually contradicted (`handoff=warm` on a cold-dest token).

## The fix (decision + implementation)

Two candidates:

**(a) `cart_prefilled` accounts for warm-handoff eligibility.**
**(b) The warm upgrade is suppressed for offers that already reported `false`.**

**Chosen: (a), in the form "downgrade the falsifiable `false` to `null`" — IMPLEMENTED.**

Where it lives:
- `services/outbound_warm_handoff.py::could_upgrade_at_click_time` — the resolve-time
  over-approximation. Calls the SAME `evaluate_warm_eligibility` the click path calls (via a
  new `assume_human` flag that waives only the user-agent knockout), so the two cannot drift.
- `routes/agent_shop_gateway.py::_cart_prefilled_claim` — the tri-state. `True` if the link
  is a cart permalink, `None` if a click could still upgrade it, `False` only when neither.
- The warm lane's own behaviour is **unchanged** — the buyer still gets the better landing.

Tests: `tests/test_offers_resolve.py` (field-level, plus an end-to-end one that follows the
minted link through `GET /r` and asserts the buyer really does reach a cart) and
`tests/test_outbound_warm_handoff_click.py` (the soundness matrix). The mutation sweep run when this
shipped (no guard, blanket null, fabricated token, and seven others) left no artifact beyond
those tests — re-run it rather than trusting this sentence. One result is worth carrying
forward: the fabricated-token mutant SURVIVED an outcome-only assertion, because a token
mismatch is a coin flip at a 50% rollout. Parity is therefore asserted by token identity, not
by the resulting boolean; keep it that way.

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
   internal key unset, unparseable destination host, affiliate destination (never
   warm-handed), host not allowlisted, or token outside the rollout bucket.
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

**Implemented 2026-08-22.** The remaining open item is the Constraint 2 tail: closing it
needs the stamped-claim machinery from (b), which has not been built and should not be
without a decision that the tail is worth it.
