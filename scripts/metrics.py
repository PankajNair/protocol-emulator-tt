#!/usr/bin/env python3
"""
metrics.py -- agent and pipeline metrics, computed on demand from what the
ledger already records (docs/VAL.md section 19). Nothing is stored: the
numbers are derived from ledger/runs.jsonl, ledger/cycles.jsonl,
scripts/mutate.py MUTANTS and redteam/candidates/, so they can't drift
from their sources.

    metrics.py            tables
    metrics.py --json     the same numbers as JSON on stdout

Per agent (live runs only for rates -- backfilled runs record accepted
work only, see section 15):
  runs, acceptance rate, first-pass rate (every gate passed on its first
  attempt: no override, no repeated or re-run gate), overrides, scope
  violations, median minutes per run, findings, and how many distinct
  agent-definition versions (hashes) the live runs used.
Triage: planted-bug benchmark accuracy per round (bench-score gate), and
  how often the agent's replay command reproduced the stated check.
Red team: candidates the parent evaluated, confirmed survivors, yield
  (survivors / candidates), precision (parent-confirmed / agent-claimed
  survivors), and how many survivors are now mutation-gate entries.
Environment: mutation-gate entries by origin; orchestrator cycle outcomes.
"""

import argparse
import importlib.util
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "ledger" / "runs.jsonl"
CYCLES = ROOT / "ledger" / "cycles.jsonl"


def jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def gate_json(g: dict) -> dict | None:
    """The last JSON object printed by a gate command (redteam_eval prints
    its result line), if any."""
    for line in reversed(g.get("tail") or []):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return None
    return None


def first_pass(r: dict) -> bool:
    names = [g["name"] for g in r.get("gates", [])]
    rerun = len(names) != len(set(names)) or any(n.endswith("-fixed") for n in names)
    return bool(names) and all(g["passed"] for g in r["gates"]) and not rerun and not r.get("override")


def agent_metrics(runs: list[dict]) -> list[dict]:
    rows = []
    by_agent = defaultdict(list)
    for r in runs:
        by_agent[r["agent"]].append(r)
    for agent, rs in sorted(by_agent.items()):
        live = [r for r in rs if r.get("source") == "live"]
        versions = Counter(r.get("agent_def_sha") for r in live)
        mins = [r["minutes"] for r in live if r.get("minutes") is not None]
        rows.append({
            "agent": agent, "runs": len(rs), "live": len(live), "backfill": len(rs) - len(live),
            "accepted": sum(r["verdict"] == "accepted" for r in live),
            "acceptance": rate(sum(r["verdict"] == "accepted" for r in live), len(live)),
            "first_pass": rate(sum(first_pass(r) for r in live), len(live)),
            "overrides": sum(bool(r.get("override")) for r in live),
            "scope_flags": sum(bool(r.get("scope_outside") or r.get("scope_protected")) for r in live),
            "median_min": round(statistics.median(mins), 1) if mins else None,
            "findings": sum(len(r.get("bugs_found") or []) for r in rs) + redteam_survivor_count(rs),
            "def_versions": len([v for v in versions if v]),
        })
    return rows


def redteam_parent_verdicts(runs: list[dict]) -> dict[str, str]:
    """candidate id -> verdict of the parent's own --full evaluation."""
    out = {}
    for r in runs:
        if r["agent"] != "red-team":
            continue
        for g in r.get("gates", []):
            if g["name"].startswith(("eval-full-", "parent-full-")):
                j = gate_json(g)
                if j and j.get("id"):
                    out[j["id"]] = j.get("verdict", "ERROR")
    return out


def redteam_survivor_count(rs: list[dict]) -> int:
    # findings for red-team runs that didn't list bugs_found (the
    # orchestrator didn't, before this script existed)
    n = 0
    for r in rs:
        if r["agent"] == "red-team" and not r.get("bugs_found"):
            n += sum(1 for g in r.get("gates", []) if g["name"].startswith(("eval-full-", "parent-full-"))
                     and (gate_json(g) or {}).get("verdict") == "SURVIVED_WITNESSED")
    return n


def redteam_metrics(runs: list[dict], mutants: list[dict]) -> dict:
    parent = redteam_parent_verdicts(runs)
    claimed = {}
    for p in (ROOT / "redteam" / "candidates").rglob("*.result.json"):
        j = json.loads(p.read_text())
        if j.get("id") and not str(j.get("id", "")).startswith("t-"):
            claimed[j["id"]] = j.get("verdict")
    confirmed = [c for c, v in parent.items() if v == "SURVIVED_WITNESSED"]
    agent_survivors = [c for c, v in claimed.items() if v == "SURVIVED_WITNESSED"]
    in_gate = [c for c in confirmed if any(c.split("_")[0] in m.get("bug_ref", "") for m in mutants)]
    return {
        "passes": sum(r["agent"] == "red-team" for r in runs),
        "candidates_evaluated": len(parent),
        "confirmed_survivors": len(confirmed),
        "yield": rate(len(confirmed), len(parent)),
        "agent_claimed": len(agent_survivors),
        "precision": rate(len([c for c in agent_survivors if parent.get(c) == "SURVIVED_WITNESSED"]),
                          len([c for c in agent_survivors if c in parent])),
        "survivors_in_mutation_gate": f"{len(in_gate)}/{len(confirmed)}",
        "verdicts": dict(Counter(parent.values())),
    }


def triage_metrics(runs: list[dict]) -> dict:
    rounds = defaultdict(lambda: [0, 0])
    replays = [0, 0]
    for r in runs:
        if r["agent"] != "triage-debug" or r.get("source") != "live":
            continue
        task = r["task"]
        rnd = "round 2 (scrubbed)" if "round 2" in task else "round 1" if task.startswith("bench") else None
        for g in r.get("gates", []):
            if g["name"] == "bench-score" and rnd:
                rounds[rnd][0] += g["passed"]
                rounds[rnd][1] += 1
            if g["name"].startswith("replay-reproduces") and not g["name"].endswith("-fixed"):
                replays[0] += g["passed"]
                replays[1] += 1
    return {
        "benchmark": {k: f"{v[0]}/{v[1]}" for k, v in sorted(rounds.items())},
        "replay_reproduced": f"{replays[0]}/{replays[1]}",
        # orchestrator tickets on this repo, not on benchmark/test copies
        "real_failures_triaged": sum(1 for r in runs if r["agent"] == "triage-debug"
                                     and "orchestrator" in r["task"] and not r.get("tree")),
    }


def load_mutants() -> list[dict]:
    spec = importlib.util.spec_from_file_location("mutate", ROOT / "scripts" / "mutate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MUTANTS


def env_metrics(mutants: list[dict], cycles: list[dict]) -> dict:
    def origin(m):
        ref = m.get("bug_ref", "").lower()
        if "red-team" in ref or "red team" in ref:
            return "red-team agent"
        if "real bug" in ref or "commit" in ref:
            return "past real bug"
        if "blind" in ref:
            return "blind-mutation survivor"
        return "curated (hazard / hand mutation)"
    real = [c for c in cycles if "scratchpad" not in c.get("root", "") and "/tmp" not in c.get("root", "")]
    return {
        "mutation_gate": len(mutants),
        "mutants_by_origin": dict(Counter(origin(m) for m in mutants)),
        "cycles": len(real),
        "cycle_outcomes": dict(Counter(
            c["outcome"] + (f": {(c.get('halt_reason') or '').split(' ')[0]}" if c.get("halt_reason") else "")
            for c in real)),
        "test_cycles": len(cycles) - len(real),
    }


def rate(n: int, d: int) -> str | None:
    return f"{100 * n / d:.0f}% ({n}/{d})" if d else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    runs, cycles, mutants = jsonl(RUNS), jsonl(CYCLES), load_mutants()
    m = {"agents": agent_metrics(runs), "triage": triage_metrics(runs),
         "redteam": redteam_metrics(runs, mutants), "environment": env_metrics(mutants, cycles)}
    if a.json:
        print(json.dumps(m, indent=2))
        return 0

    cols = ["agent", "runs", "live", "acceptance", "first_pass", "overrides", "scope_flags", "median_min", "findings"]
    print("== agents (rates over live runs)")
    print("  ".join(f"{c:>16}" if i else f"{c:18}" for i, c in enumerate(cols)))
    for r in m["agents"]:
        print("  ".join(f"{str(r[c] if r[c] is not None else '-'):>16}" if i else f"{r[c]:18}" for i, c in enumerate(cols)))
    t = m["triage"]
    print(f"\n== triage   benchmark {t['benchmark']}   replay reproduced {t['replay_reproduced']}   "
          f"real failures triaged {t['real_failures_triaged']}")
    rt = m["redteam"]
    print(f"== red team passes {rt['passes']}   candidates evaluated {rt['candidates_evaluated']}   "
          f"confirmed survivors {rt['confirmed_survivors']}   yield {rt['yield']}   precision {rt['precision']}   "
          f"survivors in mutation gate {rt['survivors_in_mutation_gate']}")
    e = m["environment"]
    print(f"== environment   mutation gate {e['mutation_gate']} entries: {e['mutants_by_origin']}")
    print(f"   orchestrator cycles {e['cycles']} (+{e['test_cycles']} test): {e['cycle_outcomes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
