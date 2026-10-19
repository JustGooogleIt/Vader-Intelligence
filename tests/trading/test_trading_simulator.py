"""Paper simulator on SYNTHETIC snapshots (invented books; not market evidence)."""

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest
from _trading_synthetic import EVENT, OTHER, TICKER, D, at, book, market

from vader_intelligence.normalize import book_data
from vader_intelligence.storage import Store
from vader_intelligence.trading import ledger as ledger_module
from vader_intelligence.trading.fees import KALSHI_2026_02_MLB_ASSUMED, PROVISIONAL_SCHEDULES
from vader_intelligence.trading.ledger import Ledger, LedgerConflict, LedgerError
from vader_intelligence.trading.marketdata import (
    open_archive,
    read_archive_markets,
    read_archive_snapshots,
)
from vader_intelligence.trading.orders import Order
from vader_intelligence.trading.portfolio import Settlement
from vader_intelligence.trading.simulator import (
    ForecastInput,
    LatencyModel,
    PaperConfig,
    PaperExchange,
    PaperInputs,
    StrategyConfig,
    _merge_worst,
    run_paper,
    sensitivity,
    worst_of,
)

THIN_NO = (("0.54", 20), ("0.53", 400))


def inputs(snapshots, forecasts, settlements=(), label="synthetic:test"):
    return PaperInputs(
        label, (market(), market(OTHER)), tuple(snapshots), tuple(forecasts), tuple(settlements)
    )


def fc(fid, t, probability="0.55", ticker=TICKER):
    return ForecastInput(fid, ticker, D(probability), at(t), "synthetic-model", "synthetic-hash")


def settle(result="yes", t=7200, ticker=TICKER, sid="s1", supersedes=None):
    return Settlement(
        sid, ticker, result, D(1 if result == "yes" else 0), "synthetic", at(t), supersedes
    )


def summary(report):
    data = report.to_dict()
    data.pop("manifest")
    return data


def test_taker_fill_settlement_and_report_by_hand(tmp_path):
    snaps = [book(t=t) for t in range(0, 601, 30)]
    report = run_paper(
        PaperConfig(), inputs(snaps, [fc("f1", 60)], [settle()]), tmp_path / "l.sqlite3"
    )
    assert [o["status"] for o in report.orders] == ["filled"]
    (f,) = report.fills
    assert (f["price"], f["quantity"], f["fee"]) == ("0.46", "28", "0.49")
    assert f["exchange_ts"] == at(60.25).isoformat()  # 250 ms default submit latency
    assert report.pnl["cash"] == "1014.63"  # 1000 - 12.88 - 0.49 + 28 payout
    assert report.pnl["realized"] == "14.63" and report.verification == []
    m = report.manifest
    assert m["simulated"] is True and "not live results" in m["notice"]
    assert m["fee_schedule"]["version"] == "kalshi-2026-02-general"
    assert m["fee_schedule"]["verified"] is False
    assert set(m["input_digests"]) == {"markets", "snapshots", "forecasts", "settlements"}
    assert any("SIMULATION ASSUMPTIONS" in a for a in m["assumptions"])
    assert "not live results" in report.to_text()
    assert json.loads(report.to_json())["pnl"]["cash"] == "1014.63"
    assert report.exposure_history[-1]["equity"] == "1014.63"


def test_partial_fill_haircut_slippage_and_ioc_remainder(tmp_path):
    snaps = [book(t=t, no=THIN_NO) for t in range(0, 301, 30)]
    report = run_paper(PaperConfig(), inputs(snaps, [fc("f1", 60)]), tmp_path / "a.sqlite3")
    (order,) = report.orders
    assert order["status"] == "canceled" and order["filled_quantity"] == "10"  # 0.5 x 20
    assert order["reason"] == "ioc_remainder"
    slipped = run_paper(
        PaperConfig(extra_slippage_ticks=1), inputs(snaps, [fc("f1", 60)]), tmp_path / "b.sqlite3"
    )
    assert slipped.fills == [] and slipped.orders[0]["status"] == "canceled"
    # Decisions never look ahead: the cheaper book at t=90 does not set the t=60 limit.
    cheap = {"yes": (("0.38", 100),), "no": (("0.60", 500),)}
    later = [book(t=0), book(t=30), book(t=60), book(t=90, **cheap)]
    ahead = run_paper(PaperConfig(), inputs(later, [fc("f1", 61)]), tmp_path / "c.sqlite3")
    assert ahead.orders[0]["limit_price"] == "0.46"


def test_ladder_walk_and_worst_of_bracketing():
    exchange = PaperExchange(None, PaperConfig(), inputs([], []))
    order = Order(
        "vi-paper-0123456789abcdef-1",
        "a" * 64,
        "i" * 64,
        TICKER,
        "yes",
        "buy",
        D(100),
        D("0.48"),
        "ioc",
        False,
        D(1),
        D("0.02"),
        at(),
    )
    snap = book(no=(("0.54", 20), ("0.53", 100), ("0.51", 50)))
    levels, crosses = exchange._executable(snap, order, market(), D(4))
    assert crosses and levels == [(D("0.46"), D(6)), (D("0.47"), D(50))]
    slipped = PaperExchange(None, PaperConfig(extra_slippage_ticks=1), inputs([], []))
    assert slipped._executable(snap, order, market(), D(0))[0] == [
        (D("0.47"), D(10)),
        (D("0.48"), D(50)),
    ]
    a = ((D("0.54"), D(100)), (D("0.53"), D(100)))
    b = ((D("0.55"), D(10)), (D("0.53"), D(300)))
    assert _merge_worst(a, b) == [(D("0.54"), D(10)), (D("0.53"), D(190))]
    merged = worst_of(book(t=0, no=a), book(t=30, no=b))
    assert merged.no_bids == ((D("0.54"), D(10)), (D("0.53"), D(190)))


def test_snapshot_gap_makes_execution_unidentifiable(tmp_path):
    snaps = [book(t=0), book(t=60), book(t=400)]
    report = run_paper(PaperConfig(), inputs(snaps, [fc("f1", 61)]), tmp_path / "l.sqlite3")
    assert report.unidentifiable == 1 and report.fills == []
    assert report.orders[0]["status"] == "rejected"
    assert report.orders[0]["reason"] == "execution_unidentifiable"
    prior = run_paper(
        PaperConfig(book_state_rule="prior_snapshot"),
        inputs(snaps, [fc("f1", 61)]),
        tmp_path / "p.sqlite3",
    )
    assert prior.unidentifiable == 0 and prior.orders[0]["status"] == "filled"


def maker_snaps():
    snaps = [book(t=t) for t in (0, 30, 60, 90)]
    snaps.append(book(t=120, no=(("0.56", 300),)))  # YES ask 0.44: touches our 0.44 bid
    snaps.append(book(t=150, yes=(("0.42", 100),), no=(("0.57", 30),)))  # ask 0.43: through
    return snaps


@pytest.mark.parametrize(
    "model,filled", [("none", "0"), ("trade_through", "15"), ("optimistic_touch", "40")]
)
def test_maker_fill_models(tmp_path, model, filled):
    config = PaperConfig(strategy=StrategyConfig(pricing="join"), maker_fill_model=model)
    report = run_paper(
        config, inputs(maker_snaps(), [fc("f1", 60)], [settle("no")]), tmp_path / "l.sqlite3"
    )
    (order,) = report.orders
    assert order["limit_price"] == "0.44" and order["time_in_force"] == "gtc"
    assert order["quantity"] == "40" and order["filled_quantity"] == filled
    assert order["status"] == ("filled" if filled == "40" else "expired")
    assert all(f["liquidity"] == "maker" and f["price"] == "0.44" for f in report.fills)
    assert report.verification == []
    if model == "optimistic_touch":
        assert any("UPPER BOUND" in a for a in report.manifest["assumptions"])


def test_maker_liquidity_is_not_granted_twice_at_one_instant(tmp_path):
    touch = {"no": (("0.56", 40),)}  # YES ask 0.44; haircut depth 20 per snapshot
    snaps = [book(t=t) for t in (0, 30, 60, 90)]
    snaps += [book(t=120, ref="syn-a", **touch), book(t=120, ref="syn-b", **touch)]
    config = PaperConfig(
        strategy=StrategyConfig(pricing="join"), maker_fill_model="optimistic_touch"
    )
    report = run_paper(config, inputs(snaps, [fc("f1", 60)]), tmp_path / "l.sqlite3")
    assert report.orders[0]["quantity"] == "40" and report.orders[0]["filled_quantity"] == "20"


def test_post_only_never_crosses_and_settlement_correction(tmp_path):
    through = {"yes": (("0.42", 100),), "no": (("0.57", 100),)}  # YES ask 0.43
    moving = [book(t=0), book(t=30), book(t=60, **through), book(t=90, **through)]
    config = PaperConfig(strategy=StrategyConfig(pricing="join"))
    resting = run_paper(config, inputs(moving, [fc("f1", 1)]), tmp_path / "x.sqlite3")
    (order,) = resting.orders  # decided on the t=0 bid 0.44; the book never crossed it
    assert order["limit_price"] == "0.44" and order["status"] == "open" and not resting.fills
    crossed = run_paper(config, inputs(moving, [fc("f1", 59.9)]), tmp_path / "y.sqlite3")
    (order,) = crossed.orders  # decided at t=59.9 on the t=30 bid; arrives into 0.43 asks
    assert order["status"] == "rejected" and order["reason"] == "post_only_would_cross"
    snaps = [book(t=t) for t in range(0, 301, 30)]
    corrected = [settle("yes"), settle("no", 7300, sid="s2", supersedes="s1")]
    report = run_paper(
        PaperConfig(), inputs(snaps, [fc("f1", 60)], corrected), tmp_path / "c.sqlite3"
    )
    assert report.pnl["cash"] == "986.63"  # payout reversed: 1000 - 12.88 - 0.49
    assert [s["settlement_id"] for s in report.settlements] == ["s2"]


def test_resume_refuses_a_different_configuration(tmp_path):
    data = inputs([book(t=0), book(t=30)], [fc("f1", 1)])
    run_paper(PaperConfig(), data, tmp_path / "l.sqlite3")
    with pytest.raises(LedgerConflict):
        run_paper(PaperConfig(seed=7), data, tmp_path / "l.sqlite3")


class Crash(BaseException):
    """Simulated process death."""


def crash_scenario(pricing):
    snaps = [book(t=t, no=THIN_NO) for t in (0, 30, 60, 90)]
    snaps += [book(t=120, yes=(("0.42", 100),), no=(("0.57", 60),))]
    snaps += [book(OTHER, 0), book(OTHER, 90)]
    forecasts = [fc("first", 31), fc("second", 61, "0.56"), fc("other", 62, "0.40", OTHER)]
    return inputs(snaps, forecasts, [settle("yes", 600)]), PaperConfig(
        strategy=StrategyConfig(pricing=pricing),
        maker_fill_model="trade_through",
        latency=LatencyModel((0, 40), (100, 400)),
        seed=3,
    )


@pytest.mark.parametrize(
    "pricing,expected",
    [
        ("take", {("canceled", "ioc_remainder")}),  # IOC partial fills
        ("join", {("expired", "market_settled")}),  # resting, maker fill, expiry at settlement
    ],
)
def test_crash_between_every_durable_step_restarts_to_identical_projections(
    tmp_path, pricing, expected
):
    data, config = crash_scenario(pricing)
    clean = _crash_at_every_point(config, data, tmp_path)
    assert clean.fills
    assert {(o["status"], o["reason"]) for o in clean.orders} == expected
    assert clean.rejections_by_reason == {"size_zero": 1}  # group direction conflict
    if pricing == "join":
        assert {f["liquidity"] for f in clean.fills} == {"maker"}


def test_crash_restart_when_an_order_expires_before_it_arrives(tmp_path):
    data, config = crash_scenario("take")
    data = replace(data, settlements=(settle("yes", 31.05),))  # before any 100 ms+ arrival
    clean = _crash_at_every_point(config, data, tmp_path)
    first = clean.orders[0]
    assert (first["status"], first["reason"], first["filled_quantity"]) == (
        "expired",
        "market_settled",
        "0",
    )
    assert "market_not_open" in clean.rejections_by_reason
    arrivals = [h for h in clean.exposure_history if h["input"].startswith("arrive:")]
    assert arrivals[0]["input"] == "arrive:" + first["client_order_id"]  # recorded no-op


@pytest.mark.parametrize("pricing", ["take", "join"])
def test_parsed_row_reuse_changes_no_result(tmp_path, monkeypatch, pricing):
    data, config = crash_scenario(pricing)
    cached = run_paper(config, data, tmp_path / "cached.sqlite3")
    monkeypatch.setattr(ledger_module, "_ROW_CACHED", frozenset())
    uncached = run_paper(config, data, tmp_path / "uncached.sqlite3")
    assert cached.fills and cached.to_json() == uncached.to_json()


def _crash_at_every_point(config, data, tmp_path):
    """Crash at each durable step in turn, restart, and require identical projections."""
    points = []
    clean = run_paper(config, data, tmp_path / "clean.sqlite3", fault=points.append)
    assert clean.verification == [] and len(points) > 20
    for index in range(len(points)):
        calls = []

        def fault(name, index=index):
            calls.append(name)
            if len(calls) == index + 1:
                raise Crash(name)

        path = tmp_path / f"crash-{index}.sqlite3"
        with pytest.raises(Crash):
            run_paper(config, data, path, fault=fault)
        resumed = run_paper(config, data, path)
        assert resumed.verification == []
        assert summary(resumed) == summary(clean), (index, points[index])
    return clean


def test_sensitivity_is_byte_reproducible_and_covers_both_fee_schedules(tmp_path):
    data = inputs([book(t=t) for t in range(0, 301, 30)], [fc("f1", 60)], [settle()])
    config = PaperConfig(latency=LatencyModel((0, 50), (100, 900)), seed=11)
    grid = {
        "fee_schedules": PROVISIONAL_SCHEDULES,
        "extra_slippage_ticks": (0, 1),
        "latency": (config.latency,),
        "maker_fill_model": ("none",),
    }
    first = sensitivity(config, data, grid, directory=tmp_path)
    second = sensitivity(config, data, grid, directory=tmp_path)
    assert first.to_json() == second.to_json()
    assert str(tmp_path) not in first.to_json()
    rows = first.rows
    assert [r["scenario"]["fee_schedule"] for r in rows] == [
        "kalshi-2026-02-general",
        "kalshi-2026-02-general",
        "kalshi-2026-02-mlb-multiplier-0.5-assumed",
        "kalshi-2026-02-mlb-multiplier-0.5-assumed",
    ]
    general, assumed = rows[0]["pnl"]["cash"], rows[2]["pnl"]["cash"]
    assert D(assumed) > D(general)  # the general schedule is the higher-fee default
    assert "not live results" in first.to_text()
    other_seed = sensitivity(replace(config, seed=12), data, grid, directory=tmp_path)
    assert other_seed.manifest["seed"] == 12
    assert KALSHI_2026_02_MLB_ASSUMED.verified is False


def _archive(path):
    """SYNTHETIC archive: real schema, invented observations."""
    Store(path, min_free_bytes=1).close()
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO runs VALUES ('r','fixture','2026-10-19T21:00:00+00:00',NULL,'done','{}',NULL)"
    )
    rows = [
        (
            "f1",
            "2026-10-19T22:00:30+00:00",
            "book",
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.4400", "10.00"]],
                    "no_dollars": [["0.5400", "5.50"]],
                }
            },
        ),
        (
            "f2",
            "2026-10-19T22:00:00+00:00",
            "book",
            {"orderbook_fp": {"yes_dollars": [["0.4300", "7.00"]], "no_dollars": []}},
        ),
        (
            "f3",
            "2026-10-19T22:01:00+00:00",
            "book",
            {"orderbook_fp": {"yes_dollars": [["0.6000", "1"]], "no_dollars": [["0.5000", "1"]]}},
        ),
        (
            "f4",
            "2026-10-19T22:00:45+00:00",
            "market",
            {
                "yes_bid_dollars": "0.4400",
                "yes_ask_dollars": "0.4600",
                "yes_bid_size_fp": "3.00",
                "yes_ask_size_fp": "4.00",
                "status": "active",
            },
        ),
    ]
    for i, (fetch, retrieved, kind, payload) in enumerate(rows):
        db.execute(
            "INSERT INTO requests VALUES (?, 'r', 'book', ?, 'u', '{}', 1, NULL)",
            (f"q{i}", f"k{i}"),
        )
        db.execute(
            "INSERT INTO fetches(id,request_id,attempt,started_at,retrieved_at,state) "
            "VALUES (?,?,1,?,?,'ok')",
            (fetch, f"q{i}", retrieved, retrieved),
        )
        data = book_data(payload) if kind == "book" else payload
        db.execute(
            "INSERT INTO observations(fetch_id,ticker,kind,parser_version,data_json) "
            "VALUES (?,?,?,1,?)",
            (fetch, TICKER, kind, json.dumps(data)),
        )
    db.execute(
        "INSERT INTO entities(fetch_id,kind,ticker,parent_ticker,parser_version,data_json)"
        " VALUES ('f1','market',?,?,1,?)",
        (TICKER, EVENT, json.dumps({"close_time": "2026-10-19T23:00:00Z"})),
    )
    db.commit()
    db.close()


def test_archive_reader_is_read_only_and_time_ordered(tmp_path):
    path = tmp_path / "archive.sqlite3"
    _archive(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="read-only"):
        read_archive_snapshots(sqlite3.connect(path), [TICKER])
    db = open_archive(path)
    result = read_archive_snapshots(db, [TICKER], kinds=("book", "market"))
    times = [s.as_of for s in result.snapshots]
    assert times == sorted(times) and len(times) == 3
    first, quote = result.snapshots[0], result.snapshots[2]
    assert first.ref.startswith("archive:observation:") and first.no_bids == ()
    assert result.snapshots[1].no_bids == ((D("0.54"), D("5.5")),)
    assert quote.source == "quote" and quote.no_bids == ((D("0.54"), D(4)),)
    assert result.skipped == {"book:ValueError": 1}  # crossed book is skipped, not repaired
    with pytest.raises(sqlite3.OperationalError):
        db.execute("CREATE TABLE x(y)")
    markets = read_archive_markets(db, [TICKER])
    assert markets[TICKER].event_ticker == EVENT and markets[TICKER].close_time == at(3600)
    db.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with pytest.raises(LedgerError):
        Ledger(path, "paper")  # an evidence archive is never used as a ledger


def quote_only(t, yes_bid="0.44", no_bid="0.54"):
    """A depth-unknown snapshot, like a historical candle (prices without sizes)."""
    from vader_intelligence.trading.marketdata import BookSnapshot

    return BookSnapshot(
        TICKER, at(t), ((D(yes_bid), D(0)),), ((D(no_bid), D(0)),), "quote", f"q{t}", False
    )


def test_depth_unknown_snapshots_fill_nothing_unless_a_depth_scenario_is_assumed(tmp_path):
    snaps = [quote_only(t) for t in range(0, 601, 30)]
    plain_run = run_paper(PaperConfig(), inputs(snaps, [fc("f1", 60)]), tmp_path / "a.sqlite3")
    assert plain_run.fills == []
    assert any("supply no depth" in a for a in plain_run.manifest["assumptions"])
    assumed = run_paper(
        PaperConfig(assumed_depth=10), inputs(snaps, [fc("f1", 60)]), tmp_path / "b.sqlite3"
    )
    (f,) = assumed.fills
    # Best NO bid 0.54 → YES ask 0.46; 10 assumed contracts x 0.5 haircut = 5.
    assert (f["price"], f["quantity"]) == ("0.46", "5")
    assert any("SCENARIO" in a for a in assumed.manifest["assumptions"])
    assert assumed.verification == []


def test_sensitivity_sweeps_assumed_depth_as_an_explicit_axis(tmp_path):
    snaps = [quote_only(t) for t in range(0, 601, 30)]
    base = PaperConfig()
    grid = {
        "fee_schedules": PROVISIONAL_SCHEDULES[:1],
        "extra_slippage_ticks": (0,),
        "latency": (base.latency,),
        "maker_fill_model": ("none",),
        "assumed_depth": (None, D(10), D(100)),
    }
    report = sensitivity(base, inputs(snaps, [fc("f1", 60)]), grid, directory=tmp_path)
    fills = [row["fills"] for row in report.rows]
    depths = [row["scenario"]["assumed_depth"] for row in report.rows]
    assert depths == [None, "10", "100"] and fills[0] == 0 and fills[1] == 1 and fills[2] == 1
