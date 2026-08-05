"""LabGo CLI — Stage 1: ingestion and ground-truth extraction (no database, no LLM)."""

from __future__ import annotations

import http.server
import json
import threading
import webbrowser
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from labgo.baseline import score_baseline
from labgo.benchmark import (
    CorpusMismatchError,
    corpus_sha,
    files_at,
    filter_answerable,
    load_benchmark,
    verify_corpus,
    write_benchmark,
)
from labgo.graph import clear_database, connect, load_graph, read_graph_json
from labgo.hybrid import score_hybrid_baseline, score_vector_baseline
from labgo.ingest.gitlog import co_change_edges, extract_history
from labgo.ingest.models import EdgeKind, NodeKind
from labgo.ingest.pyast import extract_repo

load_dotenv()  # NEO4J_*, VOYAGE_API_KEY, etc. — see .env.example

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


@app.command()
def load(
    data: Path = typer.Option(Path("data"), "--data", "-d", help="Dir with graph.json"),
    clear: bool = typer.Option(
        False, "--clear", help="Wipe the database before loading (default: MERGE over what's there)"
    ),
    uri: str = typer.Option(None, "--uri", help="Default: $NEO4J_URI or bolt://localhost:7687"),
    user: str = typer.Option(None, "--user", help="Default: $NEO4J_USER or neo4j"),
    password: str = typer.Option(None, "--password", help="Default: $NEO4J_PASSWORD"),
) -> None:
    """Load graph.json (+ cochange.json, if present) into Neo4j. `docker compose up -d` first."""
    graph_path = data / "graph.json"
    if not graph_path.exists():
        console.print(
            f"[bold red]{graph_path} not found.[/bold red] Run [bold]labgo ingest[/bold] first."
        )
        raise typer.Exit(1)

    graph, cochange = read_graph_json(data)
    driver = connect(uri, user, password)
    try:
        if clear:
            clear_database(driver)
        stats = load_graph(driver, graph, cochange)
    finally:
        driver.close()

    t = Table(title="Loaded into Neo4j", show_header=False, title_style="bold")
    t.add_row("nodes", f"{stats.nodes:,}")
    t.add_row("edges (CALLS/IMPORTS/CONTAINS/TESTS)", f"{stats.edges:,}")
    t.add_row("edges (CO_CHANGED)", f"{stats.cochange_edges:,}")
    console.print(t)


@app.command()
def baseline(
    bench_dir: Path = typer.Argument(..., help="Benchmark directory, e.g. benchmarks/httpx"),
    method: str = typer.Option(
        "calls", "--method", help="calls (Stage 2) | vectors | hybrid (Stage 3b, D015)"
    ),
    hops: int = typer.Option(2, "--hops", help="CALLS traversal depth (calls/hybrid)"),
    cochange: bool = typer.Option(
        True,
        "--cochange/--no-cochange",
        help="Include CO_CHANGED neighbors in the prediction (calls/hybrid)",
    ),
    k: int = typer.Option(10, "--k", help="Vector neighbors per seed function (vectors/hybrid)"),
    uri: str = typer.Option(None, "--uri"),
    user: str = typer.Option(None, "--user"),
    password: str = typer.Option(None, "--password"),
) -> None:
    """Score a deterministic impact prediction against a pinned benchmark. No LLM.

    `--method calls` (default) is the unchanged Stage 2 baseline (D012). `vectors` and
    `hybrid` are Stage 3b (D015) — same benchmark, same scoring, different predictor.
    """
    manifest, cases = load_benchmark(bench_dir)
    driver = connect(uri, user, password)
    try:
        if method == "calls":
            result, _ = score_baseline(driver, cases, hops=hops, use_cochange=cochange)
            title = (
                f"Deterministic baseline — '{manifest['name']}' "
                f"(hops={hops}, cochange={'on' if cochange else 'off'})"
            )
        elif method == "vectors":
            result, _ = score_vector_baseline(driver, cases, k=k)
            title = f"Vector baseline — '{manifest['name']}' (k={k})"
        elif method == "hybrid":
            result, _ = score_hybrid_baseline(driver, cases, hops=hops, use_cochange=cochange, k=k)
            title = (
                f"Hybrid baseline — '{manifest['name']}' "
                f"(hops={hops}, cochange={'on' if cochange else 'off'}, k={k})"
            )
        else:
            console.print(
                f"[bold red]unknown --method {method!r}[/bold red] — calls | vectors | hybrid"
            )
            raise typer.Exit(1)
    finally:
        driver.close()

    t = Table(title=title, show_header=False, title_style="bold")
    t.add_row("cases scored", f"{result.n_cases:,}")
    t.add_row("mean recall", f"[bold]{result.recall_mean:.1%}[/bold]")
    t.add_row("mean precision", f"{result.precision_mean:.1%}")
    t.add_row("hit rate (recall > 0)", f"{result.hit_rate:.1%}")
    t.add_row("empty predictions", f"[dim]{result.empty_predictions:,}[/dim]")
    console.print(t)
    if method == "calls":
        console.print(
            "\n[dim]This is the number every later stage has to beat. "
            "Record it in DECISIONS.md before adding vectors or agents.[/dim]"
        )


@app.command()
def embed(
    repo: Path = typer.Argument(..., help="Corpus repo — source of the text that gets embedded"),
    uri: str = typer.Option(None, "--uri"),
    user: str = typer.Option(None, "--user"),
    password: str = typer.Option(None, "--password"),
    api_key: str = typer.Option(None, "--api-key", help="Default: $VOYAGE_API_KEY"),
) -> None:
    """Embed every Function/Class node's source with Voyage and index it in Neo4j (D014).

    Requires `labgo load` first (reads node metadata from Neo4j, not graph.json) and the
    `vectors` extra: `uv sync --extra vectors`.
    """
    from labgo.embed import EMBED_DIM, EMBED_MODEL, INDEX_NAME, embed_nodes

    driver = connect(uri, user, password)
    try:
        stats = embed_nodes(driver, repo, api_key=api_key)
    finally:
        driver.close()

    t = Table(title="Embedded into Neo4j", show_header=False, title_style="bold")
    t.add_row("candidates (Function + Class)", f"{stats.candidates:,}")
    t.add_row("embedded", f"[bold]{stats.embedded:,}[/bold]")
    t.add_row("skipped (source unavailable)", f"[dim]{stats.skipped_no_source:,}[/dim]")
    t.add_row("tokens billed", f"{stats.total_tokens:,}")
    console.print(t)
    console.print(f"\n[dim]model: {EMBED_MODEL} · dim: {EMBED_DIM} · index: '{INDEX_NAME}'[/dim]")


@app.command()
def search(
    query: str = typer.Argument(..., help="Natural-language or code-like query"),
    k: int = typer.Option(10, "--k", help="Number of nearest neighbours"),
    uri: str = typer.Option(None, "--uri"),
    user: str = typer.Option(None, "--user"),
    password: str = typer.Option(None, "--password"),
    api_key: str = typer.Option(None, "--api-key", help="Default: $VOYAGE_API_KEY"),
) -> None:
    """Semantic search over embedded Function/Class nodes. No graph traversal, no LLM.

    The librarian half of the WHY.md distinction, run in isolation — run `labgo baseline`
    for the subway-map half. Requires `labgo embed` first.
    """
    from labgo.embed import semantic_search as _semantic_search

    driver = connect(uri, user, password)
    try:
        hits = _semantic_search(driver, query, k=k, api_key=api_key)
    finally:
        driver.close()

    t = Table(title=f"Nearest to: {query!r}")
    t.add_column("score")
    t.add_column("id")
    for h in hits:
        t.add_row(f"{h['score']:.3f}", h["id"])
    console.print(t)


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
