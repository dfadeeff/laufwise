"""HttpStateProvider — the first real StateProvider: a generic REST/JSON source.

A binding's `query` is a URL (absolute, or joined onto base_url), optionally templated with
{placeholders} filled from provider-level vars merged with the binding's `params`. `extract`
is a dotted path into the JSON response (list indices allowed: "items.0.status").

Failure semantics follow CLAUDE.md invariant #4:
  - network error / timeout / non-2xx / non-JSON  -> StateUnavailable (temporarily
    unavailable; retryable, and step.verify re-checks cover transient blips)
  - missing `query`, unresolved template var, or an extract path absent from the response
    -> ValueError (config error: halt loud, never silently evaluated as empty state).
    Model absence in the response shape (null, empty list), not by dangling paths.

Stdlib only — no new dependencies for the core package.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from laufwise.state.base import StateUnavailable, StateView


class HttpStateProvider:
    def __init__(
        self,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        timeout_s: float = 10.0,
        vars: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self.headers = headers or {}
        self.timeout_s = timeout_s
        self.vars = vars or {}

    # --- StateProvider protocol -------------------------------------------
    def query(self, name: str, params: dict | None = None) -> StateView:
        cfg = params or {}
        url = cfg.get("query")
        if not url:
            raise ValueError(
                f"binding {name!r}: http provider requires `query: <url>` in the binding"
            )

        template_vars = {**self.vars, **(cfg.get("vars") or {})}
        if template_vars:
            try:
                url = url.format(**template_vars)
            except (KeyError, IndexError) as exc:
                raise ValueError(
                    f"binding {name!r}: unresolved template variable {exc} in query {url!r}"
                ) from exc

        if not url.startswith(("http://", "https://")):
            url = self.base_url.rstrip("/") + "/" + url.lstrip("/")

        request = urllib.request.Request(
            url, headers={"Accept": "application/json", **self.headers}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise StateUnavailable(f"binding {name!r}: {url} -> HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise StateUnavailable(f"binding {name!r}: {url} -> {exc}") from exc

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise StateUnavailable(f"binding {name!r}: non-JSON response from {url}") from exc

        extract = cfg.get("extract")
        if extract:
            data = _extract(name, data, extract)
        return StateView(data)


def _extract(name: str, data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise ValueError(
                f"binding {name!r}: extract path {path!r} not present in response "
                f"(stopped at {part!r}) — refusing to evaluate missing state as empty"
            )
    return current