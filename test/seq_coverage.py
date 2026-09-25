# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Functional coverage model for the sequencer ISA, sampled once per
committed instruction by test_random.py. Mirrors the sibling
5-Stage-Pipelined-RISC-V-Processor project's ooo/dv/env/coverage.py
(RvfiCoverage): flat, pre-declared bin dicts (so a never-hit bin still
shows up as 0, not as a missing key), dumped as plain JSON per seed and
merged by scripts/merge_coverage.py.

Samples golden-model pre/post state, not RTL internals -- test_random.py
has already proven the two identical at every commit before sample()
runs, so this is equivalent and keeps the model simulator-free.

Every bin is a proxy for a real hardware condition named in docs/isa.md
or docs/architecture.md, not an arbitrary encoding split -- see each
group's comment. Bins the current generator structurally cannot reach
are listed in merge_coverage.py's EXPECTED_OPEN with the reason.
"""

from __future__ import annotations

import json
import os

import isa_asm as asm
from golden_model import TIMEOUT_SHIFT, _decode

COV_DIR = os.environ.get("COV_DIR", "/tmp/seq_coverage")

OPCODE_NAMES = {
    asm.OP_NOP: "NOP", asm.OP_HALT: "HALT", asm.OP_RET: "RET", asm.OP_BRANCH: "BRANCH",
    asm.OP_LDI: "LDI", asm.OP_SET: "SET", asm.OP_OUT: "OUT", asm.OP_IN: "IN",
    asm.OP_SHIFT: "SHIFT", asm.OP_DELAY: "DELAY", asm.OP_WAIT: "WAIT", asm.OP_TESTBIT: "TESTBIT",
    asm.OP_OUTB: "OUTB", asm.OP_INB: "INB", asm.OP_LOAD: "LOAD", asm.OP_STORE: "STORE",
    asm.OP_LOOP: "LOOP", asm.OP_CMP: "CMP", asm.OP_LOADX: "LOADX",
}
PROTOCOL_PINS = (0, 1, 2, 3, 7)
SET_MODE_NAMES = {asm.SET_MODE_LEAVE: "leave", asm.SET_MODE_PUSH_PULL: "pp",
                  asm.SET_MODE_OPEN_DRAIN: "od", asm.SET_MODE_INPUT: "in"}


def _regs_read(d: dict) -> set[int]:
    """GPRs an instruction reads in EXECUTE -- for the LOAD/LOADX
    trailing-writeback hazard bin (architecture.md: the write lands in
    the NEXT instruction's FETCH_LO, before that instruction reads)."""
    op = d["opcode"]
    if op in (asm.OP_OUT, asm.OP_SHIFT, asm.OP_TESTBIT, asm.OP_OUTB, asm.OP_INB, asm.OP_STORE):
        return {d["rd"]}
    if op == asm.OP_CMP:
        return {d["rd"], d["rs"]}
    if op == asm.OP_LOADX:
        return {d["rs"]}
    if op == asm.OP_LOOP:
        return {d["loop_rd"]}
    return set()


class SeqCoverage:
    def __init__(self, seed: int):
        self.seed = seed
        self.commit_count = 0
        # 19 real opcodes + reserved 19-31 (isa.md Undefined opcode behavior).
        self.opcode_bins = {n: 0 for n in OPCODE_NAMES.values()} | {"ILLEGAL": 0}
        # BRANCH cond x outcome -- BEQ/BNE each need both directions.
        self.branch_bins = {k: 0 for k in ("JMP", "BEQ_taken", "BEQ_not", "BNE_taken", "BNE_not", "CALL")}
        # WAIT exit paths (isa.md WAIT row): met on first sample, met after
        # blocking, timed out, met on the exact expiry cycle (tie -- met
        # must win), and unbounded (no timeout armed).
        self.wait_bins = {k: 0 for k in ("met_immediate", "met_later", "timeout", "tie", "unbounded")}
        # How long WAIT actually blocked (k = extra cycles past 3). 32+
        # needs the counter's exponent-1 range and a line that really
        # holds its level -- the clock-stretch / slow-host case.
        self.wait_k_bins = {k: 0 for k in ("0", "1-31", "32-479", "480+")}
        # Shared DELAY/WAIT counter's exponent field -- each exponent is a
        # different shift of the same hardware (cycle_counter.v).
        self.delay_exp_bins = {str(e): 0 for e in range(4)}
        self.wait_exp_bins = {str(e): 0 for e in range(4)}
        # SET drive-mode x pin_index -- the sticky-mode table (pin_ctrl.v
        # mode[]/drv[]), incl. no-op SETs on role-hardwired pins 4/5/6.
        self.set_mode_x_pin_bins = {f"{m}.p{p}": 0 for m in SET_MODE_NAMES.values() for p in range(8)}
        # OUTB/INB bit-select x protocol pin -- the bit-bang primitives.
        self.bitop_x_pin_bins = {f"{op}.{b}.p{p}": 0 for op in ("OUTB", "INB")
                                 for b in ("bit0", "bit7") for p in PROTOCOL_PINS}
        # OUTB against each drive mode it must obey (isa.md OUTB row: no
        # mode field of its own, reads SET's sticky mode).
        self.outb_mode_bins = {m: 0 for m in ("pp", "od", "in")}
        # Data region halves: 0-255 is LOADX-reachable, 256-511 fixed-address-only.
        self.data_region_bins = {k: 0 for k in ("LOAD.low", "LOAD.high", "STORE.low", "STORE.high", "LOADX")}
        # Every flag writer, both polarities.
        self.flag_writer_bins = {k: 0 for k in ("CMP.eq", "CMP.ne", "TESTBIT.0", "TESTBIT.1", "WAIT.0", "WAIT.1")}
        # LOOP: branch-back vs fall-through, and counter wrap (Rd=0 on
        # entry decrements to 255 and loops -- LOOP's only non-obvious case).
        self.loop_bins = {k: 0 for k in ("taken", "exit", "wrap_from_0")}
        # Sticky debug flags' set conditions (isa.md).
        self.debug_flag_bins = {k: 0 for k in ("illegal_op", "nested_call", "orphan_ret")}
        # LOAD/LOADX immediately followed by a reader of the same Rd --
        # the documented trailing-writeback hazard case.
        self.hazard_bins = {k: 0 for k in ("load_then_read", "loadx_then_read")}
        # Dynamic control-flow nesting: LOOP depth at each LOOP commit,
        # and branch/CALL executed while inside a live LOOP body -- the
        # interaction space random_gen's flat-only v1 never produces.
        self.loop_nest_bins = {k: 0 for k in ("depth1", "depth2", "depth3+", "branch_in_loop", "call_in_loop")}
        self._prev: dict | None = None
        self._loops: list[tuple[int, int]] = []  # live (body_start, loop_pc) ranges

    def sample(self, pre, post, word: int, cycles: int) -> None:
        d = _decode(word)
        op = d["opcode"]
        self.commit_count += 1
        self.opcode_bins[OPCODE_NAMES.get(op, "ILLEGAL")] += 1

        if op >= 19:
            self.debug_flag_bins["illegal_op"] += 1
        elif op == asm.OP_BRANCH:
            cond = d["cond"]
            if cond == asm.COND_JMP:
                self.branch_bins["JMP"] += 1
            elif cond == asm.COND_CALL:
                self.branch_bins["CALL"] += 1
                if pre.return_valid:
                    self.debug_flag_bins["nested_call"] += 1
            else:
                name = "BEQ" if cond == asm.COND_BEQ else "BNE"
                taken = pre.flag if cond == asm.COND_BEQ else not pre.flag
                self.branch_bins[f"{name}_{'taken' if taken else 'not'}"] += 1
        elif op == asm.OP_RET:
            if not pre.return_valid:
                self.debug_flag_bins["orphan_ret"] += 1
        elif op == asm.OP_WAIT:
            k = cycles - 3
            self.wait_k_bins["0" if k == 0 else "1-31" if k < 32 else "32-479" if k < 480 else "480+"] += 1
            mant, exp = d["wait_mantissa"], d["exponent"]
            met = not post.flag
            if mant == 0:
                self.wait_bins["unbounded"] += 1
            else:
                self.wait_exp_bins[str(exp)] += 1
                target = mant << (exp * TIMEOUT_SHIFT)
                if met and k == target:
                    self.wait_bins["tie"] += 1
            if met:
                self.wait_bins["met_immediate" if k == 0 else "met_later"] += 1
            else:
                self.wait_bins["timeout"] += 1
            self.flag_writer_bins[f"WAIT.{int(post.flag)}"] += 1
        elif op == asm.OP_DELAY:
            self.delay_exp_bins[str(d["exponent"])] += 1
        elif op == asm.OP_SET:
            self.set_mode_x_pin_bins[f"{SET_MODE_NAMES[d['rd']]}.p{d['pin_index']}"] += 1
        elif op in (asm.OP_OUTB, asm.OP_INB):
            name = "OUTB" if op == asm.OP_OUTB else "INB"
            bit = "bit7" if d["aux_bit"] == asm.BITSEL_BIT7 else "bit0"
            if d["pin_index"] in PROTOCOL_PINS:
                self.bitop_x_pin_bins[f"{name}.{bit}.p{d['pin_index']}"] += 1
                if op == asm.OP_OUTB:
                    self.outb_mode_bins[SET_MODE_NAMES[pre.pin_mode[d["pin_index"]]]] += 1
        elif op in (asm.OP_LOAD, asm.OP_STORE):
            name = "LOAD" if op == asm.OP_LOAD else "STORE"
            self.data_region_bins[f"{name}.{'low' if d['imm'] < 256 else 'high'}"] += 1
        elif op == asm.OP_LOADX:
            self.data_region_bins["LOADX"] += 1
        elif op == asm.OP_CMP:
            self.flag_writer_bins["CMP.eq" if post.flag else "CMP.ne"] += 1
        elif op == asm.OP_TESTBIT:
            self.flag_writer_bins[f"TESTBIT.{int(post.flag)}"] += 1
        elif op == asm.OP_LOOP:
            if pre.regs[d["loop_rd"]] == 0:
                self.loop_bins["wrap_from_0"] += 1
            self.loop_bins["taken" if post.regs[d["loop_rd"]] != 0 else "exit"] += 1

        self._sample_nesting(pre.pc, post.pc, d)

        if self._prev is not None and self._prev["opcode"] in (asm.OP_LOAD, asm.OP_LOADX):
            if self._prev["rd"] in _regs_read(d):
                key = "load_then_read" if self._prev["opcode"] == asm.OP_LOAD else "loadx_then_read"
                self.hazard_bins[key] += 1
        self._prev = d

    def _sample_nesting(self, pc: int, next_pc: int, d: dict) -> None:
        # A live loop is one whose body contains the executing pc; any we
        # left (fall-through, forward branch out, CALL) drop off. A loop
        # only becomes live at its first back-edge, so an outer loop's
        # first pass under-counts as not-nested -- conservative, never
        # over-reports nesting.
        op = d["opcode"]
        live = [(s, e) for (s, e) in self._loops if s <= pc <= e]
        if op == asm.OP_LOOP:
            rng = (d["addr"], pc)
            if next_pc == d["addr"] and rng not in live:
                live.append(rng)
            depth = len([1 for (s, e) in live if s <= pc <= e]) or 1
            self.loop_nest_bins["depth1" if depth == 1 else "depth2" if depth == 2 else "depth3+"] += 1
            if next_pc != d["addr"]:
                live = [r for r in live if r != rng]
        elif live and op == asm.OP_BRANCH:
            self.loop_nest_bins["call_in_loop" if d["cond"] == asm.COND_CALL else "branch_in_loop"] += 1
        self._loops = live

    def to_dict(self) -> dict:
        return {k: v for k, v in vars(self).items() if not k.startswith("_")}

    def write(self) -> str:
        os.makedirs(COV_DIR, exist_ok=True)
        path = os.path.join(COV_DIR, f"seed_{self.seed}.json")
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh)
        return path
