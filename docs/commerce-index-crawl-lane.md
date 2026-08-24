# Commerce Index: GCP crawl lane

Public crawling is a discovery and evidence lane, not a payment or merchant
catalogue authority. It must use a dedicated Cloud Run subnet/NAT; the default
egress IP is reserved for payment-partner allowlists and must not receive crawl
traffic.

## Phase 0a — payment NAT scope migration

**Owner:** Platform and payments jointly. **Do not schedule adjacent to a
cutover freeze or a partner payment certification window.** This is a
production payment-network change, even though it retains the address.

Cloud NAT cannot host the new custom-subnet crawl NAT while `pivota-nat`
covers all subnets. The prerequisite narrows `pivota-nat` to `default` only;
it does not replace its reserved IP. The operation is gated by the guarded
script, but its success criterion is an application-level payment-path result,
not merely a successful `gcloud` response.

### Preflight

1. Record the expected reserved payment egress address. In production this is
   `8.231.167.230`; staging must use its own reserved address.
2. Run the read-only guard. It verifies the regional VPC topology, existing
   NAT scope, and static-address identity without modifying GCP:

   ```bash
   EXPECTED_PAYMENT_EGRESS_IP=<reserved-payment-ip> \
     infra/gcp/migrate_payment_nat_to_default_subnet.sh staging --check
   ```

3. From a live payment workload on the default subnet, make a non-mutating
   payment-path request and record the observed source address, UTC timestamp,
   request/correlation ID, and operator. A laptop `curl`, Cloud Console call,
   or a crawler result is not a substitute: it must traverse the same default
   subnet/NAT path used by payments.
4. Confirm the rollback owner and the maintenance window before applying.

### Apply and postcheck

The script requires the preflight attestation as explicit environment values;
it will refuse to run when the observed payment-path address differs from the
reserved address.

```bash
CONFIRM_PAYMENT_NAT_SCOPE=default \
EXPECTED_PAYMENT_EGRESS_IP=<reserved-payment-ip> \
PAYMENT_PATH_EGRESS_IP=<observed-payment-path-ip> \
PAYMENT_PATH_EGRESS_VERIFIED_AT=<UTC-ISO-8601> \
  infra/gcp/migrate_payment_nat_to_default_subnet.sh staging
```

Immediately repeat the live payment-path probe. The exit gate is all of:

- `pivota-nat` has `LIST_OF_SUBNETWORKS` scope and targets only `default`.
- It still references `pivota-egress-ip`.
- The address resource is unchanged.
- The live payment path still presents the recorded reserved address.

If the postcheck fails, stop. Before any crawl subnet exists, rollback is a
single guarded `gcloud compute routers nats update` back to all-subnet scope
while preserving `pivota-egress-ip`. After a crawl NAT exists, remove the crawl
NAT/subnet first; two overlapping NAT scopes cannot coexist in the region.

Only after staging has passed, schedule the reviewed production window and
repeat the same evidence capture. Do not provision or deploy the crawl lane
until that production postcheck is recorded.

## Phase 0b — crawl egress provisioning

The existing NAT currently covers every regional subnet, which prevents a second
custom-subnet NAT. First run the guarded
`migrate_payment_nat_to_default_subnet.sh` in staging: it refuses any regional
subnet topology other than the known `default` subnet, changes only the payment
NAT scope, and preserves `pivota-egress-ip`. Verify payment egress before doing
the same reviewed action in production.

Then run `infra/gcp/setup_crawl_egress.sh` in staging with an unused RFC1918
CIDR outside the default VPC's auto-mode `10.128.0.0/9` range (for example,
`10.10.240.0/24`). The script creates only `pivota-crawl` subnet, router, NAT,
and a reserved crawl egress address. It does not deploy a worker. Record the
returned IP for crawl observability, never as an Antom/Adyen allowlist address.

Every future crawl Cloud Run Job must set:

```text
--network default --subnet pivota-crawl --vpc-egress all-traffic
--max-retries 1 --task-timeout <bounded>
DB_POOL_MIN_SIZE=1 DB_POOL_MAX_SIZE=4
```

## Store Audit UCP probe attachment (not enabled by this change)

The Store Audit UCP probe is a separate, anonymous crawl job. It is not the
interactive warm-handoff service and it has no database credentials, payment
credentials, browser, or `complete_checkout` capability. It may call one
synthetic-address `create_checkout` preview only when the claimed verifier job
provides a variant; its result stores only redacted priced facts.

The job posts one result to the backend-only receipt endpoint. That endpoint is
closed unless both of these backend values are set:

```text
STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED=true
STORE_AUDIT_UCP_PROBE_INTERNAL_KEY=<dedicated-secret>
```

The crawl job needs the same dedicated secret plus an HTTPS receipt URL:

```text
STORE_AUDIT_UCP_PROBE_RECEIPT_URL=https://<backend>/internal/store-audit/ucp-probes/receipts
STORE_AUDIT_UCP_PROBE_INTERNAL_KEY=<same-dedicated-secret>
```

It also needs the HTTPS claim URL and a stable, task-specific worker identity:

```text
STORE_AUDIT_UCP_PROBE_CLAIM_URL=https://<backend>/internal/store-audit/ucp-probes/claims
STORE_AUDIT_UCP_WORKER_ID=<stable-execution-and-task-id>
```

First deploy the backend with the receipt explicitly enabled and the dedicated
Secret Manager mapping (never add this key to a general env file):

```bash
CONFIG=apply STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED=true \
  infra/gcp/deploy_backend.sh staging <backend-tag>
```

Then create the two Jobs and their initially paused triggers. The job setup
refuses to proceed without the same secret, least-privilege identities, and
the exact HTTPS Cloud Run `web` service URL:

```bash
infra/gcp/setup_store_audit_ucp_identity.sh staging

STORE_AUDIT_UCP_REPROBE_WORKER=true PAUSED=1 \
STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL=https://<staging-web-run-app> \
  infra/gcp/setup_scheduler.sh staging <backend-tag> <gateway-tag>
```

`PAUSED=0` is intentionally rejected while Store Audit UCP is present: it
would resume unrelated Scheduler jobs. After the staging receipt and egress
checks are recorded, arm only these two triggers with:

```bash
STORE_AUDIT_UCP_REPROBE_WORKER=true STORE_AUDIT_UCP_REPROBE_ARMED=true PAUSED=1 \
STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL=https://<staging-web-run-app> \
  infra/gcp/setup_scheduler.sh staging <backend-tag> <gateway-tag>
```

Rerunning with the default `STORE_AUDIT_UCP_REPROBE_WORKER=false` pauses both
UCP triggers idempotently. The crawl Job runs as
`sa-store-audit-ucp-crawl`, which can read only the dedicated receipt secret;
it has no database secret or payment credential. The selector and Scheduler
each have separate identities with only their required permissions.

The crawl identity receives `roles/run.invoker` only on the `web` Cloud Run
service (not at project scope). The Job mints a metadata-server ID token with
that exact service origin as its audience for both claim and receipt requests;
Cloud Run IAM therefore authenticates it before the backend applies its
separate HMAC receipt-key check. Job setup reads the deployed `web` revision
and refuses to proceed unless the receipt feature flag is `true` and the
dedicated key is mounted there. It requires exactly one untagged 100%-traffic
revision and inspects that revision, so a receipt-enabled `PROMOTE=0`
candidate cannot accidentally arm a crawler against an older live revision.

The Job command is `node scripts/run_store_audit_ucp_worker.js`. One invocation
claims at most one route, then exits. It may pass a checkout variant only when
the queue carries a validated Shopify `ProductVariant` GID; arbitrary product
keys never become checkout input.

Do not reuse the warm-handoff or general agent-internal key. The receipt is
accepted only for a currently claimed `ucp_probe` verification row and worker
lease. The backend creates or refreshes the route by normalized domain + route
kind + endpoint, writes `acceptance_signal` evidence, and then completes the
existing verification state machine. A repeated receipt with the same probe ID
is acknowledged only after a committed terminal result; retryable failures are
not falsely acknowledged.

Before enabling the flag, capture all of the following in staging:

1. Migration 196 applied and an anonymous probe produces a `ucp_probe`
   verification run, one domain-keyed `execution_routes` row, and (when UCP is
   found) one `acceptance_signal` row with `detected` or `tested` evidence.
2. An intentional unregistered evidence type and verifier are rejected before
   persistence. This is the silent-coercion regression gate.
3. A cold-start `prospect_*` run stores no synthetic merchant association on
   either route or route evidence; a verified merchant conversion claims the
   existing route rather than creating a duplicate.
4. No receipt body, log, or evidence payload contains an authorization header,
   checkout/continue URL, raw tool response, secret, token, or session value.
5. The job is attached to `pivota-crawl`, sends all traffic through the crawl
   NAT, and the payment-path egress postcheck in Phase 0a remains recorded.

There is intentionally no automatic scheduled re-probe enablement in this
change.
`jobs/scheduled_audit_job.py` does not cover cold-start prospects or
domain/TTL-based route re-probing. The separate `scheduled_ucp_reprobe` job
now has a default-off selector controlled by:

```text
STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED=true
STORE_AUDIT_UCP_REPROBE_TTL_HOURS=168       # bounded: 1–720; default 168
STORE_AUDIT_UCP_REPROBE_BATCH_SIZE=25       # bounded: 1–100; default 25
```

It considers only active `ucp` routes that are expired or older than the TTL,
uses the route's canonical last audit run, and refuses to queue a second active
probe for the same route. It is invoked independently at 03:30 UTC by the
`store-audit-ucp-reprobe-enqueue` Cloud Run Job and does not broaden the
merchant APM schedule. A second `store-audit-ucp-probe` Cloud Run Job drains
one remote probe every five minutes through `pivota-crawl`; no other workload
uses that subnet. Both Scheduler triggers are created paused unless the
dedicated `STORE_AUDIT_UCP_REPROBE_ARMED=true` gate is supplied; `PAUSED=0`
is rejected for this lane because it would resume unrelated schedulers.
Neither Job is created without explicit `STORE_AUDIT_UCP_REPROBE_WORKER=true`,
the exact HTTPS `web` service origin, least-privilege service accounts, and
the dedicated Secret Manager key. Enable only after the staging receipt/egress
gate. The selector also refuses to queue when the backend receipt flag or
dedicated receipt key is absent.

## Activation gates

Before a source-pull or public crawler schedule may be resumed, require all of:

1. A merchant-authorized `commerce_index_sources` record with active consent for
   a catalogue adapter, or a source-specific public-crawl policy that writes
   evidence only.
2. Robots evaluation, a declared Pivota user-agent, per-domain concurrency of
   one, rate-limit/backoff handling, and a bounded retry budget.
3. A dry-run manifest containing URL, host, source, market, observed timestamp,
   and intended field families; operators review it before canonical writes.
4. Public-crawl price, stock, and availability remain review-required and never
   advance checkout. A live merchant quote remains the only checkout authority.
5. Metrics for request outcome, 429/403 rate, robots denial, freshness lag, and
   per-domain request count.

## Publication relationship

After a consented catalogue sync changes a product, publication workers may
update search, graph, Insights review requests, or checkout-validation markers.
They do not run crawling themselves. The first full OpenSearch backfill must be
run with `--seed-memberships` after migration 195 so later identity moves can
delete obsolete documents safely.

## Antom boundary

`antom_catalog` needs a separate merchant-authorized feed schema and credential
adapter before activation. `antom_ucp` continues to be payment-only and must
remain on the payment egress path; it must never share crawl credentials, jobs,
or subnet configuration.
