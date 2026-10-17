#!/usr/bin/env python3
"""
assert_superset.py -- anti-weakening gate for formal props edits.

    assert_superset.py REV FILE [FILE ...]

Every assert/assume statement in FILE as of git revision REV must still be
present, verbatim (whitespace-normalized), in the working-tree FILE, and
the working tree must not add any assume. An agent strengthening a props
file for induction may ADD asserts (auxiliary invariants, which are then
proven too) but may not drop, edit or relax an existing one, and may not
constrain the environment further. Exit 0 if that holds for every file.
"""

import re
import subprocess
import sys
from pathlib import Path

STMT = re.compile(r"\b(assert|assume)\s*\((.*?)\)\s*;", re.S)


def stmts(text: str) -> list[tuple[str, str]]:
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return [(k, " ".join(e.split())) for k, e in STMT.findall(text)]


def main() -> int:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    rev, files = sys.argv[1], sys.argv[2:]
    ok = True
    for f in files:
        old_text = subprocess.run(["git", "show", f"{rev}:{f}"], capture_output=True, text=True).stdout
        new = stmts(Path(f).read_text())
        old = stmts(old_text)
        missing = [e for e in old if e not in new]
        added_assumes = [e for e in new if e[0] == "assume" and e not in old]
        n_old = sum(k == "assert" for k, _ in old)
        n_new = sum(k == "assert" for k, _ in new)
        status = "OK" if not missing and not added_assumes else "FAIL"
        ok &= status == "OK"
        print(f"[assert_superset] {f}: {status}  asserts {n_old} -> {n_new}"
              f"{f', {len(missing)} missing' if missing else ''}"
              f"{f', {len(added_assumes)} new assume(s)' if added_assumes else ''}")
        for k, e in missing:
            print(f"    missing {k}: {e[:150]}")
        for k, e in added_assumes:
            print(f"    new assume: {e[:150]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
