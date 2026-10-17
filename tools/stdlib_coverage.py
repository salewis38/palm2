#!/usr/bin/env python3
"""Dependency-free statement + branch coverage for PALM (needs Python 3.12+ for sys.monitoring).

Use when coverage.py is not available. Run from the repository root:

    python tools/stdlib_coverage.py                      # palm.py, tests/, writes ./coverage-report/
    python tools/stdlib_coverage.py --fail-under 85

Method
------
* Line hits come from sys.monitoring LINE events, recorded only for the target file.
* Executable statements come from the AST, restricted to lines that really have bytecode
  (so `global`, docstrings and bare `else:` lines are not counted).
* Branches are the conditional jumps in the compiled bytecode (POP_JUMP_IF_*, FOR_ITER), minus the
  compiler-generated ones behind `except` matching and `with` cleanup; `# pragma: no cover` is honoured.
  Each has two outcomes (jump / fall through); an outcome counts when BRANCH events saw it.
  This resembles, but is not identical to, coverage.py's arc-based branch measurement.
* Combined % = (hit statements + covered branch outcomes) / (statements + branch outcomes).
"""
from __future__ import annotations

import argparse
import ast
import bisect
import contextlib
import dis
import html
import io
import json
import os
import pathlib
import platform
import re
import sys
import time
import types
import unittest
from collections import defaultdict
from datetime import datetime

if sys.version_info < (3, 12):
    sys.exit("stdlib_coverage.py needs Python 3.12+ (sys.monitoring); use coverage.py on older versions")

MON = sys.monitoring
TOOL = MON.COVERAGE_ID
EV = MON.events
PRAGMA = re.compile(r"#\s*pragma:\s*no\s*cover")
BRANCH_OPS = {"POP_JUMP_IF_TRUE", "POP_JUMP_IF_FALSE", "POP_JUMP_IF_NONE", "POP_JUMP_IF_NOT_NONE", "FOR_ITER"}


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
class Collector:
    def __init__(self, target: str):
        self.target = os.path.realpath(target)
        self.lines: set[int] = set()
        self.branches: dict[tuple, set[int]] = defaultdict(set)
        self._own: dict[str, bool] = {}

    def _is_target(self, code) -> bool:
        name = code.co_filename
        hit = self._own.get(name)
        if hit is None:
            hit = self._own[name] = os.path.realpath(name) == self.target
        return hit

    def on_line(self, code, line):
        if self._is_target(code):
            self.lines.add(line)
        return MON.DISABLE                      # one hit per location is enough

    def on_branch(self, code, offset, dest):
        if not self._is_target(code):
            return MON.DISABLE
        self.branches[(code.co_qualname, code.co_firstlineno, offset)].add(dest)
        return None

    def start(self):
        MON.use_tool_id(TOOL, "stdlib-coverage")
        MON.register_callback(TOOL, EV.LINE, self.on_line)
        MON.register_callback(TOOL, EV.BRANCH, self.on_branch)
        MON.set_events(TOOL, EV.LINE | EV.BRANCH)

    def stop(self):
        MON.set_events(TOOL, 0)
        MON.register_callback(TOOL, EV.LINE, None)
        MON.register_callback(TOOL, EV.BRANCH, None)
        MON.free_tool_id(TOOL)


def run_tests(test_dir: str):
    loader = unittest.TestLoader()
    suite = loader.discover(test_dir)
    by_module: dict[str, int] = defaultdict(int)

    def flatten(s):
        for item in s:
            if isinstance(item, unittest.TestSuite):
                yield from flatten(item)
            else:
                yield item

    for case in flatten(suite):
        by_module[type(case).__module__] += 1
    sink = io.StringIO()
    started = time.perf_counter()
    with open(os.devnull, "w") as devnull, contextlib.redirect_stderr(devnull), contextlib.redirect_stdout(devnull):
        result = unittest.TextTestRunner(stream=sink, verbosity=0).run(suite)
    return result, time.perf_counter() - started, dict(sorted(by_module.items())), sink.getvalue()


# --------------------------------------------------------------------------- #
# Static analysis
# --------------------------------------------------------------------------- #
def iter_code(code: types.CodeType):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from iter_code(const)


def static_branches(code_root: types.CodeType):
    code_lines: set[int] = set()
    branches: dict[tuple, dict] = {}
    for code in iter_code(code_root):
        code_lines |= {ln for _, _, ln in code.co_lines() if ln}
        instrs = list(dis.get_instructions(code))
        off2line, last = {}, code.co_firstlineno
        for ins in instrs:
            ln = ins.positions.lineno if ins.positions and ins.positions.lineno else last
            off2line[ins.offset] = last = ln
        for idx, ins in enumerate(instrs):
            if ins.opname in BRANCH_OPS:
                window = [p.opname for p in instrs[max(0, idx - 9):idx]]
                if "WITH_EXCEPT_START" in window or "CHECK_EXC_MATCH" in window[-2:]:
                    continue                    # `with` cleanup / `except` matching: not decisions in the source
                fall = instrs[idx + 1].offset if idx + 1 < len(instrs) else None
                branches[(code.co_qualname, code.co_firstlineno, ins.offset)] = {
                    "line": off2line[ins.offset], "fall": fall, "fall_line": off2line.get(fall),
                    "jump_line": off2line.get(ins.argval), "op": ins.opname}
    return code_lines, branches


def collect_statements(tree: ast.Module, src_lines: list[str]):
    """Return ({first_line: [last_line, scope]}, excluded_lines) for every statement or header.

    A `# pragma: no cover` on a statement (or on a compound statement's header) excludes it, and
    for compound statements everything inside it, as coverage.py does by default."""
    out: dict[int, list] = {}
    excluded: set[int] = set()

    def exclude_if_pragma(node, start, end):
        if any(PRAGMA.search(src_lines[i - 1]) for i in range(start, min(end, len(src_lines)) + 1)):
            excluded.update(range(start, (node.end_lineno or end) + 1))

    def add(start, end, scope):
        end = max(start, end)
        if start in out:
            out[start][0] = max(out[start][0], end)
        else:
            out[start] = [end, scope]

    def header_end(node, body):
        return (body[0].lineno - 1) if body else node.lineno

    def walk(body, scope, doc_ok):
        for i, node in enumerate(body):
            if (doc_ok and i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                add(start, header_end(node, node.body), scope)
                exclude_if_pragma(node, start, header_end(node, node.body))
                inner = node.name if scope == "<module>" else f"{scope}.{node.name}"
                walk(node.body, inner, True)
            elif isinstance(node, ast.Try):
                add(node.lineno, header_end(node, node.body), scope)
                exclude_if_pragma(node, node.lineno, header_end(node, node.body))
                walk(node.body, scope, False)
                for handler in node.handlers:
                    add(handler.lineno, header_end(handler, handler.body), scope)
                    exclude_if_pragma(handler, handler.lineno, header_end(handler, handler.body))
                    walk(handler.body, scope, False)
                walk(node.orelse, scope, False)
                walk(node.finalbody, scope, False)
            elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
                add(node.lineno, header_end(node, node.body), scope)
                exclude_if_pragma(node, node.lineno, header_end(node, node.body))
                walk(node.body, scope, False)
                walk(node.orelse, scope, False)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                add(node.lineno, header_end(node, node.body), scope)
                exclude_if_pragma(node, node.lineno, header_end(node, node.body))
                walk(node.body, scope, False)
            else:
                add(node.lineno, node.end_lineno or node.lineno, scope)
                exclude_if_pragma(node, node.lineno, node.end_lineno or node.lineno)

    walk(tree.body, "<module>", True)
    return out, excluded


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def analyse(path: pathlib.Path, col: Collector):
    src = path.read_text()
    src_lines = src.splitlines()
    tree = ast.parse(src)
    code_lines, branches = static_branches(compile(src, str(path), "exec"))
    stmts, excluded = collect_statements(tree, src_lines)

    executable = {s: (e, scope) for s, (e, scope) in stmts.items()
                  if s not in excluded and any(l in code_lines for l in range(s, e + 1))}
    starts = sorted(executable)

    def stmt_for(line):
        i = bisect.bisect_right(starts, line) - 1
        while i >= 0:
            s = starts[i]
            if executable[s][0] >= line:
                return s
            i -= 1
        return None

    line_status: dict[int, str] = {}
    line_notes: dict[int, list[str]] = defaultdict(list)
    scopes: dict[str, dict] = defaultdict(lambda: dict(stmts=0, miss=0, br=0, br_miss=0, missed_lines=[]))

    for s, (e, scope) in executable.items():
        hit = any(l in col.lines for l in range(s, e + 1))
        line_status[s] = "hit" if hit else "miss"
        row = scopes[scope]
        row["stmts"] += 1
        if not hit:
            row["miss"] += 1
            row["missed_lines"].append(s)

    branch_rows = []
    for key, info in branches.items():
        if info["line"] in excluded:
            continue
        owner = stmt_for(info["line"])
        scope = executable[owner][1] if owner is not None else "<module>"
        seen = col.branches.get(key, set())
        fall_seen = info["fall"] in seen
        jump_seen = any(d != info["fall"] for d in seen)
        row = scopes[scope]
        row["br"] += 2
        missing = []
        for seen_flag, kind, target in ((fall_seen, "fall-through", info["fall_line"]),
                                        (jump_seen, "jump", info["jump_line"])):
            if seen_flag:
                continue
            if target == info["line"]:           # and / or / chained comparison inside one line
                missing.append(f"one {kind} outcome of a condition on this line never occurred")
            else:
                missing.append(f"never {'fell through' if kind == 'fall-through' else 'jumped'} to line {target}")
        row["br_miss"] += len(missing)
        if missing and owner is not None:
            line_notes[owner].append(f"line {info['line']}: " + "; ".join(missing))
            if line_status.get(owner) == "hit":
                line_status[owner] = "partial"
        branch_rows.append(dict(line=info["line"], scope=scope, missing=missing))

    totals = dict(
        stmts=sum(r["stmts"] for r in scopes.values()), miss=sum(r["miss"] for r in scopes.values()),
        br=sum(r["br"] for r in scopes.values()), br_miss=sum(r["br_miss"] for r in scopes.values()))
    return dict(src_lines=src_lines, excluded=sorted(excluded), line_status=line_status, line_notes=dict(line_notes),
                scopes=dict(scopes), totals=totals, executable=executable)


def pct(hit, total):
    return 100.0 if total == 0 else 100.0 * hit / total


def row_pct(r):
    total = r["stmts"] + r["br"]
    return pct(total - r["miss"] - r["br_miss"], total)


def ranges(lines):
    lines = sorted(lines)
    out, i = [], 0
    while i < len(lines):
        j = i
        while j + 1 < len(lines) and lines[j + 1] == lines[j] + 1:
            j += 1
        out.append((lines[i], lines[j]))
        i = j + 1
    return out


def fmt_range(a, b):
    return str(a) if a == b else f"{a}-{b}"


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
def build_markdown(meta, res):
    t = res["totals"]
    stmt_pct = pct(t["stmts"] - t["miss"], t["stmts"])
    br_pct = pct(t["br"] - t["br_miss"], t["br"])
    comb = row_pct(t)
    L = [f"# Test coverage report: `{meta['target']}` ({meta['version']})", "",
         f"Generated {meta['when']} with Python {meta['python']} (stdlib `sys.monitoring`; coverage.py not used).", "",
         "## Test run", "",
         f"- **{meta['tests_run']} tests**, {meta['failures']} failures, {meta['errors']} errors, "
         f"{meta['skipped']} skipped, in {meta['duration']:.2f}s",
         f"- Lines excluded by `# pragma: no cover`: {meta['excluded']}", ""]
    L += ["| Test module | Tests |", "|---|---:|"] + [f"| `{m}` | {n} |" for m, n in meta["by_module"].items()] + [""]
    L += ["## Summary", "", "| Metric | Covered | Total | % |", "|---|---:|---:|---:|",
          f"| Statements | {t['stmts'] - t['miss']} | {t['stmts']} | {stmt_pct:.1f}% |",
          f"| Branch outcomes | {t['br'] - t['br_miss']} | {t['br']} | {br_pct:.1f}% |",
          f"| **Combined** | {t['stmts'] + t['br'] - t['miss'] - t['br_miss']} | {t['stmts'] + t['br']} | **{comb:.1f}%** |", ""]

    groups: dict[str, dict] = defaultdict(lambda: dict(stmts=0, miss=0, br=0, br_miss=0))
    for scope, r in res["scopes"].items():
        g = groups[scope.split(".")[0]]
        for k in ("stmts", "miss", "br", "br_miss"):
            g[k] += r[k]
    L += ["## By class / top-level function", "", "| Scope | Stmts | Missed | Branch outcomes | Missed | Cover |",
          "|---|---:|---:|---:|---:|---:|"]
    for name, g in sorted(groups.items(), key=lambda kv: (row_pct(kv[1]), kv[0])):
        L.append(f"| `{name}` | {g['stmts']} | {g['miss']} | {g['br']} | {g['br_miss']} | {row_pct(g):.1f}% |")
    L.append("")

    gaps = [(s, r) for s, r in res["scopes"].items() if r["miss"] or r["br_miss"]]
    gaps.sort(key=lambda kv: (-(kv[1]["miss"] + kv[1]["br_miss"]), kv[0]))
    L += ["## Functions below 100%", "", "| Function | Missed stmts | Missed branch outcomes | Cover | Uncovered lines |",
          "|---|---:|---:|---:|---|"]
    for scope, r in gaps:
        rs = ", ".join(fmt_range(a, b) for a, b in ranges(r["missed_lines"])) or "-"
        L.append(f"| `{scope}` | {r['miss']} | {r['br_miss']} | {row_pct(r):.1f}% | {rs} |")
    L.append("")

    L += ["## Uncovered statements", ""]
    for scope, r in gaps:
        if not r["missed_lines"]:
            continue
        L.append(f"**`{scope}`**")
        L.append("")
        for a, b in ranges(r["missed_lines"]):
            text = res["src_lines"][a - 1].strip()
            L.append(f"- line {fmt_range(a, b)}: `{text[:100]}`")
        L.append("")

    L += ["## Partially covered branches", ""]
    any_partial = False
    for line in sorted(res["line_notes"]):
        if res["line_status"].get(line) == "partial":
            any_partial = True
            L.append(f"- line {line} `{res['src_lines'][line - 1].strip()[:90]}`: " + " | ".join(res["line_notes"][line]))
    if not any_partial:
        L.append("None.")
    L += ["", "## Method and limits", "",
          "- Statement hits: `sys.monitoring` LINE events for the target file only. Executable statements: AST, "
          "restricted to lines with bytecode.",
          "- Branch outcomes: conditional jumps in the bytecode, each with two outcomes. Close to, but not identical to, "
          "coverage.py's arc measurement, so percentages will differ slightly.",
          "- Lines marked `# pragma: no cover` are excluded (coverage.py's default), as are the compiler-generated jumps "
          "behind `except` matching and `with` cleanup. Nothing else is excluded: the `if __name__ == '__main__'` block "
          "counts as uncovered because tests import the module.",
          "- Fakes replace the Modbus library, Shelly and PVOutput, so this measures how much of PALM's own code the tests "
          "execute, not whether it works against real hardware."]
    return "\n".join(L) + "\n"


CSS = """
:root{--bg:#fff;--fg:#1c1e21;--mut:#6b7280;--line:#e5e7eb;--hit:#16a34a;--miss:#fee2e2;--missb:#dc2626;--part:#fef3c7;--partb:#d97706;--card:#f8fafc}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e6e8eb;--mut:#9aa3af;--line:#262a31;--hit:#22c55e;--miss:#3b1618;--missb:#ef4444;--part:#3a2d10;--partb:#f59e0b;--card:#171a20}}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}.sub{color:var(--mut);margin-bottom:16px}
.cards{display:flex;gap:12px;flex-wrap:wrap}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 16px;min-width:140px}
.card b{display:block;font-size:22px}.card span{color:var(--mut);font-size:12px}
table{border-collapse:collapse;width:100%;max-width:1000px}th,td{padding:5px 10px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}th{color:var(--mut);font-weight:600}
.bar{display:inline-block;width:90px;height:8px;background:var(--line);border-radius:4px;vertical-align:middle;margin-left:8px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--hit)}
pre.src{margin:0;border:1px solid var(--line);border-radius:8px;overflow:auto;font:12.5px/1.45 ui-monospace,Menlo,Consolas,monospace}
.l{display:flex;border-left:4px solid transparent}.l .n{flex:0 0 52px;text-align:right;padding:0 10px;color:var(--mut);user-select:none}
.l .c{white-space:pre;padding-right:12px}.l.hit{border-left-color:var(--hit)}
.l.miss{background:var(--miss);border-left-color:var(--missb)}.l.partial{background:var(--part);border-left-color:var(--partb)}
.note{color:var(--partb);font-size:11px;padding-left:12px}code{font:12px ui-monospace,Menlo,monospace}
"""


def build_html(meta, res):
    t = res["totals"]
    comb = row_pct(t)
    stmt_pct = pct(t["stmts"] - t["miss"], t["stmts"])
    br_pct = pct(t["br"] - t["br_miss"], t["br"])
    e = html.escape
    out = [f"<!doctype html><meta charset=utf-8><title>Coverage: {e(meta['target'])}</title><style>{CSS}</style>",
           f"<h1>Test coverage: <code>{e(meta['target'])}</code> {e(meta['version'])}</h1>",
           f"<div class=sub>{e(meta['when'])} &middot; Python {e(meta['python'])} &middot; "
           f"{meta['tests_run']} tests, {meta['failures']} failures, {meta['errors']} errors &middot; stdlib sys.monitoring</div>",
           "<div class=cards>"]
    for label, value in (("Combined", f"{comb:.1f}%"), ("Statements", f"{stmt_pct:.1f}%"),
                         ("Branch outcomes", f"{br_pct:.1f}%"),
                         ("Missed statements", f"{t['miss']} / {t['stmts']}"),
                         ("Missed branch outcomes", f"{t['br_miss']} / {t['br']}")):
        out.append(f"<div class=card><b>{value}</b><span>{label}</span></div>")
    out.append("</div><h2>By function</h2><table><tr><th>Function<th>Stmts<th>Missed<th>Branch outcomes<th>Missed<th>Cover</tr>")
    rows = sorted(res["scopes"].items(), key=lambda kv: (row_pct(kv[1]), kv[0]))
    for scope, r in rows:
        p = row_pct(r)
        out.append(f"<tr><td><code>{e(scope)}</code><td>{r['stmts']}<td>{r['miss']}<td>{r['br']}<td>{r['br_miss']}"
                   f"<td>{p:.1f}%<span class=bar><i style='width:{p:.0f}%'></i></span></tr>")
    out.append("</table><h2>Annotated source</h2>"
               "<div class=sub>Green bar: executed &middot; red: never executed &middot; amber: executed but a branch outcome was never taken</div>"
               "<pre class=src>")
    for n, text in enumerate(res["src_lines"], 1):
        status = res["line_status"].get(n, "")
        notes = res["line_notes"].get(n)
        title = f" title='{e(' | '.join(notes), quote=True)}'" if notes else ""
        note = f"<span class=note>&#9888; {e(' | '.join(notes))}</span>" if notes and status == "partial" else ""
        out.append(f"<div class='l {status}'{title}><span class=n>{n}</span><span class=c>{e(text) or ' '}{note}</span></div>")
    out.append("</pre>")
    return "".join(out)


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default="palm.py")
    ap.add_argument("--tests", default="tests")
    ap.add_argument("--out", default="coverage-report")
    ap.add_argument("--fail-under", type=float, default=None, help="exit 1 if combined coverage is below this")
    args = ap.parse_args()

    target = pathlib.Path(args.target).resolve()
    sys.path.insert(0, str(target.parent))
    col = Collector(str(target))
    col.start()
    try:
        result, duration, by_module, _ = run_tests(args.tests)
    finally:
        col.stop()

    res = analyse(target, col)
    version = next((ln.split('"')[1] for ln in res["src_lines"] if ln.startswith("PALM_VERSION")), "")
    meta = dict(target=target.name, version=version, when=datetime.now().strftime("%Y-%m-%d %H:%M"),
                python=platform.python_version(), tests_run=result.testsRun, failures=len(result.failures),
                errors=len(result.errors), skipped=len(result.skipped), duration=duration, by_module=by_module,
                excluded=len(res['excluded']))

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "COVERAGE.md").write_text(build_markdown(meta, res))
    (out / "index.html").write_text(build_html(meta, res))
    t = res["totals"]
    (out / "coverage.json").write_text(json.dumps(dict(
        meta=meta, totals=t, combined_pct=round(row_pct(t), 2),
        functions={k: {kk: vv for kk, vv in v.items()} for k, v in res["scopes"].items()},
        missed_lines=sorted(l for l, s in res["line_status"].items() if s == "miss")), indent=2, default=str))

    comb = row_pct(t)
    print(f"{meta['tests_run']} tests, {meta['failures']} failures, {meta['errors']} errors")
    print(f"statements {t['stmts'] - t['miss']}/{t['stmts']}  branch outcomes {t['br'] - t['br_miss']}/{t['br']}  "
          f"combined {comb:.1f}%")
    print(f"reports written to {out}/")
    if result.failures or result.errors:
        return 1
    if args.fail_under is not None and comb < args.fail_under:
        print(f"FAIL: coverage {comb:.1f}% is below --fail-under {args.fail_under}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
