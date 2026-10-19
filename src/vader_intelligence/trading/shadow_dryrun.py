"""Live-adapter dry run from forward-shadow decisions (roadmap H).

For each contract-4 decision in an evidence archive that carries a probability from one
registered forecaster, the declarative strategy proposes an order against the latest
archived book at or before the decision's cutoff; the risk engine approves and sizes it;
the executor submits it to a `DryRunGateway`, which builds the exact live request (and,
with a throwaway key, signs it) and sends nothing. The ledger is a paper ledger, so the
run can never touch a live ledger.

What this demonstrates: the path from a forward-shadow forecast to a well-formed Kalshi
order request, sized by the paper risk engine under the labelled simulation limits
(`risk.simulation_defaults` for the given cash, clipped to them) and checked by the live
adapter's market rules and request construction, plus the live gateway's notional caps
when they are given. Each dry order is canceled right after it is recorded (a canary
cancels too), so no phantom exposure distorts later decisions.

What it does not check: the canary's recorded risk limits, the activation gates (kill
switch disengage, startup reconciliation, live order prefix, credential), exchange
acceptance, fills or profit. The archive is opened read-only, no network transport
exists on this path, and the ledger must be a new paper ledger.
"""

import json
from datetime import datetime

from .executor import Executor
from .fees import DEFAULT_FEE_SCHEDULE
from .kalshi.dryrun import DryRunGateway
from .kalshi.gateway import MarketRule
from .kalshi.mapping import price_ranges
from .ledger import Ledger
from .marketdata import read_archive_markets, read_archive_snapshots
from .money import dec, plain, text
from .risk import ForecastStatus, RiskEngine, simulation_defaults
from .simulator import ForecastInput, StrategyConfig, propose

SERIES = "KXMLBGAME"
LINEAR_CENT = ({"start": "0.0000", "end": "1.0000", "step": "0.0100"},)
NOTICE = (
    "Dry run: requests were constructed (and signed only with a throwaway key) and never "
    "sent. Not evidence of acceptance, fills or profit."
)


def decisions(db, forecaster_id):
    """(decision id, ticker, cutoff, probability) for decisions that scored the forecaster."""
    out = []
    rows = db.execute(
        "SELECT id, payload_json FROM forecast_decisions "
        "WHERE json_extract(payload_json, '$.contract_version') = 4 ORDER BY id"
    )
    for decision_id, payload_json in rows:
        payload = json.loads(payload_json)
        result = payload.get("results", {}).get(forecaster_id)
        if not result or result.get("probability") is None or not payload.get("cutoff"):
            continue
        out.append(
            (decision_id, payload["ticker"], payload["cutoff"], result["probability"], payload)
        )
    return out


def run(
    db,
    *,
    forecaster_id,
    ledger_path,
    kalshi_environment="demo",
    cash="1000",
    strategy=None,
    fee_schedule=DEFAULT_FEE_SCHEDULE,
    signer=None,
    clip_to_limits=True,
    max_order_notional=None,
    max_daily_notional=None,
):
    """Dry-run every scored decision; returns the would-be requests and every refusal.

    Sizes are clipped to the risk limits by default (a canary sizes down; it does not
    refuse a correctly signed small order because Kelly asked for more).
    """
    strategy = strategy or StrategyConfig()
    found = decisions(db, forecaster_id)
    tickers = sorted({ticker for _, ticker, _, _, _ in found})
    markets = read_archive_markets(db, tickers) if tickers else {}
    books = read_archive_snapshots(db, tickers) if tickers else None
    by_ticker = {}
    for snap in books.snapshots if books else ():
        by_ticker.setdefault(snap.ticker, []).append(snap)
    cash = dec(cash, "cash")
    rule = MarketRule(SERIES, price_ranges(LINEAR_CENT), exchange_index=3)
    gateway = DryRunGateway(
        kalshi_environment,
        markets=(rule,),
        cash=cash,
        signer=signer,
        max_order_notional=max_order_notional,
        max_daily_notional=max_daily_notional,
    )
    ledger = Ledger(ledger_path, "paper")
    outcomes = []
    try:
        if ledger.intents() or ledger.cash_events():
            raise ValueError("a dry run needs a new paper ledger (this one has records)")
        if not ledger.cash_events():
            from .portfolio import CashEvent

            first = min((datetime.fromisoformat(c) for _, _, c, _, _ in found), default=None)
            if first is not None:
                with ledger.transaction("setup"):
                    ledger.append_cash_event(
                        CashEvent("dryrun:deposit", "deposit", cash, first, "dry-run bankroll")
                    )
                    ledger.record_limits(simulation_defaults(cash), "dry-run", first)
        engine = RiskEngine("paper", clip_to_limits=clip_to_limits)
        executor = Executor(ledger, gateway, engine, fee_schedule)
        for decision_id, ticker, cutoff, probability, payload in found:
            at = datetime.fromisoformat(cutoff)
            market = markets.get(ticker)
            history = [s for s in by_ticker.get(ticker, []) if s.as_of <= at]
            snapshot = history[-1] if history else None
            record = {"decision_id": decision_id, "ticker": ticker, "cutoff": cutoff}
            if market is None:
                outcomes.append(record | {"outcome": "no_market_metadata"})
                continue
            forecast = ForecastInput(
                forecast_id=f"dryrun:{decision_id}",
                ticker=ticker,
                probability=dec(probability, "probability"),
                computed_at=at,
                model_id=forecaster_id,
                artifact_hash=next(
                    (
                        f["artifact_sha256"]
                        for f in payload.get("forecasters", [])
                        if f["id"] == forecaster_id
                    ),
                    "",
                ),
            )
            intent, reason = propose(strategy, forecast, market, snapshot, fee_schedule)
            if intent is None:
                outcomes.append(record | {"outcome": "no_intent", "reason": reason})
                continue
            status = ForecastStatus(
                ticker, forecast.probability, at, forecaster_id, True, forecast.standard_error
            )
            placement = executor.place(
                intent,
                now=at,
                market=market,
                snapshot=snapshot,
                forecast=status,
                marks={ticker: snapshot},
                markets=markets,
                equity_history=[],
                input_key=f"dryrun:{decision_id}",
                checkpoint={"intent": intent.intent_id},
            )
            submit = placement.submit
            submitted = submit is not None and submit.status == "submitted"
            canceled = None
            if submitted:  # release the reservation, as a canary would cancel
                result = executor.cancel(placement.order.client_order_id, now=at)
                canceled = None if result is None else result.status
            approval = placement.decision if hasattr(placement.decision, "quantity") else None
            outcomes.append(
                record
                | {
                    "outcome": "submitted_dry" if submitted else "refused",
                    "decision": type(placement.decision).__name__,
                    "risk_reasons": list(getattr(placement.decision, "reasons", ())),
                    "approved_quantity": None if approval is None else text(approval.quantity),
                    "sizing": None if approval is None else plain(approval.sizing),
                    "canceled_after_dry_run": canceled,
                    "submit_status": None if submit is None else submit.status,
                    "submit_reason": None if submit is None else submit.reason,
                    "side": intent.side,
                    "limit_price": text(intent.limit_price),
                    "book_as_of": snapshot.as_of.isoformat() if snapshot else None,
                }
            )
        return {
            "kind": "live-adapter-dry-run",
            "notice": NOTICE,
            "kalshi_environment": kalshi_environment,
            "signed": signer is not None,
            "forecaster_id": forecaster_id,
            "decisions": len(found),
            "outcomes": outcomes,
            "requests": gateway.records(),
            "refusals": [{"client_order_id": c, "reason": r} for c, r in gateway.refusals],
            "archive_skipped": {} if books is None else books.skipped,
            "checks": {
                "risk_limits": "simulation_defaults(cash) (labelled simulation assumption)",
                "clip_to_limits": clip_to_limits,
                "max_order_notional": None
                if max_order_notional is None
                else str(max_order_notional),
                "max_daily_notional": None
                if max_daily_notional is None
                else str(max_daily_notional),
                "not_checked": [
                    "canary risk limits",
                    "activation gates",
                    "exchange acceptance",
                ],
            },
            "ledger_verification": ledger.verify(),
        }
    finally:
        ledger.close()
