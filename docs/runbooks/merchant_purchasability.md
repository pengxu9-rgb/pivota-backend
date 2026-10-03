# Merchant purchasability — the fact, the sweep, the gate (WP6)

`db/merchant_purchasability.py` (the rule and the only writer), `jobs/merchant_purchasability_sweep.py`
(the loop and its bounds, and the job's CLI), `services/shopify_cart_link_preflight.py` (the
detector), `routes/merchant_purchasability_ops.py` (the operator read), migration
`db/migrations/231_merchant_purchasability.sql` (the table),
`infra/gcp/setup_merchant_purchasability_sweep_job.sh` (the job and its trigger).

This rail is **dark by default**, behind **two** dials. `MERCHANT_PURCHASABILITY_SWEEP_ENABLED`
(the job) and `MERCHANT_PURCHASABILITY_ENFORCE` (the consumers) are both unset in the code's
defaults: the sweep's Cloud Run Job is provisioned dark (gate false, trigger paused), and
`routes/agent_commerce_reap.py` does not consult the fact at all.

**The sweep is a Cloud Run Job, `merchant-purchasability-sweep`, on the crawl subnet
`pivota-crawl` (NAT 34.82.199.35), fired hourly by Cloud Scheduler.** It is not a
`services/audit_scheduler` job any more (it was until 2026-09-27): the worker's egress is the
payment-partner-allowlisted NAT, and every worker deploy restarted its interval clock. See §9.

**They are two dials because the sweep runs only in that job, and no backend deploy creates,
re-images or arms it.** One shared dial, armed on the backend, would switch the refusals on while
nothing gathered facts anywhere — a permanent 409 on every merchant in the catalogue. Arm them in
the order in §9, never together.

---

## 1. The rule

**A merchant × market row carries a PURCHASE affordance only while it holds a fresh, POSITIVE
purchasability fact.**

* POSITIVE means one thing and one thing only: we rendered that merchant's landed checkout, the
  checkout's **own** accept-list named a **card** gateway, and the line was charged at the price we
  hold. In code that is `verdict is ELIGIBLE` **and** `card_available is True` — both conjuncts,
  written out, in `db.merchant_purchasability.is_positive`. ELIGIBLE on its own is not enough:
  the Tier B lane's ELIGIBLE means "the permalink landed on a checkout with our line on it", which
  is exactly what flowerbeauty.com satisfied.
* **"Unverifiable" is NOT positive.** A row nobody can verify keeps browse and referral; it never
  keeps buy. It is not demoted either — it simply ages out through the TTL. The old liveness
  sweep's "cannot verify ⇒ freeze, keep every affordance" is the defect this rail exists to close,
  and the TTL is what stops it recurring.
* **Card only.** A wallet is not a card. Reap pays headless with a card, so a checkout offering
  PayPal, Shop Pay, Apple Pay or a gift card and nothing else is a checkout this rail cannot
  complete, however healthy the store is.

Freshness is a TTL (default 72 h) on `positive_until`, computed and compared **server-side**.
Two consecutive confirmed negatives clear the window early. One does not — a single PRICE_DRIFT on
a store mid-sale, or one LOGIN_REQUIRED from a redirect that bounced, is not a pattern.

`is_purchasable` **fails closed** on a database error. That is the opposite of what a liveness
check should do, deliberately: a liveness sweep that cannot reach a store must not delete it, but a
purchase gate that cannot prove payment must not permit it.

---

## 2. The incident this closes (2026-09-22)

**flowerbeauty.com was served as purchasable.** Four separate signals said yes and not one of them
was about paying:

| signal | what it actually said |
|---|---|
| the liveness sweep | read `products.json` over HTTP and treated "cannot verify" as a **freeze** that kept every affordance, buy included |
| the UCP reprobe | recorded `ready_for_complete`, which means the door **PRICED a cart**. It says nothing whatever about payment |
| `/.well-known/ucp` `payment_handlers` | listed `dev.shopify.card` — a **PLATFORM CONSTANT every Shopify store repeats**, not an accept-list |
| the cart-link preflight | rendered the landed checkout and read the merchandise line, the click id and the market — and **never looked at the payment methods** |

Rendered in a browser that day, flowerbeauty's one-page checkout offered **PayPal ONLY**, and its
storefront charged **USD 8.00** against our indexed **USD 14.95**.

So the fact this table holds is narrow and positive: a checkout we rendered, whose own
`availablePaymentLines` named a card gateway with card brands, at the price we think it is.

---

## 3. How the detector works — the evidence source

The landed checkout page's serialized state carries an **`availablePaymentLines`** array — the
store's ACTUAL accept-list for this checkout. It is HTML-escaped in the page, so
`checkout_payment_methods` unescapes first and then parses every JSON value following
`"availablePaymentLines":`. Each element is:

```json
{"placements": ["PAYMENT_METHOD"], "paymentMethod": {"__typename": "...", "name": "...", "paymentBrands": ["VISA", "..."]}}
```

A **CARD** is present only when some line satisfies all three conjuncts:

1. `"PAYMENT_METHOD"` is in `placements` — an `ACCELERATED_CHECKOUT`-only line is a wallet button,
   not a card form;
2. `paymentMethod.__typename == "PaymentProvider"` — the only typename that denotes a card gateway;
3. `paymentMethod.paymentBrands` intersects the card-brand set (`_CARD_BRANDS`: VISA, MASTERCARD,
   AMEX, DISCOVER, DINERS_CLUB, JCB, UNIONPAY, MAESTRO, ELO, …).

Measured live, 2026-09-22:

| merchant | variant | card line found | other lines | `card_available` |
|---|---|---|---|---|
| idewcare.com | 46722440036604 | `PaymentProvider` `shopify_payments`, brands VISA / MASTERCARD / AMEX / DISCOVER / DINERS_CLUB / ELO | GiftCard, ShopPay, ApplePay, GooglePay, Paypal, ShopifyInstallments, AnyStripeSharedToken | **True** |
| judydoll.com | 49922977038613 | `PaymentProvider` `Airwallex`, brands VISA / MASTERCARD / AMEX / MAESTRO / JCB / UNIONPAY | GiftCard, ApplePay, GooglePay, Paypal, AnyStripeSharedToken | **True** |
| flowerbeauty.com | 17281773207622 | **NONE** | AnyGiftCardPaymentMethod, PaypalWalletConfig (PAYPAL_EXPRESS), AnyStripeSharedTokenPaymentMethod | **False → NO_CARD_PAYMENT** |

`card_available` is **three-valued**. `True` = a card line was read. `False` = the accept-list was
read and holds no card line — **positive evidence**. `None` = no accept-list could be read at all,
or the array named no method, or two copies of the serialized state disagreed. **None is never
False**: an unreadable page, a bot challenge or a transport failure is unverifiable, and this
module never turns "cannot tell" into "no card".

### The traps

**Never substring-match for a card.** Every one of these is a measured false positive:

* the substring **`creditCard` appears on ALL THREE pages**, including the PayPal-only one;
* **`AnyGiftCardPaymentMethod`** and **`AnyStripeSharedTokenPaymentMethod`** are
  `availablePaymentLines` entries on all three. They are **platform constants, not an accept-list**;
* `/.well-known/ucp` `payment_handlers` naming `dev.shopify.card` is the same trap one layer up
  (see the incident above).

Only `PaymentProvider` + `PAYMENT_METHOD` placement + card `paymentBrands` counts. Reading any of
the above as a card is exactly the false positive this detector exists to stop.

### Price parity

The same merchandise line carries the money. `checkout_line_price` reads
`merchandiseLines[].totalAmount.value.{amount,currencyCode}` and the line's `.quantity`, and
returns the **unit** price in exact minor units — only when `total % quantity == 0` and only when
the copies of the serialized state agree. Live on 2026-09-22 the three landed lines read
**13.99 / 13.99 / 8.00 USD**; our index held **14.95 USD** for flowerbeauty.

Against a caller-supplied `expected_price_minor` the difference is reported **exactly**, in minor
units, and **any non-zero difference is PRICE_DRIFT**. There is no tolerance band: a drift is a
fact about our index being wrong, and widening it would re-hide the case it was written for.
Cross-currency subtraction is not a drift, it is a category error — `price_drift` returns None.

---

## 4. Verdict table

Every `services.shopify_cart_link_preflight.Verdict` the sweep can record. **Exactly five advance
`consecutive_failures`** — the ones where the store answered about **itself**
(`db.merchant_purchasability.NEGATIVE_VERDICTS`). At `DEMOTE_AFTER_FAILURES` (**2**) consecutive
negatives, `positive_until` is cleared.

| verdict | what it means | advances `consecutive_failures`? |
|---|---|---|
| `NO_CARD_PAYMENT` | the checkout's own accept-list was READ and holds no card line (flowerbeauty.com: PayPal only) | **YES — confirmed negative** |
| `NOT_ACCEPTING_ORDERS` | 403 on `/checkouts/` saying the store "isn't set up to receive orders yet" | **YES — confirmed negative** |
| `LOGIN_REQUIRED` | a hop went through `/customer_authentication/`, `/account/login` or `shopify.com/authentication/` | **YES — confirmed negative** |
| `VARIANT_GONE` | 410 on `/cart/...`, or the storefront no longer lists the variant | **YES — confirmed negative** |
| `PRICE_DRIFT` | the landed line charges a different price than our indexed one — exact, minor units, no tolerance | **YES — confirmed negative** |
| `ELIGIBLE` | landed on a checkout carrying our merchandise line, our click id and the right market. With `card_available is True` this is the **POSITIVE** fact that arms the window and resets the counter; with `card_available is None` it is unverifiable and changes nothing | no |
| `BLOCKED_UNKNOWN` | **any other 403.** The preflight's own docstring calls it "not evidence of anything in particular" — a bot challenge as often as anything. **UNVERIFIABLE, never a negative** | no |
| `TRANSPORT_ERROR` | connect error, timeout, proxy flake. Retryable. **UNVERIFIABLE, never a negative** and never proof of ineligibility | no |
| `VARIANT_UNAVAILABLE` | the storefront lists the variant (or product) but nothing is available to buy | no |
| `VARIANT_UNVERIFIED` | the storefront would not let us confirm the variant (non-200, non-JSON, or the scan cap was reached) — absence is not proven | no |
| `CHECKOUT_MARKET_MISMATCH` | our line and click id landed, but in another market than the buyer's, or the checkout's market could not be read | no |
| `CHECKOUT_PREFILL_MISSING` | landed with the variant and click id, but a buyer prefill did not stick. The sweep runs `buyer=None`, so it cannot produce this | no |
| `PASSWORD_PAGE` | the storefront is behind a password wall | no |
| `INVALID_INPUT` | the caller's arguments were refused before any request was made | no |
| `UNCLASSIFIED` | anything nobody has named yet, including too-many-redirects and a refused hop | no |

**`BLOCKED_UNKNOWN` and `TRANSPORT_ERROR` are the two to hold on to.** They are the half that is
easy to get backwards. A bot challenge is not a merchant refusing cards, and judydoll.com **reset
direct TCP from one of our egresses while answering through another** — treating that as a negative
would demote a good merchant on a network fact. Equally they must not FREEZE the affordance, which
was the original defect; the TTL is what stops that, because a row nobody can verify stops being
fresh.

---

## 5. Dials

All read **per run / per call**, never cached at import, so arming or retuning is an env change and
not a redeploy. An invalid or out-of-range value falls back to the default **with a warning naming
the variable** — never a crash, and never a silent zero (a TTL of 0 would expire every fact on
arrival).

| variable | default | bounds | what it does |
|---|---|---|---|
| `MERCHANT_PURCHASABILITY_SWEEP_ENABLED` | **unset = off** | truthy allowlist: `1`, `true`, `on`, `yes` (case/space-insensitive) | **DIAL 1 of 2** (`db.merchant_purchasability.is_sweep_enabled`). Gates the sweep JOB and nothing else. Off = the job contacts no merchant, which matters because every check creates an abandoned checkout on a live store. **Set on the sweep's Cloud Run Job by `setup_merchant_purchasability_sweep_job.sh` — `--enable` sets it true, a run without the flag sets it false.** A value on `web` or `worker` does nothing to the sweep (on `web` it only feeds the ops route's informational `sweep_enabled`) |
| `MERCHANT_PURCHASABILITY_ENFORCE` | **unset = off** | same truthy allowlist | **DIAL 2 of 2** (`db.merchant_purchasability.is_enforcement_enabled`). Gates the CONSUMERS and nothing else: the Reap route's `merchant_not_purchasable` refusal, the checkout tier's downgrade, and the offers.resolve and product-card cart mints (a browse-only merchant is minted a `referral_only` PDP instead of a prefilled cart — see "The offers.resolve mint" and "The product-card lanes" in §9). Off = a missing fact refuses nothing. Also the `enforced` field the gateway reads |
| `MERCHANT_PURCHASABILITY_TTL_HOURS` | `72` | `1`–`720` | how long one positive fact stays positive. 720 h is 30 days; past that "fresh" is not a word that means anything |
| `MERCHANT_PURCHASABILITY_BUYER_VANTAGE` | `worker` | any string, truncated to 32 chars | the vantage `is_purchasable` demands a positive fact **FROM**. See §6 — this is the dial that decides whose question the gate is answering |
| `VANTAGE_PROXY_URL` | **unset** | must start `http://` or `https://`, else ignored | when set, every merchant is ALSO checked through that proxy and recorded under vantage `proxy`. Anything else is ignored rather than handed to httpx, which would raise inside the run |
| `MERCHANT_PURCHASABILITY_BATCH` | `20` (**pinned on the job** by the setup script) | `1`–`200` | merchants per run. Each is a full redirect chain plus up to 20 catalog pages, and each leaves an abandoned checkout behind — this is a politeness bound as much as a time bound |
| `MERCHANT_PURCHASABILITY_BUDGET_SECONDS` | `600` (**pinned on the job** by the setup script) | `30`–`3600` | wall-clock budget for one run. It stops the job **STARTING** a new merchant; one already in flight runs to completion, so a run can exceed this by one merchant's worth of fetches. The job's Cloud Run **task timeout is 1200 s** (budget + one merchant, doubled); change the two together, in the script |
| `MERCHANT_PURCHASABILITY_PAUSE_MS` | `1500` | `0`–`60000` | seconds (in ms) to wait between merchants. One store at a time, unhurried: this rail has no latency requirement and a burst of checkout creations against one platform does not help us |

Two things are deliberately **not** dials: `DEMOTE_AFTER_FAILURES` (2) and the card-brand set.
Nor is the cadence any more: `MERCHANT_PURCHASABILITY_INTERVAL_SECONDS` was the scheduler
interval and is **no longer read** — the schedule is the Cloud Scheduler trigger's cron, in the
setup script. A value left on the `worker` service is inert and may be removed. The request pacing
(≥ 1.5 s between request starts across the run) is not a dial either: it is the Tier B job's
`MIN_REQUEST_INTERVAL_S` floor, shared on purpose.

**Why the dial is split.** It was one dial, and one was a bug. The sweep ran only on the
production worker then (`_add_job` registers nothing unless `_queue_worker_enabled()`), and it
runs only in its own Cloud Run Job now — in neither case does a backend deploy start it. A single
dial set on the backend would therefore arm the refusals while the sweep that feeds them never
ran anywhere:
`is_purchasable` finds no fact for any merchant, and the Reap rail answers a permanent 409
`merchant_not_purchasable` for the entire catalogue until somebody unsets the variable again.

---

## 6. Vantage — READ THIS BEFORE ARMING

**Reachability is EGRESS-DEPENDENT, and that was measured, not assumed.**

* **judydoll.com RESET direct TCP connections from one of our egresses (3/3) while answering
  through another (3/3).**
* **A human could not open flowerbeauty.com from his browser at all, while our machine could.**

A fact gathered from one egress is a fact about **that egress**. That is why
`vantage` is part of the primary key rather than a label, and why `is_purchasable` requires a
positive fact **from the vantage named by `MERCHANT_PURCHASABILITY_BUYER_VANTAGE`** (default
`worker`). A positive fact from any OTHER vantage is evidence for a human, never permission for the
door.

> **THE BUYER VANTAGE MUST MATCH THE BUYER/PARTNER EGRESS**, or the gate is answering a question
> nobody asked. The default `worker` is honest — it names where we actually looked — but it is not
> the buyer's egress unless your buyer pays from our crawl network. Set it, and configure that
> vantage, before you arm the rail.

### `worker` now means OUR CRAWL EGRESS

Since 2026-09-27 the sweep runs in a Cloud Run Job on `pivota-crawl`, so vantage `worker` names
**the crawl egress (NAT 34.82.199.35)** — not the worker service, and never the payment NAT
(8.231.167.230) the worker leaves from. The name was kept deliberately; the alternative was a new
vantage (say `crawl`) with `MERCHANT_PURCHASABILITY_BUYER_VANTAGE` switched to it. Measured on
2026-09-27: prod `web` has `MERCHANT_PURCHASABILITY_ENFORCE=1` and **no**
`MERCHANT_PURCHASABILITY_BUYER_VANTAGE`, so the door reads `worker`. Therefore:

| | keep `worker` (chosen) | new vantage + switch the buyer dial |
|---|---|---|
| door change | none | set the dial on `web` |
| data | the job upserts the **same** PK rows; positive windows carry over | every merchant needs a first positive under the new name **before** the dial moves, or it reads `browse_only` and the Reap rail answers 409 (enforcement is on); the old `worker` rows linger until deleted |
| a merchant the crawl egress cannot verify | keeps its window (rule 3) and ages out over 72 h — visible as `unverifiable` in the report, with time to act | refused from the moment the dial moves |
| truthfulness | neither egress is the buyer's (Reap pays from its own network), so the new name buys no truth | same |

And the crawl egress is the one storefronts are known to answer: the Tier B cart-link lane's
ELIGIBLE rows — most of this population — were observed from `pivota-crawl` by the Tier B job;
`setup_scheduler.sh` records that a storefront crawl from anywhere else is answered with a
Cloudflare challenge by most brand hosts. If a reachability difference shows up anyway, it shows
up as `unverifiable` counts first (§9, "Proof of a run"), never as a demotion.

Setting `VANTAGE_PROXY_URL` adds a second vantage, `proxy`, recorded under the same
(domain, market) key. **It costs one more abandoned checkout per merchant per run** — that is the
whole price, and it is why the proxy vantage is opt-in rather than always on.

`GET /ops/merchant-purchasability` reports `buyer_vantage` next to the rows for exactly this
reason (its `sweep_enabled` field reads `web`'s own copy of the sweep dial, **not** the job's gate —
the job's gate is `gcloud run jobs describe merchant-purchasability-sweep`): a reader who looks only at `card_available` will be misled by a positive fact from the
wrong vantage. The route's `tier` field is computed through `is_purchasable` — the same function the
rail calls — so it is the answer; the rows are only the evidence.

---

## 7. Operator SQL

The supported read is the **ops route**:

```
GET /ops/merchant-purchasability?domain=<domain>&market=<XX>     (admin JWT, or the gateway's
                                                                  Google identity token)
```

It normalises `domain` and `market` through the same functions the writer keys on, so an operator
cannot be shown a different row than the door reads, and it returns `tier`, `buyer_vantage`,
`gate_enabled`, `ttl_hours` and every vantage's row. **It is not gated on the dial** — an operator
arming the rail needs to see the facts first.

The SQL below is for when you are already inside a one-off job (Cloud SQL is private-IP only; see
`reference_run_a_prod_sql_census_with_a_oneoff_job`). Table `merchant_purchasability`,
PK `(merchant_domain, market_country, vantage)`.

```sql
-- THE CENSUS. Every fact we hold, newest first.
SELECT merchant_domain, market_country, vantage, verdict, card_available,
       landed_price_minor, landed_currency, expected_price_minor, price_drift_minor,
       consecutive_failures, checked_at, positive_until,
       (positive_until IS NOT NULL AND positive_until > now()) AS positive_now
  FROM merchant_purchasability
 ORDER BY checked_at DESC NULLS FIRST;
```

```sql
-- WHO IS PURCHASABLE RIGHT NOW, PER VANTAGE. Only the row whose vantage equals
-- MERCHANT_PURCHASABILITY_BUYER_VANTAGE is what the door reads; the others are evidence.
SELECT vantage, merchant_domain, market_country, verdict, payment_methods,
       checked_at, positive_until
  FROM merchant_purchasability
 WHERE positive_until IS NOT NULL
   AND positive_until > now()
 ORDER BY vantage, merchant_domain;
```

```sql
-- WHO IS DEMOTED, AND WHY. A cleared window with a live failure counter is a demotion;
-- a cleared window with counter 0 is a fact that simply aged out.
SELECT merchant_domain, market_country, vantage, verdict, card_available,
       consecutive_failures, price_drift_minor, landed_currency, checked_at,
       evidence ->> 'detail' AS detail
  FROM merchant_purchasability
 WHERE positive_until IS NULL
    OR positive_until <= now()
 ORDER BY consecutive_failures DESC, checked_at DESC;
```

> **For "which hosts have a fact", use the census, not this query.** Since 2026-09-28 the
> population also includes the CART-MINT lane (see "The cart-mint lane" below), which is computed
> by the cart minter's own Python over every active seed and cannot be written in SQL. The query
> below covers the other three lanes only. `scripts/ops/merchant_purchasability_census.sh` reports
> every lane, per host, in the four states — see "The coverage census".

```sql
-- NEVER CHECKED, for the two Reap allowlists at merchant grain plus the connected Shopify stores
-- (see "The connected-store lane" below) — NOT the cart-mint lane. An approximation of
-- `load_population` in SQL: the connected half here skips the EU/UK refusal and the
-- `normalize_shop_host` fold of a URL-spelled domain.
WITH population AS (
    SELECT lower(merchant_domain) AS domain, upper(market_country) AS market
      FROM reap_agentic_eligibility
     WHERE product_key = '' AND variant_key = '' AND enabled = TRUE
    UNION
    SELECT lower(shop_domain) AS domain, upper(market) AS market
      FROM tierb_cart_link_eligibility
     WHERE verdict = 'ELIGIBLE'
    UNION
    SELECT lower(s.domain), upper(trim(o.region))
      FROM merchant_stores s JOIN merchant_onboarding o ON o.merchant_id = s.merchant_id
     WHERE s.status IN ('active', 'connected') AND lower(s.platform) = 'shopify'
       AND COALESCE(s.domain, '') <> '' AND upper(trim(o.region)) ~ '^[A-Z]{2}$'
       AND EXISTS (SELECT 1 FROM products_cache pc WHERE pc.merchant_id = s.merchant_id)
    UNION
    SELECT lower(o.mcp_shop_domain), upper(trim(o.region))
      FROM merchant_onboarding o
     WHERE lower(COALESCE(o.mcp_platform, '')) = 'shopify' AND COALESCE(o.mcp_shop_domain, '') <> ''
       AND upper(trim(o.region)) ~ '^[A-Z]{2}$'
       AND NOT EXISTS (SELECT 1 FROM merchant_stores s WHERE s.merchant_id = o.merchant_id
                          AND s.status IN ('active', 'connected'))
       AND EXISTS (SELECT 1 FROM products_cache pc WHERE pc.merchant_id = o.merchant_id)
)
SELECT p.domain, p.market
  FROM population p
  LEFT JOIN merchant_purchasability m
         ON m.merchant_domain = p.domain
        AND m.market_country = p.market
 WHERE m.merchant_domain IS NULL
 ORDER BY p.domain, p.market;
```

Note the join keys are the **normalised** ones (`normalize_domain` lowercases and strips one
leading `www.`; `normalize_market` is the ISO-2 helper — upper-cased, exactly two letters, never
truncated; see "Market is never defaulted"), so a population row spelled
`www.Judydoll.com` will not join to the fact row `judydoll.com` unless you fold it as above.

### Forcing a re-check

**Two options, and they are not equivalent.**

* **Wait for the sweep.** This is the normal answer. `load_population` orders never-checked rows
  first and then oldest-checked first, so a stale merchant reaches the front of the queue by
  itself. At the default interval (1 h) and batch (20) a population of a few dozen merchants is
  fully re-swept well inside one TTL window. Nothing needs doing.
* **`DELETE` the row** to jump the queue:

  ```sql
  DELETE FROM merchant_purchasability
   WHERE merchant_domain = '<domain>' AND market_country = '<XX>';
  ```

  This removes the row from `list_due`, so `_staleness` sorts that merchant as never-checked and
  the next run picks it first (or execute the job by hand: §9). **It also deletes the positive
  window immediately**: until that run completes, `is_purchasable` answers False and — with the dial on — the Reap rail refuses
  `merchant_not_purchasable` for that merchant. Delete to *unstick* a merchant, never to "refresh"
  a healthy one.

There is no UPDATE that is a correct re-check. `positive_until` is only ever written by the one
upsert the sweep issues, from the **server's** clock, off a checkout somebody actually rendered.
Hand-writing a window is minting a payment fact nobody measured.

---

## 8. How to read the evidence blob

`evidence` is JSONB, bounded at 4000 characters, written by an **allow-list** of keys in
`db.merchant_purchasability._evidence`. Those keys and nothing else:

| key | what it tells you |
|---|---|
| `verdict` | the preflight's verdict, same value as the `verdict` column — kept here so the blob is self-describing when it is copied out of the row |
| `retryable` | whether the preflight considered the outcome retryable. True only on `TRANSPORT_ERROR` |
| `detail` | the preflight's own reason string, ≤ 255 chars: `no_card_in_<labels>`, `drift_<n>_<CUR>`, `checkout_country_<XX>`, `variant_not_in_catalog`, `resolve:<ExcType>`, `status_<n>`, … **This is the field to read first** |
| `final_status` | HTTP status of the landing. 200 with a non-ELIGIBLE verdict means we read a page and did not like it; 403 means we were stopped |
| `final_host` | where the redirect chain ended. A cross-host landing (shop.app, a `*.myshopify.com`) is normal and worth knowing |
| `checkout_country` | the checkout's own `buyerIdentity` country code, read off the page. The market conjunct is decided against this |
| `variant_source` | `caller` (the Tier B confirmed variant), `product` (from a handle) or `catalog` (the sweep's representative pick). A drift on a `catalog` variant is a weaker claim than one on a `caller` variant |
| `chain` | up to 12 `[status, redacted_url]` hops. Dropped first when the blob exceeds 4000 chars, in which case `detail` reads `evidence_truncated` |

**It holds NO buyer data and NO page HTML.** Three reasons, all of them load-bearing:

1. **There is no buyer to leak.** The sweep calls the preflight with `buyer=None` — the Reap path
   carries no buyer PII in the cart permalink; the buyer's email and address travel in Reap's quote
   **body**. `SweepReport` is counts-only for the same reason (no domains, no variant ids, no rows).
2. **It is an allow-list, not a deny-list.** That is what keeps (1) true when somebody later passes
   a buyer on this path: a new field cannot land in evidence by default, it has to be added.
3. **A page body would carry the merchant's own tokens** — and would dwarf every other column,
   making this table the largest thing in the database for a read nobody does. The payment-method
   **labels** (`shopify_payments`, `Airwallex`, `PAYPAL_EXPRESS`, `AnyGiftCardPaymentMethod`) are
   stored instead, in the separate `payment_methods` column, capped at 32 entries of 64 chars, and
   they are gateway names — never a token, an id or a client secret.

Every URL in the chain has already been through `redact_cart_permalink` inside the preflight, so
host, path and click id survive and buyer values do not.

---

## 9. Turning it on, and rolling back

**The sweep runs only in its Cloud Run Job**, `merchant-purchasability-sweep`, on subnet
`pivota-crawl`, provisioned by the operator with
`infra/gcp/setup_merchant_purchasability_sweep_job.sh`. CI does not run that script, and **no
backend deploy re-images the job**: it runs the `<backend-tag>` it was last given until the script
is re-run with a newer one. **This is why there are two dials, and it is why their order is not
negotiable.**

### The egress rule

**The sweep must only ever run from `pivota-crawl` (NAT 34.82.199.35).** Never from `worker`,
`web` or a job on the `default` subnet: their egress is 8.231.167.230, **the address payment
partners allowlist**, and every check renders a merchant checkout. NAT port exhaustion is per-IP;
~50 requests over 37 Cloudflare-fronted domains in ~1 minute once tripped a cross-domain, IP-level
429 for ~15 minutes (`docs/runbooks/tierb_cart_link_eligibility.md`). That is why this is a
standalone job and **not** an `audit_scheduler` entry — `tests/test_merchant_purchasability.py`
fails if the worker scheduler ever registers it again. The second reason is cadence: an interval
job on the worker restarts its clock on every deploy, and on 2026-09-27 (worker revisions 02:10,
04:03, 04:21) one report landed in four hours. A Cloud Scheduler cron is reset by nothing.

### The job

| | |
|---|---|
| Job | `merchant-purchasability-sweep`: `python -m jobs.merchant_purchasability_sweep` — ONE sweep, then exit |
| Trigger | `merchant-purchasability-sweep-cron`, `7 0-1,5-23 * * *` UTC — :07 past every hour except 02, 03 and 04 (21 runs a day) |
| Egress | `--network default --subnet pivota-crawl --vpc-egress all-traffic` |
| Bounds | `--max-retries 0`, `--task-timeout 1200s`; `MERCHANT_PURCHASABILITY_BUDGET_SECONDS=600` and `MERCHANT_PURCHASABILITY_BATCH=20` pinned on the job |
| Gate | `MERCHANT_PURCHASABILITY_SWEEP_ENABLED` on the job: `false` without `--enable`, `true` with it |
| Identity | `sa-worker@<project>` (Secret Manager access to `DATABASE_URL`; `run.invoker` for Scheduler) |

**Why that schedule.** Neighbours on the crawl address, each at its LONGEST possible window
(task timeout × (max-retries + 1)): the store-audit probes every 5 minutes and
`retailer-ingest-drain` every 10 (stages of up to ~60 minutes, so no minute is drain-free);
`external-seed-destination-sweep` at 02:20 (task timeout 3600 s **and** `mkcrawljob`'s
`--max-retries 1`, so a timed-out first attempt's retry can run to **04:20**); and
`tierb-cart-link-eligibility` at 03:30 (task timeout 1800 s, no retry, done by 04:00), **which
checks the same merchants**. `:07` never coincides with a 5- or 10-minute start. A run is over by
`:27` at the latest, so 01:07 ends before 02:20 and 05:07 starts after the latest the external-seed
retry can end; 02:07, 03:07 and 04:07 are skipped because each would share the address with a
daily crawl. The 01:07 → 05:07 gap is four hours against a 72 h TTL.
`tests/test_setup_merchant_purchasability_sweep_job.py` re-derives these windows, retries
included, from the neighbours' own scripts. Two SCHEDULED executions of this job cannot overlap:
1200 s < 3600 s (Cloud Run jobs have no `max_instances`; the spacing stands in for the scheduler's
old `max_instances=1`). A HAND execution can — see "Run once by hand" below.

**Why that task timeout.** The budget stops the job STARTING a merchant; one in flight finishes.
One merchant's realistic worst case is ~300 s (the preflight returns at its first transport
failure; a slow-but-answering store answers a catalog page or two plus 2–3 permalink hops, each
start ≥ 1.5 s apart). 600 + 300 was the old scheduler deadline; 1200 doubles the allowance so the
budget, not Cloud Run, ends a run. **That is not a bound**: a pathological store could redirect
each of 20 catalog pages and the permalink up to 10 times at 30 s a request. Such a run is killed
at 1200 s and the execution fails — which strands nothing: each fact is one upsert, written as it
goes, and the next hour starts with whoever was not reached.
`VANTAGE_PROXY_URL` is deliberately not set on the job: a second vantage doubles every merchant's
checks, and these numbers are for one.

### The connected-store lane (2026-09-28)

The fact has a second consumer: the product-card cart gate mints a connected Shopify card's
prefilled cart only on a fresh positive fact for the cart's host × the buyer's market. Connected
stores are in neither Reap allowlist, so before this lane they were never checked, and under
`MERCHANT_PURCHASABILITY_ENFORCE` their cards could never carry a cart. `load_population` now
unions a third lane (`_CONNECTED_LANE_SQL`):

* **Store.** The same store `get_merchant_active_stores` hands the card lane: a live
  (`active` / `connected`) `merchant_stores` row, or else the legacy `merchant_onboarding.mcp_*`
  store (which the card lane uses whatever `mcp_connected` says). Shopify only, and only for a
  merchant with at least one `products_cache` row, since a store that serves no card makes a fact
  that gates nothing.
* **Not a test merchant** (2026-09-29). A merchant `services.test_merchant_policy` excludes (the
  static rig ids plus every `pivota-review-demo*` store) is skipped and counted in
  `population_skipped_test_merchant`. That is the set search already hides from buyers, so its
  fact gates no card anyone is served, and each check only leaves an abandoned checkout on our
  own store. The skip runs before the market rule, so a rig's junk region is not counted as
  market-unknown.
* **Host.** `normalize_shop_host(domain)`, the host `shopify_cart_base_url` builds the card's cart
  on.
* **Market.** The merchant's declared `merchant_onboarding.region`, only when it is an ISO-2
  country. `EU` and `UK` are refused, and so is anything that is not two letters (`APAC`,
  `shopify`, `Other`, NULL). Refused rows are counted in `population_skipped_market_unknown`;
  they are never defaulted. A wrong region costs one abandoned checkout and a negative fact,
  which is the same answer as no fact.

**Every connected store is a test store today** (Peng, 2026-09-29). There is no real
outside-merchant connection yet; the lane exists so the first one is covered on day one.

Measured on prod 2026-09-29, applying the policy to the lane's own rows: **6 rows → 4
`population_skipped_test_merchant`, 1 market-unknown, 1 target**.

| merchant | store | region | outcome |
|---|---|---|---|
| `merch_efbc46b4619cfbdf` ("Chydan") | ijaqit-v9 (live) | US | skipped: test merchant |
| `merch_bbd34645bc1950cc` | i9j3i0-kj (legacy) | US | skipped: test merchant |
| `merch_shopify_00d4a720d67d96c5dcba` | pivota-review-demo (legacy) | shopify | skipped: test merchant |
| `merch_shopify_0584b37f7a8be00a5223` | pivota-review-demo-2 (legacy) | shopify | skipped: test merchant |
| `merch_c5e24a8d3738d73b` ("Pivota Live Demo Store") | ijaqit-v9 (live) | US | **swept** (not on the list) |
| `merch_shopify_0c74768217e098809ab3` | mec3xu-zd (legacy) | shopify | market-unknown (not on the list) |

Both counts are expected and are not alerts. Adding the last two merchants to
`KNOWN_TEST_MERCHANT_IDS` would also hide them from search, so that is a product call, not a sweep
fix. The first sweep of these stores (2026-09-29 06:08Z) read ijaqit-v9 `NO_CARD_PAYMENT` and
i9j3i0-kj `VARIANT_UNVERIFIED` (a 401 from its `products.json`).

**To give a connected store a cart in another market,** set its onboarding `region` to that
country. One region per merchant is all this lane reads.

Wix and WooCommerce connected stores are left out on purpose: no card lane mints a cart for them
(`_attach_connected_product_redirects` gives only Shopify a `shop_domain`), and the preflight is
Shopify-only, so their check could only ever come back unverifiable.

### The cart-mint lane (2026-09-28)

`offers.resolve` (#2407) and the product-card lanes (#2411) mint an EXTERNAL SEED's prefilled cart
only on a fresh positive fact for the cart host × the buyer's market. The #2407 census found 432
cart offers on 53 hosts, 358 of them on 47 hosts the sweep had never checked. `load_population`
now unions a fourth lane, `_cart_mint_lane`:

* **Same producer, not a restated predicate.** It pages every `status = 'active'` seed (500 a page,
  0.1 s apart) and runs the minter's own chain on each —
  `HandoverVariantResolver.choose` → `_external_seed_redirect_identity` → `resolve_cart_permalink`
  — over the union of the inputs the four seed lanes differ on (every stored variant **plus the
  empty one**, the product id from the row, the snapshot, or none, both URL spellings). A seed is in
  the lane when any of those builds a cart. `tests/test_merchant_purchasability.py` drives the
  real `mint_external_seed_links` over the same rows and asserts every cart it builds is on a key
  the population holds.
* **One resolver per page.** A resolver stops priming at 400 product keys. The #2407 census ran
  one resolver over all 23,854 seeds, so it resolved catalog variants for at most 400 of them: its
  432 / 53 is a **floor**. The 09-27 degrade-scope census bounds the other side: 6,283 cart-capable
  seeds on 189 domains.
* **Host** = `normalize_domain(<cart base url>)`, what `_CartPurchasabilityGate.allows_cart` reads.
* **Market** = the seed row's `market` (every cart seed in the #2407 census was `US`). Never
  defaulted: anything that is not ISO-2 is counted in `population_skipped_market_unknown`. The
  gate asks the *buyer's* market; a buyer elsewhere gets no cart, which is what no fact gives.
* **No variant, and no catalog hint.** Where the Tier B lane confirmed a variant for the same key,
  that variant still wins. Otherwise the preflight picks an available variant for the market
  itself. The catalog's variant hint is now used only for keys a Reap lane names (this applies to
  the connected-store lane too): on a seed host the shortest `source_variant_id` can be a SKU or a
  stale id, which the preflight answers `INVALID_INPUT` (never positive) or `VARIANT_GONE` (a
  confirmed negative that demotes the store) — a statement about our index, not about whether the
  store takes a card.
* **Scanned at most once a day, cached in `merchant_purchasability_cart_mint_scans`** (migration
  245; created at first use by `db/merchant_purchasability_cart_mint_scans.ensure_table()`, like
  `scheduler_job_slots`). A scan reads every active seed's `seed_data` on the 2-vCPU primary, so it
  runs only on the first sweep of each **05:00Z** slot (normally 05:07, after the nightly crawls);
  every other run reads the last complete scan. A cart host that starts minting mid-day is
  therefore swept from the next day's scan on — until then it has no fact, and the gate gives it
  no cart, which is the safe answer.
* **Failures, and when they page.** A scan fails when a page read raises, a catalog lookup failed
  or never ran (`handover_lookup_failed` / `handover_not_primed`, so a cart host may be missing),
  or the scan ran past its own 300 s budget. Every attempt is recorded.
  * *Today's scan failed, and the last complete scan is ≤ 72 h old:* the run uses that scan, plus
    whatever the failed one found. It logs a WARNING and is **not** counted, so a slow database
    does not fail every run of the day. The next attempt comes no sooner than 6 h later.
  * *No complete scan in the last 72 h:* counted in `population_unreadable` (exit 1), and
    whatever exists is still swept.
  * *The cache table cannot be created or read:* **no scan at all** (scanning anyway would turn a
    broken table into an hourly full scan). The lane is counted unreadable (exit 1).
  * *A scan whose result cannot be written:* counted in `errors` (exit 4). The next run scans
    again, which is the load the cache prevents, so it has to page.
* **One counts-only line per run:**
  `cart-mint lane: source=scan|cache|cache+partial-scan age_min=… seeds_scanned=… cart_seeds=…
  keys=… complete=… elapsed_ms=…`. `SweepReport.cart_mint_population_age_min` carries the same age:
  0 when this run scanned, up to ~1,440 on a normal day, more when today's scan failed, and −1
  when there is none.

```sql
-- The scan cache: one row per attempt, newest first. `hosts` is [[host, market, seeds], ...].
SELECT scanned_at, complete, reason, seeds_scanned, cart_seeds, elapsed_ms,
       json_array_length(hosts::json) AS keys
  FROM merchant_purchasability_cart_mint_scans
 ORDER BY scanned_at DESC LIMIT 10;
```

**To force a rescan today** (e.g. after a big seed ingest), delete today's rows:
`DELETE FROM merchant_purchasability_cart_mint_scans WHERE scanned_at >= date_trunc('day', now()) + interval '5 hours';`
The next hourly run scans. **To roll the cache back**, drop the table
(`db/migrations/down/245_…`). The next run recreates it and scans once.

### Capacity: a population larger than the batch (2026-09-28)

With the cart-mint lane the population is larger than `BATCH` = 20, so a run no longer checks
everyone. It does not have to: the population is sorted **never-checked first, then
least-recently-checked**, so each run takes the 20 stalest keys and every key is re-checked once
every ⌈*T* / 20⌉ runs (*T* = `population_total`). A positive fact lives 72 h.

**Measured** (read-only, `gcloud logging`):
- **On the crawl job:** the first live run on the crawl subnet (2026-09-28 01:07Z, image
  `195b2da96`) took 207.5 s for 20 targets, **10.4 s per target**. This is the number to plan with.
- **On the worker:** 14 worker `SweepReport`s of 20 targets on 2026-09-27 05:58–09:13Z took
  76.7–104.5 s: 3.8–5.2 s per target, mean 4.4 s, the 1.5 s pause between merchants included. The
  crawl job is about 2.4× slower. Every request start is paced ≥ 1.5 s apart across the run now,
  and the egress is different.
- **The seed scan:** the #2407 census job ran all 23,854 active seeds through the same functions
  and finished inside 40 s end to end, job creation included. Locally the per-seed CPU is
  ~0.05 ms.

**One run** ≈ 20 × ~10.4 s ≈ **3.5 minutes**, plus the seed scan (est. 30–60 s; hard-stopped at
300 s) **only on the first run of each 05:00Z day** — the other 20 runs a day read the cached scan.
Worst realistic case (the daily scan run): a 300 s scan, then 20 checks (~210 s), then one
pathological merchant (~300 s) ≈ 810 s — inside the 1200 s task timeout. The 600 s budget is
measured from the start of the run, scan included, so a slow scan shortens the checks, never the
other way round.

**No `BATCH` or `BUDGET_SECONDS` change is needed**, and raising `BATCH` buys little:
- *Coverage* depends on the batch, not on the time per target, so it is the same at 10.4 s as at
  4.4 s.
- *Budget.* At 10.4 s a run fits ~52 targets in the 600 s budget after a 60 s scan. `BATCH` = 40
  would halve the first pass, at twice the checks per merchant.
- *Politeness.* `BATCH` ≈ 60 would check most of the population every hour — 21 abandoned
  checkouts per merchant per day, today's rate — and would run into the budget (60 × 10.4 s
  ≈ 624 s), so the rest would be counted as `abandoned_budget` and left to the rotation anyway.

**Projected steady state** (*T* ≈ 75 with the census floor of 53 cart hosts; ≈ 230 at the
189-domain upper bound):

| | *T* = 75 | *T* = 230 |
|---|---|---|
| runs per full rotation, ⌈*T*/20⌉ | 4 | 12 |
| longest gap between two checks of one key (21 runs/day, incl. the 01:07 → 05:07 gap) | ~7 h | ~15 h |
| margin to the 72 h TTL | ~10× | ~5× |
| checks (= abandoned checkouts) per merchant per day, 21 × 20 / *T* | ~5.6 | ~1.8 |
| first full pass after arming (every key has a fact) | ~4 h | ~12–15 h |

The rotation stops keeping every positive fact fresh only past *T* ≈ 1,240 (62 runs of 20 inside
72 h). Watch `population_total` and `population_never_checked` in the `SweepReport`: after the
first rotation `population_never_checked` should read ~0, and a key that stays never-checked is
one the rotation is not reaching. If *T* ever approaches that limit, raise `BATCH` in
`infra/gcp/setup_merchant_purchasability_sweep_job.sh` (the task timeout is derived from it) —
not with `gcloud run jobs update`.

The rotation is only as good as its order, so the staleness read (`facts.list_due`) is now
strict: if it fails, the run sweeps in key order and counts one `population_unreadable`, instead of
silently treating every key as never-checked and re-sweeping the alphabetically-first 20 every hour.

### Politeness: ~40 merchants, hourly

The population is the union of the two Reap allowlists at merchant grain: 2 merchants until
2026-09-27, ~37–40 once the Tier B ELIGIBLE rows (since 03:30Z that day) are included. The
connected-store lane added 2 more on 2026-09-28.

* **Per request.** Every request of a run — every merchant, every vantage — passes one pacer:
  request **starts ≥ 1.5 s apart** (the Tier B job's `MIN_REQUEST_INTERVAL_S`, on the same
  address), one merchant at a time, plus the 1.5 s pause between merchants. Peak rate from this job
  is therefore **≤ 40 requests/minute**, the same ceiling the Tier B job was given after the 429
  incident (~50 requests/minute across 37 domains). Before this job existed the sweep spaced only
  merchants, and one merchant's pages and hops went back to back.
* **Per run.** At most `BATCH` = 20 merchants. A merchant is ~1 catalog page (250 products; the
  Tier B rows carry a confirmed variant) + ~2–3 permalink hops ≈ **3–5 requests**, bounded at 30.
  So a run is **~60–100 requests over ~2–4 minutes** (≥ 1.5 s × requests + 1.5 s × 20 pauses), well
  inside the 600 s budget — the budget binds only on a pathological store.
* **Per hour, from the crawl address.** One run: ~60–100 requests, then nothing until the next
  `:07`. Against the drain and the probes that share the address, that is a small, spaced addition.
* **Per merchant.** The population is ordered least-recently-checked first, and the batch is a
  **rotation, not a TTL filter**: with *P* merchants each is checked `21 × min(1, 20 / P)` times a
  day. At P = 40 that is **~11 checks, i.e. ~11 abandoned checkouts per merchant per day** (plus one
  from the daily Tier B job); at P ≤ 20 it is 21. With the cart-mint lane (P ≈ 75–230, see
  "Capacity") it falls to ~2–6. For comparison, the worker was running the
  sweep at a 900 s interval on 2026-09-27 — 96 checks per merchant per day for the 2 merchants
  then in the population. Lower `BATCH` in the script to cut the per-merchant count once P > 20.
* **Freshness.** A merchant is re-checked every ~2 h at P = 40, against a 72 h TTL; two consecutive
  negatives (the demotion rule) therefore land within ~4 h of a merchant turning its card off.

### Cutting over from the worker (one time, 2026-09-27 onwards)

On 2026-09-27 prod ran the sweep on the worker (`MERCHANT_PURCHASABILITY_SWEEP_ENABLED=1`,
`MERCHANT_PURCHASABILITY_INTERVAL_SECONDS=900` on `worker`) with `MERCHANT_PURCHASABILITY_ENFORCE=1`
on `web`. **Enforcement is on, so the facts must not lapse.** Order:

1. **Merge.** The on-merge deploy ships a worker without the registration: the payment-NAT sweep
   stops. The facts it wrote stay positive for 72 h from their last check — that is the window for
   steps 2–5. Do them the same day.
2. **Confirm the worker really moved on.** The serving `worker` revision must be at (or after) the
   merged commit — if the on-merge deploy failed, the old revision is still sweeping from the
   payment NAT:
   ```sh
   gcloud run services describe worker --region us-west1 --project pivota-prod \
     --format='value(status.latestReadyRevisionName,status.traffic)'
   ```
   and that revision's `PIVOTA_COMMIT_SHA` must be the merged sha or a descendant of it.
3. **Take the gate off the worker — MANDATORY, before arming.** With the gate still on the
   worker, any older worker image that comes back (a failed deploy, `deploy_worker.sh` with an
   older tag, a rollback for an unrelated incident) silently resumes the 900 s payment-NAT sweep
   next to the job:
   ```sh
   gcloud run services update worker --region us-west1 --project pivota-prod \
     --remove-env-vars MERCHANT_PURCHASABILITY_SWEEP_ENABLED,MERCHANT_PURCHASABILITY_INTERVAL_SECONDS
   ```
   This changes the service template, which `deploy_worker.sh`'s default `CONFIG=preserve`
   carries forward. **It does not change old revisions:** a traffic split or rollback to a
   revision created before this step runs with that revision's own env, gate included — check
   the env of any revision you route traffic to.
4. **Provision dark** with the merged commit's tag and check the job looks right (subnet, gate
   false, trigger paused):
   ```sh
   infra/gcp/setup_merchant_purchasability_sweep_job.sh prod <backend-tag>
   ```
5. **Arm:**
   ```sh
   infra/gcp/setup_merchant_purchasability_sweep_job.sh prod <backend-tag> --enable
   ```
   and either wait for the next `:07`, or run once by hand (below).
6. **Prove it** (next subsection): an execution succeeded, its `SweepReport` line has `checked > 0`,
   `population_unreadable=0`, `errors=0`, and `checked_at` on the facts has moved.

**Never run the old and the new together.** Two sweeps double the abandoned checkouts, and one of
them is on the payment NAT. Step 3 is what makes that true for every worker revision created from
now on; step 2 and the note in step 3 cover the ones that already exist.

### Run once by hand

```sh
gcloud run jobs execute merchant-purchasability-sweep --region us-west1 --project pivota-prod --wait
```

**Only when the next scheduled `:07` is more than 20 minutes away** (or with the trigger paused).
A hand execution does not replace the scheduled one, a run can last up to 1200 s, and Cloud Run
jobs allow concurrent executions: two overlapping runs each have their own pacer (so the rate on
the crawl address doubles) and both take the same least-recently-checked merchants (so the
abandoned checkouts double). A dark job executed by hand exits 0 and contacts nobody.

### The arming order (a fresh environment)

1. **Deploy dark.** Both dials unset; provision the job without `--enable`. The job touches no
   merchant and `routes/agent_commerce_reap.py` does not consult the fact.
2. **Set the vantage first.** `MERCHANT_PURCHASABILITY_BUYER_VANTAGE` — and, if that is not our
   crawl egress, `VANTAGE_PROXY_URL` on the job — **before** either dial. See §6. Arming with the
   wrong vantage gates on a fact about a network the buyer does not pay from.
3. **Arm the job** — `setup_merchant_purchasability_sweep_job.sh <env> <backend-tag> --enable`.
   Nothing is refused yet. The job gathers facts from its next run.
4. **Wait for one full pass over the population.** `ceil(population / 20)` runs, i.e. that many
   hours (skipping 02, 03 and 04 UTC). A pass is complete when `checked` has covered `population`
   across runs. **`errors` and `population_unreadable` are the counts that should page anyone**
   (both fail the execution); a high `unverifiable` is not an error, it is the egress telling you
   something, and §6 is where to look.
5. **Verify coverage merchant by merchant** through
   `GET /ops/merchant-purchasability?domain=…&market=…`. Every merchant you expect to be
   purchasable must read `"tier": "purchase"`. If one stays `browse_only`, the response's `note`
   names the three candidate reasons — a positive row under a DIFFERENT vantage, an expired
   window, or two consecutive negatives — and §7's demotion query tells you which. **Do not skip
   this step:** it is the only thing standing between step 6 and a 409 on a live merchant.
6. **`MERCHANT_PURCHASABILITY_ENFORCE=1`** on `web`. Only now does a missing fact refuse a purchase.

7. **AUTH: the two OIDC envs, ON THE BACKEND FIRST** — `OPS_GATEWAY_OIDC_AUDIENCE` and
   `OPS_GATEWAY_SERVICE_ACCOUNTS`. Both or neither. See §10.
8. **THEN the gateway's `PIVOTA_OPS_OIDC_AUDIENCE`,** the same string byte for byte. See §10.
9. **`MERCHANT_PURCHASABILITY_GATE_ENABLED` on the gateway must not be set until PIVOTA-Agent
   makes "enforced AND unkeyable → browse-only (no warm cart)".** That change is
   **PIVOTA-Agent #2276**; check it is MERGED and DEPLOYED to the gateway before this step is
   taken. Until
   then an unkeyable request — every click whose market was never known, which since
   2026-09-26 includes every market-less seed-lane click (see "Market is never defaulted") — is
   answered `{offer: true, source: 'failed'}` by the gateway's client and gets a warm cart.

> **Arming these in the other order is the outage.** With `ENFORCE` on and no facts gathered,
> every merchant reads `browse_only` and the Reap rail refuses `merchant_not_purchasable` (409)
> for all of them. Setting `SWEEP_ENABLED` on `web` or `worker` does not fix it — the sweep runs
> only in its job, and only the setup script's `--enable` arms that.
>
> Steps 7–8 are independent of 1–6 and may be done at any point, but the **order between them**
> is not optional, and for the same reason in reverse: gateway-first is silent. See §10.

### Proof of a run: the job's execution, and the `SweepReport` line

**The authoritative proof is a SUCCEEDED execution of the job.** (`/__scheduler_health` on the
worker no longer lists the sweep at all.)

```sh
gcloud run jobs executions list --job merchant-purchasability-sweep \
  --region us-west1 --project pivota-prod --limit 5
```

One execution per scheduled hour; the exit code is the run's verdict, and a non-zero one fails the
execution and trips the "Cloud Run job failing" alert:

| Exit | Meaning | Do |
|---|---|---|
| 0 | done — including a dark run (gate off, nobody contacted), an empty population, and a run the budget cut short (the rest are first next hour) | nothing |
| 1 | a population lane could not be read or came back incomplete, or the staleness read failed (`population_unreadable > 0`), the population could not be built at all, or the database could not be connected to (`could not connect to the database` on the job's stdout; nothing was read). Whatever *was* read was still swept. Python's own exit on an uncaught traceback is also 1 — the log tells them apart; every case means "the population was not read" | read the log's `population lane(s) could not be read` line and the WARNING before it (it names the lane and the error type); with `ENFORCE` on, merchants on the unread allowlist are ageing towards a 409 |
| 4 | the population was read, but a check raised or a fact could not be written (`errors > 0`) | read the job log; the per-check lines carry the vantage and the error type, never the merchant |

`--max-retries 0`: a failed execution is not re-run automatically (a re-run is another round of
abandoned checkouts); the next hour's run is the retry.

**The content of a run** is one line on the job's stdout, in `utils.logger`'s format (Cloud
Logging → Cloud Run Jobs → `merchant-purchasability-sweep`, severity INFO):

```
[2026-09-23 09:43:20,118] INFO - merchant_purchasability_sweep: SweepReport(population=20, population_skipped_unusable=0, population_skipped_market_unknown=0, population_skipped_test_merchant=0, population_unreadable=0, population_total=74, population_never_checked=12, checked=20, positive=14, negative=2, unverifiable=4, written=20, abandoned_budget=0, errors=0, skipped_disabled=0, duration_ms=148213)
```

```sh
gcloud logging read 'resource.type="cloud_run_job"
  AND resource.labels.job_name="merchant-purchasability-sweep"
  AND textPayload:"merchant_purchasability_sweep: SweepReport("' \
  --project pivota-prod --freshness 6h --limit 10 --format 'value(timestamp,textPayload)'
```

(The sample's counts are illustrative; its shape is current. `population_skipped_market_unknown`
was added on 2026-09-26, `population_unreadable` on 2026-09-27, and `population_total` /
`population_never_checked` on 2026-09-28, and `population_skipped_test_merchant` on 2026-09-29;
the line is emitted on a run whose population could not be built too.) Read it as: `population` merchants taken this run (≤ `BATCH`) out of
`population_total` in all lanes, of which `population_never_checked` have no fact yet (see
"Capacity"); `checked` fetched; `positive / negative / unverifiable` partition `checked`;
`written` facts upserted; `abandoned_budget` not started because the budget ran out. With the
gate off the run logs `merchant_purchasability_sweep: disabled; no merchant was contacted` and
exits 0. A skipped allowlist row (a `merchant_domain` that is not a bare host name, §7's census
query; or a non-ISO-2 market) logs once per run, as a count, at WARNING on the same channel. A
skipped test merchant logs nothing: it is policy, and it shows only in its count.

**Why the line goes through `utils.logger`.** Measured 2026-09-23 on the worker:
`/__scheduler_health` showed `runs_ok=1` and Cloud Logging held **zero**
`merchant_purchasability_sweep:` lines. The report went through the module logger
(`logging.getLogger(__name__)`), and nothing in the process configures the root logger —
`middleware/structured_logging.py` configures only the `structured_logs` logger, uvicorn only
`uvicorn.*` — so root sits at Python's default WARNING and a module logger's INFO is dropped at the
logger. The only INFO that reaches prod is the `pivota` logger in `utils/logger.py` (own INFO
level, own stdout handler, `propagate=False`), which is the channel the report, the disabled line
and the skip lines use; it lands the same way from the job. **Do not "fix" a missing line by
configuring root**: that floods INFO from every module (and in `web`, changes the uvicorn
access-log redaction path, `main.install_uvicorn_access_log_redaction`). Everything else this job
logs — dial warnings, per-check errors, the lane-failure detail — stays on the module logger, where
WARNING and above still land (stderr). The same applies to `jobs/reap_agentic_purchase_poll.py`'s
`reap_agentic_poll: PollReport(...)` line, which had 155 ok runs and zero lines the same day.

A test cannot see this through `caplog`, which hangs its handler on root and turns the level
down: `tests/pivota_log_capture.py` reads the pivota handler's own stream with root pinned at
WARNING; the `*_lands_on_pivota_stdout_*` and `*_does_not_depend_on_the_root_logger` tests in
both job suites fail on a module-logger emit (4 of 5 sweep tests and all 3 poller tests on a plain
revert, measured).

### The coverage census: which hosts have a fact (2026-09-28)

Before #2411's fail-closed cart gate is enabled on real data, every host the cart minter builds
on needs a measured fact. After two or three days of hourly sweeps, read it per host:

```sh
# from a worktree of origin/main; <tag> = any main sha at or after this PR (prod web's is fine)
scripts/ops/merchant_purchasability_census.sh <tag>
```

* **CPU-gated.** It first reads pivota-pg's `database/cpu/utilization` for the last 10 minutes
  and refuses (exit 2, no job created) if any minute peaked at ≥ 35 %, or if the metric cannot be
  read. Re-run when the primary is quieter; never lower `MAX_CPU` to get past it.
* **Read-only at the server.** `scripts/merchant_purchasability_census.py` runs on one connection
  inside one transaction opened `SET TRANSACTION READ ONLY`, and refuses to read anything unless
  `SHOW transaction_read_only` answers `on`. `statement_timeout` is 30 s. It contacts no merchant
  (default subnet).
* **The sweep's own population.** It calls `collect_population`, the one function the sweep
  builds its population with, so it reports the lanes each key came from and the cart-mint lane's
  seed counts.
* **Per (host, market), one of four states**, read from the buyer vantage:
  `positive` (the gate keeps the cart), `confirmed_negative` (the last check was the store saying
  no, e.g. no card), `unverifiable` (checked, never positive or expired, last check not a
  confirmed negative: blocked, transport, variant unavailable…), `never_checked` (no row). Only
  `positive` opens a cart. The summary gives counts over all keys, over the cart-mint keys (what
  #2411 gates) and their seed counts, and per host in its best state.
* **Output** lands in a temp dir (`census.txt`, `census.json`). Cloud Logging drops lines, so
  every line is fenced and numbered and the decoder refuses an incomplete log. Exit 3 means the
  report is complete but a population lane was unreadable or incomplete, so it under-counts.

Enable #2411 on real data only when the cart-mint keys have no `never_checked`. Every
`unverifiable` or `confirmed_negative` host will lose its carts under the gate — that is the gate
working, but look at the list before arming it.

### Gateway (PIVOTA-Agent) change

The per-merchant checkout tier is decided in the gateway repo, **not here**: the tier this backend
can see (`routes/store_audit_ops.py::checkout_tier_coverage`) is counts-only, and
`ready_for_complete` does not exist in this repo at all. The gateway change is therefore a
separate PR in **PIVOTA-Agent**, and until it lands the gate protects the Reap rail only.

**File:** ~~`services/ucpStoreAuditProbe.js`~~ — as merged (PIVOTA-Agent #2259) it is
`src/services/merchantPurchasabilityClient.js`, a module of its own rather than an edit to the
probe, so the gate has no dependency on the store-audit lane it sits beside.

**Contract:**

* Call `GET /ops/merchant-purchasability?domain=<domain>&market=<ISO-2>` on the backend.
* **Auth:** ~~the same ops credential the gateway already uses for its store-audit reads~~.
  **SURVEYED AND WRONG: there is no such caller.** The gateway calls no backend `/ops/...` route,
  holds no admin JWT and has no code that mints one. What shipped in PIVOTA-Agent #2259 was a
  standing admin JWT in `PIVOTA_OPS_ADMIN_TOKEN`, and §10 is the follow-up that replaced it.
  **Still true and still load-bearing:** this is a Bearer JWT whose `role` is `admin` or
  `super_admin` — NOT an `X-ADMIN-KEY` header. `utils/auth.py` also exports
  `require_admin_or_key`, which does accept `ADMIN_API_KEY` / `PROMOTIONS_ADMIN_KEY`; these ops
  routes deliberately do not use it, so a gateway reaching for the header will get a 401 and it
  will look like a routing problem.
* **Act on `tier` ONLY when `enforced` is `true`.** This is the whole reason `enforced` is in the
  response. With enforcement off every merchant reads `browse_only` — because no fact is being
  enforced, not because the merchant is browse-only — so a gateway that consumed `tier` alone
  would take the entire catalogue browse-only on the day the field shipped. When `enforced` is
  false, log and keep the previous behaviour.
* **Cache for at most 5 minutes** per `(domain, market)`. The fact changes at sweep cadence
  (hourly by default), so a short cache costs nothing and a long one delays a demotion.
* **Fail OPEN to the previous behaviour on transport error, timeout or a non-200.** The backend
  fails CLOSED — `is_purchasable` returns False when the database will not answer, because a
  payment gate that cannot prove payment must not permit it — and the gateway must **not**
  double-fail. Two independent fail-closed layers turn one backend blip into a catalogue-wide
  outage; one is the guarantee, two is an incident.
* `sweep_enabled` is also in the response, for diagnostics: `sweep_enabled: false` with
  `enforced: true` is the misordered state above and is worth logging loudly.

**The click lane sends `market` when it was OBSERVED; a click with no observed market is not
gated.** The warm-handoff body this backend POSTs to the gateway's
`POST /internal/ucp/warm-handoff/resolve` (`services/outbound_warm_handoff.resolve_warm_handoff`)
carries an optional `market` — the ISO-2 market the click itself was served for, read from the
signed `/r` token. Two conditions must both hold, and the decision is made at the sink
(`warm_market_decision`) so no caller can widen it:

1. **the token says the market was OBSERVED** (`market_observed: true`), and
2. **it validates as `^[A-Z]{2}$`** after upper-casing (`iso2_market`).

**Why two and not one.** All five `/r` minters default an unknown market to `"US"` —
`normalize_market`, `market_hint or "US"`, `body.market or "US"`,
`DEFAULT_EXTERNAL_SEED_MARKET`. That default is load-bearing for *serving* (it picks the
`outbound_link_rules` row, the domain allowlist, the `{{market}}` in the UTM campaign and the
`market` column on the click event) and is unchanged. But it is a **placeholder, not a fact about
the buyer**, and it was inert only while nothing keyed on it. Forwarding it to this gate would
judge a Japanese buyer against the **US** fact — the flowerbeauty false positive relocated from
"no market" to "**wrong** market", which is worse, because a wrong answer looks like an answer.
So each minter stamps `market_observed: true` on the token payload **only** when the market came
from the caller / the buyer's request (`services/outbound_links_service.market_is_observed`) —
**never from the seed row** since 2026-09-26 (see "Market is never defaulted" below) — and
nothing is ever substituted: not this process's egress country, not `SEED_MARKET`, not the
gateway's `primaryMarket()`.

When the market is not forwarded the body carries **no `market` key**, the gateway logs
`merchant_purchasability_unkeyable`, and that handoff keeps its pre-gate behaviour — an un-gated
click, by design. Those clicks are counted, not lost: the click event ctx carries `warm_market`
alongside `handoff` / `warm_reason`, holding the ISO-2 code, or `none_unobserved` (a defaulted
market — or a token minted before the flag existed), or `none_invalid` (a market *was* named and
is not ISO-2). Two reasons, because they need different fixes: `none_unobserved` is a minter that
never learned the buyer's market, `none_invalid` is a caller sending a bad code.

**Rollout note.** Every `/r` token minted before this change carries no `market_observed`, so it
reads as unobserved and the gate stays inert for it until it ages out on its own 7-day TTL.
Expect `warm_market=none_unobserved` to dominate for the first week and then fall as links turn
over; if it does *not* fall, a minter is not learning the buyer's market and that is the thing to
fix — never the gate.

### Market is never defaulted

**An unknown market is unknown.** On every path that feeds this gate, an attribution key or a
purchase decision, a market that was never known is carried as NULL / absent — never as `"US"`,
never as a truncation (`"USA"` is not `"US"`), and never as the market a seed or catalog row is
*listed* in. **This backend then makes no purchasability claim for it** — `is_purchasable` is
False, the ops route answers `market_unknown`, and the warm-handoff sink sends the gateway **no
`market` key**.

**WHAT HAPPENS NEXT IS THE GATEWAY'S DECISION, AND TODAY IT FAILS OPEN.** Not "a purchase is never
offered". The warm-handoff sink omits the market for such a click
(`services/outbound_warm_handoff.resolve_warm_handoff` via `warm_market_decision`), the gateway's
`merchantPurchasabilityClient` logs `merchant_purchasability_unkeyable` and returns
`{offer: true, source: 'failed'}` — the previous behaviour — and **the warm cart is built**. The
consequence of this change, stated plainly: market-less seed-lane clicks that main stamped as
"observed US" (and that the gateway gate therefore judged against the US fact) are now UNKEYABLE,
so once the gateway gate is armed they fail open into a purchase offer instead of being judged
against a market that was never the buyer's. That trades a wrong answer for no answer; it is only
safe once the gateway treats "enforced AND unkeyable" as browse-only (PIVOTA-Agent #2276), which
is why step 9 of the arming order holds `MERCHANT_PURCHASABILITY_GATE_ENABLED` until that change
is merged and deployed.
Until then, the Reap rail is unaffected (its market is the buyer's shipping country, validated,
never defaulted), and browse / links-out are untouched either way.

* **One helper.** `utils/market_code.iso2_market` is the only normaliser: strip, upper-case,
  `^[A-Z]{2}$`, else `None`. `db/merchant_purchasability.normalize_market`,
  `services/outbound_warm_handoff.click_market`, `services/outbound_links_service.iso2_market` and
  the name `iso2_market` in `services/tierb_cart_link_merchants` are the **same function object**
  (identity-asserted in `tests/test_purchase_gate_market_not_defaulted.py`). Tier B's raising
  `normalize_market` and its `parse_merchants` (which additionally demands canonical spelling:
  the helper must return the value unchanged) both call it, as does the Reap rail's
  shipping-country check. Until 2026-09-26 the fact store carried its own rule,
  `str(v or "").strip().upper()[:2]`, which read `"USA"` as `"US"`.
* **Consumers.** `is_purchasable`, `get_fact` and `list_facts` answer False / None / `[]` for an
  unusable market **before** touching the database and **without a log line** (it is an expected
  input, not an error — logging it would storm). `record_check` refuses the write. The ops route
  accepts **any** `market` string (or none) and puts it through the helper: absent, `""`,
  `"USA"`, `"U1"` answer `200` with `tier: "browse_only"`, `reason: "market_unknown"`,
  `market: null`, `facts: []`; `" us"` is `US`. A request that names a valid market gets exactly
  the previous body plus `reason: null`. The sweep skips an allowlist row whose market is not
  ISO-2 and counts it in `SweepReport.population_skipped_market_unknown`, one warning per run
  (both allowlist tables CHECK their market column, so expect 0).
* **Producers.** Every `/r` mint site decides `market_observed` through ONE function,
  `services/outbound_links_service.request_market_observed(request_market, listing_market=…,
  require_same=…)`. Its first argument is the **request's** raw market and is the only thing
  that can turn the flag on. A row's market may be passed only as `listing_market`, which can
  only turn it **off**: a row with no market serves the US fallback, so it is never observed.
  The find_products_multi lanes read the request market from `search.market` first, then
  `metadata.market` (`_request_market_for_multi`) — the gateway sends `search.market`, and
  `MultiSearchFilters` now keeps it (excluded from every dump). The prefetched lane additionally
  passes `require_same=True`: its served market is the candidate's, so a candidate listed in a
  market other than the one the request named is not observed. The seed row's `market` is
  where the card is listed (`external_product_seeds.market` is NOT NULL), and on
  `routes/agent_sdk_fixed` the seed fetch is filtered on `DEFAULT_EXTERNAL_SEED_MARKET`, so
  under the previous rule every click there was an "observed US" click — a defaulted US
  laundered through a WHERE clause. The served `market` on the token (rule row, allowlist, UTM,
  click event) is unchanged; only the provenance flag moved. A request that names its market
  mints byte-identical tokens to main except on the prefetched lane's `require_same` case
  (digests pinned in the test).
* **Known remaining gap: case (c), deliberately not fixed here.** On the other seed lanes the
  token's `market` is the *row's* listing market even when the request named a different one
  (request `SG`, row `US` → token `market=US, market_observed=true`, forwarded to the gate as the
  buyer's). It is not a minter one-liner: the token's `market` selects the outbound rule, the
  domain allowlist and the UTM **for the listing** (serving state), while the gate needs the
  **buyer's** market. Fixing it needs a separate `buyer_market` carrier on the token that the
  warm-handoff sink reads for the gate, leaving `market` to serving.

**For the gateway (PIVOTA-Agent), no change made from this repo:** its `get_checkout` re-read
carries no market today, so it cannot be keyed and keeps failing open; it must first carry a
market carrier (the buyer's shipping country, or the market the session was created for) before
the re-read can ask this route. Once it does, it may call this route with no `market` and will get
the explicit `market_unknown` answer instead of a 422.

### The offers.resolve mint (2026-09-27)

`offers.resolve` mints an `/r` link per external-seed offer, and where it can build a Shopify cart
permalink the link lands the buyer **in a prefilled cart** (`join_mode: cart_permalink`). The
warm-handoff click lane deliberately skips a cart join (`is_already_cart_join`), so nothing between
the mint and the buyer ever asked this fact — a merchant the Reap rail refuses was still handed out
as a one-click cart. Measured on prod 2026-09-27: 432 active-seed offers mint a cart, **358 of them
(47 of 53 hosts) with no positive fact in any market**.

Under `MERCHANT_PURCHASABILITY_ENFORCE` the mint (`routes/agent_shop_gateway._CartPurchasabilityGate`)
now asks first, with **this route's semantics**:

| state | cart? | DB read |
|---|---|---|
| dial off | kept — byte-identical token and payload | none |
| dial on, request names no ISO-2 market (`market_unknown`) | **declined** | none |
| dial on, `is_purchasable(cart host, request market)` true | kept | one per host per request |
| dial on, anything else (no row, expired, wrong vantage, DB error) | **declined** | one per host per request |

A decline removes the **cart**, never the offer: `execution_spec.cart_url` / `variant_id` null,
`rail: referral`, `tracking.join_mode: referral_only`, `cart_prefilled: false`, and the `/r` hop
signs the attributed PDP. The token ctx carries `purchasability_tier: browse_only`, and
`evaluate_warm_eligibility` knocks such a token out (`warm_reason=purchase_declined`) — otherwise
the warm lane would rebuild the refused cart at click time. The key is absent on every other
token.

The market is the **request's** (`payload.market`), never the seed row's listing market and never
the `or "US"` serving default. So a market-less caller (the UCP `get_offers` tool today) loses its
carts even on merchants that are positive for US — the same answer the gateway's own gate gives.

### The product-card lanes (2026-09-27)

The card lanes mint the same seeds' cart links, and now ask the same gate with the same table
above. Each builds **one gate per request on its request carrier** — the value its
`request_market_observed` reads — and decides **before** the mint, so the nulled cart id feeds
both the mint and `_seed_attribution_from_redirect` (which refuses unless its recomposed primary
equals the signed dest). A declined card has no `cart_url`, `tracking.join_mode: referral_only`,
an attributed PDP `destination_url`, and the same `purchasability_tier: browse_only` in its token.

| lane | request market | volume, 14 days to 2026-09-27 |
|---|---|---|
| `POST /attribution/external-seed-links` (`mint_external_seed_links`) | `body.market` (never `candidate.market`, which is the seed row's) | 477 calls from the gateway (464 × 200, 13 × 504) |
| find_products_multi seed cards + `_build_prefetched_external_seed_wrappers` | `_request_market_for_multi` (`search.market`, then `metadata.market`); **one gate shared by both** | 2,658 at the gateway door; 404 caller requests reached this door, **389 (96%) named no market** (`multi.invoke.market`) |
| `_attach_connected_product_redirects` | its `request_market` keyword: `_request_market_for_multi` from the find_products_multi wrapper, `metadata.market` from find_products / get_product_detail | find_products 0, get_product_detail 1 |

What that means under enforcement:

* **A market-less find_products_multi request gets no seed carts at all** — 96% of them today,
  until the UI sends `metadata.market` (agent-ui #376). With a market, only hosts with a fresh
  positive worker fact keep their cart.
* **Connected-store cards are referrals** until the sweep covers them: its population is the Reap
  variant ledger plus the Tier B cart-link allowlist, and connected stores are in neither.
  Measured 2026-09-27: 2 active connected Shopify stores (one host, 2 cached products), 0 facts.
  The connected lane's served `market` and its `market_observed` are unchanged; only the gate reads
  `request_market`.
* A link the caller hands the prefetched lane already minted (`external_redirect_url` on the
  candidate) is left as minted. The gateway's JS-built candidates carry none today.
* `tests/test_purchase_gate_market_not_defaulted.py::test_every_purchasability_gate_is_built_on_the_request_market`
  resolves every `_CartPurchasabilityGate(...)` argument back to its leaves against an exact list.

### Rolling back

**Unset `MERCHANT_PURCHASABILITY_ENFORCE`.** That alone stops every refusal:

* the Reap rail stops consulting the fact and behaves exactly as it did before WP6 — the refusal
  is behind `if purchasability.is_enforcement_enabled():` and nothing else changes;
* the checkout-tier surface stops reporting a downgrade, and the ops route's `enforced` goes
  `false`, which tells the gateway to fall back to its previous behaviour;
* the sweep **keeps running** and keeps the facts fresh, so re-arming later needs no second wait.

To stop contacting merchants as well, **disarm the job**: re-run the setup script **without**
`--enable` (gate false on the job, trigger paused). A dark job started by hand exits 0 and logs
`merchant_purchasability_sweep: disabled; no merchant was contacted` (see "Proof of a run"
above). Setting the dial on `web` or `worker` does nothing to the job. Disarming the job while
leaving `ENFORCE` on is the misordered state again — the facts age out through the TTL and
merchants silently become `browse_only` one by one.

**Rolling back the MOVE itself** (the crawl egress turns out to be worse than the payment NAT for
some merchant — the report's `unverifiable` climbs and §7 shows it): **disarm the job first**
(re-run the script without `--enable`) so the two never run together, then **revert the whole
commit** that moved it — never only its `services/audit_scheduler.py` hunk. That hunk imports
`job_interval_seconds` from the job module, which the same commit deleted: reverted alone, the
import raises inside `start_scheduler`'s one outer `try`, `_BOOT_ERROR` is set and **every** worker
job stops (the Reap poller and the catalog drains included). Then put
`MERCHANT_PURCHASABILITY_SWEEP_ENABLED=1` back on `worker`. The facts keep their windows through
the switch (same vantage name, same rows) — and remember the payment-NAT reasons it left.

Nothing needs to be un-migrated and no row needs deleting: stale facts are simply not read. The
rows stay, and they are still readable through the ops route (which is not gated on the dial), so
you can keep diagnosing a merchant with the rail disarmed.

---

## 10. The gateway's auth on this route: a Google identity token, not a standing JWT

### Why this route's app-level check is the whole guarantee

**Production `web` is deployed `--allow-unauthenticated`** (`infra/gcp/deploy_backend.sh`,
`PUBLIC=1`). Cloud Run IAM does **not** stand in front of `GET /ops/merchant-purchasability`:
anyone on the internet may reach it. The dependency in `utils/gateway_oidc_auth.py` is not
"defence in depth behind IAM", it **is** the defence, and every branch in it fails closed.

### Why the standing admin JWT had to go

`PIVOTA_OPS_ADMIN_TOKEN` is a long-lived `admin`/`super_admin` JWT pasted into the gateway's
environment. It is over-scoped (a role, for one read-only route) and — worse — **it expires
silently**. The gateway fails OPEN on any non-200 by design, so on the day that JWT lapses this
route answers 401, the gateway logs `merchant_purchasability_read_failed` once per five minutes,
and **the purchasability gate is disarmed while every dial on both sides still reads "on"**.

### What the route accepts now

`utils/auth.py` is unchanged. A sibling module adds
`require_admin_or_gateway_identity`, used on **this route and no other** (there is a test that
fails if a second route picks it up). It:

1. runs `require_admin` first and **unchanged** — same `get_current_user`, same `test-token`
   bypass, same 503-on-unusable-secret;
2. otherwise verifies the same `Authorization: Bearer` value as a **Google OIDC ID token**
   through `google.oauth2.id_token.verify_oauth2_token`, requiring ALL of: RS256 against Google's
   published certs (`alg: none` and HS256 are refused by the library's algorithm allow-list);
   `iss ∈ {accounts.google.com, https://accounts.google.com}`; `aud == OPS_GATEWAY_OIDC_AUDIENCE`
   exactly; `email_verified` is boolean **true**; `email ∈ OPS_GATEWAY_SERVICE_ACCOUNTS`
   (lower-cased compare); `exp`/`iat` inside a **10 s** clock skew.
3. **Costs an anonymous caller nothing.** google-auth does *not* cache certificates — it GETs
   `googleapis.com/oauth2/v1/certs` on every verification, *before* it parses the token — so on a
   public route any stranger could steer our outbound traffic one request at a time. The module
   therefore parses the JWS header first (size, compact form, `alg: RS256`, a `kid`) and refuses
   before any fetch; caches the document for `OPS_GATEWAY_OIDC_CERTS_TTL_SECONDS`; allows an
   unknown-`kid` refresh at most once per 60 s process-wide; caps fetches at 10 per minute
   process-wide; and hands the library a transport that replays the cached bytes and can reach
   nothing.
4. **Never blocks the event loop.** The verification runs in a threadpool under a 2 s
   `asyncio.wait_for`, because a blocking certs fetch on the loop stalls every other request this
   worker is serving — on a public route, remotely triggerable.
5. **Fails closed on everything else**, and the refusal is **byte-identical** to what a bad admin
   JWT gets — same status, same body — so nothing about this path is discoverable from a
   response. The reason is a rate-limited `warning` carrying a short CODE only. The token is
   never logged; the accepted service-account email is logged at **debug** only.

**`X-ADMIN-KEY` is still refused here**, exactly as before. Nothing widened to
`require_admin_or_key`.

### The envs, and the arming order

| where | variable | value |
|---|---|---|
| backend `web` | `OPS_GATEWAY_OIDC_AUDIENCE` | `https://api.pivota.cc` (this backend's canonical https origin; a bare origin — no path, no port, no trailing slash) |
| backend `web` | `OPS_GATEWAY_SERVICE_ACCOUNTS` | `sa-gateway@pivota-prod.iam.gserviceaccount.com` — the gateway's runtime SA, from `infra/gcp/deploy_gateway.sh` (`--service-account "sa-gateway@$PROJECT.iam.gserviceaccount.com"` with `PROJECT=pivota-prod`). Confirm against the live revision: `gcloud run services describe gateway --project pivota-prod --region us-west1 --format='value(spec.template.spec.serviceAccountName)'`. Comma-separated if more than one |
| gateway | `PIVOTA_OPS_OIDC_AUDIENCE` | **the same string**, byte for byte |
| gateway | `PIVOTA_OPS_ADMIN_TOKEN` | keep as a **dev fallback** only; may be unset in prod once the identity rail is confirmed |

**Both backend envs, or neither.** Either one alone reads as DISABLED — an audience-less or an
allow-list-less verification is an open door, so the half-configured state refuses rather than
admits. With both unset (the shipped default) this route behaves exactly as it did before.

Optional third env: `OPS_GATEWAY_OIDC_CERTS_TTL_SECONDS` (default `3600`, clamped to
`[60, 86400]`) — how long Google's signing certificates are reused. Leave it alone unless Google
rotates unusually; `0` is not reachable, because a TTL of 0 restores the unbounded-fetch
behaviour this route was fixed for.

### The audience is NORMALISED, and both sides normalise it the same way

A first review of this change found the two sides disagreeing. The gateway's `cloudRunAudience()`
lower-cases the host, drops a default `:443` and folds one trailing slash to the origin; this
side originally only `.strip()`ed. So an operator who pasted **the same string**
`https://api.pivota.cc/` into both envs got a 401 on every read — and a 401 fails open on the
gateway, so the gate disarmed **silently**. Both sides now apply the identical rule.

**The rule: a bare https ORIGIN.** https only; no userinfo; no path beyond `/`; no query; no
fragment; the host is lower-cased; a default `:443` is dropped; one trailing slash folds away.

| written into BOTH envs | what both sides use | accepted? |
|---|---|---|
| `https://api.pivota.cc` | `https://api.pivota.cc` | ✅ |
| `https://api.pivota.cc/` | `https://api.pivota.cc` | ✅ |
| `https://API.PIVOTA.CC` | `https://api.pivota.cc` | ✅ |
| `https://api.pivota.cc:443` | `https://api.pivota.cc` | ✅ |
| `http://api.pivota.cc` | — | ❌ disabled |
| `api.pivota.cc` | — | ❌ disabled |
| `foo` | — | ❌ disabled (the first cut accepted this verbatim) |
| `https://api.pivota.cc/ops` | — | ❌ disabled |
| `https://api.pivota.cc:8443` | — | ❌ disabled |

A value that was **set and refused** is DISABLED, not passed through, and it is logged once per
interval as `audience_env_invalid:<value>` — otherwise a typo in this env is indistinguishable
from never having set it, and the symptom of both is "the gate quietly does nothing".

### Rolling this back

**Unset both backend envs** (`OPS_GATEWAY_OIDC_AUDIENCE` and `OPS_GATEWAY_SERVICE_ACCOUNTS`).
The route immediately reverts to plain `require_admin`. The gateway then gets a **401** on every
read, which — by its own fail-open rule — means it **falls back to `PIVOTA_OPS_ADMIN_TOKEN` if
that is still set, and otherwise keeps its previous behaviour**. Nothing refuses a purchase
either way.

> **That is exactly why `PIVOTA_OPS_ADMIN_TOKEN` should stay set on the gateway until the
> identity rail has been observed working.** Rolling the backend back with the static token
> already removed leaves the gate reading nothing — inert, not broken, but inert silently. If
> you want the gateway to stop trying the identity rail too, unset `PIVOTA_OPS_OIDC_AUDIENCE`
> there; that is a second revision and is not needed for the backend rollback to be safe.

> **⚠️ BACKEND FIRST, THEN THE GATEWAY.** Setting the gateway's audience first means the gateway
> sends an identity token to a backend that does not yet accept one: every read 401s, and because
> the gateway fails OPEN that is **silent** — the gate disarms and every dial still reads "on".
> Backend first means the worst case is a backend accepting a token nobody sends yet, which
> changes nothing.
>
> **⚠️ THE TWO AUDIENCE STRINGS MUST MATCH BYTE FOR BYTE.** The check is `claims["aud"] != audience`,
> a string compare, not a URL compare. `https://api.pivota.cc/` and `http://api.pivota.cc` are
> different audiences and both 401 — silently, for the reason above. On the gateway side
> `cloudRunAudience()` refuses anything that is not a bare https origin, which narrows but does
> not remove this.

### Verifying it took

The backend logs the accepted service account at **debug** (`utils.gateway_oidc_auth`), and logs
a rate-limited `warning` with a reason code on refusal. On the gateway, a sustained
`merchant_purchasability_read_failed` with `failure: status_401` after step 8 means the audiences
do not match or the SA is not in the allow-list — **alert on that line specifically and treat it
as "the gate is off"**.
