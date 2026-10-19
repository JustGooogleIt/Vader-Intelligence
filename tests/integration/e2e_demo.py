"""Deterministic offline end-to-end demonstration (roadmap H). SYNTHETIC data only.

One chain through every integrated layer, with injected transports and clocks:

1. research registry: a synthetic dataset whose outcome follows a sharpened market mid
   (y ~ Bernoulli(sigmoid(2.5 * logit(mid))), mid ~ Beta(2, 2)); the planted relation
   proves the wiring, not anything about markets;
2. research pods (real worker subprocesses): a logistic `train_model` and a
   `symbolic_search` on a development/validation snapshot;
3. protected evaluator: recomputes validation from the registry and decides under the
   default criteria;
4. forward shadow: the validated artifact runs as a registered forecaster at C on the
   shipped collector fixture, the game settles, a contract-2 evaluation scores it, and
   the evaluation becomes shadow evidence;
5. paper: a coordinator-run backtest on labelled synthetic execution quotes becomes paper
   evidence; a synthetic operator approval promotes to paper_approved (never live);
6. the candidate's one holdout evaluation, recorded in both ledgers;
7. live-adapter dry run: the shadow decision becomes a signed (throwaway key), unsent
   order request;
8. execution arithmetic on the synthetic history archive.

Hand-checkable numbers (asserted):

- Fixture book at C: best YES bid 0.6400, best NO bid 0.3500, so YES ask 0.6500 and
  midpoint 0.6450. The game settles YES. For the model probability p printed below:
  Brier (p - 1)^2, log loss -ln p; the midpoint scores (0.645 - 1)^2 = 0.126025 and
  -ln 0.645 = 0.438505.
- History archive execution: forecast 0.75 against a YES ask of 0.61 places one buy at
  0.61. Candles carry no sizes, so without an assumed depth nothing fills; with 4 assumed
  contracts and the 50% haircut at most 2 fill at 0.61, and since the YES side won the
  realized P&L is quantity * (1 - 0.61) - fees.

Usage: .venv/bin/python tests/integration/e2e_demo.py [--keep DIR]
"""

import argparse
import hashlib
import json
import math
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for extra in ("tests", "tests/forecast", "tests/evaluation", "src"):
    sys.path.insert(0, str(ROOT / extra))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from _f34 import at, collect  # noqa: E402
from conftest import Clock  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402
from history import test_acquire as synthetic_history  # noqa: E402
from test_materialization import settle, settlement_evidence  # noqa: E402
from test_registered_forecasters import tick_at_runner, to_schema6  # noqa: E402

from vader_intelligence.config import Config  # noqa: E402
from vader_intelligence.datasets import history as dataset_history  # noqa: E402
from vader_intelligence.datasets.splits import SplitDefinition  # noqa: E402
from vader_intelligence.evaluation.service import evaluate, inspect_evaluation  # noqa: E402
from vader_intelligence.features import definitions  # noqa: E402
from vader_intelligence.forecast.forecasters import ForecasterRunner, predict_artifact  # noqa: E402
from vader_intelligence.forecast.research import Protocol  # noqa: E402
from vader_intelligence.history.acquire import Acquisition  # noqa: E402
from vader_intelligence.pods import evaluator  # noqa: E402
from vader_intelligence.pods.candidates import (  # noqa: E402
    HoldoutAlreadyConsumed,
    OperatorApprovalRecord,
    candidate_record,
)
from vader_intelligence.pods.contracts import (  # noqa: E402
    Budgets,
    ContractError,
    ExperimentManifest,
    PodSpec,
    TaskSpec,
)
from vader_intelligence.pods.coordinator import Coordinator  # noqa: E402
from vader_intelligence.pods.snapshot import feature_set_version, load  # noqa: E402
from vader_intelligence.pods.store import open_store  # noqa: E402
from vader_intelligence.provenance import json_text, source_hash  # noqa: E402
from vader_intelligence.research import backtest  # noqa: E402
from vader_intelligence.research.registry import Registry  # noqa: E402
from vader_intelligence.storage import Store  # noqa: E402
from vader_intelligence.trading import shadow_dryrun  # noqa: E402
from vader_intelligence.trading.kalshi.auth import Signer  # noqa: E402
from vader_intelligence.trading.marketdata import open_archive  # noqa: E402
from vader_intelligence.trading.simulator import ForecastInput, PaperConfig, run_paper  # noqa: E402

SPLIT = SplitDefinition("2026-07-31", "2026-08-31", embargo_days=1)
FIXTURE_MID = 0.645
NYM = "KXMLBGAME-26SEP201310PHINYM-NYM"


def demo_dataset(per_day=8, slope=2.5, seed=7, n_days=200):
    rng = np.random.default_rng(seed)
    names = [d.name for d in definitions.feature_set("fs-market-v1")]
    rows, start, game = [], date(2026, 4, 1), 900000
    for d in range(n_days):
        day = (start + timedelta(days=d)).isoformat()
        for _ in range(per_day):
            game += 1
            mid = float(np.clip(rng.beta(2, 2), 0.05, 0.95))
            truth = 1 / (1 + np.exp(-slope * np.log(mid / (1 - mid))))
            features = {n: None for n in names}
            features.update(
                mid_c=mid,
                logit_mid_c=float(np.log(mid / (1 - mid))),
                spread_c=0.02,
                pair_mid_c=mid,
                selected_is_home=float(rng.random() < 0.5),
                postseason=0.0,
            )
            rows.append(
                {
                    "opportunity_key": f"game:{game}",
                    "group": game,
                    "official_date": day,
                    "start": day + "T23:00:00+00:00",
                    "cutoff": day + "T22:00:00+00:00",
                    "ticker": f"SYN-{game}-A",
                    "event_ticker": f"SYN-{game}",
                    "selected_team_id": 1,
                    "features": features,
                    "y": int(rng.random() < truth),
                }
            )
    data = "".join(json_text(r) + "\n" for r in rows).encode()
    manifest = {
        "format": "dataset-v1",
        "name": "e2e-demo-synthetic",
        "evidence_kind": "synthetic",
        "feature_set": definitions.describe_set("fs-market-v1"),
        "rows": len(rows),
        "rows_sha256": hashlib.sha256(data).hexdigest(),
    }
    return manifest, rows


def synthetic_execution(rows):
    """Labelled synthetic quotes 60 s either side of each cutoff (bid mid-0.01, ask
    mid+0.01, on the cent grid), settled by the row's label."""
    markets, snapshots, settlements = [], [], []
    for row in rows:
        cutoff = datetime.fromisoformat(row["cutoff"])
        mid = row["features"]["mid_c"]
        bid = max(0.01, math.floor(mid * 100) / 100 - 0.01)
        ask = min(0.99, math.ceil(mid * 100) / 100 + 0.01)
        markets.append(
            {
                "ticker": row["ticker"],
                "event_ticker": row["event_ticker"],
                "close_time": (cutoff + timedelta(hours=4)).isoformat(),
                "game_date": row["official_date"],
                "teams": [str(row["group"])],
            }
        )
        for offset in (-60, 60):
            snapshots.append(
                {
                    "ticker": row["ticker"],
                    "as_of": (cutoff + timedelta(seconds=offset)).isoformat(),
                    "yes_bid": f"{bid:.2f}",
                    "yes_ask": f"{ask:.2f}",
                    "ref": f"synthetic:{row['group']}:{offset}",
                }
            )
        settlements.append(
            {
                "ticker": row["ticker"],
                "result": "yes" if row["y"] else "no",
                "effective_at": (cutoff + timedelta(hours=5)).isoformat(),
                "ref": f"synthetic-label:{row['group']}",
            }
        )
    return {
        "schema": backtest.EXECUTION_SCHEMA,
        "source": {"interpretation": "synthetic", "run_id": None},
        "window_seconds": {"before": 60, "after": 60},
        "markets": markets,
        "snapshots": snapshots,
        "settlements": settlements,
        "skipped": {},
        "notice": "SYNTHETIC quotes: not archived evidence",
    }


def submit(store, pod, operation, params):
    snapshot = load(store.artifacts.get_bytes(params["dataset_manifest_hash"]))
    manifest = ExperimentManifest(
        dataset_manifest_hash=params["dataset_manifest_hash"],
        feature_set_versions={snapshot.feature_set: feature_set_version(snapshot.manifest)},
        split={
            "development": {"through": SPLIT.development_through},
            "validation": {"through": SPLIT.validation_through},
        },
        code_hash=source_hash(),
        operation=operation,
        params=params,
        seed=int(params.get("seed", 0)),
        budget={"pod": pod},
    )
    sha = store.put_experiment_manifest(manifest)
    return store.submit(TaskSpec(pod, operation, params, sha, 0, 2))


def stage_pods(workdir, registry, dataset_id, manifest):
    store = open_store(workdir / "orchestration")
    store.register_pod(PodSpec("baseline", "baseline_models", Budgets(wall_seconds_per_task=600)))
    store.register_pod(PodSpec("symbolic", "symbolic_search", Budgets(wall_seconds_per_task=600)))
    snapshot = evaluator.snapshot_dataset(store, registry, dataset_id, SPLIT)
    trained = submit(
        store,
        "baseline",
        "train_model",
        {
            "dataset_manifest_hash": snapshot,
            "feature_set": "fs-market-v1",
            "feature_set_version": feature_set_version(manifest),
            "model_family": "logistic",
            "seed": 0,
            "hyperparameters": {"grid": {"C": [0.1, 1.0]}, "n_folds": 3},
        },
    )
    searched = submit(
        store,
        "symbolic",
        "symbolic_search",
        {
            "dataset_manifest_hash": snapshot,
            "seed": 1,
            "max_candidates": 3000,
            "max_generations": 8,
            "max_seconds": 60.0,
            "population": 60,
        },
    )
    with Coordinator(store) as coordinator:
        ran = coordinator.run(max_steps=100_000, deadline_seconds=600)
    states = {t: store.task(t)["state"] for t in (trained, searched)}
    candidates = {
        "logistic": evaluator.accept_model(store, registry, trained),
        "symbolic": evaluator.accept_model(store, registry, searched),
    }
    decisions = {name: evaluator.evaluate(store, registry, cid) for name, cid in candidates.items()}
    return store, snapshot, ran, states, candidates, decisions


def stage_shadow(workdir, model_id, artifact):
    config = Config(database=str(workdir / "shadow.sqlite3"), min_free_bytes=1)
    shadow = Store(config.database, min_free_bytes=1)
    monkeypatch = pytest.MonkeyPatch()
    try:
        to_schema6(shadow)
        collect(shadow, config, origin=at(18, 3))
        proto = Protocol(dataset="e2e-demo", forecasters=[model_id])
        runner = ForecasterRunner({model_id: artifact}, predict_artifact)
        tick_at_runner(shadow, proto, at(18, 3, 30), runner)
        tick_at_runner(shadow, proto, at(18, 5, 30), runner)
        settle(shadow, config, monkeypatch, settlement_evidence())
        clock = Clock()
        clock.origin = at(23, 45)
        result = evaluate(
            shadow,
            proto,
            view="forward-shadow",
            outcomes_as_of=at(23, 30),
            clock=clock.now,
            monotonic=clock.mono,
        )
        evaluation = inspect_evaluation(shadow.db, result["evaluation_id"])
    finally:
        monkeypatch.undo()
        shadow.close()
    return config, result, evaluation


def stage_execution_arithmetic(workdir):
    config = Config(database=str(workdir / "history.sqlite3"), min_free_bytes=1)
    store = Store(config.database, min_free_bytes=1)
    try:
        reader = synthetic_history.history_reader(
            store, config, synthetic_history.handler_factory([])
        )
        try:
            Acquisition(store, reader).run(
                start_date=date(2026, 9, 20),
                through_date=date(2026, 9, 20),
                post_cutoff_seconds=900,
            )
        finally:
            reader.close()
        _, rows = dataset_history.build(
            store.db,
            feature_set="fs-market-v1",
            start_date=date(2026, 9, 20),
            through_date=date(2026, 9, 20),
            name="e2e-history",
        )
        execution = backtest.execution_inputs(store.db, rows)
    finally:
        store.close()
    nym = next(r for r in rows if r["ticker"] == NYM)
    forecast = ForecastInput(
        forecast_id="hand:nym",
        ticker=NYM,
        probability=Decimal("0.75"),
        computed_at=datetime.fromisoformat(nym["cutoff"]),
        model_id="hand",
    )
    inputs = backtest.paper_inputs(execution, [forecast], "synthetic-history")
    no_depth = run_paper(PaperConfig(), inputs, workdir / "hand-a.sqlite3").to_dict()
    depth = run_paper(
        PaperConfig(assumed_depth=Decimal(4)), inputs, workdir / "hand-b.sqlite3"
    ).to_dict()
    quantity = sum(Decimal(f["quantity"]) for f in depth["fills"])
    fees = sum(Decimal(f["fee"]) for f in depth["fills"])
    return {
        "order_limit": no_depth["orders"][0]["limit_price"],
        "fills_without_depth": len(no_depth["fills"]),
        "filled_quantity_with_depth_4": str(quantity),
        "fill_prices": sorted({f["price"] for f in depth["fills"]}),
        "fees": str(fees),
        "realized": depth["pnl"]["realized"],
        "expected_realized": str(quantity * Decimal("0.39") - fees),
    }


def run(workdir):
    workdir = Path(workdir)
    out = {"notice": "SYNTHETIC end-to-end demonstration; no live data, orders or credentials"}
    registry = Registry(workdir / "research")
    try:
        manifest, rows = demo_dataset()
        dataset_id = registry.register_dataset(manifest, rows)
        store, snapshot, ran, states, candidates, decisions = stage_pods(
            workdir, registry, dataset_id, manifest
        )
        try:
            out["pods"] = {"coordinator": ran, "tasks": states}
            out["evaluator"] = {
                name: {
                    "state": d["state"],
                    "paired_n": d["recomputed"]["paired_n"],
                    "paired_delta": d["recomputed"].get("paired_delta"),
                    "half_deltas": d["recomputed"].get("half_deltas"),
                    "proposals_on_snapshot": d["proposals"],
                }
                for name, d in decisions.items()
            }
            candidate = candidates["logistic"]
            model_id = candidate_record(store, candidate)["spec"]["model_sha256"]
            artifact = registry.model_artifact(model_id)

            config, result, evaluation = stage_shadow(workdir, model_id, artifact)
            forecaster_id = "model:" + model_id[:16]
            report = result["report"]["forecasters"][forecaster_id]
            db = open_archive(config.database)
            try:
                (payload,) = db.execute("SELECT payload_json FROM forecast_decisions").fetchone()
            finally:
                db.close()
            p = float(json.loads(payload)["results"][forecaster_id]["probability"])
            out["shadow"] = {
                "evaluation_id": result["evaluation_id"],
                "status": result["status"],
                "model_probability": p,
                "model_brier": report["mean_brier"],
                "expected_model_brier": f"{(p - 1) ** 2:.6f}",
                "model_log_loss": report["mean_log_loss"],
                "expected_model_log_loss": f"{-math.log(p):.6f}",
                "paired_with_midpoint": report["paired_with_midpoint"],
                "midpoint_brier": f"{(FIXTURE_MID - 1) ** 2:.6f}",
                "midpoint_log_loss": f"{-math.log(FIXTURE_MID):.6f}",
            }
            if decisions["logistic"]["state"] == "validated":
                out["shadow"]["state"] = evaluator.record_shadow(store, candidate, evaluation)

                summary = backtest.simulate(
                    registry,
                    synthetic_execution(load(store.artifacts.get_bytes(snapshot)).validation),
                    dataset_id,
                    model_id,
                    SPLIT,
                    PaperConfig(assumed_depth=Decimal(10)),
                    segment="validation",
                    ledger_path=workdir / "paper-validation.sqlite3",
                )
                paper = evaluator.paper_evidence(store, candidate, summary)
                approval = OperatorApprovalRecord(
                    candidate,
                    "synthetic-demo-operator",
                    datetime(2026, 10, 19, tzinfo=timezone.utc).isoformat(),
                    "SYNTHETIC demo approval for paper only",
                )
                out["paper"] = {
                    "forecasts": summary["forecasts"],
                    "orders": len(summary["report"]["orders"]),
                    "fills": len(summary["report"]["fills"]),
                    "equity_conservative": summary["report"]["pnl"]["equity_conservative"],
                    "realized": summary["report"]["pnl"]["realized"],
                    "verification": summary["report"]["verification"],
                    "state": evaluator.record_paper(store, candidate, paper, approval),
                }
                holdout = evaluator.evaluate_holdout(store, registry, candidate)
                try:
                    evaluator.evaluate_holdout(store, registry, candidate)
                    second = "allowed"
                except (ContractError, HoldoutAlreadyConsumed) as exc:
                    second = f"refused: {exc}"
                out["holdout"] = {
                    "model_log_loss": holdout["holdout"]["model"]["log_loss"],
                    "n": holdout["holdout"]["model"]["n"],
                    "registry_ledger": holdout["registry_ledger"],
                    "second_attempt": second,
                }
            out["candidate"] = {
                "id": candidate,
                "state": candidate_record(store, candidate)["state"],
                "transitions": [t["to"] for t in candidate_record(store, candidate)["transitions"]],
            }
        finally:
            store.close()

        db = open_archive(config.database)
        try:
            dry = shadow_dryrun.run(
                db,
                forecaster_id=forecaster_id,
                ledger_path=workdir / "dryrun.sqlite3",
                signer=Signer(
                    "00000000-0000-4000-8000-000000000000", ed25519.Ed25519PrivateKey.generate()
                ),
            )
        finally:
            db.close()
        out["dry_run"] = {
            "outcomes": [o["outcome"] for o in dry["outcomes"]],
            "requests": len(dry["requests"]),
            "url": dry["requests"][0]["url"] if dry["requests"] else None,
            "body": json.loads(dry["requests"][0]["body"]) if dry["requests"] else None,
        }
        out["execution_arithmetic"] = stage_execution_arithmetic(workdir)
    finally:
        registry.close()
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", help="keep the disposable data in this directory")
    args = parser.parse_args()
    if args.keep:
        Path(args.keep).mkdir(parents=True, exist_ok=True)
        result = run(args.keep)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            result = run(tmp)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
