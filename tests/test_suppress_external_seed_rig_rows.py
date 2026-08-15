"""ADR-009 closing step — the rig-row retirement's doors answer both ways.

The step re-keys rows OFF the sentinel onto a merchant it derives from the
row's own store connection. The two guards — exactly one candidate, and it
must be a known test merchant — must ABORT before any write when they fail,
and let the run through when they hold. A tool that retires rows onto a
live tenant on a bad derivation is a wrong seller-of-record, not a degraded
one."""

import json

import pytest

import scripts.suppress_external_seed_rig_rows as mod
from scripts.backfill_seller_of_record import BANNED_BUCKET_MERCHANT_ID

RIG_ROW = {"product_key": "prod::external_seed::external_seed::rig1", "content_key": "ck",
           "source_domain": "jwx893-fz.myshopify.com", "platform": "external_seed",
           "source_product_id": "rig1", "suppression_reason": None, "seed_listing_ids": None}


class _Db:
    def __init__(self, *, rows, rig_rows):
        self.rows, self.rig_rows = rows, rig_rows
        self.executed = []
        self.connected = False

    async def connect(self): self.connected = True
    async def disconnect(self): self.connected = False

    async def fetch_all(self, sql, params=None):
        if "FROM catalog_products cp" in sql:
            return self.rows
        if "FROM merchant_stores" in sql:
            return self.rig_rows
        return []

    async def fetch_one(self, sql, params=None):
        if "count(*)" in sql:
            return {"c": 0}
        return None

    async def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), dict(params or {})))

    def transaction(self):
        class _T:
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
        return _T()


@pytest.fixture
def wire(monkeypatch):
    def _wire(db, known):
        import db.database as dbmod
        monkeypatch.setattr(dbmod, "database", db)
        monkeypatch.setattr(mod, "static_test_merchant_ids", lambda: set(known))
        # the cascade helpers touch pgm/listing tables through the flip tool;
        # stub them so this test pins the STEP's own contract only
        async def _pgm(self, b, obs): return {"moved": 1, "retired": 0}
        async def _lst(self, b, obs): return None
        monkeypatch.setattr(mod.SellerBackfill, "_resubject_group_membership", _pgm)
        monkeypatch.setattr(mod.SellerBackfill, "_migrate_listing_refs", _lst)
        return db
    return _wire


class TestDoors:
    @pytest.mark.asyncio
    async def test_aborts_when_the_rig_merchant_is_not_a_known_test_merchant(self, wire, capsys):
        db = wire(_Db(rows=[RIG_ROW], rig_rows=[{"merchant_id": "merch_live_tenant", "status": "active"}]),
                  known={"merch_efbc46b4619cfbdf"})
        assert await mod._run(apply=True) == 2
        assert db.executed == []
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
    async def test_dry_run_prints_recon_and_writes_nothing(self, wire, capsys):
        db = wire(_Db(rows=[RIG_ROW], rig_rows=[{"merchant_id": "merch_rig", "status": "inactive"}]),
                  known={"merch_rig"})
        assert await mod._run(apply=False) == 0
        assert db.executed == []
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        recon = json.loads(out[out.index("{"):out.rindex("}") + 1])["recon"]
        assert recon["cohort_rows"] == 1
        assert recon["rig_merchant_is_known_test_merchant"] == {"merch_rig": True}

    @pytest.mark.asyncio
    async def test_apply_tombstones_before_rekey_and_scopes_the_rekey_to_tombstoned_rows(self, wire):
        db = wire(_Db(rows=[RIG_ROW], rig_rows=[{"merchant_id": "merch_rig", "status": "inactive"}]),
                  known={"merch_rig"})
        assert await mod._run(apply=True) == 0
        sqls = [s for s, _ in db.executed]
        assert len(sqls) == 2
        # 1) tombstone FIRST — the row leaves every public surface before it
        #    moves anywhere
        assert sqls[0].startswith("UPDATE catalog_products SET suppression_reason = :reason")
        assert db.executed[0][1]["reason"] == "step5_test_rig_retirement"
        assert json.loads(db.executed[0][1]["meta"])["rekeyed_to"] == "merch_rig"
        # 2) then the re-key, pinned to rows that ARE tombstoned and still
        #    under the sentinel — never a blind merchant swap
        assert sqls[1].startswith("UPDATE catalog_products SET merchant_id = :rig")
        assert "AND suppression_reason = :reason" in sqls[1]
        assert db.executed[1][1]["banned"] == BANNED_BUCKET_MERCHANT_ID
        assert db.executed[1][1]["rig"] == "merch_rig"

    @pytest.mark.asyncio
    async def test_empty_cohort_is_a_clean_noop(self, wire, capsys):
        db = wire(_Db(rows=[], rig_rows=[]), known=set())
        assert await mod._run(apply=True) == 0
        assert db.executed == []
        assert "cohort is empty" in capsys.readouterr().out
