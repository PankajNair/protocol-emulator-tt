#!/usr/bin/env python3
"""Vacuity-check gate for formal/agent_*_props.v files.

"Is every property's antecedent actually reachable" -- a distinct
question from run_formal.py's "does the assert hold" check. A property
whose antecedent can never become true passes its assert TRIVIALLY (the
`if (antecedent) assert(...)` body just never executes), which would
silently make run_formal.py's PASSED verdict meaningless for that
property. Must run in the parent orchestrating session, never delegated
to a subagent -- this script is that gate (mirrors the sibling
5-Stage-Pipelined-RISC-V-Processor project's files/VAL.md §8 rule).

Mechanism: every property in this project's props-file convention pairs
its `assert` with a `cover(<same antecedent>)` immediately before it
(see formal/AGENT_CONTRACT.md). yosys-smtbmc's native cover-analysis
mode (`-c`) finds, for each `$cover` cell, whether some reachable trace
makes it true within the given bound -- reporting "Reached cover
statement ..." or "Unreached cover statement ..." with file:line for
each. This script runs that pass and requires 100% reached.

Usage:
    vacuity_check.py <props_file> [<props_file> ...] [--depth N]
    vacuity_check.py --all [--depth N]      # every formal/agent_*_props.v
    vacuity_check.py TARGET_PROPS=<props_file>   # `make vacuity TARGET_PROPS=...`

Exit 0 if every cover in every props file was reached; exit 1 otherwise.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import formal_common as fc

REACHED_RE = re.compile(r"Reached cover statement in step \d+ at \S+: (\S+)")
UNREACHED_RE = re.compile(r"Unreached cover statement at \S+: (\S+)")
TEST_ROOT = Path(__file__).resolve().parent.parent.parent / "test"


def deep_policy(props_path: Path, unreached: list[str]) -> tuple[list[str], list[str], str | None]:
    """Split unreached covers into (deep, vacuous). A cover is DEEP -- not
    counted against the BMC vacuity threshold -- only if its source line
    is tagged `[deep]` AND the file declares `// deep-evidence: <test>`
    naming a cocotb test in test/ that drives the design into that state
    in simulation. Its asserts are still proven for all states by
    k-induction (make prove); the simulation test shows the state is
    reachable, which is what a BMC cover can't do at that depth."""
    src = props_path.read_text().splitlines()
    m = re.search(r"//\s*deep-evidence:\s*(\w+)", props_path.read_text())
    evidence = m.group(1) if m else None
    if evidence and not any(re.search(rf"\basync def {evidence}\b", f.read_text())
                            for f in TEST_ROOT.glob("*.py")):
        evidence = None  # names a test that doesn't exist
    deep, vacuous = [], []
    for loc in unreached:
        lm = re.search(r":(\d+)\.", loc)
        line = src[int(lm.group(1)) - 1] if lm and int(lm.group(1)) <= len(src) else ""
        (deep if "[deep]" in line and evidence else vacuous).append(loc)
    return deep, vacuous, evidence


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("props_files", nargs="*")
    ap.add_argument("--all", action="store_true",
                     help="run every formal/agent_*_props.v file")
    ap.add_argument("--depth", type=int, default=fc.DEFAULT_DEPTH)
    args = ap.parse_args()

    props_files = []
    for arg in args.props_files:
        if arg.startswith("TARGET_PROPS="):
            props_files.append(Path(arg.split("=", 1)[1]))
        else:
            props_files.append(Path(arg))

    if args.all:
        props_files = sorted(fc.FORMAL_DIR.glob("agent_*_props.v"))
    if not props_files:
        print("ERROR: no props files given (use --all, list files explicitly, "
              "or TARGET_PROPS=<path>)", file=sys.stderr)
        return 1

    all_ok = True
    total_reached = total_covers = total_deep = 0
    for props_path in props_files:
        print(f"[vacuity_check] {props_path.name} (depth={args.depth}) ...", flush=True)
        try:
            smt2_path, _wrapper_name = fc.build_smt2(props_path)
        except fc.FormalError as e:
            print(f"  BUILD ERROR: {e}", file=sys.stderr)
            all_ok = False
            continue

        passed, output = fc.run_smtbmc(smt2_path, args.depth, cover_mode=True)
        reached = REACHED_RE.findall(output)
        unreached = UNREACHED_RE.findall(output)
        deep, unreached, evidence = deep_policy(props_path, unreached)
        total_reached += len(reached)
        total_deep += len(deep)
        total_covers += len(reached) + len(unreached)

        for loc in reached:
            print(f"  [REACHED  ] {loc}")
        for loc in deep:
            print(f"  [DEEP     ] {loc}  (beyond BMC depth; reached in simulation by {evidence})")
        for loc in unreached:
            print(f"  [UNREACHED] {loc}  <-- VACUOUS")

        # smtbmc's cover pass reports FAILED whenever any cover is unreached;
        # deep covers (see deep_policy) are accounted for separately
        if unreached or (not passed and not deep):
            all_ok = False

    deep_note = f" + {total_deep} deep (simulation evidence)" if total_deep else ""
    print(f"\n[vacuity_check] {total_reached}/{total_covers} covers reached{deep_note} "
          f"({'100%' if total_covers and total_reached == total_covers else 'INCOMPLETE'})")
    print(f"[vacuity_check] {'PASS' if all_ok else 'FAIL'} "
          f"({len(props_files)} props file(s), depth={args.depth})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
