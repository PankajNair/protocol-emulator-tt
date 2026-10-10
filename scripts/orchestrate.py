#!/usr/bin/env python3
"""
orchestrate.py -- the agentic DV pipeline as a resumable state machine
(docs/VAL.md section 18).

One *cycle* runs these stages in order:

    signoff   every gate (scripts/signoff.py)                    [script]
    triage    one ticket per failing random seed: the            [agent]
              triage-debug agent classifies it; gates = verdict
              schema, replay reproduces, culprit lines exist
    redteam   only on a clean signoff: the red-team agent        [agent]
              proposes candidates; gate = redteam_eval --full
              on each one
    close     cycle summary appended to ledger/cycles.jsonl       [script]

The script runs every gate itself and records it in the ledger
(scripts/ledger.py). When a stage needs an agent, the script opens a
ledger run, writes a *ticket* (agent type + prompt) and pauses. The
parent session launches the agent, then calls `complete`. Agents never
run gates and never decide whether their own work is accepted.

Stop rules -- the cycle HALTS for a human, it never fixes anything:
  * src/ is dirty at start (refuses to begin);
  * a triage verdict is accepted (a confirmed bug needs a human fix);
  * a directed / board / formal / determinism gate fails (triage tooling
    only handles random-seed failures);
  * a red-team candidate is confirmed SURVIVED_WITNESSED (needs a new test
    and a mutant);
  * an agent's ticket is rejected (failed gates or scope violation);
  * the agent-ticket budget is exhausted.

    orchestrate.py start [--root DIR] [--no-redteam] [--reuse-signoff]
                         [--signoff-skip G,G] [--max-tickets N]
    orchestrate.py step              advance until a ticket is needed or the cycle ends
    orchestrate.py ticket            print the pending ticket (agent type + prompt)
    orchestrate.py complete TICKET   gate the agent's output, record it, advance
    orchestrate.py status | abort
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / "orchestrate_state.json"
CYCLES = REPO / "ledger" / "cycles.jsonl"
VENV_ACT = REPO / ".venv" / "bin" / "activate"
LEDGER = [sys.executable, str(REPO / "scripts" / "ledger.py")]
TRIAGE_KEYS = {"classification", "confidence", "culprit_file", "culprit_lines", "check",
               "minimal_repro", "replay_command", "spec_refs", "evidence", "ruled_out", "suggested_fix"}
TRIAGE_CLASSES = {"DUT", "REFERENCE_MODEL", "HARNESS", "SPEC_AMBIGUITY", "NOT_REPRODUCIBLE"}
RANDOM_GATES = ("cov_", "determinism")  # signoff gates whose failures leave failure.json behind


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sh(cmd: str, cwd: Path, timeout: int = 7200) -> tuple[int, str]:
    act = f"source {VENV_ACT} && " if VENV_ACT.exists() else ""
    try:
        r = subprocess.run(["bash", "-c", act + cmd], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s"


def load() -> dict:
    if not STATE.exists():
        sys.exit("[orch] no cycle in progress (orchestrate.py start)")
    return json.loads(STATE.read_text())


def save(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=2) + "\n")


def log(s: dict, msg: str) -> None:
    s["log"].append(f"{now()} {msg}")
    print(f"[orch] {msg}", flush=True)


def halt(s: dict, reason: str) -> None:
    s["stage"], s["halt_reason"] = "halted", reason
    log(s, f"HALT: {reason}")
    close_cycle(s)


# ------------------------------------------------------------------ ledger
def ledger(*args: str) -> tuple[int, str]:
    r = subprocess.run([*LEDGER, *args], cwd=REPO, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def ledger_start(s: dict, agent: str, contract: str, task: str) -> str:
    args = ["start", "--agent", agent, "--contract", contract, "--task", task]
    if Path(s["root"]) != REPO:
        args += ["--tree", s["root"]]
    rc, out = ledger(*args)
    if rc != 0:
        sys.exit(f"[orch] ledger start failed: {out}")
    return out.splitlines()[-1].strip()


def ledger_gate(run_id: str, name: str, cmd: str) -> bool:
    act = f"source {VENV_ACT} && " if VENV_ACT.exists() else ""
    rc, out = ledger("gate", run_id, "--name", name, "--", "bash", "-c", act + cmd)
    return rc == 0 and f"gate {name}: PASS" in out


# ---------------------------------------------------------------- stages
def stage_signoff(s: dict) -> None:
    root = Path(s["root"])
    report = root / "signoff_report.json"
    reuse = False
    if s["opts"]["reuse_signoff"] and report.exists():
        r = json.loads(report.read_text())
        head = sh("git rev-parse --short HEAD", root)[1].strip()
        reuse = r.get("commit") == head and r.get("complete") and not r.get("dirty")
    if reuse:
        log(s, f"signoff: reusing report for {r['commit']}")
    else:
        shutil.rmtree(root / "test" / "regression_artifacts", ignore_errors=True)
        skip = s["opts"]["signoff_skip"]
        log(s, f"signoff: running (skip={skip or 'none'})")
        # caffeinate: the first real cycle's mutation gate took 14.5 h of
        # wall time because the laptop slept through it
        awake = "caffeinate -i " if shutil.which("caffeinate") else ""
        sh(f"{awake}python3 scripts/signoff.py {('--skip ' + skip) if skip else ''}", root)
        r = json.loads(report.read_text())
    s["signoff"] = {k: r[k] for k in ("commit", "dirty", "passed", "complete")}
    s["signoff"]["failed"] = [g for g, v in r["gates"].items() if v["verdict"] == "FAIL"]
    log(s, f"signoff: passed={r['passed']} failed={s['signoff']['failed']}")

    if r["passed"]:
        s["stage"] = "redteam" if s["opts"]["redteam"] else "close"
        return
    non_random = [g for g in s["signoff"]["failed"] if not g.startswith(RANDOM_GATES)]
    failures = sorted((root / "test" / "regression_artifacts").glob("seed_*/failure.json"))
    if not failures:
        halt(s, f"signoff failed in {s['signoff']['failed']} with no random-seed failure to triage")
        return
    for fj in failures:
        f = json.loads(fj.read_text())
        s["queue"].append({"kind": "triage", "seed": f["seed"], "profile": f.get("profile"),
                           "check": f["check"], "rtl": f["rtl"], "golden": f["golden"]})
    if non_random:
        s["pending_halt"] = f"non-random gates also failed: {non_random}"
    s["stage"] = "triage"


def triage_prompt(s: dict, t: dict) -> str:
    prof = f" (stimulus profile {t['profile']})" if t["profile"] else " (default stimulus profile)"
    return f"""Triage a failing random-regression seed found by the orchestrator (cycle {s['cycle']}).

Tree (work ONLY inside it): {s['root']}
Failing seed: {t['seed']}{prof}. Failing check: `{t['check']}` -- RTL={t['rtl']!r} golden={t['golden']!r}.

1. Read your contract triage/AGENT_CONTRACT.md (relative to the tree) in full.
2. Reproduce with scripts/triage.py replay --seed {t['seed']}{(' --profile ' + t['profile']) if t['profile'] else ''},
   minimize with --out {t['report_dir']}/min, then decide the class from the spec with
   discriminating evidence. Run one triage.py command at a time.
3. Write {t['report_dir']}/verdict.json per the contract's report_format. Put the human
   write-up in your final reply as text. Write nothing outside triage/reports/.

cd into the tree in each Bash call; source .venv/bin/activate before python/make."""


def redteam_prompt(s: dict, t: dict) -> str:
    return f"""Red-team pass for orchestrator cycle {s['cycle']} (signoff clean at {s['signoff']['commit']}).

Project: {s['root']}. Read your contract redteam/AGENT_CONTRACT.md in full, then follow your role.
Write candidates (and witnesses) ONLY under {t['cand_dir']}/ -- this cycle's directory.
Budget: at most {t['budget']} evaluated candidates, one eval at a time. Don't resubmit
anything in scripts/mutate.py MUTANTS or in earlier redteam/candidates/ subdirectories.
Never edit src/, test/, formal/, scripts/ or anything else. Source .venv/bin/activate
before python/make. Put your full write-up in your final reply as text."""


def next_ticket(s: dict) -> None:
    if s["budget_left"] <= 0:
        halt(s, "agent-ticket budget exhausted")
        return
    t = s["queue"].pop(0)
    t["id"] = f"T{len(s['tickets']) + 1}"
    if t["kind"] == "triage":
        t["report_dir"] = f"triage/reports/{s['cycle']}_seed{t['seed']}"
        t["agent"], t["contract"] = "triage-debug", "triage/AGENT_CONTRACT.md"
        t["prompt"] = triage_prompt(s, t)
        task = f"orchestrator {s['cycle']}: triage seed {t['seed']} ({t['check']})"
    else:
        t["agent"], t["contract"] = "red-team", "redteam/AGENT_CONTRACT.md"
        t["prompt"] = redteam_prompt(s, t)
        task = f"orchestrator {s['cycle']}: red-team pass"
    t["ledger_run"] = ledger_start(s, t["agent"], t["contract"], task)
    t["status"] = "dispatched"
    t["started"] = time.time()
    s["tickets"].append(t)
    s["budget_left"] -= 1
    s["waiting_on"] = t["id"]
    log(s, f"ticket {t['id']} -> {t['agent']} (ledger {t['ledger_run']}); run `orchestrate.py ticket`")


def step(s: dict) -> None:
    while s["stage"] not in ("halted", "done") and not s.get("waiting_on"):
        if s["stage"] == "signoff":
            stage_signoff(s)
        elif s["stage"] == "triage":
            if s["queue"]:
                next_ticket(s)
            elif s.get("pending_halt"):
                halt(s, s["pending_halt"])
            else:
                s["stage"] = "redteam" if s["opts"]["redteam"] else "close"
        elif s["stage"] == "redteam":
            if not any(t["kind"] == "redteam" for t in s["tickets"]):
                s["queue"].append({"kind": "redteam", "budget": 8,
                                   "cand_dir": f"redteam/candidates/{s['cycle']}"})
                next_ticket(s)
            else:
                s["stage"] = "close"
        elif s["stage"] == "close":
            s["stage"] = "done"
            log(s, "cycle complete")
            close_cycle(s)
        save(s)
    save(s)


# --------------------------------------------------------------- complete
def complete_triage(s: dict, t: dict) -> tuple[bool, str]:
    root = Path(s["root"])
    rid, rd = t["ledger_run"], root / t["report_dir"]
    vj = rd / "verdict.json"
    schema = (f"python3 -c \"import json,sys; v=json.load(open('{vj}')); "
              f"m={sorted(TRIAGE_KEYS)!r}; miss=[k for k in m if k not in v]; "
              f"assert not miss, miss; assert v['classification'] in {sorted(TRIAGE_CLASSES)!r}\"")
    ok = ledger_gate(rid, "verdict-schema", schema)
    if not ok:
        return False, "verdict.json missing or malformed"
    v = json.loads(vj.read_text())
    ok &= ledger_gate(rid, "replay-reproduces",
                      f"cd {root} && {v['replay_command']} | grep -qF '\"check\": \"{v['check']}\"'")
    lo, hi = (v["culprit_lines"] + [0, 0])[:2]
    ok &= ledger_gate(rid, "culprit-lines-exist",
                      f"test -f {root}/{v['culprit_file']} && [ $(wc -l < {root}/{v['culprit_file']}) -ge {hi} ]")
    t["verdict"] = {k: v[k] for k in ("classification", "confidence", "culprit_file", "culprit_lines")}
    return ok, f"{v['classification']} / {v['culprit_file']}:{lo}-{hi} ({v['confidence']})"


def complete_redteam(s: dict, t: dict) -> tuple[bool, str]:
    root = Path(s["root"])
    rid = t["ledger_run"]
    cands = sorted(p for p in (root / t["cand_dir"]).glob("*.json") if not p.name.endswith(".result.json"))
    t["candidates"] = {}
    ok = True
    for c in cands:
        gate_dir = Path("/tmp") / f"orch_{s['cycle']}"
        gate_dir.mkdir(exist_ok=True)
        for f in c.parent.iterdir():  # evaluate a copy, so the agent's own results aren't overwritten
            if f.is_file():
                shutil.copy2(f, gate_dir / f.name)
        gc = gate_dir / c.name
        passed = ledger_gate(rid, f"eval-full-{c.stem}",
                             f"cd {root} && python3 scripts/redteam_eval.py {gc} --full | tail -1")
        res = json.loads(gc.with_suffix(".result.json").read_text()) if gc.with_suffix(".result.json").exists() else {}
        t["candidates"][c.stem] = res.get("verdict", "ERROR")
        ok &= passed and res.get("verdict") not in (None, "INVALID")
    survivors = [k for k, v in t["candidates"].items() if v == "SURVIVED_WITNESSED"]
    t["survivors"] = survivors
    return ok, f"{len(cands)} candidates: {t['candidates']}"


def cmd_complete(a) -> None:
    s = load()
    t = next((x for x in s["tickets"] if x["id"] == a.ticket), None)
    if not t or t["status"] != "dispatched":
        sys.exit(f"[orch] no dispatched ticket {a.ticket}")
    ok, summary = complete_triage(s, t) if t["kind"] == "triage" else complete_redteam(s, t)
    verdict = "accepted" if ok else "rejected"
    rc, out = ledger("finish", t["ledger_run"], "--verdict", verdict,
                     "--reason", f"orchestrator {s['cycle']} {t['id']}: {summary}")
    if rc != 0:  # e.g. scope violation refused 'accepted'
        verdict = "rejected"
        ledger("finish", t["ledger_run"], "--verdict", "rejected",
               "--reason", f"orchestrator {s['cycle']} {t['id']}: {summary}; ledger refused accept: {out[-300:]}")
    t["status"], t["result"], t["summary"] = "done", verdict, summary
    s["waiting_on"] = None
    log(s, f"ticket {t['id']} {verdict}: {summary}")
    if verdict == "rejected":
        halt(s, f"ticket {t['id']} rejected ({summary})")
    elif t["kind"] == "triage":
        v = t["verdict"]
        halt(s, f"confirmed {v['classification']} bug at {v['culprit_file']}:{v['culprit_lines']} "
                f"(seed {t['seed']}) -- needs a human fix")
    elif t.get("survivors"):
        halt(s, f"red-team survivors {t['survivors']} -- need new tests and mutants")
    save(s)
    step(s)


# ------------------------------------------------------------------ misc
def close_cycle(s: dict) -> None:
    CYCLES.parent.mkdir(exist_ok=True)
    rec = {"cycle": s["cycle"], "root": s["root"], "started": s["started"], "finished": now(),
           "outcome": s["stage"], "halt_reason": s.get("halt_reason"), "signoff": s.get("signoff"),
           "tickets": [{k: t.get(k) for k in ("id", "kind", "agent", "ledger_run", "result", "summary",
                                              "survivors")} for t in s["tickets"]]}
    with CYCLES.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    save(s)


def cmd_start(a) -> None:
    if STATE.exists():
        s = json.loads(STATE.read_text())
        if s["stage"] not in ("done", "halted"):
            sys.exit(f"[orch] cycle {s['cycle']} still in progress (status / abort)")
    root = Path(a.root).resolve() if a.root else REPO
    if root == REPO:
        rc, out = sh("git status --porcelain -- src", REPO)
        if out.strip():
            sys.exit("[orch] src/ has uncommitted changes -- refusing to start a cycle")
    cycle = datetime.now(timezone.utc).strftime("C%Y%m%d-%H%M%S")
    s = {"cycle": cycle, "root": str(root), "started": now(), "stage": "signoff",
         "opts": {"redteam": not a.no_redteam, "reuse_signoff": a.reuse_signoff,
                  "signoff_skip": a.signoff_skip},
         "budget_left": a.max_tickets, "queue": [], "tickets": [], "waiting_on": None, "log": []}
    log(s, f"cycle {cycle} started on {root}")
    save(s)
    step(s)


def cmd_step(a) -> None:
    step(load())


def cmd_ticket(a) -> None:
    s = load()
    t = next((x for x in s["tickets"] if x["id"] == s.get("waiting_on")), None)
    if not t:
        print(f"[orch] no pending ticket (stage {s['stage']})")
        return
    print(json.dumps({"ticket": t["id"], "subagent_type": t["agent"], "ledger_run": t["ledger_run"]}))
    print(t["prompt"])


def cmd_status(a) -> None:
    s = load()
    print(f"cycle {s['cycle']}  stage {s['stage']}  root {s['root']}  budget_left {s['budget_left']}")
    if s.get("halt_reason"):
        print(f"halt: {s['halt_reason']}")
    for t in s["tickets"]:
        print(f"  {t['id']} {t['kind']:7s} {t['status']:10s} {t.get('result', ''):9s} {t.get('summary', '')}")
    for line in s["log"][-8:]:
        print("  " + line)


def cmd_abort(a) -> None:
    s = load()
    for t in s["tickets"]:
        if t["status"] == "dispatched":
            ledger("finish", t["ledger_run"], "--verdict", "rejected", "--reason", "cycle aborted")
            t["status"] = "aborted"
    halt(s, "aborted by parent")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start")
    st.add_argument("--root", help="run against another tree (e.g. a triage benchmark copy)")
    st.add_argument("--no-redteam", action="store_true")
    st.add_argument("--reuse-signoff", action="store_true", help="reuse a complete, clean report for HEAD")
    st.add_argument("--signoff-skip", default="", help="passed to signoff.py --skip (testing only)")
    st.add_argument("--max-tickets", type=int, default=3)
    st.set_defaults(fn=cmd_start)
    c = sub.add_parser("complete")
    c.add_argument("ticket")
    c.set_defaults(fn=cmd_complete)
    for name, fn in (("step", cmd_step), ("ticket", cmd_ticket), ("status", cmd_status), ("abort", cmd_abort)):
        sub.add_parser(name).set_defaults(fn=fn)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
