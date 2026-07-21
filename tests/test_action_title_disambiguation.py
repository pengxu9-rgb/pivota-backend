"""Page-usability Step 1: per-product task titles are disambiguated with the
product name, so brand-style actions emitted once per SKU (e.g. "Index your
canonical PDPs") don't render as N identical-looking rows. Titles that already
name the product are left alone.
"""

from services.task_queue_service import _extract_action_items


def _report():
    return {
        "per_product": [
            {
                "product_key": "pk-A",
                "product": {"title": "Good Night Collagen"},
                "merchant_view": {"actions": [
                    {"title": "Index your canonical PDPs with Google Search Console",
                     "lever": "indexing_acceleration", "severity": "critical"},
                    {"title": "Fill the gaps on Good Night Collagen's page",
                     "lever": "content_revision"},
                ]},
            },
            {
                "product_key": "pk-B",
                "product": {"title": "Triple Shine Grape"},
                "merchant_view": {"actions": [
                    {"title": "Index your canonical PDPs with Google Search Console",
                     "lever": "indexing_acceleration", "severity": "critical"},
                ]},
            },
        ]
    }


def test_generic_per_product_titles_get_product_name():
    items = {it["title"]: it for it in _extract_action_items(_report())}
    # the two indexing tasks now read as distinct, product-named rows
    assert "Index your canonical PDPs with Google Search Console — Good Night Collagen" in items
    assert "Index your canonical PDPs with Google Search Console — Triple Shine Grape" in items
    # they remain distinct tasks (different product_key)
    assert items[
        "Index your canonical PDPs with Google Search Console — Good Night Collagen"
    ]["evidence"]["product_key"] == "pk-A"


def test_title_already_naming_product_is_unchanged():
    items = {it["title"]: it for it in _extract_action_items(_report())}
    # content-gap action already names the product -> not double-named
    assert "Fill the gaps on Good Night Collagen's page" in items
    assert not any(
        t.startswith("Fill the gaps") and t.count("Good Night Collagen") > 1
        for t in items
    )


def test_no_product_name_leaves_title_generic():
    report = {"per_product": [{
        "product_key": "pk-x",
        "product": {},  # no title
        "merchant_view": {"actions": [
            {"title": "Index your canonical PDPs", "lever": "indexing_acceleration"}
        ]},
    }]}
    titles = [it["title"] for it in _extract_action_items(report)]
    assert titles == ["Index your canonical PDPs"]  # untouched, no em-dash suffix
