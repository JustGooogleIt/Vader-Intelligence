"""Synthetic two-game archive builder for Command Center tests (labelled fixture)."""

import json
from pathlib import Path

from vader_intelligence.config import Config
from vader_intelligence.replay import import_fixture
from vader_intelligence.settlement.schema import migrate as settlement_migrate
from vader_intelligence.storage import Store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "mlb.json"
EV1, EV2 = "KXMLBGAME-26SEP271505HOUATH", "KXMLBGAME-26SEP272005NYYBOS"


def two_game_fixture(path):
    """SYNTHETIC two-game variant of the MLB fixture (labelled; never forward evidence)."""
    d = json.loads(FIXTURE.read_text())
    d["run_id"] = "fixture-two-games"
    r = d["responses"]

    def body(e):
        return json.loads(e["body"])

    def setbody(e, b):
        e["body"] = json.dumps(b, separators=(",", ":"))

    page2 = [e for e in r if e["stage"] == "markets"][1]
    b = body(page2)
    ath = b["markets"][0]

    def mk(side, sub):
        m = dict(ath)
        m["ticker"], m["event_ticker"], m["yes_sub_title"], m["title"] = (
            f"{EV2}-{side}",
            EV2,
            sub,
            sub + " wins",
        )
        m["rules_primary"] = (
            f"If {sub} wins the Yankees vs Red Sox professional baseball game originally scheduled for Sep 27, 2026 at 8:05 PM EDT, then the market resolves to Yes."
        )
        return m

    nyy, bos = mk("NYY", "New York Yankees"), mk("BOS", "Boston Red Sox")
    b["markets"] = [ath, nyy, bos]
    setbody(page2, b)
    ev1 = [e for e in r if e["stage"] == "event"][0]
    ev2 = json.loads(json.dumps(ev1))
    ev2["id"], ev2["url"] = "fixture-event-7", ev1["url"].replace(EV1, EV2)
    ev2["started_at"] = ev2["retrieved_at"] = "2026-09-27T15:00:07+00:00"
    eb = body(ev2)
    eb["event"]["event_ticker"], eb["markets"] = EV2, [nyy, bos]
    setbody(ev2, eb)
    sch = [e for e in r if e["stage"] == "schedule"][0]
    sb = body(sch)
    g1 = sb["dates"][0]["games"][0]
    g2 = json.loads(json.dumps(g1))
    g2["gamePk"], g2["gameDate"] = 824949, "2026-09-28T00:05:00Z"
    g2["teams"] = {
        "away": {"team": {"id": 147, "name": "New York Yankees"}},
        "home": {"team": {"id": 111, "name": "Boston Red Sox"}},
    }
    sb["dates"][0]["games"] = [g1, g2]
    setbody(sch, sb)
    bk1 = [e for e in r if e["stage"] == "book"][0]
    bk2 = json.loads(json.dumps(bk1))
    bk2["id"], bk2["url"] = "fixture-book-8", bk1["url"].replace(EV1 + "-HOU", EV2 + "-NYY")
    bk2["started_at"] = bk2["retrieved_at"] = "2026-09-27T15:00:08+00:00"
    bk2["eligibility"] = {
        "ticker": EV2 + "-NYY",
        "game_id": 824949,
        "cutoff": "2026-09-28T00:03:00+00:00",
        "schedule_fetch_id": "fixture-schedule-5",
        "market_fetch_id": "fixture-event-7",
    }
    r.extend([ev2, bk2])
    Path(path).write_text(json.dumps(d))
    return path


def build_archive(path, fixture_path):
    config = Config(database=str(path), min_free_bytes=1)
    store = Store(config.database, min_free_bytes=1)
    try:
        settlement_migrate(store)
        from vader_intelligence.forecast.schema import migrate as forecast_migrate

        forecast_migrate(store.db)
        forecast_migrate(store.db, record_contract=3)
        forecast_migrate(store.db, record_contract=4)
        import_fixture(store, fixture_path, config)
    finally:
        store.close()
    return path
