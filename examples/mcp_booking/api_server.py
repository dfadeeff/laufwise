"""Mock calendar REST API with a write path. GET /slots, GET /events, POST /events."""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {
    "slots": {"items": [{"start": "2026-07-08T14:00:00", "free": True}]},
    "events": {"items": []},
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        key = self.path.strip("/")
        if key in STATE:
            self._send(STATE[key])
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.strip("/") != "events":
            self._send({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        event = json.loads(self.rfile.read(length))
        STATE["events"]["items"].append(event)
        self._send({"created": event}, 201)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1])
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
