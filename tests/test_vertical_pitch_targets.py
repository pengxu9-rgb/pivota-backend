"""Phase-4 T5: the vertical's standing authority-host pitch list, surfaced in
where_youre_losing.pitch_targets and status-stamped against the audit.

_outreach_moves only names hosts the engines happened to cite in one run's
sample; the electronics partner deliverable needs the category's pitch targets
(Rtings/SoundGuys/What Hi-Fi/Wirecutter...) visible regardless — that's the
vertical profile's authority_hosts, which until now never reached the
merchant-facing payload.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from services.merchant_narrative_builder import (  # noqa: E402
    _vertical_pitch_targets,
    _where_youre_losing,
)
from services.vertical_profiles import BEAUTY_PROFILE, get_profile  # noqa: E402

ELECTRONICS = get_profile("electronics")


def test_electronics_profile_hosts_become_pitch_targets():
    targets = _vertical_pitch_targets(ELECTRONICS, {}, [])
    hosts = [t["host"] for t in targets]
    assert set(hosts) == set(ELECTRONICS.authority_hosts)
    assert all(t["status"] == "not_yet_observed" for t in targets)
    assert all(t["first_move"] for t in targets)


def test_status_stamped_from_audit_observations():
    who = {"cited_hosts": [{"host": "rtings.com"}]}
    targets = _vertical_pitch_targets(ELECTRONICS, who, ["wirecutter.com"])
    by_host = {t["host"]: t for t in targets}
    assert by_host["wirecutter.com"]["status"] == "already_endorses_you"
    assert by_host["rtings.com"]["status"] == "cited_in_your_category"
    assert by_host["soundguys.com"]["status"] == "not_yet_observed"
    # observed targets sort ahead of not-yet-observed ones
    statuses = [t["status"] for t in targets]
    assert statuses.index("already_endorses_you") < statuses.index("not_yet_observed")
    assert statuses.index("cited_in_your_category") < statuses.index("not_yet_observed")


def test_classifier_metadata_flows_through():
    targets = _vertical_pitch_targets(ELECTRONICS, {}, [])
    rtings = next(t for t in targets if t["host"] == "rtings.com")
    # Phase-1b/1c wiring: profile authority hosts classify as editorial with a
    # grounding weight — the panel can render tier/cadence chips from this.
    assert rtings["ai_grounding_weight"] == "medium"


def test_beauty_profile_without_authority_hosts_is_empty():
    assert _vertical_pitch_targets(BEAUTY_PROFILE, {}, []) == []


def test_where_youre_losing_carries_pitch_targets():
    out = _where_youre_losing(
        "Mojawa", {}, {"endorsement_hosts": [], "findability_hosts": []},
        vertical_profile=ELECTRONICS,
    )
    assert len(out["pitch_targets"]) == len(ELECTRONICS.authority_hosts)
    # beauty (no curated list) keeps the key with an empty list — stable shape
    out_beauty = _where_youre_losing(
        "Anuko", {}, {"endorsement_hosts": [], "findability_hosts": []},
        vertical_profile=BEAUTY_PROFILE,
    )
    assert out_beauty["pitch_targets"] == []
