"""Loopback HTTP server for the Command Center: static page, JSON read API, and one
mutation route (`POST /api/commands`) that only records durable commands.

Protection of the mutation route (spec §4): same-origin `Origin`, JSON content type, the
per-process session token in `X-Vader-Session`, and a client-generated `command_id`.
GET handlers never write. No request value is used as a path or an argv. Responses carry
a strict CSP; untrusted text is returned as JSON strings for the page to render inert.
"""

import json
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import projections
from .config import PANE_LINES, PREVIEW_BYTES, CenterConfig
from .readers import now_iso
from .store import CenterStore, CommandConflict, new_id

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
MAX_BODY = 256 * 1024  # a maintenance brief carries a patch (coding.MAX_PATCH_BYTES)
RUN_ID = re.compile(r"[0-9a-f-]{8,64}\Z")
WORKER_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
COMMAND_KINDS = ("start", "pause", "resume", "cancel")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; connect-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}


class CenterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg: CenterConfig, *, pane_reader=None, now=None):
        super().__init__((cfg.host, cfg.port), Handler)
        self.cfg = cfg
        self.session_token = secrets.token_urlsafe(32)
        self.started_at = now_iso()
        self.pane_reader = pane_reader  # (cfg, run_id, worker_id) -> projection; optional
        self.now = now  # injectable clock for tests
        self.latencies = []  # recent /api/overview latencies (seconds), bounded
        self.lock = threading.Lock()

    @property
    def origin(self):
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def allowed_hosts(self):
        port = self.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    @property
    def allowed_origins(self):
        return {"http://" + h for h in self.allowed_hosts}

    def record_latency(self, seconds):
        with self.lock:
            self.latencies.append(seconds)
            del self.latencies[:-500]

    def latency_summary(self):
        with self.lock:
            values = sorted(self.latencies)
        if not values:
            return {"samples": 0, "p50_ms": None, "p95_ms": None}
        return {
            "samples": len(values),
            "p50_ms": round(values[len(values) // 2] * 1000, 1),
            "p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))] * 1000, 1),
        }


class Handler(BaseHTTPRequestHandler):
    server: CenterServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # quiet by default; the page polls every 5 s
        pass

    # Helpers ---------------------------------------------------------------------------

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, projection, status=HTTPStatus.OK):
        self._send(status, {"ok": projection.get("state") != "unavailable", "data": projection})

    def _error(self, status, message):
        if status in (HTTPStatus.FORBIDDEN, HTTPStatus.BAD_REQUEST):
            self.close_connection = True  # never parse a refused body as the next request
        self._send(status, {"ok": False, "error": message, "generated_at": now_iso()})

    def _host_ok(self):
        """DNS-rebinding guard: only the loopback host names this server is bound for."""
        return (self.headers.get("Host") or "") in self.server.allowed_hosts

    def _now(self):
        return self.server.now() if callable(self.server.now) else None

    # GET -------------------------------------------------------------------------------

    def do_GET(self):
        cfg = self.server.cfg
        if not self._host_ok():
            return self._error(HTTPStatus.BAD_REQUEST, "unexpected host")
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        query = parse_qs(url.query)
        try:
            if not parts or parts == ["index.html"]:
                return self._static("index.html")
            if parts[0] == "static" and len(parts) == 2:
                return self._static(parts[1])
            if parts[0] != "api":
                return self._error(HTTPStatus.NOT_FOUND, "not found")
            route = parts[1:]
            if route == ["session"]:
                return self._send(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "token": self.server.session_token,
                        "refresh_seconds": cfg.refresh_seconds,
                        "activity_page": cfg.activity_page,
                        "server_started_at": self.server.started_at,
                        "labels": cfg.labels,
                        "generated_at": now_iso(),
                    },
                )
            if route == ["overview"]:
                started = time.perf_counter()
                data = projections.overview(cfg, self._now())
                self.server.record_latency(time.perf_counter() - started)
                data["latency"] = self.server.latency_summary()
                return self._json(data)
            if route == ["games"]:
                return self._json(projections.games(cfg, self._now()))
            if len(route) == 2 and route[0] == "games":
                if not re.fullmatch(r"[0-9]{1,12}", route[1]):
                    return self._error(HTTPStatus.BAD_REQUEST, "game id must be numeric")
                return self._json(projections.game(cfg, int(route[1]), self._now()))
            if route == ["farms"]:
                return self._json(projections.farms(cfg, self._now()))
            if len(route) >= 2 and route[0] == "farms":
                run_id = route[1]
                if not RUN_ID.fullmatch(run_id):
                    return self._error(HTTPStatus.BAD_REQUEST, "bad run id")
                if len(route) == 2:
                    return self._json(projections.farm(cfg, run_id, self._now()))
                if route[2:] == ["activity"]:
                    page = query.get("page", ["1"])[0]
                    if not re.fullmatch(r"[0-9]{1,6}", page):
                        return self._error(HTTPStatus.BAD_REQUEST, "bad page")
                    return self._json(projections.activity(cfg, run_id, int(page), self._now()))
                if len(route) == 4 and route[2] == "artifacts" and HEX64.fullmatch(route[3]):
                    return self._json(
                        projections.artifact_preview(
                            cfg, run_id, route[3], PREVIEW_BYTES, self._now()
                        )
                    )
                if len(route) == 5 and route[2] == "workers" and route[4] == "pane":
                    if not WORKER_ID.fullmatch(route[3]):
                        return self._error(HTTPStatus.BAD_REQUEST, "bad worker id")
                    if self.server.pane_reader is None:
                        return self._json(
                            projections.unavailable("pane capture not configured", self._now())
                        )
                    return self._json(self.server.pane_reader(cfg, run_id, route[3], PANE_LINES))
            if route == ["engineering"]:
                return self._json(projections.engineering(cfg, self._now()))
            if (
                len(route) == 2
                and route[0] == "commands"
                and re.fullmatch(r"[A-Za-z0-9-]{8,64}", route[1])
            ):
                return self._json(_command_receipt(cfg, route[1]))
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        except Exception as exc:  # never a traceback to the browser
            return self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"internal error: {type(exc).__name__}"
            )

    def _static(self, name):
        if name not in STATIC_FILES:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        data = (STATIC_DIR / name).read_bytes()
        return self._send(HTTPStatus.OK, data, STATIC_FILES[name])

    # POST ------------------------------------------------------------------------------

    def do_POST(self):
        cfg = self.server.cfg
        if not self._host_ok():
            return self._error(HTTPStatus.BAD_REQUEST, "unexpected host")
        if urlsplit(self.path).path != "/api/commands":
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        origin = self.headers.get("Origin")
        if origin is None or origin.rstrip("/") not in self.server.allowed_origins:
            return self._error(HTTPStatus.FORBIDDEN, "origin mismatch")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._error(HTTPStatus.FORBIDDEN, "json content type required")
        token = self.headers.get("X-Vader-Session") or ""
        if not secrets.compare_digest(token, self.server.session_token):
            return self._error(HTTPStatus.FORBIDDEN, "session token mismatch")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return self._error(HTTPStatus.BAD_REQUEST, "bad content length")
        if not 0 < length <= MAX_BODY:
            return self._error(HTTPStatus.BAD_REQUEST, "body size out of bounds")
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, RecursionError):
            return self._error(HTTPStatus.BAD_REQUEST, "invalid json")
        if not isinstance(body, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "command must be an object")
        command_id = body.get("command_id")
        kind = body.get("kind")
        run_id = body.get("run_id")
        payload = body.get("payload") or {}
        if not isinstance(command_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", command_id):
            return self._error(HTTPStatus.BAD_REQUEST, "command_id must be a client-generated id")
        if kind not in COMMAND_KINDS:
            return self._error(HTTPStatus.BAD_REQUEST, "unknown command kind")
        if run_id is not None and (not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id)):
            return self._error(HTTPStatus.BAD_REQUEST, "bad run id")
        if not isinstance(payload, dict) or len(json.dumps(payload)) > 220 * 1024:
            return self._error(HTTPStatus.BAD_REQUEST, "payload must be a bounded object")
        try:
            with CenterStore(cfg.root) as center:
                stored = center.receive_command(command_id, kind, run_id, payload)
                receipt = center.command(command_id)
        except CommandConflict:
            return self._error(HTTPStatus.CONFLICT, "command id reused with different content")
        except RuntimeError as exc:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        return self._send(
            HTTPStatus.ACCEPTED if stored else HTTPStatus.OK,
            {"ok": True, "stored": stored, "receipt": _receipt(receipt), "generated_at": now_iso()},
        )

    def do_OPTIONS(self):  # no CORS preflight is ever answered
        return self._error(HTTPStatus.FORBIDDEN, "cross-origin requests are not served")


def _receipt(row):
    from .store import iso

    return {
        "command_id": row["command_id"],
        "kind": row["kind"],
        "run_id": row["run_id"],
        "state": row["state"],
        "reason": row["reason"],
        "received_at": iso(row["received_at"]),
        "applied_at": iso(row["applied_at"]),
    }


def _command_receipt(cfg, command_id):
    try:
        with CenterStore(cfg.root, readonly=True) as center:
            row = center.command(command_id)
            events = center.command_events(command_id) if row else []
    except FileNotFoundError:
        row = None
        events = []
    if row is None:
        return {
            "state": "empty",
            "reason": "unknown command",
            "generated_at": now_iso(),
            "source_time": None,
        }
    from .store import iso

    return {
        "state": "ok",
        "generated_at": now_iso(),
        "source_time": iso(row["applied_at"] or row["received_at"]),
        "receipt": _receipt(row),
        "events": [
            {"kind": e["kind"], "at": iso(e["at"]), "detail": json.loads(e["detail_json"])}
            for e in events
        ],
    }


def serve(cfg: CenterConfig, *, pane_reader=None, ready=None):
    """Run the server until interrupted; `ready` (a threading.Event) is set once bound."""
    server = CenterServer(cfg, pane_reader=pane_reader)
    if ready is not None:
        ready.set()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return server


def new_command_id():
    return new_id()
