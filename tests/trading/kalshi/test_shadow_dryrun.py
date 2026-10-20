"""Live-adapter dry run from a SYNTHETIC forward-shadow decision (roadmap H).

A registered forecaster's probability at C becomes an order request through the normal
strategy -> risk -> executor path into a `DryRunGateway`: built, signed with a throwaway
key, recorded, never sent. The archive is opened read-only.
"""

import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "forecast"))
from _f34 import at, collect  # noqa: E402
from test_registered_forecasters import stub_artifact, tick_at_runner, to_schema6  # noqa: E402

from vader_intelligence.forecast.forecasters import ForecasterRunner  # noqa: E402
from vader_intelligence.forecast.research import Protocol  # noqa: E402
from vader_intelligence.trading import shadow_dryrun  # noqa: E402
from vader_intelligence.trading.kalshi.auth import Signer  # noqa: E402
from vader_intelligence.trading.marketdata import open_archive  # noqa: E402

KEY_ID = "00000000-0000-4000-8000-000000000000"


@pytest.fixture
def no_network(monkeypatch):
    """Call after the synthetic archive exists: from then on nothing may be sent."""

    def forbidden(self, *args, **kwargs):
        raise AssertionError("the dry run must not send anything")

    def install():
        monkeypatch.setattr(httpx.Client, "send", forbidden)
        monkeypatch.setattr(httpx.MockTransport, "handle_request", forbidden)
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)

    return install


def shadow_archive(store, config, predict):
    to_schema6(store)
    sha, data = stub_artifact()
    runner = ForecasterRunner({sha: data}, predict)
    proto = Protocol(dataset="models", forecasters=[sha])
    collect(store, config, origin=at(18, 3))
    tick_at_runner(store, proto, at(18, 3, 30), runner)
    tick_at_runner(store, proto, at(18, 5, 30), runner)
    (payload,) = [
        json.loads(r[0]) for r in store.db.execute("SELECT payload_json FROM forecast_decisions")
    ]
    (forecaster,) = payload["forecasters"]
    return forecaster["id"]


def test_a_confident_forecast_becomes_a_signed_request_that_is_never_sent(
    store, config, tmp_path, no_network
):
    forecaster = shadow_archive(store, config, lambda data, values: min(0.95, values[0] + 0.25))
    no_network()
    db = open_archive(config.database)
    throwaway = ed25519.Ed25519PrivateKey.generate()  # never a real credential
    try:
        result = shadow_dryrun.run(
            db,
            forecaster_id=forecaster,
            ledger_path=tmp_path / "dryrun.sqlite3",
            signer=Signer(KEY_ID, throwaway),
            decision_mode="synthetic",
        )
    finally:
        db.close()
    assert result["decisions"] == 1 and result["signed"] is True
    (outcome,) = result["outcomes"]
    assert outcome["outcome"] == "submitted_dry", outcome
    assert outcome["side"] == "yes" and outcome["book_as_of"] <= outcome["cutoff"]
    request, cancel = result["requests"]  # the order, then its cancel (no phantom exposure)
    assert request["method"] == "POST" and cancel["method"] == "DELETE"
    assert outcome["canceled_after_dry_run"] == "canceled"
    assert int(outcome["approved_quantity"]) >= 1 and outcome["sizing"]
    assert "activation gates" in result["checks"]["not_checked"]
    assert request["url"].startswith("https://external-api.demo.kalshi.co/")
    assert request["headers"]["KALSHI-ACCESS-SIGNATURE"].startswith("sha256:")
    body = json.loads(request["body"])
    assert body["ticker"] == outcome["ticker"] and body["side"] == "bid"
    assert result["ledger_verification"] == []
    assert result["notice"].startswith("Dry run")
    db = open_archive(config.database)
    try:
        with pytest.raises(ValueError, match="new paper ledger"):  # no silent replays
            shadow_dryrun.run(
                db,
                forecaster_id=forecaster,
                ledger_path=tmp_path / "dryrun.sqlite3",
                decision_mode="synthetic",
            )
        capped = shadow_dryrun.run(
            db,
            forecaster_id=forecaster,
            ledger_path=tmp_path / "capped.sqlite3",
            max_order_notional="0.10",
            decision_mode="synthetic",
        )
    finally:
        db.close()
    (outcome,) = capped["outcomes"]
    assert (
        outcome["outcome"] == "refused" and outcome["submit_reason"] == "local:order_notional_cap"
    )
    assert capped["requests"] == [] and capped["checks"]["max_order_notional"] == "0.10"


def test_a_forecast_without_edge_produces_no_request(store, config, tmp_path, no_network):
    forecaster = shadow_archive(store, config, lambda data, values: values[0])
    no_network()
    db = open_archive(config.database)
    try:
        result = shadow_dryrun.run(
            db,
            forecaster_id=forecaster,
            ledger_path=tmp_path / "dryrun.sqlite3",
            decision_mode="synthetic",
        )
    finally:
        db.close()
    (outcome,) = result["outcomes"]
    assert (outcome["outcome"], outcome["reason"]) == ("no_intent", "edge_below_threshold")
    assert result["requests"] == [] and result["signed"] is False


def test_cli_dry_run_signs_with_a_throwaway_key(store, config, tmp_path, no_network):
    from vader_intelligence.cli import execute, parser

    forecaster = shadow_archive(store, config, lambda data, values: min(0.95, values[0] + 0.25))
    no_network()
    args = parser().parse_args(
        [
            "live",
            "dry-run",
            "--archive",
            config.database,
            "--forecaster",
            forecaster,
            "--ledger",
            str(tmp_path / "dry.sqlite3"),
            "--sign-with-throwaway-key",
            "--decision-mode",
            "synthetic",
        ]
    )
    result = execute(args)
    assert result["status"] == "complete" and result["signed"] is True
    assert result["decision_mode"] == "synthetic"
    assert [o["outcome"] for o in result["outcomes"]] == ["submitted_dry"]
    assert [r["method"] for r in result["requests"]] == ["POST", "DELETE"]
    args.ledger, args.decision_mode = str(tmp_path / "forward.sqlite3"), "forward_shadow"
    forward = execute(args)  # the default: this SYNTHETIC archive has no forward decision
    assert forward["status"] == "inconclusive" and forward["outcomes"] == []
    assert forward["decisions_excluded"] == {"other_mode": 1, "superseded": 0}


def add_decision(store, base_id, *, mode, supersedes_id=None):
    """Hand-insert a copy of a decision row with another mode or as a correction
    (SYNTHETIC records exercising decision selection only; lineage is not valid)."""
    columns = [r[1] for r in store.db.execute("PRAGMA table_info(forecast_decisions)")]
    row = dict(
        zip(
            columns,
            store.db.execute("SELECT * FROM forecast_decisions WHERE id=?", (base_id,)).fetchone(),
        )
    )
    n = store.db.execute("SELECT COUNT(*) FROM forecast_decisions").fetchone()[0]
    payload = json.loads(row["payload_json"]) | {"mode": mode, "supersedes_id": supersedes_id}
    text = json.dumps(payload, sort_keys=True)
    digest = hashlib.sha256(text.encode()).hexdigest()
    row |= {
        "id": None,
        "mode": mode,
        "supersedes_id": supersedes_id,
        "payload_json": text,
        "digest": digest,
        "idempotency_key": hashlib.sha256(f"{digest}:{base_id}:{mode}:{n}".encode()).hexdigest(),
    }
    with store.db:
        cursor = store.db.execute(
            f"INSERT INTO forecast_decisions({','.join(row)}) VALUES "
            f"({','.join('?' for _ in row)})",
            list(row.values()),
        )
    return cursor.lastrowid


def test_only_effective_forward_shadow_decisions_are_dry_run(store, config, tmp_path, no_network):
    forecaster = shadow_archive(store, config, lambda data, values: min(0.95, values[0] + 0.25))
    ((original,),) = store.db.execute("SELECT id FROM forecast_decisions").fetchall()
    correction = add_decision(store, original, mode="synthetic", supersedes_id=original)
    forward = add_decision(store, original, mode="forward_shadow")
    corrected = add_decision(store, original, mode="forward_shadow")
    add_decision(store, corrected, mode="historical_reconstruction", supersedes_id=corrected)
    add_decision(store, original, mode="historical_reconstruction")
    no_network()
    db = open_archive(config.database)
    try:
        assert [d[0] for d in shadow_dryrun.decisions(db, forecaster)] == [forward]
        assert [d[0] for d in shadow_dryrun.decisions(db, forecaster, "synthetic")] == [
            correction  # a decision and its correction never both yield an order
        ]
        result = shadow_dryrun.run(
            db, forecaster_id=forecaster, ledger_path=tmp_path / "forward.sqlite3"
        )
        with pytest.raises(ValueError, match="decision mode"):
            shadow_dryrun.decisions(db, forecaster, "historical_reconstruction")
    finally:
        db.close()
    assert result["decision_mode"] == "forward_shadow" and result["decisions"] == 1
    assert [o["decision_id"] for o in result["outcomes"]] == [forward]
    assert result["decisions_excluded"] == {"other_mode": 4, "superseded": 1}
