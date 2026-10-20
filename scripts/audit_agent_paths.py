#!/usr/bin/env python3
"""
audit_agent_paths.py -- did a subagent stay inside the tree it was given?
(docs/VAL.md section 21)

    audit_agent_paths.py TRANSCRIPT.jsonl --tree DIR [--allow PATH ...] [--skip N]

--skip N ignores the first N tool calls: a resumed agent appends to the
same transcript, so a later pass is audited on its own calls only.

Reads a subagent's transcript (the per-agent JSONL Claude Code writes)
and checks every tool call: file paths passed to Read/Write/Edit, and
every absolute path appearing in a Bash command. Any path outside --tree
(and outside --allow, e.g. the Python interpreter) is a violation.
Independence checks depend on this: an agent told to read only the spec
must provably not have read the design.

This is an audit of what the agent asked for, so it is conservative:
a relative path in a Bash command is resolved against the tree, and a
shell glob or variable it cannot resolve is reported as UNRESOLVED for
a human to look at. Exit 0 iff no violation and nothing unresolved.
"""

import argparse
import os
import json
import re
import sys
from pathlib import Path

# only tokens under a real filesystem root count as paths (a sed pattern
# like s/IN R1/ must not); ~ is the home directory
ROOTS = ("/Users/", "/private/", "/tmp/", "/var/", "/etc/", "/opt/", "/usr/", "/System/", "/Library/",
         "/Volumes/", "/home/", "/root/", "~/", "~")
ABS = re.compile(r"(?<![\w.~-])((?:/|~)[^\s'\"`;|&<>()]*)")
REL_UP = re.compile(r"(?<![\w/.-])((?:\.\./)+[^\s'\"`;|&<>()]*)")
CD = re.compile(r"\bcd\s+([^\s;&|]+)")
SYSTEM_OK = ("/usr/", "/bin/", "/opt/homebrew/", "/dev/null",
             "/System/", "/Library/Developer/")


def own_task_outputs(path: Path) -> set[str]:
    """Output files of background commands the agent itself started: the
    harness reports each as "running in background with ID: <id>" and the
    agent may read <tasks dir>/<id>.output. Other agents' transcripts
    live in the same directory and stay off limits."""
    ids = set(re.findall(r"running in background with ID: (\w+)", path.read_text()))
    return {f"{i}.output" for i in ids}


def tool_calls(path: Path):
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = rec.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                yield c.get("name"), c.get("input") or {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcript", type=Path)
    ap.add_argument("--tree", type=Path, required=True)
    ap.add_argument("--allow", nargs="*", default=[])
    ap.add_argument("--skip", type=int, default=0, help="ignore the first N tool calls (earlier passes)")
    a = ap.parse_args()
    tree = str(a.tree.resolve())
    variants = {tree, tree.replace("/private/tmp/", "/tmp/", 1)}
    allowed = list(variants) + [str(Path(p).resolve()) for p in a.allow]

    own = own_task_outputs(a.transcript)

    def ok(p: str) -> bool:
        if Path(p).name in own and Path(p).parent.name == "tasks":
            return True
        return any(p == t or p.startswith(t.rstrip("/") + "/") for t in allowed)

    n, bad, unresolved = 0, [], []
    for i, (name, inp) in enumerate(tool_calls(a.transcript)):
        if i < a.skip:
            continue
        n += 1
        if name in ("Read", "Write", "Edit", "NotebookEdit"):
            p = inp.get("file_path") or inp.get("notebook_path") or ""
            if not ok(str(Path(p).resolve()) if p else ""):
                bad.append(f"{name} {p}")
        elif name == "Bash":
            cmd = inp.get("command", "")
            for p in ABS.findall(cmd):
                if not p.startswith(ROOTS):
                    continue
                full = str(Path(p).expanduser().resolve())
                if ok(p) or ok(full) or p.startswith(SYSTEM_OK) or full.startswith(SYSTEM_OK):
                    continue
                bad.append(f"Bash path {p}  in: {cmd[:120]!r}")
            # ../ is resolved against the command's last absolute cd (else the tree)
            cds = [c for c in CD.findall(cmd) if c.startswith("/")]
            base = Path(cds[-1]) if cds else Path(tree)
            for p in REL_UP.findall(cmd):
                full = os.path.normpath(str(base / p))  # lexical: the tree's .venv is a deliberate symlink
                if not ok(full):
                    bad.append(f"Bash path {p} (= {full})  in: {cmd[:120]!r}")
            if re.search(r"\$\{?[A-Za-z_]|`", cmd):
                unresolved.append(f"Bash (shell variable or substitution) {cmd[:160]!r}")
        elif name in ("Glob", "Grep"):
            p = inp.get("path", "")
            if p and not ok(str(Path(p).resolve())):
                bad.append(f"{name} path {p}")
            if not p:
                unresolved.append(f"{name} without a path (searches the cwd): {inp}")
        elif name in ("SubagentHandback", "TodoWrite"):
            continue  # no filesystem access
        else:
            unresolved.append(f"tool {name}: {str(inp)[:120]}")
    for b in bad:
        print(f"  VIOLATION  {b}")
    for u in unresolved:
        print(f"  UNRESOLVED {u}")
    print(f"[audit_agent_paths] {n} tool calls: {len(bad)} outside the tree, {len(unresolved)} unresolved -> "
          f"{'PASS' if not bad and not unresolved else 'FAIL'}")
    return 0 if not bad and not unresolved else 1


if __name__ == "__main__":
    sys.exit(main())
