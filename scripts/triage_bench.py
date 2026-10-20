#!/usr/bin/env python3
"""
triage_bench.py -- planted-bug benchmark for the triage-debug agent
(docs/VAL.md section 16). Each case plants one known bug -- in the DUT,
the reference model, or the harness -- in a standalone copy of the repo,
finds a seed it breaks, and later scores the agent's verdict against the
ground truth kept only in this file.

    triage_bench.py list
    triage_bench.py setup CASE DIR     build DIR (no .git, no bench file),
                                       plant the bug, find a failing seed,
                                       write DIR/TRIAGE_TASK.md
    triage_bench.py score CASE DIR     compare DIR/triage/reports/*/verdict.json

DIR must not name the case: the path is visible to the agent, and a
directory called model_cmp or two_bugs gives the answer away (rounds
1-3 did exactly that until round 3's two-bug agent remarked on its own
tree's name). Use opaque names (case_01 ...) and keep the map elsewhere.

The copy has no git history and no copy of this file, so the agent can't
diff its way to the answer: it has to reason from the spec, the code and
the evidence. Comments that would narrate the planted bug's fix are
scrubbed too (`scrub`). Pairs of cases with the same symptom and different owners
(sync_drop vs. harness_sync, delay_offbyone vs. model_delay) are the point:
they only separate on discriminating evidence.

The main tree's src/ is never touched; bugs are planted in the copy.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CASES = {
    "dut_inb_invert": dict(
        cls="DUT", file="src/cpu/core.v",
        edits=[("? {pin_read, ra_data[6:0]}", "? {!pin_read, ra_data[6:0]}")],
        truth="INB captures the inverted pin value into bit 7"),
    "dut_sync_drop": dict(
        cls="DUT", file="src/io/pin_ctrl.v",
        edits=[("assign pin_read   = uio_in_ff2[pin_index];", "assign pin_read   = uio_in_ff1[pin_index];")],
        truth="pin_read taps the first synchronizer flop: one cycle too early versus the 2-flop spec"),
    "dut_delay_offbyone": dict(
        cls="DUT", file="src/io/cycle_counter.v",
        edits=[("count <= (shifted == 24'd0) ? 24'd0 : (shifted - 24'd1);", "count <= shifted;")],
        # the header paragraph narrates this exact fix -- a latent bug
        # wouldn't come with its own write-up
        scrub=[(r" \* On `load`, `count` gets `shifted - 1`.*?(?= \*/)", "")],
        truth="DELAY/WAIT counter not pre-decremented: one extra cycle"),
    "model_shift_rotate": dict(
        cls="REFERENCE_MODEL", file="test/golden_model.py",
        edits=[("            s.regs[f[\"rd\"]] = (v >> 1) & 0xFF\n",
                "            s.regs[f[\"rd\"]] = ((v >> 1) | (v << 7)) & 0xFF\n")],
        truth="model's SHIFT right rotates; isa.md says the vacated bit is 0"),
    "model_cmp": dict(
        cls="REFERENCE_MODEL", file="test/golden_model.py",
        edits=[("        s.flag = s.regs[f[\"rd\"]] == s.regs[f[\"rs\"]]\n",
                "        s.flag = s.regs[f[\"rd\"]] >= s.regs[f[\"rs\"]]\n")],
        truth="model's CMP sets the flag on >= instead of =="),
    "model_delay": dict(
        cls="REFERENCE_MODEL", file="test/golden_model.py",
        edits=[("        cycles = 3 + n\n", "        cycles = 2 + n\n")],
        truth="model's DELAY costs 2+N; spec says 3+N (same symptom as dut_delay_offbyone)"),
    "harness_sync": dict(
        cls="HARNESS", file="test/io_stimulus.py",
        edits=[("SYNC_DELAY = 3", "SYNC_DELAY = 2")],
        # the header records the measured lag as a literal 3
        scrub=[(r"raw_\*\(cycle - 3\)", "raw_*(cycle - SYNC_DELAY)"),
               (r"the same 3-cycle lag", "the same lag"),
               (r"one cycle more than the textbook 2-flop count\. ", ""),
               (r"  # empirically measured, see module header -- NOT the textbook 2-flop count", "  # see module header")],
        truth="stimulus sync delay one cycle short of the measured RTL path; RTL matches spec"),

    # ---- round 3: harder (docs/VAL.md section 16) -----------------------
    # timing-only symptom ("cycles used"), same symptom, opposite owners
    "r3_dut_outb_slow": dict(
        cls="DUT", file="src/cpu/core.v",
        edits=[("  wire exec_done = !delay_pending && !wait_pending;",
                "  wire exec_done = !delay_pending && !wait_pending &&\n"
                "                   !(opcode == `OP_OUTB && entering_execute);")],
        truth="OUTB holds EXECUTE an extra cycle (4 cycles); spec: every non-DELAY/WAIT instruction is 3"),
    "r3_model_outb_slow": dict(
        cls="REFERENCE_MODEL", file="test/golden_model.py",
        edits=[("        s.pin_drv[pin] = bit\n\n    elif op == asm.OP_INB:",
                "        s.pin_drv[pin] = bit\n        cycles = 4\n\n    elif op == asm.OP_INB:")],
        truth="model charges OUTB 4 cycles; spec says 3 (same symptom as r3_dut_outb_slow)"),
    # rare trigger: only seeds where a WAIT's pin matches on its expiry cycle
    "r3_dut_wait_tie": dict(
        cls="DUT", file="src/cpu/core.v",
        edits=[("`OP_WAIT:    flag <= wait_condition_met ? 1'b0 : 1'b1;",
                "`OP_WAIT:    flag <= (wait_has_timeout && cnt_expired) ? 1'b1 : (wait_condition_met ? 1'b0 : 1'b1);")],
        truth="WAIT tie resolved as a timeout; isa.md: condition-met wins"),
    "r3_model_wait_tie": dict(
        cls="REFERENCE_MODEL", file="test/golden_model.py",
        edits=[("                s.flag = not met  # isa.md WAIT row: 0 if met, 1 if timed out; met wins the tie\n",
                "                s.flag = expired or not met  # isa.md WAIT row: 0 if met, 1 if timed out\n")],
        scrub=[(r"    condition-met wins on an exact tie, matching core\.v checking the\n    condition before the counter \(docs/isa\.md WAIT row\)\. ",
                "    see docs/isa.md WAIT row. ")],
        truth="model resolves a WAIT tie as a timeout; isa.md: condition-met wins (same symptom as r3_dut_wait_tie)"),
    # a harness bug whose symptom reads as a DUT writeback bug
    "r3_harness_ldx_skip": dict(
        cls="HARNESS", file="test/test_random.py",
        edits=[('        skip_reg = decoded["rd"] if decoded["opcode"] in (asm.OP_LOAD, asm.OP_LOADX) else None\n',
                '        skip_reg = (decoded["rs"] if decoded["opcode"] == asm.OP_LOADX else decoded["rd"]) \\\n'
                '            if decoded["opcode"] in (asm.OP_LOAD, asm.OP_LOADX) else None\n')],
        truth="harness skips Rs instead of Rd at a LOADX commit, so it compares Rd before its documented trailing writeback lands"),
    # a DUT bug whose symptom reads as the harness's SYNC_DELAY being off
    "r3_dut_sync_extra": dict(
        cls="DUT", file="src/io/pin_ctrl.v",
        edits=[("  reg [7:0] uio_in_ff1, uio_in_ff2;", "  reg [7:0] uio_in_ff1, uio_in_ff2, uio_in_ff3;"),
               ("      uio_in_ff2 <= 8'd0;\n", "      uio_in_ff2 <= 8'd0;\n      uio_in_ff3 <= 8'd0;\n"),
               ("      uio_in_ff2 <= uio_in_ff1;\n", "      uio_in_ff2 <= uio_in_ff1;\n      uio_in_ff3 <= uio_in_ff2;\n"),
               ("assign pin_read   = uio_in_ff2[pin_index];", "assign pin_read   = uio_in_ff3[pin_index];")],
        truth="pin_read taps a third flop: WAIT/INB see uio one cycle late; spec: 2-flop synchronizer, uniformly"),
    # two bugs at once: the primary causes the reported failure, a decoy
    # elsewhere is live too (setup checks which one the seed needs)
    "r3_two_bugs": dict(
        cls="DUT", file="src/cpu/core.v",
        edits=[("`OP_TESTBIT: flag <= ra_data[w_bitidx];", "`OP_TESTBIT: flag <= ra_data[3'd7 - w_bitidx];")],
        decoy=dict(file="test/golden_model.py",
                   edits=[("                s.flag = not met  # isa.md WAIT row: 0 if met, 1 if timed out; met wins the tie\n",
                           "                s.flag = expired or not met  # isa.md WAIT row: 0 if met, 1 if timed out\n")]),
        truth="TEST-bit indexes bits MSB-first (7 - bitidx); a second planted bug (model WAIT tie) is live but not the cause of this seed"),
}

# Benchmark trees also leave out everything that catalogues known bugs:
# the curated and blind mutant lists contain several planted edits
# verbatim (dut_wait_tie is mutant #6), and VAL.md / formal/README.md
# describe them. Found while building round 3; rounds 1-2 had them.
BENCH_EXCLUDE_FILES = {"scripts/mutate.py", "scripts/blind_mutate.py", "scripts/formal_mutants.py",
                       "scripts/redteam_eval.py", "scripts/prove_equiv.py", "formal/equiv_proofs.json",
                       "docs/VAL.md", "formal/README.md", "blind_mutation_results.jsonl"}
BENCH_EXCLUDE_DIRS = {"redteam"}

EXCLUDE = {".git", ".venv", "sim_build", "__pycache__", "regression_artifacts", "runs", "triage_bench.py",
           "ledger", "signoff_report.json", "mutation_results.json"}


def copy_tree(dst: Path, bench: bool = False) -> None:
    """Working tree as it is now (committed + uncommitted), minus history
    and the bench's own ground truth; bench=True also drops the bug
    catalogues (BENCH_EXCLUDE_*)."""
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.split()
    for f in files:
        if set(Path(f).parts) & EXCLUDE or f.startswith("triage/reports/"):
            continue
        if bench and (f in BENCH_EXCLUDE_FILES or set(Path(f).parts) & BENCH_EXCLUDE_DIRS):
            continue
        src = ROOT / f
        if not src.is_file():
            continue
        (dst / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst / f)
    (dst / ".venv").symlink_to(ROOT / ".venv")


def cmd_setup(a) -> int:
    case = CASES[a.case]
    dst = Path(a.dir).resolve()
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    copy_tree(dst, bench=True)
    contract = dst / "triage" / "AGENT_CONTRACT.md"
    ctext = contract.read_text()
    if "docs/VAL.md, " in ctext:
        contract.write_text(ctext.replace("docs/VAL.md, ", "", 1) + (
            "\n- **This tree has no docs/VAL.md** (or mutation tooling): benchmark trees leave\n"
            "  out the catalogues of known bugs. The spec is isa.md and architecture.md.\n"))
    clean = {}

    def plant(spec, name):
        target = dst / spec["file"]
        text = clean.setdefault(spec["file"], target.read_text())
        text = target.read_text()
        for old, new in spec["edits"]:
            if text.count(old) != 1:
                sys.exit(f"[bench] {name}: edit anchor not found exactly once in {spec['file']}: {old!r}")
            text = text.replace(old, new)
        for pattern, repl in spec.get("scrub", []):
            text, n = re.subn(pattern, repl, text, flags=re.S)
            if n == 0:
                sys.exit(f"[bench] {name}: scrub pattern matched nothing in {spec['file']}: {pattern!r}")
        target.write_text(text)
        return text

    act = f"source {ROOT / '.venv/bin/activate'} && " if "VIRTUAL_ENV" not in os.environ else ""

    def first_failure(seeds, base=1):
        """Run the random regression; the first failure.json, or None."""
        shutil.rmtree(dst / "test" / "regression_artifacts", ignore_errors=True)
        env = dict(os.environ, SEEDS=str(seeds), SEED_BASE=str(base), COV_DIR="/tmp/triage_bench_cov")
        r = subprocess.run(["bash", "-c", f"{act}make -C test random"], cwd=dst, env=env, capture_output=True, text=True)
        arts = sorted((dst / "test" / "regression_artifacts").glob("seed_*/failure.json"))
        return json.loads(arts[0].read_text()) if arts else None

    primary_text = plant(case, a.case)
    decoy = case.get("decoy")
    if not decoy:
        # the test stops at the first failure
        failure = first_failure(a.seeds)
        if not failure:
            sys.exit(f"[bench] {a.case}: no failing seed in 1..{a.seeds}")
    else:
        # two live bugs: pick the first seed that fails with both planted,
        # fails the same check with the primary alone, and passes with the
        # decoy alone -- so the primary is provably this seed's cause
        decoy_text = plant(decoy, a.case + " (decoy)")
        failure = None
        for s in range(1, a.seeds + 1):
            both = first_failure(1, s)
            if not both:
                continue
            (dst / decoy["file"]).write_text(clean[decoy["file"]])
            alone = first_failure(1, s)
            (dst / case["file"]).write_text(clean[case["file"]])
            (dst / decoy["file"]).write_text(decoy_text)
            decoy_alone = first_failure(1, s)
            (dst / case["file"]).write_text(primary_text)
            if alone and alone["check"] == both["check"] and not decoy_alone:
                failure = both
                break
        if not failure:
            sys.exit(f"[bench] {a.case}: no seed in 1..{a.seeds} isolates the primary bug")
        # the decoy must still be live: some seed in range fails on it alone
        (dst / case["file"]).write_text(clean[case["file"]])
        live = first_failure(a.seeds)
        (dst / case["file"]).write_text(primary_text)
        if not live:
            sys.exit(f"[bench] {a.case}: decoy never fails in 1..{a.seeds}; it would not be live")
    seed = failure["seed"]
    shutil.rmtree(dst / "test" / "regression_artifacts")
    shutil.rmtree(dst / "test" / "sim_build", ignore_errors=True)
    (dst / "TRIAGE_TASK.md").write_text(f"""# Triage task

The random differential regression failed in this tree:

    make -C test random SEEDS={a.seeds} SEED_BASE=1

First failing seed: **{seed}** (default stimulus profile).
Failing check: `{failure['check']}` -- RTL={failure['rtl']!r} golden={failure['golden']!r}

This is a standalone copy without git history. Triage it under
triage/AGENT_CONTRACT.md and write the report to
triage/reports/seed_{seed}/.
""")
    print(json.dumps({"case": a.case, "dir": str(dst), "seed": seed, "check": failure["check"]}))
    return 0


def cmd_score(a) -> int:
    case = CASES[a.case]
    verdicts = sorted(Path(a.dir).glob("triage/reports/*/verdict.json"))
    if not verdicts:
        print(json.dumps({"case": a.case, "score": 0, "error": "no verdict.json"}))
        return 1
    v = json.loads(verdicts[0].read_text())
    cls_ok = v.get("classification") == case["cls"]
    file_ok = (v.get("culprit_file") or "").lstrip("./") == case["file"]
    res = {"case": a.case, "truth_class": case["cls"], "truth_file": case["file"],
           "got_class": v.get("classification"), "got_file": v.get("culprit_file"),
           "confidence": v.get("confidence"), "class_ok": cls_ok, "file_ok": file_ok,
           "score": int(cls_ok) + int(file_ok)}
    print(json.dumps(res))
    return 0 if cls_ok and file_ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=lambda a: print("\n".join(f"{k:20s} {v['cls']}" for k, v in CASES.items())) or 0)
    s = sub.add_parser("setup"); s.add_argument("case", choices=CASES); s.add_argument("dir")
    s.add_argument("--seeds", type=int, default=50); s.set_defaults(fn=cmd_setup)
    c = sub.add_parser("score"); c.add_argument("case", choices=CASES); c.add_argument("dir"); c.set_defaults(fn=cmd_score)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
