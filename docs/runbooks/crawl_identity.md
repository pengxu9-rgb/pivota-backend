# Crawl identity: Web Bot Auth signing

Our crawlers sign their storefront requests (RFC 9421 HTTP Message Signatures, Ed25519, the Web
Bot Auth profile) so a storefront edge can verify the requests come from Pivota.

- Code: `services/crawl_identity.py`.
- Key directory: `routes/crawl_identity.py`.
- Tests: `tests/test_crawl_identity.py`, pinned against the draft's Ed25519 test vectors.

## Why

Shopify's changelog of 2026-05-07, "Bots and agents should identify themselves via Web Bot Auth",
puts bots on storefront pages under tiered rate limits: "Bots and agents that don't sign their
requests are subject to the strictest limits". The higher tier is requested at
https://forms.gle/V88RD31uAVirqE4e9 once a signed directory exists. No Cloudflare enrolment is
needed.

Measured on 2026-10-08 (`reports/shopify_crawl_access_2026_10_08/REPORT.md`):

- From the crawl NAT, store requests got `429 local_rate_limited` with `Retry-After: 60` on the
  first request, even from a never-used GCP IP.
- The same request from a non-GCP machine got 200.

Signing is the sanctioned remedy. Changing how the crawler presents itself (proxies, IP rotation,
fingerprint or UA disguise) is out of scope.

## What is signed, and what is not yet

| lane | signed when the flag is on |
|---|---|
| external-offer fetch (`services/external_offers_service.py::_fetch_html`): the referral refresh and the HTML repair scripts | yes, every redirect hop |
| Reap cart proofs, enrichment and mirror, and `scripts/backfill_shopify_variant_ids.py`: all through `services/shopify_presentment.py::no_cookie_client` (#2527) | yes |
| Tier B, purchasability sweep (`jobs/tierb_cart_link_eligibility.py::PacedTransport`) | NOT YET: separate PR, after the sweep throttle fix lands. They also still send a desktop-Chrome UA |
| curated brand feed, retailer-ingest drain, destination sweep | NOT YET |

To wire a lane, pass `**crawl_identity.transport_kwargs()` to its `httpx.AsyncClient(...)`. Or wrap
the transport that actually sends: `crawl_identity.crawl_transport(httpx.AsyncHTTPTransport(...))`.
Signing happens in the transport, so manually followed redirect hops are signed too.

**Sign innermost.** A transport that WAITS (Tier B's `PacedTransport` sleeps inside
`handle_async_request`) must wrap the signer, not be wrapped by it:
`PacedTransport(crawl_transport(httpx.AsyncHTTPTransport()))`. Signing outside the wait can let the
60 s `expires` lapse before the request leaves.

A signed client ignores `HTTP(S)_PROXY` env vars: httpx applies them only when no transport is
passed. No crawl job sets them (checked 2026-10-08). If a lane needs a proxy, wrap its explicit
proxy transport: `crawl_transport(httpx.AsyncHTTPTransport(proxy=...))`.

## Switches

| env | where | effect |
|---|---|---|
| `WEB_BOT_AUTH_PRIVATE_KEY` | secret; on `web` and on every signing job | Ed25519 PKCS#8 PEM. On `web` it makes the directory answer; elsewhere it is the signing key |
| `CRAWL_WEB_BOT_AUTH_ENABLED` | the crawl jobs only | `true` = sign. Unset = today's bytes exactly. On without a usable key = unsigned, plus one ERROR line |
| `WEB_BOT_AUTH_SIGNATURE_AGENT` | optional; IDENTICAL on `web` and every signing job | default `https://api.pivota.cc`, the origin serving the directory. The directory answers only for this authority, so a job naming another origin gets a 404 from a verifier |
| `WEB_BOT_AUTH_AGENT_FORMAT` | optional | `string` (default) or `dictionary`; see below |

**The two `Signature-Agent` formats.** Cloudflare's live docs require the sf-string
`Signature-Agent: "https://..."` and say the dictionary form fails. The IETF draft (architecture-05,
2026-03) calls the string form legacy and uses `agent1="https://..."`. Shopify does not document
which form it accepts. Start with `string`. If signed requests still get `local_rate_limited` while
the directory check passes, try `dictionary` on one job before concluding anything.

## Setup (Peng runs; Claude never sees the private key)

1. Generate the key on your machine, into a file only you can read:
   ```bash
   (umask 077; ~/dev/pivota-backend-quality-gate/.venv/bin/python -c 'from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K; from cryptography.hazmat.primitives import serialization as s; print(K.generate().private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()).decode(), end="")' > ~/web_bot_auth_key.pem)
   ```
2. Create the secret, then delete the file:
   ```bash
   gcloud secrets create WEB_BOT_AUTH_PRIVATE_KEY --project pivota-prod --data-file="$HOME/web_bot_auth_key.pem" && rm -P ~/web_bot_auth_key.pem
   ```
3. Grant read access to the identities that use it: `sa-backend` (web), `sa-worker` (the cart-proof
   and Tier B jobs), and the default compute SA that `external-referral-refresh` runs as. That SA
   was read from `gcloud run jobs describe external-referral-refresh` on 2026-10-08; nothing in
   this repo sets it, so re-check before granting. The cart-proof setup script refuses `on` until
   `sa-worker` holds the grant.
   ```bash
   for sa in sa-backend@pivota-prod.iam.gserviceaccount.com sa-worker@pivota-prod.iam.gserviceaccount.com 388293626878-compute@developer.gserviceaccount.com; do gcloud secrets add-iam-policy-binding WEB_BOT_AUTH_PRIVATE_KEY --project pivota-prod --member "serviceAccount:$sa" --role roles/secretmanager.secretAccessor; done
   ```
4. Publish the directory. On a backend image that contains this code, mount the key on `web`:
   ```bash
   gcloud run services update web --region us-west1 --project pivota-prod --update-secrets=WEB_BOT_AUTH_PRIVATE_KEY=WEB_BOT_AUTH_PRIVATE_KEY:latest
   ```
5. Check the directory the way a verifier will. Expect `OK`:
   ```bash
   ~/dev/pivota-backend-quality-gate/.venv/bin/python scripts/ops/check_web_bot_auth_directory.py https://api.pivota.cc
   ```
6. Measure before enabling at scale: one unsigned and one signed request per store, from the crawl
   subnet, to stores that answer `local_rate_limited` today (experiment E3 in the report). The
   script is in the image once this PR is merged; `<backend-tag>` is that image's full sha:
   ```bash
   SUBNET=pivota-crawl SERVICE_ACCOUNT=sa-worker@pivota-prod.iam.gserviceaccount.com SECRETS=WEB_BOT_AUTH_PRIVATE_KEY=WEB_BOT_AUTH_PRIVATE_KEY:latest ENV_VARS=PIVOTA_ENV=production,CRAWL_WEB_BOT_AUTH_ENABLED=true IMAGE=us-west1-docker.pkg.dev/pivota-shared/pivota/backend:<backend-tag> bash scripts/ops/run_oneoff_job.sh scripts/ops/web_bot_auth_probe.py flowerknows.co dermalogica.com
   ```
   It sends at most 10 requests and bypasses the crawl pacers, so run it outside the nightly crawl
   windows (02:20–04:20 and 05:15–06:15 UTC). It needs no database: `SECRETS` above deliberately
   mounts only the key.
   Read the `E3` lines:
   - Signed 200 where unsigned gets `local_rate_limited`: signing works.
   - Both 429: Shopify may want the higher-tier form first, or may not accept this
     `Signature-Agent` form. Try `WEB_BOT_AUTH_AGENT_FORMAT=dictionary` in `ENV_VARS` once.
   - Either way, file the form (step 8).
7. Enable on the jobs:
   - Cart proofs, through their setup script (a plain re-run keeps the setting):
     ```bash
     WEB_BOT_AUTH=on bash infra/gcp/setup_reap_cart_proof_jobs.sh prod <backend-tag>
     ```
   - Referral refresh: the job must run an image with this code first. Jobs are pinned to an image;
     a deploy does not re-image them. Then:
     ```bash
     gcloud run jobs update external-referral-refresh --region us-west1 --project pivota-prod --update-secrets=WEB_BOT_AUTH_PRIVATE_KEY=WEB_BOT_AUTH_PRIVATE_KEY:latest --update-env-vars=CRAWL_WEB_BOT_AUTH_ENABLED=true
     ```
   - The cart-proof run report carries `web_bot_auth: signed | off | unsigned_<reason>`.
8. File https://forms.gle/V88RD31uAVirqE4e9 with:
   - the directory URL;
   - the User-Agent: `Mozilla/5.0 (compatible; PivotaBot/1.0; +https://pivota.cc)`, unless
     `EXTERNAL_OFFER_USER_AGENT` overrides it on a job;
   - the egress IP 34.82.199.35;
   - the request rate (the shared pacer: 2 req/s overall, about 1 req/host/s);
   - the purpose: commerce discovery feeding UCP checkout on the merchant's own store.

## Rotation

1. Generate a new key and add it as a new version of the secret.
2. Roll `web` to a new revision so the directory serves the new key. The jobs read `:latest` at
   each execution, so they pick it up on their next run without a redeploy.
3. Re-run the directory check.

Cost: this module lists ONE key, and the directory is served with `Cache-Control: max-age=3600`. A
verifier holding the old directory can reject new signatures for up to an hour. During that hour our
requests may be treated as unsigned. That is the best case: whether Shopify treats a FAILED
signature like no signature, or worse, is unverified. So rotate outside the nightly crawl windows,
or extend the module to list the old and new keys together for one cache lifetime
(http-message-signatures-directory §5.1) before relying on rotation.

## Off

- Per job: `--update-env-vars=CRAWL_WEB_BOT_AUTH_ENABLED=false`, or `WEB_BOT_AUTH=off` on the setup
  script. Requests go back to today's bytes.
- Directory: `--remove-secrets=WEB_BOT_AUTH_PRIVATE_KEY` on `web` makes it 404.
