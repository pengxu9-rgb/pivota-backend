# ADR-033: A seller is shown for a brand only after it is admitted; matching says what an item is, admission says who may sell it

**Status:** Proposed (draft 2), 2026-11-23. Nothing here is built or switched on by this ADR. Acceptance waits on the owner's questions at the end. Draft 2 answers an independent review: the brand's-own-store tier now needs a reviewed official domain; an exclusion lives on the admission record, not in the source quarantine; the keys are defined; the size of the serving change is stated; and what the owner said is separated from what this draft proposes.

**Decision owner:** Founder. **Runtime owners:** the backend owner (the admission record, review queue, signals, reconciler), the gateway owner (reading admissions on the request path, lane filters).

**Companions:** ADR-031 decides how supply is read through lanes. ADR-032 decides when two listings are the same sellable item. This ADR decides which sellers may be shown as an offer for a brand. ADR-031 D8 says a seller first seen through a lane "needs a seller record so the exclusions can be applied"; this ADR says what those exclusions are.

**Direction already decided by the owner:**
- **2026-07-07 (ADR-009).** A seller is a merchant record. A seller we only observe is a first-class record, and its `observed` status is a stage of identity, not a serving switch.
- **2026-09-24.** Besides a brand's own site, more retailers are to be found for each brand in every market, "especially medium to long tail retailers that are trust worthy", and "we can verify those retailers as well", because buyers want a choice beyond the brand's own site.
- **2026-09-26.** Proof that a store is a brand's own, read from the store's own published name, may be accepted automatically for an ingest job a person queued.
- **2026-11-08.** No merchant signup is approved automatically.

The 2026-09-24 direction named a way to find retailers that ADR-031's later standing rule no longer allows as a stored source. What "verify" means, and what is done about grey-market sellers and marketplaces, are this draft's proposals and are put to the owner (questions 2 and 3).

**Decisions this ADR follows and does not reopen:**
- **ADR-009 D1, D3 and D4, and its resolved decisions.** The seller lives on the offer and points at one merchant record. Admission is a new fact beside that record. It changes no `status`, re-keys no id and re-mints no signature.
- **ADR-019.** `official_source` is derived from seller identity and is a display and trust signal, not a rank input.
- **ADR-025, owner decision 4.** What Pivota earns never moves an offer. This ADR extends the same wall to admission: a commission, an affiliate relationship or a paid placement is never evidence for admitting a seller.
- **ADR-030 D8.** A fact about a seller is stored and decided offline. Nothing about a seller is decided by a fetch on the request path.
- **ADR-031 D1.** Nothing from a platform catalog is kept until its operator allows it.
- **ADR-031 D6.** This ADR adds no rank input and removes none.
- **ADR-031's standing rule.** No seller discovery and no stored record comes from search-grounded model results.
- **ADR-012 (catalog convergence).** Every derived store has a reconciler and a drift metric (S11).

**A rule this ADR states for itself:** a product match is never evidence that a seller is genuine. A counterfeit listing is built to match. ADR-032 does not say this; its matcher should not be read as a trust check.

**Amends** (each restated where it bites):
- **ADR-009 D2**, in two places. "Product-level gates remain the sole serving control": on the agent tools an offer also needs its seller admitted (S6). "Claiming governs ownership actions, not visibility": a verified claim on a brand's official domain may name authorised sellers and report others (S9). Public citation pages are not changed (question 6).
- **ADR-031 D8**, "Who may appear does not change". Under this ADR who may appear as an offer does change, by S2.
- **The seller-type rule** (`services/offer_seller_identity.py` and its gateway twin). It gains a `marketplace` type for admission. The keying of seller ids by that rule is not changed (S5).

## Scope

**Terms.**
- A *seller* is the party a buyer pays: a brand's own store, a retailer, or a third-party seller inside a marketplace. A marketplace host by itself is not a seller.
- *Admission* is a stored decision that one seller may be shown as an offer for one brand in one market, with the evidence for it.
- A *signal* is an observation that counts against a seller. A signal never proves a seller is fake.

**In scope:** which sellers' offers are served on the agent tools (`search_catalog`, `recommend_products`, `get_alternatives`, `get_product`, `get_offers`, `create_checkout_session`) and on hand-off links; what evidence admits a seller; what holds or excludes one; how the decision is reviewed, measured and undone.

**Out of scope:**
- **Whether a given item is genuine.** Pivota never inspects goods. It can check a seller's relationship to a brand and a seller's conduct, and nothing more. No field may claim otherwise.
- matching (ADR-032) and how supply is read (ADR-031);
- payment risk and chargeback handling by the payment partners;
- public product pages, sitemaps and feeds (question 6).

## Context

### What exists today (backend main `2fa0c64f3`, gateway main near `412001950`)

| Piece | Where | What it does | What it does not do |
|---|---|---|---|
| Seller-type rule | backend `services/offer_seller_identity.py`, gateway twin | classifies an offer as the brand's own store, a retailer, or unknown ("do not guess") | no marketplace type; says nothing about authorisation |
| Known-retailer list | the same module; a few dozen hosts in code, one global list | stops a retailer's host being mistaken for a brand's own store; decides how a seller id is keyed | not per brand or market; not an approval; its own comments record resellers it misses |
| Seller record | `catalog_merchants`, minted at ingestion as `observed` (ADR-009) | one id per seller: by domain for a listed retailer, by brand and domain for anything else | an unlisted retailer therefore has one id per brand; some seeds carry no seller reference |
| Brand's-own-store proof | retailer ingest: Tier A (the domain's name is the brand's) and Tier B (the store's own published name) | lets a queued job write a brand's canonical rows | Tier A is name equality on any domain ending; Tier B is the store describing itself. Both were approved for jobs a person queued |
| Seed kind | `seed_kind` = `self` or `cross` on a seed | row trust keeps a retailer-sourced (`cross`) observed seller under its shadow gate | covers public row trust, not offers on the agent tools |
| Row trust gates | `services/catalog_trust_policy.py`, policy `c1.v0.9` | blocks rows that are test data, quarantined, tombstoned, unpriced, suppressed or in identity conflict | no code concerns who the seller is |
| Source quarantine | `catalog_source_quarantine`, staff-entered, keyed by domain, platform or source reference | blocks a source for every reader that opts in, public rows included | no brand or market key; readers must opt in; the outbound link service does not read it |
| Merchant directory exclusions | `services/agent_merchant_directory.py` | never lists a disabled, rejected, test or operator-excluded merchant, or a quarantined domain | a directory of who can check out, not of who may sell a brand |
| Outbound allowlist | `outbound_link_allowed_domains`, per market | meant to restrict hand-off hosts | rows are staged as disabled, and an empty market allows every host |
| Outcome trust | `seller_trust_from_outcome`: return rate once a seller has 20 orders through Pivota | shown to agents on offers | no serving gate reads it; dispute and return records feed no serving gate |
| Price-ratio veto | cross-merchant matcher, 0.6 to 1.67 | stops two listings being matched | a matching rule; it records no signal about the seller |
| Brand claim | `services/brand_claim_service.py`: DNS record, domain email, or a manual method | marks the claimant's own merchant as the brand's | proves control of a domain, not that the domain is the brand's official one |

### What is missing

- No record of which retailers a brand has authorised, per brand or per market.
- No reviewed record of each brand's official domain per market.
- No admission state on a seller. A seller that arrives through a seed is minted and can serve once its products pass the product-level gates.
- No marketplace type, and no field anywhere that names the actual seller of a marketplace listing.
- No serving gate on a seller's conduct.
- No stable brand key. The seller code normalises a brand name by a rule that drops non-ASCII letters.
- No join between a platform catalog's shop identifier and the storefront domain our records use. The gateway's own notes record that the two differ.

### How large the change is

- **Most external offers have no seller type.** ADR-019 measured, on 2026-07-27, 2,166 of 2,646 external-referral offers where the seller-type rule declined to guess. That number is old; S8 re-measures it.
- **A connected merchant has no seller type** unless its brand relationship was verified.
- **A product with no admitted seller would leave search.** Recall today requires a stored offer; the offer-free citable lane exists and is off by default.

So an allow-list enforced as written, with no review first, would remove most external offers and many products from the agent tools. That is why S8 comes first and why the gate has an `enforce_new` step.

### Why this becomes urgent with lanes

Today's protection is implicit: sellers are brand sites and retailers a person chose. ADR-031 opens lanes that return sellers nobody chose. A platform's cross-merchant catalog returns any merchant that meets the platform's own bar. As opened on 2026-11-23, that platform's eligibility page covers identity verification, genuine sales, fulfilment and chargebacks, and says nothing of counterfeits or brand authorisation.

### What published practice says (sources at the end; each point rests on the pages named there)

1. **A large shopping-ads programme checks the merchant's identity and conduct.** Its pages require a legal business name, a verifiable address, working contact details, and shipping and returns policies. It bans counterfeits and suspends on detection. The pages opened ask for no proof of authorisation up front. One comparison service's terms rely on the merchant's warranty and removal after the fact.
2. **Marketplaces that protect brands rely on the brand.** One gives registered brands unit-level codes and the power to remove listings themselves. One marketplace's physical authenticity check does not cover beauty.
3. **Brands publish their authorised sellers, in prose.** A government guide for the beauty trade says many brands publish stockist lists. The two brand pages opened were plain text, per market, and each said the brand cannot guarantee goods bought elsewhere; one also named sellers it does not authorise. No structured-data standard for this was found. An allow-list is therefore curated data, per brand and per market.
4. **Affiliate-network membership is not treated here as brand approval.** That is this ADR's inference. The one network whose rules were read vets publishers; nothing read says an advertiser must prove a brand's authorisation.
5. **Signals are clues, not proof.** Consumer bodies list prices that are too good, missing company details, look-alike domains and payment methods that do not work. A government guide adds that some counterfeits are priced only slightly below the genuine item. Registry research found most fake shops on re-registered domains, most living under a year. One measurement study flagged about 4% of 1.16 million shopping domains, found a majority of the flagged shops on one hosted store platform, and had a 2.3% false-positive rate on expert review.
6. **The problem is large in beauty.** An EU agency's 2015 study estimated that legitimate cosmetics makers lose 7.8% of sector sales to counterfeits. A consumer group's 2025 test judged 23 of 34 cosmetics bought on marketplaces likely counterfeit.
7. **Grey-market law differs by region.** In the United States a first sale exhausts trademark rights unless the goods differ materially. In the EEA rights are exhausted for goods first sold there with consent, unless the goods were changed or impaired. The United Kingdom keeps an asymmetric rule. None of this is a legal conclusion.

## Decision

### S1. Admission is a stored relation, and its keys are defined

- **One record per (selling domain, brand, market).** It holds the tier, the evidence with its source and the date it was read, the reviewer, and an expiry.
- **The seller key is the storefront's registrable domain,** not the merchant id. An unlisted retailer has one merchant id per brand today, so a merchant id cannot say "this retailer". The record also carries the merchant ids it covers.
- **The brand key is the brand identity ADR-009 mints sellers from.** Its weakness with non-ASCII names is known. Until ADR-032's brand alias table exists, an admission for such a brand is entered by a person against the key the seller code produces.
- **The market is the buyer's market** as ADR-026 resolves it: the market the offer is served to.
- **A seed with no seller reference** has no seller to admit. It is listed by the S8 report and fixed before `enforce`.
- **A platform's shop identifier is not the seller key.** Where a lane names a seller by the platform's own identifier, the join to a selling domain comes from the seller's own endpoint or profile (ADR-030 D8), never from the catalog. Without that join the seller is `unverified` on that lane.
- The record is decided offline and read on the request path as a lookup. A missing record is `unverified`, never an error and never a pass.

### S2. Five tiers; three are served

| Tier | Who | Served as an offer |
|---|---|---|
| `brand_own` | the brand's own store, on a domain in the brand's official-domain record (S3) | yes |
| `authorised` | a seller the brand itself names for that market (S3) | yes |
| `verified_retailer` | a retailer, or a merchant that installed our app, that a person approved after the S3 checks, where the brand has made no statement either way | yes |
| `unverified` | every other seller, including every seller of unknown type | no; counted only (question 1) |
| `excluded` | a seller a person has excluded for that brand and market, or everywhere | no, and no hand-off link is issued to it |

- **`verified_retailer` is decided per seller and market,** and applies to a brand only while that brand is in the retailer's own listings and the brand has not named the retailer as unauthorised.
- **The order of tiers is not a rank input** (ADR-031 D6).

### S3. What admits a seller

Only these create an admission. Each is stored with its source and the date it was read.

- **The official-domain record comes first.** For each brand and market a person records the brand's official domain, with the evidence. Everything the brand "says" is read from that domain.
- **For `brand_own`:**
  - the store's domain is in the brand's official-domain record; or
  - the store was admitted by Tier A or Tier B for an ingest job a person queued. Those stay as they are.
  - A domain whose name equals the brand's, arriving through a lane with nobody having queued it, is **not** `brand_own` until a person adds it to the official-domain record. Name equality on any domain ending is what a look-alike store has.
  - This matters twice: ADR-032 M2 makes the brand's own listing the hub that other sellers attach to.
- **For `authorised`:**
  - the brand's own published list of authorised sellers or stockists for that market, read by us from the brand's official domain;
  - a statement through a brand account whose claim is on the official domain by DNS record or domain email (S9).
- **For `verified_retailer`,** this draft proposes these checks, taken by us, then a person's approval (question 3):
  - a published legal identity, contact details, and shipping and returns policies;
  - the storefront readable under our rules, with prices servable in the market's currency;
  - the brand present in the store's own listings and in stock;
  - for a merchant that installed our app, staff verification under the 2026-11-08 rule in place of the first check.
- **Never evidence:** a commission or affiliate relationship; a platform's eligibility for its catalog; a product match; a rating; a store's description of itself, outside the queued-job case above; anything a model inferred; anything from search-grounded model results.

### S4. Signals hold; only a person excludes

- **Signals that stop serving at once, pending a person's review:**
  - the brand's official domain names the seller as not authorised, or the brand reports it under S9;
  - a confirmed complaint that an item bought through Pivota was not genuine;
  - a dispute or return rate above a set threshold on orders through Pivota, once the seller has the minimum sample the outcome service already requires (question 7).
- **Signals that open a review without stopping serving:**
  - the matcher vetoes a set share of the seller's listings for a brand on price ratio against the brand's own listing. The matcher already computes that veto; this counts it per seller. Price is weak evidence both ways: a counterfeit may be priced just under the genuine item, and a legitimate sale may be deep;
  - the legal identity, contact details or policies disappear;
  - the platform or marketplace the seller was on no longer lists it.
- **A stopped seller is not yet excluded.** Its admission moves to review and it contributes no offer meanwhile. Only a person writes `excluded`, with evidence, and the write is reversible.
- **The exclusion lives on the admission record.** It is scoped to a brand and market, or to the seller everywhere. The source quarantine is not used for it: that table has no brand or market key, takes public rows down with it, and is read only by callers that opt in.
- **Pivota never tells an agent that a seller is fake or unauthorised.** An unadmitted seller is simply not offered. The only tier facts an answer may carry are positive ones (question 5).
- Signals on a seller's domain age and on copied images are not adopted here. Nothing we hold measures them today.

### S5. A marketplace is not a seller

- Admission treats a marketplace host as `marketplace`, not `retailer`.
- **Nothing we hold names the actual seller of a marketplace listing.** Until a lane carries that field, every offer on a marketplace host is `unverified`, except a storefront inside the marketplace whose address is in the brand's official-domain record.
- The known-retailer list keeps its other job unchanged: marketplace hosts stay in it for keying seller ids, so no seller id moves.
- This removes offers that serve today. S8 counts them first (question 2).

### S6. Where the gate sits

There is no single place today. A seller reaches an answer by several paths, and hand-off links are minted per path before the one ranking step.

- **The gate runs before a link is minted,** on every path that can put a seller in front of an agent:
  - offers resolution in the backend: the connected-merchant path, the external-seed path, and the sibling path;
  - the stored offers assembled into the agent product view;
  - recall's join on a stored offer;
  - the gateway's own seller selection for a canonical product;
  - every hand-off link mint site.
- **Report mode is how the list is completed.** Any seller that reaches an answer without a gate evaluation is logged, and `enforce` waits until that log is empty for a stated period.
- **A product whose only sellers are unadmitted has no offer, and today that removes it from search,** because recall requires a stored offer. Whether such a product stays findable without an offer is ADR-007's offer-free lane, which is off; turning it on is its own decision (question 4).
- **The gate reads stored admissions only.**
- The outbound allowlist stays its own control. An `excluded` seller's host is also refused there once that allowlist enforces.

### S7. Admission per lane

| Lane (ADR-031) | How the seller is known | What applies |
|---|---|---|
| A store's own door | it is the seller, by its domain | the door is called only for an admitted seller |
| Platform catalog | the answer names each offer's seller by the platform's identifier | where the catalog offers a seller filter and S1's join exists, the query is restricted to admitted sellers; any other seller in an answer is dropped in memory and counted, not recorded (ADR-031 D1) |
| Platform app a merchant installed | the merchant | staff verification under the 2026-11-08 rule, then S3 |
| Affiliate or marketplace interface, and feeds | the advertiser's or merchant's domain | the programme or feed is a way to read a seller, never evidence for it. The seller needs its own admission by S3; a marketplace follows S5 |
| Own crawl | the store's domain | S3 |

### S8. Existing sellers are measured before anything changes

- The first step is a report, with no serving change: every seller that serves today, by the tier this ADR would assign; its offers; the products that would lose their last offer; seeds with no seller reference; and offers on marketplace hosts.
- Retailers a person queued are proposed as `verified_retailer` where the S3 checks are on record. Each is confirmed by a person. None is admitted in bulk.
- Brand stores admitted by Tier A or Tier B for a queued job are proposed as `brand_own`, and their domains go into the official-domain record for a person to confirm.
- **No existing seller stops serving until the owner has seen this report and answered question 4.**

### S9. The brand can speak, from its official domain

- A brand account counts as the brand only when its claim is on a domain in the official-domain record, proved by DNS record or domain email. A manual claim, or a claim on another domain, does not.
- Such an account can submit its authorised sellers per market and can report a seller. A report stops serving pending review (S4); a person confirms any exclusion.
- A report from anyone else opens a review and changes nothing by itself.

### S10. Outcomes feed back, for orders through Pivota only

- The return rate the outcome service computes, and the dispute and return records already synced, become S4 signals once the minimum sample is met. They exist only for orders placed through Pivota; a seller reached by a link hand-off produces none.
- A complaint about authenticity is recorded against the seller and the brand, with the order where there is one.

### S11. Reconciler and drift metrics (ADR-012, catalog convergence)

| Stored thing | Upstream truth | Reconciler | Drift metric |
|---|---|---|---|
| Official-domain records | the brand's own site | re-read on a schedule; a domain that no longer resolves to the brand moves to review | records past expiry |
| Admission records | the evidence each cites | each brand list is re-read on a schedule, by our own read of the brand's page; an admission whose evidence has gone moves to review | admissions past expiry; admissions whose evidence no longer reads; time in review |
| Served sellers, including the stored offers in the agent product view | admission records | a daily check that no offer served, and no stored offer, came from a seller without an admission | offers served without an admission (zero once enforced); sellers reaching an answer with no gate evaluation |

### S12. Switches

| Switch | Where | What it does |
|---|---|---|
| `SELLER_ADMISSION` (off, report, enforce_new, enforce) | every path of S6 | report computes and logs the gate with no serving change; enforce_new applies it only to sellers first seen after the switch; enforce applies it to all |
| `SELLER_ADMISSION_SIGNALS` (off, report, on) | backend | S4 and S10 |
| `SELLER_BRAND_CHANNEL` (off, on) | backend | S9 |

## Phases

1. **Record and report.** The official-domain and admission records, the tier assignment in report mode on every S6 path, and the S8 report. Exit: the owner has the counts by tier, the products that would lose their last offer, and the list of paths.
2. **Review of existing sellers.** S8, with the marketplace type of S5 in report mode. Exit: every seller that serves today has a tier a person has confirmed, or is listed as unknown.
3. **Enforce for new sellers.** `enforce_new`, before any ADR-031 phase serves a seller a person did not choose. In ADR-031 as drafted the catalog runs in shadow through its phase 3 and first serves in its phase 6; this phase must be done before that one. Exit: no offer is served from a seller first seen after the switch without an admission.
4. **Enforce for all,** after question 4. Exit: the S11 served-seller drift is zero.
5. **Signals and outcomes.** S4 and S10, in report mode first. Exit: the review queue's volume and its false-alarm rate are known.
6. **The brand channel.** S9.

## What we count

- Sellers by tier, per market.
- Brands with an official-domain record; brands with a published authorised list that we have read; brands with neither.
- Offers dropped by the gate, by path and lane, and products left with no offer.
- Sellers a lane returned that were not admitted (counted, not recorded, on the catalog lane).
- Reviews opened by each signal, and how many ended in an exclusion.
- Authenticity complaints per thousand orders through Pivota, by tier.

## Risks

- **Coverage falls, possibly by a lot.** On the last measurement most external offers had no seller type. An allow-list keeps out legitimate retailers until someone reviews them. Question 1 and question 4 are where the owner accepts or relaxes that.
- **The work is manual.** An official domain per brand and market, a stockist list read as prose, and an approval per retailer. No standard format was found. A model may help read a page we fetched; a person confirms what is stored.
- **A brand's list can be wrong or stale.** Evidence carries a date and expires.
- **A `verified_retailer` may still sell grey-market goods.** Our checks see the storefront, not the supply chain. The brand's own statement outranks them.
- **A price signal misfires.** Legitimate sales trip it and careful counterfeiters do not. It opens a review and decides nothing.
- **Outcome signals are thin.** They exist only for orders through Pivota, and only past a minimum sample.
- **Excluding a seller has legal weight.** Declining to show a seller is a product decision; calling one fake is not something we do (S4). Competition rules on selective distribution, and the regional differences on grey-market goods, should be read by counsel before `enforce` (question 8). Nothing here is a legal conclusion.
- **Brand reports can be abused** to remove a lawful competitor. A report counts only from the brand's official domain, and every exclusion has a person and evidence behind it.
- **Admission does not prove an item is genuine.** An admitted seller can still ship a bad unit.

## Questions for the owner

1. **Sellers we cannot verify.** Not shown as an offer (this draft), or shown with a label? Ranking them lower would be a new rank input and an amendment to ADR-031 D6.
2. **Grey-market sellers and marketplaces.** Three choices this draft makes and you have not: a marketplace offer is not shown unless its storefront is the brand's own; a retailer the brand names as unauthorised stops serving; and where a brand publishes no list at all, a `verified_retailer` is enough to show. Confirm or change each.
3. **What "verify" means for a retailer.** The S3 checks plus a person's approval (this draft), or automatic admission when every check passes, or different checks?
4. **Existing sellers of unknown type, and products with no admitted seller.** Keep such sellers serving until each is reviewed, or stop them at `enforce`? And should a product with no admitted seller stay findable without an offer, which means turning on ADR-007's offer-free lane?
5. **Telling the agent.** Expose the tier of an admitted seller on an offer, beside `official_source`, or keep it internal?
6. **Public pages and feeds.** Apply the same gate to offers shown there, or leave them as ADR-009 has them?
7. **Thresholds.** The dispute and return rate that stops serving, and the share of price-ratio vetoes that opens a review. This draft sets neither.
8. **Counsel.** Read the grey-market and selective-distribution points before `enforce`?

## Consequences

- A lane can return sellers nobody chose without those sellers reaching a buyer.
- Each seller shown for a brand has evidence a person can read, with a date.
- Comparison coverage grows only as fast as sellers are reviewed.
- Pivota still does not vouch for an item. It vouches for why a seller was shown.

## Alternatives considered

- **A block-list only.** Rejected as the default: it admits every new fake shop until someone notices, and the research read says fake shops are short-lived.
- **Trust the platform's catalog bar.** Rejected: as opened, it checks identity and conduct and says nothing of counterfeits or authorisation.
- **Accept name equality or a store's own description as proof of a brand's own store, on every path.** Rejected for sellers nobody queued: it is what a look-alike store has.
- **Write exclusions to the source quarantine.** Draft 1 proposed it. Rejected: no brand or market key, public rows go down with it, and readers must opt in.
- **Let a model judge whether a seller is trustworthy.** Rejected as a decider: it cannot see a supply chain, and its output is not evidence a brand or a seller could contest.
- **Treat affiliate-programme membership as approval.** Rejected: it shows a contract with a network, and it would let what Pivota earns decide who is shown.
- **Use matching confidence as a proxy for trust.** Rejected: a counterfeit listing is built to match.

## Sources

Opened on 2026-11-23 unless marked. A snippet means only a search result was read.

- A shopping-ads programme's merchant policies: misrepresentation `support.google.com/merchants/answer/6150127`; counterfeit `support.google.com/merchants/answer/6149993`; business information `support.google.com/merchants/answer/13693865`.
- One comparison service's merchant terms: `docs.klarna.com/platform/wix/resources/legal-and-compliance/more-solutions-guidelines/search-and-compare-terms-and-conditions/`.
- Marketplace brand programmes: `sell.amazon.com/brand-registry`, `sell.amazon.com/brand-registry/transparency`, `sell.amazon.com/brand-registry/project-zero`; an authenticity programme that does not cover beauty `ebay.com/help/buying/default/buying-authenticity-guarantee?id=5470`.
- A government guide for the beauty trade: `gov.uk/guidance/avoiding-fake-beauty-products-for-businesses`. Two brands' authorised-seller pages were opened and are not named here.
- Structured data: `schema.org/Brand` has no authorised-seller property.
- An affiliate network's fraud measures: `help.awin.com/docs/fraud-prevention-measures-at-awin`.
- A store platform's catalog: eligibility `help.shopify.com/en/manual/shopify-catalog/requirements`; seller fields and filters `shopify.dev/docs/agents/catalog/global-catalog`.
- Fake-shop signals: `evz.de/en/topics/internet-shopping/online-shopping/fake-shops/`; registry research `sidnlabs.nl/en/news-and-blogs/research-into-fake-webshops-reveals-scammers-methods-and-leads-to-thousands-of-fakes-being-taken-down`; "ScamMagnifier", a measurement study at NDSS 2025.
- Scale: an EU agency's 2015 cosmetics study `euipo.europa.eu/tunnel-web/secure/webdav/guest/document_library/observatory/resources/research-and-studies/quantification-of-infringement-cosmetics_en.pdf`; a consumer group's 2025 marketplace test `which.co.uk/news/article/at-least-two-thirds-of-cosmetics-we-bought-from-online-marketplaces-are-likely-counterfeits-a6DmA3P3BjhV`.
- Grey market: `law.cornell.edu/cfr/text/19/133.23`; `legislation.gov.uk/eur/2017/1001/article/15/adopted`; `gov.uk/guidance/exhaustion-of-ip-rights-and-parallel-trade`.
