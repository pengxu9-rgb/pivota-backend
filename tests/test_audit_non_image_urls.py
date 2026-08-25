"""Unit tests for scripts/audit_non_image_urls.py.

Covers the pure decision logic: which URLs get probed, and what the repair
planner is willing to do. The planner's refusals matter more than its edits —
an image-hygiene script must never be the reason a row stops serving.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "audit_non_image_urls", ROOT / "scripts" / "audit_non_image_urls.py"
)
mod = importlib.util.module_from_spec(_spec)
sys.modules["audit_non_image_urls"] = mod
_spec.loader.exec_module(mod)


PAGE = "https://theordinary.com/en-us/aloe-2-nag-2-solution-blemish-serum-100618.html"
IMG = "https://theordinary.com/dw/image/v2/BFKJ_PRD/rdn-aloe.png?sw=900"
EXTENSIONLESS = "https://media.ultainc.com/i/ulta/2609862?w=1000&h=1000"


class TestStructuralReason:
    @pytest.mark.parametrize(
        "url",
        [
            PAGE,
            "https://m.test/p/item.htm",
            "https://m.test/p/item.php",
            "https://m.test/p/item.aspx",
            "https://m.test/p/item.jsp",
            PAGE + "?utm_source=pivota&utm_medium=affiliate",
        ],
    )
    def test_page_extensions_are_candidates(self, url):
        assert mod._structural_reason(url, ()) == "page_extension"

    @pytest.mark.parametrize(
        "url",
        [
            IMG,
            EXTENSIONLESS,
            "https://cdn.shopify.com/s/files/1/x.jpg?v=2",
            "https://m.test/i/12345",
            "https://m.test/image/render?id=9",
        ],
    )
    def test_real_images_are_not_candidates(self, url):
        # An extensionless CDN image is the common case, not the exception.
        # Flagging these would make the audit useless.
        assert mod._structural_reason(url, ()) is None

    def test_url_equal_to_the_product_page_is_a_candidate(self):
        # This branch is the one that earns its keep: an EXTENSIONLESS page URL
        # is invisible to the extension rule, and only matching it against the
        # row's own canonical/destination URL catches it.
        extensionless_page = "https://brand.test/en-us/some-product"
        pages = (extensionless_page,)
        assert mod._structural_reason(extensionless_page, pages) == "equals_product_page_url"
        assert mod._structural_reason(extensionless_page, ()) is None

    def test_query_string_does_not_hide_a_page_url(self):
        pages = (mod._strip_query(PAGE),)
        tagged = PAGE + "?utm_source=pivota"
        assert mod._structural_reason(tagged, pages) is not None

    def test_extensionless_page_is_not_guessed(self):
        # Stage 1 must stay conservative; only the probe can condemn this.
        assert mod._structural_reason("https://m.test/en-us/some-product", ()) is None


class TestAsList:
    def test_accepts_a_json_string_payload(self):
        assert mod._as_list(f'["{IMG}"]') == [IMG]

    def test_accepts_a_real_list_and_trims(self):
        assert mod._as_list([f"  {IMG}  ", "", None, 5]) == [IMG]

    @pytest.mark.parametrize("value", [None, "", "not json", {}, 7])
    def test_degrades_to_empty(self, value):
        assert mod._as_list(value) == []


class TestPlanRepair:
    def test_drops_the_bad_gallery_entry_and_keeps_the_rest(self):
        row = {"image_url": IMG, "image_urls": [IMG, PAGE]}
        plan = mod._plan_repair(row, {PAGE})

        assert plan["action"] == "prune"
        assert plan["_new_gallery"] == [IMG]
        assert plan["image_urls"]["removed"] == [PAGE]
        assert plan["image_url"] is None  # scalar was already fine

    def test_promotes_a_surviving_image_rather_than_nulling_the_scalar(self):
        row = {"image_url": PAGE, "image_urls": [PAGE, IMG]}
        plan = mod._plan_repair(row, {PAGE})

        assert plan["action"] == "prune"
        assert plan["_new_scalar"] == IMG
        assert plan["_new_gallery"] == [IMG]

    def test_refuses_when_pruning_would_empty_the_row(self):
        # The whole point of the guard: an empty gallery can flip
        # serving-eligibility downstream, so this script declines to decide.
        row = {"image_url": PAGE, "image_urls": [PAGE]}
        plan = mod._plan_repair(row, {PAGE})

        assert plan["action"] == "needs_review"
        assert "no image" in plan["why"]

    def test_refuses_when_the_only_scalar_is_bad_and_there_is_no_gallery(self):
        row = {"image_url": PAGE, "image_urls": []}
        assert mod._plan_repair(row, {PAGE})["action"] == "needs_review"

    def test_noop_when_nothing_is_bad(self):
        row = {"image_url": IMG, "image_urls": [IMG]}
        assert mod._plan_repair(row, set())["action"] == "noop"

    def test_is_idempotent(self):
        row = {"image_url": PAGE, "image_urls": [PAGE, IMG]}
        first = mod._plan_repair(row, {PAGE})
        repaired = {"image_url": first["_new_scalar"], "image_urls": first["_new_gallery"]}
        assert mod._plan_repair(repaired, {PAGE})["action"] == "noop"


class TestApplyGuards:
    def test_apply_without_confirm_is_refused(self):
        assert mod.main(["--apply"]) == 2

    def test_apply_with_wrong_confirm_is_refused(self):
        assert mod.main(["--apply", "--confirm", "yes"]) == 2

    def test_apply_without_a_probe_is_refused(self):
        # --no-probe confirms nothing, so it must never be able to write.
        assert mod.main(["--apply", "--confirm", mod.CONFIRM_TOKEN, "--no-probe"]) == 2


class TestBuildScanSql:
    def _args(self, **kw):
        base = {"content_key": None, "brand": None, "limit": 0}
        base.update(kw)
        return type("A", (), base)()

    def test_unfiltered_query_binds_nothing(self):
        sql, params = mod._build_scan_sql(self._args())
        assert params == {}
        assert "LIMIT" not in sql.split(") s ON TRUE")[1]

    def test_content_key_filter_is_bound_not_interpolated(self):
        sql, params = mod._build_scan_sql(self._args(content_key="ck_1"))
        assert params == {"content_key": "ck_1"}
        assert ":content_key" in sql
        assert "ck_1" not in sql  # no string interpolation into SQL

    def test_brand_and_content_key_compose(self):
        # These were mutually exclusive (if/elif) before; passing both silently
        # dropped the brand filter.
        sql, params = mod._build_scan_sql(self._args(content_key="ck_1", brand="The Ordinary"))
        assert params == {"content_key": "ck_1", "brand": "The Ordinary"}
        assert ":content_key" in sql and ":brand" in sql

    def test_filter_lands_before_order_by(self):
        sql, _ = mod._build_scan_sql(self._args(content_key="ck_1"))
        assert sql.index(":content_key") < sql.index("ORDER BY v.content_key")

    def test_base_or_group_stays_parenthesised(self):
        # Load-bearing: see test_and_or_precedence_would_defeat_the_filter.
        sql, _ = mod._build_scan_sql(self._args(content_key="ck_1"))
        # rindex: the LATERAL subquery has its own WHERE; we want the outer one.
        where = sql[sql.rindex("WHERE"):sql.index("ORDER BY v.content_key")]
        assert where.startswith("WHERE (")
        assert where.index(")") < where.index("AND v.content_key")

    def test_and_or_precedence_would_defeat_the_filter(self):
        """Demonstrate WHY the base WHERE group must be parenthesised.

        AND binds tighter than OR, so `a OR b AND filter` keeps every row where
        `a` holds, ignoring the filter. Shown here on sqlite because the operator
        precedence is identical and it needs no server.
        """
        import sqlite3

        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE t (img TEXT, gallery INT, key TEXT)")
        con.executemany(
            "INSERT INTO t VALUES (?,?,?)",
            [("x.png", 0, "want"), ("y.png", 0, "other"), (None, 1, "other")],
        )

        unparenthesised = con.execute(
            "SELECT key FROM t WHERE img IS NOT NULL OR gallery > 0 AND key = 'want'"
        ).fetchall()
        parenthesised = con.execute(
            "SELECT key FROM t WHERE (img IS NOT NULL OR gallery > 0) AND key = 'want'"
        ).fetchall()

        assert sorted(r[0] for r in unparenthesised) == ["other", "want"]  # filter ignored
        assert [r[0] for r in parenthesised] == ["want"]  # filter honoured
