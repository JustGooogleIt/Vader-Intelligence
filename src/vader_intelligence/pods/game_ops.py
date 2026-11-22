"""Approved game-research operations for Command Center farms (J3).

Fixed, declarative Python under the pods boundary: each reads only the coordinator-staged
evidence snapshot (and, for the checker, the stage-1 artifacts) by digest, writes its
output into its attempt directory and says what it is: a pod diagnostic, never a
forecast. No model registry, archive, ledger or provider is reachable from here.
"""

import json
from dataclasses import dataclass
from decimal import Decimal

from .contracts import HEX64, normalize_fields, require
from .operations import DeterministicFailure, register_operation

EVIDENCE_KIND = "cc_game_evidence"  # coordinator-built input (`command_center.farms`)
SUMMARY_KIND = "cc_evidence_summary"
BASELINE_KIND = "cc_baseline_analysis"
CHECK_KIND = "cc_independent_check"
EVIDENCE_VERSION = 1
DIAGNOSTIC = "pod diagnostic: not a forecast, not publication, not promotion evidence"
ROLES = ("diagnostics",)


def _hex64(value, what):
    require(bool(HEX64.match(value)), f"{what} must be a sha256 hex digest")


@dataclass(frozen=True)
class GameEvidenceParams:
    evidence_sha256: str
    game_id: int

    def __post_init__(self):
        normalize_fields(self)
        _hex64(self.evidence_sha256, "evidence_sha256")
        require(0 < self.game_id < 10**12, "game_id out of range")


@dataclass(frozen=True)
class CheckParams:
    evidence_sha256: str
    game_id: int
    summary_sha256: str
    baseline_sha256: str

    def __post_init__(self):
        normalize_fields(self)
        for name in ("evidence_sha256", "summary_sha256", "baseline_sha256"):
            _hex64(getattr(self, name), name)
        require(0 < self.game_id < 10**12, "game_id out of range")


def evidence_input(params, store):
    return ((params.evidence_sha256, EVIDENCE_KIND),)


def check_inputs(params, store):
    return (
        (params.evidence_sha256, EVIDENCE_KIND),
        (params.summary_sha256, SUMMARY_KIND),
        (params.baseline_sha256, BASELINE_KIND),
    )


def _load(ctx, sha256, kind, game_id=None):
    try:
        data = json.loads(ctx.read_input(sha256))
    except ValueError:
        raise DeterministicFailure(f"{kind} input is not JSON") from None
    if not isinstance(data, dict) or data.get("kind") != kind:
        raise DeterministicFailure(f"input {sha256[:12]} is not a {kind}")
    if game_id is not None and data.get("game_id") != game_id:
        raise DeterministicFailure("input belongs to another game")
    return data


def _latest_books(evidence):
    """Latest book observation per market (observations are newest first)."""
    books = {}
    for obs in evidence.get("observations", []):
        if obs.get("kind") == "book" and obs["ticker"] not in books:
            books[obs["ticker"]] = obs
    return books


def _summary(evidence):
    books = _latest_books(evidence)
    markets = {}
    for ticker in evidence.get("market_tickers", []):
        obs = [o for o in evidence.get("observations", []) if o["ticker"] == ticker]
        checks = [e for e in evidence.get("eligibility", []) if e["ticker"] == ticker]
        book = books.get(ticker)
        data = (book or {}).get("data") or {}
        markets[ticker] = {
            "observations": len(obs),
            "eligibility_checks": len(checks),
            "latest_reason": checks[0]["reason"] if checks else None,
            "latest_book": None
            if book is None
            else {
                "retrieved_at": book.get("retrieved_at"),
                "yes_bid": data.get("yes_bid_dollars"),
                "yes_ask": data.get("yes_ask_dollars"),
                "no_bid": data.get("no_bid_dollars"),
                "no_ask": data.get("no_ask_dollars"),
                "flags": data.get("flags", []),
            },
        }
    times = [
        o.get("retrieved_at") for o in evidence.get("observations", []) if o.get("retrieved_at")
    ]
    return {
        "kind": SUMMARY_KIND,
        "version": 1,
        "game_id": evidence["game_id"],
        "event_ticker": evidence.get("event_ticker"),
        "evidence_sha256": evidence.get("self_sha256"),
        "cutoff": evidence.get("cutoff"),
        "scheduled_start": evidence.get("scheduled_start"),
        "source_time": evidence.get("source_time"),
        "label": evidence.get("label"),
        "markets": markets,
        "span": {"first": min(times) if times else None, "last": max(times) if times else None},
        "note": DIAGNOSTIC,
    }


def _baselines(evidence):
    from ..forecast.baselines import InvalidBook, constant, midpoint

    books = _latest_books(evidence)
    out = {}
    for ticker in evidence.get("market_tickers", []):
        book = books.get(ticker)
        bids = ((book or {}).get("data") or {}).get("bids")
        try:
            # `midpoint` takes the raw orderbook_fp payload; the observation stores it as `bids`.
            mid = midpoint({"orderbook_fp": bids}) if book is not None else None
            entry = {
                "midpoint": None if mid is None else str(Decimal(mid)),
                "reason": None if mid is not None else "book_missing",
            }
        except InvalidBook as exc:
            entry = {"midpoint": None, "reason": str(exc.args[0]) if exc.args else "invalid_book"}
        entry["constant"] = str(constant())
        entry["book_retrieved_at"] = (book or {}).get("retrieved_at")
        out[ticker] = entry
    return {
        "kind": BASELINE_KIND,
        "version": 1,
        "game_id": evidence["game_id"],
        "evidence_sha256": evidence.get("self_sha256"),
        "baselines": out,
        "note": DIAGNOSTIC + "; the authoritative forecaster path is forecast.tick",
    }


@register_operation("cc_evidence_summary", GameEvidenceParams, roles=ROLES, inputs=evidence_input)
def evidence_summary(params: GameEvidenceParams, ctx):
    evidence = _load(ctx, params.evidence_sha256, EVIDENCE_KIND, params.game_id)
    evidence["self_sha256"] = params.evidence_sha256
    summary = _summary(evidence)
    ctx.emit(
        "evidence_summary.json",
        json.dumps(summary, sort_keys=True, indent=1).encode(),
        SUMMARY_KIND,
    )
    return {"status": "complete", "markets": len(summary["markets"]), "note": DIAGNOSTIC}


@register_operation("cc_baseline_analysis", GameEvidenceParams, roles=ROLES, inputs=evidence_input)
def baseline_analysis(params: GameEvidenceParams, ctx):
    evidence = _load(ctx, params.evidence_sha256, EVIDENCE_KIND, params.game_id)
    evidence["self_sha256"] = params.evidence_sha256
    report = _baselines(evidence)
    ctx.emit(
        "baseline_analysis.json",
        json.dumps(report, sort_keys=True, indent=1).encode(),
        BASELINE_KIND,
    )
    return {"status": "complete", "markets": len(report["baselines"]), "note": report["note"]}


@register_operation("cc_independent_check", CheckParams, roles=ROLES, inputs=check_inputs)
def independent_check(params: CheckParams, ctx):
    evidence = _load(ctx, params.evidence_sha256, EVIDENCE_KIND, params.game_id)
    evidence["self_sha256"] = params.evidence_sha256
    summary = _load(ctx, params.summary_sha256, SUMMARY_KIND, params.game_id)
    baseline = _load(ctx, params.baseline_sha256, BASELINE_KIND, params.game_id)
    own_summary, own_baseline = _summary(evidence), _baselines(evidence)
    disagreements, agreements = [], []
    for ticker in evidence.get("market_tickers", []):
        mine, theirs = (
            own_summary["markets"].get(ticker),
            (summary.get("markets") or {}).get(ticker),
        )
        (agreements if mine == theirs else disagreements).append(
            {"role": "evidence_inspector", "ticker": ticker, "expected": mine, "claimed": theirs}
        )
        mine, theirs = (
            own_baseline["baselines"].get(ticker),
            (baseline.get("baselines") or {}).get(ticker),
        )
        (agreements if mine == theirs else disagreements).append(
            {"role": "baseline_analyst", "ticker": ticker, "expected": mine, "claimed": theirs}
        )
    for name, doc in (("summary", summary), ("baseline", baseline)):
        if doc.get("evidence_sha256") != params.evidence_sha256:
            disagreements.append(
                {
                    "role": name,
                    "ticker": None,
                    "expected": params.evidence_sha256,
                    "claimed": doc.get("evidence_sha256"),
                }
            )
    report = {
        "kind": CHECK_KIND,
        "version": 1,
        "game_id": params.game_id,
        "evidence_sha256": params.evidence_sha256,
        "checked": {
            "summary_sha256": params.summary_sha256,
            "baseline_sha256": params.baseline_sha256,
        },
        "verdict": "consistent" if not disagreements else "inconsistent",
        "agreements": len(agreements),
        "disagreements": disagreements,
        "note": DIAGNOSTIC
        + "; a consistent check verifies internal agreement, not forecast quality",
    }
    ctx.emit("check_report.json", json.dumps(report, sort_keys=True, indent=1).encode(), CHECK_KIND)
    return {"status": "complete", "verdict": report["verdict"], "disagreements": len(disagreements)}
