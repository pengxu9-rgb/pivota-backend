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

WHAT IT COMPARES. Only `/agentic/*` paths, and for each operation everything a client can get
wrong, DEREFERENCED: the request schema with every `$ref` followed, the schema of EVERY response
code (not only the 200 -- a new 409 is a new thing the client has to classify), and the
parameter list with each parameter's schema. Descriptions, examples, titles and summaries are
stripped, because a partner rewording a description is not a change we need to react to and a
diff that cries wolf is a diff nobody runs. Everything else -- `required`, `enum`, `const`,
`properties`, `oneOf`/`anyOf`/`allOf`, `pattern`, `format`, `type` -- is compared, and every
difference is reported with its path:

    POST /agentic/enrollments: request.oneOf[2].properties.owner.required + email

THE 2026-09-25 LESSON. The first version of this script compared the request body AS A `$ref`
-- the reference string, not the schema it pointed at -- and only the 200 response. Reap made
the enrollment owner's `email` required inside `components.schemas.ClientReferenceOwner` and
added 409 responses on three operations, and the script reported neither: the ref string had
not changed, and a 409 is not a 200. ~1,900 lines of fixture churn, one line of diff output.
A pin that is compared through a reference is a pin on the reference.

THE DOCS HOST IS NOT THE API. `docs.reap.global` serves documentation; it takes no credential and
this script sends none. It never reads `REAP_API_KEY` and never touches `*.api.reap.global`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: The PUBLIC documentation URL. Not the API. No credential is sent, and none is read: this
#: script does not look at REAP_API_KEY at all.
SPEC_URL = "https://docs.reap.global/api-reference/openapi.json"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE_PATH = os.path.join(
    _REPO_ROOT, "tests", "fixtures", "reap_openapi_agentic_2026_09_25.json"
)

#: Keys dropped everywhere before comparison. Prose, not contract.
_NOISE_KEYS = ("description", "summary", "example", "examples", "title", "operationId", "tags")

#: The verbs an OpenAPI path item can carry. Anything else at that level (`parameters`,
#: `summary`, `servers`, `x-*`) is not an operation.
_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

#: Lists whose ORDER is not part of the contract: compared as sets, reported as `+ x` / `- x`.
_SET_LISTS = ("required", "enum")

#: Lists of alternative schemas. Compared by pairing branches on what they ARE, not where they
#: sit -- see `_pair_branches`.
_BRANCH_LISTS = ("oneOf", "anyOf", "allOf")

_COMPONENTS_PREFIX = "#/components/schemas/"


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
        if isinstance(ref, str) and ref.startswith(_COMPONENTS_PREFIX):
            out.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)


def dereference(node: Any, schemas: Dict[str, Any], *, _stack: Tuple[str, ...] = ()) -> Any:
    """Follow every `#/components/schemas/<name>` reference, recursively, until none is left.

    THE REF STRING IS NOT THE SCHEMA. Comparing `{"$ref": ".../ClientReferenceOwner"}` on both
    sides says the two documents agree on the NAME of the owner schema; it says nothing about
    whether `email` became required inside it. That is exactly what happened, and the old
    reference-as-value comparison reported nothing.

    Cycles are guarded by the chain of names being expanded on THIS path: a schema that refers
    back to one of its own ancestors is replaced by `{"$cycle": name}` rather than expanded
    forever. A schema referenced twice from sibling positions is expanded twice, which is what
    makes the two positions comparable. A reference to a schema the document does not define
    is kept as `{"$missing": name}` so that it is still a comparable value rather than a crash.
    Sibling keys next to a `$ref` (OpenAPI 3.1 allows them) are merged over the target.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if not ref.startswith(_COMPONENTS_PREFIX):
                # Not a schema component. Kept verbatim: it still compares, and a value that
                # started to point somewhere new is a difference worth seeing.
                return {k: dereference(v, schemas, _stack=_stack) for k, v in node.items()}
            name = ref[len(_COMPONENTS_PREFIX):]
            if name in _stack:
                return {"$cycle": name}
            target = schemas.get(name)
            if target is None:
                resolved: Any = {"$missing": name}
            else:
                resolved = dereference(target, schemas, _stack=_stack + (name,))
            siblings = {k: dereference(v, schemas, _stack=_stack)
                        for k, v in node.items() if k != "$ref"}
            if siblings and isinstance(resolved, dict):
                merged = dict(resolved)
                merged.update(siblings)
                return merged
            return resolved
        return {k: dereference(v, schemas, _stack=_stack) for k, v in node.items()}
    if isinstance(node, list):
        return [dereference(v, schemas, _stack=_stack) for v in node]
    return node


def extract_agentic(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The `/agentic/*` paths plus, TRANSITIVELY, every component schema they reference.

    Transitively, because a `$ref` that resolves to a schema we did not pin would let the thing
    the ref points at change while the pin stayed byte-identical -- the pin would be true and
    useless at the same time. The fixture keeps the refs as written; `dereference` follows them
    at comparison time, on both sides.
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


def _media_schemas(holder: Optional[Dict[str, Any]], schemas: Dict[str, Any]) -> Dict[str, Any]:
    """`content.<media type>.schema`, dereferenced and stripped, for every media type."""
    out: Dict[str, Any] = {}
    for media, entry in ((holder or {}).get("content") or {}).items():
        if isinstance(entry, dict):
            out[str(media)] = _strip(dereference(entry.get("schema"), schemas))
    return out


def _operations(subset: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """`"POST /agentic/checkouts"` -> everything a client can get wrong, fully dereferenced.

    `request` is the `application/json` request schema (the one we send); any other media type
    the operation accepts sits under `request_media` so it is still compared. `responses` is
    keyed by status code -- EVERY code, because a response code that appears is a response the
    client has never classified.
    """
    schemas = (subset.get("components") or {}).get("schemas") or {}
    out: Dict[str, Dict[str, Any]] = {}
    for path, item in (subset.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared_params = [p for p in (item.get("parameters") or []) if isinstance(p, dict)]
        for method, op in item.items():
            if method not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            body = op.get("requestBody") if isinstance(op.get("requestBody"), dict) else None
            request_media = _media_schemas(body, schemas)
            responses: Dict[str, Any] = {}
            for code, response in (op.get("responses") or {}).items():
                if not isinstance(response, dict):
                    continue
                media = _media_schemas(response, schemas)
                responses[str(code)] = {
                    "content": media,
                    "headers": _strip(dereference(response.get("headers"), schemas)),
                }
            parameters: List[Dict[str, Any]] = []
            for p in shared_params + [q for q in (op.get("parameters") or [])
                                      if isinstance(q, dict)]:
                p = dereference(p, schemas)
                parameters.append({
                    "name": str(p.get("name")),
                    "in": str(p.get("in")),
                    "required": bool(p.get("required")),
                    "schema": _strip(p.get("schema")),
                })
            parameters.sort(key=lambda p: (p["in"], p["name"]))
            out[f"{method.upper()} {path}"] = {
                "parameters": parameters,
                "request": request_media.pop("application/json", None),
                "request_media": request_media,
                "request_required": bool((body or {}).get("required")),
                "responses": responses,
            }
    return out


# --- the structural walk ------------------------------------------------------------------------


def _consts(node: Any, out: List[str]) -> None:
    if isinstance(node, dict):
        if "const" in node:
            out.append(json.dumps(node["const"], sort_keys=True))
        for value in node.values():
            _consts(value, out)
    elif isinstance(node, list):
        for value in node:
            _consts(value, out)


def _branch_identity(node: Any) -> str:
    """Name a schema branch by WHAT IT IS, not by where it sits in the list.

    A positional label (`oneOf[1]`) is not an identity: inserting one branch at the front --
    which is exactly what a partner does when they add an enrollment source -- renumbers every
    branch after it, and a positional diff then reports a change on each of them while the one
    real difference scrolls past. The property-name set plus the `const` values inside the
    branch is stable under insertion and reordering: `{source,cardId}` alone is shared by
    REAP_CARD and BIN_SPONSOR, and the error-response branches are all `{error}`, so the consts
    are what tell those apart.
    """
    if isinstance(node, dict):
        names = sorted(node["properties"]) if isinstance(node.get("properties"), dict) else []
        consts: List[str] = []
        _consts(node, consts)
        if names and consts:
            return "{" + ",".join(names) + "}=" + "|".join(sorted(set(consts)))
        if names:
            return "{" + ",".join(names) + "}"
        if consts:
            return "|".join(sorted(set(consts)))
        return json.dumps(node, sort_keys=True)
    return json.dumps(node, sort_keys=True)


def _parameter_identity(node: Any) -> str:
    """A parameter is the same parameter when it has the same name in the same place."""
    if isinstance(node, dict):
        return f"{node.get('in')}:{node.get('name')}"
    return json.dumps(node, sort_keys=True)


def _pair_branches(old: Sequence[Any], new: Sequence[Any], identity=_branch_identity):
    """Pairs `(old_index, new_index)`, then unmatched old indexes, then unmatched new indexes.

    Two passes. Pass one pairs branches whose identity is equal, which survives insertion and
    reordering; identities are NOT unique, so it is a multiset match against a list of unclaimed
    branches. Pass two pairs whatever is left IN ORDER, because a branch that gained or lost a
    property has a different identity on each side while still being the same branch -- that is
    the `enrollmentId` case, and reporting it as one removal plus one addition would lose the
    field name, which is the whole reason this exists.
    """
    unclaimed = list(range(len(new)))
    pairs: List[Tuple[int, int]] = []
    leftover_old: List[int] = []
    for i, branch in enumerate(old):
        ident = identity(branch)
        for pos, j in enumerate(unclaimed):
            if identity(new[j]) == ident:
                pairs.append((i, j))
                unclaimed.pop(pos)
                break
        else:
            leftover_old.append(i)
    while leftover_old and unclaimed:
        pairs.append((leftover_old.pop(0), unclaimed.pop(0)))
    return pairs, leftover_old, unclaimed


def _shape(node: Any) -> Optional[str]:
    """`oneOf` / `anyOf` / `allOf` for a schema that is a list of alternatives, else None. The
    KIND only, never the count: a branch inserted into an existing `oneOf` is a branch diff, not
    a shape change. The 2026-09-25 spec turned twelve single-object error responses into an
    `anyOf` of per-code shapes; reported key by key that is four lines per operation
    (`- properties`, `- required`, `- type`, `+ anyOf`), and none of the four says what the new
    alternatives ARE."""
    if isinstance(node, dict):
        for key in _BRANCH_LISTS:
            if isinstance(node.get(key), list):
                return key
    return None


def _describe(node: Any) -> str:
    """One line for a whole subtree: the branch identity, or the identities of each alternative."""
    key = _shape(node)
    if key:
        return f"{key}[" + ", ".join(_branch_identity(b) for b in node[key]) + "]"
    return _branch_identity(node)


def _short(value: Any) -> str:
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= 80 else text[:77] + "..."


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _walk(old: Any, new: Any, path: str, out: List[str]) -> None:
    """Every difference between two stripped, dereferenced values, each with its path."""
    if old == new:
        return
    if isinstance(old, dict) and isinstance(new, dict):
        if _shape(old) != _shape(new):
            # An object became a list of alternatives (or the reverse). One line naming both
            # sides beats four lines of removed and added keys that name neither.
            out.append(f"{path}: {_describe(old)} -> {_describe(new)}")
            return
        for key in sorted(set(old) - set(new)):
            out.append(f"{path} - {key}" if path else f"- {key}")
        for key in sorted(set(new) - set(old)):
            what = _describe(new[key]) if isinstance(new[key], dict) else ""
            suffix = f" ({what})" if what and not what.startswith(("{}", "{\"")) else ""
            out.append((f"{path} + {key}" if path else f"+ {key}") + suffix)
        for key in sorted(set(old) & set(new)):
            _walk(old[key], new[key], _join(path, str(key)), out)
        return
    if isinstance(old, list) and isinstance(new, list):
        leaf = path.rsplit(".", 1)[-1]
        if leaf in _SET_LISTS and all(not isinstance(v, (dict, list)) for v in old + new):
            for value in sorted(set(old) - set(new), key=str):
                out.append(f"{path} - {value}")
            for value in sorted(set(new) - set(old), key=str):
                out.append(f"{path} + {value}")
            return
        if leaf in _BRANCH_LISTS or leaf == "parameters":
            identity = _parameter_identity if leaf == "parameters" else _branch_identity
            pairs, gone, added = _pair_branches(old, new, identity)
            for i in gone:
                out.append(f"{path}[{i}] REMOVED: {identity(old[i])}")
            for j in added:
                out.append(f"{path}[{j}] ADDED: {identity(new[j])}")
            for i, j in sorted(pairs, key=lambda p: p[1]):
                _walk(old[i], new[j], f"{path}[{j}]", out)
            return
        for i in range(min(len(old), len(new))):
            _walk(old[i], new[i], f"{path}[{i}]", out)
        for i in range(len(new), len(old)):
            out.append(f"{path}[{i}] REMOVED: {_short(old[i])}")
        for i in range(len(old), len(new)):
            out.append(f"{path}[{i}] ADDED: {_short(new[i])}")
        return
    out.append(f"{path}: {_short(old)} -> {_short(new)}")


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
        lines: List[str] = []
        _walk(old[name], new[name], "", lines)
        for line in lines:
            problems.append(f"{name}: {line}")
    return problems


#: The published spec is ~1.9 MB. 8 MiB is far clear of that and still bounded: `response.read()`
#: with no argument reads whatever the host sends, and this script is pointed at a URL an operator
#: can override.
MAX_SPEC_BYTES = 8 * 1024 * 1024


def fetch(url: str = SPEC_URL, *, timeout: float = 60.0) -> Dict[str, Any]:
    if not str(url).startswith("https://"):
        # An http:// spec is a document an intermediary can rewrite, and what we do with it is
        # decide whether our request bodies are still correct.
        raise ValueError(f"spec URL must be https, got {url!r}")
    request = urllib.request.Request(url, headers={"User-Agent": "Pivota/1.0 (+https://pivota.cc)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        raw = response.read(MAX_SPEC_BYTES + 1)
    if len(raw) > MAX_SPEC_BYTES:
        raise ValueError(f"spec at {url} exceeded {MAX_SPEC_BYTES} bytes; refusing to parse it")
    return json.loads(raw.decode("utf-8"))


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
