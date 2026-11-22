"""Point-in-time quote series: one shape for candles, listing quotes and book snapshots.

A `Quote` is what was observable about one contract at time `at`: best YES bid and ask
(top of book), a last traded price if any, traded volume and open interest, plus the
retrieval identity it came from. Market features are defined on quote series, so a
feature means the same thing whichever source supplied it; the source kind is carried
along so datasets never mix sources silently.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

SOURCES = ("candles", "listing", "book")


@dataclass(frozen=True)
class Quote:
    at: datetime
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    trade_price: Decimal | None
    volume: Decimal | None  # traded in the period (candles) or cumulative (listing)
    open_interest: Decimal | None
    ref: str

    @property
    def mid(self):
        if self.yes_bid is None or self.yes_ask is None or self.yes_bid > self.yes_ask:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread(self):
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid


@dataclass(frozen=True)
class QuoteSeries:
    """Time-ordered quotes of one contract; `volume_kind` is "period" or "cumulative"."""

    ticker: str
    source: str
    volume_kind: str
    quotes: tuple

    def __post_init__(self):
        if self.source not in SOURCES or self.volume_kind not in ("period", "cumulative"):
            raise ValueError("unknown quote source or volume kind")
        times = [q.at for q in self.quotes]
        if times != sorted(times):
            raise ValueError("quotes must be time-ordered")

    def until(self, moment):
        """Only quotes observable at or before `moment` (the point-in-time cut)."""
        return QuoteSeries(
            self.ticker,
            self.source,
            self.volume_kind,
            tuple(q for q in self.quotes if q.at <= moment),
        )

    def latest(self, moment, max_age_seconds):
        """The latest quote at or before `moment`, if no older than `max_age_seconds`."""
        for quote in reversed(self.quotes):
            if quote.at <= moment:
                if (moment - quote.at).total_seconds() <= max_age_seconds:
                    return quote
                return None
        return None


def decimal_or_none(value):
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def from_candles(ticker, candles):
    """history.interpret.candles output → series (period volume; trade price = the
    period's mean traded price when there were trades, else None)."""
    quotes = tuple(
        Quote(
            at=c["end"],
            yes_bid=decimal_or_none(c["yes_bid_close"]),
            yes_ask=decimal_or_none(c["yes_ask_close"]),
            trade_price=decimal_or_none(c["price_mean"] or c["price_close"]),
            volume=decimal_or_none(c["volume"]),
            open_interest=decimal_or_none(c["open_interest"]),
            ref=c["fetch_id"],
        )
        for c in candles
    )
    return QuoteSeries(ticker, "candles", "period", quotes)


def from_listing(ticker, observations):
    """Collector market-listing observations [(retrieved_at, data, fetch_id)] → series
    (cumulative volume; trade price = last traded price as listed)."""
    quotes = tuple(
        Quote(
            at=at,
            yes_bid=decimal_or_none(data.get("yes_bid_dollars")),
            yes_ask=decimal_or_none(data.get("yes_ask_dollars")),
            trade_price=decimal_or_none(data.get("last_price_dollars")),
            volume=decimal_or_none(data.get("volume_fp")),
            open_interest=decimal_or_none(data.get("open_interest_fp")),
            ref=fetch_id,
        )
        for at, data, fetch_id in sorted(observations, key=lambda o: o[0])
    )
    return QuoteSeries(ticker, "listing", "cumulative", quotes)


def from_books(ticker, observations):
    """Collector book observations [(retrieved_at, data, fetch_id)] → series (top of book
    only; no trade price, volume or open interest in a book)."""
    quotes = tuple(
        Quote(
            at=at,
            yes_bid=decimal_or_none(data.get("yes_bid_dollars")),
            yes_ask=decimal_or_none(data.get("yes_ask_dollars")),
            trade_price=None,
            volume=None,
            open_interest=None,
            ref=fetch_id,
        )
        for at, data, fetch_id in sorted(observations, key=lambda o: o[0])
    )
    return QuoteSeries(ticker, "book", "period", quotes)
