"""Historical paper backtest: model forecasts at each row's cutoff, executed against
archived one-minute candles and settled by the archived market result (roadmap D/H).

Evidence boundaries, stated in every report:
- forecasts use only the row's features, computed from candles that end at the cutoff C;
- execution uses candle closing quotes on both sides of C (`hist-candles` merged with the
  execution-only `hist-candles-post` stage). A candle is a one-minute summary of the best
  bid/ask, not an order book, and carries no sizes: under the default configuration it
  supplies no depth and nothing fills. Fills exist only under the labelled
  `assumed_depth` scenario;
- settlement uses the archived result and settlement time of the selected market.

The execution inputs are plain JSON (`vi-execution-inputs-v1`) so that the coordinator
can build them from the history archive and a research pod can run the simulation from a
verified copy without archive access. A profitable backtest is not evidence of edge and
never evidence of live profitability.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import numpy as np

from ..datasets import splits
from ..forecast.references import digest as reference_digest
from ..history import interpret
from ..models.artifact import ModelArtifact, predict_proba
from ..provenance import source_hash
from ..trading.instruments import Market
from ..trading.marketdata import BookSnapshot
from ..trading.portfolio import Settlement
from ..trading.simulator import ForecastInput, PaperInputs, run_paper, sensitivity
from . import training

EXECUTION_SCHEMA = "vi-execution-inputs-v1"
WINDOW_BEFORE_SECONDS = 120
WINDOW_AFTER_SECONDS = 300
SEGMENTS = ("development", "validation", "holdout")
NOTICE = (
    "Historical paper backtest on archived candles. Candles have no sizes: fills exist only "
    "under the assumed_depth SCENARIO. Not evidence of edge or of live profitability."
)


def _parse_time(value):
    if value is None:
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _quote(value):
    if value is None:
        return None
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None  # a malformed archived price is no quote, never a fill
    return result if Decimal(0) < result < Decimal(1) else None


def check_archive(db, manifest):
    """Refuse a history archive other than the one the dataset was built from: the fetch
    at the dataset's recorded ceiling must have the recorded digest (later fetches, such
    as post-cutoff execution windows, may follow it)."""
    source = manifest.get("source") or {}
    archive = source.get("archive") or {}
    if source.get("kind") != "history_archive" or "fetch_ceiling" not in archive:
        raise ValueError("backtests need a dataset built from a history archive")
    row = db.execute(
        "SELECT id, seq, body_sha256, retrieved_at FROM fetches WHERE seq=?",
        (archive["fetch_ceiling"],),
    ).fetchone()
    if row is None or reference_digest(list(row)) != archive.get("latest_fetch_digest"):
        raise ValueError("history archive differs from the archive the dataset was built from")


def execution_inputs(
    db,
    rows,
    *,
    run_id=None,
    window_before=WINDOW_BEFORE_SECONDS,
    window_after=WINDOW_AFTER_SECONDS,
):
    """Plain-JSON execution inputs for `rows` (their selected markets only).

    Snapshots are candle closing quotes within [C - window_before, C + window_after];
    rows whose market lacks a settlement time are listed under `skipped`.
    """
    records = interpret.markets(db, run_id)
    markets, snapshots, settlements, skipped = [], [], [], {}
    for row in sorted(rows, key=lambda r: (r["cutoff"], r["ticker"])):
        record = records.get(row["ticker"])
        settled_at = None if record is None else _parse_time(record.get("settlement_ts"))
        if record is None or record.get("result") not in ("yes", "no") or settled_at is None:
            skipped[row["ticker"]] = "no_settlement_evidence"
            continue
        cutoff = datetime.fromisoformat(row["cutoff"])
        low = cutoff - timedelta(seconds=window_before)
        high = cutoff + timedelta(seconds=window_after)
        close_time = _parse_time(record.get("close_time"))
        markets.append(
            {
                "ticker": row["ticker"],
                "event_ticker": row["event_ticker"],
                "close_time": None if close_time is None else close_time.isoformat(),
                "game_date": row["official_date"],
                "teams": [str(row["selected_team_id"])],
            }
        )
        for candle in interpret.execution_candles(db, row["ticker"], run_id):
            if not low <= candle["end"] <= high:
                continue
            bid, ask = _quote(candle["yes_bid_close"]), _quote(candle["yes_ask_close"])
            if bid is None and ask is None:
                continue
            if bid is not None and ask is not None and bid >= ask:
                continue
            snapshots.append(
                {
                    "ticker": row["ticker"],
                    "as_of": candle["end"].isoformat(),
                    "yes_bid": None if bid is None else str(bid),
                    "yes_ask": None if ask is None else str(ask),
                    "ref": f"candle:{candle['fetch_id']}:{int(candle['end'].timestamp())}",
                }
            )
        settlements.append(
            {
                "ticker": row["ticker"],
                "result": record["result"],
                "effective_at": settled_at.isoformat(),
                "ref": f"market:{record['fetch_id']}",
            }
        )
    return {
        "schema": EXECUTION_SCHEMA,
        "source": {"interpretation": interpret.INTERPRETATION, "run_id": run_id},
        "window_seconds": {"before": window_before, "after": window_after},
        "markets": markets,
        "snapshots": snapshots,
        "settlements": settlements,
        "skipped": dict(sorted(skipped.items())),
        "notice": "candle quotes carry no sizes; execution-only data, never features",
    }


def load_execution(data):
    payload = json.loads(data) if isinstance(data, bytes | str) else data
    if payload.get("schema") != EXECUTION_SCHEMA:
        raise ValueError("not a vi-execution-inputs-v1 payload")
    return payload


def forecasts(artifact_json, manifest, rows, model_id):
    """One forecast per row at its cutoff, from the row's features only."""
    artifact = ModelArtifact.from_dict(json.loads(artifact_json))
    names = [f["name"] for f in manifest["feature_set"]["features"]]
    if artifact.training.get("feature_set") != manifest["feature_set"]["name"]:
        raise ValueError("model and dataset use different feature sets")
    if not rows:
        return []
    X, _, _, _ = training.matrices(rows, names)
    p = predict_proba(artifact, X)
    out = []
    for row, value in zip(rows, p, strict=True):
        if not np.isfinite(value):
            continue
        out.append(
            ForecastInput(
                forecast_id=f"bt:{model_id[:16]}:{row['ticker']}",
                ticker=row["ticker"],
                probability=Decimal(str(round(float(value), 6))),
                computed_at=datetime.fromisoformat(row["cutoff"]),
                model_id=model_id,
                artifact_hash=model_id,
            )
        )
    return out


def paper_inputs(execution, forecast_inputs, label):
    """Trading-layer inputs: forecasts for markets without execution data are dropped."""
    execution = load_execution(execution)
    markets = tuple(
        Market(
            ticker=m["ticker"],
            event_ticker=m["event_ticker"],
            close_time=_parse_time(m["close_time"]),
            game_date=m["game_date"],
            teams=tuple(m["teams"]),
        )
        for m in execution["markets"]
    )
    known = {m.ticker for m in markets}
    snapshots = tuple(
        BookSnapshot(
            s["ticker"],
            datetime.fromisoformat(s["as_of"]),
            () if s["yes_bid"] is None else ((Decimal(s["yes_bid"]), Decimal(0)),),
            () if s["yes_ask"] is None else ((Decimal(1) - Decimal(s["yes_ask"]), Decimal(0)),),
            "quote",
            s["ref"],
            False,
        )
        for s in execution["snapshots"]
    )
    settlements = tuple(
        Settlement(
            settlement_id=f"settle:{s['ticker']}",
            ticker=s["ticker"],
            result=s["result"],
            settlement_value=Decimal(1) if s["result"] == "yes" else Decimal(0),
            source_ref=s["ref"],
            effective_at=datetime.fromisoformat(s["effective_at"]),
        )
        for s in execution["settlements"]
    )
    kept = tuple(f for f in forecast_inputs if f.ticker in known)
    return PaperInputs(label, markets, snapshots, kept, settlements)


def summary(
    report_dict,
    *,
    segment,
    model_id,
    dataset_id,
    rows,
    n_forecasts,
    execution,
    selection_biased=None,
):
    """The backtest envelope around a simulator report (or sensitivity table).

    `selection_biased` defaults to "not the holdout"; a reused holdout is biased too."""
    days = sorted(r["official_date"] for r in rows)
    quoted = {s["ticker"] for s in execution["snapshots"]}
    return {
        "kind": "historical-paper-backtest",
        "simulated": True,
        "notice": NOTICE,
        "segment": segment,
        "selection_biased": segment != "holdout" if selection_biased is None else selection_biased,
        "model_id": model_id,
        "dataset_id": dataset_id,
        "rows": len(rows),
        "period": {"start": days[0], "end": days[-1]} if days else None,
        "forecasts": n_forecasts,
        "markets": len(execution["markets"]),
        "markets_with_execution_data": len(quoted),
        "snapshots": len(execution["snapshots"]),
        "skipped": execution["skipped"],
        "execution_window_seconds": execution["window_seconds"],
        "code_hash": source_hash(),
        "report": report_dict,
    }


def segment_rows(registry, dataset_id, split, segment):
    rows = registry.dataset_rows(dataset_id)
    if segment == "holdout":
        return splits.holdout_rows(rows, split)
    development, validation = splits.development_and_validation(rows, split)
    return development if segment == "development" else validation


def _check_model(registry, dataset_id, model_id, split):
    """The model's artifact JSON; refused when trained on another dataset or split."""
    artifact_json = registry.model_artifact(model_id)
    artifact = ModelArtifact.from_dict(json.loads(artifact_json))
    if artifact.training.get("dataset_id") != dataset_id:
        raise ValueError("model was trained on another dataset")
    if artifact.training.get("split", {}).get("key") != split.key:
        raise ValueError("backtest split differs from the model's training split")
    return artifact_json


def _guard_segment(registry, dataset_id, model_id, split, segment):
    """Holdout: the pinned ledger key, refused when this model already used it. Other
    segments: the split must end before the dataset's pinned holdout."""
    if segment not in SEGMENTS:
        raise ValueError(f"segment must be one of {SEGMENTS}")
    if segment != "holdout":
        readonly = getattr(registry, "readonly", False)
        splits.guard(registry, dataset_id, split, pin_if_missing=not readonly)
        return None
    key = splits.holdout_key(registry, dataset_id, split)
    if registry.holdout_evaluation(model_id, dataset_id, key) is not None:
        raise ValueError("this model was already evaluated on this holdout")
    return key


def run(
    registry,
    history_db,
    dataset_id,
    model_id,
    split,
    config,
    *,
    segment="validation",
    grid=None,
    ledger_path=None,
    directory=None,
    run_id=None,
    min_holdout_rows=training.MIN_EVALUATION_ROWS,
):
    """Backtest a registered model on one segment of a registry dataset.

    `holdout` is allowed only through this function's ledger entry, which is recorded
    before any holdout result is computed or written (first use consumes the pinned
    holdout, later uses by other models are reuses). With `grid`, a sensitivity table
    over disposable ledgers; otherwise one run into `ledger_path`.
    """
    _check_model(registry, dataset_id, model_id, split)
    _guard_segment(registry, dataset_id, model_id, split, segment)
    check_archive(history_db, registry.dataset(dataset_id))
    rows = segment_rows(registry, dataset_id, split, segment)
    execution = execution_inputs(history_db, rows, run_id=run_id)
    return simulate(
        registry,
        execution,
        dataset_id,
        model_id,
        split,
        config,
        segment=segment,
        grid=grid,
        ledger_path=ledger_path,
        directory=directory,
        min_holdout_rows=min_holdout_rows,
    )


def simulate(
    registry,
    execution,
    dataset_id,
    model_id,
    split,
    config,
    *,
    segment="validation",
    grid=None,
    ledger_path=None,
    directory=None,
    min_holdout_rows=training.MIN_EVALUATION_ROWS,
):
    """`run` on execution inputs already built (by `execution_inputs`, or a labelled
    synthetic payload); the same model, split, segment and holdout-ledger rules apply."""
    if (grid is None) == (ledger_path is None):
        raise ValueError("give exactly one of grid or ledger_path")
    execution = load_execution(execution)
    manifest = registry.dataset(dataset_id)
    artifact_json = _check_model(registry, dataset_id, model_id, split)
    key = _guard_segment(registry, dataset_id, model_id, split, segment)
    rows = segment_rows(registry, dataset_id, split, segment)
    if segment == "holdout" and len(rows) < min_holdout_rows:
        raise ValueError(f"insufficient holdout rows: {len(rows)}")
    forecast_inputs = forecasts(artifact_json, manifest, rows, model_id)
    label = f"{execution['source']['interpretation']}:{segment}:{dataset_id[:16]}"
    inputs = paper_inputs(execution, forecast_inputs, label)
    if not inputs.forecasts:
        raise ValueError("no forecast has execution data")
    entry = None
    if segment == "holdout":
        # Recorded before anything is simulated or written: a refused or crashed run
        # can never leave holdout results without a ledger entry.
        brief = {
            "kind": "paper_backtest",
            "summary_rows": len(rows),
            "forecasts": len(inputs.forecasts),
        }
        entry = registry.evaluate_holdout(model_id, dataset_id, key, brief)
    if grid is None:
        report = run_paper(config, inputs, ledger_path).to_dict()
    else:
        report = sensitivity(config, inputs, grid, directory=directory).to_dict()
    out = summary(
        report,
        segment=segment,
        model_id=model_id,
        dataset_id=dataset_id,
        rows=rows,
        n_forecasts=len(inputs.forecasts),
        execution=execution,
        selection_biased=segment != "holdout" or entry["evaluations"] > 1,
    )
    out["forecasts_dropped_non_finite"] = len(rows) - len(forecast_inputs)
    if entry is not None:
        out["holdout_ledger"] = entry
    return out
