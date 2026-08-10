#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
code-graph query.py - retrieve a small subgraph from .code-graph/graph.json.

Reads only the graph file (no source files), so a lookup costs a few KB instead
of reading the codebase. Prints matching symbols with their call edges and the
module slice path for deeper reading.

Usage:
  python3 query.py --root <project-root> <keyword>
  python3 query.py --root <project-root> --file <rel/path.kt>
  python3 query.py --root <project-root> <keyword> --json   # machine-readable
"""

import argparse
import json
import os
import sys

MAX_EDGES = 20


def load_graph(root):
    path = os.path.join(root, ".code-graph", "graph.json")
    if not os.path.exists(path):
        print("error: graph not found. Run update.py first.", file=sys.stderr)
        sys.exit(2)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_called_by(graph):
    called_by = {}
    for file_edges in graph.get("edges", {}).values():
        for src, dsts in file_edges.items():
            for d in dsts:
                called_by.setdefault(d, []).append(src)
    return called_by


def module_of(graph, qname):
    """Find module id containing the symbol's file."""
    sym = graph.get("symbols", {}).get(qname)
    if not sym:
        return None
    f = sym.get("file", "")
    for m in graph.get("modules", []):
        if f in m.get("files", []):
            return m["id"]
    return None


def render_text(graph, called_by, hits, keyword):
    out = []
    out.append("# query: %s (%d hit(s))" % (keyword, len(hits)))
    out.append("")
    symbols = graph.get("symbols", {})
    edges = graph.get("edges", {})
    for qn in hits:
        if qn.startswith("FILE:"):
            out.append("## %s" % qn[len("FILE:"):])
            out.append("- kind: file")
            out.append("")
            continue
        sym = symbols.get(qn)
        if not sym:
            continue
        out.append("## %s" % qn)
        out.append("- kind: %s | line: %s" % (sym.get("kind", "?"), sym.get("line", "?")))
        out.append("- file: `%s`" % sym.get("file", "?"))
        sig = sym.get("signature") or ""
        if sig:
            out.append("- signature: `%s`" % sig)
        comment = sym.get("comment") or ""
        if comment:
            out.append("- comment: %s" % comment)
        mid = module_of(graph, qn)
        if mid:
            out.append("- module: `%s` -> `.code-graph/modules/%s.md`" % (mid, mid))
        # call edges for functions
        if sym.get("kind") == "fun":
            calls = []
            for file_edges in edges.values():
                if qn in file_edges:
                    calls.extend(file_edges[qn])
            cbs = called_by.get(qn, [])
            if calls:
                out.append("- calls (%d):" % len(calls))
                for c in sorted(set(calls))[:MAX_EDGES]:
                    out.append("    %s" % c)
                if len(set(calls)) > MAX_EDGES:
                    out.append("    ... (%d more)" % (len(set(calls)) - MAX_EDGES))
            if cbs:
                out.append("- called by (%d):" % len(cbs))
                for c in sorted(set(cbs))[:MAX_EDGES]:
                    out.append("    %s" % c)
                if len(set(cbs)) > MAX_EDGES:
                    out.append("    ... (%d more)" % (len(set(cbs)) - MAX_EDGES))
        out.append("")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="code-graph query")
    ap.add_argument("--root", default=".", help="project root (default: cwd)")
    ap.add_argument("--file", default=None, help="exact file path lookup")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("keyword", nargs="?", default=None, help="keyword to match")
    args = ap.parse_args(argv)

    graph = load_graph(args.root)
    symbols = graph.get("symbols", {})
    files = graph.get("files", {})

    if args.file:
        target = args.file.replace("\\", "/")
        if target in files:
            hits = [qn for qn, s in symbols.items() if s.get("file") == target]
        else:
            hits = []
    elif args.keyword:
        kw = args.keyword.lower()
        hits = []
        for qn, s in symbols.items():
            if kw in qn.lower() or kw in s.get("signature", "").lower():
                hits.append(qn)
        # also match file paths
        for f in files:
            if kw in f.lower():
                hits.append("FILE:" + f)
    else:
        ap.error("provide a keyword or --file")

    if args.json:
        out = {"hits": hits}
        for qn in hits:
            if qn.startswith("FILE:"):
                out[qn] = {"kind": "file"}
                continue
            out[qn] = symbols.get(qn)
        print(json.dumps(out, ensure_ascii=False))
        return 0

    if not hits:
        print("# query: %s (0 hits). Try a broader keyword, or run update.py if the graph is stale." % (args.file or args.keyword))
        return 1

    called_by = build_called_by(graph)
    print(render_text(graph, called_by, hits, args.file or args.keyword))
    return 0


if __name__ == "__main__":
    sys.exit(main())
