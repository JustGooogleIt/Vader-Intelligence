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
    from .settlement.cli import register

    register(commands)
    forecast = commands.add_parser("forecast", help="explicit forecasting storage commands")
    forecast_commands = forecast.add_subparsers(dest="forecast_command", required=True)
    migrate = forecast_commands.add_parser("migrate")
    migrate.add_argument(
        "--record-contract",
        type=int,
        choices=[2],
        help="explicit migration 004: enable record contract 2 on a schema-3 archive",
    )
    tick = forecast_commands.add_parser("tick", help="finite forecast tick; no daemon")
    tick.add_argument("--research-config", help="research protocol TOML ([research] only)")
    tick.add_argument("--dataset", help="override the cohort dataset namespace")
    tick.add_argument("--budget", type=float, default=30.0, help="tick time budget in seconds")
    inspect = forecast_commands.add_parser("inspect", help="read-only recent forecast decisions")
    inspect.add_argument("--research-config")
    inspect.add_argument("--dataset", help="show only this dataset namespace")
    inspect.add_argument("--limit", type=int, default=20, help="max 100")
    inspect.add_argument("--before", type=int, help="keyset: decisions with id below this")
    reconstruct = forecast_commands.add_parser(
        "reconstruct", help="archive-only historical reconstruction; no HTTP, no publication"
    )
    reconstruct.add_argument("--research-config", help="research protocol TOML ([research] only)")
    reconstruct.add_argument("--dataset", required=True, help="explicit reconstruction namespace")
    reconstruct.add_argument(
        "--forward-dataset", help="dataset of the forward cohort whose bindings are reconstructed"
    )
    reconstruct.add_argument("--from", dest="start", required=True, help="first original date")
    reconstruct.add_argument("--through", required=True, help="last original date (max 31 days)")
    reconstruct.add_argument("--source-ceiling", type=int, help="fetch sequence ceiling")
    reconstruct.add_argument("--budget", type=float, default=30.0)
    return p


def execute(args):
    config = Config.load(args.config, args.db)
    if args.command == "forecast":
        from pathlib import Path

        from .forecast.schema import migrate

        if args.forecast_command in ("tick", "inspect", "reconstruct"):
            return _forecast_research(args, config)
        if not Path(config.database).is_file():
            raise ValueError("forecast migrate requires an existing schema-2 archive")
        with writer_lock(config.database):
            # Preflight read-only so an empty/schema-0 path is never initialized
            # as an accidental side effect of a rejected forecast migration.
            probe = sqlite3.connect(Path(config.database).resolve().as_uri() + "?mode=ro", uri=True)
            try:
                version = probe.execute("PRAGMA user_version").fetchone()[0]
                if version not in (2, 3, 4):
                    raise ValueError(
                        "forecast migrate requires schema 2; run settlement migrate first"
                    )
                if args.record_contract == 2 and version < 3:
                    # Command prerequisite checked before Store opens (and switches the
                    # journal to WAL): a rejected migration leaves the file untouched.
                    raise ValueError(
                        "record contract 2 requires schema 3; run forecast migrate first"
                    )
            finally:
                probe.close()
            store = Store(config.database, min_free_bytes=config.min_free_bytes)
            try:
                changed = migrate(store.db, record_contract=args.record_contract)
                return {
                    "status": "complete",
                    "schema_version": store.schema_version(),
                    "record_contract": 2 if store.schema_version() >= 4 else 1,
                    "migrated": changed,
                    "database": str(store.path),
                }
            finally:
                store.close()
    if args.command == "settlement":
        from .settlement.cli import execute as settlement_execute

        return settlement_execute(args, config)
    with nullcontext() if args.command == "health" else writer_lock(config.database):
        store = Store(
            config.database, min_free_bytes=config.min_free_bytes, readonly=args.command == "health"
        )
        try:
            if args.command == "db-init":
                return {
                    "status": "complete",
                    "schema_version": store.db.execute("PRAGMA user_version").fetchone()[0],
                    "database": str(store.path),
                }
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
                if errors:
                    result["status"] = "failed"
                for stage in ("series", "document", "event", "schedule", "markets", "book"):
                    count = reopened.db.execute(
                        "SELECT COUNT(*) FROM fetches f JOIN requests r ON r.id=f.request_id "
                        "WHERE r.run_id=? AND r.stage=? AND f.state='ok'",
                        (result["run_id"], stage),
                    ).fetchone()[0]
                    # Empty live universes legitimately have no event or book retrieval.
                    if not count and not (
                        result["status"] == "inconclusive" and stage in {"event", "book"}
                    ):
                        result["errors"].append("live check lacks successful " + stage)
                if result["errors"] and result["status"] in {"complete", "inconclusive"}:
                    result["status"] = "failed"
                reopened.finish_run(result["run_id"], result["status"], result)
            finally:
                reopened.close()
        return result


def _forecast_research(args, config):
    from contextlib import nullcontext

    from .forecast.research import Protocol
    from .forecast.tick import inspect, tick

    read_only = args.forecast_command == "inspect"
    # The reconstruction dataset is its own explicit namespace, never the protocol's.
    protocol = Protocol.load(
        args.research_config,
        dataset=args.forward_dataset if args.forecast_command == "reconstruct" else args.dataset,
    )
    with nullcontext() if read_only else writer_lock(config.database):
        store = Store(config.database, min_free_bytes=config.min_free_bytes, readonly=read_only)
        try:
            if read_only:
                result = inspect(
                    store,
                    limit=args.limit,
                    after=args.before,
                    namespace=protocol.observer_namespace,
                    dataset=args.dataset,
                )
                result["status"] = "complete"
                return result
            if args.forecast_command == "reconstruct":
                from .forecast.reconstruct import parse_date, reconstruct

                return reconstruct(
                    store,
                    protocol,
                    dataset=args.dataset,
                    start_date=parse_date(args.start),
                    through_date=parse_date(args.through),
                    source_ceiling=args.source_ceiling,
                    budget=args.budget,
                )
            return tick(store, protocol, budget=args.budget)
        finally:
            store.close()


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
