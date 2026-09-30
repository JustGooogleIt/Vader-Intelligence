"""Interpret one supplied market. No latest-version selection or identity join."""

from dataclasses import dataclass

from ..settlement.models import kalshi_result
from .scoring import arithmetic


@dataclass(frozen=True)
class SettlementEligibility:
    label: int | None
    reasons: tuple[str, ...]

    @property
    def eligible(self):
        return self.label is not None


def finalized_binary(market):
    """Use existing provider semantics; flags, nonfinal and scalar results never score.

    Caller must supply the latest applicable retrieval of the selected exact contract
    and prove its identity. MLB results and publication state are not inputs here.
    """
    if not isinstance(market, dict):
        return SettlementEligibility(None, ("invalid_or_missing_outcome",))
    # Provider enums must be scalar strings; malformed arrays must fail closed too.
    if any(not isinstance(market.get(k), str) for k in ("status", "result", "market_type")):
        return SettlementEligibility(None, ("invalid_or_missing_outcome",))
    with arithmetic():
        data = kalshi_result(market)
    reasons = []
    if data["status"] != "finalized":
        reasons.append("not_finalized")
    if data["payout_kind"] != "binary":
        reasons.append("not_binary_payout")
    if data.get("market_type") != "binary":
        reasons.append("not_binary_contract")
    reasons.extend(data["flags"])
    label = {"yes": 1, "no": 0}.get(data["binary_outcome"])
    if label is None and not reasons:
        reasons.append("invalid_or_missing_outcome")
    return SettlementEligibility(None if reasons else label, tuple(reasons))
