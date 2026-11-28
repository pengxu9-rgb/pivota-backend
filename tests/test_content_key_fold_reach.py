"""ADR-032 M11 item 2: every deployed process that can call a content_key writer which folds the barcode in
must run with that writer's intake identity door flag on; every hand-run entrypoint that can is listed in
the runbook with the flags to pass.

With its door flag off, ingest_standard_products (ENABLE_INTAKE_IDENTITY_SYNC) and apply_ingest_plan
(ENABLE_INTAKE_IDENTITY_ENRICHMENT) build content_key from brand + title + barcode instead of resolving it
through the intake primitive, so a listing lands on a key no other door mints and leaves its group.
scripts/backfill_content_key.py folds whatever the flags say, so no committed job may run it.

The reach comes from tests/content_key_fold_reach.py (a static call graph; see its docstring for what it
does not follow). Covered here: every Cloud Run job a committed infra/gcp/*.sh script creates or updates.
NOT covered: web and the worker, whose env their deploy scripts preserve rather than declare (both run all
five door flags per the 2026-11-28 read), and jobs created by hand outside these scripts.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

from tests import content_key_fold_reach as fold

REPO = str(Path(__file__).resolve().parents[1])
INFRA = Path(REPO) / "infra" / "gcp"
RUNBOOK = Path(REPO) / "docs" / "runbooks" / "catalog_jobs_on_gcp.md"
SYNC = ("services.catalog_sync_service", "ingest_standard_products")
APPLY = ("services.catalog_enrichment_agent.apply", "apply_ingest_plan")
BACKFILL = ("scripts.backfill_content_key", "_drive")

# Every scripts.* / jobs.* module that can reach a folding writer today, and which. A new one fails
# test_the_entrypoints_that_reach_a_folding_writer_are_the_known_ones: list it here AND in the runbook
# section, and if a job runs it, give that job the flag.
REACHING: Dict[str, Set[Tuple[str, str]]] = {
    # run inside the worker (services/audit_scheduler.py), which carries the flags
    "jobs.agentic_commerce_reconciliation": {SYNC},
    "jobs.catalog_onboard_job": {APPLY},
    # the retailer-ingest-drain Cloud Run job (infra/gcp/setup_scheduler.sh)
    "jobs.retailer_ingest_drain": {APPLY},
    # hand-run (scripts/ops/run_oneoff_job.sh passes no door flags by default)
    "scripts.audit_to_catalog_candidates": {APPLY},
    "scripts.backfill_content_key": {BACKFILL},
    "scripts.backfill_shopify_storefront_handles": {SYNC},
    "scripts.catalog_backfill_verify": {SYNC},
    "scripts.ingest_stylekorean_brand": {APPLY},
    "scripts.onboard_curated_brands": {APPLY},
    "scripts.ops.amuse_us_six_expansion": {APPLY},
    "scripts.ops.kiokii_us_retailer_offers": {APPLY},
    "scripts.ops.reviewed_retailer_offers": {APPLY},
    "scripts.ops.surratt_retailer_offers": {APPLY},
    "scripts.run_catalog_enrichment": {APPLY},
    "scripts.run_catalog_onboard_queue": {APPLY},
    "scripts.stylekorean_hitl": {APPLY},
    "scripts.validate_fashion_metafield_pipeline": {SYNC},
}
RUN_IN_THE_WORKER = {"jobs.agentic_commerce_reconciliation", "jobs.catalog_onboard_job"}


# ---------------------------------------------------------------------------
# The committed job definitions
# ---------------------------------------------------------------------------


def logical_lines(text: str) -> List[str]:
    """Shell lines with backslash continuations joined."""
    return re.sub(r"\\\n\s*", " ", text).split("\n")


_ASSIGN = re.compile(r"""^\s*(?:local\s+)?([A-Za-z_][A-Za-z0-9_]*)=(["'])(.*?)\2""")
_ARGS = re.compile(r"""--args(?:=|\s+)(["'])(.*?)\1""")


def job_commands(text: str) -> List[Tuple[str, str]]:
    """(python entrypoint module, the logical line) for every `--command python --args ...` line.
    `--args "$VAR"` resolves to VAR's first assignment in the same script."""
    lines = logical_lines(text)
    assigned: Dict[str, str] = {}
    for line in lines:
        m = _ASSIGN.match(line)
        if m and m.group(1) not in assigned:
            assigned[m.group(1)] = m.group(3)
    out = []
    for line in lines:
        if line.lstrip().startswith("#") or not re.search(r"--command\s+python3?\b", line):
            continue
        m = _ARGS.search(line)
        if not m:
            continue
        args = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", lambda v: assigned.get(v.group(1), v.group(0)),
                      m.group(2))
        parts = args.split(",")
        entry = None
        if "-m" in parts and parts.index("-m") + 1 < len(parts):
            entry = parts[parts.index("-m") + 1]
        elif parts and re.fullmatch(r"(scripts|jobs)/[\w/]+\.py", parts[0]):
            entry = parts[0][:-3].replace("/", ".")
        if entry:
            out.append((entry, line))
    return out


def flag_violations(entry: str, line: str, writers) -> List[str]:
    out = []
    for w in sorted(writers):
        flag = fold.WRITERS[w]
        if flag is None:
            out.append(f"{entry}: runs {w[0]}.{w[1]}, which folds the barcode whatever the flags say")
        elif "--set-env-vars" not in line:
            out.append(f"{entry}: reaches {w[0]}.{w[1]} on a line that leaves the job's env unset ({flag})")
        elif not re.search(rf"(?:^|[\s\"',|^]){flag}=1(?:$|[\s\"',|])", line):
            out.append(f"{entry}: reaches {w[0]}.{w[1]} without {flag}=1 in its --set-env-vars")
    return out


def _committed_jobs() -> List[Tuple[str, str, str]]:
    return [(p.name, e, line) for p in sorted(INFRA.glob("*.sh")) for e, line in job_commands(p.read_text())]


# ---------------------------------------------------------------------------
# The repo
# ---------------------------------------------------------------------------


def test_the_entrypoints_that_reach_a_folding_writer_are_the_known_ones():
    found = {e: set(fold.writers_reached(REPO, e)) for e in fold.entrypoints(REPO)}
    found = {e: w for e, w in found.items() if w}
    new = {e: [fold.chain(REPO, e, w) for w in ws] for e, ws in found.items() if REACHING.get(e) != ws}
    gone = sorted(set(REACHING) - set(found))
    assert not new and not gone, (
        f"entrypoints that reach a folding writer changed. New or different (with chains): {new}. "
        f"No longer reaching: {gone}. Update REACHING and the runbook section; give any job that runs "
        f"a new one its door flag.")


def test_every_committed_job_that_reaches_a_folding_writer_sets_its_door_flag():
    jobs = _committed_jobs()
    reaching = [(s, e, line) for s, e, line in jobs if REACHING.get(e)]
    assert {e for _, e, _ in reaching} == {"jobs.retailer_ingest_drain"}, (
        "the committed jobs that reach a folding writer changed; check each sets its flag")
    problems = [f"{s}: {v}" for s, e, line in reaching for v in flag_violations(e, line, REACHING[e])]
    assert problems == []


def test_the_job_parser_sees_the_committed_python_jobs():
    """Not vacuous: the parser finds the python jobs these scripts define, variable args included."""
    entries = {e for _, e, _ in _committed_jobs()}
    for e in ("jobs.retailer_ingest_drain", "jobs.external_seed_destination_sweep", "jobs.external_referral_refresh",
              "jobs.catalog_destination_sweep", "jobs.merchant_purchasability_sweep", "jobs.reap_cart_proof_refresh",
              "scripts.elect_content_canonicals", "scripts.promote_brand_official_canonicals"):
        assert e in entries, e
    for e in entries:
        assert fold._path_of(REPO, e), f"{e} is not a module in the repo"


def test_the_runbook_lists_every_hand_run_entrypoint_and_the_flags():
    text = RUNBOOK.read_text()
    start = text.index("## Jobs and one-offs that write content keys")
    section = text[start: text.find("\n## ", start + 3) if text.find("\n## ", start + 3) > 0 else None]
    for e in REACHING:
        if e in RUN_IN_THE_WORKER:
            continue
        path = e.replace(".", "/") + ".py"
        assert f"`{path}`" in section, path
    for flag in ("ENABLE_INTAKE_IDENTITY_SYNC=1", "ENABLE_INTAKE_IDENTITY_ENRICHMENT=1"):
        assert flag in section, flag


# ---------------------------------------------------------------------------
# The call graph and the job parser, on small inputs
# ---------------------------------------------------------------------------


def _write(root: Path, files: Dict[str, str]) -> str:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    for d in {p.parent for p in root.rglob("*.py")}:
        if d != root and not (d / "__init__.py").exists():
            (d / "__init__.py").write_text("")
    return str(root)


_WRITER_MODULES = {
    "services/catalog_sync_service.py": "async def ingest_standard_products(**kw):\n    pass\n",
    "services/catalog_enrichment_agent/apply.py": "async def apply_ingest_plan(plan, **kw):\n    pass\n",
    "scripts/backfill_content_key.py": "async def _drive(args):\n    pass\n",
}


def test_the_call_graph_follows_each_import_shape(tmp_path):
    repo = _write(tmp_path, {
        **_WRITER_MODULES,
        "services/mid.py": ("from services.catalog_enrichment_agent.apply import apply_ingest_plan\n"
                            "async def go(plan):\n    return await apply_ingest_plan(plan)\n"),
        "scripts/from_import.py": "from services.mid import go\nasync def main():\n    await go({})\n",
        "scripts/module_alias.py": "import services.mid as m\nasync def main():\n    await m.go({})\n",
        "scripts/dotted.py": "import services.mid\nasync def main():\n    await services.mid.go({})\n",
        "scripts/lazy.py": "async def main():\n    from services import mid\n    await mid.go({})\n",
        "scripts/dotted_deep.py": ("import services.catalog_enrichment_agent\n"
                                   "async def main(p):\n"
                                   "    await services.catalog_enrichment_agent.apply.apply_ingest_plan(p)\n"),
        "scripts/method.py": ("from services.catalog_sync_service import ingest_standard_products\n"
                              "class R:\n    async def run(self):\n        await self.step()\n"
                              "    async def step(self):\n        await ingest_standard_products()\n"),
        "scripts/imports_only.py": "from services.catalog_sync_service import ingest_standard_products  # noqa\n",
        "scripts/same_name_elsewhere.py": "def apply_ingest_plan(p):\n    return p\napply_ingest_plan({})\n",
        "jobs/backfill_job.py": "from scripts import backfill_content_key as b\nasync def main(a):\n    await b._drive(a)\n",
    })
    fold.reach.cache_clear()
    got = {e: set(fold.writers_reached(repo, e)) for e in fold.entrypoints(repo)}
    assert got["scripts.from_import"] == {APPLY}
    assert got["scripts.module_alias"] == {APPLY}
    assert got["scripts.dotted"] == {APPLY}
    assert got["scripts.lazy"] == {APPLY}
    assert got["scripts.dotted_deep"] == {APPLY}, "a dotted call past the imported package"
    assert got["scripts.method"] == {SYNC}
    assert got["scripts.imports_only"] == set(), "an import without a call is not a path"
    assert got["scripts.same_name_elsewhere"] == set(), "a local function of the same name is not the writer"
    assert got["jobs.backfill_job"] == {BACKFILL}
    assert fold.chain(repo, "scripts.from_import", APPLY) == [
        "scripts.from_import:main", "services.mid:go", "services.catalog_enrichment_agent.apply:apply_ingest_plan"]
    fold.reach.cache_clear()


_SCRIPT = '''
SWEEP_ARGS="-m,jobs.sweep,--limit,10"
mkjob a "$IMG" "$SA" \\
  --set-env-vars "PIVOTA_ENV=prod,ENABLE_INTAKE_IDENTITY_ENRICHMENT=1" \\
  --command python --args="-m,jobs.drain"
mkjob b "$IMG" "$SA" \\
  --set-env-vars "^|^PIVOTA_ENV=prod|ENABLE_INTAKE_IDENTITY_ENRICHMENT=0" \\
  --command python --args="$SWEEP_ARGS"
gcloud run jobs update c --command python --args "scripts/one.py,--apply" --quiet
# mkjob d --command python --args="-m,jobs.commented"
mkjob e "$IMG" "$SA" --command node --args "scripts/thing.js"
'''


def test_the_job_parser_resolves_variables_and_continuations():
    assert [e for e, _ in job_commands(_SCRIPT)] == ["jobs.drain", "jobs.sweep", "scripts.one"]


def test_a_reaching_job_needs_its_flag_set_to_1_on_its_own_line():
    lines = dict(job_commands(_SCRIPT))
    assert flag_violations("jobs.drain", lines["jobs.drain"], {APPLY}) == []
    assert flag_violations("jobs.sweep", lines["jobs.sweep"], {APPLY}) == [
        "jobs.sweep: reaches services.catalog_enrichment_agent.apply.apply_ingest_plan without "
        "ENABLE_INTAKE_IDENTITY_ENRICHMENT=1 in its --set-env-vars"]
    assert flag_violations("scripts.one", lines["scripts.one"], {SYNC}) == [
        "scripts.one: reaches services.catalog_sync_service.ingest_standard_products on a line that leaves "
        "the job's env unset (ENABLE_INTAKE_IDENTITY_SYNC)"]
    assert flag_violations("jobs.drain", lines["jobs.drain"], {SYNC, APPLY}) == [
        "jobs.drain: reaches services.catalog_sync_service.ingest_standard_products without "
        "ENABLE_INTAKE_IDENTITY_SYNC=1 in its --set-env-vars"]
    assert flag_violations("jobs.drain", lines["jobs.drain"], {BACKFILL})[0].endswith(
        "which folds the barcode whatever the flags say")
