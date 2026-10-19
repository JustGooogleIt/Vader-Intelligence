"""Book and quote snapshots, and a read-only reader over the evidence archive.

A `BookSnapshot` holds both bid ladders of one market (best first); the YES ask is
`1 - best NO bid` and the NO ask `1 - best YES bid`, as in the archived Kalshi book.
Archive snapshots are timestamped only with the archive's own `fetches.retrieved_at`;
the reader refuses a connection that is not read-only and never writes.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..normalize import decimal_value
from .instruments import Market, identifier
from .money import ONE, ZERO, in_exact, nonnegative, opt_utc, price, utc

SOURCES = ("book", "quote", "synthetic")
_CHUNK = 400


def _levels(levels, depth_known, name):
    parsed = []
    for level in levels:
        if len(level) != 2:
            raise ValueError(f"{name}: level must be (price, quantity)")
        p, q = price(level[0], f"{name} price"), nonnegative(level[1], f"{name} quantity")
        if depth_known and q == 0:
            raise ValueError(f"{name}: zero-quantity level in a depth-known book")
        parsed.append((p, q))
    parsed.sort(key=lambda level: level[0], reverse=True)
    if len({p for p, _ in parsed}) != len(parsed):
        raise ValueError(f"{name}: duplicate price level")
    return tuple(parsed)


@dataclass(frozen=True)
class BookSnapshot:
    """Both bid ladders of one market at `as_of` (best first). `depth_known=False`
    (quotes without sizes) carries prices only: such a snapshot never supplies depth."""

    ticker: str
    as_of: datetime
    yes_bids: tuple
    no_bids: tuple
    source: str = "synthetic"
    ref: str = ""
    depth_known: bool = True

    def __post_init__(self):
        identifier(self.ticker, "ticker")
        if self.source not in SOURCES:
            raise ValueError(f"unknown snapshot source {self.source!r}")
        if not isinstance(self.depth_known, bool) or not isinstance(self.ref, str):
            raise TypeError("depth_known must be a bool and ref text")
        object.__setattr__(self, "as_of", utc(self.as_of, "as_of"))
        object.__setattr__(self, "yes_bids", _levels(self.yes_bids, self.depth_known, "yes"))
        object.__setattr__(self, "no_bids", _levels(self.no_bids, self.depth_known, "no"))
        if self.best_yes_bid is not None and self.best_no_bid is not None:
            if self.best_yes_bid + self.best_no_bid > ONE:
                raise ValueError("crossed book: YES bid + NO bid exceeds $1")

    @property
    def best_yes_bid(self):
        return self.yes_bids[0][0] if self.yes_bids else None

    @property
    def best_no_bid(self):
        return self.no_bids[0][0] if self.no_bids else None

    def bids(self, side):
        return self.yes_bids if side == "yes" else self.no_bids

    def bid(self, side):
        return self.best_yes_bid if side == "yes" else self.best_no_bid

    def ask(self, side):
        other = self.best_no_bid if side == "yes" else self.best_yes_bid
        return None if other is None else ONE - other

    @property
    @in_exact
    def yes_mid(self):
        bid, ask = self.best_yes_bid, self.ask("yes")
        return None if bid is None or ask is None else (bid + ask) / 2

    def mid(self, side):
        mid = self.yes_mid
        return None if mid is None else (mid if side == "yes" else ONE - mid)


def snapshot_from_book(ticker, as_of, bids, *, ref="", source="book"):
    """Snapshot from an archived `orderbook_fp`-style dict (`yes_dollars`/`no_dollars`).

    Uses the collector's exact decimal validation; zero-quantity levels are dropped and
    missing/null sides are empty ladders.
    """
    if not isinstance(bids, dict):
        raise ValueError("book bids must be an object")
    ladders = {}
    for side in ("yes", "no"):
        levels = bids.get(side + "_dollars") or []
        if not isinstance(levels, list) or len(levels) > 10000:
            raise ValueError("invalid or oversized book levels")
        parsed = []
        for level in levels:
            if not isinstance(level, list) or len(level) != 2:
                raise ValueError("book level must contain price and quantity")
            p, q = decimal_value(level[0], price=True), decimal_value(level[1])
            if q > 0:
                parsed.append((p, q))
        ladders[side] = parsed
    return BookSnapshot(ticker, as_of, ladders["yes"], ladders["no"], source, ref, True)


def snapshot_from_quote(ticker, as_of, data, *, ref=""):
    """Snapshot from a listing quote (best bid/ask only). Depth is known only when both
    `yes_bid_size_fp` and `yes_ask_size_fp` are present."""
    yes_bid, yes_ask = data.get("yes_bid_dollars"), data.get("yes_ask_dollars")
    bid_size, ask_size = data.get("yes_bid_size_fp"), data.get("yes_ask_size_fp")
    depth_known = bid_size is not None and ask_size is not None
    yes, no = [], []
    if yes_bid is not None:
        q = decimal_value(bid_size) if depth_known else ZERO
        if decimal_value(yes_bid, price=True) > 0 and (q > 0 or not depth_known):
            yes.append((decimal_value(yes_bid, price=True), q))
    if yes_ask is not None:
        q = decimal_value(ask_size) if depth_known else ZERO
        ask = decimal_value(yes_ask, price=True)
        if ask < ONE and (q > 0 or not depth_known):
            no.append((ONE - ask, q))
    return BookSnapshot(ticker, as_of, yes, no, "quote", ref, depth_known)


def open_archive(path):
    """Read-only connection to an evidence archive (`mode=ro`, `query_only`)."""
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.execute("PRAGMA query_only=ON")
    return db


def _require_readonly(db):
    if db.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise ValueError("archive connection must be read-only (mode=ro and query_only=ON)")


@dataclass(frozen=True)
class ArchiveSnapshots:
    snapshots: tuple
    skipped: dict


def read_archive_snapshots(db, tickers, *, kinds=("book",), start=None, end=None, limit=500_000):
    """Time-ordered snapshots for `tickers` from archive observations.

    `kind='book'` rows become depth-known books from their `bids`; `kind='market'`
    rows become listing quotes. `as_of` is the archive's retrieval time. Rows that fail
    validation (crossed, unparsable, flagged) are skipped and counted, never repaired.
    Bounded by `limit` rows (exceeding it raises rather than truncating silently).
    """
    _require_readonly(db)
    if not set(kinds) <= {"book", "market"} or not kinds:
        raise ValueError("kinds must be a subset of ('book', 'market')")
    tickers = sorted({identifier(t, "ticker") for t in tickers})
    start, end = opt_utc(start, "start"), opt_utc(end, "end")
    rows = []
    for i in range(0, len(tickers), _CHUNK):
        chunk = tickers[i : i + _CHUNK]
        marks = ",".join("?" * len(chunk))
        kind_marks = ",".join("?" * len(kinds))
        rows.extend(
            db.execute(
                "SELECT o.id, o.ticker, o.kind, o.data_json, f.retrieved_at FROM observations o "
                "JOIN fetches f ON f.id=o.fetch_id "
                f"WHERE o.ticker IN ({marks}) AND o.kind IN ({kind_marks}) "
                "AND f.state='ok' AND f.retrieved_at IS NOT NULL ORDER BY o.id LIMIT ?",
                (*chunk, *kinds, limit + 1),
            ).fetchall()
        )
        if len(rows) > limit:
            raise ValueError(f"more than {limit} archive rows; narrow the tickers or window")
    snapshots, skipped = [], {}
    for row_id, ticker, kind, data_json, retrieved_at in rows:
        try:
            as_of = utc(retrieved_at, "retrieved_at")
            if (start and as_of < start) or (end and as_of > end):
                continue
            data = json.loads(data_json)
            ref = f"archive:observation:{row_id}"
            if kind == "book":
                if data.get("flags"):
                    raise ValueError("flagged book: " + ",".join(sorted(data["flags"])))
                snapshots.append(snapshot_from_book(ticker, as_of, data["bids"], ref=ref))
            else:
                snapshots.append(snapshot_from_quote(ticker, as_of, data, ref=ref))
        except (ValueError, TypeError, KeyError) as exc:
            reason = f"{kind}:{type(exc).__name__}"
            skipped[reason] = skipped.get(reason, 0) + 1
    snapshots.sort(key=lambda s: (s.as_of, s.ticker, s.source, s.ref))
    return ArchiveSnapshots(tuple(snapshots), dict(sorted(skipped.items())))


def read_archive_markets(db, tickers):
    """Market metadata from the earliest archived market entity of each ticker.

    Only identity fields are taken (event ticker, close time); the status is `open`
    because later lifecycle states are represented by settlement inputs, not metadata.
    """
    _require_readonly(db)
    result = {}
    for ticker in sorted({identifier(t, "ticker") for t in tickers}):
        row = db.execute(
            "SELECT parent_ticker, data_json FROM entities WHERE kind='market' AND ticker=? "
            "ORDER BY id LIMIT 1",
            (ticker,),
        ).fetchone()
        if row is None or row[0] is None:
            continue
        data = json.loads(row[1])
        close = data.get("close_time")
        result[ticker] = Market(
            ticker=ticker,
            event_ticker=row[0],
            close_time=datetime.fromisoformat(close.replace("Z", "+00:00")) if close else None,
        )
    return result
