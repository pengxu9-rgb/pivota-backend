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
        description="Mint a Pivota employee/admin JWT from JWT_SECRET_KEY or Railway service variables."
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
        "--railway-service",
        default=None,
        help="If provided, load JWT_SECRET_KEY from `railway variables --service <name> --json`.",
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
    if args.railway_service:
        secret = _load_secret_from_railway(str(args.railway_service), args.railway_environment)
    if not secret:
        raise SystemExit("JWT secret is required via --jwt-secret, JWT_SECRET_KEY, or --railway-service")

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
