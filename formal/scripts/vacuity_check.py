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
UNREACHED_RE = re.compile(r"Unreached cover statement at (\S+)")


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
    total_reached = total_covers = 0
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
        total_reached += len(reached)
        total_covers += len(reached) + len(unreached)

        for loc in reached:
            print(f"  [REACHED  ] {loc}")
        for loc in unreached:
            print(f"  [UNREACHED] {loc}  <-- VACUOUS")

        if unreached or not passed:
            all_ok = False

    print(f"\n[vacuity_check] {total_reached}/{total_covers} covers reached "
          f"({'100%' if total_covers and total_reached == total_covers else 'INCOMPLETE'})")
    print(f"[vacuity_check] {'PASS' if all_ok else 'FAIL'} "
          f"({len(props_files)} props file(s), depth={args.depth})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
