# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Instruction-level golden reference model for the protocol-emulator
sequencer ISA. A from-scratch Python re-implementation of core.v's
architectural semantics (docs/isa.md is the spec; src/cpu/core.v is
what this predicts), used by test_random.py to differentially test
randomly generated programs against the real RTL: run the same word
stream through both, compare architectural state after every
instruction commit.

Field decoding mirrors src/cpu/isa_defs.v's bit positions exactly (not
re-derived from prose) -- reuses isa_asm's opcode/cond/mode constants
directly rather than redefining them, so the encoder and decoder can
never silently drift apart.

Scope: every opcode random_gen.py emits, which is now all 19 --
NOP, HALT, RET, BRANCH (JMP/BEQ/BNE/CALL), LDI, SET, OUT, IN, SHIFT,
DELAY, WAIT, TESTBIT, OUTB, INB, LOAD, STORE, LOOP, CMP, LOADX.
WAIT/IN/INB depend on live external pin/bus state, not just internal
architectural state -- io_stimulus.py is the scripted-external-
stimulus-timeline this file's header used to call future work; see its
own header for the timing convention (empirically confirmed against
real RTL, not derived from pin_ctrl.v's own registers) and its scoping
choices (protocol pins only, bounded WAIT timeout only). Calling
step() on one of these three without both `io_read` and `abs_cycle`
still raises NotImplementedError immediately -- fail loud on a caller
bug, never silently mis-model.

Self-trust: this model's DELAY cycle-count formula and CALL/RET/LOOP
semantics are independently cross-checked against test.py's own
RTL-proven directed tests in test_golden_model_selfcheck.py, before
this model is ever trusted as a live comparison gate -- see that
file's own header for why (a formal-verification checker earlier in
this project's history silently passed against a buggy DUT because its
ghost model was fit to the DUT's own logic instead of the spec; this
model must not repeat that mistake).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

import isa_asm as asm

TIMEOUT_SHIFT = 5  # isa_defs.v `TIMEOUT_SHIFT -- cycles = mantissa << (exponent*TIMEOUT_SHIFT)
PROGRAM_WORDS = 256  # bytes 0-511, 16-bit words -- docs/isa.md "Data memory"
DATA_BYTES = 512  # bytes 512-1023
_WAIT_HARD_CAP = 2_000_000  # defensive guard, well above WAIT's real max reach (~1,015,808, isa.md) -- catches a broken io_read/stimulus fixture as a loud error, not a silent hang


class IllegalOpcode(Exception):
    """Raised only for an opcode value outside 0-18 -- the random
    generator never emits one (see random_gen.py), so this should never
    actually fire in the differential-testing path; exists so a real
    generator bug surfaces loudly instead of silently mis-modeling."""


@dataclass
class SequencerState:
    pc: int = 0
    regs: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    flag: bool = False
    retaddr: int = 0
    return_valid: bool = False
    illegal_op_flag: bool = False
    call_ret_misuse_flag: bool = False
    halted: bool = False
    uo_out: int = 0
    # data_mem[i] == RTL byte address (asm.DATA_BASE + i), i in 0..511.
    data_mem: list[int] = field(default_factory=lambda: [0] * DATA_BYTES)
    # Sticky per-pin state, indexed by pin_index (0-7) -- mirrors
    # pin_ctrl.v's mode[]/drv[] arrays exactly, including the 3 non-
    # protocol indices (4/5/6) that never actually get SET/OUTB'd by
    # the in-scope random generator but are tracked uniformly anyway,
    # same "one flat table" reasoning pin_ctrl.v itself uses.
    pin_mode: list[int] = field(default_factory=lambda: [asm.SET_MODE_INPUT] * 8)
    pin_drv: list[int] = field(default_factory=lambda: [0] * 8)

    @classmethod
    def reset(cls) -> "SequencerState":
        """docs/architecture.md Pipeline "Reset values" section."""
        return cls()

    def clone(self) -> "SequencerState":
        return deepcopy(self)


def _decode(word: int) -> dict:
    """Every field position mirrored directly from src/cpu/isa_defs.v."""
    return {
        "opcode": (word >> 11) & 0x1F,
        "addr": (word >> 2) & 0x1FF,
        "cond": word & 0x3,
        "rd": (word >> 9) & 0x3,
        "imm": word & 0x1FF,
        "ldi_value": word & 0xFF,
        "pin_index": (word >> 6) & 0x7,
        "aux_bit": (word >> 5) & 0x1,
        "bitidx": word & 0x7,
        "exponent": (word >> 9) & 0x3,
        "delay_mantissa": word & 0x1FF,
        "wait_mantissa": word & 0x1F,
        "loop_rd": word & 0x3,
        "rs": (word >> 7) & 0x3,
        "shift_dir": word & 0x1,
    }


def step(state: SequencerState, word: int, io_read=None, abs_cycle: int | None = None) -> tuple[SequencerState, int]:
    """Executes one instruction. Returns (new_state, cycles) where
    `cycles` is the expected FSM occupancy for this instruction (3 for
    everything except DELAY, which is 3+N -- docs/architecture.md
    Pipeline section, and src/io/cycle_counter.v's header comment for
    why this exact formula, including N=0, needed two separate bugfixes
    to actually be true in the RTL) -- and now WAIT, which is 3+k where
    k is however many extra cycles the live stimulus actually took to
    satisfy the pin condition or time out.

    `io_read(t) -> (ui_in_sync, uio_in_sync)` and `abs_cycle` (the
    absolute cycle number of THIS instruction's own entering_execute --
    i.e. its FETCH_LO-start cycle + 2, FETCH_LO and FETCH_HI always
    being exactly 1 cycle each) are WAIT/IN/INB's extension seam: IN
    samples `io_read(abs_cycle)` once, INB likewise for one bit, WAIT
    walks `io_read(abs_cycle + k)` for k=0,1,2,... until the pin
    condition matches or (for a bounded WAIT) the countdown expires --
    condition-met wins on an exact tie, matching core.v checking the
    condition before the counter (docs/isa.md WAIT row). Omitted on
    purpose for every other opcode; calling step() with WAIT/IN/INB
    and no `io_read`/`abs_cycle` raises NotImplementedError immediately
    rather than silently returning a wrong answer."""
    f = _decode(word)
    op = f["opcode"]
    s = state.clone()

    def _need_io():
        if io_read is None or abs_cycle is None:
            raise NotImplementedError(
                f"opcode {op} (WAIT/IN/INB) needs both io_read and abs_cycle -- "
                "see golden_model.py's step() docstring and io_stimulus.py"
            )

    cycles = 3
    next_pc = (s.pc + 1) & 0x1FF

    if op == asm.OP_NOP:
        pass

    elif op == asm.OP_HALT:
        s.halted = True
        return s, cycles

    elif op == asm.OP_RET:
        next_pc = s.retaddr
        if not s.return_valid:
            s.call_ret_misuse_flag = True
        s.return_valid = False

    elif op == asm.OP_BRANCH:
        cond = f["cond"]
        taken = (
            cond == asm.COND_JMP
            or (cond == asm.COND_BEQ and s.flag)
            or (cond == asm.COND_BNE and not s.flag)
            or cond == asm.COND_CALL
        )
        if cond == asm.COND_CALL:
            if s.return_valid:
                s.call_ret_misuse_flag = True
            s.retaddr = (s.pc + 1) & 0x1FF
            s.return_valid = True
        if taken:
            next_pc = f["addr"]

    elif op == asm.OP_LDI:
        s.regs[f["rd"]] = f["ldi_value"]

    elif op == asm.OP_SET:
        pin = f["pin_index"]
        if f["rd"] != asm.SET_MODE_LEAVE:
            s.pin_mode[pin] = f["rd"]
        s.pin_drv[pin] = f["aux_bit"]

    elif op == asm.OP_OUT:
        s.uo_out = s.regs[f["rd"]]

    elif op == asm.OP_IN:
        _need_io()
        ui_sync, _uio_sync = io_read(abs_cycle)
        s.regs[f["rd"]] = ui_sync

    elif op == asm.OP_SHIFT:
        v = s.regs[f["rd"]]
        if f["shift_dir"] == asm.SHIFT_LEFT:
            s.regs[f["rd"]] = (v << 1) & 0xFF
        else:
            s.regs[f["rd"]] = (v >> 1) & 0xFF

    elif op == asm.OP_DELAY:
        n = f["delay_mantissa"] << (f["exponent"] * TIMEOUT_SHIFT)
        cycles = 3 + n

    elif op == asm.OP_WAIT:
        _need_io()
        pin = f["pin_index"]
        level = f["aux_bit"]
        # core.v: wait_has_timeout = (w_wait_mant != 5'd0) -- exponent
        # never matters for the has-timeout question, since a nonzero
        # mantissa shifted left by any amount is never 0, and a zero
        # mantissa is 0 regardless of exponent (isa.md WAIT row's
        # "any encoding whose computed cycles is 0" is just this fact
        # stated more generally).
        has_timeout = f["wait_mantissa"] != 0
        target = (f["wait_mantissa"] << (f["exponent"] * TIMEOUT_SHIFT)) if has_timeout else None
        k = 0
        while True:
            if k > _WAIT_HARD_CAP:
                raise RuntimeError(
                    f"WAIT never resolved within {_WAIT_HARD_CAP} cycles (has_timeout={has_timeout}) -- "
                    "broken io_read/stimulus, not a real unbounded wait"
                )
            _ui_sync, uio_sync = io_read(abs_cycle + k)
            met = ((uio_sync >> pin) & 1) == level
            expired = has_timeout and (k >= target)
            if met or expired:
                s.flag = not met  # isa.md WAIT row: 0 if met, 1 if timed out; met wins the tie
                cycles = 3 + k
                break
            k += 1

    elif op == asm.OP_TESTBIT:
        s.flag = bool((s.regs[f["rd"]] >> f["bitidx"]) & 1)

    elif op == asm.OP_OUTB:
        pin = f["pin_index"]
        bit = (s.regs[f["rd"]] >> 7) & 1 if f["aux_bit"] == asm.BITSEL_BIT7 else s.regs[f["rd"]] & 1
        s.pin_drv[pin] = bit

    elif op == asm.OP_INB:
        _need_io()
        _ui_sync, uio_sync = io_read(abs_cycle)
        pin = f["pin_index"]
        bit = (uio_sync >> pin) & 1
        rd = f["rd"]
        if f["aux_bit"] == asm.BITSEL_BIT7:
            s.regs[rd] = (bit << 7) | (s.regs[rd] & 0x7F)
        else:
            s.regs[rd] = (s.regs[rd] & 0xFE) | bit

    elif op == asm.OP_LOAD:
        s.regs[f["rd"]] = s.data_mem[f["imm"]]

    elif op == asm.OP_STORE:
        s.data_mem[f["imm"]] = s.regs[f["rd"]]

    elif op == asm.OP_LOOP:
        rd = f["loop_rd"]
        result = (s.regs[rd] - 1) & 0xFF
        s.regs[rd] = result
        if result != 0:
            next_pc = f["addr"]

    elif op == asm.OP_CMP:
        s.flag = s.regs[f["rd"]] == s.regs[f["rs"]]

    elif op == asm.OP_LOADX:
        s.regs[f["rd"]] = s.data_mem[s.regs[f["rs"]]]

    elif 19 <= op <= 31:
        s.illegal_op_flag = True

    else:
        raise IllegalOpcode(f"unhandled opcode {op} for word {word:#06x}")

    s.pc = next_pc
    return s, cycles
