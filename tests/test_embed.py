"""Voyage embedding — pure-Python parts only. No live Voyage/Neo4j required for these."""

from __future__ import annotations

from pathlib import Path

from labgo.embed import read_source


def test_read_source_extracts_the_line_range(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "def f():\n    return 1\n\n\ndef g():\n    return 2\n", encoding="utf-8"
    )

    text = read_source(tmp_path, "a.py", lineno=1, end_lineno=2)

    assert text == "def f():\n    return 1"


def test_read_source_returns_none_without_line_numbers(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert read_source(tmp_path, "a.py", lineno=None, end_lineno=None) is None


def test_read_source_returns_none_when_the_file_is_missing(tmp_path: Path) -> None:
    assert read_source(tmp_path, "does_not_exist.py", lineno=1, end_lineno=1) is None
