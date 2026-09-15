"""Blue team, dependency-free for a no-build Cloud Run source deploy.

Same MockAegisTarget logic as everywhere else in this repo -- built on
Python's stdlib http.server instead of Flask, since this deploy path skips
the build/pip-install step entirely (deployed via the GCP MCP connector's
deploy_service_from_file_contents, which places raw source files into a
runtime base image with no build phase).
"""
import http.server
import json
import os
import socketserver
import sys

from aegis_redteam.llm import ChatMessage
from aegis_redteam.target import MockAegisTarget

target = MockAegisTarget()
BLUE_TEAM_TOKEN = os.environ.get("BLUE_TEAM_TOKEN", "")
PORT = int(os.environ.get("PORT", 8080))


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return bool(BLUE_TEAM_TOKEN) and self.headers.get("Authorization", "") == f"Bearer {BLUE_TEAM_TOKEN}"

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/chat":
            self._send_json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send_json(400, {"error": "invalid json"})
            return
        history = [ChatMessage(role=m["role"], content=m["content"]) for m in body.get("messages", [])]
        if not history:
            self._send_json(400, {"error": "messages required"})
            return
        self._send_json(200, {"reply": target.respond(history)})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % (fmt % args))


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
