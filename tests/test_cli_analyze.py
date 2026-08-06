"""`labgo analyze` / `labgo doctor` — the plug-and-play surface (D019).

Commands are called as plain functions with every argument explicit (Typer
defaults are OptionInfo sentinels, not values), against a synthesized git repo —
no network, no credentials, matching the rest of the suite.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from labgo.cli import _corpus_name, _is_git_url, analyze, doctor


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _commit_all(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", msg, "--no-verify")


def _mixed_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "corpus"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "main.go").write_text(
        textwrap.dedent(
            """
            package main

            func main() { run() }

            func run() {}
            """
        ),
        encoding="utf-8",
    )
    (repo / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    _commit_all(repo, "one")
    (repo / "main.go").write_text(
        (repo / "main.go").read_text(encoding="utf-8") + "\nfunc extra() {}\n", encoding="utf-8"
    )
    (repo / "util.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
    _commit_all(repo, "two")
    return repo


def test_analyze_local_repo_end_to_end(tmp_path: Path) -> None:
    repo = _mixed_git_repo(tmp_path)
    out = tmp_path / "data"
    analyze(target=str(repo), out=out, serve=False, port=4173, max_commits=2000)

    assert (out / "graph.json").exists()
    assert (out / "cochange.json").exists()
    assert (out / "evalset.json").exists()

    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    ids = {n["id"] for n in graph["nodes"]}
    assert "main.go::run" in ids, "Go must be AST-parsed by analyze"
    assert "util.py::helper" in ids, "Python must be AST-parsed by analyze"

    cochange = json.loads((out / "cochange.json").read_text(encoding="utf-8"))
    assert cochange, "the two-language commit must produce a co-change edge (D018)"
    assert {cochange[0]["src"], cochange[0]["dst"]} == {"main.go", "util.py"}


def test_analyze_non_git_dir_still_produces_graph(tmp_path: Path) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    (repo / "a.go").write_text("package a\n\nfunc A() {}\n", encoding="utf-8")
    out = tmp_path / "data"
    analyze(target=str(repo), out=out, serve=False, port=4173, max_commits=2000)
    assert (out / "graph.json").exists()
    assert not (out / "cochange.json").exists(), "no git history -> no co-change file"


def test_url_detection_and_corpus_naming() -> None:
    assert _is_git_url("https://github.com/encode/httpx.git") is True
    assert _is_git_url("git@github.com:encode/httpx.git") is True
    assert _is_git_url("../labgo-corpora/httpx") is False
    assert _is_git_url("/abs/path/repo") is False
    assert _corpus_name("https://github.com/encode/httpx.git") == "httpx"
    assert _corpus_name("https://github.com/encode/httpx/") == "httpx"


def test_doctor_is_a_report_not_a_gate(capsys) -> None:
    doctor(probe=False)
    out = capsys.readouterr().out
    assert "labgo doctor" in out
    assert "analyze" in out
