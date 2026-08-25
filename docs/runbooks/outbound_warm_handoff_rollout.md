# Warm-handoff click lane — rollout constraints

Operational constraints for `OUTBOUND_WARM_HANDOFF_*`
(`services/outbound_warm_handoff.py`, `routes/outbound_links.py::redirect_endpoint`,
flags in `config/settings.py`). Spec: `Pivota_Warm_Handoff_Click_Lane_Spec_2026-07-22.md`
(not in this repo).

Read this **before widening `OUTBOUND_WARM_HANDOFF_BRANDS`**, before raising
`OUTBOUND_WARM_HANDOFF_ROLLOUT_PCT`, and before changing `evaluate_warm_eligibility`.

## State as of 2026-08-24 (verified, not assumed)

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

  **Know where that guarantee actually lives — it MOVED.** This paragraph used to say that
  nothing in *this* repo enforced it, and that was true when written: eligibility had no
  knockout for a `dest` that was already a cart, and `_validate_continue_url` checked only
  scheme + host, so the invariant rested entirely on the gateway choosing to return only
  `create_cart` output (`PIVOTA-Agent` `src/services/ucpWarmHandoff.js`). A change on THAT
  side could have falsified a `true` here with nothing local failing.
  **Constraint 5 closed that.** The knockout now refuses an already-a-cart dest (on the
  signed `join_mode` OR the dest path shape), and `_validate_continue_url` requires the 302
  target to be a cart/checkout. The guarantee is enforced locally; the gateway's behaviour is
  no longer the only thing holding it up.
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
## Constraint 5 — an ALREADY-PREFILLED cart must never be re-resolved (FIXED 2026-08-24)

### The defect

`evaluate_warm_eligibility` had **no knockout for a `dest` that is already a cart
permalink**. `_make_external_redirect_url` mints two kinds of `dest`:

- `join_mode == "cart_permalink"` — a Shopify cart permalink,
  `.../cart/{variant}:{qty}?...&attributes[pivota_click_id]=...`
- `join_mode == "referral_only"` — a bare PDP

The lane exists to upgrade the *second* kind. It fired on **both**. Verified by running the
predicate: a `dest` of `https://brand.com/cart/51895645012184:1?...` on an allowlisted brand
returned `(True, 'allowlisted')`.

On exactly that population the gateway was called **with no product identity at all**:

- `extract_product_handle` matches `/products/([...])`, which a `/cart/...` path cannot
  satisfy → no `product_handle` in the payload.
- The variant hint read `ctx["shopify_variant_id"]` — **a key nothing has ever written.**
  Confirmed two ways: a grep over `routes/` + `services/` finds exactly one occurrence (the
  read itself, no writer), and minting a real token through `_make_external_redirect_url`
  yields ctx keys `join_mode, pvt_click_id, pvt_surface, tool` only.

So the payload was `brand_domain` plus an unparseable cart URL, and nothing else.

Third, `_validate_continue_url` checked only **scheme + host**. Verified by running it with
`brand_host="brand.com"`: it accepted `https://brand.com/products/other`, `https://brand.com/`
and `https://brand.com/404-not-found`.

**Consequence.** A correct prefilled cart — right variant, carrying the
`attributes[pivota_click_id]` order-side attribution join that `_make_external_redirect_url`
deliberately appends — could be replaced at click time by whatever the gateway returned from
a request naming no product: plausibly a different product, or a non-cart page. The
click-id join was discarded with it, breaking order-side attribution for those clicks.

This was **live**, not latent: the flag is `true` on the serving revision with six brands
allowlisted. It predates PR #1845 and was independent of it.

### The fix

**(a) Skip the warm lane when the dest is already a cart. — CHOSEN, primary.**

`evaluate_warm_eligibility` takes the signed token's `ctx` and knocks out with reason
**`already_cart`** on **either of two independent signals, OR-ed**. Adversarial review
established that neither alone is sufficient:

- **`join_mode == "cart_permalink"`** — mint-time truth, decided once by
  `resolve_cart_permalink` and riding inside the HMAC, so a click cannot forge it and no new
  data goes on the wire. It is shape-independent, so it still catches a cart URL whose path
  uses a word the segment set below does not know.
  **But it records "we BUILT a cart", not "dest IS a cart."**
- **the dest PATH shape** — catches what `join_mode` cannot. A `destination_url` that was
  *already* a cart while no variant id could be recovered mints **`referral_only`** over a
  cart dest, and `join_mode` alone waves it straight through — reopening the exact defect
  this constraint exists to close. Four other minters make that worse:
  `services/outbound_links_service.py` and `routes/employee_products.py` omit `join_mode`
  entirely, while `routes/agent_api.py` and `routes/agent_sdk_fixed.py` **hardcode
  `"referral_only"` regardless of dest** — and all of them reach this same public `/r` route.

  Measured on prod 2026-08-24: **ZERO of 11,352 active seeds and ZERO of 4,200
  `outbound_link_rules`** carry a cart-shaped destination, so this arm is **latent today**.
  It is here because it costs nothing (a `/products/...` PDP has no cart segment, so it can
  only fire on something already a cart) and because those four minters mean one future
  writer of a cart-shaped `destination_url` would silently reopen the hole.

**Knockout ORDER: last, not first.** It runs after the affiliate, bot and allowlist gates so
that `warm_reason=already_cart` counts **only clicks that would otherwise have been warmed**.
Ordered first it also swallowed bot/prefetch traffic (heavy on `/r`, and an absent UA counts
as a bot), every non-allowlisted brand, and affiliate links — none of which were ever at
risk — which would have inflated the rollout dial below by roughly an order of magnitude and
left it flat when the allowlist widens. Behaviour is identical either way; all of those
paths are ineligible regardless.

`ctx` is a **required** parameter with no default. A defaulted one would make omission
silent — a new call site would simply stop knocking out prefilled carts with nothing
failing. Required means a `TypeError` the suite catches. (This is the same reasoning as
`_make_external_redirect_url`'s `cart_variant_id`.)

`ctx` is a **required** parameter with no default. A defaulted one would make omission
silent — a new call site would simply stop knocking out prefilled carts with nothing
failing. Required means a `TypeError` the suite catches. (This is the same reasoning as
`_make_external_redirect_url`'s `cart_variant_id`.)

**(b) Require a cart-shaped path in `_validate_continue_url`. — ADOPTED, but WIDENED first.**

**Taken literally, (b) alone would have broken the legitimate PDP-upgrade path.** The
gateway's `continue_url` comes from PIVOTA-Agent `extractHandoffUrl`
(`src/services/ucpBuyerAgentClient.js`), which returns the merchant's UCP
`continue_url || checkout_url || permalink || url`. Both `/cart/c/<token>` and
`/checkouts/<token>` appear as literals in the gateway's own tests, so a `/cart`-only rule
would refuse anything arriving on the `checkout_url` leg of that chain. (Precisely: the
`/checkouts/...` shape comes from `create_checkout`, which the internal warm-handoff route
does not forward today — so allowing it is right because the fallback chain **can** yield
it, not because storefronts answer that way on this endpoint right now.)

So the guard allows the segments `cart`, `carts`, `checkout`, `checkouts`, matched on whole
lowercased segments in **any** position (a locale-prefixed `/en/cart/c/abc` passes), and the
matching segment **must be followed by at least one more segment**.

Both rules earn their keep:

- *Equality, not substring*, is what makes any-position matching safe: `/products/cart-organizer`
  has no segment equal to `cart`.
- *Must be followed by something* is what separates a PREFILLED cart from the storefront's
  EMPTY one. A prefilled cart always names what is in it (`/cart/c/<token>`,
  `/cart/{variant}:{qty}`). Without this rule the guard accepted bare `/cart` and
  `/checkout`, plus `/products/cart`, `/pages/cart` and `/blogs/news/cart` — and 302-ing a
  shopper off a correct PDP onto an empty cart page is a **wrong landing, not merely a
  missed upgrade**.

This is defence-in-depth, not the primary fix, and it is **safe by construction**: failing it
returns `None`, and the caller falls back to the cold `dest` — the correct product page.

**The guard is the ONLY filter that exists.** The gateway does no validation whatsoever —
`extractHandoffUrl` is a verbatim passthrough with no scheme, host, or path check. Note
`permalink`, third in that chain, is by convention a PRODUCT-PAGE field in WooCommerce/WP
REST, so a merchant whose `create_cart` echoes a product object would hand us a PDP labelled
as a cart. This guard correctly refuses that.

**KNOWN NARROW, deliberately.** These fall back to the cold PDP: Wix `/cart-page`,
BigCommerce `/cart.php`, Salesforce `Cart-Show`, `/basket`, `/panier`, `/warenkorb`,
`/carrito`, `%2F`-encoded paths, and query/fragment-only carts. All are currently
unreachable — the gateway's internal route requires a `gid://shopify/ProductVariant/<n>`, so
a non-Shopify merchant fails earlier at `variant_unresolved`. **This becomes live the moment
variant resolution is generalized past Shopify.** Widen the set from OBSERVED gateway
responses then — never speculatively, since every addition widens what we will redirect a
shopper to.

**(c) The dead `ctx["shopify_variant_id"]` read — DELETED, not wired up.**

Wiring it up is **forbidden**. `_make_external_redirect_url` carries the recovered numeric
Shopify variant id on `cart_variant_id`, a channel only the cart-permalink construction
reads, and the round-4 review of #1813 established that stamping it into the token ctx leaks
it up a grain: `commerce_attribution_service` cross-fills product↔variant ids **both ways**,
so a numeric variant id in ctx lands in `surface_click_events.canonical_product_id`.

The read was therefore never a wiring gap. It was a branch that could not fire, which read as
though variant identity were being passed when it never was. Removed.

The test that covered it (`test_resolve_warm_handoff_passes_variant_hint_and_attribution`)
**passed while asserting behaviour production never exercised** — its fixture hand-built the
ctx key that no producer writes, manufacturing the very evidence it checked. It is now a
knockout in the opposite direction, and the real invariant (no producer stamps that key) is
pinned against the actual builder in
`test_real_mint_stamps_cart_permalink_and_never_a_variant_id`.

### How this was unioned with PR #1845 (DONE — recorded because the trap is subtle)

#1845 (`fix(offers.resolve): stop claiming cart_prefilled=false…`) merged to `main` as
`55005658` while this change was in review, so the two were unioned here. Recorded because
**the dangerous part of that conflict is the part git does NOT flag.**

`git merge-tree` reported three conflicts: this runbook (add/add), a block of tests, and in
`services/outbound_warm_handoff.py` **one hunk that was the docstring only**.
`routes/outbound_links.py` auto-merged silently, and — the trap — the
`evaluate_warm_eligibility` signature and body auto-merged *perfectly*: `ctx`,
`assume_human`, `already_cart` and the bot gate all landed in the right order.

So resolving what presented as a **prose** conflict yielded a file that imported cleanly
while `could_upgrade_at_click_time` still called `evaluate_warm_eligibility` **without
`ctx`**. That state was built and run: **12 failures**,
`TypeError: … missing 1 required keyword-only argument: 'ctx'`. Had it escaped CI,
`_cart_prefilled_claim` is called inline in the external-offer loop of `_handle_offers_resolve`
with no enclosing `try`, so the `TypeError` propagates and **every `offers.resolve` returning
an external offer 500s** — the live agent-facing lane.

The four changes applied:

1. `evaluate_warm_eligibility` docstring — kept **both** paragraphs; they document different
   parameters and do not contradict.
2. `could_upgrade_at_click_time` — threaded `ctx` through, keeping the no-default discipline,
   and passed it to the inner `evaluate_warm_eligibility`.
3. `routes/agent_shop_gateway.py` — the single call site passes
   `ctx={"join_mode": "referral_only"}`. **Exact, not conservative**: that line is unreachable
   unless `if cart_url: return True` fell through, and the mint stamps `join_mode` from the
   same `cart_url` decision, so the knockout provably cannot fire there.
4. #1845's own tests — `ctx={}` on its 9 direct calls.
   `test_evaluate_warm_eligibility_requires_ctx` was deliberately left alone: it omits `ctx`
   on purpose.

Also rewritten, because this change made them false: the `_cart_prefilled_claim` docstring
and Constraint 1's "Know where that guarantee actually lives" both stated the guard did not
exist in this repo.

**`could_upgrade_at_click_time` remains SOUND.** Its contract is that `False` guarantees the
click path is also ineligible. `already_cart` only ever *removes* eligibility, so it cannot
turn a resolve-time `False` into a click-time `True`. On the leg where it is actually
reached, `cart_url` is falsy ⟹ `join_mode` is provably `referral_only` ⟹ the knockout
provably cannot fire.

### Exposure — MEASURED on prod, 2026-08-24

**34 of 416 active external-seed rows (8.2%)** on the six allowlisted domains mint
`join_mode == "cart_permalink"`. Those are the affected clicks — every one of them a
*correct* prefilled cart that the lane would re-resolve from a request naming no product.

| Brand | `cart_permalink` (affected) | `referral_only` |
|---|---:|---:|
| cosrx.com | **21** | 127 |
| skin1004.com | **7** | 87 |
| beautyofjoseon.com | **3** | 113 |
| medicube.us | **3** | 16 |
| anua.us | 0 | 35 |
| mixsoon.us | 0 | 4 |
| **total** | **34** | **382** |

Method: replayed the **real** `_external_seed_redirect_identity` + `resolve_cart_permalink`
over `external_product_seeds WHERE status='active'` on those six domains — not a SQL
re-expression of the predicate. That matters: the predicate is "exactly one snapshot variant
entry, stamped numeric" (`sole_stamped_variant_id`) **or** a writer-verified `shopify`
attachment with a numeric `attached_variant_id`, and a SQL approximation of it would be a
second implementation of exactly the logic under test.

Two readings of this number, both worth keeping:

- **It is small in share but not small in kind.** 8.2% is a minority of rows, but each one is
  a cart-capable offer — the *most* commercially valuable links we mint, and the only ones
  carrying the order-side attribution join. The rows we damage are the good rows.
- **It is concentrated.** 21 of the 34 are `cosrx.com`. A brand-by-brand rollout would not
  have surfaced this evenly, and two of the six brands (`anua.us`, `mixsoon.us`) cannot
  exhibit it at all today — so a canary on those two would have shown nothing wrong.

This is a row count, not a click count; clicks are distributed over these rows by demand, so
the click-weighted share could differ in either direction. `warm_reason=already_cart` is the
live click-weighted dial (see the checklist).

## Rollout checklist (additions from Constraint 5)

- [ ] Widening `OUTBOUND_WARM_HANDOFF_BRANDS` now also widens the `already_cart` knockout —
      that is correct and needs no action, but expect `warm_reason=already_cart` to appear
      for the new brands.
- [ ] Watch `warm_reason=already_cart` on click-event ctx. It is the dial for this
      constraint: **it is the count of clicks that would previously have had a correct cart
      rebuilt from a request naming no product.** That reading is only valid BECAUSE the
      knockout runs last — it fires only on clicks that passed the bot, affiliate and
      allowlist gates. If anyone reorders it above those, this dial silently becomes a
      population counter (every cart click lane-wide, bots and non-allowlisted brands
      included) and this sentence stops being true. It is the click-weighted version of the
      8.2% row-share measured above; expect it concentrated on `cosrx.com`.
- [ ] If warm-handoff success drops after this change, check for `continue_url` shapes
      refused by the cart/checkout path guard before assuming a gateway fault — the guard
      fails closed to the cold PDP. Distinguish a guard that is too narrow from a gateway
      extraction bug: a refused path carrying a `products`/`product` segment means
      `extractHandoffUrl` fell through to `permalink` and handed us a PDP, which is the
      gateway's problem, not the guard's.
- [ ] Rollback is immediate and needs no cache drain: `_memo` is a plain in-process
      `OrderedDict`, so it dies with the revision and cannot carry entries across a deploy.
      While the guard is live, note a refused `continue_url` memoizes as a MISS for 600s, so
      one bad-shaped answer costs that token ~10 minutes — relevant when reading a dip.
