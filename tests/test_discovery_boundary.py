"""F3.3a: two-phase collection on schema 3. All evidence is synthetic (plan §7 cases).

Legacy schema-1/2 behavior is covered by the unmodified tests/test_collector.py and
tests/test_integration.py; this module only adds the schema-3 discovery boundary.
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import Clock, response
from test_collector import handler_for
from test_transport import reader_for

from ops import archive
from vader_intelligence.collector import Collector
from vader_intelligence.forecast import journal
from vader_intelligence.forecast.discovery import REASON_ORDER, DiscoveryOutcome
from vader_intelligence.forecast.schema import migrate as forecast_migrate
from vader_intelligence.settlement.schema import migrate as settlement_migrate
from vader_intelligence.storage import DiscoveryGateError, Store
from vader_intelligence.transport import KALSHI, Reader

TESTS = Path(__file__).resolve().parent


@pytest.fixture
def research(store):
    """Schema 3 through the two explicit migrations; collection still writes raw rows."""
    settlement_migrate(store)
    forecast_migrate(store.db)
    return store


def passes(store):
    return [
        (row["id"], row["run_id"], row["session_id"], json.loads(row["payload_json"]))
        for row in store.db.execute("SELECT * FROM discovery_passes ORDER BY id")
    ]


def book_calls(calls):
    return [c for c in calls if c.url.path.endswith("/orderbook")]


def stable(payload):
    """Summary content that must not depend on identifiers, clocks or books."""
    manifest = {
        k: v
        for k, v in payload["manifest"].items()
        if k
        not in {
            "started_at",
            "fetch_seq_after",
            "fetch_seq_through",
            "pages",
            "documents",
            "schedule_fetch_ids",
            "candidates",
            "events",
            "eligibility",
        }
    }
    manifest["candidates"] = [c["ticker"] for c in payload["manifest"]["candidates"]]
    manifest["eligibility"] = [
        (e["ticker"], e["eligible"], e["reason"], e["game_id"])
        for e in payload["manifest"]["eligibility"]
    ]
    return {
        "complete": payload["complete"],
        "reasons": payload["reasons"],
        "counts": payload["counts"],
        "manifest": manifest,
    }


# ----------------------------------------------------------------------------- A


def test_complete_run_commits_summary_before_any_book(research, config, fixture_data):
    handler, calls, _ = handler_for(fixture_data)
    seen_at_book = []

    def observing(request):
        if request.url.path.endswith("/orderbook"):
            seen_at_book.append(passes(research))
        return handler(request)

    result = Collector(research, reader_for(research, config, observing), config).run()
    assert result["status"] == "complete", result
    assert result["books"] == 2 and result["books_this_pass"] == 2
    assert result["discovery_complete"] is True and result["discovery_reasons"] == []
    rows = passes(research)
    assert len(rows) == 1
    pass_id, run_id, session, payload = rows[0]
    assert result["discovery_pass_id"] == pass_id and run_id == result["run_id"]
    assert (
        result["discovery_digest"]
        == research.db.execute(
            "SELECT digest FROM discovery_passes WHERE id=?", (pass_id,)
        ).fetchone()[0]
    )
    # A4: every book request observed exactly one committed, complete summary.
    assert len(seen_at_book) == 2
    assert all(len(s) == 1 and s[0][3]["complete"] for s in seen_at_book)
    assert payload["complete"] is True and payload["reasons"] == []
    assert payload["counts"] == {"contracts": 2, "games": 1}
    assert payload["phase_sequence"] == 1 and payload["mode"] == "synthetic"
    manifest = payload["manifest"]
    assert manifest["phase_reached"] == "done" and manifest["candidates_known"]
    assert manifest["pages_complete"] and manifest["eligibility_complete"]
    assert manifest["pages"]["terminal"] and manifest["pages"]["page_count"] == 1
    assert [c["ticker"] for c in manifest["candidates"]] == sorted(
        m["ticker"] for m in json.loads(fixture_data["responses"][4]["body"])["markets"]
    )
    assert manifest["official_game_ids"] == [824948] and manifest["unmatched_games"] == []
    assert len(manifest["eligibility"]) == 2 and manifest["incomplete_attempts"] == []
    # Typed references cover every completed discovery fetch, and no book.
    referenced = {r["key"]["id"] for r in payload["references"]}
    discovery_fetches = {
        row[0]
        for row in research.db.execute(
            "SELECT f.id FROM fetches f JOIN requests r ON r.id=f.request_id "
            "WHERE r.run_id=? AND r.stage!='book'",
            (run_id,),
        )
    }
    assert referenced == discovery_fetches and len(referenced) == 7
    assert research.verify_integrity() == []
    assert (
        archive.classify({"database": str(research.path)}, result, 0) == "collected_eligible_games"
    )
    provenance = json.loads(
        research.db.execute("SELECT provenance_json FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    )
    assert provenance["tool_configuration"]["collection_protocol"] == "two-phase-v1"
    health = research.health()["latest_discovery"]
    assert health["id"] == pass_id and health["complete"] and health["phase_reached"] == "done"


@pytest.mark.parametrize("failure", ["error", "timeout", "truncated"])
def test_book_outcome_never_changes_the_summary(research, config, fixture_data, failure):
    handler, calls, _ = handler_for(fixture_data)
    baseline = Collector(research, reader_for(research, config, handler), config).run()
    assert baseline["status"] == "complete"
    reference_summary = stable(passes(research)[0][3])
    clock = Clock()
    before_books = []

    def failing(request):
        if request.url.path.endswith("/orderbook"):
            before_books.append(
                research.db.execute(
                    "SELECT * FROM discovery_passes ORDER BY id DESC LIMIT 1"
                ).fetchone()
            )
            if failure == "error":
                return response(503)
            if failure == "timeout":
                clock.value += 60
                return response(data={"orderbook_fp": {}})
            return response(body=b"{" + b"x" * (config.max_json_bytes + 10))
        return handler(request)

    result = Collector(research, reader_for(research, config, failing, clock), config).run()
    assert result["status"] == "partial", result
    assert result["books_this_pass"] == 0 and result["discovery_complete"] is True
    rows = passes(research)
    assert len(rows) == 2 and rows[1][3]["complete"] is True
    assert stable(rows[1][3]) == reference_summary  # A2
    after = research.db.execute(
        "SELECT * FROM discovery_passes WHERE id=?", (rows[1][0],)
    ).fetchone()
    assert before_books and all(tuple(b) == tuple(after) for b in before_books)  # A3
    assert research.verify_integrity() == []


# ----------------------------------------------------------------------------- B


@pytest.mark.parametrize(
    "bound,reason",
    [
        ("cursor", "cursor_repeated"),
        ("pages", "page_limit_exceeded"),
        ("markets", "market_limit_exceeded"),
    ],
)
def test_pagination_bounds_commit_incomplete_summary_without_books(
    research, config, fixture_data, bound, reason
):
    _, _, event = handler_for(fixture_data)
    n = 0

    def pages(request):
        nonlocal n
        n += 1
        return {"markets": event["markets"], "cursor": "same" if bound == "cursor" else str(n)}

    handler, calls, _ = handler_for(fixture_data, pages=pages)
    conf = replace(
        config,
        max_pages=2 if bound == "pages" else 100,
        max_markets=1 if bound == "markets" else 100,
    )
    result = Collector(research, reader_for(research, conf, handler), conf).run()
    assert result["status"] == "failed" and result["discovery_complete"] is False
    assert result["discovery_reasons"] == [reason]
    assert book_calls(calls) == [] and result["books"] == 0
    ((_, _, _, payload),) = passes(research)
    assert payload["complete"] is False and payload["reasons"] == [reason]
    assert payload["manifest"]["candidates_known"] is False
    assert payload["manifest"]["pages_complete"] is False
    assert payload["manifest"]["phase_reached"] == "documents"
    assert payload["counts"] == {"contracts": 0, "games": 0}
    assert payload["manifest"]["pages"]["page_count"] >= 1  # pages fetched are evidence
    assert archive.classify({"database": str(research.path)}, result, 1) == "failed_or_partial"
    assert research.verify_integrity() == []


@pytest.mark.parametrize("case", ["unavailable", "identity", "omitted", "membership"])
def test_event_failures_fail_closed_with_no_books(research, config, fixture_data, case):
    handler, calls, event = handler_for(fixture_data)
    original = json.loads(json.dumps(event))

    def broken(request):
        if "/events/" in request.url.path:
            if case == "unavailable":
                return response(503)
            data = json.loads(json.dumps(original))
            if case == "identity":
                data["event"]["event_ticker"] = "KXMLBGAME-OTHER"
            elif case == "omitted":
                for market in data["markets"]:
                    market["ticker"] += "-DIFFERENT"
            else:
                data["markets"] = data["markets"][:1]
            return response(data=data)
        if request.url.path.endswith("/markets"):
            return response(data={"markets": original["markets"], "cursor": ""})
        return handler(request)

    result = Collector(research, reader_for(research, config, broken), config).run()
    expected = {
        "unavailable": "event_unavailable",
        "identity": "event_identity_mismatch",
        "omitted": "eligibility_incomplete",
        "membership": "event_membership_ambiguous",
    }[case]
    assert result["status"] == "partial" and result["discovery_complete"] is False
    assert expected in result["discovery_reasons"]
    assert book_calls(calls) == [] and result["books"] == 0
    ((_, _, _, payload),) = passes(research)
    assert payload["complete"] is False and expected in payload["reasons"]
    assert payload["manifest"]["candidates_known"] is True
    assert payload["manifest"]["eligibility_complete"] is False
    assert len(payload["manifest"]["candidates"]) == 2
    if case == "omitted":
        assert payload["manifest"]["phase_reached"] == "events"
        assert payload["manifest"]["eligibility"] == []
    assert payload["reasons"] == [r for r in REASON_ORDER if r in payload["reasons"]]
    assert research.verify_integrity() == []


def test_empty_terminal_discovery_is_complete_and_distinct_from_failure(
    research, config, fixture_data
):
    handler, calls, _ = handler_for(
        fixture_data, pages=lambda request: {"markets": [], "cursor": ""}
    )
    result = Collector(research, reader_for(research, config, handler), config).run()
    assert result["status"] == "inconclusive" and result["errors"] == []
    assert result["discovery_complete"] is True and result["discovery_reasons"] == []
    ((_, _, _, payload),) = passes(research)
    assert payload["complete"] is True and payload["reasons"] == []
    assert payload["counts"] == {"contracts": 0, "games": 0}
    manifest = payload["manifest"]
    assert manifest["phase_reached"] == "done" and manifest["candidates_known"] is True
    assert manifest["candidates"] == [] and manifest["eligibility_complete"] is True
    assert manifest["pages"]["terminal"] is True
    assert book_calls(calls) == []
    assert (
        archive.classify({"database": str(research.path)}, result, 3)
        == "discovery_no_eligible_games"
    )


def test_all_candidates_ineligible_is_complete_with_zero_books(research, config, fixture_data):
    handler, calls, _ = handler_for(fixture_data, no_games=True)
    result = Collector(research, reader_for(research, config, handler), config).run()
    assert result["status"] == "inconclusive" and result["discovery_complete"] is True
    assert result["eligibility_reasons"] == {"unmapped_game": 2}
    ((_, _, _, payload),) = passes(research)
    assert payload["complete"] is True and payload["counts"] == {"contracts": 0, "games": 0}
    assert payload["manifest"]["reason_counts"] == {"unmapped_game": 2}
    assert len(payload["manifest"]["eligibility"]) == 2
    assert book_calls(calls) == []


# ------------------------------------------------------------- persistence gate


def test_store_refuses_books_without_a_complete_summary_for_this_invocation(
    research, config, fixture_data
):
    handler, calls, _ = handler_for(fixture_data)

    def no_event(request):
        return response(503) if "/events/" in request.url.path else handler(request)

    collector = Collector(research, reader_for(research, config, no_event), config)
    result = collector.run()
    assert result["discovery_complete"] is False
    run_id = result["run_id"]
    ((_, _, session, _),) = passes(research)
    url = KALSHI + "/markets/KXMLBGAME-26SEP271505HOUATH-HOU/orderbook"
    before = research.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    with pytest.raises(DiscoveryGateError, match="incomplete"):
        research.request(run_id, "book", session + ":X", url, {"depth": 0})
    with pytest.raises(DiscoveryGateError, match="another invocation"):
        research.request(run_id, "book", "other-session:X", url, {"depth": 0})
    # No summary at all for a fresh two-phase run.
    fresh_run = "fresh-two-phase-run"
    from vader_intelligence.provenance import RunProvenanceV1, source_hash

    research.start_run(
        RunProvenanceV1(
            fresh_run,
            "collect",
            source_hash(),
            {},
            tool_configuration={"collection_protocol": "two-phase-v1"},
        )
    )
    with pytest.raises(DiscoveryGateError, match="no committed"):
        research.request(fresh_run, "book", "s:X", url, {"depth": 0})
    assert research.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == before
    # Non-book stages are unaffected by the gate.
    research.request(fresh_run, "status", "s", KALSHI + "/exchange/status", {})


def test_bypassing_caller_cannot_retain_books_after_incomplete_discovery(
    research, config, fixture_data
):
    """A caller that ignores completeness is stopped at the Store, before any request."""
    handler, calls, event = handler_for(fixture_data)

    def no_event(request):
        return response(503) if "/events/" in request.url.path else handler(request)

    collector = Collector(research, reader_for(research, config, no_event), config)
    result = collector.run()
    assert result["discovery_complete"] is False
    ((_, run_id, session, _),) = passes(research)
    from vader_intelligence.scope import Eligibility
    from vader_intelligence.transport import Response

    market = event["markets"][0]
    fabricated = {
        "ticker": market["ticker"],
        "market": market,
        "event": event["event"],
        "event_markets": event["markets"],
        "event_response": Response("x", b"", collector.reader.now().isoformat()),
        "schedule_response": Response("y", b"", collector.reader.now().isoformat()),
        "check": Eligibility(True, "eligible", 824948, "2026-09-27T19:03:00+00:00"),
    }
    with pytest.raises(DiscoveryGateError, match="incomplete"):
        collector._books(run_id, session, [fabricated], [])
    assert book_calls(calls) == []
    assert (
        research.db.execute("SELECT COUNT(*) FROM requests WHERE stage='book'").fetchone()[0] == 0
    )


def test_summary_insert_failure_fails_the_run_without_books(
    research, config, fixture_data, monkeypatch
):
    handler, calls, _ = handler_for(fixture_data)

    def broken_append(table, payload):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(research, "append_forecast", broken_append)
    result = Collector(research, reader_for(research, config, handler), config).run()
    assert result["status"] == "failed" and result["discovery_complete"] is False
    assert result["discovery_pass_id"] is None and result["discovery_digest"] is None
    assert result["discovery_reasons"] == ["discovery_summary_not_committed"]
    assert any("discovery_summary_not_committed" in e for e in result["errors"])
    assert passes(research) == [] and book_calls(calls) == []
    # Raw discovery evidence is retained.
    assert research.db.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 7


# ----------------------------------------------------------------- D and E


CHILD = textwrap.dedent(
    """
    import json, os, signal, sys
    sys.path.insert(0, sys.argv[1])
    from conftest import response
    from test_collector import handler_for
    from test_transport import reader_for
    from vader_intelligence.collector import Collector
    from vader_intelligence.config import Config
    from vader_intelligence.storage import Store, writer_lock

    fixture = json.loads(open(sys.argv[3]).read())
    stage = sys.argv[4]
    config = Config(database=sys.argv[2], min_free_bytes=1)
    handler, _, _ = handler_for(fixture)

    def killing(request):
        if stage in request.url.path:
            os.kill(os.getpid(), signal.SIGKILL)
        return handler(request)

    with writer_lock(config.database):
        store = Store(config.database, min_free_bytes=1)
        Collector(store, reader_for(store, config, killing), config).run()
    """
)


def kill_child(research, stage, fixture_path):
    research.close()
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, str(TESTS), str(research.path), str(fixture_path), stage],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == -signal.SIGKILL, (proc.returncode, proc.stdout, proc.stderr)
    return Store(research.path, min_free_bytes=1)


def test_kill_before_summary_leaves_no_summary_and_resume_starts_a_new_pass(
    research, config, fixture_data
):
    store = kill_child(research, "/events/", TESTS / "fixtures/mlb.json")
    try:
        assert passes(store) == []
        run_id = store.db.execute("SELECT id FROM runs").fetchone()[0]
        assert store.db.execute("SELECT status FROM runs").fetchone()[0] == "running"
        states = dict(
            store.db.execute("SELECT state,COUNT(*) FROM fetches GROUP BY state").fetchall()
        )
        assert states == {"ok": 6, "pending": 1}  # status…schedule committed; event pending
        handler, calls, _ = handler_for(fixture_data)
        result = Collector(store, reader_for(store, config, handler), config).run(resume=run_id)
        assert result["status"] == "complete" and result["books_this_pass"] == 2
        rows = passes(store)
        assert len(rows) == 1 and rows[0][3]["phase_sequence"] == 1  # E3 after D1
        assert rows[0][3]["manifest"]["resumed"] is True
        # Resume refreshed the universe: only the refresh chain counts.
        assert all(
            p["request_key"].startswith("refresh:")
            for p in rows[0][3]["manifest"]["pages"]["chain"]
        )
        assert (
            store.db.execute("SELECT COUNT(*) FROM fetches WHERE state='interrupted'").fetchone()[0]
            == 1
        )
        assert store.verify_integrity() == []
    finally:
        store.close()


def test_kill_before_first_request_leaves_nothing_witnessable(research, config, fixture_data):
    store = kill_child(research, "/exchange/status", TESTS / "fixtures/mlb.json")
    try:
        assert passes(store) == []
        assert dict(store.db.execute("SELECT state,COUNT(*) FROM fetches GROUP BY state")) == {
            "pending": 1
        }
    finally:
        store.close()


def test_interrupt_after_summary_keeps_it_and_resume_is_phase_two(research, config, fixture_data):
    handler, _, _ = handler_for(fixture_data)
    books = 0

    def interrupt_first_book(request):
        nonlocal books
        if request.url.path.endswith("/orderbook"):
            books += 1
            raise KeyboardInterrupt()
        return handler(request)

    with pytest.raises(KeyboardInterrupt):
        Collector(research, reader_for(research, config, interrupt_first_book), config).run()
    run_id, status = research.db.execute("SELECT id,status FROM runs").fetchone()
    assert status == "interrupted"
    (first,) = passes(research)
    assert first[3]["complete"] is True and first[3]["phase_sequence"] == 1
    first_row = research.db.execute("SELECT * FROM discovery_passes WHERE id=1").fetchone()
    handler, calls, _ = handler_for(fixture_data, no_games=True)
    result = Collector(research, reader_for(research, config, handler), config).run(resume=run_id)
    assert result["status"] == "inconclusive" and result["books_this_pass"] == 0
    assert result["discovery_complete"] is True and result["eligible_contracts"] == 0
    rows = passes(research)
    assert [r[3]["phase_sequence"] for r in rows] == [1, 2]
    assert rows[0][2] != rows[1][2]  # new session_id
    assert tuple(research.db.execute("SELECT * FROM discovery_passes WHERE id=1").fetchone()) == (
        tuple(first_row)
    )
    assert (
        archive.classify({"database": str(research.path)}, result, 3)
        == "discovery_no_eligible_games"
    )
    assert research.verify_integrity() == []


def test_operator_interrupt_during_discovery_writes_incomplete_summary(
    research, config, fixture_data
):
    handler, calls, _ = handler_for(fixture_data)

    def interrupt_event(request):
        if "/events/" in request.url.path:
            raise KeyboardInterrupt()
        return handler(request)

    with pytest.raises(KeyboardInterrupt):
        Collector(research, reader_for(research, config, interrupt_event), config).run()
    ((_, _, _, payload),) = passes(research)
    assert payload["complete"] is False and payload["reasons"] == ["interrupted"]
    assert payload["manifest"]["phase_reached"] == "schedule"
    assert payload["manifest"]["candidates_known"] is True
    assert payload["manifest"]["incomplete_attempts"] == []  # interrupted attempt completed
    assert payload["counts"] == {"contracts": 0, "games": 0}
    assert book_calls(calls) == []
    row = research.db.execute("SELECT status,summary_json FROM runs").fetchone()
    assert row[0] == "interrupted"
    summary = json.loads(row[1])
    assert summary["discovery_complete"] is False and summary["discovery_pass_id"] == 1
    assert research.verify_integrity() == []


def test_legacy_protocol_run_cannot_be_resumed_on_schema_three(store, config, fixture_data):
    settlement_migrate(store)
    handler, _, _ = handler_for(fixture_data)
    books = 0

    def interrupt_second_book(request):
        nonlocal books
        if request.url.path.endswith("/orderbook"):
            books += 1
            if books == 2:
                raise KeyboardInterrupt()
        return handler(request)

    with pytest.raises(KeyboardInterrupt):
        Collector(store, reader_for(store, config, interrupt_second_book), config).run()
    run_id = store.db.execute("SELECT id FROM runs").fetchone()[0]
    forecast_migrate(store.db)
    with pytest.raises(ValueError, match="legacy collection loop"):
        Collector(store, reader_for(store, config, handler), config).run(resume=run_id)
    assert passes(store) == []


# ------------------------------------------------------------------------- H1


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_schemas_keep_the_interleaved_loop_and_write_no_research_rows(
    store, config, fixture_data, version
):
    if version == 2:
        settlement_migrate(store)
    handler, calls, _ = handler_for(fixture_data)
    result = Collector(store, reader_for(store, config, handler), config).run()
    assert result["status"] == "complete" and result["books"] == 2
    assert "discovery_pass_id" not in result
    provenance = json.loads(store.db.execute("SELECT provenance_json FROM runs").fetchone()[0])
    assert "collection_protocol" not in provenance["tool_configuration"]
    assert not store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='discovery_passes'"
    ).fetchone()
    # Interleaving is unchanged: the first book precedes the second eligibility decision.
    order = [c.url.path.rsplit("/", 1)[-1] for c in calls]
    assert order.index("orderbook") < len(order) - 1


# ------------------------------------------------------------------------- L1


@pytest.mark.parametrize(
    "stage,reason,phase,known",
    [
        ("/exchange/status", "status_unavailable", "none", False),
        ("/series/", "series_unavailable", "status", False),
        (".pdf", "document_unavailable", "series", False),
        ("/markets", "pages_incomplete", "documents", False),
        ("/schedule", "schedule_unavailable", "pages", True),
        ("/events/", "event_unavailable", "schedule", True),
    ],
)
def test_failure_points_are_recorded_honestly(
    research, config, fixture_data, stage, reason, phase, known
):
    handler, calls, _ = handler_for(fixture_data)

    def failing(request):
        return response(503) if stage in request.url.path else handler(request)

    result = Collector(research, reader_for(research, config, failing), config).run()
    assert result["discovery_complete"] is False and reason in result["discovery_reasons"]
    assert result["status"] in ("failed", "partial")
    ((_, _, _, payload),) = passes(research)
    manifest = payload["manifest"]
    assert payload["complete"] is False and payload["reasons"] == [reason]
    assert manifest["phase_reached"] == phase and manifest["candidates_known"] is known
    assert payload["eligibility_after_id"] == payload["eligibility_through_id"]
    assert payload["counts"] == {"contracts": 0, "games": 0}
    assert manifest["eligibility"] == []
    # The failed attempts are completed retrievals and therefore typed references.
    failed = {row[0] for row in research.db.execute("SELECT id FROM fetches WHERE state='error'")}
    assert failed and failed <= {r["key"]["id"] for r in payload["references"]}
    assert all(f["stage"] for f in manifest["failures"])
    assert book_calls(calls) == []
    assert journal.integrity(research.db)["integrity"] == "verified"


def test_series_identity_mismatch_is_its_own_reason(research, config, fixture_data):
    handler, calls, _ = handler_for(fixture_data)
    series = json.loads(fixture_data["responses"][0]["body"])

    def wrong_series(request):
        if "/series/" in request.url.path:
            return response(data={"series": dict(series["series"], ticker="KXNBA")})
        return handler(request)

    result = Collector(research, reader_for(research, config, wrong_series), config).run()
    assert result["status"] == "failed"
    ((_, _, _, payload),) = passes(research)
    assert payload["reasons"] == ["series_identity_mismatch"]
    assert payload["manifest"]["phase_reached"] == "status"
    assert book_calls(calls) == []


def test_access_denied_during_discovery_fails_closed(research, config, fixture_data):
    handler, calls, _ = handler_for(fixture_data)

    def forbidden(request):
        return response(403) if "/events/" in request.url.path else handler(request)

    result = Collector(research, reader_for(research, config, forbidden), config).run()
    assert result["status"] == "failed"
    ((_, _, _, payload),) = passes(research)
    assert payload["reasons"] == ["access_denied"] and payload["complete"] is False
    assert book_calls(calls) == []


# ----------------------------------------------------------------- Phase B


def test_phase_b_renews_stale_schedule_proof_outside_the_summary(research, config, fixture_data):
    handler, calls, _ = handler_for(fixture_data)
    clock = Clock()
    conf = replace(config, schedule_max_age=10)

    def slow(request):
        if "/events/" in request.url.path or request.url.path.endswith("/orderbook"):
            clock.value += 20
        return handler(request)

    result = Collector(research, reader_for(research, conf, slow, clock), conf).run()
    assert result["status"] == "complete", result
    assert result["books_this_pass"] == 2
    schedules = [c for c in calls if c.url.path.endswith("/schedule")]
    assert len(schedules) == 3  # initial, Phase A refresh, Phase B renewal
    ((_, _, _, payload),) = passes(research)
    assert payload["complete"] is True
    assert len(payload["manifest"]["eligibility"]) == 2
    assert payload["eligibility_through_id"] == 2
    rows = research.db.execute("SELECT id,eligible,schedule_fetch_id FROM eligibility").fetchall()
    assert len(rows) == 3 and rows[2][0] == 3 and rows[2][1] == 1  # renewed proof after bounds
    assert rows[2][2] != rows[1][2]
    assert result["eligibility_through_id"] == 3 and result["eligible_contracts"] == 2
    assert research.verify_integrity() == []


def test_cutoff_crossing_during_two_phase_discovery_is_inconclusive(research, config, fixture_data):
    handler, calls, _ = handler_for(fixture_data)
    clock = Clock()
    clock.origin = clock.origin.replace(hour=19, minute=2, second=50)
    collector = Collector(research, reader_for(research, config, handler, clock), config)
    original = collector.record_eligibility

    def slow_commit(*args):
        original(*args)
        clock.origin += timedelta(hours=1)

    collector.record_eligibility = slow_commit
    result = collector.run()
    assert result["discovery_complete"] and result["eligible_contracts"] == 1
    assert result["books_this_pass"] == 0 and result["status"] == "inconclusive"
    assert book_calls(calls) == []
    assert (
        archive.classify({"database": str(research.path)}, result, 3)
        == "inconclusive_with_eligible_games"
    )


def test_403_on_a_book_still_fails_the_run_after_the_summary(research, config, fixture_data):
    handler, calls, _ = handler_for(fixture_data)

    def forbidden(request):
        return response(403) if request.url.path.endswith("/orderbook") else handler(request)

    result = Collector(research, reader_for(research, config, forbidden), config).run()
    assert result["status"] == "failed" and result["discovery_complete"] is True
    ((_, _, _, payload),) = passes(research)
    assert payload["complete"] is True  # book outcome never edits the summary


# -------------------------------------------------------------------- lineage


def test_reader_lineage_flag(research, config, fixture_data):
    handler, _, _ = handler_for(fixture_data)
    assert reader_for(research, config, handler).live is False
    live = Reader(research, config)
    try:
        assert live.live is True
    finally:
        live.close()


def test_outcome_rejects_unknown_reasons():
    outcome = DiscoveryOutcome(kind="collect", resumed=False)
    with pytest.raises(ValueError):
        outcome.fail("made_up", "events", "k", "e")
    outcome.reached("pages")
    outcome.reached("status")  # never regresses
    assert outcome.phase_reached == "pages"


def test_collector_does_not_import_the_witness():
    code = (
        "import sys, vader_intelligence.collector; "
        "print(json.dumps(sorted(m for m in sys.modules if 'witness' in m or 'evidence' in m)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", "import json; " + code], capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout) == []


def test_live_check_summary_round_trips_through_cli_finish(research, config, fixture_data):
    """live-check reopens the store and rewrites the run summary; additive fields survive."""
    from vader_intelligence import cli

    handler, _, _ = handler_for(fixture_data)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(cli, "Reader", lambda store, config: reader_for(store, config, handler))
    try:
        research.close()
        result = cli.execute(cli.parser().parse_args(["--db", str(research.path), "live-check"]))
    finally:
        monkey.undo()
    assert result["status"] == "complete", result
    assert result["archive_integrity"] == "verified_after_reopen"
    assert result["discovery_complete"] is True and result["discovery_pass_id"] == 1
    research.db = sqlite3.connect(research.path)
    research.db.row_factory = sqlite3.Row
    assert json.loads(research.db.execute("SELECT summary_json FROM runs").fetchone()[0]) == result
    assert os.path.exists(str(research.path) + ".lockfile")
