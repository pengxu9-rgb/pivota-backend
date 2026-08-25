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
            "Most of this project's secrets carry an 'env-' prefix and a few are "
            "bare; the list lives in docs/runbooks/operating_on_gcp_production.md "
            "and is not restated here, because a copy is a copy that goes stale "
            "separately. Confirm the real name from the running service rather "
            "than trusting any list, that one included:\n"
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
    # NOT .strip(). Cloud Run mounts the payload verbatim and the app reads it
    # with a bare os.getenv (config/settings.py:623), so the service's key is the
    # exact bytes stored. If a payload was created with a trailing newline — the
    # classic `echo ... | gcloud secrets create --data-file=-` — stripping here
    # would sign with a DIFFERENT key than the service verifies with, producing
    # exactly the "401 with nothing in the logs" this flag exists to avoid.
    value = subprocess.check_output(cmd, text=True)
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

    # Exactly one source, chosen EXPLICITLY. These name different signing keys —
    # production's and the rollback's, or one you typed — and letting a later
    # branch silently overwrite an earlier one mints a token that authenticates
    # against exactly one platform, with nothing in the output to say which.
    #
    # Membership, not truthiness: `--gcp-secret=` with an empty value is still a
    # source the caller ASKED for, and treating it as absent would skip both the
    # guard and the load and quietly fall through to another key.
    sources = []
    if args.gcp_secret is not None:
        sources.append(("--gcp-secret", lambda: _load_secret_from_gcp(str(args.gcp_secret), str(args.gcp_project))))
    if args.railway_service is not None:
        sources.append(("--railway-service", lambda: _load_secret_from_railway(str(args.railway_service), args.railway_environment)))
    if args.jwt_secret:
        sources.append(("--jwt-secret/JWT_SECRET_KEY", lambda: str(args.jwt_secret)))

    if len(sources) > 1:
        raise SystemExit(
            "more than one signing-key source given ("
            + ", ".join(name for name, _ in sources)
            + "); they name different keys, so pass only one"
        )
    if not sources:
        raise SystemExit(
            "JWT secret is required via --jwt-secret, JWT_SECRET_KEY, --gcp-secret "
            "(production), or --railway-service (the rollback)"
        )
    if args.gcp_secret is not None and not args.gcp_secret:
        raise SystemExit("--gcp-secret was given an empty secret name")

    secret = sources[0][1]()
    if not secret:
        raise SystemExit(f"{sources[0][0]} produced an empty signing key")

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
