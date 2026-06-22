#!/usr/bin/env python3
"""Live monitoring dashboard — http://HOST:8080/"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_PATH = Path(__file__).resolve().parent
if str(BASE_PATH) not in sys.path:
    sys.path.insert(0, str(BASE_PATH))

from monitoring.status import build_status_payload  # noqa: E402

PORT = int(os.getenv("MONITOR_PORT", "8080"))
HOST = os.getenv("MONITOR_HOST", "0.0.0.0")
TOKEN = os.getenv("MONITOR_TOKEN", "").strip()
REFRESH_SEC = int(os.getenv("MONITOR_REFRESH_SEC", "10"))

HTML_PATH = BASE_PATH / "monitoring" / "dashboard.html"


def _load_html() -> str:
    return HTML_PATH.read_text(encoding="utf-8").replace("__REFRESH_SEC__", str(REFRESH_SEC))


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "BettingArbMonitor/1.0"

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        header = self.headers.get("X-Monitor-Token", "")
        qs = parse_qs(urlparse(self.path).query)
        query = (qs.get("token") or [""])[0]
        return header == TOKEN or query == TOKEN

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authorized():
            self._send(401, b"Unauthorized", "text/plain")
            return

        path = urlparse(self.path).path
        if path == "/api/status":
            payload = build_status_payload()
            self._send(200, json.dumps(payload, default=str).encode(), "application/json")
        elif path in ("/", "/index.html"):
            self._send(200, _load_html().encode(), "text/html; charset=utf-8")
        elif path == "/health":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"Not found", "text/plain")

    def log_message(self, fmt, *args):
        if os.getenv("MONITOR_VERBOSE", "").lower() in ("1", "true", "yes"):
            super().log_message(fmt, *args)


def main():
    html = HTML_PATH
    if not html.exists():
        raise SystemExit(f"Missing dashboard template: {html}")
    server = HTTPServer((HOST, PORT), DashboardHandler)
    print(f"Monitor dashboard listening on http://{HOST}:{PORT}/")
    if TOKEN:
        print("Token auth enabled (MONITOR_TOKEN)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
