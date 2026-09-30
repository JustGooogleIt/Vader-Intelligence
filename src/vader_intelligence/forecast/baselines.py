"""Arithmetic only: a valid book is not proof of timely or eligible evidence."""

from decimal import Decimal

from ..evaluation.scoring import arithmetic
from ..normalize import book_data
from ..settlement.models import dollar


class InvalidBook(ValueError):
    def __init__(self, reason, flags=()):
        self.reason, self.flags = reason, tuple(flags)
        super().__init__(reason)


def constant():
    """Unconditional arithmetic reference; callers must apply common eligibility."""
    return Decimal("0.5")


def midpoint(book, notional="1.0000"):
    """Return YES midpoint from one raw orderbook_fp payload, or raise InvalidBook.

    Reuse the collector's exact price/quantity validation and normalization flags.
    No quotes from other responses, fallbacks or liquidity-based selection.
    """
    if book is None:
        raise InvalidBook("book_missing")
    try:
        if dollar(notional) != 1:
            raise ValueError("unsupported notional")
    except ValueError as exc:
        raise InvalidBook("unsupported_notional") from exc
    if not isinstance(book, dict):
        raise InvalidBook("book_invalid_price_or_quantity")
    try:
        with arithmetic():
            parsed = book_data(book)
    except ValueError as exc:
        raise InvalidBook("book_invalid_price_or_quantity") from exc
    for state in ("missing", "null", "empty"):
        if state in parsed["side_state"].values():
            raise InvalidBook("book_side_" + state, parsed["flags"])
    if any(parsed[side + "_bid_dollars"] is None for side in ("yes", "no")):
        raise InvalidBook("book_no_positive_quantity", parsed["flags"])
    if "crossed_book" in parsed["flags"]:
        raise InvalidBook("book_crossed", parsed["flags"])
    if parsed["flags"]:
        raise InvalidBook("book_flagged", parsed["flags"])
    with arithmetic():
        return (Decimal(parsed["yes_bid_dollars"]) + Decimal(parsed["yes_ask_dollars"])) / 2
