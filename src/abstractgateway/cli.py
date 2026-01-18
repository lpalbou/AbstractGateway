from __future__ import annotations

import argparse
import os
import signal
import threading


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="abstractgateway", description="AbstractGateway (Run Gateway host)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run the AbstractGateway HTTP/SSE server")
    serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    serve.add_argument("--reload", action="store_true", help="Enable auto-reload (dev only)")
    serve.add_argument(
        "--no-runner",
        action="store_true",
        help="Serve the HTTP API without starting the runner (use `abstractgateway runner` in another process).",
    )

    runner = sub.add_parser("runner", help="Run the AbstractGateway runner worker (no HTTP)")

    tg = sub.add_parser("telegram-auth", help="One-time TDLib authentication bootstrap for Telegram Secret Chats (E2EE)")
    tg.add_argument("--timeout-s", type=float, default=120.0, help="Max seconds to wait for TDLib authorization (default: 120)")

    mig = sub.add_parser("migrate", help="Migrate durable stores between backends (best-effort)")
    mig.add_argument("--from", dest="src", default="file", choices=["file"], help="Source backend (default: file)")
    mig.add_argument("--to", dest="dst", default="sqlite", choices=["sqlite"], help="Destination backend (default: sqlite)")
    mig.add_argument(
        "--data-dir",
        default=None,
        help="Source data dir (defaults to ABSTRACTGATEWAY_DATA_DIR or ./runtime)",
    )
    mig.add_argument(
        "--db-path",
        default=None,
        help="Destination sqlite file path (defaults to <data-dir>/gateway.sqlite3)",
    )
    mig.add_argument("--overwrite", action="store_true", help="Overwrite destination DB if it exists")

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        prev_runner_env = os.environ.get("ABSTRACTGATEWAY_RUNNER")
        if bool(getattr(args, "no_runner", False)):
            # Override env for this process: do not start the background runner loop in the HTTP API process.
            os.environ["ABSTRACTGATEWAY_RUNNER"] = "0"

        try:
            import uvicorn
        except Exception as e:
            raise SystemExit(
                "AbstractGateway HTTP server dependencies are missing.\n"
                "Install with: `pip install \"abstractgateway[http]\"`\n"
                f"(import failed: {e})"
            )

        try:
            uvicorn.run(
                "abstractgateway.app:app",
                host=str(args.host),
                port=int(args.port),
                reload=bool(args.reload),
            )
        finally:
            if prev_runner_env is None:
                os.environ.pop("ABSTRACTGATEWAY_RUNNER", None)
            else:
                os.environ["ABSTRACTGATEWAY_RUNNER"] = prev_runner_env
        return

    if args.cmd == "runner":
        # Force runner enabled for this process.
        prev_runner_env = os.environ.get("ABSTRACTGATEWAY_RUNNER")
        os.environ["ABSTRACTGATEWAY_RUNNER"] = "1"

        from .service import start_gateway_runner, stop_gateway_runner

        stop = threading.Event()

        def _handle(_signum, _frame) -> None:  # pragma: no cover
            stop.set()

        try:
            signal.signal(signal.SIGINT, _handle)
            signal.signal(signal.SIGTERM, _handle)
        except Exception:
            # Some platforms (or embedded interpreters) may not support signals.
            pass

        start_gateway_runner()
        try:
            while not stop.is_set():
                stop.wait(0.5)
        finally:
            stop_gateway_runner()
            if prev_runner_env is None:
                os.environ.pop("ABSTRACTGATEWAY_RUNNER", None)
            else:
                os.environ["ABSTRACTGATEWAY_RUNNER"] = prev_runner_env
        return

    if args.cmd == "telegram-auth":
        # This command is intentionally interactive. It is meant to be run once to
        # create the TDLib session under ABSTRACT_TELEGRAM_DB_DIR.
        import getpass

        try:
            from abstractcore.tools.telegram_tdlib import TdlibClient, TdlibConfig
        except Exception as e:
            raise SystemExit(
                "TDLib bootstrap requires the optional Telegram dependencies. "
                "Install with: `pip install \"abstractgateway[telegram]\"` "
                f"(import failed: {e})"
            )

        try:
            base_cfg = TdlibConfig.from_env()
        except Exception as e:
            raise SystemExit(f"Missing/invalid Telegram env config: {e}")

        code = input("Telegram login code (leave blank if not needed): ").strip() or None
        pw = getpass.getpass("Telegram 2FA password (leave blank if none): ").strip() or None

        cfg = TdlibConfig(
            api_id=base_cfg.api_id,
            api_hash=base_cfg.api_hash,
            phone=base_cfg.phone,
            database_directory=base_cfg.database_directory,
            files_directory=base_cfg.files_directory,
            database_encryption_key=base_cfg.database_encryption_key,
            use_secret_chats=base_cfg.use_secret_chats,
            login_code=code or base_cfg.login_code,
            two_factor_password=pw or base_cfg.two_factor_password,
        )

        client = TdlibClient(config=cfg)
        client.start()
        try:
            ok = client.wait_until_ready(timeout_s=float(args.timeout_s))
            if not ok:
                err = client.last_error or "Timed out waiting for TDLib authorization"
                raise SystemExit(err)
            print("TDLib authorization: OK (session stored in TDLib database directory).")
        finally:
            try:
                client.stop()
            except Exception:
                pass
        return

    if args.cmd == "migrate":
        from pathlib import Path

        from .migrate import migrate_file_to_sqlite

        data_dir = Path(str(args.data_dir or "")).expanduser().resolve() if args.data_dir else None
        if data_dir is None:
            data_dir = Path(os.getenv("ABSTRACTGATEWAY_DATA_DIR", "./runtime")).expanduser().resolve()
        db_path = Path(str(args.db_path or "")).expanduser().resolve() if args.db_path else (data_dir / "gateway.sqlite3")

        if str(args.src).strip().lower() != "file" or str(args.dst).strip().lower() != "sqlite":
            raise SystemExit("Only --from=file --to=sqlite is supported in v0")

        migrate_file_to_sqlite(base_dir=data_dir, db_path=db_path, overwrite=bool(args.overwrite))
        print(f"Migrated file stores from {data_dir} to sqlite DB {db_path}")
        return

    raise SystemExit(2)

if __name__ == "__main__":
    main()
