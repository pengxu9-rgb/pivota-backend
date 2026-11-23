# ADR-033: A seller is shown for a brand only after it is admitted; matching says what an item is, admission says who may sell it

**Status:** Proposed (draft 4), 2026-11-23. Nothing here is built or switched on by this ADR. Acceptance waits on the owner's questions at the end. Draft 2 answered a first independent review. Draft 3 answers a second: the owner's 2026-07-27 direction that crawled merchants are real is now followed, so sellers already served stay served and the default-deny applies to sellers nobody chose; admission is enforced where a seller enters and exclusion where a seller is emitted or used; the brand key is one named function; the existing official-domain tables are reused; and a person can exclude a seller from the first phase. Draft 4 answers a third: one cut-off and one meaning of "first seen"; entry is enforced where a seller record is minted and an offer is written; a job a person queued admits its seller once the checks pass; a marketplace host never gets a blanket admission; and an exclusion is stored with its own reason on seeds and offers.

**Decision owner:** Founder. **Runtime owners:** the backend owner (the admission record, review queue, signals, reconciler), the gateway owner (reading admissions on the request path, lane filters).

**Companions:** ADR-031 decides how supply is read through lanes. ADR-032 decides when two listings are the same sellable item. This ADR decides which sellers may be shown as an offer for a brand. ADR-031 D8 says a seller first seen through a lane "needs a seller record so the exclusions can be applied"; this ADR says what those exclusions are.

**Direction already decided by the owner:**
- **2026-07-07 (ADR-009).** A seller is a merchant record. A seller we only observe is a first-class record, and its `observed` status is a stage of identity, not a serving switch.
- **2026-07-27 (ADR-018).** "All of our crawled merchants and products should be considered as real merchants." The connection layer says how a transaction executes, not whether it is transactable.
- **2026-09-24.** Besides a brand's own site, more retailers are to be found for each brand in every market, "especially medium to long tail retailers that are trust worthy", and "we can verify those retailers as well", because buyers want a choice beyond the brand's own site.
- **2026-09-26.** For an ingest job a person queued, a store may be accepted automatically as a brand's own when its published name starts with the brand, it ships to the job's market, and it prices in that market's currency, all together and among other conditions on the name (Tier B).
- **2026-11-08.** Managed onboarding for now: no merchant signup is approved automatically.

The 2026-09-24 direction named a way to find retailers that ADR-031's later standing rule no longer allows as a stored source. What "verify" means, and what is done about grey-market sellers and marketplaces, are this draft's proposals and are put to the owner (questions 2 and 3).

**Decisions this ADR follows and does not reopen:**
- **ADR-018.** A merchant we already serve, on any of its three connection layers, is a real merchant. It stays served under this ADR without waiting for a review (S8). A merchant a person chooses to add is admitted once the checks pass, without a second approval (S3). The default-deny of this ADR is for sellers nobody chose: those a lane returns.
- **ADR-009 D1, D3 and D4, and its resolved decisions.** The seller lives on the offer and points at one merchant record. Admission is a new fact beside that record. It changes no `status`, re-keys no id and re-mints no signature.
- **ADR-025, owner decision 4.** What Pivota earns never moves an offer. This ADR extends the same wall to admission: a commission, an affiliate relationship or a paid placement is never evidence for admitting a seller.
- **ADR-030 D8.** A fact about a seller is stored and decided offline. Nothing about a seller is decided by a fetch on the request path.
- **ADR-031 D1.** Nothing from a platform catalog is kept until its operator allows it.
- **ADR-031 D6, for the order of offers.** This ADR adds no input to how offers are ordered. It does change which sellers are in the set that is ordered; S6 says where that shows.
- **ADR-031's standing rule.** No seller discovery and no stored record comes from search-grounded model results.
- **ADR-012 (catalog convergence).** Every derived store has a reconciler and a drift metric (S11).

**A rule this ADR states for itself:** a product match is never evidence that a seller is genuine. A counterfeit listing is built to match. ADR-032 does not say this; its matcher should not be read as a trust check.

**Amends** (each restated where it bites):
- **ADR-009 D2**, in two places. "Product-level gates remain the sole serving control": a person's exclusion of a seller is now a serving control too (S4, S6). "Claiming governs ownership actions, not visibility": a verified claim on a brand's official domain may name authorised sellers and report others (S9).
- **ADR-018.** A crawled merchant is real until a person excludes it. That exception is new.
- **ADR-019.** The seller-type rule calls a store the brand's own when the brand's name is in its domain. For a seller nobody chose, that is no longer enough for the `brand_own` tier (S3). `official_source` itself is not changed.
- **The stated contract of `merchant_domain_attestations`.** Its own description says it is read only by seller-type labelling and never widens a tier. Under S3 an attestation in force also counts toward `brand_own`. That is a new reader and a wider effect, decided here.
- **ADR-001's precedence, as built.** The canonical pick prefers a brand-store row. An exclusion can remove that row, so the pick can change. The same holds for ADR-032 M2's hub.
- **ADR-031 D8**, "Who may appear does not change". Who may appear does change, by S2.
- **ADR-007 D2,** only if question 4 turns the offer-free lane on.

## Scope

**Terms.**
- A *seller* is the party a buyer pays: a brand's own store, a retailer, or a third-party seller inside a marketplace. A marketplace host by itself is not a seller.
- *Admission* is a stored decision that one seller may be shown as an offer for one brand in one market, with the evidence for it.
- A *signal* is an observation that counts against a seller. A signal never proves a seller is fake.

**In scope:** which sellers' offers are served on the agent tools (`search_catalog`, `recommend_products`, `get_alternatives`, `get_product`, `get_offers`, `create_checkout_session`) and on hand-off links; what evidence admits a seller; what holds or excludes one; how the decision is reviewed, measured and undone.

**Also touched:** the gateway's other reads of the same offers (the public read tier and the first-party shopping interface) are covered by S6. Public product pages, sitemaps and the product feed are touched by an exclusion only, through the stored offers (S6; question 6).

**Out of scope:**
- **Whether a given item is genuine.** Pivota never inspects goods. It can check a seller's relationship to a brand and a seller's conduct, and nothing more. No field may claim otherwise.
- matching (ADR-032) and how supply is read (ADR-031);
- payment risk and chargeback handling by the payment partners;

## Context

### What exists today (backend main `2fa0c64f3`, gateway main near `412001950`)

| Piece | Where | What it does | What it does not do |
|---|---|---|---|
| Seller-type rule | backend `services/offer_seller_identity.py`, gateway twin | classifies an offer as the brand's own store, a retailer, or unknown ("do not guess") | no marketplace type; says nothing about authorisation |
| Known-retailer list | three copies: the backend seller-type module, its gateway twin, and the gateway's public read sourcing | stops a retailer's host being mistaken for a brand's own store; decides how a seller id is keyed | not per brand or market; not an approval; its own comments record resellers it misses |
| Other seller lists | gateway: the checkout quote's store list and the catalog attribution store list; backend: the cart-link merchant file | each switches one feature on for named stores | none says who may sell a brand |
| Seller record | `catalog_merchants`, minted at ingestion as `observed` (ADR-009) | one id per seller: by domain for a listed retailer, by brand and domain for anything else | an unlisted retailer therefore has one id per brand; some seeds carry no seller reference |
| Brand's-own-store proof | retailer ingest: Tier A (the domain's name is the brand's); Tier B (the store's published name starts with the brand, carries no reseller word and is one store's name, and the store ships to the market and prices in its currency, all together); or a person accepting the unproven-domain flag | lets a queued job write a brand's canonical rows | Tier A is name equality on any domain ending. Tier B is the store describing itself. The code records the owner's approval for Tier B; none is recorded for Tier A |
| Official-domain store | `merchant_official_domains` (per merchant; a source of verified, asserted, inferred or declared; a liveness status) with its liveness job, dormant by default; `merchant_domain_attestations` (an operator states a domain is a brand's official store, with the sources reviewed) | decides first-party on cited hosts; feeds seller-type labelling | per merchant, not per market; not read by any offer gate |
| Seed kind | `seed_kind` = `self` or `cross` on a seed | row trust keeps a retailer-sourced (`cross`) observed seller under its shadow gate | covers public row trust, not offers on the agent tools |
| Row trust gates | `services/catalog_trust_policy.py`, policy `c1.v0.9` | blocks rows that are test data, quarantined, tombstoned, unpriced, suppressed or in identity conflict | no code concerns who the seller is |
| Source quarantine | `catalog_source_quarantine`, staff-entered, keyed by domain, platform or source reference | blocks a source for every reader that opts in, public rows included | no brand or market key; readers must opt in; the outbound link service does not read it |
| Merchant directory exclusions | `services/agent_merchant_directory.py` | never lists a disabled, rejected, test or operator-excluded merchant, or a quarantined domain | a directory of who can check out, not of who may sell a brand |
| Outbound allowlist | `outbound_link_allowed_domains`, per market | restricts hand-off hosts where a market has active rows | in code, a market with no active rows allows every host |
| Outcome trust | `seller_trust_from_outcome`: return rate once a seller has 20 orders through Pivota | shown to agents on offers | no serving gate reads it; dispute and return records feed no serving gate |
| Price-ratio veto | cross-merchant matcher, 0.6 to 1.67 | stops two listings being matched | a matching rule; it records no signal about the seller |
| Brand claim | `services/brand_claim_service.py`: DNS record, domain email, or a manual method | marks the claimant's own merchant as the brand's | proves control of a domain, not that the domain is the brand's official one |

### What is missing

- No record of which retailers a brand has authorised, per brand or per market.
- No admission state on a seller. A seller that arrives through a seed is minted and can serve once its products pass the product-level gates.
- No way to exclude a seller for one brand or one market. The quarantine is all or nothing and takes public rows with it.
- No marketplace notion, and no field anywhere that names the actual seller of a marketplace listing.
- No serving gate on a seller's conduct.
- No single brand key. Two functions share one name: the one seller ids use keeps non-ASCII letters but treats an accented and an unaccented spelling as two brands; the one the seller-type rule uses reduces every brand written in a non-Latin script to an empty string.
- No join between a platform catalog's shop identifier and the storefront domain our records use. The gateway's own notes record that the two differ.

### How large the change is

- **Sellers already served are not removed by this ADR.** Under ADR-018 they are real merchants. They receive a record that says so (S8) and stay served unless a person excludes one.
- **How many have no seller type is not known today.** ADR-019 measured, on 2026-07-27, that of 2,646 external-referral offers not marked first-party, 2,166 had no seller type. That is one slice on one day. S8 measures all of it.
- **A seller a person excludes may be a product's only seller.** Recall today requires a stored offer; the offer-free citable lane exists and is off by default. So an exclusion can remove a product from search.
- **Exclusion changes more than the offer list.** It can change the canonical pick, which prefers a brand-store row, and the hub ADR-032 attaches other sellers to.

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

- **One record per (selling domain, brand key, market).** It holds the tier, the evidence with its source and the date it was read, the reviewer, and an expiry.
- **The seller key is the storefront's registrable domain,** not the merchant id. An unlisted retailer has one merchant id per brand today, so a merchant id cannot say "this retailer". The record also carries the merchant ids it covers.
- **The brand key is one new function,** used for nothing else:
  - Unicode NFKC, then case folding, then the trademark and company-suffix stripping that `services.catalog_identity.normalize_brand` already does;
  - an empty result is rejected: no admission is written or read for it, so the seller is `unverified`. The seller-type module's own normaliser is not used, because it empties every non-Latin name;
  - the gateway twin is byte-identical and both are pinned by one shared file of test names;
  - an accented and an unaccented spelling remain two keys until ADR-032's brand alias table joins them. Until then a person enters the admission under each spelling the seller code produces.
- **The market is the buyer's market** as ADR-026 resolves it: the market the offer is served to.
- **"Everywhere" is an explicit value.** A record may carry the wildcard brand, the wildcard market, or both. Only `excluded` and `established` (S2) may use a wildcard brand, and `established` may not use one on a marketplace host (S5).
- **One cut-off.** The cut-off is the moment `enforce_new` is set. A selling domain is "first seen after the cut-off" when it had no admission record at that moment. Nothing else defines it.
- **A seed with no seller reference** has no seller to admit. It is listed by the S8 report and fixed before `enforce`.
- **A platform's shop identifier is not the seller key.** Where a lane names a seller by the platform's own identifier, the join to a selling domain comes from the seller's own endpoint or profile (ADR-030 D8), never from the catalog. Without that join the seller is `unverified` on that lane.
- The record is decided offline and read on the request path as a lookup. From the cut-off on, a missing record is `unverified`: never an error and never a pass. S8's record-writing is run again at the cut-off itself, so no seller served up to that moment is missing one.

### S2. Six tiers; four are served

| Tier | Who | Served as an offer |
|---|---|---|
| `brand_own` | the brand's own store, on a domain the official-domain store holds as the brand's (S3) | yes |
| `authorised` | a seller the brand itself names for that market (S3) | yes |
| `verified_retailer` | a retailer, or a merchant that installed our app, that a person chose and that passed the S3 checks | yes |
| `established` | a seller already served at the cut-off, on any connection layer of ADR-018 | yes; reviewed over time, in order of demand |
| `unverified` | any other seller: in practice one a lane returned that nobody chose | no; counted only (question 1) |
| `excluded` | a seller a person has excluded, for a brand and market or everywhere | no; no link is issued to it and none already issued is honoured |

- **`verified_retailer` is decided per seller and market,** and applies to a brand only while that brand is in the retailer's own listings.
- **Where a brand publishes a list of authorised sellers for a market,** a seller not on it is not newly admitted for that brand as a `verified_retailer`; it goes to review. An `established` seller not on the list stays served and is reviewed (question 2).
- **The order of tiers is not an input to how offers are ordered** (ADR-031 D6).

### S3. What admits a seller

Only these create an admission. Each is stored with its source and the date it was read.

- **The brand's official domain comes first, and the store for it exists.** `merchant_official_domains` holds a brand merchant's domains with how each was established; `merchant_domain_attestations` holds an operator's reviewed statement. This ADR reuses both and adds only the market on the admission record.
  - A domain counts as the brand's when its row is `verified` or `asserted` (the brand proved control), or an operator attestation for it is in force.
  - An `inferred` or `declared` row does not count.
  - Everything the brand "says" is read from such a domain.
- **For `brand_own`:**
  - the store's domain counts as the brand's by the rule above; or
  - the store was accepted for an ingest job a person queued, by Tier A, Tier B, or a person accepting the unproven-domain flag. Those stay as they are.
  - A domain whose name contains or equals the brand's, arriving through a lane with nobody having queued it, is **not** `brand_own` until it counts by the rule above. Name equality on any domain ending is what a look-alike store has.
  - This matters twice: ADR-032 M2 makes the brand's own listing the hub that other sellers attach to.
- **For `authorised`:**
  - the brand's own published list of authorised sellers or stockists for that market, read by us from the brand's official domain;
  - a statement through a brand account whose claim is on the official domain by DNS record or domain email (S9).
- **For `verified_retailer`,** a person's choice and these checks, taken by us (question 3):
  - a published legal identity, contact details, and shipping and returns policies;
  - the storefront readable under our rules, with prices servable in the market's currency;
  - the brand present in the store's own listings and in stock;
  - for a merchant that installed our app, staff verification under the 2026-11-08 rule in place of the first check.
  - **The person's choice is the queued job or the staff approval.** A retailer ingest job a person queues, whose dry run passes these checks, admits its seller, with the person who queued it recorded. A failed check holds the job for a person, as the ingest pipeline already holds a flagged cohort. This keeps ADR-018: a seller someone chose is not default-denied.
- **For `established`:** the seller was served at the cut-off (S1). Nothing else, and nothing after it.
- **Never evidence:** a commission or affiliate relationship; a platform's eligibility for its catalog; a product match; a rating; a store's description of itself, outside the queued-job case above; anything a model inferred; anything from search-grounded model results.

### S4. Signals hold; only a person excludes

- **Signals that stop serving at once, pending a person's review:**
  - the brand's official domain names the seller as not authorised, or the brand reports it under S9;
  - a confirmed complaint that an item bought through Pivota was not genuine;
  - a dispute or return rate above a set threshold on orders through Pivota, once the seller has the minimum sample the outcome service already requires (question 7).
- **Signals that open a review without stopping serving:**
  - the matcher vetoes a set share of the seller's listings for a brand on price ratio against the brand's own listing. The matcher already computes that veto; this counts it per seller. Price is weak evidence both ways: a counterfeit may be priced just under the genuine item, and a legitimate sale may be deep;
  - an `established` seller is absent from a list its brand publishes;
  - the legal identity, contact details or policies disappear;
  - the platform or marketplace the seller was on no longer lists it.
- **A stopped seller is not yet excluded.** Its admission moves to review and it contributes no offer meanwhile. Only a person writes `excluded`, with evidence, and the write is reversible.
- **The exclusion lives on the admission record.** It is scoped to a brand and market, or to the seller everywhere (S1's wildcard).
- **A person's exclusion works from the first phase, in every mode of the switch.** It does not wait for `enforce`. Until every reader of S6 honours the record, the source quarantine remains the tool for taking a seller down everywhere: it exists, it works for the readers that opt in, and its bluntness (no brand or market, public rows too) is accepted for that interim.
- **Pivota never tells an agent that a seller is fake or unauthorised.** An unadmitted seller is simply not offered. The only tier facts an answer may carry are positive ones (question 5).
- Signals on a seller's domain age and on copied images are not adopted here. Nothing we hold measures them today.

### S5. A marketplace is not a seller

- The admission data holds a list of marketplace hosts. It must be a subset of the known-retailer list, and a test pins that. The three copies of the known-retailer list, and the seller-type rule, are not changed, so no seller id moves and no existing label changes.
- **Nothing we hold names the actual seller of a marketplace listing,** and the seller key is a domain, so it cannot name one storefront inside a marketplace. Until a lane carries the listing's seller and the key can hold it, no seller on a marketplace host is newly admitted. That includes a brand's own storefront there.
- **Offers on a marketplace host that are served at the cut-off stay served.** Their `established` record is per brand, never the wildcard, and it covers those offers only: after the cut-off the offer writers add no new offer on a marketplace host. These are first in the review order (question 2).

### S6. Admission is enforced where a seller enters; exclusion where a seller is stored, emitted or used

There is no single place where every seller passes. So the two halves of the rule are enforced at different points.

**Admission, at entry.** Sellers enter by more than one door: a lane, a retailer ingest job, a connected merchant's catalog sync, the seed batch writer, and operator scripts that write offers directly. Two points sit behind all of them.
- **Where a seller record is minted** (the observed-seller functions of `services/seller_identity.py`). After the cut-off no seller record is minted for a selling domain without a served tier.
- **Where an offer or a seed is written.** The writers check the admission record. They write nothing for an `unverified` or `excluded` seller, and nothing new on a marketplace host (S5).
- The lane module of ADR-031 D2 serves nothing from a seller without a served tier, before either point is reached.
- This is the whole of `enforce_new`.

**Exclusion, in stored state.** An excluded seller must disappear from what is already stored.
- **An exclusion has its own reason value,** on the seller's seed rows and on its offer rows, for the excluded brand and market. External offers are served from seed rows, so marking offers alone would not hide them.
- **Writers respect it.** A crawl refresh, an ingest or a projection never re-writes or clears a row marked for exclusion; each checks the admission record first.
- **Lifting an exclusion clears only that reason.** A row suppressed for another reason stays suppressed.
- The stored product views are rebuilt, and the nightly relationship graph that `get_alternatives` serves from leaves excluded sellers out at its next build.

**Exclusion, at emit.** Every point that emits a seller or its address checks the record, before any cut to a top few, any cap, and any collapse of sellers into one. Known points, from review of both repositories; report mode completes the list:

| Where | Points |
|---|---|
| Backend offers resolution | the connected-merchant, external-seed and sibling paths; the plain destination returned when a link cannot be minted |
| Backend search and product | the search handler's link attachment, the connected-product redirect attachment, the prefetched seed wrappers, the product read's offers, the product-by-id loader |
| Backend link issue | caller-supplied seed link minting; the two legacy product search routes; the rule-based outbound link resolver |
| Stored views | the agent product view's stored offers and its canonical pick; the relationship graph's stored candidates |
| Gateway search | seed cards that carry a raw destination address when attribution fails; the direct-database recall paths; the gateway's seller selection for a canonical product |
| Gateway tools | `get_alternatives`; the public read tier; the first-party shopping interface's offers call |
| Caches | the shared search answer cache |

**Exclusion, at use.** A link or an answer issued before an exclusion must not work after it:
- link redemption re-checks the record. A link is valid for seven days and is not re-checked today;
- checkout creation re-checks it on every path: the agent-to-agent purchase, the card-rail purchase, the gateway's checkout hand-off, the storefront cart escalation, the card-rail agentic lane, and the warm hand-off;
- the shared search cache's lifetime, and the one night between graph builds, bound how long an excluded seller can still appear. The emit check covers the graph meanwhile. Both bounds are stated in the change.

**What this changes besides the offer list:** which offers survive a cap, the canonical pick, and ADR-032's hub. None is a new ordering input; all are effects of removing a seller, and they are measured in report mode.

**Bulk and public surfaces** (the product feed for agent checkout, public pages, sitemaps): an exclusion reaches them through the marked rows. Whether `unverified` matters there does not arise, since an unverified seller is never stored (question 6).

**Report mode logs any seller that reaches an answer without an evaluation.** `enforce` waits until that log is empty for a stated period.

### S7. Admission per lane

| Lane (ADR-031) | How the seller is known | What applies |
|---|---|---|
| A store's own door | it is the seller, by its domain | the door is called only for an admitted seller |
| Platform catalog | the answer names each offer's seller by the platform's identifier | where the catalog offers a seller filter and S1's join exists, the query is restricted to admitted sellers; any other seller in an answer is dropped in memory and counted, not recorded (ADR-031 D1) |
| Platform app a merchant installed | the merchant | staff verification under the 2026-11-08 rule, then S3 |
| Affiliate or marketplace interface, and feeds | the advertiser's or merchant's domain | the programme or feed is a way to read a seller, never evidence for it. The seller needs its own admission by S3 before anything from the feed is stored or served; a marketplace follows S5 |
| Own crawl | the store's domain | S3 |

### S8. Sellers already served get a record, and stay served

- **A report first, with no serving change:** every seller that serves today, on any connection layer; the tier this ADR would give it; its offers; seeds with no seller reference; sellers on marketplace hosts; and, for each seller, the products for which it is the only seller.
- **Then every served seller gets a record.** A brand store whose domain counts by S3 is `brand_own`. A retailer whose S3 checks are on record is `verified_retailer`. Every other served seller is `established`: with the wildcard brand, except on a marketplace host, where it is per brand (S5).
- **The record-writing runs twice:** once in phase 2, and again at the cut-off, so a seller first served between the two is not left without a record.
- **`established` is a fact about the past.** It cannot be given to a seller after the cut-off.
- **Review of `established` sellers runs in order of demand.** Marketplace hosts and sellers with S4 signals go first. This draft sets no deadline after which an unreviewed seller stops (question 4).

### S9. The brand can speak, from its official domain

- A brand account counts as the brand only when its claim is on a domain that counts as the brand's by S3, proved by DNS record or domain email. A manual claim, or a claim on another domain, does not.
- Such an account can submit its authorised sellers per market and can report a seller. A report stops serving pending review (S4); a person confirms any exclusion.
- A report from anyone else opens a review and changes nothing by itself.

### S10. Outcomes feed back, for orders through Pivota only

- The return rate the outcome service computes, and the dispute and return records already synced, become S4 signals once the minimum sample is met. They exist only for orders placed through Pivota; a seller reached by a link hand-off produces none.
- A complaint about authenticity is recorded against the seller and the brand, with the order where there is one.

### S11. Reconciler and drift metrics (ADR-012, catalog convergence)

| Stored thing | Upstream truth | Reconciler | Drift metric |
|---|---|---|---|
| Official domains | the brand's own site | the existing official-domain liveness job, which is dormant by default and is turned on for this; a `dead` domain moves its admissions to review | domains `dead` or unchecked past a set age |
| Operator attestations | the sources the operator reviewed | an attestation carries an expiry under this ADR and is re-reviewed by a person when it lapses; a revoked one moves its admissions to review. The liveness job does not cover these | attestations past expiry; admissions resting on a revoked attestation (zero) |
| Exclusion marks on seeds and offers, and the relationship graph | admission records | a daily check that every row of an excluded seller carries the exclusion reason, that no row carries it without a record, and that the latest graph holds no excluded seller | rows of an excluded seller without the mark (zero); marks without a record (zero); excluded sellers in the graph (zero after one build) |
| Admission records | the evidence each cites | each brand list is re-read on a schedule, by our own read of the brand's page; an admission whose evidence has gone moves to review | admissions past expiry; admissions whose evidence no longer reads; time in review |
| Served sellers, including the stored offers in the agent product view | admission records | a daily check that no offer served, and no stored offer, came from an excluded seller or from a seller with no record | offers from an excluded seller (zero from phase 1); offers from a seller with no record (zero once enforced); sellers reaching an answer with no evaluation |

### S12. Switches

| Switch | Where | What it does |
|---|---|---|
| `SELLER_EXCLUSION` (off, on) | every point of S6 | honours a person's `excluded` record. On from phase 1 and independent of the switch below |
| `SELLER_ADMISSION` (off, report, enforce_new, enforce) | the lane module and ingest; the S6 points | report computes and logs with no serving change. enforce_new sets the cut-off (S1) and refuses, at the two entry points of S6, any selling domain first seen after it without a served tier. enforce also requires every served seller to have a record |
| `SELLER_ADMISSION_SIGNALS` (off, report, on) | backend | S4 and S10 |
| `SELLER_BRAND_CHANNEL` (off, on) | backend | S9 |

## Phases

1. **Record, exclusion and report.** The admission record and the brand key. A person's exclusion honoured at the S6 points, with the quarantine as the interim tool where a point is not yet covered. The S8 report. Exit: the owner has the counts by tier and the products that depend on one seller; an exclusion written by a person removes that seller's offers and its issued links stop working.
2. **Records for served sellers.** S8's first run, with the marketplace list of S5. Exit: every seller that serves today has a record.
3. **Enforce for new sellers.** S8's second run, then `enforce_new` in the same step, before any ADR-031 phase serves or stores a seller a person did not choose. In ADR-031 as drafted that is its phase 6, which brings both the catalog as a serving lane and affiliate feeds; this phase must be done before either. Exit: nothing is served or stored from a selling domain first seen after the switch without a served tier.
4. **Enforce for all.** Exit: the S11 drift for sellers with no record is zero.
5. **Signals and outcomes.** S4 and S10, in report mode first. Exit: the review queue's volume and its false-alarm rate are known.
6. **The brand channel.** S9.

## What we count

- Sellers by tier, per market.
- Brands with a domain that counts as theirs by S3; brands with a published authorised list that we have read; brands with neither.
- Offers dropped by the gate, by path and lane, and products left with no offer.
- Sellers a lane returned that were not admitted (counted, not recorded, on the catalog lane).
- Reviews opened by each signal, and how many ended in an exclusion.
- Authenticity complaints per thousand orders through Pivota, by tier.

## Risks

- **New coverage grows slowly.** Sellers already served stay. A seller a lane returns is not shown until someone reviews it, so the catalog lane adds few sellers at first. Question 1 is where the owner accepts or relaxes that.
- **Entry has several doors.** Catalog sync, the seed batch writer and operator scripts also bring sellers in. The mint and write points are meant to sit behind all of them; a writer that skips both would bypass the rule, and S11's daily check is what would show it.
- **`established` is not a check.** It carries ADR-018's direction forward; it says nothing about a seller's relationship to a brand. A fake seller already in the crawl stays until a signal or a person finds it.
- **Exclusion has many readers.** A missed emit point, a seven-day link or a cached answer can still show an excluded seller. S6 lists what is known; report mode and the time-of-use checks cover the rest.
- **The work is manual.** A stockist list read as prose, an approval per retailer, and an attestation where a brand has not proved its domain. No standard format was found. A model may help read a page we fetched; a person confirms what is stored.
- **A brand's list can be wrong or stale.** Evidence carries a date and expires.
- **A `verified_retailer` may still sell grey-market goods.** Our checks see the storefront, not the supply chain. The brand's own statement outranks them.
- **A price signal misfires.** Legitimate sales trip it and careful counterfeiters do not. It opens a review and decides nothing.
- **Outcome signals are thin.** They exist only for orders through Pivota, and only past a minimum sample.
- **Excluding a seller has legal weight.** Declining to show a seller is a product decision; calling one fake is not something we do (S4). Competition rules on selective distribution, and the regional differences on grey-market goods, should be read by counsel before `enforce` (question 8). Nothing here is a legal conclusion.
- **Brand reports can be abused** to remove a lawful competitor. A report counts only from the brand's official domain, and every exclusion has a person and evidence behind it.
- **Admission does not prove an item is genuine.** An admitted seller can still ship a bad unit.

## Questions for the owner

1. **A seller a lane returns that we cannot verify.** Not shown as an offer (this draft), or shown with a label? Ordering them lower would be a new ordering input and an amendment to ADR-031 D6.
2. **Marketplaces and grey-market sellers.** Four choices this draft makes and you have not:
   - no new seller or offer on a marketplace host is admitted, a brand's own storefront there included, until we can tell who the seller of a listing is;
   - marketplace offers already served stay, per brand, and are reviewed first;
   - a retailer the brand names as unauthorised stops serving for that brand;
   - where a brand publishes a list, a new retailer not on it is not admitted for that brand; where a brand publishes none, a `verified_retailer` is enough.
3. **What "verify" means for a retailer.** A person queues the job and the S3 checks pass (this draft, which follows how retailer ingest already works), or a second person's approval on top, or different checks?
4. **Sellers already served.** This draft reads your 2026-07-27 direction as: they stay served, with no deadline for review. Confirm, or set a date after which an unreviewed seller stops. And when an exclusion removes a product's only seller, should the product stay findable without an offer, which means turning on ADR-007's offer-free lane?
5. **Telling the agent.** Expose the tier of an admitted seller on an offer, beside `official_source`, or keep it internal?
6. **Public pages and feeds.** An exclusion reaches them through the stored offers (this draft). Confirm, or keep an excluded seller's offers on public pages?
7. **Thresholds.** The dispute and return rate that stops serving, and the share of price-ratio vetoes that opens a review. This draft sets neither.
8. **Counsel.** Read the grey-market and selective-distribution points before a seller is excluded at a brand's request?

## Consequences

- A lane can return sellers nobody chose without those sellers reaching a buyer.
- Each seller shown for a brand has evidence a person can read, with a date.
- Comparison coverage grows only as fast as sellers are reviewed.
- Pivota still does not vouch for an item. It vouches for why a seller was shown.

## Alternatives considered

- **A block-list only.** Rejected as the default: it admits every new fake shop until someone notices, and the research read says fake shops are short-lived.
- **Trust the platform's catalog bar.** Rejected: as opened, it checks identity and conduct and says nothing of counterfeits or authorisation.
- **Accept name equality or a store's own description as proof of a brand's own store, on every path.** Rejected for sellers nobody queued: it is what a look-alike store has.
- **Write exclusions to the source quarantine as the standing design.** Draft 1 proposed it. Rejected: no brand or market key, public rows go down with it, and readers must opt in. It stays as the interim tool (S4).
- **Default-deny for sellers already served.** Draft 2 implied it. Rejected: the owner's 2026-07-27 direction is that crawled merchants are real, and it would have removed an unmeasured share of offers.
- **A second official-domain record.** Draft 2 proposed one. Rejected: the store and its liveness job exist.
- **A second approval for a seller a person queued.** Draft 3 implied it. Rejected as the default: it would default-deny a chosen seller, against ADR-018. It is question 3.
- **A blanket `established` record for a marketplace host.** Draft 3 allowed it. Rejected: it would admit every third-party seller and every future listing there.
- **Hiding an excluded seller by offer suppression alone.** Draft 3 proposed it. Rejected: external offers are served from seed rows, a refresh can re-write a row, and a shared reason could be cleared by an unrelated revert.
- **One gate "before a link is minted".** Draft 2 proposed it. Rejected: some paths emit a seller's address with no mint, and links and cached answers outlive the check.
- **Let a model judge whether a seller is trustworthy.** Rejected as a decider: it cannot see a supply chain, and its output is not evidence a brand or a seller could contest.
- **Treat affiliate-programme membership as approval.** Rejected: it shows a contract with a network, and it would let what Pivota earns decide who is shown.
- **Use matching confidence as a proxy for trust.** Rejected: a counterfeit listing is built to match.

## Sources

Opened on 2026-11-23 unless marked. Company pages are described, not linked; public bodies and standards are linked.

- The merchant policies of a large shopping-ads programme: its misrepresentation policy, its counterfeit policy and its business-information requirements.
- One price-comparison service's merchant terms.
- One marketplace's brand-protection programmes (brand registration, unit-level codes, brand self-service removal), and another marketplace's authenticity programme, which does not cover beauty.
- A government guide for the beauty trade: `gov.uk/guidance/avoiding-fake-beauty-products-for-businesses`. Two brands' authorised-seller pages were opened and are not named here.
- Structured data: `schema.org/Brand` has no authorised-seller property.
- An affiliate network's published fraud measures. That network membership is not brand approval is this ADR's inference.
- A store platform's catalog: eligibility `help.shopify.com/en/manual/shopify-catalog/requirements`; seller fields and filters `shopify.dev/docs/agents/catalog/global-catalog`.
- Fake-shop signals: a European consumer centre `evz.de/en/topics/internet-shopping/online-shopping/fake-shops/`; a domain registry's research `sidnlabs.nl/en/news-and-blogs/research-into-fake-webshops-reveals-scammers-methods-and-leads-to-thousands-of-fakes-being-taken-down`; "ScamMagnifier", a measurement study at NDSS 2025.
- Scale: an EU agency's 2015 cosmetics study `euipo.europa.eu/tunnel-web/secure/webdav/guest/document_library/observatory/resources/research-and-studies/quantification-of-infringement-cosmetics_en.pdf`; a consumer group's July 2025 test of cosmetics bought on marketplaces.
- Grey market: `law.cornell.edu/cfr/text/19/133.23`; `legislation.gov.uk/eur/2017/1001/article/15/adopted`; `gov.uk/guidance/exhaustion-of-ip-rights-and-parallel-trade`.
