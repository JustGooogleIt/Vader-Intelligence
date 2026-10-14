"""Archive-only historical reconstruction campaign (specification §2, §7).

`forecast reconstruct` recomputes the cutoff decision for every prepared, witnessed
binding of a cohort whose original date falls in an explicit range, under an explicit
dataset namespace and source ceiling fixed before selection. It reads the same
pre-cutoff evidence the forward tick would have read (receipts observed at or before
C), ignores actual invocation and publication timing, never writes a publication
(reconstruction is `not_applicable`), and never requests anything over HTTP.

Mode: a binding of forward lineage yields a `historical_reconstruction` decision that
cites the live binding and pass (plan §9.3 M9/M10); a synthetic binding yields a
`synthetic` decision, so synthetic provenance is retained in the reconstruction view.
Bounded to 31 days and 1000 games per command; exceeding the game cap is an explicit
error, never a truncation. Idempotent: the decision key excludes time, so a repeat adds
nothing and resumes where a budget-exhausted run stopped.
"""

import time
import uuid
from datetime import date, datetime, timedelta

from ..provenance import RunProvenanceV1, source_hash
from ..storage import timestamp, utc_now
from . import service
from .policy import utc
from .tick import _decision_exists

MAX_DAYS = 31
MAX_GAMES = 1000
MODE_OF = {"forward_shadow": "historical_reconstruction", "synthetic": "synthetic"}


def reconstruct(
    store,
    protocol,
    *,
    dataset,
    start_date,
    through_date,
    source_ceiling=None,
    clock=None,
    monotonic=None,
    budget=30.0,
):
    """Run one bounded reconstruction. Returns a summary dict (complete | inconclusive |
    partial); the CLI maps the status to an exit code."""
    injected = clock is not None or monotonic is not None
    clock = clock or utc_now
    monotonic = monotonic or time.monotonic
    if not isinstance(dataset, str) or not 1 <= len(dataset) <= 128:
        raise ValueError("reconstruction requires an explicit dataset namespace")
    if dataset == protocol.dataset:
        raise ValueError(
            "reconstruction dataset must differ from the forward dataset namespace "
            f"{protocol.dataset!r}"
        )
    if not isinstance(start_date, date) or not isinstance(through_date, date):
        raise ValueError("start and through must be dates")
    if through_date < start_date or (through_date - start_date).days + 1 > MAX_DAYS:
        raise ValueError(f"reconstruction range must be 1..{MAX_DAYS} days")
    if not isinstance(budget, (int, float)) or not 0 < budget <= 600:
        raise ValueError("budget must be in (0, 600] seconds")
    if store.db.in_transaction:
        raise ValueError("reconstruct requires an idle connection")
    if store.schema_version() < 4:
        raise ValueError(
            "forecast reconstruct requires record contract 2: "
            "vader forecast migrate --record-contract 2"
        )
    db = store.db
    archive_ceiling = db.execute("SELECT COALESCE(MAX(seq),0) FROM fetches").fetchone()[0]
    if source_ceiling is None:
        source_ceiling = archive_ceiling
    if type(source_ceiling) is not int or not 0 <= source_ceiling <= archive_ceiling:
        raise ValueError("source ceiling must be an integer within the archive")
    bindings = db.execute(
        "SELECT id, mode, json_extract(payload_json,'$.game_id'),"
        " json_extract(payload_json,'$.original_date'), json_extract(payload_json,'$.cutoff')"
        " FROM forecast_bindings WHERE policy_version=? AND config_hash=?"
        " AND mode IN ('forward_shadow','synthetic')"
        " AND json_extract(payload_json,'$.original_date') BETWEEN ? AND ?"
        " ORDER BY json_extract(payload_json,'$.cutoff'), id LIMIT ?",
        (
            protocol.policy_version,
            protocol.config_hash,
            start_date.isoformat(),
            through_date.isoformat(),
            MAX_GAMES + 1,
        ),
    ).fetchall()
    if len(bindings) > MAX_GAMES:
        raise ValueError(f"reconstruction range holds more than {MAX_GAMES} games; narrow it")
    # The ceiling freezes the archive view. It is recorded in every decision; it may not
    # exclude evidence retrieved before any cutoff in range, because the decision reads
    # the pre-cutoff archive and that view must equal the view at the ceiling.
    latest_cutoff = max((utc(datetime.fromisoformat(row[4])) for row in bindings), default=None)
    if (
        latest_cutoff is not None
        and db.execute(
            "SELECT 1 FROM fetches WHERE seq>? AND retrieved_at IS NOT NULL AND retrieved_at<=?"
            " LIMIT 1",
            (source_ceiling, latest_cutoff.isoformat()),
        ).fetchone()
    ):
        raise ValueError(
            "source ceiling excludes evidence retrieved before a cutoff in range; raise it"
        )
    run_id = str(uuid.uuid4())
    code_hash = source_hash()
    configuration = {
        "protocol": protocol.config_hash,
        "dataset": dataset,
        "start_date": start_date.isoformat(),
        "through_date": through_date.isoformat(),
        "source_ceiling": source_ceiling,
    }
    store.start_run(
        RunProvenanceV1(
            run_id,
            "forecast-reconstruct",
            code_hash,
            configuration,
            tool_configuration={"source_lineage": "synthetic" if injected else "live"},
        ),
        timestamp(clock()),
    )
    deadline = monotonic() + budget
    summary = {
        "run_id": run_id,
        "protocol": protocol.protocol_id,
        "config_hash": protocol.config_hash,
        "dataset": dataset,
        "start_date": start_date.isoformat(),
        "through_date": through_date.isoformat(),
        "source_ceiling": source_ceiling,
        "bindings_in_range": len(bindings),
        "decisions_eligible": 0,
        "decisions_abstained": 0,
        "decisions_existing": 0,
        "by_mode": {},
        "publication": "not_applicable",
        "errors": [],
    }
    status = "complete"
    try:
        for binding_id, binding_mode, game_id, _original, cutoff in bindings:
            if monotonic() >= deadline:
                status = "partial"
                break
            mode = MODE_OF[binding_mode]
            if _decision_exists(db, protocol, mode, game_id, dataset=dataset):
                summary["decisions_existing"] += 1
                continue
            try:
                result = service.decide_opportunity(
                    store,
                    binding_id=binding_id,
                    protocol=protocol,
                    run_id=run_id,
                    code_hash=code_hash,
                    mode=mode,
                    created_at=timestamp(clock()),
                    namespace=protocol.observer_namespace,
                    dataset=dataset,
                    source_ceiling=source_ceiling,
                )
            except (ValueError, KeyError, TypeError) as exc:
                summary["errors"].append(f"reconstruct {game_id}: {str(exc)[:200]}")
                continue
            summary["by_mode"][mode] = summary["by_mode"].get(mode, 0) + 1
            if result.cutoff_eligible:
                summary["decisions_eligible"] += 1
            else:
                summary["decisions_abstained"] += 1
    except BaseException as exc:
        summary["errors"].append(str(exc)[:200])
        store.finish_run(run_id, "interrupted", summary | {"status": "interrupted"})
        raise
    if summary["errors"] and status == "complete":
        status = "partial"
    if status == "complete" and not bindings:
        status = "inconclusive"
    summary["status"] = status
    store.finish_run(run_id, status, summary)
    return summary


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("dates must be YYYY-MM-DD") from exc


def date_range_days(start_date, through_date):
    return (through_date - start_date) // timedelta(days=1) + 1
