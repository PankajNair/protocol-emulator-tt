#!/usr/bin/env python3
"""
ledger.py -- append-only record of every agent run (docs/VAL.md section 15).

One JSON line per run in ledger/runs.jsonl: which agent, under which
contract, asked to do what, which files it changed, whether those stayed
inside the contract's write_scope, which gates ran (with their real exit
codes) and whether the parent accepted or rejected the result.

Flow, all run by the parent session (never by the agent itself):

    ledger.py start  --agent stimulus-gen --contract test/stim/AGENT_CONTRACT.md \\
                     --task "wide_timing profile" [--prompt-file p.md]
        -> prints RUN_ID, snapshots the working tree
    ... agent runs ...
    ledger.py gate   RUN_ID --name seeds -- make -C test random SEEDS=200
    ledger.py gate   RUN_ID --signoff          # import signoff_report.json
    ledger.py finish RUN_ID --verdict accepted|rejected --reason "..." [--commit SHA]
    ledger.py show [--agent NAME] [-n N]
    ledger.py stats

Rules `finish` enforces:
  * changed files = files that differ from the start snapshot; files that
    were already dirty at start and are byte-identical now don't count;
  * a change outside write_scope, or touching protected_paths, is a scope
    violation;
  * `accepted` needs at least one gate, every gate passing, and no scope
    violation -- `--override REASON` records an explicit exception.
"""

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_DIR = ROOT / "ledger"
LEDGER = LEDGER_DIR / "runs.jsonl"
OPEN_DIR = LEDGER_DIR / ".open"
PROMPTS_DIR = LEDGER_DIR / "prompts"
AGENTS_DIR = Path.home() / ".claude" / "agents"
SIGNOFF_REPORT = ROOT / "signoff_report.json"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else None


def dirty_files() -> set[str]:
    """Tracked files differing from HEAD plus untracked, non-ignored files."""
    out = git("status", "--porcelain", "--untracked-files=all", "-z")
    files = set()
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        if len(e) > 3:
            files.add(e[3:])
            if e[0] == "R":  # rename: the next entry is the source path
                i += 1
        i += 1
    return {f for f in files if not f.startswith("ledger/")}


def snapshot() -> dict[str, str | None]:
    return {f: sha256(ROOT / f) for f in sorted(dirty_files())}


def parse_contract(path: Path) -> dict:
    """Read the YAML-ish frontmatter of an AGENT_CONTRACT.md (flat keys,
    flow-style lists) without needing PyYAML."""
    text = path.read_text()
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    meta = {}
    for line in (m.group(1).splitlines() if m else []):
        k, sep, v = line.partition(":")
        if not sep or line.startswith(" "):
            continue
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k.strip()] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
        else:
            meta[k.strip()] = v.strip("\"'")
    return meta


def under(path: str, prefixes: list[str]) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def open_path(run_id: str) -> Path:
    p = OPEN_DIR / f"{run_id}.json"
    if not p.exists():
        sys.exit(f"[ledger] no open run {run_id} (open: {[q.stem for q in OPEN_DIR.glob('*.json')]})")
    return p


def load_runs() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]


# --------------------------------------------------------------------- start
def cmd_start(a) -> None:
    contract = ROOT / a.contract
    if not contract.exists():
        sys.exit(f"[ledger] contract not found: {a.contract}")
    meta = parse_contract(contract)
    if meta.get("role") and meta["role"] != a.agent:
        sys.exit(f"[ledger] contract role is {meta['role']!r}, not {a.agent!r}")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + f"-{a.agent}"
    agent_def = AGENTS_DIR / f"{a.agent}.md"
    rec = {
        "id": run_id,
        "agent": a.agent,
        "agent_def_sha": sha256(agent_def),
        "contract": a.contract,
        "contract_sha": sha256(contract),
        "write_scope": meta.get("write_scope"),
        "protected_paths": meta.get("protected_paths", []),
        "task": a.task,
        "base_commit": git("rev-parse", "--short", "HEAD").strip(),
        "started": now(),
        "started_epoch": time.time(),
        "snapshot": snapshot(),
        "gates": [],
    }
    if a.prompt_file:
        PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        dst = PROMPTS_DIR / f"{run_id}.md"
        dst.write_text(Path(a.prompt_file).read_text())
        rec["prompt"] = str(dst.relative_to(ROOT))
    OPEN_DIR.mkdir(parents=True, exist_ok=True)
    (OPEN_DIR / f"{run_id}.json").write_text(json.dumps(rec, indent=2))
    pre = len(rec["snapshot"])
    print(run_id)
    if pre:
        print(f"[ledger] note: {pre} file(s) already dirty at start; only further changes count", file=sys.stderr)


# ---------------------------------------------------------------------- gate
def cmd_gate(a) -> None:
    p = open_path(a.run_id)
    rec = json.loads(p.read_text())
    if a.signoff:
        if not SIGNOFF_REPORT.exists() or SIGNOFF_REPORT.stat().st_mtime < rec["started_epoch"]:
            sys.exit("[ledger] signoff_report.json missing or older than this run's start -- run make signoff first")
        r = json.loads(SIGNOFF_REPORT.read_text())
        for name, g in r["gates"].items():
            if g["verdict"] == "SKIPPED":
                continue
            rec["gates"].append({"name": f"signoff:{name}", "cmd": "make signoff",
                                 "passed": g["verdict"] == "PASS", "seconds": g.get("seconds"),
                                 "info": {k: v for k, v in g.items() if k not in ("verdict", "seconds")}})
        print(f"[ledger] imported {len(r['gates'])} signoff gates, passed={r['passed']}")
    else:
        cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
        if not cmd or not a.name:
            sys.exit("[ledger] gate needs --name NAME -- COMMAND..., or --signoff")
        shell = " ".join(shlex.quote(c) for c in cmd) if len(cmd) > 1 else cmd[0]
        t0 = time.time()
        r = subprocess.run(["bash", "-c", shell], cwd=ROOT, capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip().splitlines()
        rec["gates"].append({"name": a.name, "cmd": shell, "passed": r.returncode == 0,
                             "rc": r.returncode, "seconds": round(time.time() - t0),
                             "tail": out[-8:]})
        print("\n".join(out[-15:]))
        print(f"[ledger] gate {a.name}: {'PASS' if r.returncode == 0 else 'FAIL'} (rc={r.returncode})")
    p.write_text(json.dumps(rec, indent=2))


# -------------------------------------------------------------------- finish
def changed_since(rec: dict) -> list[str]:
    # files changed by commits made since start, plus the working tree
    committed = set(git("diff", "--name-only", rec["base_commit"], "HEAD").split())
    candidates = committed | dirty_files() | set(rec["snapshot"])
    changed = []
    for f in sorted(candidates):
        if f.startswith("ledger/"):
            continue
        if f in rec["snapshot"] and f not in committed and sha256(ROOT / f) == rec["snapshot"][f]:
            continue  # dirty before the agent ran, untouched since
        changed.append(f)
    return changed


def cmd_finish(a) -> None:
    p = open_path(a.run_id)
    rec = json.loads(p.read_text())
    files = changed_since(rec)
    scope = rec.get("write_scope")
    outside = [f for f in files if scope and not under(f, [scope])]
    protected = [f for f in files if under(f, rec.get("protected_paths", []))]
    gates_ok = bool(rec["gates"]) and all(g["passed"] for g in rec["gates"])

    problems = []
    if a.verdict == "accepted":
        if not rec["gates"]:
            problems.append("no gates recorded")
        elif not gates_ok:
            problems.append("failing gates: " + ", ".join(g["name"] for g in rec["gates"] if not g["passed"]))
        if outside or protected:
            problems.append(f"scope violations: outside={outside} protected={protected}")
    if problems and not a.override:
        sys.exit("[ledger] refusing 'accepted': " + "; ".join(problems) + "\n         (fix it, reject, or pass --override REASON)")

    entry = {
        "id": rec["id"], "source": "live", "agent": rec["agent"],
        "agent_def_sha": rec["agent_def_sha"], "contract": rec["contract"],
        "contract_sha": rec["contract_sha"], "task": rec["task"],
        "prompt": rec.get("prompt"), "base_commit": rec["base_commit"],
        "started": rec["started"], "finished": now(),
        "minutes": round((time.time() - rec["started_epoch"]) / 60, 1),
        "files_changed": files, "scope_outside": outside, "scope_protected": protected,
        "gates": rec["gates"], "gates_passed": gates_ok,
        "verdict": a.verdict, "reason": a.reason, "override": a.override,
        "commit": a.commit, "bugs_found": a.bugs_found,
    }
    LEDGER_DIR.mkdir(exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    p.unlink()
    flag = " SCOPE-VIOLATION" if outside or protected else ""
    print(f"[ledger] {entry['id']}: {a.verdict}{flag}, {len(files)} file(s), "
          f"{len(rec['gates'])} gate(s) {'all pass' if gates_ok else 'NOT all pass'}")


# ------------------------------------------------------------- show / stats
def cmd_show(a) -> None:
    runs = [r for r in load_runs() if not a.agent or r["agent"] == a.agent][-a.n:]
    for r in runs:
        g = r.get("gates") or []
        gates = f"{sum(x['passed'] for x in g)}/{len(g)}" if g else "-"
        scope = "!" if r.get("scope_outside") or r.get("scope_protected") else " "
        print(f"{r['id']:40s} {r['verdict']:9s}{scope} gates {gates:6s} "
              f"{r.get('commit') or '-':8s} {r['task'][:60]}")
    opened = sorted(q.stem for q in OPEN_DIR.glob("*.json")) if OPEN_DIR.exists() else []
    if opened:
        print(f"open: {', '.join(opened)}")


def cmd_stats(a) -> None:
    runs = load_runs()
    agents = sorted({r["agent"] for r in runs})
    print(f"{'agent':18s} {'runs':>4s} {'acc':>4s} {'rej':>4s} {'acc%':>5s} {'scope!':>6s} {'bugs':>4s}  live/backfill")
    for ag in agents:
        rs = [r for r in runs if r["agent"] == ag]
        acc = sum(r["verdict"] == "accepted" for r in rs)
        rej = sum(r["verdict"] == "rejected" for r in rs)
        viol = sum(bool(r.get("scope_outside") or r.get("scope_protected")) for r in rs)
        bugs = sum(len(r.get("bugs_found") or []) for r in rs)
        live = sum(r.get("source") == "live" for r in rs)
        print(f"{ag:18s} {len(rs):4d} {acc:4d} {rej:4d} {100 * acc / len(rs):4.0f}% {viol:6d} {bugs:4d}  {live}/{len(rs) - live}")
    if any(r.get("source") == "backfill" for r in runs):
        print("note: backfilled runs record accepted work only; rejected attempts before the ledger were not kept")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("--agent", required=True)
    s.add_argument("--contract", required=True, help="repo-relative AGENT_CONTRACT.md")
    s.add_argument("--task", required=True)
    s.add_argument("--prompt-file")
    s.set_defaults(fn=cmd_start)

    g = sub.add_parser("gate")
    g.add_argument("run_id")
    g.add_argument("--name")
    g.add_argument("--signoff", action="store_true", help="import signoff_report.json")
    g.add_argument("cmd", nargs="*", help="after --: the gate command")
    g.set_defaults(fn=cmd_gate)

    f = sub.add_parser("finish")
    f.add_argument("run_id")
    f.add_argument("--verdict", required=True, choices=["accepted", "rejected"])
    f.add_argument("--reason", required=True)
    f.add_argument("--commit")
    f.add_argument("--bugs-found", action="append", default=[], help="repeatable: short description of a real bug the run found")
    f.add_argument("--override", help="reason for accepting despite failing gates / scope violation")
    f.set_defaults(fn=cmd_finish)

    sh = sub.add_parser("show")
    sh.add_argument("--agent")
    sh.add_argument("-n", type=int, default=30)
    sh.set_defaults(fn=cmd_show)

    st = sub.add_parser("stats")
    st.set_defaults(fn=cmd_stats)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
