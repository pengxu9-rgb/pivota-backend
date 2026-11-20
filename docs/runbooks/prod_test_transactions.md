# Prod test transactions

A fixed list of test calls against the **prod gateway**, made by a dedicated first-party test agent. It's the one sanctioned exception to the no-prod-probe rule:
- only this job (`prod-test-transactions`);
- only its keys;
- only on the owner's go, given for each run.

No partner key is ever used, and nothing is fetched from a laptop.

- **Job:** `scripts/ops/prod_test_transactions.py`
- **Launcher:** `scripts/ops/prod_test_transactions_launch.py`, run from `infra/gcp/cloudbuild.prod_test_transactions.yaml`.
- **Review:** `reports/prod_test_transactions_review_2026_11_19.md` (v1 + v2) found no path to payment.

## Stop point

The job never submits a payment. Payment submission is **enabled** in prod (`AGENT_CHECKOUT_STRICT_SUBMIT_PAYMENT_ENABLED=1`), so the job's request guard is what stops it. Before anything is sent, the **whole** body must pass two checks:

1. **Its call's own key schema** (`CALL_SCHEMAS`):
   - invoke `offers.resolve`;
   - `/mcp` `create_checkout_session`, `get_checkout_session` and `cancel_checkout_session`;
   - `/ucp/mcp` `create_checkout` and `get_checkout`.
2. **A normalised substring denylist** (`DENY_FRAGMENTS`):
   - It matches `pay`, `card`, `token`, `credential`, `instrument`, `mandate`, `authoriz`, `reap`, `cvv`/`cvc`, `wallet`, `address`, `email`, `order`, `confirm`, `submit`, `complete` and more.
   - Matching ignores case, and `_` and `-`.
   - There are exactly three exceptions, each an exact path: `payload` (invoke), `quote.customer_email` and `checkout.context.address_country`.

On top of that:
- invoke `metadata` must be empty;
- MCP `params` carries only `name` and `arguments`.

## The private targets

Every id the job touches lives in a **private** reviewed input, never in this public repository: the Secret Manager JSON `prod-test-transactions-targets`.
- It is mounted by version at `/var/run/pivota-test-targets/targets.json`.
- The job checks its sha256 against `TARGETS_SHA256`. That constant is filled in **review**, and the sha is the only part that is public.
- Receipts carry the sha, and the hash of the seller host; never an id.

```json
{
  "offers_product_id": "<a connected test merchant's product, with offers in US and SG>",
  "kernel_test_merchant": {"merchant_id": "<TEST_MERCHANT_IDS member>", "product_id": "…", "variant_id": "…"},
  "storefront_seller": {"merchant_id": "merch_obs_…", "product_id": "…", "variant_id": "…",
                        "host": "<the store's host, lower case>"},
  "buyer_email": "<a test buyer mailbox Pivota owns>",
  "user_token": {"issuer": "https://…", "audience": "…", "kid": "…", "azp": "…", "scope": "…"}
}
```

The job refuses the targets if:
- the sha differs;
- a member is missing or extra;
- the storefront seller is not `merch_obs_…`;
- the host is not a plain lower-case host;
- the issuer is not https.

## Decisions (owner, 2026-11-19, in the flip session)

### End user: a test issuer bound to the test agent only ("JWT")

For each case the job signs a 5-minute ES256 `X-Agent-User-JWT`:
- **claims:** `iss`, `aud`, `azp`, `scope`, `sub=ptt-test-buyer`, `iat`, `nbf`, `exp`, a fresh `jti`, and `acp_session_id=ptt-<run id>-<case id>`;
- **header:** `kid`;
- **no `email` claim**, so a leaked key cannot mint attested buyer data.

A case's create, read-back and cancel all carry one token, so they share one buyer and one session. The signing key, EC P-256, is `prod-test-agent-user-key`. It is mounted by version only while the job is armed, and only when a selected case needs it.

### Buyer email in the `/mcp` quote ("quote.customer_email")

`/mcp` runs buyer intake before any market check, so a create with no email is refused with `QUOTE_REQUIRED/acp_buyer_email_required`. The `/mcp` cases send the reviewed `buyer_email` as `quote.customer_email`. `/ucp/mcp` cases send no email.

### Issuer binding toggled per run ("You toggle it per run")

The owner **enables** the test issuer binding in the developer portal before each go, and **disables** it (`DELETE …/identity-issuers/{id}`) after the run. The binding is registered with `authorized_party` = the token's `azp` and `required_scopes` = its `scope`. Between runs, even a leaked signing key plus the test agent key cannot act as a buyer.

**The disable is not instant.** The gateway's issuer registry refreshes on its own TTL: about **60 s** normally, and up to **15 min** while the backend is unreachable (it serves a stale map). After disabling:
1. Wait out that window.
2. Confirm the binding is gone: the portal lists it as disabled, and the internal registry no longer returns it.

Only then count the issuer as off.

**`jti` gives no replay protection.** The gateway keeps no `jti` store; the token carries one for audit only. A token stays valid until its `exp` (5 min), so do not rely on `jti`. The bound is the short `exp` plus the binding toggle.

### Storefront carts expire ("accept expiry")

A storefront checkout (`esc_…`) cannot be cancelled: the gateway refuses by design, and the seller's cart expires unpaid. Such ids are:
- never cancel-called;
- listed under `expires_on_seller` as `esc_#<sha256 prefix>`, because the id carries the seller's cart id;
- not a failure.

Every other checkout that **may** exist must read back `canceled`, or it is `left_open` and the build fails. This includes a create whose answer was a 5xx, an unreadable 200, or a 200 with no visible id, all reported as `unknown:<case>`. It also includes a checkout case with no result line.

### Live carts approved, cart-referral OFF ("Approve, referral off")

**Carts per full run:** 3 live, unpaid carts on the **one reviewed store**:
- MCP-SF-US
- UCP-SF-US
- UCP-SF-none (the assumed-US cart)

Each also gets one re-read. SG, DE, no market (on `/mcp`) and USA are refused before any store call.

**The first-run order adds 2 carts** (MCP-SF-US and UCP-SF-US run first), so at most 5 across the order.

**Store check:**
- Each storefront create asserts that its `continue_url` host equals the reviewed `storefront_seller.host`. A mismatch fails the case; that cart still expires and is listed.
- The result keeps only a true/false, never the host.

**Cart-referral:** every storefront case refuses unless `AGENT_CHECKOUT_UCP_CART_REFERRAL_ENABLED` is off, so no test click reaches attribution.

## What one run does

1. The build copies the scripts from the release image, pinned by digest. It checks that the config is the release copy, and that the digest is the release tag's.
2. **Gateway check.** The gateway must serve one revision at 100%. Each case's required flags must match the live plain values. The path-4 cases need the serving revision's **image digest** in `REVIEWED_GATEWAY_DIGESTS`: it is read from `revisions describe` `status.imageDigest`, not from the `PIVOTA_COMMIT_SHA` env, which a `deploy --image` can leave stale. Today that set holds `080d7297f` → `sha256:67a5940f…` and `7d650dce8` → `sha256:908df3e4…`. A later gateway image is added by a reviewed PR.
3. **Lock.** `gs://pivota-prod-ops-locks/prod-test-transactions.lock`, create-only.
4. **Run:**
   1. `oneoff_gate.py` must print `GATE_OPEN`. The job accepts a gate reading up to 300 s old.
   2. **Arm.** These are mounted, each by numbered version:
      - the key;
      - the targets;
      - the issuer key, only for checkout cases.

      The gate, the cases and the run id are set.
   3. The job is executed, and the launcher waits for it by name for **at most 840 s**, counted from the run step's own start; the task itself times out at 600 s. No poll starts unless its sleep and a full 60 s describe still fit before the deadline. 495 s stay reserved for the cancel call, its 120 s terminal poll (plus one interval) and the disarm, inside the 2100 s step. If the wait runs out, the launcher **cancels** the execution. The run is then not ok.
   4. **Disarm, on every exit path.** The job is redeployed with `--clear-secrets` and no gate or cases. This happens whatever happened before: a failed execute, an unfound execution, a lookup or wait error, or a cancel that never reached a terminal state. A running execution keeps what it already mounted, so the disarm only stops a NEW execution from mounting the keys. Whether the lock is released is decided separately: an unidentified or non-terminal execution keeps it HELD.
5. **Publish.** `log.txt`, `rows.json` and `result.json` are written, create-only, to `gs://pivota-prod-ops-receipts/prod-test-transactions/<build id>/`.
   - The lock is released only when nothing can still run and the job is disarmed.
   - The build fails, naming them, if anything may be **left open**.
   - **The shadow lookup (path 3) is advisory.** The gateway's line carries no request id or caller, and the job publishes no seller host, not even a hash (an unsalted host hash is reversible). So the check counts **every** shadow line in the run window, plus 3 minutes, and never decides `ok`. A small gateway PR that adds the caller `agent_id` or a request id to the line would make it exact.

**Limits:**
- 40 calls per run;
- 3 calls reserved before every create (read back, cancel, read back);
- a **total** 45 s deadline per call, because the MCP heartbeat would hold a per-read timeout open. A cold storefront create reaches the seller's cart API. If one times out, its case is `unknown:<case>`, so expect a re-run of that case;
- no retries, no redirects, `trust_env=False`;
- `gateway.pivota.cc` only. Never `mcp.pivota.cc`, the public-read policy host.

**Egress.** The job has **no** VPC egress: it does not use the default subnet's NAT, which is the payment-allowlisted IP. Its calls are attributed by the test agent's own key.

## Cases

| Case | Door | Market | Expected |
|---|---|---|---|
| OR-US, OR-us-lower, OR-none | invoke `offers.resolve` | US / us / (none) | `status=success`, `offers_count ≥ 1` |
| ~~OR-SG~~, ~~OR-USA-beside-SG~~ (**disabled**) | invoke `offers.resolve` | SG / USA+SG | Served in SG needs an SGD source for the reviewed product. The 2026-11-19 census found none (USD only), so both are listed for review only until a reviewed SGD source exists. |
| OR-USA, OR-long-s | invoke `offers.resolve` | USA / ſg | `status=error`, `MARKET_INVALID` / `market_invalid` |
| OR-DE | invoke `offers.resolve` | DE | `status=error`, `MARKET_NOT_PRICED` / `market_not_priced` |
| MCP-SF-US | `/mcp` storefront | US | `completes_on_seller_storefront`, USD, minor, reviewed host; the cart expires; shadow lookup |
| MCP-SF-SG / DE / none / USA | `/mcp` storefront | SG / DE / – / USA | `NO_MERCHANT_OFFER/seller_not_priced_in_market_currency` / `NO_MERCHANT_OFFER/market_not_priced` / `QUOTE_REQUIRED/market_required_for_seller_storefront` / `INVALID_ARGUMENTS` |
| MCP-K-US | `/mcp` test merchant | US | `ready_for_payment`, USD → cancel → reads back `canceled` |
| UCP-SF-US / none | `/ucp/mcp` | US / – | `requires_escalation`, USD, reviewed host; the cart expires (US also runs the shadow lookup) |
| UCP-SF-SG / DE / USA | `/ucp/mcp` | SG / DE / USA | `NO_MERCHANT_OFFER/seller_not_priced_in_market_currency` / `NO_MERCHANT_OFFER/market_not_priced` / `QUOTE_REQUIRED/ucp_market_invalid` |

**Flags required:**
- **Storefront cases:** `STRICT`, `MCP_STOREFRONT_ESCALATION` (on `/mcp`) or `UCP_TOOL_DOOR` + `UCP_ESCALATION` (on `/ucp/mcp`), `CURRENCY_FOLLOWS_MARKET` and `MERCHANT_PRICING`, all `1`. Also `SHOPIFY_CATALOG_ATTRIBUTION=shadow`, and `UCP_CART_REFERRAL` off.
- **MCP-K-US:** `STRICT`, `MCP_STOREFRONT_ESCALATION` and `CURRENCY_FOLLOWS_MARKET`, all `1`, because `/mcp` declares `market` only while the storefront path is on.

**What counts as "nothing created".** Only a 200 **tool** error (`result.isError`) whose code is a string other than `UNEXPECTED_ERROR` / `CHECKOUT_OUTCOME_UNKNOWN` / `INTERNAL_ERROR`. Any of these is `unknown:<case>`:
- a top-level JSON-RPC error, numeric (the heartbeat's `-32603`) or string;
- a catch-all or unsure code;
- a non-200;
- an answer with no code.

## What the owner sets up (names only; no session reads or creates these)

1. **The test agent and its key.** The owner's existing test shopping agent, set up for testing (owner, 2026-11-19). Its key lives in the existing secret `test-shopping-agent-api-key`, created 2026-09-24 with versions 1 and 2, and mounted by no other service or job. Pass the valid version as `_KEY_VERSION` (version **2**, the one the live Pivota shopping agent uses). The job refuses the key unless it is in the gateway's `ak_` format.
   - **This is a LIVE agent** (owner, 2026-11-19: "Live agent, fine"). The test calls count against its rate limit and quota and show in its attribution.
   - For the checkout cases, the test issuer's binding attaches to this agent, so the per-run enable and the disable afterwards (then waiting out the registry lag) are mandatory, never optional.
2. **The targets.** `prod-test-transactions-targets` holds the JSON above. Review records its sha256 as `TARGETS_SHA256`.
3. **The test issuer:**
   1. Generate an EC P-256 key pair on your own machine.
   2. Store the private key, as PKCS8 PEM, as `prod-test-agent-user-key`.
   3. Publish the public JWKS (`{"keys":[{…, "kid", "alg": "ES256", "use": "sig"}]}`) on a **Pivota-owned https host**.
   4. Register `{issuer, jwks_uri, audience, algs:["ES256"], authorized_party, required_scopes}` for the **test agent** in the developer portal, then **disable** the binding until the first go.
4. **Service account:** `sa-prod-test-txn@pivota-prod.iam.gserviceaccount.com`, with `roles/secretmanager.secretAccessor` **only** on `test-shopping-agent-api-key` and `prod-test-transactions-targets`. Add `prod-test-agent-user-key` when the checkout cases come. It gets nothing else.
5. **Launcher (`sa-ops-launcher`):**
   - `roles/iam.serviceAccountUser` on `sa-prod-test-txn`;
   - `roles/logging.viewAccessor` on **two** log views on `_Default` (location `global`), **added alongside** the project-wide `roles/logging.viewer` it already has. Keep that viewer: the seed-batch launcher reads its job logs with a plain project-level `gcloud logging read`, so removing it would break the seed batch. Trimming it waits until the seed batch reads through a view too (the controller's call, 2026-11-19).
   - A view filter allows nesting depth 1, so one OR-of-ANDs view is refused (found 2026-11-20). Each source gets its own flat view:

     | View | Filter | Read by |
     |---|---|---|
     | `prod-test-transactions` (created 2026-11-20) | `resource.type="cloud_run_job" AND resource.labels.job_name="prod-test-transactions"` | the job's own lines (every run) |
     | `prod-test-transactions-gateway-shadow` | `resource.type="cloud_run_revision" AND resource.labels.service_name="gateway" AND jsonPayload.event="ucp_shopify_catalog_attribution"` | the shadow lookup lines (storefront runs only) |

   - The shadow view is needed **before the first run with a US storefront create** (MCP-SF-US, UCP-SF-US, `all`); OR-* and the refusal cases never read it. If the view API refuses the `jsonPayload` term, drop it: the launcher's query still filters on the event. The view then shows all gateway lines, which the launcher can already read through its project viewer.
   - Without that view, the shadow read finds no lines. The check then reports `lines: 0, all_shadow_unsent: null` (unknown), never `true`.

## First-run order (each run is its own go; enable the issuer binding before, disable it after)

1. **`_CASES=OR-DE`:** a refusal that creates nothing. Review `result.json` with the controller.
2. **`_CASES=OR-US,OR-us-lower,OR-none,OR-USA,OR-long-s`:** the rest of path 4 (OR-SG and OR-USA-beside-SG are disabled). The reviewed test product's cache rows expire **2026-11-26**; after that, the served cases fail and report until the rows are refreshed or a new targets version is reviewed.
3. **Checkout cases.** Add `_USER_KEY_VERSION`:
   1. **MCP-SF-DE:** a refusal that proves the token and the email are accepted.
   2. **MCP-K-US:** create, then cancel.
   3. **MCP-SF-US, then UCP-SF-US:** 2 live carts that expire, plus the advisory shadow check.
   4. **`all`.**

Every run needs:
- the owner's go in the session that launches it (`_GO_REFERENCE`);
- numbered `_KEY_VERSION` and `_TARGETS_VERSION` (never `latest`), plus `_USER_KEY_VERSION` for checkout cases;
- the gate open, outside 05:15–06:15Z and 10:30–11:20Z.

## Known limits (review v5, accepted)

- **If `offers_product_id` becomes unpriced** (`offers_count` 0) or its merchant is no longer a test merchant, the served OR-* cases **fail and report**, with no retry. The fix is a new targets version, its sha in a reviewed PR, and a new go.
- **The flags come from the service template.** They match what serves only because the gateway step requires serving == latest created. They, and the image pin, are checked at the gateway step, not at execution, which leaves a window of a few minutes. A gateway deploy or env change in that window is serialized away by the controller, which holds the gateway during a run.
- **A slow gateway can run the job past its 600 s task timeout.** That is possible with 45 s per call. It fails safe: the missing checkout rows become `unknown:<case>`, and the run is not ok.
- **A named storefront refusal raised after the store built a cart** leaves one seller cart for that case. It expires unpaid and stays inside the approved cart count.

## Rollback / cleanup

- **The job holds no state.** If a build is interrupted, `result.json` and the lock say what to check.
- **An armed job left behind:** redeploy it disarmed (`job_spec_args(armed=False)`) and disable the issuer binding.
- **A `left_open` kernel id:** cancel it through `/mcp` `cancel_checkout_session`, with the same buyer (`sub`) and `acp_session_id=ptt-<run id>-<case id>`.
- **An `unknown:<case>`:** look the session up by that `acp_session_id` before re-running.
- **An `expires_on_seller` id:** nothing to do.
