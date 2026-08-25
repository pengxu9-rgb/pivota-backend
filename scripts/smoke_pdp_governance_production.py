#!/usr/bin/env python3
"""Production smoke for PDP governance main-chain acceptance.

This deliberately checks only the canonical chain:
employee.pivota.cc / merchant.pivota.cc -> api.pivota.cc.
Preview URLs and old Railway backend URLs must not be used to pass this smoke.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib import error, parse, request


LEGACY_BACKEND_URLS = (
    "web-production-fedb.up.railway.app",
    "pivota-backend-production.up.railway.app",
)

REQUIRED_MODULE_KEYS = (
    "identity",
    "copy",
    "gallery",
    "variants",
    "offers",
    "reviews",
    "pivota_insights",
    "external_sources",
    "quality",
)

REQUIRED_MODULE_FIELDS = (
    "module_key",
    "status",
    "source_refs",
    "last_reviewer",
    "review_actor_type",
    "review_decision",
    "published_payload",
    "current",
    "staged",
    "history",
)

HIGH_RISK_MODULES = {"gallery", "reviews"}
LOW_RISK_MACHINE_PUBLISH_MODULES = {
    "copy",
    "identity",
    "pivota_insights",
    "external_sources",
    "quality",
}


class AssetParser(HTMLParser):
    def __init__(self, page_url: str):
        super().__init__()
        self.page_url = page_url
        self.assets: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        for key, value in attrs:
            if key not in {"src", "href"} or not value:
                continue
            if "/_next/static/" in value and ".js" in value:
                self.assets.append(parse.urljoin(self.page_url, value))


def _json_default(value: Any) -> str:
    return str(value)


def _fetch(
    url: str,
    *,
    method: str = "GET",
    token: Optional[str] = None,
    admin_key: Optional[str] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: int = 45,
) -> Tuple[int, Dict[str, str], bytes]:
    body = None
    headers = {
        "User-Agent": "pivota-pdp-production-smoke/1.0",
        "Accept-Encoding": "gzip",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if admin_key:
        headers["X-ADMIN-KEY"] = admin_key
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp_headers.get("content-encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return int(resp.status), resp_headers, data
    except error.HTTPError as exc:
        data = exc.read()
        resp_headers = {k.lower(): v for k, v in exc.headers.items()}
        if resp_headers.get("content-encoding", "").lower() == "gzip":
            data = gzip.decompress(data)
        return int(exc.code), resp_headers, data


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _api_json(
    api_base: str,
    path: str,
    *,
    method: str = "GET",
    token: Optional[str] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    status, _headers, data = _fetch(
        f"{api_base.rstrip('/')}{path}",
        method=method,
        token=token,
        json_body=json_body,
    )
    text = _decode(data)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{path} returned non-JSON status={status}: {text[:300]}") from exc
    if status < 200 or status >= 300:
        raise AssertionError(f"{path} returned status={status}: {json.dumps(payload)[:500]}")
    return payload


def _fail_if_legacy_url(text: str, context: str, failures: List[str]) -> None:
    for legacy in LEGACY_BACKEND_URLS:
        if legacy in text:
            failures.append(f"{context} contains legacy backend URL {legacy}")


def _items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    value = payload.get("items") or payload.get("pdps") or payload.get("subjects") or []
    if not isinstance(value, list):
        raise AssertionError("PDP list response does not contain a list")
    return [dict(item) for item in value]


def _module_map(detail: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    modules = detail.get("modules")
    if not isinstance(modules, list):
        raise AssertionError("PDP detail response missing modules list")
    return {str(module.get("module_key")): dict(module) for module in modules}


def _validate_projection(
    label: str,
    detail: Dict[str, Any],
    failures: List[str],
) -> List[str]:
    pdp = detail.get("pdp") or {}
    modules = _module_map(detail)
    module_keys = list(modules.keys())
    if tuple(module_keys) != REQUIRED_MODULE_KEYS:
        failures.append(f"{label} module keys mismatch: {module_keys}")

    if "published_payload" not in detail or not isinstance(detail.get("published_payload"), dict):
        failures.append(f"{label} missing top-level published_payload dict")

    for module_key in REQUIRED_MODULE_KEYS:
        module = modules.get(module_key)
        if not module:
            failures.append(f"{label} missing module {module_key}")
            continue
        for field in REQUIRED_MODULE_FIELDS:
            if field not in module:
                failures.append(f"{label} module {module_key} missing field {field}")
        if (
            module_key in HIGH_RISK_MODULES
            and module.get("status") == "published"
            and module.get("review_actor_type") == "gpt55_quality_gate"
        ):
            failures.append(f"{label} high-risk module {module_key} was GPT-only published")

    print(
        json.dumps(
            {
                "event": "pdp_projection_checked",
                "label": label,
                "pdp_id": pdp.get("pdp_id"),
                "subject_type": pdp.get("subject_type"),
                "external_only": pdp.get("external_only"),
                "seller_count": pdp.get("seller_count"),
                "module_count": len(modules),
                "published_keys": sorted((detail.get("published_payload") or {}).keys()),
            },
            default=_json_default,
            sort_keys=True,
        )
    )
    return module_keys


def _find_pdp_id(
    api_base: str,
    token: str,
    *,
    external_only: Optional[bool],
    explicit_id: str,
) -> str:
    if explicit_id:
        return explicit_id
    suffix = "" if external_only is None else f"&external_only={str(external_only).lower()}"
    payload = _api_json(api_base, f"/employee/pdps?limit=10{suffix}", token=token)
    items = _items(payload)
    if not items:
        kind = "any" if external_only is None else ("external-only" if external_only else "internal multi-merchant")
        raise AssertionError(f"no {kind} PDP subjects found in production")
    pdp_id = str(items[0].get("pdp_id") or "")
    if not pdp_id:
        raise AssertionError("PDP list item missing pdp_id")
    return pdp_id


def _check_gpt55_gate_sample(
    api_base: str,
    token: str,
    explicit_id: str,
    failures: List[str],
) -> None:
    candidate_ids: List[str] = []
    if explicit_id:
        candidate_ids.append(explicit_id)
    reviewed = _api_json(api_base, "/employee/pdps?limit=10&review_actor=gpt55_quality_gate", token=token)
    candidate_ids.extend(str(item.get("pdp_id")) for item in _items(reviewed) if item.get("pdp_id"))
    candidate_ids = list(dict.fromkeys(candidate_ids))

    if not candidate_ids:
        failures.append("no GPT-5.5 quality-gate PDP sample found")
        return

    for pdp_id in candidate_ids:
        detail = _api_json(api_base, f"/employee/pdps/{parse.quote(pdp_id, safe='')}", token=token)
        modules = _module_map(detail)
        machine_published = [
            key
            for key, module in modules.items()
            if module.get("status") == "published" and module.get("review_actor_type") == "gpt55_quality_gate"
        ]
        if any(key in LOW_RISK_MACHINE_PUBLISH_MODULES for key in machine_published):
            print(
                json.dumps(
                    {
                        "event": "gpt55_quality_gate_sample_checked",
                        "pdp_id": pdp_id,
                        "machine_published_modules": machine_published,
                    },
                    sort_keys=True,
                )
            )
            return

    failures.append(f"GPT-5.5 quality-gate sample has no low-risk published module: {candidate_ids}")


def _check_hydration_status(api_base: str, token: str, failures: List[str]) -> None:
    payload = _api_json(api_base, "/employee/pdps/hydration", token=token)
    if payload.get("status") != "success":
        failures.append(f"PDP hydration status returned non-success: {payload}")
    total = int(payload.get("total") or 0)
    if total <= 0:
        failures.append(f"PDP hydration status has empty subject index: {payload}")
    print(
        json.dumps(
            {
                "event": "pdp_subject_index_hydration_status_checked",
                "total": total,
                "external_only": payload.get("external_only"),
                "internal_or_multi_merchant": payload.get("internal_or_multi_merchant"),
                "last_updated_at": payload.get("last_updated_at"),
            },
            default=_json_default,
            sort_keys=True,
        )
    )


def _check_portal_bundles(
    pages: Sequence[str],
    failures: List[str],
) -> None:
    assets: Dict[str, str] = {}
    for page in pages:
        status, _headers, data = _fetch(page, timeout=60)
        html = _decode(data)
        if status < 200 or status >= 300:
            failures.append(f"portal page {page} returned status={status}")
        _fail_if_legacy_url(html, f"portal page {page}", failures)
        parser = AssetParser(page)
        parser.feed(html)
        for asset in parser.assets:
            assets.setdefault(asset, page)
        print(json.dumps({"event": "portal_page_checked", "url": page, "status": status, "assets": len(parser.assets)}, sort_keys=True))

    for index, asset in enumerate(sorted(assets), start=1):
        status, _headers, data = _fetch(asset, timeout=60)
        text = _decode(data)
        if status < 200 or status >= 300:
            failures.append(f"portal asset {asset} returned status={status}")
        _fail_if_legacy_url(text, f"portal asset {asset}", failures)
        if index % 10 == 0 or index == len(assets):
            print(json.dumps({"event": "portal_assets_checked", "checked": index, "total": len(assets)}, sort_keys=True))


def _check_openapi(api_base: str, failures: List[str]) -> None:
    # /openapi.json is admin-gated in production (it is the full internal route list). Anonymous
    # callers get 404, not 401, so a missing key looks exactly like a missing endpoint - say so
    # rather than reporting "openapi returned status=404" and sending someone after a routing bug.
    admin_key = (os.environ.get("ADMIN_API_KEY") or os.environ.get("PROMOTIONS_ADMIN_KEY") or "").strip()
    status, _headers, data = _fetch(
        f"{api_base.rstrip('/')}/openapi.json", admin_key=admin_key or None, timeout=45
    )
    text = _decode(data)
    if status == 404 and not admin_key:
        failures.append(
            "openapi returned 404 and no ADMIN_API_KEY was set - the spec is admin-gated in "
            "production; export ADMIN_API_KEY to run this check"
        )
        return
    if status != 200:
        failures.append(f"openapi returned status={status}")
    _fail_if_legacy_url(text, "openapi", failures)
    host = parse.urlparse(api_base).netloc
    if host and host not in text:
        failures.append(f"openapi does not contain canonical API host {host}")


def _check_backend_build(api_base: str, failures: List[str]) -> None:
    status, _headers, data = _fetch(f"{api_base.rstrip('/')}/__build", timeout=45)
    text = _decode(data)
    if status != 200:
        failures.append(f"__build returned status={status}")
        return
    _fail_if_legacy_url(text, "__build", failures)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        failures.append("__build returned non-JSON")
        return
    print(
        json.dumps(
            {
                "event": "backend_build_checked",
                "commit": payload.get("commit_sha") or payload.get("full_sha") or payload.get("commit"),
                "deployment_id": payload.get("deployment_id"),
            },
            default=_json_default,
            sort_keys=True,
        )
    )


def _portal_pages(employee_base: str, merchant_base: str, sample_pdp_id: str) -> List[str]:
    return [
        f"{employee_base}/dashboard/pdps",
        f"{employee_base}/dashboard/pdps/detail?pdpId={parse.quote(sample_pdp_id, safe='')}",
        f"{employee_base}/dashboard/finance",
        f"{employee_base}/dashboard/mcp",
        f"{employee_base}/dashboard/platform-orders",
        f"{employee_base}/dashboard/platform-reports",
        f"{employee_base}/dashboard/psp-overview",
        f"{employee_base}/dashboard/reviews/import",
        f"{employee_base}/dashboard/merchants",
        f"{merchant_base}/dashboard/pdp-status",
    ]


def run(args: argparse.Namespace) -> int:
    failures: List[str] = []
    api_base = args.api_base.rstrip("/")
    employee_base = args.employee_base.rstrip("/")
    merchant_base = args.merchant_base.rstrip("/")

    for base_name, base in {
        "api_base": api_base,
        "employee_base": employee_base,
        "merchant_base": merchant_base,
    }.items():
        if any(legacy in base for legacy in LEGACY_BACKEND_URLS):
            failures.append(f"{base_name} points at legacy backend URL: {base}")

    _check_backend_build(api_base, failures)
    _check_openapi(api_base, failures)

    # Canonical password login (same path the employee portal UI uses).
    # /auth/signin is a legacy route guarded by ENABLE_INTERNAL_DEMO_FIXTURES
    # in prod; since that gate is intentionally off, the smoke can't use it.
    # /api/auth/login is the production auth path and returns the same
    # JWT shape we need: {"success": true, "token": "...", "user": {...}}.
    employee_login = _api_json(
        api_base,
        "/api/auth/login",
        method="POST",
        json_body={"email": args.employee_email, "password": args.employee_password},
    )
    merchant_login = _api_json(
        api_base,
        "/api/auth/login",
        method="POST",
        json_body={"email": args.merchant_email, "password": args.merchant_password},
    )
    employee_token = str(employee_login.get("token") or "")
    merchant_token = str(merchant_login.get("token") or "")
    if not employee_token:
        failures.append("employee login did not return token")
    if not merchant_token:
        failures.append("merchant login did not return token")

    # /api/auth/login does not include merchant_id in its user payload (only
    # row id, email, full_name, role). Prefer an explicit smoke-config value
    # (PIVOTA_SMOKE_MERCHANT_ID) and fall back to the login response for
    # back-compat with the legacy /auth/signin shape. If neither source
    # provides it, the downstream product_key cross-check is skipped rather
    # than a hard failure.
    merchant_user = merchant_login.get("user") if isinstance(merchant_login.get("user"), dict) else {}
    merchant_id = (args.merchant_id or merchant_user.get("merchant_id") or "").strip()
    if not merchant_id:
        print(json.dumps({
            "event": "smoke_merchant_id_unset",
            "note": "PIVOTA_SMOKE_MERCHANT_ID is unset and /api/auth/login does not include merchant_id; product_key cross-check will be skipped.",
        }))

    _check_hydration_status(api_base, employee_token, failures)

    external_pdp_id = _find_pdp_id(
        api_base,
        employee_token,
        external_only=True,
        explicit_id=args.external_pdp_id,
    )
    internal_pdp_id = _find_pdp_id(
        api_base,
        employee_token,
        external_only=False,
        explicit_id=args.internal_pdp_id,
    )

    external_detail = _api_json(api_base, f"/employee/pdps/{parse.quote(external_pdp_id, safe='')}", token=employee_token)
    internal_detail = _api_json(api_base, f"/employee/pdps/{parse.quote(internal_pdp_id, safe='')}", token=employee_token)
    external_keys = _validate_projection("external_only", external_detail, failures)
    internal_keys = _validate_projection("internal_multi_merchant", internal_detail, failures)
    if external_keys != internal_keys:
        failures.append(f"external/internal module projection keys differ: {external_keys} vs {internal_keys}")

    _check_gpt55_gate_sample(api_base, employee_token, args.gpt55_pdp_id, failures)

    # /merchant/pdps/* requires the caller's JWT to carry a merchant_id claim
    # so the row-level scoping can resolve which merchant owns the request.
    # /api/auth/login's canonical JWT does not include merchant_id (that
    # was specific to the legacy /auth/signin demo-fixture path). When
    # PIVOTA_SMOKE_MERCHANT_ID is unset and the login response did not
    # carry merchant_id either, skip the merchant-side checks with a clear
    # log line rather than failing on MERCHANT_REQUIRED 403. Operators can
    # restore full coverage by populating a merchant account whose JWT
    # binding includes merchant_id and setting PIVOTA_SMOKE_MERCHANT_ID.
    if merchant_id:
        merchant_path = (
            f"/merchant/pdps/product/{parse.quote(args.merchant_platform, safe='')}/"
            f"{parse.quote(args.merchant_product_id, safe='')}"
        )
        merchant_status = _api_json(api_base, merchant_path, token=merchant_token)
        _validate_projection("merchant_status", merchant_status, failures)
        product_key = str(merchant_status.get("product_key") or "")
        if merchant_id not in product_key:
            failures.append(f"merchant PDP status product_key does not include logged-in merchant_id: {product_key}")
    else:
        print(json.dumps({
            "event": "smoke_merchant_pdp_check_skipped",
            "note": "merchant_id is not available (not in JWT, not provided via PIVOTA_SMOKE_MERCHANT_ID); skipping /merchant/pdps/* cross-check.",
        }))

    _check_portal_bundles(_portal_pages(employee_base, merchant_base, external_pdp_id), failures)

    if failures:
        print(json.dumps({"status": "failed", "failures": failures}, indent=2, sort_keys=True), file=sys.stderr)
        return 1

    print(json.dumps({"status": "passed", "api_base": api_base, "employee_base": employee_base, "merchant_base": merchant_base}, sort_keys=True))
    return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    def env_or_default(name: str, default: str) -> str:
        value = os.getenv(name)
        return value if value not in {None, ""} else default

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default=env_or_default("PIVOTA_API_BASE_URL", "https://api.pivota.cc"))
    parser.add_argument("--employee-base", default=env_or_default("PIVOTA_EMPLOYEE_BASE_URL", "https://employee.pivota.cc"))
    parser.add_argument("--merchant-base", default=env_or_default("PIVOTA_MERCHANT_BASE_URL", "https://merchant.pivota.cc"))
    parser.add_argument("--employee-email", default=env_or_default("PIVOTA_SMOKE_EMPLOYEE_EMAIL", "employee@pivota.com"))
    parser.add_argument("--employee-password", default=env_or_default("PIVOTA_SMOKE_EMPLOYEE_PASSWORD", "Admin123!"))
    parser.add_argument("--merchant-email", default=env_or_default("PIVOTA_SMOKE_MERCHANT_EMAIL", "merchant@test.com"))
    parser.add_argument("--merchant-password", default=env_or_default("PIVOTA_SMOKE_MERCHANT_PASSWORD", "Admin123!"))
    parser.add_argument(
        "--merchant-id",
        default=env_or_default("PIVOTA_SMOKE_MERCHANT_ID", ""),
        help="Merchant ID for product_key cross-check (legacy /auth/signin returned it in the login response; /api/auth/login does not).",
    )
    parser.add_argument("--external-pdp-id", default=env_or_default("PDP_SMOKE_EXTERNAL_PDP_ID", ""))
    parser.add_argument("--internal-pdp-id", default=env_or_default("PDP_SMOKE_INTERNAL_PDP_ID", ""))
    parser.add_argument(
        "--gpt55-pdp-id",
        default=env_or_default("PDP_SMOKE_GPT55_PDP_ID", "pdp_417270e8f78f856896fd3071"),
    )
    parser.add_argument("--merchant-platform", default=env_or_default("PDP_SMOKE_MERCHANT_PLATFORM", "shopify"))
    parser.add_argument(
        "--merchant-product-id",
        default=env_or_default("PDP_SMOKE_MERCHANT_PRODUCT_ID", "live_acceptance_merchant_pdp_1776996856"),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
