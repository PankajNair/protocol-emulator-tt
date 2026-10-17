#!/usr/bin/env python3
"""
formal_mutants.py -- which mutation-gate mutants does FORMAL alone kill?
(docs/VAL.md section 6)

    formal_mutants.py [--ids 3,7,12] [--props formal/x.v ...]

For each scripts/mutate.py mutant (or just --ids): copy the working tree
to a scratch dir (the real src/ is never touched), apply the mutant, and
run `make prove` there (BMC + k-induction) on --props, or on every props
file. KILLED = some file fails BMC or induction. Each copy has its own
FORMAL_SCRATCH, so sweeps can run alongside other formal work.
Exit 0 iff every selected mutant is killed.
"""

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from triage_bench import copy_tree  # noqa: E402

VENV_ACT = ROOT / ".venv" / "bin" / "activate"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", help="comma-separated mutant ids (default: all)")
    ap.add_argument("--props", nargs="*", default=[], help="props files (default: all)")
    a = ap.parse_args()
    spec = importlib.util.spec_from_file_location("mutate", ROOT / "scripts" / "mutate.py")
    mut = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mut)
    ids = {int(x) for x in a.ids.split(",")} if a.ids else None
    mutants = [m for m in mut.MUTANTS if ids is None or m["id"] in ids]
    target = " ".join(a.props)
    act = f"source {VENV_ACT} && " if VENV_ACT.exists() else ""
    killed = []
    for m in mutants:
        d = Path(tempfile.mkdtemp(prefix="formal_mut_"))
        try:
            copy_tree(d)
            f = d / "src" / m["file"]
            text = f.read_text()
            for old, new in m["edits"]:
                if old not in text:
                    print(f"#{m['id']:2d} ERROR    edit anchor not found: {old[:60]!r}")
                    break
                text = text.replace(old, new)
            else:
                f.write_text(text)
                env = dict(os.environ, FORMAL_SCRATCH=str(d / "fscratch"))
                cmd = f"python3 formal/scripts/run_formal.py --induct 20 {target or '--all'}"
                r = subprocess.run(["bash", "-c", act + cmd], cwd=d, env=env,
                                   capture_output=True, text=True, timeout=1800)
                out = r.stdout + r.stderr
                summary = next((l for l in reversed(out.splitlines()) if "PROVEN" in l and "/" in l), "")
                k = r.returncode != 0
                if k:
                    killed.append(m["id"])
                print(f"#{m['id']:2d} {'KILLED ' if k else 'SURVIVE'}  {m['desc'][:58]:58s} "
                      f"{summary.split(':')[0].replace('[run_formal] ', '')}", flush=True)
        finally:
            shutil.rmtree(d, ignore_errors=True)
    print(f"[formal_mutants] killed {len(killed)}/{len(mutants)}: {killed}")
    return 0 if len(killed) == len(mutants) else 1


if __name__ == "__main__":
    sys.exit(main())
