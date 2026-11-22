"""Paper-run and sensitivity reports: deterministic machine JSON and concise text.

Every report carries a manifest (input digests, config digest, fee schedule version and
verification flag, assumptions) and states that simulated results are not live results.
Reports are built from ledger projections, never from in-memory run state.
"""

import json
from collections import Counter
from dataclasses import dataclass

from .money import plain, text

NOTICE = "Simulated paper-trading results under stated assumptions; these are not live results."
_INPUT_PREFIXES = ("snapshot:", "forecast:", "arrive:", "settlement:")


def _json(value):
    return (
        json.dumps(plain(value), sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False)
        + "\n"
    )


@dataclass(frozen=True)
class PaperRunReport:
    manifest: dict
    pnl: dict
    orders: list
    fills: list
    exposure_history: list
    rejections_by_reason: dict
    forecasts_without_intent: dict
    unidentifiable: int
    open_markets: list
    settlements: list
    verification: list

    def to_dict(self):
        return plain(self)

    def to_json(self):
        return _json(self)

    def to_text(self):
        p, m = self.pnl, self.manifest
        fee = m["fee_schedule"]
        filled = sum(1 for o in self.orders if o["status"] == "filled")
        lines = [
            f"PAPER RUN (simulated; not live results) inputs={m['input_label']}",
            f"fee schedule {fee['version']} verified={fee['verified']}; seed {m['seed']}; "
            f"config {m['config_digest'][:12]}",
            f"orders {len(self.orders)} (filled {filled}), fills {len(self.fills)}, "
            f"unidentifiable {self.unidentifiable}",
            f"cash {p['cash']} realized {p['realized']} fees {p['fees']}",
            f"equity conservative {p['equity_conservative']} (unrealized "
            f"{p['unrealized_conservative']}); mid {p['equity_mid']} "
            f"(unrealized {p['unrealized_mid']})",
            "rejections: "
            + (
                ", ".join(f"{k}={v}" for k, v in sorted(self.rejections_by_reason.items()))
                or "none"
            ),
            "open markets: " + (", ".join(o["ticker"] for o in self.open_markets) or "none"),
            "verification: " + ("ok" if not self.verification else "; ".join(self.verification)),
        ]
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class SensitivityReport:
    manifest: dict
    rows: list

    def to_dict(self):
        return plain(self)

    def to_json(self):
        return _json(self)

    def to_text(self):
        lines = [
            f"PAPER SENSITIVITY (simulated; not live results) inputs="
            f"{self.manifest['input_label']} scenarios={len(self.rows)}"
        ]
        for row in self.rows:
            s = row["scenario"]
            lines.append(
                f"{s['fee_schedule']} slip={s['extra_slippage_ticks']} "
                f"maker={s['maker_fill_model']} latency={s['latency']['submit_ms']}: "
                f"equity {row['pnl']['equity_conservative']} fills {row['fills']} "
                f"unidentifiable {row['unidentifiable']}"
            )
        return "\n".join(lines) + "\n"


def paper_report(ledger, *, manifest, marks):
    """Build a `PaperRunReport` from the ledger's projection and checkpoints."""
    projection = ledger.projection()
    port = projection.portfolio
    states = sorted(
        projection.orders.values(), key=lambda s: (s.order.created_at, s.order.client_order_id)
    )
    orders = [
        {
            "client_order_id": s.order.client_order_id,
            "ticker": s.order.ticker,
            "side": s.order.side,
            "action": s.order.action,
            "quantity": text(s.order.quantity),
            "limit_price": text(s.order.limit_price),
            "time_in_force": s.order.time_in_force,
            "created_at": s.order.created_at.isoformat(),
            "status": s.status,
            "filled_quantity": text(s.filled_quantity),
            "reason": s.reason,
        }
        for s in states
    ]
    conservative, missing = port.market_value(marks, "conservative")
    mid, missing_mid = port.market_value(marks, "mid")
    pnl = {
        "cash": text(port.cash),
        "net_deposits": text(port.net_deposits),
        "realized": text(port.realized),
        "fees": text(port.totals.fees),
        "reserved_cash": text(port.reserved_cash),
        "unrealized_conservative": text(conservative - port.remaining_cost),
        "unrealized_mid": text(mid - port.remaining_cost),
        "equity_conservative": text(port.cash + conservative),
        "equity_mid": text(port.cash + mid),
        "marks_missing": sorted(set(missing) | set(missing_mid)),
    }
    history = []
    processed = ledger.processed_inputs()
    for key, record in processed.items():
        data = record["data"]
        if key.startswith(_INPUT_PREFIXES) and isinstance(data, dict) and "equity" in data:
            history.append({"at": record["at"], "input": key, **data})
    history.sort(key=lambda h: (h["at"], h["input"]))
    no_intent = Counter(
        record["data"]["reason"]
        for key, record in processed.items()
        if key.startswith("forecast:")
        and isinstance(record["data"], dict)
        and record["data"].get("intent", "") is None
    )
    rejections = Counter(r for rejection in ledger.rejections() for r in rejection.reasons)
    unidentifiable = sum(
        1
        for e in ledger.events()
        if e.kind == "rejected" and e.payload.get("reason") == "execution_unidentifiable"
    )
    open_markets = []
    for ticker, h in sorted(port.holdings.items()):
        snap = marks.get(ticker)
        open_markets.append(
            {
                "ticker": ticker,
                "yes_qty": text(h.yes_qty),
                "no_qty": text(h.no_qty),
                "cost": text(h.yes_cost + h.no_cost),
                "best_yes_bid": text(snap.best_yes_bid)
                if snap and snap.best_yes_bid is not None
                else None,
                "best_no_bid": text(snap.best_no_bid)
                if snap and snap.best_no_bid is not None
                else None,
                "mark_as_of": snap.as_of.isoformat() if snap else None,
            }
        )
    return PaperRunReport(
        manifest=manifest,
        pnl=pnl,
        orders=orders,
        fills=[f.to_dict() for f in ledger.fills()],
        exposure_history=history,
        rejections_by_reason=dict(sorted(rejections.items())),
        forecasts_without_intent=dict(sorted(no_intent.items())),
        unidentifiable=unidentifiable,
        open_markets=open_markets,
        settlements=[
            s.to_dict() for s in sorted(port.settlements.values(), key=lambda s: s.ticker)
        ],
        verification=ledger.verify(),
    )
