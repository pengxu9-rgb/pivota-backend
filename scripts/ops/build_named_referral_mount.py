"""Build the EXACT named-referral manifest bytes web will mount for a batch, and prove them with the real loader.

Web mounts one file for every seed (the live ones and the batch's). If those bytes are too large or refused, the
serving loader disables shade pinning for EVERY seed, the live ones included. So nothing is uploaded unless this
script has passed on the very bytes that go up:

    python3 -I scripts/ops/build_named_referral_mount.py --current <file web mounts now> \\
        --batch <batch manifest> --out <file to upload>

It prints `NAMED_MOUNT_OK size=<n> sha256=<hex>` and exits 0 only when:
- the current file loads (non-empty) through the serving loader;
- no batch seed is already in it (extending a live seed is not supported: the plan pins each entry's additions
  exactly);
- the merged bytes fit the loader's cap;
- the real loader, reading the written file under its sha pin, returns every current pin unchanged plus exactly
  this batch's pins and listings.

Otherwise it prints `NAMED_MOUNT_REFUSED: <reason>` and exits 3. It reads only local files; it never calls a
network service. Upload exactly the written file (`gcloud secrets versions add ... --data-file <out>`), then check
the printed sha against the new version's.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services import external_seed_review_pilot as pilot  # noqa: E402
from services import reviewed_seed_batch as batch  # noqa: E402


class Refused(Exception):
    pass


def require(condition, message: str) -> None:
    if not condition:
        raise Refused(message)


def _validated(raw: bytes, listings) -> tuple[dict, dict, dict]:
    """(document, additions, private listings) for bytes the loader's own validator accepts, else Refused."""
    try:
        document = json.loads(raw, object_pairs_hook=pilot._unique_object)
        additions, private = pilot.validate_named_referral_manifest(document, listings)
    except (ValueError, RecursionError) as error:
        raise Refused(f"the current web mount does not validate: {error}") from None
    return document, additions, private


def merge(current: bytes, entries, listings) -> bytes:
    """The merged schema-v2 bytes: every current seed unchanged, plus the batch's seeds. Canonical and compact."""
    document, _, _ = _validated(current, listings)
    if document["schema_version"] == 1:
        merged = {seed_id: {"additions": additions} for seed_id, additions in document["listings"].items()}
    else:
        merged = dict(document["listings"])
    addition = batch.named_referral_manifest(entries, listings)["listings"]
    clash = sorted(set(addition) & set(merged))
    require(not clash, f"a batch seed is already in the web mount: {', '.join(clash)}")
    merged.update(addition)
    out = json.dumps({"schema_version": 2, "listings": merged}, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode()
    require(len(out) <= pilot._MANIFEST_LIMIT,
            f"the merged web mount is {len(out)} bytes, over the loader's {pilot._MANIFEST_LIMIT}: split the batch")
    return out


def load_through_the_real_loader(raw: bytes, listings) -> tuple[dict, dict]:
    """What web would load from exactly these bytes: the serving loader, reading a file, under its sha pin."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "named.json"
        path.write_bytes(raw)
        saved = {key: os.environ.get(key) for key in (pilot.NAMED_REFERRAL_VARIANTS_MANIFEST,
                                                      pilot.NAMED_REFERRAL_VARIANTS_MANIFEST_SHA256)}
        os.environ[pilot.NAMED_REFERRAL_VARIANTS_MANIFEST] = str(path)
        os.environ[pilot.NAMED_REFERRAL_VARIANTS_MANIFEST_SHA256] = hashlib.sha256(raw).hexdigest()
        try:
            return pilot.load_named_referral_manifest(listings)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def verify(out: bytes, current: bytes, entries, listings) -> None:
    """The real loader on the exact output keeps every current pin and adds exactly the batch's."""
    _, current_additions, current_private = _validated(current, listings)
    require(current_additions, "the current web mount pins nothing")
    require(load_through_the_real_loader(current, listings) == (current_additions, current_private),
            "the serving loader refuses the current web mount")
    additions, private = load_through_the_real_loader(out, listings)
    require(additions, "the serving loader refuses the merged web mount (it would disable every shade pin)")
    for seed_id, pins in current_additions.items():
        require(additions.get(seed_id) == pins, f"the merged mount changes a live seed's pins: {seed_id}")
    for seed_id, listing in current_private.items():
        require(private.get(seed_id) == listing, f"the merged mount changes a live seed's listing: {seed_id}")
    for entry in entries:
        require(additions.get(entry.seed_id) == {n: (g, s) for n, g, _, s in entry.additions},
                f"the merged mount does not pin exactly this batch's additions: {entry.seed_id}")
        if entry.seed_id not in listings:
            listing = private.get(entry.seed_id)
            require(listing is not None and (listing.url, listing.seller_ref, listing.native_product_id,
                                             listing.variants) ==
                    (entry.url, entry.seller_ref, entry.native_product_id, entry.existing),
                    f"the merged mount does not carry this batch's listing: {entry.seed_id}")
    require(set(additions) == set(current_additions) | {e.seed_id for e in entries},
            "the merged mount pins a seed that is neither live nor in the batch")


def build(current: bytes, batch_manifest: bytes, listings=None) -> bytes:
    listings = pilot.LISTINGS if listings is None else listings
    try:
        _, entries = batch.parse_manifest(batch_manifest)
    except batch.Refused as error:
        raise Refused(f"the batch manifest is refused: {error}") from None
    out = merge(current, entries, listings)
    verify(out, current, entries, listings)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--current", required=True, help="the named-referral manifest web mounts now")
    parser.add_argument("--batch", required=True, help="the batch manifest")
    parser.add_argument("--out", required=True, help="where to write the bytes to upload (must not exist)")
    args = parser.parse_args(argv)
    try:
        require(not Path(args.out).exists(), "the output file already exists")
        out = build(Path(args.current).read_bytes(), Path(args.batch).read_bytes())
        with open(args.out, "xb") as target:
            target.write(out)
        require(Path(args.out).read_bytes() == out, "the written file differs from the verified bytes")
    except (Refused, OSError) as error:
        print(f"NAMED_MOUNT_REFUSED: {error}", flush=True)
        return 3
    print(f"NAMED_MOUNT_OK size={len(out)} sha256={hashlib.sha256(out).hexdigest()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
