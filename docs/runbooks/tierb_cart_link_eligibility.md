# Tier B cart-link eligibility (daily job)

Records which Shopify merchants on our Tier B list currently accept the cart-permalink checkout
(`https://{shop}/cart/{variant}:1?attributes[pivota_click_id]=...`). One row per
`(shop_domain, market)` in `tierb_cart_link_eligibility` (migration 228). **Dark**: nothing reads
the table yet.

| Piece | Where |
|---|---|
| Merchant list (40 rows) | `config/tierb_cart_link_merchants.json`, validated by `services/tierb_cart_link_merchants.py` |
| Job | `python -m jobs.tierb_cart_link_eligibility` |
| Storage + read API | `db/tierb_cart_link_eligibility.py` (`record_result`, `get_eligibility`, `is_cart_link_eligible`) |
| Schema | `db/migrations/228_*` and the self-heal `db/tierb_cart_link_eligibility_schema.py` |
| Provisioning | `infra/gcp/setup_tierb_cart_link_eligibility_job.sh` |
| The check itself | `services/shopify_cart_link_preflight.py` (PR #2209) |

## The egress rule (read this first)

**This job must only ever run from the crawl subnet `pivota-crawl` (NAT 34.82.199.35).** Never
from `worker`, `web` or any job on the `default` subnet: their egress is 8.231.167.230, **the
address payment partners allowlist**. NAT port exhaustion is per-IP, and ~50 requests over 37
Cloudflare-fronted domains in ~1 minute once tripped a cross-domain, IP-level 429 for ~15
minutes. That is why this is a standalone Cloud Run Job and **not** an `audit_scheduler` entry.

Inside the job: request starts are ≥ 1.5 s apart across all merchants, at most 3 merchants are in
flight, and a 20-minute budget stops new requests.

## The side effect

Every run creates **one abandoned Shopify checkout per merchant** (two for a merchant whose first
attempt hit a transport error and was retried). The checkout carries our click id
(`clk_tierbelig_<UTC stamp>_<domain>`) and **no buyer data**: the job never passes a buyer, so
no email, name or address is ever in the link. Nothing is paid; no payment step is reached. A
merchant may see these in their abandoned-checkout list.

## Arm / disarm

```sh
# create or update, DARK (gate false, trigger paused) — the default:
infra/gcp/setup_tierb_cart_link_eligibility_job.sh prod <backend-tag>

# arm (gate true on the job, trigger resumed; runs daily at 03:30 UTC):
infra/gcp/setup_tierb_cart_link_eligibility_job.sh prod <backend-tag> --enable
```

Re-running **without** `--enable` disarms. The gate `TIERB_CART_LINK_ELIGIBILITY_ENABLED` is also
checked inside the job: with it off, a run logs a WARNING, contacts nobody and exits 0.

The backend image must contain this code, and `web` must have started on it at least once (its
startup self-heal creates the table; the job also runs the same idempotent `CREATE TABLE IF NOT
EXISTS` before writing).

## Run once by hand

A dry run from the crawl subnet (prints verdicts, writes nothing — the DB secret is not even
needed, but the runner mounts it by default):

```sh
SUBNET=pivota-crawl \
ENV_VARS=PIVOTA_ENV=production,TIERB_CART_LINK_ELIGIBILITY_ENABLED=true \
TASK_TIMEOUT=1800s \
  scripts/ops/run_oneoff_job.sh -m jobs.tierb_cart_link_eligibility --dry-run
```

A recording run (writes to production):

```sh
SUBNET=pivota-crawl \
ENV_VARS=PIVOTA_ENV=production,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600,TIERB_CART_LINK_ELIGIBILITY_ENABLED=true \
TASK_TIMEOUT=1800s \
  scripts/ops/run_oneoff_job.sh -m jobs.tierb_cart_link_eligibility
```

`SUBNET=pivota-crawl` is **not optional**: the runner's default is `default`, the payment
address. Restrict to some merchants with `--only judydoll.com` (repeatable; a domain not on the
list is refused). Or execute the provisioned job: `gcloud run jobs execute
tierb-cart-link-eligibility --region us-west1 --project pivota-prod --wait` (only does anything
when armed).

## Reading the output

One line per merchant, then a `summary {...}` JSON line:

```
judydoll.com  US  ELIGIBLE  retryable=n attempts=1 variant=50041364447509(caller) final=200 landing=https://judydoll.com/checkouts/cn/<token>/... recorded=Y
```

Exit code (the verdict of the run; non-zero fails the execution and trips the "Cloud Run job
failing" alert):

| Code | Meaning | Do |
|---|---|---|
| 0 | done; at most a quarter of merchants indefinite | nothing |
| 1 | more than a quarter ended INDEFINITE | suspect the crawl address (blocked / 429) or a Shopify change; look at `last_error_code` |
| 2 | the merchant list is invalid; nothing attempted | fix `config/tierb_cart_link_merchants.json` |
| 3 | the budget ran out; some merchants have no result this run | their old rows are untouched; check pacing vs list size |
| 4 | a DB write failed or the preflight crashed | read the job log |

`--max-retries 0`: a failed execution is not re-run automatically (a re-run re-creates every
checkout).

## Reading the table

```sql
SELECT shop_domain, market, verdict, checked_at, consecutive_same, previous_verdict,
       verdict_changed_at, last_attempt_at, last_error_code
  FROM tierb_cart_link_eligibility ORDER BY verdict NULLS FIRST, shop_domain;
```

- `verdict` is the last **definite** verdict (ELIGIBLE, LOGIN_REQUIRED, NOT_ACCEPTING_ORDERS,
  VARIANT_GONE, VARIANT_UNAVAILABLE, PASSWORD_PAGE, BLOCKED_UNKNOWN, CHECKOUT_PREFILL_MISSING,
  CHECKOUT_MARKET_MISMATCH)
  and `checked_at` is when it was observed.
- An **indefinite** result (TRANSPORT_ERROR, VARIANT_UNVERIFIED, UNCLASSIFIED, INVALID_INPUT)
  never overwrites a verdict: it moves only `last_attempt_at` and `last_error_code`. So
  `last_attempt_at > checked_at` with a `last_error_code` means "the last run could not tell; this
  is the older verdict". `verdict IS NULL` means no run has ever produced a definite verdict.
- `is_cart_link_eligible(domain, market)` is true only for ELIGIBLE with `checked_at` within 48 h.
  Two missed days turn a merchant off on their own.

**ELIGIBLE does not prove shipping.** The checkout loads shipping rates with JavaScript, so an
HTTP check cannot see them: heartpercent.us (no US delivery) and anua.us / skin1004.com (qty 1
under a basket minimum) are ELIGIBLE here. On the Reap path, Reap's quote is the shipping proof.

**VARIANT_GONE / VARIANT_UNAVAILABLE are about the listed variant**, which the job confirms and
never substitutes. If a merchant flips to one of these, the fix is a new `variant_id` in the list
(or removing the hint so the preflight picks a representative variant), not the merchant.
