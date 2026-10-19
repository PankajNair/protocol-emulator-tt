#!/usr/bin/env python3
"""
blind_mutate.py -- blind (operator-based) mutation campaign
(docs/VAL.md section 20).

scripts/mutate.py's 23 mutants were each chosen by someone who knew the
gates; killing them shows the known gaps are closed, not how many are
left. This script generates mutants mechanically from src/ with standard
operator rules, with no human choosing sites, and measures every gate
against them:

    blind_mutate.py list  [--cap N] [--seed S]        # print the sample
    blind_mutate.py run   [--cap N] [--seed S] [-j J] [--out F] [--ids ...]
    blind_mutate.py report [--out F]

Operators (one site per mutant):
    REL    == <-> !=,  < <-> <=,  > <-> >=   (relational only: inside
           parentheses or right of an assignment, never a nonblocking <=)
    LOG    && <-> ||
    BIT    & <-> |,  ^ -> &,  drop ~,  drop !
    ARITH  + <-> -,  << <-> >>              (not inside [] selects)
    CONST  sized literal +1 / -1 mod 2^width (1-bit: flip)
    COND   if (c) -> if (!(c))
    DEL    delete an assignment statement (replaced by a null statement)

Excluded by construction: every site inside a `for (...)` header
(init, bound, step). These loops only replicate logic (reset of the
register file, per-pin flops) and are unrolled at elaboration. A step
turned from i + 1 into i - 1 never terminates: the simulator spins at
time 0 allocating memory (one reached 3 GB in 3 minutes) until the suite
timeout, so it measures the tool, not the gates. A bound change (< 4 ->
<= 4) only touches a nonexistent index. A suite that still hits its
timeout is recorded as `killed:<suite>:timeout` and listed by `report`.

Per mutant, in a private copy of the working tree (the real src/ is never
touched): compile (iverilog; a compile error = STILLBORN, excluded from
scores), then simulation -- directed, SEEDS random differential seeds,
board testbench, stopping at the first failing suite -- and, separately,
formal alone (`run_formal.py --induct 20 --all`). A suite timeout counts
as a kill (a hang is observable), as in mutate.py. Results append to
--out (JSONL) as they finish; `run` skips ids already there, so an
interrupted campaign resumes.

Survivors of every gate are either EQUIVALENT (no spec-visible change;
documented and excluded) or real gaps (promoted to mutate.py with a test
or property that kills them). Must run in the parent session.
"""

import argparse
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from triage_bench import copy_tree  # noqa: E402

VENV_ACT = ROOT / ".venv" / "bin" / "activate"
DEFAULT_OUT = ROOT / "blind_mutation_results.jsonl"
SRC_FILES = ["cpu/core.v", "cpu/regfile.v", "cpu/isa_defs.v", "io/cycle_counter.v",
             "io/pin_ctrl.v", "mem/mem.v", "top.v"]
SIM_TIMEOUT = 300
FORMAL_TIMEOUT = 1800
DECL = re.compile(r"^\s*(wire|reg|logic|integer|localparam|parameter|genvar)\b")
SKIP_LINE = re.compile(r"^\s*(input|output|inout|module|endmodule|`default_nettype|`include|`timescale)\b")


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def mask(text: str) -> str:
    """Comments and strings blanked to spaces, positions preserved."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif text[i] == '"':
            j = text.find('"', i + 1)
            j = n if j < 0 else j + 1
        else:
            i += 1
            continue
        for k in range(i, j):
            if out[k] != "\n":
                out[k] = " "
        i = j
    return "".join(out)


def context(m: str) -> tuple[list[int], list[int], list[bool], set[int]]:
    """Per position: paren depth, bracket depth, 'in an expression' flag;
    plus the positions of nonblocking-assignment `<=`."""
    n = len(m)
    pd, bd, expr = [0] * n, [0] * n, [False] * n
    nba = set()
    p = b = 0
    in_rhs = False
    line_start = 0
    line_ok = True
    in_define = False
    for i, c in enumerate(m):
        if i == line_start:
            e = m.find("\n", i)
            line = m[i:e if e >= 0 else n]
            line_ok = not SKIP_LINE.match(line)
            in_define = line.lstrip().startswith("`define")
            if in_define:
                in_rhs = True  # a macro body is an expression, ended by its newline
        if c == "\n":
            line_start = i + 1
            if in_define:
                in_rhs = False
        pd[i], bd[i] = p, b
        expr[i] = line_ok and (p > 0 or in_rhs)
        if c == "(":
            p += 1
        elif c == ")":
            p = max(p - 1, 0)
        elif c == "[":
            b += 1
        elif c == "]":
            b = max(b - 1, 0)
        elif c == ";":
            in_rhs = False
        elif c == "=" and p == 0:
            prev, nxt = m[i - 1] if i else "", m[i + 1] if i + 1 < n else ""
            if prev == "<" and not in_rhs:
                nba.add(i - 1)
                in_rhs = True
            elif prev not in "=!<>" and nxt != "=":
                in_rhs = True
        elif c == "?" and p == 0:
            in_rhs = True
    return pd, bd, expr, nba


OPS = [
    # (group, regex, replacement-or-None, needs_expr, allow_in_brackets)
    ("REL", r"(?<![=!<>])==(?!=)", "!=", True, True),
    ("REL", r"!=(?!=)", "==", True, True),
    ("REL", r"(?<![<>=!])<=", "<", True, True),
    ("REL", r"(?<![<>=!-])<(?![<=])", "<=", True, True),
    ("REL", r"(?<![<>=])>=", ">", True, True),
    ("REL", r"(?<![<>=-])>(?![>=])", ">=", True, True),
    ("LOG", r"&&", "||", True, True),
    ("LOG", r"\|\|", "&&", True, True),
    ("BIT", r"(?<!&)&(?!&)", "|", True, True),
    ("BIT", r"(?<!\|)\|(?!\|)", "&", True, True),
    ("BIT", r"\^", "&", True, True),
    ("BIT", r"~(?![&|^])", "", True, True),
    ("BIT", r"!(?!=)", "", True, True),
    ("ARITH", r"(?<![+])\+(?![+:])", "-", True, False),
    ("ARITH", r"(?<![-'\d])-(?![-:>])", "+", True, False),
    ("ARITH", r"(?<!<)<<(?![<=])", ">>", True, False),
    ("ARITH", r"(?<!>)>>(?![>=])", "<<", True, False),
]
# Survivors of every gate that were traced and found to have no
# spec-visible effect. Keyed by (file, line) and pinned to that line's
# exact text: if the RTL changes there, the entry stops matching and the
# survivor is reported as unexplained again.
EQUIVALENT = {
    ("cpu/core.v", 207): ("reg_w_data = 8'd0;",
        "default write data of the regfile mux; every opcode that sets reg_w_en also sets reg_w_data"),
    ("cpu/core.v", 281): ("mem_addr_c  = 10'd0;",
        "default SRAM address; the byte it reads lands in FETCH_LO, used there only for a pending "
        "LOAD/LOADX writeback, and those set their own address"),
    ("cpu/core.v", 283): ("mem_wdata_c = 8'd0;", "default write data; only read when mem_we_c is 1, which sets it"),
    ("cpu/core.v", 318): ("assign set_mode  = (state == S_LOAD) ? 2'd0 : w_rd;",
        "LOAD-mode ack drives pin 5, whose oe/out are hardwired by role; mode[5] is never read"),
    ("cpu/core.v", 339): ("ir                    <= 16'd0;",
        "EXECUTE's first cycle decodes the live {mem_rdata, ir_lo} and latches ir; before that, ir only "
        "feeds logic gated on state == S_EXECUTE"),
    ("cpu/core.v", 351): ("pending_lx_rd         <= 2'd0;",
        "only read while pending_lx_writeback is 1, which reset clears and LOAD/LOADX set together with it"),
    ("cpu/core.v", 437): ("wire _unused = &{start_fall, illegal_op_flag, call_ret_misuse_flag, 1'b0};",
        "lint tie-off with no reader"),
    ("cpu/isa_defs.v", 47): ("`define OP_NOP    5'd0", "macro referenced only in comments"),
    ("cpu/isa_defs.v", 229): ("`define SHIFT_RIGHT  1'b1", "macro referenced only in comments (core.v tests SHIFT_LEFT)"),
    ("cpu/isa_defs.v", 237): ("`define BITSEL_BIT0  1'b0", "macro referenced only in comments (core.v tests BITSEL_BIT7)"),
    ("io/pin_ctrl.v", 202): ("assign uio_out[gi] = 1'b0;",
        "HOST_GO pin's out value with uio_oe hardwired 0: never driven onto the pad"),
    ("top.v", 98): ("wire _unused = &{ena, 1'b0};", "lint tie-off with no reader"),
}
LIT = re.compile(r"\b(\d+)'([bdh])([0-9a-fA-F_]+)\b")


def gen_file(rel: str) -> list[dict]:
    text = (ROOT / "src" / rel).read_text()
    m = mask(text)
    pd, bd, expr, nba = context(m)
    muts = []
    # positions inside a for (...) header: excluded (see module docstring)
    in_for = set()
    for mt in re.finditer(r"\bfor\s*\(", m):
        depth, j = 0, mt.end() - 1
        while j < len(m):
            depth += {"(": 1, ")": -1}.get(m[j], 0)
            if depth == 0:
                break
            j += 1
        in_for.update(range(mt.start(), j + 1))

    def add(group, start, end, new, desc):
        if start in in_for:
            return
        line = text.count("\n", 0, start) + 1
        muts.append(dict(file=rel, line=line, start=start, end=end, new=new, group=group,
                         desc=desc, id=f"{rel}:{line}:{start}:{group}:{new!r}"))

    for group, rx, rep, need_expr, in_br in OPS:
        for mt in re.finditer(rx, m):
            s = mt.start()
            if s in nba or (need_expr and not expr[s]) or (not in_br and bd[s] > 0):
                continue
            add(group, s, mt.end(), rep, f"{mt.group(0)} -> {rep or '(drop)'}")
    for mt in LIT.finditer(m):
        s = mt.start()
        if not expr[s] or bd[s] > 0:
            continue
        w, base, digits = int(mt.group(1)), mt.group(2), mt.group(3).replace("_", "")
        if not digits or w > 32:
            continue
        v = int(digits, {"b": 2, "d": 10, "h": 16}[base])
        vals = {v ^ 1} if w == 1 else {(v + 1) % (1 << w), (v - 1) % (1 << w)}
        for nv in sorted(vals - {v}):
            add("CONST", s, mt.end(), f"{w}'d{nv}", f"{mt.group(0)} -> {w}'d{nv}")
    for mt in re.finditer(r"\bif\s*\(", m):
        o = mt.end() - 1
        depth, j = 0, o
        while j < len(m):
            depth += {"(": 1, ")": -1}.get(m[j], 0)
            if depth == 0:
                break
            j += 1
        cond = text[o + 1:j]
        add("COND", o, j + 1, f"(!({cond}))", f"if ({' '.join(cond.split())[:40]}) negated")
    # DEL: a whole assignment statement alone on its line
    for mt in re.finditer(r"(?m)^([ \t]*)([A-Za-z_][\w\[\]:.{}, `']*?)\s*(<=|=)\s*[^;]*;[ \t]*$", m):
        lhs = mt.group(2).strip()
        if DECL.match(mt.group(0)) or SKIP_LINE.match(mt.group(0)) or lhs.split()[0] in (
                "assign", "if", "else", "case", "default", "for"):
            continue
        s = mt.start() + len(mt.group(1))
        add("DEL", s, mt.end(), ";", f"delete `{' '.join(text[s:mt.end()].split())[:50]}`")
    return muts


def generate(cap: int, seed: int) -> tuple[list[dict], int]:
    allm = [x for f in SRC_FILES for x in gen_file(f)]
    total = len(allm)
    if cap and total > cap:
        allm = random.Random(seed).sample(allm, cap)
    allm.sort(key=lambda x: (x["file"], x["start"], x["group"], x["new"]))
    return allm, total


def apply(text: str, mu: dict) -> str:
    return text[:mu["start"]] + mu["new"] + text[mu["end"]:]


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def sh(cmd: str, cwd: Path, timeout: int, env=None) -> tuple[int, str]:
    act = f"source {VENV_ACT} && " if VENV_ACT.exists() else ""
    pr = subprocess.Popen(["bash", "-c", act + cmd], cwd=cwd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = pr.communicate(timeout=timeout)
        return pr.returncode, out
    except subprocess.TimeoutExpired:
        os.killpg(pr.pid, signal.SIGKILL)
        pr.communicate()
        return 124, f"TIMEOUT after {timeout}s"


def failed(rc: int, test_dir: Path) -> bool:
    xml = test_dir / "results.xml"
    return rc != 0 or (xml.exists() and "<failure" in xml.read_text())


def run_one(mu: dict, seeds: int) -> dict:
    t0 = time.time()
    d = Path(tempfile.mkdtemp(prefix="blind_mut_"))
    row = {k: mu[k] for k in ("id", "file", "line", "group", "desc")}
    try:
        copy_tree(d)
        f = d / "src" / mu["file"]
        f.write_text(apply(f.read_text(), mu))
        t = d / "test"
        rc, out = sh("make sim_build/rtl/sim.vvp", t, SIM_TIMEOUT)
        if rc != 0:
            row.update(status="stillborn", detail=out.strip().splitlines()[-1:] or "")
            return row
        sim = "survived"
        for name, cmd in (("directed", "rm -f results.xml && make"),
                          ("random", f"rm -f results.xml && SEEDS={seeds} make random"),
                          ("board", "rm -rf sim_build/board results.xml && make board")):
            rc, out = sh(cmd, t, SIM_TIMEOUT)
            if failed(rc, t):
                sim = f"killed:{name}" + (":timeout" if rc == 124 else "")
                break
        env = dict(os.environ, FORMAL_SCRATCH=str(d / "fscratch"))
        rc, out = sh("python3 formal/scripts/run_formal.py --induct 20 --all", d, FORMAL_TIMEOUT, env)
        summ = next((l for l in reversed(out.splitlines()) if re.search(r"PROVEN \d+/\d+", l)), "")
        if rc == 124:
            formal = "killed:timeout"
        elif not summ:
            formal = "error"
        elif rc != 0:
            proven = set(re.findall(r"'([\w.]+_props\.v)'", summ))
            allf = {p.name for p in (d / "formal").glob("agent_*_props.v")}
            formal = "killed:" + ",".join(sorted(x.replace("agent_", "").replace("_props.v", "")
                                                 for x in allf - proven))
        else:
            formal = "survived"
        row.update(status="ok", sim=sim, formal=formal)
        return row
    except Exception as e:  # noqa: BLE001 -- a harness crash is recorded, not fatal
        row.update(status="error", detail=repr(e))
        return row
    finally:
        row["secs"] = round(time.time() - t0)
        shutil.rmtree(d, ignore_errors=True)


def load(out: Path) -> dict:
    if not out.exists():
        return {}
    rows = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    return {r["id"]: r for r in rows}


def cmd_list(a) -> int:
    muts, total = generate(a.cap, a.seed)
    for mu in muts:
        print(f"{mu['file']}:{mu['line']:<4} {mu['group']:5s} {mu['desc']}")
    by = {}
    for mu in muts:
        by[mu["group"]] = by.get(mu["group"], 0) + 1
    print(f"[blind_mutate] {len(muts)} of {total} generated (seed {a.seed}); by operator: {by}")
    return 0


def cmd_run(a) -> int:
    muts, total = generate(a.cap, a.seed)
    if a.ids:
        want = set(a.ids)
        muts = [mu for mu in muts if mu["id"] in want]
    done = load(a.out)
    todo = [mu for mu in muts if mu["id"] not in done]
    print(f"[blind_mutate] {len(muts)} selected of {total}; {len(done)} already done; running {len(todo)} "
          f"with -j {a.jobs}", flush=True)
    with ThreadPoolExecutor(a.jobs) as ex, a.out.open("a") as fh:
        futs = {ex.submit(run_one, mu, a.seeds): mu for mu in todo}
        for i, fu in enumerate(as_completed(futs), 1):
            row = fu.result()
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            print(f"[{i}/{len(todo)}] {row['status']:9s} sim={row.get('sim', '-'):18s} "
                  f"formal={row.get('formal', '-')[:30]:30s} {row['file']}:{row['line']} {row['desc'][:50]}",
                  flush=True)
    return cmd_report(a)


def equivalent(r: dict) -> str | None:
    e = EQUIVALENT.get((r["file"], r["line"]))
    if not e:
        return None
    line = (ROOT / "src" / r["file"]).read_text().splitlines()[r["line"] - 1].strip()
    return e[1] if line == e[0] else None


def tieoff_kill(mu: dict) -> bool:
    """scripts/check_tieoffs.py on the mutated mem.v (the only file it reads)."""
    if mu["file"] != "mem/mem.v":
        return False
    import check_tieoffs
    t = Path(tempfile.mkdtemp(prefix="tieoff_")) / "mem.v"
    t.write_text(apply((ROOT / "src" / mu["file"]).read_text(), mu))
    saved, check_tieoffs.MEM = check_tieoffs.MEM, t
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return check_tieoffs.main() != 0
    finally:
        check_tieoffs.MEM = saved
        shutil.rmtree(t.parent, ignore_errors=True)


def cmd_report(a) -> int:
    gen = {mu["id"]: mu for mu in generate(a.cap, a.seed)[0]}
    current = set(gen)
    allrows = list(load(a.out).values())
    rows = [r for r in allrows if r["id"] in current]
    if len(rows) != len(allrows):
        print(f"[blind_mutate] {len(allrows) - len(rows)} result(s) ignored: no longer generated "
              f"(excluded site, or a different --cap/--seed)")
    ok = [r for r in rows if r["status"] == "ok"]
    still = sum(r["status"] == "stillborn" for r in rows)
    err = [r for r in rows if r["status"] == "error" or r.get("formal") == "error"]
    sim_k = sum(r["sim"].startswith("killed") for r in ok)
    for_k = sum(r["formal"].startswith("killed") for r in ok)
    for r in ok:
        r["tieoff"] = tieoff_kill(gen[r["id"]])
    kill = lambda r: r["sim"].startswith("killed") or r["formal"].startswith("killed") or r["tieoff"]  # noqa: E731
    any_k = sum(kill(r) for r in ok)
    tie_k = sum(r["tieoff"] for r in ok)
    n = len(ok) or 1
    print(f"[blind_mutate] {len(rows)} run: {len(ok)} live, {still} stillborn, {len(err)} error")
    print(f"  simulation kills  {sim_k}/{len(ok)} ({100 * sim_k / n:.1f}%)")
    print(f"  formal-only kills {for_k}/{len(ok)} ({100 * for_k / n:.1f}%)")
    print(f"  tie-off check     {tie_k}/{len(ok)}")
    print(f"  any gate          {any_k}/{len(ok)} ({100 * any_k / n:.1f}%)")
    files = sorted({r["file"] for r in ok})
    for f in files:
        fr = [r for r in ok if r["file"] == f]
        print(f"    {f:20s} live {len(fr):3d}  sim {sum(r['sim'].startswith('killed') for r in fr):3d}"
              f"  formal {sum(r['formal'].startswith('killed') for r in fr):3d}"
              f"  any {sum(kill(r) for r in fr):3d}")
    surv = [r for r in ok if not kill(r)]
    eq = [r for r in surv if equivalent(r)]
    real = [r for r in surv if not equivalent(r)]
    m = len(ok) - len(eq) or 1
    print(f"  survivors of every gate: {len(surv)} = {len(eq)} equivalent + {len(real)} unexplained")
    print(f"  score excluding equivalent: {any_k}/{len(ok) - len(eq)} ({100 * any_k / m:.1f}%)")
    print(f"  unexplained survivors ({len(real)}):")
    for r in sorted(real, key=lambda r: (r["file"], r["line"])):
        print(f"    {r['file']}:{r['line']:<4} {r['group']:5s} {r['desc']}")
    print(f"  equivalent ({len(eq)}):")
    for r in sorted(eq, key=lambda r: (r["file"], r["line"])):
        print(f"    {r['file']}:{r['line']:<4} {r['group']:5s} {r['desc']:28s} {equivalent(r)[:70]}")
    sim_only = [r for r in ok if r["sim"] == "survived" and r["formal"].startswith("killed")]
    print(f"  killed by formal only, not simulation ({len(sim_only)}):")
    for r in sorted(sim_only, key=lambda r: (r["file"], r["line"])):
        print(f"    {r['file']}:{r['line']:<4} {r['group']:5s} {r['desc']}  [{r['formal']}]")
    hung = [r for r in ok if "timeout" in r["sim"] or "timeout" in r["formal"]]
    print(f"  suite timeouts (hangs) ({len(hung)}):")
    for r in hung:
        print(f"    {r['file']}:{r['line']:<4} {r['group']:5s} {r['desc']}  [sim={r['sim']} formal={r['formal']}]")
    for r in err:
        print(f"  ERROR {r['id']}: {r.get('detail', r.get('formal'))}")
    return 1 if real else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "run", "report"):
        p = sub.add_parser(name)
        p.add_argument("--cap", type=int, default=0, help="random sample of N mutants (default 0: all)")
        p.add_argument("--seed", type=int, default=1)
        p.add_argument("--out", type=Path, default=DEFAULT_OUT)
        if name == "run":
            p.add_argument("-j", "--jobs", type=int, default=5)
            p.add_argument("--seeds", type=int, default=50, help="random differential seeds")
            p.add_argument("--ids", nargs="*")
    a = ap.parse_args()
    return {"list": cmd_list, "run": cmd_run, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
