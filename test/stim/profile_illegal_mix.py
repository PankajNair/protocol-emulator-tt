# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""STIM_PROFILE=illegal_mix -- reserved opcodes 19-31 sprinkled into
otherwise-normal programs (test/stim/AGENT_CONTRACT.md requested
profile #3).

Gap this closes: random_gen.py only emits the 19 defined opcodes, so
the "Undefined opcode behavior" path of docs/isa.md (execute as NOP,
set the sticky illegal-opcode flag, cleared only by reset) is never
reached by the random differential test -- opcode_bins.ILLEGAL and
debug_flag_bins.illegal_op are EXPECTED_OPEN.

Real-world condition modelled: a corrupted firmware image -- a bit-rot
or bad-flash word, a garbled boot-stream byte, or execution running
into data. Such words don't have tidy operands, so every reserved word
gets a uniformly random opcode in 19..31 AND uniformly random 11
operand bits. That matters beyond realism: the RTL decodes operand
fields combinationally regardless of opcode, so a reserved word whose
rd/pin/imm bits look like a real instruction's is exactly where a
leaked side effect (a register/pin/flag/timer write gated on the wrong
opcode bits) would hide. The golden model predicts NOP + flag for all
of 19..31 (reference_model_scope), so any such leak is a mismatch.

How: call random_gen.generate_program() UNCHANGED, then overwrite a
small number of its existing LEAF words IN PLACE (same approach as
profile_wide_timing). Program length, every address, every branch /
LOOP / CALL target and the trailing HALT are byte-identical to the
default program, so termination-by-construction is untouched. Words
that are NEVER eligible for replacement:
  - BRANCH (JMP/BEQ/BNE/CALL), LOOP, RET, HALT;
  - the LDI immediately before each LOOP body that seeds that LOOP's
    counter register (replacing it would leave the counter at whatever
    the program last wrote -- possibly 0 or 255 -- no longer a
    constructed 1..8 iteration bound).
A replaced leaf simply stops doing what it did (an LDI no longer sets
its register, a WAIT no longer writes the flag) -- harmless for a
differential test, and a reserved word writes nothing, so it cannot
clobber a protected loop counter either.

Placement (each where the program has an eligible word):
  - one "early" word in the first third of the main body, so the flag
    is usually set before most of the program runs and the remaining
    normal instructions are exercised with the sticky flag already 1;
  - one inside a LOOP body (executed up to 8x -- flag must stay set and
    the NOP must not perturb the LOOP decrement/branch);
  - one inside the CALLed subroutine (executed once per CALL, across
    the CALL/RET return-address path);
  - 0..EXTRA_MAX further main-body words anywhere.
Total is capped at MAX_FRACTION of the program's eligible leaves, so
the program stays mostly normal behaviour.

Determinism: own random.Random(f"stim:illegal_mix:{seed}") for every
choice here; base program comes from random_gen's own seeded rng.
Output depends only on the seed (and the clamp dicts passed through).
"""

from __future__ import annotations

import random

import isa_asm as asm
import random_gen

RESERVED_OPCODES = range(19, 32)
EXTRA_MAX = 3
MAX_FRACTION = 0.12  # of eligible leaf words


def _op(w: int) -> int:
    return (w >> 11) & 0x1F


def reserved_word(rng: random.Random) -> int:
    return (rng.choice(RESERVED_OPCODES) << 11) | rng.getrandbits(11)


def classify(words: list[int]) -> dict[str, list[int]]:
    """Eligible leaf pcs by region: 'main', 'loop', 'sub'."""
    halt_pc = next(i for i, w in enumerate(words) if _op(w) == asm.OP_HALT)
    protected = set()
    loop_body = set()
    for pc, w in enumerate(words[:halt_pc]):
        if _op(w) == asm.OP_LOOP:
            tgt = (w >> 2) & 0x1FF
            protected.add(tgt - 1)  # counter-seeding LDI of this LOOP
            loop_body.update(range(tgt, pc))
    structural = (asm.OP_BRANCH, asm.OP_LOOP, asm.OP_RET, asm.OP_HALT)
    regions = {"main": [], "loop": [], "sub": []}
    for pc, w in enumerate(words):
        if pc == halt_pc or pc in protected or _op(w) in structural or _op(w) >= 19:
            continue
        if pc > halt_pc:
            regions["sub"].append(pc)
        elif pc in loop_body:
            regions["loop"].append(pc)
        else:
            regions["main"].append(pc)
    return regions


def generate_program(seed: int, max_delay_mantissa: dict | None = None,
                     max_wait_mantissa: dict | None = None) -> list[int]:
    words = list(random_gen.generate_program(
        seed, max_delay_mantissa=max_delay_mantissa, max_wait_mantissa=max_wait_mantissa))
    rng = random.Random(f"stim:illegal_mix:{seed}")
    regions = classify(words)
    n_eligible = sum(len(v) for v in regions.values())
    cap = max(1, int(n_eligible * MAX_FRACTION))

    chosen: list[int] = []
    main = regions["main"]
    if main:
        early = [pc for pc in main if pc <= main[len(main) // 3]]
        chosen.append(rng.choice(early))
    for region in ("loop", "sub"):
        if regions[region]:
            chosen.append(rng.choice(regions[region]))
    rest = [pc for pc in main if pc not in chosen]
    rng.shuffle(rest)
    chosen += rest[:rng.randint(0, EXTRA_MAX)]
    chosen = chosen[:cap]

    for pc in chosen:
        words[pc] = reserved_word(rng)

    assert len(words) <= random_gen.PROGRAM_WORD_LIMIT
    return words
