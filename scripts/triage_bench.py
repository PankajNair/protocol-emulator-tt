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
}

EXCLUDE = {".git", ".venv", "sim_build", "__pycache__", "regression_artifacts", "runs", "triage_bench.py",
           "ledger", "signoff_report.json", "mutation_results.json"}


def copy_tree(dst: Path) -> None:
    """Working tree as it is now (committed + uncommitted), minus history
    and the bench's own ground truth."""
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.split()
    for f in files:
        if set(Path(f).parts) & EXCLUDE or f.startswith("triage/reports/"):
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
    copy_tree(dst)
    target = dst / case["file"]
    text = target.read_text()
    for old, new in case["edits"]:
        if text.count(old) != 1:
            sys.exit(f"[bench] {a.case}: edit anchor not found exactly once in {case['file']}: {old!r}")
        text = text.replace(old, new)
    for pattern, repl in case.get("scrub", []):
        text, n = re.subn(pattern, repl, text, flags=re.S)
        if n == 0:
            sys.exit(f"[bench] {a.case}: scrub pattern matched nothing in {case['file']}: {pattern!r}")
    target.write_text(text)

    # find the first failing seed (the test stops at the first failure)
    env = dict(os.environ, SEEDS=str(a.seeds), SEED_BASE="1", COV_DIR="/tmp/triage_bench_cov")
    act = f"source {ROOT / '.venv/bin/activate'} && " if "VIRTUAL_ENV" not in os.environ else ""
    r = subprocess.run(["bash", "-c", f"{act}make -C test random"], cwd=dst, env=env, capture_output=True, text=True)
    arts = sorted((dst / "test" / "regression_artifacts").glob("seed_*/failure.json"))
    if not arts:
        print(r.stdout[-2000:])
        sys.exit(f"[bench] {a.case}: no failing seed in 1..{a.seeds}")
    failure = json.loads(arts[0].read_text())
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
