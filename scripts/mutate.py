#!/usr/bin/env python3
"""
mutate.py -- mutation-testing gate for the protocol-emulator RTL. Port of
the sibling 5-Stage-Pipelined-RISC-V-Processor project's
ooo/scripts/mutate.py (same structure, same discipline).

Answers what "regression passes" alone can't: would the suite actually
catch a real bug, or does it just not currently see one? Each mutant is a
targeted break of a guard/behavior this project has a documented reason
to care about (a past real bug, a mutation already hand-run during
development, or a hazard docs/isa.md / docs/architecture.md calls out) --
not blind operator mutation.

Must run in the parent orchestrating session, never delegated to a
subagent: a subagent must never grade its own output.

Per mutant: save original text, apply edits (all-or-nothing; a missing
old_string = "error", never silently skipped), rebuild, run directed
suite + <seeds> random differential seeds, classify killed/survived,
then UNCONDITIONALLY restore in try/finally -- a crash mid-mutant must
never leave src/ mutated. One final rebuild on the restored tree.

Usage:
    mutate.py [--seeds N] [--only ID[,ID...]]

Exit 0 iff score >= SCORE_THRESHOLD and nothing errored.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR   = REPO_ROOT / "src"
TEST_DIR  = REPO_ROOT / "test"
VENV_BIN  = REPO_ROOT / ".venv" / "bin"

DEFAULT_SEEDS   = 50
SCORE_THRESHOLD = 0.80
REPORT_OUT      = REPO_ROOT / "mutation_results.json"

MUTANTS = [
    dict(
        id=1, file="io/cycle_counter.v",
        desc="revert DELAY/WAIT counter pre-decrement on load",
        hazard="DELAY 3+N timing off-by-one",
        bug_ref="commit 1121ccc (real bug, found by test_delay_timing)",
        edits=[("count <= (shifted == 24'd0) ? 24'd0 : (shifted - 24'd1);",
                "count <= shifted;")],
    ),
    dict(
        id=2, file="io/pin_ctrl.v",
        desc="drop one synchronizer flop on pin_read",
        hazard="uio input sync depth (architecture.md: every input 2-flop synced)",
        bug_ref="hand mutation during WAIT/IN/INB random-stimulus work",
        edits=[("assign pin_read   = uio_in_ff2[pin_index];",
                "assign pin_read   = uio_in_ff1[pin_index];")],
    ),
    dict(
        id=3, file="cpu/core.v",
        desc="INB bit7 captures inverted pin value",
        hazard="INB receive-path bit capture",
        bug_ref="hand mutation during WAIT/IN/INB review",
        edits=[("? {pin_read, ra_data[6:0]}", "? {!pin_read, ra_data[6:0]}")],
    ),
    dict(
        id=4, file="cpu/core.v",
        desc="nested CALL no longer sets call_ret_misuse_flag",
        hazard="CALL overwrite hazard diagnosability (isa.md Branch format)",
        bug_ref="test_call_ret_nested_misuse_flag",
        edits=[("if (return_valid) call_ret_misuse_flag <= 1'b1;",
                "if (1'b0) call_ret_misuse_flag <= 1'b1;")],
    ),
    dict(
        id=5, file="cpu/core.v",
        desc="orphan RET no longer sets call_ret_misuse_flag",
        hazard="orphan-RET diagnosability (isa.md Branch format)",
        bug_ref="test_ret_without_call_misuse_flag",
        edits=[("if (!return_valid) call_ret_misuse_flag <= 1'b1;",
                "if (1'b0) call_ret_misuse_flag <= 1'b1;")],
    ),
    dict(
        id=6, file="cpu/core.v",
        desc="WAIT tie: timeout wins over condition-met",
        hazard="WAIT boundary rule (isa.md: condition-met wins on expiry cycle)",
        bug_ref="docs/isa.md WAIT row",
        edits=[("`OP_WAIT:    flag <= wait_condition_met ? 1'b0 : 1'b1;",
                "`OP_WAIT:    flag <= (wait_has_timeout && cnt_expired) ? 1'b1 : (wait_condition_met ? 1'b0 : 1'b1);")],
    ),
    dict(
        id=7, file="cpu/core.v",
        desc="LOADX writeback dropped (only LOAD arms pending writeback)",
        hazard="LOAD/LOADX trailing writeback (architecture.md Pipeline)",
        bug_ref="docs/architecture.md FETCH_LO/FETCH_HI/EXECUTE",
        edits=[("if (opcode == `OP_LOAD || opcode == `OP_LOADX) begin",
                "if (opcode == `OP_LOAD) begin")],
    ),
    dict(
        id=8, file="cpu/core.v",
        desc="opcode 19 treated as recognized (no illegal flag)",
        hazard="illegal-opcode detection boundary (isa.md Undefined opcode)",
        bug_ref="test_illegal_opcode_sets_flag",
        edits=[("wire opcode_recognized = (opcode <= 5'd18);",
                "wire opcode_recognized = (opcode <= 5'd19);")],
    ),
    dict(
        id=9, file="io/pin_ctrl.v",
        desc="open-drain oe polarity inverted",
        hazard="open-drain drive semantics (isa.md SET row)",
        bug_ref="test_open_drain_release / test_open_drain_drive_low",
        edits=[("(is_pp || (is_od && !drv[gi]))", "(is_pp || (is_od && drv[gi]))")],
    ),
    dict(
        id=10, file="cpu/core.v",
        desc="boot byte-address counter wraps at 1023 instead of saturating",
        hazard="boot-stream overflow corrupting address 0 (architecture.md LOAD)",
        bug_ref="docs/architecture.md 'saturates at 1023, does not wrap'",
        edits=[("wire [9:0] boot_addr_next = (boot_addr == 10'd1023) ? boot_addr : boot_addr + 10'd1;",
                "wire [9:0] boot_addr_next = boot_addr + 10'd1;")],
    ),
    dict(
        id=11, file="cpu/core.v",
        desc="LOOP writes the shared flag",
        hazard="flag-writer set stays CMP/TEST-bit/WAIT only (isa.md LOOP row)",
        bug_ref="test_loop_does_not_touch_flag",
        edits=[("`OP_TESTBIT: flag <= ra_data[w_bitidx];",
                "`OP_TESTBIT: flag <= ra_data[w_bitidx];\n                `OP_LOOP:    flag <= loop_taken;")],
    ),
    dict(
        id=12, file="cpu/core.v",
        desc="SHIFT left rotates instead of zero-filling",
        hazard="SHIFT vacated bit always 0 (isa.md SHIFT row)",
        bug_ref="docs/isa.md SHIFT row",
        edits=[("{ra_data[6:0], 1'b0}", "{ra_data[6:0], ra_data[7]}")],
    ),
    dict(
        id=13, file="io/pin_ctrl.v",
        desc="HOST_ERROR driver enabled before START's falling edge",
        hazard="uio[6] driver contention with host (architecture.md LOAD)",
        bug_ref="docs/architecture.md 'seen START fall' gate",
        edits=[("assign uio_oe[gi]  = mode_load ? 1'b0 : seen_start_fall;",
                "assign uio_oe[gi]  = mode_load ? 1'b0 : 1'b1;")],
    ),
    dict(
        id=14, file="io/pin_ctrl.v",
        desc="seen_start_fall latched from mode_load-gated start_fall",
        hazard="HOST_ERROR permanently undrivable (START fall only seen after LOAD exit)",
        bug_ref="real bug, found by test_host_error_gated_until_start_fall",
        edits=[("else if (!mode_load && start_fall_raw) seen_start_fall <= 1'b1;",
                "else if (start_fall) seen_start_fall <= 1'b1;")],
    ),
]


def _bash(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f"source {VENV_BIN}/activate && {cmd}"],
                          cwd=TEST_DIR, capture_output=True, text=True)


def _failed(r: subprocess.CompletedProcess) -> bool:
    # Belt and braces: nonzero exit OR a <failure> in results.xml.
    xml = TEST_DIR / "results.xml"
    return r.returncode != 0 or (xml.exists() and "<failure" in xml.read_text())


def rebuild() -> bool:
    # Build-only target, so a mutant that makes a test fail (a real kill)
    # is never misreported as a build error.
    r = _bash("rm -rf sim_build results.xml && make sim_build/rtl/sim.vvp")
    if r.returncode != 0:
        print(f"ERROR: rebuild failed\n{r.stdout}\n{r.stderr}", file=sys.stderr)
        return False
    return True


def run_fast_sample(seeds: int) -> tuple[bool, str]:
    d = _bash("rm -f results.xml && make")
    directed_ok = not _failed(d)
    r = _bash(f"rm -f results.xml && SEEDS={seeds} make random")
    seeds_ok = not _failed(r)
    detail = f"directed={'PASS' if directed_ok else 'FAIL'} seeds={'PASS' if seeds_ok else 'FAIL'}"
    return directed_ok and seeds_ok, detail


def apply_edits(path: Path, edits: list[tuple[str, str]]) -> tuple[bool, str]:
    text = path.read_text()
    for old, new in edits:
        if old not in text:
            return False, f"old_string not found: {old[:80]!r}"
        text = text.replace(old, new)
    path.write_text(text)
    return True, ""


def _row(m: dict, status: str, detail: str) -> dict:
    return {**{k: m[k] for k in ("id", "file", "desc", "hazard", "bug_ref")},
            "status": status, "detail": detail}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=DEFAULT_SEEDS)
    ap.add_argument("--only", type=str, default=None, help="comma-separated mutant ids")
    args = ap.parse_args()
    only = {int(x) for x in args.only.split(",")} if args.only else None
    mutants = [m for m in MUTANTS if only is None or m["id"] in only]

    print(f"[mutate] baseline check (directed + SEEDS={args.seeds}) ...", flush=True)
    if not rebuild():
        return 1
    ok, detail = run_fast_sample(args.seeds)
    if not ok:
        print(f"ERROR: baseline fails ({detail}) -- aborting; every verdict would be "
              "meaningless against a failing baseline.", file=sys.stderr)
        return 1
    print(f"[mutate] baseline clean ({detail})\n", flush=True)

    results = []
    for m in mutants:
        path = SRC_DIR / m["file"]
        original = path.read_text()
        print(f"[mutate] #{m['id']:2d} {m['file']:20s} {m['desc']}", flush=True)
        try:
            ok, err = apply_edits(path, m["edits"])
            if not ok:
                print(f"           -> ERROR: {err}", flush=True)
                results.append(_row(m, "error", err))
                continue
            if not rebuild():
                results.append(_row(m, "error", "rebuild failed with mutant applied"))
                continue
            passed, detail = run_fast_sample(args.seeds)
            status = "survived" if passed else "killed"
            print(f"           -> {status.upper()} ({detail})", flush=True)
            results.append(_row(m, status, detail))
        finally:
            path.write_text(original)

    rebuild()

    killed   = sum(r["status"] == "killed" for r in results)
    survived = sum(r["status"] == "survived" for r in results)
    errored  = sum(r["status"] == "error" for r in results)
    scored   = killed + survived
    score    = killed / scored if scored else 0.0

    W = 64
    print(f"\n{'=' * W}\nMutation Testing Report -- {len(mutants)} mutants\n{'=' * W}")
    for r in results:
        print(f"  #{r['id']:2d} [{r['status'].upper():8s}] {r['file']:20s} {r['desc']}")
    print("=" * W)
    print(f"Killed: {killed}  Survived: {survived}  Errored: {errored}")
    print(f"Mutation score: {score:.0%} (threshold: {SCORE_THRESHOLD:.0%})")
    if survived:
        print("\nSURVIVED (real gaps -- suite would not catch this bug):")
        for r in results:
            if r["status"] == "survived":
                print(f"  #{r['id']} {r['file']}: {r['desc']} ({r['bug_ref']})")
    print("=" * W)

    report = {
        "mutants_total": len(mutants), "killed": killed, "survived": survived,
        "errored": errored, "score": score, "threshold": SCORE_THRESHOLD,
        "gate_pass": bool(score >= SCORE_THRESHOLD and errored == 0),
        "results": results,
    }
    REPORT_OUT.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["gate_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
