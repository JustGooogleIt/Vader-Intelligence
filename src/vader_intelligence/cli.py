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
        choices=[2, 3],
        help="3: explicit migration 005 (and 004 from schema 3) opening record contract 3;"
        " 2 is frozen (no-op at schema 4/5, refused on schema 3)",
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
    evaluate = commands.add_parser("evaluate", help="materialize one immutable evaluation")
    evaluate.add_argument("--research-config", help="research protocol TOML ([research] only)")
    evaluate.add_argument("--dataset", help="dataset namespace to evaluate (default: protocol)")
    evaluate.add_argument(
        "--view", required=True, choices=("forward-shadow", "cutoff-reconstruction")
    )
    evaluate.add_argument("--outcomes-as-of", required=True, help="aware UTC retrieval boundary")
    evaluate.add_argument("--source-ceiling", type=int, help="fetch sequence ceiling")
    evaluate.add_argument("--from", dest="start", help="first scheduled date (YYYY-MM-DD)")
    evaluate.add_argument("--through", help="last scheduled date (YYYY-MM-DD)")
    evaluate.add_argument("--collector-config-hash", help="evaluate this collector cohort only")
    evaluate.add_argument("--budget", type=float, default=30.0)
    evaluate.add_argument(
        "--record-contract",
        type=int,
        choices=[2, 3],
        default=3,
        help="decisions of this record contract only (2 = legacy cohort; default 3)",
    )
    evaluation = commands.add_parser("evaluation", help="read-only evaluation inspection")
    evaluation_commands = evaluation.add_subparsers(dest="evaluation_command", required=True)
    einspect = evaluation_commands.add_parser("inspect")
    einspect.add_argument("--id", type=int, help="one evaluation; omit to list recent ones")
    einspect.add_argument("--items", action="store_true", help="include the item rows")
    einspect.add_argument("--limit", type=int, default=20)
    einspect.add_argument("--before", type=int)
    einspect.add_argument("--dataset")
    einspect.add_argument("--view", choices=("forward-shadow", "cutoff-reconstruction"))
    ereport = evaluation_commands.add_parser("report", help="one evaluation as text or JSON")
    ereport.add_argument("--id", type=int, required=True)
    ereport.add_argument("--format", choices=("text", "json"), default="text")
    ereport.add_argument("--items", action="store_true", help="include items in JSON output")
    ereport.add_argument("--export", help="write the JSON document to this new file")
    ecompare = evaluation_commands.add_parser(
        "compare", help="two evaluations on the opportunities both scored"
    )
    ecompare.add_argument("--ids", required=True, help="two evaluation ids, comma separated")
    ecompare.add_argument("--format", choices=("text", "json"), default="text")
    research = commands.add_parser("research", help="reproducible offline research runs")
    research_commands = research.add_subparsers(dest="research_command", required=True)
    rrun = research_commands.add_parser("run", help="reconstruct (if needed) and evaluate a scope")
    rrun.add_argument("--research-config", help="research protocol TOML ([research] only)")
    rrun.add_argument("--dataset", help="study namespace (required for cutoff-reconstruction)")
    rrun.add_argument("--view", required=True, choices=("forward-shadow", "cutoff-reconstruction"))
    rrun.add_argument("--outcomes-as-of", required=True)
    rrun.add_argument("--from", dest="start", required=True)
    rrun.add_argument("--through", required=True)
    rrun.add_argument("--source-ceiling", type=int)
    rrun.add_argument("--development-through", help="last development date (split)")
    rrun.add_argument("--holdout-from", help="first held-out date (split)")
    rrun.add_argument("--budget", type=float, default=120.0)
    rrun.add_argument("--out", help="write the run summary JSON to this new file")
    rinspect = research_commands.add_parser("inspect", help="read-only research run inspection")
    rinspect.add_argument("--run-id", help="one run; omit to list recent runs")
    rinspect.add_argument("--limit", type=int, default=20)
    history = commands.add_parser(
        "history", help="public historical market data for research (dedicated archive)"
    )
    history_commands = history.add_subparsers(dest="history_command", required=True)
    hacquire = history_commands.add_parser("acquire", help="bounded, resumable acquisition")
    hacquire.add_argument("--from", dest="start", required=True, help="first game date")
    hacquire.add_argument("--through", required=True, help="last game date (max 250 days)")
    hacquire.add_argument("--resume", help="resume this unfinished historical-acquire run")
    hacquire.add_argument("--max-requests", type=int, default=8000)
    hacquire.add_argument("--budget", type=float, default=7200.0, help="seconds")
    hacquire.add_argument("--window-seconds", type=int, default=3600)
    hsummary = history_commands.add_parser("summary", help="read-only interpretation counts")
    hsummary.add_argument("--run-id", help="one acquisition run; omit for the whole archive")
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
                if version not in (2, 3, 4, 5):
                    raise ValueError(
                        "forecast migrate requires schema 2; run settlement migrate first"
                    )
                if args.record_contract is not None and version < 3:
                    # Command prerequisite checked before Store opens (and switches the
                    # journal to WAL): a rejected migration leaves the file untouched.
                    raise ValueError(
                        f"record contract {args.record_contract} requires schema 3;"
                        " run forecast migrate first"
                    )
                if args.record_contract == 2 and version == 3:
                    raise ValueError(
                        "record contract 2 is frozen (decision D-001); use --record-contract 3"
                    )
            finally:
                probe.close()
            store = Store(config.database, min_free_bytes=config.min_free_bytes)
            try:
                changed = migrate(store.db, record_contract=args.record_contract)
                return {
                    "status": "complete",
                    "schema_version": store.schema_version(),
                    "record_contract": {5: 3, 4: 2}.get(store.schema_version(), 1),
                    "migrated": changed,
                    "database": str(store.path),
                }
            finally:
                store.close()
    if args.command == "settlement":
        from .settlement.cli import execute as settlement_execute

        return settlement_execute(args, config)
    if args.command in ("evaluate", "evaluation"):
        return _evaluation(args, config)
    if args.command == "research":
        return _research(args, config)
    if args.command == "history":
        return _history(args, config)
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


def _evaluation(args, config):
    from .evaluation.service import evaluate, inspect_evaluation, list_evaluations
    from .forecast.reconstruct import parse_date
    from .forecast.research import Protocol

    if args.command == "evaluation":
        from .research import report as reports

        store = Store(config.database, min_free_bytes=config.min_free_bytes, readonly=True)
        try:
            if args.evaluation_command == "report":
                document = reports.machine_report(store.db, args.id, include_items=args.items)
                if args.export:
                    document["exported_to"] = reports.export(args.export, document)
                if args.format == "text":
                    return {"status": "complete", "text": reports.text_report(store.db, args.id)}
                return document | {"status": "complete"}
            if args.evaluation_command == "compare":
                ids = [int(v) for v in args.ids.split(",")]
                if len(ids) != 2:
                    raise ValueError("--ids needs exactly two evaluation ids")
                comparison = reports.compare(store.db, *ids)
                if args.format == "text":
                    return {"status": "complete", "text": reports.text_compare(comparison)}
                return comparison | {"status": "complete"}
            if args.id is not None:
                result = inspect_evaluation(store.db, args.id, items=args.items)
            else:
                result = list_evaluations(
                    store.db,
                    limit=args.limit,
                    before=args.before,
                    dataset=args.dataset,
                    view=args.view,
                )
            result["status"] = "complete"
            return result
        finally:
            store.close()
    from datetime import datetime

    protocol = Protocol.load(args.research_config)
    as_of = datetime.fromisoformat(args.outcomes_as_of.replace("Z", "+00:00"))
    with writer_lock(config.database):
        store = Store(config.database, min_free_bytes=config.min_free_bytes)
        try:
            return evaluate(
                store,
                protocol,
                view=args.view,
                outcomes_as_of=as_of,
                dataset=args.dataset,
                source_ceiling=args.source_ceiling,
                start_date=None if args.start is None else parse_date(args.start),
                through_date=None if args.through is None else parse_date(args.through),
                collector_config_hash=args.collector_config_hash,
                budget=args.budget,
                record_contract=args.record_contract,
            )
        finally:
            store.close()


def _research(args, config):
    from datetime import datetime

    from .forecast.reconstruct import parse_date
    from .forecast.research import Protocol
    from .research import report as reports
    from .research.runner import inspect_run, list_runs, run

    if args.research_command == "inspect":
        store = Store(config.database, min_free_bytes=config.min_free_bytes, readonly=True)
        try:
            result = (
                inspect_run(store.db, args.run_id)
                if args.run_id
                else list_runs(store.db, limit=args.limit)
            )
            result["status"] = "complete"
            return result
        finally:
            store.close()
    protocol = Protocol.load(args.research_config)
    as_of = datetime.fromisoformat(args.outcomes_as_of.replace("Z", "+00:00"))
    with writer_lock(config.database):
        store = Store(config.database, min_free_bytes=config.min_free_bytes)
        try:
            summary = run(
                store,
                protocol,
                view=args.view,
                outcomes_as_of=as_of,
                start_date=parse_date(args.start),
                through_date=parse_date(args.through),
                dataset=args.dataset,
                source_ceiling=args.source_ceiling,
                development_through=None
                if args.development_through is None
                else parse_date(args.development_through),
                holdout_from=None if args.holdout_from is None else parse_date(args.holdout_from),
                budget=args.budget,
            )
        finally:
            store.close()
    if args.out:
        summary["exported_to"] = reports.export(args.out, summary)
    return summary


HISTORY_RUN_KINDS = ("historical-acquire",)


def _history(args, config):
    """Historical acquisition writes only to a dedicated archive: one holding nothing but
    historical-acquire runs, so collector and forecast evidence never share it."""
    from .forecast.reconstruct import parse_date
    from .history import interpret
    from .history.acquire import Acquisition

    if args.history_command == "summary":
        store = Store(config.database, min_free_bytes=config.min_free_bytes, readonly=True)
        try:
            result = interpret.summary(store.db, args.run_id)
        finally:
            store.close()
        return {"status": "complete", **result}
    with writer_lock(config.database):
        store = Store(config.database, min_free_bytes=config.min_free_bytes)
        try:
            kinds = {r[0] for r in store.db.execute("SELECT DISTINCT kind FROM runs")}
            if kinds - set(HISTORY_RUN_KINDS) or store.schema_version() != 1:
                raise ValueError(
                    "history acquisition requires a dedicated schema-1 archive holding only"
                    " historical-acquire runs"
                )
            reader = Reader(store, config, profile="history")
            try:
                return Acquisition(
                    store, reader, max_requests=args.max_requests, budget=args.budget
                ).run(
                    start_date=parse_date(args.start),
                    through_date=parse_date(args.through),
                    resume=args.resume,
                    window_seconds=args.window_seconds,
                )
            finally:
                reader.close()
        finally:
            store.close()


def main():
    args = parser().parse_args()
    try:
        result = execute(args)
        if isinstance(result, dict) and set(result) == {"status", "text"}:
            print(result["text"])  # human-readable report
            return 0
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
