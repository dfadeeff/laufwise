"""CompositeStateProvider — routes each binding to the provider it declares.

One runbook may ground different bindings in different systems of record (candidate in the
ATS via http, working notes in memory). The engine still sees a single StateProvider; this
router dispatches on the binding's declared `provider` name. An unregistered provider name is
a config error (halt loud), not StateUnavailable — nothing transient about it.
"""

from __future__ import annotations

from laufwise.state.base import StateProvider, StateView


class CompositeStateProvider:
    def __init__(self, providers: dict[str, StateProvider], default: str = "memory") -> None:
        self.providers = providers
        self.default = default

    def query(self, name: str, params: dict | None = None) -> StateView:
        provider_name = (params or {}).get("provider") or self.default
        provider = self.providers.get(provider_name)
        if provider is None:
            raise ValueError(
                f"binding {name!r} declares provider {provider_name!r}, but no such "
                f"StateProvider is registered (have {sorted(self.providers)})"
            )
        return provider.query(name, params)