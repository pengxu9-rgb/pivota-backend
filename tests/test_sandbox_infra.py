"""Partner sandbox infra (infra/gcp/sandbox/): the profile guard and the dry-run default.

Two claims are tested here:

1. check_profile.py passes on the committed profiles, and each of its rules (R1-R11) refuses a
   profile that breaks it. A rule with no failing case would be a rule nobody has seen fire.
2. Every mutating script is a DRY RUN unless APPLY=1: run against a fake gcloud that records its
   argv, no create / delete / bind / deploy / enable / link verb is ever executed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "infra" / "gcp" / "sandbox"
sys.path.insert(0, str(SANDBOX))
import check_profile  # noqa: E402

WEB_URL = "https://web-123456789012.us-west1.run.app"


def _private_test_ranges():
    """A private /24 and a non-overlapping private /20, computed, so no address literal is in the repo."""
    import ipaddress
    block = ipaddress.ip_network((10 << 24, 8))          # the RFC 1918 10/8 block
    return next(block.subnets(new_prefix=24)), list(block.subnets(new_prefix=20))[1]


@pytest.fixture(autouse=True, scope="module")
def _sandbox_local_config(tmp_path_factory):
    """config.sh reads the sandbox's private ranges from an untracked file; tests get a throwaway one
    with generic ranges computed here (the repo carries no private IP literal)."""
    subnet, psa = _private_test_ranges()
    path = tmp_path_factory.mktemp("sbxcfg") / "config.local.sh"
    path.write_text(f"SUBNET_RANGE={subnet}\nPSA_RANGE_START={psa.network_address}\n")
    old = os.environ.get("SANDBOX_LOCAL_CONFIG")
    os.environ["SANDBOX_LOCAL_CONFIG"] = str(path)
    yield path
    if old is None:
        os.environ.pop("SANDBOX_LOCAL_CONFIG", None)
    else:
        os.environ["SANDBOX_LOCAL_CONFIG"] = old


def _copy_tree(tmp_path: Path) -> tuple[Path, Path]:
    env_dir, sec_dir = tmp_path / "env", tmp_path / "secrets"
    shutil.copytree(SANDBOX / "env", env_dir)
    shutil.copytree(SANDBOX / "secrets", sec_dir)
    return env_dir, sec_dir


def _set(path: Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines()
    out, done = [], False
    for line in lines:
        if line.startswith(f"{key}:"):
            out.append(f'{key}: "{value}"')
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f'{key}: "{value}"')
    path.write_text("\n".join(out) + "\n")


def _drop(path: Path, key: str) -> None:
    path.write_text("\n".join(l for l in path.read_text().splitlines() if not l.startswith(f"{key}:")) + "\n")


def _problems(env_dir: Path, sec_dir: Path) -> list[str]:
    return check_profile.check_all(env_dir, sec_dir)


def test_committed_profiles_pass():
    assert _problems(SANDBOX / "env", SANDBOX / "secrets") == []


def test_committed_profiles_pass_after_render(tmp_path):
    rc = check_profile.main(["--render", str(tmp_path / "out"), "--web-url", WEB_URL,
                             "--gateway-host", "sandbox.mcp.pivota.cc", "--agenpaz-host", "sandbox.agenpaz.com",
                             "--sandbox-project", "pivota-sandbox"])
    assert rc == 0
    gw = (tmp_path / "out" / "gateway.yaml").read_text()
    assert f'PIVOTA_API_BASE: "{WEB_URL}"' in gw
    assert 'AGENPAS_PUBLIC_BASE_URL: "https://sandbox.agenpaz.com"' in gw
    for svc in ("web", "worker", "gateway", "agenpaz"):
        assert 'GOOGLE_CLOUD_PROJECT: "pivota-sandbox"' in (tmp_path / "out" / f"{svc}.yaml").read_text()
    assert not [l for l in gw.splitlines() if "@@" in l and not l.startswith("#")]


#: The backend's sandbox startup guard refuses to boot web/worker unless these are set this way:
#: each code default is ON or a prod URL (ENV_PROFILE.md, "Required by the backend's sandbox
#: startup guard"). PUBLIC_API_BASE_URL is the gateway host token, rendered below.
S1_GUARD_SETTINGS = {
    "FF_ENABLE_INTERNAL_REFUND": "false",
    "FF_ENABLE_MERCHANT_ORDER_FAILURE_REFUND": "false",
    "PIVOTA_AGENT_INTERNAL_URL": "",
    "OUTBOUND_WARM_HANDOFF_RESOLVE_URL": "",
    "PUBLIC_API_BASE_URL": "https://@@GATEWAY_HOST@@",
    "AGENT_SHOP_BUDGET_FX_LATEST_FALLBACK_ENABLED": "false",
    "STORE_LIFECYCLE_RECONCILE_ENABLED": "false",
    "AUDIT_RESOLVE_GROUNDING_REDIRECTS": "false",
}


def test_web_and_worker_set_what_the_backend_sandbox_guard_requires(tmp_path):
    for svc in ("web", "worker"):
        env = check_profile.parse_profile(SANDBOX / "env" / f"{svc}.yaml", [])
        assert {k: env.get(k) for k in S1_GUARD_SETTINGS} == S1_GUARD_SETTINGS, svc
    assert check_profile.main(["--render", str(tmp_path / "out"), "--web-url", WEB_URL,
                               "--gateway-host", "sandbox.mcp.pivota.cc", "--agenpaz-host", "sandbox.agenpaz.com",
                               "--sandbox-project", "pivota-sandbox"]) == 0
    for svc in ("web", "worker"):
        assert 'PUBLIC_API_BASE_URL: "https://sandbox.mcp.pivota.cc"' in (tmp_path / "out" / f"{svc}.yaml").read_text()


@pytest.mark.parametrize(
    "svc,key,value,rule",
    [
        ("web", "PIVOTA_ENV", "staging", "R1"),
        ("gateway", "GOOGLE_CLOUD_PROJECT", "pivota-prod", "R1"),
        ("agenpaz", "GOOGLE_CLOUD_PROJECT", "", "R1"),
        ("gateway", "PIVOTA_ENV", "production", "R1"),
        ("gateway", "AGENPAS_CONSENT_ENABLED", "0", "R4"),
        ("agenpaz", "AGENPAS_CONSENT_ENABLED", "1", "R4"),
        ("gateway", "NODE_ENV", "production", "R12"),
        ("agenpaz", "NODE_ENV", "test", "R12"),
        ("agenpaz", "CATALOG_IMAGE_CACHE_JOB_RUNNER_ENABLED", "true", "R5"),
        ("gateway", "UCP_LIVE_OFFER_READ", "1", "R5"),
        ("web", "LEGACY_SETTLEMENT_LIVE", "true", "R5"),
        ("worker", "A2A_RAIL_ENABLED", "1", "R2"),
        ("web", "A2A_SHIPPING_CAPTURE_ENABLED", "1", "R2"),
        ("web", "REAP_API_BASE_URL", "https://api.reap.global", "R3"),
        ("worker", "REAP_AGENTIC_SIMULATE_CHECKOUT", "", "R3"),
        ("gateway", "AGENPAS_RAILS", "a2a,reap", "R4"),
        ("agenpaz", "AGENPAS_RAILS", "a2a", "R4"),
        ("web", "LIVE_OFFER_VERIFICATION_ENABLED", "1", "R5"),
        ("gateway", "SERVE_LIVE_MERCHANT_PRICE", "1", "R5"),
        ("gateway", "LLM_KILL_SWITCH", "false", "R5"),
        ("agenpaz", "LLM_KILL_SWITCH", "1", "R5"),
        ("agenpaz", "VERTEX_AI_ENABLED", "true", "R5"),
        ("web", "AGENPAS_HELP_URL", "https://pivota.cc/help", "R6"),
        ("gateway", "SKIP_DB_MIGRATIONS", "false", "R5"),
        ("agenpaz", "SKIP_DB_MIGRATIONS", "", "R5"),
        ("agenpaz", "DB_AUTO_MIGRATE", "true", "R5"),
        ("web", "ISSUING_AGENT_EXCLUDED_AGENT_IDS", "agent_982b1ea2df866206", "R13"),
        ("worker", "ISSUING_ASSERTION_VOUCHER_AGENT_IDS", "", "R13"),
        ("web", "AGENT_ACP_ALLOW_LIVE_CAPTURE", "true", "R5"),
        ("gateway", "PIVOTA_API_BASE", "https://api.pivota.cc", "R6"),
        ("gateway", "PIVOTA_API_BASE", "https://web-999999999999.us-west1.run.app", "R6"),
        ("web", "CATALOG_INTELLIGENCE_BASE_URL", "https://x.up.railway.app", "R6"),
        ("agenpaz", "AGENPAS_HOSTED_HOSTS", "sandbox.collect.prava.space,agent-dev.agenzo.com", "R6"),
        ("agenpaz", "AGENPAS_PUBLIC_BASE_URL", "https://pay.agenpaz.com", "R6"),
        ("web", "STRIPE_SECRET_KEY", "sk_live_x", "R7"),
        ("gateway", "OPENAI_API_KEY", "x", "R7"),
    ],
)
def test_each_rule_refuses_a_breaking_value(tmp_path, svc, key, value, rule):
    env_dir, sec_dir = _copy_tree(tmp_path)
    _set(env_dir / f"{svc}.yaml", key, value)
    if svc == "web" and key not in check_profile.WORKER_ONLY_DIFFERENCES:
        _set(env_dir / "worker.yaml", key, value)  # isolate the rule under test from R9
    got = _problems(env_dir, sec_dir)
    assert any(p.startswith(rule) and key in p for p in got), got


@pytest.mark.parametrize("svc,key", [("web", "WEBHOOK_DELIVERY_ENABLED"), ("gateway", "PDP_CORE_PREWARM_ENABLED"),
                                     ("agenpaz", "CATALOG_IMAGE_CACHE_JOB_RUNNER_ENABLED"),
                                     ("worker", "PAYMENT_RECONCILE_SWEEP_ENABLED")])
def test_an_off_switch_must_be_present_not_merely_absent(tmp_path, svc, key):
    env_dir, sec_dir = _copy_tree(tmp_path)
    _drop(env_dir / f"{svc}.yaml", key)
    assert any(p.startswith("R5") and key in p and "explicitly" in p for p in _problems(env_dir, sec_dir))


def test_unknown_secret_and_double_mount_are_refused(tmp_path):
    env_dir, sec_dir = _copy_tree(tmp_path)
    with (sec_dir / "web.list").open("a") as f:
        f.write("STRIPE_SECRET_KEY=env-STRIPE_SECRET_KEY\nJWT_SECRET_KEY=JWT_SECRET_KEY\n")
    got = _problems(env_dir, sec_dir)
    assert any(p.startswith("R8") and "env-STRIPE_SECRET_KEY" in p for p in got)
    assert any(p.startswith("R8") and "JWT_SECRET_KEY mounted twice" in p for p in got)


def test_the_gateway_agent_id_is_one_value_in_config_and_both_profiles():
    agent = check_profile.sandbox_gateway_agent_id()
    assert agent.startswith("agent_") and len(agent) == 22
    for svc in ("web", "worker"):
        env = check_profile.parse_profile(SANDBOX / "env" / f"{svc}.yaml", [])
        assert [env[k] for k in check_profile.GATEWAY_AGENT_KEYS] == [agent, agent]
    assert agent in (SANDBOX / "RUNBOOK.md").read_text()


def test_catalog_drop_dry_run_shows_the_legacy_binding_removal_before_the_bucket_exists(tmp_path):
    fake = _fake_gcloud(tmp_path)
    env = {**os.environ, "GCLOUD": str(fake)}
    env.pop("APPLY", None)
    res = subprocess.run(["bash", str(SANDBOX / "setup_catalog_drop.sh"), "sandbox"], env=env,
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert "Traceback" not in res.stdout + res.stderr
    assert "remove every roles/storage.legacy* binding" in res.stdout + res.stderr
    assert _executed_mutations(tmp_path / "calls.jsonl") == []


def test_worker_may_not_drift_from_web(tmp_path):
    env_dir, sec_dir = _copy_tree(tmp_path)
    _set(env_dir / "worker.yaml", "RATE_LIMIT_RPM", "9999")
    assert any(p.startswith("R9") and "RATE_LIMIT_RPM" in p for p in _problems(env_dir, sec_dir))


def test_duplicate_key_and_malformed_line_are_refused(tmp_path):
    env_dir, sec_dir = _copy_tree(tmp_path)
    with (env_dir / "gateway.yaml").open("a") as f:
        f.write('AGENPAS_RAILS: "a2a"\nNOT A LINE\n')
    got = _problems(env_dir, sec_dir)
    assert any(p.startswith("R10") and "duplicate key AGENPAS_RAILS" in p for p in got)
    assert any(p.startswith("R10") and "not a KEY" in p for p in got)


def test_unknown_token_and_unrendered_token(tmp_path):
    env_dir, sec_dir = _copy_tree(tmp_path)
    _set(env_dir / "gateway.yaml", "PIVOTA_BACKEND_BASE_URL", "@@PROD_URL@@")
    assert any(p.startswith("R11") and "@@PROD_URL@@" in p for p in _problems(env_dir, sec_dir))
    rendered = check_profile.check_all(SANDBOX / "env", SANDBOX / "secrets", rendered=True)
    assert any(p.startswith("R11") for p in rendered)


def test_render_refuses_a_web_url_that_is_not_a_sandbox_run_app_url(tmp_path):
    rc = check_profile.main(["--render", str(tmp_path / "o"), "--web-url", "https://api.pivota.cc",
                             "--gateway-host", "sandbox.mcp.pivota.cc", "--agenpaz-host", "sandbox.agenpaz.com",
                             "--sandbox-project", "pivota-sandbox"])
    assert rc == 2


@pytest.mark.parametrize("project", ["pivota-prod", "PIVOTA-SANDBOX", "388293626878", "pivota-staging-sbx", ""])
def test_render_refuses_a_sandbox_project_that_is_not_one(tmp_path, project):
    rc = check_profile.main(["--render", str(tmp_path / "o"), "--web-url", WEB_URL,
                             "--gateway-host", "sandbox.mcp.pivota.cc", "--agenpaz-host", "sandbox.agenpaz.com",
                             "--sandbox-project", project])
    assert rc == 2


def test_every_listed_secret_is_in_the_inventory_and_the_lists_cover_every_service():
    names = {p.stem for p in (SANDBOX / "secrets").glob("*.list")}
    assert names == {"web", "worker", "gateway", "agenpaz", "catalog-import", "schema-apply", "ops"}
    for p in (SANDBOX / "secrets").glob("*.list"):
        for line in p.read_text().split():
            assert line.split("=", 1)[1] in check_profile.ALLOWED_SECRETS


# ------------------------------------------------------------------------------- dry-run default

MUTATING = ("create", "delete", "add-iam-policy-binding", "remove-iam-policy-binding", "deploy", "enable",
            "link", "import", "connect", "execute", "update", "update-traffic", "add", "add-backend")


def _fake_gcloud(tmp_path: Path) -> Path:
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "gcloud"
    fake.write_text(f"""#!{sys.executable}
import json, sys
with open({str(log)!r}, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(1)   # nothing exists, every read fails
""")
    fake.chmod(0o755)
    return fake


def _git_repo(path: Path) -> Path:
    bare = path.with_suffix(".git")
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", str(bare)], check=True)
    return path


def _executed_mutations(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    calls = [json.loads(l) for l in log.read_text().splitlines()]
    return [c for c in calls if any(v in c for v in MUTATING) and "describe" not in c and "list" not in c]


@pytest.mark.parametrize(
    "argv",
    [
        ["bootstrap_sandbox.sh", "project"],
        ["bootstrap_sandbox.sh", "network"],
        ["bootstrap_sandbox.sh", "sql"],
        ["bootstrap_sandbox.sh", "secrets"],
        ["bootstrap_sandbox.sh", "iam"],
        ["setup_sandbox_lb.sh"],
        ["setup_catalog_drop.sh", "sandbox"],
        ["setup_catalog_drop.sh", "prod-writer"],
        ["deploy_sandbox.sh", "sandbox-r1"],
        ["catalog_export_prod.sh", "us-west1-docker.pkg.dev/pivota-shared/pivota/backend@sha256:" + "d" * 64, "7"],
    ],
)
def test_dry_run_executes_no_mutating_cloud_call(tmp_path, argv):
    fake = _fake_gcloud(tmp_path)
    env = {**os.environ, "GCLOUD": str(fake), "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "BILLING_ACCOUNT": "000000-000000-000000", "HOME": str(tmp_path),
           "GATEWAY_REPO_DIR": str(_git_repo(tmp_path / "gw")),
           "BACKEND_REPO_DIR": str(_git_repo(tmp_path / "be"))}
    env.pop("APPLY", None)
    res = subprocess.run(["bash", str(SANDBOX / argv[0]), *argv[1:]], env=env, capture_output=True, text=True,
                         timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "dry run" in (res.stdout + res.stderr)
    assert _executed_mutations(tmp_path / "calls.jsonl") == []
    # ...and the plan was printed, so a reviewer sees the exact commands.
    assert "[dry-run]" in res.stderr or "[dry-run]" in res.stdout


DRY_RUN_ARGVS = [
    ["bootstrap_sandbox.sh", "project"], ["bootstrap_sandbox.sh", "network"], ["bootstrap_sandbox.sh", "sql"],
    ["bootstrap_sandbox.sh", "secrets"], ["bootstrap_sandbox.sh", "iam"], ["setup_sandbox_lb.sh"],
    ["setup_catalog_drop.sh", "sandbox"], ["setup_catalog_drop.sh", "prod-writer"], ["deploy_sandbox.sh", "sandbox-r1"],
    ["schema_dump_prod.sh", "us-west1-docker.pkg.dev/pivota-shared/pivota/sandbox-schema-dump@sha256:" + "b" * 64],
    ["catalog_export_prod.sh", "us-west1-docker.pkg.dev/pivota-shared/pivota/backend@sha256:" + "d" * 64, "7"],
]


@pytest.mark.parametrize("argv", DRY_RUN_ARGVS)
def test_the_project_id_has_one_source(tmp_path, argv):
    """RUNBOOK Q13: renaming the project is SANDBOX_PROJECT alone. Under another id, no planned
    command, no gcloud call and no printed line may still name the default id."""
    fake = _fake_gcloud(tmp_path)
    env = {**os.environ, "GCLOUD": str(fake), "SANDBOX_PROJECT": "pivota-sbx-test",
           "BILLING_ACCOUNT": "000000-000000-000000", "HOME": str(tmp_path),
           "GATEWAY_REPO_DIR": str(_git_repo(tmp_path / "gw")), "BACKEND_REPO_DIR": str(_git_repo(tmp_path / "be"))}
    env.pop("APPLY", None)
    res = subprocess.run(["bash", str(SANDBOX / argv[0]), *argv[1:]], env=env, capture_output=True, text=True,
                         timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout + res.stderr
    assert "pivota-sbx-test" in out
    assert "pivota-sandbox" not in out
    log = tmp_path / "calls.jsonl"
    assert "pivota-sandbox" not in (log.read_text() if log.exists() else "")


@pytest.mark.parametrize("project", [
    "pivota-prod", "pivota-staging", "pivota-shared",
    # variants a name match alone would miss: case, whitespace, a project NUMBER (gcloud --project
    # accepts numbers), a trailing hyphen, a too-short id, an environment word inside the id
    "PIVOTA-PROD", "Pivota-Staging", " pivota-prod", "pivota-prod ", "pivota-prod\n", "pivota\tprod",
    "123456789012", "0pivota-sandbox", "pivota-sandbox-", "pivo", "x" * 31,
    "pivota-prod-sandbox", "sandbox-staging", "pivota-shared-sbx", "pivota-stg-sandbox", "pivota-production",
])
def test_scripts_refuse_a_real_environment_as_the_sandbox_project(tmp_path, project):
    fake = _fake_gcloud(tmp_path)
    env = {**os.environ, "GCLOUD": str(fake), "SANDBOX_PROJECT": project, "BILLING_ACCOUNT": "x"}
    for argv in DRY_RUN_ARGVS + [["verify_sandbox.sh"]]:
        res = subprocess.run(["bash", str(SANDBOX / argv[0]), *argv[1:]], env=env,
                             capture_output=True, text=True, timeout=60)
        assert res.returncode == 2 and "refusing" in res.stderr, (argv, res.stderr)
    assert not (tmp_path / "calls.jsonl").exists()


@pytest.mark.parametrize("project", ["pivota-sandbox", "pivota-partner-sandbox", "pivota-sbx-us", "pivota-sandbox-2"])
def test_valid_sandbox_ids_are_accepted(tmp_path, project):
    fake = _fake_gcloud(tmp_path)
    env = {**os.environ, "GCLOUD": str(fake), "SANDBOX_PROJECT": project}
    env.pop("APPLY", None)
    res = subprocess.run(["bash", str(SANDBOX / "bootstrap_sandbox.sh"), "network"], env=env,
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr


# ------------------------------------------------------------------- APPLY-mode harness (review S1-S5)
# A stateful fake gcloud that answers like a real sandbox project, and real git origins carrying
# annotated release tags, so the deploy's promote path runs for real (nothing leaves the machine).

PN = "123456789012"
DIGEST = "sha256:" + "a" * 64


def _apply_gcloud(tmp_path: Path, scenario: str, project: str = "pivota-sandbox") -> Path:
    fake = tmp_path / "gcloud"
    fake.write_text(f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
with open({str(tmp_path / "calls.jsonl")!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
scenario = {scenario!r}; project = {project!r}
def has(*w): return all(x in args for x in w)
def fmt(): return next((a.split("=", 1)[1] for a in args if a.startswith("--format=")), "")
if has("projects", "describe"):
    print({PN!r}); sys.exit(0)
if has("secrets", "versions", "list"):
    print("1"); sys.exit(0)
if has("artifacts", "docker", "images", "describe"):
    print({DIGEST!r}); sys.exit(0)
flags = set(scenario.split("+"))  # the drop tests combine scenarios, e.g. "owner-only+no-sa"
if has("iam", "service-accounts", "describe") and "no-sa" in flags:
    sys.exit(1)
if has("config", "get-value", "account"):
    if "no-account" in flags:
        sys.exit(0)  # prints nothing, as gcloud does with no account set
    print("Operator@Example.com"); sys.exit(0)
if has("projects", "get-iam-policy"):
    admin = {{"role": "roles/storage.admin", "members": ["user:operator@example.com"]}}
    if "owner-only" in flags:
        admin = None
    elif "storage-admin-conditional" in flags:
        admin["condition"] = {{"title": "t", "expression": "request.time < timestamp('2026-01-01T00:00:00Z')"}}
    elif "storage-admin-other-user" in flags:
        admin["members"] = ["user:someone-else@example.com"]
    print(json.dumps({{"bindings": [b for b in ({{"role": "roles/owner", "members": ["user:operator@example.com"]}}, admin) if b]}}))
    sys.exit(0)
if has("storage", "buckets", "describe"):
    # The top-level key lists of gcloud 581's two shapes, as read from the real drop bucket on
    # 2026-11-27 (names only). Only --raw (the API's own resource) carries the project number.
    num = "999999999999" if scenario == "foreign-bucket" else {PN!r}
    if "--raw" in args:
        print(json.dumps({{"etag": "CAM=", "generation": "1", "iamConfiguration": {{"bucketPolicyOnly": {{"enabled": True}},
                          "uniformBucketLevelAccess": {{"enabled": True}}, "publicAccessPrevention": "enforced"}},
                          "id": "x", "kind": "storage#bucket", "lifecycle": {{"rule": []}}, "location": "US-WEST1",
                          "locationType": "region", "metageneration": "3", "name": "x", "projectNumber": num,
                          "satisfiesPZI": False, "selfLink": "https://www.googleapis.com/storage/v1/b/x",
                          "softDeletePolicy": {{}}, "storageClass": "STANDARD",
                          "timeCreated": "2026-11-27T00:00:00.000Z", "updated": "2026-11-27T00:00:00.000Z"}}))
    else:
        print(json.dumps({{"creation_time": "2026-11-27T00:00:00+0000", "default_storage_class": "STANDARD",
                          "generation": 1, "lifecycle_config": {{"rule": []}}, "location": "US-WEST1",
                          "location_type": "region", "metageneration": 3, "name": "x",
                          "public_access_prevention": "enforced", "soft_delete_policy": {{}}, "storage_url": "gs://x/",
                          "uniform_bucket_level_access": True, "update_time": "2026-11-27T00:00:00+0000"}}))
    sys.exit(0)
if has("storage", "buckets") and ("get-iam-policy" in args or "add-iam-policy-binding" in args
                                   or "remove-iam-policy-binding" in args):
    # A new bucket's default policy (the project-convenience legacy bindings), kept between calls.
    import os
    state = {str(tmp_path / "bucket_policy.json")!r}
    if os.path.exists(state):
        pol = json.load(open(state))
    else:
        p = "projectOwner:" + project, "projectEditor:" + project, "projectViewer:" + project
        pol = {{"bindings": [{{"role": "roles/storage.legacyBucketOwner", "members": [p[0], p[1]]}},
                            {{"role": "roles/storage.legacyBucketReader", "members": [p[2]]}},
                            {{"role": "roles/storage.legacyObjectOwner", "members": [p[0], p[1]]}},
                            {{"role": "roles/storage.legacyObjectReader", "members": [p[2]]}}]}}
    if "get-iam-policy" in args:
        print(json.dumps(pol)); sys.exit(0)
    member = next(a.split("=", 1)[1] for a in args if a.startswith("--member="))
    role = next(a.split("=", 1)[1] for a in args if a.startswith("--role="))
    if "remove-iam-policy-binding" in args and scenario == "remove-is-noop":
        sys.exit(0)
    b = next((b for b in pol["bindings"] if b["role"] == role), None)
    if "add-iam-policy-binding" in args:
        if b is None:
            b = {{"role": role, "members": []}}; pol["bindings"].append(b)
        if member not in b["members"]:
            b["members"].append(member)
    elif b is not None and member in b["members"]:
        b["members"].remove(member)
        pol["bindings"] = [x for x in pol["bindings"] if x["members"]]
    json.dump(pol, open(state, "w")); sys.exit(0)
if has("run", "services", "describe"):
    svc = args[args.index("describe") + 1]
    if fmt() == "json":
        if scenario == "first":
            sys.exit(1)
        print(json.dumps({{"status": {{"traffic": [{{"revisionName": svc + "-old", "percent": 100}}],
                                       "latestReadyRevisionName": svc + "-old"}}}})); sys.exit(0)
    print(svc + "-new"); sys.exit(0)
if has("run", "revisions", "describe"):
    if fmt() == "json":
        env = [{{"name": "PIVOTA_ENV", "value": "sandbox"}}, {{"name": "GOOGLE_CLOUD_PROJECT", "value": project}}]
        if scenario == "missing-gcp":
            env = env[:1]
        print(json.dumps({{"spec": {{"containers": [{{"env": env}}]}}}}))
    else:
        print("True")
    sys.exit(0)
if has("run", "jobs", "deploy"):
    sys.exit(1 if scenario == "probe-deploy-fails" else 0)
if has("run", "deploy") and "--env-vars-file" in args:
    with open({str(tmp_path / "envfiles.jsonl")!r}, "a") as f:
        f.write(json.dumps([args[args.index("deploy") + 1], open(args[args.index("--env-vars-file") + 1]).read()]) + "\\n")
sys.exit(0)
""")
    fake.chmod(0o755)
    return fake


def _sandbox_copy(dest: Path, src: Path = SANDBOX) -> Path:
    """infra/gcp/sandbox + infra/gcp/_gateway_image.sh under dest, as a release commit carries them."""
    shutil.copytree(src, dest / "infra" / "gcp" / "sandbox", symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "config.local.sh"))
    shutil.copy(SANDBOX.parent / "_gateway_image.sh", dest / "infra" / "gcp" / "_gateway_image.sh")
    return dest / "infra" / "gcp" / "sandbox"


def _release_repo(path: Path, *, on_main: bool = True, tree: Path | None = None, name: str | None = None) -> Path:
    """A repo whose annotated tag sandbox-r1 is on origin/main. With `tree`, the tagged commit holds
    that directory as infra/gcp/sandbox (the backend: S4, a deploy runs the TAG's config). Its origin
    is named like the real repository (the deploy pins the name): be -> pivota-backend, gw -> PIVOTA-Agent."""
    name = name or {"be": "pivota-backend", "gw": "PIVOTA-Agent"}.get(path.name, path.name)
    bare = path.parent / f"{path.name}-origin" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    run = lambda *a, cwd=None: subprocess.run(a, cwd=cwd, check=True, capture_output=True)
    run("git", "init", "-q", "--bare", "-b", "main", str(bare))
    run("git", "init", "-q", "-b", "main", str(path))
    g = ["git", "-C", str(path), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    if tree is not None:
        _sandbox_copy(path, tree)
        run(*g, "add", "-A")
    run(*g, "commit", "-q", "--allow-empty", "-m", "base")
    run(*g, "remote", "add", "origin", str(bare))
    run(*g, "push", "-q", "origin", "main")
    if not on_main:
        run(*g, "checkout", "-q", "-b", "side")
        run(*g, "commit", "-q", "--allow-empty", "-m", "not on main")
    run(*g, "tag", "-a", "sandbox-r1", "-m", "r1")
    run(*g, "push", "-q", "origin", "refs/tags/sandbox-r1")
    return path


def _deploy(tmp_path, scenario, *, bash="bash", tree=SANDBOX, release_tree=SANDBOX, backend_on_main=True,
            services="web", extra_env=None, drop_env=()):
    """Run `tree`'s deploy_sandbox.sh (the operator's checkout) against a backend whose tag holds
    `release_tree` (None: a tag without any sandbox config)."""
    fake = _apply_gcloud(tmp_path, scenario)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    env = {**os.environ, "GCLOUD": str(fake), "APPLY": "1", "HOME": str(tmp_path), "SERVICES": services,
           "TMPDIR": str(tmp_path / "tmp"),
           "BACKEND_REPO_DIR": str(_release_repo(tmp_path / "be", on_main=backend_on_main, tree=release_tree)),
           "GATEWAY_REPO_DIR": str(_release_repo(tmp_path / "gw")), **(extra_env or {})}
    for k in drop_env:
        env.pop(k, None)
    res = subprocess.run([bash, str(tree / "deploy_sandbox.sh"), "sandbox-r1"], env=env,
                         capture_output=True, text=True, timeout=120)
    log = tmp_path / "calls.jsonl"
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return res, calls


def _named(calls, *words):
    return [c for c in calls if all(w in c for w in words)]


def test_s1_a_failed_probe_deploy_never_promotes(tmp_path):
    res, calls = _deploy(tmp_path, "probe-deploy-fails")
    assert res.returncode != 0
    assert "probe job deploy failed" in res.stderr
    assert not _named(calls, "jobs", "execute"), "executed the OLD probe job definition"
    assert not _named(calls, "update-traffic")


def test_m8_m9_an_existing_service_gets_a_candidate_then_promotion_by_revision_name(tmp_path):
    res, calls = _deploy(tmp_path, "existing")
    assert res.returncode == 0, res.stderr
    deploy = _named(calls, "run", "deploy", "web")
    assert deploy and "--no-traffic" in deploy[0] and "--tag" in deploy[0]
    promote = _named(calls, "update-traffic")
    assert promote and "--to-revisions" in promote[0] and "web-new=100" in promote[0]
    assert not any("--to-latest" in c for c in calls)
    assert list((tmp_path / ".pivota-sandbox-deploys" / "pivota-sandbox").glob("*.rollback"))


@pytest.mark.parametrize("bash", ["bash", "/bin/bash"])
def test_s5_first_deploy_runs_under_bash_3_2_and_5(tmp_path, bash):
    if not Path(bash).exists() and bash.startswith("/"):
        pytest.skip(f"{bash} not present")
    res, calls = _deploy(tmp_path, "first", bash=bash)
    assert res.returncode == 0, res.stderr
    deploy = _named(calls, "run", "deploy", "web")
    assert deploy and "--no-traffic" not in deploy[0]
    assert not _named(calls, "update-traffic")


def test_m11_a_tag_that_is_not_on_main_is_refused(tmp_path):
    res, calls = _deploy(tmp_path, "existing", backend_on_main=False)
    assert res.returncode != 0 and "not on origin/main" in res.stderr
    assert not _named(calls, "run", "deploy")


def test_m10_a_failing_profile_check_stops_the_deploy(tmp_path):
    tree = _sandbox_copy(tmp_path / "tree")
    _set(tree / "env" / "web.yaml", "A2A_RAIL_ENABLED", "1")
    res, calls = _deploy(tmp_path, "existing", release_tree=tree)  # the TAG carries the bad profile
    assert res.returncode != 0 and "R2" in res.stdout + res.stderr
    assert not _named(calls, "run", "deploy")


def test_m16_render_recheck_catches_a_bad_host_only_visible_after_render(tmp_path):
    # The template holds @@AGENPAZ_HOST@@, so only the rendered re-check can see pay.agenpaz.com.
    assert check_profile.check_all(SANDBOX / "env", SANDBOX / "secrets") == []
    rc = check_profile.main(["--render", str(tmp_path / "o"), "--web-url", WEB_URL,
                             "--gateway-host", "sandbox.mcp.pivota.cc", "--agenpaz-host", "pay.agenpaz.com",
                             "--sandbox-project", "pivota-sandbox"])
    assert rc == 1


def test_m12_the_prod_writer_grant_is_create_only(tmp_path):
    fake = _fake_gcloud(tmp_path)
    env = {**os.environ, "GCLOUD": str(fake)}
    env.pop("APPLY", None)
    res = subprocess.run(["bash", str(SANDBOX / "setup_catalog_drop.sh"), "prod-writer"], env=env,
                         capture_output=True, text=True, timeout=60)
    out = res.stdout + res.stderr
    assert res.returncode == 0 and "--role=roles/storage.objectCreator" in out
    assert "objectAdmin" not in out and "objectViewer" not in out.split("prod-writer")[0]


def test_s2_drop_bucket_cannot_be_overridden(tmp_path):
    fake = _fake_gcloud(tmp_path)
    env = {**os.environ, "GCLOUD": str(fake), "DROP_BUCKET": "some-prod-bucket", "APPLY": "1"}
    res = subprocess.run(["bash", str(SANDBOX / "setup_catalog_drop.sh"), "sandbox"], env=env,
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 2 and "DROP_BUCKET" in res.stderr
    assert not (tmp_path / "calls.jsonl").exists()


@pytest.mark.parametrize("side", ["sandbox", "prod-writer"])
def test_s2_a_same_named_bucket_of_another_project_is_never_changed(tmp_path, side):
    fake = _apply_gcloud(tmp_path, "foreign-bucket")
    env = {**os.environ, "GCLOUD": str(fake), "APPLY": "1"}
    res = subprocess.run(["bash", str(SANDBOX / "setup_catalog_drop.sh"), side], env=env,
                         capture_output=True, text=True, timeout=60)
    calls = [json.loads(l) for l in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert res.returncode != 0 and "belongs to project number 999999999999" in res.stderr
    for verb in ("update", "add-iam-policy-binding", "remove-iam-policy-binding"):
        assert not _named(calls, "storage", "buckets", verb), verb


# --------------------------------------------------- the drop bucket's first apply (2026-11-27)

def _drop_apply(tmp_path, side, scenario="ok"):
    fake = _apply_gcloud(tmp_path, scenario)
    env = {**os.environ, "GCLOUD": str(fake), "APPLY": "1"}
    res = subprocess.run(["bash", str(SANDBOX / "setup_catalog_drop.sh"), side], env=env,
                         capture_output=True, text=True, timeout=60)
    log = tmp_path / "calls.jsonl"
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    pol_file = tmp_path / "bucket_policy.json"
    return res, calls, (json.loads(pol_file.read_text()) if pol_file.exists() else None)


def _changes(calls):
    return [c for c in calls if any(v in c for v in ("create", "update", "add-iam-policy-binding",
                                                     "remove-iam-policy-binding"))]


@pytest.mark.parametrize("side", ["sandbox", "prod-writer"])
def test_the_bucket_project_number_is_read_from_the_raw_resource(tmp_path, side):
    # gcloud's standardized describe has no project number; only --raw has projectNumber.
    res, calls, _ = _drop_apply(tmp_path, side, "ok+no-sa")
    assert res.returncode == 0, res.stderr
    if side == "prod-writer":  # a missing SA is created, after every check
        assert _named(calls, "iam", "service-accounts", "create")
    assert f"belongs to pivota-sandbox ({PN})" in res.stdout
    assert any("--raw" in c for c in _named(calls, "storage", "buckets", "describe"))


def test_sandbox_side_adds_the_reader_before_any_legacy_removal_and_reads_back(tmp_path):
    res, calls, pol = _drop_apply(tmp_path, "sandbox")
    assert res.returncode == 0, res.stderr
    edits = [c for c in calls if "add-iam-policy-binding" in c or "remove-iam-policy-binding" in c]
    assert "add-iam-policy-binding" in edits[0] and "--role=roles/storage.objectViewer" in edits[0]
    removed = {(next(a for a in c if a.startswith("--role=")), next(a for a in c if a.startswith("--member=")))
               for c in edits if "remove-iam-policy-binding" in c}
    assert len(removed) == 6  # owner+editor on two owner roles, viewer on two reader roles
    assert pol == {"bindings": [{"role": "roles/storage.objectViewer",
                                 "members": ["serviceAccount:sa-catalog-import@pivota-sandbox.iam.gserviceaccount.com"]}]}
    assert "read back: no legacy convenience binding, reader present" in res.stdout
    # the read-back is a second policy read, after the last removal
    reads = [i for i, c in enumerate(calls) if "get-iam-policy" in c and "buckets" in c]
    last_remove = max(i for i, c in enumerate(calls) if "remove-iam-policy-binding" in c)
    assert len(reads) == 2 and reads[1] > last_remove


def test_sandbox_side_fails_when_a_legacy_binding_survives_the_removal(tmp_path):
    res, _, _ = _drop_apply(tmp_path, "sandbox", "remove-is-noop")
    assert res.returncode != 0 and "legacy bindings remain on gs://" in res.stderr


@pytest.mark.parametrize("side", ["sandbox", "prod-writer"])
@pytest.mark.parametrize("scenario,why", [
    ("owner-only", "does not hold roles/storage.admin"),
    ("storage-admin-conditional", "does not hold roles/storage.admin"),
    ("storage-admin-other-user", "does not hold roles/storage.admin"),
    ("no-account", "cannot read the operator account"),
])
def test_an_operator_without_direct_storage_admin_is_refused_before_any_change(tmp_path, side, scenario, why):
    # Project Owner alone lost the bucket's IAM mid-run once the legacy bindings were removed.
    # "no-sa": the prod export SA does not exist yet, so prod-writer would create it if it checked late.
    res, calls, _ = _drop_apply(tmp_path, side, scenario + "+no-sa")
    assert res.returncode != 0 and why in res.stderr, res.stderr
    assert _changes(calls) == []


def test_the_operator_check_matches_the_account_case_insensitively(tmp_path):
    # gcloud may print the account with capitals; IAM members are lower case.
    res, _, _ = _drop_apply(tmp_path, "sandbox")
    assert res.returncode == 0 and "operator operator@example.com holds roles/storage.admin" in res.stdout


def test_a_rerun_on_a_half_done_bucket_finishes_it(tmp_path):
    # The first apply's state: reader missing, some legacy bindings already gone.
    proj = "pivota-sandbox"
    (tmp_path / "bucket_policy.json").write_text(json.dumps({"bindings": [
        {"role": "roles/storage.legacyBucketOwner", "members": [f"projectEditor:{proj}"]},
        {"role": "roles/storage.legacyObjectReader", "members": [f"projectViewer:{proj}"]}]}))
    res, _, pol = _drop_apply(tmp_path, "sandbox")
    assert res.returncode == 0, res.stderr
    assert [b["role"] for b in pol["bindings"]] == ["roles/storage.objectViewer"]


@pytest.mark.parametrize("var,host", [
    ("GATEWAY_HOST", "mcp.pivota.cc"), ("GATEWAY_HOST", "api.pivota.cc"), ("AGENPAZ_HOST", "pay.agenpaz.com"),
    ("GATEWAY_HOST", "sandbox.example.com"), ("AGENPAZ_HOST", "Sandbox.agenpaz.com"),
    ("GATEWAY_HOST", "sandbox.agenpaz.com"),  # the same host as AgenPaz's: exclusive, refused
    ("AGENPAZ_HOST", "sandbox.pay.agenpaz.com"), ("GATEWAY_HOST", "sandbox.api.pivota.cc"),  # under a prod host
    ("GATEWAY_HOST", "sandbox.ucp.pivota.cc"), ("GATEWAY_HOST", "sandbox.acp.pivota.cc"),
    ("GATEWAY_HOST", "sandbox.commerce.mcp.pivota.cc"),
])
def test_s3_lb_hostnames_must_be_sandbox_hosts(tmp_path, var, host):
    fake = _fake_gcloud(tmp_path)
    env = {**os.environ, "GCLOUD": str(fake), var: host}
    env.pop("APPLY", None)
    res = subprocess.run(["bash", str(SANDBOX / "setup_sandbox_lb.sh")], env=env,
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 2 and "refusing" in res.stderr
    assert not (tmp_path / "calls.jsonl").exists()


def test_s6_db_password_and_token_never_on_an_argv_and_temp_files_removed(tmp_path):
    pw, token = "PW-" + "q" * 40, "TOK-" + "z" * 40
    log, made = tmp_path / "calls.jsonl", tmp_path / "user-created"
    fake = tmp_path / "gcloud"
    fake.write_text(f"""#!{sys.executable}
import json, os, sys
a = sys.argv[1:]
open({str(log)!r}, "a").write(json.dumps(["gcloud"] + a) + "\\n")
def has(*w): return all(x in a for x in w)
if has("auth", "print-access-token"): print({token!r}); sys.exit(0)
if has("sql", "instances", "describe"):
    print("192.0.2.10" if any("ipAddress" in x for x in a) else "ok"); sys.exit(0)
if has("sql", "users", "list"):
    print("pivota" if os.path.exists({str(made)!r}) else ""); sys.exit(0)
if has("secrets", "versions", "access"):
    out = next((x.split("=", 1)[1] for x in a if x.startswith("--out-file=")), None)
    val = "postgresql://pivota:x@192.0.2.10:5432/pivota" if "--secret=DATABASE_URL" in a else {pw!r}
    (open(out, "w").write(val) if out else sys.stdout.write(val)); sys.exit(0)
if has("secrets", "versions", "list"): print("1"); sys.exit(0)
sys.exit(0)
""")
    fake.chmod(0o755)
    curl = tmp_path / "bin" / "curl"
    curl.parent.mkdir()
    curl.write_text(f"""#!{sys.executable}
import json, sys
open({str(log)!r}, "a").write(json.dumps(["curl"] + sys.argv[1:]) + "\\n")
body = next(x for x in sys.argv[1:] if x.startswith("@") and x.endswith(".json"))[1:]
assert json.load(open(body))["password"] == {pw!r}
open({str(made)!r}, "w").write("1")
""")
    curl.chmod(0o755)
    tmpdir = tmp_path / "tmp"; tmpdir.mkdir()
    env = {**os.environ, "GCLOUD": str(fake), "APPLY": "1", "TMPDIR": str(tmpdir),
           "PATH": f"{curl.parent}:{os.environ['PATH']}"}
    res = subprocess.run(["bash", str(SANDBOX / "bootstrap_sandbox.sh"), "secrets"], env=env,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    assert made.exists(), "the DB user was not created through the API body"
    text = log.read_text()
    assert pw not in text and token not in text and "--password" not in text
    curl_calls = [json.loads(l) for l in text.splitlines() if l.startswith('["curl"')]
    assert curl_calls and all(c[1] == "-q" for c in curl_calls), "curl must ignore ~/.curlrc (-q first)"
    assert list(tmpdir.iterdir()) == [], "a secret temp file outlived the run"


def test_readback_is_fail_closed_when_a_signal_is_missing_on_the_candidate(tmp_path):
    res, calls = _deploy(tmp_path, "missing-gcp")
    assert res.returncode != 0 and "does not identify as the sandbox" in res.stderr
    deploy = _named(calls, "run", "deploy", "web")
    assert deploy and "--no-traffic" in deploy[0], "the read-back must run on the 0% candidate"
    assert not _named(calls, "jobs", "execute") and not _named(calls, "update-traffic")


def test_no_service_runs_as_or_mounts_what_only_the_ops_jobs_hold(tmp_path):
    res, calls = _deploy(tmp_path, "existing", services="web worker gateway agenpaz")
    assert res.returncode == 0, res.stderr
    deploys = {c[c.index("deploy") + 1]: c for c in _named(calls, "run", "deploy") if "jobs" not in c}
    probes = [c for c in _named(calls, "jobs", "deploy")]
    assert probes and all("sa-sandbox-ops" not in " ".join(c) for c in probes)
    assert set(deploys) == {"web", "worker", "gateway", "agenpaz"}
    for svc, argv in deploys.items():
        assert not any("sa-sandbox-ops" in a for a in argv), svc
        mounts = argv[argv.index("--set-secrets") + 1]
        if svc != "gateway":
            assert "PIVOTA_API_KEY" not in mounts and "DATABASE_URL_NOVERIFY" not in mounts, svc


def test_a_shape_that_names_the_ops_sa_is_refused(tmp_path):
    tree = _sandbox_copy(tmp_path / "tree")
    p = tree / "deploy_sandbox.sh"
    p.write_text(p.read_text().replace('web)     echo "$BACKEND_PIN $SA_BACKEND', 'web)     echo "$BACKEND_PIN $SA_OPS', 1))
    res, calls = _deploy(tmp_path, "existing", release_tree=tree)  # the TAG carries the bad shape
    assert res.returncode == 2 and "one-off jobs only" in res.stderr
    assert not _named(calls, "run", "deploy")


# ------------------------------------------------------------------- S4: the release's own config
def _web_memory(calls):
    argv = next(c for c in _named(calls, "run", "deploy", "web") if "jobs" not in c)
    return argv[argv.index("--memory") + 1]


def _set_web_memory(tree: Path, mem: str) -> None:
    p = tree / "deploy_sandbox.sh"
    old = 'web)     echo "$BACKEND_PIN $SA_BACKEND internal                          1 1 2 1 2Gi'
    assert old in p.read_text()
    p.write_text(p.read_text().replace(old, old.replace(" 2Gi", f" {mem}"), 1))


def test_s4_a_change_only_in_the_working_tree_is_not_deployed(tmp_path):
    checkout = _sandbox_copy(tmp_path / "checkout")
    _set_web_memory(checkout, "3Gi")
    with (checkout / "env" / "web.yaml").open("a") as f:
        f.write('S4_PROBE: "working-tree"\n')
    (checkout / "secrets" / "web.list").write_text((checkout / "secrets" / "web.list").read_text() + "X=STRAY\n")
    res, calls = _deploy(tmp_path, "existing", tree=checkout)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "handing over to sandbox-r1's own deploy script" in res.stdout
    assert "verified file by file" in res.stdout
    assert _web_memory(calls) == "2Gi"
    envfiles = dict(json.loads(l) for l in (tmp_path / "envfiles.jsonl").read_text().splitlines())
    assert "S4_PROBE" not in envfiles["web"]
    argv = next(c for c in _named(calls, "run", "deploy", "web") if "jobs" not in c)
    assert "STRAY" not in argv[argv.index("--set-secrets") + 1]


def test_s4_the_tags_config_is_deployed(tmp_path):
    release = _sandbox_copy(tmp_path / "release")
    _set_web_memory(release, "4Gi")
    for svc in ("web", "worker"):  # worker == web (R10), so the probe goes in both
        with (release / "env" / f"{svc}.yaml").open("a") as f:
            f.write('S4_PROBE: "tag"\n')
    res, calls = _deploy(tmp_path, "existing", release_tree=release, services="web worker")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _web_memory(calls) == "4Gi"
    envfiles = dict(json.loads(l) for l in (tmp_path / "envfiles.jsonl").read_text().splitlines())
    assert 'S4_PROBE: "tag"' in envfiles["web"] and 'S4_PROBE: "tag"' in envfiles["worker"]
    assert not list((tmp_path / "tmp").glob("sbx-release.*")), "the extracted release tree was left behind"


def test_s4_a_tag_without_the_sandbox_config_is_refused(tmp_path):
    res, calls = _deploy(tmp_path, "existing", release_tree=None)
    assert res.returncode == 1
    assert "has no infra/gcp/sandbox/deploy_sandbox.sh" in res.stderr
    assert not _named(calls, "run", "deploy")


def test_s4_a_symlink_in_the_tags_config_is_refused(tmp_path):
    release = _sandbox_copy(tmp_path / "release")
    (release / "env" / "extra.yaml").symlink_to("/etc/hosts")
    res, calls = _deploy(tmp_path, "existing", release_tree=release)
    assert res.returncode == 1 and "plain files only" in res.stderr
    assert not _named(calls, "run", "deploy")


def _tag_sha(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "sandbox-r1^{commit}"], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_s4_a_tag_that_moved_during_the_handover_is_refused(tmp_path):
    res, calls = _deploy(tmp_path, "existing", extra_env={"SANDBOX_DEPLOY_TREE_OF": "f" * 40})
    assert res.returncode == 1 and "now resolves to" in res.stderr
    assert not _named(calls, "run", "deploy")


@pytest.mark.parametrize("change", ["edit", "extra-file", "exec-bit"])
def test_s4_a_checkout_that_differs_from_the_tag_is_not_the_release(tmp_path, change):
    """The release copy's own check, run on a checkout claiming to be the tag."""
    checkout_root = tmp_path / "checkout"
    checkout = _sandbox_copy(checkout_root)
    be = _release_repo(tmp_path / "be", tree=SANDBOX)
    if change == "edit":
        _set_web_memory(checkout, "3Gi")
    elif change == "extra-file":
        (checkout / "secrets" / "stray.list").write_text("X=STRAY\n")
    else:
        (checkout / "deploy_sandbox.sh").chmod(0o644)
    fake = _apply_gcloud(tmp_path, "existing")
    env = {**os.environ, "GCLOUD": str(fake), "APPLY": "1", "HOME": str(tmp_path), "SERVICES": "web",
           "BACKEND_REPO_DIR": str(be), "GATEWAY_REPO_DIR": str(_release_repo(tmp_path / "gw")),
           "SANDBOX_DEPLOY_TREE_OF": _tag_sha(be)}
    res = subprocess.run(["bash", str(checkout / "deploy_sandbox.sh"), "sandbox-r1"], env=env,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 1 and "is not sandbox-r1's sandbox config" in res.stderr, res.stdout + res.stderr
    log = tmp_path / "calls.jsonl"
    assert not log.exists() or not _named([json.loads(l) for l in log.read_text().splitlines()], "run", "deploy")


def test_s4_a_clean_checkout_of_the_tag_passes_its_own_check(tmp_path):
    """The controller's other accepted form: a clean tree equal to the tag (with the operator's
    untracked config.local.sh and bytecode caches beside it) is the release."""
    be = _release_repo(tmp_path / "be", tree=SANDBOX)
    checkout = be / "infra" / "gcp" / "sandbox"
    (checkout / "config.local.sh").write_text(Path(os.environ["SANDBOX_LOCAL_CONFIG"]).read_text())
    (checkout / "__pycache__").mkdir()
    (checkout / "__pycache__" / "x.pyc").write_bytes(b"\0")
    fake = _apply_gcloud(tmp_path, "existing")
    env = {**os.environ, "GCLOUD": str(fake), "APPLY": "1", "HOME": str(tmp_path), "SERVICES": "web",
           "BACKEND_REPO_DIR": str(be), "GATEWAY_REPO_DIR": str(_release_repo(tmp_path / "gw")),
           "SANDBOX_DEPLOY_TREE_OF": _tag_sha(be)}
    env.pop("SANDBOX_LOCAL_CONFIG")
    res = subprocess.run(["bash", str(checkout / "deploy_sandbox.sh"), "sandbox-r1"], env=env,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "verified file by file" in res.stdout


def test_s4_the_operators_local_config_reaches_the_release_copy(tmp_path):
    """config.local.sh is untracked, so no tag holds it: the working-tree copy passes its path on."""
    checkout = _sandbox_copy(tmp_path / "checkout")
    (checkout / "config.local.sh").write_text(Path(os.environ["SANDBOX_LOCAL_CONFIG"]).read_text())
    res, calls = _deploy(tmp_path, "existing", tree=checkout, drop_env=("SANDBOX_LOCAL_CONFIG",))
    assert res.returncode == 0, res.stdout + res.stderr
    assert _named(calls, "run", "deploy", "web")


def test_s4_a_dry_run_without_tags_says_it_uses_the_working_tree(tmp_path):
    fake = _fake_gcloud(tmp_path)
    env = {**os.environ, "GCLOUD": str(fake), "HOME": str(tmp_path),
           "GATEWAY_REPO_DIR": str(_git_repo(tmp_path / "gw")), "BACKEND_REPO_DIR": str(_git_repo(tmp_path / "be"))}
    env.pop("APPLY", None)
    res = subprocess.run(["bash", str(SANDBOX / "deploy_sandbox.sh"), "sandbox-r1"], env=env,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "deploy config: this WORKING TREE (no release tag)" in res.stdout


@pytest.mark.parametrize("lst,line", [("web", "PIVOTA_API_KEY=PIVOTA_API_KEY"),
                                      ("worker", "DATABASE_URL=DATABASE_URL_NOVERIFY"),
                                      ("agenpaz", "PIVOTA_API_KEY=PIVOTA_API_KEY")])
def test_the_gateway_key_and_noverify_url_stay_with_the_gateway(tmp_path, lst, line):
    env_dir, sec_dir = _copy_tree(tmp_path)
    with (sec_dir / f"{lst}.list").open("a") as f:
        f.write(line + "\n")
    assert any(p.startswith("R8") and "may only be mounted" in p for p in _problems(env_dir, sec_dir))


def test_the_schema_dump_image_runs_only_the_dump_script_and_has_no_floating_tag():
    df = (SANDBOX / "Dockerfile.schema-dump").read_text()
    assert "FROM postgres:17" in df  # pg_dump must match prod's server major version
    assert "COPY scripts/sandbox/dump_prod_schema.sh /usr/local/bin/dump_prod_schema.sh" in df
    assert 'ENTRYPOINT ["/usr/local/bin/dump_prod_schema.sh"]' in df and "USER postgres" in df
    loop = next(l for l in df.splitlines() if l.startswith("RUN for t in "))
    assert set(loop.split(" in ", 1)[1].split(";")[0].split()) >= {"pg_dump", "psql", "curl", "gzip", "sha256sum", "timeout"}
    assert "command -v" in loop and "exit 1" in loop  # the BUILD fails without each one
    assert "pg_dump --version | grep -q ' 17\\.'" in df
    assert "RUN f=/usr/local/bin/dump_prod_schema.sh" in df  # the format-2 guard (behaviour tested below)
    cb = (SANDBOX / "cloudbuild.schema-dump.yaml").read_text()
    assert "infra/gcp/sandbox/Dockerfile.schema-dump" in cb and ":${COMMIT_SHA}" in cb
    assert ":latest" not in cb



# ------------------------------------------------------------------- S7: the stricter static rules
AK64 = "0123456789abcdef" * 4


@pytest.mark.parametrize("svc,key,value,rule", [
    # an empty value is not off
    ("gateway", "SERVE_LIVE_MERCHANT_PRICE", "", "R5"),
    ("web", "LIVE_OFFER_VERIFICATION_ENABLED", "", "R5"),
    # hosts outside URLs: bare, //host, email, inside a list
    ("web", "SHOP_UPSTREAM_HOST", "api.pivota.cc", "R6"),
    ("gateway", "UPSTREAM_REF", "//gateway.pivota.cc/x", "R6"),
    ("worker", "SUPPORT_EMAIL", "noreply@pivota.cc", "R6"),
    ("agenpaz", "AGENPAS_LEGAL_HOSTS", "pivota.cc,shop.pivota.cc", "R6"),
    # the brand rule now runs on every service, not only web/worker
    ("gateway", "HELP_LINK", "https://pivota.cc/help", "R6"),
    # the consent-link exception is exact: same setting, other path or a query string
    ("web", "AGENPAS_TERMS_URL", "https://pivota.cc/terms?v=2", "R6"),
    ("web", "AGENPAS_PRIVACY_URL", "https://merchant.pivota.cc/other", "R6"),
    # and only for that setting
    ("web", "SOME_TERMS_LINK", "https://pivota.cc/terms", "R6"),
    # prod/staging names anywhere in a value
    ("worker", "EXPORT_BUCKET", "pivota-staging-migration", "R6"),
    ("gateway", "VERTEX_HINT", "projects/pivota-prod/locations/us", "R6"),
    # a run.app host of any other project, in hash form too
    ("gateway", "PIVOTA_BACKEND_BASE_URL", "https://web-abc123def4-uw.a.run.app", "R6"),
    # IPs outside the sandbox's ranges: other private ranges (as prod's and staging's are), and a public one
    ("web", "PGHOST", "198.51.100.2", "R15"),
    ("worker", "REDIS_ADDR", "198.51.100.9:6379", "R15"),
    ("gateway", "LEGACY_DB_ADDR", "203.0.113.3", "R15"),
    ("agenpaz", "RESOLVER", "8.8.8.8", "R15"),
    # DB/Redis hosts may not be plain settings at all
    ("gateway", "PGHOST", "203.0.113.7", "R7"),
    ("web", "REDIS_HOST", "203.0.113.8", "R7"),
    # credential-shaped values under innocent names
    ("web", "PRICE_LABEL", "sk_live_" + "a1b2c3d4e5f6", "R14"),
    ("gateway", "DEBUG_NOTE", "ak_live_" + AK64, "R14"),
    ("gateway", "DEBUG_NOTE", "ak_test_" + AK64, "R14"),
    ("worker", "BACKUP_TARGET", "postgresql://u:pw@192.0.2.10:5432/pivota", "R14"),
    ("agenpaz", "STRIPE_HOOK_ID", "whsec_" + "x" * 24, "R14"),
    ("web", "AWS_HINT", "AKIA" + "ABCDEFGHIJKLMNOP", "R14"),
    # cross-project settings and the Aurora switches
    ("gateway", "PRICE_CACHE_FIRESTORE_PROJECT", "project-other", "R17"),
    ("gateway", "PIVOTA_AGENT_CENTER_GEMINI_VERTEX_PROJECT", "pivota-sbx-x", "R17"),
    ("gateway", "AURORA_ROUTES_FAIL_CLOSED", "true", "R17"),
    ("agenpaz", "AURORA_BFF_FAIL_CLOSED", "1", "R17"),
])
def test_s7_each_new_rule_refuses_a_breaking_value(tmp_path, svc, key, value, rule):
    env_dir, sec_dir = _copy_tree(tmp_path)
    _set(env_dir / f"{svc}.yaml", key, value)
    if svc == "web" and key not in check_profile.WORKER_ONLY_DIFFERENCES:
        _set(env_dir / "worker.yaml", key, value)
    got = _problems(env_dir, sec_dir)
    assert any(p.startswith(rule) and key in p for p in got), got


@pytest.mark.parametrize("svc,key,value", [
    ("gateway", "PRICE_CACHE_FIRESTORE_PROJECT", "@@SANDBOX_PROJECT@@"),  # equal to GOOGLE_CLOUD_PROJECT
    ("gateway", "AURORA_ROUTES_FAIL_CLOSED", "false"),
    ("worker", "MODEL_HINT", "gemini-2.5-flash-image"),                     # not a host
    ("agenpaz", "AGENPAS_LEGAL_HOSTS", "merchant.pivota.cc,pivota.cc"),
])
def test_s7_values_that_must_still_pass(tmp_path, svc, key, value):
    env_dir, sec_dir = _copy_tree(tmp_path)
    for target in (("web", "worker") if svc in ("web", "worker") else (svc,)):
        _set(env_dir / f"{target}.yaml", key, value)
    assert _problems(env_dir, sec_dir) == []


@pytest.mark.parametrize("lst,line,why", [
    ("web", "A2A_RAIL_API_KEY=REAP_API_KEY", "A2A"),
    ("gateway", "NODE_ENV=ADMIN_API_KEY", "pin"),
    ("web", "ISSUING_AGENT_EXCLUDED_AGENT_IDS=ADMIN_API_KEY", "pin"),
    ("worker", "REAP_API_BASE_URL=REAP_API_KEY", "pin"),
    ("web", "RATE_LIMIT_RPM=ADMIN_API_KEY", "shadow"),
    ("agenpaz", "AGENPAS_HOSTED_HOSTS=AGENPAS_LINK_SECRET", "shadow"),
    ("ops", "LOG_LEVEL=DATABASE_URL", "secret-shaped"),
])
def test_s7_secret_mount_names_are_checked(tmp_path, lst, line, why):
    env_dir, sec_dir = _copy_tree(tmp_path)
    with (sec_dir / f"{lst}.list").open("a") as f:
        f.write(line + "\n")
    reason = {"A2A": "no A2A_*", "pin": "rules pin", "shadow": "would shadow",
              "secret-shaped": "secret-shaped"}[why]
    got = [p for p in _problems(env_dir, sec_dir) if p.startswith("R16")]
    assert any(line.split("=")[0] in p and reason in p for p in got), got


def test_s7_rendered_profiles_hold_only_the_sandbox_web_run_app_host(tmp_path):
    out = tmp_path / "r"
    assert check_profile.main(["--render", str(out), "--web-url", WEB_URL, "--gateway-host", "sandbox.mcp.pivota.cc",
                               "--agenpaz-host", "sandbox.agenpaz.com", "--sandbox-project", "pivota-sandbox"]) == 0
    hosts = set()
    for svc in ("web", "worker", "gateway", "agenpaz"):
        for k, v in check_profile.parse_profile(out / f"{svc}.yaml", []).items():
            hosts |= {h for h in check_profile._hosts_in(k, v) if h.endswith(".run.app")}
    assert hosts == {"web-123456789012.us-west1.run.app"}


# ------------------------------------------------------------------- S8: verify_sandbox checks what it claims
import verify_checks as vc  # noqa: E402

P, N = "pivota-sandbox", "123456789012"
SA = lambda n: f"serviceAccount:{n}@{P}.iam.gserviceaccount.com"  # noqa: E731


def _pol(*pairs):
    return {"bindings": [{"role": r, "members": [m]} for r, m in pairs]}


@pytest.mark.parametrize("member,hit", [
    (SA("sa-backend"), True), (f"serviceAccount:service-{N}@serverless-robot-prod.iam.gserviceaccount.com", True),
    (f"serviceAccount:{N}-compute@developer.gserviceaccount.com", True), (f"serviceAccount:{N}@cloudbuild.gserviceaccount.com", True),
    (f"deleted:serviceAccount:sa-gateway@{P}.iam.gserviceaccount.com?uid=1", True),
    ("serviceAccount:sa-backend@pivota-prod.iam.gserviceaccount.com", False), ("user:ops@example.com", False),
])
def test_s8_foreign_policy_finds_every_kind_of_sandbox_principal(member, hit):
    assert bool(vc.foreign_policy(_pol(("roles/viewer", member)), P, N)) is hit


def test_s8_asset_search_resource_level_and_the_registry_allowance():
    repo = "//artifactregistry.googleapis.com/projects/pivota-shared/locations/us-west1/repositories/pivota"
    reader = [{"resource": repo, "policy": _pol(("roles/artifactregistry.reader", SA("sa-web")))}]
    writer = [{"resource": repo, "policy": _pol(("roles/artifactregistry.writer", SA("sa-web")))}]
    bucket = [{"resource": "//storage.googleapis.com/prod-bucket", "policy": _pol(("roles/storage.objectViewer", SA("sa-worker")))}]
    other_repo = [{"resource": repo.replace("/pivota", "/other"), "policy": _pol(("roles/artifactregistry.reader", SA("sa-web")))}]
    assert vc.asset_search(reader, P, N) and not vc.asset_search(reader, P, N, allow_registry_read=True)
    assert vc.asset_search(writer, P, N, allow_registry_read=True)
    assert vc.asset_search(other_repo, P, N, allow_registry_read=True)
    assert vc.asset_search(bucket, P, N, allow_registry_read=True)


@pytest.mark.parametrize("role,member,bad", [
    ("roles/editor", f"serviceAccount:{N}-compute@developer.gserviceaccount.com", True),   # the default Compute SA
    ("roles/editor", f"serviceAccount:{N}@cloudservices.gserviceaccount.com", False),      # Google APIs agent, by design
    ("roles/owner", SA("sa-backend"), True),
    ("roles/run.invoker", "allUsers", True),
    ("roles/secretmanager.secretAccessor", SA("sa-backend"), True),                      # project-wide secret access
    ("roles/logging.logWriter", "serviceAccount:sa-x@pivota-prod.iam.gserviceaccount.com", True),
    ("roles/run.serviceAgent", "serviceAccount:service-999999999999@serverless-robot-prod.iam.gserviceaccount.com", True),
    ("roles/run.serviceAgent", f"serviceAccount:service-{N}@serverless-robot-prod.iam.gserviceaccount.com", False),
    ("roles/logging.logWriter", SA("sa-worker"), False),
    ("roles/owner", "user:owner@example.com", False),
])
def test_s8_sandbox_policy(role, member, bad):
    assert bool(vc.sandbox_policy(_pol((role, member)), P, N, allowed_owner=["user:owner@example.com"])) is bad


def test_s8_secret_policy_is_exact():
    exp = [SA("sa-backend"), SA("sa-worker")]
    ok = _pol(("roles/secretmanager.secretAccessor", exp[0]), ("roles/secretmanager.secretAccessor", exp[1]))
    assert vc.secret_policy(ok, expect=exp) == []
    assert vc.secret_policy(_pol(("roles/secretmanager.secretAccessor", exp[0])), expect=exp)            # missing
    assert vc.secret_policy({"bindings": ok["bindings"] + _pol(("roles/secretmanager.secretAccessor", SA("sa-gateway")))["bindings"]}, expect=exp)
    assert vc.secret_policy({"bindings": ok["bindings"] + _pol(("roles/secretmanager.admin", SA("sa-backend")))["bindings"]}, expect=exp)


def _spec(sa=None, image="us-west1-docker.pkg.dev/pivota-shared/pivota/backend@sha256:" + "a" * 64, env=None, ann=None, vols=None):
    base_env = [{"name": "PIVOTA_ENV", "value": "sandbox"}, {"name": "GOOGLE_CLOUD_PROJECT", "value": P},
                {"name": "REAP_API_BASE_URL", "value": "https://sandbox.api.reap.global"},
                {"name": "REAP_AGENTIC_SIMULATE_CHECKOUT", "value": "COMPLETED"},
                {"name": "REAP_API_KEY", "valueFrom": {"secretKeyRef": {"name": "REAP_API_KEY", "key": "latest"}}}]
    return {"spec": {"template": {"metadata": {"annotations": ann or {}},
                                  "spec": {"serviceAccountName": sa or SA("sa-backend").split(":", 1)[1],
                                           "containers": [{"image": image, "env": base_env + (env or [])}],
                                           "volumes": vols or []}}}}


def _run(spec, expect=("REAP_API_KEY",)):
    return vc.run_service(spec, P, N, "web", SA("sa-backend").split(":", 1)[1], list(expect))


def test_s8_run_service_secret_references_and_identity():
    assert _run(_spec()) == []
    assert _run(_spec(sa=f"sa-gateway@{P}.iam.gserviceaccount.com"))                       # wrong identity
    assert _run(_spec(image="us-west1-docker.pkg.dev/pivota-shared/pivota/backend:latest"))  # not digest-pinned
    alias = {"run.googleapis.com/secrets": "prodkey:projects/388293626878/secrets/DATABASE_URL"}
    aliased = _spec(ann=alias, env=[{"name": "DATABASE_URL", "valueFrom": {"secretKeyRef": {"name": "prodkey", "key": "latest"}}}])
    got = _run(aliased, expect=("REAP_API_KEY", "DATABASE_URL"))
    assert any("another project" in p or "not this project" in p for p in got), got     # cross-project ALIAS
    assert _run(_spec(env=[{"name": "X", "valueFrom": {"secretKeyRef": {"name": "STRAY_SECRET", "key": "1"}}}]))
    assert _run(_spec(vols=[{"name": "v", "secret": {"secretName": "projects/388293626878/secrets/K"}}]))
    own = {"run.googleapis.com/secrets": f"k:projects/{N}/secrets/REAP_API_KEY"}
    assert _run(_spec(ann=own, env=[])) == []


@pytest.mark.parametrize("svc,member,bad", [
    ("worker", "allUsers", True), ("web", "allUsers", False), ("gateway", "allUsers", False),
    ("worker", SA("sa-worker"), False), ("web", "serviceAccount:x@pivota-prod.iam.gserviceaccount.com", True),
])
def test_s8_run_invoker(svc, member, bad):
    assert bool(vc.run_invoker(_pol(("roles/run.invoker", member)), P, N, svc)) is bad


def test_s8_cli_fails_closed_on_unreadable_input():
    res = subprocess.run([sys.executable, str(SANDBOX / "verify_checks.py"), "sandbox-policy", "--project", P,
                          "--number", N, "--label", "x"], input="", capture_output=True, text=True)
    assert res.stdout.startswith("FAIL x: could not read")


def _verify_gcloud(tmp_path, scenario):
    fx = {"sandbox_policy": _pol(("roles/logging.logWriter", SA("sa-backend"))),
          "prod_policy": _pol(("roles/viewer", "user:ops@example.com"))}
    if scenario == "default-sa-editor":
        fx["sandbox_policy"]["bindings"].append({"role": "roles/editor", "members": [f"serviceAccount:{N}-compute@developer.gserviceaccount.com"]})
    fake = tmp_path / "gcloud"
    fake.write_text(f"""#!{sys.executable}
import json, sys
a = sys.argv[1:]; scenario = {scenario!r}; fx = {json.dumps(fx)!r}
fx = json.loads(fx)
open({str(tmp_path / "calls.jsonl")!r}, "a").write(json.dumps(a) + "\\n")
def has(*w): return all(x in a for x in w)
if has("projects", "describe"): print({N!r}); sys.exit(0)
if has("asset", "search-all-iam-policies"):
    if scenario == "asset-fails": sys.exit(1)
    if scenario == "first-fails" and any(q == "--query=policy:" + {P!r} for q in a): sys.exit(1)
    print("[]"); sys.exit(0)
if has("run", "services", "list"): print("agenpaz\\ngateway\\nweb\\nworker"); sys.exit(0)
if has("run", "services", "describe"): print("[]"); sys.exit(0)        # a shape verify_checks cannot read
if has("run", "services", "get-iam-policy"): print("{{}}"); sys.exit(0)
if has("projects", "get-iam-policy"):
    print(json.dumps(fx["sandbox_policy"] if {P!r} in a else fx["prod_policy"])); sys.exit(0)
if has("artifacts", "repositories", "get-iam-policy"):
    print(json.dumps({{"bindings": [{{"role": "roles/artifactregistry.reader", "members": ["serviceAccount:sa-web@{P}.iam.gserviceaccount.com"]}}]}})); sys.exit(0)
sys.exit(1)
""")
    fake.chmod(0o755)
    return fake


@pytest.mark.parametrize("scenario,passes,needle", [
    ("clean", True, "no resource-level policy"),
    ("asset-fails", False, "NOT verified"),
    ("first-fails", False, "NOT verified"),          # only the first of the two searches fails
    ("default-sa-editor", False, "default Compute SA"),
])
def test_s8_verify_iam_group_end_to_end(tmp_path, scenario, passes, needle):
    fake = _verify_gcloud(tmp_path, scenario)
    res = subprocess.run(["bash", str(SANDBOX / "verify_sandbox.sh"), "iam"],
                         env={**os.environ, "GCLOUD": str(fake), "SANDBOX_ALLOWED_OWNERS": "user:ops@example.com"},
                         capture_output=True, text=True, timeout=120)
    out = res.stdout + res.stderr
    assert (res.returncode == 0) is passes, out
    assert needle in out, out


def test_s8_bootstrap_iam_plans_removing_the_default_compute_sa_editor(tmp_path):
    fake = tmp_path / "gcloud"
    fake.write_text(f"""#!{sys.executable}
import json, sys
a = sys.argv[1:]
open({str(tmp_path / 'calls.jsonl')!r}, "a").write(json.dumps(a) + "\\n")
if "projects" in a and "describe" in a: print({N!r}); sys.exit(0)
if "projects" in a and "get-iam-policy" in a:
    print(json.dumps({{"bindings": [{{"role": "roles/editor", "members": ["serviceAccount:{N}-compute@developer.gserviceaccount.com"]}}]}})); sys.exit(0)
sys.exit(1)
""")
    fake.chmod(0o755)
    env = {**os.environ, "GCLOUD": str(fake)}
    env.pop("APPLY", None)
    res = subprocess.run(["bash", str(SANDBOX / "bootstrap_sandbox.sh"), "iam"], env=env, capture_output=True, text=True, timeout=120)
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert f"remove-iam-policy-binding pivota-sandbox --member=serviceAccount:{N}-compute@developer.gserviceaccount.com --role=roles/editor" in out
    assert _executed_mutations(tmp_path / "calls.jsonl") == []


def test_s8_a_declared_cross_project_alias_fails_even_unmounted():
    ann = {"run.googleapis.com/secrets": "prodkey:projects/388293626878/secrets/DATABASE_URL"}
    got = _run(_spec(ann=ann))  # no env references the alias
    assert any("secret alias prodkey" in p for p in got), got



# ------------------------------------------------------------------- review T1-T6, R16, nits
def test_t1_ctrl_c_stops_the_bootstrap(tmp_path):
    """An INT that only cleans up returns to the script, which carried on to `secrets create`."""
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "gcloud"
    fake.write_text(f"""#!{sys.executable}
import json, os, signal, sys
open({str(log)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
os.kill(os.getppid(), signal.SIGINT)   # the operator presses ^C during the first gcloud call
sys.exit(0)
""")
    fake.chmod(0o755)
    env = {**os.environ, "GCLOUD": str(fake), "APPLY": "1"}
    import signal
    # a shell started with SIGINT ignored cannot trap it; reset it, as a terminal would deliver it
    res = subprocess.run(["bash", str(SANDBOX / "bootstrap_sandbox.sh"), "secrets"], env=env,
                         capture_output=True, text=True, timeout=60,
                         preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    assert res.returncode == 130, res.stdout + res.stderr
    assert len(log.read_text().splitlines()) == 1, "kept running gcloud after ^C"


def test_t4_the_dump_base_is_digest_pinned_and_the_build_wants_a_full_commit():
    df = (SANDBOX / "Dockerfile.schema-dump").read_text()
    assert re.search(r"^FROM postgres:17-bookworm@sha256:[0-9a-f]{64}$", df, re.M)
    cb = (SANDBOX / "cloudbuild.schema-dump.yaml").read_text()
    script = re.search(r"args: \['-c', '(.*?)'\]", cb).group(1)
    for sha, ok in (("a" * 40, True), ("A" * 40, False), ("abc123", False), ("latest", False), ("a" * 41, False), ("", False)):
        # Cloud Build substitutes ${COMMIT_SHA} and turns $$ into $
        body = script.replace("${COMMIT_SHA}", sha).replace("$$", "$")
        assert (subprocess.run(["bash", "-c", body], capture_output=True).returncode == 0) is ok, sha


@pytest.mark.parametrize("argv", DRY_RUN_ARGVS + [["verify_sandbox.sh"]])
def test_t5_a_drop_bucket_override_is_refused_by_every_script(tmp_path, argv):
    fake = _fake_gcloud(tmp_path)
    env = {**os.environ, "GCLOUD": str(fake), "DROP_BUCKET": "x", "BILLING_ACCOUNT": "x"}
    env.pop("APPLY", None)
    res = subprocess.run(["bash", str(SANDBOX / argv[0]), *argv[1:]], env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 2 and "DROP_BUCKET" in res.stderr, (argv, res.stderr)
    assert not (tmp_path / "calls.jsonl").exists()


@pytest.mark.parametrize("line", ["OPENAI_API_KEY=ADMIN_API_KEY", "GEMINI_API_KEY=JWT_SECRET_KEY",
                                  "ANTHROPIC_API_KEY=ADMIN_API_KEY", "GOOGLE_API_KEY=CHECKOUT_UI_KEY",
                                  "PIVOTA_GEMINI_API_KEY=ADMIN_API_KEY", "AURORA_SKIN_GEMINI_API_KEY=ADMIN_API_KEY",
                                  "OPENROUTER_API_KEY=ADMIN_API_KEY", "PIVOTA_OPENROUTER_KEY=JWT_SECRET_KEY",
                                  "FIREWORKS_API_KEY=ADMIN_API_KEY"])
def test_r16_model_provider_key_names_are_refused_as_mounts(tmp_path, line):
    env_dir, sec_dir = _copy_tree(tmp_path)
    with (sec_dir / "gateway.list").open("a") as f:
        f.write(line + "\n")
    got = [p for p in _problems(env_dir, sec_dir) if p.startswith("R16")]
    assert any(line.split("=")[0] in p and "model-provider" in p for p in got), got


@pytest.mark.parametrize("key,value,ok", [
    ("DIAG_SKINMASK_MODEL_FILE", "/models/face.parsing.onnx", True),
    # the review's regression: a _PATH joined onto a base URL may not name a host, whatever its shape
    ("REAP_ISSUE_CARD_PATH", "@api.reap.global/v1/cards", False),
    ("REAP_ISSUE_CARD_PATH", "@198.51.100.4/v1/cards", False),
    ("REAP_ISSUE_CARD_PATH", "/v1/cards", True),
    ("REAP_ISSUE_CARD_PATH", "evil.example/v1/cards", False),               # base + path = another host
    ("REAP_ISSUE_CARD_PATH", ".evil.example/v1/cards", False),              # a leading dot, too
    ("REAP_ISSUE_CARD_PATH", "//evil.example/v1", False),
    ("REAP_ISSUE_CARD_PATH", "/v1:8443/cards", False),
    ("DIAG_SKINMASK_MODEL_PATH", "/models/face.parsing.onnx", False),        # _PATH is NOT in the hatch: still host-scanned
    ("RANKER_MODEL_FILE", "@api.reap.global/x", False),                       # `@` defeats the hatch
    ("RANKER_MODEL_VERSION", "//api.reap.global/x", False),                   # and so does `//`
    ("RANKER_MODEL_VERSION", "1.2.3.4", False),                               # R15 never skips: an IP is an IP
    ("REDIS_HOST_FILE", str(_private_test_ranges()[0].network_address + 2), False),  # the review's X_FILE=<private IP>
    ("RANKER_MODEL_FILE", "198.51.100.7", False),
    ("SHOPIFY_API_VERSION", "mcp.pivota.cc", False),                          # the review's X_VERSION=<host>
    ("X_VERSION", "api.reap.global", False),
    ("RANKER_MODEL_FILE", "evil.example", False),                             # not a file extension: a host
    ("RANKER_MODEL_FILE", "weights.zip", False),                              # .zip is a TLD, so not exempt
    ("RANKER_MODEL_FILE", "https://weights.v2.json/x", False),               # a URL host in a _FILE key
    ("RANKER_MODEL_FILE", "weights.v2.json", True),
    ("RANKER_MODEL_VERSION", "2024-10", True),
    # {name} placeholders in a _PATH (the real default of REAP_REVOKE_CARD_PATH), and nothing else in braces
    ("REAP_REVOKE_CARD_PATH", "/v1/cards/{card_id}/cancel", True),
    ("REAP_REVOKE_CARD_PATH", "/v1/cards/{card_id/cancel", False),
    ("REAP_REVOKE_CARD_PATH", "/v1/cards/{CARD}/cancel", False),
    ("REAP_REVOKE_CARD_PATH", "/v1/{a}{b}@x", False),
    ("REAP_REVOKE_CARD_PATH", "@198.51.100.4/v1/cards/{card_id}/cancel", False),
    ("REAP_ISSUE_CARD_PATH", "/%2F%2Fapi.reap.global/v1", False),
    ("X_ENDPOINT", ".api.reap.global/v1/cards", False),
    ("DIAG_SKINMASK_MODEL_FILE", "https://api.pivota.cc/models/x", False),   # URL hosts are still checked
    ("RANKER_MODEL_FILE", "sk_live_" + "a1b2c3d4e5f6", False),               # credential shapes too
    ("EXPORT_FILE", "gs://pivota-prod-migration/x", False),                  # and prod/staging names
    ("RANKER_MODEL_NAME_ALIAS", "weights.v2.json", False),                   # the hatch is by suffix only
])
def test_escape_hatch_for_file_and_version_values_is_narrow(tmp_path, key, value, ok):
    env_dir, sec_dir = _copy_tree(tmp_path)
    for svc in ("web", "worker"):
        _set(env_dir / f"{svc}.yaml", key, value)
    assert (_problems(env_dir, sec_dir) == []) is ok


def test_t6_the_dump_role_is_made_in_sql_read_only_and_outside_cloudsqlsuperuser():
    rb = (SANDBOX / "RUNBOOK.md").read_text()
    role = rb[rb.index("*10a-role (**PROD**"):rb.index("*10a-prod (**PROD**")]
    for must in ("CREATE ROLE sandbox_schema_dump LOGIN NOINHERIT", "NOT with `gcloud sql users create`",
                 "default_transaction_read_only = on", "GRANT USAGE ON SCHEMA public", "pg_auth_members",
                 "prosecdef", "NOLOGIN PASSWORD NULL", "NEVER reused for the daily catalog export",
                 # review must-fix 4: the write paths the old checks missed
                 "has_any_column_privilege", "'r', 'p', 'v', 'm', 'S', 'f'",
                 "has_sequence_privilege('sandbox_schema_dump', c.oid, 'USAGE, UPDATE')",  # nextval() via USAGE
                 "TRIGGER, REFERENCES, MAINTAIN", "has_schema_privilege('sandbox_schema_dump', oid, 'CREATE')",
                 "has_database_privilege('sandbox_schema_dump', current_database(), 'CREATE')",
                 "REVOKE CREATE ON SCHEMA public FROM PUBLIC", "GRANT SELECT ON ALL TABLES IN SCHEMA public",
                 'read -rs DSN && printf %s "$DSN" | gcloud secrets versions add SANDBOX_SCHEMA_DUMP_DATABASE_URL',
                 # sandbox-backend d66c349c9: one allowed per-role setting, owns nothing at all, writes no LO
                 "setconfig <> ARRAY['default_transaction_read_only=on']",
                 "refobjid = 'sandbox_schema_dump'::regrole AND deptype = 'o'", "lomowner = 'sandbox_schema_dump'::regrole",
                 "has_largeobject_privilege('sandbox_schema_dump', oid, 'UPDATE')", "current_setting('lo_compat_privileges')",
                 "REVOKE EXECUTE ON FUNCTION lo_create(oid)", "accepted as documented (no prod change)"):
        assert must in role, must
    assert "Open, for the controller and the owner" not in role
    assert "REVOKE TEMPORARY ON DATABASE" not in role           # a no-op through PUBLIC; said so instead
    assert "no-op" in role


def test_nit_the_gateway_migrate_job_sets_the_sandbox_project():
    rb = (SANDBOX / "RUNBOOK.md").read_text()
    step = rb[rb.index("**10b. Gateway schema.**"):rb.index("**10c.")]
    assert 'GOOGLE_CLOUD_PROJECT=$SANDBOX_PROJECT' in step


# ------------------------------------------------------------------- 10a-sandbox: the schema-apply role job
ROLE_SH = SANDBOX / "sql" / "schema_apply_role.sh"
APPLY_PW = "Zq" + "9x" * 20
VERIFIER = "SCRAM-SHA-256$4096:c2FsdHNhbHRzYWx0c2FsdA==$c3RvcmVka2V5LXN0b3JlZGtleS1zdG9yZWRrZXktc3Q=:c2VydmVya2V5LXNlcnZlcmtleS1zZXJ2ZXJrZXktc2U="  # 16 / 32 / 32 bytes


def _role_run(tmp_path, mode, apply_url=None, pivota_env="sandbox", exists="f", verifier=VERIFIER):
    log = tmp_path / "psql.jsonl"
    fake = tmp_path / "bin" / "psql"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text(f"""#!{sys.executable}
import json, os, sys
a = sys.argv[1:]
stdin = "" if "-c" in a else sys.stdin.read()
open({str(log)!r}, "a").write(json.dumps({{"argv": a, "stdin": stdin, "env": {{k: v for k, v in os.environ.items() if k.startswith("SBX_")}}}}) + "\\n")
if "-c" in a:
    print({exists!r})
""")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}", "PIVOTA_ENV": pivota_env,
           "DATABASE_URL": "postgresql://pivota:adminpw@192.0.2.10:5432/pivota?sslmode=require",
           "SCHEMA_APPLY_DATABASE_URL": apply_url or f"postgresql://sandbox_schema_apply:{APPLY_PW}@192.0.2.10:5432/pivota?sslmode=require"}
    if verifier is not None:
        env["SCHEMA_APPLY_ROLE_VERIFIER"] = verifier
    res = subprocess.run(["bash", "-c", ROLE_SH.read_text(), "schema_apply_role", mode], env=env,
                         capture_output=True, text=True, timeout=30)
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return res, calls


@pytest.mark.parametrize("salt_len", [16, 17, 18, 32])
def test_role_create_accepts_a_well_formed_verifier(tmp_path, salt_len):
    res, _ = _role_run(tmp_path, "create", verifier=_v(b"s" * salt_len, b"k" * 32, b"k" * 32))
    assert res.returncode == 0 and "DONE create" in res.stdout, res.stderr


def test_role_create_sends_only_the_scram_verifier(tmp_path):
    res, calls = _role_run(tmp_path, "create")
    assert res.returncode == 0 and "DONE create" in res.stdout, res.stderr
    blob = json.dumps(calls)
    assert APPLY_PW not in blob                                    # the plaintext reaches neither argv, SQL nor env
    write = calls[-1]
    assert write["env"]["SBX_APPLY_VERIFIER"] == VERIFIER and VERIFIER not in " ".join(write["argv"])
    sql = write["stdin"]
    assert "\\getenv apply_verifier SBX_APPLY_VERIFIER" in sql and "PASSWORD :'apply_verifier'" in sql
    for must in ("CREATE ROLE sandbox_schema_apply LOGIN NOINHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB",
                 "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 2", "GRANT USAGE, CREATE ON SCHEMA public",
                 "CREATE EXTENSION IF NOT EXISTS pg_trgm", "CREATE EXTENSION IF NOT EXISTS pgcrypto",
                 "CREATE EXTENSION IF NOT EXISTS vector", "pg_auth_members",
                 # sandbox-backend d66c349c9: no TEMP for PUBLIC, TEMP back for the app owner, no per-role setting
                 'REVOKE TEMPORARY ON DATABASE :"db" FROM PUBLIC;', 'GRANT TEMPORARY ON DATABASE :"db" TO :"admin";',
                 "has_database_privilege('sandbox_schema_apply', current_database(), 'TEMP')",
                 "has_database_privilege(current_user, current_database(), 'TEMP')",
                 "pg_db_role_setting WHERE setrole = 'sandbox_schema_apply'::regrole"):
        assert must in sql, must
    assert "ALTER ROLE sandbox_schema_apply SET" not in ROLE_SH.read_text()
    assert sql.index("REVOKE TEMPORARY") < sql.index("GRANT TEMPORARY") < sql.index("DO $$")
    assert "CREATE ON DATABASE" not in sql and "GRANT cloudsqlsuperuser" not in sql
    assert "--single-transaction" in write["argv"] and "ON_ERROR_STOP=1" in write["argv"]


def _v(salt: bytes, stored: bytes, server: bytes, it: int = 4096) -> str:
    import base64
    e = lambda x: base64.b64encode(x).decode()
    return f"SCRAM-SHA-256${it}:{e(salt)}${e(stored)}:{e(server)}"


@pytest.mark.parametrize("verifier", [
    "md5abc", APPLY_PW, "", None,
    "SCRAM-SHA-256$4096:AAAA$AAAA:AAAA",                       # the review's repro: stored as a plaintext hash
    _v(b"s" * 15, b"k" * 32, b"k" * 32),                        # salt one byte short
    _v(b"s" * 16, b"k" * 31, b"k" * 32), _v(b"s" * 16, b"k" * 32, b"k" * 33),
    _v(b"s" * 16, b"k" * 32, b"k" * 32, it=1),
    _v(b"s" * 16, b"k" * 32, b"k" * 32) + "\n",
    _v(b"s" * 16, b"k" * 32, b"k" * 32).replace("$4096:", "$4096:==", 1),
])
def test_role_create_refuses_anything_but_a_verifier(tmp_path, verifier):
    res, calls = _role_run(tmp_path, "create", verifier=verifier)
    assert res.returncode == 2 and "VERIFIER" in res.stderr
    assert not any(c["stdin"] for c in calls), "no CREATE statement may run"


def test_role_create_refuses_an_existing_role_and_says_how_to_recover(tmp_path):
    res, calls = _role_run(tmp_path, "create", exists="t")
    assert res.returncode == 2 and "already exists" in res.stderr and "`drop`" in res.stderr
    assert not any(c["stdin"] for c in calls)


def test_role_retire_reassigns_then_switches_login_and_password_off(tmp_path):
    res, calls = _role_run(tmp_path, "retire", exists="t")
    assert res.returncode == 0 and "DONE retire" in res.stdout, res.stderr
    sql = calls[-1]["stdin"]
    order = [sql.index(s) for s in ("GRANT sandbox_schema_apply TO", "REASSIGN OWNED BY sandbox_schema_apply",
                                     "REVOKE sandbox_schema_apply FROM", "ALTER ROLE sandbox_schema_apply NOLOGIN PASSWORD NULL")]
    assert order == sorted(order)


def test_role_drop_recovers_and_is_a_no_op_without_the_role(tmp_path):
    res, calls = _role_run(tmp_path, "drop", exists="t")
    assert res.returncode == 0 and "DONE drop: sandbox_schema_apply removed" in res.stdout
    assert "DROP ROLE sandbox_schema_apply" in calls[-1]["stdin"]
    res2, calls2 = _role_run(tmp_path / "b", "drop", exists="f") if (tmp_path / "b").mkdir() is None else (None, None)
    assert res2.returncode == 0 and "nothing to do" in res2.stdout and not any(c["stdin"] for c in calls2)


@pytest.mark.parametrize("url,env,why", [
    ("postgresql://pivota:pw@192.0.2.10:5432/pivota", "sandbox", "must name sandbox_schema_apply"),
    ("postgresql://sandbox_schema_apply@192.0.2.10:5432/pivota", "sandbox", "no password"),
    (None, "production", "PIVOTA_ENV=sandbox"),
])
def test_role_job_refuses_a_wrong_dsn_or_environment(tmp_path, url, env, why):
    res, calls = _role_run(tmp_path, "create", apply_url=url, pivota_env=env)
    assert res.returncode == 2 and why in res.stderr and calls == []


def test_role_script_survives_the_runbooks_argument_delimiter():
    assert "@@" not in ROLE_SH.read_text()
    rb = (SANDBOX / "RUNBOOK.md").read_text()
    assert '--args="^@@^-c@@$(cat infra/gcp/sandbox/sql/schema_apply_role.sh)@@schema_apply_role@@$1"' in rb
    assert "--set-secrets DATABASE_URL=SCHEMA_APPLY_DATABASE_URL:latest" in rb   # the apply never uses the app DSN
    assert "SCHEMA_APPLY_ROLE_VERIFIER=SCHEMA_APPLY_ROLE_VERIFIER:latest" in rb


def test_bootstrap_scram_verifier_is_rfc7677_and_verifies(tmp_path):
    import base64, hashlib, hmac
    # eval, not `source <(...)`: bash 3.2 cannot source a process substitution
    res = subprocess.run(["bash", "-c", f"eval \"$(sed -n '/^scram_verifier(){{/,/; }}$/p' {SANDBOX / 'bootstrap_sandbox.sh'})\"; scram_verifier \"$PW\""],
                         env={**os.environ, "PW": APPLY_PW}, capture_output=True, text=True)
    v = res.stdout
    m = re.fullmatch(r"SCRAM-SHA-256\$4096:([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)", v)
    assert m, (v, res.stderr)
    salt, stored, server = (base64.b64decode(g) for g in m.groups())
    salted = hashlib.pbkdf2_hmac("sha256", APPLY_PW.encode(), salt, 4096)
    assert hashlib.sha256(hmac.new(salted, b"Client Key", hashlib.sha256).digest()).digest() == stored
    assert hmac.new(salted, b"Server Key", hashlib.sha256).digest() == server
    assert APPLY_PW not in v
    assert len(salt) == 16 and len(stored) == len(server) == 32
    # a fresh random salt each time (review: a fixed-salt mutant survived): same password, new verifier
    res2 = subprocess.run(["bash", "-c", f"eval \"$(sed -n '/^scram_verifier(){{/,/; }}$/p' {SANDBOX / 'bootstrap_sandbox.sh'})\"; scram_verifier \"$PW\""],
                          env={**os.environ, "PW": APPLY_PW}, capture_output=True, text=True)
    salt2 = base64.b64decode(res2.stdout.split(":", 1)[1].split("$", 1)[0])
    assert salt2 != salt and salt != bytes(16)
    # and the role job accepts what the bootstrap mints
    r, _ = _role_run(tmp_path, "create", verifier=v)
    assert r.returncode == 0, r.stderr


# ------------------------------------------------------------------- the ranges live in an untracked file
def _cfg_run(tmp_path, content=None):
    env = {**os.environ}
    if content is None:
        env["SANDBOX_LOCAL_CONFIG"] = str(tmp_path / "absent.sh")
    else:
        (tmp_path / "local.sh").write_text(content)
        env["SANDBOX_LOCAL_CONFIG"] = str(tmp_path / "local.sh")
    return subprocess.run(["bash", "-c", f". {SANDBOX / 'config.sh'} && echo ok"], env=env, capture_output=True, text=True)


def test_ranges_missing_local_config_refuses(tmp_path):
    res = _cfg_run(tmp_path)
    assert res.returncode == 2 and "is missing" in res.stderr


def _bad_configs():
    subnet, psa = _private_test_ranges()
    wide = subnet.supernet(new_prefix=22)          # does NOT overlap the PSA /20: only the prefix rule refuses it
    narrow = next(subnet.subnets(new_prefix=29))
    assert not wide.overlaps(psa)
    good_psa = f"PSA_RANGE_START={psa.network_address}\n"
    return {
        "public": "SUBNET_RANGE=8.8.8.0/24\nPSA_RANGE_START=8.8.16.0\n",           # valid, aligned, non-overlapping
        "public-subnet": f"SUBNET_RANGE=8.8.8.0/24\n{good_psa}",
        "public-psa": f"SUBNET_RANGE={subnet}\nPSA_RANGE_START=8.8.16.0\n",
        "documentation": f"SUBNET_RANGE=192.0.2.0/24\n{good_psa}",            # Python calls it private; not RFC 1918
        "cgnat": f"SUBNET_RANGE=100.64.0.0/24\n{good_psa}",
        "too-wide": f"SUBNET_RANGE={wide}\n{good_psa}",
        "too-narrow": f"SUBNET_RANGE={narrow}\n{good_psa}",
        "psa-unaligned": f"SUBNET_RANGE={subnet}\nPSA_RANGE_START={psa.network_address + 256}\n",
        "overlap": f"SUBNET_RANGE={subnet}\nPSA_RANGE_START={subnet.network_address}\n",
    }


@pytest.mark.parametrize("case", ["public", "public-subnet", "public-psa", "documentation", "cgnat", "too-wide",
                                  "too-narrow", "psa-unaligned", "overlap"])
def test_ranges_bad_local_config_refuses(tmp_path, case):
    content = _bad_configs()[case]
    res = _cfg_run(tmp_path, content)
    assert res.returncode == 2 and "must set SUBNET_RANGE" in res.stderr, res.stderr


def test_ranges_good_local_config_passes(tmp_path):
    subnet, psa = _private_test_ranges()
    content = f"# a comment\n\nSUBNET_RANGE={subnet}\nPSA_RANGE_START={psa.network_address}\n"
    res = _cfg_run(tmp_path, content)
    assert res.returncode == 0 and res.stdout.strip() == "ok", res.stderr


def _good_lines():
    subnet, psa = _private_test_ranges()
    return f"SUBNET_RANGE={subnet}\nPSA_RANGE_START={psa.network_address}\n"


@pytest.mark.parametrize("extra", [
    "SANDBOX_PROJECT=pivota-prod",                       # the review's repro
    "GATEWAY_HOST=api.pivota.cc",
    "git(){ :; }",
    "export APPLY=1",
    "SUBNET_RANGE=$(touch /tmp/sbx-pwned)",
    'SUBNET_RANGE="{s}"',
    "SUBNET_RANGE={s} ",
    "SUBNET_RANGE={s}; SANDBOX_PROJECT=pivota-prod",
    "SUBNET_RANGE={s}",                                  # set twice
    " # indented comment is not a comment line",
    "PSA_RANGE_START={p}\r",
])
def test_the_local_config_is_parsed_never_run(tmp_path, extra):
    """Review blocker: config.local.sh was SOURCED after every guard. It is data: two KEY=value lines."""
    subnet, psa = _private_test_ranges()
    line = extra.replace("{s}", str(subnet)).replace("{p}", str(psa.network_address))
    res = _cfg_run(tmp_path, _good_lines() + line + "\n")      # the ONE bad line is the last line
    assert res.returncode == 2 and "refusing:" in res.stderr and "line 3:" in res.stderr, res.stdout + res.stderr
    assert not Path("/tmp/sbx-pwned").exists()


def test_the_local_config_never_runs_a_command(tmp_path):
    marker = tmp_path / "ran"
    res = _cfg_run(tmp_path, _good_lines() + f"touch {marker}\n")
    assert res.returncode == 2 and "line 3:" in res.stderr
    assert not marker.exists()


def test_a_second_subnet_line_is_refused_even_when_it_differs(tmp_path):
    subnet, psa = _private_test_ranges()
    other = list(subnet.supernet(new_prefix=16).subnets(new_prefix=24))[5]
    res = _cfg_run(tmp_path, _good_lines() + f"SUBNET_RANGE={other}\n")
    assert res.returncode == 2 and "SUBNET_RANGE is set twice" in res.stderr
    res = _cfg_run(tmp_path, _good_lines() + f"PSA_RANGE_START={psa.network_address}\n")
    assert res.returncode == 2 and "PSA_RANGE_START is set twice" in res.stderr


@pytest.mark.parametrize("bash", ["bash", "/bin/bash"])
def test_the_reviews_local_config_repro_plans_nothing_against_prod(tmp_path, bash):
    """The repro: a valid ranges file that also sets SANDBOX_PROJECT=pivota-prod, then APPLY=1
    bootstrap_sandbox.sh iam. Refused before any gcloud call."""
    cfg = tmp_path / "evil.local.sh"
    cfg.write_text(_good_lines() + "SANDBOX_PROJECT=pivota-prod; GATEWAY_HOST=api.pivota.cc\n")
    fake = _fake_gcloud(tmp_path)
    env = {**os.environ, "GCLOUD": str(fake), "APPLY": "1", "HOME": str(tmp_path), "SANDBOX_LOCAL_CONFIG": str(cfg),
           "BILLING_ACCOUNT": "000000-000000-000000"}
    res = subprocess.run([bash, str(SANDBOX / "bootstrap_sandbox.sh"), "iam"], env=env, capture_output=True, text=True,
                         timeout=60)
    assert res.returncode == 2 and "is parsed, never run" in res.stderr
    assert not (tmp_path / "calls.jsonl").exists()


def test_ranges_are_not_in_the_repo():
    cfg = (SANDBOX / "config.sh").read_text() + (SANDBOX / "config.local.example.sh").read_text()
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", cfg)
    assert "infra/gcp/sandbox/config.local.sh" in (ROOT / ".gitignore").read_text()
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "infra/gcp/sandbox/config.local.sh"],
                             capture_output=True, text=True).stdout
    assert tracked == ""


def test_no_private_ip_literal_in_any_sandbox_file():
    import ipaddress
    files = [p for p in SANDBOX.rglob("*") if p.is_file() and p.name != "config.local.sh"] + [Path(__file__)]
    rfc1918 = [ipaddress.ip_network((a << 24 | b << 16 | c << 8, n)) for a, b, c, n in ((10, 0, 0, 8), (172, 16, 0, 12), (192, 168, 0, 16))]
    hits = []
    for p in files:
        for m in re.finditer(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])", p.read_text(errors="ignore")):
            try:
                addr = ipaddress.ip_address(m.group(1))
            except ValueError:
                continue
            # RFC 1918 only: the documentation ranges (192.0.2/24, 198.51.100/24, 203.0.113/24) are fine
            if any(addr in net for net in rfc1918):
                hits.append(f"{p.name}: {m.group(1)}")
    assert hits == [], hits



# ------------------------------------------------------------------- must-fix 1 + 6: the dump image's build guards
def _format_guard():
    df = (SANDBOX / "Dockerfile.schema-dump").read_text()
    m = re.search(r"^RUN (f=/usr/local/bin/dump_prod_schema\.sh .*?)\n(?!\s)", df, re.S | re.M)
    return re.sub(r"\\\n\s*", " ", m.group(1))   # Docker joins backslash-continued lines


@pytest.mark.parametrize("script,ok", [
    ('{"format_version": 2, "kind": "schema", "stamp": "$STAMP",\n', True),
    # the review's bypass: a format-1 script with a comment that says 2
    ('# writes "format_version": 2 manifests\n{"format_version": 1, "kind": "schema", "stamp": "$STAMP",\n', False),
    ('{"format_version": 1, "kind": "schema", "stamp": "$STAMP",\n', False),
    ('# "format_version": 2 only in a comment\n', False),                                     # no manifest line
    ('  {"format_version": 2, "kind": "schema"\n', False),                                   # not at column 0
    ('{"format_version": 2, "kind": "schema"\n# old: "format_version": 1\n', False),        # any other value
])
def test_format_guard_needs_the_anchored_manifest_line_and_no_other_value(tmp_path, script, ok):
    f = tmp_path / "dump.sh"
    f.write_text(script)
    guard = _format_guard().replace("/usr/local/bin/dump_prod_schema.sh", str(f))
    assert (subprocess.run(["sh", "-c", guard], capture_output=True).returncode == 0) is ok


def test_format_guard_matches_the_apply_job_and_the_real_script():
    job, script = ROOT / "jobs" / "sandbox_schema_apply.py", ROOT / "scripts" / "sandbox" / "dump_prod_schema.sh"
    if not job.exists() or not script.exists():
        pytest.skip("sandbox-backend's apply job / dump script are not on this branch yet (same-SHA check runs on main)")
    assert re.search(r"^FORMAT_VERSION = 2$", job.read_text(), re.M), "the apply job's FORMAT_VERSION moved; update the guard"
    guard = _format_guard().replace("/usr/local/bin/dump_prod_schema.sh", str(script))
    assert subprocess.run(["sh", "-c", guard], capture_output=True).returncode == 0


def test_t4_one_pinned_base_and_one_commit_tag_only():
    df = (SANDBOX / "Dockerfile.schema-dump").read_text()
    froms = re.findall(r"^FROM .*$", df, re.M)
    assert len(froms) == 1 and re.fullmatch(r"FROM postgres:17-bookworm@sha256:[0-9a-f]{64}", froms[0]), froms
    cb = (SANDBOX / "cloudbuild.schema-dump.yaml").read_text()
    names = re.findall(r"^\s*- name: (\S+)", cb, re.M)
    assert names and all(re.fullmatch(r"[\w./-]+@sha256:[0-9a-f]{64}", n) for n in names), names
    assert cb.count("'-t'") == 1
    images = re.findall(r"^images:\n((?:  - .*\n)+)", cb, re.M)
    assert images and images[0].strip().splitlines() == ["- '${_REGION}-docker.pkg.dev/$PROJECT_ID/${_REPO}/${_IMAGE}:${COMMIT_SHA}'"]



# ------------------------------------------------------------------- T3: the dump step removes its grant on every path
DUMP_IMG = "us-west1-docker.pkg.dev/pivota-shared/pivota/sandbox-schema-dump@sha256:" + "c" * 64


def _dump_run(tmp_path, fail_on=None, apply=True, img=DUMP_IMG):
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "gcloud"
    fake.write_text(f"""#!{sys.executable}
import json, sys
a = sys.argv[1:]
open({str(log)!r}, "a").write(json.dumps(a) + "\\n")
sys.exit(1 if {fail_on!r} and {fail_on!r} in a else 0)
""")
    fake.chmod(0o755)
    env = {**os.environ, "GCLOUD": str(fake)}
    env.pop("APPLY", None)
    if apply:
        env["APPLY"] = "1"
    res = subprocess.run(["bash", str(SANDBOX / "schema_dump_prod.sh"), img], env=env, capture_output=True, text=True, timeout=60)
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return res, calls


def _verbs(calls):
    out = []
    for c in calls:
        if "add-iam-policy-binding" in c: out.append("grant")
        elif "remove-iam-policy-binding" in c: out.append("ungrant")
        elif "deploy" in c: out.append("deploy")
        elif "execute" in c: out.append("execute")
    return out


@pytest.mark.parametrize("fail_on,want,rc_ok", [
    (None, ["grant", "deploy", "execute", "ungrant"], True),
    ("execute", ["grant", "deploy", "execute", "ungrant"], False),
    ("deploy", ["grant", "deploy", "ungrant"], False),
])
def test_t3_the_dump_grant_is_removed_on_every_path(tmp_path, fail_on, want, rc_ok):
    res, calls = _dump_run(tmp_path, fail_on=fail_on)
    assert _verbs(calls) == want, (calls, res.stderr)
    assert (res.returncode == 0) is rc_ok
    ungrant = [c for c in calls if "remove-iam-policy-binding" in c][0]
    assert "SANDBOX_SCHEMA_DUMP_DATABASE_URL" in ungrant and "pivota-prod" in ungrant
    assert "--role=roles/secretmanager.secretAccessor" in ungrant


def test_t3_dry_run_prints_the_same_order_and_runs_nothing(tmp_path):
    res, calls = _dump_run(tmp_path, apply=False)
    out = res.stdout + res.stderr
    assert res.returncode == 0 and calls == []
    order = [out.index(s) for s in ("secrets add-iam-policy-binding", "run jobs deploy sandbox-schema-dump",
                                     "run jobs execute sandbox-schema-dump", "secrets remove-iam-policy-binding")]
    assert order == sorted(order)


@pytest.mark.parametrize("img", ["postgres:17", "us-west1-docker.pkg.dev/pivota-shared/pivota/sandbox-schema-dump:latest", ""])
def test_t3_the_dump_step_wants_an_image_by_digest(tmp_path, img):
    res, calls = _dump_run(tmp_path, img=img)
    assert res.returncode == 2 and calls == []



# ------------------------------------------------------------------- S8 gaps (second review)
def test_s8_iam_group_is_opt_in(tmp_path):
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "gcloud"
    fake.write_text(f"""#!{sys.executable}
import json, sys
open({str(log)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(1)
""")
    fake.chmod(0o755)
    res = subprocess.run(["bash", str(SANDBOX / "verify_sandbox.sh")], env={**os.environ, "GCLOUD": str(fake)},
                         capture_output=True, text=True, timeout=120)
    assert "iam: SKIPPED in a bare run" in res.stdout
    calls = log.read_text() if log.exists() else ""
    assert "pivota-prod" not in calls and "pivota-staging" not in calls and "organizations/" not in calls


def test_s8_a_crashing_check_is_a_failure_not_a_pass(tmp_path):
    fake = _verify_gcloud(tmp_path, "clean")
    res = subprocess.run(["bash", str(SANDBOX / "verify_sandbox.sh"), "run"], env={**os.environ, "GCLOUD": str(fake)},
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 1 and "crashed (nothing verified)" in res.stdout, res.stdout


@pytest.mark.parametrize("member", [f"serviceAccount:{P}.svc.id.goog[ns/ksa]", f"serviceAccount:{P}@appspot.gserviceaccount.com",
                                    f"serviceAccount:p{N}-123456@gcp-sa-logging.iam.gserviceaccount.com"])
def test_s8_more_sandbox_principal_shapes(member):
    assert vc.foreign_policy(_pol(("roles/viewer", member)), P, N)


def test_s8_run_service_refuses_a_sidecar():
    spec = _spec()
    spec["spec"]["template"]["spec"]["containers"].append({"image": "docker.io/library/busybox", "env": []})
    assert any("sidecar" in p for p in _run(spec))


@pytest.mark.parametrize("role,member,owners,bad", [
    ("roles/owner", "user:ops@example.com", ["user:ops@example.com"], False),
    ("roles/owner", "user:stranger@example.com", ["user:ops@example.com"], True),
    ("roles/editor", "group:everyone@example.com", ["user:ops@example.com"], True),
    ("roles/viewer", "user:ops@example.com", [], True),                       # owners not verified: fail closed
    ("roles/secretmanager.admin", "user:ops@example.com", ["user:ops@example.com"], True),
    ("roles/iam.serviceAccountTokenCreator", "user:ops@example.com", ["user:ops@example.com"], True),
    ("roles/iam.serviceAccountUser", SA("sa-backend"), ["user:ops@example.com"], True),
    ("roles/run.invoker", "principalSet://iam.googleapis.com/projects/999999999999/locations/global/workloadIdentityPools/x/*", ["user:ops@example.com"], True),
    ("roles/run.invoker", f"principalSet://iam.googleapis.com/projects/{N}/locations/global/workloadIdentityPools/x/*", ["user:ops@example.com"], False),
])
def test_s8_sandbox_policy_owners_admin_impersonation_and_wif(role, member, owners, bad):
    assert bool(vc.sandbox_policy(_pol((role, member)), P, N, allowed_owner=owners)) is bad


def _res(resource, role, member, project=None):
    return {"resource": resource, "project": project or f"projects/{N}", "policy": _pol((role, member))}


@pytest.mark.parametrize("item,bad", [
    (_res(f"//iam.googleapis.com/projects/{P}/serviceAccounts/sa-x", "roles/iam.serviceAccountTokenCreator", "user:ops@example.com"), True),
    (_res(f"//run.googleapis.com/projects/{P}/locations/us-west1/services/worker", "roles/run.invoker", "allUsers"), True),
    (_res(f"//run.googleapis.com/projects/{P}/locations/us-west1/services/web", "roles/run.invoker", "allUsers"), False),
    (_res(f"//run.googleapis.com/projects/{P}/locations/us-west1/services/web", "roles/run.invoker", "allAuthenticatedUsers"), True),
    (_res(f"//storage.googleapis.com/{P}-catalog-drop", "roles/storage.objectCreator",
          "serviceAccount:sa-sandbox-export@pivota-prod.iam.gserviceaccount.com"), False),
    (_res(f"//storage.googleapis.com/{P}-catalog-drop", "roles/storage.objectViewer",
          "serviceAccount:sa-sandbox-export@pivota-prod.iam.gserviceaccount.com"), True),
    (_res(f"//secretmanager.googleapis.com/projects/{N}/secrets/X", "roles/secretmanager.secretAccessor",
          "serviceAccount:sa-x@pivota-staging.iam.gserviceaccount.com"), True),
    (_res(f"//secretmanager.googleapis.com/projects/{N}/secrets/X", "roles/secretmanager.secretAccessor", SA("sa-backend")), False),
])
def test_s8_sandbox_resource_policies(item, bad):
    got = vc.sandbox_resources([item], P, N, drop_bucket=f"{P}-catalog-drop",
                               export_sa="sa-sandbox-export@pivota-prod.iam.gserviceaccount.com")
    assert bool(got) is bad, got


def test_s8_org_search_skips_the_sandbox_own_project_and_allows_only_shared_registry_reads():
    own = {"resource": f"//secretmanager.googleapis.com/projects/{N}/secrets/X", "project": f"projects/{N}",
           "policy": _pol(("roles/secretmanager.secretAccessor", SA("sa-backend")))}
    shared = {"resource": "//artifactregistry.googleapis.com/projects/pivota-shared/locations/us-west1/repositories/pivota",
              "project": "projects/1", "policy": _pol(("roles/artifactregistry.reader", SA("sa-web")))}
    prod_repo = {"resource": "//artifactregistry.googleapis.com/projects/pivota-prod/locations/us-west1/repositories/pivota",
                 "project": "projects/2", "policy": _pol(("roles/artifactregistry.reader", SA("sa-web")))}
    assert vc.asset_search([own, shared], P, N, allow_registry_read=True) == []
    assert vc.asset_search([prod_repo], P, N, allow_registry_read=True)


def test_the_unfilled_example_is_refused(tmp_path):
    res = _cfg_run(tmp_path, (SANDBOX / "config.local.example.sh").read_text())
    assert res.returncode == 2 and "is parsed, never run" in res.stderr


def test_secret_versions_are_disabled_by_number_never_the_latest_alias():
    """Review: `versions disable` takes a version number; `latest` is an access alias only."""
    rb = (SANDBOX / "RUNBOOK.md").read_text()
    assert not re.search(r"versions disable (latest|\$?\{?latest)", rb)
    retire = rb[rb.index("3. **Retire (own go)"):rb.index("**Rollback (all of 10a-sandbox):**")]
    assert "--filter='state=ENABLED' --format='value(name.basename())'" in retire
    assert 'secrets versions disable "$v" --secret "$s"' in retire
    for s_ in ("SCHEMA_APPLY_DATABASE_URL", "SCHEMA_APPLY_ROLE_VERIFIER"):
        assert s_ in retire


@pytest.mark.parametrize("bash", ["bash", "/bin/bash"])
def test_t3_a_failed_grant_removal_fails_the_run_loudly(tmp_path, bash):
    """Review: the removal failing used to exit 0 with a WARNING. A grant left on a prod secret is a
    failed step even when the dump succeeded."""
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "gcloud"
    fake.write_text(f"""#!{sys.executable}
import json, sys
a = sys.argv[1:]
open({str(log)!r}, "a").write(json.dumps(a) + "\\n")
sys.exit(1 if "remove-iam-policy-binding" in a else 0)
""")
    fake.chmod(0o755)
    env = {**os.environ, "GCLOUD": str(fake), "APPLY": "1"}
    res = subprocess.run([bash, str(SANDBOX / "schema_dump_prod.sh"), DUMP_IMG], env=env, capture_output=True,
                         text=True, timeout=60)
    assert res.returncode == 3, res.stdout + res.stderr
    assert "!!! FAILED: could not remove" in res.stderr and "STILL ON a prod secret" in res.stderr
    assert "gcloud secrets remove-iam-policy-binding SANDBOX_SCHEMA_DUMP_DATABASE_URL --project pivota-prod" in res.stderr


def test_t3_a_failed_run_keeps_its_own_exit_code_when_the_removal_also_fails(tmp_path):
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "gcloud"
    fake.write_text(f"""#!{sys.executable}
import json, sys
a = sys.argv[1:]
open({str(log)!r}, "a").write(json.dumps(a) + "\\n")
sys.exit(1 if ("remove-iam-policy-binding" in a or "execute" in a) else 0)
""")
    fake.chmod(0o755)
    env = {**os.environ, "GCLOUD": str(fake), "APPLY": "1"}
    res = subprocess.run(["bash", str(SANDBOX / "schema_dump_prod.sh"), DUMP_IMG], env=env, capture_output=True,
                         text=True, timeout=60)
    assert res.returncode == 1 and "STILL ON a prod secret" in res.stderr


def test_s8_the_org_wide_search_is_scoped_to_the_org_by_name_and_number(tmp_path):
    """Review: mutating the scope `organizations/$ORG_ID` to one project survived. The search that
    finds sandbox principals in OTHER projects must cover the whole org, by the sandbox's id AND number."""
    fake = _verify_gcloud(tmp_path, "clean")
    res = subprocess.run(["bash", str(SANDBOX / "verify_sandbox.sh"), "iam"],
                         env={**os.environ, "GCLOUD": str(fake), "SANDBOX_ALLOWED_OWNERS": "user:ops@example.com"},
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    calls = [json.loads(l) for l in (tmp_path / "calls.jsonl").read_text().splitlines()]
    searches = [c for c in calls if "search-all-iam-policies" in c]
    scope = lambda c: next(x.split("=", 1)[1] for x in c if x.startswith("--scope="))
    query = lambda c: next((x.split("=", 1)[1] for x in c if x.startswith("--query=")), "")
    org = sorted(query(c) for c in searches if scope(c) == "organizations/867785023602")
    assert org == sorted([f"policy:{P}", f"policy:{N}"]), searches
    assert [scope(c) for c in searches if scope(c) != "organizations/867785023602"] == [f"projects/{P}"]


@pytest.mark.parametrize("member", [
    f"principal://iam.googleapis.com/projects/{N}/locations/global/workloadIdentityPools/p/subject/x",   # review V2
    f"principalSet://iam.googleapis.com/projects/{N}/locations/global/workloadIdentityPools/p/*",
    f"deleted:serviceAccount:service-{N}@gcp-sa-cloudbuild.iam.gserviceaccount.com?uid=123",           # review V1
    f"deleted:serviceAccount:{N}-compute@developer.gserviceaccount.com?uid=123",
    f"deleted:serviceAccount:sa-backend@{P}.iam.gserviceaccount.com?uid=123",
])
def test_s8_sandbox_principals_in_a_prod_policy_include_wif_and_deleted_shapes(member):
    assert vc.foreign_policy(_pol(("roles/viewer", member)), P, N)


@pytest.mark.parametrize("member", [f"deleted:serviceAccount:{N}-compute@developer.gserviceaccount.com?uid=1",
                                    f"deleted:serviceAccount:sa-x@{P}.iam.gserviceaccount.com?uid=1"])
def test_s8_a_deleted_service_account_with_editor_is_flagged(member):
    """Review V5: a deleted SA's binding is restored with the SA if it is undeleted."""
    got = vc.sandbox_policy(_pol(("roles/editor", member)), P, N, allowed_owner=["user:ops@example.com"])
    assert any("editor" in g for g in got), got


@pytest.mark.parametrize("which,name", [("be", "pivota-backend-fork"), ("be", "PIVOTA-Agent"), ("gw", "pivota-backend")])
def test_the_repo_dirs_must_fetch_from_the_right_repositories(tmp_path, which, name):
    """Review: BACKEND_REPO_DIR's origin was not pinned. The URL fetch uses must name the repository."""
    fake = _apply_gcloud(tmp_path, "existing")
    be = _release_repo(tmp_path / "be", tree=SANDBOX, name=name if which == "be" else None)
    gw = _release_repo(tmp_path / "gw", name=name if which == "gw" else None)
    env = {**os.environ, "GCLOUD": str(fake), "APPLY": "1", "HOME": str(tmp_path), "SERVICES": "web",
           "BACKEND_REPO_DIR": str(be), "GATEWAY_REPO_DIR": str(gw)}
    res = subprocess.run(["bash", str(SANDBOX / "deploy_sandbox.sh"), "sandbox-r1"], env=env,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 1 and "which is not the" in res.stderr, res.stdout + res.stderr
    log = tmp_path / "calls.jsonl"
    assert not log.exists() or not _named([json.loads(l) for l in log.read_text().splitlines()], "run", "deploy")


def test_github_origins_must_share_an_owner(tmp_path):
    run = lambda *a: subprocess.run(["bash", "-c", f'eval "$(sed -n \'/^origin_of(){{/,/^}}$/p\' {SANDBOX / "deploy_sandbox.sh"})"; ' + a[0]],
                                    capture_output=True, text=True)
    out = run('github_owner_of https://github.com/Acme/pivota-backend.git; github_owner_of git@github.com:acme/PIVOTA-Agent.git; '
              'github_owner_of ssh://git@github.com/other/x; github_owner_of /srv/x.git; echo end').stdout.split()
    assert out == ["acme", "acme", "other", "end"]
    out = run('repo_name_of https://github.com/a/PIVOTA-Agent.git; repo_name_of git@github.com:a/pivota-backend; '
              'repo_name_of /srv/pivota-backend.git/').stdout.split()
    assert out == ["pivota-agent", "pivota-backend", "pivota-backend"]


@pytest.mark.parametrize("be,gw,ok", [
    ("https://github.com/acme/pivota-backend.git", "git@github.com:Acme/PIVOTA-Agent.git", True),
    ("https://github.com/acme/pivota-backend.git", "https://github.com/fork/PIVOTA-Agent.git", False),
    ("https://github.com/acme/pivota-backend.git", "/srv/PIVOTA-Agent.git", False),
    ("/srv/pivota-backend.git", "/srv/PIVOTA-Agent.git", True),               # neither on github: nothing to compare
])
def test_same_owner_compares_the_two_github_owners(be, gw, ok):
    fns = subprocess.run(["sed", "-n", "/^origin_of(){/,/^}$/p;/^same_owner(){/,/^}$/p", str(SANDBOX / "deploy_sandbox.sh")],
                         capture_output=True, text=True, check=True).stdout
    script = fns + '\norigin_of(){ case "$1" in be) echo "$BE";; gw) echo "$GW";; esac; }\n' \
                   'BACKEND_REPO_DIR=be GATEWAY_REPO_DIR=gw same_owner'
    res = subprocess.run(["bash", "-c", script], env={**os.environ, "BE": be, "GW": gw}, capture_output=True, text=True)
    assert (res.returncode == 0) is ok, res.stderr


@pytest.mark.parametrize("key,tail", [
    ("SUBNET_RANGE", " "), ("SUBNET_RANGE", "; SANDBOX_PROJECT=pivota-prod"), ("SUBNET_RANGE", " #x"),
    ("SUBNET_RANGE", "\r"), ("SUBNET_RANGE", "$(id)"),
    ("PSA_RANGE_START", " "), ("PSA_RANGE_START", "; GATEWAY_HOST=api.pivota.cc"), ("PSA_RANGE_START", "/20"),
])
def test_a_range_line_must_end_at_its_value(tmp_path, key, tail):
    """The ONLY line for its key, so no "set twice" refusal can hide a loose pattern."""
    subnet, psa = _private_test_ranges()
    lines = {"SUBNET_RANGE": f"SUBNET_RANGE={subnet}", "PSA_RANGE_START": f"PSA_RANGE_START={psa.network_address}"}
    lines[key] += tail
    res = _cfg_run(tmp_path, lines["SUBNET_RANGE"] + "\n" + lines["PSA_RANGE_START"] + "\n")
    want = "line 1:" if key == "SUBNET_RANGE" else "line 2:"
    assert res.returncode == 2 and want in res.stderr and "is parsed, never run" in res.stderr, res.stderr


# ------------------------------------------------------------------- S9: the issuing-agent assertion key
ASSERTION = "ISSUING_AGENT_ASSERTION_SECRET"


def test_s9_the_assertion_key_is_mounted_by_exactly_the_signer_and_the_verifier():
    holders = {p.stem for p in (SANDBOX / "secrets").glob("*.list")
               if re.search(rf"^{ASSERTION}={ASSERTION}$", p.read_text(), re.M)}
    assert holders == {"web", "gateway"}
    assert ASSERTION in check_profile.ALLOWED_SECRETS


@pytest.mark.parametrize("drop_from", ["web", "gateway"])
def test_s9_one_side_alone_is_refused(tmp_path, drop_from):
    """With the key on one side only every assertion fails to verify and no partner is credited, silently."""
    env_dir, sec_dir = _copy_tree(tmp_path)
    p = sec_dir / f"{drop_from}.list"
    p.write_text("\n".join(l for l in p.read_text().splitlines() if not l.startswith(ASSERTION + "=")) + "\n")
    got = [x for x in _problems(env_dir, sec_dir) if x.startswith("R8")]
    assert got == [f"R8 {drop_from}.list: must hold exactly {ASSERTION}={ASSERTION} (it works only on ['gateway', 'web'] together)"], got


@pytest.mark.parametrize("lst", ["worker", "agenpaz", "ops", "catalog-import", "schema-apply"])
def test_s9_no_other_service_holds_the_assertion_key(tmp_path, lst):
    env_dir, sec_dir = _copy_tree(tmp_path)
    with (sec_dir / f"{lst}.list").open("a") as f:
        f.write(f"{ASSERTION}={ASSERTION}\n")
    assert any(x.startswith("R8") and "may only be mounted by ['gateway', 'web']" in x for x in _problems(env_dir, sec_dir))


def test_s9_the_bootstrap_mints_it_and_grants_only_the_two_service_accounts(tmp_path):
    fake = _fake_gcloud(tmp_path)
    env = {**os.environ, "GCLOUD": str(fake), "HOME": str(tmp_path)}
    env.pop("APPLY", None)
    minted = subprocess.run(["bash", "-c", f'. {SANDBOX / "config.sh"}; eval "$(sed -n \'/^minted_secret_names(){{/,/^}}$/p\' {SANDBOX / "bootstrap_sandbox.sh"})"; minted_secret_names'],
                            env=env, capture_output=True, text=True)
    names = minted.stdout.split()
    assert ASSERTION in names, minted.stderr
    rb = (SANDBOX / "RUNBOOK.md").read_text()
    assert f"minted internal keys ({len(names)}," in rb and f"mints the {len(names)} internal keys" in rb
    assert f"need {len(names)} internal shared keys" in rb
    res = subprocess.run(["bash", str(SANDBOX / "bootstrap_sandbox.sh"), "iam"], env=env, capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    grants = [l for l in (res.stdout + res.stderr).splitlines() if "add-iam-policy-binding" in l and f" {ASSERTION} " in l + " "]
    who = sorted(re.search(r"serviceAccount:(sa-[a-z-]+)@", l).group(1) for l in grants)
    assert who == ["sa-backend", "sa-gateway"], grants


# ------------------------------------------------------------------- R16 = the gateway's boot-check names
def _gateway_regex_literal(src: str, const: str) -> str:
    m = re.search(rf"^const {const} = /(.*)/;$", src, re.M)
    assert m, const
    return m.group(1)


@pytest.mark.skipif(not os.environ.get("GATEWAY_REPO_DIR"), reason="set GATEWAY_REPO_DIR to a PIVOTA-Agent checkout to compare the pin")
def test_r16_the_pinned_gateway_rules_equal_the_gateways_origin_main():
    """The pin must be the gateway's rule VERBATIM: a change on either side fails here."""
    gw = os.environ["GATEWAY_REPO_DIR"]
    src = subprocess.run(["git", "-C", gw, "show", "origin/main:src/config/sandbox.js"], capture_output=True, text=True, check=True).stdout
    assert _gateway_regex_literal(src, "LLM_KEY_SETTING_RE") == check_profile.GATEWAY_LLM_KEY_SETTING_RE_SOURCE
    assert _gateway_regex_literal(src, "VERTEX_PATH_SETTING_RE") == check_profile.GATEWAY_VERTEX_PATH_SETTING_RE_SOURCE


def test_r16_the_provider_list_is_the_pinned_gateway_list_plus_named_extras():
    alternation = re.search(r"\(\^\|_\)\((.*)\)\(_\|\$\)", check_profile.LLM_KEY_NAME_RE.pattern).group(1).split("|")
    gw = re.search(r"\(\?:(GEMINI\|[A-Z_|]+)\)", check_profile.GATEWAY_LLM_KEY_SETTING_RE_SOURCE).group(1).split("|")
    assert alternation == gw + list(check_profile.SANDBOX_EXTRA_LLM_PROVIDERS)
    assert len(gw) == 30 and {"OPENROUTER", "DASHSCOPE", "QWEN", "BEDROCK", "VOYAGE", "JINA", "LLM", "GENAI"} <= set(gw)


def _gateway_refused_names():
    names = []
    for prov in check_profile.GATEWAY_LLM_PROVIDERS:
        for pre in ("", "PIVOTA_", "AURORA_SKIN_"):
            for suf in ("_API_KEY", "_APIKEY", "_KEY", "_KEYS", "_TOKEN", "_SECRET", "_ACCESS_KEY", "_API_KEY_2"):
                names.append(pre + prov + suf)
    return names + ["GOOGLE_API_KEY", "GOOGLE_APIKEYS", "GOOGLE_API_KEY_3"]


def test_r16_every_mount_name_the_gateway_refuses_is_refused_here(tmp_path):
    """Superset, checked by enumeration: no name the gateway's boot check refuses can pass R16."""
    names = _gateway_refused_names()
    assert all(check_profile.GATEWAY_LLM_KEY_SETTING_RE.search(n) for n in names)
    missed = [n for n in names if not check_profile.is_llm_key_name(n)]
    assert missed == []


@pytest.mark.parametrize("line", ["DASHSCOPE_API_KEY=ADMIN_API_KEY", "QWEN_TOKEN=JWT_SECRET_KEY", "BEDROCK_ACCESS_KEY=ADMIN_API_KEY",
                                  "GOOGLE_APIKEYS=ADMIN_API_KEY",                       # found by the enumeration
                                  # the sandbox's own extras, which the gateway does not list
                                  "SERPAPI_API_KEY=ADMIN_API_KEY", "HF_TOKEN=ADMIN_API_KEY", "LOOK_REPLICATOR_KEY=ADMIN_API_KEY",
                                  "GOOGLE_AI_API_KEY=ADMIN_API_KEY",
                                  "LLM_API_KEY=ADMIN_API_KEY", "PIVOTA_VOYAGE_API_KEY=ADMIN_API_KEY", "JINA_KEY=ADMIN_API_KEY"])
def test_r16_the_new_gateway_providers_are_refused_as_mounts(tmp_path, line):
    env_dir, sec_dir = _copy_tree(tmp_path)
    with (sec_dir / "gateway.list").open("a") as f:
        f.write(line + "\n")
    assert any(p.startswith("R16") and line.split("=")[0] in p for p in _problems(env_dir, sec_dir))


@pytest.mark.parametrize("svc,key,value,why", [
    ("gateway", "LLM_API_KEY", "", "LLM key setting"),
    ("agenpaz", "FALLBACK_GEMINI_API_KEY_2", "", "LLM key setting"),
    ("gateway", "ANTHROPIC_VERTEX_REGION", "us-east5", "Vertex path setting"),
    ("agenpaz", "MERCHANT_AUDIT_GEMINI_VERTEX_PROJECT", "@@SANDBOX_PROJECT@@", "Vertex path setting"),
    ("gateway", "PIVOTA_AGENT_CENTER_ANTHROPIC_VERTEX_LOCATION", "us-east5", "Vertex path setting"),
    ("gateway", "VERTEX_PROJECT", "@@SANDBOX_PROJECT@@", "Vertex path setting"),
])
def test_r16_profile_settings_the_gateway_refuses_at_boot_fail_at_check_time(tmp_path, svc, key, value, why):
    env_dir, sec_dir = _copy_tree(tmp_path)
    _set(env_dir / f"{svc}.yaml", key, value)
    assert any(p.startswith(f"R16 {svc}.yaml: {key}") and why in p for p in _problems(env_dir, sec_dir))


def test_r16_gateway_rules_do_not_refuse_the_settings_they_name_as_fine():
    for ok in ("LLM_KILL_SWITCH", "GEMINI_JSON_TIMEOUT", "AURORA_MAX_OUTPUT_TOKENS", "VERTEX_AI_ENABLED"):
        assert not check_profile.GATEWAY_LLM_KEY_SETTING_RE.search(ok), ok
        assert not check_profile.GATEWAY_VERTEX_PATH_SETTING_RE.search(ok), ok


# ------------------------------------------------------------------- S9 review: the pair's env name is pinned
@pytest.mark.parametrize("lst,edit", [
    # the review's two repros
    ("web", lambda t: t.replace(f"{ASSERTION}={ASSERTION}", f"ISSUING_ASSERTION_HMAC_SECRET={ASSERTION}")),
    ("web", lambda t: t.replace(f"{ASSERTION}={ASSERTION}", f"{ASSERTION}=JWT_SECRET_KEY\nX_SECRET={ASSERTION}")),
    ("gateway", lambda t: t.replace(f"{ASSERTION}={ASSERTION}", f"ISSUING_ASSERTION_HMAC_SECRET={ASSERTION}")),
    ("gateway", lambda t: t + f"X_SECRET={ASSERTION}\n"),                  # an extra alias beside the right line
    ("worker", lambda t: t + f"{ASSERTION}=JWT_SECRET_KEY\n"),             # the env name fed by another secret
])
def test_s9_the_assertion_key_lands_in_exactly_one_env_name(tmp_path, lst, edit):
    env_dir, sec_dir = _copy_tree(tmp_path)
    p = sec_dir / f"{lst}.list"
    p.write_text(edit(p.read_text()))
    got = [x for x in _problems(env_dir, sec_dir) if x.startswith("R8")]
    assert got and all(lst in x or "must hold exactly" in x for x in got), got
    assert any(("must hold exactly" in x) or ("may only be mounted as" in x) or ("may only be fed by" in x) for x in got), got


@pytest.mark.parametrize("key,value", [("AGENPAS_ENABLED", "0"), ("AGENPAS_ENABLED", ""), ("AGENPAS_PUBLIC_BASE_URL", ""),
                                       ("AGENPAS_PUBLIC_BASE_URL", "http://sandbox.agenpaz.com")])
def test_s9_agenpaz_keeps_its_host_exclusive(tmp_path, key, value):
    """agenpaz holds no assertion key because its host serves only the buyer pages; both switches pinned."""
    env_dir, sec_dir = _copy_tree(tmp_path)
    _set(env_dir / "agenpaz.yaml", key, value)
    assert any(key in x and x.split()[1] == "agenpaz.yaml:" for x in _problems(env_dir, sec_dir))


@pytest.mark.parametrize("base,ok", [("https://sandbox.agenpaz.com", True), ("https://sandbox.agenpaz.com/", True),
                                     ("https://pay.agenpaz.com", False), ("https://web-123456789012.us-west1.run.app", False),
                                     ("https://sandbox.agenpaz.com/x", False), ("", False),
                                     ("https://sandbox.mcp.pivota.cc", False),     # the GATEWAY's host: wrong host made exclusive
                                     ("https://sandbox.evil.example", False),
                                     ("https://SANDBOX.agenpaz.com", False)])
def test_s9_the_rendered_agenpaz_base_must_be_its_sandbox_origin(tmp_path, base, ok):
    env_dir, sec_dir = _copy_tree(tmp_path)
    out = tmp_path / "out"
    check_profile.render(env_dir, out, {"SANDBOX_PROJECT": "pivota-sandbox", "WEB_URL": WEB_URL,
                                        "GATEWAY_HOST": "sandbox.mcp.pivota.cc", "AGENPAZ_HOST": "sandbox.agenpaz.com",
                                        "AGENPAZ_PUBLIC_URL": "https://sandbox.agenpaz.com",
                                        "GATEWAY_PUBLIC_URL": "https://sandbox.mcp.pivota.cc"})
    _set(out / "agenpaz.yaml", "AGENPAS_PUBLIC_BASE_URL", base)
    got = [x for x in check_profile.check_all(out, sec_dir, rendered=True, sandbox_project="pivota-sandbox",
                                              extra_hosts={"sandbox.mcp.pivota.cc", "sandbox.agenpaz.com",
                                                           "web-123456789012.us-west1.run.app"},
                                              agenpaz_host="sandbox.agenpaz.com")
           if "AGENPAS_PUBLIC_BASE_URL must be" in x]
    assert (got == []) is ok, got


# ------------------------------------------------------------------- the local config: kind, mode, NUL
def test_a_fifo_local_config_is_refused_as_what_it_is(tmp_path):
    fifo = tmp_path / "local.sh"
    os.mkfifo(fifo)
    res = subprocess.run(["bash", "-c", f". {SANDBOX / 'config.sh'} && echo ok"], env={**os.environ, "SANDBOX_LOCAL_CONFIG": str(fifo)},
                         capture_output=True, text=True, timeout=20)
    assert res.returncode == 2 and "is not a regular file" in res.stderr and "missing" not in res.stderr


@pytest.mark.parametrize("mode", [0o664, 0o646, 0o666])
def test_a_group_or_world_writable_local_config_is_refused(tmp_path, mode):
    res_path = tmp_path / "local.sh"
    res_path.write_text(_good_lines())
    res_path.chmod(mode)
    res = subprocess.run(["bash", "-c", f". {SANDBOX / 'config.sh'} && echo ok"], env={**os.environ, "SANDBOX_LOCAL_CONFIG": str(res_path)},
                         capture_output=True, text=True, timeout=20)
    assert res.returncode == 2 and "writable by group or others" in res.stderr


@pytest.mark.parametrize("bash", ["bash", "/bin/bash"])
def test_a_nul_byte_in_the_local_config_is_refused(tmp_path, bash):
    subnet, psa = _private_test_ranges()
    p = tmp_path / "local.sh"
    p.write_bytes(f"SUBNET_RANGE={subnet}\0\nPSA_RANGE_START={psa.network_address}\n".encode())
    p.chmod(0o600)
    res = subprocess.run([bash, "-c", f". {SANDBOX / 'config.sh'} && echo ok"], env={**os.environ, "SANDBOX_LOCAL_CONFIG": str(p)},
                         capture_output=True, text=True, timeout=20)
    assert res.returncode == 2 and "NUL byte" in res.stderr


def test_dump_check_5b_reads_database_wide_settings():
    rb = (SANDBOX / "RUNBOOK.md").read_text()
    role = rb[rb.index("*10a-role (**PROD**"):rb.index("*10a-prod (**PROD**")]
    assert "WHERE setrole = 0 AND setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database());" in role
    assert "(Checks 4 and 5b are human reads" in role


def test_the_origin_check_says_it_is_accident_protection_only():
    rb = (SANDBOX / "RUNBOOK.md").read_text()
    assert "protection against an ACCIDENTALLY wrong remote only" in rb and "It is not an identity check." in rb


def test_s9_a_rendered_check_without_the_agenpaz_host_fails_closed(tmp_path):
    env_dir, sec_dir = _copy_tree(tmp_path)
    out = tmp_path / "out"
    check_profile.render(env_dir, out, {"SANDBOX_PROJECT": "pivota-sandbox", "WEB_URL": WEB_URL, "GATEWAY_HOST": "sandbox.mcp.pivota.cc",
                                        "AGENPAZ_HOST": "sandbox.agenpaz.com", "AGENPAZ_PUBLIC_URL": "https://sandbox.agenpaz.com"})
    got = check_profile.check_all(out, sec_dir, rendered=True, sandbox_project="pivota-sandbox",
                                  extra_hosts={"sandbox.mcp.pivota.cc", "sandbox.agenpaz.com", "web-123456789012.us-west1.run.app"})
    assert any("AGENPAS_PUBLIC_BASE_URL must be" in x for x in got)


@pytest.mark.parametrize("target_mode,ok", [(0o666, False), (0o646, False), (0o600, True), (0o644, True)])
def test_a_symlinked_local_config_is_judged_by_its_target(tmp_path, target_mode, ok):
    """Review: the mode check looked at the link itself, so a link to a 666 file passed."""
    target = tmp_path / "real.sh"
    target.write_text(_good_lines())
    target.chmod(target_mode)
    link = tmp_path / "local.sh"
    link.symlink_to(target)
    res = subprocess.run(["bash", "-c", f". {SANDBOX / 'config.sh'} && echo ok"], env={**os.environ, "SANDBOX_LOCAL_CONFIG": str(link)},
                         capture_output=True, text=True, timeout=20)
    assert (res.returncode == 0) is ok, res.stderr
    if not ok:
        assert "writable by group or others" in res.stderr



# ------------------------------------------------------------------- the catalog export (role design R5)
EXPORT_IMG = "us-west1-docker.pkg.dev/pivota-shared/pivota/backend@sha256:" + "e" * 64


def _export_run(tmp_path, fail_on=(), apply=True, img=EXPORT_IMG, version="7", executions=""):
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "gcloud"
    fake.write_text(f"""#!{sys.executable}
import json, sys
a = sys.argv[1:]
open({str(log)!r}, "a").write(json.dumps(a) + "\\n")
if "executions" in a and "list" in a:
    print({executions!r})
sys.exit(1 if any(f in a for f in {list(fail_on)!r}) else 0)
""")
    fake.chmod(0o755)
    env = {**os.environ, "GCLOUD": str(fake)}
    env.pop("APPLY", None)
    if apply:
        env["APPLY"] = "1"
    res = subprocess.run(["bash", str(SANDBOX / "catalog_export_prod.sh"), img, version], env=env,
                         capture_output=True, text=True, timeout=60)
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return res, calls


def _export_verbs(calls):
    out = []
    for c in calls:
        if "executions" in c and "list" in c: out.append("list")
        elif "executions" in c and "cancel" in c: out.append("cancel")
        elif "add-iam-policy-binding" in c: out.append("grant")
        elif "remove-iam-policy-binding" in c: out.append("ungrant")
        elif "deploy" in c: out.append("deploy")
        elif "execute" in c: out.append("execute")
        elif "delete" in c: out.append("delete-job")
    return out


@pytest.mark.parametrize("fail_on,want,rc", [
    ((), ["grant", "deploy", "execute", "ungrant", "delete-job"], 0),
    (("execute",), ["grant", "deploy", "execute", "list", "ungrant", "delete-job"], 1),
    (("deploy",), ["grant", "deploy", "ungrant", "delete-job"], 1),
    (("add-iam-policy-binding",), ["grant", "ungrant"], 1),
])
def test_export_grant_is_removed_and_the_job_deleted_on_every_path(tmp_path, fail_on, want, rc):
    res, calls = _export_run(tmp_path, fail_on=fail_on)
    assert _export_verbs(calls) == want, (calls, res.stderr)
    assert res.returncode == rc, res.stdout + res.stderr
    for c in calls:
        assert "--project" in c and c[c.index("--project") + 1] == "pivota-prod", c


def test_export_job_is_exactly_the_reviewed_run(tmp_path):
    res, calls = _export_run(tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr
    grant, deploy, execute, ungrant, delete = calls
    member = "--member=serviceAccount:sa-sandbox-export@pivota-prod.iam.gserviceaccount.com"
    for c in (grant, ungrant):
        assert c[:3] in (["secrets", "add-iam-policy-binding", "CATALOG_EXPORT_DATABASE_URL"],
                         ["secrets", "remove-iam-policy-binding", "CATALOG_EXPORT_DATABASE_URL"])
        assert member in c and "--role=roles/secretmanager.secretAccessor" in c
    assert deploy[:4] == ["run", "jobs", "deploy", "sandbox-catalog-export"]
    arg = lambda c, k: c[c.index(k) + 1]
    assert arg(deploy, "--image") == EXPORT_IMG
    assert arg(deploy, "--service-account") == "sa-sandbox-export@pivota-prod.iam.gserviceaccount.com"
    assert arg(deploy, "--set-secrets") == "DATABASE_URL=CATALOG_EXPORT_DATABASE_URL:7"  # the number, never latest
    assert arg(deploy, "--set-env-vars") == "PIVOTA_ENV=production,CATALOG_COPY_GCS_URI=gs://pivota-sandbox-catalog-drop/catalog"
    assert arg(deploy, "--command") == "python" and "--args=-m,jobs.sandbox_catalog_copy,export" in deploy
    assert arg(deploy, "--max-retries") == "0" and arg(deploy, "--task-timeout") == "3600s"
    assert arg(deploy, "--vpc-egress") == "all-traffic"
    assert execute[:4] == ["run", "jobs", "execute", "sandbox-catalog-export"] and "--wait" in execute
    assert delete[:4] == ["run", "jobs", "delete", "sandbox-catalog-export"]


@pytest.mark.parametrize("fail_on,needle", [
    (("remove-iam-policy-binding",), "STILL ON a prod secret"),
    (("delete",), "could not delete job sandbox-catalog-export"),
])
def test_export_a_failed_cleanup_after_a_good_run_exits_3_loudly(tmp_path, fail_on, needle):
    res, calls = _export_run(tmp_path, fail_on=fail_on)
    assert res.returncode == 3, res.stdout + res.stderr
    assert needle in res.stderr
    assert _export_verbs(calls)[-2:] == ["ungrant", "delete-job"]  # both cleanups are tried either way


def test_export_a_failed_run_keeps_its_own_code_when_a_cleanup_also_fails(tmp_path):
    res, _ = _export_run(tmp_path, fail_on=("execute", "remove-iam-policy-binding"))
    assert res.returncode == 1 and "STILL ON a prod secret" in res.stderr


def test_export_dry_run_prints_the_same_order_and_runs_nothing(tmp_path):
    res, calls = _export_run(tmp_path, apply=False)
    out = res.stdout + res.stderr
    assert res.returncode == 0 and calls == []
    order = [out.index(s) for s in ("secrets add-iam-policy-binding", "run jobs deploy sandbox-catalog-export",
                                     "run jobs execute sandbox-catalog-export", "secrets remove-iam-policy-binding",
                                     "run jobs delete sandbox-catalog-export")]
    assert order == sorted(order)


@pytest.mark.parametrize("img,version", [
    ("us-west1-docker.pkg.dev/pivota-shared/pivota/backend:latest", "7"),
    ("us-west1-docker.pkg.dev/pivota-shared/pivota/gateway@sha256:" + "e" * 64, "7"),
    ("", "7"),
    (EXPORT_IMG, "latest"), (EXPORT_IMG, "0"), (EXPORT_IMG, ""), (EXPORT_IMG, "7;x"), (EXPORT_IMG, "1234567"),
])
def test_export_wants_an_image_by_digest_and_a_version_number(tmp_path, img, version):
    res, calls = _export_run(tmp_path, img=img, version=version)
    assert res.returncode == 2 and calls == [], res.stdout + res.stderr



def test_export_a_failed_execute_cancels_this_jobs_executions_before_the_delete(tmp_path):
    res, calls = _export_run(tmp_path, fail_on=("execute",), executions="sandbox-catalog-export-abc12")
    assert res.returncode == 1
    assert _export_verbs(calls) == ["grant", "deploy", "execute", "list", "cancel", "ungrant", "delete-job"]
    cancel = [c for c in calls if "cancel" in c][0]
    assert cancel[:5] == ["run", "jobs", "executions", "cancel", "sandbox-catalog-export-abc12"]
    listing = [c for c in calls if "list" in c][0]
    assert "--job" in listing and listing[listing.index("--job") + 1] == "sandbox-catalog-export"


def test_export_a_good_run_cancels_nothing(tmp_path):
    res, calls = _export_run(tmp_path, executions="sandbox-catalog-export-abc12")
    assert res.returncode == 0 and "list" not in _export_verbs(calls) and "cancel" not in _export_verbs(calls)


def _interrupt_twice(tmp_path, script, args):
    """^C as a terminal sends it: to the whole process group. Once during the run (the execute), and
    again while the cleanup removes the grant. The second must not stop the cleanup."""
    import signal
    import time

    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "gcloud"
    fake.write_text(f"""#!{sys.executable}
import json, sys, time
a = sys.argv[1:]
def note(x):
    open({str(log)!r}, "a").write(json.dumps(x) + "\\n")
if "execute" in a:
    note(["started"] + a); time.sleep(30)
if "remove-iam-policy-binding" in a:
    note(["removing"]); time.sleep(2)
if "executions" in a and "list" in a:
    print("sandbox-catalog-export-abc12")
note(a)
""")
    fake.chmod(0o755)
    env = {**os.environ, "GCLOUD": str(fake), "APPLY": "1"}
    proc = subprocess.Popen(["bash", str(SANDBOX / script), *args], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)

    def wait_for(word):
        for _ in range(200):
            if log.exists() and word in log.read_text():
                return
            time.sleep(0.05)
        raise AssertionError("never saw %s" % word)

    wait_for('"started"')
    os.killpg(proc.pid, signal.SIGTERM)
    wait_for('"removing"')
    os.killpg(proc.pid, signal.SIGTERM)  # the second one, mid-cleanup
    out, err = proc.communicate(timeout=60)
    calls = [json.loads(l) for l in log.read_text().splitlines()]
    return proc.returncode, calls, err


def test_export_a_second_interrupt_during_cleanup_does_not_skip_it(tmp_path):
    rc, calls, err = _interrupt_twice(tmp_path, "catalog_export_prod.sh", [EXPORT_IMG, "7"])
    assert rc == 130, err
    done = [c for c in calls if c and c[0] not in ("started", "removing")]
    assert _export_verbs(done)[-4:] == ["list", "cancel", "ungrant", "delete-job"], done
    assert "STILL ON" not in err


def test_dump_a_second_interrupt_during_cleanup_does_not_skip_it(tmp_path):
    rc, calls, err = _interrupt_twice(tmp_path, "schema_dump_prod.sh", [DUMP_IMG])
    assert rc == 130, err
    assert any("remove-iam-policy-binding" in c for c in calls), calls


def test_the_prod_scripts_say_prod_in_their_banner(tmp_path):
    for script, args in (("catalog_export_prod.sh", [EXPORT_IMG, "7"]), ("schema_dump_prod.sh", [DUMP_IMG])):
        env = {**os.environ, "GCLOUD": str(_fake_gcloud(tmp_path)), "APPLY": "1"}
        res = subprocess.run(["bash", str(SANDBOX / script), *args], env=env, capture_output=True, text=True,
                             timeout=60)
        assert "changes WILL be made in project pivota-prod" in res.stdout, (script, res.stdout)
        assert "in project pivota-sandbox" not in res.stdout
