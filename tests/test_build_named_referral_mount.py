"""The merged web-mount builder: exact bytes, under the cap, proven with the real serving loader (placeholders only)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.ops import build_named_referral_mount as mount
from services import external_seed_review_pilot as pilot
from tests.test_reviewed_seed_batch import data, entry_raw, gtin14, manifest

PUBLIC = "eps_public"
RELEASE = "a" * 40


@pytest.fixture(autouse=True)
def this_tree_is_the_release(monkeypatch):
    monkeypatch.setattr(mount, "release_of_this_tree", lambda: RELEASE)


def build(current, batch_manifest, listings=None):
    """The builder with the right pins: web's live sha of `current` and this tree's release."""
    return mount.build(current, batch_manifest, listings, current_sha256=hashlib.sha256(current).hexdigest(),
                       web_release=RELEASE)


def pins(current_path):
    return ["--current-sha256", hashlib.sha256(Path(current_path).read_bytes()).hexdigest(), "--web-release", RELEASE]


def public_listings():
    return {PUBLIC: pilot.PilotListing(
        "https://brand.example/products/tinted-balm", "merch_obs_example", 2500, "Example Brand", "Tinted Balm",
        "A tinted balm.", (("900000000001", gtin14("0009000000001"), "Shade 1", "PB-01"),), 1)}


def live_v1(n=13):
    """The shape web mounts today: v1, additions on one public seed."""
    return data({"schema_version": 1, "listings": {PUBLIC: {
        f"90000000{i + 2:04}": [gtin14(f"000900000{i + 2:04}"), f"PB-{i + 2:02}"] for i in range(n)}}})


def big_entry(n):
    """The largest entry the writer accepts: 40 existing + 40 additions, 80-char shades, 64-char SKUs, 24-digit ids."""
    raw = entry_raw(n, additions=0)

    def row(i):
        return [f"{n % 9 + 1}{n:03}{i:020}", gtin14(f"000{n:03}{i:07}"), "S" * 76 + f"{i:04}", f"X{n:03}{i:04}" + "Y" * 56]

    raw["existing"] = [row(i) for i in range(1, 41)]
    raw["additions"] = [row(i) for i in range(41, 81)]
    return raw


def batch_of(*entries):
    return data(manifest(*entries))


def test_a_batch_merges_onto_the_live_mount_and_the_real_loader_keeps_every_live_pin(tmp_path):
    listings = public_listings()
    out = build(live_v1(), batch_of(entry_raw(1), entry_raw(2, additions=3)), listings)
    document = json.loads(out)
    assert document["schema_version"] == 2 and set(document["listings"]) == {PUBLIC, f"eps_{1:024x}", f"eps_{2:024x}"}
    assert out == json.dumps(document, sort_keys=True, separators=(",", ":")).encode()      # canonical, compact
    additions, private = mount.load_through_the_real_loader(out, listings)
    live, _ = mount.load_through_the_real_loader(live_v1(), listings)
    assert additions[PUBLIC] == live[PUBLIC] and len(live[PUBLIC]) == 13
    assert set(private) == {f"eps_{1:024x}", f"eps_{2:024x}"}


def test_a_worst_case_batch_fits_once_beside_the_live_mount():
    """5 of the largest entries, next to the live 13 shades: ~63 KB, so it fits once; the next one cannot."""
    out = build(live_v1(), batch_of(*(big_entry(n) for n in range(1, 6))), public_listings())
    assert 60000 < len(out) <= pilot._MANIFEST_LIMIT
    assert len(mount.load_through_the_real_loader(out, public_listings())[0]) == 6


def test_an_oversized_merge_is_refused_before_anything_is_written(tmp_path, monkeypatch, capsys):
    listings = public_listings()
    monkeypatch.setattr(pilot, "LISTINGS", listings)
    current = build(live_v1(), batch_of(*(big_entry(n) for n in range(1, 6))), listings)
    while True:      # grow the live mount with earlier batches until the next maximal batch cannot fit
        base = 10 * (len(json.loads(current)["listings"]) // 5 + 1)
        try:
            current = build(current, batch_of(*(big_entry(base + n) for n in range(5))), listings)
        except mount.Refused as error:
            assert "over the loader's 65536" in str(error) and "split the batch" in str(error)
            break
    out = tmp_path / "out.json"
    (tmp_path / "cur.json").write_bytes(current)
    (tmp_path / "b.json").write_bytes(batch_of(*(big_entry(900 + n) for n in range(5))))
    rc = mount.main(["--current", str(tmp_path / "cur.json"), "--batch", str(tmp_path / "b.json"), "--out", str(out), *pins(tmp_path / "cur.json")])
    assert rc == 3 and not out.exists() and "over the loader's 65536" in capsys.readouterr().out


@pytest.mark.parametrize(("case", "message"), [
    ("current_refused", "current web mount does not validate"),
    ("seed_already_live", "already in the web mount"),
    ("bad_batch", "batch manifest is refused"),
])
def test_refusals(case, message):
    listings = public_listings()
    current, batch = live_v1(), batch_of(entry_raw(1))
    if case == "current_refused":
        current = data({"schema_version": 1, "listings": {"eps_unknown": {"1": ["x", "y"]}}})
    elif case == "seed_already_live":
        current = build(current, batch, listings)
    elif case == "bad_batch":
        batch = b"{}"
    with pytest.raises(mount.Refused, match=message):
        build(current, batch, listings)


def test_a_merge_the_real_loader_would_refuse_is_never_ok(monkeypatch):
    """If the validator and the loader ever disagree, the loader (what web runs) decides."""
    listings = public_listings()
    real = mount.load_through_the_real_loader
    monkeypatch.setattr(mount, "load_through_the_real_loader",
                        lambda raw, l: ({}, {}) if b'"schema_version":2' in raw else real(raw, l))
    with pytest.raises(mount.Refused, match="refuses the merged web mount"):
        build(live_v1(), batch_of(entry_raw(1)), listings)


def test_a_merge_that_changes_a_live_pin_is_refused(monkeypatch):
    listings = public_listings()
    real = mount.load_through_the_real_loader

    def drop_one_live_pin(raw, l):
        additions, private = real(raw, l)
        if b'"schema_version":2' in raw:
            additions = {**additions, PUBLIC: dict(list(additions[PUBLIC].items())[1:])}
        return additions, private

    monkeypatch.setattr(mount, "load_through_the_real_loader", drop_one_live_pin)
    with pytest.raises(mount.Refused, match="changes a live seed's pins"):
        build(live_v1(), batch_of(entry_raw(1)), listings)


def test_main_writes_exactly_the_verified_bytes_and_prints_their_sha(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(pilot, "LISTINGS", public_listings())
    (tmp_path / "cur.json").write_bytes(live_v1())
    (tmp_path / "b.json").write_bytes(batch_of(entry_raw(1)))
    out = tmp_path / "out.json"
    rc = mount.main(["--current", str(tmp_path / "cur.json"), "--batch", str(tmp_path / "b.json"), "--out", str(out), *pins(tmp_path / "cur.json")])
    assert rc == 0
    raw = out.read_bytes()
    assert capsys.readouterr().out.strip() == f"NAMED_MOUNT_OK size={len(raw)} sha256={hashlib.sha256(raw).hexdigest()}"
    assert mount.main(["--current", str(tmp_path / "cur.json"), "--batch", str(tmp_path / "b.json"),
                       "--out", str(out), *pins(tmp_path / "cur.json")]) == 3                                                 # never overwrites


def test_a_merge_that_drops_a_batch_pin_is_refused(monkeypatch):
    listings = public_listings()
    real = mount.load_through_the_real_loader
    seed = f"eps_{1:024x}"

    def drop_one_batch_pin(raw, l):
        additions, private = real(raw, l)
        if seed in additions:
            additions = {**additions, seed: dict(list(additions[seed].items())[1:])}
        return additions, private

    monkeypatch.setattr(mount, "load_through_the_real_loader", drop_one_batch_pin)
    with pytest.raises(mount.Refused, match="does not pin exactly this batch's additions"):
        build(live_v1(), batch_of(entry_raw(1)), listings)


def test_a_current_mount_the_real_loader_refuses_is_never_built_on(monkeypatch):
    """The validator accepts it, but web's loader (sha pin, size, file read) would not: stop before merging onto it."""
    listings = public_listings()
    real = mount.load_through_the_real_loader
    monkeypatch.setattr(mount, "load_through_the_real_loader",
                        lambda raw, l: ({}, {}) if b'"schema_version": 1' in raw else real(raw, l))
    with pytest.raises(mount.Refused, match="refuses the current web mount"):
        build(live_v1(), batch_of(entry_raw(1)), listings)


def test_an_output_created_after_the_check_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "LISTINGS", public_listings())
    (tmp_path / "cur.json").write_bytes(live_v1())
    (tmp_path / "b.json").write_bytes(batch_of(entry_raw(1)))
    out = tmp_path / "out.json"
    out.write_bytes(b"someone else's file")
    monkeypatch.setattr(mount.Path, "exists", lambda self: False)        # the race: it appears after the check
    rc = mount.main(["--current", str(tmp_path / "cur.json"), "--batch", str(tmp_path / "b.json"), "--out", str(out), *pins(tmp_path / "cur.json")])
    assert rc == 3 and out.read_bytes() == b"someone else's file"


@pytest.mark.parametrize(("case", "message"), [
    ("stale_current", "not the one web mounts"),
    ("sha_not_hex", "web's live 64-hex manifest pin"),
    ("other_release", "not web's release"),
    ("release_not_hex", "40-hex release"),
])
def test_the_current_file_and_this_tree_must_be_webs(case, message):
    current, batch = live_v1(), batch_of(entry_raw(1))
    sha, release = hashlib.sha256(current).hexdigest(), RELEASE
    if case == "stale_current":
        sha = hashlib.sha256(live_v1(12)).hexdigest()        # web mounts a different (newer or older) file
    elif case == "sha_not_hex":
        sha = sha.upper()
    elif case == "other_release":
        release = "b" * 40
    elif case == "release_not_hex":
        release = "latest"
    with pytest.raises(mount.Refused, match=message):
        mount.build(current, batch, public_listings(), current_sha256=sha, web_release=release)


def test_main_refuses_a_current_file_that_is_not_webs(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(pilot, "LISTINGS", public_listings())
    (tmp_path / "cur.json").write_bytes(live_v1())
    (tmp_path / "b.json").write_bytes(batch_of(entry_raw(1)))
    out = tmp_path / "out.json"
    rc = mount.main(["--current", str(tmp_path / "cur.json"), "--batch", str(tmp_path / "b.json"), "--out", str(out),
                     "--current-sha256", "0" * 64, "--web-release", RELEASE])
    assert rc == 3 and not out.exists() and "not the one web mounts" in capsys.readouterr().out


def test_release_of_this_tree_is_a_clean_heads_commit(monkeypatch, tmp_path):
    monkeypatch.undo()          # the real function, not the autouse stand-in
    import subprocess

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args[3:])
        out = {"rev-parse": "c" * 40 + "\n", "status": ""}[args[3]]
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(mount.subprocess, "run", fake_run)
    assert mount.release_of_this_tree() == "c" * 40
    assert ["status", "--porcelain", "--", "services", "scripts", "routes"] in calls

    def dirty_run(args, **kwargs):
        out = {"rev-parse": "c" * 40, "status": " M services/external_seed_review_pilot.py"}[args[3]]
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(mount.subprocess, "run", dirty_run)
    with pytest.raises(mount.Refused, match="local changes"):
        mount.release_of_this_tree()
