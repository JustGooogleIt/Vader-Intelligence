"""Registered forecasters at the cutoff (decision D-004).

Forward features use only evidence the projection admits at C: market-listing
observations whose fetch was retrieved at or before C and witnessed by an operational
receipt observed at or before C (`evidence.fetch_availability`). Game context (home team,
game type) comes from the schedule retrieval that renewed the decision. Only
forward-capable feature sets are accepted; a missing or incompatible artifact, or a
feature that may not be missing, is an explicit abstention reason, never a fallback.

Model inference is delegated to a `predict` callable (artifact JSON bytes, ordered feature
values) → probability, supplied by the model library, so this module has no dependency on
how a model family is evaluated.
"""

import hashlib
import json
from datetime import datetime, timedelta

from ..features import definitions, quotes
from . import evidence

LOOKBACK = timedelta(hours=2)
PROBABILITY_PLACES = 6


def forecaster_id(sha):
    return "model:" + sha[:16]


def witnessed_listing(db, ticker, cutoff, namespace, mode):
    """QuoteSeries of listing observations retrieved and witnessed by C (bounded lookback)."""
    rows = db.execute(
        "SELECT f.id, f.retrieved_at, o.data_json FROM observations o JOIN fetches f"
        " ON f.id=o.fetch_id WHERE o.ticker=? AND o.kind='market' AND f.state='ok'"
        " AND f.retrieved_at<=? AND f.retrieved_at>=? ORDER BY f.seq",
        (ticker, cutoff.isoformat(), (cutoff - LOOKBACK).isoformat()),
    ).fetchall()
    kept = []
    for fetch_id, retrieved_at, data_json in rows:
        availability = evidence.fetch_availability(db, fetch_id, namespace=namespace, mode=mode)
        if availability.status != "witnessed" or availability.observed_at > cutoff:
            continue
        kept.append((datetime.fromisoformat(retrieved_at), json.loads(data_json), fetch_id))
    return quotes.from_listing(ticker, kept)


def game_context(db, schedule_fetch_id, game_id):
    """(home team id, game type) from an archived official schedule body, or (None, None)."""
    if schedule_fetch_id is None:
        return None, None
    row = db.execute(
        "SELECT b.body FROM fetches f JOIN blobs b ON b.sha256=f.body_sha256 WHERE f.id=?",
        (schedule_fetch_id,),
    ).fetchone()
    if row is None:
        return None, None
    for date in json.loads(row[0]).get("dates", []):
        for game in date.get("games", []):
            if game.get("gamePk") == game_id:
                home = game.get("teams", {}).get("home", {}).get("team", {}).get("id")
                return home, game.get("gameType")
    return None, None


class ForecasterRunner:
    """Loads registered artifacts once and computes their forecasts for one decision."""

    def __init__(self, artifacts, predict):
        """`artifacts`: {sha256: artifact JSON bytes} already read from the registry."""
        self.predict = predict
        self.models = {}
        for sha, data in sorted(artifacts.items()):
            if hashlib.sha256(data).hexdigest() != sha:
                raise ValueError(f"artifact {sha} failed verification")
            artifact = json.loads(data)
            feature_set = artifact.get("training", {}).get("feature_set")
            if feature_set not in definitions.FEATURE_SETS:
                raise ValueError(f"artifact {sha[:12]} names no known feature set")
            if not definitions.describe_set(feature_set)["forward_capable"]:
                raise ValueError(
                    f"artifact {sha[:12]} uses feature set {feature_set}, which is not "
                    "forward capable; it can only be evaluated on research datasets"
                )
            names = [d.name for d in definitions.feature_set(feature_set)]
            if artifact.get("feature_names") != names:
                raise ValueError(f"artifact {sha[:12]} features differ from {feature_set}")
            self.models[sha] = {
                "data": data,
                "feature_set": feature_set,
                "family": artifact.get("family"),
                "names": names,
            }

    def descriptors(self):
        return [
            {
                "id": forecaster_id(sha),
                "artifact_sha256": sha,
                "family": m["family"],
                "feature_set": m["feature_set"],
            }
            for sha, m in sorted(self.models.items(), key=lambda kv: forecaster_id(kv[0]))
        ]

    def results(self, db, *, binding, facts, namespace, evidence_mode):
        """{forecaster id: {probability, reasons, inputs}} for one decision at its cutoff."""
        out = {}
        if not facts.cutoff_eligible:
            for d in self.descriptors():
                out[d["id"]] = {"probability": None, "reasons": list(facts.reasons), "inputs": {}}
            return out
        cutoff = facts.cutoff
        alternatives = binding["manifest"].get("alternatives", [])
        selected = witnessed_listing(db, binding["ticker"], cutoff, namespace, evidence_mode)
        other = (
            witnessed_listing(db, alternatives[0], cutoff, namespace, evidence_mode)
            if len(alternatives) == 1
            else None
        )
        home, game_type = game_context(db, facts.renewal.schedule_fetch_id, binding["game_id"])
        context_reasons = []
        if home is None or game_type is None:
            context_reasons.append("model_game_context_unavailable")
        for sha, model in sorted(self.models.items()):
            key = forecaster_id(sha)
            if context_reasons:
                out[key] = {"probability": None, "reasons": context_reasons, "inputs": {}}
                continue
            try:
                ctx = definitions.FeatureContext(
                    cutoff=cutoff,
                    start=datetime.fromisoformat(binding["scheduled_start"]),
                    official_date=binding["original_date"],
                    game_type=game_type,
                    selected_team=binding["yes_team_id"],
                    opponent_team=next(
                        t for t in binding["team_ids"] if t != binding["yes_team_id"]
                    ),
                    selected_is_home=binding["yes_team_id"] == home,
                    selected=selected,
                    other=other,
                )
                values = definitions.compute(model["feature_set"], ctx)
            except ValueError as exc:
                out[key] = {
                    "probability": None,
                    "reasons": ["model_feature_unavailable:" + str(exc)[:80]],
                    "inputs": {},
                }
                continue
            try:
                probability = self.predict(model["data"], [values[n] for n in model["names"]])
            except (ValueError, KeyError, TypeError) as exc:
                out[key] = {
                    "probability": None,
                    "reasons": ["model_incompatible:" + str(exc)[:80]],
                    "inputs": {},
                }
                continue
            low, high = 10**-PROBABILITY_PLACES, 1 - 10**-PROBABILITY_PLACES
            clipped = min(max(float(probability), low), high)
            out[key] = {
                "probability": f"{clipped:.{PROBABILITY_PLACES}f}",
                "reasons": [],
                "inputs": {
                    "artifact_sha256": sha,
                    "features": values,
                    "selected_quote_fetches": sorted({q.ref for q in selected.quotes})[-20:],
                    "selected_quotes": len(selected.quotes),
                },
            }
        return out


_LOADED = {}


def predict_artifact(data, values):
    """Model-library inference for one row (None → NaN for the artifact's own missing-value
    treatment). Artifacts are parsed once per digest."""
    import numpy as np

    from ..models.artifact import ModelArtifact, predict_proba

    sha = hashlib.sha256(data).hexdigest()
    if sha not in _LOADED:
        _LOADED[sha] = ModelArtifact.from_dict(json.loads(data))
    row = np.array([[np.nan if v is None else float(v) for v in values]], dtype=np.float64)
    return float(predict_proba(_LOADED[sha], row)[0])


def runner_from_registry(registry_root, protocol):
    """A runner for the protocol's forecasters read (and verified) from a research registry;
    None when the protocol registers none."""
    if not protocol.forecasters:
        return None
    from ..research.registry import Registry

    registry = Registry(registry_root, readonly=True)
    try:
        artifacts = {sha: registry.model_artifact(sha) for sha in protocol.forecasters}
    finally:
        registry.close()
    return ForecasterRunner(artifacts, predict_artifact)
