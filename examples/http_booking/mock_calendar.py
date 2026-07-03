"""Tiny mock calendar API for the e2e demo. /slots has a free slot; /events_empty is an
empty calendar (the write never landed); /events_booked has the event (the write landed)."""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

ROUTES = {
    "/slots": {"items": [{"start": "2026-07-07T14:00:00", "free": True}]},
    "/events_empty": {"items": []},
    "/events_booked": {"items": [{"title": "Interview c-1", "start": "2026-07-07T14:00:00"}]},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = ROUTES.get(self.path)
        if payload is None:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    print(httpd.server_port, flush=True)
    httpd.serve_forever()
