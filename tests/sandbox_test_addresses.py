"""Stand-in addresses for the sandbox tests.

The repo is public: prod's and staging's private IPs and the sandbox's private range are held only
as sha256 digests (config/sandbox_guard.py, config/sandbox_identity.py). The tests swap in digests of
documentation-range addresses (RFC 5737) and use those, so no real address is written in a test.
Import ``stand_in_addresses`` into a test module to apply it to every test there (autouse)."""

from __future__ import annotations

import hashlib
import ipaddress

import pytest

from config import sandbox_guard as _guard
from config import sandbox_identity as _identity

#: The REAL digests, read before any test swaps them (for the shape and the no-real-address checks).
REAL_PROD_IP_SHA256 = _guard._PROD_PRIVATE_IP_SHA256
REAL_SANDBOX_DB_NETWORKS_SHA256 = _identity.SANDBOX_DB_NETWORKS_SHA256

#: The stand-in sandbox range: a /25 inside TEST-NET-2, so an address can sit just outside it.
SANDBOX_NETWORK = "198.51.100.0/25"
SANDBOX_IP = "198.51.100.5"
SANDBOX_IP_EDGE = "198.51.100.126"
OUTSIDE_SANDBOX_IP = "198.51.100.200"
#: The stand-ins for prod's Cloud SQL and Redis and staging's Cloud SQL (TEST-NET-1).
PROD_DB_IP = "192.0.2.25"
PROD_REDIS_IP = "192.0.2.196"
STAGING_DB_IP = "192.0.2.3"
#: A private-looking address that is neither the sandbox's nor prod's (TEST-NET-3).
OTHER_IP = "203.0.113.70"


def ip_sha256(ip: str) -> str:
    return hashlib.sha256(str(ipaddress.ip_address(ip)).encode("ascii")).hexdigest()


def network_sha256(network: str) -> str:
    return hashlib.sha256(str(ipaddress.ip_network(network)).encode("ascii")).hexdigest()


def use_stand_ins(monkeypatch) -> None:
    net = ipaddress.ip_network(SANDBOX_NETWORK)
    monkeypatch.setattr(_identity, "SANDBOX_DB_NETWORKS_SHA256", ((net.prefixlen, network_sha256(SANDBOX_NETWORK)),))
    monkeypatch.setattr(_guard, "_PROD_PRIVATE_IP_SHA256",
                        frozenset(ip_sha256(ip) for ip in (PROD_DB_IP, PROD_REDIS_IP, STAGING_DB_IP)))


@pytest.fixture(autouse=True)
def stand_in_addresses(monkeypatch):
    use_stand_ins(monkeypatch)
    yield
