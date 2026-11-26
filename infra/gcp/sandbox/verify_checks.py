#!/usr/bin/env python3
"""The analysis half of verify_sandbox.sh: pure functions over the JSON gcloud returns.

verify_sandbox.sh fetches (read-only) and pipes JSON in; this decides. Every function returns a
list of problems (empty = the check holds), and the CLI prints `FAIL <problem>` lines or one
`ok <claim>` line whose claim says exactly what was checked, no more (review S8).

    python3 verify_checks.py <check> --project P --number N [--svc S] [--service-account SA]
        [--expect-member M ...] < policy-or-spec.json

Checks:
  foreign-policy   a policy of ANOTHER project (pivota-prod/-staging/-shared, project level) names
                   no sandbox principal: user SAs @P.iam, service agents service-N@..., the default
                   Compute SA N-compute@..., Cloud Build N@cloudbuild..., workload identity @P.svc.id.goog
  asset-search     the same, over `gcloud asset search-all-iam-policies` results (resource level:
                   buckets, secrets, repos, ... anywhere in that project)
  sandbox-policy   the sandbox's project-level policy: no allUsers/allAuthenticatedUsers, no
                   project-wide secretAccessor, no editor/owner on ANY service account (including the
                   default Compute SA), and every service account / service agent belongs to the
                   sandbox or is a Google-managed agent of the sandbox's own project number
  secret-policy    one secret's policy: only roles/secretmanager.secretAccessor, held exactly by the
                   --expect-member set (derived from secrets/*.list)
  registry-policy  a pivota-shared repo policy: sandbox principals hold artifactregistry.reader only
  run-service      a Cloud Run service spec: runs as --service-account, image digest-pinned from
                   pivota-shared, env contract, every secret reference resolves to THIS project
                   (secretKeyRef names AND the run.googleapis.com/secrets alias annotation), and
                   every mounted secret is one secrets/<svc>.list names (--expect-member = names)
  run-invoker      a Cloud Run service's IAM: roles/run.invoker only; allUsers only on the public
                   services; any other member must be a sandbox principal
"""
from __future__ import annotations

import argparse
import json
import re
import sys

GOOGLE_AGENT_DOMAINS = re.compile(
    r"@(gcp-sa-[a-z0-9-]+\.iam\.gserviceaccount\.com|serverless-robot-prod\.iam\.gserviceaccount\.com|"
    r"compute-system\.iam\.gserviceaccount\.com|container-engine-robot\.iam\.gserviceaccount\.com|"
    r"cloudbuild\.gserviceaccount\.com|cloudservices\.gserviceaccount\.com|"
    r"gs-project-accounts\.iam\.gserviceaccount\.com|cloud-sql\.iam\.gserviceaccount\.com)$")
PUBLIC_SERVICES = {"web", "gateway", "agenpaz"}


def _members(policy: dict):
    for b in policy.get("bindings", []) or []:
        for m in b.get("members", []) or []:
            yield b.get("role", ""), m


def _address(member: str) -> str:
    """`deleted:serviceAccount:x@y?uid=1` -> `x@y`; `serviceAccount:x@y` -> `x@y`."""
    m = member.lower()
    while ":" in m and m.split(":", 1)[0] in ("deleted", "serviceaccount", "user", "group", "domain", "principal", "principalset"):
        m = m.split(":", 1)[1]
    return m.split("?", 1)[0]


def is_sandbox_principal(member: str, project: str, number: str) -> bool:
    m = _address(member)
    return (m.endswith(f"@{project}.iam.gserviceaccount.com") or m.endswith(f"@{project}.svc.id.goog")
            or m.startswith(f"{project}.svc.id.goog[")                       # GKE workload identity
            or m == f"{project}@appspot.gserviceaccount.com"                  # App Engine default SA
            or (bool(number) and (m.startswith(f"service-{number}@") or m == f"{number}-compute@developer.gserviceaccount.com"
                                  or m == f"{number}@cloudbuild.gserviceaccount.com"
                                  or m.startswith(f"p{number}-")              # per-project agents (gcp-sa-logging)
                                  or m.startswith(f"{number}@") or f"projects/{number}/" in member)))


def foreign_policy(policy: dict, project: str, number: str, **_) -> list[str]:
    return [f"{role} names sandbox principal {m}" for role, m in _members(policy) if is_sandbox_principal(m, project, number)]


REGISTRY_READ_OK = re.compile(r"/projects/pivota-shared/locations/us-west1/repositories/(pivota|pivota-gateway)$")


def asset_search(results, project: str, number: str, allow_registry_read: bool = False, **_) -> list[str]:
    """Resource-level grants to sandbox principals. In pivota-shared, the ONE expected kind is
    artifactregistry.reader on the two image repos (bootstrap phase iam); anything else is a finding."""
    out = []
    for r in results or []:
        resource = r.get("resource", "?")
        if number and r.get("project") == f"projects/{number}":
            continue   # the sandbox's own resources: judged by sandbox-resources, not here
        for role, m in _members(r.get("policy", {})):
            if not is_sandbox_principal(m, project, number):
                continue
            if allow_registry_read and role == "roles/artifactregistry.reader" and REGISTRY_READ_OK.search(resource):
                continue
            out.append(f"{resource}: {role} names sandbox principal {m}")
    return out


def _foreign_sa(member: str, project: str, number: str) -> bool:
    """A service account / agent that is neither the sandbox's own nor a Google agent OF the sandbox."""
    if not member.startswith(("serviceAccount:", "deleted:serviceAccount:")):
        return False
    if is_sandbox_principal(member, project, number):
        return False
    addr = _address(member)
    if GOOGLE_AGENT_DOMAINS.search(addr):
        # a Google-managed agent is fine only when it is the sandbox's own (service-<N>@ / <N>@)
        return not (addr.startswith(f"service-{number}@") or addr.startswith(f"{number}@"))
    return True


IMPERSONATION_ROLES = ("roles/iam.serviceAccountTokenCreator", "roles/iam.serviceAccountUser",
                       "roles/iam.workloadIdentityUser", "roles/iam.serviceAccountKeyAdmin", "roles/iam.serviceAccountAdmin")
WIF_POOL_RE = re.compile(r"^principal(?:set)?://iam\.googleapis\.com/projects/([^/]+)/locations/[^/]+/workloadIdentityPools/", re.I)


def sandbox_policy(policy: dict, project: str, number: str, allowed_owner=(), **_) -> list[str]:
    out = []
    allowed = set(allowed_owner)
    if not allowed:
        out.append("owners/editors NOT verified: pass the allowed humans in SANDBOX_ALLOWED_OWNERS (kept out of the repo)")
    for role, m in _members(policy):
        if role in ("roles/owner", "roles/editor") and m.startswith(("user:", "group:", "domain:")) and allowed and m not in allowed:
            out.append(f"{role} held by {m}, who is not in SANDBOX_ALLOWED_OWNERS")
        if role in ("roles/secretmanager.admin", "roles/secretmanager.secretVersionManager") :
            out.append(f"{role} for {m} at project level (secret values are reachable through it)")
        if role in IMPERSONATION_ROLES:
            out.append(f"{role} for {m} at project level (impersonation of every service account)")
        w = WIF_POOL_RE.match(m)
        if w and w.group(1) not in (number, project):
            out.append(f"{role} granted to a workload identity pool of another project: {m}")
        if m in ("allUsers", "allAuthenticatedUsers"):
            out.append(f"{role} granted to {m} at project level")
        if role == "roles/secretmanager.secretAccessor":
            out.append(f"project-level secretAccessor for {m} (secrets are granted one by one)")
        if (role in ("roles/editor", "roles/owner") and m.startswith(("serviceAccount:", "deleted:serviceAccount:"))
                and m != f"serviceAccount:{number}@cloudservices.gserviceaccount.com"):
            # The Google APIs Service Agent (<N>@cloudservices) holds editor by Google's design and
            # must keep it; every other service account, the default Compute SA first, may not.
            out.append(f"{role} on service account {m} (including the default Compute SA, none may hold it)")
        if _foreign_sa(m, project, number):
            out.append(f"{role} held by a service account of another project: {m}")
    return out


def sandbox_resources(results, project: str, number: str, drop_bucket: str = "", export_sa: str = "", **_) -> list[str]:
    """Every RESOURCE-level policy inside the sandbox (Cloud Asset search over the project): no
    impersonation or owner/editor grant anywhere; allUsers only as run.invoker on the public
    services; no principal of another project except the prod export SA creating objects in the
    drop bucket."""
    out = []
    for r in results or []:
        resource = r.get("resource", "?")
        for role, m in _members(r.get("policy", {})):
            if role in IMPERSONATION_ROLES or role in ("roles/owner", "roles/editor"):
                out.append(f"{resource}: {role} for {m}")
            if m in ("allUsers", "allAuthenticatedUsers"):
                public_run = role == "roles/run.invoker" and re.search(r"/services/(web|gateway|agenpaz)$", resource)
                if m == "allAuthenticatedUsers" or not public_run:
                    out.append(f"{resource}: {role} for {m}")
                continue
            if m.startswith(("serviceAccount:", "deleted:serviceAccount:", "principal")) and not is_sandbox_principal(m, project, number):
                agent = GOOGLE_AGENT_DOMAINS.search(_address(m)) and (_address(m).startswith(f"service-{number}@"))
                export = (export_sa and m == f"serviceAccount:{export_sa}" and role == "roles/storage.objectCreator"
                          and resource.endswith(f"/{drop_bucket}"))
                if not (agent or export):
                    out.append(f"{resource}: {role} for {m}, a principal of another project")
    return out


def secret_policy(policy: dict, expect: list[str], **_) -> list[str]:
    out, seen = [], set()
    for role, m in _members(policy):
        if role != "roles/secretmanager.secretAccessor":
            out.append(f"unexpected role {role} for {m}")
        else:
            seen.add(m)
    want = set(expect)
    for m in sorted(seen - want):
        out.append(f"unexpected accessor {m}")
    for m in sorted(want - seen):
        out.append(f"missing accessor {m}")
    return out


def registry_policy(policy: dict, project: str, number: str, **_) -> list[str]:
    return [f"sandbox principal {m} holds {role} (reader only)" for role, m in _members(policy)
            if is_sandbox_principal(m, project, number) and role != "roles/artifactregistry.reader"]


def run_service(spec: dict, project: str, number: str, svc: str, service_account: str, expect: list[str], **_) -> list[str]:
    out = []
    tmpl = spec.get("spec", {}).get("template", {})
    containers = tmpl.get("spec", {}).get("containers") or [{}]
    if len(containers) != 1:
        out.append(f"{len(containers)} containers: the sandbox runs no sidecar (only one container is checked otherwise)")
    c = containers[0]
    env = {e.get("name"): e for e in c.get("env", []) or []}
    if tmpl.get("spec", {}).get("serviceAccountName") != service_account:
        out.append(f"runs as {tmpl.get('spec', {}).get('serviceAccountName')!r}, not {service_account}")
    image = c.get("image", "")
    if not image.startswith("us-west1-docker.pkg.dev/pivota-shared/") or "@sha256:" not in image:
        out.append("image not pinned by digest from pivota-shared")
    if env.get("PIVOTA_ENV", {}).get("value") != "sandbox":
        out.append("PIVOTA_ENV is not sandbox")
    if env.get("GOOGLE_CLOUD_PROJECT", {}).get("value") != project:
        out.append("GOOGLE_CLOUD_PROJECT is not the sandbox project")
    out += [f"{n} is set" for n in env if (n or "").startswith("A2A_")]
    if svc in ("web", "worker"):
        if env.get("REAP_API_BASE_URL", {}).get("value") != "https://sandbox.api.reap.global":
            out.append("REAP_API_BASE_URL is not the Reap sandbox")
        if env.get("REAP_AGENTIC_SIMULATE_CHECKOUT", {}).get("value") != "COMPLETED":
            out.append("REAP_AGENTIC_SIMULATE_CHECKOUT is not COMPLETED")
    if svc in ("gateway", "agenpaz") and env.get("AGENPAS_RAILS", {}).get("value") != "reap":
        out.append("AGENPAS_RAILS is not reap")
    # Secret references: a cross-project secret is mounted under an ALIAS, with the real
    # projects/<n>/secrets/<name> in the run.googleapis.com/secrets annotation (review S8).
    aliases = {}
    ann = (tmpl.get("metadata", {}).get("annotations") or {}).get("run.googleapis.com/secrets", "")
    for part in filter(None, (p.strip() for p in ann.split(","))):
        alias, _, target = part.partition(":")
        aliases[alias] = target
        m = re.match(r"projects/([^/]+)/secrets/([^/]+)$", target)
        if not m or m.group(1) not in (number, project):
            out.append(f"secret alias {alias} points at {target}, not this project")
    refs = []
    for n, e in env.items():
        ref = (e.get("valueFrom") or {}).get("secretKeyRef")
        if ref:
            refs.append((n, ref.get("name", "")))
    for vol in tmpl.get("spec", {}).get("volumes", []) or []:
        if vol.get("secret"):
            refs.append((f"volume {vol.get('name')}", vol["secret"].get("secretName", "")))
    for n, name in refs:
        target = aliases.get(name, name)
        m = re.match(r"projects/([^/]+)/secrets/([^/]+)$", target)
        real = m.group(2) if m else target
        if m and m.group(1) not in (number, project):
            out.append(f"{n} mounts {target}, a secret of another project")
        elif real not in expect:
            out.append(f"{n} mounts secret {real!r}, which secrets/{svc}.list does not name")
    return out


def run_invoker(policy: dict, project: str, number: str, svc: str, **_) -> list[str]:
    out = []
    for role, m in _members(policy):
        if role != "roles/run.invoker":
            out.append(f"unexpected role {role} for {m}")
        elif m == "allUsers":
            if svc not in PUBLIC_SERVICES:
                out.append(f"{svc} is invokable by allUsers (it must stay private)")
        elif not is_sandbox_principal(m, project, number):
            out.append(f"run.invoker for {m}, not a sandbox principal")
    return out


CHECKS = {"foreign-policy": foreign_policy, "asset-search": asset_search, "sandbox-policy": sandbox_policy,
          "sandbox-resources": sandbox_resources,
          "secret-policy": secret_policy, "registry-policy": registry_policy, "run-service": run_service,
          "run-invoker": run_invoker}
CLAIMS = {
    "foreign-policy": "project-level policy names no sandbox principal (SAs, service agents, default Compute SA, Cloud Build)",
    "asset-search": "no resource-level policy in this scope names a sandbox principal, outside the sandbox (Cloud Asset search)",
    "sandbox-resources": "sandbox resource policies: no impersonation/owner grant, allUsers only on public run services, no foreign principal but the export SA",
    "sandbox-policy": "sandbox project policy: no public grant, no project-wide secret access/admin or impersonation, no editor/owner SA, owners allowlisted, no foreign SA or WIF pool",
    "secret-policy": "only secretAccessor, held by exactly the service accounts its secrets list names",
    "registry-policy": "sandbox principals hold artifactregistry.reader only",
    "run-service": "service account, digest-pinned image, env contract, every secret from this project and named in its list",
    "run-invoker": "only run.invoker; allUsers only on a public service; every other invoker a sandbox principal",
}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("check", choices=sorted(CHECKS))
    ap.add_argument("--project", required=True)
    ap.add_argument("--number", default="")
    ap.add_argument("--svc", default="")
    ap.add_argument("--service-account", default="")
    ap.add_argument("--expect-member", action="append", default=[])
    ap.add_argument("--label", default="")
    ap.add_argument("--allow-registry-read", action="store_true")
    ap.add_argument("--allowed-owner", action="append", default=[])
    ap.add_argument("--drop-bucket", default="")
    ap.add_argument("--export-sa", default="")
    a = ap.parse_args(argv)
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        data = None
    if data is None:
        print(f"FAIL {a.label}: could not read {a.check} input (nothing verified)")
        return 0
    problems = CHECKS[a.check](data, project=a.project, number=a.number, svc=a.svc,
                               service_account=a.service_account, expect=a.expect_member,
                               allow_registry_read=a.allow_registry_read, allowed_owner=a.allowed_owner,
                               drop_bucket=a.drop_bucket, export_sa=a.export_sa)
    for p in problems:
        print(f"FAIL {a.label}: {p}")
    if not problems:
        print(f"ok {a.label}: {CLAIMS[a.check]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
