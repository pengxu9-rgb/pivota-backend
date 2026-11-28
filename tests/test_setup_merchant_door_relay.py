"""`infra/gcp/setup_merchant_door_relay.sh` provisions the merchant-door relay's own lane.

Gateway calls to merchants' UCP doors must stop leaving from the default NAT (the address payment
partners allowlist) without moving onto the crawl lane (whose throttles and IP breaker must never
hit a live buyer request). So the relay gets its own subnet, router, reserved address and NAT, and
an internal, IAM-gated Cloud Run service that holds no secret and that only the gateway may invoke.

These tests drive the REAL script through its `GCLOUD` injection point against a fake that records
every invocation ONE ARGUMENT PER FIELD, so an assertion about `--args=...` or `--subnet X` is about
the argv gcloud would receive. Nothing here reaches Google Cloud.

The ranges below are arbitrary private/documentation test values, not any environment's range.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "gcp" / "setup_merchant_door_relay.sh"
SHA = "c" * 40
CIDR = "10.0.0.0/26"
RELAY_URL = "https://merchant-door-relay-test.a.run.app"
ADDRESS_MARKER = "address-from-describe"

SUBNET, ROUTER, ADDR, NAT = (
    "pivota-door-relay", "pivota-door-relay-router", "pivota-door-relay-egress-ip", "pivota-door-relay-nat")
SERVICE = "merchant-door-relay"
# The payment lane (setup_egress_nat.sh) and the crawl lane (setup_crawl_egress.sh): never mutated.
OTHER_LANES = {"pivota-router", "pivota-nat", "pivota-egress-ip",
               "pivota-crawl", "pivota-crawl-router", "pivota-crawl-nat", "pivota-crawl-egress-ip"}


def _matching_nat(**override) -> str:
    nat = {
        "name": NAT,
        "sourceSubnetworkIpRangesToNat": "LIST_OF_SUBNETWORKS",
        "subnetworks": [{"name": f"https://www.googleapis.com/compute/v1/projects/p/regions/us-west1/subnetworks/{SUBNET}",
                         "sourceIpRangesToNat": ["ALL_IP_RANGES"]}],
        "natIpAllocateOption": "MANUAL_ONLY",
        "natIps": [f"https://www.googleapis.com/compute/v1/projects/p/regions/us-west1/addresses/{ADDR}"],
    }
    nat.update(override)
    return json.dumps(nat)


# Answers the script's reads from env:
#   EXISTS=1            every relay resource already exists (the re-run case)
#   IMAGE_MISSING=<repos, comma-separated> / IMAGE_DENIED=1
#   GATEWAY_SA_EXISTS=0, PAYMENT_SCOPE, DEFAULT_PGA, EXISTING_RANGE, EXISTING_PGA, NAT_JSON,
#   RELAY_ROLES, SERVICE_POLICY
FAKE_GCLOUD = textwrap.dedent(
    r'''
    #!/usr/bin/env python3
    import json, os, sys
    a = sys.argv[1:]
    with open(os.environ["GCLOUD_LOG"], "a") as f:
        f.write("\x1f".join(a) + "\x1f\n")
    env = os.environ.get
    exists = env("EXISTS") == "1"
    fmt = next((t[len("--format="):] for t in a if t.startswith("--format=")), None)
    if fmt is None and "--format" in a:
        fmt = a[a.index("--format") + 1]

    def out(text, rc=0):
        if text:
            print(text)
        sys.exit(rc)

    if a[:4] == ["artifacts", "docker", "images", "describe"]:
        if env("IMAGE_DENIED") == "1":
            sys.stderr.write("ERROR: PERMISSION_DENIED\n"); sys.exit(1)
        repo = a[4].split("/")[2]
        if repo in (env("IMAGE_MISSING") or "").split(","):
            sys.stderr.write("ERROR: NOT_FOUND: image not found\n"); sys.exit(1)
        out("")
    if a[:3] == ["iam", "service-accounts", "describe"]:
        if a[3].startswith("sa-gateway@"):
            sys.exit(0 if env("GATEWAY_SA_EXISTS", "1") == "1" else 1)
        sys.exit(0 if exists else 1)
    if a[:3] == ["compute", "routers", "describe"]:
        if a[3] == "pivota-router":
            out(env("PAYMENT_SCOPE", "LIST_OF_SUBNETWORKS"))
        sys.exit(0 if exists else 1)
    if a[:4] == ["compute", "networks", "subnets", "describe"]:
        if a[4] == "default":
            out(env("DEFAULT_PGA", "True"))
        if not exists:
            sys.exit(1)
        if fmt == "value(ipCidrRange)":
            out(env("EXISTING_RANGE", env("DOOR_RELAY_SUBNET_CIDR", "")))
        if fmt == "value(privateIpGoogleAccess)":
            out(env("EXISTING_PGA", "True"))
        out("")
    if a[:3] == ["compute", "addresses", "describe"]:
        if fmt == "value(address)":
            out("ADDRESS_MARKER")
        sys.exit(0 if exists else 1)
    if a[:4] == ["compute", "routers", "nats", "describe"]:
        if not exists:
            sys.exit(1)
        out(env("NAT_JSON", "") if fmt == "json" else "")
    if a[:2] == ["projects", "get-iam-policy"]:
        out(env("RELAY_ROLES", ""))
    if a[:3] == ["run", "services", "describe"]:
        if fmt == "value(status.url)":
            out("RELAY_URL")
        sys.exit(0 if exists else 1)
    if a[:3] == ["run", "services", "get-iam-policy"]:
        out(env("SERVICE_POLICY", ""))
    sys.exit(0)
    ''').lstrip().replace("ADDRESS_MARKER", ADDRESS_MARKER).replace("RELAY_URL", RELAY_URL)


def _default_policy(project: str = "pivota-prod") -> str:
    return json.dumps({"bindings": [{"role": "roles/run.invoker",
                                     "members": [f"serviceAccount:sa-gateway@{project}.iam.gserviceaccount.com"]}]})


def _run(tmp_path: Path, *args: str, env: Optional[Dict[str, str]] = None, cidr: Optional[str] = CIDR,
         bash: str = "bash") -> Tuple[subprocess.CompletedProcess, List[List[str]]]:
    fake = tmp_path / "fake-gcloud"
    fake.write_text(FAKE_GCLOUD)
    fake.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    base = {k: v for k, v in os.environ.items() if k != "DOOR_RELAY_SUBNET_CIDR"}
    defaults = {"NAT_JSON": _matching_nat(), "SERVICE_POLICY": _default_policy()}
    if cidr is not None:
        defaults["DOOR_RELAY_SUBNET_CIDR"] = cidr
    proc = subprocess.run(
        [bash, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=str(REPO),
        env={**base, "GCLOUD": str(fake), "GCLOUD_LOG": str(log), **defaults, **(env or {})},
    )
    calls = [line.split("\x1f")[:-1] for line in log.read_text().splitlines() if line]
    return proc, calls


def _one(calls: List[List[str]], *prefix: str) -> List[str]:
    found = [c for c in calls if c[: len(prefix)] == list(prefix)]
    assert len(found) == 1, (prefix, calls)
    return found[0]


def _flag(argv: List[str], name: str) -> str:
    """The value of `--name VALUE` or `--name=VALUE` in one recorded argv."""
    for i, token in enumerate(argv):
        if token == name:
            return argv[i + 1]
        if token.startswith(name + "="):
            return token[len(name) + 1:]
    raise AssertionError(f"{name} not passed: {argv}")


def _has(argv: List[str], name: str) -> bool:
    return any(t == name or t.startswith(name + "=") for t in argv)


MUTATING = re.compile(r"^(create|update|delete|deploy|patch|replace|resume|pause|execute|add-.*|remove-.*|set-.*|update-.*)$")


def _mutating(calls: List[List[str]]) -> List[List[str]]:
    """Every call whose verb (any of the first five fields) changes something."""
    return [c for c in calls if any(MUTATING.match(t) for t in c[:5])]


def _creates(calls: List[List[str]]) -> List[List[str]]:
    return [c for c in calls if "create" in c[:5]]


def _deploy(calls: List[List[str]]) -> List[str]:
    return _one(calls, "run", "deploy", SERVICE)


# ── shape of the file ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bash", ["/bin/bash", "/opt/homebrew/bin/bash", "bash"])
def test_the_script_parses_under_every_bash_and_is_executable(bash):
    if bash.startswith("/") and not Path(bash).exists():
        pytest.skip(f"{bash} not installed here")
    subprocess.run([bash, "-n", str(SCRIPT)], check=True)
    assert os.access(SCRIPT, os.X_OK), "the operator runs it directly"


def test_the_committed_script_names_no_address_or_range():
    """The repo is public. The range comes from DOOR_RELAY_SUBNET_CIDR and the address is printed
    from `describe` at run time; neither, nor any other literal, belongs in the file."""
    text = SCRIPT.read_text()
    ipv4 = re.findall(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?(?![\w.])", text)
    assert ipv4 == [], ipv4
    ipv6 = re.findall(r"(?<![\w:])(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}(?:/\d{1,3})?(?![\w:])|::[0-9a-fA-F]", text)
    assert ipv6 == [], ipv6
    for flag in ("--set-secrets", "--update-secrets", "--env-vars-file", "--update-env-vars"):
        assert flag not in text, flag


# ── refusals before any gcloud call ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("args, message", [
    ((), "usage"),
    (("qa", SHA), "usage"),
    (("prod",), "gateway-sha is required"),
    (("staging",), "gateway-sha is required"),
    (("prod", "latest"), "latest"),
    (("staging", "latest"), "latest"),
    (("prod", "abc1234"), "40-character"),
    (("staging", "c" * 39), "40-character"),
    (("prod", "C" * 40), "40-character"),
    (("prod", "g" * 40), "40-character"),
    (("prod", SHA, "extra"), "too many arguments"),
])
def test_bad_arguments_exit_2_before_touching_anything(tmp_path, args, message):
    proc, calls = _run(tmp_path, *args)
    assert proc.returncode == 2 and message in proc.stderr, (proc.returncode, proc.stderr)
    assert calls == []


@pytest.mark.parametrize("cidr", [
    None,                 # unset
    "",                   # set but empty
    "not-a-range",
    "10.0.0.0",           # no prefix
    "10.0.0.0/8",         # wider than /16
    "10.0.0.0/27",        # narrower than /26
    "10.0.0.1/26",        # host bits set
    "192.0.2.0/24",       # documentation range, not RFC1918
    "172.32.0.0/16",      # just outside 172.16/12
    "010.0.0.0/26",       # leading zero
    "10.0.0.256/26",
    "10.0.0.0/026",
    "10.0.0.0/26 ",
])
def test_a_missing_or_malformed_range_is_refused_before_any_gcloud_call(tmp_path, cidr):
    proc, calls = _run(tmp_path, "prod", SHA, cidr=cidr)
    assert proc.returncode == 2 and "DOOR_RELAY_SUBNET_CIDR" in proc.stderr, (proc.returncode, proc.stderr)
    assert calls == []


@pytest.mark.parametrize("cidr", ["10.0.0.0/26", "172.16.0.0/20", "192.168.0.0/24", "10.0.0.0/16"])
def test_an_aligned_private_range_is_accepted_and_is_the_subnets_range(tmp_path, cidr):
    proc, calls = _run(tmp_path, "prod", SHA, cidr=cidr)
    assert proc.returncode == 0, proc.stderr
    subnet = _one(calls, "compute", "networks", "subnets", "create", SUBNET)
    assert _flag(subnet, "--range") == cidr


# ── the network ────────────────────────────────────────────────────────────────────────────────


def test_a_fresh_run_creates_the_lane_with_its_own_nat_and_reserved_address(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA)
    assert proc.returncode == 0, proc.stderr

    subnet = _one(calls, "compute", "networks", "subnets", "create", SUBNET)
    assert _flag(subnet, "--network") == "default"
    assert _flag(subnet, "--region") == "us-west1"
    assert "--enable-private-ip-google-access" in subnet

    router = _one(calls, "compute", "routers", "create", ROUTER)
    assert _flag(router, "--network") == "default" and _flag(router, "--region") == "us-west1"

    address = _one(calls, "compute", "addresses", "create", ADDR)
    assert _flag(address, "--region") == "us-west1", "regional: a NAT takes only a regional address"
    assert not _has(address, "--subnet"), "external, not an internal address"

    nat = _one(calls, "compute", "routers", "nats", "create", NAT)
    assert _flag(nat, "--router") == ROUTER
    assert _flag(nat, "--nat-custom-subnet-ip-ranges") == SUBNET, "ONLY the relay's subnet"
    assert _flag(nat, "--nat-external-ip-pool") == ADDR, "ONLY the reserved address (manual allocation)"
    assert not _has(nat, "--nat-all-subnet-ip-ranges")
    assert not _has(nat, "--auto-allocate-nat-external-ips")

    # Address and router before the NAT that uses them; subnet before the NAT that covers it.
    order = [c[:5] for c in _creates(calls)]
    assert order.index(nat[:5]) > max(order.index(subnet[:5]), order.index(router[:5]), order.index(address[:5]))


def test_the_payment_and_crawl_lanes_are_never_in_a_changing_call(tmp_path):
    for env in ({}, {"EXISTS": "1"}):
        proc, calls = _run(tmp_path, "prod", SHA, env=env)
        assert proc.returncode == 0, proc.stderr
        for call in _mutating(calls):
            tokens = {part for t in call for part in re.split(r"[=,]", t)}
            assert not tokens & OTHER_LANES, call
        # The payment router is only ever READ (its NAT's scope), like setup_crawl_egress.sh does.
        assert [c[:4] for c in calls if "pivota-router" in c] == [["compute", "routers", "describe", "pivota-router"]]
        # The default subnet is only ever read; nothing deploys onto it.
        assert all(c[:5] == ["compute", "networks", "subnets", "describe", "default"]
                   for c in calls if c[:3] == ["compute", "networks", "subnets"] and "default" in c[4:5])
        assert not [c for c in _mutating(calls) if _has(c, "--subnet") and _flag(c, "--subnet") != SUBNET]


def test_a_payment_nat_that_still_covers_all_subnets_stops_before_anything_is_created(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA, env={"PAYMENT_SCOPE": "ALL_SUBNETWORKS_ALL_IP_RANGES"})
    assert proc.returncode == 3 and "migrate_payment_nat_to_default_subnet.sh" in proc.stderr
    assert _mutating(calls) == []


def test_an_existing_subnet_with_another_range_is_refused_not_changed(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA, env={"EXISTS": "1", "EXISTING_RANGE": "10.0.1.0/26"})
    assert proc.returncode == 1 and "different range" in proc.stderr
    assert _mutating(calls) == []


def test_an_existing_subnet_without_private_google_access_is_refused(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA, env={"EXISTS": "1", "EXISTING_PGA": "False"})
    assert proc.returncode == 1 and "Private Google Access" in proc.stderr
    assert _mutating(calls) == []


@pytest.mark.parametrize("override, what", [
    ({"sourceSubnetworkIpRangesToNat": "ALL_SUBNETWORKS_ALL_IP_RANGES"}, "scope"),
    ({"subnetworks": [{"name": SUBNET}, {"name": "default"}]}, "subnetworks"),
    ({"natIpAllocateOption": "AUTO_ONLY", "natIps": []}, "allocation"),
    ({"natIps": [f"regions/us-west1/addresses/{ADDR}", "regions/us-west1/addresses/pivota-egress-ip"]}, "addresses"),
])
def test_an_existing_nat_that_does_not_match_is_refused_not_changed(tmp_path, override, what):
    proc, calls = _run(tmp_path, "prod", SHA, env={"EXISTS": "1", "NAT_JSON": _matching_nat(**override)})
    assert proc.returncode == 1 and what in proc.stderr, proc.stderr
    assert _mutating(calls) == []


def test_an_unreadable_existing_nat_is_refused(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA, env={"EXISTS": "1", "NAT_JSON": "not json"})
    assert proc.returncode == 1 and "unreadable" in proc.stderr
    assert _mutating(calls) == []


def test_no_private_google_access_on_the_gateways_subnet_is_a_warning_and_nothing_changes_there(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA, env={"DEFAULT_PGA": "False"})
    assert proc.returncode == 0, proc.stderr
    assert "WARNING" in proc.stderr and "Private Google Access" in proc.stderr
    assert not [c for c in _mutating(calls) if "default" in c[:5]]


# ── the identity and the one grant ─────────────────────────────────────────────────────────────


def test_the_relay_identity_is_created_with_no_role_and_the_only_grant_is_the_gateways_invoker(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA)
    assert proc.returncode == 0, proc.stderr
    sa = _one(calls, "iam", "service-accounts", "create", "sa-door-relay")
    assert _flag(sa, "--project") == "pivota-prod"

    grants = [c for c in calls if any(t in ("add-iam-policy-binding", "set-iam-policy") for t in c[:4])]
    assert len(grants) == 1, grants
    grant = grants[0]
    assert grant[:4] == ["run", "services", "add-iam-policy-binding", SERVICE]
    assert _flag(grant, "--member") == "serviceAccount:sa-gateway@pivota-prod.iam.gserviceaccount.com"
    assert _flag(grant, "--role") == "roles/run.invoker"
    assert not [c for c in calls if c[:1] == ["secrets"]], "the relay touches no secret"
    assert not [c for c in calls if c[:2] == ["projects", "add-iam-policy-binding"]]
    assert not [c for c in calls if "artifacts" in c[:1] and "add-iam-policy-binding" in c]


def test_a_relay_identity_holding_a_project_role_is_refused_before_the_service(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA, env={"EXISTS": "1", "RELAY_ROLES": "roles/editor"})
    assert proc.returncode == 1 and "roles/editor" in proc.stderr
    assert not [c for c in calls if c[:2] == ["run", "deploy"]]
    assert not [c for c in calls if "add-iam-policy-binding" in c]
    read = _one(calls, "projects", "get-iam-policy", "pivota-prod")
    assert _flag(read, "--filter") == "bindings.members:serviceAccount:sa-door-relay@pivota-prod.iam.gserviceaccount.com"


def test_a_missing_gateway_identity_stops_before_anything_is_created(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA, env={"GATEWAY_SA_EXISTS": "0"})
    assert proc.returncode == 1 and "sa-gateway@pivota-prod" in proc.stderr
    assert _mutating(calls) == []


def test_an_extra_invoker_is_reported_not_removed(tmp_path):
    policy = json.dumps({"bindings": [
        {"role": "roles/run.invoker", "members": ["serviceAccount:sa-gateway@pivota-prod.iam.gserviceaccount.com",
                                                 "allUsers"]}]})
    proc, calls = _run(tmp_path, "prod", SHA, env={"SERVICE_POLICY": policy})
    assert proc.returncode == 0, proc.stderr
    assert "WARNING" in proc.stderr and "allUsers" in proc.stderr
    assert not [c for c in calls if "remove-iam-policy-binding" in c]


def test_the_expected_policy_prints_no_warning(tmp_path):
    proc, _calls = _run(tmp_path, "prod", SHA)
    assert proc.returncode == 0, proc.stderr
    assert "WARNING" not in proc.stderr, proc.stderr


# ── the service ────────────────────────────────────────────────────────────────────────────────


def test_the_service_is_internal_iam_gated_warm_on_its_own_subnet_and_holds_no_secret(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA)
    assert proc.returncode == 0, proc.stderr
    svc = _deploy(calls)

    assert _flag(svc, "--image") == f"us-west1-docker.pkg.dev/pivota-shared/pivota/gateway:{SHA}"
    assert _flag(svc, "--command") == "node"
    assert _flag(svc, "--args") == "src/services/doorRelay/server.js"
    assert _flag(svc, "--service-account") == "sa-door-relay@pivota-prod.iam.gserviceaccount.com"
    assert _flag(svc, "--ingress") == "internal"
    assert "--no-allow-unauthenticated" in svc and not _has(svc, "--allow-unauthenticated")
    assert _flag(svc, "--min-instances") == "1", "no cold start inside the gateway's 5 s budget"
    assert 1 <= int(_flag(svc, "--max-instances")) <= 4
    assert _flag(svc, "--cpu") == "1" and _flag(svc, "--memory") == "512Mi"
    assert _flag(svc, "--network") == "default"
    assert _flag(svc, "--subnet") == SUBNET
    assert _flag(svc, "--vpc-egress") == "all-traffic", "private-ranges-only would bypass the relay's NAT"
    assert _flag(svc, "--region") == "us-west1" and _flag(svc, "--project") == "pivota-prod"

    for secret_flag in ("--set-secrets", "--update-secrets", "--secrets", "--env-vars-file", "--update-env-vars"):
        assert not _has(svc, secret_flag), secret_flag
    assert "--clear-secrets" in svc, "a secret mounted by hand is dropped on every run"
    env = dict(pair.split("=", 1) for pair in _flag(svc, "--set-env-vars").split(","))
    assert env == {"PIVOTA_ENV": "production", "PIVOTA_SERVICE_NAME": SERVICE, "PIVOTA_COMMIT_SHA": SHA}

    labels = dict(pair.split("=", 1) for pair in _flag(svc, "--labels").split(","))
    assert labels == {"env": "prod", "managed-by": "infra-gcp", "lane": "merchant-door-relay"}


def test_the_relay_is_deployed_only_after_its_lane_and_identity_exist(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA)
    assert proc.returncode == 0, proc.stderr
    deploy_at = calls.index(_deploy(calls))
    nat_at = calls.index(_one(calls, "compute", "routers", "nats", "create", NAT))
    sa_at = calls.index(_one(calls, "iam", "service-accounts", "create", "sa-door-relay"))
    grant_at = calls.index(_one(calls, "run", "services", "add-iam-policy-binding", SERVICE))
    assert nat_at < deploy_at and sa_at < deploy_at < grant_at


def test_a_full_sha_missing_from_pivota_resolves_to_pivota_gateway(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA, env={"IMAGE_MISSING": "pivota"})
    assert proc.returncode == 0, proc.stderr
    assert _flag(_deploy(calls), "--image") == f"us-west1-docker.pkg.dev/pivota-shared/pivota-gateway/gateway:{SHA}"


@pytest.mark.parametrize("env, message", [
    ({"IMAGE_MISSING": "pivota,pivota-gateway"}, "build it first"),
    ({"IMAGE_DENIED": "1"}, "lookup error"),
])
def test_a_missing_or_unreadable_image_stops_before_anything_is_created(tmp_path, env, message):
    proc, calls = _run(tmp_path, "prod", SHA, env=env)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert _mutating(calls) == []


def test_the_gateway_service_is_never_named(tmp_path):
    """The relay is dark until a separate, reviewed gateway env change. Nothing here may run
    against the `gateway` service - not an update, not an env change, not even a describe."""
    for env in ({}, {"EXISTS": "1"}):
        proc, calls = _run(tmp_path, "prod", SHA, env=env)
        assert proc.returncode == 0, proc.stderr
        for call in calls:
            assert "gateway" not in call, call
            assert not any("MERCHANT_DOOR_RELAY" in t for t in call), call
            if call[:1] == ["run"]:
                operand = [t for t in call[:4] if not t.startswith("-")][-1]
                assert operand == SERVICE, call
    assert not re.search(r"run\s+services\s+update\b", SCRIPT.read_text())


# ── idempotency ────────────────────────────────────────────────────────────────────────────────


def test_a_rerun_over_an_existing_lane_creates_nothing_and_redeploys_in_place(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA, env={"EXISTS": "1"})
    assert proc.returncode == 0, proc.stderr
    assert _creates(calls) == [], _creates(calls)
    # The NAT and subnet are read and checked, never updated.
    assert not [c for c in calls if c[:3] == ["compute", "routers", "nats"] and c[3] != "describe"]
    assert not [c for c in calls if c[:3] == ["compute", "networks", "subnets"] and c[3] != "describe"]
    _deploy(calls)  # exactly one create-or-update of the service
    _one(calls, "run", "services", "add-iam-policy-binding", SERVICE)
    assert "updated" in proc.stdout


def test_two_runs_create_each_resource_at_most_once(tmp_path):
    """First run from nothing, second over what the first made: each create appears once overall."""
    (tmp_path / "1").mkdir()
    (tmp_path / "2").mkdir()
    first, first_calls = _run(tmp_path / "1", "prod", SHA)
    second, second_calls = _run(tmp_path / "2", "prod", SHA, env={"EXISTS": "1"})
    assert first.returncode == 0 and second.returncode == 0, (first.stderr, second.stderr)
    created = [tuple(c[:5]) for c in _creates(first_calls + second_calls)]
    assert len(created) == len(set(created)) == 5, created  # subnet, address, router, NAT, identity


# ── output, environments, project ──────────────────────────────────────────────────────────────


def test_the_follow_ups_are_printed_from_describe_not_run(tmp_path):
    proc, calls = _run(tmp_path, "prod", SHA)
    assert proc.returncode == 0, proc.stderr
    assert ADDRESS_MARKER in proc.stdout, "the address comes from describe at run time"
    assert f"MERCHANT_DOOR_RELAY_URL={RELAY_URL}" in proc.stdout
    assert "MERCHANT_DOOR_RELAY=on" in proc.stdout
    assert "NOT done here" in proc.stdout
    _one(calls, "compute", "addresses", "describe", ADDR, "--region", "us-west1", "--project", "pivota-prod",
         "--format=value(address)")


def test_staging_targets_the_staging_project_throughout(tmp_path):
    proc, calls = _run(tmp_path, "staging", SHA, env={"SERVICE_POLICY": _default_policy("pivota-staging")})
    assert proc.returncode == 0, proc.stderr
    svc = _deploy(calls)
    assert _flag(svc, "--project") == "pivota-staging"
    assert _flag(svc, "--service-account") == "sa-door-relay@pivota-staging.iam.gserviceaccount.com"
    assert dict(p.split("=", 1) for p in _flag(svc, "--set-env-vars").split(","))["PIVOTA_ENV"] == "staging"
    grant = _one(calls, "run", "services", "add-iam-policy-binding", SERVICE)
    assert _flag(grant, "--member") == "serviceAccount:sa-gateway@pivota-staging.iam.gserviceaccount.com"
    assert "WARNING" not in proc.stderr, proc.stderr


@pytest.mark.parametrize("env", ["prod", "staging"])
def test_every_project_scoped_call_names_the_envs_project(tmp_path, env):
    project = f"pivota-{env}"
    proc, calls = _run(tmp_path, env, SHA, env={"SERVICE_POLICY": _default_policy(project)})
    assert proc.returncode == 0, proc.stderr
    for call in calls:
        if call[:4] == ["artifacts", "docker", "images", "describe"]:
            continue  # images live in pivota-shared, named in full by the image path
        if call[:2] == ["projects", "get-iam-policy"]:
            assert call[2] == project, call
            continue
        assert _flag(call, "--project") == project, call
