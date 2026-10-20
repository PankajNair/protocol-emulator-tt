#!/usr/bin/env python3
"""
prove_equiv.py -- prove that the blind-mutation survivors recorded as
equivalent (blind_mutate.EQUIVALENT) really are: no input sequence from
reset makes the mutant chip's pins differ from the original's
(docs/VAL.md section 20).

    prove_equiv.py [--k K] [--only FILE:LINE ...]
    prove_equiv.py --controls        # negative control (CONTROLS below)

Negative control: a prover that proves everything is worthless, so
--controls runs CONTROLS, mutants the campaign killed, each chosen to
exercise one part of the miter, and requires every one to be REFUTED: a
BMC from reset with only the pin and write asserts finds a real
counterexample. That search is exponential in depth (two chip copies
plus the SRAM array: depth 12 takes ~20 s, 14 ~2 min), so the controls
are mutants whose pin difference appears within --control-depth cycles.

Per mutant, a sequential miter of the whole chip:
  - gold = src/ as is, gate = src/ with the mutant applied. Each is
    flattened with yosys; the SRAM instance (u_mem) is cut out
    (`expose -evert`) and every flop is exposed as a port (`expose -dff`).
  - Both copies get the same inputs (ui_in, uio_in, ena, rst_n) and share
    one SRAM model (IHP's behavioral semantics as tied off in mem.v:
    enabled while rst_n, a read every cycle, write-through). The memory
    starts with arbitrary contents, the same for both; each copy has its
    own read register, equal at step 0. Only gold writes the array, which
    is exact because the miter also asserts that both copies issue the
    same writes.
  - Asserted every cycle (all must hold):
      pins:   uo_out equal, uio_oe equal, uio_out equal where uio_oe = 1
              (an output with its enable off never reaches the pad);
      writes: same write enable, and when writing, same address and data;
      state:  every exposed flop equal, except the per-mutant ALLOWED_DIFF
              set, plus per-mutant invariants about those flops.
    The state asserts are strengthening for induction; the pin and write
    asserts are the claim.
  - Proof: BMC from reset (the one assume: rst_n low at step 0) for K
    steps, then K-step induction. Both passing = equivalent for all time.

Results are recorded in formal/equiv_proofs.json together with a hash of
the RTL they were proven against; blind_mutate.py report calls a
survivor "proven equivalent" only while that hash still matches.

Exit 0 iff every selected mutant is PROVEN (with --controls: iff every
control is refuted).
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import blind_mutate  # noqa: E402

TOP = "tt_um_pankajnair_protocol_emulator"
PROOFS = ROOT / "formal" / "equiv_proofs.json"
RTL = ["cpu/isa_defs.v", "cpu/regfile.v", "io/cycle_counter.v", "cpu/core.v", "io/pin_ctrl.v", "top.v"]
PINS_OUT = ("uo_out", "uio_out", "uio_oe")
MEM_BB = """(* blackbox *)
module mem (input wire clk, input wire rst_n, input wire [9:0] addr, input wire we,
            input wire [7:0] wdata, output wire [7:0] rdata);
endmodule
"""
S_LOAD, S_FETCH_LO, S_EXECUTE = "2'd0", "2'd1", "2'd3"

# Flops a mutant may legitimately hold different from the original, and
# the invariant that keeps the difference unobservable. Each invariant is
# itself asserted (and so proven), never assumed. "dr" is the SRAM read
# register pair, asserted equal unless listed.
CONTROLS = [
    # (mutant id, what it exercises)
    ("cpu/core.v:349:13658:CONST:\"8'd1\"", "uo_out compare at reset (killed by formal only)"),
    ("io/pin_ctrl.v:204:8903:CONST:\"1'd0\"", "uio_oe compare (HOST_STATUS enable)"),
    ("cpu/core.v:420:16180:REL:'!='", "a program executing from the shared SRAM (OUT taken for every other opcode)"),
    ("cpu/core.v:299:11656:DEL:';'", "write compare (STORE data; killed by simulation only)"),
]

ALLOWED_DIFF = {
    ("cpu/core.v", 281): (["dr"],
        # the default address is issued only in EXECUTE for non-memory
        # opcodes; that byte is in dr on EXECUTE's later cycles (decode
        # uses ir then) and in FETCH_LO (used only by a pending LOAD/LOADX
        # writeback, whose address is not the default); a reset during
        # such a read leaves it in LOAD (the SRAM holds dr while rst_n is
        # low), where mem_rdata is not read and every cycle reloads dr
        f"((dr_g == dr_m) || (g_u_core_state == {S_EXECUTE} && !g_u_core_entering_execute) || "
        f"(g_u_core_state == {S_FETCH_LO} && !g_u_core_pending_lx_writeback) || "
        f"(g_u_core_state == {S_LOAD})) && "
        # design invariant the step above needs: the deferred writeback
        # flag is set by a LOAD/LOADX commit and cleared in the FETCH_LO
        # that follows, so it is never seen in any other state
        f"(!g_u_core_pending_lx_writeback || g_u_core_state == {S_FETCH_LO}) && "
        # and: HALT commits on EXECUTE's first cycle, and that same edge
        # clears entering_execute, so a halted core decodes ir (the HALT
        # word), never the live SRAM byte
        "(!g_u_core_halted || !g_u_core_entering_execute)"),
    ("cpu/core.v", 339): (["u_core.ir"],
        # ir is latched on EXECUTE's first cycle; until then nothing reads it
        f"(g_u_core_ir == m_u_core_ir) || (g_u_core_state != {S_EXECUTE}) || g_u_core_entering_execute"),
    ("cpu/core.v", 351): (["u_core.pending_lx_rd"],
        "(g_u_core_pending_lx_rd == m_u_core_pending_lx_rd) || !g_u_core_pending_lx_writeback"),
}


def rtl_sha() -> str:
    import hashlib
    h = hashlib.sha256()
    for f in RTL + ["mem/mem.v"]:
        h.update((ROOT / "src" / f).read_bytes())
    h.update(Path(__file__).read_bytes())  # the miter construction is part of the proof
    return h.hexdigest()[:16]


def record(results: dict[str, str], k: int) -> None:
    sha = rtl_sha()
    old = json.loads(PROOFS.read_text()) if PROOFS.exists() else {}
    keep = old.get("results", {}) if old.get("rtl_sha") == sha else {}
    keep.update(results)
    PROOFS.write_text(json.dumps({"rtl_sha": sha, "k": k, "results": dict(sorted(keep.items()))}, indent=1) + "\n")


def proven_ids() -> set[str]:
    """Mutant ids proven equivalent against the current RTL."""
    if not PROOFS.exists():
        return set()
    d = json.loads(PROOFS.read_text())
    if d.get("rtl_sha") != rtl_sha():
        return set()
    return {i for i, st in d["results"].items() if st == "PROVEN"}


def sid(name: str) -> str:
    return re.sub(r"\W", "_", name)


def build_chip(src: Path, module: str, out: Path) -> dict:
    (out.parent / "mem_bb.v").write_text(MEM_BB)
    files = " ".join(str(src / f) for f in RTL) + f" {out.parent / 'mem_bb.v'}"
    script = (f"read_verilog -sv {files}; hierarchy -top {TOP}; proc; flatten; "
              f"memory -nomap; memory_map; opt_clean; "
              f"expose -evert {TOP}/u_mem; expose -dff {TOP}; opt_clean; "
              f"rename {TOP} {module}; hierarchy -top {module}; "
              f"write_rtlil {out}; write_json {out.with_suffix('.json')}")
    r = subprocess.run(["yosys", "-q", "-p", script], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"yosys build of {module} failed:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
    ports = json.loads(out.with_suffix(".json").read_text())["modules"][module]["ports"]
    return {n: (p["direction"], len(p["bits"])) for n, p in ports.items()}


def miter(gp: dict, mp: dict, allowed: list[str], invariant: str | None) -> str:
    ins = [n for n, (d, _) in gp.items() if d == "input" and not n.startswith("u_mem.")]
    lines = ["module miter(" + ", ".join(f"input wire [{gp[n][1] - 1}:0] {n}" for n in ins) + ");"]
    sigs = [n for n, (d, _) in gp.items() if d == "output" or n.startswith("u_mem.")]
    for pre, ports in (("g", gp), ("m", mp)):
        for n in sigs:
            if n in ports:
                lines.append(f"  wire [{ports[n][1] - 1}:0] {pre}_{sid(n)};")
    lines.append("  reg [7:0] mem_arr [0:1023];")
    lines.append("  reg [7:0] dr_g, dr_m;")
    lines.append("  assign g_u_mem_rdata = dr_g;")
    lines.append("  assign m_u_mem_rdata = dr_m;")
    for pre, mod, ports in (("g", "chip_gold", gp), ("m", "chip_gate", mp)):
        conns = [f".{n}({n})" for n in ins]
        conns += [f".\\{n} ({pre}_{sid(n)})" for n in sigs if n in ports]
        lines.append(f"  {mod} {pre}_chip (" + ", ".join(conns) + ");")
    lines += [
        "  always @(posedge clk) begin",
        "    if (g_u_mem_rst_n && g_u_mem_we) begin",
        "      mem_arr[g_u_mem_addr] <= g_u_mem_wdata;",
        "      dr_g <= g_u_mem_wdata;",
        "    end else if (g_u_mem_rst_n) dr_g <= mem_arr[g_u_mem_addr];",
        "    if (m_u_mem_rst_n && m_u_mem_we) dr_m <= m_u_mem_wdata;",
        "    else if (m_u_mem_rst_n) dr_m <= mem_arr[m_u_mem_addr];",
        "  end",
        "  initial assume (!rst_n);",
        "  initial assume (dr_g == dr_m);",
        "  always @(*) begin",
        "    assert (g_uo_out == m_uo_out);",
        "    assert (g_uio_oe == m_uio_oe);",
        "    assert ((g_uio_out & g_uio_oe) == (m_uio_out & m_uio_oe));",
        "    assert (g_u_mem_rst_n == m_u_mem_rst_n);",
        "    assert (g_u_mem_we == m_u_mem_we);",
        "    if (g_u_mem_we) assert (g_u_mem_addr == m_u_mem_addr && g_u_mem_wdata == m_u_mem_wdata);",
        "`ifdef STRENGTHEN",
    ]
    for n, (d, _) in gp.items():
        if d == "output" and n not in PINS_OUT and not n.startswith("u_mem.") and n not in allowed:
            if n not in mp:
                raise RuntimeError(f"flop {n} missing in the mutant and not in ALLOWED_DIFF")
            lines.append(f"    assert (g_{sid(n)} == m_{sid(n)});")
    if "dr" not in allowed:
        lines.append("    assert (dr_g == dr_m);")
    if invariant:
        lines.append(f"    assert ({invariant});")
    lines += ["`endif", "  end", "endmodule", ""]
    return "\n".join(lines)


def smtbmc(smt2: Path, k: int, induction: bool) -> tuple[bool, str]:
    cmd = ["yosys-smtbmc", "-s", "z3", "-t", str(k)] + (["-i"] if induction else ["--presat"]) + [str(smt2)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    out = r.stdout + r.stderr
    return "Status: PASSED" in out, out


def prove(mu: dict, gold_il: Path, gp: dict, k: int, work: Path, refute_depth: int = 0) -> tuple[str, str]:
    d = work / sid(mu["id"])
    d.mkdir()
    src = d / "src"
    shutil.copytree(ROOT / "src", src)
    f = src / mu["file"]
    f.write_text(blind_mutate.apply(f.read_text(), mu))
    mp = build_chip(src, "chip_gate", d / "gate.il")
    allowed, inv = ALLOWED_DIFF.get((mu["file"], mu["line"]), ([], None))
    (d / "miter.v").write_text(miter(gp, mp, allowed, inv))
    smt2 = {}
    for name, define in (("claims", ""), ("full", "-DSTRENGTHEN ")):
        smt2[name] = d / f"miter_{name}.smt2"
        script = (f"read_rtlil {gold_il}; read_rtlil {d / 'gate.il'}; read_verilog -sv {define}{d / 'miter.v'}; "
                  f"hierarchy -top miter; prep -flatten -top miter; async2sync; chformal -lower; dffunmap; "
                  f"write_smt2 -wires {smt2[name]}")
        r = subprocess.run(["yosys", "-q", "-p", script], capture_output=True, text=True)
        if r.returncode != 0:
            return "ERROR", (r.stdout + r.stderr)[-1500:]
    if refute_depth:
        # pins/writes only, from reset: a failure is a real, pin-visible counterexample
        ok, out = smtbmc(smt2["claims"], refute_depth, False)
        if not ok:
            return "REFUTED", ""
        return "NOT REFUTED", f"no pin/write difference within {refute_depth} cycles"
    # all asserts (claims + strengthening), base case then induction
    ok, out = smtbmc(smt2["full"], k, False)
    if not ok:
        return "INCONCLUSIVE", "strengthening assert fails from reset (an ALLOWED_DIFF entry is missing):\n" + \
            "\n".join(l for l in out.splitlines() if "Assert failed" in l)[:600]
    ok, out = smtbmc(smt2["full"], k, True)
    if not ok:
        return "INCONCLUSIVE", "induction step fails (strengthening too weak):\n" + \
            "\n".join(l for l in out.splitlines() if "Assert failed" in l)[:600]
    return "PROVEN", ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--only", nargs="*", help="FILE:LINE sites")
    ap.add_argument("--keep", action="store_true", help="keep the work directory")
    ap.add_argument("--controls", action="store_true", help="negative control: CONTROLS must be refuted")
    ap.add_argument("--control-depth", type=int, default=12)
    a = ap.parse_args()
    gen = blind_mutate.generate(0, 1)[0]
    if a.controls:
        by_id = {mu["id"]: mu for mu in gen}
        missing = [c for c, _ in CONTROLS if c not in by_id]
        if missing:
            sys.exit(f"[prove_equiv] control mutant(s) no longer generated: {missing}")
        muts = [by_id[c] for c, _ in CONTROLS]
    else:
        # the campaign's survivors of every gate that EQUIVALENT explains
        rows = blind_mutate.load(blind_mutate.DEFAULT_OUT)
        sites = set(blind_mutate.EQUIVALENT)
        if a.only:
            sites &= {(s.rsplit(":", 1)[0], int(s.rsplit(":", 1)[1])) for s in a.only}
        muts = [mu for mu in gen if (mu["file"], mu["line"]) in sites and mu["id"] in rows
                and rows[mu["id"]].get("status") == "ok" and rows[mu["id"]]["sim"] == "survived"
                and rows[mu["id"]]["formal"] == "survived" and blind_mutate.equivalent(rows[mu["id"]])]
    work = Path(tempfile.mkdtemp(prefix="prove_equiv_"))
    try:
        gp = build_chip(ROOT / "src", "chip_gold", work / "gold.il")
        results = []
        recorded = {}
        for mu in muts:
            status, detail = prove(mu, work / "gold.il", gp, a.k, work, a.control_depth if a.controls else 0)
            results.append(status)
            if not a.controls:
                recorded[mu["id"]] = status
                record(recorded, a.k)
            print(f"{status:15s} {mu['file']}:{mu['line']:<4} {mu['group']:5s} {mu['desc']}", flush=True)
            if detail:
                print("    " + detail.replace("\n", "\n    "))
        n = sum(s == "PROVEN" for s in results)
        if a.controls:
            ref = sum(s == "REFUTED" for s in results)
            print(f"[prove_equiv] controls: REFUTED {ref}/{len(results)} killed mutants (k={a.k})")
            return 0 if ref == len(results) else 1
        print(f"[prove_equiv] PROVEN {n}/{len(results)} equivalent (k={a.k})")
        return 0 if n == len(results) else 1
    finally:
        if a.keep:
            print(f"[prove_equiv] work dir: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
