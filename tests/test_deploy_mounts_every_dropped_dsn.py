"""Every DSN the porter drops must be re-mounted by the script that deploys the reader.

`infra/gcp/port_railway_env.py` strips every datastore DSN out of the ported Railway env via
`DROP_EXACT`. That is deliberate and correct: `--set-secrets` is last-wins and the ported list is
appended after the explicit mounts, so a ported Railway DSN would silently override the Cloud SQL
one and keep the service reading the retired platform after cutover.

The drop only holds if each deploy path re-mounts the DSNs its own service actually reads. That is
an unwritten contract between two files that never import each other, and it was already broken
once: `PCI_KB_DATABASE_URL` is re-mounted by `deploy_gateway.sh` and by `setup_scheduler.sh`, but
`deploy_backend.sh` mounted only `DATABASE_URL` and `REDIS_URL`, so `web` shipped to GCP without
it. Three of four call sites remembered; nothing checked the fourth.

It stayed invisible because neither reader fails loudly. `routes/employee_products.py` turns the
`RuntimeError` into a 503, and `services/attached_seed_runtime_evidence.py` catches it and
continues with `kb_rows = []` — so `sync_shopify_products_for_merchant`, which serves live Shopify
webhooks on `web`, kept writing catalog payloads with attached-seed runtime evidence silently
dropped. A missing DSN that degrades quietly is worse than one that crashes: nothing pages, and the
damage lands in stored catalog rows.

SCOPE. This checks `deploy_backend.sh` against the Python application code, because that script
deploys `web` and `worker` — the two services that run `main.py`. It deliberately does not scan
`scripts/`: those run as Cloud Run Jobs deployed by `setup_scheduler.sh`, which mounts the DSN on
its own. The gateway is Node and is covered by `deploy_gateway.sh`. Widening the scan without
widening the mount source it is compared against would make this test wrong, not stricter.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PORTER = REPO / "infra" / "gcp" / "port_railway_env.py"
DEPLOY_BACKEND = REPO / "infra" / "gcp" / "deploy_backend.sh"

# Directories loaded by main.py in the deployed image. Excludes tests/, scripts/, infra/ and the
# non-source trees for the reason given in SCOPE above.
APP_DIRS = (
    "adapters", "catalog", "cohorts", "config", "contracts", "core", "dashboard", "data", "db",
    "jobs", "middleware", "models", "mvp", "observability", "orchestrator", "psp", "readiness",
    "realtime", "routes", "services", "utils",
)
APP_ROOT_MODULES = ("main.py", "openapi_config.py", "proof_issuer_main.py")

# A DSN is what this contract is about. Restricting to `_URL` names also keeps PORT out: it is in
# DROP_EXACT because Cloud Run supplies it, not because anything must re-mount it.
def _is_dsn(name: str) -> bool:
    return name.endswith("_URL")


def _drop_exact() -> set[str]:
    """Read DROP_EXACT out of the porter by AST.

    Parsed rather than grepped so this cannot be satisfied by a comment, a docstring, or by this
    test file's own prose naming the same variable.
    """
    tree = ast.parse(PORTER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "DROP_EXACT":
                return {
                    elt.value
                    for elt in getattr(node.value, "elts", [])
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                }
    raise AssertionError(f"DROP_EXACT not found as a module-level assignment in {PORTER}")


def _env_reads() -> dict[str, list[str]]:
    """Map env var name -> source locations, for reads the application performs at runtime.

    Covers os.getenv(NAME), os.environ[NAME] and os.environ.get(NAME) with a literal name. A
    computed name is out of reach here and is not what this contract is about.
    """
    reads: dict[str, list[str]] = {}
    targets = [REPO / d for d in APP_DIRS] + [REPO / m for m in APP_ROOT_MODULES]
    files: list[Path] = []
    for t in targets:
        if t.is_dir():
            files.extend(p for p in t.rglob("*.py") if "__pycache__" not in p.parts)
        elif t.is_file():
            files.append(t)

    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Call):
                fn = node.func
                is_getenv = isinstance(fn, ast.Attribute) and fn.attr == "getenv"
                is_environ_get = (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "get"
                    and isinstance(fn.value, ast.Attribute)
                    and fn.value.attr == "environ"
                )
                if (is_getenv or is_environ_get) and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        name = arg.value
            elif isinstance(node, ast.Subscript):
                val = node.value
                if isinstance(val, ast.Attribute) and val.attr == "environ":
                    idx = node.slice
                    if isinstance(idx, ast.Constant) and isinstance(idx.value, str):
                        name = idx.value
            if name:
                loc = f"{path.relative_to(REPO)}:{node.lineno}"
                reads.setdefault(name, []).append(loc)
    return reads


def _mounted_by_deploy_backend() -> set[str]:
    """Env names deploy_backend.sh mounts from Secret Manager via DB_SECRETS.

    Parses the quoted value of each `DB_SECRETS=` assignment, not the file text, so the surrounding
    comments — which necessarily name these same variables — cannot satisfy the assertion.
    """
    text = DEPLOY_BACKEND.read_text(encoding="utf-8")
    mounted: set[str] = set()
    for value in re.findall(r'^\s*(?:\[[^\]]*\]\s*&&\s*)?DB_SECRETS="([^"]*)"', text, re.M):
        for pair in value.split(","):
            env_name = pair.split("=", 1)[0].strip()
            if env_name:
                mounted.add(env_name)
    return mounted


def test_porter_still_drops_the_dsns():
    """If DROP_EXACT stops covering the DSNs, the mount contract below is moot.

    Deleting a name from DROP_EXACT would make the test below pass by letting the ported Railway
    value through — which is the original cutover hazard, not a fix.
    """
    drop = _drop_exact()
    assert {"DATABASE_URL", "PCI_KB_DATABASE_URL"} <= drop, (
        "port_railway_env.py must keep dropping every datastore DSN from the ported env; "
        f"DROP_EXACT is currently {sorted(drop)}"
    )


def test_deploy_backend_mounts_every_dsn_the_app_reads():
    drop = _drop_exact()
    reads = _env_reads()
    mounted = _mounted_by_deploy_backend()

    # Guard against a vacuous pass: an empty parse on either side would make the assertion below
    # unfailable. DATABASE_URL is read by db/database.py and mounted by the script, so it is the
    # fixed point that proves both parsers actually resolved something.
    assert "DATABASE_URL" in reads, f"env-read scan found nothing for DATABASE_URL: {DEPLOY_BACKEND}"
    assert "DATABASE_URL" in mounted, f"DB_SECRETS parse found no mounts in {DEPLOY_BACKEND}"

    required = {name for name in drop if _is_dsn(name) and name in reads}
    assert required, "no dropped DSN is read by the app — the scan is too narrow to be meaningful"

    missing = sorted(required - mounted)
    detail = "\n".join(f"  {n}\n    read at: {', '.join(reads[n][:3])}" for n in missing)
    assert not missing, (
        "port_railway_env.py drops these DSNs from the ported env, the application reads them at "
        "runtime, and deploy_backend.sh never re-mounts them — so web/worker boot without a value "
        f"and the reader degrades silently:\n{detail}\n"
        "Fix by adding the name to DB_SECRETS in deploy_backend.sh (and granting the service "
        "account access to the secret first — Cloud Run resolves secrets at instance start). If "
        "the name is a Railway-only concept such as DATABASE_PUBLIC_URL, delete the read instead; "
        "do not mount it."
    )


@pytest.mark.parametrize("name", ["PCI_KB_DATABASE_URL"])
def test_known_reader_is_mounted(name):
    """Pin the specific regression rather than trusting the sweep to keep finding it.

    The sweep above is only as good as its scan set. This asserts the one name that was actually
    missing, so narrowing APP_DIRS can never quietly retire the coverage.
    """
    assert name in _mounted_by_deploy_backend(), (
        f"{name} is read by services/pci_kb_scope_review.py and must be mounted by "
        "deploy_backend.sh; without it /pci-kb-scope-reviews 503s and the Shopify webhook sync "
        "silently writes catalog payloads with attached-seed runtime evidence dropped"
    )
