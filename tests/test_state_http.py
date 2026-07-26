"""HttpStateProvider against a real local HTTP server — the first provider that touches
reality. Failure semantics per CLAUDE.md invariant #4: transient trouble is StateUnavailable
(retryable), misconfiguration is a loud ValueError, and missing state is never silently
evaluated as empty."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from laufwise.state.base import StateUnavailable
from laufwise.state.composite import CompositeStateProvider
from laufwise.state.http import HttpStateProvider
from laufwise.state.memory import MemoryStateProvider

ROUTES = {
    "/candidates/c-1": {"id": "c-1", "status": "active", "docs": ["cv", "id_card"]},
    "/slots": {"items": [{"start": "2026-07-07T14:00:00", "free": True}]},
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/boom":
            self.send_response(500)
            self.end_headers()
            return
        if self.path == "/notjson":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html>not json</html>")
            return
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

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_query_returns_real_state(server):
    provider = HttpStateProvider(base_url=server)
    view = provider.query("candidate", params={"query": "/candidates/c-1"})
    assert view.exists
    assert view.get_field("status") == "active"


def test_extract_dotted_path_with_list_index(server):
    provider = HttpStateProvider(base_url=server)
    view = provider.query(
        "slot", params={"query": "/slots", "extract": "items.0.free"}
    )
    assert view.value is True


def test_template_vars_from_binding_params(server):
    provider = HttpStateProvider(base_url=server)
    view = provider.query(
        "candidate",
        params={"query": "/candidates/{cid}", "vars": {"cid": "c-1"}},
    )
    assert view.get_field("id") == "c-1"


def test_http_error_is_state_unavailable(server):
    provider = HttpStateProvider(base_url=server)
    with pytest.raises(StateUnavailable, match="HTTP 500"):
        provider.query("thing", params={"query": "/boom"})


def test_connection_refused_is_state_unavailable():
    provider = HttpStateProvider(base_url="http://127.0.0.1:1")  # nothing listens here
    with pytest.raises(StateUnavailable):
        provider.query("thing", params={"query": "/anything"})


def test_non_json_is_state_unavailable(server):
    provider = HttpStateProvider(base_url=server)
    with pytest.raises(StateUnavailable, match="non-JSON"):
        provider.query("thing", params={"query": "/notjson"})


def test_missing_query_is_config_error(server):
    provider = HttpStateProvider(base_url=server)
    with pytest.raises(ValueError, match="requires `query"):
        provider.query("thing", params={})


def test_missing_extract_path_fails_loud_not_empty(server):
    # A dangling extract path must never be evaluated as empty state a check could pass on.
    provider = HttpStateProvider(base_url=server)
    with pytest.raises(ValueError, match="not present in response"):
        provider.query(
            "candidate", params={"query": "/candidates/c-1", "extract": "nope.deeper"}
        )


def test_unresolved_template_var_is_config_error(server):
    provider = HttpStateProvider(base_url=server)
    with pytest.raises(ValueError, match="unresolved template variable"):
        provider.query("candidate", params={"query": "/candidates/{cid}", "vars": {"x": 1}})


# --- composite routing ----------------------------------------------------------------


def test_composite_routes_by_binding_provider(server):
    composite = CompositeStateProvider(
        {
            "memory": MemoryStateProvider({"notes": ["call at 14:00"]}),
            "http": HttpStateProvider(base_url=server),
        }
    )
    mem_view = composite.query("notes", params={"provider": "memory"})
    http_view = composite.query(
        "candidate", params={"provider": "http", "query": "/candidates/c-1"}
    )
    assert mem_view.count == 1
    assert http_view.get_field("status") == "active"


def test_composite_unregistered_provider_is_config_error():
    composite = CompositeStateProvider({"memory": MemoryStateProvider({})})
    # The message is built from implicitly-concatenated f-strings, so it renders with a
    # space — the old alternation carried a dead `\n` branch and read as an intentional regex.
    with pytest.raises(ValueError, match="no such StateProvider"):
        composite.query("x", params={"provider": "salesforce"})