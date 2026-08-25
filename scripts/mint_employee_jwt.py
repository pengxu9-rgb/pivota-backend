#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from typing import Any, Dict, Optional

import jwt


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Mint a Pivota employee/admin JWT from JWT_SECRET_KEY, from Google "
            "Secret Manager (production), or from Railway service variables "
            "(the ROLLBACK)."
        ),
        epilog=(
            "Production is Cloud Run in pivota-prod/us-west1 since the 2026-08-22 "
            "cutover; Railway is the ROLLBACK and holds its own JWT_SECRET_KEY. A "
            "token minted from the rollback's secret is rejected by production, "
            "which reads as 'my admin account is broken' rather than 'I signed "
            "with the wrong key'. Prefer:\n"
            "  scripts/mint_employee_jwt.py --email you@pivota.cc --gcp-secret\n"
            "Most of this project's secrets carry an 'env-' prefix; a handful "
            "(DATABASE_URL, REDIS_URL, PCI_KB_DATABASE_URL, "
            "STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY) are bare. Confirm the real "
            "name from the running service rather than trusting one written down:\n"
            "  gcloud run services describe web --project pivota-prod "
            "--region us-west1 --format=json   # read the secretKeyRef"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--role",
        default="admin",
        choices=("super_admin", "admin", "employee", "outsourced", "merchant", "agent"),
    )
    parser.add_argument("--sub", default=None, help="JWT sub claim; defaults to email.")
    parser.add_argument("--user-id", default=None, help="Optional user_id claim; defaults to email.")
    parser.add_argument("--employee-id", default=None, help="Optional employee_id claim.")
    parser.add_argument("--merchant-id", default=None, help="Optional merchant_id claim.")
    parser.add_argument("--agent-id", default=None, help="Optional agent_id claim.")
    parser.add_argument("--expires-minutes", type=int, default=60)
    parser.add_argument("--jwt-secret", default=os.getenv("JWT_SECRET_KEY") or "")
    parser.add_argument(
        "--gcp-secret",
        nargs="?",
        const="env-JWT_SECRET_KEY",
        default=None,
        metavar="SECRET_NAME",
        help=(
            "Load JWT_SECRET_KEY from Google Secret Manager in the production "
            "project (default secret: env-JWT_SECRET_KEY). This is the "
            "production path."
        ),
    )
    parser.add_argument(
        "--gcp-project",
        default=os.getenv("GCP_PROJECT") or "pivota-prod",
        help="Project to read --gcp-secret from (default: pivota-prod).",
    )
    parser.add_argument(
        "--railway-service",
        default=None,
        help=(
            "ROLLBACK ONLY. Load JWT_SECRET_KEY from `railway variables "
            "--service <name> --json`. Railway is not production; a token signed "
            "with its secret will not authenticate against api.pivota.cc. Use "
            "--gcp-secret unless you are deliberately operating the rollback."
        ),
    )
    parser.add_argument(
        "--railway-environment",
        default=None,
        help="Optional Railway environment override when using --railway-service.",
    )
    parser.add_argument(
        "--format",
        choices=("token", "header", "json"),
        default="token",
        help="Output token only, Authorization header, or JSON metadata.",
    )
    return parser.parse_args()


def _load_secret_from_gcp(secret: str, project: str) -> str:
    """Read JWT_SECRET_KEY out of Secret Manager in the production project.

    `gcloud` prints the payload with no trailing newline, but strip anyway: a
    stray newline signs a DIFFERENT key than the service loads, and the failure
    surfaces as a 401 with nothing in the logs to distinguish it from a genuinely
    wrong secret.
    """
    cmd = [
        "gcloud",
        "secrets",
        "versions",
        "access",
        "latest",
        f"--secret={secret}",
        f"--project={project}",
    ]
    value = subprocess.check_output(cmd, text=True).strip()
    if not value:
        raise RuntimeError(f"{secret} is empty in project {project}")
    return value


def _load_secret_from_railway(service: str, environment: Optional[str]) -> str:
    cmd = ["railway", "variables", "--service", service, "--json"]
    if environment:
        cmd.extend(["--environment", environment])
    payload = json.loads(subprocess.check_output(cmd, text=True))
    secret = str(payload.get("JWT_SECRET_KEY") or "").strip()
    if not secret:
        raise RuntimeError("JWT_SECRET_KEY not found in Railway service variables")
    return secret


def _build_claims(args: argparse.Namespace) -> Dict[str, Any]:
    email = str(args.email).strip()
    now = int(time.time())
    claims: Dict[str, Any] = {
        "sub": str(args.sub or email),
        "email": email,
        "role": str(args.role),
        "user_id": str(args.user_id or email),
        "iat": now,
        "exp": now + max(1, int(args.expires_minutes)) * 60,
    }
    if args.employee_id:
        claims["employee_id"] = str(args.employee_id)
    if args.merchant_id:
        claims["merchant_id"] = str(args.merchant_id)
    if args.agent_id:
        claims["agent_id"] = str(args.agent_id)
    return claims


def main() -> int:
    args = _parse_args()
    secret = str(args.jwt_secret or "").strip()
    if args.gcp_secret and args.railway_service:
        # Not a style preference: these are two DIFFERENT keys on two different
        # platforms. Letting one silently win would mint a token that works
        # against exactly one of them, with no way to tell which from the output.
        raise SystemExit(
            "--gcp-secret (production) and --railway-service (the rollback) name "
            "different signing keys; pass only one"
        )
    if args.gcp_secret:
        secret = _load_secret_from_gcp(str(args.gcp_secret), str(args.gcp_project))
    if args.railway_service:
        secret = _load_secret_from_railway(str(args.railway_service), args.railway_environment)
    if not secret:
        raise SystemExit(
            "JWT secret is required via --jwt-secret, JWT_SECRET_KEY, --gcp-secret "
            "(production), or --railway-service (the rollback)"
        )

    claims = _build_claims(args)
    token = jwt.encode(claims, secret, algorithm="HS256")

    if args.format == "header":
        print(f"Authorization: Bearer {token}")
        return 0
    if args.format == "json":
        print(json.dumps({"token": token, "claims": claims}, indent=2, ensure_ascii=False))
        return 0

    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
