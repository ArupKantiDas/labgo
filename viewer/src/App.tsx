import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import ForceGraph2D, {
  type ForceGraphMethods,
  type NodeObject,
  type LinkObject,
} from "react-force-graph-2d";
import type {
  EdgeKind,
  GraphLink,
  GraphNode,
  NodeKind,
  RawCochangeEdge,
  RawGraph,
} from "./types";
import "./App.css";

const NODE_COLORS: Record<NodeKind, string> = {
  File: "#5b8def",
  Class: "#f2a541",
  Function: "#4fbf83",
};

const NODE_KIND_FLOOR: Record<NodeKind, number> = {
  File: 5,
  Class: 3,
  Function: 1.5,
};

// CONTAINS is the File->Class->Function tree (1200+ edges) and drowns out
// everything else if shown by default, so start with just the call graph.
// CO_CHANGED is off by default too — impact mode surfaces it in the sidebar
// and as node highlighting regardless of whether the line itself is drawn.
const EDGE_KINDS: EdgeKind[] = ["CALLS", "IMPORTS", "TESTS", "CONTAINS", "CO_CHANGED"];
const DEFAULT_VISIBLE_EDGE_KINDS: EdgeKind[] = ["CALLS"];

const EDGE_COLORS: Record<EdgeKind, string> = {
  CALLS: "#e0607e",
  IMPORTS: "#3fb8c4",
  TESTS: "#9b6bde",
  CONTAINS: "#9298a1",
  CO_CHANGED: "#f2b134",
};

const IMPACT_SELECTED_COLOR = "#ff3b5c";
const IMPACT_CALLER_COLOR = "#ff5470";
const IMPACT_COCHANGE_COLOR = "#f2b134";

// zoomToFit's computed zoom is unbounded in both directions — a single isolated
// node has a tiny bounding box and would otherwise fill the whole screen.
const CAMERA_MAX_ZOOM = 4;
const CAMERA_MIN_ZOOM = 0.4;

type Mode = "explore" | "impact";

interface Neighbor {
  otherId: string;
  edgeKind: EdgeKind;
  dir: "out" | "in";
}

function hexToRgba(hex: string, alpha: number): string {
  const n = parseInt(hex.slice(1), 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function linkEndpointId(end: LinkObject<GraphNode, GraphLink>["source"]): string {
  return typeof end === "object" && end !== null ? String((end as GraphNode).id) : String(end);
}

export default function App() {
  const [graph, setGraph] = useState<RawGraph | null>(null);
  const [cochange, setCochange] = useState<RawCochangeEdge[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [visibleKinds, setVisibleKinds] = useState<Set<EdgeKind>>(
    () => new Set(DEFAULT_VISIBLE_EDGE_KINDS),
  );
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("explore");
  const [hops, setHops] = useState(3);
  const [dims, setDims] = useState(() => ({
    width: Math.max(320, window.innerWidth - 380),
    height: window.innerHeight,
  }));

  const fgRef = useRef<ForceGraphMethods<GraphNode, GraphLink> | undefined>(undefined);

  const [isDark, setIsDark] = useState(
    () => window.matchMedia("(prefers-color-scheme: dark)").matches,
  );
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = (e: MediaQueryListEvent) => setIsDark(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  useEffect(() => {
    fetch("/graph.json")
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load graph.json: ${res.status} ${res.statusText}`);
        return res.json() as Promise<RawGraph>;
      })
      .then(setGraph)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  useEffect(() => {
    // Optional — only present once `labgo history` has been run. A missing
    // file just means no co-change signal, not an error.
    fetch("/cochange.json")
      .then((res) => (res.ok ? (res.json() as Promise<RawCochangeEdge[]>) : []))
      .then(setCochange)
      .catch(() => setCochange([]));
  }, []);

  useEffect(() => {
    const onResize = () =>
      setDims({ width: Math.max(320, window.innerWidth - 380), height: window.innerHeight });
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const nodesById = useMemo(() => {
    const map = new Map<string, GraphNode>();
    graph?.nodes.forEach((n) => map.set(n.id, n));
    return map;
  }, [graph]);

  // Co-change is mined from full git history; the graph is built from the
  // corpus at HEAD. Renamed/deleted files show up as pairs whose endpoints
  // no longer exist as File nodes — same temporal-drift issue as D008,
  // just not worth pinning a benchmark over here. Drop them from the merge,
  // but count them so the UI can say so rather than silently under-report.
  const { combinedEdges, cochangeDroppedCount } = useMemo(() => {
    if (!graph) return { combinedEdges: [] as GraphLink[], cochangeDroppedCount: 0 };
    const fileIds = new Set(graph.nodes.filter((n) => n.kind === "File").map((n) => n.id));
    let dropped = 0;
    const ccLinks: GraphLink[] = [];
    for (const e of cochange) {
      if (fileIds.has(e.src) && fileIds.has(e.dst)) {
        ccLinks.push({ source: e.src, target: e.dst, kind: "CO_CHANGED", props: { count: e.count } });
      } else {
        dropped += 1;
      }
    }
    const base: GraphLink[] = graph.edges.map((e) => ({
      source: e.src,
      target: e.dst,
      kind: e.kind,
      props: e.props,
    }));
    return { combinedEdges: [...base, ...ccLinks], cochangeDroppedCount: dropped };
  }, [graph, cochange]);

  // Full adjacency, independent of the edge-kind filter — drives node sizing
  // and the detail panel, so those stay stable as filters are toggled.
  const neighborsById = useMemo(() => {
    const map = new Map<string, Neighbor[]>();
    const add = (id: string, neighbor: Neighbor) => {
      const arr = map.get(id);
      if (arr) arr.push(neighbor);
      else map.set(id, [neighbor]);
    };
    combinedEdges.forEach((e) => {
      add(e.source, { otherId: e.target, edgeKind: e.kind, dir: "out" });
      add(e.target, { otherId: e.source, edgeKind: e.kind, dir: "in" });
    });
    return map;
  }, [combinedEdges]);

  // Reverse CALLS adjacency: dst -> [callers]. This is the "what breaks"
  // direction — if a function's behaviour changes, its callers are at risk,
  // not its callees.
  const callersById = useMemo(() => {
    const map = new Map<string, string[]>();
    combinedEdges.forEach((e) => {
      if (e.kind !== "CALLS") return;
      const arr = map.get(e.target);
      if (arr) arr.push(e.source);
      else map.set(e.target, [e.source]);
    });
    return map;
  }, [combinedEdges]);

  // Forward CONTAINS adjacency: File -> Class/Function, Class -> Function.
  const containsForward = useMemo(() => {
    const map = new Map<string, string[]>();
    combinedEdges.forEach((e) => {
      if (e.kind !== "CONTAINS") return;
      const arr = map.get(e.source);
      if (arr) arr.push(e.target);
      else map.set(e.source, [e.target]);
    });
    return map;
  }, [combinedEdges]);

  const degreeOf = useCallback((id: string) => neighborsById.get(id)?.length ?? 0, [neighborsById]);

  // A File or Class isn't itself a call-graph node — resolve it to the
  // Functions it contains (recursing through nested classes) so selecting
  // a file analyzes impact for everything defined in it.
  const functionSeedsFor = useCallback(
    (id: string): string[] => {
      const node = nodesById.get(id);
      if (!node) return [];
      if (node.kind === "Function") return [id];
      const seeds: string[] = [];
      const queue = [...(containsForward.get(id) ?? [])];
      const seen = new Set(queue);
      while (queue.length) {
        const cur = queue.shift() as string;
        const curNode = nodesById.get(cur);
        if (curNode?.kind === "Function") {
          seeds.push(cur);
          continue;
        }
        for (const child of containsForward.get(cur) ?? []) {
          if (seen.has(child)) continue;
          seen.add(child);
          queue.push(child);
        }
      }
      return seeds;
    },
    [nodesById, containsForward],
  );

  // Toggling edge kinds reheats the simulation and reshuffles layout, which
  // can strand the camera over empty space if it was zoomed in — reframe.
  const didMountRef = useRef(false);
  useEffect(() => {
    if (!didMountRef.current) {
      didMountRef.current = true;
      return;
    }
    const timer = setTimeout(() => fgRef.current?.zoomToFit(400, 60), 50);
    return () => clearTimeout(timer);
  }, [visibleKinds]);

  const graphData = useMemo(() => {
    if (!graph) return { nodes: [] as GraphNode[], links: [] as GraphLink[] };
    return { nodes: graph.nodes, links: combinedEdges.filter((e) => visibleKinds.has(e.kind)) };
  }, [graph, combinedEdges, visibleKinds]);

  const searchMatches = useMemo(() => {
    if (!graph || !search.trim()) return [];
    const q = search.trim().toLowerCase();
    return graph.nodes
      .filter((n) => n.name.toLowerCase().includes(q) || n.id.toLowerCase().includes(q))
      .slice(0, 40);
  }, [graph, search]);

  // Impact mode is deliberately click-driven, not hover-driven: the BFS
  // below is cheap once, but re-running it on every mouse-move across the
  // canvas would be wasteful and the flicker would make the list unreadable.
  const focusId = mode === "impact" ? selectedId : (hoverId ?? selectedId);
  const selectedNode = selectedId ? (nodesById.get(selectedId) ?? null) : null;

  const highlightSet = useMemo(() => {
    if (mode !== "explore" || !focusId) return null;
    const set = new Set<string>([focusId]);
    (neighborsById.get(focusId) ?? []).forEach((n) => set.add(n.otherId));
    return set;
  }, [mode, focusId, neighborsById]);

  // Same shape as highlightSet, but keyed on selectedId only, never hoverId.
  // The camera must not move on hover — only highlightSet should react to that.
  const selectionNeighbors = useMemo(() => {
    if (mode !== "explore" || !selectedId) return null;
    const set = new Set<string>([selectedId]);
    (neighborsById.get(selectedId) ?? []).forEach((n) => set.add(n.otherId));
    return set;
  }, [mode, selectedId, neighborsById]);

  // Hop distance from the selected node, walking CALLS edges backwards
  // (callee -> caller). Hop 0 = the selected node and, if it's a File or
  // Class, every Function it contains — the surface of the change itself.
  const impact = useMemo(() => {
    if (mode !== "impact" || !selectedId) return null;
    const seeds = functionSeedsFor(selectedId);
    const dist = new Map<string, number>();
    dist.set(selectedId, 0);
    seeds.forEach((s) => dist.set(s, 0));
    let frontier = seeds.length > 0 ? seeds : [selectedId];
    for (let h = 1; h <= hops && frontier.length > 0; h++) {
      const next: string[] = [];
      for (const id of frontier) {
        for (const callerId of callersById.get(id) ?? []) {
          if (!dist.has(callerId)) {
            dist.set(callerId, h);
            next.push(callerId);
          }
        }
      }
      frontier = next;
    }
    return dist;
  }, [mode, selectedId, hops, functionSeedsFor, callersById]);

  const impactCallers = useMemo(() => {
    if (!impact) return [];
    return [...impact.entries()]
      .filter(([, hop]) => hop > 0)
      .sort((a, b) => a[1] - b[1])
      .map(([id, hop]) => ({ id, hop }));
  }, [impact]);

  const impactFileCount = useMemo(() => {
    const files = new Set<string>();
    impactCallers.forEach(({ id }) => {
      const n = nodesById.get(id);
      if (n) files.add(n.path);
    });
    return files.size;
  }, [impactCallers, nodesById]);

  const selectedFilePath = useMemo(() => {
    if (!selectedNode) return null;
    return selectedNode.kind === "File" ? selectedNode.id : selectedNode.path;
  }, [selectedNode]);

  const cochangeForSelected = useMemo(() => {
    if (mode !== "impact" || !selectedFilePath) return [];
    const seen = new Map<string, number>();
    combinedEdges.forEach((e) => {
      if (e.kind !== "CO_CHANGED") return;
      const count = (e.props?.count as number | undefined) ?? 0;
      if (e.source === selectedFilePath) seen.set(e.target, count);
      else if (e.target === selectedFilePath) seen.set(e.source, count);
    });
    return [...seen.entries()]
      .map(([id, count]) => ({ id, count }))
      .sort((a, b) => b.count - a.count);
  }, [mode, selectedFilePath, combinedEdges]);

  const cochangeIdSet = useMemo(() => new Set(cochangeForSelected.map((c) => c.id)), [
    cochangeForSelected,
  ]);

  // What the camera should frame for the current selection: hop-1 neighbors in
  // Explore mode, the call-graph blast radius in Impact mode. Co-changed files
  // are deliberately excluded from the fit — CO_CHANGED edges aren't part of the
  // force simulation (off by default in visibleKinds), so those nodes have no
  // pull toward the call-graph cluster and sit scattered essentially at random.
  // Including them in the bounding box zooms out to fit empty space rather than
  // the thing being analyzed; they're still visible via the sidebar list and
  // amber node coloring, just not used to frame the camera.
  const cameraBoundsIds = useMemo(() => {
    if (!selectedId) return null;
    if (mode === "impact") return impact ? new Set(impact.keys()) : new Set([selectedId]);
    return selectionNeighbors;
  }, [selectedId, mode, impact, selectionNeighbors]);

  // Centralized camera control: every selection change (click, search, sidebar
  // list) funnels through setSelectedId, which recomputes cameraBoundsIds, which
  // this effect frames. No call site does its own centerAt/zoom math.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg || !cameraBoundsIds || cameraBoundsIds.size === 0) return;
    // Guard against fitting to not-yet-simulated positions.
    const anchor = selectedId
      ? (nodesById.get(selectedId) as (GraphNode & { x?: number }) | undefined)
      : undefined;
    if (anchor?.x === undefined) return;

    const fitTimer = setTimeout(() => {
      fg.zoomToFit(400, 100, (n) => cameraBoundsIds.has(String((n as GraphNode).id)));
    }, 50); // let a just-reheated simulation place any newly-relevant nodes first
    const clampTimer = setTimeout(() => {
      const z = fg.zoom();
      if (z > CAMERA_MAX_ZOOM) fg.zoom(CAMERA_MAX_ZOOM, 200);
      else if (z < CAMERA_MIN_ZOOM) fg.zoom(CAMERA_MIN_ZOOM, 200);
    }, 470); // after the 400ms zoomToFit transition (+50ms delay) settles
    return () => {
      clearTimeout(fitTimer);
      clearTimeout(clampTimer);
    };
  }, [cameraBoundsIds, selectedId, nodesById]);

  const focusNode = (id: string) => {
    setSelectedId(id);
  };

  const handleSidebarClick = (e: MouseEvent<HTMLElement>) => {
    // Clicking blank sidebar space (headings, padding, hint text) deselects,
    // same as clicking empty canvas — but not clicks on controls, which
    // already manage selection themselves (e.g. picking a search result).
    const target = e.target as HTMLElement;
    if (target.closest("button, input, label, a")) return;
    setSelectedId(null);
  };

  const toggleEdgeKind = (kind: EdgeKind) => {
    setVisibleKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  };

  const nodeColor = useCallback(
    (node: NodeObject<GraphNode>) => {
      const n = node as GraphNode;
      const base = NODE_COLORS[n.kind];
      if (mode === "impact") {
        if (!selectedId) return hexToRgba(base, 0.3);
        if (n.id === selectedId) return IMPACT_SELECTED_COLOR;
        const hop = impact?.get(n.id);
        if (hop !== undefined && hop > 0) {
          const t = Math.min(1, (hop - 1) / Math.max(1, hops));
          return hexToRgba(IMPACT_CALLER_COLOR, 0.95 - t * 0.55);
        }
        if (cochangeIdSet.has(n.id)) return IMPACT_COCHANGE_COLOR;
        return hexToRgba(base, 0.06);
      }
      if (!highlightSet) return base;
      return highlightSet.has(String(node.id)) ? base : hexToRgba(base, 0.12);
    },
    [mode, selectedId, impact, hops, cochangeIdSet, highlightSet],
  );

  const nodeVal = useCallback(
    (node: NodeObject<GraphNode>) => {
      const n = node as GraphNode;
      return NODE_KIND_FLOOR[n.kind] + Math.sqrt(degreeOf(n.id));
    },
    [degreeOf],
  );

  const linkColor = useCallback(
    (link: LinkObject<GraphNode, GraphLink>) => {
      const l = link as GraphLink;
      const base = EDGE_COLORS[l.kind];
      if (mode === "impact") {
        if (!selectedId) return hexToRgba(base, 0.05);
        const s = linkEndpointId(link.source);
        const t = linkEndpointId(link.target);
        if (l.kind === "CALLS" && impact?.has(s) && impact?.has(t)) return base;
        if (l.kind === "CO_CHANGED" && (s === selectedFilePath || t === selectedFilePath)) {
          return IMPACT_COCHANGE_COLOR;
        }
        return hexToRgba(base, 0.04);
      }
      if (focusId) {
        const touches = linkEndpointId(link.source) === focusId || linkEndpointId(link.target) === focusId;
        return touches ? base : "rgba(140,140,150,0.05)";
      }
      return hexToRgba(base, 0.45);
    },
    [mode, selectedId, impact, selectedFilePath, focusId],
  );

  const linkWidth = useCallback(
    (link: LinkObject<GraphNode, GraphLink>) => {
      const l = link as GraphLink;
      if (mode === "impact") {
        if (!selectedId) return 0.3;
        const s = linkEndpointId(link.source);
        const t = linkEndpointId(link.target);
        if (l.kind === "CALLS" && impact?.has(s) && impact?.has(t)) return 1.8;
        if (l.kind === "CO_CHANGED" && (s === selectedFilePath || t === selectedFilePath)) {
          const count = (l.props?.count as number | undefined) ?? 1;
          return Math.min(4, 1 + Math.log2(count));
        }
        return 0.2;
      }
      if (!focusId) return 0.6;
      const touches = linkEndpointId(link.source) === focusId || linkEndpointId(link.target) === focusId;
      return touches ? 1.8 : 0.4;
    },
    [mode, selectedId, impact, selectedFilePath, focusId],
  );

  const nodeCanvasObject = useCallback(
    (node: NodeObject<GraphNode>, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const n = node as GraphNode & { x?: number; y?: number };
      if (n.x === undefined || n.y === undefined) return;
      const isFocused = focusId === n.id;
      const inImpact = mode === "impact" && (impact?.has(n.id) || cochangeIdSet.has(n.id));
      const dimmed = mode === "impact" ? Boolean(selectedId) && !inImpact : highlightSet ? !highlightSet.has(n.id) : false;
      const showLabel = isFocused || inImpact || globalScale > 4.5 || (n.kind === "File" && globalScale > 1.4);
      if (!showLabel || dimmed) return;

      const fontSize = Math.max(11 / globalScale, isFocused ? 1.8 : 1.4);
      ctx.font = `${isFocused ? "600 " : ""}${fontSize}px system-ui, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = isFocused
        ? isDark
          ? "#f5f6f8"
          : "#111318"
        : isDark
          ? "rgba(230,230,235,0.75)"
          : "rgba(20,20,26,0.75)";
      const r = Math.sqrt(nodeVal(node)) * 3.2;
      ctx.fillText(n.name, n.x, n.y + r + 1);
    },
    [focusId, mode, impact, cochangeIdSet, selectedId, highlightSet, nodeVal, isDark],
  );

  if (error) {
    return (
      <div className="state-screen">
        <h1>Couldn't load graph.json</h1>
        <p>{error}</p>
        <p className="hint">
          Run <code>labgo ingest &lt;repo&gt;</code> then <code>labgo view</code> (see repo
          README).
        </p>
      </div>
    );
  }

  if (!graph) {
    return (
      <div className="state-screen">
        <p>Loading graph…</p>
      </div>
    );
  }

  return (
    <div className="app">
      <aside className="sidebar" onClick={handleSidebarClick}>
        <header>
          <h1>LabGo code graph</h1>
          <p className="subtitle">
            {graph.nodes.length} nodes · {combinedEdges.length} edges
          </p>
        </header>

        <section className="mode-toggle">
          <div className="segmented">
            <button className={mode === "explore" ? "active" : ""} onClick={() => setMode("explore")}>
              Explore
            </button>
            <button className={mode === "impact" ? "active" : ""} onClick={() => setMode("impact")}>
              Impact
            </button>
          </div>
          {mode === "impact" && !selectedNode && (
            <p className="hint">
              Pick a file or function to see what would be affected by changing it — before you
              change it.
            </p>
          )}
        </section>

        <section>
          <h2>Search</h2>
          <input
            className="search-input"
            type="text"
            placeholder="Find a file, class, or function…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          {searchMatches.length > 0 && (
            <ul className="search-results">
              {searchMatches.map((n, i) => (
                // graph.json has a handful of duplicate node ids (property
                // getter/setter pairs sharing a qualified name), so key on
                // index too rather than assuming id uniqueness.
                <li key={`${n.id}-${i}`}>
                  <button onClick={() => focusNode(n.id)} className={`kind-${n.kind}`}>
                    <span className="dot" style={{ background: NODE_COLORS[n.kind] }} />
                    {n.name}
                    <span className="path">{n.path}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        {mode === "impact" && selectedNode && (
          <section className="impact">
            <h2>
              <span className="dot" style={{ background: NODE_COLORS[selectedNode.kind] }} />
              {selectedNode.name}
            </h2>

            <label className="hop-control">
              Hops
              <input
                type="range"
                min={1}
                max={6}
                value={hops}
                onChange={(e) => setHops(Number(e.target.value))}
              />
              <span className="hop-value">{hops}</span>
            </label>

            <p className="impact-summary">
              <strong>{impactCallers.length}</strong>{" "}
              {impactCallers.length === 1 ? "caller" : "callers"} across{" "}
              <strong>{impactFileCount}</strong> {impactFileCount === 1 ? "file" : "files"}
              {" · "}
              <strong>{cochangeForSelected.length}</strong> co-changed historically
            </p>

            <h3>Callers within {hops} hops</h3>
            {impactCallers.length === 0 ? (
              <p className="hint">
                None found by static call resolution — either nothing calls this, or the calls
                are dynamic/unresolved (27% resolution on httpx, see D004). Check co-change below.
              </p>
            ) : (
              <ul className="impact-list">
                {impactCallers.map(({ id, hop }) => {
                  const other = nodesById.get(id);
                  if (!other) return null;
                  return (
                    <li key={id}>
                      <button onClick={() => focusNode(id)}>
                        <span className="dot" style={{ background: NODE_COLORS[other.kind] }} />
                        {other.name}
                        <span className="path">{other.path}</span>
                        <span className="hop-badge">{hop}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}

            <h3>Changed together historically</h3>
            {cochangeForSelected.length === 0 ? (
              <p className="hint">
                No co-change evidence for this file — either it's unrelated, or{" "}
                <code>labgo history</code> hasn't been run.
              </p>
            ) : (
              <ul className="impact-list">
                {cochangeForSelected.map(({ id, count }) => (
                  <li key={id}>
                    <button onClick={() => focusNode(id)}>
                      <span className="dot" style={{ background: IMPACT_COCHANGE_COLOR }} />
                      {id}
                      <span className="count-badge">{count}×</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}

        <section>
          <h2>Node kinds</h2>
          <ul className="legend">
            {(Object.keys(NODE_COLORS) as NodeKind[]).map((kind) => (
              <li key={kind}>
                <span className="dot" style={{ background: NODE_COLORS[kind] }} />
                {kind}
                <span className="count">
                  {graph.nodes.filter((n) => n.kind === kind).length}
                </span>
              </li>
            ))}
          </ul>
        </section>

        <section>
          <h2>Edge kinds</h2>
          <ul className="legend legend-toggle">
            {EDGE_KINDS.map((kind) => (
              <li key={kind}>
                <label>
                  <input
                    type="checkbox"
                    checked={visibleKinds.has(kind)}
                    onChange={() => toggleEdgeKind(kind)}
                  />
                  <span className="line" style={{ background: EDGE_COLORS[kind] }} />
                  {kind}
                  <span className="count">
                    {combinedEdges.filter((e) => e.kind === kind).length}
                  </span>
                </label>
              </li>
            ))}
          </ul>
          <p className="hint">CONTAINS is the file/class/function tree — noisy by default, toggle it on to see structure.</p>
          {cochangeDroppedCount > 0 && (
            <p className="hint">
              {cochangeDroppedCount} co-change pairs reference files renamed or deleted since —
              excluded (same drift as D008).
            </p>
          )}
        </section>

        {mode === "explore" && selectedNode && (
          <section className="detail">
            <h2>
              <span className="dot" style={{ background: NODE_COLORS[selectedNode.kind] }} />
              {selectedNode.name}
            </h2>
            <dl>
              <dt>Kind</dt>
              <dd>{selectedNode.kind}</dd>
              <dt>Path</dt>
              <dd className="mono">{selectedNode.path}</dd>
              {selectedNode.lineno !== null && (
                <>
                  <dt>Lines</dt>
                  <dd>
                    {selectedNode.lineno}
                    {selectedNode.end_lineno ? `–${selectedNode.end_lineno}` : ""}
                  </dd>
                </>
              )}
              {Object.entries(selectedNode.props).map(([k, v]) => (
                <div key={k} className="prop-row">
                  <dt>{k}</dt>
                  <dd>{String(v)}</dd>
                </div>
              ))}
            </dl>

            <h3>Connections ({neighborsById.get(selectedNode.id)?.length ?? 0})</h3>
            <ul className="neighbors">
              {(neighborsById.get(selectedNode.id) ?? []).map((nb, i) => {
                const other = nodesById.get(nb.otherId);
                if (!other) return null;
                return (
                  <li key={`${nb.otherId}-${nb.edgeKind}-${nb.dir}-${i}`}>
                    <button onClick={() => focusNode(other.id)}>
                      <span className="dot" style={{ background: NODE_COLORS[other.kind] }} />
                      <span className="arrow">{nb.dir === "out" ? "→" : "←"}</span>
                      {other.name}
                      <span className="edge-kind" style={{ color: EDGE_COLORS[nb.edgeKind] }}>
                        {nb.edgeKind}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        )}
      </aside>

      <main className="canvas-area">
        <ForceGraph2D
          ref={fgRef}
          graphData={graphData}
          width={dims.width}
          height={dims.height}
          nodeId="id"
          nodeVal={nodeVal}
          nodeColor={nodeColor}
          nodeLabel={(n) => `${(n as GraphNode).kind}: ${(n as GraphNode).path}`}
          nodeCanvasObjectMode={() => "after"}
          nodeCanvasObject={nodeCanvasObject}
          linkColor={linkColor}
          linkWidth={linkWidth}
          linkDirectionalArrowLength={3}
          linkDirectionalArrowRelPos={1}
          onNodeClick={(node) => focusNode(String(node.id))}
          onNodeHover={(node) => setHoverId(node ? String(node.id) : null)}
          onBackgroundClick={() => setSelectedId(null)}
          cooldownTicks={150}
          backgroundColor={isDark ? "#14151a" : "#fbfbfc"}
        />
      </main>
    </div>
  );
}
