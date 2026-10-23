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
up by client order id at the venue (one negative answer is not final for such an order:
see `Executor.reconcile`). A new executor (a restart) reconciles before its first
submission. An intent that already has a decision is never re-evaluated and its order is
never resubmitted.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta

from .gateway import (
    CancelResult,
    GatewayEvent,
    SubmitResult,
    UnreadableSettlement,
    settlement_fingerprint,
)
from .ledger import (
    CHECKPOINT_PREFIX,
    EnvironmentMismatch,
    LedgerConflict,
    LedgerError,
    build_order,
)
from .money import ONE, ZERO, dec, digest, in_exact, text, ts_text
from .orders import ABSENT, GATEWAY_SOURCE, Fill, OrderEvent
from .portfolio import CashEvent, Settlement
from .risk import Approval, Rejection, RiskContext

MAX_FILL_PAGES = 1000
FILLS_CURSOR = CHECKPOINT_PREFIX + "reconcile:fills_cursor"
SETTLEMENTS_CURSOR = CHECKPOINT_PREFIX + "reconcile:settlements_cursor"
# Insert-once processed keys: an operator's acknowledgement that venue settlement records
# (named by `settlement_ack_key`) are resolved outside the executor and may be skipped.
SETTLEMENT_ACK_PREFIX = "settlement_ack:"
log = logging.getLogger(__name__)
_VENUE_TERMINAL = ("canceled", "expired", "rejected", "not_found")
# A submission whose outcome was never known is `not_found` only on a negative venue read
# at least this long after the first one (REST reads lag; a create may still be in
# flight), with a clean open-orders sweep in the same reconciliation.
ABSENCE_GRACE_SECONDS = 300


def settlement_ack_key(token, evidence=None):
    """Processed-input key of an operator's acknowledgement of the venue settlement
    records named `token` (a ticker, or the token of an unreadable record). With
    `evidence` (the fingerprints of the records resolved) the key also names that
    evidence, so a token can carry several acknowledgements, each scoped to what the
    operator saw. Without it the key is the legacy form (`settlement_ack:<token>`), which
    the executor no longer treats as covering any record."""
    if evidence is None:
        return SETTLEMENT_ACK_PREFIX + token
    return f"{SETTLEMENT_ACK_PREFIX}{token}:{digest(list(_evidence(evidence)))[:16]}"


def _evidence(evidence):
    """Validated, sorted, distinct settlement fingerprints (a mapping's keys count)."""
    if isinstance(evidence, Mapping):
        evidence = evidence.keys()
    if isinstance(evidence, (str, bytes)) or evidence is None:
        raise ValueError("settlement evidence is a collection of fingerprints")
    items = tuple(sorted(set(evidence)))
    if not items or not all(isinstance(v, str) and v.strip() for v in items):
        raise ValueError("an acknowledgement needs the fingerprints of the records it resolves")
    return items


def acknowledge_settlement(ledger, token, *, actor, note, evidence, at):
    """Record an operator's acknowledgement (insert-once, auditable) that the venue
    settlement records named `token` whose fingerprints are `evidence` (from
    `settlement_evidence`) are resolved outside the executor, which then skips exactly
    those records. A record the venue lists later, or lists with changed content, is not
    covered: it is reconciled like any other and reported until the operator resolves it
    on its own evidence. Positions and balance are still reconciled. Repeating the same
    acknowledgement is a no-op; a different note for the same evidence is refused by
    the ledger (`LedgerConflict`)."""
    if not all(isinstance(v, str) and v.strip() for v in (token, actor, note)):
        raise ValueError("an acknowledgement needs a token, an actor and a note")
    items = _evidence(evidence)
    record = {"actor": actor, "note": note, "token": token, "evidence": list(items)}
    return ledger.mark_processed(settlement_ack_key(token, items), record, at)


def settlement_evidence(gateway, ledger):
    """The venue settlement records the executor reads next, by token:
    `{token: {fingerprint: summary}}`. An operator inspects these, then acknowledges a
    token with the fingerprints (or the whole inner mapping) of the records resolved.
    Reads the venue from the ledger's settlement cursor; records nothing."""
    read = getattr(gateway, "settlements_since", None)
    if read is None:
        return {}
    data = ledger.processed(SETTLEMENTS_CURSOR)
    venue, _ = read(data.get("cursor") if data else None)
    out = {}
    for item in venue:
        if isinstance(item, UnreadableSettlement):
            summaries = {fp: {"error": item.error} for fp in item.fingerprints}
            for record in item.records:
                summaries[settlement_fingerprint(record)] = _settlement_summary(record)
            out[item.token] = summaries
        else:
            out[item.ticker] = {settlement_fingerprint(item): _settlement_summary(item)}
    return out


def _settlement_summary(record):
    return {
        "result": record.result,
        "settlement_value": text(record.settlement_value),
        "yes_count": text(record.yes_count),
        "no_count": text(record.no_count),
        "revenue": text(record.revenue),
        "fee": text(record.fee),
        "settled_at": ts_text(record.settled_at),
        "source_ref": record.source_ref,
    }


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
    def __init__(
        self,
        ledger,
        gateway,
        engine,
        fee_schedule,
        *,
        absence_grace_seconds=ABSENCE_GRACE_SECONDS,
    ):
        env = ledger.environment
        if gateway.environment != env or engine.environment != env:
            raise EnvironmentMismatch("ledger, gateway and risk engine environments differ")
        grace = absence_grace_seconds
        if isinstance(grace, bool) or not isinstance(grace, int) or grace <= 0:
            raise ValueError("absence_grace_seconds must be a positive integer")
        self.ledger, self.gateway, self.engine = ledger, gateway, engine
        self.fee_schedule = fee_schedule
        self.absence_grace = timedelta(seconds=grace)
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
        """Request a cancel. Returns None when the order is already terminal, unless that
        terminal state came only from reconciliation (`not_found`, an absence the venue
        never confirmed): such an order can still surface and rest at the venue
        (`venue_open_untracked`), so the cancel is sent. A recorded outcome never reopens
        a terminal order."""
        state = self.ledger.projection().orders.get(client_order_id)
        if state is None:
            raise LedgerError(f"unknown order {client_order_id}")
        if not state.active and not self._ended_by_absence(state):
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

    def _ended_by_absence(self, state):
        """Terminal only through a reconciled `not_found`: no venue event ended it."""
        if state.status != "rejected" or state.venue_status != "not_found":
            return False
        ended = ("rejected", "canceled", "expired")
        return not any(e.kind in ended for e in self.ledger.events(state.order.client_order_id))

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
        that report them), compare open orders, positions and balance.

        One negative venue read never resolves a submission whose outcome was never known
        (`unknown`, or `pending_submit` without an outcome). A negative read (`lookup`
        returns None, and the open-orders sweep of the same reconciliation was read and
        does not list the order) is recorded once as an `absent` reconciliation: the
        order stays unknown and keeps its reservation. It becomes `not_found` (terminal)
        only on another negative read at least `absence_grace_seconds` after the first.
        Until then it is reported `absence_unconfirmed:<coid>`. A None for any other
        order is not taken as final while the sweep lists the order or failed. A
        venue-open order that is not an active ledger order is reported
        `venue_open_untracked:<client id>` (`Executor.cancel` can still cancel one that
        ended only by absence).

        Mismatches and venue failures become `unknown_reasons`, which block
        risk-increasing orders until a later reconciliation is clean.
        """
        ledger, reasons, reconciled, gaps = self.ledger, [], [], {}
        projection = ledger.projection()
        venue_open, sweep_reasons = self._sweep_open_orders(projection)
        reasons.extend(sweep_reasons)
        for coid, state in sorted(projection.orders.items()):
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
            payload = {"status": status, "filled_quantity": text(filled)}
            if venue is None:
                awaiting = state.status == "unknown" or unresolved
                since = self._absent_since(coid) if awaiting else None
                listed = venue_open is None or coid in venue_open
                final = not listed and (
                    not awaiting or (since is not None and now - since >= self.absence_grace)
                )
                if not final:
                    reasons.append(f"absence_unconfirmed:{coid}")
                    if awaiting and since is None and not listed:  # first negative read
                        absent = {"status": ABSENT, "filled_quantity": text(ZERO)}
                        self._record_reconciled(coid, now, absent, None, unresolved)
                        reconciled.append(coid)
                    elif unresolved:
                        ledger.append_event(self._unknown(coid, now))
                    continue
                if since is not None:
                    payload["absent_since"] = ts_text(since)
            changed = status in _VENUE_TERMINAL or (
                status == "open" and state.status == "pending_submit"
            )
            if state.status == "unknown" or unresolved or changed:
                exchange_order_id = venue.exchange_order_id if venue else None
                self._record_reconciled(coid, now, payload, exchange_order_id, unresolved)
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

    def _record_reconciled(self, coid, now, payload, exchange_order_id, unresolved):
        with self.ledger.transaction("reconcile"):
            if unresolved:
                self.ledger.append_event(self._unknown(coid, now))
            self.ledger.append_event(
                OrderEvent(
                    "reconciled:" + digest({"order": coid, "at": now, **payload}),
                    coid,
                    "reconciled",
                    self.source,
                    now,
                    exchange_order_id=exchange_order_id,
                    payload=payload,
                )
            )

    def _absent_since(self, client_order_id):
        """Receipt time of an order's first `absent` reconciliation, else None."""
        return min(
            (
                e.received_at
                for e in self.ledger.events(client_order_id)
                if e.kind == "reconciled" and e.payload.get("status") == ABSENT
            ),
            default=None,
        )

    def _sweep_open_orders(self, projection):
        """(client ids the venue lists as open, reasons); (None, reasons) when the list
        cannot be read. Each venue-open order must be an active ledger order: one unknown
        to the ledger, or terminal there (e.g. `not_found`, then surfaced), is
        `venue_open_untracked:<client id, else exchange id>`."""
        try:
            listed = list(self.gateway.open_orders())
        except Exception:
            return None, ["open_orders_unavailable"]
        reasons = []
        for item in listed:
            state = projection.orders.get(item.client_order_id)
            if state is None or not state.active:
                name = item.client_order_id or item.exchange_order_id or "unidentified"
                reasons.append(f"venue_open_untracked:{name}")
        return {item.client_order_id for item in listed}, reasons

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
        record (`acknowledge_settlement`) after investigating. Once a market is settled in
        the ledger, later changes to the venue record other than its result or value
        surface only through the positions and balance checks.

        An acknowledgement covers exactly the venue records whose fingerprints it names
        (`acknowledge_settlement`, review N-34a): those are skipped, whether or not the
        ledger has settled the market. Any other record the venue lists for the token
        (new, or the same source time with changed content) is reconciled as if nothing
        had been acknowledged: a readable one through the rules above, so for a market
        the ledger has settled it must agree on result and value or `settlement_conflict`
        is reported until an immutable operator correction supersedes that settlement;
        an unparseable one as `settlement_unreadable`. A legacy acknowledgement that
        names no evidence covers nothing (its record is reported again and a warning is
        logged) until the operator re-acknowledges with evidence.

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
        scoped, legacy = self._acknowledgements()
        for item in venue:
            unreadable = isinstance(item, UnreadableSettlement)
            token = item.token if unreadable else item.ticker
            covered = scoped.get(token)
            if covered:
                reason = self._acknowledged(item, unreadable, covered)
            else:
                reason = "settlement_unreadable" if unreadable else self._settle(item)
            if reason is not None:
                if token in legacy:
                    log.warning(
                        "acknowledgement of %s names no evidence and does not cover %s",
                        token,
                        reason,
                    )
                reasons.append(f"{reason}:{token}")
        if not reasons and next_cursor != cursor:
            self.ledger.set_checkpoint(SETTLEMENTS_CURSOR, {"cursor": next_cursor}, now)
        return reasons

    def _acknowledgements(self):
        """Evidence-scoped acknowledgements by token (`{token: {fingerprint, ...}}`) and
        the tokens of legacy acknowledgements that name no evidence."""
        scoped, legacy = {}, set()
        for key, record in self.ledger.processed_inputs().items():
            if not key.startswith(SETTLEMENT_ACK_PREFIX):
                continue
            data = record.get("data") if isinstance(record, dict) else None
            token = data.get("token") if isinstance(data, dict) else None
            evidence = data.get("evidence") if isinstance(data, dict) else None
            if isinstance(token, str) and isinstance(evidence, list) and evidence:
                scoped.setdefault(token, set()).update(v for v in evidence if isinstance(v, str))
            else:
                legacy.add(key[len(SETTLEMENT_ACK_PREFIX) :])
        return scoped, legacy

    def _acknowledged(self, item, unreadable, covered):
        """Reason for a venue record of a token with acknowledged evidence `covered`, or
        None to skip it. A readable record is skipped only when its own fingerprint is
        covered; otherwise it is reconciled as usual. An unreadable group is skipped
        when every record behind it is covered; otherwise its uncovered records decide:
        an unparseable one is `settlement_unreadable` (the operator must look again); a
        single readable one is reconciled as usual; several readable ones are still
        conflicting (`settlement_unreadable`) unless the ledger has settled the market
        and all of them agree with that settlement on result and value, else
        `settlement_conflict`. An old acknowledged record never covers a newly observed
        contradictory one."""
        if not unreadable:
            return None if settlement_fingerprint(item) in covered else self._settle(item)
        if not item.fingerprints:
            return "settlement_unreadable"
        remaining = [r for r in item.records if settlement_fingerprint(r) not in covered]
        parsed = {settlement_fingerprint(r) for r in item.records}
        if any(fp not in covered and fp not in parsed for fp in item.fingerprints):
            return "settlement_unreadable"
        if not remaining:
            return None
        if len(remaining) == 1:
            return self._settle(remaining[0])
        recorded = self.ledger.projection().portfolio.settlements.get(item.token)
        if recorded is None:
            return "settlement_unreadable"
        current = (recorded.result, recorded.settlement_value)
        if all((r.result, r.settlement_value) == current for r in remaining):
            return None
        return "settlement_conflict"

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
