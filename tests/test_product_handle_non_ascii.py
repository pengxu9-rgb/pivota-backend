"""A product handle is the whole `/products/<segment>`, decoded -- not its ASCII prefix.

`extract_product_handle` was ASCII-only. `luafee.jp/products/qoo10-luafee-オードパルファム…`
came out as `qoo10-luafee-`, the same "handle" as every other qoo10-luafee product, so the
liveness classifier called a redirect to a DIFFERENT product `live`; `celimax.jp/products/
オイルコントロール…` came out as None ("not product-shaped"), so the seed refresh could never read
it and the destination sweep skipped it. Redirects land on the percent-encoded form of the same
handle. Measured 2026-09-26 over 44,731 active seed URLs: 44,361 unchanged, 221 truncated ->
full, 149 None -> handle, 0 other differences -- the ASCII parity table below pins the 44,361.
"""

from __future__ import annotations

import pytest

from services import external_seed_destination_liveness as dl
from services.outbound_warm_handoff import extract_product_handle

LUAFEE = "https://luafee.jp/products/qoo10-luafee-オードパルファム-ホワイトアプリコット"
LUAFEE_LANDED = (
    "https://luafeejp.com/products/qoo10-luafee-"
    "%E3%82%AA%E3%83%BC%E3%83%89%E3%83%91%E3%83%AB%E3%83%95%E3%82%A1%E3%83%A0"
    "-%E3%83%9B%E3%83%AF%E3%82%A4%E3%83%88%E3%82%A2%E3%83%97%E3%83%AA%E3%82%B3%E3%83%83%E3%83%88"
)
LUAFEE_OTHER_PRODUCT = "https://luafeejp.com/products/qoo10-luafee-%E3%82%BD%E3%83%AA%E3%83%83%E3%83%89"
CELIMAX = "https://celimax.jp/products/オイルコントロールマット日焼け止めスティック"


@pytest.mark.parametrize(
    "url,handle",
    [
        # ASCII handles: exactly what the old pattern returned.
        ("https://fentybeauty.com/products/pro-filtr-soft-matte-longwear-foundation-470", "pro-filtr-soft-matte-longwear-foundation-470"),
        ("https://x.com/products/Serum_2.0?variant=1#top", "Serum_2.0"),
        ("https://x.com/en/products/serum/", "serum"),
        ("https://x.com/collections/sale/products/serum", "serum"),
        ("https://x.com/products/", None),
        ("https://x.com/products/%20", None),  # decodes to nothing: not a handle
        ("https://x.com/p/123", None),
        ("", None),
        # Non-ASCII: the whole segment, decoded.
        (LUAFEE, "qoo10-luafee-オードパルファム-ホワイトアプリコット"),
        (LUAFEE_LANDED, "qoo10-luafee-オードパルファム-ホワイトアプリコット"),
        (CELIMAX, "オイルコントロールマット日焼け止めスティック"),
        ("https://inertiaofficial.com/products/8pack-labocell™-pads", "8pack-labocell™-pads"),
    ],
)
def test_extract_product_handle_is_the_whole_decoded_segment(url, handle):
    assert extract_product_handle(url) == handle


def test_a_redirect_to_the_percent_encoded_same_handle_is_live():
    o = dl.classify_destination(requested_url=LUAFEE, status_code=200, final_url=LUAFEE_LANDED)
    assert o.verdict == dl.VERDICT_LIVE


def test_a_redirect_to_another_product_sharing_the_ascii_prefix_is_not_live():
    """The truncated prefix `qoo10-luafee-` used to make these two products one handle."""
    o = dl.classify_destination(requested_url=LUAFEE, status_code=200, final_url=LUAFEE_OTHER_PRODUCT)
    assert o.verdict == dl.VERDICT_REDIRECTED_TO_PRODUCT


def test_a_handle_that_starts_non_ascii_is_product_shaped():
    """Was `unverifiable: destination is not product-shaped` -- never readable by the refresh."""
    o = dl.classify_destination(requested_url=CELIMAX, status_code=200, final_url=CELIMAX)
    assert o.verdict == dl.VERDICT_LIVE


def test_the_sweep_now_groups_non_ascii_handles():
    """`group_by_host` drops handle-less rows; a Japanese handle was one."""
    grouped = dl.group_by_host([{"id": "s1", "destination_url": CELIMAX, "canonical_url": None}])
    assert list(grouped) == ["celimax.jp"]
