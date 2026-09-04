#!/usr/bin/env python3
"""Read-only diagnostics for the Store Audit UCP probe lane.

Answers, from INSIDE the VPC, the two questions that are unanswerable from a
laptop because Cloud SQL is private-IP only:

  1. Would the checkout-tested tier ever fire? Counts active UCP routes whose
     merchant association passes the same predicate
     jobs/scheduled_ucp_reprobe_job applies. At 0, flipping
     STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED changes nothing.
  2. Why do clean storefronts report `inconclusive`? Prints the recorded
     status/error_message/evidence for the probed domains, plus a 7-day
     histogram of every reason the lane has produced.

SELECTs only — no INSERT, UPDATE, DELETE or DDL anywhere in this file. It is
the read-only twin of GET /ops/store-audit/{domain-diagnostics,checkout-tier-coverage}
(routes/store_audit_ops.py), which exists on main but is not deployed while the
deploy path is blocked.

RUN IT ON THE CRAWL SUBNET OR THE OUTBOUND HALF PROVES NOTHING.
store-audit-ucp-probe runs with network-interfaces
`[{"network":"default","subnetwork":"pivota-crawl"}]` and vpc-access-egress
all-traffic — its own subnet, and therefore its own Cloud NAT egress address.
The reprobe-enqueue job uses `default` instead. A reachability test from the
wrong subnet exercises a different path than the one that fails, so the job
must be created with:

    --network=default --subnet=pivota-crawl --vpc-egress=all-traffic
    --service-account=sa-store-audit-ucp-crawl@pivota-prod.iam.gserviceaccount.com

Run as a Cloud Run job, the same shape as scripts/run_scheduled_ucp_reprobes.py:
    python scripts/diagnose_store_audit_lane.py

URLs are scrubbed from free text before printing, because Cloud Run logs are a
wider audience than the row is.
"""
import asyncio, http.client, ipaddress, json, re, socket, ssl, time
from db.database import database

URL = re.compile(r"https?://\S*", re.I)
def scrub(v):
    return URL.sub("[url]", v) if isinstance(v, str) else v

DOMAINS = ["cosrx.com", "judydoll.com", "anua.com", "podl.us",
           "murad.com", "rovectin.com"]

COVERAGE = """
WITH proven AS (
  SELECT domain, merchant_id FROM merchant_official_domains
   WHERE verification_status = 'verified'
     AND source = 'verified'
     AND (liveness_status IS NULL OR liveness_status <> 'dead')
), sole_domain AS (
  SELECT domain FROM proven GROUP BY domain HAVING count(DISTINCT merchant_id) = 1
), sole_merchant AS (
  SELECT merchant_id FROM proven GROUP BY merchant_id HAVING count(DISTINCT domain) = 1
)
SELECT
  (SELECT count(*) FROM execution_routes
     WHERE route_kind='ucp' AND is_active) AS active_ucp_routes,
  (SELECT count(DISTINCT er.execution_route_id)
     FROM execution_routes er
     JOIN proven p ON p.domain = er.normalized_domain
     JOIN sole_domain sd ON sd.domain = p.domain
     JOIN sole_merchant sm ON sm.merchant_id = p.merchant_id
    WHERE er.route_kind='ucp' AND er.is_active
      AND er.last_audit_run_id IS NOT NULL) AS routes_with_proven_merchant,
  (SELECT count(*) FROM merchant_official_domains
     WHERE verification_status='verified') AS verified_domain_rows,
  (SELECT count(*) FROM merchant_official_domains
     WHERE verification_status='verified' AND source='verified')
        AS verified_and_bound_rows
"""

HISTORY = """
SELECT er.normalized_domain AS domain, er.route_kind, er.is_active,
       vr.status, vr.error_message, vr.evidence_jsonb,
       vr.retry_count, vr.created_at, vr.completed_at
  FROM verification_runs vr
  JOIN execution_routes er ON er.execution_route_id = vr.execution_route_id
 WHERE vr.verifier_id = 'ucp_probe'
   AND er.normalized_domain = ANY(:domains)
 ORDER BY vr.created_at DESC
 LIMIT 60
"""

REASONS = """
SELECT vr.status, vr.error_message, count(*) AS n
  FROM verification_runs vr
 WHERE vr.verifier_id = 'ucp_probe'
   AND vr.created_at > now() - interval '7 days'
 GROUP BY 1, 2 ORDER BY n DESC LIMIT 25
"""

# ---------------------------------------------------------------------
# Outbound reachability, from wherever this job runs
# ---------------------------------------------------------------------

# What the gateway client sends (src/services/ucpBuyerAgentClient.js): its own
# UA, Accept: application/json, and a 4s timeout with one retry. Reproduced
# exactly — a test with different headers or a longer timeout answers a
# different question than the one the probe is failing.
_UA = "Pivota-UCP-BuyerAgent/1.0"
_TIMEOUT = 4.0


def _dns(host):
    """Resolve, and say whether every address is public.

    The client refuses a merchant endpoint that resolves to a non-public
    address, and that refusal THROWS — which surfaces as `profile_unreachable`,
    indistinguishable from a timeout or a TLS reset in the stored evidence.
    Printing the addresses is what separates them.
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}
    addrs = sorted({i[4][0] for i in infos})
    verdict = []
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
            verdict.append({"addr": a, "public": ip.is_global})
        except ValueError:
            verdict.append({"addr": a, "public": None})
    return {"addresses": verdict}


def _request(host, method, path, body=None, headers=None):
    """One request, no redirect following, with the exception preserved.

    `profile_unreachable` is the branch where the fetch THREW, so the exception
    type and message are the entire diagnosis. A status code means it did not
    throw and the failure is elsewhere.
    """
    started = time.time()
    conn = None
    try:
        conn = http.client.HTTPSConnection(
            host, 443, timeout=_TIMEOUT, context=ssl.create_default_context()
        )
        conn.request(method, path, body=body,
                     headers=headers or {"accept": "application/json",
                                         "user-agent": _UA})
        res = conn.getresponse()
        payload = res.read(2048)
        return {
            "status": res.status,
            "location": scrub(res.getheader("location") or "") or None,
            "bytes": len(payload),
            "ms": int((time.time() - started) * 1000),
        }
    except Exception as exc:
        return {
            "threw": f"{type(exc).__name__}: {exc}"[:300],
            "ms": int((time.time() - started) * 1000),
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def outbound_report():
    out = []
    for host in DOMAINS:
        row = {"domain": host, "dns": _dns(host)}
        # 1. What the PROBE does: the well-known profile, redirects NOT followed.
        row["profile"] = _request(host, "GET", "/.well-known/ucp")
        # 2. What the BACKEND caller does: the pinned MCP path. It reaches these
        #    doors from the web service, so a split verdict here says the
        #    failure is the crawl subnet's egress and not the merchant.
        #    tools/list only — never tools/call, which the merchant treats as a
        #    grant and which this diagnostic has no business issuing.
        row["pinned_mcp"] = _request(
            host, "POST", "/api/ucp/mcp",
            body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            headers={"content-type": "application/json",
                     "accept": "application/json, text/event-stream",
                     "user-agent": _UA},
        )
        out.append(row)
    return out


async def main():
    await database.connect()
    out = {}
    cov = await database.fetch_one(COVERAGE)
    out["coverage"] = dict(cov) if cov else None

    rows = await database.fetch_all(HISTORY, {"domains": DOMAINS})
    hist = []
    for r in rows:
        d = dict(r)
        ev = d.get("evidence_jsonb")
        if isinstance(ev, str):
            try: ev = json.loads(ev)
            except Exception: ev = {"raw": ev[:200]}
        if isinstance(ev, dict):
            ev = {k: scrub(v) for k, v in ev.items()
                  if k in ("reason", "verification_status", "stage", "message",
                           "observed_at", "verifier_id")}
        hist.append({
            "domain": d["domain"], "route_kind": d["route_kind"],
            "active": d["is_active"], "status": d["status"],
            "error": scrub(d.get("error_message") or "")[:300],
            "evidence": ev,
            "created": str(d.get("created_at"))[:19],
        })
    out["per_domain"] = hist

    out["reason_histogram_7d"] = [
        {"status": r["status"], "error": scrub(r["error_message"] or "")[:160],
         "n": r["n"]}
        for r in await database.fetch_all(REASONS)
    ]
    out["outbound"] = outbound_report()
    print("LANE_DIAG " + json.dumps(out, default=str))
    await database.disconnect()

asyncio.run(main())
