#!/usr/bin/env python3
"""
rtl_coverage.py -- structural (code) coverage of src/ under Verilator
(docs/VAL.md section 22).

The functional bins (test/seq_coverage.py) measure what the stimulus
*meant* to exercise; this measures what the RTL actually did: every
line/branch block executed and every bit of every signal toggled both
ways (0->1 and 1->0), merged over the whole suite. A hole is a line no
test reached or a bit no test moved -- a place where a bug could hide
from every simulation gate.

    rtl_coverage.py run    [--suites a,b,..] [--seeds N]   collect .dat per suite
    rtl_coverage.py report [--all]                         merged numbers + holes
    rtl_coverage.py gate                                   exit 1 on any unwaived hole
    rtl_coverage.py prove                                  the unreachability proof a waiver cites

Suites (each a Verilator build with --coverage-line --coverage-toggle):
  directed   test.py + test_uart.py
  spec       spec_tests/ (the spec-only conformance suite)
  random     test_random.py, default generator, --seeds seeds
  <profile>  test_random.py under each STIM_PROFILE, --seeds/2 seeds

The board suite (tb_board.v) is not one of them: it models floating
nets and pull-ups as X/Z, which 2-state Verilator cannot; its design-side
activity is a subset of what the single-chip suites drive. `--suites
board` still builds it, for when that changes.

Coverage points are merged across hierarchy: a point counts as hit if
any instance hit it. Only files
under src/ are scored; the SRAM behavioural model and the testbenches
are not the design. Holes that cannot be closed from the pins are
listed in WAIVED below, each with its reason; `gate` fails on any hole
not in WAIVED and on any waiver that no longer matches a hole (stale).
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST = ROOT / "test"
SRC = ROOT / "src"
OUT = ROOT / "build" / "rtl_cov"
PROFILES = ["protocol_pins", "wide_timing", "illegal_mix", "nested_flow"]
VFLAGS = "--coverage-line --coverage-toggle --coverage-underscore"

# (file, kind, line, signal/comment) -> reason. kind is "line" or
# "toggle"; for toggle the comment is "sig[bit]:0->1" as Verilator
# names it. Filled from the first full run's holes; every entry says
# why the hole cannot (or need not) be closed from the pins.
_UNREACH_CASE = "unreachable: state is 2 bits and all four encodings have their own case arm"
_START_FALL = ("unreachable: LOAD is entered only from reset and START's first rise leaves it, so "
               "START is never seen falling in LOAD (`rtl_coverage.py prove`, BMC + induction); "
               "no consumer either (core.v marks it unused)")
_CONST0 = "constant 0 by construction: uio[4] is HOST_GO, a fixed input (pin_ctrl.v G_HOST_GO)"
_OE5 = ("constant 1 by construction: uio[5] is HOST_STATUS, a fixed output (pin_ctrl.v "
        "G_HOST_STATUS); the 0->1 is only Verilator's 2-state power-up")
_UNUSED = "lint sink (`_unused` reduction of deliberately unconnected inputs), drives nothing"
_ENA = "the TinyTapeout harness holds ena high whenever the design is powered; unused by the design"
WAIVED: dict[tuple[str, str, int, str], str] = {
    ("cpu/core.v", "line", 304, "case"): _UNREACH_CASE,
    ("cpu/core.v", "line", 427, "case"): _UNREACH_CASE,
    **{(f, "toggle", n, f"start_fall:{e}"): _START_FALL
       for f, n in (("cpu/core.v", 74), ("io/pin_ctrl.v", 84), ("top.v", 38)) for e in ("0->1", "1->0")},
    **{(f, "toggle", n, f"{s}[4]:{e}"): _CONST0
       for f, n, s in (("io/pin_ctrl.v", 63, "uio_out"), ("io/pin_ctrl.v", 64, "uio_oe"),
                       ("top.v", 18, "uio_out"), ("top.v", 19, "uio_oe")) for e in ("0->1", "1->0")},
    ("io/pin_ctrl.v", "toggle", 64, "uio_oe[5]:1->0"): _OE5,
    ("top.v", "toggle", 19, "uio_oe[5]:1->0"): _OE5,
    **{(f, "toggle", n, f"_unused:{e}"): _UNUSED
       for f, n in (("cpu/core.v", 437), ("top.v", 98)) for e in ("0->1", "1->0")},
    ("top.v", "toggle", 20, "ena:1->0"): _ENA,
}

# `prove`: the start_fall waiver's proof. Asserts injected into a scratch
# copy of src/; the two strengthening invariants are asserted (proven),
# never assumed. The SRAM is a blackbox whose read data is a free input,
# which over-approximates the chip, so unreachability carries over.
TOP = "tt_um_pankajnair_protocol_emulator"
PROVE_INJECT = {
    "top.v": "  always @(posedge clk) if (rst_n) assert (!start_fall);\n  initial assume (!rst_n);\n",
    "io/pin_ctrl.v": "  always @(posedge clk) if (rst_n) assert (!(mode_load && start_prev));\n",
    "cpu/core.v": "  always @(posedge clk) if (rst_n) assert (!(halted && state == S_LOAD));\n",
}
# negative control: without the mode_load gate start_fall is reachable,
# so the same flow must find a counterexample
PROVE_CONTROL = ("assign start_fall   = mode_load && ", "assign start_fall   = ")
MEM_BB = """(* blackbox *)
module mem (input wire clk, input wire rst_n, input wire [9:0] addr, input wire we,
            input wire [7:0] wdata, output wire [7:0] rdata);
endmodule
"""
RTL = ["cpu/isa_defs.v", "cpu/core.v", "cpu/regfile.v", "io/cycle_counter.v", "io/pin_ctrl.v", "top.v"]


def suite_cmd(name: str, seeds: int) -> tuple[str, dict]:
    env = {}
    base = f"make -C {TEST} SIM=verilator EXTRA_ARGS='{VFLAGS}'"
    if name == "directed":
        return f"{base} SIM_BUILD=sim_build/vcov COCOTB_TEST_MODULES=test,test_uart", env
    if name == "spec":
        return f"{base} SIM_BUILD=sim_build/vcov spec", env
    if name == "board":
        return f"{base} SIM_BUILD=sim_build/vcov_board board", env
    env["COV_DIR"] = str(OUT / f"seqcov_{name}")
    if name == "random":
        env["SEEDS"] = str(seeds)
    elif name in PROFILES:
        env["SEEDS"] = str(max(1, seeds // 2))
        env["STIM_PROFILE"] = name
        if name == "wide_timing":
            env["MAX_CYCLES"] = "50000"
    else:
        sys.exit(f"unknown suite {name}")
    return f"{base} SIM_BUILD=sim_build/vcov random", env


def cmd_run(a) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    suites = a.suites.split(",") if a.suites else ["directed", "spec", "random", *PROFILES]
    if not a.suites:  # a full run replaces every suite's data
        for old in OUT.glob("*.dat"):
            old.unlink()
    rc_all = 0
    for s in suites:
        dat = TEST / "coverage.dat"
        dat.unlink(missing_ok=True)
        cmd, env = suite_cmd(s, a.seeds)
        print(f"[rtl_coverage] {s}: {cmd}", flush=True)
        r = subprocess.run(cmd, shell=True, cwd=ROOT, env={**os.environ, **env},
                           capture_output=True, text=True)
        tot = re.findall(r"TESTS=(\d+) PASS=(\d+) FAIL=(\d+)", r.stdout)
        failed = r.returncode != 0 or not tot or any(int(f) for _, _, f in tot)
        if failed or not dat.exists():
            print(r.stdout[-3000:], r.stderr[-2000:])
            print(f"[rtl_coverage] {s}: FAILED (rc={r.returncode}, totals={tot}, dat={dat.exists()})")
            rc_all = 1
            continue
        shutil.move(dat, OUT / f"{s}.dat")
        print(f"[rtl_coverage] {s}: {tot} -> {OUT / (s + '.dat')}", flush=True)
    return rc_all


def parse(path: Path):
    """Yield (fields, count) per point of a Verilator coverage.dat."""
    for ln in path.read_text().splitlines():
        if not ln.startswith("C '"):
            continue
        key, cnt = ln[3:].rsplit("' ", 1)
        fields = {}
        for part in key.split("\x01")[1:]:
            k, _, v = part.partition("\x02")
            fields[k] = v
        yield fields, int(cnt)


def merged() -> dict:
    """(file, kind, line, comment) -> total count over every suite."""
    pts: dict = defaultdict(int)
    dats = sorted(OUT.glob("*.dat"))
    if not dats:
        sys.exit(f"no coverage data in {OUT}; run `rtl_coverage.py run` first")
    for d in dats:
        for f, cnt in parse(d):
            p = Path(os.path.normpath(f["f"]))
            try:
                rel = str(p.relative_to(SRC))
            except ValueError:
                continue  # SRAM model, testbench
            kind = "toggle" if f.get("t") == "toggle" else "line"
            pts[(rel, kind, int(f["l"]), f.get("o", ""))] += cnt
    return pts


def src_line(rel: str, n: int) -> str:
    try:
        return (SRC / rel).read_text().splitlines()[n - 1].strip()
    except (OSError, IndexError):
        return ""


def summarize(pts):
    by = defaultdict(lambda: [0, 0, 0, 0])  # line hit/total, toggle hit/total
    for (rel, kind, _, _), c in pts.items():
        i = 0 if kind == "line" else 2
        by[rel][i + 1] += 1
        by[rel][i] += c > 0
    return by


def holes(pts):
    return sorted(k for k, c in pts.items() if c == 0)


def cmd_report(a) -> int:
    pts = merged()
    by = summarize(pts)
    print(f"{'file':28} {'line':>14} {'toggle':>14}")
    tl = [0, 0, 0, 0]
    for rel in sorted(by):
        v = by[rel]
        tl = [x + y for x, y in zip(tl, v)]
        print(f"{rel:28} {v[0]:5}/{v[1]:<5} {100*v[0]/max(v[1],1):3.0f}% {v[2]:5}/{v[3]:<5} {100*v[2]/max(v[3],1):3.0f}%")
    print(f"{'TOTAL':28} {tl[0]:5}/{tl[1]:<5} {100*tl[0]/max(tl[1],1):3.0f}% {tl[2]:5}/{tl[3]:<5} {100*tl[2]/max(tl[3],1):3.0f}%")
    hs = holes(pts)
    unwaived = [h for h in hs if h not in WAIVED]
    print(f"\nholes: {len(hs)} ({len(hs) - len(unwaived)} waived)")
    for h in (hs if a.all else unwaived):
        rel, kind, n, o = h
        tag = "WAIVED " if h in WAIVED else ""
        print(f"  {tag}{rel}:{n} {kind:6} {o:40} | {src_line(rel, n)[:70]}")
    return 0


def _prove_once(control: bool, depth: int = 30) -> tuple[bool, bool]:
    """(bmc_passed, induction_passed) on a scratch copy of src/."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for rel in RTL:
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            t = (SRC / rel).read_text()
            if control and rel == "io/pin_ctrl.v":
                assert t.count(PROVE_CONTROL[0]) == 1, "control edit no longer applies"
                t = t.replace(*PROVE_CONTROL)
            if rel in PROVE_INJECT:
                i = t.rindex("endmodule")
                t = t[:i] + PROVE_INJECT[rel] + t[i:]
            (d / rel).write_text(t)
        (d / "mem_bb.v").write_text(MEM_BB)
        files = " ".join(str(d / r) for r in RTL) + f" {d / 'mem_bb.v'}"
        ys = (f"read_verilog -formal {files}; hierarchy -top {TOP}; proc; flatten; "
              f"expose -evert {TOP}/u_mem; prep -top {TOP}; async2sync; dffunmap; "
              f"write_smt2 -wires {d / 'chip.smt2'}")
        r = subprocess.run(["yosys", "-q", "-p", ys], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"yosys failed:\n{r.stderr[-2000:]}")
        res = []
        for extra in ([], ["-i"]):
            r = subprocess.run(["yosys-smtbmc", "-s", "z3", *extra, "-t", str(depth), str(d / "chip.smt2")],
                               capture_output=True, text=True)
            res.append("Status: PASSED" in r.stdout)
        return res[0], res[1]


def cmd_prove(a) -> int:
    bmc, ind = _prove_once(control=False)
    cbmc, _ = _prove_once(control=True)
    ok = bmc and ind and not cbmc
    print(f"[rtl_coverage] start_fall unreachable: BMC {'PASS' if bmc else 'FAIL'}, "
          f"induction {'PASS' if ind else 'FAIL'}; control (ungated) "
          f"{'REFUTED' if not cbmc else 'NOT REFUTED'} -> {'PROVEN' if ok else 'NOT PROVEN'}")
    return 0 if ok else 1


def cmd_gate(a) -> int:
    pts = merged()
    hs = set(holes(pts))
    unwaived = sorted(hs - WAIVED.keys())
    stale = sorted(k for k in WAIVED if k not in hs)
    by = summarize(pts)
    lh, lt = sum(v[0] for v in by.values()), sum(v[1] for v in by.values())
    th, tt = sum(v[2] for v in by.values()), sum(v[3] for v in by.values())
    for h in unwaived:
        print(f"  HOLE   {h[0]}:{h[2]} {h[1]} {h[3]}")
    for s in stale:
        print(f"  STALE  waiver {s} now covered (or gone): remove it")
    proven = cmd_prove(a) == 0
    ok = not unwaived and not stale and proven
    print(f"[rtl_coverage] line {lh}/{lt}  toggle {th}/{tt}  waived {len(hs & WAIVED.keys())}  "
          f"unwaived {len(unwaived)}  stale {len(stale)} -> RTL COVERAGE GATE {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--suites", default="")
    r.add_argument("--seeds", type=int, default=100)
    p = sub.add_parser("report")
    p.add_argument("--all", action="store_true", help="also list waived holes")
    sub.add_parser("gate", help="unwaived holes, stale waivers, then `prove`")
    sub.add_parser("prove", help="prove the start_fall waiver (unreachability)")
    a = ap.parse_args()
    return {"run": cmd_run, "report": cmd_report, "gate": cmd_gate, "prove": cmd_prove}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
