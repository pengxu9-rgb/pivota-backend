# Runbook — unauthenticated `/agent/*` rate limiting

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

Because behind Railway it is the platform's internal proxy, not the client.
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
railway logs --service web -n 5000 --http | grep -oE '"path":"/agent/[^"]*' | sort | uniq -c | sort -rn
```

Three things matter in that command, and the earlier version of this runbook got
them wrong:

- **`-n` is required.** `railway logs` *streams* by default, so `sort` never sees
  EOF and the pipeline hangs forever, printing nothing. macOS has no
  `timeout(1)`, so an operator cannot rescue it with a prefix.
- **`--http` is the better source.** It carries the real `srcIp`, `edgeRegion`
  and `clientUa` — what you need to identify who consumed the budget. The deploy
  log does not.
- **The deploy log is bounded by the CURRENT deployment**, so after a redeploy
  there is no long window left to sample. Prod redeployed six times on
  2026-08-11.

A better single number, straight from the aggregated table:

```bash
curl -s "https://api.pivota.cc/agent/v1/metrics/timeline?hours=336"
```

## Kill switch

If legitimate traffic is being rejected, disable enforcement — it takes effect on
the next deploy/restart because the value is read at middleware construction:

```bash
railway variables --service web --set ANON_RATE_LIMIT_ENABLED=false
```

Accepted falsy values: `false`, `0`, `no`, `off`.

**On redeploying:** the value is read once in `RateLimitMiddleware.__init__` at
app construction, so a new process is required. `railway variables --set`
*triggers a deploy by default*, so normally you need do nothing further — but if
you pass `--skip-deploys` (or set the variable in the dashboard without
deploying) **the kill switch stays inert**. Confirm the running build changed:

```bash
curl -s https://api.pivota.cc/version
```

Prefer **raising the limit** over disabling, unless you are mid-incident:

```bash
railway variables --service web --set ANON_RATE_LIMIT_GLOBAL_RPM=5000
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
railway logs --service web -n 2000 | grep anon_rate_limit_ceiling
```

## A note on the measured baseline

The 14-day figure below is trustworthy; the *method* in the first draft was not.

- **Trustworthy:** `GET /agent/v1/metrics/timeline?hours=336` aggregates
  `agent_usage_logs` — 624 `/agent/v1` requests over 14 days, busiest hour 43
  requests (~0.72/min). The ceiling is ~1,600× that peak.
- **Not trustworthy:** counting `/agent/*` lines out of `railway logs`. That
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
