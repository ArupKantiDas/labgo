"""LabGo CLI — Stage 1: ingestion and ground-truth extraction (no database, no LLM)."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from labgo.ingest.gitlog import co_change_edges, extract_history
from labgo.ingest.models import EdgeKind, NodeKind
from labgo.ingest.pyast import extract_repo

app = typer.Typer(add_completion=False, help="Change Impact Analyst")
console = Console()


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    console.print(f"  [dim]wrote[/dim] {path}")


@app.command()
def ingest(
    repo: Path = typer.Argument(..., help="Path to a Python repo to analyse"),
    out: Path = typer.Option(Path("data"), "--out", "-o", help="Output directory"),
) -> None:
    """Parse a repo into a code graph. No database required."""
    graph = extract_repo(repo)
    s = graph.stats

    t = Table(title="AST extraction", show_header=False, title_style="bold")
    t.add_row("files parsed", f"{s.files_parsed:,}")
    t.add_row("files failed", f"{s.files_failed:,}")
    t.add_row("functions", f"{s.functions:,}")
    t.add_row("classes", f"{s.classes:,}")
    t.add_row("", "")
    t.add_row("call sites (all)", f"{s.total_calls:,}")
    t.add_row("  [dim]external (builtin/stdlib/3p)[/dim]", f"[dim]{s.external_calls:,}[/dim]")
    t.add_row("[bold]in-scope call sites[/bold]", f"[bold]{s.in_scope_calls:,}[/bold]")
    t.add_row("  exact (import)", f"{s.resolved_exact:,}")
    t.add_row("  self/cls (same class)", f"{s.resolved_self:,}")
    t.add_row("  local (same file)", f"{s.resolved_local:,}")
    t.add_row("  heuristic (unique name)", f"{s.resolved_heuristic:,}")
    t.add_row("  [yellow]unresolved[/yellow]", f"[yellow]{s.unresolved_calls:,}[/yellow]")
    t.add_row("resolution rate", f"[bold]{s.resolution_rate:.1%}[/bold] [dim]of in-scope[/dim]")
    console.print(t)

    calls = sum(1 for e in graph.edges if e.kind is EdgeKind.CALLS)
    imports = sum(1 for e in graph.edges if e.kind is EdgeKind.IMPORTS)
    tests = sum(1 for e in graph.edges if e.kind is EdgeKind.TESTS)
    console.print(
        f"\ngraph: [bold]{len(graph.nodes):,}[/bold] nodes  "
        f"([dim]{sum(1 for n in graph.nodes if n.kind is NodeKind.FILE):,} files[/dim])  "
        f"[bold]{len(graph.edges):,}[/bold] edges  "
        f"[dim]({calls:,} CALLS · {imports:,} IMPORTS · {tests:,} TESTS)[/dim]"
    )
    console.print(
        "\n[dim]The unresolved count is the ceiling on call-graph recall. "
        "Record it now — it is the baseline every later improvement is measured against.[/dim]"
    )
    _write(out / "graph.json", graph.to_dict())


@app.command()
def history(
    repo: Path = typer.Argument(..., help="Path to a git repo"),
    out: Path = typer.Option(Path("data"), "--out", "-o"),
    max_commits: int = typer.Option(2000, "--max-commits"),
    max_files: int = typer.Option(20, "--max-files", help="Drop commits touching more than this"),
) -> None:
    """Mine co-change edges and build the ground-truth eval set from git history."""
    hist = extract_history(repo, max_commits=max_commits, max_files=max_files)
    st = hist.stats

    t = Table(title="Git history", show_header=False, title_style="bold")
    t.add_row("commits scanned", f"{st.commits_scanned:,}")
    t.add_row("  skipped: too large", f"{st.too_large_skipped:,}")
    t.add_row("  skipped: no source files", f"{st.no_source_skipped:,}")
    t.add_row("commits used", f"[bold]{st.commits_used:,}[/bold]")
    t.add_row("eval cases", f"[bold green]{st.eval_cases:,}[/bold green]")
    console.print(t)

    edges = co_change_edges(hist)
    console.print(f"\nco-change edges (seen >=2x): [bold]{len(edges):,}[/bold]")
    if edges:
        console.print("\n[dim]strongest coupling:[/dim]")
        for e in edges[:5]:
            console.print(f"  [dim]{e['count']:>3}x[/dim]  {e['src']}  [dim]<->[/dim]  {e['dst']}")

    _write(out / "cochange.json", edges)
    _write(out / "evalset.json", [c.to_dict() for c in hist.cases])
    console.print(
        "\n[dim]evalset.json is your ground truth. Nothing in this project is worth "
        "trusting until it is scored against it.[/dim]"
    )


if __name__ == "__main__":
    app()
