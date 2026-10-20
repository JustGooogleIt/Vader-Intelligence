"""The execution ledger: one SQLite file per environment, append-only, idempotent.

- Separate database file (never the evidence archive: a non-ledger database is refused).
  WAL, `synchronous=FULL`, foreign keys, and a process `flock` on `<db>.lockfile` held
  for the lifetime of a writable ledger. Every write is one short `BEGIN IMMEDIATE`
  transaction (nested writes join the outer one); nothing calls a network inside.
- `ledger_meta.environment` (`paper` | `live`) is written once at creation and is
  immutable (triggers). Opening with a different expected environment raises.
- Every table except `processed_inputs` is append-only: UPDATE, DELETE and
  INSERT-OR-REPLACE are refused by triggers. Environment triggers refuse exchange
  fills/events in a paper ledger and simulated ones in a live ledger, and refuse
  cash-event kinds that do not belong to the environment.
- Appending an existing key with identical content returns the existing row; different
  content raises `LedgerConflict`. Nothing is ever overwritten.
- State (orders, positions, cash, reservations) is never stored: it is a deterministic
  projection of the records, recomputed on demand and checked by `verify()`. Full reads
  of the append-only record tables parse each committed row once (`_table_rows`).
"""

import hashlib
import json
import re
import sqlite3
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..storage import utc_now, writer_lock
from .money import canonical, on_grid, plain, ts_text, utc
from .orders import (
    ENVIRONMENTS,
    EVENT_KINDS,
    GATEWAY_KINDS,
    GATEWAY_SOURCE,
    LOCAL_KINDS,
    Order,
    client_order_id,
    event_from_dict,
    fill_from_dict,
    intent_from_dict,
    order_from_dict,
)
from .portfolio import (
    CASH_KINDS,
    CASH_KINDS_BY_ENVIRONMENT,
    CashEvent,
    PortfolioError,
    Settlement,
    check,
    effective_settlements,
    project,
)
from .risk import Approval, Rejection, approval_from_dict, limits_from_dict, rejection_from_dict

SCHEMA_VERSION = 1
CHECKPOINT_PREFIX = "checkpoint:"


class LedgerError(Exception):
    """Base class for ledger failures."""


class LedgerConflict(LedgerError):
    """An append that contradicts recorded history (never resolved by overwriting)."""


class EnvironmentMismatch(LedgerError):
    """A record or an open request from the wrong environment."""


class LedgerLocked(LedgerError):
    """Another process holds the ledger writer lock."""


@dataclass(frozen=True)
class KillSwitchEvent:
    engaged: bool
    reason: str
    actor: str
    at: datetime

    def __post_init__(self):
        if not isinstance(self.engaged, bool):
            raise TypeError("engaged must be a bool")
        for name in ("reason", "actor"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 500:
                raise ValueError(f"kill switch {name} is required")
        object.__setattr__(self, "at", utc(self.at, "at"))


@dataclass(frozen=True)
class KillSwitchState:
    engaged: bool
    reason: str
    actor: str | None
    at: datetime | None
    default: bool


def _kinds(values):
    return ",".join(f"'{v}'" for v in values)


_GATEWAY_SOURCE_SQL = (
    "(SELECT CASE value WHEN 'paper' THEN 'simulated' WHEN 'live' THEN 'exchange' END "
    "FROM ledger_meta WHERE key='environment')"
)
_ENV_SQL = "(SELECT value FROM ledger_meta WHERE key='environment')"

_TABLES = f"""
CREATE TABLE ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE intents (
    intent_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, event_ticker TEXT NOT NULL,
    created_at TEXT NOT NULL, digest TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE TABLE risk_limit_versions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, version_id TEXT NOT NULL UNIQUE,
    limits_hash TEXT NOT NULL, actor TEXT NOT NULL, recorded_at TEXT NOT NULL,
    digest TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL UNIQUE REFERENCES intents(intent_id),
    limits_version TEXT NOT NULL, created_at TEXT NOT NULL, digest TEXT NOT NULL,
    data_json TEXT NOT NULL);
CREATE TABLE rejections (
    rejection_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL UNIQUE REFERENCES intents(intent_id),
    created_at TEXT NOT NULL, digest TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE TABLE orders (
    client_order_id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(approval_id),
    ticker TEXT NOT NULL, created_at TEXT NOT NULL, digest TEXT NOT NULL,
    data_json TEXT NOT NULL);
CREATE TABLE order_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
    client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
    kind TEXT NOT NULL CHECK (kind IN ({_kinds(EVENT_KINDS)})),
    source TEXT NOT NULL CHECK (source IN ('local','simulated','exchange')),
    digest TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE INDEX order_events_order ON order_events(client_order_id, seq);
CREATE TABLE fills (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, fill_id TEXT NOT NULL UNIQUE,
    client_order_id TEXT NOT NULL REFERENCES orders(client_order_id), ticker TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('simulated','exchange')),
    digest TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE INDEX fills_order ON fills(client_order_id, seq);
CREATE TABLE settlements (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, settlement_id TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL, supersedes_id TEXT UNIQUE REFERENCES settlements(settlement_id),
    digest TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE TABLE cash_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ({_kinds(CASH_KINDS)})),
    digest TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE TABLE kill_switch (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
    engaged INTEGER NOT NULL CHECK (engaged IN (0, 1)), digest TEXT NOT NULL,
    data_json TEXT NOT NULL);
CREATE TABLE processed_inputs (
    input_key TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, digest TEXT NOT NULL,
    data_json TEXT NOT NULL);
"""

# (table, columns that must stay unique). processed_inputs is a mutable checkpoint table.
_APPEND_ONLY = (
    ("ledger_meta", ("key",)),
    ("intents", ("intent_id",)),
    ("risk_limit_versions", ("version_id",)),
    ("approvals", ("approval_id", "intent_id")),
    ("rejections", ("rejection_id", "intent_id")),
    ("orders", ("client_order_id", "approval_id")),
    ("order_events", ("event_id",)),
    ("fills", ("fill_id",)),
    ("settlements", ("settlement_id", "supersedes_id")),
    ("cash_events", ("event_id",)),
    ("kill_switch", ("event_id",)),
)


# Record tables whose full reads are served from parsed committed rows (`_table_rows`).
_ROW_CACHED = frozenset(
    (
        "intents",
        "approvals",
        "rejections",
        "orders",
        "order_events",
        "fills",
        "settlements",
        "cash_events",
    )
)


def _triggers():
    out = []
    for table, unique in _APPEND_ONLY:
        clash = " OR ".join(f"{c} = NEW.{c}" for c in unique)
        out.append(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, 'append-only ledger table {table}'); END;\n"
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, 'append-only ledger table {table}'); END;\n"
            f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} "
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {clash}) "
            f"BEGIN SELECT RAISE(ABORT, 'append-only ledger key exists in {table}'); END;"
        )
    paper, live = CASH_KINDS_BY_ENVIRONMENT["paper"], CASH_KINDS_BY_ENVIRONMENT["live"]
    out.append(
        f"""
CREATE TRIGGER ledger_meta_environment BEFORE INSERT ON ledger_meta
WHEN NEW.key = 'environment' AND NEW.value NOT IN ('paper', 'live')
BEGIN SELECT RAISE(ABORT, 'environment guard: unknown environment'); END;
CREATE TRIGGER fills_environment BEFORE INSERT ON fills
WHEN NEW.source IS NOT {_GATEWAY_SOURCE_SQL}
BEGIN SELECT RAISE(ABORT, 'environment guard: fill source does not match the ledger'); END;
CREATE TRIGGER order_events_environment BEFORE INSERT ON order_events
WHEN NEW.source <> 'local' AND NEW.source IS NOT {_GATEWAY_SOURCE_SQL}
BEGIN SELECT RAISE(ABORT, 'environment guard: event source does not match the ledger'); END;
CREATE TRIGGER order_events_kinds BEFORE INSERT ON order_events
WHEN (NEW.source = 'local' AND NEW.kind NOT IN ({_kinds(LOCAL_KINDS)}))
  OR (NEW.source <> 'local' AND NEW.kind NOT IN ({_kinds(GATEWAY_KINDS)}))
BEGIN SELECT RAISE(ABORT, 'event kind not allowed for its source'); END;
CREATE TRIGGER cash_events_environment BEFORE INSERT ON cash_events
WHEN {_ENV_SQL} IS NULL
  OR ({_ENV_SQL} = 'paper' AND NEW.kind NOT IN ({_kinds(paper)}))
  OR ({_ENV_SQL} = 'live' AND NEW.kind NOT IN ({_kinds(live)}))
BEGIN SELECT RAISE(ABORT, 'environment guard: cash event kind not allowed'); END;
CREATE TRIGGER approvals_exclusive BEFORE INSERT ON approvals
WHEN EXISTS (SELECT 1 FROM rejections WHERE intent_id = NEW.intent_id)
BEGIN SELECT RAISE(ABORT, 'intent already decided'); END;
CREATE TRIGGER rejections_exclusive BEFORE INSERT ON rejections
WHEN EXISTS (SELECT 1 FROM approvals WHERE intent_id = NEW.intent_id)
BEGIN SELECT RAISE(ABORT, 'intent already decided'); END;
CREATE TRIGGER approvals_limits BEFORE INSERT ON approvals
WHEN NOT EXISTS (SELECT 1 FROM risk_limit_versions WHERE limits_hash = NEW.limits_version)
BEGIN SELECT RAISE(ABORT, 'approval cites unrecorded risk limits'); END;
"""
    )
    return "\n".join(out)


SCHEMA = _TABLES + _triggers()
EXPECTED_TRIGGERS = frozenset(re.findall(r"CREATE TRIGGER (\w+)", SCHEMA))


def build_order(environment, approval, intent, created_at):
    """The only order an approval can become (fields fixed by approval and intent)."""
    return Order(
        client_order_id=client_order_id(environment, approval.approval_id),
        approval_id=approval.approval_id,
        intent_id=intent.intent_id,
        ticker=intent.ticker,
        side=intent.side,
        action=intent.action,
        quantity=approval.quantity,
        limit_price=approval.limit_price,
        time_in_force=intent.time_in_force,
        post_only=intent.post_only,
        qty_step=approval.qty_step,
        fee_bound_per_step=approval.fee_bound_per_step,
        created_at=created_at,
    )


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _translate(exc):
    message = str(exc)
    if "environment guard" in message:
        return EnvironmentMismatch(message)
    return LedgerConflict(message)


class Ledger:
    """One ledger file. Use as a context manager or call `close()`."""

    def __init__(self, path, environment, *, create=True, readonly=False, created_at=None):
        if environment not in ENVIRONMENTS:
            raise ValueError(f"unknown environment {environment!r}")
        self.path = Path(path).resolve()
        self.environment = environment
        self.readonly = readonly
        self.fault = None  # test hook: called with durable-step names
        self._depth = 0
        self._cache = None
        self._committed = {}  # table -> (last rowid, parsed committed rows)
        self._stack = ExitStack()
        if not readonly:
            try:
                self._stack.enter_context(writer_lock(self.path))
            except RuntimeError as exc:
                self._stack.close()
                raise LedgerLocked("another process holds the ledger writer lock") from exc
        exists = self.path.exists() and self.path.stat().st_size > 0
        if not exists and (readonly or not create):
            self._stack.close()
            raise LedgerError(f"ledger {self.path} does not exist")
        try:
            if readonly:
                self.db = sqlite3.connect(
                    self.path.as_uri() + "?mode=ro", uri=True, timeout=5, isolation_level=None
                )
                self.db.execute("PRAGMA query_only=ON")
            else:
                self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        except BaseException:
            self._stack.close()
            raise
        try:
            self._open(created_at)
        except BaseException:
            self.close()
            raise

    def _open(self, created_at):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if version == 0 and (tables or self.readonly):
            raise LedgerError("refusing to initialize a ledger inside a non-ledger database")
        if version not in (0, SCHEMA_VERSION):
            raise LedgerError(f"unsupported ledger schema version {version}")
        if version and "ledger_meta" not in tables:
            raise LedgerError("not a ledger database")
        self.db.execute("PRAGMA foreign_keys=ON")
        if not self.readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA fullfsync=ON")
            self.db.execute("PRAGMA busy_timeout=5000")
        if version == 0:
            when = ts_text(created_at or utc_now())
            try:
                self.db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
                for key, value in (
                    ("environment", self.environment),
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("created_at", when),
                ):
                    self.db.execute("INSERT INTO ledger_meta VALUES (?, ?)", (key, value))
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        meta = self.meta()
        if meta.get("environment") != self.environment:
            raise EnvironmentMismatch(
                f"ledger environment is {meta.get('environment')!r}, expected {self.environment!r}"
            )

    def close(self):
        try:
            if getattr(self, "db", None) is not None:
                self.db.close()
                self.db = None
        finally:
            self._stack.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- transactions -------------------------------------------------------------

    @property
    def in_transaction(self):
        return bool(self._depth)

    def fault_point(self, name):
        """Named durable-step boundary; tests inject crashes here."""
        if self.fault is not None:
            self.fault(name)

    @contextmanager
    def transaction(self, label="write"):
        """One BEGIN IMMEDIATE transaction; nested use joins the outer transaction."""
        if self.readonly:
            raise LedgerError("ledger opened read-only")
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
            self.fault_point(f"commit:{label}")
            self.db.execute("COMMIT")
        except BaseException:
            self._cache = None
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0
        self.fault_point(f"committed:{label}")

    def _append(self, table, key_column, key, record, columns=None):
        payload = canonical(record)
        digest = _sha(payload)
        with self.transaction(f"append:{table}"):
            row = self.db.execute(
                f"SELECT digest FROM {table} WHERE {key_column}=?", (key,)
            ).fetchone()
            if row is not None:
                if row[0] == digest:
                    return False
                raise LedgerConflict(f"{table} {key!r} already recorded with different content")
            values = {key_column: key, **(columns or {}), "digest": digest, "data_json": payload}
            names = ",".join(values)
            marks = ",".join("?" * len(values))
            try:
                self.db.execute(
                    f"INSERT INTO {table}({names}) VALUES ({marks})", tuple(values.values())
                )
            except sqlite3.IntegrityError as exc:
                raise _translate(exc) from exc
        return True

    def _exists(self, table, key_column, key):
        return (
            self.db.execute(f"SELECT 1 FROM {table} WHERE {key_column}=?", (key,)).fetchone()
            is not None
        )

    # -- reads ---------------------------------------------------------------------

    def meta(self):
        return dict(self.db.execute("SELECT key, value FROM ledger_meta").fetchall())

    def bind_meta(self, key, value):
        """Record `key = value` once (e.g. the Kalshi environment a live ledger trades);
        the same value again is a no-op, another value raises `LedgerConflict`."""
        if key in ("environment", "schema_version", "created_at") or not isinstance(value, str):
            raise LedgerError(f"ledger meta {key!r} cannot be bound")
        with self.transaction("bind-meta"):
            row = self.db.execute("SELECT value FROM ledger_meta WHERE key=?", (key,)).fetchone()
            if row is not None:
                if row[0] != value:
                    raise LedgerConflict(f"ledger is bound to {key}={row[0]!r}, not {value!r}")
                return False
            self.db.execute("INSERT INTO ledger_meta VALUES (?, ?)", (key, value))
        return True

    def _rows(self, table, factory, where="", params=(), order="rowid"):
        if not where and table in _ROW_CACHED and order in ("rowid", "seq"):
            return self._table_rows(table, factory)
        sql = f"SELECT data_json FROM {table} {where} ORDER BY {order}"
        return [factory(json.loads(r[0])) for r in self.db.execute(sql, params)]

    def _table_rows(self, table, factory):
        """Every row of an append-only table in rowid order, parsing committed rows once.

        Triggers refuse UPDATE, DELETE and REPLACE on these tables and one writer commits
        at a time, so a committed row never changes and every later row has a larger
        rowid (`seq` is the rowid). Only rows read outside any transaction are kept: inside
        one, rows this connection wrote may still roll back (and their rowids be reused)."""
        last, committed = self._committed.get(table, (0, ()))
        fresh = self.db.execute(
            f"SELECT rowid, data_json FROM {table} WHERE rowid>? ORDER BY rowid", (last,)
        ).fetchall()
        parsed = [factory(json.loads(data)) for _, data in fresh]
        if fresh and not self.db.in_transaction:
            committed = (*committed, *parsed)
            self._committed[table] = (fresh[-1][0], committed)
            return list(committed)
        return [*committed, *parsed]

    def _one(self, table, factory, key_column, key):
        rows = self._rows(table, factory, f"WHERE {key_column}=?", (key,))
        return rows[0] if rows else None

    def intents(self):
        return self._rows("intents", intent_from_dict)

    def intent(self, intent_id):
        return self._one("intents", intent_from_dict, "intent_id", intent_id)

    def approvals(self):
        return self._rows("approvals", approval_from_dict)

    def approval(self, approval_id):
        return self._one("approvals", approval_from_dict, "approval_id", approval_id)

    def rejections(self):
        return self._rows("rejections", rejection_from_dict)

    def decision_for(self, intent_id):
        return self._one("approvals", approval_from_dict, "intent_id", intent_id) or self._one(
            "rejections", rejection_from_dict, "intent_id", intent_id
        )

    def orders(self):
        return self._rows("orders", order_from_dict)

    def order(self, coid):
        return self._one("orders", order_from_dict, "client_order_id", coid)

    def order_for_approval(self, approval_id):
        return self._one("orders", order_from_dict, "approval_id", approval_id)

    def events(self, coid=None):
        if coid is None:
            return self._rows("order_events", event_from_dict, order="seq")
        return self._rows(
            "order_events", event_from_dict, "WHERE client_order_id=?", (coid,), "seq"
        )

    def fills(self, coid=None):
        if coid is None:
            return self._rows("fills", fill_from_dict, order="seq")
        return self._rows("fills", fill_from_dict, "WHERE client_order_id=?", (coid,), "seq")

    def settlements(self):
        return self._rows("settlements", lambda d: Settlement(**d), order="seq")

    def settlement(self, settlement_id):
        rows = self._rows(
            "settlements", lambda d: Settlement(**d), "WHERE settlement_id=?", (settlement_id,)
        )
        return rows[0] if rows else None

    def cash_events(self):
        return self._rows("cash_events", lambda d: CashEvent(**d), order="seq")

    def projection(self):
        """Current projection; cached between writes (never across a rollback).

        The cache key combines this connection's change count with `data_version`, which
        moves when another connection commits (e.g. for a read-only ledger)."""
        key = (self.db.execute("PRAGMA data_version").fetchone()[0], self.db.total_changes)
        if self._cache is not None and self._cache[0] == key and not self._depth:
            return self._cache[1]
        result = project(
            self.environment,
            intents=self.intents(),
            orders=self.orders(),
            events=self.events(),
            fills=self.fills(),
            settlements=self.settlements(),
            cash_events=self.cash_events(),
        )
        if not self._depth:
            self._cache = (key, result)
        return result

    def kill_switch(self):
        """Latest kill-switch row; default engaged for live, disengaged for paper."""
        row = self.db.execute(
            "SELECT data_json FROM kill_switch ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            live = self.environment == "live"
            reason = "default: live starts engaged" if live else "default: paper"
            return KillSwitchState(live, reason, None, None, True)
        event = KillSwitchEvent(**json.loads(row[0]))
        return KillSwitchState(event.engaged, event.reason, event.actor, event.at, False)

    def _require_complete_limits(self, limits_hash):
        """A live approval must cite recorded, complete operator limits."""
        row = self.db.execute(
            "SELECT data_json FROM risk_limit_versions WHERE limits_hash=? LIMIT 1", (limits_hash,)
        ).fetchone()
        if row is None:
            raise LedgerConflict("live approval cites unrecorded limits")
        try:
            limits_from_dict(json.loads(row[0])["limits"]).require_complete()
        except ValueError as exc:
            raise LedgerConflict(f"live approval cites incomplete limits: {exc}") from None

    def current_limits(self):
        """(RiskLimits, limits hash) of the latest operator-written version, or (None, None)."""
        row = self.db.execute(
            "SELECT data_json FROM risk_limit_versions ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None, None
        data = json.loads(row[0])
        limits = limits_from_dict(data["limits"])
        if limits.digest() != data["limits_hash"]:
            raise LedgerError("risk limit version does not match its hash")
        return limits, data["limits_hash"]

    def processed(self, key):
        row = self.db.execute(
            "SELECT data_json FROM processed_inputs WHERE input_key=?", (key,)
        ).fetchone()
        return None if row is None else json.loads(row[0])["data"]

    def processed_inputs(self):
        return {
            k: json.loads(v)
            for k, v in self.db.execute(
                "SELECT input_key, data_json FROM processed_inputs ORDER BY input_key"
            )
        }

    # -- writes --------------------------------------------------------------------

    def record_intent(self, intent):
        return self._append(
            "intents",
            "intent_id",
            intent.intent_id,
            intent.to_dict(),
            {
                "ticker": intent.ticker,
                "event_ticker": intent.event_ticker,
                "created_at": ts_text(intent.created_at),
            },
        )

    def record_limits(self, limits, actor, at):
        """Operator path: append a limits version. Returns its hash (the limits_version)."""
        if not isinstance(actor, str) or not actor:
            raise ValueError("an actor is required")
        record = {
            "limits": limits.to_dict(),
            "limits_hash": limits.digest(),
            "actor": actor,
            "at": ts_text(at),
        }
        self._append(
            "risk_limit_versions",
            "version_id",
            _sha(canonical(record)),
            record,
            {"limits_hash": limits.digest(), "actor": actor, "recorded_at": ts_text(at)},
        )
        return limits.digest()

    def record_decision(self, decision):
        """Record a risk Approval or Rejection for a recorded intent (one per intent)."""
        intent = self.intent(decision.intent_id)
        if intent is None:
            raise LedgerConflict("decision for an unrecorded intent")
        if isinstance(decision, Approval):
            if decision.limit_price != intent.limit_price:
                raise LedgerConflict("approved limit price differs from the intent")
            if self.environment == "live":
                self._require_complete_limits(decision.limits_version)
            if intent.max_quantity is not None and decision.quantity > intent.max_quantity:
                raise LedgerConflict("approved quantity exceeds the intent maximum")
            return self._append(
                "approvals",
                "approval_id",
                decision.approval_id,
                decision.to_dict(),
                {
                    "intent_id": decision.intent_id,
                    "limits_version": decision.limits_version,
                    "created_at": ts_text(decision.created_at),
                },
            )
        if isinstance(decision, Rejection):
            return self._append(
                "rejections",
                "rejection_id",
                decision.rejection_id,
                decision.to_dict(),
                {"intent_id": decision.intent_id, "created_at": ts_text(decision.created_at)},
            )
        raise TypeError("decision must be an Approval or a Rejection")

    def create_order(self, order):
        """Create the order of an unused approval; every field is checked against it."""
        approval = self.approval(order.approval_id)
        if approval is None:
            raise LedgerConflict("an order requires a recorded approval")
        intent = self.intent(approval.intent_id)
        expected = build_order(self.environment, approval, intent, order.created_at)
        if order != expected:
            raise LedgerConflict("order fields differ from its approval")
        if order.created_at < approval.created_at:
            raise LedgerConflict("order predates its approval")
        used = self.order_for_approval(order.approval_id)
        if used is not None and used.client_order_id != order.client_order_id:
            raise LedgerConflict("approval already used by another order")
        return self._append(
            "orders",
            "client_order_id",
            order.client_order_id,
            order.to_dict(),
            {
                "approval_id": order.approval_id,
                "ticker": order.ticker,
                "created_at": ts_text(order.created_at),
            },
        )

    def _require_source(self, source):
        if source != "local" and source != GATEWAY_SOURCE[self.environment]:
            raise EnvironmentMismatch(
                f"{source!r} records are refused by a {self.environment} ledger"
            )

    def _submitted(self, coid):
        return (
            self.db.execute(
                "SELECT 1 FROM order_events WHERE client_order_id=? AND kind='submit_requested'",
                (coid,),
            ).fetchone()
            is not None
        )

    def _has_events(self, coid):
        return (
            self.db.execute(
                "SELECT 1 FROM order_events WHERE client_order_id=? LIMIT 1", (coid,)
            ).fetchone()
            is not None
        )

    def append_event(self, event):
        """Append an order event. An order is submitted at most once."""
        self._require_source(event.source)
        with self.transaction("event"):
            if self.order(event.client_order_id) is None:
                raise LedgerConflict("event for an unknown order")
            row = self.db.execute(
                "SELECT digest FROM order_events WHERE event_id=?", (event.event_id,)
            ).fetchone()
            if row is None:
                submitted = self._submitted(event.client_order_id)
                if event.kind == "submit_requested" and submitted:
                    raise LedgerConflict("order already submitted; resubmission is refused")
                if event.kind == "submit_requested" and self._has_events(event.client_order_id):
                    raise LedgerConflict("submit_requested must be an order's first event")
                if event.source == "local" and event.kind == "canceled" and submitted:
                    raise LedgerConflict("only a never-submitted order can be canceled locally")
                if event.source != "local" and not submitted:
                    raise LedgerConflict("gateway event for an order that was never submitted")
            return self._append(
                "order_events",
                "event_id",
                event.event_id,
                event.to_dict(),
                {
                    "client_order_id": event.client_order_id,
                    "kind": event.kind,
                    "source": event.source,
                },
            )

    def append_fill(self, fill):
        """Append a fill after checking it against its order and the current holdings."""
        self._require_source(fill.source)
        with self.transaction("fill"):
            row = self.db.execute(
                "SELECT digest FROM fills WHERE fill_id=?", (fill.fill_id,)
            ).fetchone()
            if row is None:
                self._check_fill(fill)
            return self._append(
                "fills",
                "fill_id",
                fill.fill_id,
                fill.to_dict(),
                {
                    "client_order_id": fill.client_order_id,
                    "ticker": fill.ticker,
                    "source": fill.source,
                },
            )

    def _check_fill(self, fill):
        order = self.order(fill.client_order_id)
        if order is None:
            raise LedgerConflict("fill for an unknown order")
        if (fill.ticker, fill.side, fill.action) != (order.ticker, order.side, order.action):
            raise LedgerConflict("fill does not match its order")
        if (order.action == "buy" and fill.price > order.limit_price) or (
            order.action == "sell" and fill.price < order.limit_price
        ):
            raise LedgerConflict("fill price violates the order limit")
        if not self._submitted(order.client_order_id):
            raise LedgerConflict("fill for an order that was never submitted")
        if not on_grid(fill.quantity, order.qty_step):
            # Reservations bound fees per step lot; an off-grid fill could exceed them.
            raise LedgerConflict("fill quantity is off the order's quantity step grid")
        if fill.fee > (fill.quantity / order.qty_step) * order.fee_bound_per_step:
            # Reservations assumed this bound; a higher fee means the fee model is wrong:
            # refused (state becomes unknown, buys stop) rather than silently underfunded.
            raise LedgerConflict("fill fee exceeds the order's fee bound")
        projection = self.projection()
        state = projection.orders[order.client_order_id]
        if state.filled_quantity + fill.quantity > order.quantity:
            raise LedgerConflict("fills would exceed the order quantity")
        if fill.ticker in projection.portfolio.settlements:
            raise LedgerConflict("fill on a settled market")
        if fill.action == "sell":
            held = projection.portfolio.holding(fill.ticker).quantity(fill.side)
            if fill.quantity > held:
                raise LedgerConflict("sell fill exceeds the holding")

    def append_settlement(self, settlement):
        """Append a settlement, or a correction superseding the current one."""
        with self.transaction("settlement"):
            if not self._exists("settlements", "settlement_id", settlement.settlement_id):
                current = effective_settlements(self.settlements())
                head = current.get(settlement.ticker)
                if settlement.supersedes_id is None and head is not None:
                    raise LedgerConflict("market already settled; append a correction")
                if settlement.supersedes_id is not None and (
                    head is None or head.settlement_id != settlement.supersedes_id
                ):
                    raise LedgerConflict("a correction must supersede the current settlement")
            return self._append(
                "settlements",
                "settlement_id",
                settlement.settlement_id,
                settlement.to_dict(),
                {"ticker": settlement.ticker, "supersedes_id": settlement.supersedes_id},
            )

    def append_cash_event(self, event):
        allowed = CASH_KINDS_BY_ENVIRONMENT[self.environment]
        if event.kind not in allowed:
            raise EnvironmentMismatch(
                f"{event.kind!r} cash events are refused in {self.environment}"
            )
        with self.transaction("cash"):
            if not self._exists("cash_events", "event_id", event.event_id):
                if self.environment == "paper" and event.kind in ("withdrawal", "adjustment"):
                    port = self.projection().portfolio
                    delta = -event.amount if event.kind == "withdrawal" else event.amount
                    if port.cash + delta < port.reserved_cash:
                        raise LedgerConflict("cash event would leave reservations unfunded")
            return self._append(
                "cash_events", "event_id", event.event_id, event.to_dict(), {"kind": event.kind}
            )

    def set_kill_switch(self, engaged, reason, actor, at):
        """Append a kill-switch row. Repeating the current row is a no-op; the row id
        chains to the previous row, so re-engaging after a disengage is always recorded."""
        record = plain(KillSwitchEvent(engaged, reason, actor, at))
        with self.transaction("kill_switch"):
            last = self.db.execute(
                "SELECT event_id, data_json FROM kill_switch ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if last is None or json.loads(last[1]) != record:
                event_id = _sha(canonical({**record, "previous": last[0] if last else None}))
                self._append("kill_switch", "event_id", event_id, record, {"engaged": int(engaged)})
        return self.kill_switch()

    def mark_processed(self, key, data, at):
        """Record an input as processed (insert-once; same content is idempotent)."""
        if key.startswith(CHECKPOINT_PREFIX):
            raise ValueError("checkpoint keys are written with set_checkpoint")
        record = {"input_key": key, "data": data, "at": ts_text(at)}
        return self._append(
            "processed_inputs", "input_key", key, record, {"recorded_at": record["at"]}
        )

    def set_checkpoint(self, key, data, at):
        """Upsert a mutable checkpoint (e.g. a reconciliation cursor). Only keys under
        `CHECKPOINT_PREFIX`, so insert-once processed keys can never be overwritten."""
        if not key.startswith(CHECKPOINT_PREFIX):
            raise ValueError(f"checkpoint keys must start with {CHECKPOINT_PREFIX!r}")
        record = {"input_key": key, "data": data, "at": ts_text(at)}
        payload = canonical(record)
        with self.transaction("checkpoint"):
            self.db.execute(
                "INSERT INTO processed_inputs(input_key, recorded_at, digest, data_json) "
                "VALUES (?,?,?,?) ON CONFLICT(input_key) DO UPDATE SET "
                "recorded_at=excluded.recorded_at, digest=excluded.digest, "
                "data_json=excluded.data_json",
                (key, record["at"], _sha(payload), payload),
            )

    # -- verification ----------------------------------------------------------------

    def verify(self):
        """Recompute every projection from the records and check the invariants.

        Returns a list of failure strings (empty when the ledger is consistent).
        """
        # Recompute from the records on disk, never from rows parsed earlier.
        self._committed, self._cache = {}, None
        failures = []
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            failures.append("SQLite quick_check failed")
        if self.db.execute("PRAGMA foreign_key_check").fetchone():
            failures.append("foreign key check failed")
        triggers = {
            r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        missing = sorted(EXPECTED_TRIGGERS - triggers)
        if missing:
            failures.append("missing guard triggers: " + ", ".join(missing))
        meta = self.meta()
        if meta.get("environment") != self.environment:
            failures.append("environment mismatch")
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            failures.append("schema version mismatch")
        columns = {
            "intents": ("intent_id", ("intent_id", "ticker", "event_ticker")),
            "approvals": ("approval_id", ("approval_id", "intent_id", "limits_version")),
            "rejections": ("rejection_id", ("rejection_id", "intent_id")),
            "orders": ("client_order_id", ("client_order_id", "approval_id", "ticker")),
            "order_events": ("event_id", ("event_id", "client_order_id", "kind", "source")),
            "fills": ("fill_id", ("fill_id", "client_order_id", "ticker", "source")),
            "settlements": ("settlement_id", ("settlement_id", "ticker", "supersedes_id")),
            "cash_events": ("event_id", ("event_id", "kind")),
            "risk_limit_versions": ("version_id", ("limits_hash", "actor")),
            "kill_switch": ("event_id", ("engaged",)),
            "processed_inputs": ("input_key", ("input_key",)),
        }
        for table, (key, cols) in columns.items():
            for row in self.db.execute(
                f"SELECT {key}, digest, data_json, {','.join(cols)} FROM {table}"
            ):
                if _sha(row[2]) != row[1]:
                    failures.append(f"{table} {row[0]}: digest mismatch")
                    continue
                data = json.loads(row[2])
                for name, value in zip(cols, row[3:]):
                    stored = data.get(name)
                    if table == "kill_switch":
                        stored = int(bool(data.get("engaged")))
                    if stored != value:
                        failures.append(f"{table} {row[0]}: column {name} disagrees with record")
        try:
            intents = {i.intent_id: i for i in self.intents()}
            approvals = {a.approval_id: a for a in self.approvals()}
            for order in self.orders():
                approval = approvals.get(order.approval_id)
                if approval is None or order != build_order(
                    self.environment, approval, intents[approval.intent_id], order.created_at
                ):
                    failures.append(f"order {order.client_order_id} differs from its approval")
            recorded = {
                r[0] for r in self.db.execute("SELECT limits_hash FROM risk_limit_versions")
            }
            for approval in approvals.values():
                if approval.limits_version not in recorded:
                    failures.append(f"approval {approval.approval_id} cites unrecorded limits")
            projection = self.projection()
            failures.extend(
                check(
                    projection.portfolio,
                    fills=self.fills(),
                    settlements=self.settlements(),
                    cash_events=self.cash_events(),
                    order_states=list(projection.orders.values()),
                    environment=self.environment,
                )
            )
        except (PortfolioError, ValueError, KeyError, TypeError) as exc:
            failures.append(f"projection failed: {type(exc).__name__}: {str(exc)[:300]}")
        return failures

    def backup(self, path):
        """Online-backup copy to `path`, then verify the copy. Returns its failures."""
        target = Path(path).resolve()
        if target.exists():
            raise LedgerError("backup target already exists")
        dest = sqlite3.connect(target)
        try:
            self.db.backup(dest)
        finally:
            dest.close()
        with Ledger(target, self.environment, create=False) as copy:
            return copy.verify()


def open_ledger(path, environment, **options):
    """Open (creating when missing) the ledger at `path` for `environment`."""
    return Ledger(path, environment, **options)
