#!/usr/bin/env python3
"""BMC (assert) pass for formal/agent_*_props.v files.

This is the "does the property actually hold" gate -- checks every
`assert` in a props file's checker module against its bound target
module over a bounded number of cycles (see formal_common.DEFAULT_DEPTH
for why). See formal_common.py's module docstring for the full
toolchain recipe and every gotcha behind it.

Usage:
    run_formal.py <props_file> [<props_file> ...] [--depth N]
    run_formal.py --all [--depth N]      # every formal/agent_*_props.v

Exit 0 if every props file's assert pass PASSED; exit 1 otherwise.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import formal_common as fc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("props_files", nargs="*")
    ap.add_argument("--all", action="store_true",
                     help="run every formal/agent_*_props.v file")
    ap.add_argument("--depth", type=int, default=fc.DEFAULT_DEPTH)
    ap.add_argument("--induct", type=int, metavar="K", default=0,
                    help="after a passing BMC, run K-step induction; a file is PROVEN "
                         "(unbounded) only if both pass (make prove)")
    args = ap.parse_args()

    # Support the documented `make formal TARGET_PROPS=<path>` invocation
    # form, where make passes "TARGET_PROPS=<path>" as a bare positional
    # argument rather than a flag.
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
    proven = []
    for props_path in props_files:
        print(f"[run_formal] {props_path.name} (depth={args.depth}) ...", flush=True)
        try:
            smt2_path, _wrapper_name = fc.build_smt2(props_path)
        except fc.FormalError as e:
            print(f"  BUILD ERROR: {e}", file=sys.stderr)
            all_ok = False
            continue

        passed, output = fc.run_smtbmc(smt2_path, args.depth, cover_mode=False)
        status = "PASSED" if passed else "FAILED"
        print(f"  Status: {status}")
        if not passed:
            all_ok = False
            for line in output.splitlines():
                if "Assert failed" in line or "BMC failed" in line:
                    print(f"    {line}")
        elif args.induct:
            ind_ok, ind_out = fc.run_smtbmc(smt2_path, args.induct, cover_mode=False, induction=True)
            print(f"  Induction (k={args.induct}): {'PROVEN' if ind_ok else 'NOT PROVEN (bounded only)'}")
            if ind_ok:
                proven.append(props_path.name)
            else:
                all_ok = False
                for line in ind_out.splitlines():
                    if "Assert failed" in line or "failed" in line.lower() and "induction" in line.lower():
                        print(f"    {line.strip()[:200]}")

    if args.induct:
        print(f"\n[run_formal] PROVEN {len(proven)}/{len(props_files)} (k={args.induct}): {proven}")
    print(f"\n[run_formal] {'PASS' if all_ok else 'FAIL'} "
          f"({len(props_files)} props file(s), depth={args.depth})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
