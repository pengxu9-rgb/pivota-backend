#!/usr/bin/env python3
"""Read-only diagnostics for the Store Audit UCP probe lane.

Answers, from INSIDE the VPC, the two questions that are unanswerable from a
laptop because Cloud SQL is private-IP only:

  1. Would the checkout-tested tier ever fire? Counts active UCP routes whose
     merchant association passes the same predicate
     jobs/scheduled_ucp_reprobe_job applies. At 0, flipping
     STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED changes nothing. Above 0 it is
     an UPPER BOUND: like the endpoint, it stops short of the final conjunct —
     whether that merchant's catalogue yields an in-stock variant.
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
    --service-account=sa-store-audit-ucp-selector@pivota-prod.iam.gserviceaccount.com

The SELECTOR account, not the crawl one. Egress follows the SUBNET, and only the
selector SA is granted DATABASE_URL (infra/gcp/setup_store_audit_ucp_jobs.sh);
the crawl SA has run.invoker and no database access, so this job would start and
then fail at connect.

Run as a MODULE, not by script path:
    python -m scripts.diagnose_store_audit_lane

`python scripts/diagnose_store_audit_lane.py` puts /app/scripts on sys.path[0]
rather than /app, so `from db.database import ...` raises ModuleNotFoundError.
The sibling job scripts share that hazard; -m is correct either way.

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
# MAX_MERCHANT_RESPONSE_BYTES in ucpBuyerAgentClient.js: past this the
# client throws, which the probe stores as profile_unreachable.
_MAX_MERCHANT_BYTES = 2 * 1024 * 1024


# The client's refusal list, transcribed from isForbiddenNetworkAddress in
# PIVOTA-Agent src/services/ucpBuyerAgentClient.js. NOT ipaddress.is_global:
# that returns True for 224.0.0.0/4, 192.88.99.0/24, 64:ff9b::/96 and
# ::ffff:0:0/96, every one of which the client REFUSES. Using is_global would
# print "public: True" for an address the client throws on — a false all-clear
# on precisely the mechanism this function exists to identify.
_FORBIDDEN = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "::/128", "::1/128", "::/96", "::ffff:0:0/96", "64:ff9b::/96",
    "100::/64", "2001:db8::/32", "2001:2::/48", "fc00::/7", "fe80::/10",
    "ff00::/8",
))


def _client_would_refuse(addr):
    """Would the gateway client refuse this address? Unknown family fails CLOSED,
    matching the client's `return true` for a non-IP."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    return any(ip in net for net in _FORBIDDEN if ip.version == net.version)


def _dns(host):
    """Resolve, and say whether the CLIENT would refuse any address.

    A refused address throws inside the client and is stored as
    `profile_unreachable` — the same string a timeout or a TLS reset produces.
    Separating them is the entire reason this runs. The client refuses on ANY
    forbidden record in a mixed answer, so `refused` is reported per address and
    `client_refuses` is the disjunction.
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        return {"error": scrub(f"{type(exc).__name__}: {exc}")[:200]}
    addrs = sorted({i[4][0] for i in infos})
    verdict = [{"addr": a, "refused": _client_would_refuse(a)} for a in addrs]
    return {
        "addresses": verdict,
        "client_refuses": any(v["refused"] for v in verdict),
    }


def _request(host, method, path, body=None, headers=None, budget=_TIMEOUT,
             addr=None):
    """One request, optionally pinned to ONE resolved address, verdict-faithful.

    PINNED PER ADDRESS ON PURPOSE. judydoll.com resolves v6-first; Python has no
    Happy Eyeballs, while the Node client's autoSelectFamily abandons an address
    after 250ms. So if the crawl subnet blackholes v6 rather than refusing it,
    an unpinned attempt burns the whole budget on v6 and reports a timeout for a
    host the client reaches over v4 — a false FAILURE on one of the very domains
    under investigation. Attempting each address separately turns that from a
    wrong answer into the answer.

    WHAT THE BUDGET DOES AND DOES NOT BOUND. It bounds the VERDICT, not the wall
    clock: DNS inside getaddrinfo takes as long as it takes, and a phase already
    in flight is not interrupted — an overrun is detected after the fact. A slow
    host can therefore exceed `budget` in elapsed time and still be reported
    correctly as a throw. Do not read `ms` as bounded by it.

    A 2xx IS NOT A SUCCESS FOR THE CLIENT. discoverEndpoint does an unguarded
    `res.json()` inside the same try whose catch is PROFILE_UNREACHABLE, so a
    WAF interstitial served as 200 text/html — the canonical datacenter-egress
    symptom, and invisible from a laptop — THROWS there while reading as a clean
    200 here. `json_ok` is therefore part of the verdict, not decoration, and so
    is `oversize`: the client caps a merchant response at 2 MiB and throws past
    it.
    """
    deadline = time.monotonic() + budget
    started = time.time()

    def _left():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"total budget of {budget}s exhausted")
        return remaining

    conn = None
    try:
        conn = http.client.HTTPSConnection(host, 443, timeout=_left())
        if addr:
            raw = socket.create_connection((addr, 443), timeout=_left())
            conn.sock = ssl.create_default_context().wrap_socket(
                raw, server_hostname=host
            )
        else:
            conn.connect()
        # Re-arm ONCE, before the exchange. Not after getresponse(): that closes
        # the connection and sets conn.sock=None whenever the response says
        # `Connection: close`, is HTTP/1.0, or carries neither Content-Length
        # nor chunked encoding — so touching it there raised AttributeError and
        # reported a REACHABLE host as a throw, on exactly the edge/WAF/proxy
        # error responses this exists to classify. res.fp reads through the same
        # socket this timeout is set on.
        conn.sock.settimeout(_left())
        conn.request(method, path, body=body,
                     headers=headers or {"accept": "application/json",
                                         "user-agent": _UA})
        res = conn.getresponse()
        # cap+1, not everything: the client destroys the request one byte past
        # its cap, so this yields the identical oversize verdict without
        # buffering hundreds of megabytes of an error page into job memory.
        payload = res.read(_MAX_MERCHANT_BYTES + 1)
        _left()                     # reading may have exhausted it
        out = {
            "status": res.status,
            "location": scrub(res.getheader("location") or "") or None,
            "bytes": len(payload),
            "ms": int((time.time() - started) * 1000),
        }
        if addr:
            out["addr"] = addr
        # STATUS-BLIND, because the client's cap is: it lives in the response
        # data handler and fires for every status, so a 3 MiB error page throws
        # in discovery and is stored as profile_unreachable while a bare
        # `status: 404` here would read as a clean refusal.
        out["oversize"] = len(payload) > _MAX_MERCHANT_BYTES
        if 200 <= res.status < 300:
            try:
                # Decoded leniently on purpose. res.json() builds a WHATWG
                # Response over a Buffer and uses the spec's NON-FATAL decoder,
                # so a stray Latin-1 byte in a merchant name parses there. A
                # strict decode here would report json_ok: False for a profile
                # the client read fine — a false FAILURE, and the reader would
                # take it as the explanation for profile_unreachable.
                json.loads(payload.decode("utf-8-sig", "replace"))
                out["json_ok"] = True
            except Exception:
                # The client would have thrown here and the probe would have
                # stored profile_unreachable. Saying "200" alone would exonerate
                # the network for a body that is the whole problem.
                out["json_ok"] = False
        return out
    except Exception as exc:
        out = {
            "threw": scrub(f"{type(exc).__name__}: {exc}")[:300],
            "ms": int((time.time() - started) * 1000),
        }
        if addr:
            out["addr"] = addr
        return out
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
        # And once per resolved address, so a v6 blackhole shows up as itself
        # rather than as "this host is unreachable".
        addrs = [a["addr"] for a in row["dns"].get("addresses", [])]
        if len(addrs) > 1:
            row["profile_per_address"] = [
                _request(host, "GET", "/.well-known/ucp", addr=a) for a in addrs
            ]
        # 2. The PINNED MCP path, under this script's own request shape — NOT a
        #    reproduction of services/merchant_ucp_checkout.py, which uses
        #    httpx's default UA, `accept: */*`, a 12s budget and follows one
        #    apex<->www redirect. Reading a split verdict as "the crawl subnet
        #    is at fault" would therefore be unlicensed: it could equally be the
        #    UA or the budget. What it DOES isolate is path-vs-path from one
        #    place at one moment: profile refused while /api/ucp/mcp answers
        #    means the door is up and discovery is what fails.
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
    out = {}
    try:
        await database.connect()
        cov = await database.fetch_one(COVERAGE)
        out["coverage"] = dict(cov) if cov else None

        rows = await database.fetch_all(HISTORY, {"domains": DOMAINS})
        hist = []
        for r in rows:
            d = dict(r)
            ev = d.get("evidence_jsonb")
            if isinstance(ev, str):
                try:
                    ev = json.loads(ev)
                except Exception:
                    ev = {"raw": ev[:200]}
            if isinstance(ev, dict):
                ev = {k: scrub(v) for k, v in ev.items()
                      if k in ("reason", "verification_status", "stage",
                               "message", "observed_at", "verifier_id")}
            hist.append({
                "domain": d["domain"], "route_kind": d["route_kind"],
                "active": d["is_active"], "status": d["status"],
                "error": scrub(d.get("error_message") or "")[:300],
                "evidence": ev,
                "created": str(d.get("created_at"))[:19],
            })
        out["per_domain"] = hist

        out["reason_histogram_7d"] = [
            {"status": r["status"],
             "error": scrub(r["error_message"] or "")[:160], "n": r["n"]}
            for r in await database.fetch_all(REASONS)
        ]
    except Exception as exc:
        # The DB half failing must not cost the outbound half. Losing both to
        # one traceback is how a diagnostic run gets spent for nothing.
        out["db_error"] = scrub(f"{type(exc).__name__}: {exc}")[:300]
    finally:
        try:
            await database.disconnect()
        except Exception:
            pass

    # PRINTED INCREMENTALLY, because an endless drip on one host lets the job
    # hit its --task-timeout and be killed. A single print at the end would take
    # the DB half down with it, so each half is emitted as soon as it exists.
    print("LANE_DIAG_DB " + json.dumps(out, default=str), flush=True)

    try:
        outbound = outbound_report()
    except Exception as exc:
        outbound = {"error": scrub(f"{type(exc).__name__}: {exc}")[:300]}
    print("LANE_DIAG_OUTBOUND " + json.dumps(outbound, default=str), flush=True)

asyncio.run(main())
