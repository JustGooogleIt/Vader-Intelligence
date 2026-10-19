"""The executor: the only caller of an exchange gateway.

Durable order of operations for a placement:

1. one transaction records the intent, the risk decision (approval or rejection), and,
   for an approval, the order and its `submit_requested` event (the reservation starts
   here) — strictly before the gateway is called;
2. `gateway.submit` runs outside any transaction; an exception or timeout is recorded
   as `submit_unknown`, never retried;
3. a second transaction records the gateway's answer.

A crash between 1 and 3 leaves a `pending_submit` order without an outcome; like an
`unknown` one it blocks risk-increasing orders until `reconcile()` has looked the order
up by client order id at the venue. A new executor (a restart) reconciles before its
first submission. An intent that already has a decision is never re-evaluated and its
order is never resubmitted.
"""

from dataclasses import dataclass, replace

from .gateway import CancelResult, GatewayEvent, SubmitResult, UnreadableSettlement
from .ledger import (
    CHECKPOINT_PREFIX,
    EnvironmentMismatch,
    LedgerConflict,
    LedgerError,
    build_order,
)
from .money import ONE, ZERO, dec, digest, in_exact, text
from .orders import GATEWAY_SOURCE, Fill, OrderEvent
from .portfolio import CashEvent, Settlement
from .risk import Approval, Rejection, RiskContext

MAX_FILL_PAGES = 1000
FILLS_CURSOR = CHECKPOINT_PREFIX + "reconcile:fills_cursor"
SETTLEMENTS_CURSOR = CHECKPOINT_PREFIX + "reconcile:settlements_cursor"
# Insert-once processed key: an operator's acknowledgement that a venue settlement record
# (named by `settlement_ack_key`) is resolved outside the executor and may be skipped.
SETTLEMENT_ACK_PREFIX = "settlement_ack:"
_VENUE_TERMINAL = ("canceled", "expired", "rejected", "not_found")


def settlement_ack_key(token):
    """Processed-input key by which an operator acknowledges a venue settlement record
    (a ticker, or the token of an unreadable record) as resolved outside the executor:
    `ledger.mark_processed(settlement_ack_key(token), {...}, at)`."""
    return SETTLEMENT_ACK_PREFIX + token


@dataclass(frozen=True)
class Placement:
    decision: object
    order: object
    submit: SubmitResult | None
    existing: bool


@dataclass(frozen=True)
class ReconcileReport:
    reconciled: tuple
    unresolved: tuple

    @property
    def clean(self):
        return not self.unresolved


def _resolve(checkpoint):
    return checkpoint() if callable(checkpoint) else checkpoint


class Executor:
    def __init__(self, ledger, gateway, engine, fee_schedule):
        env = ledger.environment
        if gateway.environment != env or engine.environment != env:
            raise EnvironmentMismatch("ledger, gateway and risk engine environments differ")
        self.ledger, self.gateway, self.engine = ledger, gateway, engine
        self.fee_schedule = fee_schedule
        self.source = GATEWAY_SOURCE[env]
        self.unknown_reasons = ()
        self.needs_reconcile = True

    def context(
        self, *, now, market, snapshot, forecast, marks=None, markets=None, equity_history=()
    ):
        limits, version = self.ledger.current_limits()
        return RiskContext(
            environment=self.ledger.environment,
            now=now,
            projection=self.ledger.projection(),
            limits=limits,
            limits_version=version,
            kill_switch_engaged=self.ledger.kill_switch().engaged,
            fee_schedule=self.fee_schedule,
            market=market,
            snapshot=snapshot,
            forecast=forecast,
            marks=dict(marks or {}),
            markets=dict(markets or {}),
            equity_history=tuple(equity_history),
            unknown_state=self.unknown_reasons,
        )

    def place(
        self,
        intent,
        *,
        now,
        market,
        snapshot,
        forecast,
        marks=None,
        markets=None,
        equity_history=(),
        input_key=None,
        checkpoint=None,
    ):
        """Evaluate, record and (if approved) submit one intent. See module docstring."""
        if self.ledger.in_transaction:
            # submit_requested must be durable before the gateway call.
            raise LedgerError("place() must not run inside an open ledger transaction")
        existing = self.ledger.decision_for(intent.intent_id)
        if existing is not None:
            order = None
            if isinstance(existing, Approval):
                order = self.ledger.order_for_approval(existing.approval_id)
            if input_key is not None:
                self.ledger.mark_processed(input_key, _resolve(checkpoint), now)
            return Placement(existing, order, None, True)
        if self.needs_reconcile or self.ledger.projection().unknown_flags():
            self.reconcile(now=now)
        ctx = self.context(
            now=now,
            market=market,
            snapshot=snapshot,
            forecast=forecast,
            marks=marks,
            markets=markets,
            equity_history=equity_history,
        )
        decision = self.engine.evaluate(intent, ctx)
        order = None
        with self.ledger.transaction("decision"):
            self.ledger.record_intent(intent)
            self.ledger.record_decision(decision)
            if isinstance(decision, Rejection) and decision.engage_kill_switch:
                if not ctx.kill_switch_engaged:
                    stops = ",".join(r for r in decision.reasons if r.endswith("_stop"))
                    self.ledger.set_kill_switch(True, "auto: " + stops, "risk-engine", now)
            if isinstance(decision, Approval):
                order = build_order(self.ledger.environment, decision, intent, now)
                self.ledger.create_order(order)
                coid = order.client_order_id
                self.ledger.append_event(
                    OrderEvent(f"{coid}:submit_requested", coid, "submit_requested", "local", now)
                )
            if input_key is not None:
                self.ledger.mark_processed(input_key, _resolve(checkpoint), now)
        if order is None:
            return Placement(decision, None, None, False)
        return Placement(decision, order, self._submit(order, now), False)

    def _submit(self, order, now):
        self.ledger.fault_point("submit:before")
        try:
            result = self.gateway.submit(order)
        except Exception as exc:  # outcome unknown: never assume, never retry
            result = SubmitResult("unknown", reason=f"{type(exc).__name__}: {str(exc)[:200]}")
        self.ledger.fault_point("submit:after")
        coid = order.client_order_id
        if result.status == "unknown":
            self.needs_reconcile = True
            event = OrderEvent(
                f"{coid}:submit_unknown",
                coid,
                "submit_unknown",
                "local",
                now,
                payload={"reason": result.reason or "unknown"},
            )
        else:
            event = OrderEvent(
                f"{coid}:submit:{result.status}",
                coid,
                result.status,
                self.source,
                now,
                exchange_order_id=result.exchange_order_id,
                sequence=result.sequence,
                exchange_ts=result.exchange_ts,
                payload={"reason": result.reason} if result.reason else {},
            )
        self.ledger.append_event(event)
        return result

    def cancel(self, client_order_id, *, now):
        """Request a cancel. Returns None when the order is already terminal."""
        state = self.ledger.projection().orders.get(client_order_id)
        if state is None:
            raise LedgerError(f"unknown order {client_order_id}")
        if not state.active:
            return None
        n = sum(1 for e in self.ledger.events(client_order_id) if e.kind == "cancel_requested")
        with self.ledger.transaction("cancel"):
            self.ledger.append_event(
                OrderEvent(
                    f"{client_order_id}:cancel_requested:{n + 1}",
                    client_order_id,
                    "cancel_requested",
                    "local",
                    now,
                )
            )
        try:
            result = self.gateway.cancel(client_order_id)
        except Exception as exc:  # cancel outcome unknown; the reservation stays held
            self.needs_reconcile = True
            return CancelResult("pending", reason=f"{type(exc).__name__}")
        if result.status == "canceled":
            self.ledger.append_event(
                OrderEvent(
                    f"{client_order_id}:cancel:{n + 1}",
                    client_order_id,
                    "canceled",
                    self.source,
                    now,
                    sequence=result.sequence,
                    exchange_ts=result.exchange_ts,
                    payload={"reason": result.reason or "cancel_requested"},
                )
            )
        return result

    def ingest(self, items=(), *, now, settlement=None, input_key=None, checkpoint=None):
        """Record gateway events and fills (and optionally a settlement) atomically.

        Duplicates are idempotent (a gateway event resent later is the same event: its
        receipt time is not part of its identity); a contradiction raises
        `LedgerConflict`. Any failure records nothing from the batch and flags the state
        unknown until reconciliation.
        """
        try:
            with self.ledger.transaction("ingest"):
                if settlement is not None:
                    self.ledger.append_settlement(settlement)
                for item in items:
                    if isinstance(item, Fill):
                        self.ledger.append_fill(item)
                    elif isinstance(item, GatewayEvent):
                        self._append_gateway_event(self._event(item, now))
                    else:
                        raise TypeError("ingest accepts Fill and GatewayEvent items")
                if input_key is not None:
                    self.ledger.mark_processed(input_key, _resolve(checkpoint), now)
        except (LedgerConflict, EnvironmentMismatch, ValueError, TypeError):
            self.unknown_reasons = tuple(sorted(set(self.unknown_reasons) | {"ingest_conflict"}))
            self.needs_reconcile = True
            raise

    def _append_gateway_event(self, event):
        """Append a gateway event; an identical earlier copy (receipt time aside) is a
        no-op, a different one with the same id is a contradiction."""
        prior = next(
            (e for e in self.ledger.events(event.client_order_id) if e.event_id == event.event_id),
            None,
        )
        if prior is None:
            self.ledger.append_event(event)
        elif replace(prior, received_at=event.received_at) != event:
            raise LedgerConflict(f"event {event.event_id} resent with different content")

    def _event(self, item, now):
        return OrderEvent(
            item.event_id,
            item.client_order_id,
            item.kind,
            self.source,
            now,
            exchange_order_id=item.exchange_order_id,
            sequence=item.sequence,
            exchange_ts=item.exchange_ts,
            payload=item.payload,
        )

    def reconcile(self, *, now):
        """Resolve submissions, fetch missed fills, record venue settlements (gateways
        that report them), compare positions and balance.

        Mismatches and venue failures become `unknown_reasons`, which block
        risk-increasing orders until a later reconciliation is clean.
        """
        ledger, reasons, reconciled, gaps = self.ledger, [], [], {}
        for coid, state in sorted(ledger.projection().orders.items()):
            if not state.active:
                continue
            if state.status == "new":
                with ledger.transaction("reconcile"):
                    ledger.append_event(
                        OrderEvent(
                            f"{coid}:never_submitted",
                            coid,
                            "canceled",
                            "local",
                            now,
                            payload={"reason": "never_submitted"},
                        )
                    )
                reconciled.append(coid)
                continue
            unresolved = state.status == "pending_submit" and not state.submit_outcome
            try:
                venue = self.gateway.lookup(coid)
            except Exception:
                if unresolved:
                    ledger.append_event(self._unknown(coid, now))
                reasons.append(f"lookup_failed:{coid}")
                continue
            status = venue.status if venue is not None else "not_found"
            filled = venue.filled_quantity if venue is not None else ZERO
            changed = status in _VENUE_TERMINAL or (
                status == "open" and state.status == "pending_submit"
            )
            if state.status == "unknown" or unresolved or changed:
                payload = {"status": status, "filled_quantity": text(filled)}
                with ledger.transaction("reconcile"):
                    if unresolved:
                        ledger.append_event(self._unknown(coid, now))
                    ledger.append_event(
                        OrderEvent(
                            "reconciled:" + digest({"order": coid, "at": now, **payload}),
                            coid,
                            "reconciled",
                            self.source,
                            now,
                            exchange_order_id=venue.exchange_order_id if venue else None,
                            payload=payload,
                        )
                    )
                reconciled.append(coid)
            if filled > state.filled_quantity:
                gaps[coid] = filled
        fill_reasons = self._reconcile_fills(now)
        reasons.extend(fill_reasons)
        reasons.extend(self._reconcile_settlements(now, deferred=bool(fill_reasons)))
        projection = ledger.projection()
        for coid, venue_filled in sorted(gaps.items()):
            if projection.orders[coid].filled_quantity < venue_filled:
                reasons.append(f"fills_missing:{coid}")
        reasons.extend(self._reconcile_positions(projection.portfolio))
        reasons.extend(self._reconcile_balance(projection.portfolio, now))
        self.unknown_reasons = tuple(sorted(set(reasons)))
        self.needs_reconcile = bool(self.unknown_reasons) or bool(
            ledger.projection().unknown_flags()
        )
        return ReconcileReport(tuple(reconciled), self.unknown_reasons)

    @staticmethod
    def _unknown(coid, now):
        return OrderEvent(
            f"{coid}:submit_unknown",
            coid,
            "submit_unknown",
            "local",
            now,
            payload={"reason": "no_submit_outcome_recorded"},
        )

    def _reconcile_fills(self, now):
        cursor_data = self.ledger.processed(FILLS_CURSOR)
        cursor = cursor_data.get("cursor") if cursor_data else None
        for _ in range(MAX_FILL_PAGES):
            try:
                fills, next_cursor = self.gateway.fills_since(cursor)
            except Exception:
                return ["fills_unavailable"]
            for fill in fills:
                try:
                    self.ledger.append_fill(fill)
                except LedgerConflict:
                    return [f"fill_conflict:{fill.fill_id}"]
            if next_cursor != cursor:
                self.ledger.set_checkpoint(FILLS_CURSOR, {"cursor": next_cursor}, now)
            if not fills or next_cursor == cursor:
                return []
            cursor = next_cursor
        return ["fills_pagination_exceeded"]

    def _reconcile_settlements(self, now, *, deferred=False):
        """Record the venue's settlements of this ledger's markets.

        Runs only after a clean fills reconciliation (otherwise `settlements_deferred`).
        A settlement is recorded only when it agrees exactly with the ledger: the market
        has ledger orders, none is still active, the ledger holds at least every fill the
        venue reported for each of them (`reconciled` events), the YES and NO counts it
        settled equal the ledger's holding, the cash it paid equals that holding's payout,
        and it charged no fee (the ledger models none). A market already settled in the
        ledger (by the executor or an operator's correction) only has to agree on result
        and value. Otherwise nothing is recorded, the reason blocks risk-increasing
        orders, and the cursor is kept so the next reconciliation reads the record again;
        an operator either records the settlement through the ledger or acknowledges the
        record (`settlement_ack_key`) after investigating.

        A fill arriving after its market's settlement was recorded contradicts the venue's
        own final fill count for its order or the counts it settled, and stays refused by
        the ledger.
        """
        read = getattr(self.gateway, "settlements_since", None)
        if read is None:
            return []
        if deferred:
            return ["settlements_deferred"]
        data = self.ledger.processed(SETTLEMENTS_CURSOR)
        cursor = data.get("cursor") if data else None
        try:
            venue, next_cursor = read(cursor)
        except Exception:
            return ["settlements_unavailable"]
        reasons = []
        for item in venue:
            unreadable = isinstance(item, UnreadableSettlement)
            token = item.token if unreadable else item.ticker
            if self.ledger.processed(settlement_ack_key(token)) is not None:
                continue
            reason = "settlement_unreadable" if unreadable else self._settle(item)
            if reason is not None:
                reasons.append(f"{reason}:{token}")
        if not reasons and next_cursor != cursor:
            self.ledger.set_checkpoint(SETTLEMENTS_CURSOR, {"cursor": next_cursor}, now)
        return reasons

    def _venue_filled(self, client_order_id):
        """The largest fill count the venue reported for an order in `reconciled` events
        (zero when there are none); None when such an event lacks it."""
        reported = ZERO
        for event in self.ledger.events(client_order_id):
            if event.kind == "reconciled":
                value = event.payload.get("filled_quantity")
                if value is None:
                    return None
                reported = max(reported, dec(value, "filled_quantity"))
        return reported

    @in_exact
    def _settle(self, item):
        projection = self.ledger.projection()
        recorded = projection.portfolio.settlements.get(item.ticker)
        if recorded is not None:
            agrees = (recorded.result, recorded.settlement_value) == (
                item.result,
                item.settlement_value,
            )
            return None if agrees else "settlement_conflict"
        states = [s for s in projection.orders.values() if s.order.ticker == item.ticker]
        if not states:
            return "settlement_foreign"
        if any(s.active for s in states):
            return "settlement_orders_active"
        for state in states:
            # Canceled at the venue (e.g. at close) is terminal before its fills arrive.
            reported = self._venue_filled(state.order.client_order_id)
            if reported is None or state.filled_quantity < reported:
                return "settlement_fills_missing"
        if item.fee != ZERO:
            return "settlement_fee"
        held = projection.portfolio.holding(item.ticker)
        value = item.settlement_value
        payout = held.yes_qty * value + held.no_qty * (ONE - value)
        if (item.yes_count, item.no_count, item.revenue) != (held.yes_qty, held.no_qty, payout):
            return "settlement_mismatch"
        settlement = Settlement(
            # A venue settles each market of an account once.
            settlement_id="kalshi:" + item.ticker,
            ticker=item.ticker,
            result=item.result,
            settlement_value=item.settlement_value,
            source_ref=item.source_ref,
            effective_at=item.settled_at,
        )
        try:
            self.ledger.append_settlement(settlement)
        except LedgerConflict:
            return "settlement_conflict"
        return None

    def _reconcile_positions(self, portfolio):
        try:
            venue = self.gateway.positions()
        except Exception:
            return ["positions_unavailable"]
        out = []
        for ticker in sorted(set(venue) | set(portfolio.holdings)):
            theirs = venue.get(ticker)
            ours = portfolio.holding(ticker)
            quantities = (theirs.yes_qty, theirs.no_qty) if theirs is not None else (ZERO, ZERO)
            if quantities != (ours.yes_qty, ours.no_qty):
                out.append(f"position_mismatch:{ticker}")
        return out

    def _reconcile_balance(self, portfolio, now):
        try:
            balance = self.gateway.balance()
        except Exception:
            return ["balance_unavailable"]
        if balance is None:
            return ["balance_unknown"]
        balance = dec(balance, "balance")
        if self.ledger.environment == "live":
            event_id = "balance:" + digest({"amount": balance, "at": now})
            self.ledger.append_cash_event(CashEvent(event_id, "balance_observed", balance, now))
        return [] if balance == portfolio.cash else ["balance_mismatch"]
