# Merchant purchasability — the fact, the sweep, the gate (WP6)

`db/merchant_purchasability.py` (the rule and the only writer), `jobs/merchant_purchasability_sweep.py`
(the loop and its bounds), `services/shopify_cart_link_preflight.py` (the detector),
`routes/merchant_purchasability_ops.py` (the operator read), migration
`db/migrations/231_merchant_purchasability.sql` (the table).

This rail is **dark by default**, behind **two** dials. `MERCHANT_PURCHASABILITY_SWEEP_ENABLED`
(the job) and `MERCHANT_PURCHASABILITY_ENFORCE` (the consumers) are both unset: the scheduler job
is registered but inert, and `routes/agent_commerce_reap.py` does not consult the fact at all.

**They are two dials because the sweep runs only on the WORKER and the normal backend deploy does
not ship the worker.** One shared dial, armed on the backend, would switch the refusals on while
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
| `MERCHANT_PURCHASABILITY_SWEEP_ENABLED` | **unset = off** | truthy allowlist: `1`, `true`, `on`, `yes` (case/space-insensitive) | **DIAL 1 of 2** (`db.merchant_purchasability.is_sweep_enabled`). Gates the sweep JOB and nothing else. Off = the job contacts no merchant, which matters because every check creates an abandoned checkout on a live store. **Set this on the WORKER service** |
| `MERCHANT_PURCHASABILITY_ENFORCE` | **unset = off** | same truthy allowlist | **DIAL 2 of 2** (`db.merchant_purchasability.is_enforcement_enabled`). Gates the CONSUMERS and nothing else: the Reap route's `merchant_not_purchasable` refusal and the checkout tier's downgrade. Off = a missing fact refuses nothing. Also the `enforced` field the gateway reads |
| `MERCHANT_PURCHASABILITY_TTL_HOURS` | `72` | `1`–`720` | how long one positive fact stays positive. 720 h is 30 days; past that "fresh" is not a word that means anything |
| `MERCHANT_PURCHASABILITY_BUYER_VANTAGE` | `worker` | any string, truncated to 32 chars | the vantage `is_purchasable` demands a positive fact **FROM**. See §6 — this is the dial that decides whose question the gate is answering |
| `VANTAGE_PROXY_URL` | **unset** | must start `http://` or `https://`, else ignored | when set, every merchant is ALSO checked through that proxy and recorded under vantage `proxy`. Anything else is ignored rather than handed to httpx, which would raise inside the run |
| `MERCHANT_PURCHASABILITY_INTERVAL_SECONDS` | `3600` | `60`–`86400` | the scheduler `interval` trigger **and** `misfire_grace_time`. **Registration-time only** — a change needs a restart. Hourly against a 72 h TTL is 72 chances to refresh a fact before it expires |
| `MERCHANT_PURCHASABILITY_BATCH` | `20` | `1`–`200` | merchants per run. Each is a full redirect chain plus up to 20 catalog pages, and each leaves an abandoned checkout behind — this is a politeness bound as much as a time bound |
| `MERCHANT_PURCHASABILITY_BUDGET_SECONDS` | `600` | `30`–`3600` | wall-clock budget for one run. It stops the job **STARTING** a new merchant; one already in flight runs to completion, so a run can exceed this by one merchant's worth of fetches. The scheduler's run deadline for `merchant_purchasability_sweep` is **900 s** |
| `MERCHANT_PURCHASABILITY_PAUSE_MS` | `1500` | `0`–`60000` | seconds (in ms) to wait between merchants. One store at a time, unhurried: this rail has no latency requirement and a burst of checkout creations against one platform does not help us |

Two things are deliberately **not** dials: `DEMOTE_AFTER_FAILURES` (2) and the card-brand set.

**Why the dial is split.** It was one dial, and one was a bug.
`services.audit_scheduler._add_job` registers every job only when
`_queue_worker_enabled()` is true — prod and staging share one Postgres, so only the production
worker may run singleton crons — and **the normal backend deploy does not ship the worker** (see
`project_scheduler_lane_runs_on_undeployed_worker_2026_09_02`). A single dial set on the backend
would therefore arm the refusals while the sweep that feeds them never ran anywhere:
`is_purchasable` finds no fact for any merchant, and the Reap rail answers a permanent 409
`merchant_not_purchasable` for the entire catalogue until somebody unsets the variable again.

---

## 6. Vantage — READ THIS BEFORE ARMING

**Reachability is EGRESS-DEPENDENT, and that was measured, not assumed.**

* **judydoll.com RESET direct TCP connections from one of our egresses (3/3) while answering
  through another (3/3).**
* **A human could not open flowerbeauty.com from his browser at all, while our machine could.**

A fact gathered from the worker's egress is a fact about **the worker's egress**. That is why
`vantage` is part of the primary key rather than a label, and why `is_purchasable` requires a
positive fact **from the vantage named by `MERCHANT_PURCHASABILITY_BUYER_VANTAGE`** (default
`worker`). A positive fact from any OTHER vantage is evidence for a human, never permission for the
door.

> **THE BUYER VANTAGE MUST MATCH THE BUYER/PARTNER EGRESS**, or the gate is answering a question
> nobody asked. The default `worker` is honest — it names where we actually looked — but it is not
> the buyer's egress unless your buyer pays from the worker's network. Set it, and configure that
> vantage, before you arm the rail.

Setting `VANTAGE_PROXY_URL` adds a second vantage, `proxy`, recorded under the same
(domain, market) key. **It costs one more abandoned checkout per merchant per run** — that is the
whole price, and it is why the proxy vantage is opt-in rather than always on.

`GET /ops/merchant-purchasability` reports `buyer_vantage` next to the rows for exactly this
reason: a reader who looks only at `card_available` will be misled by a positive fact from the
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

```sql
-- NEVER CHECKED. The population is the UNION of the two Reap allowlists at merchant grain;
-- a merchant absent from both cannot be bought from, so a fact about it would gate nothing.
WITH population AS (
    SELECT lower(merchant_domain) AS domain, upper(market_country) AS market
      FROM reap_agentic_eligibility
     WHERE product_key = '' AND variant_key = '' AND enabled = TRUE
    UNION
    SELECT lower(shop_domain) AS domain, upper(market) AS market
      FROM tierb_cart_link_eligibility
     WHERE verdict = 'ELIGIBLE'
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
leading `www.`; `normalize_market` uppercases to two characters), so a population row spelled
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
  the next tick picks it first. **It also deletes the positive window immediately**: until that
  tick completes, `is_purchasable` answers False and — with the dial on — the Reap rail refuses
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

The sweep runs **only on the worker service** — `_add_job` registers nothing unless
`services.audit_scheduler._queue_worker_enabled()` is true, because prod and staging share one
Postgres. The normal backend deploy does not ship the worker; see
`project_scheduler_lane_runs_on_undeployed_worker_2026_09_02`. **This is why there are two dials,
and it is why their order is not negotiable.**

### The arming order

1. **Deploy dark.** Both dials unset. The job touches no merchant and
   `routes/agent_commerce_reap.py` does not consult the fact.
2. **Set the vantage first.** `MERCHANT_PURCHASABILITY_BUYER_VANTAGE` — and, if that is not the
   worker's own egress, `VANTAGE_PROXY_URL` — **before** either dial. See §6. Arming with the
   wrong vantage gates on a fact about a network the buyer does not pay from.
3. **`MERCHANT_PURCHASABILITY_SWEEP_ENABLED=1`, ON THE WORKER.** Nothing is refused yet. The job
   begins gathering facts on its next tick; no redeploy and no scheduler restart.
4. **Wait for one full pass over the population.** At the defaults that is
   `ceil(population / MERCHANT_PURCHASABILITY_BATCH)` ticks, i.e. `ceil(population / 20)` hours
   at the hourly interval. Watch the sweep's counts-only report:
   `population / checked / positive / negative / unverifiable / written / abandoned_budget /
   errors / skipped_disabled / duration_ms`. A pass is complete when `checked` has covered
   `population` across ticks. **`errors` is the only count that should page anyone**; a high
   `unverifiable` is not an error, it is the egress telling you something, and §6 is where to look.
5. **Verify coverage merchant by merchant** through
   `GET /ops/merchant-purchasability?domain=…&market=…`. Every merchant you expect to be
   purchasable must read `"tier": "purchase"`. If one stays `browse_only`, the response's `note`
   names the three candidate reasons — a positive row under a DIFFERENT vantage, an expired
   window, or two consecutive negatives — and §7's demotion query tells you which. **Do not skip
   this step:** it is the only thing standing between step 6 and a 409 on a live merchant.
6. **`MERCHANT_PURCHASABILITY_ENFORCE=1`.** Only now does a missing fact refuse a purchase.

7. **AUTH: the two OIDC envs, ON THE BACKEND FIRST** — `OPS_GATEWAY_OIDC_AUDIENCE` and
   `OPS_GATEWAY_SERVICE_ACCOUNTS`. Both or neither. See §10.
8. **THEN the gateway's `PIVOTA_OPS_OIDC_AUDIENCE`,** the same string byte for byte. See §10.

> **Arming these in the other order is the outage.** With `ENFORCE` on and no facts gathered,
> every merchant reads `browse_only` and the Reap rail refuses `merchant_not_purchasable` (409)
> for all of them. Because the job is worker-only, setting `SWEEP_ENABLED` on the backend does
> not fix it — the sweep is not running there at all.
>
> Steps 7–8 are independent of 1–6 and may be done at any point, but the **order between them**
> is not optional, and for the same reason in reverse: gateway-first is silent. See §10.

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
from the caller / request / seed row (`services/outbound_links_service.market_is_observed`), and
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

### Rolling back

**Unset `MERCHANT_PURCHASABILITY_ENFORCE`.** That alone stops every refusal:

* the Reap rail stops consulting the fact and behaves exactly as it did before WP6 — the refusal
  is behind `if purchasability.is_enforcement_enabled():` and nothing else changes;
* the checkout-tier surface stops reporting a downgrade, and the ops route's `enforced` goes
  `false`, which tells the gateway to fall back to its previous behaviour;
* the sweep **keeps running** and keeps the facts fresh, so re-arming later needs no second wait.

Unset `MERCHANT_PURCHASABILITY_SWEEP_ENABLED` as well to stop contacting merchants; the sweep then
returns `skipped_disabled=1`. Unsetting only the sweep dial while leaving `ENFORCE` on is the
misordered state again — the facts age out through the TTL and merchants silently become
`browse_only` one by one.

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
