"""The loopback server: read-only GETs, security headers, bounded routes and the
protected command intake. Runs a real server on an ephemeral loopback port."""

import hashlib
import http.client
import json
import threading
import time

import pytest

from vader_intelligence.command_center.config import CenterConfig
from vader_intelligence.command_center.server import CenterServer
from vader_intelligence.command_center.store import CenterStore


@pytest.fixture
def server(cfg, free_port):
    cfg = CenterConfig(**{**cfg.__dict__, "port": free_port})
    srv = CenterServer(cfg)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    data = None if body is None else json.dumps(body).encode()
    conn.request(method, path, body=data, headers=headers or {})
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw
    return response.status, dict(response.getheaders()), payload


def digest_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reads_are_read_only_bounded_and_carry_security_headers(server, cfg):
    before = digest_file(cfg.archive)
    for path in (
        "/",
        "/static/app.js",
        "/static/style.css",
        "/api/session",
        "/api/overview",
        "/api/games",
        "/api/games/824948",
        "/api/farms",
        "/api/engineering",
        "/api/farms/11111111-1111-4111-8111-111111111111",
        "/api/farms/11111111-1111-4111-8111-111111111111/activity?page=2",
    ):
        status, headers, payload = call(server, "GET", path)
        assert status == 200, path
        assert headers["Content-Security-Policy"].startswith("default-src 'none'")
        assert (
            headers["X-Content-Type-Options"] == "nosniff"
            and headers["Cache-Control"] == "no-store"
        )
        assert int(headers["Content-Length"]) < 2_000_000
    assert digest_file(cfg.archive) == before
    assert not (cfg.root / "command-center.sqlite3").exists()  # a GET never creates the store
    status, _, payload = call(server, "GET", "/api/games/824948")
    assert payload["ok"] and payload["data"]["pins"]["market_tickers"] == [
        "KXMLBGAME-26SEP271505HOUATH-HOU"
    ]
    assert payload["data"]["generated_at"] and "source_time" in payload["data"]


def test_unknown_and_malformed_routes_fail_closed(server):
    for path in (
        "/api/nope",
        "/static/../pyproject.toml",
        "/static/evil.js",
        "/api/games/abc",
        "/api/farms/..%2F..",
        "/api/farms/x/artifacts/zz",
        "/api/farms/11111111-1111-4111-8111-111111111111/workers/%2Fetc/pane",
    ):
        status, _, payload = call(server, "GET", path)
        assert status in (400, 404), path
        assert payload["ok"] is False
    status, _, payload = call(
        server, "GET", "/api/farms/11111111-1111-4111-8111-111111111111/workers/w1/pane"
    )
    assert status == 200 and payload["data"]["state"] == "unavailable"  # no pane reader configured
    status, _, _ = call(
        server, "OPTIONS", "/api/commands", headers={"Origin": "http://evil.example"}
    )
    assert status == 403


def test_command_intake_requires_origin_token_and_json(server, cfg):
    origin = server.origin
    token = call(server, "GET", "/api/session")[2]["token"]
    body = {
        "command_id": "aaaaaaaa-0000-4000-8000-000000000001",
        "kind": "start",
        "run_id": None,
        "payload": {"game_id": 824948},
    }
    good = {"Origin": origin, "Content-Type": "application/json", "X-Vader-Session": token}
    for bad in (
        {**good, "Origin": "http://evil.example"},
        {k: v for k, v in good.items() if k != "Origin"},
        {**good, "X-Vader-Session": "wrong"},
        {**good, "Content-Type": "text/plain"},
    ):
        status, _, payload = call(server, "POST", "/api/commands", body, bad)
        assert status == 403, bad
    assert not (cfg.root / "command-center.sqlite3").exists()  # refused before any write
    status, _, payload = call(server, "POST", "/api/commands", body, good)
    assert status == 202 and payload["stored"] and payload["receipt"]["state"] == "accepted"
    status, _, payload = call(server, "POST", "/api/commands", body, good)  # duplicate click
    assert status == 200 and payload["stored"] is False
    status, _, payload = call(
        server, "POST", "/api/commands", {**body, "payload": {"game_id": 1}}, good
    )
    assert status == 409
    with CenterStore(cfg.root, readonly=True) as center:
        assert len(center.pending_commands()) == 1
        assert [e["kind"] for e in center.command_events(body["command_id"])] == ["accepted"]
    status, _, payload = call(server, "GET", "/api/commands/" + body["command_id"])
    assert (
        payload["data"]["receipt"]["state"] == "accepted"
        and payload["data"]["events"][0]["kind"] == "accepted"
    )
    for bad_body in (
        {"command_id": "x", "kind": "start"},
        {"command_id": body["command_id"], "kind": "detonate"},
        {"command_id": "bbbbbbbb-0000-4000-8000-000000000002", "kind": "cancel", "run_id": "../x"},
        [],
    ):
        status, _, _ = call(server, "POST", "/api/commands", bad_body, good)
        assert status == 400, bad_body
    status, _, _ = call(server, "POST", "/api/other", body, good)
    assert status == 404


def test_overview_latency_is_measured(server):
    samples = []
    for _ in range(30):
        started = time.perf_counter()
        status, _, payload = call(server, "GET", "/api/overview")
        samples.append(time.perf_counter() - started)
        assert status == 200
    summary = payload["data"]["latency"]
    assert summary["samples"] == 30 and summary["p95_ms"] is not None
    samples.sort()
    assert samples[int(len(samples) * 0.95) - 1] < 1.0  # the one-second p95 target, measured here
