"""Tests for the one-canonical-URL-per-content_key election.

The property under test is not "the code runs" but the cross-repo invariant:

    the sig the sitemap advertises == the sig every sibling PDP canonicalises at

Two things can break it. The elector can crown a sig the feed never emits (a
candidate-set mismatch — guarded by the SQL tests at the bottom, which assert
the elector selects through the SAME shared filter the feed does), or the winner
can move for a reason the sitemap would not have moved it for (a stickiness
break — guarded by the picker tests).

pivota-backend CI is back (the 2026-07-14 Actions billing outage is resolved)
but runs only a partial suite, so this file is still the real gate. Anything
here that SKIPS is not a gate at all — which is why the parity claim is a
checked-in golden with the live node differential as a secondary guard on the
golden itself, rather than the other way round.
"""

from __future__ import annotations

import json
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

    # GOLDEN TABLE, generated from pivota-agent-ui's REAL `preferSitemapId` by
    # folding it pairwise left-to-right exactly as `mergeDuplicateProduct` does.
    # Each entry is [candidate indices, incumbent indices, expected winner
    # index] into _PARITY_SIGS.
    #
    # This is checked in rather than computed because the live differential
    # below only runs where a pivota-agent-ui worktree happens to exist — i.e.
    # never in CI, and not even locally once the worktree is removed. A parity
    # claim that silently skips is not a gate. Regenerate with the snippet in
    # the live test when preferSitemapId's ordering changes on purpose.
    _PARITY_SIGS = ["sig_" + c * 32 for c in "abc"] + ["sig_" + "d" * 24]
    _PARITY_GOLDEN = json.loads(
        """
        [[[0,1],[],0],[[0,1],[0],0],[[0,1],[1],1],[[0,1],[0,1],0],[[0,2],[],0],[[0,2
        ],[0],0],[[0,2],[2],2],[[0,2],[0,2],0],[[0,3],[],0],[[0,3],[0],0],[[0,3],[3]
        ,3],[[0,3],[0,3],0],[[1,2],[],1],[[1,2],[1],1],[[1,2],[2],2],[[1,2],[1,2],1]
        ,[[1,3],[],1],[[1,3],[1],1],[[1,3],[3],3],[[1,3],[1,3],1],[[2,3],[],2],[[2,3
        ],[2],2],[[2,3],[3],3],[[2,3],[2,3],2],[[0,1,2],[],0],[[0,1,2],[0],0],[[0,1,
        2],[1],1],[[0,1,2],[2],2],[[0,1,2],[0,1],0],[[0,1,2],[0,2],0],[[0,1,2],[1,2]
        ,1],[[0,1,2],[0,1,2],0],[[0,1,3],[],0],[[0,1,3],[0],0],[[0,1,3],[1],1],[[0,1
        ,3],[3],3],[[0,1,3],[0,1],0],[[0,1,3],[0,3],0],[[0,1,3],[1,3],1],[[0,1,3],[0
        ,1,3],0],[[0,2,3],[],0],[[0,2,3],[0],0],[[0,2,3],[2],2],[[0,2,3],[3],3],[[0,
        2,3],[0,2],0],[[0,2,3],[0,3],0],[[0,2,3],[2,3],2],[[0,2,3],[0,2,3],0],[[1,2,
        3],[],1],[[1,2,3],[1],1],[[1,2,3],[2],2],[[1,2,3],[3],3],[[1,2,3],[1,2],1],[
        [1,2,3],[1,3],1],[[1,2,3],[2,3],2],[[1,2,3],[1,2,3],1],[[0,1,2,3],[],0],[[0,
        1,2,3],[0],0],[[0,1,2,3],[1],1],[[0,1,2,3],[2],2],[[0,1,2,3],[3],3],[[0,1,2,
        3],[0,1],0],[[0,1,2,3],[0,2],0],[[0,1,2,3],[0,3],0],[[0,1,2,3],[1,2],1],[[0,
        1,2,3],[1,3],1],[[0,1,2,3],[2,3],2],[[0,1,2,3],[0,1,2],0],[[0,1,2,3],[0,1,3]
        ,0],[[0,1,2,3],[0,2,3],0],[[0,1,2,3],[1,2,3],1],[[0,1,2,3],[0,1,2,3],0]]
        """
    )

    def _golden_cases(self):
        for cand_ix, inc_ix, want_ix in self._PARITY_GOLDEN:
            yield (
                [self._PARITY_SIGS[i] for i in cand_ix],
                {self._PARITY_SIGS[i] for i in inc_ix},
                self._PARITY_SIGS[want_ix],
            )

    def test_parity_with_preferSitemapId_golden(self):
        """Runs EVERYWHERE, including CI. The gate that actually holds.

        The seed's job is to import the sitemap's answer, and "import" is only
        meaningful if the two agree. Hand-reasoning about two orderings in two
        languages is exactly how they drifted: an earlier version of this picker
        disagreed with the JS on 60 of these cases.
        """
        mismatches = [
            (cands, sorted(inc), want, pick_winner(cands, incumbents=inc or None)[0])
            for cands, inc, want in self._golden_cases()
            if pick_winner(cands, incumbents=inc or None)[0] != want
        ]
        assert not mismatches, (
            f"{len(mismatches)}/{len(self._PARITY_GOLDEN)} diverge from "
            f"preferSitemapId: {mismatches[:3]}"
        )

    def test_golden_table_still_matches_the_live_preferSitemapId(self):
        """Guards the GOLDEN itself against agent-ui changing its ordering.

        Skips where no pivota-agent-ui worktree is present (CI, and any machine
        that has not checked it out) — which is precisely why the golden above
        exists and carries the real assertion.
        """
        import shutil
        import subprocess

        node = shutil.which("node")
        lib = "/Users/pengchydan/dev/pa-ui-canonical/scripts/sitemap_lib.mjs"
        if not node or not os.path.exists(lib):
            pytest.skip("needs node + a pivota-agent-ui worktree; golden covers CI")

        cases = [
            {"cands": cands, "inc": sorted(inc)}
            for cands, inc, _ in self._golden_cases()
        ]
        script = (
            f"import {{ preferSitemapId }} from {json.dumps(lib)};"
            "const cases = JSON.parse(process.argv[1]);"
            # Fold pairwise left-to-right — exactly how mergeDuplicateProduct
            # reduces a group of 3+ siblings.
            "console.log(JSON.stringify(cases.map(c =>"
            "  c.cands.reduce((a,b) => preferSitemapId(a,b,new Set(c.inc),'')))));"
        )
        live = json.loads(
            subprocess.run(
                [node, "--input-type=module", "-e", script, json.dumps(cases)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        expected = [want for _, _, want in self._golden_cases()]
        assert live == expected, "preferSitemapId changed — regenerate _PARITY_GOLDEN"

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


class TestTheFixesAreActuallyWIRED:
    """Both round-1 fixes reverted silently with the whole suite green.

    `TestElectableSigGuard` exercises the helper in isolation and
    `sitemap_electable_filter` was only ever compiled through `candidates_query`
    — so swapping the feed's column back to the raw
    `content_canonical_election.c.canonical_sig_id`, or deleting
    `not_tombstoned` outright, changed nothing any test could see. A fix nothing
    asserts is a fix that comes back out on the next refactor.
    """

    # NOTE: the "feed publishes the guarded column" assertion deliberately lives
    # in tests/test_pivota_canonical_routes.py, against the SQL the route
    # actually issues. A version of it here — compiling
    # `_elected_canonical_sig_column` directly — passed even with the route
    # reverted to the raw column, because the helper still existed. Testing the
    # helper proves the helper works, not that anything calls it.

    def test_election_candidates_exclude_tombstoned_rows(self):
        """The BLOCKER fix: never crown a step-5 dedupe loser.

        #1833 points every tombstone at its keeper INSIDE the same content_key.
        If the election could crown the tombstone, the two would canonicalise
        one group in opposite directions and the live keeper would end up
        pointing at the duplicate it replaced.
        """
        for widen in (False, True):
            sql = _compile(candidates_query(widen=widen))
            assert "suppression_reason IS NULL" in sql, (
                f"tombstoned rows are electable (widen={widen})"
            )

    def test_the_guard_also_excludes_tombstones(self):
        """Same rule on the read side, or a pre-existing election survives it."""
        from services.canonical_sitemap_candidates import electable_sig_exists

        sql = _compile(
            select(electable_sig_exists(literal_column("'sig_x'"), widen=False))
        )
        assert "cp_elected.suppression_reason IS NULL" in sql


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
