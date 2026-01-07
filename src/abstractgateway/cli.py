from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="abstractgateway", description="AbstractGateway (Run Gateway host)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run the AbstractGateway HTTP/SSE server")
    serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    serve.add_argument("--reload", action="store_true", help="Enable auto-reload (dev only)")

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run(
            "abstractgateway.app:app",
            host=str(args.host),
            port=int(args.port),
            reload=bool(args.reload),
        )
        return

    raise SystemExit(2)


