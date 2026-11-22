"""Representative runtime and memory probe for selection, replay-side integrity, the
open-time contract preflight and evaluation materialization on a synthetic archive.

Builds a disposable schema-4 archive with N games × passes, witnessed receipts,
bindings and decisions written through the real journal, then times:

- `schema.check_contracts` (runs on every Store open, before WAL);
- `evidence.discovery_as_of` + `selection.select_inputs` for one cutoff (what a tick does
  per due game);
- `journal.integrity` (what replay, inspect, backup and restore run);
- `evaluation.service.evaluate` over the whole cohort;
- `tick.inspect` keyset page.

Usage: .venv/bin/python tests/integration/perf_probe.py --games 50 --passes 20
Prints one JSON document with sizes, wall-clock seconds and peak traced memory.
"""

import argparse
import hashlib
import json
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from vader_intelligence.evaluation.service import evaluate  # noqa: E402
from vader_intelligence.forecast import evidence, journal, schema  # noqa: E402
from vader_intelligence.forecast.references import digest, reference  # noqa: E402
from vader_intelligence.forecast.research import Protocol  # noqa: E402
from vader_intelligence.forecast.selection import select_inputs  # noqa: E402
from vader_intelligence.forecast.tick import inspect  # noqa: E402
from vader_intelligence.provenance import RunProvenanceV1, json_text  # noqa: E402
from vader_intelligence.storage import Store  # noqa: E402

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
HASH = "a" * 64
NS = "local-witness-v1"


def iso(moment):
    return moment.isoformat()


def timed(label, fn, results):
    tracemalloc.start()
    started = time.perf_counter()
    value = fn()
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    results[label] = {"seconds": round(elapsed, 4), "peak_traced_mib": round(peak / 1048576, 2)}
    return value


def build(path, games, passes):
    store = Store(path, min_free_bytes=1)
    from vader_intelligence.settlement.schema import migrate as settlement_migrate

    settlement_migrate(store)
    schema.migrate(store.db)
    schema.migrate(store.db, record_contract=3)
    db = store.db
    db.execute("PRAGMA synchronous=OFF")  # build speed only; the probe measures reads
    collector_config = {"max_pages": 3}
    witness_tool = {
        "observer_namespace": NS,
        "session_id": "probe-session",
        "source_lineage": "synthetic",
    }
    db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            "witness-run",
            "forecast-witness",
            iso(T0),
            None,
            "running",
            RunProvenanceV1(
                "witness-run", "forecast-witness", HASH, {}, tool_configuration=witness_tool
            ).to_json(),
            None,
        ),
    )
    db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            "tick-run",
            "forecast-tick",
            iso(T0),
            None,
            "running",
            RunProvenanceV1(
                "tick-run",
                "forecast-tick",
                HASH,
                {},
                tool_configuration={"source_lineage": "synthetic"},
            ).to_json(),
            None,
        ),
    )
    seq = 0
    pass_ids = []
    protocol = Protocol(dataset="perf-probe")
    tool = {"collection_protocol": "two-phase-v1", "source_lineage": "synthetic"}
    for p in range(passes):
        run_id = f"collect-{p}"
        db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                "collect",
                iso(T0),
                None,
                "running",
                RunProvenanceV1(
                    run_id, "collect", HASH, collector_config, tool_configuration=tool
                ).to_json(),
                None,
            ),
        )
        completed = T0 + timedelta(seconds=120 * p)
        fetch_ids = []
        for stage in ("status", "series", "markets", "schedule", "event"):
            seq += 1
            fetch_id = f"{run_id}-{stage}"
            body = json_text({"probe": fetch_id}).encode()
            sha = hashlib.sha256(body).hexdigest()
            db.execute("INSERT OR IGNORE INTO blobs VALUES (?,?,?)", (sha, body, len(body)))
            db.execute(
                "INSERT INTO requests(id,run_id,stage,request_key,url,params_json,done) VALUES (?,?,?,?,?,?,1)",
                (fetch_id + "-req", run_id, stage, fetch_id, "https://example.invalid/x", "{}"),
            )
            db.execute(
                "INSERT INTO fetches(id,request_id,attempt,started_at,retrieved_at,body_sha256,state,status) VALUES (?,?,1,?,?,?,'ok',200)",
                (
                    fetch_id,
                    fetch_id + "-req",
                    iso(completed - timedelta(seconds=2)),
                    iso(completed - timedelta(seconds=1)),
                    sha,
                ),
            )
            fetch_ids.append(fetch_id)
        after = db.execute("SELECT COALESCE(MAX(id),0) FROM eligibility").fetchone()[0]
        for g in range(games):
            for side in ("A", "B"):
                data = {
                    "eligible": True,
                    "reason": "eligible",
                    "game_id": 700000 + g,
                    "cutoff": iso(T0 + timedelta(hours=5)),
                    "scheduled_start": iso(T0 + timedelta(hours=5)),
                    "team_id": 100 + (0 if side == "A" else 1),
                    "mapping_version": 1,
                }
                db.execute(
                    "INSERT INTO eligibility(run_id,ticker,checked_at,schedule_fetch_id,market_fetch_id,game_id,eligible,reason,cutoff,data_json) VALUES (?,?,?,?,?,?,1,'eligible',?,?)",
                    (
                        run_id,
                        f"KXMLBGAME-PROBE{g}-{side}",
                        iso(completed),
                        f"{run_id}-schedule",
                        f"{run_id}-event",
                        700000 + g,
                        data["cutoff"],
                        json_text(data),
                    ),
                )
        through = db.execute("SELECT COALESCE(MAX(id),0) FROM eligibility").fetchone()[0]
        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": iso(completed),
            "code_hash": HASH,
            "config_hash": digest(collector_config),
            "mode": "synthetic",
            "references": [reference(db, "fetches", {"id": f}) for f in fetch_ids],
            "session_id": f"session-{p}",
            "phase_sequence": 1,
            "completed_at": iso(completed),
            "complete": True,
            "reasons": [],
            "counts": {"games": games, "contracts": 2 * games},
            "eligibility_after_id": after,
            "eligibility_through_id": through,
            "manifest": {
                "manifest_version": 1,
                "phase_reached": "done",
                "candidates_known": True,
                "eligibility_complete": True,
                "pages_complete": True,
                "fetch_seq_through": seq,
                "official_game_ids": [700000 + g for g in range(games)],
                "candidates": [
                    {
                        "ticker": f"KXMLBGAME-PROBE{g}-{s}",
                        "event_ticker": f"KXMLBGAME-PROBE{g}",
                        "page_fetch_id": f"{run_id}-markets",
                    }
                    for g in range(games)
                    for s in ("A", "B")
                ],
                "documents": [
                    {
                        "url": "https://example.invalid/terms.pdf",
                        "fetch_id": f"{run_id}-series",
                        "raw_sha256": "b" * 64,
                    }
                ],
            },
        }
        pass_id = journal.append(db, "discovery_passes", payload)
        pass_ids.append(pass_id)
        observed = completed + timedelta(seconds=30)
        for subject_kind, subject_id in [("discovery_pass", pass_id)] + [
            ("fetch", f) for f in fetch_ids
        ]:
            ref = reference(
                db,
                "discovery_passes" if subject_kind == "discovery_pass" else "fetches",
                {"id": subject_id},
            )
            subject_digest = (
                db.execute(
                    "SELECT digest FROM discovery_passes WHERE id=?", (subject_id,)
                ).fetchone()[0]
                if subject_kind == "discovery_pass"
                else ref["digest"]
            )
            journal.append(
                db,
                "forecast_receipts",
                {
                    "schema_version": 1,
                    "contract_version": 3,
                    "run_id": "witness-run",
                    "created_at": iso(observed),
                    "code_hash": HASH,
                    "config_hash": digest({}),
                    "mode": "synthetic",
                    "references": [ref],
                    "subject_kind": subject_kind,
                    "fetch_id": subject_id if subject_kind == "fetch" else None,
                    "binding_id": None,
                    "publication_id": None,
                    "discovery_id": subject_id if subject_kind == "discovery_pass" else None,
                    "subject_digest": subject_digest,
                    "observed_at": iso(observed),
                    "session_id": "probe-session",
                    "observer_namespace": NS,
                    "monotonic_offset": "1.0",
                    "clock_status": "trusted",
                },
            )
    # One binding and one decision per game, pinned to the last pass.
    last = pass_ids[-1]
    last_row = db.execute(
        "SELECT digest, run_id FROM discovery_passes WHERE id=?", (last,)
    ).fetchone()
    for g in range(games):
        binding = {
            "schema_version": 1,
            "run_id": "tick-run",
            "created_at": iso(T0 + timedelta(hours=1)),
            "code_hash": HASH,
            "config_hash": protocol.config_hash,
            "mode": "synthetic",
            "references": [],
            "candidate_key": str(700000 + g),
            "revision": 1,
            "previous_id": None,
            "policy_version": protocol.policy_version,
            "game_id": 700000 + g,
            "event_ticker": f"KXMLBGAME-PROBE{g}",
            "ticker": f"KXMLBGAME-PROBE{g}-A",
            "yes_team_id": 100,
            "team_ids": [100, 101],
            "original_date": "2026-10-01",
            "original_start": iso(T0 + timedelta(hours=5)),
            "game_number": None,
            "scheduled_start": iso(T0 + timedelta(hours=5)),
            "cutoff": iso(T0 + timedelta(hours=4)),
            "status": "prepared",
            "reasons": [],
            "manifest": {
                "schedule_fetch_id": f"{last_row[1]}-schedule",
                "event_fetch_id": f"{last_row[1]}-event",
                "alternatives": [],
            },
            "terms_digest": "b" * 64,
        }
        binding_id = journal.append(db, "forecast_bindings", binding)
        journal.append(
            db,
            "forecast_decisions",
            {
                "schema_version": 1,
                "contract_version": 3,
                "run_id": "tick-run",
                "created_at": iso(T0 + timedelta(hours=4, seconds=10)),
                "code_hash": HASH,
                "config_hash": protocol.config_hash,
                "mode": "synthetic",
                "references": [],
                "dataset": protocol.dataset,
                "protocol": protocol.protocol_id,
                "game_id": 700000 + g,
                "unresolved_key": None,
                "ticker": binding["ticker"],
                "yes_team_id": 100,
                "horizon": 3600,
                "scheduled_start": binding["scheduled_start"],
                "cutoff": binding["cutoff"],
                "binding_id": binding_id,
                "discovery_id": last,
                "selection_version": protocol.selection_version,
                "baseline_versions": ["constant-v1", "midpoint-v1"],
                "cutoff_eligible": True,
                "reasons": [],
                "results": {
                    "constant-v1": {"probability": "0.5", "reasons": [], "inputs": {}},
                    "midpoint-v1": {"probability": None, "reasons": ["book_missing"], "inputs": {}},
                },
                "manifest": {
                    "discovery": {
                        "id": last,
                        "digest": last_row[0],
                        "collector_config_hash": digest(collector_config),
                    },
                    "selected_ticker": binding["ticker"],
                    "renewal": {"eligibility_id": None},
                },
                "source_ceiling": seq,
                "supersedes_id": None,
            },
        )
    db.execute("PRAGMA synchronous=FULL")
    return store, protocol, seq


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=50)
    parser.add_argument("--passes", type=int, default=20)
    options = parser.parse_args()
    results = {"games": options.games, "passes": options.passes}
    with tempfile.TemporaryDirectory(prefix="vader-perf-") as temporary:
        path = Path(temporary) / "perf.sqlite3"
        started = time.perf_counter()
        store, protocol, ceiling = build(path, options.games, options.passes)
        results["build_seconds"] = round(time.perf_counter() - started, 3)
        db = store.db
        results["rows"] = {
            t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in (
                "fetches",
                "discovery_passes",
                "forecast_receipts",
                "forecast_bindings",
                "forecast_decisions",
                "eligibility",
            )
        }
        results["archive_bytes"] = path.stat().st_size
        cutoff = T0 + timedelta(hours=4)
        timed("check_contracts", lambda: schema.check_contracts(db), results)
        timed(
            "discovery_as_of",
            lambda: evidence.discovery_as_of(db, cutoff, namespace=NS, mode="synthetic"),
            results,
        )
        timed(
            "select_inputs",
            lambda: select_inputs(
                db,
                ticker="KXMLBGAME-PROBE0-A",
                game_id=700000,
                cutoff=cutoff,
                namespace=NS,
                mode="synthetic",
            ),
            results,
        )
        timed("journal_integrity", lambda: journal.integrity(db), results)
        timed("inspect_page_20", lambda: inspect(store, limit=20, namespace=NS), results)
        timed(
            "evaluate_forward",
            lambda: evaluate(
                store,
                protocol,
                view="forward-shadow",
                outcomes_as_of=T0 + timedelta(days=1),
                clock=lambda: T0 + timedelta(days=1),
                monotonic=time.monotonic,
                budget=600,
            ),
            results,
        )
        timed(
            "store_open_readonly",
            lambda: Store(path, min_free_bytes=1, readonly=True).close(),
            results,
        )
        store.close()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
