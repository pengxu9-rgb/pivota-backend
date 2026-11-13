# Runbook — unauthenticated `/agent/*` rate limiting

> **Production is GCP Cloud Run (`pivota-prod`, `us-west1`), not Railway.** Every command below was
> rewritten for it on 2026-08-25; a `railway variables --set` here would have turned the dial on the
> ROLLBACK while the incident continued. See
> [operating_on_gcp_production.md](operating_on_gcp_production.md).

Added 2026-08-11. Closes the gap where most unauthenticated `/agent/*` traffic
was unmetered and any keyed caller could evade its bucket by rotating the header.

## Scope correction (read this first)

An earlier draft of this runbook and of PR #1727 said unauthenticated `/agent/*`
traffic had **no rate limiting at all**. That was wrong for two routes:

- `/agent/shop/v1/invoke` — `SHOP_INVOKE_ANON_RPM`, per-IP, default 60, added by
  the 2026-08-08 audit (`routes/agent_shop_gateway.py`).
- `/agent/v1/citation/*` — `_citation_rate_limit`, tier `standard` = **1000 rpm
  per caller** (`routes/agent_citation_v1.py`).

The gap was real but narrower: *most* of `/agent/*` was unlimited for
unauthenticated callers, and every keyed caller could evade its bucket by
rotating the header.

**Unreconciled tension worth knowing:** the citation limiter advertises 1000 rpm
*per caller* while the new global ceiling is 1200 rpm for *all* `/agent/*`
combined. For a programme whose goal is to be crawled more, that ordering should
be revisited deliberately if agent traffic grows — see "Re-measure" below.

## What it replaced

`middleware/rate_limiter.py` began its keyless path with:

```python
# No API key = no rate limiting (will fail auth later)
if not api_key:
    return await call_next(request)
```

Two problems:

1. **Anonymous callers were unlimited.** The "will fail auth later" assumption
   stopped being true once the citation and discovery routes under `/agent/`
   became public by design — they answer `200` with no credentials.
2. **Keyed callers could rotate out of their bucket.** The per-key bucket keys on
   an *unvalidated* `x-api-key`, so `x-api-key: <random-per-request>` mints a
   fresh bucket every time and never trips.

## The two layers, and which one actually stops an attacker

| Layer | Env var | Default | Keyed on | Stops rotation? |
|---|---|---|---|---|
| Per-client | `ANON_RATE_LIMIT_PER_IP_RPM` | **0 — disabled** | leftmost `X-Forwarded-For` | No |
| Global ceiling | `ANON_RATE_LIMIT_GLOBAL_RPM` | `max(600, 10 × RATE_LIMIT_RPM)` = **1200** in prod | nothing | **Yes** |

### Why the per-client layer ships DISABLED

Adversarial review established that the aggregation point that matters is not
Railway's CGNAT pool — it is the **single egress IP of each server-side caller**.
The Node gateway and the Vercel UI each front *all* of their users from one
address, so a per-IP bucket throttles those users collectively rather than
isolating them. It is also redundant on the busiest public route:
`routes/agent_shop_gateway.py` has run `SHOP_INVOKE_ANON_RPM` (default 60,
per-IP) on `/agent/shop/v1/invoke` since the 2026-08-08 audit.

Enable it only once (a) a verified trusted-proxy hop count makes the identity
trustworthy, and (b) the aggregation points are exempt. Until then the global
ceiling carries the entire guarantee — which is the half that is rotation-proof
anyway.

### Why the ceiling is derived rather than flat

A flat 600 sat *below* what legitimate authenticated traffic can consume: each
distinct key may spend `RATE_LIMIT_RPM` (120 in prod), so **5 busy agents
exhausted it** and got the anonymous 429 with no per-agent headers to pace
against. Deriving it as `max(600, 10 × RATE_LIMIT_RPM)` keeps that relationship
from silently inverting when `RATE_LIMIT_RPM` is raised.

A limit of *N* allows exactly *N* requests per 60s window, then rejects with
`429` + `Retry-After`. The threshold is never published in a header or body (see
PR #1724). Both counter backends — Redis when `REDIS_URL` is set, in-memory
otherwise — use the same fixed-window algorithm, so the limit does not change
meaning depending on whether Redis is up.

### Why not `request.client.host`

Because behind Railway it was the platform's internal proxy, not the client.

**This changed at the 2026-08-22 cutover and the paragraphs below are pre-cutover analysis.**
Measured on Cloud Run 2026-08-25: `request.client.host` now carries real public client addresses
(`71.202.214.234`, `98.92.15.176`) and `httpRequest.remoteIp` agrees — zero `100.64.0.0/10` CGNAT
peers. So the per-IP dial is no longer inherently untrustworthy the way it was on Railway, and the
"Known limitation" below — establishing the trusted-proxy hop count for *Railway's* edge — is
obsolete rather than outstanding. Re-derive it for the GCP load balancer before enabling
`ANON_RATE_LIMIT_PER_IP_RPM`; do not carry the Railway conclusion across.
Sampled from prod access logs on 2026-08-11, **every** peer address was in
`100.64.0.0/10` (RFC 6598 CGNAT) across 12 distinct addresses:

```
INFO: 100.64.0.7:20882  - "GET /agent/internal/disputes HTTP/1.1" 200 OK
INFO: 100.64.0.12:47760 - "GET /agent/internal/disputes HTTP/1.1" 200 OK
```

Bucketing on that would collapse every external caller into a handful of shared
buckets, so one abuser would lock out everybody — a rate limiter that works as a
denial-of-service amplifier. It is therefore never used as the identity, and a
caller with no usable `X-Forwarded-For` gets **no** per-client bucket at all,
falling under the global ceiling only. `tests/test_anonymous_rate_limit.py`
pins this with explicit isolation tests.

## Why the defaults are what they are

Not guessed, but see "A note on the measured baseline" below for which evidence
is actually load-bearing — the first draft of this runbook justified the numbers
with a log-sampling method that does not hold up, even though its order of
magnitude was right.

The short version: 14 days of `agent_usage_logs` show a busiest hour of 43
requests (~0.72/min), so the 1200/min ceiling is roughly **1,600× measured
peak**. That is why enforcement ships enabled rather than defaulted off.

Re-measure before tightening:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="web" AND httpRequest.requestUrl!=""' \
  --project pivota-prod --limit 5000 \
  --format='value(httpRequest.requestUrl)' --freshness=2h \
  | sed -E 's|https?://[^/]+||; s|\?.*||' | grep -E '^/agent/' | sort | uniq -c | sort -rn
```

Three things matter in that command:

- **`--freshness` is required.** Without it the read starts at the beginning of
  retention and the command looks like it has hung. `--limit` is a hard cap, not
  a page size.
- **Request logs are structured**, so the path lives in `httpRequest.requestUrl`
  as a full URL — hence the `sed`. That record also carries `httpRequest.remoteIp`
  and `userAgent`, which is what identifies who consumed the budget; add them to
  `--format` when you need to attribute rather than count.
- **It is NOT bounded by the current deployment.** This was the Railway version's
  worst limitation — its deploy log reset on every redeploy, and prod redeployed
  six times on 2026-08-11. Cloud Logging spans revisions, so add
  `AND resource.labels.revision_name="web-000NN-xxx"` when you specifically want
  one build.

A better single number, straight from the aggregated table:

```bash
# Staff-only since S-09 (2026-11): pass X-ADMIN-KEY or an admin bearer token.
curl -s -H "X-ADMIN-KEY: $ADMIN_API_KEY" "https://api.pivota.cc/agent/v1/metrics/timeline?hours=336"
```

## Kill switch

If legitimate traffic is being rejected, disable enforcement — it takes effect on
the next deploy/restart because the value is read at middleware construction:

```bash
gcloud run services update web --project pivota-prod --region us-west1 \
  --update-env-vars ANON_RATE_LIMIT_ENABLED=false
```

Accepted falsy values: `false`, `0`, `no`, `off`.

**`--update-env-vars` MERGES.** `--set-env-vars` and `--env-vars-file` replace the
entire set and would wipe the other ~250 variables on `web`. Mid-incident is
exactly when that mistake gets made.

**On restarting:** the value is read once in `RateLimitMiddleware.__init__` at app
construction, so a new process is required. `gcloud run services update` creates a
new revision and shifts traffic to it once healthy, so the kill switch takes
effect on its own — no separate deploy step. Confirm NEW PROCESSES are serving by checking the
revision moved. **Not `/version`:** an env-only update reuses the same image, so the build SHA is
identical before and after whether or not the change applied.

```bash
gcloud run services describe web --project pivota-prod --region us-west1 \
  --format='value(status.latestReadyRevisionName)'
```

Prefer **raising the limit** over disabling, unless you are mid-incident:

```bash
gcloud run services update web --project pivota-prod --region us-west1 \
  --update-env-vars ANON_RATE_LIMIT_GLOBAL_RPM=5000
```

A non-positive or unparseable value falls back to the default rather than being
honoured — `int("0")` succeeds, and a `0` limit would reject *all* public agent
traffic, so that footgun is disarmed in `_positive_int`.

## Who is exempt — the concrete prod answer

Do not read the env-var list below as "these are all in play". Measured on prod
2026-08-11, only `SHOP_GATEWAY_AGENT_API_KEY` is set, so the middleware's trusted
set is **exactly one key** — and the Node gateway sends a *different*
`ak_live_*` value, so **the gateway is not rate-limit-exempt**. It authenticates
fine and has its own per-agent limit; it simply also draws from the ceiling. If
that becomes a problem, set `RATE_LIMIT_TRUSTED_API_KEYS` to the gateway's key.

Also note the two trusted-key sets in this codebase differ: the middleware reads
`RATE_LIMIT_TRUSTED_API_KEYS`/`AGENT_API_KEY`/`PIVOTA_API_KEY`/
`SHOP_GATEWAY_AGENT_API_KEY`, while `routes/agent_auth.py` reads a different
tuple for *authentication*. Today both reduce to the same single key, so the
asymmetry is dormant — but setting `PIVOTA_AGENT_API_KEY` to fix auth would
produce an auth-trusted caller that is **not** rate-limit-exempt.

## Who is exempt

- Keys in the middleware's trusted set (`RATE_LIMIT_TRUSTED_API_KEYS`,
  `AGENT_API_KEY`, `PIVOTA_API_KEY`, `SHOP_GATEWAY_AGENT_API_KEY`).
- A valid `X-Internal-Key` matching `AGENT_AUTH_INTROSPECT_INTERNAL_KEY`. **This
  one is a hard requirement, not a courtesy.** `/agent/internal/auth/introspect`
  is how the Node gateway validates *every* agent API key, from a single egress
  IP. A 429 there is classified `AUTH_INTROSPECT_REJECTED` by the gateway, a code
  deliberately excluded from its emergency-auth-fallback allowlist — so a 429 is
  treated *worse* than a 500 and returns **503 for every authenticated agent
  request**, with no negative caching to damp the retries. Without this
  exemption, a control aimed at anonymous abuse takes down authenticated
  commerce.
- A valid `X-ADMIN-KEY` matching `PROMOTIONS_ADMIN_KEY` or `ADMIN_API_KEY`,
  compared in constant time. This matters: `/agent/internal/disputes`
  authenticates with `X-ADMIN-KEY` rather than the agent dependency, sends no
  `x-api-key`, and is polled internally about every 30s — without the exemption
  the limiter would eventually throttle an internal poller.

Presenting *any* `X-ADMIN-KEY` is not enough; the value is checked.

## Failure behaviour

Fails **open**. A Redis outage returns a count of `0` rather than raising, so
infrastructure trouble cannot black-hole the public agent surface. The in-memory
fallback is capped (`ANON_RATE_LIMIT_MAX_TRACKED_KEYS`, default 20,000) and
pruned once per window; at the cap new keys stop being *tracked* rather than
evicting live ones, since eviction would let a rotating attacker flush a
legitimate client's counter.

## Verify after deploy

```bash
for i in $(seq 1 12); do
  curl -s -o /dev/null -w '%{http_code} ' "https://api.pivota.cc/agent/v1/citation/search?q=serum"
done; echo
```

All `200` at current volume — the default ceiling is far above a 12-request
burst. To confirm the mechanism is live rather than inert, set
`ANON_RATE_LIMIT_GLOBAL_RPM` to something small on **staging** and repeat; you
should see `200`s up to the limit then `429`s carrying `Retry-After` and no
`X-RateLimit-Limit`.

## When the ceiling fires, you will see it

`middleware/rate_limiter.py` logs `anon_rate_limit_ceiling_engaged` with the
path, the count, the limit and the caller identity, plus
`anon_rate_limit_ceiling_at_80pct` on the way up. This matters because
`StructuredLoggingMiddleware`'s JSON does **not** reach stdout in prod, so
without these lines an operator sees only uvicorn's bare `429` and cannot tell
the ceiling from a per-key bucket.

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="web"' \
  --project pivota-prod --limit 2000 --format='value(textPayload)' --freshness=2h \
  | grep anon_rate_limit_ceiling
```

## A note on the measured baseline

The 14-day figure below is trustworthy; the *method* in the first draft was not.

- **Trustworthy:** `GET /agent/v1/metrics/timeline?hours=336` aggregates
  `agent_usage_logs` — 624 `/agent/v1` requests over 14 days, busiest hour 43
  requests (~0.72/min). The ceiling is ~1,600× that peak.
- **Not trustworthy:** counting `/agent/*` lines out of raw logs. That
  buffer is bounded by the current deployment and dominated by scheduler noise
  (`run_executor_worker_tick` every 5s logging `skipped: maximum number of
  running instances reached`), so its denominator is not a time window.
- **Neither bounds the risky path.** `agent_usage_logs` only covers `/agent/v1`
  (`middleware/usage_logger.py`), so it excludes `/agent/shop/v1/invoke`,
  `/agent/v2/*` and all of `/agent/internal/*`.

## Known limitation / follow-up

The per-client layer will remain spoofable until there is a **verified**
trusted-proxy hop count for Railway's edge — i.e. knowing which
`X-Forwarded-For` element the platform appends, so the rightmost trustworthy
entry can be selected instead of the leftmost attacker-chosen one. That was not
established here (the platform's chain semantics were not confirmed
empirically), so the leftmost element is used, matching the convention already
used elsewhere in this repo for logging, and the global ceiling carries the
adversarial load. Establishing the hop count is the upgrade path.

## `/agent/shop/v1/invoke` money operations need a credential (2026-11-12)

Rate limiting is not authentication. The money operations on `/invoke` —
`create_order`, `create_payment_link`, `submit_payment`,
`record_payment_offer_evidence` (`INVOKE_MONEY_OPERATIONS` in
`routes/agent_shop_gateway.py`) — are proxied by `_proxy_agent_api`, which sends the
caller's `X-Checkout-Token` when there is one and otherwise the backend's OWN
`AGENT_API_KEY`. Without a credential check an anonymous caller acted as Pivota's own
agent. They now run only when the request carries:

- an `X-Checkout-Token` (forwarded as before; the `/agent/v1|v2` route validates it), or
- an agent API key (`X-API-Key`, or `Authorization: Bearer`) that resolves to an ACTIVE
  agent (`db.agents.resolve_agent_id_by_api_key`, the lookup `get_agent_context` uses;
  an internal trusted key is admitted as `get_agent_context` admits it).

Anything else answers `401` with `detail.error = CREDENTIAL_REQUIRED_FOR_MONEY_OPERATION`
before the payload is parsed, with no upstream call and no database write; the log line
`[shop_invoke] money operation refused` names the operation and a reason
(`no_credential`, `malformed_key`, `unknown_or_inactive_key`), never a header value.
Any key-lookup failure (transient or not) answers `503` (fail closed) and logs
`[shop_invoke] money operation key lookup failed`. A checkout token is the dispatcher's
stripped value, so a whitespace-only `X-Checkout-Token` counts as none. The per-IP
anonymous limiter runs first, so an over-budget anonymous call is `429`, not `401`.
Every other operation is unchanged, credential or not.

Rollback only — `SHOP_INVOKE_MONEY_OPS_REQUIRE_CREDENTIAL`, read per call, default ON.
Only `0`, `false`, `off` or `no` switches the check off:

```bash
gcloud run services update web --project pivota-prod --region us-west1 \
  --update-env-vars SHOP_INVOKE_MONEY_OPS_REQUIRE_CREDENTIAL=0
```

Not changed by that check: an admitted caller's request still goes upstream with the
backend's own key when it carries no checkout token, so the order is attributed to that
service agent, not to the caller's agent. The next section is the switch for that.

## `/agent/shop/v1/invoke`: a caller with its own key acts as itself

Flag `SHOP_INVOKE_FORWARD_CALLER_KEY`, read per call, **default OFF**. Only `1`, `true`,
`on` or `yes` switches it on; unset or anything else is off, and off is the request the
route sent before the flag existed.

With it on, `_proxy_agent_api` sends upstream, in this order: the caller's
`X-Checkout-Token` when there is one (unchanged); otherwise the agent key the caller
presented (`X-API-Key`, or `Authorization: Bearer`; the same value the money check
judged), as `X-API-Key`; otherwise, for a caller that presented no key, the backend's own
key as before (reachable only with the money check switched off). A presented key is
never replaced by the backend's own key: the `/agent/v1|v2` route judges it.

Only a key with the money check's own key format goes upstream (one rule,
`_agent_key_has_money_gate_format`: an internal trusted key, or the agent key shape). Any
other presented value (over-long, carrying CR/LF or another control character, non-ASCII,
two keys joined, the wrong shape) is answered by the proxy itself with `401` and the fixed
body `{"detail": {"detail": "Invalid API Key format"}}`, which carries none of the caller's
bytes; nothing is sent upstream and the backend's key is not used instead. This is
reachable only with the money check switched off, because that check refuses the same
values first with its own `401`.

So a keyed caller's money operation is authenticated, limited, scoped, counted and owned
as that caller's agent, not as the backend's service agent.

Calls that are served with the flag off and refused with it on (all pinned in
`tests/test_invoke_forwards_callers_own_key.py`):

| Caller | Answer with the flag on |
|---|---|
| Key whose `allowed_merchants` does not include the merchant (a new self-serve key has an empty list) | `403 Not authorized for this merchant` on `create_order`, `submit_payment`, `record_payment_offer_evidence` |
| Key over its own per-minute limit or daily quota | `429`, counted against that agent. The upstream `X-RateLimit-*` / `X-Quota-*` headers are not relayed. |
| Agent whose governance status is suspended or blocked | `403` on `create_order` and `create_payment_link`. `submit_payment` and `record_payment_offer_evidence` do not consult governance upstream and are still served. |
| Deactivated, unknown or malformed key | Already `401` at the money check. With that check switched off: `403 Agent is deactivated` / `401` from the upstream route (deactivated, unknown) or `401 Invalid API Key format` from the proxy with no upstream call (malformed), instead of being served on the backend's key. |
| `create_payment_link` for an order the caller does not own, including **an order it created through `/invoke` before the flag was switched on** (owned by the service agent) | `403 Not authorized for this order` |

Not refused, and worth knowing before switching on:

- `submit_payment` and `record_payment_offer_evidence` on an order created before the
  switch still work: those upstream routes compare the order's merchant with the caller's
  scope, not the order's owner.
- A caller's own `GET /agent/v1/orders/{id}` on an order created through `/invoke` before
  the switch is `403` before and after (the service agent owns it). Orders created after
  the switch are owned by the caller.
- `create_payment_link` for an order the caller created with its own key on the keyed API
  was `403` through `/invoke` and is now served.
- Each money operation counts TWICE against the caller in `middleware/rate_limiter.py`:
  once for the `/invoke` call and once for the loopback call the proxy makes with the same
  key. Both counters are hit twice:
  - the per-key bucket (keyed on the presented key): a keyed caller gets half as many
    money operations per minute from that bucket as it gets plain keyed calls;
  - the global ceiling (`_reject_global`, the budget shared by every `/agent/*` request
    that is not exempt): keyed money traffic fills it twice as fast.

  With the flag off the loopback call carries the backend's own key, which the middleware
  exempts from both counters (`trusted_api_keys`, unless `RATE_LIMIT_TRUST_INTERNAL_KEYS`
  is switched off), so the operation counted once. A
  caller's key is not exempt, and the loopback call is not exempted either. When the
  second count is the one that goes over, the loopback call is refused before its route
  runs, nothing is created or changed, and the caller receives that `429`. The per-agent
  limit and daily quota checked by the upstream route's own auth are counted once per
  operation.
- An empty `X-API-Key` header AHEAD of a real one is a request with no key. The key reader
  takes the first `X-API-Key` header; when it is empty or blank and there is no
  `Authorization: Bearer`, a later `X-API-Key` header is not read. The money check refuses
  such a request (`no_credential`). With that check switched off it is served on the
  backend's key like any keyless request, flag on or off. Pinned as it is in the test file.
- Keyless callers are not changed by this flag.

Before turning the flag on:

1. Count the keyed callers of the four money operations on `/invoke` first (requests that
   carry an agent key and no checkout token). They are the only callers this flag changes.
2. Among them, look specifically for self-serve keys whose `allowed_merchants` is an EMPTY
   list. With the flag on every `create_order`, `submit_payment` and
   `record_payment_offer_evidence` from such a key is `403 Not authorized for this
   merchant` (first row of the table above); with the flag off they are served. Give those
   keys their merchants first, or expect the refusals.
3. Look for in-flight payment links: orders those callers created through `/invoke` before
   the change that still need a payment link. Such an order is owned by the service agent,
   so `create_payment_link` for it with the caller's own key is `403 Not authorized for
   this order` once the flag is on (last row of the table above). Let those orders finish
   or expire before switching on, or switch on at a time when none are open.
4. Compare those callers' money-operation rate with their per-key budget in the
   middleware: it is used twice as fast (see above).

Switch on / roll back: an environment change only, no build. Set
`SHOP_INVOKE_FORWARD_CALLER_KEY=1` on the web service the same way the money check's
switch is set above; remove the variable (or set `0`) to roll back.
