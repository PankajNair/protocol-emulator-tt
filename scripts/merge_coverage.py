#!/usr/bin/env python3
"""Merge per-seed coverage JSON (test/seq_coverage.py) and report bin
closure. Port of the sibling 5-Stage-Pipelined-RISC-V-Processor
project's ooo/scripts/merge_coverage.py.

Usage:
    merge_coverage.py [COV_DIR]   # default $COV_DIR or /tmp/seq_coverage

Exit 0 always -- informational, not a gate (mutation score is the gate,
see scripts/mutate.py).

EXPECTED_OPEN lists bins test/random_gen.py structurally cannot reach,
each for a stated reason -- reported apart from real gaps so closure %
isn't polluted either way. A bin leaving this list means the generator
changed; a bin needing to join it means someone should explain why.
"""

import glob
import json
import os
import sys

EXPECTED_OPEN = {
    # Default generator never emits reserved opcodes 19-31; test.py's
    # test_illegal_opcode* cover NOP-fallthrough + sticky flag, and
    # STIM_PROFILE=illegal_mix reaches this bin and debug_flag_bins.illegal_op.
    "opcode_bins": {"ILLEGAL"},
    # random_gen.py makes both CALL/RET misuse structurally unreachable
    # (single leaf subroutine, RET only via CALL) and never emits
    # illegal opcodes; test.py's directed misuse/illegal tests cover these.
    "debug_flag_bins": {"illegal_op", "nested_call", "orphan_ret"},
    # WAIT always gets a mandatory nonzero timeout (random_gen.py header:
    # unbounded WAIT against random stimulus has no termination bound).
    "wait_bins": {"unbounded"},
    # Default mantissa clamps only cover exponents 0/1 (keeps a default
    # regression's cycle cost bounded; exponent 3 reaches ~16.7M cycles).
    # Reachable via STIM_PROFILE=wide_timing (delay exp 3 also needs
    # MAX_CYCLES=50000) -- expected-open for the DEFAULT generator only.
    "delay_exp_bins": {"2", "3"},
    "wait_exp_bins": {"2", "3"},
    # LOOP counters are always seeded by LDI 1-8 and protected from body
    # writes (forbid_write_reg), so Rd=0 on LOOP entry never happens.
    "loop_bins": {"wrap_from_0"},
    # Default generator is flat-only: LOOP/CALL/branches never nest in a
    # LOOP body (random_gen.py header). Target of STIM_PROFILE=nested_flow.
    "loop_nest_bins": {"depth2", "depth3+", "branch_in_loop", "call_in_loop"},
}

META_KEYS = {"seed", "commit_count"}


def load_files(cov_dir: str) -> list[dict]:
    records = []
    for p in sorted(glob.glob(os.path.join(cov_dir, "seed_*.json"))):
        with open(p) as fh:
            records.append(json.load(fh))
    return records


def merge(records: list[dict]) -> dict:
    agg: dict = {"seeds": len(records), "commit_count": 0}
    for d in records:
        agg["commit_count"] += d.get("commit_count", 0)
        for section, bins in d.items():
            if section in META_KEYS:
                continue
            tgt = agg.setdefault(section, {})
            for k, v in bins.items():
                tgt[k] = tgt.get(k, 0) + v
    return agg


def print_section(section: str, bins: dict) -> tuple[list[str], list[str]]:
    hit = sum(1 for v in bins.values() if v > 0)
    print(f"\n{section} ({hit}/{len(bins)} hit):")
    real_open, expected_open = [], []
    expected = EXPECTED_OPEN.get(section, set())
    for k, v in bins.items():
        tag = "HIT     " if v > 0 else ("OPEN(ok)" if k in expected else "OPEN    ")
        print(f"  [{tag}] {k:<18} {v:>8}")
        if v == 0:
            (expected_open if k in expected else real_open).append(f"{section}.{k}")
    return real_open, expected_open


def main() -> None:
    cov_dir = sys.argv[1] if len(sys.argv) >= 2 else os.environ.get("COV_DIR", "/tmp/seq_coverage")
    records = load_files(cov_dir)
    if not records:
        print(f"No coverage files in {cov_dir} -- nothing to merge.")
        return

    agg = merge(records)
    W = 64
    print(f"\n{'=' * W}")
    print(f"Coverage Closure Report -- {agg['seeds']} seeds, {agg['commit_count']} commits")
    print("=" * W)

    real_open: list[str] = []
    expected_open: list[str] = []
    total = hit = 0
    for section, bins in agg.items():
        if section in META_KEYS or section == "seeds":
            continue
        ro, eo = print_section(section, bins)
        real_open += ro
        expected_open += eo
        total += len(bins)
        hit += sum(1 for v in bins.values() if v > 0)

    real_total = total - len(expected_open)
    print(f"\n{'=' * W}")
    print(f"TOTAL: {hit}/{total} bins hit ({100 * hit // total if total else 0}%)")
    print(f"REAL (excluding expected-open): {hit}/{real_total} ({100 * hit // real_total if real_total else 0}%)")
    if real_open:
        print(f"Open (unexpected -- investigate): {', '.join(real_open)}")
    if expected_open:
        print(f"Open (expected -- see EXPECTED_OPEN): {', '.join(expected_open)}")
    print("=" * W)


if __name__ == "__main__":
    main()
