# Runbook — unauthenticated `/agent/*` rate limiting

Added 2026-08-11. Closes the gap where unauthenticated callers to `/agent/*` were
not rate limited at all.

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
| Per-client | `ANON_RATE_LIMIT_PER_IP_RPM` | 60 | leftmost `X-Forwarded-For` | **No** |
| Global ceiling | `ANON_RATE_LIMIT_GLOBAL_RPM` | 600 | nothing | **Yes** |

**Be clear about the per-client layer.** `X-Forwarded-For` is caller-supplied and
the leftmost element is attacker-chosen, so an adversary defeats that bucket by
rotating the header. It buys fair isolation between real clients and protection
against runaway/buggy clients — it is not an adversarial control.

**The global ceiling is the anti-abuse guarantee.** It is a single budget for all
unauthenticated `/agent/*` traffic and keys on no caller-supplied value, so no
amount of header rotation escapes it.

A limit of *N* allows exactly *N* requests per 60s window, then rejects with
`429` + `Retry-After`. The threshold is never published in a header or body (see
PR #1724).

### Why not `request.client.host`

Because behind Railway it is the platform's internal proxy, not the client.
Sampled from prod access logs on 2026-08-11, **every** peer address was in
`100.64.0.0/10` (RFC 6598 CGNAT) across 12 distinct addresses:

```
INFO: 100.64.0.7:20882  - "GET /agent/internal/promotions HTTP/1.1" 200 OK
INFO: 100.64.0.12:47760 - "GET /agent/internal/promotions HTTP/1.1" 200 OK
```

Bucketing on that would collapse every external caller into a handful of shared
buckets, so one abuser would lock out everybody — a rate limiter that works as a
denial-of-service amplifier. It is therefore never used as the identity, and a
caller with no usable `X-Forwarded-For` gets **no** per-client bucket at all,
falling under the global ceiling only. `tests/test_anonymous_rate_limit.py`
pins this with explicit isolation tests.

## Why the defaults are what they are

Not guessed. Sampled from prod access logs over ~65 minutes on 2026-08-11 there
were **15 `/agent/*` requests in total** (~0.23/min), most of them authenticated
or internal. The global default of 600/min is roughly **2,600× the entire
measured volume**, so enforcement ships enabled.

Re-measure before tightening:

```bash
railway logs --service web | grep -oE '"(GET|POST) /agent/[^ ?"]*' | sort | uniq -c | sort -rn
```

## Kill switch

If legitimate traffic is being rejected, disable enforcement — it takes effect on
the next deploy/restart because the value is read at middleware construction:

```bash
railway variables --service web --set ANON_RATE_LIMIT_ENABLED=false
```

Then redeploy. Accepted falsy values: `false`, `0`, `no`, `off`.

Prefer **raising the limit** over disabling, unless you are mid-incident:

```bash
railway variables --service web --set ANON_RATE_LIMIT_GLOBAL_RPM=5000
```

A non-positive or unparseable value falls back to the default rather than being
honoured — `int("0")` succeeds, and a `0` limit would reject *all* public agent
traffic, so that footgun is disarmed in `_positive_int`.

## Who is exempt

- Keys in the middleware's trusted set (`RATE_LIMIT_TRUSTED_API_KEYS`,
  `AGENT_API_KEY`, `PIVOTA_API_KEY`, `SHOP_GATEWAY_AGENT_API_KEY`).
- A valid `X-ADMIN-KEY` matching `PROMOTIONS_ADMIN_KEY` or `ADMIN_API_KEY`,
  compared in constant time. This matters: `/agent/internal/promotions`
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

## Known limitation / follow-up

The per-client layer will remain spoofable until there is a **verified**
trusted-proxy hop count for Railway's edge — i.e. knowing which
`X-Forwarded-For` element the platform appends, so the rightmost trustworthy
entry can be selected instead of the leftmost attacker-chosen one. That was not
established here (the platform's chain semantics were not confirmed
empirically), so the leftmost element is used, matching the convention already
used elsewhere in this repo for logging, and the global ceiling carries the
adversarial load. Establishing the hop count is the upgrade path.
