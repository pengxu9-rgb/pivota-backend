# Commerce Index: GCP crawl lane

Public crawling is a discovery and evidence lane, not a payment or merchant
catalogue authority. It must use a dedicated Cloud Run subnet/NAT; the default
egress IP is reserved for payment-partner allowlists and must not receive crawl
traffic.

## Provisioning

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

### Dry-run manifest review gate

The Gateway utility `scripts/validate-commerce-index-crawl-manifest.js` accepts
only a local JSON manifest passed with `--manifest`. It never makes a network
request, writes a database row, or permits live execution. It rejects a manifest
unless all of the following are supplied:

- `dry_run: true`, an ISO market, and the declared `PivotaCommerceIndexBot/…`
  user agent;
- an explicit source ID plus either merchant `consent_ref` or a public-crawl
  policy reference;
- fresh per-host robots evidence, no older than 24 hours, pointing at that
  host's HTTPS `/robots.txt` URL;
- product-only HTTPS targets without embedded credentials, with a bounded
  per-domain request count, concurrency of exactly one, a delay of at least one
  second, and no more than one retry.

This is an operator/review control, not the final authorization boundary. A
future crawl executor must independently resolve the source ID against the
active `commerce_index_sources` registry and fetch/evaluate robots.txt itself
immediately before a request. A caller-provided manifest can never grant that
authorization.

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
