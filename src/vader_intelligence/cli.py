import argparse
import json
import sqlite3
import sys
from contextlib import nullcontext
from pathlib import Path

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
        choices=[2, 3, 4],
        help="3: migration 005 (record contract 3, schema 5); 4: migration 006 (decision"
        " contract 4 with registered forecasters, schema 6); earlier markers first; 2 is"
        " frozen (no-op at schema >= 4, refused on schema 3)",
    )
    tick = forecast_commands.add_parser("tick", help="finite forecast tick; no daemon")
    tick.add_argument("--research-config", help="research protocol TOML ([research] only)")
    tick.add_argument("--dataset", help="override the cohort dataset namespace")
    tick.add_argument("--budget", type=float, default=30.0, help="tick time budget in seconds")
    tick.add_argument(
        "--model-registry", help="research registry holding the protocol's forecasters"
    )
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
    reconstruct.add_argument(
        "--model-registry", help="research registry holding the protocol's forecasters"
    )
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
        choices=[2, 3, 4],
        help="decisions of this record contract only (default: the archive's current"
        " contract, 4 on schema 6, else 3; 2 = legacy cohort)",
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
    hacquire.add_argument(
        "--max-requests",
        type=int,
        default=8000,
        help="bound on logical requests started by this invocation (requests_made); each"
        " may retry up to the configured max_attempts, so HTTP attempts (attempts_made)"
        " can exceed it",
    )
    hacquire.add_argument("--budget", type=float, default=7200.0, help="seconds")
    hacquire.add_argument("--window-seconds", type=int, default=3600)
    hacquire.add_argument(
        "--identity-rules",
        default="default",
        help="comma list; add history-date-only to admit 2025-era contracts with no stated"
        " start (rows are marked start_corroborated=false)",
    )
    hacquire.add_argument(
        "--no-pre-cutoff", action="store_true", help="skip the feature window ending at C"
    )
    hacquire.add_argument(
        "--post-cutoff-seconds",
        type=int,
        default=0,
        help="also fetch execution-only candles for (C, C + N] (never read by features)",
    )
    hsummary = history_commands.add_parser("summary", help="read-only interpretation counts")
    hsummary.add_argument("--run-id", help="one acquisition run; omit for the whole archive")
    hsummary.add_argument("--identity-rules", default="default")
    dataset = commands.add_parser("dataset", help="point-in-time research datasets (registry)")
    dataset_commands = dataset.add_subparsers(dest="dataset_command", required=True)
    dbuild = dataset_commands.add_parser("build", help="build and register one dataset")
    dbuild.add_argument("--registry", required=True, help="research registry directory")
    dbuild.add_argument("--source", required=True, choices=("history", "collection"))
    dbuild.add_argument("--history-db", required=True, help="historical-acquisition archive")
    dbuild.add_argument("--collection-db", help="collection archive (source=collection)")
    dbuild.add_argument("--quote-source", choices=("listing", "book"), default="listing")
    dbuild.add_argument("--lineage", choices=("live", "synthetic"), default="live")
    dbuild.add_argument("--feature-set", required=True)
    dbuild.add_argument("--from", dest="start", required=True)
    dbuild.add_argument("--through", required=True)
    dbuild.add_argument("--name", required=True)
    dbuild.add_argument("--identity-rules", default="default")
    dpin = dataset_commands.add_parser(
        "pin-holdout", help="pin a dataset's final holdout start (recorded once, append-only)"
    )
    dpin.add_argument("--registry", required=True)
    dpin.add_argument("--dataset", required=True)
    dpin.add_argument("--from", dest="holdout_from", required=True, help="first holdout date")
    dpin.add_argument("--actor", default="operator")
    dinspect = dataset_commands.add_parser("inspect", help="read-only dataset listing/manifest")
    dinspect.add_argument("--registry", required=True)
    dinspect.add_argument("--id", help="one dataset; omit to list")
    model = commands.add_parser("model", help="train, evaluate and inspect registered models")
    model_commands = model.add_subparsers(dest="model_command", required=True)
    for name in ("train", "holdout"):
        sub = model_commands.add_parser(name)
        sub.add_argument("--registry", required=True)
        sub.add_argument("--dataset", required=True, help="dataset id")
        sub.add_argument("--development-through", required=True)
        sub.add_argument("--validation-through", required=True)
        sub.add_argument("--embargo-days", type=int, default=1)
        sub.add_argument("--seed", type=int, default=0)
        if name == "train":
            sub.add_argument("--family", required=True, choices=("logistic", "gbm"))
            sub.add_argument(
                "--calibration", choices=("platt", "isotonic", "none"), default="platt"
            )
            sub.add_argument("--folds", type=int, default=4)
            sub.add_argument("--max-configs", type=int, default=24)
            sub.add_argument("--deadline", type=float, default=900.0, help="search seconds")
        else:
            sub.add_argument("--model", required=True, help="model id (artifact SHA-256)")
    symbolic = commands.add_parser("symbolic", help="bounded symbolic search on a dataset")
    symbolic_commands = symbolic.add_subparsers(dest="symbolic_command", required=True)
    ssearch = symbolic_commands.add_parser("search")
    ssearch.add_argument("--registry", required=True)
    ssearch.add_argument("--dataset", required=True)
    ssearch.add_argument("--development-through", required=True)
    ssearch.add_argument("--validation-through", required=True)
    ssearch.add_argument("--embargo-days", type=int, default=1)
    ssearch.add_argument("--population", type=int, default=200)
    ssearch.add_argument("--generations", type=int, default=30)
    ssearch.add_argument("--max-candidates", type=int, default=20000)
    ssearch.add_argument("--max-seconds", type=float, default=60.0)
    ssearch.add_argument("--max-depth", type=int, default=5)
    ssearch.add_argument("--max-nodes", type=int, default=25)
    ssearch.add_argument("--seed", type=int, default=0)
    ledger = commands.add_parser("ledger", help="trading ledger operator commands (no orders)")
    ledger_commands = ledger.add_subparsers(dest="ledger_command", required=True)
    for name in ("init", "show", "verify", "kill-switch", "limits"):
        sub = ledger_commands.add_parser(name)
        sub.add_argument("--ledger", required=True, help="ledger SQLite path (its own file)")
        sub.add_argument("--environment", required=True, choices=("paper", "live"))
        if name == "init":
            sub.add_argument("--deposit", help="paper only: initial simulated cash (dollars)")
        if name == "kill-switch":
            state = sub.add_mutually_exclusive_group(required=True)
            state.add_argument("--engage", action="store_true")
            state.add_argument("--release", action="store_true")
            sub.add_argument("--reason", required=True)
            sub.add_argument("--actor", required=True)
        if name == "limits":
            sub.add_argument("--file", required=True, help="TOML with a [limits] table")
            sub.add_argument("--actor", required=True)
    live = commands.add_parser(
        "live", help="live adapter readiness (local checks only; never activates or orders)"
    )
    live_commands = live.add_subparsers(dest="live_command", required=True)
    lready = live_commands.add_parser("readiness", help="evaluate every activation gate locally")
    lready.add_argument("--activation-config", required=True, help="live activation TOML")
    lready.add_argument("--acknowledge", required=True, help="SHA-256 of the config file")
    lready.add_argument("--ledger", required=True, help="live ledger path")
    ldry = live_commands.add_parser(
        "dry-run", help="forward-shadow forecasts -> would-be order requests; nothing is sent"
    )
    ldry.add_argument("--archive", required=True, help="evidence archive (opened read-only)")
    ldry.add_argument("--forecaster", required=True, help="registered forecaster id")
    ldry.add_argument("--ledger", required=True, help="new PAPER ledger for the dry run")
    ldry.add_argument("--kalshi-environment", choices=("demo", "production"), default="demo")
    ldry.add_argument("--cash", default="1000")
    ldry.add_argument("--min-edge", default="0.02")
    ldry.add_argument(
        "--sign-with-throwaway-key",
        action="store_true",
        help="sign with an in-memory key generated for this run (never a real credential)",
    )
    ldry.add_argument(
        "--max-order-notional", help="apply the live gateway's per-order notional cap"
    )
    ldry.add_argument("--max-daily-notional", help="apply the live gateway's daily notional cap")
    minspect = model_commands.add_parser("inspect")
    minspect.add_argument("--registry", required=True)
    minspect.add_argument("--id", help="one model; omit to list")
    backtest = commands.add_parser(
        "backtest", help="historical paper backtest of a registered model (simulated)"
    )
    backtest_commands = backtest.add_subparsers(dest="backtest_command", required=True)
    brun = backtest_commands.add_parser("run")
    brun.add_argument("--registry", required=True)
    brun.add_argument("--history-db", required=True, help="history archive (opened read-only)")
    brun.add_argument("--dataset", required=True)
    brun.add_argument("--model", required=True)
    brun.add_argument("--development-through", required=True)
    brun.add_argument("--validation-through", required=True)
    brun.add_argument("--embargo-days", type=int, default=1)
    brun.add_argument(
        "--segment", choices=("development", "validation", "holdout"), default="validation"
    )
    brun.add_argument(
        "--acknowledge-holdout",
        action="store_true",
        help="required for --segment holdout: the run is recorded in the holdout ledger",
    )
    brun.add_argument("--min-edge", default="0.02")
    brun.add_argument("--fee-schedule", default="kalshi-2026-02-general")
    brun.add_argument("--initial-cash", default="1000")
    brun.add_argument(
        "--assumed-depth", action="append", default=[], help="SCENARIO depth (repeatable)"
    )
    brun.add_argument("--ledger", help="single run into this new paper ledger (else: grid)")
    brun.add_argument("--seed", type=int, default=0)
    pods = commands.add_parser("pods", help="research pods: orchestration and candidates")
    pods_commands = pods.add_subparsers(dest="pods_command", required=True)
    for name in (
        "init",
        "register",
        "snapshot",
        "execution-snapshot",
        "submit",
        "run",
        "status",
        "accept",
        "evaluate",
        "candidate",
        "holdout",
        "autopilot",
    ):
        sub = pods_commands.add_parser(name)
        sub.add_argument("--root", required=True, help="orchestration root directory")
        if name in ("snapshot", "execution-snapshot", "accept", "evaluate", "holdout"):
            sub.add_argument("--registry", required=True)
        if name == "autopilot":
            sub.add_argument("--registry", help="required with --accept-best")
            sub.add_argument("--pod", required=True)
            sub.add_argument("--snapshot", required=True, help="snapshot sha256 (pods snapshot)")
            sub.add_argument(
                "--planner",
                choices=("heuristic", "vendor"),
                default="heuristic",
                help="vendor: a real provider, which raises (paid calls are disabled)",
            )
            sub.add_argument("--vendor", default="unspecified")
            sub.add_argument("--rounds", type=int, default=3)
            sub.add_argument("--max-tasks", type=int, default=12)
            sub.add_argument("--max-tasks-per-round", type=int, default=4)
            sub.add_argument("--deadline-seconds", type=float, default=3600.0)
            sub.add_argument("--seed", type=int, default=0)
            sub.add_argument(
                "--accept-best",
                action="store_true",
                help="accept the best model by development score; the evaluator decides",
            )
        if name in ("snapshot", "execution-snapshot"):
            sub.add_argument("--dataset", required=True)
            sub.add_argument("--development-through", required=True)
            sub.add_argument("--validation-through", required=True)
            sub.add_argument("--embargo-days", type=int, default=1)
        if name == "execution-snapshot":
            sub.add_argument("--history-db", required=True)
        if name == "register":
            sub.add_argument("--pod", required=True)
            sub.add_argument(
                "--role",
                required=True,
                choices=("baseline_models", "symbolic_search", "robustness_execution"),
            )
            sub.add_argument("--max-concurrent", type=int, default=1)
            sub.add_argument("--wall-seconds", type=float, default=900.0)
            sub.add_argument("--memory-mb", type=int, default=4096)
            sub.add_argument("--cpu-seconds-per-day", type=float, default=4 * 3600.0)
        if name == "submit":
            sub.add_argument("--pod", required=True)
            sub.add_argument("--operation", required=True)
            sub.add_argument("--params", required=True, help="JSON object")
            sub.add_argument("--max-attempts", type=int, default=2)
        if name == "run":
            sub.add_argument("--max-steps", type=int, default=1_000_000)
            sub.add_argument("--deadline-seconds", type=float, default=3600.0)
        if name == "accept":
            sub.add_argument("--task", required=True)
        if name in ("evaluate", "candidate", "holdout"):
            sub.add_argument("--candidate", required=True)
        if name in ("evaluate", "autopilot"):
            sub.add_argument(
                "--allow-weaker-criteria",
                action="store_true",
                help="record a labelled decision under criteria weaker than the D-008 default",
            )
            sub.add_argument("--min-n", type=int, default=100)
            sub.add_argument("--metric", choices=("log_loss", "brier"), default="log_loss")
            sub.add_argument("--baseline", default="market:mid_c")
        if name == "holdout":
            sub.add_argument(
                "--acknowledge-final",
                action="store_true",
                help="required: consumes this candidate's one holdout evaluation",
            )
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
                if version not in (2, 3, 4, 5, 6):
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
                    "record_contract": {6: 4, 5: 3, 4: 2}.get(store.schema_version(), 1),
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
    if args.command == "dataset":
        return _dataset(args)
    if args.command == "model":
        return _model(args)
    if args.command == "symbolic":
        return _symbolic(args)
    if args.command == "ledger":
        return _ledger(args)
    if args.command == "live":
        return _live(args)
    if args.command == "backtest":
        return _backtest(args)
    if args.command == "pods":
        return _pods(args)
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
    runner = None
    if not read_only and protocol.forecasters:
        from .forecast.forecasters import runner_from_registry

        if not args.model_registry:
            raise ValueError("the protocol registers forecasters: --model-registry is required")
        runner = runner_from_registry(args.model_registry, protocol)
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
                    runner=runner,
                )
            return tick(store, protocol, budget=args.budget, runner=runner)
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
            result = interpret.summary(store.db, args.run_id, tuple(args.identity_rules.split(",")))
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
                    identity_rules=tuple(args.identity_rules.split(",")),
                    pre_cutoff=not args.no_pre_cutoff,
                    post_cutoff_seconds=args.post_cutoff_seconds,
                )
            finally:
                reader.close()
        finally:
            store.close()


def _readonly(path):
    if not Path(path).is_file():
        raise ValueError(f"archive not found: {path}")
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.execute("PRAGMA query_only=ON")
    return db


def _dataset(args):
    """Datasets read archives through read-only connections and write only the registry."""
    from .research.registry import Registry

    if args.dataset_command == "pin-holdout":
        registry = Registry(args.registry)
        try:
            pinned = registry.pin_holdout(args.dataset, args.holdout_from, args.actor)
            return {"status": "complete", "dataset": args.dataset, "holdout_from": pinned}
        finally:
            registry.close()
    if args.dataset_command == "inspect":
        registry = Registry(args.registry, readonly=True)
        try:
            if args.id:
                return {"status": "complete", "manifest": registry.dataset(args.id)}
            rows = registry.db.execute(
                "SELECT id, name, created_at, json_extract(manifest_json,'$.rows'),"
                " json_extract(manifest_json,'$.evidence_kind') FROM datasets ORDER BY created_at"
            ).fetchall()
            return {
                "status": "complete",
                "datasets": [
                    dict(zip(("id", "name", "created_at", "rows", "evidence_kind"), r))
                    for r in rows
                ],
            }
        finally:
            registry.close()
    from .datasets import collection as collection_dataset
    from .datasets import history as history_dataset
    from .forecast.reconstruct import parse_date

    rules = tuple(args.identity_rules.split(","))
    history_db = _readonly(args.history_db)
    try:
        if args.source == "history":
            manifest, rows = history_dataset.build(
                history_db,
                feature_set=args.feature_set,
                start_date=parse_date(args.start),
                through_date=parse_date(args.through),
                name=args.name,
                identity_rules=rules,
            )
        else:
            if not args.collection_db:
                raise ValueError("--collection-db is required for source=collection")
            collection_db = _readonly(args.collection_db)
            try:
                manifest, rows = collection_dataset.build(
                    collection_db,
                    history_db,
                    feature_set=args.feature_set,
                    quote_source=args.quote_source,
                    start_date=parse_date(args.start),
                    through_date=parse_date(args.through),
                    name=args.name,
                    identity_rules=rules,
                    lineage=args.lineage,
                )
            finally:
                collection_db.close()
    finally:
        history_db.close()
    registry = Registry(args.registry)
    try:
        dataset_id = registry.register_dataset(manifest, rows)
    finally:
        registry.close()
    summary = {k: manifest[k] for k in ("name", "evidence_kind", "rows", "positives")}
    summary |= {k: manifest[k] for k in ("exclusions", "missing_by_feature")}
    return {"status": "complete" if rows else "inconclusive", "dataset_id": dataset_id, **summary}


def _model(args):
    """Models read datasets from the registry and write only the registry."""
    from .datasets.splits import SplitDefinition
    from .research import training
    from .research.registry import Registry

    if args.model_command == "inspect":
        registry = Registry(args.registry, readonly=True)
        try:
            if args.id:
                row = registry.db.execute("SELECT * FROM models WHERE id=?", (args.id,)).fetchone()
                if row is None:
                    raise ValueError("unknown model")
                evaluations = [
                    {
                        "id": e["id"],
                        "split": e["split"],
                        "selection_biased": bool(e["selection_biased"]),
                        "metrics": json.loads(e["metrics_json"]),
                    }
                    for e in registry.db.execute(
                        "SELECT * FROM evaluations WHERE model_id=? ORDER BY id", (args.id,)
                    )
                ]
                artifact = json.loads(registry.model_artifact(args.id))
                return {
                    "status": "complete",
                    "model": dict(row) | {"summary": json.loads(row["summary_json"])},
                    "training": artifact["training"],
                    "evaluations": evaluations,
                }
            return {
                "status": "complete",
                "models": [
                    dict(r) | {"summary_json": None}
                    for r in registry.db.execute(
                        "SELECT id, family, dataset_id, created_at FROM models ORDER BY created_at"
                    )
                ],
            }
        finally:
            registry.close()
    split = SplitDefinition(args.development_through, args.validation_through, args.embargo_days)
    registry = Registry(args.registry)
    try:
        if args.model_command == "train":
            import time

            return training.train(
                registry,
                args.dataset,
                family=args.family,
                split=split,
                n_folds=args.folds,
                seed=args.seed,
                max_configs=args.max_configs,
                deadline_seconds=args.deadline,
                calibration=args.calibration,
                clock=time.monotonic,
            )
        return training.evaluate_holdout(registry, args.dataset, args.model, split, seed=args.seed)
    finally:
        registry.close()


def _symbolic(args):
    import time

    from .datasets.splits import SplitDefinition
    from .research import symbolic_search
    from .research.registry import Registry
    from .symbolic import SearchConfig

    config = SearchConfig(
        population=args.population,
        generations=args.generations,
        max_candidates=args.max_candidates,
        max_seconds=args.max_seconds,
        max_depth=args.max_depth,
        max_nodes=args.max_nodes,
        seed=args.seed,
    )
    split = SplitDefinition(args.development_through, args.validation_through, args.embargo_days)
    registry = Registry(args.registry)
    try:
        return symbolic_search.run(
            registry, args.dataset, split=split, config=config, clock=time.monotonic
        )
    finally:
        registry.close()


def _backtest(args):
    """Historical paper backtest (simulated; never an order)."""
    from decimal import Decimal

    from .datasets.splits import SplitDefinition
    from .research import backtest
    from .research.registry import Registry
    from .trading.fees import PROVISIONAL_SCHEDULES
    from .trading.marketdata import open_archive
    from .trading.simulator import LatencyModel, PaperConfig, StrategyConfig

    if args.segment == "holdout" and not args.acknowledge_holdout:
        raise ValueError("--segment holdout requires --acknowledge-holdout (ledger-recorded)")
    fees = [f for f in PROVISIONAL_SCHEDULES if f.version == args.fee_schedule]
    if not fees:
        raise ValueError(f"unknown fee schedule {args.fee_schedule!r}")
    depths = [Decimal(d) for d in args.assumed_depth]
    if args.ledger and len(depths) > 1:
        raise ValueError("a single --ledger run takes one --assumed-depth (the grid takes several)")
    config = PaperConfig(
        initial_cash=Decimal(args.initial_cash),
        seed=args.seed,
        strategy=StrategyConfig(min_edge=Decimal(args.min_edge)),
        fee_schedule=fees[0],
        assumed_depth=depths[0] if args.ledger and depths else None,
    )
    grid = None
    if not args.ledger:
        grid = {
            "fee_schedules": tuple(PROVISIONAL_SCHEDULES),
            "extra_slippage_ticks": (0, 1),
            "latency": (config.latency, LatencyModel(submit_ms=(1250, 1250))),
            "maker_fill_model": ("none",),
            "assumed_depth": (None, *depths),
        }
    split = SplitDefinition(args.development_through, args.validation_through, args.embargo_days)
    # Only a holdout run writes (its holdout-ledger entry); other segments read the
    # registry read-only, so several backtests can run at once.
    registry = Registry(args.registry, readonly=args.segment != "holdout")
    db = open_archive(args.history_db)
    try:
        result = backtest.run(
            registry,
            db,
            args.dataset,
            args.model,
            split,
            config,
            segment=args.segment,
            grid=grid,
            ledger_path=args.ledger,
        )
    finally:
        db.close()
        registry.close()
    return {"status": "complete"} | result


def _pods(args):
    """Research pods: approved operations only, no paid calls, no live state."""
    from .datasets.splits import SplitDefinition
    from .pods import autopilot, evaluator, planner
    from .pods.candidates import candidate_record
    from .pods.contracts import Budgets, PodSpec
    from .pods.coordinator import Coordinator
    from .pods.providers import UnwiredVendorProvider
    from .pods.snapshot import feature_set_version, load
    from .pods.store import open_store, status
    from .research.registry import Registry

    store = open_store(args.root)
    registry = Registry(args.registry) if getattr(args, "registry", None) else None
    try:
        command = args.pods_command
        if command == "init":
            return {"status": "complete", "root": str(store.root)}
        if command == "register":
            budgets = Budgets(
                max_concurrent=args.max_concurrent,
                wall_seconds_per_task=args.wall_seconds,
                memory_mb=args.memory_mb,
                cpu_seconds_per_day=args.cpu_seconds_per_day,
            )
            created = store.register_pod(PodSpec(args.pod, args.role, budgets))
            return {"status": "complete", "pod": args.pod, "created": created}
        if command in ("snapshot", "execution-snapshot"):
            split = SplitDefinition(
                args.development_through, args.validation_through, args.embargo_days
            )
            if command == "snapshot":
                sha = evaluator.snapshot_dataset(store, registry, args.dataset, split)
                snapshot = load(store.artifacts.get_bytes(sha))
                return {
                    "status": "complete",
                    "snapshot_sha256": sha,
                    "development_rows": len(snapshot.development),
                    "validation_rows": len(snapshot.validation),
                    "feature_set": snapshot.feature_set,
                    "feature_set_version": feature_set_version(snapshot.manifest),
                }
            from .trading.marketdata import open_archive

            db = open_archive(args.history_db)
            try:
                sha = evaluator.execution_snapshot(store, registry, db, args.dataset, split)
            finally:
                db.close()
            return {"status": "complete", "execution_inputs_sha256": sha}
        if command == "submit":
            params = json.loads(args.params)
            task_id = autopilot.submit_operation(
                store, args.pod, args.operation, params, max_attempts=args.max_attempts
            )
            return {"status": "complete", "task_id": task_id}
        if command == "autopilot":
            if args.accept_best and registry is None:
                raise ValueError("--accept-best requires --registry")
            if args.planner == "heuristic":
                chosen = planner.HeuristicPlanner(seed=args.seed)
            else:
                chosen = planner.ProviderPlanner(UnwiredVendorProvider(args.vendor), seed=args.seed)
            criteria = evaluator.Criteria(
                baseline=args.baseline,
                metric=args.metric,
                min_n=args.min_n,
                allow_weaker=args.allow_weaker_criteria,
            )
            with Coordinator(store) as coordinator:
                summary = autopilot.run(
                    store,
                    registry,
                    pod_id=args.pod,
                    snapshot_sha=args.snapshot,
                    planner=chosen,
                    coordinator=coordinator,
                    rounds=args.rounds,
                    max_tasks=args.max_tasks,
                    max_tasks_per_round=args.max_tasks_per_round,
                    deadline_seconds=args.deadline_seconds,
                    accept_best=args.accept_best,
                    criteria=criteria,
                )
            return {"status": "complete"} | summary
        if command == "run":
            with Coordinator(store) as coordinator:
                report = coordinator.run(args.max_steps, args.deadline_seconds)
            return {"status": "complete", "run": report, "pods": status(store)}
        if command == "status":
            return {"status": "complete", "pods": status(store)}
        if command == "accept":
            candidate = evaluator.accept_model(store, registry, args.task)
            return {"status": "complete", "candidate": candidate_record(store, candidate)}
        if command == "evaluate":
            criteria = evaluator.Criteria(
                baseline=args.baseline,
                metric=args.metric,
                min_n=args.min_n,
                allow_weaker=args.allow_weaker_criteria,
            )
            return {"status": "complete"} | evaluator.evaluate(
                store, registry, args.candidate, criteria
            )
        if command == "candidate":
            return {"status": "complete", "candidate": candidate_record(store, args.candidate)}
        if not args.acknowledge_final:
            raise ValueError("holdout requires --acknowledge-final (one evaluation per candidate)")
        return {"status": "complete"} | evaluator.evaluate_holdout(store, registry, args.candidate)
    finally:
        if registry is not None:
            registry.close()
        store.close()


def _ledger(args):
    """Operator paths of the trading ledger. Nothing here submits an order or reads a
    credential; a live ledger starts with its kill switch engaged."""
    import tomllib
    from datetime import datetime, timezone

    from .trading.ledger import Ledger
    from .trading.portfolio import CashEvent
    from .trading.risk import limits_from_dict

    now = datetime.now(timezone.utc)
    create = args.ledger_command == "init"
    if not create and not Path(args.ledger).is_file():
        raise ValueError(f"ledger not found: {args.ledger}")
    readonly = args.ledger_command in ("show", "verify")
    ledger = Ledger(args.ledger, args.environment, create=create, readonly=readonly)
    try:
        if args.ledger_command == "init":
            if args.deposit is not None:
                if args.environment != "paper":
                    raise ValueError("deposits exist only in paper ledgers")
                # One initial deposit per ledger: re-running init never adds cash again.
                if not any(e.event_id == "init-deposit" for e in ledger.cash_events()):
                    ledger.append_cash_event(
                        CashEvent("init-deposit", "deposit", args.deposit, now, "init")
                    )
        if args.ledger_command == "kill-switch":
            ledger.set_kill_switch(bool(args.engage), args.reason, args.actor, now)
        if args.ledger_command == "limits":
            with open(args.file, "rb") as handle:
                data = tomllib.load(handle)
            if set(data) != {"limits"}:
                raise ValueError("limits file must contain exactly a [limits] table")
            limits = limits_from_dict(data["limits"])
            version = ledger.record_limits(limits, args.actor, now)
            return {
                "status": "complete",
                "limits_hash": version,
                "unset": list(limits.missing()),
                "live_ready": not limits.missing(),
            }
        if args.ledger_command == "verify":
            failures = ledger.verify()
            return {"status": "complete" if not failures else "failed", "failures": failures}
        projection = ledger.projection()
        portfolio = projection.portfolio
        kill = ledger.kill_switch()
        limits, limits_hash = ledger.current_limits()
        return {
            "status": "complete",
            "environment": ledger.environment,
            "cash": format(portfolio.cash, "f"),
            "reserved_cash": format(portfolio.reserved_cash, "f"),
            "realized": format(portfolio.realized, "f"),
            "holdings": {
                t: {"yes": format(h.yes_qty, "f"), "no": format(h.no_qty, "f")}
                for t, h in sorted(portfolio.holdings.items())
            },
            "active_orders": [s.order.client_order_id for s in projection.active_orders],
            "unknown_state": list(projection.unknown_flags()),
            "kill_switch": {
                "engaged": kill.engaged,
                "reason": kill.reason,
                "default": kill.default,
            },
            "limits_hash": limits_hash,
            "limits_unset": None if limits is None else list(limits.missing()),
        }
    finally:
        ledger.close()


def _live(args):
    """Readiness only: every activation gate except loading the credential, with no network
    request. Activation itself is a separate, reviewed user decision (runbook)."""
    from datetime import datetime, timezone

    if args.live_command == "dry-run":
        return _live_dry_run(args)

    from .trading.kalshi.activation import check_gates
    from .trading.ledger import Ledger

    if not Path(args.ledger).is_file():
        raise ValueError(f"ledger not found: {args.ledger}")
    ledger = Ledger(args.ledger, "live", create=False, readonly=True)
    try:
        results, activation = check_gates(
            args.activation_config,
            acknowledge_hash=args.acknowledge,
            ledger=ledger,
            now=datetime.now(timezone.utc),
        )
    finally:
        ledger.close()
    gates = [{"gate": r.gate, "passed": r.passed, "detail": r.detail} for r in results]
    ready = all(g["passed"] for g in gates) and activation is not None
    return {
        "status": "complete" if ready else "failed",
        "ready_except_credential": ready,
        "credential_checked": False,
        "gates": gates,
        "note": "readiness never loads the key, contacts the exchange or places an order",
    }


def _live_dry_run(args):
    """Dry run of the live adapter from forward-shadow decisions: a paper ledger, a
    `DryRunGateway`, no credential and no network transport."""
    from decimal import Decimal

    from .trading import shadow_dryrun
    from .trading.marketdata import open_archive
    from .trading.simulator import StrategyConfig

    signer = None
    if args.sign_with_throwaway_key:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        from .trading.kalshi.auth import Signer

        throwaway = ed25519.Ed25519PrivateKey.generate()  # in memory only, discarded
        signer = Signer("00000000-0000-4000-8000-000000000000", throwaway)
    db = open_archive(args.archive)
    try:
        result = shadow_dryrun.run(
            db,
            forecaster_id=args.forecaster,
            ledger_path=args.ledger,
            kalshi_environment=args.kalshi_environment,
            cash=args.cash,
            strategy=StrategyConfig(min_edge=Decimal(args.min_edge)),
            signer=signer,
            max_order_notional=args.max_order_notional,
            max_daily_notional=args.max_daily_notional,
        )
    finally:
        db.close()
    return {"status": "complete" if result["decisions"] else "inconclusive"} | result


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
