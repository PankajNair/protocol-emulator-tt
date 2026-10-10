#!/usr/bin/env python3
"""
signoff.py -- the executable definition of "the verification environment
is stable" (docs/VAL.md section 14). Runs every gate, records each one's
verdict and key numbers, writes signoff_report.json, exits non-zero if any
gate fails.

Gates:
  directed      make -C test -B            all directed + UART end-to-end tests pass
  pytest        golden-model self-check + assembler unit tests pass
  board         make -C test board          loopback, two-chip link, I2C, SPI pass
  cov_default   make coverage-gate (100 seeds), default generator closure
  cov_<profile> make coverage-gate per stimulus profile (50 seeds)
  determinism   same random seeds twice -> byte-identical coverage results
  formal        make formal, every props file PASS
  vacuity       make vacuity, 100% of covers reached
  mutation      scripts/mutate.py, score >= threshold, nothing errored

Not covered here (run separately, recorded in docs/VAL.md): GDS hardening,
precheck and gate-level simulation (the TinyTapeout `gds` workflow).

Usage:
    signoff.py [--skip GATE[,GATE...]] [--mutate-seeds N]
Run from the repo root with cocotb on PATH (e.g. the .venv activated).
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "signoff_report.json"
PROFILES = ["protocol_pins", "wide_timing", "illegal_mix", "nested_flow"]


def sh(cmd: str, timeout: int) -> tuple[int, str]:
    try:
        r = subprocess.run(["bash", "-c", cmd], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out + f"\nTIMEOUT after {timeout}s"


def cocotb_totals(out: str):
    m = re.findall(r"TESTS=(\d+) PASS=(\d+) FAIL=(\d+) SKIP=(\d+)", out)
    return [dict(zip(("tests", "pass", "fail", "skip"), map(int, x))) for x in m]


def gate_cocotb(cmd, timeout):
    rc, out = sh(cmd, timeout)
    tot = cocotb_totals(out)
    ok = rc == 0 and bool(tot) and all(t["fail"] == 0 for t in tot) and "failure" not in _results_xml()
    return ok, {"totals": tot}, out


def _results_xml():
    p = ROOT / "test" / "results.xml"
    return p.read_text() if p.exists() else ""


def gate_pytest(timeout):
    rc, out = sh("cd test && python3 -m pytest test_golden_model_selfcheck.py test_assembler.py -q", timeout)
    m = re.search(r"(\d+) passed", out)
    failed = re.search(r"(\d+) failed", out)
    return rc == 0 and not failed, {"passed": int(m.group(1)) if m else 0}, out


def gate_coverage(profile, seeds, timeout):
    cmd = f"make coverage-gate COV_SEEDS={seeds}" + (f" STIM_PROFILE={profile}" if profile else "")
    rc, out = sh(cmd, timeout)
    real = re.search(r"REAL \(excluding expected-open\): (\d+)/(\d+)", out)
    verdict = re.search(r"COVERAGE GATE (PASS|FAIL)", out)
    info = {"seeds": seeds, "real_bins": f"{real.group(1)}/{real.group(2)}" if real else None}
    return rc == 0 and verdict is not None and verdict.group(1) == "PASS", info, out


def gate_determinism(timeout):
    """Same seeds, two runs: coverage JSON must be byte-identical. A
    difference means hidden nondeterminism (time, dict order, global RNG)
    that would make a failing seed irreproducible."""
    dirs = ["/tmp/seq_cov_det_a", "/tmp/seq_cov_det_b"]
    outs = []
    for d in dirs:
        rc, out = sh(f"rm -rf {d} && COV_DIR={d} SEEDS=25 make -C test random", timeout)
        if rc != 0:
            return False, {"error": "random run failed"}, out
        outs.append(out)
    diffs = []
    for f in sorted(Path(dirs[0]).glob("seed_*.json")):
        g = Path(dirs[1]) / f.name
        if not g.exists() or f.read_bytes() != g.read_bytes():
            diffs.append(f.name)
    n = len(list(Path(dirs[0]).glob("seed_*.json")))
    return n > 0 and not diffs, {"seeds_compared": n, "differing": diffs}, "\n".join(outs)


def gate_formal(timeout):
    rc, out = sh("make formal", timeout)
    m = re.search(r"\[run_formal\] (PASS|FAIL) \((\d+) props file", out)
    return rc == 0 and m is not None and m.group(1) == "PASS", {"files": int(m.group(2)) if m else 0}, out


def gate_vacuity(timeout):
    rc, out = sh("make vacuity", timeout)
    m = re.findall(r"(\d+)/(\d+) covers reached", out)
    reached = sum(int(a) for a, _ in m)
    total = sum(int(b) for _, b in m)
    ok = rc == 0 and total > 0 and reached == total and "FAIL" not in out
    return ok, {"covers": f"{reached}/{total}"}, out


def gate_mutation(seeds, timeout):
    rc, out = sh(f"python3 scripts/mutate.py --seeds {seeds}", timeout)
    rep = ROOT / "mutation_results.json"
    info = {}
    if rep.exists():
        r = json.loads(rep.read_text())
        info = {k: r[k] for k in ("killed", "survived", "errored", "score", "gate_pass")}
    return rc == 0 and info.get("gate_pass", False), info, out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip", default="", help="comma-separated gates to skip")
    ap.add_argument("--mutate-seeds", type=int, default=50)
    args = ap.parse_args()
    skip = {s for s in args.skip.split(",") if s}

    gates = [
        ("directed", lambda: gate_cocotb("make -C test -B", 1200)),
        ("pytest", lambda: gate_pytest(300)),
        ("board", lambda: gate_cocotb("make -C test board", 1800)),
        ("cov_default", lambda: gate_coverage("", 100, 1800)),
        *[(f"cov_{p}", (lambda p=p: gate_coverage(p, 50, 1800))) for p in PROFILES],
        ("determinism", lambda: gate_determinism(900)),
        ("formal", lambda: gate_formal(1800)),
        ("vacuity", lambda: gate_vacuity(1800)),
        ("mutation", lambda: gate_mutation(args.mutate_seeds, 5400)),
    ]

    rc, gitout = sh("git rev-parse --short HEAD && git status --porcelain -- src test scripts formal firmware", 30)
    git = gitout.split("\n")
    if rc != 0:  # a copy without history (e.g. a triage benchmark tree): never a sign-off
        report = {"commit": "no-git", "dirty": True, "gates": {}}
    else:
        report = {"commit": git[0].strip(), "dirty": any(l.strip() for l in git[1:]), "gates": {}}
    for name, fn in gates:
        if name in skip:
            report["gates"][name] = {"verdict": "SKIPPED"}
            print(f"[signoff] {name:22s} SKIPPED", flush=True)
            continue
        t0 = time.time()
        ok, info, out = fn()
        dt = round(time.time() - t0)
        report["gates"][name] = {"verdict": "PASS" if ok else "FAIL", "seconds": dt, **info}
        print(f"[signoff] {name:22s} {'PASS' if ok else 'FAIL'}  {dt:5d}s  {json.dumps(info)}", flush=True)
        if not ok:
            tail = "\n".join(out.strip().splitlines()[-25:])
            print(f"--- {name} output (tail) ---\n{tail}\n---", flush=True)

    ran = [g for g in report["gates"].values() if g["verdict"] != "SKIPPED"]
    report["passed"] = all(g["verdict"] == "PASS" for g in ran)
    report["complete"] = len(ran) == len(gates)
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    verdict = "STABLE" if report["passed"] and report["complete"] and not report["dirty"] else \
              ("PASS (partial or dirty tree -- not a sign-off)" if report["passed"] else "FAIL")
    print(f"\n[signoff] {verdict}  commit {report['commit']}  -> {REPORT.name}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
