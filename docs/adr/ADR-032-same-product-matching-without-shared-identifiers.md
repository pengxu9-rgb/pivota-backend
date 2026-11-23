# ADR-032: The same product at another seller: matching at variant grain when listings share no identifier

**Status:** Proposed (draft 5), 2026-11-23. Nothing here is built or switched on by this ADR. Acceptance waits on the owner's questions at the end. Drafts 2 and 3 answered two passes of an independent review. Draft 4 answers a further review: one storage rule shared with ADR-031 (nothing derived from a door's or a catalog's content is kept until the source allows it); the beauty-first spec and the handling of unsure labels are put to the owner instead of being stated as decided; the intake's exact GTIN attach is named as family-grain only. Draft 5 applies that storage rule to identifiers, the retrieval indexes, membership evidence, adjudication records and the gold table.

**Decision owner:** Founder. **Runtime owners:** the backend owner (identity engine, records, evaluation), the gateway owner (lane field mapping, request-time lookup).

**Companion:** ADR-031 decides how supply is read through lanes. This ADR decides when two listings, from any lanes, are the same sellable item.

**Direction already decided by the owner (2026-10-16 to 2026-10-19):**
- Identity is brand + line + formula. Size and shade are variants. Phase 1 compares the same size only.
- The backend content key and group are the system of record; the gateway follows.
- A tier may be accepted automatically only after a random audit of at least 600 accepted pairs for that tier shows no error. Until then, proposals are approved by an operator after the owner labels an audit sample.
- Agents may help: one model reads attributes from each listing, a matcher and a sceptic judge a pair, a deterministic verifier checks their evidence, and a person decides where they disagree.
- Reading the pack text from the image is acceptable in principle as a standing gate.
- A wrong merge is reverted, and the pair is not proposed again until its cause is resolved.

**Builds on and extends:**
- **ADR-010 (accepted in principle).** The tiered resolver, the two-grain model, reversibility and hysteresis, and its safety invariants, all of which stand:
  - money and attribution stay keyed on merchant-scoped ids;
  - public signature ids are write-once;
  - a cross-merchant card needs resolution at or above the automatic threshold and `pdp_scope='multi_merchant_canonical'`; mis-merge is worse than fragmentation;
  - mis-merge rate is a neutrality metric.

  This ADR writes the pieces ADR-010 named and left unbuilt: the attribute spec (Tier 1), candidate retrieval (Tier 2), adjudication (Tier 3), and the gold labels.
  - **Proposed amendment (question 4):** ADR-010's committed increment names electronics as the pilot vertical for the spec. This ADR proposes beauty first, because the owner's 2026-10-16 matching direction was given for beauty.
  - **Proposed amendment (question 4):** ADR-010 says beauty's fields come "via its lexicon graph, not `llm_attributes`". This ADR keeps the lexicon and structured options as the sources that count, and adds model-filled fields only as weak evidence: they can veto or hold, and never create agreement (M3).
  - **Amended:** ADR-010 defers Tiers 2 and 3 "until the measurement justifies them". The measurements below are that justification: retailers share no hard key, and exact titles alone leave most multi-seller brands unmatched. Tiers 2 and 3 start here in shadow and propose mode only. Neither may accept automatically under this ADR.
- **ADR-011.** Attach before mint (R1), one namespace (R2), offers stay beneath identity (R6), no existing key is re-minted (R7). M1 and M8 say how a listing pointer meets each.
- **ADR-012 (catalog convergence).** Every derived store has a reconciler and a drift metric (M12).
- **ADR-007.** The no-GTIN mis-merge gate: GTIN or resolved identity before any cross-merchant capture. Ranking stays on merit.
- **ADR-031 D1.** What may be kept from each lane. This ADR adds no storage rule of its own.
- **ADR-026.** Cross-border offers are served, ranked below domestic ones, and labelled.

## Scope

**In scope:** deciding that a seller's listing is the same sellable item as another seller's, across merchants, store platforms, languages and regions; what evidence is kept; when a decision may be automatic; how it is measured and undone.

**Out of scope:** alternatives and related products (the relationship graph); ranking; how prices are read (ADR-031).

## Context

### What exists today (backend `826e6e090`, gateway `634e794cf`)

- **Keys.** `make_content_key` (`services/catalog_identity.py`) hashes normalised brand, title and GTIN. ADR-010 calls the content key the row-level spine and the product group the family. Two formulas are in use: the ADR-011 intake keeps the GTIN out of the key and in its own column, and the first-party sync path folds the raw barcode in unless the intake flag is on. ADR-011 R3's text describes the second.
- **Grouping.** `services/product_group_autogrouper.py` groups rows that share a content key. Its multi-member write replaces an existing membership.
- **Exact tiers.** GTIN and variant barcodes, content key, canonical URL, source product id, and a brand-prefix rule, in `services/intake_identity.py`. Guards: a validated check digit, and a refusal when a barcode is reused across titles.
- **The same-size strategy** (`services/identity_cross_merchant_match.py`, rule version `cmss.v4`):
  - every listing is judged against one anchor, never retailer to retailer;
  - a listing must share a tier key with the anchor: T0a GTIN + brand + size + core, T0b exact normalised title, or T0c brand + ordered core words + size (`services/product_match_tiers.py`);
  - size is read per SKU from the variant's title, its visible option labels and its option fields;
  - vetoes: conflict tokens (concentration, SPF, version, market, refill, mini, set), shade token, differing GTIN, size, and a same-size price ratio outside 0.6 to 1.67;
  - holds: unknown or several sizes, unpinned offers, unknown price, currency;
  - it proposes only. Approval is by an operator, with a digest and a per-proposal content fingerprint. Revert is per proposal.
- **Two other paths can attach a listing to a product:**
  - `services/pdp_matcher/runner.py` attaches a seed by exact source id, exact URL, or title similarity, with a model for near misses (`PDP_MATCHER_ATTACH_ENABLED`, default off);
  - `services/retailer_ingest/official_match_judge.py` has a model judge a retailer listing against the brand's own enumerated catalogue, and treats 0.85 as attachable.

  Neither applies the same-size strategy's vetoes. Their production state was not read for this ADR.
- **A judge.** `services/identity_tier3_judge.py` asks a small model about a group. Its prompt holds title, brand and URL, and it only annotates.
- **Request time.** Offers resolution reads results. One lane admits another seller's listing only through an applied same-size proposal (`OFFERS_RESOLVE_GROUP_SEED_SIBLINGS`, shadow in production on 2026-11-22). The older catalog arm widens to every listing that shares a content key, with no identity check.

### What is missing

- **No typed record.** ADR-010's Tier 1 spec was never written. Size is parsed from titles and option fields at match time; shade is compared as text tokens. Nothing stores "this variant is shade X, 30 ml, single".
- **No candidate retrieval beyond exact keys and title similarity.** The seed matcher uses title similarity. Nothing in the identity path uses text or image embeddings, or compares images.
- **No variant-grain entity across merchants.** The existing `canonical_variants` table is unique per merchant.
- **No gold set.** There is no label table and no precision or recall measurement for the cross-merchant matcher.
- **Stored and not used as evidence:** image URLs, ingredient lists, SKU strings, part numbers, product type, description.

### What our own measurements say (private records; no names here)

- **Retailers share no hard key.** In a sample of 22 retailers of the same brands, no SKU string was shared between any two. A barcode was published by fewer than a third of them, and it produced one cross-retailer match.
- **Exact normalised title works; fuzzy title does not.** Token-set similarity with union-find merged product lines and sizes.
- **A barcode alone is not identity.** Reused barcodes across shades and placeholder values were found.
- **The first reviewed batch held.** Every pair the owner could label was correct. One merge was reverted afterwards: a listing whose title said one item and whose photo showed another.
- **Reading the pack text from the image agreed with the owner** on all but one of the cases checked.

### What each lane gives as evidence (published schemas, as read 2026-11-22)

| Lane | Title, options, images, URL | Brand | SKU | Barcode | Category |
|---|---|---|---|---|---|
| Platform catalog | yes (options normalised; some attributes inferred by the platform) | no; seller name only | no | no | filter only |
| Store door | yes, with description and tags | no field | yes | not shown | yes |
| Platform app the merchant installed | yes | optional | optional | optional | the merchant's own |
| Affiliate or merchant feed | yes | often | yes | often | often |
| Own crawl of a permitted page | yes | where marked up | where marked up | where marked up | no |

- At request time a lane gives titles, option values, images, a price and a URL. **Barcodes mostly arrive offline**, from feeds, installed apps and page data, and only where a merchant filled them.
- The platform catalog's cluster id is product-level: shade, size and pack sit inside one cluster. Its builder publishes no accuracy figure and writes that barcodes are neither consistently present nor reliable.
- **Registries do not help.** The cosmetics registries read are closed or offer no product lookup. Open barcode databases are small for beauty.
- **GS1's rules explain the regional case.** A language-only pack version and any change of declared net content get a new GTIN; each shade has its own. So equal GTINs are strong evidence, and different GTINs do not prove different products.

### What published practice says (sources at the end)

1. **The working recipe without identifiers is the same everywhere:** extract typed attributes, retrieve candidates through several channels, score with a cheap model, escalate the uncertain middle to a stronger model with images, and keep people as the floor.
2. **Attach to a canonical product, not listing to listing.** It is the easier problem, and it removes chains. The code already does this for the same-size strategy.
3. **Accept automatically only above an audited precision bar.** Published bars are 98% and 99.5%. One system accepts under half of its links automatically at 98% and escalates the rest. One team passed 90% only with expert reviewers in the loop.
4. **Numbers defeat embeddings.** Thirteen embedding models scored near chance on number-sensitive retrieval. In one study, most false positives from a large language model came from ignoring a small but significant difference.
5. **Images help, and cannot separate near-identical variants** unless the pack text is read. Image and text together beat either alone wherever it was measured.
6. **Clusters are not built by transitive closure.** One wrong link joins two groups.
7. **A model may label training data only after it is checked against people.**
8. **Nothing published covers our two hardest cases:** shade-level matching in beauty, and matching between Chinese, Japanese or Korean listings and English ones. No production system reports variant-level precision. These must be measured here.

## Decision

### M1. Grains, and how each meets the existing rules

| Grain | What it is | Stored as |
|---|---|---|
| **Family** | brand + line + formula (the owner's rule) | the product group, as ADR-010 says. The content key stays the row key and is never re-minted |
| **Cross-merchant variant** | the family plus the fields that make a different sellable item | **new.** This is ADR-010's canonical-variant layer, keyed off the family. It is not a second identity spine. It does not reuse the merchant-scoped `canonical_variants` table |
| **Listing pointer** | one seller's sellable variant on one lane | **new.** Lane, seller, the source's bare product and variant ids |

- **Offers compare at cross-merchant variant grain only** (ADR-010). A family-level link never puts two prices side by side.
- **A listing pointer and ADR-011:**
  - it is not a `catalog_products` row, so it adds no writer of that table (R2);
  - it resolves to an existing identity or stays unattached; it never mints one (R1);
  - it holds no price (R6);
  - when a listing does become a catalog row, it goes through the intake primitive like any other.
- **The beauty variant tuple, version 1:** shade (code and name), net content (value and unit), pack count, form (full size, refill, mini or travel, set, tester), stated strength (concentration, SPF and PA), edition, and market version where the pack states one.
- **Only `same_variant` joins a comparison.** Other relations are recorded and never merge: `same_family_other_variant`, `regional_version_of`, `contains` (a set and its parts), `refill_of`.
- **Regional versions are separate cross-merchant variants**, linked by `regional_version_of`, unless the brand's own catalogue lists them as one item. Which one a buyer sees follows ADR-026: the domestic offer first, a cross-border one below it, labelled.
- A new vertical needs its own tuple before any listing in it is matched.

### M2. The brand's own listing is the hub

- Where the brand sells the item itself, that listing is the anchor. Its option list is the list of shades and sizes that exist.
- A listing attaches to a hub variant. Two retailers are the same variant because each attached to the same hub variant, never because they resemble each other.
- **No hub:** a provisional anchor may be formed from retailers only through the exact tiers and a person's approval.
- **When the brand's listing appears later,** a move to it is proposed. No member moves without a proposal (ADR-010 hysteresis).
- The hub's variant list is the attribute record (M3) of the brand's own listing. It is built from a source Pivota may store: its own crawl of a permitted page, a feed, a connected merchant, or brand-supplied data. Most hubs are such listings already.

### M3. Evidence is a typed record per listing version

- Each listing version gets one **attribute record**: brand, line, name core, category, the variant tuple of M1, identifiers found (a validated GTIN, a part number, code-like tokens in the SKU or URL), an image fingerprint, the script and language, and the market.
- Every field carries its source.
- **Strong sources. Only these can create agreement:**
  - a structured option value from the seller;
  - a structured attribute the seller entered on the platform;
  - a validated identifier;
  - a whole token of the title, parsed by the deterministic parsers.
- **Weak sources. These can veto or hold; they never create agreement:**
  - a field a model filled in;
  - a field a platform inferred;
  - text read from the pack image;
  - the description.
- **Which fields must be known.** The spec marks each variant field per category:
  - **required:** net content, and shade wherever the hub lists shade options. Each must be known on both sides for any automatic acceptance;
  - **conditional:** pack count, form, stated strength, edition, market version. A listing rarely states "single" or "full size", so absent on both sides is agreement. Stated on both sides, they are compared. Stated on one side only is a hold, as today's conflict-token rule treats a refill, mini or set word.
- **Not stated means unknown.** A field is never inferred from the fact that a listing shows a single option.
- **Extraction:** the existing deterministic parsers first, then the lexicon. A vision-language model fills what they cannot, as a weak source, in two calls: one extracts, a second checks the first against the inputs. Units are converted in a separate deterministic step.
- **An identifier is evidence, never a requirement.** It is collected wherever a storable source gives one (a feed, our own crawl of a permitted page, a connected merchant, brand-supplied data) and attached to the cross-merchant variant. A barcode, SKU or part number read from a door or a catalog is a field value from that content: it is used in memory when the cascade runs and is not kept (ADR-031 D1; question 5). No stage needs an identifier to exist.
- **What is kept follows ADR-031 D1.**
  - For a listing Pivota may store (its own crawl of a permitted page, a feed, a connected merchant, brand-supplied data), the attribute record is stored.
  - For a listing known only through a store's door or a platform catalog, the record is built in memory from a fresh read when the cascade runs. What is kept is the pointer, the verdict, which fields agreed, and a fingerprint of the inputs. The field values are not kept until the source allows it in writing (question 5).
  - **The same rule binds every store this ADR names.** The retrieval indexes of M4 (identifier tokens, text embedding, image similarity, image fingerprint) hold only listings Pivota may store; a door-only or catalog-only listing is matched in memory against them and is never indexed. For such a listing, the membership evidence of M7, the adjudication record of stage D and the gold table of M9 keep the pointer, field names, outcomes and fingerprints, never the values, text or images that were read. A person labelling or reviewing such a pair sees a fresh read.
  - **A fingerprint here is a one-way hash of the inputs,** kept to detect change. The image fingerprint of M4 is a different thing, a value derived from the image, and is not kept for such a listing.
- The record is tied to a fingerprint of its inputs. When the listing changes, the record is rebuilt and the membership is re-checked (M7).

### M4. Candidates come from several channels, and recall is measured apart

- **Block** on brand (folded through the alias table, across scripts) and category.
- **Channels, combined:**
  - the exact tier keys that exist today;
  - an exact index of identifier-like tokens;
  - a multilingual text embedding of the normalised name core;
  - image similarity, and an exact match on the image fingerprint;
  - a platform's own cluster id, as a list of candidates.
- A platform's cluster id is a candidate source and one feature. **It never decides a match.** It is product-level, rebuilt by its owner, and blind to other platforms.
- The indexes hold only listings Pivota may store (M3's storage rule).
- Retrieval recall is measured against the gold set separately from the decision.

### M5. The decision cascade

| Stage | What decides | Automatic acceptance |
|---|---|---|
| **A. Exact tiers** (exist) | T0a, T0b, T0c, with every veto and hold of `cmss.v4` unchanged | per tier, after that tier passes the gate (M9) and the owner turns it on |
| **B. Typed agreement** (ADR-010 Tier 1) | the listing attaches to the hub's family; every required variant field is known on both sides **from a strong source** and is equal; and no conditional field conflicts or is stated on one side only | after the stage passes the gate and the owner turns it on |
| **C. Score** (ADR-010 Tier 2) | a small model over field agreement, text and image similarity, price relation, token overlap and cluster agreement | none under this ADR; it orders the queue |
| **D. Adjudication** (ADR-010 Tier 3) | a matcher and a sceptic, each given both records and both images, answer per field: agree, conflict or unknown, citing what they read. A deterministic verifier checks each citation exists in the input | none under this ADR; it prepares the case for a person |
| **E. A person** | approves or rejects with the case in front of them | n/a |

- **Vetoes run before every stage and again before a write.** A filled conflict on a variant field ends the pair, whichever source filled it. No model overrides a veto.
- **Unknown is not agreement.** A required variant field (M3) unknown on either side means no automatic acceptance at any stage.
- **The price veto stays as built:** a same-size price ratio outside the band vetoes. A matching price never accepts.
- **The image gate applies to every automatic acceptance, stages A and B included.** The pack text read from each side's image must not contradict its record. A contradiction, or no image on either side, sends the pair to a person. This is the gate the one reverted merge would have met.
- Models are named in configuration, not here. Stage D uses two independent models and a verifier that is not a model.

### M6. Variant comparison is typed and whole-token

Never substring, never "contains". The implementation starts from this table as tests.

| Left | Right | Verdict |
|---|---|---|
| `Glow Serum 30 ml` | `Glow Serum 30ml` | same size |
| `1 L` | `1000 ml` | same size |
| `３０ｍｌ` (full-width) | `30 ml` | same size |
| shade `120 Ivory` | shade `Ivory (120)` | same shade |
| `50ml` | `150ml` | refuse |
| shade `120` | shade `125` | refuse |
| `Red` | `Fired Brick` | refuse |
| `2 x 30 ml` | `60 ml` | refuse |
| `SPF 30` | `SPF 50` | refuse |
| `Refill` | full size | refuse |
| the only option shown is `Mini` | `Standard` | refuse |
| `Hand Cream` | `Hydro Cream` | refuse |
| `1 fl oz` | `30 ml` | hold, unless the hub states both for one variant |
| size not stated | `50 ml` | unknown: no automatic acceptance |

### M7. Clusters are stars, and every membership can be undone

- A cross-merchant variant is a star around its hub. There is no transitive closure and no listing-to-listing chain.
- A veto between two listings is a cannot-link, and is kept.
- One cross-merchant variant has one variant tuple. A second size or shade arriving on it is a hold. This generalises today's one-size-per-key rule.
- A star that grows past a set size raises an alarm and stops accepting automatically.
- **Every membership records** its stage, evidence, rule and model versions, the fingerprints of both attribute records, and the state before it. It can be reverted alone.
- **A listing that changes in place** gets a new fingerprint. Its membership moves to `needs_recheck`, its offer leaves the comparable pool, and it goes through the cascade again.
- **A reverted pair is not proposed again** until a person clears the cause.
- Every writer of a content key or a membership passes the engine's move guard.

### M8. At request time: look up only

- **A request reads stored memberships.** It makes no model call and accepts nothing.
- **A listing a lane returns that Pivota has not seen** gets a pointer and is queued for the cascade. Demand sets the order: listings for products that partners ask about are matched first.
- **The intake's exact attach is family grain only.** `services/intake_identity.py` attaches a catalog row to an existing identity on an exact GTIN or other exact key, automatically, today. That stays. It never creates a `same_variant` membership; only the cascade does.
- **Until it is resolved, it is not another seller of that product.** Whether it may appear at all is ADR-031 D8's rule.
- Once matched, the answer is a lookup for every later request, on every lane.

### M9. Evaluation and gates

- **A gold table** holds labelled pairs: same variant, same family and other variant, different, unsure. Every decision by a person is captured there (ADR-010).
- It is stratified by brand tier, category, tier or stage, hard cases (same family, different variant), and language pair. A share is labelled twice to measure agreement between labellers.
- **The gate for automatic acceptance** is the owner's: at least 600 randomly sampled accepted pairs with no error.
  - It is taken per tier for stage A (T0a, T0b, T0c separately), per stage for B, and separately for each vertical and language pair.
  - No error in 600 gives a one-sided 95% upper bound of about 0.5% on the error rate, for that slice only.
  - **Proposed, pending question 3:** a pair labelled unsure does not count toward the 600 and is replaced by a new draw, so the bound describes the pairs a person could judge. The unsure share is reported, and an unsure pair is never accepted automatically. The owner's gate as given does not say how unsure labels are handled.
  - A gate that finds an error fails. It is taken again, on a new sample, only after the cause is fixed and the rule version has changed.
- **After a gate passes:** a weekly random audit of automatic acceptances. One confirmed error suspends automatic acceptance for that slice until it is understood.
- **Monitors that need no label:** a price far from the others inside a cross-merchant variant; an image that disagrees with the hub's; a live title that no longer matches the record's fingerprint.
- **Reported:** mis-merge rate with a time limit for the unmerge (ADR-010), retrieval recall, the share of asked-for products with a second resolved seller, and time from first sight of a listing to a decision.
- **Model-made labels** may train or pre-sort only after their agreement with people is measured on the gold set. They never count toward a gate.

### M10. Across platforms, languages and regions

- **The matcher has no platform-specific rule.** Each lane maps its fields into the attribute record. A lane that gives fewer fields produces more unknowns and so more escalation, never a looser rule.
- **Brand names** are folded through one alias table that holds each brand's names in every script, taken first from the brand's own localised listings.
- **Variant fields are compared after typing**, so a size or a shade code compares the same in any language. Shade names are mapped through the hub's option list.
- **A pair in two scripts always goes to stage D or a person** until that language pair passes its own gate.

### M11. Fix first

A wider matcher would amplify these. Each is fixed before a new stage is switched on. A fix that changes a served answer ships behind its own flag, default off, named in its change.

1. The catalog arm of offers resolution widens to every listing sharing a content key with no identity check. It takes the same resolved-membership rule as the sibling lane.
2. One content key rule on every door. ADR-011 R3's text and the intake code disagree on whether the GTIN is in the key. Which one stands is owner question 6. Either way, no existing key is re-minted (R7).
3. The autogrouper's multi-member write replaces existing memberships. It stops doing so.
4. The two other attach paths (the seed matcher and the official-catalogue judge) take the variant vetoes, and accept automatically only through M5 and M9.
5. The existing judge is given the variant fields and the images, or is retired in favour of stage D.

### M12. Reconcilers and drift metrics (ADR-012, catalog convergence)

| New stored thing | Upstream truth | Reconciler | Drift metric |
|---|---|---|---|
| Attribute records (stored listings only) | the listing version | rebuilt when the input fingerprint changes; a periodic pass, stalest first | records whose fingerprint differs from the current listing |
| Cross-merchant variants and memberships | applied decisions | the engine's apply and revert; a periodic check that every member's record still agrees with the hub's tuple | members in `needs_recheck` beyond a set age; members whose record now conflicts |
| Listing pointers | the lane's answers | ADR-031 D11 | ADR-031 D11 |

The gold table and the decision log are records, not derived stores.

### M13. Switches

Every new behaviour defaults off.

| Flag | Effect |
|---|---|
| `IDENTITY_ATTRIBUTE_RECORD` (off, shadow, on) | M3 records are built and stored |
| `IDENTITY_STAGE_A_AUTO` (a list of tiers, empty by default) | a tier accepts automatically; a tier is added only after its gate and an owner decision |
| `IDENTITY_STAGE_B` (off, propose, auto) | typed agreement; `auto` only after its gate and an owner decision |
| `IDENTITY_STAGE_C`, `IDENTITY_STAGE_D` (off, shadow, propose) | score and adjudication; no `auto` value exists |
| `IDENTITY_LANE_INTAKE` (off, shadow, on) | M8 pointers and queue for listings a lane returns |

Moving `OFFERS_RESOLVE_GROUP_SEED_SIBLINGS` from shadow to on is a separate owner decision, not part of this ADR.

## Phases

1. **Fix first, and measure.** M11. The gold table, seeded from the labels the owner has already given. Attribute records built offline for the listings Pivota may store. Exit: a measured baseline for today's tiers on the gold set.
2. **Stage B for listings with a hub.** Same size first, then shade (question 1). Propose, audit, then the gate. Exit: the gate passes for one vertical in one market.
3. **Retrieval, score and adjudication in shadow.** Each measured against the gold set before it proposes anything. Exit: retrieval recall and stage precision are known numbers.
4. **Lane intake.** M8, with store-door search as the source (ADR-031 phase 3): pointers, a queue, and decisions made from a fresh read. The platform catalog is added once ADR-031's open dependency 1 is answered. Exit: the share of asked-for products with a second resolved seller.
5. **A second language pair.** Its own gold slice and gate.

## Risks

- **A wrong merge shows a buyer the wrong price for the wrong item.** Every rule here trades recall for precision. A missed match leaves a listing standing alone, which is the safe failure.
- **Labelling load.** The owner has been the labeller. Gates of 600 per slice need more labellers and a written guideline (question 2).
- **Unknowns dominate where a lane gives few fields.** Comparison coverage will be low at first on the platform catalog lane.
- **A match on a door-only listing cannot be re-examined offline.** Its field values are not kept (ADR-031 D1), only the verdict and which fields agreed. A person reviewing it later needs a fresh read. That changes if question 5 is answered with an allowance.
- **The hub can be wrong.** A brand's own listing may be mis-titled or regional. A hub variant is itself a record with evidence and can be corrected.
- **Model drift.** A model change re-runs the gold set before it serves, and never moves an attached member on its own.

## Questions for the owner

1. **Shade grain.** Phase 1 was same size, with equal shade tokens. Open shade-level matching in phase 2 for listings with a hub, or wait until same-size has passed its gate?
2. **Labellers.** Who labels beyond the owner, and may a second trained person's labels count toward a gate?
3. **The bar.** Keep "no error in 600" for every slice, or accept a stated error rate for stages that only propose to a person? And how are unsure labels counted: excluded and redrawn (this draft, M9), or counted as errors?
4. **The beauty-first spec.** Confirm both proposed amendments to ADR-010: the first attribute spec is beauty, not electronics; and model-filled fields are admitted for beauty as weak evidence only.
5. **Field values derived from a door's or a catalog's content (M3; ADR-031 D1).** Not kept until the source allows it in writing (this draft), or ask the sources for that allowance now as part of the same request as ADR-031's open dependencies 1 and 5?
6. **The content key rule (M11 item 2).** GTIN out of the key and in its own column, as the intake code does, or in the key, as ADR-011 R3's text says?

## Consequences

- Comparison across sellers becomes possible without a shared identifier, at the price of slow, audited growth in coverage.
- Identity becomes independent of any one platform's ids, and every lane feeds the same record.
- Each match has evidence a person can read, and can be undone alone.
- The matcher gets numbers: precision, recall and coverage on a gold set, per stage and slice.

## Alternatives considered

- **Use a platform's cluster id as identity.** Rejected: it is product-level, covers one platform, publishes no accuracy, and is rebuilt by its owner.
- **Require GTIN or a part number.** Rejected: most listings carry neither, and barcodes that exist are reused and mis-entered.
- **One large model judging pairs.** Rejected as the decider: its documented failure is ignoring small significant differences, which is exactly the variant case. It is used inside stage D with a verifier.
- **Fuzzy title similarity with connected groups.** Rejected on our own measurement: it merges product lines and sizes.
- **Accept stages A and B inline at request time.** Draft 1 allowed it. Rejected: the image gate needs a model call, and a request must not wait on one.

## Sources read for this ADR

- Shopify Engineering, "Catalog clustering", parts I and II: `shopify.engineering/catalog-clustering`, `shopify.engineering/catalog-clustering-part-ii`; and "Leveraging multimodal LLMs": `shopify.engineering/leveraging-multimodal-llms`.
- "Retrieve, Match, Escalate: Accurate and Scalable Product Linking with VLM-Distilled Cross-Encoders and Agentic VLMs" (2026): `arxiv.org/abs/2608.25037`. The 98% bar and the share accepted automatically.
- Peeters, Steiner and Bizer, "Entity Matching using Large Language Models" (EDBT 2025): `arxiv.org/abs/2310.11244`. The false-positive analysis.
- "Entity Resolution in Practice: Lessons from a Self-Serve Pipeline" (2026): `arxiv.org/abs/2607.26298`. Hard vetoes, the 99.5% bar, verified merges.
- "End-to-end multi-modal product matching in fashion e-commerce" (2024): `arxiv.org/abs/2403.11593`. Image and text together; expert reviewers.
- "Revealing the Numeracy Gap: An Empirical Investigation of Text Embedding Models" (2025): `arxiv.org/abs/2509.05691`.
- Instacart, attribute extraction with multimodal models: `company.instacart.com/tech-innovation/scaling-catalog-attribute-extraction-with-multi-modal-llms`.
- GS1 GTIN Management Standard: `ref.gs1.org/standards/gtin-management`.
- UCP catalog specification: `ucp.dev/specification/shopping/catalog/`.

These were read through a summarising reader. The figures above have not been checked against the pages themselves; that check comes before anyone relies on one.
