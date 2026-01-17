from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="abstractgateway", description="AbstractGateway (Run Gateway host)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run the AbstractGateway HTTP/SSE server")
    serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    serve.add_argument("--reload", action="store_true", help="Enable auto-reload (dev only)")

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
        import uvicorn

        uvicorn.run(
            "abstractgateway.app:app",
            host=str(args.host),
            port=int(args.port),
            reload=bool(args.reload),
        )
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
            import os

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
