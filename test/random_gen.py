# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Random program generator for the protocol-emulator sequencer ISA,
for differential testing against golden_model.py (test_random.py).

Scope matches golden_model.py exactly: only opcodes whose behavior
depends purely on internal architectural state are emitted -- WAIT/IN/
INB are never generated (they need live external pin/bus state the
golden model doesn't model yet, see golden_model.py's header).

Termination is guaranteed BY CONSTRUCTION, not by chance, so a
MAX_CYCLES trip in test_random.py always means a real bug (RTL hang,
golden-model bug, or a bug in this file), never an expected timeout:
  - JMP/BEQ/BNE targets are always strictly forward (never create a
    cycle).
  - LOOP is only ever emitted as the fixed pattern LDI Rk,C (C=1-8) +
    a small leaf-only body + LOOP body_start,Rk -- guarantees exactly
    C iterations, never open-ended.
  - CALL always targets the one shared, leaf-only subroutine appended
    after the main body, which only ever ends in RET -- a second CALL
    can never fire while the first is still pending (return_valid
    can't already be set when the only place that sets it, the single
    CALL site currently executing, hasn't itself resolved yet), so the
    nested-call hazard (docs/isa.md Branch format section) is
    structurally unreachable here, not just avoided by low probability.
  - Program always ends with a literal HALT.

Deliberately NOT supported in v1 (kept flat/non-nested to avoid a
combinatorial explosion of interaction cases): branches, LOOP, and
CALL never appear nested inside a LOOP body or inside the shared
subroutine -- both are leaf-instruction-only. A future version could
relax this once the flat case is proven.

Every seed uses its own random.Random(seed) instance, never the global
random module -- generate_program(seed) is byte-identical every call,
so a failing seed is trivially reproducible standalone:
    python3 test/random_gen.py --seed 137
"""

from __future__ import annotations

import random

import isa_asm as asm

DEFAULT_MIN_BLOCKS = 40
DEFAULT_MAX_BLOCKS = 100
DEFAULT_MAX_LOOP_SITES = 4
DEFAULT_MAX_CALL_SITES = 3
DEFAULT_BRANCH_PROB = 0.12
DEFAULT_LOOP_PROB = 0.04
DEFAULT_CALL_PROB = 0.03
# exponent=0/1 only by default -- keeps a default regression's DELAY
# cost bounded (max 63 and 15*32=480 extra cycles respectively) rather
# than occasionally drawing near the ISA's real ~16.7M-cycle ceiling.
# Override for an occasional heavier run.
DEFAULT_MAX_DELAY_MANTISSA = {0: 63, 1: 15}

PROGRAM_WORD_LIMIT = 256  # docs/isa.md "Data memory" -- program region is bytes 0-511 = 256 words

_LEAF_OP_POOL = (
    ["LDI"] * 3 + ["OUT"] * 2
    + ["SET", "OUTB", "SHIFT", "DELAY", "TESTBIT", "CMP", "LOAD", "STORE", "LOADX", "NOP"]
)

_BLOCK_LEAF = "leaf"
_BLOCK_BRANCH = "branch"
_BLOCK_LOOP = "loop"
_BLOCK_CALL = "call"


def _reg(rng: random.Random, forbid_write: int | None) -> int:
    """A register index 0-3, excluding `forbid_write` when given (used
    for a loop body's register-WRITING opcodes only, see
    _gen_leaf_word's forbid_write_reg parameter)."""
    if forbid_write is None:
        return rng.randint(0, 3)
    choices = [r for r in range(4) if r != forbid_write]
    return rng.choice(choices)


def _gen_leaf_word(rng: random.Random, max_delay_mantissa: dict, forbid_write_reg: int | None = None) -> int:
    """`forbid_write_reg`, when given, excludes that register from every
    register-WRITING opcode's destination (LDI/SHIFT/LOAD/LOADX) --
    used only when generating a LOOP body, to protect the loop's own
    counter register from being clobbered mid-loop. Found the hard way:
    an earlier version had no such protection, and SHIFT (or LDI/LOAD/
    LOADX) landing on the loop's counter register inside its own body
    silently undid LOOP's decrement every pass, making the "exactly C
    iterations, guaranteed by construction" claim false -- confirmed via
    a real hang (seed 4, R3 as both loop counter and a body SHIFT's
    destination) before this fix. Read-only uses (CMP, STORE reading a
    register, TESTBIT, OUTB, the loop-index register itself via LOOP)
    are unaffected -- only opcodes that WRITE a GPR need excluding."""
    op = rng.choice(_LEAF_OP_POOL)
    if op == "LDI":
        return asm.ldi(_reg(rng, forbid_write_reg), rng.randint(0, 255))
    if op == "OUT":
        return asm.out(rng.randint(0, 3))
    if op == "SET":
        mode = rng.choices(
            [asm.SET_MODE_LEAVE, asm.SET_MODE_PUSH_PULL, asm.SET_MODE_OPEN_DRAIN, asm.SET_MODE_INPUT],
            weights=[1, 3, 3, 3],
        )[0]
        return asm.set_pin(mode, rng.randint(0, 7), rng.randint(0, 1))
    if op == "OUTB":
        return asm.outb(rng.randint(0, 3), rng.randint(0, 7), rng.choice([asm.BITSEL_BIT0, asm.BITSEL_BIT7]))
    if op == "SHIFT":
        return asm.shift(_reg(rng, forbid_write_reg), rng.choice([asm.SHIFT_LEFT, asm.SHIFT_RIGHT]))
    if op == "DELAY":
        exponent = rng.choice(sorted(max_delay_mantissa.keys()))
        return asm.delay(rng.randint(0, max_delay_mantissa[exponent]), exponent)
    if op == "TESTBIT":
        return asm.testbit(rng.randint(0, 3), rng.randint(0, 7))
    if op == "CMP":
        return asm.cmp_(rng.randint(0, 3), rng.randint(0, 3))
    if op == "LOAD":
        return asm.load(_reg(rng, forbid_write_reg), rng.randint(0, 511))
    if op == "STORE":
        return asm.store(rng.randint(0, 3), rng.randint(0, 511))
    if op == "LOADX":
        return asm.loadx(_reg(rng, forbid_write_reg), rng.randint(0, 3))
    if op == "NOP":
        return asm.nop()
    raise AssertionError(f"unhandled leaf op {op!r}")


def generate_program(
    seed: int,
    min_blocks: int = DEFAULT_MIN_BLOCKS,
    max_blocks: int = DEFAULT_MAX_BLOCKS,
    max_loop_sites: int = DEFAULT_MAX_LOOP_SITES,
    max_call_sites: int = DEFAULT_MAX_CALL_SITES,
    branch_prob: float = DEFAULT_BRANCH_PROB,
    loop_prob: float = DEFAULT_LOOP_PROB,
    call_prob: float = DEFAULT_CALL_PROB,
    max_delay_mantissa: dict | None = None,
) -> list[int]:
    """Returns a list of 16-bit instruction words. Deterministic in
    `seed` alone (own random.Random instance, no global state)."""
    rng = random.Random(seed)
    max_delay_mantissa = max_delay_mantissa or DEFAULT_MAX_DELAY_MANTISSA
    n_blocks = rng.randint(min_blocks, max_blocks)

    # Pass 1: decide the abstract block-type sequence and every forward
    # branch's target BLOCK index, without emitting any real
    # instructions yet -- see module docstring for why this two-pass
    # split is what makes forward-reference scheduling simple instead
    # of needing incremental gap-collision bookkeeping.
    block_types = []
    branch_targets = {}  # block_index -> target_block_index (always > block_index)
    loop_sites = 0
    call_sites = 0

    for i in range(n_blocks):
        r = rng.random()
        if r < branch_prob:
            gap = rng.randint(1, 15)
            target = min(i + gap, n_blocks - 1)
            if target <= i:
                target = min(i + 1, n_blocks - 1)
            cond = rng.choice([asm.COND_JMP, asm.COND_BEQ, asm.COND_BNE])
            block_types.append({"kind": _BLOCK_BRANCH, "cond": cond})
            if target > i:
                branch_targets[i] = target
            else:
                block_types[-1] = {"kind": _BLOCK_LEAF}  # n_blocks==1 edge case, no room to branch forward
        elif loop_sites < max_loop_sites and r < branch_prob + loop_prob:
            block_types.append({
                "kind": _BLOCK_LOOP,
                "count": rng.randint(1, 8),
                "body_len": rng.randint(2, 6),
                "reg": rng.randint(0, 3),
            })
            loop_sites += 1
        elif call_sites < max_call_sites and r < branch_prob + loop_prob + call_prob:
            block_types.append({"kind": _BLOCK_CALL})
            call_sites += 1
        else:
            block_types.append({"kind": _BLOCK_LEAF})

    target_labels = {}
    for tgt in branch_targets.values():
        target_labels.setdefault(tgt, f"L{tgt}")

    # Pass 2: expand each block into real isa_asm entries.
    entries = []
    for i, bt in enumerate(block_types):
        label = target_labels.get(i)
        kind = bt["kind"]
        if kind == _BLOCK_LEAF:
            entries.append((label, _gen_leaf_word(rng, max_delay_mantissa)))
        elif kind == _BLOCK_BRANCH:
            cond = bt["cond"]
            tgt_label = target_labels[branch_targets[i]]
            entries.append((label, (lambda L, cond=cond, tgt_label=tgt_label: asm.branch(cond, L[tgt_label]))))
        elif kind == _BLOCK_LOOP:
            reg, count, body_len = bt["reg"], bt["count"], bt["body_len"]
            loop_label = f"loopbody_{i}"
            entries.append((label, asm.ldi(reg, count)))
            for j in range(body_len):
                entries.append((loop_label if j == 0 else None,
                                 _gen_leaf_word(rng, max_delay_mantissa, forbid_write_reg=reg)))
            entries.append((None, (lambda L, loop_label=loop_label, reg=reg: asm.loop_(L[loop_label], reg))))
        elif kind == _BLOCK_CALL:
            entries.append((label, (lambda L: asm.call(L["sub_start"]))))
        else:
            raise AssertionError(f"unhandled block kind {kind!r}")

    entries.append((None, asm.halt()))

    if call_sites > 0:
        sub_len = rng.randint(3, 10)
        entries.append(("sub_start", _gen_leaf_word(rng, max_delay_mantissa)))
        for _ in range(sub_len - 1):
            entries.append((None, _gen_leaf_word(rng, max_delay_mantissa)))
        entries.append((None, asm.ret()))

    words = asm.assemble(entries)
    assert len(words) <= PROGRAM_WORD_LIMIT, f"seed {seed}: program has {len(words)} words, limit {PROGRAM_WORD_LIMIT}"
    return words


_OPCODE_NAMES = {
    asm.OP_NOP: "NOP", asm.OP_HALT: "HALT", asm.OP_RET: "RET", asm.OP_BRANCH: "BRANCH",
    asm.OP_LDI: "LDI", asm.OP_SET: "SET", asm.OP_OUT: "OUT", asm.OP_IN: "IN",
    asm.OP_SHIFT: "SHIFT", asm.OP_DELAY: "DELAY", asm.OP_WAIT: "WAIT", asm.OP_TESTBIT: "TESTBIT",
    asm.OP_OUTB: "OUTB", asm.OP_INB: "INB", asm.OP_LOAD: "LOAD", asm.OP_STORE: "STORE",
    asm.OP_LOOP: "LOOP", asm.OP_CMP: "CMP", asm.OP_LOADX: "LOADX",
}


def disassemble(word: int) -> str:
    """Coarse debug-only disassembly (opcode name + raw operand field
    bits) -- not a real disassembler, just enough for failure-artifact
    dumps and the CLI below to be human-scannable."""
    opcode = (word >> 11) & 0x1F
    name = _OPCODE_NAMES.get(opcode, f"ILLEGAL({opcode})")
    operand = word & 0x7FF
    return f"{name} operand={operand:#05x}"


def words_to_text(words: list[int]) -> str:
    lines = [f"{i:3d}: {w:#06x}  {disassemble(w)}" for i, w in enumerate(words)]
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, required=True)
    args = ap.parse_args()
    prog = generate_program(args.seed)
    print(f"# seed={args.seed}, {len(prog)} words")
    print(words_to_text(prog))
