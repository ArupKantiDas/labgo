"""Tree-sitter extractor tests (D018).

Same discipline as test_ingest.py: assert on non-emptiness and metric invariants,
not just happy-path shapes — a query that silently matches nothing must fail a
test here, not return a plausible empty graph (the D006 lesson, applied to the
grammar layer). These tests are also the permanent tripwire for grammar-version
drift: if a language-pack upgrade renames a node type, the language's test fails
loudly instead of the extractor emitting nothing.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from labgo.ingest.languages import spec_for_path
from labgo.ingest.models import EdgeKind, Graph, NodeKind
from labgo.ingest.tsast import extract


def _write(root: Path, rel: str, src: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src), encoding="utf-8")


def _extract(root: Path) -> Graph:
    """Run tsast over every grammar-tier file under `root`."""
    graph = Graph()
    files: dict[str, list[tuple[str, Path]]] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        spec = spec_for_path(p)
        if spec is not None and spec.grammar is not None:
            rel = str(p.relative_to(root))
            files.setdefault(spec.grammar, []).append((rel, p))
    unhandled = extract(root, files, graph)
    assert unhandled == [], f"unexpectedly degraded: {unhandled}"
    return graph


def _calls(graph: Graph) -> dict[tuple[str, str], str]:
    return {
        (e.src, e.dst): e.props["confidence"]
        for e in graph.edges
        if e.kind is EdgeKind.CALLS
    }


# ---------------------------------------------------------------------- Go


def _go_repo(tmp_path: Path) -> None:
    _write(tmp_path, "go.mod", "module example.com/app\n\ngo 1.22\n")
    _write(
        tmp_path,
        "server/server.go",
        """
        package server

        import "example.com/app/util"

        type Server struct{}

        func (s *Server) Start() {
            s.helper()
            prepare()
            util.Helper()
            n := len("x")
            _ = n
        }

        func (s *Server) helper() {}

        func prepare() {}
        """,
    )
    _write(
        tmp_path,
        "util/util.go",
        """
        package util

        func Helper() {}

        func Unique_name_somewhere() {}
        """,
    )
    _write(
        tmp_path,
        "server/server_test.go",
        """
        package server

        import "testing"

        func TestStart(t *testing.T) {
            prepare()
        }
        """,
    )


def test_go_definitions_and_qualnames(tmp_path: Path) -> None:
    _go_repo(tmp_path)
    g = _extract(tmp_path)
    ids = {n.id for n in g.nodes}
    assert "server/server.go::Server" in ids, "struct should be a Class node"
    assert "server/server.go::Server.Start" in ids, "method qualname must be Type.Name"
    assert "util/util.go::Helper" in ids
    kinds = {n.id: n.kind for n in g.nodes}
    assert kinds["server/server.go::Server"] is NodeKind.CLASS
    assert kinds["server/server.go::Server.Start"] is NodeKind.FUNCTION
    assert g.stats.per_language["go"]["functions"] >= 4, "non-emptiness invariant (D006)"


def test_go_full_resolution_ladder(tmp_path: Path) -> None:
    _go_repo(tmp_path)
    g = _extract(tmp_path)
    calls = _calls(g)

    assert calls[("server/server.go::Server.Start", "server/server.go::Server.helper")] == "self", (
        "receiver-variable call must resolve SELF"
    )
    assert calls[("server/server.go::Server.Start", "server/server.go::prepare")] == "local"
    assert calls[("server/server.go::Server.Start", "util/util.go::Helper")] == "exact", (
        "go.mod-resolved package import must be EXACT"
    )


def test_go_imports_edge_and_external_builtin(tmp_path: Path) -> None:
    _go_repo(tmp_path)
    g = _extract(tmp_path)
    imports = {(e.src, e.dst) for e in g.edges if e.kind is EdgeKind.IMPORTS}
    assert ("server/server.go", "util/util.go") in imports

    per = g.stats.per_language["go"]
    assert per["external_calls"] >= 1, "len() must be classified external (D005)"
    assert g.stats.external_calls == per["external_calls"]


def test_go_test_detection_and_tests_edge(tmp_path: Path) -> None:
    _go_repo(tmp_path)
    g = _extract(tmp_path)
    test_fn = next(n for n in g.nodes if n.id == "server/server_test.go::TestStart")
    assert test_fn.props["is_test"] is True
    tests = {(e.src, e.dst) for e in g.edges if e.kind is EdgeKind.TESTS}
    assert ("server/server_test.go::TestStart", "server/server.go::prepare") in tests


def test_go_stdlib_import_is_not_an_imports_edge(tmp_path: Path) -> None:
    _go_repo(tmp_path)
    g = _extract(tmp_path)
    dsts = {e.dst for e in g.edges if e.kind is EdgeKind.IMPORTS}
    assert all(d.endswith(".go") for d in dsts), "stdlib imports must not create edges"


# ------------------------------------------------------------------- TS / JS


def _ts_repo(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/util.ts",
        """
        export function helper(): number {
            return 1;
        }
        """,
    )
    _write(
        tmp_path,
        "src/engine.ts",
        """
        import { helper } from "./util.js";
        import { sortBy } from "lodash";

        export class Engine {
            run(): number {
                this.step();
                sortBy([]);
                return helper();
            }
            step(): void {}
        }

        export function outer(): void {
            [1].map(() => {
                prepare();
            });
        }

        const prepare = () => {
            console.log("x");
        };
        """,
    )


def test_ts_defs_classes_and_arrow_functions(tmp_path: Path) -> None:
    _ts_repo(tmp_path)
    g = _extract(tmp_path)
    kinds = {n.id: n.kind for n in g.nodes}
    assert kinds["src/engine.ts::Engine"] is NodeKind.CLASS
    assert kinds["src/engine.ts::Engine.run"] is NodeKind.FUNCTION
    assert kinds["src/engine.ts::prepare"] is NodeKind.FUNCTION, "arrow-const def missed"
    assert g.stats.per_language["typescript"]["functions"] >= 4


def test_ts_ladder_self_exact_and_nodenext_specifier(tmp_path: Path) -> None:
    _ts_repo(tmp_path)
    g = _extract(tmp_path)
    calls = _calls(g)
    assert calls[("src/engine.ts::Engine.run", "src/engine.ts::Engine.step")] == "self"
    assert calls[("src/engine.ts::Engine.run", "src/util.ts::helper")] == "exact", (
        "a `./util.js` NodeNext specifier must resolve to util.ts"
    )
    imports = {(e.src, e.dst) for e in g.edges if e.kind is EdgeKind.IMPORTS}
    assert ("src/engine.ts", "src/util.ts") in imports


def test_ts_external_package_import_leaves_denominator(tmp_path: Path) -> None:
    _ts_repo(tmp_path)
    g = _extract(tmp_path)
    per = g.stats.per_language["typescript"]
    assert per["external_calls"] >= 2, "sortBy (lodash) and console must be external"
    assert ("src/engine.ts::Engine.run", "sortBy") not in {
        (e.src, e.dst) for e in g.edges if e.kind is EdgeKind.CALLS
    }


def test_ts_call_in_callback_attributes_to_named_function(tmp_path: Path) -> None:
    """A call inside an anonymous arrow inside `outer` belongs to `outer`."""
    _ts_repo(tmp_path)
    g = _extract(tmp_path)
    calls = _calls(g)
    assert ("src/engine.ts::outer", "src/engine.ts::prepare") in calls


# ------------------------------------------------- smoke tests, one per language
#
# Each writes a tiny repo, then asserts ≥1 function extracted (the D006 tripwire —
# a renamed grammar node type must fail loudly here, not yield a plausible empty
# graph) plus that language's one distinctive behavior.


def test_java_annotations_and_import_resolution(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/java/a/b/Util.java",
        """
        package a.b;
        public class Util {
            public static int go() { return 1; }
        }
        """,
    )
    _write(
        tmp_path,
        "src/test/java/a/b/EngineTest.java",
        """
        package a.b;
        import a.b.Util;
        public class EngineTest {
            @Test
            public void run() { helper(); Util.go(); }
            private void helper() {}
        }
        """,
    )
    g = _extract(tmp_path)
    calls = _calls(g)
    src_id = "src/test/java/a/b/EngineTest.java::EngineTest.run"
    assert calls[(src_id, "src/test/java/a/b/EngineTest.java::EngineTest.helper")] == "self", (
        "bare call in a class body must resolve SELF (implicit this)"
    )
    assert calls[(src_id, "src/main/java/a/b/Util.java::Util.go")] == "exact", (
        "import a.b.Util + Util.go() must resolve EXACT via path-suffix lookup"
    )
    tests = {(e.src, e.dst) for e in g.edges if e.kind is EdgeKind.TESTS}
    assert (src_id, "src/test/java/a/b/EngineTest.java::EngineTest.helper") in tests, (
        "@Test annotation must mark the method as a test"
    )
    assert g.stats.per_language["java"]["functions"] >= 3


def test_rust_impl_qualnames_and_mod_imports(tmp_path: Path) -> None:
    _write(tmp_path, "src/util.rs", "pub fn helper() {}\n")
    _write(
        tmp_path,
        "src/main.rs",
        """
        mod util;
        use std::fmt;

        struct Server;

        impl Server {
            fn start(&self) {
                self.step();
                util::helper();
            }
            fn step(&self) {}
        }
        """,
    )
    g = _extract(tmp_path)
    calls = _calls(g)
    assert calls[("src/main.rs::Server.start", "src/main.rs::Server.step")] == "self", (
        "self.method() in an impl must resolve SELF with Type.method qualname"
    )
    assert calls[("src/main.rs::Server.start", "src/util.rs::helper")] == "exact", (
        "mod util; + util::helper() must resolve via sibling-file lookup"
    )
    assert ("src/main.rs", "src/util.rs") in {
        (e.src, e.dst) for e in g.edges if e.kind is EdgeKind.IMPORTS
    }
    assert g.stats.per_language["rust"]["functions"] >= 3


def test_c_quoted_include_and_libc_external(tmp_path: Path) -> None:
    _write(tmp_path, "util.h", "int helper(void);\n")
    _write(tmp_path, "util.c", '#include "util.h"\nint helper(void) { return 1; }\n')
    _write(
        tmp_path,
        "main.c",
        """
        #include "util.h"
        #include <stdio.h>

        int main(void) {
            helper();
            printf("x");
            return 0;
        }
        """,
    )
    g = _extract(tmp_path)
    assert ("main.c", "util.h") in {
        (e.src, e.dst) for e in g.edges if e.kind is EdgeKind.IMPORTS
    }, "quoted #include must produce an IMPORTS edge"
    per = g.stats.per_language["c"]
    assert per["external_calls"] >= 1, "printf must be external (D005)"
    assert per["functions"] >= 2
    calls = _calls(g)
    assert ("main.c::main", "util.c::helper") in calls, "helper() must resolve cross-file"


def test_cpp_out_of_line_method_qualname(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "foo.cpp",
        """
        class Foo {
        public:
            void bar();
            void inner() {}
        };

        void Foo::bar() {
            this->inner();
            std::sort();
        }
        """,
    )
    g = _extract(tmp_path)
    ids = {n.id for n in g.nodes}
    assert "foo.cpp::Foo.bar" in ids, "out-of-line Foo::bar must get qualname Foo.bar"
    calls = _calls(g)
    assert calls[("foo.cpp::Foo.bar", "foo.cpp::Foo.inner")] == "self"
    per = g.stats.per_language["cpp"]
    assert per["external_calls"] >= 1, "std::sort() must be external"
    assert per["functions"] >= 2


def test_ruby_implicit_self_and_require_relative(tmp_path: Path) -> None:
    _write(tmp_path, "lib/util.rb", "def helper\nend\n")
    _write(
        tmp_path,
        "lib/engine.rb",
        """
        require_relative "util"

        class Engine
          def run
            step()
            helper()
            puts "x"
          end

          def step
          end
        end
        """,
    )
    g = _extract(tmp_path)
    calls = _calls(g)
    assert calls[("lib/engine.rb::Engine.run", "lib/engine.rb::Engine.step")] == "self", (
        "a bare in-class call must resolve SELF"
    )
    assert calls[("lib/engine.rb::Engine.run", "lib/util.rb::helper")] == "exact", (
        "require_relative must bind the required file's functions EXACT"
    )
    per = g.stats.per_language["ruby"]
    assert per["external_calls"] >= 1, "puts must be external"
    assert per["functions"] >= 3


def test_ruby_bare_parenless_call_is_invisible_not_unresolved(tmp_path: Path) -> None:
    """D005 direction: an uncapturable call must not inflate the denominator.

    A paren-less no-arg `step` is grammatically a variable read in Ruby, so it is
    invisible to the extractor — neither counted nor marked unresolved.
    """
    _write(
        tmp_path,
        "lib/one.rb",
        """
        class C
          def run
            step
          end

          def step
          end
        end
        """,
    )
    g = _extract(tmp_path)
    per = g.stats.per_language["ruby"]
    assert per.get("total_calls", 0) == 0
    assert per.get("unresolved_calls", 0) == 0


def test_csharp_attributes_and_implicit_this(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "Engine.cs",
        """
        public class Engine {
            [Fact]
            public void TestRun() { Helper(); }
            private void Helper() { Console.WriteLine("x"); }
        }
        """,
    )
    g = _extract(tmp_path)
    calls = _calls(g)
    assert calls[("Engine.cs::Engine.TestRun", "Engine.cs::Engine.Helper")] == "self"
    tests = {(e.src, e.dst) for e in g.edges if e.kind is EdgeKind.TESTS}
    assert ("Engine.cs::Engine.TestRun", "Engine.cs::Engine.Helper") in tests, (
        "[Fact] must mark the method as a test"
    )
    per = g.stats.per_language["csharp"]
    assert per["external_calls"] >= 1, "Console.WriteLine must be external"
    assert per["functions"] >= 2


def test_php_this_scoped_calls_and_require(tmp_path: Path) -> None:
    _write(tmp_path, "src/util.php", "<?php\nfunction prepare() {}\n")
    _write(
        tmp_path,
        "src/engine.php",
        """
        <?php
        require "util.php";

        class Engine {
            public function run() {
                $this->step();
                self::helper();
                prepare();
                strlen("x");
            }
            public function step() {}
            public static function helper() {}
        }
        """,
    )
    g = _extract(tmp_path)
    calls = _calls(g)
    assert calls[("src/engine.php::Engine.run", "src/engine.php::Engine.step")] == "self"
    assert calls[("src/engine.php::Engine.run", "src/engine.php::Engine.helper")] == "self", (
        "self::helper() must resolve SELF"
    )
    assert calls[("src/engine.php::Engine.run", "src/util.php::prepare")] == "exact", (
        "require 'util.php' must bind the file's functions EXACT"
    )
    per = g.stats.per_language["php"]
    assert per["external_calls"] >= 1, "strlen must be external"
    assert per["functions"] >= 4


def test_tsx_parses_under_tsx_grammar(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/App.tsx",
        """
        export function App(): number {
            return render();
        }

        function render(): number {
            return 1;
        }
        """,
    )
    g = _extract(tmp_path)
    calls = _calls(g)
    assert calls[("src/App.tsx::App", "src/App.tsx::render")] == "local"
    assert g.stats.per_language["typescript"]["functions"] >= 2


def test_js_class_and_local_call(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "lib/app.js",
        """
        class App {
            boot() {
                this.wire();
            }
            wire() {}
        }

        function main() {
            helperFn();
        }

        const helperFn = () => {};
        """,
    )
    g = _extract(tmp_path)
    calls = _calls(g)
    assert calls[("lib/app.js::App.boot", "lib/app.js::App.wire")] == "self"
    assert calls[("lib/app.js::main", "lib/app.js::helperFn")] == "local"
    assert g.stats.per_language["javascript"]["functions"] >= 3
