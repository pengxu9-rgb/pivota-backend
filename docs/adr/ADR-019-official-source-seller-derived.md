# ADR-019 — `official_source` is derived from seller identity, not from a URL comparison

**Status:** Accepted (implementation ships behind `OFFICIAL_SOURCE_SELLER_DERIVED`, default OFF)
**Date:** 2026-07-27
**Supersedes the derivation in:** `services/pivot_query_service._build_canonical_offer_node`
**Related:** ADR-009 (seller-of-record identity), Fix Plan C (`services/offer_seller_identity.py`)

## Context

`OfferNode.official_source` is the authenticity signal an agent sees. Its contract
(`models/catalog.py:282-288`) is:

> True when the offer is served from the **BRAND'S OWN official domain** … A retailer or
> marketplace mirror is **NOT** `official_source`. Lets the decision-grade `trust` dimension
> pass on official-brand seeds that are correctly not `is_first_party`.

Two lanes compute it, and they disagree.

**Lane A** — `services/pivot_query_service.py:637`, the canonical catalog offer:

```python
official_source = bool(is_first_party) or _is_official_brand_source(
    row.get("offer_source_domain") or row.get("source_domain"),
    row.get("canonical_url"),
)
```

`_is_official_brand_source` (`:549`) is true when the two registrable hosts are equal.

**Lane B** — `services/pivot_query_service.py:1749`, the external-seed candidate:

```python
official_source=bool(seller_identity["is_first_party"]),
```

where `seller_identity` comes from `derive_offer_seller_identity`
(`services/offer_seller_identity.py:159`), whose precedence is: known-retailer host preempts →
official-domain match → brand token in domain → **unknown, do not guess**.

### The defect

For an external-seed mirror row, `catalog_products.canonical_url` is written from the *same seed
record* that `catalog_offers.source_domain` is written from
(`scripts/mirror_external_seeds_to_catalog_products.py:1129,1268`). So lane A's comparison asks
whether a value equals itself. **It is a tautology, not a signal.**

This is not hypothetical and not merely latent. Measured on prod 2026-07-27, over
`catalog_track='external_referral'`, unsuppressed, `is_first_party=FALSE`, `source_domain`
populated:

| stored `offer_type` | rows | `official_source` **wrongly true today** |
|---|---:|---:|
| `retailer` | 480 | **480** |
| `NULL` (derivation declined to guess) | 2,166 | **2,166** |
| **total** | **2,646** | **2,646 (100%)** |

A 100% hit rate is the proof: a real signal does not fire on every row. We are currently telling
agents that 480 offers the seller-identity derivation explicitly classified as **retailer** are
served from the brand's own official domain, and manufacturing a positive authenticity claim for
2,166 more where the derivation deliberately returned "unknown".

### The cohort the field was built for does not exist

The contract justifies the disjunct by pointing at "official-brand seeds that are correctly not
`is_first_party`". Measured on prod:

```
offer_type='brand_direct' AND is_first_party=FALSE   ->  0 rows
catalog_track='internal_merchant' AND is_first_party=FALSE  ->  0 rows
```

Zero, and structurally so: `derive_offer_seller_identity` returns `brand_direct` and
`is_first_party=True` **together**, from the same rules. There is no path that produces one
without the other. So the disjunct has no legitimate live consumer on either track — its entire
production effect is the 2,646 false positives above.

### Why this blocks the `source_domain` blind spot fix

`services/external_offer_dual_write.py` already has the seed's domain in hand and discards it
instead of writing `catalog_offers.source_domain`. That is why 4,718 of 18,809 live offers carry
no `source_domain`, of which 2,727 are unreachable even through the attached-seed fallback and so
cannot be currency-audited at all (worklist P2).

Populating that column is a two-line change. But under lane A's current derivation it would
extend the false claim to a further ~3,414 offers. **The blind spot is the only thing currently
holding this signal up.** Repairing the audit gap would ship a provenance lie — so the semantics
have to be settled first. That ordering is the whole point of this ADR.

## Decision

**`official_source` is derived from stored seller identity, never from a `source_domain` ↔
`canonical_url` comparison.**

```python
official_source = bool(is_first_party)
```

This makes lane A agree with lane B, and makes the signal mean what
`derive_offer_seller_identity` determined at write time — a rule that compares the offer domain
against the **declared brand** (and against a known-retailer list that preempts everything), not
against a URL derived from the same record.

`_is_official_brand_source` is retained but no longer consulted on the offer path, because the
comparison is still meaningful for any *future* caller that has two independently-sourced values.
The lesson it encodes is worth keeping visible: the function is not wrong, its **inputs** were.

### Options considered

**(1) Keep the comparison, exclude `external_referral`.** Rejected. It leaves a disjunct whose
only live cohort is empty, so it is dead code that reads as load-bearing, and the next person to
add a track has to rediscover why. It also leaves the internal_merchant path resting on an
accident (0 rows with `is_first_party=FALSE` today), not on a rule.

**(2) Compare `source_domain` against a genuine brand-official domain.** This is the "right"
answer in the abstract, and it is exactly what `derive_offer_seller_identity` rule 1 already does
via `official_domain`. Adding a *second* implementation on the read path would give us two rules
that can drift — precisely the failure ADR-009 and Fix Plan C exist to prevent. Rejected in
favour of consuming the stored result.

**(3) Chosen: `official_source = is_first_party`.** One rule, one place, computed at write time,
already mirrored in Node (`PIVOTA-Agent/src/services/offerSellerIdentity.js`).

### What breaks, per consumer

Three consumers read the field. Under the change, `official_source` goes **true → false** for the
2,646 rows above, and for nothing else.

| consumer | today | after | assessment |
|---|---|---|---|
| `services/decision_grade_eval.py:145` — `_score_trust` | `first_party or authenticity` passes for those 2,646 | `trust` dimension fails for them | **Correct.** They were passing on a fabricated signal. Expect the decision-grade trust rate to drop; that drop is the measurement becoming honest, not a regression. Re-baseline rather than "fix". |
| `services/behavioral_eval.py:72` — `_fmt_offer` | prints "official brand source" | omits the phrase | **Correct.** That phrase in agent-facing text about a `ulta.com` offer is the actual harm this ADR removes. |
| `models/catalog.py:288` — public `OfferNode.official_source` | `true` | `false` | Public contract field. Value changes, type and name do not. |

Nothing gates checkout, pricing, or ranking on `official_source` — verified by inspection; it is a
display/trust signal only. So the blast radius is the trust dimension and agent-facing prose.

## Consequences

- Once this flag is ON, `catalog_offers.source_domain` becomes safe to populate at ingest, which
  unblocks the currency-audit blind spot (P2) — 2,727 offers currently unauditable.
- The decision-grade `trust` baseline must be re-measured after the flip. A lower number is the
  expected, correct outcome.
- `official_source` and `is_first_party` become equal on every live row. If a genuine
  "official brand, not first party" case ever appears, it must be introduced in
  `derive_offer_seller_identity` — one rule, one place — not by reviving a URL comparison on the
  read path.

## Rollout

1. Ship behind `OFFICIAL_SOURCE_SELLER_DERIVED`, **default OFF** — byte-identical behaviour off.
2. Flip on staging; confirm the 2,646 rows lose `official_source` and no `brand_direct` row does.
3. Re-baseline decision-grade `trust`.
4. Flip in prod, then land the `source_domain` ingest write (P2) and close the audit blind spot.
5. Once the flag has been on long enough to trust, delete the flag and the disjunct.

**The OFF state is a known-false signal, not a safe default.** It is off only so the flip is a
separate, observable step from the code change.
