"""scripts/ops/reprobe_retired_seed_destinations.py: observe-only, paced, stops on the first breaker trip."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from scripts.ops import reprobe_retired_seed_destinations as tool
# `_clean` (autouse there) resets the politeness, breaker and pacer state and zeroes the per-host waits.
from tests.test_external_seed_destination_sweep_global_pacing import VirtualClock, _clean, vclock  # noqa: F401

NOTE = "auto-retired 2026-10-15: destination redirected_off_product (http 200)"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "false")
    for name in ("CRAWL_IP_CHALLENGE_TRIP_HOSTS", "CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS", "CRAWL_GLOBAL_PACER_DISABLED"):
        monkeypatch.delenv(name, raising=False)


def _targets(urls: List[str]) -> List[Dict[str, Any]]:
    return [{"id": f"s{i}", "url": u, "notes": NOTE} for i, u in enumerate(urls)]


def _client(seen: List[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        path = request.url.path
        if path.endswith("gone"):
            return httpx.Response(404, request=request)
        if path.endswith("moved"):
            return httpx.Response(302, headers={"location": f"https://{request.url.host}/collections/all"},
                                  request=request)
        if path.endswith("cf"):
            return httpx.Response(403, headers={"cf-mitigated": "challenge"}, request=request)
        return httpx.Response(200, content=b"<html></html>", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_each_target_is_probed_once_and_classified_by_the_sweeps_rules(vclock, capsys) -> None:  # noqa: F811
    seen: List[str] = []
    urls = [f"https://shop.example/products/{p}" for p in ("alive", "gone", "moved", "cf")]
    summary = asyncio.run(tool.reprobe(_targets(urls), client=_client(seen)))
    assert summary["verdicts"] == {"live": 1, "dead_404": 1, "redirected_off_product": 1, "unverifiable": 1}
    assert summary["probed"] == 4 and summary["stopped"] is None
    assert [u for u in seen if "/products/" in u] == urls, "one request per target"
    out = capsys.readouterr().out
    assert '"retired_as": "redirected_off_product"' in out and '"final_url": "https://shop.example/collections/all"' in out


def test_the_first_breaker_trip_stops_the_run(vclock, monkeypatch, capsys) -> None:  # noqa: F811
    """A one-off on a freshly swapped address does not ride out a cool-down: any trip stops it."""
    monkeypatch.setenv("CRAWL_IP_CHALLENGE_TRIP_HOSTS", "2")
    seen: List[str] = []
    urls = [f"https://h{i}.example/products/cf" for i in range(5)]
    summary = asyncio.run(tool.reprobe(_targets(urls), client=_client(seen)))
    assert summary["probed"] == 2 and summary["stopped"]
    assert len(seen) == 2, "nothing is sent after the trip"
    assert '"rp": "stopped"' in capsys.readouterr().out


def test_requests_are_paced_globally(vclock) -> None:  # noqa: F811
    seen: List[str] = []
    times: List[float] = []
    client = _client(seen)
    real_send = client.send

    async def timed_send(request, **kwargs):
        times.append(vclock.t)
        return await real_send(request, **kwargs)

    client.send = timed_send
    urls = [f"https://h{i}.example/products/alive" for i in range(3)]
    asyncio.run(tool.reprobe(_targets(urls), client=client))
    assert [round(b - a, 3) for a, b in zip(times, times[1:])] == [2.0, 2.0], "0.5 requests/s"


@pytest.mark.parametrize("value, parsed", [
    ("2026-10-15:redirected_off_product", ("2026-10-15", "redirected_off_product", None)),
    ("2026-10-14:dead_404:www.Addros.com", ("2026-10-14", "dead_404", "addros.com")),
])
def test_select_parses(value, parsed) -> None:
    assert tool.parse_select(value) == parsed


@pytest.mark.parametrize("value", ["2026-10-15", "15-10-2026:dead_404", "2026-10-15:dead 404",
                                   "2026-10-15:dead_404:a/b", "2026-10-15:dead_404:x:y", "2026-10-15:'; drop"])
def test_select_rejects_anything_else(value) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        tool.parse_select(value)


def test_a_selection_over_the_cap_is_refused_before_any_fetch(monkeypatch, capsys) -> None:
    async def fake_load(selections):
        return _targets([f"https://h{i}.example/products/x" for i in range(5)])

    async def no_probe(*a, **k):
        raise AssertionError("fetched despite the refusal")

    monkeypatch.setattr(tool, "load_targets", fake_load)
    monkeypatch.setattr(tool, "reprobe", no_probe)
    rc = asyncio.run(tool.main(["--select", "2026-10-15:redirected_off_product", "--max-targets", "4"]))
    assert rc == 2 and '"rp": "refused"' in capsys.readouterr().out


def test_it_writes_nothing() -> None:
    """The only SQL is the SELECT, and nothing reaches the sweep's write paths."""
    src = Path(tool.__file__).read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "record_destination_observation(",
                      "retire_seed_for_dead_destination(", "execute(\"UPDATE", "status = 'active'"):
        assert forbidden not in code, forbidden
    assert tool.SELECT_SQL.lstrip().upper().startswith("SELECT")


def test_it_runs_without_a___file__() -> None:
    """run_oneoff_job.sh -c execs the source with no __file__ (it broke the repair script once)."""
    src = Path(tool.__file__).read_text()
    glb: Dict[str, Any] = {"__name__": "reprobe_exec"}
    cwd = os.getcwd()
    try:
        os.chdir(Path(tool.__file__).resolve().parents[2])
        exec(compile(src, "<string>", "exec"), glb)  # noqa: S102
    finally:
        os.chdir(cwd)
    assert callable(glb["reprobe"]) and glb["SELECT_SQL"] == tool.SELECT_SQL
