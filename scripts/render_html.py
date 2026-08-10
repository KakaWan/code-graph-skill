#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
code-graph render_html.py - emit a self-contained HTML viewer for the graph.

Reads .code-graph/graph.json and writes .code-graph/code-graph.html inside
the project's .code-graph/ directory (all code-graph artifacts live under
.code-graph/, never in the project root). The HTML inlines all graph data,
styles and scripts: open it in any browser with zero dependencies (no
network, no third-party libs, file:// friendly). Regenerated on every
update.py run, so it always reflects the latest graph.

Features:
  - four views: module graph / file graph / class graph / function graph
  - click a node -> side panel with details (signature, file:line,
    callers/callees, module summary, symbol lists...)
  - wheel zoom (centered on cursor), drag empty space to pan,
    drag nodes to rearrange, double-click to zoom into a node
  - search box jumps to any node across all views
  - links drawn as straight segments with optional direction arrows
  - hand-written force simulation (grid-accelerated repulsion) on Canvas,
    smooth for thousands of nodes

Usage:
  python3 render_html.py --root <project-root>
  python3 render_html.py --root <project-root> --out <path>

Called automatically at the end of update.py (skip with --no-html); run
standalone to regenerate without re-scanning sources.
"""

import argparse
import json
import os
import sys

OUT_FILE = "code-graph.html"


def qname_of(pkg, owner, name):
    prefix = pkg or ""
    if owner:
        prefix = (prefix + "." + owner) if prefix else owner
    return (prefix + "." + name) if prefix else name


def short_name(qn):
    return qn.rsplit(".", 1)[-1]


def build_views(graph):
    """Flatten graph.json into per-view {nodes, links} with rich node details."""
    files = graph.get("files", {}) or {}
    table = graph.get("symbols", {}) or {}
    edges = graph.get("edges", {}) or {}
    modules = graph.get("modules", []) or []

    # qname -> owner short class name (for class-view edges).
    owner_of = {}
    for rel, fdata in files.items():
        pkg = (fdata or {}).get("package") or ""
        for s in (fdata or {}).get("symbols", []) or []:
            owner_of.setdefault(
                qname_of(pkg, s.get("owner"), s.get("name", "")),
                s.get("owner"),
            )

    file_module = {}
    for m in modules:
        for f in m.get("files", []) or []:
            file_module[f] = m.get("id", "")

    # Flatten call edges into (src, dst) qname pairs.
    call_pairs = []
    for out in edges.values():
        for src, dsts in (out or {}).items():
            for dst in dsts or []:
                call_pairs.append((src, dst))

    # ---- functions view ----
    fun_ids = [qn for qn, v in table.items() if v.get("kind") == "fun"]
    fun_set = set(fun_ids)
    fun_out, fun_in = {}, {}
    for src, dst in call_pairs:
        if src in fun_set and dst in fun_set:
            fun_out.setdefault(src, set()).add(dst)
        if dst in fun_set:
            # callers may be classes (Kotlin property initializers / init{}
            # blocks attribute their calls to the owning class) — keep them
            # so "called by" still shows who uses the function.
            fun_in.setdefault(dst, set()).add(src)

    fun_nodes = []
    for qn in sorted(fun_ids):
        t = table[qn]
        outs = sorted(fun_out.get(qn, set()))
        ins = sorted(fun_in.get(qn, set()))
        degree = len(outs) + len(ins)
        fun_nodes.append({
            "id": qn,
            "label": short_name(qn),
            "sub": "%s:%d" % (t["file"], t.get("line", 0)),
            "size": 9 + min(8, int(degree * 0.5)),
            "detail": {
                "kind": "fun",
                "qname": qn,
                "signature": t.get("signature", ""),
                "comment": t.get("comment", ""),
                "file": t["file"],
                "line": t.get("line", 0),
                "module": file_module.get(t["file"], ""),
                "callers": [
                    {"id": s, "label": short_name(s),
                     "file": table[s]["file"] if s in table else ""}
                    for s in ins[:200]
                ],
                "calls": [
                    {"id": d, "label": short_name(d), "file": table[d]["file"]}
                    for d in outs[:200]
                ],
            },
        })
    fun_links = [{"s": src, "t": dst}
                 for src in sorted(fun_out) for dst in sorted(fun_out[src])]

    # ---- files view (cross-file call edges) ----
    file_out, file_in = {}, {}
    for src, dst in call_pairs:
        # src may be a pseudo-caller (`<Owner>.__property__`) not in the table.
        fs = table[src]["file"] if src in table else ""
        fd = table[dst]["file"] if dst in table else ""
        if fs != fd:
            file_out.setdefault(fs, set()).add(fd)
            file_in.setdefault(fd, set()).add(fs)

    file_nodes = []
    for rel in sorted(files):
        fdata = files[rel] or {}
        syms = fdata.get("symbols", []) or []
        outs = sorted(file_out.get(rel, set()))
        ins = sorted(file_in.get(rel, set()))
        file_nodes.append({
            "id": rel,
            "label": os.path.basename(rel),
            "sub": os.path.dirname(rel) or "/",
            "size": 14 + min(12, len(syms) // 2),
            "detail": {
                "kind": "file",
                "path": rel,
                "lang": fdata.get("lang", ""),
                "package": fdata.get("package", ""),
                "module": file_module.get(rel, ""),
                "symbols": [
                    {"name": s["name"], "kind": s["kind"], "line": s.get("line", 0)}
                    for s in syms[:300]
                ],
                "calls": [{"id": x, "label": os.path.basename(x)} for x in outs],
                "calledBy": [{"id": x, "label": os.path.basename(x)} for x in ins],
            },
        })
    file_links = [{"s": rel, "t": d}
                  for rel in sorted(file_out) for d in sorted(file_out[rel])]

    # ---- classes view (edges between classes owning calling functions) ----
    cls_ids = [qn for qn, v in table.items() if v.get("kind") == "class"]
    cls_set = set(cls_ids)
    cls_by_short = {}
    for qn in cls_ids:
        cls_by_short.setdefault(short_name(qn), qn)

    cls_out, cls_in = {}, {}
    for src, dst in call_pairs:
        so = cls_by_short.get(owner_of.get(src))
        do = cls_by_short.get(owner_of.get(dst))
        if so and do and so in cls_set and do in cls_set and so != do:
            cls_out.setdefault(so, set()).add(do)
            cls_in.setdefault(do, set()).add(so)

    cls_methods = {}
    for rel, fdata in files.items():
        for s in (fdata or {}).get("symbols", []) or []:
            if s.get("kind") == "fun" and s.get("owner"):
                cn = cls_by_short.get(s["owner"])
                if cn:
                    cls_methods.setdefault(cn, []).append(
                        {"name": s["name"], "line": s.get("line", 0)})

    cls_nodes = []
    for qn in sorted(cls_ids):
        t = table[qn]
        outs = sorted(cls_out.get(qn, set()))
        ins = sorted(cls_in.get(qn, set()))
        methods = cls_methods.get(qn, [])
        cls_nodes.append({
            "id": qn,
            "label": short_name(qn),
            "sub": t["file"],
            "size": 12 + min(10, len(methods) // 3),
            "detail": {
                "kind": "class",
                "qname": qn,
                "comment": t.get("comment", ""),
                "file": t["file"],
                "line": t.get("line", 0),
                "module": file_module.get(t["file"], ""),
                "methods": methods[:150],
                "refs": [
                    {"id": x, "label": short_name(x), "file": table[x]["file"]}
                    for x in outs
                ],
                "refdBy": [
                    {"id": x, "label": short_name(x), "file": table[x]["file"]}
                    for x in ins
                ],
            },
        })
    cls_links = [{"s": a, "t": b}
                 for a in sorted(cls_out) for b in sorted(cls_out[a])]

    # ---- modules view (import-level deps) ----
    dep_by = {}
    for m in modules:
        for d in set(m.get("deps", []) or []):
            dep_by.setdefault(d, []).append(m.get("id", ""))

    mod_nodes = []
    for m in modules:
        mid = m.get("id", "")
        mod_nodes.append({
            "id": mid,
            "label": mid,
            "sub": m.get("path", ""),
            "size": 22 + min(18, (m.get("fileCount", 0) or 0) * 2),
            "detail": {
                "kind": "module",
                "id": mid,
                "path": m.get("path", ""),
                "fileCount": m.get("fileCount", 0),
                "symbolCount": m.get("symbolCount", 0),
                "summary": m.get("summary", ""),
                "deps": [{"id": x, "label": x} for x in sorted(set(m.get("deps", []) or []))],
                "depBy": [{"id": x, "label": x} for x in sorted(set(dep_by.get(mid, [])))],
                "files": (m.get("files", []) or [])[:300],
            },
        })
    mod_links = [{"s": m.get("id", ""), "t": d}
                 for m in modules for d in sorted(set(m.get("deps", []) or []))]

    views = {
        "modules": {"nodes": mod_nodes, "links": mod_links},
        "files": {"nodes": file_nodes, "links": file_links},
        "classes": {"nodes": cls_nodes, "links": cls_links},
        "functions": {"nodes": fun_nodes, "links": fun_links},
    }
    counts = {k: {"nodes": len(v["nodes"]), "links": len(v["links"])}
              for k, v in views.items()}
    return views, counts


def generate(root, graph, out_path):
    views, counts = build_views(graph)
    data = {
        "meta": {
            "title": os.path.basename(os.path.abspath(root)) or root,
            "generated": graph.get("generatedAt", ""),
            "counts": counts,
        },
        "views": views,
    }
    # Escape "<" so data embedded in <script> can never terminate the tag.
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    html = HTML_TEMPLATE.replace("__DATA__", payload)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return len(html)


def main(argv=None):
    ap = argparse.ArgumentParser(description="code-graph HTML viewer generator")
    ap.add_argument("--root", default=".", help="project root (default: cwd)")
    ap.add_argument("--out", default=None, help="output html path (default: <root>/.code-graph/code-graph.html)")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    out_dir = os.path.join(root, ".code-graph")
    graph_path = os.path.join(out_dir, "graph.json")
    if not os.path.exists(graph_path):
        print("error: no graph at %s (run update.py first)" % graph_path, file=sys.stderr)
        return 1
    try:
        with open(graph_path, "r", encoding="utf-8") as f:
            graph = json.load(f)
    except Exception as exc:
        print("error: cannot read graph: %s" % exc, file=sys.stderr)
        return 1

    out_path = args.out or os.path.join(out_dir, OUT_FILE)
    size = generate(root, graph, out_path)
    print("wrote %s (%d bytes)" % (out_path.replace(os.sep, "/"), size))
    return 0


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>依赖图</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { height: 100%; overflow: hidden; background: #10141c; color: #e2e8f0;
  font: 13px/1.5 system-ui, "Segoe UI", "Microsoft YaHei", sans-serif; }
#toolbar { position: fixed; top: 0; left: 0; right: 0; height: 48px; z-index: 10;
  display: flex; align-items: center; gap: 8px; padding: 0 12px;
  background: #151b27; border-bottom: 1px solid #232c3d; }
.brand { font-weight: 700; font-size: 13px; color: #7dd3fc; white-space: nowrap; }
.brand small { color: #64748b; font-weight: 400; font-size: 11px; margin-left: 8px; }
.btn-group { display: flex; gap: 2px; }
button { background: #1d2637; color: #cbd5e1; border: 1px solid #2c3a52;
  border-radius: 6px; padding: 4px 10px; cursor: pointer; font-size: 12px; }
button:hover { background: #263449; }
button.on { background: #0e7490; border-color: #0e7490; color: #fff; }
#relayout { margin-left: 4px; }
label.chk { display: flex; align-items: center; gap: 4px; font-size: 12px;
  color: #94a3b8; cursor: pointer; white-space: nowrap; user-select: none; }
#searchBox { position: relative; margin-left: auto; }
#search { background: #1d2637; border: 1px solid #2c3a52; border-radius: 6px;
  color: #e2e8f0; padding: 4px 10px; font-size: 12px; width: 210px; outline: none; }
#search:focus { border-color: #0e7490; }
#results { position: absolute; top: 100%; right: 0; width: 320px; max-height: 380px;
  overflow: auto; background: #171d29; border: 1px solid #2c3a52; border-radius: 8px;
  margin-top: 4px; z-index: 20; }
.ri { padding: 6px 10px; cursor: pointer; border-bottom: 1px solid #1f2937; }
.ri:hover { background: #1f2937; }
.ri b { display: block; color: #e2e8f0; font-weight: 600; font-size: 12px; }
.ri i { color: #64748b; font-size: 11px; font-style: normal; }
#wrap { position: fixed; top: 48px; left: 0; right: 0; bottom: 0; }
#cv { width: 100%; height: 100%; display: block; cursor: grab; }
#panel { position: fixed; top: 56px; right: 10px; bottom: 10px; width: 330px;
  background: #151b27; border: 1px solid #232c3d; border-radius: 10px; z-index: 15;
  display: flex; flex-direction: column; overflow: hidden; }
#panel[hidden] { display: none; }
#panelHead { display: flex; align-items: center; padding: 10px 12px;
  border-bottom: 1px solid #232c3d; }
#panelTitle { flex: 1; font-weight: 700; font-size: 13px; color: #f1f5f9;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#panelClose { background: none; border: none; color: #94a3b8; font-size: 18px;
  cursor: pointer; padding: 0 4px; line-height: 1; }
#panelClose:hover { color: #fff; }
#panelBody { flex: 1; overflow: auto; padding: 10px 12px; }
.kv { display: flex; gap: 8px; margin-bottom: 6px; }
.kv .k { color: #64748b; flex: 0 0 52px; padding-top: 1px; }
.kv .v { color: #e2e8f0; word-break: break-all; white-space: pre-wrap; }
.sec { margin-top: 12px; }
.sec-t { color: #7dd3fc; font-size: 11px; font-weight: 700; margin-bottom: 4px; }
.sec a { display: flex; flex-direction: column; padding: 4px 8px; border-radius: 6px;
  text-decoration: none; }
.sec a:hover { background: #1f2937; }
.sec a b { color: #cbd5e1; font-size: 12px; font-weight: 500; }
.sec a i { color: #64748b; font-size: 11px; font-style: normal; }
.sym { padding: 3px 8px; font-size: 12px; color: #94a3b8; word-break: break-all;
  font-family: Consolas, Menlo, monospace; }
.sym span { color: #475569; font-size: 11px; }
#legend { position: fixed; left: 12px; bottom: 10px; z-index: 10; display: flex;
  gap: 14px; align-items: center; font-size: 11px; color: #64748b;
  background: rgba(16, 20, 28, .72); padding: 5px 10px; border-radius: 8px; }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
  margin-right: 4px; vertical-align: -1px; }
.dot.c0 { background: #f43f5e; }
.dot.c1 { background: #f97316; }
.dot.c2 { background: #eab308; }
.dot.c3 { background: #84cc16; }
#legend .hint { color: #475569; margin-left: 6px; }
</style>
</head>
<body>
<div id="toolbar">
  <div class="brand">CODE GRAPH <span id="proj"></span><small id="gen"></small></div>
  <div class="btn-group" id="viewBtns"></div>
  <button id="relayout" title="重新运行布局">重新布局</button>
  <button id="fit" title="缩放平移以适配全部节点">适应窗口</button>
  <label class="chk" title="显示/隐藏连线的方向箭头（边按引用方向绘制）"><input type="checkbox" id="arrows" checked> 箭头</label>
  <label class="chk" title="显示/隐藏孤立节点（无任何连边的节点孤立成堆，放在图外圈最下方）"><input type="checkbox" id="isolated"> 孤立节点</label>
  <label class="chk" title="开启后弹簧会把同一分区内的有边节点向理想距离拉拢（每个簇/岛屿内部各自收紧，分区之间互不拉扯；开启时节点会持续轻微微动）"><input type="checkbox" id="springs"> 弹性拉近</label>
  <div id="searchBox">
    <input id="search" type="text" placeholder="搜索节点（名称 / 路径）">
    <div id="results" hidden></div>
  </div>
</div>
<div id="wrap"><canvas id="cv"></canvas></div>
<div id="panel" hidden>
  <div id="panelHead"><div id="panelTitle"></div>
    <button id="panelClose" title="关闭 (Esc)">&times;</button></div>
  <div id="panelBody"></div>
</div>
<div id="legend">
  <span><i class="dot c0"></i><i class="dot c1"></i><i class="dot c2"></i><i class="dot c3"></i>颜色 = 所属模块（分类用） · 节点大小 = 关联数量 · 位置 = 按引用关系分堆</span>
  <span class="hint">滚轮缩放 · 拖拽平移 / 拖节点重排 · 双击聚焦 · 点节点看详情</span>
</div>
<script>
"use strict";
const DATA = __DATA__;
const VIEWS = DATA.views;
const META = DATA.meta;

const VIEW_TITLES = { modules: "模块图", files: "文件图",
  classes: "类图", functions: "函数图" };
// Auto color per module (module view colors per module id; other views color
// by the node's owning module so nodes of one module share a hue and clusters
// read apart). No module (unclassified files) -> neutral gray.
const PALETTE = ["#f43f5e", "#f97316", "#eab308", "#84cc16", "#22c55e",
  "#14b8a6", "#06b6d4", "#0ea5e9", "#6366f1", "#8b5cf6", "#d946ef",
  "#ec4899", "#f59e0b", "#10b981", "#3b82f6", "#a3e635", "#fbbf24",
  "#2dd4bf", "#60a5fa", "#c084fc"];
const GRAY = "#94a3b8";
function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h;
}
function nodeColor(n) {
  const d = n.detail || {};
  const m = d.module || (state.view === "modules" ? (d.qname || d.id || "") : "");
  if (!m) return GRAY;
  return PALETTE[hashStr(m) % PALETTE.length];
}
const COLORS = { modules: "#ffb347", files: "#5aa9ff",
  classes: "#b06bff", functions: "#2dd4a7" };
const EDGE = "rgba(138,148,166,0.55)";
const EDGE_DIM = "rgba(138,148,166,0.06)";

const canvas = document.getElementById("cv");
const ctx = canvas.getContext("2d");
const wrap = document.getElementById("wrap");
const panel = document.getElementById("panel");
const panelBody = document.getElementById("panelBody");
const search = document.getElementById("search");
const results = document.getElementById("results");
const isolatedBox = document.getElementById("isolated");
const springsBox = document.getElementById("springs");
const arrowsBox = document.getElementById("arrows");

let W = 0, H = 0, DPR = 1;

const state = {
  view: "modules",
  // "孤立节点" default OFF in every view: no-link nodes must be visible —
  // they live in their own pile below the ring (see initPositions). Users
  // can still hide them via the checkbox.
  isolated: { modules: false, files: false, classes: false, functions: false },
  nodes: [], links: [],
  poses: {}, vels: {},
  scale: 1, ox: 0, oy: 0,
  running: false, frame: 0,
  springs: false,   // spring force on by default? No: springs pull linked nodes
                    // together and make dragging ripple through neighbors — the
                    // layout is static unless the user explicitly enables this.
  autoFit: false, userMovedView: false,
  selected: null, hovered: null,
  dragging: null, panning: false,
  p0: { x: 0, y: 0 }, moved: false,
  mx: 0, my: 0,
};

function resize() {
  DPR = window.devicePixelRatio || 1;
  W = wrap.clientWidth; H = wrap.clientHeight;
  canvas.width = Math.round(W * DPR);
  canvas.height = Math.round(H * DPR);
  canvas.style.width = W + "px";
  canvas.style.height = H + "px";
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  dirty = true;
}
window.addEventListener("resize", resize);

function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* ---------- node set filtering ---------- */
function setViewNodes() {
  const V = VIEWS[state.view];
  if (!state.isolated[state.view]) {
    state.nodes = V.nodes; state.links = V.links;
    return;
  }
  const deg = {};
  for (const l of V.links) { deg[l.s] = (deg[l.s] || 0) + 1; deg[l.t] = (deg[l.t] || 0) + 1; }
  state.nodes = V.nodes.filter(n => (deg[n.id] || 0) > 0);
  state.links = V.links.filter(l => deg[l.s] > 0 && deg[l.t] > 0);
}

/* ---------- static layout (springs optional) ---------- */
function initPositions() {
  const nodes = state.nodes;
  const map = new Map(), vmap = new Map();
  const n = Math.max(1, nodes.length);
  // Deterministic static layout, one rule for every view:
  //   - regions: iso pile (no links at all), islands (hub stars + detached
  //     small components), main ring (the main component), and radial leaves
  //     (degree-1 nodes hugging their single neighbor)
  //   - the main ring is grouped by RELATION (hub-centered BFS clusters),
  //     NOT by module — color still tags the module, position follows links
  //   - node size scales with link count (area ∝ degree)
  // Springs are OFF by default — the layout is purely static, so nothing
  // ever drifts or jitters. With springs on, and while dragging, radial
  // leaves and island members follow their owner rigidly (familyByOwner).
  const K = 800 / Math.sqrt(Math.max(1, n));
  const CELL = clamp(K * 0.85, 34, 96);
  const deg = new Map(), inDeg = new Map(), outDeg = new Map();
  for (const nd of nodes) { deg.set(nd.id, 0); inDeg.set(nd.id, 0); outDeg.set(nd.id, 0); }
  for (const l of state.links) {
    if (deg.has(l.s)) deg.set(l.s, deg.get(l.s) + 1);
    if (deg.has(l.t)) deg.set(l.t, deg.get(l.t) + 1);
    if (inDeg.has(l.t)) inDeg.set(l.t, inDeg.get(l.t) + 1);
    if (outDeg.has(l.s)) outDeg.set(l.s, outDeg.get(l.s) + 1);
  }
  // ---- regions ----
  // 1) iso pile: nodes with NO link at all (in=0 AND out=0).
  const iso = nodes.filter(nd => !(inDeg.get(nd.id) || 0) && !(outDeg.get(nd.id) || 0));
  const isoSet = new Set(iso.map(nd => nd.id));

  // 2) islands: self-contained subgraphs that stay out of the main ring —
  //    small connected components of their own, and "hub stars" (a hub with
  //    its private leaves) detached from the main component.
  const adj = new Map();
  for (const nd of nodes) adj.set(nd.id, new Set());
  for (const l of state.links) {
    if (adj.has(l.s) && adj.has(l.t)) { adj.get(l.s).add(l.t); adj.get(l.t).add(l.s); }
  }
  const udeg = (x) => (adj.get(x) || new Set()).size;
  // Node size ∝ link count (area scales with degree; capped so ring slots
  // stay clear). iso nodes stay small dots.
  for (const nd of nodes) {
    nd.size = Math.max(3, Math.min(17, Math.round(3 + Math.sqrt(udeg(nd.id)) * 1.4)));
  }
  // Connected components (undirected) of the linked nodes.
  const compOf = new Map(), comps = [];
  for (const nd of nodes) {
    if (isoSet.has(nd.id) || compOf.has(nd.id)) continue;
    const stack = [nd.id], comp = [];
    compOf.set(nd.id, comps.length);
    while (stack.length) {
      const x = stack.pop();
      comp.push(x);
      for (const y of adj.get(x) || []) {
        if (!compOf.has(y)) { compOf.set(y, comps.length); stack.push(y); }
      }
    }
    comps.push(comp);
  }
  let mainIdx = 0;
  comps.forEach((c, i) => { if (c.length > comps[mainIdx].length) mainIdx = i; });

  // Hub stars: a hub (undirected degree >= 4) with >= 3 private leaves
  // (leaf: degree <= 2 whose neighbors are all the hub or other low-degree
  // nodes) and <= 3 outside connections — the group is self-contained enough
  // for its own island instead of crowding the ring.
  const mainSet = new Set(comps[mainIdx] || []);
  const stars = [];
  for (const h of mainSet) {
    if (udeg(h) < 4) continue;
    const leaves = [...(adj.get(h) || [])].filter(l =>
      udeg(l) <= 2 && [...(adj.get(l) || [])].every(x => x === h || udeg(x) <= 2));
    if (leaves.length >= 3 && udeg(h) - leaves.length <= 3) {
      stars.push({ hub: h, leaves });
    }
  }
  const starSet = new Set();
  stars.forEach(s => { starSet.add(s.hub); s.leaves.forEach(l => starSet.add(l)); });

  // 3) main ring groups by RELATION, not by module: multi-source BFS from the
  //    high-degree hubs (biggest hubs claim their reachable neighborhood
  //    first), so tightly-linked nodes sit in one cluster and color stays a
  //    classification, not a position.
  const mainNodes = (comps[mainIdx] || []).filter(id => !starSet.has(id));
  const HUB_MIN = Math.max(4, Math.round(Math.sqrt(comps[mainIdx].length || 1)));
  const hubList = mainNodes.filter(id => udeg(id) >= HUB_MIN)
    .sort((a, b) => udeg(b) - udeg(a) || (a < b ? -1 : 1));
  const groupOf = new Map(), queue = [];
  for (const h of hubList) { groupOf.set(h, h); queue.push(h); }
  for (let qi = 0; qi < queue.length; qi++) {
    const x = queue[qi];
    for (const y of adj.get(x) || []) {
      if (isoSet.has(y) || starSet.has(y) || groupOf.has(y)) continue;
      groupOf.set(y, groupOf.get(x));
      queue.push(y);
    }
  }
  const groups = new Map();
  for (const id of mainNodes) {
    const key = groupOf.get(id) || "__rest__";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(id);
  }

  // Islands list: hub stars first, then non-main components (>= 2 nodes).
  const islands = [];
  stars.forEach(s => islands.push({ nodes: [s.hub].concat(s.leaves) }));
  comps.forEach((c, i) => {
    if (i !== mainIdx && c.length >= 2) islands.push({ nodes: c });
  });

  // Chain tails: nodes the hub BFS cannot reach because every path to a hub
  // crosses a star member (e.g. deleteByIds -> deleteEmailHistory under the
  // performDelete star). Each such block folds into the region its neighbors
  // belong to — the most-connected adjacent region (star island or main-ring
  // cluster) wins — instead of forming a tiny standalone "__rest__" cluster.
  const islandIdx = new Map();
  islands.forEach((is, i) => is.nodes.forEach(id => islandIdx.set(id, i)));
  const restN = mainNodes.filter(id => !groupOf.has(id));
  const restSeen = new Set();
  for (const id of restN) {
    if (restSeen.has(id)) continue;
    const blk = [id]; restSeen.add(id);
    const stk = [id];
    while (stk.length) {
      const x = stk.pop();
      for (const y of adj.get(x) || []) {
        if (!restSeen.has(y) && restN.includes(y)) { restSeen.add(y); stk.push(y); blk.push(y); }
      }
    }
    // count edges to each adjacent region (cluster or island)
    const edgeTo = new Map();
    for (const x of blk) {
      for (const y of adj.get(x) || []) {
        if (blk.includes(y)) continue;
        const rk = groupOf.has(y) ? "c:" + groupOf.get(y)
          : (islandIdx.has(y) ? "i:" + islandIdx.get(y) : null);
        if (rk) edgeTo.set(rk, (edgeTo.get(rk) || 0) + 1);
      }
    }
    let best = null, bestN = 0;
    for (const [rk, n] of edgeTo) if (n > bestN) { bestN = n; best = rk; }
    if (best) {
      if (best.startsWith("c:")) {
        const ck = best.slice(2);
        for (const x of blk) { groupOf.set(x, ck); groups.get(ck).push(x); }
      } else {
        const is = islands[Number(best.slice(2))];
        for (const x of blk) { is.nodes.push(x); islandIdx.set(x, Number(best.slice(2))); }
      }
    }
    // else: genuinely detached — stays "__rest__" via the groupOf fallback.
  }
  // Re-collect groups: island-folded nodes leave the ring, cluster-folded
  // nodes were appended above, and any leftover rest block keeps "__rest__".
  groups.clear();
  for (const id of mainNodes) {
    if (islandIdx.has(id)) continue;
    const key = groupOf.get(id) || "__rest__";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(id);
  }

  let list = [...groups.entries()].sort(
    (a, b) => b[1].length - a[1].length || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  const arranged = [];
  let lo = 0, hi = list.length - 1;
  while (lo <= hi) { arranged.push(list[lo++]); if (lo <= hi) arranged.push(list[hi--]); }
  list = arranged;
  function ringCount(cnt) { let R = 0, acc = 1; while (acc < cnt) { R++; acc += 6 * R; } return R; }
  const radii = list.map(([, gs]) => 15 + ringCount(gs.length) * CELL);
  let maxR = 0, sumR = 0;
  for (const r of radii) { maxR = Math.max(maxR, r); sumR += r; }
  // Ring big enough that arc length >= the diameters that share it: with
  // adjacent-cluster radii interleaved big/small, 2*pi*R ~= 2*sumR suffices.
  const R_BIG = Math.max(420, sumR / Math.PI + maxR + 40);
  // Main ring: each relation cluster on the ring (big/small interleaved),
  // concentric rings inside a cluster by degree — the highest-degree node at
  // the center, lower-degree nodes on outer rings. Degree-1 nodes are NOT
  // placed here: they radiate around their single neighbor afterwards.
  list.forEach(([, gs], idx) => {
    const single = list.length === 1;
    const ang = (idx / list.length) * Math.PI * 2 - Math.PI / 2;
    const cx = single ? 0 : Math.cos(ang) * R_BIG;
    const cy = single ? 0 : Math.sin(ang) * R_BIG;
    const sorted = gs.slice().sort((a, b) => udeg(b) - udeg(a) || (a < b ? -1 : 1));
    let j = 0, ring = 0;
    while (j < sorted.length) {
      const ringSize = ring === 0 ? 1 : 6 * ring;
      const layer = sorted.slice(j, j + ringSize);
      const r = ring * CELL;
      if (ring === 0) {
        // gs holds node ids (relation clusters), not node objects.
        map.set(layer[0], { x: cx, y: cy });
        vmap.set(layer[0], { x: 0, y: 0 });
      } else {
        const V = Math.max(layer.length, 8);
        const rEff = Math.max(r, V * CELL / 4.5);
        layer.forEach((id, k) => {
          const a2 = (k / V) * Math.PI * 2 - Math.PI / 2;
          map.set(id, { x: cx + Math.cos(a2) * rEff, y: cy + Math.sin(a2) * rEff });
          vmap.set(id, { x: 0, y: 0 });
        });
      }
      j += layer.length; ring++;
    }
  });
  // Rigid families: owner -> [{id, dx, dy}] — radial leaves and island
  // members keep their relative position to the owner under springs/dragging.
  const familyByOwner = new Map();
  // Islands: each self-contained group gets its own small ring on a band
  // outside the main ring — the group's biggest node at the center, the rest
  // evenly around it. Island members form a rigid family with the center:
  // under springs / dragging the whole island moves together.
  const islandSet = new Set();
  islands.forEach(is => is.nodes.forEach(id => islandSet.add(id)));
  const islandR = islands.map(is => 15 + ringCount(is.nodes.length) * CELL);
  const maxIR = islands.length ? Math.max.apply(null, islandR) : 0;
  // Pre-compute radial-leaf fans (owner -> leaves) and the largest fan radius:
  // R_ISLAND must reserve the leaf reach so leaves never pierce an island.
  // The placement loop below (after the islands) reuses these maps.
  const leavesOfOwner = new Map();
  const leafSet = new Set();
  let maxLeafR = 0;
  for (const nd of nodes) {
    const id = nd.id;
    if (isoSet.has(id) || islandSet.has(id)) continue;
    if (udeg(id) !== 1) continue;
    const owner = [...(adj.get(id) || [])][0];
    if (owner == null) continue;
    if (!leavesOfOwner.has(owner)) leavesOfOwner.set(owner, []);
    leavesOfOwner.get(owner).push(id);
    leafSet.add(id);
  }
  for (const [owner, ls] of leavesOfOwner) {
    const ownerNode = nodes.find(x => x.id === owner);
    const R = Math.max(30, 5 * ls.length + 12 + (ownerNode ? ownerNode.size : 6));
    if (R > maxLeafR) maxLeafR = R;
  }
  const R_ISLAND = (R_BIG || 420) + maxR + maxLeafR + maxIR + 80;
  islands.forEach((is, i) => {
    const ang = islands.length === 1 ? -Math.PI / 2
      : (i / islands.length) * Math.PI * 2 - Math.PI / 2;
    const cx = Math.cos(ang) * R_ISLAND, cy = Math.sin(ang) * R_ISLAND;
    const ns = is.nodes.slice().sort((a, b) => udeg(b) - udeg(a));
    map.set(ns[0], { x: cx, y: cy });
    vmap.set(ns[0], { x: 0, y: 0 });
    const r = islandR[i];
    const fam = [];
    ns.slice(1).forEach((id, j) => {
      const a = (j / Math.max(1, ns.length - 1)) * Math.PI * 2 - Math.PI / 2;
      map.set(id, { x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r });
      vmap.set(id, { x: 0, y: 0 });
      fam.push({ id, dx: Math.cos(a) * r, dy: Math.sin(a) * r });
    });
    if (ns.length > 1) familyByOwner.set(ns[0], fam);
  });

  if (iso.length) {
    // The isolated pile: a tidy grid below the ring, well outside it.
    // Sorted by module so same-module nodes sit next to each other.
    iso.sort((a, b) => {
      const ma = (a.detail && a.detail.module) || "", mb = (b.detail && b.detail.module) || "";
      return ma < mb ? -1 : ma > mb ? 1 : (a.id < b.id ? -1 : 1);
    });
    const isoCols = Math.ceil(Math.sqrt(iso.length));
    const isoRows = Math.ceil(iso.length / isoCols);
    const isoR = 14 + isoCols * CELL / 2;
    const iy = R_ISLAND + maxIR + isoR + 120;
    iso.forEach((nd, j) => {
      const row = Math.floor(j / isoCols), col = j % isoCols;
      map.set(nd.id, {
        x: (col - (isoCols - 1) / 2) * CELL,
        y: iy + (row - (isoRows - 1) / 2) * CELL,
      });
      vmap.set(nd.id, { x: 0, y: 0 });
    });
  }
  // ---- radial leaves: degree-1 nodes sit tightly around their single
  // neighbor, fanning out evenly; the whole group moves rigidly with the
  // owner under springs / dragging (state.familyByOwner). They are placed
  // LAST, hugging the already-placed ring / island nodes (leafSet and
  // leavesOfOwner were pre-computed above for the R_ISLAND reserve).
  for (const [owner, ls] of leavesOfOwner) {
    const op = map.get(owner);
    if (!op) {   // owner not placed (defensive) — drop the leaves
      ls.forEach(id => leafSet.delete(id));
      continue;
    }
    const ownerNode = nodes.find(x => x.id === owner);
    // Radius grows with fan size so the arc spacing stays >= one diameter.
    const R = Math.max(30, 5 * ls.length + 12 + (ownerNode ? ownerNode.size : 6));
    const base = (hashStr(owner) % 100) / 100 * Math.PI * 2;
    const fam = [];
    ls.slice().sort().forEach((id, i) => {
      const a = base + (i / ls.length) * Math.PI * 2;
      map.set(id, { x: op.x + Math.cos(a) * R, y: op.y + Math.sin(a) * R });
      vmap.set(id, { x: 0, y: 0 });
      fam.push({ id, dx: Math.cos(a) * R, dy: Math.sin(a) * R });
    });
    familyByOwner.set(owner, fam);
  }
  state.poses[state.view] = map;
  state.vels[state.view] = vmap;
  // Rigid families (radial leaves + island members) and the leaf set exempt
  // from hard-core separation: the static seed positions are already clear,
  // and under springs / dragging the rigid pass keeps them glued to the owner.
  state.familyByOwner = familyByOwner;
  state.leafSet = leafSet;
  // Region map for the region-local spring pass: every node belongs to one
  // region — a relation cluster ("c:..."), an island ("i:n") or the iso pile.
  // Radial leaves inherit their owner's region. The spring pass pulls only
  // edges INSIDE a region, so "弹性拉近" tightens each region without
  // cross-region edges (cluster<->island, island<->island) yanking the
  // regions toward each other.
  const regionOf = new Map();
  list.forEach(([k, gs]) => gs.forEach(id => regionOf.set(id, "c:" + k)));
  islands.forEach((is, i) => is.nodes.forEach(id => regionOf.set(id, "i:" + i)));
  for (const id of isoSet) regionOf.set(id, "iso");
  for (const [owner, ls] of leavesOfOwner) {
    const oreg = regionOf.get(owner);
    if (!oreg) continue;
    ls.forEach(id => regionOf.set(id, oreg));
  }
  // Per-view cache: switchView may reuse the poses without re-running
  // initPositions, so the region map must follow the view, not the build.
  if (!state.regionMap) state.regionMap = {};
  state.regionMap[state.view] = regionOf;
  state.regionOf = regionOf;
  // Leaf/island-member -> rigid owner reverse map for hardSep: locked family
  // members can't move, so their overlaps delegate the push to the owner
  // (the whole fan / island shifts away instead).
  const leafOwner = new Map();
  for (const [owner, fam] of familyByOwner) {
    if (!map.get(owner)) continue;
    for (const f of fam) leafOwner.set(f.id, owner);
  }
  if (!state.leafOwnerMap) state.leafOwnerMap = {};
  state.leafOwnerMap[state.view] = leafOwner;
  state.leafOwner = leafOwner;
}

function maxFrame() { return Math.min(900, 150 + Math.ceil(state.nodes.length * 0.4)); }

function step() {
  const pos = state.poses[state.view], vel = state.vels[state.view];
  const nodes = state.nodes, links = state.links;
  // Layout philosophy: the golden-spiral seed (initPositions, R0=480) is
  // already a dense, non-overlapping layout (~47px spacing at 414 nodes).
  // Pure physics sims (FR repulsion + attraction) destroyed it — a saturated
  // repulsion cap spun a limit cycle (collapsed 169px core), an uncapped one
  // flung every node to the BOUND, and even a weak origin anchor (GRAV)
  // dragged the whole cluster to a point over a few hundred frames (steady
  // velocity = force/(1-DAMP), which exceeds VMAX for any GRAV*d > 0.4).
  // So the physics pass only refines, deterministically, O(n + edges):
  //   - ideal springs pull edges toward K = 800/sqrt(n)
  //   - hard-core separation below enforces the minimum spacing
  // Ideal edge length ~= the layout's natural spacing. With a small K
  // (800/sqrt(n) = 39 at 415 nodes) every edge pulls hard: hub->ring edges
  // dominate and collapse each cluster radially, so nodes move perpendicular
  // to their ring edges ("拉的方向不对，没有沿着线"). Floor at ~64 so short
  // ring edges are tightened gently ALONG the edge, and the 2.2*K cutoff
  // below ignores the long radial hub edges entirely.
  const K = Math.max(72, 800 / Math.sqrt(Math.max(1, nodes.length)));
  // Annealing to T=0 at 60% of the run (not a 0.3 floor, not at the very
  // end!): any nonzero floor keeps feeding the springs — steady velocity =
  // force/(1-DAMP) ≈ 10*force, so spring force ~0.4 makes the whole layout
  // cruise at VMAX forever (boiling limit cycle; nodes fly through each
  // other, hard-core separation can't out-pace the closing velocity). T must
  // hit 0 early enough for DAMP (0.9) to drain the kinetic energy before
  // maxFrame ends — 4px/frame needs ~50 frames to fall below the
  // convergence threshold.
  // Ramp-in: the first ~30 frames scale the spring force from 0 so turning
  // the checkbox on doesn't jerk nodes (velocity kick ~VMAX in frame 1) —
  // with the ramp the motion direction is visible edge-aligned instead.
  const T = Math.min(1, state.frame / 30) * Math.max(0, 1 - state.frame / (maxFrame() * 0.6));
  const DAMP = 0.9;
  const VMAX = 1;       // velocity clamp: max 1px/frame — slow enough that
                        // hard-core separation always out-paces the closing
                        // velocity, so nodes never slide through each other
                        // (the "stirring together" the user saw)
  const BOUND = 3000;   // hard position clamp so the layout stays in view
  // Spring pass is opt-in (the "弹性拉近" checkbox): the cluster layout is
  // already tidy on its own, and springs make dragging ripple through the
  // whole graph (the perpetual jitter). Off by default: positions stay put.
  if (state.springs) {
    const regionOf = state.regionOf, leafSet = state.leafSet;
    for (const l of links) {
      // Region-local springs: only edges inside the same region pull. Cross-
      // region edges (cluster<->island, island<->island, cluster<->cluster)
      // keep the regions apart — pulling them would drag islands into the
      // main ring again.
      if (regionOf && regionOf.get(l.s) !== regionOf.get(l.t)) continue;
      // Leaf edges are owned by the rigid pass (leaf glued to owner): a
      // spring on them would yank the owner toward the fan rim.
      if (leafSet && (leafSet.has(l.s) || leafSet.has(l.t))) continue;
      const a = pos.get(l.s), b = pos.get(l.t);
      if (!a || !b) continue;
      const va = vel.get(l.s), vb = vel.get(l.t);
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      // Pull-only spring: edges longer than K are shortened toward K; edges
      // at or under K are left alone (no compression — pushing would collapse
      // the ring clusters radially instead of tightening along the edges).
      // Range-limited to ~2.2*K — longer edges (the cross-region ones) must
      // NOT yank whole regions out of place.
      const f = d > K * 2.2 ? 0 : (d > K ? 0.008 * (d - K) * T : 0);
      const ux = dx / d, uy = dy / d;
      va.x += ux * f; va.y += uy * f;
      vb.x -= ux * f; vb.y -= uy * f;
    }
  }
  let totalV = 0;
  for (const n of nodes) {
    const p = pos.get(n.id); if (!p) continue;
    const v = vel.get(n.id);
    v.x *= DAMP; v.y *= DAMP;   // damping: without this the springs feed the
                                // velocities forever and the layout boils at
                                // VMAX (no convergence, nodes sliding through
                                // each other past the hard-core separation)
    if (v.x > VMAX) v.x = VMAX; else if (v.x < -VMAX) v.x = -VMAX;
    if (v.y > VMAX) v.y = VMAX; else if (v.y < -VMAX) v.y = -VMAX;
    p.x += v.x; p.y += v.y;
    if (p.x < -BOUND) { p.x = -BOUND; v.x = 0; } else if (p.x > BOUND) { p.x = BOUND; v.x = 0; }
    if (p.y < -BOUND) { p.y = -BOUND; v.y = 0; } else if (p.y > BOUND) { p.y = BOUND; v.y = 0; }
    totalV += v.x * v.x + v.y * v.y;
  }
  // Leaves join the hard separation like every other node (static layout
  // must be overlap-free even against locked island members); under springs
  // / dragging the rigid pass below re-glues them to their owner — and then
  // hardSep delegates their pushes to the owner so overlaps still resolve.
  const rigidOwnerOf = (state.springs || state.dragging) ? state.leafOwner : null;
  let minOverlap = hardSep(pos, nodes, undefined, rigidOwnerOf);
  minOverlap = Math.min(minOverlap, hardSep(pos, nodes, undefined, rigidOwnerOf));  // second pass: undo any new overlaps the first pass created
  // Rigid families: radial leaves and island members stay glued to their
  // owner — under springs (unrelated edges would pull them away) and while
  // dragging (the user expects the whole star to move together).
  if (state.springs || state.dragging) {
    const fams = state.familyByOwner;
    if (fams) {
      // Freeze the static-settled relative offsets on first use. The seed
      // offsets in familyByOwner are PRE-separation: the hard-separation pass
      // pushes leaves away from the crowd, so snapping them back to the seed
      // offsets when springs turn on would recreate the overlaps separation
      // already resolved (that was the springsTest ov=74 regression).
      const fz = state.famFrozenByView || (state.famFrozenByView = {});
      let frozen = fz[state.view];
      if (!frozen) {
        frozen = {}; fz[state.view] = frozen;
        for (const [owner, fam] of fams) {
          const op = pos.get(owner);
          if (!op) continue;
          frozen[owner] = fam.map(f => {
            const p = pos.get(f.id);
            if (!p) return f;
            return { id: f.id, dx: p.x - op.x, dy: p.y - op.y };
          });
        }
      }
      for (const owner of Object.keys(frozen)) {
        const op = pos.get(owner);
        if (!op) continue;
        for (const f of frozen[owner]) {
          if (f.id === state.dragging) continue;   // the dragged leaf moves freely
          const p = pos.get(f.id);
          if (!p) continue;
          p.x = op.x + f.dx; p.y = op.y + f.dy;
          const v = vel.get(f.id);
          if (v) { v.x = 0; v.y = 0; }
        }
      }
    }
  }
  // Converged when the mean squared velocity is tiny AND no overlapping pairs
  // remain — velocity alone is not enough: once the springs die out (T=0) the
  // velocities freeze at ~0 while the hard-core pass still needs many frames
  // to finish pushing crowded cores apart.
  // minOverlap >= 25.5 uses a float tolerance: the half-push (26-d)/2 converges
  // asymptotically (e.g. 25.97px), so an exact `min dist >= 26` never fires and
  // the sim runs all maxFrame frames — that was the perpetual jitter.
  // Frame floor: with the spring ramp-in the first frames have ~zero force —
  // without the floor the "no velocity yet" test would fire on frame 1 and
  // freeze the sim before the springs ever engage.
  if (state.frame > 90 && nodes.length && minOverlap >= 25.5 && totalV / nodes.length < 0.05) state.frame = maxFrame();
}

// Hard-core separation: deterministically push any pair closer than MIND
// apart (grid-accelerated). A crowded core would otherwise sit in a force
// balance limit cycle forever — inside a dense ball the symmetric repulsion
// cancels, only the rim gets pushed out and the springs pull it back. Direct
// position projection guarantees monotonic de-overlap instead.
// Returns the closest surviving pair distance (Infinity if none < MIND).
function hardSep(pos, nodes, skip, rigidOwnerOf) {
  const MIND = 26, MC = 26;
  let minOverlap = Infinity;
  const grid = new Map();
  for (const n of nodes) {
    if (skip && skip.has(n.id)) continue;
    const p = pos.get(n.id); if (!p) continue;
    const k = Math.floor(p.x / MC) + "," + Math.floor(p.y / MC);
    let arr = grid.get(k);
    if (!arr) { arr = []; grid.set(k, arr); }
    arr.push(n);
  }
  for (const n of nodes) {
    if (skip && skip.has(n.id)) continue;
    const p = pos.get(n.id); if (!p) continue;
    const cx = Math.floor(p.x / MC), cy = Math.floor(p.y / MC);
    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
      const arr = grid.get((cx + dx) + "," + (cy + dy));
      if (!arr) continue;
      for (const m of arr) {
        if (m === n) continue;
        const q = pos.get(m.id); if (!q) continue;
        let dxp = p.x - q.x, dyp = p.y - q.y;
        let d2 = dxp * dxp + dyp * dyp;
        if (d2 >= MIND * MIND) continue;
        if (d2 === 0) { dxp = 1; dyp = 0; d2 = 1; }  // exact overlap: fixed axis
        const d = Math.sqrt(d2);
        if (d < minOverlap) minOverlap = d;
        // Push targets. Locked family members (leaves / island satellites)
        // can't move under springs — the rigid pass glues them to their
        // owner — so their overlap delegates the push to the owner: the
        // whole fan / island shifts away and the overlap resolves. Same-
        // family pairs (two leaves of one fan) are non-overlapping by
        // construction, so nothing to resolve.
        let t1 = p, t2 = q;
        if (rigidOwnerOf) {
          const o1 = rigidOwnerOf.get(n.id), o2 = rigidOwnerOf.get(m.id);
          if (o1) { t1 = pos.get(o1); if (!t1) continue; }
          if (o2) { t2 = pos.get(o2); if (!t2) continue; }
          if (t1 === t2) continue;
        }
        // Full push: position projection de-overlaps each pair in one frame.
        // Half-push (MIND-d)/2 converges asymptotically (25.97, 25.985, ...)
        // and never reaches MIND, so the layout settles with ~0.1px overlap
        // residue that shows up as ov>0 in the verifier.
        const push = MIND - d;
        const ux = dxp / d, uy = dyp / d;
        t1.x += ux * push; t1.y += uy * push;
        t2.x -= ux * push; t2.y -= uy * push;
      }
    }
  }
  return minOverlap;
}

/* ---------- rendering ---------- */
function nodeRadius(n) {
  return Math.max(2.5, n.size * clamp(state.scale, 0.5, 1.4));
}

function render() {
  ctx.clearRect(0, 0, W, H);
  const pos = state.poses[state.view];
  if (!pos) return;
  const sel = state.selected;
  let focusSet = null;
  if (sel) {
    focusSet = new Set([sel]);
    for (const l of state.links) {
      if (l.s === sel) focusSet.add(l.t);
      if (l.t === sel) focusSet.add(l.s);
    }
  }
  const es = state.scale;
  ctx.lineWidth = Math.max(0.6, 1.1 * Math.min(1, es));
  for (const l of state.links) {
    const a = pos.get(l.s), b = pos.get(l.t);
    if (!a || !b) continue;
    const rel = !sel || (focusSet.has(l.s) && focusSet.has(l.t));
    ctx.strokeStyle = rel ? EDGE : EDGE_DIM;
    ctx.beginPath();
    ctx.moveTo(a.x * es + state.ox, a.y * es + state.oy);
    ctx.lineTo(b.x * es + state.ox, b.y * es + state.oy);
    ctx.stroke();
    if (state.showArrows && rel) arrow(a, b, es);
  }
  for (const n of state.nodes) drawNode(n, pos, es, sel, focusSet);
  if (state.hovered && !state.dragging && !state.panning) drawTooltip();
}

function drawNode(n, pos, es, sel, focusSet) {
  const p = pos.get(n.id); if (!p) return;
  const sx = p.x * es + state.ox, sy = p.y * es + state.oy;
  const r = nodeRadius(n);
  const active = !sel || focusSet.has(n.id);
  const isSel = sel === n.id;
  ctx.globalAlpha = active ? (isSel ? 1 : 0.95) : 0.08;
  ctx.fillStyle = nodeColor(n);
  ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI * 2); ctx.fill();
  if (isSel) {
    ctx.globalAlpha = 1;
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(sx, sy, r + 3, 0, Math.PI * 2); ctx.stroke();
  } else {
    ctx.globalAlpha = active ? 0.9 : 0.05;
    ctx.strokeStyle = "rgba(255,255,255,0.35)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI * 2); ctx.stroke();
  }
  ctx.globalAlpha = 1;
  const minScale = state.view === "modules" ? 0.38 : state.view === "files" ? 0.55
    : state.view === "classes" ? 0.62 : 1.05;
  if (es >= minScale) {
    ctx.font = (12 * clamp(es, 0.7, 1.4)) + "px system-ui, sans-serif";
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    ctx.fillStyle = active ? "rgba(226,232,240,0.92)" : "rgba(226,232,240,0.15)";
    ctx.fillText(n.label, sx + r + 5, sy);
  }
}

function arrow(a, b, es) {
  const sx = a.x * es + state.ox, sy = a.y * es + state.oy;
  const tx = b.x * es + state.ox, ty = b.y * es + state.oy;
  const dx = tx - sx, dy = ty - sy;
  const d = Math.sqrt(dx * dx + dy * dy) || 1;
  const ux = dx / d, uy = dy / d;
  const r2 = nodeRadius(b) + 1;
  const px = tx - ux * r2, py = ty - uy * r2;
  const al = 5 * clamp(es, 0.7, 1.3);
  const ang = Math.atan2(uy, ux);
  ctx.fillStyle = EDGE;
  ctx.beginPath();
  ctx.moveTo(px, py);
  ctx.lineTo(px - al * Math.cos(ang - 0.45), py - al * Math.sin(ang - 0.45));
  ctx.lineTo(px - al * Math.cos(ang + 0.45), py - al * Math.sin(ang + 0.45));
  ctx.closePath(); ctx.fill();
}

function drawTooltip() {
  const n = state.hovered;
  const p = state.poses[state.view].get(n.id);
  if (!p) return;
  ctx.font = "11px system-ui, sans-serif";
  const lines = [n.label, n.sub || ""];
  let w = 16;
  for (const l of lines) w = Math.max(w, ctx.measureText(l).width + 18);
  const h = lines.length * 15 + 8;
  let tx = state.mx + 12, ty = state.my + 14;
  if (tx + w > W) tx = state.mx - w - 12;
  if (ty + h > H) ty = state.my - h - 10;
  ctx.fillStyle = "rgba(23,29,41,0.94)";
  ctx.strokeStyle = "#2c3a52";
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.roundRect(tx, ty, w, h, 6); ctx.fill(); ctx.stroke();
  ctx.textBaseline = "middle";
  lines.forEach((l, i) => {
    ctx.fillStyle = i === 0 ? "#e2e8f0" : "#94a3b8";
    ctx.fillText(l, tx + 8, ty + 12 + i * 15);
  });
}

/* ---------- interaction ---------- */
function canvasPos(e) {
  const r = canvas.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

function hitTest(mx, my) {
  const pos = state.poses[state.view];
  if (!pos) return null;
  let best = null, bd = 1e18;
  for (const n of state.nodes) {
    const p = pos.get(n.id); if (!p) continue;
    const sx = p.x * state.scale + state.ox, sy = p.y * state.scale + state.oy;
    const r = nodeRadius(n) + 4;
    const dx = mx - sx, dy = my - sy;
    const d = dx * dx + dy * dy;
    if (d < r * r && d < bd) { bd = d; best = n; }
  }
  return best;
}

canvas.addEventListener("pointerdown", (e) => {
  state.p0 = { x: e.clientX, y: e.clientY };
  state.moved = false;
  const pt = canvasPos(e);
  const hit = hitTest(pt.x, pt.y);
  if (hit) {
    state.dragging = hit.id;
    canvas.setPointerCapture(e.pointerId);
    const v = state.vels[state.view].get(hit.id);
    if (v) { v.x = 0; v.y = 0; }
    state.running = true; state.frame = 0;
    state.autoFit = false;   // user is rearranging; do not re-fit when it settles
  } else {
    state.panning = true;
    state.panFrom = { x: pt.x, y: pt.y };
  }
});

canvas.addEventListener("pointermove", (e) => {
  const pt = canvasPos(e);
  state.mx = pt.x; state.my = pt.y;
  if (state.dragging || state.panning) {
    if (Math.abs(e.clientX - state.p0.x) + Math.abs(e.clientY - state.p0.y) > 3) state.moved = true;
    if (state.dragging) {
      const p = state.poses[state.view].get(state.dragging);
      if (p) {
        p.x = (pt.x - state.ox) / state.scale;
        p.y = (pt.y - state.oy) / state.scale;
      }
      dirty = true;
    } else if (state.panning) {
      state.ox += pt.x - state.panFrom.x;
      state.oy += pt.y - state.panFrom.y;
      state.panFrom = pt;
      state.userMovedView = true;
      dirty = true;
    }
  } else {
    const hit = hitTest(pt.x, pt.y);
    if (hit !== state.hovered) { state.hovered = hit; dirty = true; }
    canvas.style.cursor = hit ? "pointer" : "grab";
  }
});

canvas.addEventListener("pointerup", (e) => {
  if (state.dragging && state.familyByOwner) {
    // The user dragged a leaf away on purpose: detach it from its family so
    // the rigid pass doesn't yank it back to the owner.
    for (const [owner, fam] of state.familyByOwner) {
      const kept = fam.filter(f => f.id !== state.dragging);
      if (kept.length !== fam.length) {
        state.familyByOwner.set(owner, kept);
        if (state.leafSet) state.leafSet.delete(state.dragging);
      }
    }
  }
  if (!state.moved) {
    const pt = canvasPos(e);
    const hit = hitTest(pt.x, pt.y);
    if (hit) selectNode(hit); else clearSelection();
  }
  state.dragging = null;
  state.panning = false;
});

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const pt = canvasPos(e);
  const f = Math.exp(-e.deltaY * 0.0012);
  const ns = clamp(state.scale * f, 0.04, 12);
  if (ns === state.scale) return;
  const wx = (pt.x - state.ox) / state.scale;
  const wy = (pt.y - state.oy) / state.scale;
  state.scale = ns;
  state.ox = pt.x - wx * ns;
  state.oy = pt.y - wy * ns;
  state.userMovedView = true;
  dirty = true;
}, { passive: false });

canvas.addEventListener("dblclick", (e) => {
  const pt = canvasPos(e);
  const hit = hitTest(pt.x, pt.y);
  if (hit) focusNode(state.view, hit.id);
});

/* ---------- selection / view switching ---------- */
function selectNode(node) {
  state.selected = node.id;
  state.hovered = null;
  showPanel(node);
  dirty = true;
}

function clearSelection() {
  state.selected = null;
  hidePanel();
  dirty = true;
}

function switchView(name) {
  state.view = name;
  setViewNodes();
  if (!state.poses[name]) initPositions();
  if (state.regionMap) state.regionOf = state.regionMap[name];  // cached view: keep its region map
  if (state.leafOwnerMap) state.leafOwner = state.leafOwnerMap[name];
  state.running = true; state.frame = 0;
  state.autoFit = true;   // re-fit to the settled layout when the sim ends
  state.userMovedView = false;
  state.selected = null; state.hovered = null;
  hidePanel(); updateToolbar();
  fit();
  dirty = true;
}

function focusNode(view, id) {
  if (state.view !== view) switchView(view);
  const p = state.poses[state.view].get(id);
  if (!p) return;
  const target = Math.max(state.scale, 1.6);
  state.scale = target;
  state.ox = W / 2 - p.x * target;
  state.oy = H / 2 - p.y * target;
  const n = state.nodes.find(x => x.id === id);
  if (n) selectNode(n); else clearSelection();
  dirty = true;
}

function fit() {
  const pos = state.poses[state.view];
  const ns = state.nodes;
  if (!ns.length || !pos) return;
  let x0 = 1e18, y0 = 1e18, x1 = -1e18, y1 = -1e18;
  for (const n of ns) {
    const p = pos.get(n.id); if (!p) continue;
    if (p.x < x0) x0 = p.x; if (p.x > x1) x1 = p.x;
    if (p.y < y0) y0 = p.y; if (p.y > y1) y1 = p.y;
  }
  const bw = (x1 - x0) || 1, bh = (y1 - y0) || 1;
  const s = Math.min((W - 120) / bw, (H - 120) / bh, 2);
  state.scale = Math.max(0.05, s);
  state.ox = W / 2 - (x0 + x1) / 2 * state.scale;
  state.oy = H / 2 - (y0 + y1) / 2 * state.scale;
}

/* ---------- detail panel ---------- */
function kv(k, v) {
  return '<div class="kv"><span class="k">' + esc(k) + '</span><span class="v">' + esc(v) + "</span></div>";
}

function jumpLink(view, id, label, sub) {
  return '<a href="javascript:void(0)" data-view="' + view + '" data-id="' + esc(id)
    + '"><b>' + esc(label) + "</b><i>" + esc(sub || "") + "</i></a>";
}

function sec(title, inner) {
  return inner ? '<div class="sec"><div class="sec-t">' + esc(title) + "</div>" + inner + "</div>" : "";
}

function symRow(name, extra) {
  return '<div class="sym">' + esc(name) + (extra ? " <span>" + esc(extra) + "</span>" : "") + "</div>";
}

function renderFun(d) {
  let h = kv("签名", d.signature || "-");
  if (d.comment) h += kv("注释", d.comment);
  h += kv("文件", d.file + (d.line ? " : " + d.line : ""));
  h += kv("模块", d.module || "-");
  h += sec("被调用 " + d.calls.length,
    d.calls.map(c => jumpLink("functions", c.id, c.label, c.file)).join(""));
  h += sec("调用者 " + d.callers.length,
    d.callers.map(c => jumpLink("functions", c.id, c.label, c.file)).join(""));
  return h;
}

function renderFile(d) {
  let h = kv("路径", d.path);
  h += kv("语言", d.lang || "-");
  if (d.package) h += kv("包", d.package);
  h += kv("模块", d.module || "-");
  h += sec("符号 " + d.symbols.length,
    d.symbols.map(s => symRow(s.name, s.kind + " L" + s.line)).join(""));
  h += sec("调用文件 " + d.calls.length,
    d.calls.map(c => jumpLink("files", c.id, c.label, "")).join(""));
  h += sec("被调用 " + d.calledBy.length,
    d.calledBy.map(c => jumpLink("files", c.id, c.label, "")).join(""));
  return h;
}

function renderClass(d) {
  let h = kv("完整名", d.qname);
  if (d.comment) h += kv("注释", d.comment);
  h += kv("文件", d.file + (d.line ? " : " + d.line : ""));
  h += kv("模块", d.module || "-");
  h += sec("方法 " + d.methods.length,
    d.methods.map(m => symRow(m.name, "L" + m.line)).join(""));
  h += sec("引用 " + d.refs.length,
    d.refs.map(c => jumpLink("classes", c.id, c.label, c.file)).join(""));
  h += sec("被引用 " + d.refdBy.length,
    d.refdBy.map(c => jumpLink("classes", c.id, c.label, c.file)).join(""));
  return h;
}

function renderModule(d) {
  let h = kv("路径", d.path);
  h += kv("文件数", d.fileCount);
  h += kv("符号数", d.symbolCount);
  if (d.summary) h += kv("摘要", d.summary);
  h += sec("依赖 " + d.deps.length,
    d.deps.map(c => jumpLink("modules", c.id, c.label, "")).join(""));
  h += sec("被依赖 " + d.depBy.length,
    d.depBy.map(c => jumpLink("modules", c.id, c.label, "")).join(""));
  h += sec("文件 " + d.files.length,
    d.files.map(f => symRow(f)).join(""));
  return h;
}

function renderDetail(node) {
  const d = node.detail;
  if (d.kind === "fun") return renderFun(d);
  if (d.kind === "file") return renderFile(d);
  if (d.kind === "class") return renderClass(d);
  return renderModule(d);
}

function showPanel(node) {
  document.getElementById("panelTitle").textContent = node.label;
  panelBody.innerHTML = renderDetail(node);
  panel.hidden = false;
}

function hidePanel() { panel.hidden = true; }

panelBody.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-view]");
  if (!a) return;
  e.preventDefault();
  focusNode(a.dataset.view, a.dataset.id);
});
document.getElementById("panelClose").addEventListener("click", clearSelection);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { clearSelection(); results.hidden = true; }
});

/* ---------- search ---------- */
let INDEX = null;
function buildIndex() {
  INDEX = [];
  for (const vn of ["modules", "files", "classes", "functions"]) {
    for (const n of VIEWS[vn].nodes) {
      INDEX.push({ view: vn, id: n.id, label: n.label, sub: n.sub });
    }
  }
}

search.addEventListener("input", () => {
  const q = search.value.trim().toLowerCase();
  results.innerHTML = "";
  if (!q) { results.hidden = true; return; }
  const hits = [];
  for (const it of INDEX) {
    if (it.label.toLowerCase().indexOf(q) >= 0
      || it.sub.toLowerCase().indexOf(q) >= 0
      || it.id.toLowerCase().indexOf(q) >= 0) {
      hits.push(it);
      if (hits.length >= 30) break;
    }
  }
  results.hidden = hits.length === 0;
  for (const h of hits) {
    const d = document.createElement("div");
    d.className = "ri";
    d.innerHTML = "<b>" + esc(h.label) + "</b><i>" + esc(h.sub) + "</i>";
    d.addEventListener("click", () => {
      focusNode(h.view, h.id);
      results.hidden = true;
      search.value = "";
    });
    results.appendChild(d);
  }
});

/* ---------- toolbar ---------- */
const viewBtns = document.getElementById("viewBtns");
for (const vn of ["modules", "files", "classes", "functions"]) {
  const b = document.createElement("button");
  b.textContent = VIEW_TITLES[vn];
  b.dataset.view = vn;
  b.addEventListener("click", () => switchView(vn));
  viewBtns.appendChild(b);
}

function updateToolbar() {
  for (const b of viewBtns.children) b.classList.toggle("on", b.dataset.view === state.view);
  isolatedBox.checked = state.isolated[state.view];
}

isolatedBox.addEventListener("change", () => {
  state.isolated[state.view] = isolatedBox.checked;
  setViewNodes();
  state.running = true; state.frame = 0;
  state.autoFit = false;
  dirty = true;
});

springsBox.addEventListener("change", () => {
  state.springs = springsBox.checked;
  state.running = true; state.frame = 0;   // restart the sim so springs settle
  dirty = true;
});

arrowsBox.addEventListener("change", () => {
  state.showArrows = arrowsBox.checked;
  dirty = true;
});

document.getElementById("relayout").addEventListener("click", () => {
  initPositions();
  state.running = true; state.frame = 0;
  state.autoFit = true;
  state.userMovedView = false;
  fit();
  dirty = true;
});
document.getElementById("fit").addEventListener("click", () => { fit(); dirty = true; });

/* ---------- main loop ---------- */
let dirty = true;
function tick() {
  if (state.running) {
    step();
    state.frame++;
    if (state.frame > maxFrame()) state.running = false;
    if (!state.running) {
      if (state.autoFit && !state.userMovedView) fit();
      state.autoFit = false;
    }
    dirty = true;
  }
  if (dirty) { render(); dirty = false; }
  requestAnimationFrame(tick);
}

resize();
buildIndex();
document.getElementById("proj").textContent = META.title;
const c = META.counts;
document.getElementById("gen").textContent = " · " + META.generated + " · "
  + c.modules.nodes + " 模块 / " + c.files.nodes + " 文件 / "
  + c.functions.nodes + " 函数";
document.title = META.title + " · 依赖图";
switchView("modules");
tick();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
