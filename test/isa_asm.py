# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Minimal Python instruction encoder for the protocol-emulator sequencer
ISA, mirroring src/cpu/isa_defs.v exactly (opcode values, field
positions, cond/mode values) -- that file is the single source of truth;
if these two ever disagree, isa_defs.v wins and this file is wrong.

Not a real assembler (no labels/symbols) -- test programs compute branch
targets as plain word-index integers by hand, same as writing raw
machine code. Good enough for test.py's directed unit tests; a real
assembler (firmware/isa/) is separate, unstarted work.
"""

OP_NOP = 0
OP_HALT = 1
OP_RET = 2
OP_BRANCH = 3
OP_LDI = 4
OP_SET = 5
OP_OUT = 6
OP_IN = 7
OP_SHIFT = 8
OP_DELAY = 9
OP_WAIT = 10
OP_TESTBIT = 11
OP_OUTB = 12
OP_INB = 13
OP_LOAD = 14
OP_STORE = 15
OP_LOOP = 16
OP_CMP = 17
OP_LOADX = 18

COND_JMP = 0b00
COND_BEQ = 0b01
COND_BNE = 0b10
COND_CALL = 0b11

SET_MODE_LEAVE = 0b00
SET_MODE_PUSH_PULL = 0b01
SET_MODE_OPEN_DRAIN = 0b10
SET_MODE_INPUT = 0b11

SHIFT_LEFT = 0
SHIFT_RIGHT = 1

BITSEL_BIT0 = 0
BITSEL_BIT7 = 1

DATA_BASE = 512


def _word(opcode: int, rest11: int) -> int:
    return ((opcode & 0x1F) << 11) | (rest11 & 0x7FF)


def nop() -> int:
    return _word(OP_NOP, 0)


def halt() -> int:
    return _word(OP_HALT, 0)


def ret() -> int:
    return _word(OP_RET, 0)


def raw(opcode: int) -> int:
    """Any opcode value with an all-zero operand field -- used to test
    unassigned opcodes (19-31) decode as NOP."""
    return _word(opcode, 0)


def branch(cond: int, addr: int) -> int:
    rest = ((addr & 0x1FF) << 2) | (cond & 0x3)
    return _word(OP_BRANCH, rest)


def jmp(addr: int) -> int:
    return branch(COND_JMP, addr)


def beq(addr: int) -> int:
    return branch(COND_BEQ, addr)


def bne(addr: int) -> int:
    return branch(COND_BNE, addr)


def call(addr: int) -> int:
    return branch(COND_CALL, addr)


def ldi(rd: int, value: int) -> int:
    rest = ((rd & 0x3) << 9) | (value & 0xFF)
    return _word(OP_LDI, rest)


def set_pin(mode: int, pin_index: int, value: int) -> int:
    imm = ((pin_index & 0x7) << 6) | ((value & 0x1) << 5)
    rest = ((mode & 0x3) << 9) | imm
    return _word(OP_SET, rest)


def out(rd: int) -> int:
    return _word(OP_OUT, (rd & 0x3) << 9)


def in_(rd: int) -> int:
    return _word(OP_IN, (rd & 0x3) << 9)


def shift(rd: int, direction: int) -> int:
    rest = ((rd & 0x3) << 9) | (direction & 0x1)
    return _word(OP_SHIFT, rest)


def delay(mantissa: int, exponent: int) -> int:
    rest = ((exponent & 0x3) << 9) | (mantissa & 0x1FF)
    return _word(OP_DELAY, rest)


def wait_(pin_index: int, level: int, mantissa: int = 0, exponent: int = 0) -> int:
    imm = ((pin_index & 0x7) << 6) | ((level & 0x1) << 5) | (mantissa & 0x1F)
    rest = ((exponent & 0x3) << 9) | imm
    return _word(OP_WAIT, rest)


def testbit(rd: int, bitidx: int) -> int:
    rest = ((rd & 0x3) << 9) | (bitidx & 0x7)
    return _word(OP_TESTBIT, rest)


def outb(rd: int, pin_index: int, bitsel: int) -> int:
    imm = ((pin_index & 0x7) << 6) | ((bitsel & 0x1) << 5)
    rest = ((rd & 0x3) << 9) | imm
    return _word(OP_OUTB, rest)


def inb(rd: int, pin_index: int, bitsel: int) -> int:
    imm = ((pin_index & 0x7) << 6) | ((bitsel & 0x1) << 5)
    rest = ((rd & 0x3) << 9) | imm
    return _word(OP_INB, rest)


def load(rd: int, imm: int) -> int:
    rest = ((rd & 0x3) << 9) | (imm & 0x1FF)
    return _word(OP_LOAD, rest)


def store(rd: int, imm: int) -> int:
    rest = ((rd & 0x3) << 9) | (imm & 0x1FF)
    return _word(OP_STORE, rest)


def loop_(addr: int, rd: int) -> int:
    rest = ((addr & 0x1FF) << 2) | (rd & 0x3)
    return _word(OP_LOOP, rest)


def cmp_(rd: int, rs: int) -> int:
    rest = ((rd & 0x3) << 9) | ((rs & 0x3) << 7)
    return _word(OP_CMP, rest)


def loadx(rd: int, rs: int) -> int:
    rest = ((rd & 0x3) << 9) | ((rs & 0x3) << 7)
    return _word(OP_LOADX, rest)


def assemble(entries: list) -> list[int]:
    """entries: list of (label_or_None, word_or_callable). Each entry is
    exactly one instruction (no pseudo-ops), so an entry's list index is
    its word address -- two-pass: pass 1 records label->address, pass 2
    resolves any callable(labels_dict) -> int entries (for forward or
    backward branch targets) into plain words. Exists so branch/loop/
    call test programs reference symbolic targets instead of hand-
    computed word indices, which is exactly the kind of arithmetic
    that's easy to get wrong (and easy to get wrong *silently* -- an
    off-by-one branch target still assembles and still runs, it just
    tests the wrong thing)."""
    labels = {}
    for i, (label, _word) in enumerate(entries):
        if label is not None:
            if label in labels:
                raise ValueError(f"duplicate label {label!r}")
            labels[label] = i
    return [w(labels) if callable(w) else w for _label, w in entries]


def to_bytes(words: list[int]) -> list[int]:
    """Boot-stream byte order: low byte of instruction 0 first, matching
    FETCH_LO/FETCH_HI's own PC*2/PC*2+1 order (docs/architecture.md
    Pipeline LOAD section)."""
    out_bytes = []
    for w in words:
        out_bytes.append(w & 0xFF)
        out_bytes.append((w >> 8) & 0xFF)
    return out_bytes
