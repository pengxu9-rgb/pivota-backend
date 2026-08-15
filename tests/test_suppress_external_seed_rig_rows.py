"""ADR-009 closing step — the rig-row retirement's doors answer both ways, and
the apply path honours the flip's full write contract.

The step re-keys rows OFF the sentinel onto a merchant it derives from the
row's own store connection. Every door — exactly one candidate, it must be a
known test merchant, no listing new-ref conflict — must ABORT before any
write when it fails and let the run through when it holds. The apply path
must run the flip's full cascade inside ONE transaction and roll back on
residue. The fake here enforces exact bind equality on every statement, is
STATEFUL for the sentinel rows / dependents / pgm (so post-write audits
count like the real tables), and records transaction boundaries."""

import json
import re

import pytest

import scripts.suppress_external_seed_rig_rows as mod
from scripts.backfill_seller_of_record import BANNED_BUCKET_MERCHANT_ID

RIG = "merch_rig0000000"
RIG_ROW = {"product_key": "prod::external_seed::external_seed::rig1", "content_key": "ck1",
           "source_domain": "jwx893-fz.myshopify.com", "canonical_url": None,
           "platform": "external_seed", "source_product_id": "rig1",
           "suppression_reason": None, "suppression_metadata": None,
           "seed_domain": None, "seed_destination_url": None, "seed_listing_ids": ["seed9"]}
NON_RIG_ROW = {**RIG_ROW, "product_key": "prod::external_seed::external_seed::other",
               "source_domain": "brand.com", "source_product_id": "other", "seed_listing_ids": None}


def _assert_binds_match(sql, params):
    named = set(re.findall(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", sql))
    given = set((params or {}).keys())
    assert named == given, f"bind mismatch: SQL {sorted(named)} vs params {sorted(given)}\n{sql}"


class _Db:
    def __init__(self, *, rows, rig_rows, cascade=None, listing_old=(), listing_new=(),
                 pgm_banned=None):
        self.rows = [dict(r) for r in rows]        # sentinel rows (stateful)
        self.rig_rows = rig_rows
        self.cascade = cascade or []                # [(table, [cols])]
        self.listing_old = set(listing_old)         # old refs that exist
        self.listing_new = set(listing_new)         # new refs that exist
        self.pgm_banned = list(pgm_banned or [])    # stateful banned pgm rows
        self.dependent_banned = {t: {r["product_key"] for r in self.rows}
                                 for t, _ in self.cascade}
        self.executed, self.fetched, self.txn_events = [], [], []

    async def connect(self): pass
    async def disconnect(self): pass

    async def fetch_all(self, sql, params=None):
        _assert_binds_match(sql, params)
        self.fetched.append(" ".join(sql.split()))
        if "FROM catalog_products cp" in sql:
            return [dict(r) for r in self.rows]
        if "FROM merchant_stores" in sql:
            return self.rig_rows
        if "information_schema.columns" in sql and "pdp_identity_listing" in sql:
            return [{"column_name": c} for c in ("source_listing_ref", "merchant_id", "product_id")]
        if "FROM information_schema.columns" in sql:
            return [{"table_name": t, "column_name": c} for t, cols in self.cascade for c in cols]
        if "FROM catalog_skus" in sql:
            return []
        if "FROM product_group_members" in sql and "ANY(:pids)" in sql:
            pids = set(params["pids"])
            return [r for r in self.pgm_banned if r["platform_product_id"] in pids]
        return []

    async def fetch_one(self, sql, params=None):
        _assert_binds_match(sql, params)
        self.fetched.append(" ".join(sql.split()))
        if "FROM pdp_identity_listing WHERE source_listing_ref" in sql:
            ref = params.get("r") or params.get("old_ref") or ""
            return {"?column?": 1} if (ref in self.listing_old or ref in self.listing_new) else None
        if "count(*)" in sql and "FROM catalog_products WHERE merchant_id = :banned" in sql:
            if "ANY(:pks)" in sql:
                pks = set(params["pks"])
                return {"c": sum(1 for r in self.rows if r["product_key"] in pks)}
            return {"c": len(self.rows)}
        if "count(*)" in sql:
            for t, _ in self.cascade:               # residue audit over reflected dependents
                if f"FROM {t}" in sql:
                    return {"c": len(self.dependent_banned[t] & set(params["pks"]))}
            return {"c": 0}
        return None

    async def execute(self, sql, params=None):
        _assert_binds_match(sql, params)
        flat = " ".join(sql.split())
        self.executed.append((flat, dict(params or {})))
        # state transitions mirroring the real tables
        if flat.startswith("UPDATE catalog_products SET merchant_id = :rig"):
            pks = set(params["pks"])
            self.rows = [r for r in self.rows
                         if not (r["product_key"] in pks and r.get("suppression_reason") == params["reason"])]
        elif flat.startswith("UPDATE catalog_products SET suppression_reason"):
            for r in self.rows:
                if r["product_key"] in set(params["pks"]):
                    r["suppression_reason"] = params["reason"]
        elif any(flat.startswith(f"UPDATE {t} SET") for t, _ in self.cascade):
            for t, _ in self.cascade:
                if flat.startswith(f"UPDATE {t} SET") and "product_key = :scope" in flat:
                    self.dependent_banned[t].discard(params["scope"])
        elif "product_group_members" in flat and params.get("banned") == BANNED_BUCKET_MERCHANT_ID:
            self.pgm_banned = [r for r in self.pgm_banned
                               if not (r["platform"] == params["platform"] and
                                       r["platform_product_id"] == params["spid"])]

    def transaction(self):
        db = self

        class _T:
            async def __aenter__(s):
                db.txn_events.append("begin")
                return s

            async def __aexit__(s, exc_type, *a):
                db.txn_events.append("rollback" if exc_type else "commit")
                return False
        return _T()


@pytest.fixture
def wire(monkeypatch):
    def _wire(db, known):
        import db.database as dbmod
        monkeypatch.setattr(dbmod, "database", db)
        monkeypatch.setattr(mod, "static_test_merchant_ids", lambda: set(known))
        return db
    return _wire


def _recon(out):
    return json.loads(out[out.index("{"):out.index("\n}\n") + 2])["recon"]


class TestDoors:
    @pytest.mark.asyncio
    async def test_aborts_when_the_rig_merchant_is_not_a_known_test_merchant(self, wire, capsys):
        db = wire(_Db(rows=[RIG_ROW], rig_rows=[{"merchant_id": "merch_live", "status": "active"}]),
                  known={"merch_efbc46b4619cfbdf"})
        assert await mod._run(apply=True) == 2
        assert db.executed == [] and db.txn_events == []
        assert "not a known test merchant" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_aborts_when_the_derivation_is_ambiguous(self, wire, capsys):
        db = wire(_Db(rows=[RIG_ROW], rig_rows=[{"merchant_id": "merch_a", "status": "inactive"},
                                                 {"merchant_id": "merch_b", "status": "inactive"}]),
                  known={"merch_a", "merch_b"})
        assert await mod._run(apply=True) == 2
        assert db.executed == []
        assert "exactly one rig merchant" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_aborts_on_a_listing_new_ref_conflict_before_any_write(self, wire, capsys):
        # A listing already lives under <rig>:rig1 AND the old external_seed:rig1
        # exists: migrating would merge operator work onto unreviewed content.
        db = wire(_Db(rows=[RIG_ROW], rig_rows=[{"merchant_id": RIG, "status": "inactive"}],
                      listing_old={"external_seed:rig1"}, listing_new={f"{RIG}:rig1"}),
                  known={RIG})
        assert await mod._run(apply=True) == 2
        assert db.executed == [] and db.txn_events == []
        out = capsys.readouterr()
        assert "listing new-ref conflict" in out.err
        assert _recon(out.out)["listing_new_ref_conflicts"][0]["conflicting_ref"] == f"{RIG}:rig1"

    @pytest.mark.asyncio
    async def test_conflict_door_is_evaluated_in_dry_run_too(self, wire):
        db = wire(_Db(rows=[RIG_ROW], rig_rows=[{"merchant_id": RIG, "status": "inactive"}],
                      listing_old={"external_seed:rig1"}, listing_new={f"{RIG}:rig1"}),
                  known={RIG})
        assert await mod._run(apply=False) == 2
        assert db.executed == []

    @pytest.mark.asyncio
    async def test_dry_run_prints_recon_and_writes_nothing(self, wire, capsys):
        db = wire(_Db(rows=[RIG_ROW, NON_RIG_ROW], rig_rows=[{"merchant_id": RIG, "status": "inactive"}]),
                  known={RIG})
        assert await mod._run(apply=False) == 0
        assert db.executed == [] and db.txn_events == []
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        recon = _recon(out)
        # cohort selected by the flip's OWN predicate; the non-rig row is
        # reported as left behind, not silently swept in
        assert recon["sentinel_rows_total"] == 2
        assert recon["cohort_rows"] == 1
        assert recon["non_rig_sentinel_rows_left_behind"] == 1
        assert recon["rig_merchant_is_known_test_merchant"] == {RIG: True}

    @pytest.mark.asyncio
    async def test_empty_cohort_is_a_clean_noop(self, wire, capsys):
        db = wire(_Db(rows=[], rig_rows=[]), known=set())
        assert await mod._run(apply=True) == 0
        assert db.executed == []
        assert "cohort is empty" in capsys.readouterr().out


class TestApply:
    def _db(self, **kw):
        base = dict(rows=[RIG_ROW], rig_rows=[{"merchant_id": RIG, "status": "inactive"}],
                    cascade=[("catalog_offers", ["merchant_id", "product_key"])],
                    listing_old={"external_seed:rig1", "external_seed:seed9"},
                    pgm_banned=[{"platform": "amazon", "platform_product_id": "seed9",
                                 "product_group_id": "g"}])
        base.update(kw)
        return _Db(**base)

    @pytest.mark.asyncio
    async def test_apply_runs_the_full_flip_contract_in_one_transaction(self, wire, capsys):
        db = wire(self._db(), known={RIG})
        assert await mod._run(apply=True) == 0
        # ONE transaction, committed
        assert db.txn_events == ["begin", "commit"]
        sqls = [s for s, _ in db.executed]
        # 1) tombstone FIRST, carrying the run's provenance
        assert sqls[0].startswith("UPDATE catalog_products SET suppression_reason = :reason")
        assert db.executed[0][1]["reason"] == "step5_test_rig_retirement"
        assert json.loads(db.executed[0][1]["meta"])["rekeyed_to"] == RIG
        # 2) then the re-key, scoped to tombstoned sentinel rows
        assert sqls[1].startswith("UPDATE catalog_products SET merchant_id = :rig")
        assert "AND suppression_reason = :reason" in sqls[1]
        # 3) the reflected dependent leg actually ran (review finding 2)
        assert any(s.startswith("UPDATE catalog_offers SET merchant_id = :obs") for s in sqls)
        # 4) pgm hunted on the seed id + foreign platform, and listings migrated
        assert any(s.startswith("UPDATE product_group_members") and p["spid"] == "seed9"
                   for s, p in db.executed)
        assert any(s.startswith("INSERT INTO pdp_identity_listing") for s in sqls)
        # 5) residue audit ran inside the txn over the reflected table
        assert any("count(*)" in f and "FROM catalog_offers" in f for f in db.fetched)
        # bucket empty, asserted; pgm counted
        out = capsys.readouterr().out
        report = json.loads(out[out.rindex('{\n  "run_id"'):])
        assert report["sentinel_rows_remaining_total"] == 0
        assert report["pgm"] == {"moved": 1, "retired": 0}

    @pytest.mark.asyncio
    async def test_cascade_residue_rolls_the_whole_step_back(self, wire):
        db = wire(self._db(), known={RIG})
        real_execute = db.execute

        async def leaky(sql, params=None):
            # a dependent UPDATE that silently affects nothing → residue stays
            if sql.lstrip().startswith("UPDATE catalog_offers"):
                _assert_binds_match(sql, params)
                db.executed.append((" ".join(sql.split()), dict(params or {})))
                return
            return await real_execute(sql, params)

        db.execute = leaky
        with pytest.raises(RuntimeError, match="cascade residue"):
            await mod._run(apply=True)
        assert db.txn_events == ["begin", "rollback"]

    @pytest.mark.asyncio
    async def test_prior_tombstone_provenance_is_carried_in_metadata(self, wire):
        prior = dict(RIG_ROW, suppression_reason="step5_orphan_seed_mirror",
                     suppression_metadata={"run_id": "old"})
        db = wire(self._db(rows=[prior]), known={RIG})
        assert await mod._run(apply=True) == 0
        meta = json.loads(db.executed[0][1]["meta"])
        assert meta["prior_tombstones"][RIG_ROW["product_key"]]["reason"] == "step5_orphan_seed_mirror"

    @pytest.mark.asyncio
    async def test_leftover_non_rig_sentinel_row_fails_the_step_loudly(self, wire, capsys):
        # a row the flip's exclusion never matched but the flip also left
        # behind: this step must NOT sweep it in, and must NOT report success
        db = wire(self._db(rows=[RIG_ROW, NON_RIG_ROW]), known={RIG})
        assert await mod._run(apply=True) == 1
        assert db.txn_events == ["begin", "commit"]   # the rig rows still retire
        assert "still under the sentinel" in capsys.readouterr().err
        assert all("other" not in json.dumps(p) for _, p in db.executed)


class TestVerifierGlobalResidue:
    """The verifier grades the rig retirement (and any stray writer) with a
    GLOBAL residue over every text-typed seller column of every public base
    table, read straight from information_schema — NOT through the flip's
    reflection. It splits tables by a PRINCIPLED rule, not a list: a table
    with a product-scope column is OWNERSHIP (fails on residue once the
    bucket is empty); one without is HISTORY (a record of what happened —
    reported, never failed, never rewritten)."""

    class _V:
        def __init__(self, counts):
            self.counts, self.counted = counts, []

        async def fetch_all(self, sql, params=None):
            assert "information_schema.columns" in sql
            assert "data_type IN" in sql          # int seller cols raise DataError
            assert "product_scoped" in sql
            return [
                {"table_name": "catalog_products", "column_name": "merchant_id", "product_scoped": True},
                {"table_name": "catalog_offers", "column_name": "merchant_id", "product_scoped": True},
                {"table_name": "beauty_sku_ingredients", "column_name": "primary_merchant_id",
                 "product_scoped": True},
                {"table_name": "product_group_members", "column_name": "merchant_id",
                 "product_scoped": False},   # keys on platform_product_id, not a scope col
                {"table_name": "agent_product_events", "column_name": "merchant_id",
                 "product_scoped": False},
            ]

        async def fetch_one(self, sql, params=None):
            _assert_binds_match(sql, params)
            assert params["banned"] == BANNED_BUCKET_MERCHANT_ID
            self.counted.append(sql)
            for t, n in self.counts.items():
                if f"FROM {t} " in sql:
                    return {"c": n}
            return {"c": 0}

    @pytest.mark.asyncio
    async def test_splits_ownership_from_history_by_product_scope(self):
        import scripts.verify_seller_rekey as ver

        v = self._V({"catalog_products": 0, "catalog_offers": 12,
                     "agent_product_events": 151698, "product_group_members": 0})
        out = await ver.global_residue(v)
        # product-scoped residue is an orphan -> ownership; event rows are history
        assert out["ownership"] == {"catalog_products": 0, "catalog_offers": 12}
        assert out["history"] == {"agent_product_events": 151698}
        # the reflected seller column is honoured per table
        assert any("FROM beauty_sku_ingredients WHERE primary_merchant_id" in q for q in v.counted)

    def test_only_ownership_residue_fails_once_the_bucket_is_empty(self):
        from scripts.verify_seller_rekey import orphan_failures  # the REAL rule
        assert orphan_failures({"ownership": {"catalog_products": 0, "catalog_offers": 12},
                                "history": {"agent_product_events": 151698}}) == \
            ["orphan_residue:catalog_offers=12"]
        assert orphan_failures({"ownership": {"catalog_products": 0},
                                "history": {"agent_product_events": 151698}}) == []
        # while the bucket still holds rows, dependents legitimately share it
        assert orphan_failures({"ownership": {"catalog_products": 50, "catalog_offers": 12},
                                "history": {}}) == []


class TestRekeyDoor:
    @pytest.mark.asyncio
    async def test_a_rekey_that_affects_nothing_rolls_the_step_back(self, wire):
        # Review mutant M2: a re-key UPDATE that silently no-ops must be caught
        # INSIDE the transaction by the left!=0 door and rolled back — never
        # discovered only by the post-commit count.
        db = wire(TestApply()._db(), known={RIG})
        real_execute = db.execute

        async def leaky(sql, params=None):
            if sql.lstrip().startswith("UPDATE catalog_products") and "SET merchant_id = :rig" in sql:
                _assert_binds_match(sql, params)
                db.executed.append((" ".join(sql.split()), dict(params or {})))
                return  # rows stay under the sentinel
            return await real_execute(sql, params)

        db.execute = leaky
        with pytest.raises(RuntimeError, match="still under the sentinel after re-key"):
            await mod._run(apply=True)
        assert db.txn_events == ["begin", "rollback"]
