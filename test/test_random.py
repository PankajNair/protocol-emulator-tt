# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Randomized differential testing: generates a program per seed
(random_gen.py), runs it on the real RTL and on golden_model.py in
lockstep, comparing full architectural state after every instruction
commit. Where test.py's 18 directed tests ask "does this one sequence
work," this asks "does every sequence agree with the spec" -- the
class of check that would have caught the cycle_counter.v DELAY
off-by-one (commit 1121ccc) systematically, not just because someone
happened to write a timing-differential test for that one opcode.

Does not modify test.py or isa_asm.py -- both stay untouched, reused
directly. See golden_model.py's header for scope (now all 19 opcodes,
including WAIT/IN/INB) and random_gen.py's header for how program
termination is guaranteed by construction, and for WAIT/INB's scoped-
down pin_index/timeout generation.

WAIT/IN/INB need live external pin/bus stimulus, which this file now
drives every cycle post-boot from a deterministic per-seed timeline
(io_stimulus.py) -- see that module's header for the exact timing
convention (empirically confirmed against real RTL, not assumed) and
for why cycles at or before `boot_exit_cycle` are handled as a special
case rather than pulled from the random timeline.

Env vars (matching test/Makefile's existing SIM ?= convention):
    SEEDS           number of seeds to run (default 25)
    SEED_BASE       first seed value (default 1)
    MAX_CYCLES      per-seed hard-failure cycle budget (default 20000)
    MAX_DELAY_EXP0  DELAY mantissa clamp at exponent=0 (default 63)
    MAX_DELAY_EXP1  DELAY mantissa clamp at exponent=1 (default 15)
    MAX_WAIT_EXP0   WAIT mantissa clamp at exponent=0 (default 31)
    MAX_WAIT_EXP1   WAIT mantissa clamp at exponent=1 (default 15)
    STIM_PROFILE    stimulus profile test/stim/profile_<name>.py (default: none)
    COV_DIR         coverage JSON output dir (default /tmp/seq_coverage)

Run: make -C test COCOTB_TEST_MODULES=test_random SEEDS=200
     (or the `make -C test random` convenience target)
"""

import importlib
import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

import isa_asm as asm
import random_gen
from golden_model import SequencerState, _decode, step
from io_stimulus import IoStimulus
from seq_coverage import SeqCoverage

S_LOAD, S_FETCH_LO, S_FETCH_HI, S_EXECUTE = 0, 1, 2, 3
START_BIT = 6

ARTIFACT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regression_artifacts")


def _load_profile():
    """STIM_PROFILE=<name> -> test/stim/profile_<name>.py (see
    test/stim/AGENT_CONTRACT.md). Each hook the profile exports
    (generate_program, make_stimulus) replaces the default; anything it
    doesn't export falls back to random_gen / IoStimulus. Unset = the
    default generator, unchanged."""
    name = os.environ.get("STIM_PROFILE", "")
    if not name:
        return "", random_gen.generate_program, IoStimulus
    mod = importlib.import_module(f"stim.profile_{name}")
    gen = getattr(mod, "generate_program", None)
    mk = getattr(mod, "make_stimulus", None)
    if gen is None and mk is None:
        raise RuntimeError(f"stim/profile_{name}.py exports neither generate_program nor make_stimulus")
    return name, gen or random_gen.generate_program, mk or IoStimulus


async def fast_boot(dut, words):
    """Pokes the full 1024-byte SRAM image directly (program words +
    zero-fill everywhere else -- mem.v's storage[] has no reset, see
    its own header comment, so a never-written byte is simulator-
    dependent garbage that would cause a spurious mismatch against a
    golden model that assumes zero), then does a REAL uio[6] (START)
    pin toggle rather than a poke -- pin_ctrl.v's own edge-detect/
    driver-contention-gate logic (`seen_start_fall`) must actually run,
    or anything touching pin_index=6 (HOST_ERROR) would silently
    diverge from a real boot. Deliberately skips the slow per-byte
    LOAD-mode handshake -- already proven by test.py's 18 directed
    tests; this harness spends its budget on instruction semantics.

    Returns the cycle count (relative to START's assertion) at which
    `state` first left S_LOAD -- i.e. instruction 0's own FETCH_LO
    cycle -- so the caller can seed its cycle-count baseline exactly,
    rather than approximately (found the hard way: an earlier version
    just returned after a fixed hold and let the caller assume it was
    already past boot-exit, which raced the real transition and either
    missed it entirely or under-counted instruction 0's own cycle
    cost).

    Does NOT start the clock -- the caller starts it exactly once per
    test. An earlier version started a fresh Clock here every seed and
    never stopped the old ones, so per-cycle cost grew with seed index
    and total wall time grew ~quadratically in SEEDS."""
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)

    image = asm.to_bytes(words)
    image += [0] * (1024 - len(image))
    mem = dut.user_project.u_mem
    for i, b in enumerate(image):
        mem.storage[i].value = b

    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    core = dut.user_project.u_core
    dut.uio_in.value = 1 << START_BIT
    boot_exit_cycle = None
    for c in range(1, 5):
        await RisingEdge(dut.clk)
        if boot_exit_cycle is None and int(core.state.value) != S_LOAD:
            boot_exit_cycle = c
    dut.uio_in.value = 0

    if boot_exit_cycle is None:
        raise RuntimeError("state never left S_LOAD during the START hold window")
    return boot_exit_cycle


def _dump_artifacts(seed, words, report):
    seed_dir = os.path.join(ARTIFACT_DIR, f"seed_{seed}")
    os.makedirs(seed_dir, exist_ok=True)
    with open(os.path.join(seed_dir, "program.txt"), "w") as fh:
        fh.write(f"# seed={seed}, {len(words)} words\n")
        fh.write(random_gen.words_to_text(words))
        fh.write("\n")
    with open(os.path.join(seed_dir, "failure.txt"), "w") as fh:
        fh.write(report + "\n")


async def run_one_seed(dut, seed, max_cycles, max_delay_mantissa, max_wait_mantissa, gen_program, make_stimulus):
    words = gen_program(seed, max_delay_mantissa=max_delay_mantissa, max_wait_mantissa=max_wait_mantissa)
    boot_exit_cycle = await fast_boot(dut, words)
    # Margin beyond max_cycles: a WAIT's own lookahead can probe a few
    # cycles past the program's eventual hard-failure point before that
    # failure is detected; IoStimulus.raw_*() already returns 0 past
    # max_cycle regardless, this margin just avoids relying on that.
    stim = make_stimulus(seed, boot_exit_cycle, max_cycle=max_cycles + 32)

    core = dut.user_project.u_core
    regfile = dut.user_project.u_core.u_regfile
    mem = dut.user_project.u_mem
    pin_ctrl = dut.user_project.u_pin_ctrl

    golden = SequencerState.reset()

    # Cycle 0 == the cycle state first showed S_FETCH_LO (instruction
    # 0's own first cycle) -- fast_boot() measured this precisely, see
    # its own docstring for why approximating it was a real bug.
    cycle = boot_exit_cycle
    last_commit_cycle = boot_exit_cycle
    prev_state = int(core.state.value)
    prev_halted = int(core.halted.value)
    instr_index = 0
    opcode_counts: dict[int, int] = {}
    cov = SeqCoverage(seed)

    def fail(msg):
        report = f"seed={seed} instr={instr_index} pc={golden.pc}\n{msg}"
        _dump_artifacts(seed, words, report)
        cov.write()  # reported even on a failing seed, same as the RISC-V env
        assert False, report

    def check(name, rtl_value, golden_value):
        if rtl_value != golden_value:
            fail(f"{name} mismatch: RTL={rtl_value!r} golden={golden_value!r}")

    while True:
        await RisingEdge(dut.clk)
        cycle += 1
        # Drive this cycle's raw external stimulus -- see io_stimulus.py's
        # header for the exact convention and why every opcode (not just
        # WAIT/IN/INB) can safely be driven uniformly every cycle: no
        # other opcode's RTL path reads ui_in_sync/pin_read at all.
        dut.ui_in.value = stim.raw_ui_in(cycle)
        dut.uio_in.value = stim.raw_uio_in(cycle)
        if cycle > max_cycles:
            fail(
                f"exceeded MAX_CYCLES={max_cycles} without halting -- random_gen.py "
                "guarantees termination by construction, so this means a real RTL "
                "hang, a golden-model bug, or a generator-discipline bug, not an "
                "expected timeout"
            )

        cur_state = int(core.state.value)
        cur_halted = int(core.halted.value)

        is_commit = (prev_state == S_EXECUTE and cur_state == S_FETCH_LO) or (prev_halted == 0 and cur_halted == 1)
        prev_state, prev_halted = cur_state, cur_halted
        if not is_commit:
            continue

        # This instruction's own FETCH_LO-start cycle is `last_commit_cycle`
        # BEFORE this reassignment -- `is_commit` fires on the transition
        # INTO S_FETCH_LO, i.e. `cycle` right now is where the CURRENT
        # instruction's own FETCH_LO just began; `last_commit_cycle` (the
        # value from the PREVIOUS iteration) already holds exactly that
        # same cycle number for the FIRST instruction ever processed
        # (seeded from boot_exit_cycle, fast_boot()'s own contract), and
        # for every instruction after that it's this same variable, one
        # commit-cycle behind `cycle` itself. Reusing `cycle` directly
        # here would be off by one whole instruction's occupancy (3+
        # cycles) -- found via a live RTL/golden IN mismatch: golden
        # computed an abs_cycle 3 cycles too late, matching the very
        # first WAIT/IN/INB commit checked end to end.
        instr_fetch_lo_cycle = last_commit_cycle
        cycles_used = cycle - last_commit_cycle
        last_commit_cycle = cycle
        # entering_execute: FETCH_LO -> FETCH_HI -> EXECUTE, always
        # exactly 1 cycle each (docs/architecture.md Pipeline).
        abs_cycle = instr_fetch_lo_cycle + 2

        word = words[golden.pc] if golden.pc < len(words) else asm.nop()
        rtl_ir = int(core.ir.value)
        decoded = _decode(word)
        pre_step_pc = golden.pc
        opcode_counts[decoded["opcode"]] = opcode_counts.get(decoded["opcode"], 0) + 1
        pre_golden = golden
        golden, expected_cycles = step(golden, word, io_read=stim.io_read, abs_cycle=abs_cycle)
        instr_index += 1
        cov.sample(pre_golden, golden, word, expected_cycles)

        check("fetched instruction (ir)", rtl_ir, word)
        check("cycles used", cycles_used, expected_cycles)
        check("pc", int(core.pc.value), golden.pc)
        # LOAD/LOADX's register write trails by exactly 1 cycle, into
        # the FOLLOWING instruction's own FETCH_LO (docs/architecture.md
        # Pipeline section, already proven hazard-free even for that
        # immediately-following instruction) -- at THIS commit boundary
        # the RTL write hasn't landed yet even though golden_model's
        # step() already applied it atomically, so skip checking JUST
        # that one register now; it gets checked normally next commit
        # (by which point RTL has caught up), as an ordinary side
        # effect of this same per-commit loop, not a separate deferred-
        # check step. Found the hard way: an earlier version compared
        # all 4 registers unconditionally and false-failed on every
        # LOAD/LOADX.
        skip_reg = decoded["rd"] if decoded["opcode"] in (asm.OP_LOAD, asm.OP_LOADX) else None
        for i in range(4):
            if i == skip_reg:
                continue
            check(f"regs[{i}]", int(regfile.regs[i].value), golden.regs[i])
        check("flag", int(core.flag.value), int(golden.flag))
        check("retaddr", int(core.retaddr.value), golden.retaddr)
        check("return_valid", int(core.return_valid.value), int(golden.return_valid))
        check("illegal_op_flag", int(core.illegal_op_flag.value), int(golden.illegal_op_flag))
        check("call_ret_misuse_flag", int(core.call_ret_misuse_flag.value), int(golden.call_ret_misuse_flag))
        check("halted", int(core.halted.value), int(golden.halted))
        check("uo_out", int(dut.uo_out.value), golden.uo_out)
        for i in range(8):
            check(f"pin_mode[{i}]", int(pin_ctrl.mode[i].value), golden.pin_mode[i])
            check(f"pin_drv[{i}]", int(pin_ctrl.drv[i].value), golden.pin_drv[i])

        if decoded["opcode"] == asm.OP_STORE:
            addr = decoded["imm"]
            check(f"data_mem[{addr}] (just STOREd, pc={pre_step_pc})",
                  int(mem.storage[512 + addr].value), golden.data_mem[addr])

        if golden.halted:
            break

    # Closing full-region sanity diff -- catches any memory mismatch a
    # per-STORE spot-check might have missed (e.g. a write landing at
    # the wrong address entirely).
    for addr in range(512):
        check(f"data_mem[{addr}] (closing diff)", int(mem.storage[512 + addr].value), golden.data_mem[addr])

    cov.write()
    return instr_index, opcode_counts


@cocotb.test()
async def test_random_differential(dut):
    n_seeds = int(os.environ.get("SEEDS", "25"))
    seed_base = int(os.environ.get("SEED_BASE", "1"))
    max_cycles = int(os.environ.get("MAX_CYCLES", "20000"))
    max_delay_mantissa = {
        0: int(os.environ.get("MAX_DELAY_EXP0", "63")),
        1: int(os.environ.get("MAX_DELAY_EXP1", "15")),
    }
    max_wait_mantissa = {
        0: int(os.environ.get("MAX_WAIT_EXP0", "31")),
        1: int(os.environ.get("MAX_WAIT_EXP1", "15")),
    }

    profile, gen_program, make_stimulus = _load_profile()
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())  # 50MHz (info.yaml clock_hz), once for all seeds
    dut._log.info(f"random differential test: SEEDS={n_seeds} SEED_BASE={seed_base} MAX_CYCLES={max_cycles} "
                  f"STIM_PROFILE={profile or 'default'}")

    total_instr = 0
    total_opcode_counts: dict[int, int] = {}
    for seed in range(seed_base, seed_base + n_seeds):
        n_instr, opcode_counts = await run_one_seed(dut, seed, max_cycles, max_delay_mantissa, max_wait_mantissa, gen_program, make_stimulus)
        total_instr += n_instr
        for op, count in opcode_counts.items():
            total_opcode_counts[op] = total_opcode_counts.get(op, 0) + count
        if seed % 10 == 0 or seed == seed_base + n_seeds - 1:
            dut._log.info(f"seed={seed}: {n_instr} instructions, matched RTL exactly")

    wait_in_inb = sum(total_opcode_counts.get(op, 0) for op in (asm.OP_WAIT, asm.OP_IN, asm.OP_INB))
    pct = 100.0 * wait_in_inb / total_instr if total_instr else 0.0
    dut._log.info(
        f"WAIT/IN/INB: {wait_in_inb}/{total_instr} committed instructions ({pct:.1f}%) -- "
        f"WAIT={total_opcode_counts.get(asm.OP_WAIT, 0)} "
        f"IN={total_opcode_counts.get(asm.OP_IN, 0)} "
        f"INB={total_opcode_counts.get(asm.OP_INB, 0)}"
    )
    dut._log.info(f"ALL {n_seeds} SEEDS PASSED ({seed_base}..{seed_base + n_seeds - 1})")
