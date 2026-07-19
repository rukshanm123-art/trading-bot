"""Read-only monitoring HTTP endpoint (localhost by default).

GET /health/live   -> liveness
GET /health/ready  -> readiness (component breakdown)
GET /health        -> full component health JSON
GET /metrics       -> Prometheus text format

No control actions are exposed over HTTP — control is CLI-only, so there is
no CSRF/auth surface for state changes. If MONITORING_TOKEN is set, all
endpoints require the 'Authorization: Bearer <token>' header.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from trading_bot.monitoring.health import HEALTH
from trading_bot.monitoring.metrics import METRICS

log = logging.getLogger(__name__)


def _make_handler(token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "trading-bot-monitor"

        def log_message(self, fmt: str, *args) -> None:  # route to app logging
            log.debug("monitor: " + fmt, *args)

        def _authorized(self) -> bool:
            if not token:
                return True
            header = self.headers.get("Authorization", "")
            return header == f"Bearer {token}"

        def _send(self, code: int, body: str, content_type: str = "application/json") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            if not self._authorized():
                self._send(401, '{"error": "unauthorized"}')
                return
            if self.path == "/health/live":
                ok = HEALTH.live()
                self._send(200 if ok else 503, json.dumps({"live": ok}))
            elif self.path == "/health/ready":
                ok = HEALTH.ready()
                self._send(200 if ok else 503, json.dumps({"ready": ok}))
            elif self.path == "/health":
                self._send(200, json.dumps(HEALTH.snapshot(), indent=2))
            elif self.path == "/metrics":
                self._send(200, METRICS.render_prometheus(), "text/plain; version=0.0.4")
            else:
                self._send(404, '{"error": "not found"}')

        def do_POST(self) -> None:  # noqa: N802 - stdlib API
            self._send(405, '{"error": "read-only endpoint; control is CLI-only"}')

    return Handler


class MonitoringServer:
    def __init__(self, host: str, port: int, token: str | None = None) -> None:
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._token = token

    def start(self) -> None:
        self._server = ThreadingHTTPServer((self.host, self.port), _make_handler(self._token))
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="monitoring", daemon=True
        )
        self._thread.start()
        log.info("monitoring endpoint on http://%s:%s (read-only)", self.host, self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
