"""Entirely synthetic SQLite archives; no Store, fake locks, alarms or network."""

import hashlib
import sqlite3
from importlib.resources import files

from vader_intelligence.forecast import journal, schema
from vader_intelligence.forecast.references import digest, reference
from vader_intelligence.provenance import RunProvenanceV1, json_text

AT = "2026-09-30T19:00:00+00:00"
HASH = "a" * 64


def connect(path):
    db = sqlite3.connect(path, isolation_level=None, timeout=5)
    db.execute("PRAGMA foreign_keys=ON")
    return db


def legacy(path, version=2):
    db = connect(path)
    for i, name in ((1, "001_initial.sql"), (2, "002_settlement.sql")):
        if i <= version:
            # These exact legacy DDL scripts seed tests; they do NOT simulate flock.
            db.executescript(files("vader_intelligence").joinpath("migrations/" + name).read_text())
            db.execute("INSERT INTO schema_migrations VALUES (?,?)", (i, AT))
    db.execute(f"PRAGMA user_version={version}")
    provenance = RunProvenanceV1("synthetic-run", "fixture", HASH, {}).to_json()
    db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        ("synthetic-run", "fixture", AT, AT, "complete", provenance, "{}"),
    )
    for index, value in enumerate(("A", "B", "A"), 1):
        body = json_text({"synthetic": True, "value": value}).encode()
        sha = hashlib.sha256(body).hexdigest()
        db.execute("INSERT OR IGNORE INTO blobs VALUES (?,?,?)", (sha, body, len(body)))
        db.execute(
            "INSERT INTO requests(id,run_id,stage,request_key,url,params_json,done) VALUES (?,?,?,?,?,?,1)",
            (
                f"request-{index}",
                "synthetic-run",
                "document",
                str(index),
                "https://example.invalid/synthetic",
                "{}",
            ),
        )
        db.execute(
            "INSERT INTO fetches(id,request_id,attempt,started_at,retrieved_at,body_sha256,state,status) VALUES (?,?,1,?,?,?,'ok',200)",
            (f"fetch-{index}", f"request-{index}", AT, AT, sha),
        )
        db.execute(
            "INSERT INTO entities(fetch_id,kind,ticker,parser_version,data_json) VALUES (?,'synthetic','SYNTHETIC',1,?)",
            (f"fetch-{index}", json_text({"value": value})),
        )
        db.execute(
            "INSERT INTO observations(fetch_id,ticker,kind,parser_version,data_json) VALUES (?,'SYNTHETIC','synthetic',1,?)",
            (f"fetch-{index}", json_text({"value": value})),
        )
        if version == 2:
            db.execute(
                "INSERT INTO settlement_versions VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    index,
                    "kalshi",
                    "SYNTHETIC",
                    index,
                    index - 1 or None,
                    digest(value),
                    json_text({"synthetic": value}),
                    "corrected",
                    f"fetch-{index}",
                ),
            )
            db.execute(
                "INSERT INTO settlement_observations VALUES ('kalshi','SYNTHETIC',?,?,1)",
                (f"fetch-{index}", index),
            )
    db.execute(
        "INSERT INTO eligibility(run_id,ticker,checked_at,schedule_fetch_id,market_fetch_id,game_id,eligible,reason,cutoff,data_json) VALUES ('synthetic-run','SYNTHETIC',?,'fetch-1','fetch-2',900001,1,'synthetic',?,'{}')",
        (AT, AT),
    )
    if version == 2:
        db.execute(
            "INSERT INTO settlement_targets VALUES ('target','synthetic-run','SYNTHETIC','fetch-1','fetch-2','fetch-2','fetch-3',NULL,'pending')"
        )
    return db


def snapshot(db):
    return {
        r[0]: db.execute('SELECT * FROM "' + r[0] + '" ORDER BY rowid').fetchall()
        for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    }


def common(db):
    return dict(
        schema_version=1,
        run_id="synthetic-run",
        created_at=AT,
        code_hash=HASH,
        config_hash=HASH,
        mode="synthetic",
        references=[reference(db, "fetches", {"id": "fetch-1"})],
    )


def envelopes(db):
    c = common(db)
    discovery = c | dict(
        session_id="synthetic-pass",
        phase_sequence=1,
        completed_at=AT,
        complete=True,
        reasons=[],
        counts={"games": 1, "contracts": 2},
        eligibility_after_id=0,
        eligibility_through_id=1,
        manifest={"synthetic": True},
    )
    binding = c | dict(
        candidate_key="synthetic-game",
        revision=1,
        previous_id=None,
        policy_version="t60-v1",
        game_id=900001,
        event_ticker="KXMLBGAME-SYNTHETIC",
        ticker="KXMLBGAME-SYNTHETIC-A",
        yes_team_id=1,
        team_ids=[1, 2],
        original_date="2026-09-30",
        original_start="2026-09-30T20:00:00+00:00",
        game_number=1,
        scheduled_start="2026-09-30T20:00:00+00:00",
        cutoff=AT,
        status="synthetic-prepared",
        reasons=[],
        manifest={"synthetic": True},
        terms_digest=HASH,
    )
    discovery_id = journal.append(db, "discovery_passes", discovery)
    binding_id = journal.append(db, "forecast_bindings", binding)
    decision = c | dict(
        dataset="synthetic-study",
        protocol="mlb-t60-v1",
        game_id=900001,
        unresolved_key=None,
        ticker=binding["ticker"],
        yes_team_id=1,
        horizon=3600,
        scheduled_start=binding["scheduled_start"],
        cutoff=AT,
        binding_id=binding_id,
        discovery_id=discovery_id,
        selection_version="v1",
        baseline_versions=["constant-v1", "midpoint-v1"],
        cutoff_eligible=True,
        reasons=[],
        results={
            "constant-v1": {"probability": "0.5", "reasons": [], "inputs": {}},
            "midpoint-v1": {"probability": None, "reasons": ["book_missing"], "inputs": {}},
        },
        manifest={"synthetic": True},
        source_ceiling=3,
        supersedes_id=None,
    )
    decision_id = journal.append(db, "forecast_decisions", decision)
    publication = c | dict(
        decision_id=decision_id,
        decision_digest=digest(decision),
        attempted_at=AT,
        verdict="allowed",
        publication_policy="v1",
        reasons=[],
        manifest={"synthetic": True},
    )
    publication_id = journal.append(db, "forecast_publications", publication)
    receipt = c | dict(
        subject_kind="publication",
        fetch_id=None,
        binding_id=None,
        publication_id=publication_id,
        discovery_id=None,
        subject_digest=digest(publication),
        observed_at=AT,
        session_id="synthetic-session",
        observer_namespace="synthetic-observer",
        monotonic_offset="0.1",
        clock_status="trusted",
    )
    receipt_id = journal.append(db, "forecast_receipts", receipt)
    item = c | dict(
        opportunity_key="synthetic-game",
        game_id=900001,
        horizon=3600,
        decision_id=decision_id,
        publication_id=publication_id,
        receipt_id=receipt_id,
        mapping_id=None,
        mlb_id=None,
        kalshi_id=3,
        cutoff_status="eligible",
        cutoff_reasons=[],
        publication_status="publication_unconfirmed",
        publication_reasons=["not_verified"],
        outcome_status="pending",
        outcome_reasons=["synthetic_not_scorable"],
        payout=None,
        y=None,
        scores={"constant-v1": None, "midpoint-v1": None},
        clipped={"constant-v1": False, "midpoint-v1": False},
        paired_delta=None,
    )
    item["references"] = item["references"] + [reference(db, "settlement_versions", {"id": 3})]
    evaluation = c | dict(
        dataset="synthetic-study",
        protocol="mlb-t60-v1",
        view="cutoff-reconstruction",
        evaluator_version="v1",
        runtime_version="decimal40",
        outcomes_as_of=AT,
        source_ceiling=3,
        population_manifest=[
            {k: item[k] for k in ("opportunity_key", "decision_id", "game_id", "horizon")}
        ],
        publication_manifest=[reference(db, "forecast_publications", {"id": publication_id})],
        outcome_manifest=[reference(db, "settlement_versions", {"id": 3})],
        status="inconclusive",
        report={"items_digest": digest([item]), "synthetic": True},
        supersedes_id=None,
        correction_reason=None,
    )
    for k in ("publication", "outcome"):
        evaluation[k + "_digest"] = digest(evaluation[k + "_manifest"])
    return dict(
        discovery_passes=discovery,
        forecast_bindings=binding,
        forecast_decisions=decision,
        forecast_publications=publication,
        forecast_receipts=receipt,
        evaluation_runs=evaluation,
        evaluation_items=item,
    )


def populated(path):
    db = legacy(path)
    schema.migrate(db)
    payloads = envelopes(db)
    journal.append_evaluation(db, payloads["evaluation_runs"], [payloads["evaluation_items"]])
    return db, payloads
