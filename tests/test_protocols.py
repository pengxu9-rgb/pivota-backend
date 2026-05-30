from services.protocols import validate_protocol


def test_validate_protocol_accepts_default() -> None:
    assert validate_protocol("pdp_direct") == "pdp_direct"


def test_validate_protocol_accepts_reserved_identifier() -> None:
    assert validate_protocol("ucp_session") == "ucp_session"


def test_validate_protocol_fails_open_for_unknown() -> None:
    assert validate_protocol("bogus") == "pdp_direct"


def test_validate_protocol_fails_open_for_none() -> None:
    assert validate_protocol(None) == "pdp_direct"
