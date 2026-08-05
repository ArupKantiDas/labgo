export type NodeKind = "File" | "Class" | "Function";
export type EdgeKind = "CONTAINS" | "CALLS" | "TESTS" | "IMPORTS" | "CO_CHANGED";

export interface RawGraphNode {
  id: string;
  kind: NodeKind;
  name: string;
  path: string;
  lineno: number | null;
  end_lineno: number | null;
  props: Record<string, unknown>;
}

export interface RawGraphEdge {
  src: string;
  dst: string;
  kind: EdgeKind;
  props: Record<string, unknown>;
}

export interface RawGraph {
  nodes: RawGraphNode[];
  edges: RawGraphEdge[];
  stats: Record<string, number>;
}

// From `labgo history` — git co-change evidence. File -> File, keyed by the
// same ids as File nodes (their path), so it merges into the graph directly.
export interface RawCochangeEdge {
  src: string;
  dst: string;
  count: number;
}

// Shape react-force-graph-2d wants: nodes keep their fields, links use
// source/target (it mutates these in place to point at node objects once the
// simulation resolves them, so keep this distinct from RawGraphEdge).
export interface GraphNode extends RawGraphNode {}

export interface GraphLink {
  source: string;
  target: string;
  kind: EdgeKind;
  props?: Record<string, unknown>;
}
