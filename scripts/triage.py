#!/usr/bin/env python3
"""
triage.py -- deterministic tooling for turning a failing random seed into
evidence (docs/VAL.md section 16). The triage-debug agent drives it; it
never decides anything itself.

    triage.py replay   --seed N [--profile P] [--program FILE] [--root DIR]
        Run one seed (optionally a fixed program) and print the failure
        record (test/regression_artifacts/seed_N/failure.json) or PASS.

    triage.py minimize --seed N [--profile P] [--root DIR] [--out DIR]
        Delta-debug the failing program down to the fewest instructions
        that still fail the SAME check. Instructions are replaced with NOP
        rather than deleted, so addresses and branch targets don't move.
        Candidates the golden model says won't halt within MAX_CYCLES are
        skipped without simulating. Writes minimal.txt (full program),
        minimal_core.txt (non-NOP lines only) and failure.json to --out.

    triage.py bisect   --seed N --good SHA [--bad REV] [--program FILE]
        Keep today's test/ harness and model, swap in src/ from each commit
        in good..bad, and find the first RTL commit where the failure
        appears. If it also fails with the --good src/, the RTL history
        isn't the cause -- the failure follows the harness/model.

All runs happen against --root (default: this repo); bisect uses a
throwaway git worktree, so the main tree's src/ is never touched.
"""

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_ACT = ROOT / ".venv" / "bin" / "activate"
DEFAULT_MAX_CYCLES = 20000


def run_seed(root: Path, seed: int, profile: str | None, program: Path | None, max_cycles: int) -> dict | None:
    """One seed through the RTL/golden lockstep. Returns failure.json's
    contents, or None on pass. Holds a per-tree lock: two runs in one tree
    share sim_build/ and regression_artifacts/seed_N/, so overlapping runs
    (e.g. a minimize still going while a replay starts) read each other's
    results -- seen once as a minimizer "result" that then passed."""
    lock = open(root / "test" / ".triage.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[triage] another triage run holds {root}/test -- waiting", file=sys.stderr, flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        return _run_seed(root, seed, profile, program, max_cycles)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _run_seed(root: Path, seed: int, profile: str | None, program: Path | None, max_cycles: int) -> dict | None:
    art = root / "test" / "regression_artifacts" / f"seed_{seed}"
    shutil.rmtree(art, ignore_errors=True)
    env = dict(os.environ, SEEDS="1", SEED_BASE=str(seed), MAX_CYCLES=str(max_cycles),
               COV_DIR=tempfile.gettempdir() + "/triage_cov")
    env.pop("PROGRAM_FILE", None)
    env.pop("STIM_PROFILE", None)
    if profile:
        env["STIM_PROFILE"] = profile
    if program:
        env["PROGRAM_FILE"] = str(program.resolve())
    act = f"source {VENV_ACT} && " if VENV_ACT.exists() and "VIRTUAL_ENV" not in os.environ else ""
    r = subprocess.run(["bash", "-c", f"{act}make -C test random"], cwd=root, env=env,
                       capture_output=True, text=True, timeout=600)
    out = r.stdout + r.stderr
    fj = art / "failure.json"
    if fj.exists():
        return json.loads(fj.read_text())
    if "FAIL=0" in out and "ALL 1 SEEDS PASSED" in out:
        return None
    # failed without a failure record: build/infra problem, not a DUT verdict
    tail = "\n".join(out.strip().splitlines()[-30:])
    raise RuntimeError(f"seed {seed} neither passed nor produced failure.json:\n{tail}")


def generate(root: Path, seed: int, profile: str | None) -> list[int]:
    code = (
        "import sys, os, json; sys.path.insert(0, 'test')\n"
        "import importlib, random_gen\n"
        f"p = {profile!r}\n"
        "gen = random_gen.generate_program\n"
        "if p:\n"
        "    m = importlib.import_module('stim.profile_' + p)\n"
        "    gen = getattr(m, 'generate_program', None) or gen\n"
        f"print(json.dumps(gen({seed}, max_delay_mantissa={{0: 63, 1: 15}}, max_wait_mantissa={{0: 31, 1: 15}})))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def write_program(path: Path, words: list[int], header: str) -> None:
    sys.path.insert(0, str(ROOT / "test"))
    import random_gen
    path.write_text(f"# {header}\n" + random_gen.words_to_text(words) + "\n")


def golden_halts(root: Path, words: list[int], seed: int, profile: str | None, max_cycles: int) -> bool:
    """Cheap pre-filter: does the golden model halt within budget? The
    boot-exit cycle is approximated, so this only screens out candidates
    that obviously hang (e.g. a LOOP whose LDI counter was NOPed). Uses
    --root's own model, so a run against a modified tree stays consistent."""
    sys.path.insert(0, str(root / "test"))
    import importlib
    from golden_model import SequencerState, step
    from io_stimulus import IoStimulus
    mk = IoStimulus
    if profile:
        m = importlib.import_module(f"stim.profile_{profile}")
        mk = getattr(m, "make_stimulus", None) or IoStimulus
    stim = mk(seed, 3, max_cycle=max_cycles + 32)
    s, cycle = SequencerState.reset(), 3
    try:
        while cycle < max_cycles * 0.9:
            word = words[s.pc] if s.pc < len(words) else 0
            s, n = step(s, word, io_read=stim.io_read, abs_cycle=cycle + 2)
            cycle += n
            if s.halted:
                return True
    except Exception:
        return True  # let the real run decide
    return False


# ------------------------------------------------------------------ replay
def cmd_replay(a) -> int:
    f = run_seed(Path(a.root), a.seed, a.profile, Path(a.program) if a.program else None, a.max_cycles)
    if f is None:
        print(f"[triage] seed {a.seed}: PASS")
        return 0
    print(json.dumps(f, indent=2))
    return 1


# ---------------------------------------------------------------- minimize
def cmd_minimize(a) -> int:
    root = Path(a.root)
    out = Path(a.out or root / "test" / "regression_artifacts" / f"seed_{a.seed}_min")
    out.mkdir(parents=True, exist_ok=True)
    words = generate(root, a.seed, a.profile)
    first = run_seed(root, a.seed, a.profile, None, a.max_cycles)
    if first is None:
        print(f"[triage] seed {a.seed} passes -- nothing to minimize")
        return 2
    signature = first["check"]
    print(f"[triage] seed {a.seed}: {len(words)} words, fails on '{signature}'", flush=True)

    cand_file = out / "candidate.txt"
    stats = {"sim_runs": 1, "golden_skips": 0}

    def build(keep: set[int]) -> list[int]:
        return [w if i in keep else 0 for i, w in enumerate(words)]

    def fails(keep: set[int]) -> bool:
        prog = build(keep)
        if not golden_halts(root, prog, a.seed, a.profile, a.max_cycles):
            stats["golden_skips"] += 1
            return False
        write_program(cand_file, prog, f"candidate, seed={a.seed}")
        stats["sim_runs"] += 1
        f = run_seed(root, a.seed, a.profile, cand_file, a.max_cycles)
        return f is not None and f["check"] == signature

    # ddmin (Zeller) over the indices of non-NOP words
    items = [i for i, w in enumerate(words) if w != 0]
    n = 2
    while len(items) >= 2:
        chunk = max(1, len(items) // n)
        subsets = [items[i:i + chunk] for i in range(0, len(items), chunk)]
        reduced = False
        for s in subsets:                         # try a subset alone
            if fails(set(s)):
                items, n, reduced = s, 2, True
                break
        if not reduced:
            for s in subsets:                     # try removing a subset
                comp = [i for i in items if i not in s]
                if comp and fails(set(comp)):
                    items, n, reduced = comp, max(n - 1, 2), True
                    break
        if not reduced:
            if n >= len(items):
                break
            n = min(len(items), n * 2)
        print(f"[triage]   {len(items)} instructions left ({stats['sim_runs']} sims, "
              f"{stats['golden_skips']} golden skips)", flush=True)

    final = build(set(items))
    write_program(out / "minimal.txt", final, f"minimal repro, seed={a.seed}, profile={a.profile}, check={signature}")
    sys.path.insert(0, str(ROOT / "test"))
    import random_gen
    core = [f"{i:3d}: {w:#06x}  {random_gen.disassemble(w)}" for i, w in enumerate(final) if w != 0]
    (out / "minimal_core.txt").write_text("\n".join(core) + "\n")
    f = run_seed(root, a.seed, a.profile, out / "minimal.txt", a.max_cycles)
    if f is None:
        raise RuntimeError("minimal program passes on re-run although it failed during reduction -- "
                           "nondeterminism (check for another simulation in this tree)")
    (out / "failure.json").write_text(json.dumps(f, indent=2) + "\n")
    cand_file.unlink(missing_ok=True)
    summary = {"seed": a.seed, "profile": a.profile, "check": signature,
               "original_instructions": len([w for w in words if w]), "minimal_instructions": len(items),
               **stats, "out": str(out)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("\n".join(core))
    print(f"[triage] {summary['original_instructions']} -> {len(items)} instructions, "
          f"{stats['sim_runs']} sims; written to {out}")
    return 0


# ------------------------------------------------------------------ bisect
def cmd_bisect(a) -> int:
    git = lambda *x, cwd=ROOT: subprocess.run(["git", *x], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()
    bad = git("rev-parse", a.bad)
    commits = git("rev-list", "--reverse", "--ancestry-path", f"{a.good}..{bad}", "--", "src").split()
    tmp = Path(tempfile.mkdtemp(prefix="triage_bisect_"))
    wt = tmp / "wt"
    git("worktree", "add", "--detach", str(wt), "HEAD")
    try:
        # today's test/ (including uncommitted harness changes) + candidate src/
        shutil.rmtree(wt / "test")
        shutil.copytree(ROOT / "test", wt / "test", ignore=shutil.ignore_patterns("sim_build", "__pycache__", "regression_artifacts", "results*.xml"))
        if a.program:
            program = Path(a.program).resolve()
        else:
            program = tmp / "program.txt"
            write_program(program, generate(ROOT, a.seed, a.profile), f"seed={a.seed}")

        def fails_at(sha: str) -> bool:
            git("checkout", sha, "--", "src", cwd=wt)
            shutil.rmtree(wt / "test" / "sim_build", ignore_errors=True)
            f = run_seed(wt, a.seed, a.profile, program, a.max_cycles)
            print(f"[triage]   src@{sha[:7]}: {'FAIL ' + f['check'] if f else 'pass'}", flush=True)
            return f is not None

        if fails_at(git("rev-parse", a.good)):
            print("[triage] fails with the --good commit's src/ too: not an RTL regression in this range "
                  "(look at the harness/model, or pick an older --good)")
            return 3
        if not fails_at(bad):
            print("[triage] passes with the --bad commit's src/: failure isn't caused by src/ alone")
            return 4
        lo, hi = -1, len(commits) - 1     # commits[hi] fails; good (index -1) passes
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if fails_at(commits[mid]):
                hi = mid
            else:
                lo = mid
        first = commits[hi]
        print(f"[triage] first failing src/ commit: {git('log', '-1', '--format=%h %s', first)}")
        print(git("show", "--stat", "--format=", first, "--", "src"))
        return 0
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("replay", cmd_replay), ("minimize", cmd_minimize), ("bisect", cmd_bisect)):
        p = sub.add_parser(name)
        p.add_argument("--seed", type=int, required=True)
        p.add_argument("--profile")
        p.add_argument("--max-cycles", type=int, default=DEFAULT_MAX_CYCLES)
        p.set_defaults(fn=fn)
        if name in ("replay", "bisect"):
            p.add_argument("--program")
        if name in ("replay", "minimize"):
            p.add_argument("--root", default=str(ROOT))
        if name == "minimize":
            p.add_argument("--out")
        if name == "bisect":
            p.add_argument("--good", required=True)
            p.add_argument("--bad", default="HEAD")
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
