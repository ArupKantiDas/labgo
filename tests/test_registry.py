"""Language registry tests.

The registry is a table, so these are table-integrity tests: no extension claimed
twice, both tiers reachable, and the test-convention matcher agreeing with each
language's real-world layout.
"""

from __future__ import annotations

import pytest

from labgo.ingest.languages import (
    ALL_NOISE_NAMES,
    ALL_SKIP_DIRS,
    ALL_SOURCE_SUFFIXES,
    LANGUAGES,
    LanguageSpec,
    is_test_path,
    language_for_path,
    spec_for_path,
)


def _spec(name: str, ext: str) -> LanguageSpec:
    spec = spec_for_path(f"x{ext}")
    assert spec is not None
    assert spec.name == name
    return spec


def test_no_duplicate_extensions():
    seen: dict[str, str] = {}
    for spec in LANGUAGES:
        for ext in spec.extensions:
            assert ext not in seen, f"{ext} claimed by {seen[ext]} and {spec.name}"
            seen[ext] = spec.name


def test_language_for_path_covers_both_tiers():
    assert language_for_path("a/b.tsx") == "typescript"
    assert language_for_path("cmd/main.go") == "go"
    assert language_for_path("App.kt") == "kotlin"  # fallback tier
    assert language_for_path("weird.xyz") is None
    assert language_for_path("Makefile") is None


def test_spec_for_path_grammar_tier_only():
    assert spec_for_path("a.go") is not None
    assert spec_for_path("a.kt") is None  # fallback tier has no spec
    assert spec_for_path("A.GO") is not None, "suffix matching must be case-insensitive"


def test_derived_sets_cover_the_essentials():
    assert {".py", ".go", ".ts", ".rs", ".java", ".rb", ".kt"} <= ALL_SOURCE_SUFFIXES
    assert {"go.sum", "Cargo.lock", "composer.lock", "Gemfile.lock", "pnpm-lock.yaml"} <= (
        ALL_NOISE_NAMES
    )
    assert {"node_modules", "vendor", "target", ".git"} <= ALL_SKIP_DIRS


@pytest.mark.parametrize(
    ("rel", "ext", "lang", "expected"),
    [
        ("pkg/server_test.go", ".go", "go", True),
        ("pkg/server.go", ".go", "go", False),
        ("src/Foo.spec.ts", ".ts", "typescript", True),
        ("src/foo.ts", ".ts", "typescript", False),
        ("spec/foo_spec.rb", ".rb", "ruby", True),
        ("src/test/java/FooTest.java", ".java", "java", True),
        ("src/main/java/Foo.java", ".java", "java", False),
        ("tests/integration.rs", ".rs", "rust", True),
    ],
)
def test_is_test_path_per_language(rel, ext, lang, expected):
    assert is_test_path(rel, _spec(lang, ext)) is expected


def test_test_dir_must_be_a_directory_not_the_filename():
    """A file literally named `test.ts` is not in a test directory."""
    assert is_test_path("src/test.ts", _spec("typescript", ".ts")) is False
