"""Run with python -m reap_rehearsal {probe,migrate,web,tick}; fixed staging target only."""

import asyncio
import json
import os
import sys
from .guard import GuardRejected, install_database_only_egress, run_guard


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"probe", "migrate", "web", "tick"}:
        raise SystemExit("usage: python -m reap_rehearsal {probe,migrate,web,tick}")
    command = sys.argv[1]
    install_database_only_egress()  # before DB driver and application imports
    try:
        receipt = asyncio.run(run_guard("migrate" if command == "migrate" else "runtime"))
        if command == "probe":
            print(json.dumps(receipt))
            return
        if command == "migrate":
            from .migration import migrate

            asyncio.run(migrate())
            return
        if command == "web":
            from .application import create_app

            app = create_app()
            import uvicorn

            port = int(os.environ.get("PORT", "8080"))
            if not 1024 <= port <= 65535:
                raise GuardRejected("listen_port")
            uvicorn.run(app, host="0.0.0.0", port=port, workers=1, access_log=False)
        else:
            from .worker import tick

            print(json.dumps(asyncio.run(tick())))
    except GuardRejected as exc:
        print(json.dumps({"guard": "rejected", "reason": str(exc)}), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
