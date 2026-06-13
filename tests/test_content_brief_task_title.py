"""content_brief task titles are target-specific (the duplicate-row fix).

Pre-fix, the executor built its task title/body from b.get("query"), but the
brief parser stores the query under "target_query". So every run fell back to a
generic count-only title ("Write N content briefs for failed category
visibility queries"), making distinct runs look like duplicate rows and
defeating the (merchant_id, lever, title) supersession identity.

These tests pin the fix: titles name their real target_query, single vs multi
phrasing is distinct, and the generic fallback only fires when no query is
present.
"""

from __future__ import annotations

from services.executor_agents.content_brief import (
    _brief_target_queries,
    _content_brief_task_title,
)


def test_reads_target_query_not_query():
    briefs = [
        {"target_query": "best vegan collagen", "query": "WRONG_KEY"},
        {"target_query": "collagen for sleep"},
    ]
    assert _brief_target_queries(briefs) == [
        "best vegan collagen",
        "collagen for sleep",
    ]


def test_single_brief_title_names_the_query():
    title = _content_brief_task_title([{"target_query": "best vegan collagen"}])
    assert title == "Content brief: best vegan collagen"


def test_multi_brief_title_lists_queries():
    briefs = [
        {"target_query": "best vegan collagen"},
        {"target_query": "collagen for sleep"},
    ]
    assert _content_brief_task_title(briefs) == (
        "2 content briefs: best vegan collagen, collagen for sleep"
    )


def test_three_plus_briefs_summarize_with_more():
    briefs = [
        {"target_query": "q one"},
        {"target_query": "q two"},
        {"target_query": "q three"},
    ]
    assert _content_brief_task_title(briefs) == (
        "3 content briefs: q one, q two, +1 more"
    )


def test_distinct_query_sets_produce_distinct_titles():
    # The core of the duplicate-row bug: different failing queries must not
    # collapse to the same title.
    a = _content_brief_task_title([{"target_query": "alpha"}])
    b = _content_brief_task_title([{"target_query": "beta"}])
    assert a != b


def test_no_query_falls_back_to_generic_title():
    # Briefs present but without a usable target_query (defensive) — still get a
    # non-crashing generic title, never an empty string.
    title = _content_brief_task_title([{"suggested_title": "x"}])
    assert "content brief" in title.lower()
    assert title.strip()
