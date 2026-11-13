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
`[shop_invoke] money operation key lookup failed` (with `SHOP_INVOKE_KEYLESS_MODE` in
`observe` or `enforce` the `503` is the same but that line is NOT written: the keyless step
made the lookup and wrote `[shop_invoke] keyless-mode key lookup failed` instead, under its
line budget; see that section). A checkout token is the dispatcher's
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

A trusted key that is itself not ASCII in the configuration half-works: it passes the
format rule (it is a trusted key) and is matched only by a sender that puts the same raw
bytes in the header, never by one that sends it as UTF-8; and with this flag on the proxy
cannot build the upstream header from it, so that caller's money operation fails with a
`500`. Keep every configured trusted key ASCII.

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

## `/agent/shop/v1/invoke` keyless callers: `SHOP_INVOKE_KEYLESS_MODE` (off | observe | enforce)

The money gate above covers four operations. Every other operation on `/invoke` still
serves anyone, and the per-address limiter is skipped by any request that merely carries
an `X-API-Key`, `Authorization` or `X-Checkout-Token` header, valid or not.
`SHOP_INVOKE_KEYLESS_MODE` closes the route to callers without a **verified** credential,
in steps. It is read per request, so changing it needs no deploy.

| Value | Behaviour |
|---|---|
| unset, empty, `off`, or any other value | Off. The code path before the flag existed: no classification, no key lookup, no line. |
| `observe` | **No answer changes.** The limiter is the one off runs (any credential header still skips it). After it, each request is classified and a caller without a verified credential gets one warning line. |
| `enforce` | A caller without a verified credential is answered `401` `CREDENTIAL_REQUIRED`. The per-address limiter covers every such caller: a header that verifies nothing no longer skips it. |

A header being present is not a credential. A credential is one of:

- the internal trusted key (`trusted_key`);
- an `X-Checkout-Token` with a valid signature, unexpired, naming an agent
  (`checkout_token`; verified in process, no database; a whitespace-only token is none);
- a well-formed agent key in `X-API-Key` or `Authorization: Bearer` that resolves to an
  ACTIVE agent (`agent_key`; the lookup only reads, it counts no quota, rate limit or stats).

Everything else is keyless: `none`, `malformed_key`, `unknown_or_inactive_key`,
`bad_checkout_token`. `lookup_error` (the key could not be looked up) is served and logged
in `observe`; in `enforce` it is `503` and is never counted by the limiter, so an outage
reads as `503` and not as `429`. A verified checkout token or the trusted key is checked
first, so those requests cost no lookup. `X-API-Key` is read before `Authorization`, as on
the keyed routes: a junk `X-API-Key` hides a good Bearer.

Order inside the handler in `observe`:

1. Body validation (a malformed envelope is still `422`).
2. The limiter, exactly as in off: only a request with none of the three headers is
   counted, and `429` over budget.
3. Classify the credential (a well-formed key is looked up; the auth cache answers a key it
   has seen). A caller without a verified credential gets the line. This step answers
   nothing and cannot fail the request.
4. The money gate (`SHOP_INVOKE_MONEY_OPS_REQUIRE_CREDENTIAL`). It is handed the class from
   step 3, so the key is not looked up a second time.
5. Dispatch.

Order inside the handler in `enforce`:

1. Body validation.
2. The in-process checks: the trusted key, a checkout token that verifies. A verified
   caller goes to step 6 and is never counted by the limiter.
3. No well-formed key: keyless without a lookup. The limiter counts the request (`429`
   over budget), then the line and the `401`.
4. A well-formed key whose answer the auth cache already holds: no query. An active
   agent's key goes to step 6, uncounted; any other is counted, logged and answered `401`.
5. A well-formed key the cache does not hold: the limiter is **consulted** first, without
   counting. An address that is over budget is answered `429` and the key is not looked
   up. Otherwise the key is looked up: an active agent's key goes to step 6, uncounted; a
   lookup that could not run is logged and answered `503`, uncounted; any other key is
   counted (`429` over budget), logged and answered `401`.
6. The money gate, handed the class, then dispatch.

What a flood costs in `enforce`:

- **Made-up well-formed keys from one address:** at most `SHOP_INVOKE_ANON_RPM` (default
  60) key lookups per minute for that address, per instance; every request after that is a
  `429` with no lookup. Each looked-up key leaves a short-lived not-found entry in the auth
  cache, as it does on every keyed route.
- **The price of that:** a real key that is not in the auth cache (first use, or its cache
  entry has expired), sent from an address that is over budget, is answered `429` without a
  lookup until that address has budget again. It is not counted. Callers that share one
  address with keyless traffic (a server proxy that does not forward its caller's address)
  share this.
- The limiter keys on the first `X-Forwarded-For` entry, which the caller writes. A caller
  that rotates it has a fresh budget each time, here as on every limiter keyed this way.
  The lookup bound above is per address, so it does not hold against that caller.

In `observe` every request with a well-formed key the cache has not seen costs one lookup,
because a credential header still skips the limiter (the price of changing no answer).

With the money gate: in `observe` a keyless money operation still gets the money gate's
own `401` (and both lines). In `enforce` the keyless `401` comes first, whatever the money
gate's switch says, and a made-up checkout token (which the money gate passes on for the
upstream route to reject) is refused here. In off the money gate looks the key up itself,
as before.

The refusal is the same bytes for every keyless request, whatever the operation (known,
unknown or empty), payload or headers. It names no operation and no reason:

```
HTTP 401
WWW-Authenticate: Bearer
{"detail":{"error":"CREDENTIAL_REQUIRED","message":"This endpoint needs a credential. Send an active agent API key in X-API-Key or Authorization: Bearer."}}
```

The line, at warning level, once per keyless request the limiter did not answer (a `429`
gets no line):

```
[shop_invoke] keyless request: mode=<observe|enforce> op=<name|other> cred=<none|malformed_key|unknown_or_inactive_key|bad_checkout_token|lookup_error> ua=<node|axios|python-httpx|python-other|curl|browser|go|other|none> origin=<first_party|other|none> hop=<0|1> source=<known label|other|none> request_id=<id|h_<hash>|none>
```

- `op`: the operation only when it is one the dispatcher knows, else `other`.
- `ua`: a family from a fixed prefix table, never the header value.
- `origin`: whether an `Origin` header is present and first-party, never its value.
- `hop`: whether the first-party proxy's hop header is present. A hint, not proof.
- `source`: `metadata.source` only when it normalises to a known surface label.
- `request_id`: the request log's id for the same request, to join on. A caller-supplied
  id that is not a plain id (`[A-Za-z0-9._-]`, at most 64 characters) is logged as `h_` plus
  a short hash of it.

No header value, address, query or payload is ever in the line.

### The line budget: `SHOP_INVOKE_KEYLESS_LOG_LINES_PER_MINUTE`

The limiter does not bound the lines (its address is the caller's to choose), so the lines
have their own budget: per instance, per minute, **per label**. Default `60`. A whole
number from `1` to `10000`; anything else (unset, `0`, negative, not a number, larger) is
the default. Read per request.

The labels are the five `cred=` classes above, plus `key_lookup_failed` for the warning
`[shop_invoke] keyless-mode key lookup failed: <error type>` and `classify_failed` for
`[shop_invoke] keyless-mode classification failed: <error type>` (observe only: the request
is served as in off). One class running out of budget does not silence another.

When the classification or a line itself fails:

- `observe`: the request is answered exactly as in off. The money gate is handed no class
  and looks the key up itself, as in off, so a failed classification can never admit a money
  operation. The failure is logged as `classification failed` when that line can be written,
  and is not logged at all when it cannot.
- `enforce`: nothing is served. The step has no catch-all, so the request is an unhandled
  error (`500`) before the money gate and before any operation runs. A caller the failing
  step is not reached for (a verified one, when only the line fails) is answered as usual.

When a label ran out, one summary line is written after its minute is over, at the next
keyless request of any label:

```
[shop_invoke] keyless lines suppressed: label=<label> suppressed=<count>
```

So a flood costs at most budget + 1 lines per label per minute per instance, whatever
addresses or keys it rotates. A count in `observe` is therefore the lines plus the
`suppressed=` numbers; the request log still has every request, joinable on `request_id`.

### Enforce checklist

Do not set `enforce` until every line is true.

1. `observe` has run long enough to cover every first-party caller's traffic pattern, and
   the lines (plus the `suppressed=` counts) show no first-party traffic under any `cred=`.
2. Every first-party caller of `/invoke` sends a credential that verifies. A caller that
   sends none shows up in `observe` as `cred=none`.
3. Every key a first-party server proxy sends or forwards is an ACTIVE agent of this
   backend. That includes any fast-path service key and any emergency fallback key the
   proxy accepts on its own side without asking this backend: such a key shows up as
   `cred=unknown_or_inactive_key` here.
4. First-party server clients whose key comes from an environment variable have it set in
   every environment. Two such clients send no key at all when theirs is unset, and show
   up as `cred=none`.
5. The shopping UI's review lane sends a checkout token alone when it has one. A token
   older than its lifetime (one hour by default) is `cred=bad_checkout_token`, so under
   `enforce` that lane gets `401` until it also sends a key or refreshes the token.
6. This backend's own fallback hop to another instance's `/invoke`, and every script that
   calls `/invoke`, send a credential.
7. First-party server proxies forward their caller's address, or their keyless traffic is
   under the per-address budget: callers behind one address share that budget, and a real
   key not yet in the auth cache gets `429` while the address is over it (see above).
8. Accepted as built: a forged or expired token is refused; a deactivated agent's unexpired
   token still admits reads until it expires (the money path rechecks the agent); a junk
   `X-API-Key` beside a good Bearer is keyless.

Not built yet. Each of these is built, or accepted in writing by the owner, before
`enforce`:

9. A real key from an over-budget address is not denied. Today it is answered `429`
   unlooked until the minute is over (and, during a lookup outage, `429` rather than `503`;
   both carry `Retry-After`). Give an over-budget address a small separate lookup
   allowance per minute, or let a recently expired positive auth-cache entry count as
   "the cache holds it".
10. The per-address limiter keys on the address our own front end appends to
    `X-Forwarded-For`, not on the first entry, which the caller writes. Until then the
    lookup bound and the `429` do not hold against a caller that rotates that entry.
11. The suppressed-lines summary has a window or a timer. Today it is written at the next
    keyless request of any label, however much later that is, and carries no window: write
    one line at the first suppression, or on a timer, and put the window in the summary.
12. The money gate's `key lookup failed` line is dropped in `observe` and `enforce` (the
    keyless step's budgeted line replaces it). Write both, or move every alert and search
    that reads the old line to the new one.
13. The line budget is per class only. One dominant caller can use up a class's lines every
    minute and hide the rare callers `observe` exists to find: key the budget on the whole
    label set of the line (every field has a fixed vocabulary, so it stays bounded).
14. Lookups per address are unbounded in `observe`: every well-formed key the auth cache
    has not seen costs one lookup, as on the keyed routes. Watch database load while
    `observe` is on, and decide whether that is acceptable before `enforce` is planned.
15. The change that forwards a caller's own key and compares the trusted key as bytes is
    deployed. Before it, a key that is not ASCII made `enforce` answer `500`, not `401`.

Rollback from `enforce`: set the flag to `observe` or remove it.

### The recording behind the off and observe tests

`tests/test_shop_invoke_keyless_mode.py` compares off and observe, case for case, with
`tests/fixtures/shop_invoke_keyless_mode_parent_recording.json`: what main answers without
this flag's code (status, headers, body, what the handlers and the upstream saw).

Regenerate it when, and only when, a change **on main** legitimately changes one of those
answers (the tests then fail in off as well as in observe), or when a case is added to the
matrix in the test file. Never regenerate it to make a change to the keyless code pass:
a difference there is the defect the tests exist to catch.

It must come from a clean tree of main, never from a working tree or a branch: export the
commit into an empty directory and record there. The recorder unsets the flag (and
`SHOP_INVOKE_FORWARD_CALLER_KEY`, the money gate's switch and the limiter's settings), so on
a main that already has the keyless code it records the off path.

```
git archive <main commit> | tar -x -C <empty dir>
cd <empty dir>
python -c "import sys, importlib.util; sys.path.insert(0, '.'); \
  spec = importlib.util.spec_from_file_location('rec', '<repo>/tests/test_shop_invoke_keyless_mode.py'); \
  m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); \
  m.record_parent_matrix('<repo>/tests/fixtures/shop_invoke_keyless_mode_parent_recording.json')"
```

`record_parent_matrix` refuses to run when the routes module it imported is not the one in
the working directory, or when the flag is not off. Review the diff of the recording: every
changed case must be explained by the change on main, and a recording made on a main that
has the keyless code is only as good as that main's off branch in the handler, so check
that branch is untouched by the same change.
