# Non-product listings in the catalog: census and serving rule (2026-10-21)

**Trigger.** The relationship-graph targeted rebuild (2026-10-20, from 15:07Z) proposed
fentybeauty.com's **"Donate To The Clara Lionel Foundation"** (`product:ext_8e78084dca165cf7b0449f5b`)
as a `competitive_alternative` candidate for 8 anchors.

**Method.** There were four read-only one-off jobs on the production image (`scripts/ops/run_oneoff_job.sh`). Each ran in a READ ONLY session with a 25 s statement timeout, and each started only after a gate passed: pivota-pg CPU under 35% over 5 min, and no drain or relgraph-sync execution running. Peak pg CPU across the run window was 0.063. The re-runnable census is
`scripts/ops/non_product_listing_census.py`. The patterns covered donation, gift card / e-gift / voucher / certificate, shipping / package / order / delivery protection or insurance, Route, tip / gratuity, deposit, and free-gift add-ons. They were matched against the title and `product_type` of every `catalog_products` row and every `external_product_seeds` row, in any state.

## 1. What is live

| | rows |
|---|---|
| catalog rows matching, any state | 66 |
| ... live and unsuppressed | 36 |
| ... of those, `pdp_will_render` and `serving_eligible` | **1: "Heart To Tarte Donation" (tartecosmetics.com, trust `public`, $1.00)** |
| ... blocked by the existing `non_core_product` rule | 32 |
| ... blocked only by `low_quality` (would serve once rescored) | 2: blindbarber.com "Add-on Donation", "Round Up Donation" |
| active seeds matching | 49 (33 on a live catalog row, 11 on a stale one, 3 unattached) |

The Clara Lionel listing is **not** live in the catalog. Its `catalog_products` row is `sync_status = 'stale'`, with blocker `not_live`, `pdp_will_render = false` and trust `blocked`. Its seed `eps_96e8c7f8d3104b6b8c70382a` is **active** and was refreshed on 2026-10-20 05:47Z. The active seed is what put it in front of the relationship graph (section 3). A refresh that flips the row back to `live` would have served it: no rule named donations.

Live, unsuppressed rows by domain:

| domain | n | titles |
|---|---|---|
| blindbarber.com | 4 | Add-on Donation, Round Up Donation, GiftCard, SavedBy Package Protection |
| fentybeauty.com | 2 | Fenty Beauty E-Gift Cards, Shipping Protection (+ the stale Clara Lionel donation) |
| tartecosmetics.com | 2 | **Heart To Tarte Donation**, tarte gift card |
| iliabeauty.com | 2 | Gift card, Delivery Guarantee |
| tower28beauty.com | 2 | $10 Gift Card, Gift Card |
| us.comfortzoneskin.com | 3 | Birthday / Holiday / plain E-Gift Card |
| poopourri.com | 3 | e-Gift Cards |
| manyo.us | 2 | E-Gift Card, Shipping Protection by Route |
| one each | 16 | gift cards: 786cosmetics, flowerbeauty, glossier, intelligentchange, kyliecosmetics, lav-kids, maccosmetics, saiehello, tirtir, tonymoly, lapoem, paulmitchell, rovectin, tangleangel, a pivota-research test store; Shipping Protection: purito-seoul |

The census found no tip and no deposit listings. The rule still names them, matched narrowly (section 2).

## 2. How listings are kept off the shelf today, and the rule

The **suppression fields** are `catalog_products.suppressed_at` and `suppression_reason`. They are written by named ops scripts, such as `scripts/withdraw_catalog_rows.py`, which works per product_key, is reversible and also deactivates the attached seeds. Two other writers set them: the catalog-sync stale sweep and identity resolution. Every serving reader gates on `suppressed_at`. There is no standing rule that suppresses a class of listing.

The **serving classifier** is `index_pipeline_state_service._classify_product`, recomputed for every row by the nightly index-health job at 04:00 UTC. It is the one fence every writer passes through: the external-seed mirror, the retailer drain, the enrichment agent, brand crawls and hand SQL. Its `non_core_product` blocker already covered samples, gift cards, shipping protection, GWP and packettes.

The **ingest lanes** have no shared non-product filter. Each keeps its own word list, for example `curated_brand_feed._LASH_NAIL_NOT_A_PRODUCT` and `onboard_external_brand_from_crawl._is_junk_title`. They are written in two languages across two repos, and at least one writer exists outside the repo.

**The rule goes in the classifier.** It extends the existing `non_core_product` blocker; it doesn't add a new code, because every reader of `non_core_product` already treats it as "not a product". It adds:

- donations, gift vouchers and certificates, e-vouchers and store credit;
- shipping / package / order / delivery / parcel protection, insurance and guarantees;
- Route insurance and protection;
- gratuity and tip jars;
- booking / pre-order / appointment / reservation / security deposits.

These are read from each title on its own, never from the URL, so a real product filed under `/collections/donations/` stays a product. A charity edition ("Pink Lip Balm (Donation Edition)") and an add-on donation ("Lip Gloss + $1 Donation") are products too. "Donate ...", "Tip", "Add a Tip" and "Deposit" / "Deposit for ..." / "Deposit - $50" count only at the start of a title, so "Q-Tip", "Fingertip", "Colour Deposit Mask", "Deposit-Only Color Gloss" and "proceeds donated" stay products. The rule also reads `cp.product_type`, which is now selected by every eligibility query, and blocks it when it is exactly a gift card, donation, insurance, protection or deposit type. "Tips" isn't on that list, because nail stores file press-on tips under it. Separately, the existing gift-card patterns stop catching gift card holders, cases, wallets and sleeves, and "Gift Cardigan" scents; no prod row is affected by that.

The full rule was then replayed in prod over **all 26,907 live, unsuppressed catalog rows**:

- 98 hits.
- 95 of them the old rule already caught.
- **3 are new**: Heart To Tarte Donation (serving → blocked) and the two blindbarber donations (relabelled from `low_quality`).
- No real product was hit.

The effect of shipping is that one public PDP leaves serving at the first 04:00 UTC nightly after deploy. That is a prod change, so the PR waits for Peng.

*Seen in passing, not changed here:* 5 serving rows match the **old** rule but stay eligible through a sibling on the same `content_key`, because IPS is content-key grained and prefers the eligible row. They are an OHUI set "+ FREE GIFT", an INIKA "-gwp" brush, two microperfumes "Sample Vial" listings and a Tower 28 gift-with-purchase mini. This is the grain documented in `withdraw_catalog_rows.py`, not a rule gap.

## 3. Relationship-graph labels on these listings

Labels name these listings by `product:<seed id>` or `product:<signature>`. Across all 66 catalog rows and 58 seeds there are 30 labels where one is the candidate:

| relation | state | labels | listings |
|---|---|---|---|
| competitive_alternative | generated | 17 | Clara Lionel (11 on the ext ref, 6 on the sig ref) |
| competitive_alternative | prefilter_rejected | 10 | Clara Lionel |
| related_product | generated | 3 | Flower E-Gift Card (1), Pixi E-Gift Card 25 (2) |

None of them is approved, verified or served. As an anchor, there are another 26 `generated` and 5 `prefilter_rejected` labels, on Clara Lionel and Pixi E-Gift Card 25.

Suppressing a listing in the catalog doesn't rewrite the labels already generated for it, and an active seed can keep proposing the listing until the seed is deactivated. Both are handled outside this PR:

- **Existing labels:** on 2026-10-21 ~01:00Z, with Peng's go, the relationship-graph owner moved all 46 `generated` labels on these four listings to `needs_evidence`, flagged `non_product_listing` (run `relgraph_nonproduct_20261021T0100`, undoable by run id). A read-back shows none left `generated`; the 15 `prefilter_rejected` labels are untouched.
- **New candidates:** a PIVOTA-Agent change makes relationship-graph candidates respect the catalog's serving gate.

Follow-ups this census does not cover: non-English gift-card and donation titles (기프트카드, 寄付, 礼品卡, Gutschein, bon cadeau), and protection apps listed as products under other names (Extend, Seel, EcoCart, Navidium, Corso).
