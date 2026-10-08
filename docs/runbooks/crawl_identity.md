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
| Reap cart proofs, enrichment (`jobs/enrichment_cart_variant_proof.py::no_cookie_client`) | yes |
| Reap cart proofs, mirror (`jobs/reap_cart_proof_refresh.py`, `scripts/backfill_shopify_variant_ids.py`) | yes |
| Tier B, purchasability sweep (`jobs/tierb_cart_link_eligibility.py::PacedTransport`) | NOT YET: separate PR, after the sweep throttle fix lands. They also still send a desktop-Chrome UA |
| curated brand feed, retailer-ingest drain, destination sweep | NOT YET |

To wire a lane, pass `**crawl_identity.transport_kwargs()` to its `httpx.AsyncClient(...)`. Or wrap
its existing transport with `crawl_identity.crawl_transport(inner)`. Signing happens in the
transport, so manually followed redirect hops are signed too.

## Switches

| env | where | effect |
|---|---|---|
| `WEB_BOT_AUTH_PRIVATE_KEY` | secret; on `web` and on every signing job | Ed25519 PKCS#8 PEM. On `web` it makes the directory answer; elsewhere it is the signing key |
| `CRAWL_WEB_BOT_AUTH_ENABLED` | the crawl jobs only | `true` = sign. Unset = today's bytes exactly. On without a usable key = unsigned, plus one ERROR line |
| `WEB_BOT_AUTH_SIGNATURE_AGENT` | optional | default `https://api.pivota.cc`, the origin serving the directory |
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
   and Tier B jobs), and the default compute SA that `external-referral-refresh` runs as.
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
6. Measure before enabling at scale. Run one signed request from the crawl subnet to a store that
   429s today, next to an unsigned one (experiment E3 in the report). If the signed request gets
   200 where the unsigned one gets `local_rate_limited`, signing works. If both get 429, Shopify
   wants the higher-tier form first. Either way, file the form (step 8).
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
   - the User-Agent (`PivotaBot/1.0; +https://pivota.cc`);
   - the egress IP 34.82.199.35;
   - the request rate (the shared pacer: 2 req/s overall, about 1 req/host/s);
   - the purpose: commerce discovery feeding UCP checkout on the merchant's own store.

## Rotation

Generate a new key, add it as a new secret version, and redeploy `web` (directory) and the jobs.
This module serves one key, so for a few minutes the directory and the jobs may disagree. A request
signed by a key the directory no longer lists fails verification and gets the unsigned tier: nothing
breaks, it is slower for those minutes. Re-run the directory check after rotating.

## Off

- Per job: `--update-env-vars=CRAWL_WEB_BOT_AUTH_ENABLED=false`, or `WEB_BOT_AUTH=off` on the setup
  script. Requests go back to today's bytes.
- Directory: `--remove-secrets=WEB_BOT_AUTH_PRIVATE_KEY` on `web` makes it 404.
