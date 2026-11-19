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

**Output.** One `PROD_TEST_TXN_RESULT <base64 JSON>` line per case, with these fields: `case`, `door`, `market`, `expected`, `got`, `pass`, `created`, `cancelled`, `left_open`, `error`. A final `run` line carries `ok`, `cases`, `passed`, `calls` and `left_open`.

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

## Not in this release: checkout cases (paths 1–3), pending the owner's decisions

Paths 1–3 are the UCP checkout currency, the `/mcp` storefront cart with `market`, and a store-cart create for the shadow catalog lookup. Two things block them.

### 1. End-user identity

`create`/`get`/`cancel` checkout are `user_ref` tools. An API key alone gets `USER_AUTH_REQUIRED`. There are two options:

- **(recommended)** A test issuer registered as the test agent's federated binding. The job then signs a short-lived `X-Agent-User-JWT` with a second Secret Manager key, mounted the same way. This affects only the test agent.
- **(not recommended)** The gateway's test-identity window (`AGENT_CHECKOUT_ALLOW_TEST_IDENTITY=1` + `AGENT_CHECKOUT_TEST_IDENTITY_WINDOW=1`). It would let every API-key caller assert any `user_ref`.

### 2. Storefront carts

A storefront checkout (`esc_…`) cannot be cancelled: the gateway refuses it by design, and the seller's cart expires on its own. `/ucp/mcp` has no cancel tool. The job reports such a cart as `left_open`, and the run is not ok, until the owner decides one of these:
- accept unpaid carts that expire; or
- target test storefronts only.

Kernel checkouts are cancelled through `/mcp` `cancel_checkout_session`, then read back as `canceled`. If a create fails with no answer, the result is reported as `left_open: unknown:<case>`, because a session may exist that the job cannot name.

The machinery for these cases (`run_checkout`, `cancel_checkout`) is in the job and tested against fakes, including a failed create, a failed cancel and a failed read-back.

## What the owner sets up (names only; no session reads or creates these)

1. **Secret:** `prod-test-agent-key` in pivota-prod Secret Manager. It holds the test agent's key (format `ak_…`), for a first-party agent created for this job only.
2. **Service account:** `sa-prod-test-txn@pivota-prod.iam.gserviceaccount.com`, with `roles/secretmanager.secretAccessor` on `prod-test-agent-key` **only**, and nothing else.
3. **Launcher:** `sa-ops-launcher` needs `roles/iam.serviceAccountUser` on `sa-prod-test-txn` so it can deploy the job as it. It already has the run, storage and logging roles it uses for the seed batch.
4. **Later, if decided:** the identity signing secret and its binding (see the decisions above).

## First-run order

1. **One case.** Run `_CASES=OR-DE`, a refusal that creates nothing. Review `result.json` with the controller.
2. **The rest of path 4.** Run `_CASES=all` on a new go.
3. **Checkout cases.** These come only after a reviewed PR adds them, once the decisions above are made, and again one case first.

Every run needs:
- the owner's go in the session that launches it, recorded in `_GO_REFERENCE`;
- a numbered `_KEY_VERSION`, never `latest`;
- the gate open, outside 05:15–06:15Z and 10:30–11:20Z.

## Rollback / cleanup

The job holds no state. If a build is interrupted, `result.json` and the lock say what to check:
- **An armed job left behind:** redeploy it disarmed with `job_spec_args(armed=False)`.
- **A `left_open` id:** cancel it through `/mcp` `cancel_checkout_session`, using the same test identity.
