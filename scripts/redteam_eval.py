#!/usr/bin/env python3
"""
redteam_eval.py -- evaluate a red-team candidate mutant against every
verification gate (docs/VAL.md section 17). The red-team agent may run it
to pre-screen; the parent re-runs it as the gate and trusts only its own
run.

A candidate is a JSON file (format: redteam/AGENT_CONTRACT.md):
    {"id": "...", "file": "io/pin_ctrl.v", "edits": [["old", "new"], ...],
     "claim": "...", "spec_ref": "...",
     "witness": {"program": "witness.txt", "seed": 1, "profile": null}}
or, for behaviour the random harness doesn't model (boot handshake, host
pins 4/5/6, unbounded WAIT, board-level protocols):
     "witness": {"cocotb_test": "witness_x.py", "board": false}
`file` is relative to src/; each `old` must occur exactly once. Witness
paths are relative to the candidate file. A cocotb witness module is
copied into test/ of a mutant copy and of a clean copy and run there
(BOARD=yes toplevel when "board" is true).

    redteam_eval.py CANDIDATE.json [--seeds 200] [--full] [--keep]

Steps:
  1. copy the working tree to a scratch dir (src/ of this repo is never
     touched) and apply the edits;
  2. run the gates on the copy: directed, random (--seeds), board,
     formal (+ the 4 stimulus profiles with --full). Any failing gate =
     KILLED;
  3. if every gate passes, check the witness: it must FAIL on the mutant
     (the change is observable) and PASS on the clean tree (the witness
     itself is legal and the clean design satisfies it).
Verdict: KILLED | SURVIVED_WITNESSED (a real gap) |
         SURVIVED_NO_WITNESS (possibly equivalent -- not a finding) |
         INVALID (edits don't apply / build breaks / bad witness).
Writes <candidate>.result.json next to the candidate.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from triage_bench import copy_tree  # noqa: E402  (same copy rules: no .git, no ledger)

VENV_ACT = ROOT / ".venv" / "bin" / "activate"
PROFILES = ["protocol_pins", "wide_timing", "illegal_mix", "nested_flow"]


def sh(cmd: str, cwd: Path, timeout: int = 900) -> tuple[int, str]:
    act = f"source {VENV_ACT} && " if VENV_ACT.exists() else ""
    pr = subprocess.Popen(["bash", "-c", act + cmd], cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = pr.communicate(timeout=timeout)
        return pr.returncode, out
    except subprocess.TimeoutExpired:
        os.killpg(pr.pid, signal.SIGKILL)
        pr.communicate()
        return 124, f"TIMEOUT after {timeout}s"


def cocotb_ok(rc: int, out: str, test_dir: Path) -> bool:
    # cocotb's make can exit 0 with failing tests: check the totals and
    # results.xml as well
    xml = test_dir / "results.xml"
    return (rc == 0 and "FAIL=0" in out and "TESTS=" in out
            and not (xml.exists() and "<failure" in xml.read_text()))


def gates(tree: Path, seeds: int, full: bool) -> dict:
    t = tree / "test"
    res = {}
    rc, out = sh("make -B", t)
    res["directed"] = cocotb_ok(rc, out, t)
    rc, out = sh(f"rm -f results.xml && make random SEEDS={seeds}", t)
    res["random"] = cocotb_ok(rc, out, t)
    if full:
        for p in PROFILES:
            mc = "MAX_CYCLES=50000 " if p == "wide_timing" else ""
            rc, out = sh(f"rm -f results.xml && {mc}make random SEEDS=50 STIM_PROFILE={p}", t)
            res[f"random:{p}"] = cocotb_ok(rc, out, t)
    rc, out = sh("rm -rf sim_build/board results.xml && make board", t, timeout=1200)
    res["board"] = cocotb_ok(rc, out, t)
    rc, out = sh("make formal", tree, timeout=1800)
    res["formal"] = rc == 0 and "[run_formal] PASS" in out
    return res


def replay(tree: Path, w: dict, program: Path) -> int:
    cmd = f"python3 scripts/triage.py replay --seed {w.get('seed', 1)} --program {program}"
    if w.get("profile"):
        cmd += f" --profile {w['profile']}"
    rc, _ = sh(cmd, tree, timeout=600)
    return rc  # 0 pass, 1 fail, other = error


def run_cocotb_witness(tree: Path, w: dict, module_file: Path) -> int:
    name = module_file.stem
    shutil.copy2(module_file, tree / "test" / module_file.name)
    board = "BOARD=yes " if w.get("board") else ""
    rc, out = sh(f"rm -rf sim_build results.xml && make {board}COCOTB_TEST_MODULES={name}", tree / "test")
    if "TESTS=" not in out:
        return 2  # didn't run: import/build error
    return 0 if cocotb_ok(rc, out, tree / "test") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("candidate")
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--full", action="store_true", help="also run the 4 stimulus profiles")
    ap.add_argument("--keep", action="store_true", help="keep the scratch copy")
    a = ap.parse_args()

    cpath = Path(a.candidate).resolve()
    cand = json.loads(cpath.read_text())
    result = {"id": cand.get("id"), "file": cand.get("file"), "seeds": a.seeds, "full": a.full}
    tmp = Path(tempfile.mkdtemp(prefix="redteam_"))
    t0 = time.time()
    try:
        copy_tree(tmp)
        target = tmp / "src" / cand["file"]
        if not target.exists():
            result.update(verdict="INVALID", reason=f"no such file src/{cand['file']}")
            return finish(cpath, result, t0)
        text = target.read_text()
        for old, new in cand["edits"]:
            if text.count(old) != 1:
                result.update(verdict="INVALID", reason=f"edit anchor must occur exactly once: {old[:80]!r}")
                return finish(cpath, result, t0)
            text = text.replace(old, new)
        target.write_text(text)

        rc, out = sh("make -B sim_build/rtl/sim.vvp", tmp / "test")
        if rc != 0:
            result.update(verdict="INVALID", reason="mutant does not build", log=out[-1500:])
            return finish(cpath, result, t0)

        g = gates(tmp, a.seeds, a.full)
        result["gates"] = {k: ("PASS" if v else "FAIL") for k, v in g.items()}
        if not all(g.values()):
            result["verdict"] = "KILLED"
            return finish(cpath, result, t0)

        w = cand.get("witness") or {}
        if w.get("program"):
            prog = (cpath.parent / w["program"]).resolve()
            on_mutant = replay(tmp, w, prog)
            on_clean = replay(ROOT, w, prog)
        elif w.get("cocotb_test"):
            mod = (cpath.parent / w["cocotb_test"]).resolve()
            on_mutant = run_cocotb_witness(tmp, w, mod)
            clean = Path(tempfile.mkdtemp(prefix="redteam_clean_"))
            try:
                copy_tree(clean)
                on_clean = run_cocotb_witness(clean, w, mod)
            finally:
                shutil.rmtree(clean, ignore_errors=True)
        else:
            result["verdict"] = "SURVIVED_NO_WITNESS"
            return finish(cpath, result, t0)
        result["witness"] = {"on_mutant": {0: "PASS", 1: "FAIL"}.get(on_mutant, f"ERROR rc={on_mutant}"),
                             "on_clean": {0: "PASS", 1: "FAIL"}.get(on_clean, f"ERROR rc={on_clean}")}
        if on_mutant == 1 and on_clean == 0:
            result["verdict"] = "SURVIVED_WITNESSED"
        else:
            result.update(verdict="INVALID", reason="witness must FAIL on the mutant and PASS on the clean tree")
        return finish(cpath, result, t0)
    finally:
        if a.keep:
            print(f"[redteam] scratch copy kept at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def finish(cpath: Path, result: dict, t0: float) -> int:
    result["seconds"] = round(time.time() - t0)
    out = cpath.with_suffix(".result.json")
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["verdict"] in ("KILLED", "SURVIVED_WITNESSED", "SURVIVED_NO_WITNESS") else 2


if __name__ == "__main__":
    sys.exit(main())
