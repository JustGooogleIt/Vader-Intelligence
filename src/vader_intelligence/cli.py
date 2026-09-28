import argparse
import json
import sqlite3
import sys
from contextlib import nullcontext

from .collector import Collector
from .config import Config
from .replay import import_fixture, replay
from .storage import Store, writer_lock
from .transport import Reader


def parser():
    p = argparse.ArgumentParser(description="Vader Intelligence: public MLB market collection")
    p.add_argument("--config", help="TOML configuration")
    p.add_argument("--db", help="override local SQLite database path")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("db-init")
    for name in ("discover", "collect", "live-check"):
        sub = commands.add_parser(name)
        sub.add_argument("--resume", help="resume this unfinished run ID")
        if name == "collect":
            sub.add_argument(
                "--once",
                action="store_true",
                required=True,
                help="perform one bounded collection pass",
            )
    sub = commands.add_parser("replay")
    sub.add_argument("--fixture", help="import deterministic local fixture manifest; no HTTP")
    commands.add_parser("health")
    return p


def execute(args):
    config = Config.load(args.config, args.db)
    with nullcontext() if args.command == "health" else writer_lock(config.database):
        store = Store(
            config.database, min_free_bytes=config.min_free_bytes, readonly=args.command == "health"
        )
        try:
            if args.command == "db-init":
                return {"status": "complete", "schema_version": 1, "database": str(store.path)}
            if args.command == "health":
                result = store.health(config.health_stale_seconds)
                latest = result["latest_run"]
                result["status"] = (
                    "complete"
                    if latest
                    and latest["status"] == "complete"
                    and not (
                        result["stale"] or result["pending_fetches"] or result["storage_pressure"]
                    )
                    else "unhealthy"
                )
                return result
            if args.command == "replay":
                return (
                    import_fixture(store, args.fixture, config) if args.fixture else replay(store)
                )
            reader = Reader(store, config)
            try:
                result = Collector(store, reader, config).run(kind=args.command, resume=args.resume)
            finally:
                reader.close()
        finally:
            store.close()
        if args.command == "live-check":
            reopened = Store(config.database, min_free_bytes=config.min_free_bytes)
            try:
                errors = reopened.verify_integrity()
                result["archive_integrity"] = "failed" if errors else "verified_after_reopen"
                result["errors"].extend(errors)
                for stage in ("series", "document", "event", "schedule", "markets", "book"):
                    count = reopened.db.execute(
                        "SELECT COUNT(*) FROM fetches f JOIN requests r ON r.id=f.request_id "
                        "WHERE r.run_id=? AND r.stage=? AND f.state='ok'",
                        (result["run_id"], stage),
                    ).fetchone()[0]
                    if not count:
                        result["errors"].append("live check lacks successful " + stage)
                if result["errors"] and result["status"] == "complete":
                    result["status"] = "failed"
                reopened.finish_run(result["run_id"], result["status"], result)
            finally:
                reopened.close()
        return result


def main():
    args = parser().parse_args()
    try:
        result = execute(args)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return (
            0 if result["status"] == "complete" else 3 if result["status"] == "inconclusive" else 1
        )
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "error": "operator interrupted collection"}))
        return 130
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)[:500]}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
