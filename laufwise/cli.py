"""rh — the Laufwise CLI. Composition root: wires the seams together and renders results.

    rh run    <runbook.yaml> --case <case.json>
    rh test   <runbook.yaml>   (stub)
    rh replay <episode.jsonl>   (stub)
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console

from laufwise.adapters.base import SimulatedAdapter
from laufwise.approval.base import AutoApprovalGate
from laufwise.contract.evaluator import BuiltinEvaluator
from laufwise.engine.base import StepStatus
from laufwise.engine.local import LocalEngine
from laufwise.spec.loader import load_runbook
from laufwise.state.composite import CompositeStateProvider
from laufwise.state.http import HttpStateProvider
from laufwise.state.memory import MemoryStateProvider
from laufwise.trace.jsonl import JsonlTraceSink

console = Console()


def _next_episode(run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(run_dir.glob("episode_*.jsonl"))) + 1
    return run_dir / f"episode_{n:03d}.jsonl"


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
    spec = load_runbook(runbook)
    fixture = json.loads(Path(case_path).read_text(encoding="utf-8"))

    provider = MemoryStateProvider(fixture)
    # Bindings choose their provider; the case fixture's _params template http URLs.
    declared = {b.provider for b in spec.state.values()}
    state_provider = provider
    if declared - {"memory"}:
        providers = {"memory": provider}
        if "http" in declared:
            providers["http"] = HttpStateProvider(vars=provider.params)
        state_provider = CompositeStateProvider(providers)
    trace_path = _next_episode(Path(output_dir) / spec.runbook)
    trace = JsonlTraceSink(trace_path)
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
    console.print()

    raise SystemExit(1 if halted else 0)


@cli.command()
@click.argument("runbook", type=click.Path(exists=True, dir_okay=False))
def test(runbook: str) -> None:
    """Validate a runbook spec (stub — full case-matrix testing comes later)."""
    spec = load_runbook(runbook)
    console.print(f"[green]ok[/green] {spec.runbook}: {len(spec.steps)} steps, schema valid")


@cli.command()
@click.argument("episode", type=click.Path(exists=True, dir_okay=False))
def replay(episode: str) -> None:
    """Replay an episode trace (stub — deterministic replay driver comes later)."""
    console.print("[yellow]rh replay is not yet implemented (v0)[/yellow]")
    console.print(f"episode: {episode}")