"""LabGo CLI — Stage 1: ingestion and ground-truth extraction (no database, no LLM)."""

from __future__ import annotations

import http.server
import json
import threading
import webbrowser
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from labgo.benchmark import (
    CorpusMismatchError,
    corpus_sha,
    files_at,
    filter_answerable,
    load_benchmark,
    verify_corpus,
    write_benchmark,
)
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
    evidence_max_files: int = typer.Option(
        20, "--evidence-max-files", help="Commits above this contribute no co-change pairs"
    ),
    eval_max_files: int = typer.Option(
        10, "--eval-max-files", help="Commits above this produce no eval case (D010)"
    ),
) -> None:
    """Mine co-change edges and build the ground-truth eval set from git history."""
    hist = extract_history(
        repo,
        max_commits=max_commits,
        evidence_max_files=evidence_max_files,
        eval_max_files=eval_max_files,
    )
    st = hist.stats

    t = Table(title="Git history", show_header=False, title_style="bold")
    t.add_row("commits scanned", f"{st.commits_scanned:,}")
    t.add_row("  skipped: no source files", f"{st.no_source_skipped:,}")
    t.add_row(f"  skipped: >{evidence_max_files} files (evidence)", f"{st.too_large_skipped:,}")
    t.add_row("commits used for evidence", f"[bold]{st.commits_used:,}[/bold]")
    t.add_row(f"  of those, >{eval_max_files} files (no case)", f"{st.too_large_for_eval:,}")
    t.add_row("eval cases (raw)", f"[bold green]{st.eval_cases:,}[/bold green]")
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
        "\n[yellow]Raw — not yet usable for scoring.[/yellow] "
        "Run [bold]labgo benchmark[/bold] to filter out temporally-unanswerable cases (D008)."
    )


@app.command()
def benchmark(
    repo: Path = typer.Argument(..., help="Corpus repo — will be pinned at its current HEAD"),
    name: str = typer.Option(..., "--name", "-n", help="Benchmark name, e.g. 'httpx'"),
    out: Path = typer.Option(Path("benchmarks"), "--out", "-o"),
    max_commits: int = typer.Option(2000, "--max-commits"),
    evidence_max_files: int = typer.Option(20, "--evidence-max-files"),
    eval_max_files: int = typer.Option(10, "--eval-max-files"),
) -> None:
    """Build a pinned, committed benchmark: answerable cases + provenance manifest."""
    hist = extract_history(
        repo,
        max_commits=max_commits,
        evidence_max_files=evidence_max_files,
        eval_max_files=eval_max_files,
    )
    sha = corpus_sha(repo)
    live = files_at(repo, sha)
    kept, report = filter_answerable(hist.cases, live)

    t = Table(title=f"Benchmark '{name}'", show_header=False, title_style="bold")
    t.add_row("corpus sha", f"[dim]{sha[:12]}[/dim]")
    t.add_row("files in tree", f"{len(live):,}")
    t.add_row("", "")
    t.add_row("raw cases", f"{report.before:,}")
    t.add_row("  dropped: seed deleted", f"[yellow]{report.dropped_dead_seed:,}[/yellow]")
    t.add_row("  dropped: expected deleted", f"[yellow]{report.dropped_dead_expected:,}[/yellow]")
    t.add_row("answerable cases", f"[bold green]{report.after:,}[/bold green]")
    t.add_row("dropped", f"[bold]{report.dropped_pct:.1f}%[/bold]")
    console.print(t)

    if report.after == 0:
        console.print("\n[red]No answerable cases. Refusing to write an empty benchmark.[/red]")
        raise typer.Exit(1)

    bench = write_benchmark(
        out_dir=out,
        name=name,
        repo=repo,
        cases=kept,
        report=report,
        extraction={
            "max_commits": max_commits,
            "evidence_max_files": evidence_max_files,
            "eval_max_files": eval_max_files,
            "min_files": 2,
        },
    )
    console.print(
        f"\n  [dim]wrote[/dim] {bench}/manifest.json\n  [dim]wrote[/dim] {bench}/cases.json"
    )
    console.print(
        "\n[dim]Commit this directory. Regenerating it changes the exam — do that "
        "as a logged decision, not as a side effect of updating the corpus.[/dim]"
    )


@app.command()
def verify(
    bench_dir: Path = typer.Argument(..., help="Benchmark directory"),
    repo: Path = typer.Argument(..., help="Corpus repo to check"),
) -> None:
    """Check the corpus is at the commit a benchmark was built against."""
    manifest, cases = load_benchmark(bench_dir)
    try:
        verify_corpus(manifest, repo)
    except CorpusMismatchError as exc:
        console.print(f"[bold red]corpus mismatch[/bold red]\n{exc}")
        raise typer.Exit(1) from exc
    console.print(
        f"[bold green]ok[/bold green]  '{manifest['name']}' · "
        f"{len(cases):,} cases · corpus at {manifest['corpus']['sha'][:12]}"
    )


def _view_handler(
    dist_dir: Path, graph_path: Path, cochange_path: Path
) -> type[http.server.SimpleHTTPRequestHandler]:
    """Build a handler serving the prebuilt viewer, with data routed live from disk.

    `dist/` ships committed to the repo so `labgo view` needs no Node at runtime —
    but it must never serve a graph baked in at *build* time, because that would be
    whatever corpus the maintainer last ingested, not the user's own repo. So
    /graph.json and /cochange.json are the two paths this handler intercepts and
    re-points at the live data directory; everything else falls through to the
    static bundle.
    """

    class ViewHandler(http.server.SimpleHTTPRequestHandler):
        """SimpleHTTPRequestHandler bound to dist/, with data routes overridden."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            """Bind the handler to the prebuilt viewer bundle."""
            super().__init__(*args, directory=str(dist_dir), **kwargs)

        def translate_path(self, path: str) -> str:
            """Route the two data endpoints to the live data dir, not dist/."""
            if path == "/graph.json":
                return str(graph_path)
            if path == "/cochange.json" and cochange_path.exists():
                return str(cochange_path)
            return super().translate_path(path)

    return ViewHandler


@app.command()
def view(
    data: Path = typer.Option(
        Path("data"), "--data", "-d", help="Directory with graph.json / cochange.json"
    ),
    port: int = typer.Option(4173, "--port", "-p"),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the viewer in a browser"
    ),
) -> None:
    """Serve the interactive impact viewer. No Node, database, or LLM at runtime."""
    repo_root = Path(__file__).resolve().parents[2]
    dist_dir = repo_root / "viewer" / "dist"
    graph_path = data / "graph.json"
    cochange_path = data / "cochange.json"

    if not dist_dir.exists():
        console.print(
            f"[bold red]{dist_dir} not found.[/bold red] Build it once with:\n"
            "  cd viewer && npm install && npm run build"
        )
        raise typer.Exit(1)
    if not graph_path.exists():
        console.print(
            f"[bold red]{graph_path} not found.[/bold red] "
            "Run [bold]labgo ingest <repo>[/bold] first."
        )
        raise typer.Exit(1)
    if not cochange_path.exists():
        console.print(
            f"[yellow]{cochange_path} not found[/yellow] — impact mode will show call-graph "
            "reach but no co-change history. Run [bold]labgo history <repo>[/bold] to add it."
        )

    try:
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", port), _view_handler(dist_dir, graph_path, cochange_path)
        )
    except OSError as exc:
        console.print(
            f"[bold red]could not bind to port {port}:[/bold red] {exc}\n"
            f"Try [bold]labgo view --port {port + 1}[/bold]"
        )
        raise typer.Exit(1) from exc

    url = f"http://127.0.0.1:{port}"
    console.print(
        f"\n[bold green]serving[/bold green] {url}  [dim](data: {data}/, ctrl-c to stop)[/dim]"
    )
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")


if __name__ == "__main__":
    app()
