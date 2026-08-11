"""
Cross-repo protocol vocabulary — digest-enforced (the content_key #1696/#1942 mechanism).

The gateway (PIVOTA-Agent) writes ``protocol_name`` onto backend orders and the
backend's kill switch gates charges on that label. Nothing except convention kept
those vocabularies aligned; a rename on either side would silently un-gate (or
dead-end) a protocol lane. ``contracts/protocol_vocabulary_v1.json`` is now the
one shared statement, mirrored byte-identical into both repos, and BOTH repos pin
its sha256: regenerating it breaks both CIs until it is consciously re-mirrored.
"""
import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = REPO_ROOT / "contracts" / "protocol_vocabulary_v1.json"

# Pinned in BOTH repos (PIVOTA-Agent: tests/protocol_vocabulary_contract.test.js).
CORPUS_SHA256 = "e4c19342d5ff149de85ed8b5c457cc429fa36eb418dd5fb7af3ab1f20463e1b0"


def _corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text())


def test_corpus_is_byte_for_byte_the_mirrored_one():
    digest = hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest()
    assert digest == CORPUS_SHA256, (
        "contracts/protocol_vocabulary_v1.json changed. Update CORPUS_SHA256 here "
        "AND mirror the file + new digest into PIVOTA-Agent "
        "tests/protocol_vocabulary_contract.test.js in the same change set."
    )


def test_kill_switch_guarded_set_matches_the_corpus():
    from services.agent_checkout_kill_switch import GUARDED_PROTOCOLS

    assert GUARDED_PROTOCOLS == frozenset(_corpus()["guarded_protocols"])


def test_protocol_labels_match_the_corpus():
    from services.protocols import DEFAULT_PROTOCOL, KNOWN_PROTOCOLS

    corpus = _corpus()
    assert KNOWN_PROTOCOLS == frozenset(corpus["known_protocol_labels"])
    assert DEFAULT_PROTOCOL == corpus["default_protocol"]


def test_gateway_delegated_lane_is_a_guarded_protocol():
    corpus = _corpus()
    # The gateway's SPT delegated lane stamps this protocol_name onto backend
    # orders; if it ever left the guarded set, those charges would bypass the
    # Tier-2 kill switch entirely.
    assert corpus["gateway_delegated_lane_protocol"] in set(corpus["guarded_protocols"])
