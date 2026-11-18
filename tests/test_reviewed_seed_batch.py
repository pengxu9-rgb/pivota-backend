"""Reviewed seed batch: manifest, plan and job guards, through the real emit -> parse path.

Placeholders only (example hosts, synthetic ids). No database: the mechanics that need Postgres are in
tests/test_reviewed_seed_batch_postgres.py.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from scripts.ops import reviewed_seed_batch as job
from services import reviewed_seed_batch as batch


def gtin14(prefix13: str) -> str:
    return prefix13 + str(-sum(int(d) * w for d, w in zip(prefix13, (3, 1) * 7)) % 10)


def entry_raw(n: int = 1, *, additions: int = 2) -> dict:
    seller = f"merch_obs_{n:016x}"
    return {
        "seed_id": f"eps_{n:024x}", "product_key": f"prod::{seller}::external_seed::ext_{n:024x}", "seller_ref": seller,
        "signature": f"sig_{n:032x}", "group_id": f"pg_{n:032x}", "content_key": f"ck_{n:032x}",
        "listing": {"url": f"https://shop{n}.example/products/tinted-balm", "native_product_id": 1000 + n,
                    "price_minor": 1400, "brand": "Example Brand", "title": "Tinted Balm"},
        "existing": [[f"{n}0000000001", gtin14(f"000{n:03}0000001"), "Shade A", f"EX{n}-A"]],
        "additions": [[f"{n}000000000{i + 2}", gtin14(f"000{n:03}000000{i + 2}"), f"Shade {chr(66 + i)}", f"EX{n}-{chr(66 + i)}"]
                      for i in range(additions)],
    }


def manifest(*entries) -> dict:
    return {"schema_version": 1, "batch_id": "batch-one", "market": "US", "currency": "USD",
            "entries": list(entries) or [entry_raw(1)]}


def data(payload) -> bytes:
    return json.dumps(payload).encode()


# ------------------------------------------------------------------ manifest

def test_parse_manifest_accepts_a_placeholder_batch():
    batch_id, entries = batch.parse_manifest(data(manifest(entry_raw(1), entry_raw(2, additions=3))))
    assert batch_id == "batch-one" and [len(e.additions) for e in entries] == [2, 3]
    first = entries[0]
    assert first.external_product_id == f"ext_{1:024x}"
    assert first.landing("10000000002") == "https://shop1.example/products/tinted-balm?variant=10000000002"


def _mutate(case):
    raw = manifest(entry_raw(1))
    entry = raw["entries"][0]
    if case == "six_entries":
        raw["entries"] = [entry_raw(n) for n in range(1, 7)]
    elif case == "no_entries":
        raw["entries"] = []
    elif case == "market_gb":
        raw["market"] = "GB"
    elif case == "schema_true":
        raw["schema_version"] = True
    elif case == "bad_batch_id":
        raw["batch_id"] = "Batch One"
    elif case == "extra_top_key":
        raw["note"] = "x"
    elif case == "extra_entry_key":
        entry["note"] = "x"
    elif case == "product_key_other_seller":
        entry["product_key"] = entry["product_key"].replace(entry["seller_ref"], "merch_obs_" + "f" * 16)
    elif case == "http_url":
        entry["listing"]["url"] = entry["listing"]["url"].replace("https", "http")
    elif case == "url_query":
        entry["listing"]["url"] += "?variant=1"
    elif case == "price_float":
        entry["listing"]["price_minor"] = 14.0
    elif case == "product_id_bool":
        entry["listing"]["native_product_id"] = True
    elif case == "gtin_13":
        entry["additions"][0][1] = entry["additions"][0][1][1:]
    elif case == "gtin_check_digit":
        g = entry["additions"][0][1]
        entry["additions"][0][1] = g[:-1] + str((int(g[-1]) + 1) % 10)
    elif case == "blank_shade":
        entry["additions"][0][2] = " "
    elif case == "padded_shade":
        entry["additions"][0][2] = " Shade B"
    elif case == "bad_sku":
        entry["additions"][0][3] = "EX B"
    elif case == "addition_repeats_existing_id":
        entry["additions"][0][0] = entry["existing"][0][0]
    elif case == "addition_repeats_existing_gtin":
        entry["additions"][0][1] = entry["existing"][0][1]
    elif case == "addition_repeats_sku":
        entry["additions"][1][3] = entry["additions"][0][3]
    elif case == "no_additions":
        entry["additions"] = []
    elif case == "two_entries_one_seed":
        other = entry_raw(2)
        other["seed_id"] = entry["seed_id"]
        raw["entries"].append(other)
    elif case == "two_entries_one_gtin":
        other = entry_raw(2)
        other["additions"][0][1] = entry["additions"][0][1]
        raw["entries"].append(other)
    return raw


@pytest.mark.parametrize(("case", "message"), [
    ("six_entries", "1..5 entries"), ("no_entries", "1..5 entries"), ("market_gb", "US/USD only"),
    ("schema_true", "schema_version"), ("bad_batch_id", "batch_id"), ("extra_top_key", "must be {schema_version"),
    ("extra_entry_key", "missing or extra fields"), ("product_key_other_seller", "product_key"),
    ("http_url", "https product page"), ("url_query", "https product page"), ("price_float", "price"),
    ("product_id_bool", "native_product_id"), ("gtin_13", "invalid native id, GTIN-14"),
    ("gtin_check_digit", "invalid native id, GTIN-14"), ("blank_shade", "invalid native id, GTIN-14"),
    ("padded_shade", "invalid native id, GTIN-14"), ("bad_sku", "invalid native id, GTIN-14"),
    ("addition_repeats_existing_id", "repeats a native id"), ("addition_repeats_existing_gtin", "repeats a GTIN"),
    ("addition_repeats_sku", "repeats a SKU"), ("no_additions", "1..40"),
    ("two_entries_one_seed", "share a seed_id"), ("two_entries_one_gtin", "share a variant or GTIN"),
])
def test_parse_manifest_refusals(case, message):
    with pytest.raises(batch.Refused, match=message.replace("(", r"\(").replace("{", r"\{")):
        batch.parse_manifest(data(_mutate(case)))


@pytest.mark.parametrize("url", [
    "https://user@shop1.example/products/tinted-balm", "https://shop1.example:8443/products/tinted-balm",
    "https://SHOP1.example/products/tinted-balm", "https://shop1.example/products/",
    "https://shop1.example/products/tinted-balm;x=1", "https://shop1.example/products/tinted-balm\n",
    "https://shop1.example\\@other.example/products/tinted-balm",
], ids=["userinfo", "port", "upper_host", "empty_handle", "params", "newline", "backslash_at"])
def test_parse_manifest_refuses_any_url_the_serving_loader_refuses(url):
    raw = manifest()
    raw["entries"][0]["listing"]["url"] = url
    with pytest.raises(batch.Refused, match="https product page"):
        batch.parse_manifest(data(raw))


def test_parse_manifest_refuses_duplicate_keys_and_oversize():
    good = data(manifest())
    with pytest.raises(batch.Refused, match="duplicate manifest key"):
        batch.parse_manifest(good[:-1] + b', "market": "US"}')
    with pytest.raises(batch.Refused, match="too large"):
        batch.parse_manifest(good + b" " * batch.MANIFEST_LIMIT)


def test_named_referral_manifest_round_trips_through_the_serving_loader(tmp_path, monkeypatch):
    from services import external_seed_review_pilot as pilot

    _, entries = batch.parse_manifest(data(manifest(entry_raw(1), entry_raw(2))))
    payload = batch.named_referral_manifest(entries, public_listings={})
    raw = data(payload)
    path = tmp_path / "named.json"
    path.write_bytes(raw)
    monkeypatch.setenv(pilot.NAMED_REFERRAL_VARIANTS_MANIFEST, str(path))
    monkeypatch.setenv(pilot.NAMED_REFERRAL_VARIANTS_MANIFEST_SHA256, hashlib.sha256(raw).hexdigest())
    additions, listings = pilot.load_named_referral_manifest({})
    for entry in entries:
        assert additions[entry.seed_id] == {n: (g, s) for n, g, _, s in entry.additions}
        assert (listings[entry.seed_id].url, listings[entry.seed_id].seller_ref) == (entry.url, entry.seller_ref)
        assert listings[entry.seed_id].variants == entry.existing


def test_seed_variant_rows_use_the_listing_price_and_shade_link():
    _, (entry,) = batch.parse_manifest(data(manifest()))
    rows = batch.seed_variant_rows(entry)
    assert [r["variant_id"] for r in rows] == [a[0] for a in entry.additions]
    assert all(r["price_amount"] == 14.0 and r["url"] == entry.landing(r["variant_id"]) and len(r["barcode"]) == 13
               for r in rows)


# ------------------------------------------------------------------ digests and the reviewed plan

def _state(entry, *, extra_offer=False):
    skus = [{"sku_key": f"k{n}", "source_variant_id": n, "suppressed_at": None, "suppression_reason": None}
            for n, *_ in entry.existing]
    offers = [{"offer_id": f"o{n}"} for n, *_ in entry.existing] + ([{"offer_id": "other"}] if extra_offer else [])
    return {"seed": {"variants": [{"variant_id": n} for n, *_ in entry.existing]}, "skus": skus, "offers": offers}


def test_verify_original_accepts_its_own_digests_and_refuses_any_change():
    _, (entry,) = batch.parse_manifest(data(manifest()))
    state = _state(entry)
    rollback = batch.rollback_digests(state, entry)
    batch.verify_original(state, entry, rollback)
    with pytest.raises(batch.Refused, match="preimage differs"):
        batch.verify_original(_state(entry, extra_offer=True), entry, rollback)


def _plan(entries, *, finished, manifest_sha="m" * 64, release="r" * 40):
    rows = [{"seed_id": e.seed_id, "existing_variants": len(e.existing), "added_variants": len(e.additions),
             "rows": [{"variant": n, "gtin14": g, "sku": s} for n, g, _, s in e.additions], "rollback": {}}
            for e in entries]
    return batch.plan_document(batch_id="batch-one", manifest_sha256=manifest_sha, release_sha=release,
                               execution="x", finished_at=finished.isoformat(), entries=rows)


NOW = datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)


def test_check_plan_accepts_a_fresh_matching_plan():
    _, entries = batch.parse_manifest(data(manifest(entry_raw(1), entry_raw(2))))
    rows = batch.check_plan(_plan(entries, finished=NOW - timedelta(minutes=5)), manifest_sha256="m" * 64,
                            release_sha="r" * 40, entries=entries, now=NOW)
    assert list(rows) == [e.seed_id for e in entries]


@pytest.mark.parametrize(("change", "message"), [
    ("stale", "freshness"), ("future", "freshness"), ("other_manifest", "different batch manifest"),
    ("other_release", "different release"), ("reordered", "plan entries differ"), ("other_rows", "plan rows differ"),
    ("writes", "not a reviewed"),
])
def test_check_plan_refusals(change, message):
    _, entries = batch.parse_manifest(data(manifest(entry_raw(1), entry_raw(2))))
    plan = _plan(entries, finished=NOW - timedelta(minutes=5))
    manifest_sha, release = "m" * 64, "r" * 40
    if change == "stale":
        plan["finished_at"] = (NOW - batch.PLAN_MAX_AGE - timedelta(seconds=1)).isoformat()
    elif change == "future":
        plan["finished_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif change == "other_manifest":
        manifest_sha = "n" * 64
    elif change == "other_release":
        release = "s" * 40
    elif change == "reordered":
        plan["entries"].reverse()
    elif change == "other_rows":
        plan["entries"][0]["rows"][0]["sku"] = "OTHER"
    elif change == "writes":
        plan["production_writes"] = 1
    with pytest.raises(batch.Refused, match=message):
        batch.check_plan(plan, manifest_sha256=manifest_sha, release_sha=release, entries=entries, now=NOW)


# ------------------------------------------------------------------ job guards

@pytest.fixture
def job_env(monkeypatch, tmp_path):
    commit = "a" * 40
    image = tmp_path / ".image_commit_sha"
    image.write_text(commit + "\n")
    real_path = job.Path

    def fake_path(value, *args):
        return image if value == "/app/.image_commit_sha" else real_path(value, *args)

    monkeypatch.setattr(job, "Path", fake_path)
    for key, value in {"CLOUD_RUN_JOB": "reviewed-seed-batch-one", "CLOUD_RUN_EXECUTION": "reviewed-seed-batch-one-abc",
                       "PIVOTA_ENV": "production", "DB_AUTO_MIGRATE": "false", job.RELEASE_COMMIT_ENV: commit}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv(job.WRITE_FLAG, raising=False)
    return commit


def test_environment_plan_needs_no_write_flag_and_writes_need_it(job_env, monkeypatch):
    assert job.check_environment("plan") == job_env
    with pytest.raises(batch.Refused, match="write flag"):
        job.check_environment("apply")
    monkeypatch.setenv(job.WRITE_FLAG, "1")
    assert job.check_environment("apply") == job_env
    with pytest.raises(batch.Refused, match="write flag"):
        job.check_environment("plan")


@pytest.mark.parametrize(("key", "value"), [
    ("K_SERVICE", "web"), ("CLOUD_RUN_JOB", "oneoff-1"), ("CLOUD_RUN_EXECUTION", "other-abc"),
    ("PIVOTA_ENV", "staging"), ("DB_AUTO_MIGRATE", "true"), (job.RELEASE_COMMIT_ENV, "b" * 40),
])
def test_environment_refuses_a_service_or_another_job_or_release(job_env, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(batch.Refused, match="reviewed production seed batch job"):
        job.check_environment("plan")


def gate_at(observed, **changes):
    return {"verdict": "GATE_OPEN", "observed_at": observed.isoformat(), "source": "scripts/ops/oneoff_gate.py",
            "cpu_peak_5m": 0.05, "db_job_executions_running": 0, **changes}


@pytest.mark.parametrize(("gate", "now", "message"), [
    (None, NOW, "not GATE_OPEN"), (gate_at(NOW, verdict="GATE_CLOSED"), NOW, "not GATE_OPEN"),
    (gate_at(NOW - timedelta(seconds=181)), NOW, "stale"), (gate_at(NOW + timedelta(seconds=1)), NOW, "stale"),
    (gate_at(datetime(2026, 1, 2, 5, 20, tzinfo=timezone.utc)), datetime(2026, 1, 2, 5, 21, tzinfo=timezone.utc),
     "closed one-off window"),
    # A bare GATE_OPEN is not enough: the reviewed core's field rules apply.
    ({"verdict": "GATE_OPEN", "observed_at": NOW.isoformat()}, NOW, "CPU and running-job readings"),
    (gate_at(NOW, cpu_peak_5m=0.2), NOW, "CPU and running-job readings"),
    (gate_at(NOW, cpu_peak_5m=float("nan")), NOW, "CPU and running-job readings"),
    (gate_at(NOW, cpu_peak_5m=0), NOW, "CPU and running-job readings"),
    (gate_at(NOW, db_job_executions_running=1), NOW, "CPU and running-job readings"),
    (gate_at(NOW, db_job_executions_running=False), NOW, "CPU and running-job readings"),
    (gate_at(NOW, source="hand-written"), NOW, "CPU and running-job readings"),
])
def test_gate_refusals(gate, now, message):
    with pytest.raises((batch.Refused, ValueError, TypeError), match=message):
        job.check_gate(json.dumps(gate), now)


def test_gate_accepts_a_fresh_open_read_from_the_gate_script():
    assert job.check_gate(json.dumps(gate_at(NOW)), NOW)["verdict"] == "GATE_OPEN"


def test_plan_refuses_any_write_switch_value(job_env, monkeypatch):
    monkeypatch.setenv(job.WRITE_FLAG, "0")
    with pytest.raises(batch.Refused, match="write flag"):
        job.check_environment("plan")


def test_read_pinned_checks_the_pin(tmp_path, monkeypatch):
    path = tmp_path / "m.json"
    path.write_bytes(b"{}")
    monkeypatch.setenv(job.ENV + "MANIFEST_PATH", str(path))
    monkeypatch.setenv(job.ENV + "MANIFEST_SHA256", hashlib.sha256(b"{}").hexdigest())
    assert job.read_pinned("MANIFEST") == b"{}"
    monkeypatch.setenv(job.ENV + "MANIFEST_SHA256", "0" * 64)
    with pytest.raises(batch.Refused, match="do not match"):
        job.read_pinned("MANIFEST")


def test_approval_must_name_this_mode_manifest_plan_and_release(monkeypatch, tmp_path):
    approval = {"mode": "apply", "manifest_sha256": "m", "plan_sha256": "p", "release_sha": "r", "go_reference": "go",
                "plan_execution": "reviewed-seed-batch-one-plan"}
    raw = json.dumps(approval).encode()
    (tmp_path / "approval.json").write_bytes(raw)
    monkeypatch.setenv(job.ENV + "APPROVAL_PATH", str(tmp_path / "approval.json"))
    monkeypatch.setenv(job.ENV + "APPROVAL_SHA256", hashlib.sha256(raw).hexdigest())
    assert job.check_approval("apply", manifest_sha="m", plan_sha="p", release="r", receipts_sha=None,
                              plan_execution="reviewed-seed-batch-one-plan") == approval
    for wrong in ({"manifest_sha": "x"}, {"plan_sha": "x"}, {"release": "x"}, {"plan_execution": "reviewed-seed-batch-other"}):
        args = {"manifest_sha": "m", "plan_sha": "p", "release": "r", "receipts_sha": None,
                "plan_execution": "reviewed-seed-batch-one-plan", **wrong}
        with pytest.raises(batch.Refused, match="explicit go"):
            job.check_approval("apply", **args)
    with pytest.raises(batch.Refused, match="explicit go"):
        job.check_approval("rollback", manifest_sha="m", plan_sha="p", release="r", receipts_sha="s",
                           plan_execution="reviewed-seed-batch-one-plan")


# ------------------------------------------------------------------ commit-point classification, real emit -> parse

class FakeTransaction:
    def __init__(self, fail_commit=False):
        self.fail_commit = fail_commit

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None and self.fail_commit:
            raise ConnectionError("commit lost")
        return False


class FakeDB:
    def __init__(self, fail_commit_for=()):
        self.fail_commit_for = set(fail_commit_for)
        self.current = None

    def transaction(self, *args, **kwargs):
        return FakeTransaction(fail_commit=self.current in self.fail_commit_for)

    async def connect(self):
        pass

    async def disconnect(self):
        pass


def _run_batch(monkeypatch, capsys, entries, *, mode="apply", behaviour, gate=None):
    """Drive the REAL write_all with fake writes; return the parsed result lines."""
    fake = FakeDB(fail_commit_for={seed for seed, how in behaviour.items() if how == "commit_fails"})
    import db.database as database_module

    monkeypatch.setattr(database_module, "database", fake)
    calls = []

    async def read_back(entry):
        return {"state_sha256": "s" * 64}

    async def confirm(entry):
        return NOW.isoformat()

    async def run_entry(db, entry, plan_row, mode, **kwargs):
        calls.append(entry.seed_id)
        fake.current = entry.seed_id
        how = behaviour.get(entry.seed_id, "ok")
        if how == "refuse_before":
            raise batch.Refused("guard before the transaction")
        async with db.transaction():
            if how == "refuse_inside":
                raise batch.Refused("guard inside the transaction")
        if how == "error_after":
            raise RuntimeError("after commit")
        return {"seed_id": entry.seed_id, "mode": mode, "postimage_sha256": "p", "preimage_sha256": "q"}

    monkeypatch.setattr(job, "read_back", read_back)
    monkeypatch.setattr(batch, "confirm_current_storefront", confirm)
    monkeypatch.setattr(batch, "run_entry", run_entry)
    monkeypatch.setattr(job, "check_gate", gate or (lambda raw, now: {}))
    approval = {"go_reference": "go", "manifest_sha256": "m", "plan_sha256": "p", "release_sha": "r"}
    plan_rows = {e.seed_id: {} for e in entries}
    asyncio.run(job.write_all(mode, entries, plan_rows, approval, "{}", {}, server_pins=None))
    return calls, job.parse_result_lines(capsys.readouterr().out)


@pytest.mark.parametrize(("how", "status"), [
    ("refuse_before", "refused"), ("refuse_inside", "rolled_back"), ("commit_fails", "outcome_unknown"),
    ("error_after", "committed_then_error"),
])
def test_entry_status_is_classified_at_the_commit_point_and_the_batch_stops(monkeypatch, capsys, how, status):
    _, entries = batch.parse_manifest(data(manifest(entry_raw(1), entry_raw(2), entry_raw(3))))
    calls, rows = _run_batch(monkeypatch, capsys, entries, behaviour={entries[1].seed_id: how})
    assert calls == [entries[0].seed_id, entries[1].seed_id]
    assert [(r["seed_id"], r["status"]) for r in rows] == [
        (entries[0].seed_id, "committed"), (entries[1].seed_id, status), (entries[2].seed_id, "not_attempted")]
    committed = rows[0]
    assert committed["receipt_sha256"] == batch.digest(committed["receipt"])
    assert committed["receipt"]["release_sha"] == "r" and committed["pre_read_back"] == {"state_sha256": "s" * 64}
    if status != "refused":
        assert "read_back" in rows[1]


def test_rollback_runs_entries_in_reverse_order(monkeypatch, capsys):
    _, entries = batch.parse_manifest(data(manifest(entry_raw(1), entry_raw(2))))
    calls, rows = _run_batch(monkeypatch, capsys, entries, mode="rollback", behaviour={})
    assert calls == [entries[1].seed_id, entries[0].seed_id]
    assert [r["status"] for r in rows] == ["committed", "committed"]


def test_result_lines_are_text_base64_and_survive_a_log_prefix(capsys):
    job.emit({"kind": "batch", "status": "planned", "n": 12345678901234567})
    line = capsys.readouterr().out
    assert line.startswith(job.PREFIX) and line.endswith("\n")
    parsed = job.parse_result_lines("2026-01-02T07:00:00Z stdout " + line)
    assert parsed[0]["n"] == 12345678901234567 and parsed[0]["scope"] == batch.OPERATION
    with pytest.raises(ValueError):
        job.parse_result_lines(job.PREFIX + "not-base64!\n")
    assert base64.b64decode(line[len(job.PREFIX):].strip())


def test_approval_is_only_read_from_its_pinned_file(monkeypatch, tmp_path):
    monkeypatch.delenv(job.ENV + "APPROVAL_PATH", raising=False)
    monkeypatch.delenv(job.ENV + "APPROVAL_SHA256", raising=False)
    monkeypatch.setenv(job.ENV + "APPROVAL", json.dumps({"mode": "apply"}))
    with pytest.raises(batch.Refused, match="approval requires a path and a SHA-256 pin"):
        job.check_approval("apply", manifest_sha="m", plan_sha="p", release="r", receipts_sha=None)


# ------------------------------------------------------------------ serving listing (public and private alike)

def _listing_for(entry, **changes):
    from services.external_seed_review_pilot import PilotListing

    fields = dict(url=entry.url, seller_ref=entry.seller_ref, price_minor=0, brand="", title="", description="",
                  variants=tuple((n, g, shade, sku) for n, g, shade, sku in entry.existing),
                  native_product_id=entry.native_product_id)
    fields.update(changes)
    return PilotListing(**fields)


@pytest.mark.parametrize("where", ["LISTINGS", "NAMED_REFERRAL_LISTINGS"])
def test_pilot_listing_keeps_the_serving_listings_pins(monkeypatch, where):
    from services import external_seed_review_pilot as pilot

    _, (entry,) = batch.parse_manifest(data(manifest()))
    serving = _listing_for(entry, native_shop_domain="example-shop.myshopify.com", price_minor=1400)
    monkeypatch.setitem(getattr(pilot, where), entry.seed_id, serving)
    listing = batch.pilot_listing(entry, variants=[("1", "g", "s", "k")])
    assert listing.native_shop_domain == "example-shop.myshopify.com" and listing.variants == (("1", "g", "s", "k"),)
    assert listing.price_minor == entry.price_minor


@pytest.mark.parametrize(("change", "message"), [
    ({"url": "https://other.example/products/tinted-balm"}, "differs from the serving listing"),
    ({"seller_ref": "merch_obs_" + "f" * 16}, "differs from the serving listing"),
    ({"native_product_id": 9}, "differs from the serving listing"),
    ({"variants": ()}, "existing variants differ"),
    ({"price_minor": 1500}, "reviewed price"),
])
def test_serving_listing_must_be_this_entrys_listing(monkeypatch, change, message):
    from services import external_seed_review_pilot as pilot

    _, (entry,) = batch.parse_manifest(data(manifest()))
    monkeypatch.setitem(pilot.LISTINGS, entry.seed_id, _listing_for(entry, **change))
    with pytest.raises(batch.Refused, match=message):
        batch.serving_listing(entry)


def test_a_seed_with_no_serving_listing_is_refused():
    _, (entry,) = batch.parse_manifest(data(manifest()))
    with pytest.raises(batch.Refused, match="no reviewed listing"):
        batch.serving_listing(entry)


def test_gtin14_must_start_with_zero():
    raw = manifest()
    raw["entries"][0]["additions"][0][1] = gtin14("1001000000002")
    with pytest.raises(batch.Refused, match="invalid native id, GTIN-14"):
        batch.parse_manifest(data(raw))


@pytest.mark.parametrize(("state", "kind", "status"), [
    ("reported", "batch", "stopped"), ("open", "entry", "outcome_unknown"), ("committed", "entry", "committed_then_error"),
    ("not_started", "entry", "refused"),
])
def test_sigterm_prints_exactly_one_line_and_never_contradicts_a_reported_entry(monkeypatch, capsys, state, kind, status):
    monkeypatch.setitem(job._phase, "state", state)
    monkeypatch.setitem(job._current, "seed_id", "eps_" + "1" * 24)
    monkeypatch.setitem(job._current, "mode", "apply")
    monkeypatch.setattr(job.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        job._on_sigterm(15, None)
    (row,) = job.parse_result_lines(capsys.readouterr().out)
    assert (row["kind"], row["status"]) == (kind, status)



def test_job_name_prefix_is_exact(job_env, monkeypatch):
    assert job.JOB_PREFIX == "reviewed-seed-batch-"
    for name in ("reviewed-seed-batc-one", "seed-batch-one", "reviewed-seed-batch"):
        monkeypatch.setenv("CLOUD_RUN_JOB", name)
        monkeypatch.setenv("CLOUD_RUN_EXECUTION", name + "-abc")
        with pytest.raises(batch.Refused, match="reviewed production seed batch job"):
            job.check_environment("plan")


def test_gate_is_rechecked_before_every_entry(monkeypatch, capsys):
    _, entries = batch.parse_manifest(data(manifest(entry_raw(1), entry_raw(2), entry_raw(3))))
    seen = []

    def gate(raw, now):
        seen.append(1)
        if len(seen) == 2:
            raise batch.Refused("launcher gate is stale")
        return {}

    calls, rows = _run_batch(monkeypatch, capsys, entries, behaviour={}, gate=gate)
    assert calls == [entries[0].seed_id]
    assert [r["status"] for r in rows] == ["committed", "refused", "not_attempted"]
    assert "stale" in rows[1]["error"]


def test_serving_pins_must_equal_the_additions(monkeypatch):
    from services import external_seed_review_pilot as pilot

    _, (entry,) = batch.parse_manifest(data(manifest()))
    monkeypatch.setitem(pilot.LISTINGS, entry.seed_id, _listing_for(entry))
    pins = {n: (g, s) for n, g, _, s in entry.additions}
    monkeypatch.setitem(pilot.NAMED_REFERRAL_VARIANTS, entry.seed_id, pins)
    job.check_serving_pins([entry])
    for wrong in ({}, dict(list(pins.items())[:1]), {**pins, "99": ("0" * 14, "X")}):
        monkeypatch.setitem(pilot.NAMED_REFERRAL_VARIANTS, entry.seed_id, wrong)
        with pytest.raises(batch.Refused, match="shade pins"):
            job.check_serving_pins([entry])


def test_approval_key_set_is_exact(monkeypatch, tmp_path):
    base = {"mode": "apply", "manifest_sha256": "m", "plan_sha256": "p", "release_sha": "r", "go_reference": "go",
            "plan_execution": "reviewed-seed-batch-one-plan"}
    for approval in ({**base, "extra": 1}, {k: v for k, v in base.items() if k != "go_reference"},
                     {**base, "go_reference": " "}, {**base, "go_reference": "x" * 161}):
        raw = json.dumps(approval).encode()
        (tmp_path / "a.json").write_bytes(raw)
        monkeypatch.setenv(job.ENV + "APPROVAL_PATH", str(tmp_path / "a.json"))
        monkeypatch.setenv(job.ENV + "APPROVAL_SHA256", hashlib.sha256(raw).hexdigest())
        with pytest.raises(batch.Refused, match="explicit go"):
            job.check_approval("apply", manifest_sha="m", plan_sha="p", release="r", receipts_sha=None,
                               plan_execution="reviewed-seed-batch-one-plan")



def test_write_all_leaves_each_reported_entry_in_the_reported_phase(monkeypatch, capsys):
    _, entries = batch.parse_manifest(data(manifest(entry_raw(1), entry_raw(2))))
    seen = []
    real_emit = job.emit

    def emit(payload):
        real_emit(payload)
        seen.append(None)

    monkeypatch.setattr(job, "emit", emit)
    calls, rows = _run_batch(monkeypatch, capsys, entries, behaviour={})
    assert [r["status"] for r in rows] == ["committed", "committed"]
    assert job._phase["state"] == "reported"
