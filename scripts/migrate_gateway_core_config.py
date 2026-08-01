#!/usr/bin/env python3
"""One-shot: merge a legacy Gateway-scoped AbstractCore store into THE Core store.

The Gateway runs this itself at startup; this is the same migration for an
operator who wants to run it (and read what it did) before starting anything,
or to inspect the plan first with `--dry-run`.

    python scripts/migrate_gateway_core_config.py --dry-run
    python scripts/migrate_gateway_core_config.py --data-dir ./runtime

Semantics, backups and the merge rules are stated in
`abstractgateway/core_config_migration.py`. Idempotent: a second run finds no
legacy store and says so.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Gateway data dir holding config/abstractcore.json (default: ABSTRACTGATEWAY_DATA_DIR or ./runtime)",
    )
    parser.add_argument(
        "--core-file",
        default=None,
        help="AbstractCore store to merge INTO (default: whatever AbstractCore itself resolves)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report the merge without touching any file")
    parser.add_argument("--json", action="store_true", help="Emit the raw report as JSON")
    args = parser.parse_args(argv)

    from abstractgateway.core_config_migration import (
        format_migration_report,
        migrate_legacy_gateway_stores,
    )

    report = migrate_legacy_gateway_stores(
        data_dir=Path(args.data_dir) if args.data_dir else None,
        core_file=Path(args.core_file) if args.core_file else None,
        dry_run=bool(args.dry_run),
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        lines = format_migration_report(report)
        if not lines:
            print("[core-config] no legacy gateway store found; nothing to migrate")
        for line in lines:
            print(line)
    return 0 if all(bool(part.get("ok", False)) for part in report.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
