"""Re-fetch Reap's published OpenAPI document and diff its `/agentic/*` surface against our pin.

WHY THIS EXISTS. Reap changed a REQUIRED FIELD on `POST /agentic/checkouts` -- `owner` was one
of three required properties and is now absent from the schema entirely, replaced by
`enrollmentId` -- and `info.version` is still `1.0.0`. The document does not say it moved. There
is no changelog endpoint, no `Reap-Version` bump (that header is pinned to `2025-02-14` and was
not touched), and no webhook. The only way we find out is by looking.

Worse, we would not find out from PRODUCTION either. Reap accepts unknown keys silently with a
200 and drops them, so a request still carrying a removed field looks exactly like a request that
worked -- which is how #2136's invented `source`, `merchant` and `attribution` blocks survived
review. A field that disappears from the spec is therefore INVISIBLE at runtime until the day it
becomes required-and-missing somewhere else.

So the spec is pinned to a file in this repo and this script is the thing that notices:

    python3 scripts/ops/reap_spec_diff.py               # fetch live, diff, exit 1 on any change
    python3 scripts/ops/reap_spec_diff.py --spec local.json    # diff a file you already have
    python3 scripts/ops/reap_spec_diff.py --write-fixture      # re-pin after a REVIEWED change

IT IS NOT PART OF THE TEST SUITE, and must not become one: a unit test that reaches the network
fails in CI for reasons that have nothing to do with the code, and a test that fails when a
partner edits their docs teaches people to ignore it. The FIXTURE is what the tests use -- see
`test_every_builder_body_validates_against_the_pinned_spec` -- and this script is what an
operator runs, on a schedule or before a release, to find out whether the fixture is still true.

WHAT IT COMPARES. Only `/agentic/*` paths, and for each operation only the things a client can
get wrong: the request body schema, the 200 response schema, and the parameter list (name, in,
required). Descriptions, examples and titles are stripped, because a partner rewording a
description is not a change we need to react to and a diff that cries wolf is a diff nobody runs.

THE DOCS HOST IS NOT THE API. `docs.reap.global` serves documentation; it takes no credential and
this script sends none. It never reads `REAP_API_KEY` and never touches `*.api.reap.global`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any, Dict, List, Tuple

#: The PUBLIC documentation URL. Not the API. No credential is sent, and none is read: this
#: script does not look at REAP_API_KEY at all.
SPEC_URL = "https://docs.reap.global/api-reference/openapi.json"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE_PATH = os.path.join(
    _REPO_ROOT, "tests", "fixtures", "reap_openapi_agentic_2026_09_17.json"
)

#: Keys dropped everywhere before comparison. Prose, not contract.
_NOISE_KEYS = ("description", "summary", "example", "examples", "title", "operationId", "tags")


def _strip(node: Any) -> Any:
    """Drop prose so the diff reports contract changes only.

    A partner rewording a description is not a thing a client can get wrong. A diff that fires on
    one is a diff that gets muted, and a muted diff would not have caught `owner` disappearing.
    """
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in sorted(node.items()) if k not in _NOISE_KEYS}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def _refs(node: Any, out: set) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            out.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)


def extract_agentic(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The `/agentic/*` paths plus, TRANSITIVELY, every component schema they reference.

    Transitively, because a `$ref` that resolves to a schema we did not pin would let the thing
    the ref points at change while the pin stayed byte-identical -- the pin would be true and
    useless at the same time.
    """
    paths = {k: v for k, v in (spec.get("paths") or {}).items() if k.startswith("/agentic/")}
    schemas = spec.get("components", {}).get("schemas", {}) or {}

    wanted: set = set()
    _refs(paths, wanted)
    seen: set = set()
    while wanted - seen:
        name = (wanted - seen).pop()
        seen.add(name)
        if name in schemas:
            _refs(schemas[name], wanted)

    return {
        # `info` is pinned even though it has lied to us, precisely so that the day it DOES move
        # is recorded next to the change it was supposed to announce.
        "info": {"title": spec.get("info", {}).get("title"),
                 "version": spec.get("info", {}).get("version")},
        "servers": spec.get("servers"),
        "paths": {k: paths[k] for k in sorted(paths)},
        "components": {"schemas": {k: schemas[k] for k in sorted(seen) if k in schemas}},
    }


def _operations(subset: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """`"POST /agentic/checkouts"` -> the three things a client can get wrong."""
    out: Dict[str, Dict[str, Any]] = {}
    for path, item in (subset.get("paths") or {}).items():
        for method, op in (item or {}).items():
            if not isinstance(op, dict):
                continue
            body = (((op.get("requestBody") or {}).get("content") or {})
                    .get("application/json") or {}).get("schema")
            ok = (((op.get("responses") or {}).get("200") or {}).get("content") or {})
            ok = (ok.get("application/json") or {}).get("schema")
            out[f"{method.upper()} {path}"] = {
                "parameters": sorted(
                    (str(p.get("name")), str(p.get("in")), bool(p.get("required")))
                    for p in (op.get("parameters") or []) if isinstance(p, dict)
                ),
                "requestBody": _strip(body),
                "response200": _strip(ok),
            }
    return out


def diff(pinned: Dict[str, Any], live: Dict[str, Any]) -> List[str]:
    """Human-readable differences, path by path. Empty means the pin is still true."""
    problems: List[str] = []

    if pinned.get("info") != live.get("info"):
        problems.append(f"info changed: pinned {pinned.get('info')} -> live {live.get('info')}")
    if pinned.get("servers") != live.get("servers"):
        problems.append("servers[] changed — check ALLOWED_HOST_SUFFIXES still covers every host")

    old, new = _operations(pinned), _operations(live)
    for gone in sorted(set(old) - set(new)):
        problems.append(f"OPERATION REMOVED: {gone}")
    for added in sorted(set(new) - set(old)):
        problems.append(f"operation added: {added}")

    for name in sorted(set(old) & set(new)):
        for field in ("parameters", "requestBody", "response200"):
            if old[name][field] == new[name][field]:
                continue
            problems.append(f"{name}: {field} changed")
            if field == "requestBody":
                # Named explicitly: a required request field appearing or disappearing is the
                # exact failure this script was built for, and "requestBody changed" buries it.
                for line in _required_delta(old[name][field], new[name][field]):
                    problems.append(f"    {line}")
    return problems


def _required_delta(old: Any, new: Any) -> List[str]:
    """Required-key changes across a schema, including inside a `oneOf`."""
    def required_sets(node: Any, out: List[Tuple[str, frozenset]], label: str = "") -> None:
        if isinstance(node, dict):
            if isinstance(node.get("required"), list):
                out.append((label or "<body>", frozenset(str(r) for r in node["required"])))
            for branch in ("oneOf", "anyOf", "allOf"):
                for i, sub in enumerate(node.get(branch) or []):
                    required_sets(sub, out, f"{label}{branch}[{i}]")
        return None

    before: List[Tuple[str, frozenset]] = []
    after: List[Tuple[str, frozenset]] = []
    required_sets(old, before)
    required_sets(new, after)
    lines: List[str] = []
    for (label, was), (_, now) in zip(before, after):
        if was == now:
            continue
        for key in sorted(was - now):
            lines.append(f"REQUIRED FIELD REMOVED from {label}: {key}")
        for key in sorted(now - was):
            lines.append(f"REQUIRED FIELD ADDED to {label}: {key}")
    if len(before) != len(after):
        lines.append(f"the number of schema branches changed: {len(before)} -> {len(after)}")
    return lines


def fetch(url: str = SPEC_URL, *, timeout: float = 60.0) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "Pivota/1.0 (+https://pivota.cc)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", help="read the live spec from this file instead of fetching it")
    ap.add_argument("--write-fixture", action="store_true",
                    help="RE-PIN: overwrite the fixture with the live subset. Only after a human "
                         "has read the diff and the client has been updated to match.")
    ap.add_argument("--url", default=SPEC_URL)
    args = ap.parse_args()

    if args.spec:
        with open(args.spec, "r", encoding="utf-8") as handle:
            spec = json.load(handle)
    else:
        print(f"fetching {args.url} (public docs; no credential is sent)")
        try:
            spec = fetch(args.url)
        except Exception as exc:  # noqa: BLE001
            print(f"FETCH FAILED: {type(exc).__name__}")
            print("A fetch failure is UNVERIFIABLE, not a clean bill of health. Re-run it.")
            return 2

    live = extract_agentic(spec)

    if args.write_fixture:
        os.makedirs(os.path.dirname(FIXTURE_PATH), exist_ok=True)
        with open(FIXTURE_PATH, "w", encoding="utf-8") as handle:
            json.dump(live, handle, indent=1, sort_keys=True)
            handle.write("\n")
        print(f"re-pinned {FIXTURE_PATH}")
        print("NOW READ THE GIT DIFF. A re-pin that nobody read is a pin that means nothing.")
        return 0

    with open(FIXTURE_PATH, "r", encoding="utf-8") as handle:
        pinned = json.load(handle)

    problems = diff(pinned, live)
    if not problems:
        print(f"no change across {len(_operations(pinned))} /agentic/* operations")
        return 0

    print(f"\n{len(problems)} DIFFERENCE(S) against {os.path.basename(FIXTURE_PATH)}:\n")
    for problem in problems:
        print(f"  {problem}")
    print("\nA difference is not automatically a break: Reap accepts unknown keys silently with")
    print("a 200 and drops them, so a field that disappeared here is still returning 200 in")
    print("production and doing nothing. Read the change, fix the client, THEN --write-fixture.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
