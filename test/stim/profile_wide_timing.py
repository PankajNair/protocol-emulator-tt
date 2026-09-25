# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""STIM_PROFILE=wide_timing -- DELAY/WAIT exponents 2 and 3
(test/stim/AGENT_CONTRACT.md requested profile #2).

Gap this closes: random_gen.py's default mantissa clamps only have keys
for exponents 0/1, so the shared DELAY/WAIT counter's 1024-cycle
(exponent 2) and 32768-cycle (exponent 3) granularities are never
decoded. Those are the ranges real firmware reaches for once a wait is
longer than ~16K cycles (~330us @ 50MHz): device settle/execution delays
(e.g. a character-LCD command's 37us-1.5ms, sensor conversion time,
EEPROM write cycle), inter-frame gaps, and ms-scale protocol timeouts
(`WAIT SCL,1,timeout` against I2C clock stretch, SMBus's 25-35ms,
`WAIT` on a slow host/peripheral response) -- docs/isa.md DELAY/WAIT
rows.

How: call random_gen.generate_program() UNCHANGED (same seed, same
clamps the harness passes), then rewrite a small, bounded number of its
existing DELAY/WAIT words IN PLACE -- only the exponent/mantissa fields
change, WAIT's pin/level are kept. In-place rewriting was chosen over
passing wider clamp dicts to random_gen because (a) random_gen draws
the exponent uniformly per DELAY/WAIT, so a wider dict gives an
unbounded-by-count number of wide instructions, including inside
LOOP bodies (x up to 8) and the CALLed subroutine (x up to 3) -- no
per-program cycle bound; (b) an in-place rewrite of one leaf timing op
into another cannot touch any termination-by-construction rule:
program length, every address, branch/LOOP/CALL targets, LOOP-counter
protection and the trailing HALT are all byte-identical to the
default program. DELAY touches no register/flag; WAIT stays a WAIT
with the same mandatory nonzero timeout (mantissa >= 1). Only when a
program has no DELAY (resp. WAIT) word at all, one NOP is converted
into one -- NOP->DELAY is side-effect-free; NOP->WAIT adds a flag
write, which can only redirect FORWARD branches, never create a cycle.

Cycle budget (the real design constraint). A static execution-count
upper bound is computed per word: 1 in the main body, the LOOP's
LDI-seeded count inside a LOOP body, the number of CALL sites inside
the subroutine. Wide-DELAY cycles are charged against
    budget = MAX_CYCLES - BASELINE_RESERVE
(BASELINE_RESERVE = 6000; the default generator's own run length is
54..4292 cycles over seeds 1-300, measured with golden_model +
IoStimulus). At the default MAX_CYCLES=20000 that leaves 14000 cycles:
1-2 exponent-2 DELAYs with small mantissas (1..EXP2_MANTISSA_CAP=4,
i.e. ~20-80us settle delays; total capped at 8192 cycles), charged by
execution count, so a DELAY inside an 8x LOOP only gets rewritten if 8*m*1024 still fits.
Exponent-3 DELAY (>= 32768 cycles) CANNOT fit the default budget; it is
emitted only when MAX_CYCLES >= BASELINE_RESERVE + 32768 (+ exp-2
room), i.e. run with MAX_CYCLES=50000 to exercise it (on EXP3_PROB of
seeds, main-body-only, mantissa 1). MAX_CYCLES is read from the
environment (same variable test_random.py reads) because the seam does
not pass it to generate_program -- see report.

WAIT exponents 2/3 get mantissa 1..31 (~20us..20ms timeouts). Their
cost is stimulus-driven, not timeout-driven: under the default 50/50
per-cycle IoStimulus a WAIT resolves in a few cycles; running past
MAX_CYCLES would need ~15000 consecutive wrong-level samples
(probability ~2^-15000). Termination itself is still structural (every
timeout nonzero and <= 1,015,808 cycles). NOTE: pairing this with a
level-holding stimulus (e.g. a stuck line) would make wide WAITs time
out for real and needs a much bigger MAX_CYCLES.

Determinism: own random.Random(f"stim:wide_timing:{seed}") for the
rewrite choices; the base program comes from random_gen's own seeded
rng. Output depends only on (seed, MAX_CYCLES env).
"""

from __future__ import annotations

import os
import random

import isa_asm as asm
import random_gen

BASELINE_RESERVE = 6000
EXP2_UNIT = 1 << 10  # mantissa << (2*5), docs/isa.md DELAY row
EXP3_UNIT = 1 << 15
EXP2_MANTISSA_CAP = 4
EXP2_TOTAL_CAP = 8 * EXP2_UNIT  # keeps regression wall-time sane even when MAX_CYCLES is raised
MAX_WIDE_DELAYS = 2
MAX_WIDE_WAITS = 3
EXP3_PROB = 0.5
WAIT_MANTISSA_MAX = 31  # 5-bit field


def _op(w: int) -> int:
    return (w >> 11) & 0x1F


def _exec_bounds(words: list[int]) -> list[int]:
    """Static upper bound on how many times each word executes (0 for
    unreachable-by-construction padding never occurs here)."""
    halt_pc = next(i for i, w in enumerate(words) if _op(w) == asm.OP_HALT)
    n_calls = sum(1 for w in words[:halt_pc]
                  if _op(w) == asm.OP_BRANCH and (w & 0x3) == asm.COND_CALL)
    mult = [1] * len(words)
    for pc in range(halt_pc + 1, len(words)):
        mult[pc] = n_calls  # leaf-only subroutine
    for pc, w in enumerate(words[:halt_pc]):
        if _op(w) != asm.OP_LOOP:
            continue
        tgt, reg = (w >> 2) & 0x1FF, w & 0x3
        count = 8
        seed_w = words[tgt - 1] if tgt >= 1 else None
        if seed_w is not None and _op(seed_w) == asm.OP_LDI and ((seed_w >> 9) & 0x3) == reg:
            count = max(1, seed_w & 0xFF)
        for b in range(tgt, pc):
            mult[b] = max(mult[b], count)
    return mult


def generate_program(seed: int, max_delay_mantissa: dict | None = None,
                     max_wait_mantissa: dict | None = None) -> list[int]:
    words = list(random_gen.generate_program(
        seed, max_delay_mantissa=max_delay_mantissa, max_wait_mantissa=max_wait_mantissa))
    rng = random.Random(f"stim:wide_timing:{seed}")
    max_cycles = int(os.environ.get("MAX_CYCLES", "20000"))
    budget = max(0, max_cycles - BASELINE_RESERVE)
    mult = _exec_bounds(words)
    halt_pc = next(i for i, w in enumerate(words) if _op(w) == asm.OP_HALT)
    nops = [pc for pc in range(halt_pc) if _op(words[pc]) == asm.OP_NOP and mult[pc] == 1]
    rng.shuffle(nops)

    # --- DELAY: charged against the cycle budget by execution count ---
    delays = [pc for pc, w in enumerate(words) if _op(w) == asm.OP_DELAY and mult[pc] > 0]
    if not delays and nops:
        delays = [nops.pop()]  # NOP -> DELAY: no register/flag side effects either way
    rng.shuffle(delays)
    delays.sort(key=lambda pc: mult[pc])  # prefer straight-line code; stable, so shuffle order breaks ties
    if budget >= EXP3_UNIT and delays and mult[delays[0]] == 1 and rng.random() < EXP3_PROB:
        pc = delays.pop(0)
        words[pc] = asm.delay(1, 3)
        budget -= EXP3_UNIT
    exp2_budget = min(budget, EXP2_TOTAL_CAP)
    n_wide = rng.randint(1, MAX_WIDE_DELAYS)
    for pc in delays:
        if n_wide == 0:
            break
        cap = min(EXP2_MANTISSA_CAP, exp2_budget // (EXP2_UNIT * mult[pc]))
        if cap < 1:
            continue
        m = rng.randint(1, cap)
        words[pc] = asm.delay(m, 2)
        exp2_budget -= m * EXP2_UNIT * mult[pc]
        n_wide -= 1

    # --- WAIT: timeout cost is stimulus-driven, see module header ---
    waits = [pc for pc, w in enumerate(words) if _op(w) == asm.OP_WAIT and mult[pc] > 0]
    if not waits and nops:
        pc = nops.pop()
        words[pc] = asm.wait_(rng.choice(random_gen.PROTOCOL_PIN_INDICES), rng.randint(0, 1), mantissa=1)
        waits = [pc]
    rng.shuffle(waits)
    for pc in waits[:rng.randint(1, MAX_WIDE_WAITS)]:
        w = words[pc]
        pin, level = (w >> 6) & 0x7, (w >> 5) & 0x1
        words[pc] = asm.wait_(pin, level, mantissa=rng.randint(1, WAIT_MANTISSA_MAX), exponent=rng.choice((2, 3)))

    assert len(words) <= random_gen.PROGRAM_WORD_LIMIT
    return words
