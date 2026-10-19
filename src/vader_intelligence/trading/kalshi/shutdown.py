"""Kill switch / shutdown: stop submissions, cancel open orders, reconcile, record.

`shutdown(executor, now=, reason=, actor=)` runs, in order:

1. `gateway.halt(reason)`: no further submission leaves this process;
2. engages the ledger kill switch (persistent; a restart stays blocked until an operator
   records a disengage, which live activation also requires);
3. cancels every active ledger order through `executor.cancel` (cancel requests and
   outcomes are recorded as order events); the batch-cancel endpoint is not on the
   allowlist, so orders are canceled one by one;
4. cancels resting venue orders that carry this system's live client-id prefix but are
   not active in the ledger (strays) directly at the venue; resting orders of other
   origin are reported, never touched;
5. runs `executor.reconcile` and records the outcome summary in the ledger
   (`processed_inputs` key `shutdown:<digest>`), returning a `ShutdownReport`.

Failures never stop the sequence: each is reported. A `pending` cancel was sent but its
confirmation is left to reconciliation (e.g. fills not yet recorded). `report.clean`
holds only when, after the final reconciliation, no ledger order is still active and
nothing is unexplained, failed or unresolved.
"""

from dataclasses import dataclass

from ..money import digest, plain
from .gateway import LIVE_PREFIX


@dataclass(frozen=True)
class ShutdownReport:
    reason: str
    actor: str
    at: object
    canceled: tuple
    pending: tuple
    not_canceled: tuple
    strays_canceled: tuple
    foreign_resting: tuple
    errors: tuple
    unresolved: tuple
    still_active: tuple

    @property
    def clean(self):
        return not (self.still_active or self.foreign_resting or self.errors or self.unresolved)

    def to_dict(self):
        return {**plain(self), "clean": self.clean}


def _failure(exc):
    return type(exc).__name__


def shutdown(executor, *, now, reason, actor, engage_kill_switch=True):
    ledger, gateway = executor.ledger, executor.gateway
    gateway.halt(reason)
    if engage_kill_switch:
        ledger.set_kill_switch(True, reason, actor, now)
    canceled, pending, not_canceled, strays, foreign, errors = [], [], [], [], [], []
    for coid, state in sorted(ledger.projection().orders.items()):
        if not state.active or state.status == "new":
            continue  # never-submitted orders are closed by reconciliation
        try:
            result = executor.cancel(coid, now=now)
        except Exception as exc:
            errors.append(f"cancel:{coid}:{_failure(exc)}")
            continue
        if result is None:
            continue
        bucket = {"canceled": canceled, "pending": pending}.get(result.status, not_canceled)
        bucket.append(f"{coid}:{result.reason or result.status}")
    resting_orders = getattr(gateway, "resting_orders", None)
    if resting_orders is not None:
        try:
            resting = resting_orders()
        except Exception as exc:
            resting = ()
            errors.append(f"resting_orders:{_failure(exc)}")
        active = {c for c, s in ledger.projection().orders.items() if s.active}
        for venue in resting:
            if venue.client_order_id in active:
                continue  # its cancel outcome is already reported above
            if not (venue.client_order_id or "").startswith(LIVE_PREFIX):
                foreign.append(venue.order_id)
                continue
            try:
                result = gateway.cancel_venue_order(venue)
            except Exception as exc:
                errors.append(f"cancel_stray:{venue.order_id}:{_failure(exc)}")
                continue
            if result.status == "canceled":
                strays.append(venue.order_id)
            else:
                pending.append(f"stray:{venue.order_id}:{result.reason or result.status}")
    try:
        unresolved = executor.reconcile(now=now).unresolved
    except Exception as exc:
        unresolved = (f"reconcile:{_failure(exc)}",)
    still_active = tuple(s.order.client_order_id for s in ledger.projection().active_orders)
    report = ShutdownReport(
        reason=reason,
        actor=actor,
        at=now,
        canceled=tuple(canceled),
        pending=tuple(pending),
        not_canceled=tuple(not_canceled),
        strays_canceled=tuple(strays),
        foreign_resting=tuple(foreign),
        errors=tuple(errors),
        unresolved=tuple(unresolved),
        still_active=still_active,
    )
    summary = report.to_dict()
    ledger.mark_processed("shutdown:" + digest(summary), summary, now)
    return report
