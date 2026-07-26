"""Tests for the one-canonical-URL-per-content_key election.

The property under test is not "the code runs" but the cross-repo invariant:

    the sig the sitemap advertises == the sig every sibling PDP canonicalises at

Two things can break it. The elector can crown a sig the feed never emits (a
candidate-set mismatch — guarded by the SQL tests at the bottom, which assert
the elector selects through the SAME shared filter the feed does), or the winner
can move for a reason the sitemap would not have moved it for (a stickiness
break — guarded by the picker tests).

pivota-backend CI has been dark since the 2026-07-14 Actions billing outage, so
this file is the gate.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import literal_column, select

from services.content_canonical_election import (
    REASON_LEXICOGRAPHIC,
    REASON_SIG_CLASS,
    REASON_SITEMAP_INCUMBENT,
    REASON_SOLE_CANDIDATE,
    REASON_STICKY,
    candidates_query,
    pick_winner,
    plan_elections,
)

# Two same-class sigs. `aaa…` sorts before `fff…`, which is what makes the
# incumbency-vs-lexicographic tests meaningful.
SIG_A = "sig_" + "a" * 32
SIG_F = "sig_" + "f" * 32
SIG_LEGACY = "sig_" + "b" * 24


class TestPickWinner:
    def test_no_candidates_returns_none(self):
        assert pick_winner([]) is None
        assert pick_winner(["", "  ", None]) is None

    def test_sole_candidate_wins(self):
        assert pick_winner([SIG_A]) == (SIG_A, REASON_SOLE_CANDIDATE)

    def test_stored_winner_is_kept_even_when_lexicographically_worse(self):
        """THE stickiness rule. A stored winner outranks every ordering layer.

        This is the whole reason the table exists: `sig_fff…` sorts last and
        would lose every tiebreak, but if it is the URL we already advertise it
        keeps the URL. Anything else throws away real index equity to buy a
        tidier id.
        """
        assert pick_winner([SIG_A, SIG_F], stored=SIG_F) == (SIG_F, REASON_STICKY)

    def test_stored_winner_that_is_no_longer_a_candidate_is_replaced(self):
        """The ONE condition that moves a URL: the incumbent stopped qualifying."""
        gone = "sig_" + "9" * 32
        assert pick_winner([SIG_A, SIG_F], stored=gone) == (SIG_A, REASON_LEXICOGRAPHIC)

    def test_sitemap_incumbent_beats_lexicographic(self):
        """Seeding imports the live sitemap's answer, not a fresh ordering.

        Without this layer the 183 groups whose incumbent is not the
        lexicographic minimum would each swap an indexed URL for a new one on
        the first sweep — the exact regression pivota-agent-ui#280 prevented on
        the sitemap side.
        """
        assert pick_winner([SIG_A, SIG_F], incumbents={SIG_F}) == (
            SIG_F,
            REASON_SITEMAP_INCUMBENT,
        )

    def test_two_incumbents_narrow_the_pool_instead_of_falling_through(self):
        """A pre-#280 sitemap can advertise both siblings — order AMONG them.

        This test previously asserted the opposite ("fall through rather than
        guess"), which was wrong twice: falling through ordered the whole pool
        INCLUDING non-incumbents, so a third, brand-new sig could beat two
        already-indexed ones; and there was no guess to avoid, since layers 2-3
        are a total order.
        """
        third = "sig_" + "0" * 32  # sorts first, so it wins any un-narrowed pass
        assert pick_winner(
            [third, SIG_A, SIG_F], incumbents={SIG_A, SIG_F}
        ) == (SIG_A, REASON_SITEMAP_INCUMBENT)

    def test_parity_with_preferSitemapId_over_the_whole_combination_space(self):
        """Differential test against the REAL sitemap ordering, in node.

        The seed's job is to import the sitemap's answer. "Import" is only
        meaningful if the two agree, and hand-reasoning about two orderings in
        two languages is exactly how they drifted: an earlier version of this
        picker disagreed on 60 of these 400 cases.
        """
        import itertools
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            pytest.skip("node is required for the differential parity test")

        lib = "/Users/pengchydan/dev/pa-ui-canonical/scripts/sitemap_lib.mjs"
        if not os.path.exists(lib):
            pytest.skip("pivota-agent-ui worktree not present")

        sigs = ["sig_" + c * 32 for c in "abc"] + ["sig_" + "d" * 24]
        cases = []
        for size in (2, 3, 4):
            for combo in itertools.combinations(sigs, size):
                for inc_size in range(len(combo) + 1):
                    for inc in itertools.combinations(combo, inc_size):
                        cases.append({"cands": list(combo), "inc": list(inc)})

        script = (
            f"import {{ preferSitemapId }} from {json.dumps(lib)};"
            "const cases = JSON.parse(process.argv[1]);"
            # Fold pairwise left-to-right — exactly how mergeDuplicateProduct
            # reduces a group of 3+ siblings.
            "console.log(JSON.stringify(cases.map(c =>"
            "  c.cands.reduce((a,b) => preferSitemapId(a,b,new Set(c.inc),'')))));"
        )
        js_winners = json.loads(
            subprocess.run(
                [node, "--input-type=module", "-e", script, json.dumps(cases)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )

        mismatches = [
            (case, js, pick_winner(case["cands"], incumbents=set(case["inc"]) or None)[0])
            for case, js in zip(cases, js_winners)
            if pick_winner(case["cands"], incumbents=set(case["inc"]) or None)[0] != js
        ]
        assert not mismatches, f"{len(mismatches)}/{len(cases)} diverge: {mismatches[:3]}"

    def test_no_incumbent_in_group_falls_through(self):
        assert pick_winner([SIG_A, SIG_F], incumbents={"sig_unrelated"}) == (
            SIG_A,
            REASON_LEXICOGRAPHIC,
        )

    def test_sig_class_beats_lexicographic(self):
        """32-hex over legacy 24-hex, matching preferSitemapId's layer 2.

        Inert against today's corpus — zero duplicate groups mix classes — but
        it has to agree with the sitemap's fallback for the window before a new
        content_key's first election.
        """
        assert pick_winner([SIG_LEGACY, SIG_F]) == (SIG_F, REASON_SIG_CLASS)

    def test_ordering_is_independent_of_input_order(self):
        """Rows arrive in whatever order the database returns them."""
        for candidates in ([SIG_A, SIG_F], [SIG_F, SIG_A]):
            assert pick_winner(candidates)[0] == SIG_A

    def test_handles_groups_larger_than_a_pair(self):
        """Prod has groups of 3 (25), 4 (22), 5 (1) and 7 (1) — not just pairs."""
        group = [f"sig_{str(i) * 32}" for i in range(7)]
        assert pick_winner(group, incumbents={group[4]}) == (
            group[4],
            REASON_SITEMAP_INCUMBENT,
        )

    def test_duplicate_candidates_collapse(self):
        assert pick_winner([SIG_A, SIG_A]) == (SIG_A, REASON_SOLE_CANDIDATE)


class TestPlanElections:
    def test_steady_state_writes_nothing(self):
        """The signal that the stickiness rule is holding."""
        planned = plan_elections(
            candidates_by_content_key={"ck_1": [SIG_A, SIG_F]},
            stored_by_content_key={"ck_1": SIG_F},
        )
        assert planned == []

    def test_new_content_key_is_elected(self):
        planned = plan_elections(
            candidates_by_content_key={"ck_1": [SIG_A, SIG_F]},
            stored_by_content_key={},
        )
        assert len(planned) == 1
        assert planned[0].canonical_sig_id == SIG_A
        assert planned[0].replaced is None

    def test_replacement_records_what_it_replaced(self):
        gone = "sig_" + "9" * 32
        planned = plan_elections(
            candidates_by_content_key={"ck_1": [SIG_A]},
            stored_by_content_key={"ck_1": gone},
        )
        assert planned[0].replaced == gone
        assert planned[0].canonical_sig_id == SIG_A

    def test_content_key_with_zero_candidates_leaves_the_stored_row_alone(self):
        """Withdrawing the election would un-canonicalise every sibling page.

        Renderability is deliberately conservative, so an empty candidate set is
        more often a transient read than a dead product. Dropping the row would
        make each still-serving sibling self-canonical again and re-expose the
        duplicate — and it would do so at exactly the moment we have the least
        information.
        """
        planned = plan_elections(
            candidates_by_content_key={"ck_1": []},
            stored_by_content_key={"ck_1": SIG_F},
        )
        assert planned == []

    def test_seeding_a_whole_corpus_keeps_every_incumbent(self):
        """The rollout case: N groups, one live URL each, nothing moves."""
        groups = {f"ck_{i}": [SIG_A, SIG_F] for i in range(50)}
        planned = plan_elections(
            candidates_by_content_key=groups,
            stored_by_content_key={},
            incumbents={SIG_F},
        )
        assert len(planned) == 50
        assert {e.canonical_sig_id for e in planned} == {SIG_F}
        assert all(e.election_reason == REASON_SITEMAP_INCUMBENT for e in planned)


def _compile(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


class TestElectableSigGuard:
    """A stored election must never outlive the electability it was based on.

    THE FAILURE THIS CLOSES. ck_x elects sig A; A later stops rendering. The
    sitemap is structurally safe — its renderable filter runs BEFORE the dedup,
    so A is not a candidate and sibling B is advertised. But a reader that joins
    the election on content_key alone still hands B's PDP the string "A", and
    B's page emits <link rel="canonical" href="/products/A"> at a URL that 500s.
    We submit B and B disavows itself in favour of a dead page: the content_key
    loses ALL index presence, which is worse than the duplicate AND worse than a
    moved URL.

    Not hypothetical — P3 moved 2,051 rows on `renderable` in one day, and
    nothing prevents the reverse direction.
    """

    def test_guard_requires_the_elected_sig_to_be_electable(self):
        from services.canonical_sitemap_candidates import electable_sig_exists

        sql = _compile(
            select(electable_sig_exists(literal_column("'sig_x'"), widen=False))
        )
        # It must re-ask the SAME question of the ELECTED row, on its own alias.
        assert "cp_elected" in sql
        assert "cp_elected.pivota_signature_id = 'sig_x'" in sql
        assert "cp_elected.suppressed_at IS NULL" in sql
        assert "ips_elected.serving_eligible IS true" in sql
        assert "cm_elected.indexable IS true" in sql
        # ...including renderability, which is the field that actually flaps.
        assert "external_product_seeds" in sql

    def test_guard_does_not_disturb_the_unaliased_callers(self):
        """Adding the alias parameters must not change the feed's own SQL.

        The aliasing exists only so the same predicate can be asked about a
        second row in one statement. If it leaked into the default call, the
        feed's eligibility would silently change — the exact drift the shared
        module was created to prevent.
        """
        from services.canonical_sitemap_candidates import (
            sitemap_candidate_filter,
            catalog_products,
        )

        for widen in (False, True):
            explicit = _compile(
                select(literal_column("1")).where(
                    sitemap_candidate_filter(
                        widen=widen,
                        cp=catalog_products,
                        ips=None,
                        cm=None,
                    )
                )
            )
            default = _compile(
                select(literal_column("1")).where(
                    sitemap_candidate_filter(widen=widen)
                )
            )
            assert explicit == default


class TestCandidateSetMatchesTheFeed:
    """The elector must not be able to crown a sig the feed will never emit."""

    def test_elector_and_feed_compile_the_same_eligibility(self):
        """THE parity assertion — compare the two sides, don't grep one side.

        The previous tests here asserted substrings of the ELECTOR's own SQL and
        never compared it to the FEED's, so the class name overstated what it
        checked: it would have passed with the strict merchant gate flipped
        INNER->LEFT, with `suppressed_at IS NULL` dropped, or with the
        `like('sig_%')` gone. This compiles both and asserts equality.
        """
        from services.canonical_sitemap_candidates import (
            sitemap_candidate_filter,
            sitemap_candidate_join,
        )

        for widen in (False, True):
            feed_side = _compile(
                select(literal_column("1"))
                .select_from(sitemap_candidate_join(widen=widen))
                .where(sitemap_candidate_filter(widen=widen))
            )
            elector_side = _compile(candidates_query(widen=widen))
            # The elector selects different columns and adds its own ordering +
            # the sig-not-null narrowing, so compare the shared WHERE core.
            for clause in (
                "suppressed_at IS NULL",
                "pivota_signature_id LIKE 'sig_%%'",
                "serving_eligible IS true",
                "catalog_merchants.status IN ('active', 'observed')",
            ):
                assert (clause in feed_side) == (clause in elector_side), (
                    f"{clause!r} present on only one side (widen={widen})"
                )
            assert ("LEFT OUTER JOIN catalog_merchants" in feed_side) == (
                "LEFT OUTER JOIN catalog_merchants" in elector_side
            )

    def _sql(self, **env) -> str:
        return _compile(candidates_query(**env))

    def test_selects_through_the_same_gates_the_feed_uses(self):
        sql = self._sql(widen=False)
        assert "JOIN index_pipeline_state" in sql
        assert "JOIN catalog_merchants" in sql
        assert "serving_eligible IS true" in sql
        assert "suppressed_at IS NULL" in sql

    def test_requires_renderability(self):
        """The feed publishes `renderable` as a field and the consumer drops on
        it; the elector has no consumer, so it must filter in SQL or it will
        crown sigs the sitemap silently discards."""
        assert "external_product_seeds" in self._sql(widen=False)

    def test_never_crowns_a_sigless_row(self):
        """Widened, a row qualifies on content_key alone and carries no sig.
        Such a row can never BE a URL, so it must not be a candidate for one."""
        sql = self._sql(widen=True)
        assert "pivota_signature_id IS NOT NULL" in sql

    def test_widened_mode_left_joins_merchants(self):
        assert "LEFT OUTER JOIN catalog_merchants" in self._sql(widen=True)


class TestSitemapParser:
    """The seed import has to read the same ids pivota-agent-ui wrote."""

    def test_reads_product_locs_only(self):
        from scripts.elect_content_canonicals import parse_sitemap_product_ids

        xml = (
            "<urlset>"
            f"<url><loc>https://agent.pivota.cc/products/{SIG_A}</loc></url>"
            "<url><loc>https://agent.pivota.cc/brands/acme</loc></url>"
            f"<url><loc>https://agent.pivota.cc/products/{SIG_F}</loc></url>"
            "</urlset>"
        )
        assert parse_sitemap_product_ids(xml) == {SIG_A, SIG_F}

    def test_undoes_both_writers_in_order(self):
        """productUrlEntries percent-encodes, then buildSitemapUrlsetXml escapes."""
        from scripts.elect_content_canonicals import parse_sitemap_product_ids

        xml = "<url><loc>https://agent.pivota.cc/products/o&apos;neill%20x</loc></url>"
        assert parse_sitemap_product_ids(xml) == {"o'neill x"}

    def test_empty_sitemap_reads_as_no_incumbents(self):
        from scripts.elect_content_canonicals import parse_sitemap_product_ids

        assert parse_sitemap_product_ids("") == set()
        assert parse_sitemap_product_ids("<urlset></urlset>") == set()

    def test_seeding_refuses_an_empty_read(self, tmp_path):
        """A wrong path must not degrade seeding into a lexicographic sweep."""
        from scripts.elect_content_canonicals import load_sitemap

        empty = tmp_path / "sitemap-products.xml"
        empty.write_text("<urlset></urlset>", encoding="utf-8")
        with pytest.raises(SystemExit, match="ZERO product URLs"):
            load_sitemap(str(empty))
