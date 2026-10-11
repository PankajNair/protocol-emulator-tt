#!/usr/bin/env python3
"""
nversion.py -- differential fuzz of two independent ISA models
(docs/VAL.md section 20): test/golden_model.py against the spec-only
Rust model in model_rs/ (written by the model-builder agent without
seeing the Python one), on identical programs and identical
synchronized pin stimulus. No simulator is involved, so this compares
instruction semantics only. The RTL is compared separately, by
test_random.py against the Python model.

    nversion.py [--programs N] [--seed-base S] [--profile P] [--max-cycles C]
                [--stop-after K] [--out DIR]

Programs come from the project's generator (random_gen, or a stimulus
profile), so they obey its legality rules. Stimulus is per-seed and
mixes random toggling with long held levels, so WAITs are met early,
met late and time out.
Every divergence is reported once per (field, opcode) signature and
saved with a repro (input line plus both outputs) under --out. Exit 1
if any divergence is found.
"""

import argparse
import importlib
import json
import random
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "test"))
import isa_asm as asm  # noqa: E402
import random_gen  # noqa: E402
from golden_model import SequencerState, step  # noqa: E402

BIN = ROOT / "model_rs" / "target" / "release" / "model_rs"
STATE_KEYS = ["pc", "regs", "flag", "retaddr", "return_valid", "illegal_op_flag",
              "call_ret_misuse_flag", "halted", "uo_out", "pin_mode", "pin_drv"]


def make_stimulus(seed: int, n: int) -> tuple[list[int], list[int]]:
    """Synchronized per-cycle values: alternating stretches of random
    toggling and held levels (the kind protocol_pins produces), so WAIT
    exercises met-now, met-later and timeout."""
    rng = random.Random(f"nversion:{seed}")
    ui, uio = [], []
    while len(ui) < n:
        run = rng.choice([1, 2, 3, 8, 40, 300, 2000])
        if rng.random() < 0.5:
            u, v = rng.randrange(256), rng.randrange(256)
            ui += [u] * run
            uio += [v] * run
        else:
            ui += [rng.randrange(256) for _ in range(run)]
            uio += [rng.randrange(256) for _ in range(run)]
    return ui[:n], uio[:n]


def gen_program(seed: int, profile: str | None) -> list[int]:
    gen = random_gen.generate_program
    if profile:
        mod = importlib.import_module(f"stim.profile_{profile}")
        gen = getattr(mod, "generate_program", None) or gen
    return gen(seed, max_delay_mantissa={0: 63, 1: 15}, max_wait_mantissa={0: 31, 1: 15})


def run_python(image: list[int], ui: list[int], uio: list[int], max_cycles: int) -> dict:
    def word_at(pc):
        a = (2 * pc) % 1024
        return image[a] | (image[(a + 1) % 1024] << 8)  # isa_asm.to_bytes order

    def io_read(t):
        return (ui[t] if t < len(ui) else 0, uio[t] if t < len(uio) else 0)

    s, cycle, commits = SequencerState.reset(), 0, []
    data0 = list(image[512:])
    s.data_mem = data0
    while not s.halted:
        pc, w = s.pc, word_at(s.pc)
        nxt, n = step(s, w, io_read=io_read, abs_cycle=cycle + 2)
        if cycle + n > max_cycles:  # INTERFACE.md: an instruction ending past max_cycles doesn't commit
            break
        s, cycle = nxt, cycle + n
        commits.append({"pc": pc, "word": w, "cycles": n, "state": state_of(s)})
    final = state_of(s)
    final["data_mem"] = list(s.data_mem)
    return {"commits": commits, "final": final, "halted": bool(s.halted), "timeout": not s.halted}


def state_of(s) -> dict:
    return {"pc": s.pc, "regs": list(s.regs), "flag": int(s.flag), "retaddr": s.retaddr,
            "return_valid": int(s.return_valid), "illegal_op_flag": int(s.illegal_op_flag),
            "call_ret_misuse_flag": int(s.call_ret_misuse_flag), "halted": int(s.halted),
            "uo_out": s.uo_out, "pin_mode": list(s.pin_mode), "pin_drv": list(s.pin_drv)}


def first_divergence(py: dict, rs: dict) -> dict | None:
    if rs.get("error"):
        return {"field": "rust error", "detail": rs["error"], "index": None}
    for i, (a, b) in enumerate(zip(py["commits"], rs.get("commits", []))):
        for k in ("pc", "word", "cycles"):
            if a[k] != b.get(k):
                return {"field": k, "index": i, "word": a["word"], "py": a[k], "rs": b.get(k)}
        for k in STATE_KEYS:
            av, bv = a["state"][k], b.get("state", {}).get(k)
            if k == "pin_mode" and isinstance(bv, list):  # fixed-role pins 4/5/6: not observable
                av = [m for i, m in enumerate(av) if i not in (4, 5, 6)]
                bv = [m for i, m in enumerate(bv) if i not in (4, 5, 6)]
            if k == "pin_drv" and isinstance(bv, list):
                # A protocol pin's drive value only reaches the pad while it is
                # push-pull or open-drain. In input mode the spec says SET's value
                # is ignored and OUTB is a no-op; the RTL and the Python model
                # still latch it, but every path back to an output writes a fresh
                # value, so the latch is never observable.
                # Pin 4 (HOST_GO) is a hardwired input, so it never drives
                # either; pins 5/6 are hardwired outputs and always compared.
                mode = a["state"]["pin_mode"]

                def seen(i):
                    return i in (5, 6) or (i != 4 and mode[i] in (asm.SET_MODE_PUSH_PULL, asm.SET_MODE_OPEN_DRAIN))
                av = [d if seen(i) else None for i, d in enumerate(av)]
                bv = [d if seen(i) else None for i, d in enumerate(bv)]
            if av != bv:
                return {"field": k, "index": i, "word": a["word"], "py": a["state"][k], "rs": b["state"].get(k)}
    if len(py["commits"]) != len(rs.get("commits", [])):
        n = min(len(py["commits"]), len(rs.get("commits", [])))
        return {"field": "commit count", "index": n, "word": None,
                "py": len(py["commits"]), "rs": len(rs.get("commits", []))}
    if py["final"]["data_mem"] != rs["final"].get("data_mem"):
        diffs = [i for i, (x, y) in enumerate(zip(py["final"]["data_mem"], rs["final"]["data_mem"])) if x != y]
        return {"field": "data_mem", "index": None, "word": None, "py": diffs[:8], "rs": "differs"}
    if py["halted"] != rs.get("halted") or py["timeout"] != rs.get("timeout"):
        return {"field": "halted/timeout", "index": None, "word": None,
                "py": [py["halted"], py["timeout"]], "rs": [rs.get("halted"), rs.get("timeout")]}
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--programs", type=int, default=2000)
    ap.add_argument("--seed-base", type=int, default=1)
    ap.add_argument("--profile")
    ap.add_argument("--max-cycles", type=int, default=20000)
    ap.add_argument("--stop-after", type=int, default=20, help="stop after this many distinct signatures")
    ap.add_argument("--out", default=str(ROOT / "nversion_out"))
    a = ap.parse_args()
    if not BIN.exists():
        sys.exit(f"[nversion] {BIN} missing -- cd model_rs && cargo build --release")
    out = Path(a.out)
    out.mkdir(exist_ok=True)

    sigs: "OrderedDict[tuple, dict]" = OrderedDict()
    checked = commits = 0
    batch = 200
    for start in range(a.seed_base, a.seed_base + a.programs, batch):
        seeds = list(range(start, min(start + batch, a.seed_base + a.programs)))
        lines, pys = [], []
        for sd in seeds:
            words = gen_program(sd, a.profile)
            image = (asm.to_bytes(words) + [0] * 1024)[:1024]
            ui, uio = make_stimulus(sd, a.max_cycles + 64)
            inp = {"id": f"s{sd}", "image": image, "ui": ui, "uio": uio, "max_cycles": a.max_cycles}
            lines.append(json.dumps(inp))
            pys.append(run_python(image, ui, uio, a.max_cycles))
        r = subprocess.run([str(BIN)], input="\n".join(lines) + "\n", capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"[nversion] model_rs exited {r.returncode}: {r.stderr[-800:]}")
        outs = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
        if len(outs) != len(seeds):
            sys.exit(f"[nversion] model_rs answered {len(outs)} lines for {len(seeds)} inputs")
        for sd, inp, py, rs in zip(seeds, lines, pys, outs):
            checked += 1
            commits += len(py["commits"])
            d = first_divergence(py, rs)
            if not d:
                continue
            op = (d["word"] >> 11) & 0x1F if isinstance(d.get("word"), int) else None
            key = (d["field"], op)
            if key not in sigs:
                d.update(seed=sd, opcode=op, asm=random_gen.disassemble(d["word"]) if op is not None else None)
                sigs[key] = d
                (out / f"div_{len(sigs)}_s{sd}.json").write_text(json.dumps(
                    {"divergence": d, "input": json.loads(inp), "python": py, "rust": rs}) + "\n")
                print(f"[nversion] DIVERGENCE {len(sigs)}: seed {sd} commit {d['index']} {d.get('asm')} "
                      f"field={d['field']} py={d['py']} rs={d['rs']}", flush=True)
        if len(sigs) >= a.stop_after:
            break
    print(f"[nversion] {checked} programs, {commits} commits compared, {len(sigs)} distinct divergence(s)"
          f"{' (repros in ' + str(out) + ')' if sigs else ''}")
    return 1 if sigs else 0


if __name__ == "__main__":
    sys.exit(main())
