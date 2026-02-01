from __future__ import annotations

import argparse
import os
import signal
import threading
import json


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="abstractgateway", description="AbstractGateway (Run Gateway host)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run the AbstractGateway HTTP/SSE server")
    serve.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
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

    triage = sub.add_parser("triage-reports", help="Triage /bug and /feature reports (decision queue + optional backlog drafts)")
    triage.add_argument(
        "--data-dir",
        default=None,
        help="Gateway data dir (defaults to ABSTRACTGATEWAY_DATA_DIR or ./runtime/gateway)",
    )
    triage.add_argument(
        "--repo-root",
        default=None,
        help="Repo root containing docs/backlog (auto-detected from CWD if omitted)",
    )
    triage.add_argument("--write-drafts", action="store_true", help="Write backlog drafts into docs/backlog/proposed/")
    triage.add_argument("--llm", action="store_true", help="Enable optional LLM assist (env/config required)")
    triage.add_argument("--action-base-url", default=None, help="Base URL for triage action links (e.g., https://<host>)")
    triage.add_argument("--print-actions", action="store_true", help="Print approve/defer/reject action links for pending decisions")
    triage.add_argument("--notify", action="store_true", help="Send a notification (Telegram/email) when pending decisions exist")
    triage.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")

    triage_apply = sub.add_parser("triage-apply", help="Apply a triage decision action (approve/reject/defer)")
    triage_apply.add_argument("decision_id", help="Decision id (stable hash) under <data_dir>/triage_queue/")
    triage_apply.add_argument("action", choices=["approve", "reject", "defer"], help="Action to apply")
    triage_apply.add_argument(
        "--data-dir",
        default=None,
        help="Gateway data dir (defaults to ABSTRACTGATEWAY_DATA_DIR or ./runtime/gateway)",
    )
    triage_apply.add_argument(
        "--repo-root",
        default=None,
        help="Repo root containing docs/backlog (auto-detected from CWD if omitted)",
    )

    be = sub.add_parser("backlog-exec-runner", help="Run backlog execution runner (consumes backlog_exec_queue)")
    be.add_argument(
        "--data-dir",
        default=None,
        help="Gateway data dir (defaults to ABSTRACTGATEWAY_DATA_DIR or ./runtime/gateway)",
    )
    be.add_argument(
        "--repo-root",
        default=None,
        help="Repo root containing docs/backlog (defaults to ABSTRACTGATEWAY_TRIAGE_REPO_ROOT or CWD)",
    )

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

    if args.cmd == "triage-reports":
        from pathlib import Path

        from .maintenance.action_tokens import build_action_links
        from .maintenance.notifier import send_email_notification, send_telegram_notification
        from .maintenance.triage import triage_reports
        from .maintenance.triage_queue import decisions_dir, iter_decisions

        data_dir = Path(str(args.data_dir or "")).expanduser().resolve() if args.data_dir else None
        if data_dir is None:
            data_dir = Path(os.getenv("ABSTRACTGATEWAY_DATA_DIR", "./runtime/gateway")).expanduser().resolve()

        repo_root = Path(str(args.repo_root)).expanduser().resolve() if args.repo_root else None

        out = triage_reports(
            gateway_data_dir=data_dir,
            repo_root=repo_root,
            write_drafts=bool(args.write_drafts),
            enable_llm=bool(args.llm),
        )
        pending = []
        qdir = decisions_dir(gateway_data_dir=data_dir)
        pending = [d for d in iter_decisions(qdir) if d.status == "pending"]

        if (bool(args.print_actions) or bool(args.notify)) and args.action_base_url:
            secret = os.getenv("ABSTRACTGATEWAY_TRIAGE_ACTION_SECRET") or os.getenv("ABSTRACT_TRIAGE_ACTION_SECRET") or ""
            secret = str(secret).strip()
            if secret:
                out["pending_decisions"] = len(pending)
                out["action_links"] = {}
                for d in pending[:25]:
                    out["action_links"][d.decision_id] = build_action_links(
                        decision_id=d.decision_id,
                        base_url=str(args.action_base_url),
                        secret=secret,
                    )
            else:
                out["action_links_error"] = "Missing TRIAGE_ACTION_SECRET (links disabled)"

        if bool(args.notify) and pending:
            # Compose a compact, actionable digest.
            lines = [f"Triage: {len(pending)} pending report decisions"]
            for d in pending[:10]:
                lines.append(f"- {d.decision_id}: {d.report_relpath}")
                if d.missing_fields:
                    lines.append(f"  missing: {', '.join(d.missing_fields[:3])}")
                links = (out.get("action_links") or {}).get(d.decision_id) if isinstance(out.get("action_links"), dict) else None
                if isinstance(links, dict) and links:
                    lines.append(f"  approve: {links.get('approve')}")
                    lines.append(f"  defer 1d: {links.get('defer_1d')}")
                    lines.append(f"  defer 7d: {links.get('defer_7d')}")
                    lines.append(f"  reject: {links.get('reject')}")
                else:
                    lines.append(f"  approve: abstractgateway triage-apply {d.decision_id} approve")
                    lines.append(f"  reject: abstractgateway triage-apply {d.decision_id} reject")
                    lines.append(f"  defer:  ABSTRACT_TRIAGE_DEFER_DAYS=7 abstractgateway triage-apply {d.decision_id} defer")
            body = "\n".join(lines).strip() + "\n"
            # Telegram first (short), then email (full).
            ok_tg, err_tg = send_telegram_notification(text=body[:3500])
            ok_em, err_em = send_email_notification(subject=f"[AbstractFramework] Triage pending ({len(pending)})", body_text=body)
            out["notify"] = {
                "telegram": {"ok": ok_tg, "error": err_tg},
                "email": {"ok": ok_em, "error": err_em},
            }
        if bool(args.json):
            print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"Reports scanned: {out.get('reports')}")
            print(f"Decision queue: {out.get('decisions_dir')}")
            if out.get("drafts_written"):
                print("Drafts written:")
                for p in out["drafts_written"]:
                    print(f"  - {p}")
            if out.get("action_links"):
                print("Action links (pending):")
                for did, links in list(out["action_links"].items())[:10]:
                    print(f"  - {did}:")
                    for k, v in links.items():
                        print(f"      {k}: {v}")
        return

    if args.cmd == "backlog-exec-runner":
        from pathlib import Path

        from .maintenance.backlog_exec_runner import BacklogExecRunner, BacklogExecRunnerConfig

        data_dir = Path(str(args.data_dir or "")).expanduser().resolve() if args.data_dir else None
        if data_dir is None:
            data_dir = Path(os.getenv("ABSTRACTGATEWAY_DATA_DIR", "./runtime/gateway")).expanduser().resolve()

        repo_root = Path(str(args.repo_root)).expanduser().resolve() if args.repo_root else None
        if repo_root is None:
            rr = os.getenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT") or os.getenv("ABSTRACT_TRIAGE_REPO_ROOT") or ""
            repo_root = Path(rr).expanduser().resolve() if rr.strip() else Path.cwd().expanduser().resolve()
        os.environ["ABSTRACTGATEWAY_TRIAGE_REPO_ROOT"] = str(repo_root)

        cfg = BacklogExecRunnerConfig.from_env()
        cfg = BacklogExecRunnerConfig(
            enabled=True,
            poll_interval_s=cfg.poll_interval_s,
            executor=cfg.executor,
            notify=cfg.notify,
            codex_bin=cfg.codex_bin,
            codex_model=cfg.codex_model,
            codex_sandbox=cfg.codex_sandbox,
            codex_approvals=cfg.codex_approvals,
        )

        stop = threading.Event()

        def _handle(_signum, _frame) -> None:  # pragma: no cover
            stop.set()

        try:
            signal.signal(signal.SIGINT, _handle)
            signal.signal(signal.SIGTERM, _handle)
        except Exception:
            pass

        runner = BacklogExecRunner(gateway_data_dir=data_dir, cfg=cfg)
        runner.start()
        try:
            while not stop.is_set():
                stop.wait(0.5)
        finally:
            runner.stop()
        return

    if args.cmd == "triage-apply":
        from pathlib import Path

        from .maintenance.triage import apply_decision_action

        data_dir = Path(str(args.data_dir or "")).expanduser().resolve() if args.data_dir else None
        if data_dir is None:
            data_dir = Path(os.getenv("ABSTRACTGATEWAY_DATA_DIR", "./runtime/gateway")).expanduser().resolve()
        repo_root = Path(str(args.repo_root)).expanduser().resolve() if args.repo_root else None

        decision, err = apply_decision_action(
            gateway_data_dir=data_dir,
            decision_id=str(args.decision_id),
            action=str(args.action),
            repo_root=repo_root,
        )
        if err:
            raise SystemExit(err)
        if decision is None:
            raise SystemExit("No decision updated")
        print(f"Updated decision {decision.decision_id}: status={decision.status} draft={decision.draft_relpath or '(none)'}")
        return

    raise SystemExit(2)

if __name__ == "__main__":
    main()
