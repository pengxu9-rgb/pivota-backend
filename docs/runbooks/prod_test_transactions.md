# Prod test transactions

A fixed list of test calls against the **prod gateway**, made by a dedicated first-party test agent. It's the one sanctioned exception to the no-prod-probe rule:
- only this job (`prod-test-transactions`);
- only this key (`prod-test-agent-key`);
- only on the owner's go, given for each run.

No partner key is ever used, and nothing is fetched from a laptop.

- **Job code:** `scripts/ops/prod_test_transactions.py`. It holds the case list, the transport, cancel-always and the output.
- **Launcher:** `scripts/ops/prod_test_transactions_launch.py`, run from `infra/gcp/cloudbuild.prod_test_transactions.yaml`.

## Stop point

The job never submits a payment. A checkout is created, read back and **cancelled**.

Payment submission is **enabled** in prod (`AGENT_CHECKOUT_STRICT_SUBMIT_PAYMENT_ENABLED=1`), so the job's transport is the guard:
- It sends only the calls in `ALLOWED_CALLS`:
  - invoke `offers.resolve`;
  - `/mcp` `create_checkout_session`, `get_checkout_session` and `cancel_checkout_session`;
  - `/ucp/mcp` `create_checkout` and `get_checkout`.
- It refuses any request member named in `FORBIDDEN_MEMBERS` (`payment`, `reap`, `card`, `token`, …), at any depth.

Both checks run before anything is sent, and the tests pin them.

## What one run does

1. The build copies the scripts from the release image, which is pinned by digest. It then checks that the config is the release's own copy, and that the digest is the release tag's.
2. **Gateway check.** The gateway must serve one revision at 100%. Each selected case's required flags must equal the live plain values (secret values are never read).
3. **Lock.** The build takes `gs://pivota-prod-ops-locks/prod-test-transactions.lock`, create-only.
4. **Run:**
   1. `oneoff_gate.py` must print `GATE_OPEN`.
   2. **Arm.** The job is deployed with:
      - the key mounted **by numbered version** at `/var/run/pivota-test-agent/key`;
      - the gate reading;
      - the case ids;
      - the build id, which every idempotency key is derived from.
   3. The job is executed, and the launcher waits for the execution by name.
   4. **Disarm.** The job is redeployed with `--clear-secrets` and no gate or cases, so any later execution refuses at once.
5. **Publish.** `log.txt`, `rows.json` and `result.json` are written, create-only, to `gs://pivota-prod-ops-receipts/prod-test-transactions/<build id>/`. The lock is released only when nothing can still run and the job is disarmed. If anything was **left open**, the build fails and names it.

**Limits:**
- 40 calls per run;
- a 20 s timeout per call;
- no retries, no redirects;
- the gateway host only.

The job egresses through the default subnet and pivota-nat, so the gateway logs attribute every call to the one reserved prod IP.

**Output.** One `PROD_TEST_TXN_RESULT <base64 JSON>` line per case, with these fields: `case`, `door`, `market`, `expected`, `got`, `pass`, `created`, `cancelled`, `left_open`, `error`. A final `run` line carries `ok`, `cases`, `passed`, `calls`, `left_open` and `expires_on_seller`.

`got` holds only the asserted fields, each a scalar. The key and response bodies are never printed.

## Cases in this release

| Case | Door | Market sent | Expected |
|---|---|---|---|
| OR-US, OR-SG, OR-us-lower, OR-none | invoke `offers.resolve` | US, SG, us, (none) | served (no `error.code`) |
| OR-USA | invoke `offers.resolve` | USA | `status=error`, `MARKET_INVALID` / `market_invalid` |
| OR-long-s | invoke `offers.resolve` | ſg (U+017F) | `status=error`, `MARKET_INVALID` / `market_invalid` |
| OR-DE | invoke `offers.resolve` | DE | `status=error`, `MARKET_NOT_PRICED` / `market_not_priced` |
| OR-USA-beside-SG | invoke `offers.resolve` | `offers.market=USA`, `payload.market=SG` | served (SG) |

The `offers.resolve` cases need `OFFERS_PRODUCT_ID`, which is **filled in at review**: one connected test merchant's product. While it is empty, every such case refuses.

## Checkout cases (paths 1–3)

Two owner decisions, made 2026-11-19 ("JWT, accept expiry"), shape these cases.

### End-user identity

The checkout tools (create, get, cancel) need a signed-in end user. The job provides one as follows:
- **Issuer.** A test issuer is registered to the **test agent only**, as a federated binding (developer portal: `PUT /agents/{agent_id}/identity-issuers`, table `agent_identity_issuers`).
- **Token.** For each case, the job signs a 5-minute ES256 `X-Agent-User-JWT`. Its claims are `iss`, `aud`, `sub=ptt-test-buyer`, `iat`, `nbf`, `exp` and `acp_session_id=ptt-<run id>-<case id>`, and its header carries `kid`.
- **One token per case.** The gateway derives the user from `(iss, sub)`, and the session from `acp_session_id`. A case's create, read-back and cancel all carry the same token, so the gateway accepts them as the same user and session.
- **Signing key.** The job signs with an EC P-256 private key from Secret Manager (`prod-test-agent-user-key`). It is mounted by version only while the job is armed, and only when a selected case needs it.

### Storefront carts

A storefront checkout (`esc_…`) cannot be cancelled: the gateway refuses by design, and the seller's cart expires unpaid. The owner accepted this. Such ids are:
- never cancel-called;
- listed under `expires_on_seller`;
- not a failure of the run.

Every other created checkout must read back `canceled`, or it is reported `left_open` and the run fails. If a create fails with no answer, it is reported `left_open: unknown:<case>`, with no retry.

### Cases

**`/mcp` `create_checkout_session` (step 6).** Flags: `STRICT`, `MCP_STOREFRONT_ESCALATION`, `CURRENCY_FOLLOWS_MARKET`, `MERCHANT_PRICING` all `1`, and `SHOPIFY_CATALOG_ATTRIBUTION=shadow`.

| Case | Target | Market | Expected |
|---|---|---|---|
| MCP-SF-US | storefront seller | US | `completes_on_seller_storefront`, USD, `amount_unit=minor`; the cart expires on the seller; runs the shadow lookup |
| MCP-SF-SG | storefront seller | SG | refused `NO_MERCHANT_OFFER` / `seller_not_priced_in_market_currency`; nothing created |
| MCP-SF-DE | storefront seller | DE | refused `NO_MERCHANT_OFFER` / `market_not_priced` |
| MCP-SF-none | storefront seller | (none) | refused `QUOTE_REQUIRED` / `market_required_for_seller_storefront` |
| MCP-SF-USA | storefront seller | USA | refused `INVALID_ARGUMENTS` |
| MCP-K-US | connected test merchant | US | `ready_for_payment`, USD → cancel → reads back `canceled` |

MCP-K-US needs `STRICT=1` only.

**`/ucp/mcp` `create_checkout` (step 1).** Flags: `STRICT`, `UCP_TOOL_DOOR`, `UCP_ESCALATION`, `CURRENCY_FOLLOWS_MARKET`, `MERCHANT_PRICING` all `1`, and attribution `shadow`.

| Case | Market | Expected |
|---|---|---|
| UCP-SF-US | US | `requires_escalation`, USD; the cart expires on the seller; runs the shadow lookup |
| UCP-SF-SG | SG | `NO_MERCHANT_OFFER` / `seller_not_priced_in_market_currency` |
| UCP-SF-DE | DE | `NO_MERCHANT_OFFER` / `market_not_priced` |
| UCP-SF-USA | USA | `QUOTE_REQUIRED` / `ucp_market_invalid` |
| UCP-SF-none | (none) | `requires_escalation`, USD (`market_assumed`, D-10); the cart expires |

On `/ucp/mcp`, the market is sent as `checkout.context.address_country`.

**Path 3, the shadow catalog lookup.** If MCP-SF-US or UCP-SF-US passes, publish reads the gateway's `ucp_shopify_catalog_attribution` lines for the run's window, plus 3 minutes. It requires one line per passed create, and every line must show `mode=shadow` and `sent=false`, because shadow never changes the cart. The check goes into `result.json` as `shadow_lookup` and counts toward `ok`. The storefront cases refuse to run unless the gateway still has `SHOPIFY_CATALOG_ATTRIBUTION=shadow`.

No case sends a buyer email, an address, a payment or a Reap member.

### Reviewed targets (filled in REVIEW, never at run time; empty refuses every case that needs it)

| Constant | What | Why it is named |
|---|---|---|
| `OFFERS_PRODUCT_ID` | a connected test merchant's product | read only |
| `KERNEL_TEST_MERCHANT` | `merchant_id`, `product_id`, `variant_id` of a connected **test** merchant (`TEST_MERCHANT_IDS`) | its checkout is created and cancelled |
| `STOREFRONT_SELLER` | `merchant_id` (must start `merch_obs_`), `product_id`, `variant_id` of a **live** US/USD Shopify storefront seller | its storefront carts are real carts on that store that **expire unpaid**: the owner approves the store here |
| `USER_TOKEN_ISSUER`, `USER_TOKEN_AUDIENCE`, `USER_TOKEN_KID` | the registered test issuer's `iss`, `aud` and key id | must match the binding |

## What the owner sets up (names only; no session reads or creates these)

1. **Secret:** `prod-test-agent-key` in pivota-prod Secret Manager. It holds the test agent's key (format `ak_…`), for a first-party agent created for this job only.
2. **Service account:** `sa-prod-test-txn@pivota-prod.iam.gserviceaccount.com`, with `roles/secretmanager.secretAccessor` on `prod-test-agent-key` **only**, and nothing else.
3. **Launcher:** `sa-ops-launcher` needs `roles/iam.serviceAccountUser` on `sa-prod-test-txn` so it can deploy the job as it. It already has the run, storage and logging roles it uses for the seed batch.
4. **The test issuer, for checkout cases:**
   1. Generate an EC P-256 key pair on your own machine.
   2. Store the private key, as PKCS8 PEM, as `prod-test-agent-user-key` in pivota-prod Secret Manager.
   3. Grant `sa-prod-test-txn` `secretAccessor` on that secret only.
   4. Publish the public key as a JWKS (`{"keys":[{… "kid": "<kid>", "alg": "ES256", "use": "sig"}]}`) at a public **https** URL whose host resolves only to public IPs.
   5. Register `{issuer, jwks_uri, audience, algs:["ES256"]}` for the **test agent** in the developer portal. Registration refuses an issuer that is reserved or already registered to another agent, and fetches the JWKS at once.
   6. Put the iss, aud and kid into the reviewed targets.

## First-run order

1. **One case.** Run `_CASES=OR-DE`, a refusal that creates nothing. Review `result.json` with the controller.
2. **The rest of path 4.** Run `_CASES=all` on a new go.
3. **Checkout cases.** Pass `_USER_KEY_VERSION=<n>` as well, and again start with one case:
   1. **MCP-SF-DE**, a refusal that creates nothing and proves the token is accepted.
   2. **MCP-K-US**: create, then cancel.
   3. **MCP-SF-US** and **UCP-SF-US**: a live storefront cart that expires, plus the shadow check.
   4. Then the rest.

Every run needs:
- the owner's go in the session that launches it, recorded in `_GO_REFERENCE`;
- a numbered `_KEY_VERSION`, never `latest`, plus `_USER_KEY_VERSION` whenever a checkout case is selected;
- the gate open, outside 05:15–06:15Z and 10:30–11:20Z.

## Rollback / cleanup

The job holds no state. If a build is interrupted, `result.json` and the lock say what to check:
- **An armed job left behind:** redeploy it disarmed with `job_spec_args(armed=False)`.
- **A `left_open` id:** cancel it through `/mcp` `cancel_checkout_session`, using the same test identity: the same `sub` and the same `acp_session_id=ptt-<run id>-<case id>`.
- **An `expires_on_seller` id:** nothing to do; the seller cart expires unpaid.
