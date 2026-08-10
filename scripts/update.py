#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
code-graph update.py - incremental project structure graph builder.

Pure Python 3 stdlib (3.8+), no network, no third-party packages. Works on any
code project; ships language-aware scanners for Kotlin / Java / Python /
TypeScript / Go plus a generic fallback (file-level only) for other languages.

Workflow:
  1. Walk the source tree (build/artifact dirs skipped).
  2. sha256-hash every file and diff against .code-graph/manifest.json.
  3. Re-scan only changed files; merge with the previous symbol table.
  4. Rebuild call edges (a cheap full pass over source bodies).
  5. Rewrite .code-graph/{manifest.json, graph.json, index.md, modules/*.md}.
  6. Regenerate <root>/code-graph.html (self-contained interactive viewer,
     see render_html.py). Skip with --no-html.

stdout carries a compact JSON report of what changed, for the AI to read.

Usage:
  python3 update.py --root <project-root>
  python3 update.py --root <project-root> --force     # full rebuild
  python3 update.py --root <project-root> --no-html   # skip the HTML viewer
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

OUT_DIR_NAME = ".code-graph"
MANIFEST_NAME = "manifest.json"
GRAPH_NAME = "graph.json"
INDEX_NAME = "index.md"
MODULES_DIR = "modules"
MAX_MODULE_DEPTH = 3
MAX_SIG_LEN = 120
MAX_COMMENT_LEN = 240

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".trae", ".claude",
    "build", "out", "dist", "target", "node_modules", "venv", ".venv",
    "__pycache__", ".gradle", ".cache", ".code-graph", "Pods", "DerivedData",
    "captures", ".externalNativeBuild", ".cxx", ".jdks", ".kotlin", ".js",
    ".run", "xcuserdata",
}

# Exact language scanners (symbol-level).
LANG_EXTS = {
    "kotlin": {".kt", ".kts"},
    "java": {".java"},
    "python": {".py"},
    "typescript": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
    "go": {".go"},
}
BRACE_LANGS = {"kotlin", "java", "typescript", "go"}
PYTHON_LANG = {"python"}

# Generic fallback: file-level only (no symbols).
GENERIC_EXTS = {
    "c": {".c", ".h"},
    "cpp": {".cpp", ".hpp", ".cc", ".cxx", ".hh"},
    "rust": {".rs"},
    "swift": {".swift"},
    "ruby": {".rb"},
    "php": {".php"},
    "cs": {".cs"},
    "scala": {".scala"},
    "sql": {".sql"},
}

IDENT_RE = re.compile(r"[A-Za-z_]\w*")
# prefix.method — negative lookahead drops intermediate segments of a chain
# (`container.locationSampleRepository.insert` must match the rightmost pair,
# not the property access), so the actual call site `repo.insert(...)` is seen.
QUAL_RE = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)(?!\.\s*[A-Za-z_]\w*)\b")
LOCAL_TYPE_RE = re.compile(r"\b(?:val|var)\s+([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*)")
# Kotlin class-level property declaration with explicit type (for chains):
# `val locationSampleRepository: LocationSampleRepository by lazy { ... }`
PROP_DECL_RE = re.compile(r"^\s*(?:private\s+|internal\s+|protected\s+|public\s+|lateinit\s+|abstract\s+|open\s+|override\s+)*(?:val|var)\s+([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*)")
# `val container = (appContext as SingleLifeApplication).container`
AS_CHAIN_RE = re.compile(r"\b(?:val|var)\s+([A-Za-z_]\w*)\s*=\s*\([^)]*\bas\s+([A-Za-z_]\w*)\)\s*\.\s*([A-Za-z_]\w*)")
# `val app = applicationContext as SingleLifeApplication`
AS_ASSERT_RE = re.compile(r"\b(?:val|var)\s+([A-Za-z_]\w*)\s*=\s*[^=]*\bas\s+([A-Za-z_]\w*)\s*$")
# `head.rest.chain` segments for property-type propagation.
CHAIN_RE = re.compile(r"\b([A-Za-z_]\w*)((?:\.\s*[A-Za-z_]\w*)+)")

KOTLIN_FUN_RE = re.compile(r"^(?:[\w@]+\s+)*fun\s+([A-Za-z_]\w*)\s*\(")
KOTLIN_FUN_SIG_RE = re.compile(r"^fun\s+(?:[A-Za-z_]\w*\s*\.\s*)?([A-Za-z_]\w*)\s*\([^)]*\)\s*(?::\s*([\w.<>?,() ]+))?")
# Modifiers optional: package-private Java methods ("String getName() {")
# are common. "(?!new\s)" keeps constructor expressions off the table.
JAVA_METHOD_RE = re.compile(r"^(?:(?:public|private|protected|static|final|synchronized|abstract|default)\s+)?(?!new\s)[\w.<>\[\], ?]+\s+([A-Za-z_]\w*)\s*\(")
PYTHON_DEF_RE = re.compile(r"^(\s*)def\s+([A-Za-z_]\w*)\s*\(")
TS_FUN_RE = re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$\w]*)\s*\(")
TS_ARROW_RE = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$\w]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$\w]+)\s*=>")
# TS class methods ("foo() {", "get name() {", "foo = () => {...}") have no
# "function" keyword. "(?!...)" excludes control-flow statements that also
# match "name(..." on the line.
TS_METHOD_RE = re.compile(r"^(?:(?:public|private|protected|static|async|get|set)\s+)?(?!if\b|for\b|while\b|switch\b|catch\b|return\b|new\b|throw\b|do\b|else\b|typeof\b)[A-Za-z_$]\w*\s*\([^)]*\)\s*[{=]")
# Group 1 = receiver type (func (r *T) M(...)), group 2 = method name.
GO_FUNC_RE = re.compile(r"^func\s+(?:\(\s*\w+\s+\*?([A-Za-z_]\w*)\s*\)\s+)?([A-Za-z_]\w*)\s*\(")

# Build a class-membership stack per file: (line_no, owner_name).
CLS_RE = {
    "kotlin": re.compile(r"^(?:data\s+|sealed\s+|enum\s+|abstract\s+|open\s+|internal\s+)*(?:class|interface|object|enum\s+class|annotation\s+class)\s+([A-Za-z_]\w*)"),
    "java": re.compile(r"^(?:public|private|protected|final|abstract|static)\s+(?:class|interface|enum)\s+([A-Za-z_]\w*)"),
    "typescript": re.compile(r"^(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$\w]*)"),
    "go": re.compile(r"^type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b"),
}


def relpath(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def walk_sources(root: str):
    """Yield absolute paths of source files under root."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            yield path


def lang_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    for lang, exts in LANG_EXTS.items():
        if ext in exts:
            return lang
    for lang, exts in GENERIC_EXTS.items():
        if ext in exts:
            return lang
    return ""


def common_package_root(packages) -> str:
    """Longest common dotted prefix of all non-empty packages."""
    parts_list = [p.split(".") for p in packages if p]
    if not parts_list:
        return ""
    common = parts_list[0]
    for parts in parts_list[1:]:
        n = 0
        while n < len(common) and n < len(parts) and common[n] == parts[n]:
            n += 1
        common = common[:n]
    return ".".join(common)


class Symbol:
    __slots__ = ("name", "kind", "line", "owner", "signature", "comment")

    def __init__(self, name, kind, line, owner, signature, comment=""):
        self.name = name
        self.kind = kind
        self.line = line
        self.owner = owner
        self.signature = signature
        self.comment = comment


class FileInfo:
    __slots__ = ("path", "lang", "package", "imports", "symbols", "text")

    def __init__(self, path, lang):
        self.path = path
        self.lang = lang
        self.package = None
        self.imports = []
        self.symbols = []
        self.text = ""


def clean_signature(sig: str) -> str:
    sig = " ".join(sig.split())
    if len(sig) > MAX_SIG_LEN:
        sig = sig[:MAX_SIG_LEN] + "..."
    return sig


def clean_doc_comment(buf):
    """Strip /** */ and leading * from a doc-comment block, collapse to one line."""
    parts = []
    for ln in buf:
        t = ln.strip()
        if t.startswith("/**"):
            t = t[3:]
        if t.endswith("*/"):
            t = t[:-2]
        t = t.lstrip("*").strip()
        if t:
            parts.append(t)
    text = re.sub(r"\s+", " ", " ".join(parts))
    if len(text) > MAX_COMMENT_LEN:
        text = text[:MAX_COMMENT_LEN] + "..."
    return text


def collect_doc_comments(lines):
    """Map symbol line (1-based, matching parse_text's enumerate) -> doc text."""
    docs = {}
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i].strip()
        if raw.startswith("/**"):
            buf = [raw]
            j = i + 1
            while j < n and "*/" not in buf[-1]:
                buf.append(lines[j])
                j += 1
            # j = 0-based line right after the closing */; the symbol there is
            # line j+1 in 1-based terms — the key parse_text will look up.
            docs[j + 1] = clean_doc_comment(buf)
            i = j
            continue
        i += 1
    return docs


def parse_text(info: FileInfo):
    lang = info.lang
    lines = info.text.splitlines()
    if lang == "python":
        parse_python(info, lines)
        return
    owner = None
    # Class membership scoped by indentation: a fun whose indent is <= the
    # last class's indent belongs to an OUTER class (or top level), never the
    # last class seen — e.g. a Kotlin `object AppText { object Contacts {..}
    # fun summary(...) }` must not inherit Contacts.
    owner_stack = []   # [(indent_cols, class_name)]
    docs = collect_doc_comments(lines)

    if lang == "kotlin":
        cls_re, fun_re, sig_re = CLS_RE["kotlin"], KOTLIN_FUN_RE, KOTLIN_FUN_SIG_RE
        method_re = None
    elif lang == "java":
        cls_re, fun_re, sig_re = CLS_RE["java"], JAVA_METHOD_RE, None
        method_re = None
    elif lang == "typescript":
        cls_re, fun_re, sig_re = CLS_RE["typescript"], TS_FUN_RE, None
        method_re = TS_METHOD_RE
    elif lang == "go":
        cls_re, fun_re, sig_re = CLS_RE["go"], GO_FUNC_RE, None
        method_re = None
    else:
        return

    for i, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith(("//", "#", "*", "/*")):
            continue
        if info.package is None:
            m = re.match(r"^(?:package|namespace)\s+([\w.]+)", line)
            if m:
                info.package = m.group(1)
                continue
        m = re.match(r"^import\s+([\w.*]+)", line)
        if m and lang != "go":
            info.imports.append(m.group(1))
            continue
        ind = len(raw) - len(line)   # indent in columns (tabs count as 1)
        m = cls_re.match(line) if cls_re else None
        if m:
            while owner_stack and owner_stack[-1][0] >= ind:
                owner_stack.pop()
            owner = m.group(1)
            owner_stack.append((ind, owner))
            info.symbols.append(Symbol(owner, "class", i, None, clean_signature(line), docs.get(i, "")))
            continue
        m = fun_re.match(line) if fun_re else None
        if not m and method_re:
            m = method_re.match(line)   # TS class methods without "function"
        if m:
            if lang == "go" and m.lastindex and m.lastindex >= 2 and m.group(1):
                owner = m.group(1)      # method receiver: func (r *T) M(...)
                name = m.group(2)
            else:
                while owner_stack and owner_stack[-1][0] >= ind:
                    owner_stack.pop()
                owner = owner_stack[-1][1] if owner_stack else None
                name = m.group(1)
            if sig_re is not None:
                sm = sig_re.match(line)
                sig = clean_signature(sm.group(0)) if sm else clean_signature(line)
            else:
                sig = clean_signature(line)
            info.symbols.append(Symbol(name, "fun", i, owner, sig, docs.get(i, "")))


def python_docstring(lines, sym_line):
    """Return the docstring immediately after def/class at 1-based sym_line ('' if none)."""
    if sym_line >= len(lines):
        return ""
    t = lines[sym_line].strip()
    if not (t.startswith('"""') or t.startswith("'''")):
        return ""
    quote = t[:3]
    if t.endswith(quote) and len(t) > 3:
        inner = t[3:-3].strip()
    else:
        inner = t[3:].strip()
        j = sym_line + 1
        parts = [inner]
        while j < len(lines):
            u = lines[j].strip()
            if quote in u:
                parts.append(u.split(quote)[0].strip())
                break
            parts.append(u)
            j += 1
        inner = " ".join(p for p in parts if p)
    inner = re.sub(r"\s+", " ", inner)
    if len(inner) > MAX_COMMENT_LEN:
        inner = inner[:MAX_COMMENT_LEN] + "..."
    return inner


def parse_python(info: FileInfo, lines):
    """Class membership via indentation; handles class + def in one pass."""
    class_stack = []
    for i, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^class\s+([A-Za-z_]\w*)", line)
        if m:
            class_stack.append((i, m.group(1)))
            info.symbols.append(Symbol(m.group(1), "class", i, None, clean_signature(line), python_docstring(lines, i)))
            continue
        m = PYTHON_DEF_RE.match(raw)
        if m:
            while class_stack and class_stack[-1][0] >= i:
                class_stack.pop()
            owner = class_stack[-1][1] if class_stack else None
            info.symbols.append(Symbol(m.group(2), "fun", i, owner, clean_signature(line), python_docstring(lines, i)))


def extract_body(lines, fun_line_idx, lang):
    """Return the function body text (brace-balanced or indent-based)."""
    if lang in BRACE_LANGS:
        first = lines[fun_line_idx]
        # The opening "{" must belong to THIS function's signature: on the
        # same line, or on a continuation of a multi-line signature /
        # expression body (first line ends with "(", "," or "="). An
        # abstract/interface method (`abstract fun x(): T`, `void f();`) has
        # neither — without this gate the scan would swallow the next
        # declaration's block as this function's body.
        sig_open = "{" in first or first.rstrip().endswith(("(", ",", "="))
        depth = 0
        started = False
        parts = []
        for j in range(fun_line_idx, len(lines)):
            line = lines[j]
            for ch in line:
                if ch == "{":
                    if not started and not sig_open:
                        return None
                    depth += 1
                    started = True
                elif ch == "}":
                    depth -= 1
                    if started and depth <= 0:
                        return "\n".join(parts + [line])
            if started:
                parts.append(line)
        # No opening brace anywhere (multi-line signature included): the
        # function is an expression body (`fun f() = g()`), possibly with the
        # expression continued on more-indented lines. Note the brace scan
        # above found NOTHING, so this cannot be a block-bodied function whose
        # first line merely lacked "{".
        first = lines[fun_line_idx]
        base_ind = len(first) - len(first.lstrip())
        parts = [first]
        for j in range(fun_line_idx + 1, len(lines)):
            nxt = lines[j]
            if nxt.strip() == "":
                continue
            # Continuation lines are deeper-indented; a closing paren line
            # ("    ) = expr") belongs to the signature, not to a new decl.
            if len(nxt) - len(nxt.lstrip()) <= base_ind and not nxt.lstrip().startswith(")"):
                break
            parts.append(nxt)
        body = "\n".join(parts)
        # A brace-less method is only an expression body if it contains an
        # assignment (`fun f() = g()`); interface/abstract method signatures
        # (`fun insert(entity: X)`, `void foo();`) have no `=` and must not
        # produce call edges from their parameter types/names.
        if "=" not in body:
            return None
        return body
    if lang == "python":
        base_indent = len(lines[fun_line_idx]) - len(lines[fun_line_idx].lstrip())
        parts = []
        for j in range(fun_line_idx + 1, len(lines)):
            line = lines[j]
            if line.strip() == "":
                continue
            if len(line) - len(line.lstrip()) <= base_indent:
                break
            parts.append(line)
        return "\n".join(parts)
    return ""


def qname_of(info: FileInfo, sym: Symbol) -> str:
    prefix = info.package or ""
    if sym.owner:
        prefix = prefix + "." + sym.owner if prefix else sym.owner
    return prefix + "." + sym.name if prefix else sym.name


def build_symbol_table(files):
    """qname -> {"kind","file","line","signature"}; short name -> [qname]."""
    table = {}
    short_to_qnames = {}
    for info in files.values():
        for sym in info.symbols:
            qn = qname_of(info, sym)
            table[qn] = {
                "kind": sym.kind,
                "file": info.path,
                "line": sym.line,
                "signature": sym.signature,
                "comment": sym.comment,
            }
            short_to_qnames.setdefault(sym.name, []).append(qn)
    return table, short_to_qnames


def collect_prop_types(files):
    """{(owner_short, prop): type_short} — Kotlin class-level property
    declarations with explicit types, across all files. Used to resolve
    chained call sites like `container.locationSampleRepository.insert`
    (AppContainer.locationSampleRepository : LocationSampleRepository)."""
    props = {}
    for info in files.values():
        if info.lang != "kotlin":
            continue
        lines = info.text.splitlines()
        owner_stack = []  # [(indent_cols, owner_short)]
        for raw in lines:
            t = raw.strip()
            if not t or t.startswith(("//", "*")):
                continue
            ind = len(raw) - len(raw.lstrip())
            m = re.match(r"^(?:data\s+|sealed\s+|enum\s+|abstract\s+|open\s+|internal\s+)*(?:class|interface|object)\s+([A-Za-z_]\w*)", t)
            if m:
                while owner_stack and owner_stack[-1][0] >= ind:
                    owner_stack.pop()
                owner_stack.append((ind, m.group(1)))
                continue
            if not owner_stack:
                continue
            pm = PROP_DECL_RE.match(raw)
            if pm:
                props[(owner_stack[-1][1], pm.group(1))] = pm.group(2)
    return props


def collect_local_types(info, lines, class_short_names, prop_types):
    """{local_name: type_short} — Kotlin property/ctor-param/local declarations
    with explicit types (or a `db.xxxDao()` initializer / `(x as T).prop`
    chain, whose return types are inferred), used to disambiguate
    `prefix.method` call sites: `dao.insert(...)` resolves to the
    AlertHistoryDao.insert the prefix was declared with, instead of every
    same-named method."""
    if info.lang != "kotlin":
        return {}
    types = {}
    for i, ln in enumerate(lines):
        t = ln.strip()
        if re.match(r"^(?:data\s+|sealed\s+|enum\s+|abstract\s+|open\s+|internal\s+)*(?:class|interface|object)\s+\w+.*\(", t):
            # Class constructor list may span lines: merge until the closing
            # ")" at the class indent.
            buf = [ln]
            for k in range(i + 1, len(lines)):
                nxt = lines[k]
                buf.append(nxt)
                if nxt.strip().startswith(")"):
                    break
            for m in LOCAL_TYPE_RE.finditer(" ".join(buf)):
                types[m.group(1)] = m.group(2)
        else:
            for m in LOCAL_TYPE_RE.finditer(ln):
                types[m.group(1)] = m.group(2)
            # Inferred from the initializer: `val dao = db.alertHistoryDao()`
            # has no explicit type; trust the factory name only when the
            # CamelCased class actually exists (avoids inventing types).
            m2 = re.search(r"\b(?:val|var)\s+([A-Za-z_]\w*)\s*=\s*(?:\w+\.)*([A-Za-z_]\w*Dao)\s*\(", ln)
            if m2:
                cap = m2.group(2)[0].upper() + m2.group(2)[1:]
                if cap in class_short_names:
                    types.setdefault(m2.group(1), cap)
            # Inferred from a cast chain: `val container = (appContext as
            # SingleLifeApplication).container` — container's type is the
            # declared type of AppContainer.container.
            m3 = AS_CHAIN_RE.search(ln)
            if m3:
                pt = prop_types.get((m3.group(2), m3.group(3)))
                if pt:
                    types.setdefault(m3.group(1), pt)
            # Inferred from a plain cast: `val app = applicationContext as
            # SingleLifeApplication` — app's type is the cast target.
            m5 = AS_ASSERT_RE.search(ln)
            if m5:
                types.setdefault(m5.group(1), m5.group(2))
            # Inferred from a property chain off a known-typed head:
            # `val historyRepository = container.alertHistoryRepository`
            # has no explicit type; propagate the head's type through the
            # cross-file property table.
            m4 = re.search(r"\b(?:val|var)\s+([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)((?:\.\s*[A-Za-z_]\w*)+)", ln)
            if m4:
                cur = types.get(m4.group(2))
                if cur:
                    for s in re.findall(r"\.\s*([A-Za-z_]\w*)", m4.group(3)):
                        pt = prop_types.get((cur, s))
                        if not pt:
                            cur = None
                            break
                        cur = pt
                    if cur:
                        types.setdefault(m4.group(1), cur)
    return types


def scan_tokens(body, src, short_to_qnames, local_types, file_of_qn, src_file, prop_types=None):
    """Identifiers in body matching known symbols (never self).

    Disambiguation (approximates Kotlin resolution rules):
      - `prefix.method` sites: when the prefix's declared type is known, only
        same-named methods of that type are linked; otherwise same-file
        symbols win, else every same-named symbol (documented approximation).
      - bare tokens: same-file functions are preferred over same-named ones
        in other files (Kotlin resolves a bare call to the current file's
        declaration first).
    Comment lines are skipped so prose mentioning a function name cannot
    fabricate edges.
    """
    outs = []
    seen = set()
    resolved = set()
    body = "\n".join(
        l for l in body.splitlines()
        if not re.match(r"^\s*(//|#|\*)", l)
        # Function declaration lines inside an initializer body (e.g. the
        # `override fun migrate(...)` signature inside a property-assigned
        # anonymous object) name the function but do not call it.
        and not re.match(r"^\s*(?:override\s+|private\s+|internal\s+|protected\s+|public\s+|abstract\s+)*fun\s+[A-Za-z_]\w*\s*\(", l)
    )

    def pool_for(dsts):
        same = [d for d in dsts if file_of_qn.get(d) == src_file and d != src]
        return same if same else [d for d in dsts if d != src]

    def add(pool):
        for d in pool:
            if d not in seen:
                seen.add(d)
                outs.append(d)

    # Property-chain propagation: for each `head.rest...` run whose head has
    # a known local type, walk `(type).segment` through the cross-file
    # property-type table; remember each segment's inferred type so a
    # qualified call `container.locationSampleRepository.insert` resolves to
    # LocationSampleRepository.insert instead of every same-named method.
    seg_types = {}
    if prop_types:
        for m in CHAIN_RE.finditer(body):
            head, tail = m.group(1), m.group(2)
            t = local_types.get(head)
            if not t:
                continue
            for s in tail.split("."):
                s = s.strip()
                if not s:
                    continue
                pt = prop_types.get((t, s))
                if not pt:
                    break
                seg_types[s] = pt
                t = pt

    for m in QUAL_RE.finditer(body):
        pref, meth = m.group(1), m.group(2)
        dsts = short_to_qnames.get(meth)
        if not dsts:
            continue
        typ = local_types.get(pref) or seg_types.get(pref)
        if typ:
            want = [d for d in dsts if d.endswith("." + typ + "." + meth) and d != src]
            if want:
                resolved.add(meth)
                add(want)
                continue
        add(pool_for(dsts))
    for m in IDENT_RE.finditer(body):
        tok = m.group(0)
        if tok in resolved:
            continue
        dsts = short_to_qnames.get(tok)
        if not dsts:
            continue
        add(pool_for(dsts))
    return outs


def kotlin_extra_bodies(info, lines):
    """[(src_qname, body)] — Kotlin property initializers and init{} blocks
    call functions (e.g. `val flow = repo.observe().stateIn(...)`) that no
    fun body contains. Attribute them to the owning class (indent-scoped, like
    parse_text) so callers still show up in the graph."""
    if info.lang != "kotlin":
        return []
    # (class indent, class short name) in line order.
    classes = []
    for sym in info.symbols:
        if sym.kind != "class":
            continue
        raw = lines[sym.line - 1] if 0 <= sym.line - 1 < len(lines) else ""
        classes.append((len(raw) - len(raw.lstrip()), sym.name))

    def owner_for(ind):
        own = None
        for ci, cn in classes:
            if ci < ind:
                own = cn
            else:
                break
        return own

    out = []
    n = len(lines)
    i = 0
    while i < n:
        raw = lines[i]
        t = raw.strip()
        if not t or t.startswith(("//", "*", "/*")):
            i += 1
            continue
        ind = len(raw) - len(raw.lstrip())
        if (t.startswith(("val ", "var ", "const val ")) and "=" in t) or t.startswith("init "):
            own = owner_for(ind)
            src = qname_of(info, Symbol("__property__", "fun", 0, own, ""))
            if t.startswith("init "):
                body = extract_body(lines, i, "kotlin")
            else:
                # Property initializer, possibly continued on more-indented
                # lines or "....chain(" lines.
                body = raw
                k = i + 1
                while k < n:
                    nxt = lines[k]
                    if nxt.strip() == "":
                        k += 1
                        continue
                    if len(nxt) - len(nxt.lstrip()) <= ind and not nxt.strip().startswith((".", "?", ")")):
                        break
                    body += "\n" + nxt
                    k += 1
            out.append((src, body))
        i += 1
    return out


def build_edges(files, short_to_qnames, file_of_qn, class_short_names):
    """file-path -> {src_qname: [dst_qname...]} via identifier matching in bodies."""
    prop_types = collect_prop_types(files)
    edges = {}
    for info in files.values():
        if info.lang not in BRACE_LANGS and info.lang not in PYTHON_LANG:
            continue
        lines = info.text.splitlines()
        local_types = collect_local_types(info, lines, class_short_names, prop_types)
        for sym in info.symbols:
            if sym.kind != "fun":
                continue
            body = extract_body(lines, sym.line - 1, info.lang)
            if not body:
                continue
            src = qname_of(info, sym)
            outs = scan_tokens(body, src, short_to_qnames, local_types, file_of_qn, info.path, prop_types)
            if outs:
                edges.setdefault(info.path, {})[src] = outs
        # Kotlin: property initializers / init{} blocks carry real calls too.
        if info.lang == "kotlin":
            fe = edges.setdefault(info.path, {})
            for src, body in kotlin_extra_bodies(info, lines):
                outs = scan_tokens(body, src, short_to_qnames, local_types, file_of_qn, info.path, prop_types)
                if outs:
                    merged = fe.setdefault(src, [])
                    for x in outs:
                        if x not in merged:
                            merged.append(x)
    return edges


def module_path_for(info: FileInfo, pkg_root: str) -> str:
    """Two-level module key: package-based for Kotlin/Java, dir-based otherwise."""
    if info.lang in ("kotlin", "java") and info.package and pkg_root:
        rest = info.package
        if rest.startswith(pkg_root + "."):
            rest = rest[len(pkg_root) + 1:]
        elif rest == pkg_root:
            rest = ""  # file sits directly in the package root
        parts = rest.split(".") if rest else []
    else:
        parts = [p for p in info.path.split("/") if p]
        # Strip filename, keep first two directory levels.
        parts = parts[:-1]
    while len(parts) > MAX_MODULE_DEPTH:
        parts.pop()
    if not parts:
        parts = ["root"]
    return "/".join(parts)


def module_id(module_path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", module_path).strip("-").lower() or "root"


def module_deps(module_files, pkg_root, module_ids):
    """Packages this module's files import, mapped to module ids.

    Longest module-path prefix match: an import of a class (e.g.
    'core.text.AppText') must resolve to the module of its package
    ('core.text' -> 'core-text'), not to a fake module named after the
    class.  Depth starts at MAX_MODULE_DEPTH because a module path never
    exceeds that many segments.
    """
    deps = set()
    for info in module_files:
        if info.lang in ("kotlin", "java") and info.package and pkg_root:
            for imp in info.imports:
                if imp.startswith(pkg_root) and not imp.startswith(info.package):
                    rest = imp[len(pkg_root) + 1:].split(".")
                    # Skip single-character tails (e.g. generated R class).
                    if not rest or (len(rest) == 1 and len(rest[0]) == 1):
                        continue
                    depth = min(len(rest), MAX_MODULE_DEPTH)
                    while depth > 0:
                        mid = module_id("/".join(rest[:depth]))
                        if mid in module_ids:
                            deps.add(mid)
                            break
                        depth -= 1
    return sorted(deps)


def render_index(meta, modules, pkg_root):
    out = []
    out.append("# Project Structure Graph")
    out.append("")
    out.append("- generated: %s" % meta["generatedAt"])
    out.append("- root: `%s`" % meta["root"])
    out.append("- modules: %d | files: %d | symbols: %d" % (len(modules), meta["fileCount"], meta["symbolCount"]))
    out.append("")
    out.append("## Module Index")
    out.append("")
    for m in modules:
        summary = m.get("summary") or ""
        extra = " - %s" % summary if summary else ""
        out.append("- **%s** (`%s`): %d files, %d symbols - deps: %s [details](%s/%s.md)%s" % (
            m["id"], m["path"], m["fileCount"], m["symbolCount"],
            ", ".join(m["deps"]) if m["deps"] else "-",
            MODULES_DIR, m["id"], extra,
        ))
        for f in m["files"][:8]:
            out.append("  - `%s`" % f)
        if len(m["files"]) > 8:
            out.append("  - ... (%d more)" % (len(m["files"]) - 8))
    out.append("")
    out.append("> Source roots: `%s`" % (pkg_root or "(no package root detected)"))
    return "\n".join(out)


def extract_notes(old_md):
    """{(file, name): note} from a previously annotated module file.

    The AI may have appended "职责: ..." lines under individual symbols.
    Incremental rewrites must keep them, not wipe hand-written notes.
    """
    notes = {}
    if not old_md:
        return notes
    cur_file, cur_name = None, None
    for ln in old_md.splitlines():
        m = re.match(r"^## (.+)$", ln)
        if m:
            cur_file, cur_name = m.group(1), None
            continue
        m = re.match(r"^- `([^`]+)` L\d+ (?:cls|fun) ", ln)
        if m:
            cur_name = m.group(1)
            continue
        m = re.match(r"^职责:\s*(.+)$", ln)
        if m and cur_file and cur_name:
            notes[(cur_file, cur_name)] = m.group(1)
    return notes


def render_module(m, symbols_by_file, old_md=""):
    notes = extract_notes(old_md)
    out = []
    out.append("# %s" % m["id"])
    out.append("")
    out.append("Path: `%s` | Files: %d | Symbols: %d" % (m["path"], m["fileCount"], m["symbolCount"]))
    out.append("")
    out.append("Deps: %s" % (", ".join(m["deps"]) if m["deps"] else "-"))
    out.append("")
    out.append("Summary: %s" % (m.get("summary") or "<!-- AI: fill module responsibility here -->"))
    out.append("")
    for f in m["files"]:
        syms = symbols_by_file.get(f, [])
        out.append("## %s" % f)
        out.append("")
        if not syms:
            out.append("_no symbols extracted (generic language or empty)_")
            out.append("")
            continue
        for s in syms:
            kind = "cls" if s["kind"] == "class" else "fun"
            out.append("- `%s` L%d %s `%s`" % (s["name"], s["line"], kind, s["signature"]))
            note = notes.get((f, s["name"]))
            if note:
                out.append("  职责: %s" % note)
        out.append("")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="code-graph incremental updater")
    ap.add_argument("--root", default=".", help="project root (default: cwd)")
    ap.add_argument("--force", action="store_true", help="full rebuild ignoring manifest")
    ap.add_argument("--no-html", action="store_true", help="skip the code-graph.html viewer regeneration")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    out_dir = os.path.join(root, OUT_DIR_NAME)
    os.makedirs(os.path.join(out_dir, MODULES_DIR), exist_ok=True)
    manifest_path = os.path.join(out_dir, MANIFEST_NAME)

    # 1. Hash all source files, diff against manifest.
    old_manifest = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                old_manifest = json.load(f)
        except Exception:
            old_manifest = {}

    source_paths = []
    new_manifest = {}
    for path in walk_sources(root):
        lang = lang_of(path)
        if not lang:
            continue
        rel = relpath(root, path)
        source_paths.append((rel, path, lang))
        new_manifest[rel] = hash_file(path)

    changed = []
    deleted = []
    for rel in old_manifest:
        if rel not in new_manifest:
            deleted.append(rel)
    for rel, path, lang in source_paths:
        if args.force or old_manifest.get(rel) != new_manifest[rel]:
            changed.append(rel)

    # 2. Load previous graph for untouched files.
    graph_path = os.path.join(out_dir, GRAPH_NAME)
    old_graph = {}
    if os.path.exists(graph_path):
        try:
            with open(graph_path, "r", encoding="utf-8") as f:
                old_graph = json.load(f)
        except Exception:
            old_graph = {}

    old_files = {k: v for k, v in old_graph.get("files", {}).items() if k not in changed and k not in deleted}
    for rel in deleted:
        old_files.pop(rel, None)

    # 3. Scan changed files.
    files = {}
    for rel, path, lang in source_paths:
        if rel in old_files and rel not in changed:
            files[rel] = old_files[rel]
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        info = FileInfo(rel, lang)
        info.text = text
        parse_text(info)
        files[rel] = {
            "lang": lang,
            "package": info.package or "",
            "imports": info.imports,
            "symbols": [
                {"name": s.name, "kind": s.kind, "line": s.line,
                 "owner": s.owner, "signature": s.signature,
                 "comment": s.comment}
                for s in info.symbols
            ],
        }

    # 4. Symbol table + call edges (full pass; cheap).
    info_map = {}
    for rel, data in files.items():
        info = FileInfo(rel, data["lang"])
        info.package = data["package"] or None
        info.imports = data["imports"]
        info.symbols = [
            Symbol(s["name"], s["kind"], s["line"], s["owner"],
                   s["signature"], s.get("comment", ""))
            for s in data["symbols"]
        ]
        # Re-read source bodies for call-edge building (structure reuses the cached
        # graph, but edges need the actual text; a full pass is cheap).
        try:
            with open(os.path.join(root, rel.replace("/", os.sep)), "r", encoding="utf-8", errors="replace") as f:
                info.text = f.read()
        except OSError:
            info.text = ""
        info_map[rel] = info

    table, short_to_qnames = build_symbol_table(info_map)
    # Call edges point at functions only (class-name tokens are references, not calls).
    fun_short = {
        n: qns
        for n, qns in short_to_qnames.items()
        if any(table[q]["kind"] == "fun" for q in qns)
    }
    file_of_qn = {qn: v["file"] for qn, v in table.items()}
    class_short_names = {qn.rsplit(".", 1)[-1] for qn, v in table.items() if v["kind"] == "class"}
    edges = build_edges(info_map, fun_short, file_of_qn, class_short_names)

    # 5. Group into modules.
    pkg_root = common_package_root([i.package for i in info_map.values() if i.package])
    mod_files = {}
    for rel, info in info_map.items():
        mp = module_path_for(info, pkg_root)
        mod_files.setdefault(mp, []).append(info)

    modules = []
    module_ids = {module_id(mp) for mp in mod_files}
    for mp, infos in sorted(mod_files.items()):
        mid = module_id(mp)
        old_mod = next((x for x in old_graph.get("modules", []) if x["id"] == mid), None)
        deps = [d for d in module_deps(infos, pkg_root, module_ids) if d != mid]
        syms = [s for i in infos for s in i.symbols]
        modules.append({
            "id": mid,
            "path": mp,
            "files": sorted(i.path for i in infos),
            "fileCount": len(infos),
            "symbolCount": len(syms),
            "deps": deps,
            "summary": (old_mod or {}).get("summary", ""),
        })

    # 6. Write outputs.
    graph = {
        "version": 1,
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": root,
        "packageRoot": pkg_root,
        "modules": modules,
        "files": files,
        "symbols": table,
        "edges": edges,
    }
    with open(graph_path, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=1)

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(new_manifest, f, ensure_ascii=False, indent=0)

    # Module markdown files (only rewrite modules with changed files).
    changed_modules = set()
    for rel in changed + deleted:
        for mp, infos in mod_files.items():
            if any(i.path == rel for i in infos):
                changed_modules.add(module_id(mp))
    for m in modules:
        if m["id"] in changed_modules or args.force:
            symbols_by_file = {}
            for f in m["files"]:
                data = files.get(f)
                if data:
                    symbols_by_file[f] = data["symbols"]
            md_path = os.path.join(out_dir, MODULES_DIR, m["id"] + ".md")
            old_md = ""
            if os.path.exists(md_path):
                try:
                    with open(md_path, "r", encoding="utf-8") as f:
                        old_md = f.read()
                except OSError:
                    old_md = ""
            md = render_module(m, symbols_by_file, old_md)
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(md + "\n")
    # Modules that vanished (all files deleted / moved) leave stale slices;
    # drop them so the slice dir mirrors the graph.
    live_ids = {m["id"] for m in modules}
    for fn in os.listdir(os.path.join(out_dir, MODULES_DIR)):
        if fn.endswith(".md") and fn[:-3] not in live_ids:
            os.remove(os.path.join(out_dir, MODULES_DIR, fn))

    # index.md (always rewritten; small).
    meta = {
        "generatedAt": graph["generatedAt"],
        "root": root,
        "fileCount": len(files),
        "symbolCount": len(table),
    }
    with open(os.path.join(out_dir, INDEX_NAME), "w", encoding="utf-8") as f:
        f.write(render_index(meta, modules, pkg_root) + "\n")

    report = {
        "changed": sorted(changed),
        "deleted": sorted(deleted),
        "modules": len(modules),
        "files": len(files),
        "symbols": len(table),
        "graphPath": os.path.join(out_dir, GRAPH_NAME).replace(os.sep, "/"),
    }

    # 7. Regenerate the self-contained HTML viewer inside .code-graph/ (best
    #    effort; never fail the update over viewer issues). All code-graph
    #    artifacts live under .code-graph/, never in the project root.
    if not args.no_html:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from render_html import generate as render_html_generate
            html_path = os.path.join(out_dir, "code-graph.html")
            render_html_generate(root, graph, html_path)
            report["htmlViewer"] = html_path.replace(os.sep, "/")
        except Exception as exc:
            report["htmlViewer"] = "failed: %s" % exc

    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
