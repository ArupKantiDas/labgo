"""The language registry — the single place a language is described.

Everything the pipeline needs to know about a language lives in one row here:
which extensions it owns, which tree-sitter grammar parses it (if any), what its
test-file conventions look like, which directories it vendors dependencies into,
and which lockfiles it generates. Three consumers read this table and nothing else:

* `labgo.ingest.extract` — routes each file to a parsing backend (pyast for
  Python, tree-sitter for grammar languages, file-level fallback for the rest).
* `labgo.ingest.tsast` — grammar key, test conventions, per-language settings.
* `labgo.ingest.gitlog` — which suffixes count as source, which names are noise.

Two tiers, deliberately (D018): languages with a `grammar` get definitions and
call/import edges; `FALLBACK_EXTENSIONS` languages get File nodes and git-derived
signal only. The second tier is not a stub — co-change evidence is language-agnostic
and carries most of the retrieval signal on its own (D010/D012), so "file-level only"
still powers impact mode, the eval set, and the viewer for any repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class LanguageSpec:
    """Everything the pipeline needs to know about one language, in one row.

    `extensions` are lowercase and include the dot. `grammar` is the
    tree-sitter-language-pack key, or None for languages some other backend owns
    (Python -> pyast) — extraction routing is decided in `labgo.ingest.extract`,
    not here. Test conventions are split three ways because languages disagree on
    where the signal lives: the file name (`*_test.go`), the directory (`spec/`),
    or the function name (`^Test[A-Z]`).
    """

    name: str  # canonical name; lands in Node.props["language"] and per-language stats
    extensions: tuple[str, ...]  # lowercase, with dot
    grammar: str | None  # tree-sitter-language-pack key; None => not parsed by tsast
    test_file_globs: tuple[str, ...] = ()  # fnmatch against the file *name*
    test_dir_parts: tuple[str, ...] = ()  # any path part equal to one of these => test file
    test_func_pattern: str | None = None  # regex a function name must match to be a test
    skip_dirs: tuple[str, ...] = ()  # extra vendored/build dirs this language contributes
    noise_names: tuple[str, ...] = ()  # lockfiles etc. excluded from co-change evidence


LANGUAGES: tuple[LanguageSpec, ...] = (
    LanguageSpec(
        name="python",
        extensions=(".py", ".pyi"),
        grammar=None,  # parsed by labgo.ingest.pyast, not tsast — the measured path (D004)
        test_file_globs=("test_*.py", "*_test.py"),
        test_dir_parts=("tests",),
        test_func_pattern=r"^test_",
        noise_names=("uv.lock", "poetry.lock", "Pipfile.lock", "requirements.txt", "pdm.lock"),
    ),
    LanguageSpec(
        name="javascript",
        extensions=(".js", ".jsx", ".mjs", ".cjs"),
        grammar="javascript",
        test_file_globs=(
            "*.test.js",
            "*.spec.js",
            "*.test.jsx",
            "*.spec.jsx",
            "*.test.mjs",
            "*.spec.mjs",
        ),
        test_dir_parts=("__tests__", "test", "tests"),
        test_func_pattern=r"^test",
        skip_dirs=("node_modules", ".next", ".nuxt", "coverage", "bower_components"),
        noise_names=(
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "npm-shrinkwrap.json",
            "bun.lock",
            "bun.lockb",
            "deno.lock",
        ),
    ),
    LanguageSpec(
        name="typescript",
        extensions=(".ts", ".mts", ".cts"),
        grammar="typescript",
        test_file_globs=("*.test.ts", "*.spec.ts", "*.test.mts", "*.spec.mts"),
        test_dir_parts=("__tests__", "test", "tests"),
        test_func_pattern=r"^test",
    ),
    LanguageSpec(
        # Same language name, different grammar: .tsx needs the tsx grammar variant.
        name="typescript",
        extensions=(".tsx",),
        grammar="tsx",
        test_file_globs=("*.test.tsx", "*.spec.tsx"),
        test_dir_parts=("__tests__", "test", "tests"),
        test_func_pattern=r"^test",
    ),
    LanguageSpec(
        name="go",
        extensions=(".go",),
        grammar="go",
        test_file_globs=("*_test.go",),
        test_func_pattern=r"^(Test|Benchmark|Fuzz|Example)[A-Z_]",
        skip_dirs=("vendor",),
        noise_names=("go.sum", "go.work.sum"),
    ),
    LanguageSpec(
        name="java",
        extensions=(".java",),
        grammar="java",
        test_file_globs=("*Test.java", "*Tests.java", "*TestCase.java", "Test*.java"),
        test_dir_parts=("test",),  # Maven/Gradle src/test/java; a `test` util package misflags
        test_func_pattern=r"^test",
        skip_dirs=(".gradle", "gradle", "target", "out"),
        noise_names=("gradle.lockfile",),
    ),
    LanguageSpec(
        name="rust",
        extensions=(".rs",),
        grammar="rust",
        test_dir_parts=("tests", "benches"),
        test_func_pattern=r"^test_",  # weak — #[cfg(test)] detection deliberately deferred (D018)
        skip_dirs=("target",),
        noise_names=("Cargo.lock",),
    ),
    LanguageSpec(
        # .h is assigned to C, not C++: the C grammar parses most C-ish headers, and a
        # C++ header that fails just yields fewer definitions (tree-sitter is error-
        # tolerant). Revisit only if a C++-heavy corpus shows it matters.
        name="c",
        extensions=(".c", ".h"),
        grammar="c",
        test_dir_parts=("test", "tests"),
        test_func_pattern=r"^test_",
        skip_dirs=("cmake-build-debug", "cmake-build-release", "CMakeFiles"),
    ),
    LanguageSpec(
        name="cpp",
        extensions=(".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".inl"),
        grammar="cpp",
        test_file_globs=("*_test.cpp", "*_test.cc", "*Test.cpp"),
        test_dir_parts=("test", "tests"),
        test_func_pattern=r"^(TEST|test_)",
    ),
    LanguageSpec(
        name="ruby",
        extensions=(".rb", ".rake"),
        grammar="ruby",
        test_file_globs=("*_spec.rb", "*_test.rb", "test_*.rb"),
        test_dir_parts=("spec", "test"),
        test_func_pattern=r"^test_",
        skip_dirs=(".bundle",),
        noise_names=("Gemfile.lock",),
    ),
    LanguageSpec(
        name="csharp",
        extensions=(".cs",),
        grammar="csharp",
        test_file_globs=("*Test.cs", "*Tests.cs"),
        test_dir_parts=("test", "tests"),
        test_func_pattern=r"^Test",
        skip_dirs=("bin", "obj", "packages"),
        noise_names=("packages.lock.json",),
    ),
    LanguageSpec(
        name="php",
        extensions=(".php",),
        grammar="php",
        test_file_globs=("*Test.php",),
        test_dir_parts=("tests", "test"),
        test_func_pattern=r"^test",
        noise_names=("composer.lock",),
    ),
)

# Second tier: recognized as *source* (File nodes, co-change evidence, eval cases)
# but never AST-parsed. Being here is not a stub — see the module docstring.
FALLBACK_EXTENSIONS: dict[str, str] = {
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".dart": "dart",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".lua": "lua",
    ".pl": "perl",
    ".pm": "perl",
    ".r": "r",
    ".jl": "julia",
    ".clj": "clojure",
    ".m": "objective-c",
    ".zig": "zig",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".vue": "vue",
    ".svelte": "svelte",
    ".proto": "protobuf",
}

# Never-interesting directories, regardless of language. The per-language additions
# (vendor/, target/, bin/, ...) fold in below via ALL_SKIP_DIRS — one walker, one set.
COMMON_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        ".tox",
        ".eggs",
        "site-packages",
        ".idea",
        ".vscode",
        "Pods",
        "DerivedData",
        ".terraform",
        "_build",
        ".dart_tool",
        "zig-cache",
    }
)

ALL_SKIP_DIRS: frozenset[str] = COMMON_SKIP_DIRS | frozenset(
    d for spec in LANGUAGES for d in spec.skip_dirs
)

ALL_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    ext for spec in LANGUAGES for ext in spec.extensions
) | frozenset(FALLBACK_EXTENSIONS)

ALL_NOISE_NAMES: frozenset[str] = (
    frozenset(n for spec in LANGUAGES for n in spec.noise_names)
    | frozenset({"CHANGELOG.md", "Podfile.lock", "mix.lock"})
)


def _build_by_ext() -> dict[str, LanguageSpec]:
    """Map extension -> spec, refusing duplicate claims at import time.

    A silent duplicate would route files to whichever spec came last — the kind of
    plausible-but-wrong behavior D006 exists to warn about, so it fails loudly here.
    """
    by_ext: dict[str, LanguageSpec] = {}
    for spec in LANGUAGES:
        for ext in spec.extensions:
            if ext in by_ext:
                msg = f"extension {ext!r} claimed by both {by_ext[ext].name!r} and {spec.name!r}"
                raise ValueError(msg)
            by_ext[ext] = spec
    return by_ext


_BY_EXT: dict[str, LanguageSpec] = _build_by_ext()


def spec_for_path(path: str | Path) -> LanguageSpec | None:
    """The grammar-tier spec owning this path's extension, or None."""
    return _BY_EXT.get(Path(path).suffix.lower())


def language_for_path(path: str | Path) -> str | None:
    """Canonical language name for a path — either tier — or None for non-source."""
    suffix = Path(path).suffix.lower()
    spec = _BY_EXT.get(suffix)
    if spec is not None:
        return spec.name
    return FALLBACK_EXTENSIONS.get(suffix)


def is_test_path(rel: str, spec: LanguageSpec) -> bool:
    """Is this file a test file, by the language's own conventions?

    PurePosixPath because `rel` comes from git output or a relative walk — always
    forward-slashed — and must not be reinterpreted by a Windows host.
    """
    p = PurePosixPath(rel)
    if any(fnmatch(p.name, g) for g in spec.test_file_globs):
        return True
    return any(part in spec.test_dir_parts for part in p.parts[:-1])
