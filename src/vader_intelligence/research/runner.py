"""Conservative offline research runner (release milestone 3).

A research run turns one archive, one research protocol and one evaluation scope into
immutable, reproducible results using only the existing interfaces: `forecast
reconstruct` (cutoff-reconstruction view) and `evaluate`. It adds no provider request,
no model, no tuning and no new table: the run's identity, inputs and outputs are a
`runs` row of kind `research-run` whose provenance records the code hash, the
protocol hash, the archive identity and the scope, and whose summary names every
decision cohort and evaluation it produced.

Order and separation:

1. Reconstruction (cutoff-reconstruction view only) walks bindings in cutoff order,
   chunked to the 31-day command bound; the forward view evaluates the forward dataset
   as published.
2. Each split segment (`development` before `holdout`, or `whole`) is evaluated once
   per collector configuration cohort pinned by the segment's decisions; a cohort of
   the run with no decision in a segment is listed under `cohorts_without_decisions`.
   Cohorts and record modes are never pooled; a segment's results are keyed by cohort
   hash.
3. Everything is idempotent by construction: decisions and evaluations have content
   keys, so a repeated or resumed run reports `existing` results and writes nothing
   new.

Held-out observations: the protocol is fixed (hash recorded) before any evaluation of
the run, the development segment is evaluated before the holdout segment, the two
ranges may not overlap, and the summary lists every earlier evaluation in this dataset
whose period overlaps the holdout range, in any view, together with the protocol hash
it used, so a reviewer can see whether a protocol was changed after its holdout was
observed. v1 has no
tunable parameter, so this framework records provenance; it does not claim to prevent
every form of researcher leakage.
"""

import time
import uuid
from datetime import date, timedelta

from ..evaluation.service import evaluate
from ..forecast.reconstruct import MAX_DAYS, reconstruct
from ..forecast.references import digest
from ..provenance import RunProvenanceV1, source_hash
from ..storage import timestamp, utc_now

RUN_KIND = "research-run"
VIEWS = ("forward-shadow", "cutoff-reconstruction")


def archive_identity(db):
    """What the archive was when the run started: enough to detect a different input."""
    return {
        "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
        "fetch_ceiling": db.execute("SELECT COALESCE(MAX(seq),0) FROM fetches").fetchone()[0],
        "fetches": db.execute("SELECT COUNT(*) FROM fetches").fetchone()[0],
        "discovery_passes": db.execute("SELECT COUNT(*) FROM discovery_passes").fetchone()[0],
        "forecast_bindings": db.execute("SELECT COUNT(*) FROM forecast_bindings").fetchone()[0],
        "forecast_decisions": db.execute("SELECT COUNT(*) FROM forecast_decisions").fetchone()[0],
        "settlement_versions": db.execute("SELECT COUNT(*) FROM settlement_versions").fetchone()[0],
        "latest_fetch_digest": _latest_fetch_digest(db),
    }


def _latest_fetch_digest(db):
    row = db.execute(
        "SELECT id, seq, body_sha256, retrieved_at FROM fetches ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    return None if row is None else digest(list(row))


def _chunks(start, through):
    current = start
    while current <= through:
        end = min(through, current + timedelta(days=MAX_DAYS - 1))
        yield current, end
        current = end + timedelta(days=1)


def _cohorts(db, protocol, dataset, modes, start, through):
    """Collector cohorts pinned by the dataset's decisions scheduled in [start, through],
    the scope in which `evaluate` looks for them; [None] when none is pinned there."""
    rows = db.execute(
        "SELECT DISTINCT json_extract(payload_json,'$.manifest.discovery.collector_config_hash')"
        " FROM forecast_decisions WHERE protocol=? AND json_extract(payload_json,'$.dataset')=?"
        " AND json_extract(payload_json,'$.config_hash')=?"
        " AND mode IN (" + ",".join("?" for _ in modes) + ")"
        " AND substr(json_extract(payload_json,'$.scheduled_start'),1,10) BETWEEN ? AND ?"
        " ORDER BY 1",
        (
            protocol.protocol_id,
            dataset,
            protocol.config_hash,
            *modes,
            start.isoformat(),
            through.isoformat(),
        ),
    ).fetchall()
    hashes = sorted(r[0] for r in rows if r[0] is not None)
    return hashes or [None]


def _holdout_history(db, dataset, start, through):
    """Every earlier evaluation of the dataset, in any view, whose period overlaps the
    holdout range (an open start or end overlaps everything on that side): each one may
    have observed a holdout outcome."""
    rows = db.execute(
        "SELECT id, json_extract(payload_json,'$.config_hash'),"
        " json_extract(payload_json,'$.code_hash'), json_extract(payload_json,'$.created_at'),"
        " view, json_extract(payload_json,'$.report.period.start_date'),"
        " json_extract(payload_json,'$.report.period.through_date')"
        " FROM evaluation_runs WHERE json_extract(payload_json,'$.dataset')=?"
        " AND COALESCE(json_extract(payload_json,'$.report.period.start_date')<=?, 1)"
        " AND COALESCE(json_extract(payload_json,'$.report.period.through_date')>=?, 1)"
        " ORDER BY id",
        (dataset, through.isoformat(), start.isoformat()),
    ).fetchall()
    return [
        {
            "evaluation_id": r[0],
            "research_config_hash": r[1],
            "code_hash": r[2],
            "created_at": r[3],
            "view": r[4],
            "start_date": r[5],
            "through_date": r[6],
        }
        for r in rows
    ]


def run(
    store,
    protocol,
    *,
    view,
    outcomes_as_of,
    start_date,
    through_date,
    dataset=None,
    source_ceiling=None,
    development_through=None,
    holdout_from=None,
    clock=None,
    monotonic=None,
    budget=120.0,
):
    """Execute one research run. Returns the run summary (also stored in `runs`)."""
    injected = clock is not None or monotonic is not None
    clock = clock or utc_now
    monotonic = monotonic or time.monotonic
    if view not in VIEWS:
        raise ValueError("view must be forward-shadow or cutoff-reconstruction")
    if not isinstance(start_date, date) or not isinstance(through_date, date):
        raise ValueError("start and through must be dates")
    if through_date < start_date:
        raise ValueError("through must not precede start")
    if (development_through is None) != (holdout_from is None):
        raise ValueError("a split needs both --development-through and --holdout-from")
    if development_through is not None:
        if not (start_date <= development_through < holdout_from <= through_date):
            raise ValueError(
                "split boundaries must satisfy start <= development_through < holdout_from <= through"
            )
    if not isinstance(budget, (int, float)) or not 0 < budget <= 3600:
        raise ValueError("budget must be in (0, 3600] seconds")
    if view == "cutoff-reconstruction":
        if dataset is None or dataset == protocol.dataset:
            raise ValueError(
                "a reconstruction study needs its own --dataset namespace, distinct from the"
                f" forward dataset {protocol.dataset!r}"
            )
    else:
        dataset = protocol.dataset if dataset is None else dataset
    db = store.db
    identity = archive_identity(db)
    if source_ceiling is None:
        source_ceiling = identity["fetch_ceiling"]
    segments = (
        [("whole", start_date, through_date)]
        if development_through is None
        else [
            ("development", start_date, development_through),
            ("holdout", holdout_from, through_date),
        ]
    )
    run_id = str(uuid.uuid4())
    code_hash = source_hash()
    configuration = {
        "protocol": protocol.config_hash,
        "forward_dataset": protocol.dataset,
        "dataset": dataset,
        "view": view,
        "outcomes_as_of": outcomes_as_of.isoformat(),
        "source_ceiling": source_ceiling,
        "start_date": start_date.isoformat(),
        "through_date": through_date.isoformat(),
        "development_through": None
        if development_through is None
        else development_through.isoformat(),
        "holdout_from": None if holdout_from is None else holdout_from.isoformat(),
        "archive": identity,
    }
    store.start_run(
        RunProvenanceV1(
            run_id,
            RUN_KIND,
            code_hash,
            configuration,
            tool_configuration={"source_lineage": "synthetic" if injected else "live"},
        ),
        timestamp(clock()),
    )
    started = monotonic()
    deadline = started + budget
    summary = {
        "run_id": run_id,
        "status": "complete",
        "view": view,
        "dataset": dataset,
        "protocol": protocol.protocol_id,
        "research_config_hash": protocol.config_hash,
        "code_hash": code_hash,
        "manifest": configuration,
        "segments": {},
        "reconstruct": [],
        "holdout_history": [],
        "errors": [],
    }
    try:
        if view == "cutoff-reconstruction":
            for chunk_start, chunk_through in _chunks(start_date, through_date):
                if monotonic() >= deadline:
                    raise TimeoutError("research budget exhausted during reconstruction")
                result = reconstruct(
                    store,
                    protocol,
                    dataset=dataset,
                    start_date=chunk_start,
                    through_date=chunk_through,
                    source_ceiling=source_ceiling,
                    clock=clock if injected else None,
                    monotonic=monotonic if injected else None,
                    budget=max(1.0, min(600.0, deadline - monotonic())),
                )
                summary["reconstruct"].append(
                    {k: result[k] for k in result if k not in ("protocol", "config_hash")}
                )
                if result["status"] == "partial":
                    summary["status"] = "partial"
        modes = (
            "synthetic",
            "forward_shadow" if view == "forward-shadow" else "historical_reconstruction",
        )
        run_cohorts = _cohorts(db, protocol, dataset, modes, start_date, through_date)
        for segment, seg_start, seg_through in segments:
            if segment == "holdout":
                summary["holdout_history"] = _holdout_history(db, dataset, seg_start, seg_through)
            cohorts = _cohorts(db, protocol, dataset, modes, seg_start, seg_through)
            entry = {
                "start_date": seg_start.isoformat(),
                "through_date": seg_through.isoformat(),
                "cohorts": {},
                # A cohort of the run that decided nothing in this segment is listed, not
                # evaluated there (evaluate refuses a cohort its scope does not pin).
                "cohorts_without_decisions": [
                    c for c in run_cohorts if c is not None and c not in cohorts
                ],
            }
            summary["segments"][segment] = entry
            for cohort in cohorts:
                if monotonic() >= deadline:
                    raise TimeoutError("research budget exhausted during evaluation")
                result = evaluate(
                    store,
                    protocol,
                    view=view,
                    outcomes_as_of=outcomes_as_of,
                    dataset=dataset,
                    source_ceiling=source_ceiling,
                    start_date=seg_start,
                    through_date=seg_through,
                    collector_config_hash=cohort,
                    clock=clock if injected else None,
                    monotonic=monotonic if injected else None,
                    budget=max(1.0, min(600.0, deadline - monotonic())),
                )
                key = cohort or "unpinned"
                entry["cohorts"][key] = {
                    "collector_config_hash": cohort,
                    "evaluation_id": result["evaluation_id"],
                    "status": result["status"],
                    "existing": result["existing"],
                    "supersedes_id": result.get("supersedes_id"),
                    "mode": result["mode"],
                    "population": result.get("report", {}).get("population"),
                    "paired": result.get("report", {}).get("scores", {}).get("paired"),
                    "errors": result["errors"],
                }
                if result["status"] == "partial":
                    summary["status"] = "partial"
    except TimeoutError as exc:
        summary["status"] = "partial"
        summary["errors"].append(str(exc)[:300])
    except BaseException as exc:
        summary["status"] = "failed"
        summary["errors"].append(str(exc)[:300])
        store.finish_run(run_id, "failed", summary)
        raise
    if summary["errors"] and summary["status"] == "complete":
        summary["status"] = "partial"
    statuses = [
        cohort["status"]
        for segment in summary["segments"].values()
        for cohort in segment["cohorts"].values()
    ]
    if summary["status"] == "complete" and statuses and all(s == "inconclusive" for s in statuses):
        # No paired binary outcome anywhere: the run is inconclusive (§7 exit 3), not a
        # success claim about an empty or unsettled population.
        summary["status"] = "inconclusive"
    summary["runtime_seconds"] = monotonic() - started
    summary["archive_after"] = archive_identity(db)
    store.finish_run(run_id, summary["status"], summary)
    return summary


def inspect_run(db, run_id):
    row = db.execute(
        "SELECT kind, started_at, finished_at, status, provenance_json, summary_json FROM runs"
        " WHERE id=?",
        (run_id,),
    ).fetchone()
    if row is None or row[0] != RUN_KIND:
        raise ValueError("missing research run")
    import json

    return {
        "run_id": run_id,
        "started_at": row[1],
        "finished_at": row[2],
        "status": row[3],
        "provenance": json.loads(row[4]),
        "summary": None if row[5] is None else json.loads(row[5]),
    }


def list_runs(db, *, limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer in [1, 100]")
    import json

    rows = db.execute(
        "SELECT id, started_at, finished_at, status, summary_json FROM runs WHERE kind=?"
        " ORDER BY rowid DESC LIMIT ?",
        (RUN_KIND, limit),
    ).fetchall()
    out = []
    for run_id, started, finished, status, summary_json in rows:
        summary = json.loads(summary_json) if summary_json else {}
        out.append(
            {
                "run_id": run_id,
                "started_at": started,
                "finished_at": finished,
                "status": status,
                "view": summary.get("view"),
                "dataset": summary.get("dataset"),
                "segments": {
                    name: {k: v["evaluation_id"] for k, v in seg.get("cohorts", {}).items()}
                    for name, seg in summary.get("segments", {}).items()
                },
            }
        )
    return {"count": len(out), "runs": out}
