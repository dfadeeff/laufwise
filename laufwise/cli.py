"""rh — the Laufwise CLI. Composition root: wires the seams together and renders results.

    rh run    <runbook.yaml> --case <case.json>
    rh test   <runbook.yaml>            (static: schema + every check expression)
    rh replay <episode.jsonl>   (stub)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import click
from pydantic import ValidationError
from rich.console import Console

from laufwise.adapters.base import SimulatedAdapter, StubAdapter
from laufwise.approval.base import AutoApprovalGate
from laufwise.contract.evaluator import BuiltinEvaluator
from laufwise.engine.base import StepStatus
from laufwise.engine.local import LocalEngine
from laufwise.spec.loader import RunbookValidationError, load_runbook
from laufwise.state.base import StateProvider
from laufwise.state.composite import CompositeStateProvider
from laufwise.state.http import HttpStateProvider
from laufwise.state.memory import MemoryStateProvider
from laufwise.trace.jsonl import JsonlTraceSink

console = Console()


def _next_episode(run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(run_dir.glob("episode_*.jsonl"))) + 1
    return run_dir / f"episode_{n:03d}.jsonl"


def _run_context(spec) -> dict:
    """Identity stamped onto every trace event. Minted here, at the composition root: the
    engine stays a deterministic function of (spec, state), so replay re-drives it and gets
    the same rulings, while the run's wall-clock identity lives with the recorder."""
    return {
        "run_id": uuid.uuid4().hex[:16],
        "runbook": spec.runbook,
        "runbook_version": spec.version,
    }


def _load_or_exit(runbook: str):
    """Load a runbook, reporting an invalid one as a clean CLI failure.

    A broken check is a config error, so it deserves the same treatment as a bad flag — a
    readable message and exit 1, not a traceback. Every entry point loads through here so
    no runbook reaches an engine without its checks having been validated first.
    """
    try:
        return load_runbook(runbook)
    except (RunbookValidationError, ValidationError) as exc:
        kind = "" if isinstance(exc, RunbookValidationError) else "  (schema)"
        console.print()
        console.print("  [bold white on magenta] INVALID [/bold white on magenta]  "
                      f"[bold]{Path(runbook).name}[/bold]{kind}")
        for line in str(exc).splitlines():
            console.print(f"    {line}")
        console.print()
        raise SystemExit(1) from None


def _build_state_provider(spec, fixture: dict) -> tuple[MemoryStateProvider, StateProvider]:
    """Wire the state seam: memory always; other declared providers routed via composite.
    Returns (memory_provider, state_provider) — the memory provider is also the demo
    adapter's write target, so both are needed at the composition root."""
    provider = MemoryStateProvider(fixture)
    declared = {b.provider for b in spec.state.values()}
    state_provider: StateProvider = provider
    if declared - {"memory"}:
        providers: dict[str, StateProvider] = {"memory": provider}
        if "http" in declared:
            providers["http"] = HttpStateProvider(vars=provider.params)
        state_provider = CompositeStateProvider(providers)
    return provider, state_provider


@click.group()
@click.version_option(package_name="laufwise", prog_name="rh")
def cli() -> None:
    """Laufwise — force agents to execute inside a process contract."""


@cli.command()
@click.argument("runbook", type=click.Path(exists=True, dir_okay=False))
@click.option("--case", "case_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--output-dir", default="runs", show_default=True)
def run(runbook: str, case_path: str, output_dir: str) -> None:
    """Run a runbook against a case fixture."""
    spec = _load_or_exit(runbook)
    fixture = json.loads(Path(case_path).read_text(encoding="utf-8"))

    # Bindings choose their provider; the case fixture's _params template http URLs.
    provider, state_provider = _build_state_provider(spec, fixture)
    trace_path = _next_episode(Path(output_dir) / spec.runbook)
    trace = JsonlTraceSink(trace_path, context=_run_context(spec))
    engine = LocalEngine(
        provider=state_provider,
        evaluator=BuiltinEvaluator(),
        trace=trace,
        approval=AutoApprovalGate(),
        adapter=SimulatedAdapter(provider),
    )

    try:
        results = engine.run(spec)
    finally:
        trace.close()

    console.print()
    halted = False
    for r in results:
        if r.status is StepStatus.OK:
            console.print(f"  [green]✓[/green] {r.step_id}")
        elif r.status is StepStatus.BLOCK:
            halted = True
            console.print()
            console.print(f"  [bold white on red] BLOCK [/bold white on red]  [bold]{r.step_id}[/bold]")
            console.print(f"    precondition failed: [yellow]{r.expr}[/yellow]")
            console.print(f"    reason: {r.reason}")
            if r.blocked_tool:
                console.print(f"    blocked tool: [red]{r.blocked_tool}[/red]")
            console.print(f"    trace: [dim]{trace_path}[/dim]")
        elif r.status is StepStatus.REJECT:
            halted = True
            console.print()
            console.print(f"  [bold white on dark_orange] REJECT [/bold white on dark_orange]  [bold]{r.step_id}[/bold]")
            console.print(f"    postcondition failed: [yellow]{r.expr}[/yellow]")
            console.print(f"    reason: {r.reason}")
            console.print(f"    trace: [dim]{trace_path}[/dim]")
        elif r.status is StepStatus.STATE_UNAVAILABLE:
            halted = True
            console.print()
            console.print(f"  [bold black on yellow] STATE UNAVAILABLE [/bold black on yellow]  [bold]{r.step_id}[/bold]")
            console.print(f"    could not resolve state: {r.reason}")
            console.print(f"    trace: [dim]{trace_path}[/dim]")
        elif r.status is StepStatus.CHECK_ERROR:
            halted = True
            console.print()
            console.print(f"  [bold white on magenta] CHECK ERROR [/bold white on magenta]  [bold]{r.step_id}[/bold]")
            console.print(f"    broken check: [yellow]{r.expr}[/yellow]")
            console.print(f"    {r.reason}")
            console.print("    [dim]the outcome is UNVERIFIED — this is a config error, fix the check[/dim]")
            console.print(f"    trace: [dim]{trace_path}[/dim]")
    console.print()

    raise SystemExit(1 if halted else 0)


@cli.command()
@click.argument("runbook", type=click.Path(exists=True, dir_okay=False))
@click.option("--case", "case_path", type=click.Path(exists=True, dir_okay=False),
              help="Optional case fixture (memory bindings + _params for http URLs).")
@click.option("--wrap", "wrap_commands", multiple=True,
              help="Downstream MCP server command to wrap (repeatable).")
@click.option("--output-dir", default="runs", show_default=True)
def serve(runbook: str, case_path: str | None, wrap_commands: tuple[str, ...], output_dir: str) -> None:
    """Serve a runbook as an MCP step session wrapping downstream MCP servers (§1.6).

    The connected agent calls begin_step / complete_step; between them only the current
    step's allowlisted downstream tools are visible and forwarded.
    """
    try:
        import asyncio

        from laufwise.mcp.server import serve_stdio
    except ImportError as exc:
        raise click.ClickException(
            f'rh serve requires the mcp extra: pip install "laufwise[mcp]" ({exc})'
        ) from exc

    spec = _load_or_exit(runbook)
    fixture = json.loads(Path(case_path).read_text(encoding="utf-8")) if case_path else {}
    _, state_provider = _build_state_provider(spec, fixture)

    trace_path = _next_episode(Path(output_dir) / spec.runbook)
    trace = JsonlTraceSink(trace_path, context=_run_context(spec))
    engine = LocalEngine(
        provider=state_provider,
        evaluator=BuiltinEvaluator(),
        trace=trace,
        approval=AutoApprovalGate(),
        adapter=StubAdapter(),  # session mode: the agent acts via the scoped proxy
    )
    try:
        asyncio.run(serve_stdio(spec, engine, list(wrap_commands)))
    finally:
        trace.close()


@cli.command()
@click.argument("runbook", type=click.Path(exists=True, dir_okay=False))
def test(runbook: str) -> None:
    """Validate a runbook: schema, goto targets, and every check expression.

    Static only — no state is queried and no step runs. It answers "will this runbook's
    checks actually evaluate?", which used to be answerable only by running it and finding
    out after a tool had already fired.
    """
    spec = _load_or_exit(runbook)
    checks = sum(len(s.preconditions) + len(s.postconditions) for s in spec.steps)
    console.print(
        f"[green]ok[/green] {spec.runbook}: {len(spec.steps)} steps, "
        f"{checks} checks validated against {len(spec.state)} state bindings"
    )


@cli.command()
@click.argument("episode", type=click.Path(exists=True, dir_okay=False))
def replay(episode: str) -> None:
    """Replay an episode trace (stub — deterministic replay driver comes later)."""
    console.print("[yellow]rh replay is not yet implemented (v0)[/yellow]")
    console.print(f"episode: {episode}")