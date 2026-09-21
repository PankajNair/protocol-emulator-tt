# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Confirms cocotb can read internal RTL state directly via hierarchical
signal paths, on both icarus and verilator -- this is the foundation
the golden-model differential-testing harness (test_random.py) builds
on: comparing DUT-internal state (regfile contents, PC, sticky pin
state, SRAM contents) against a software model needs no new RTL debug
ports, only that cocotb's VPI access actually works the same way on
both simulator backends for plain registers AND unpacked-array
elements.

Kept permanently (not deleted after bring-up) as a cheap toolchain-
drift canary -- this project has already hit real icarus-vs-verilator
divergences this session (declare-before-use ordering in core.v,
verilator needing an explicit --timing flag cocotb's own Makefile.sim
didn't pass) that had nothing to do with the RTL's own correctness.
Array-element VPI access is exactly the kind of thing worth confirming
empirically rather than assuming carries over identically.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

HOST_STATUS_BIT = 5
START_BIT = 6


@cocotb.test()
async def test_hierarchy_smoke(dut):
    clock = Clock(dut.clk, 20, unit="ns")
    cocotb.start_soon(clock.start())

    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    core = dut.user_project.u_core
    regfile = dut.user_project.u_core.u_regfile
    mem = dut.user_project.u_mem
    pin_ctrl = dut.user_project.u_pin_ctrl

    # Plain internal registers.
    state0 = int(core.state.value)
    pc0 = int(core.pc.value)
    flag0 = int(core.flag.value)
    halted0 = int(core.halted.value)
    dut._log.info(f"post-reset: state={state0} pc={pc0} flag={flag0} halted={halted0}")
    assert state0 == 0, f"expected S_LOAD (0) post-reset, got {state0}"
    assert pc0 == 0
    assert flag0 == 0
    assert halted0 == 0

    # Unpacked-array elements: regfile.regs[0:3], mem.storage[0:1023],
    # pin_ctrl.mode[0:7]/drv[0:7] -- read every element of each to
    # shake out any simulator-specific indexing quirk, not just index 0.
    for i in range(4):
        v = int(regfile.regs[i].value)
        assert v == 0, f"regs[{i}] expected 0 post-reset, got {v}"

    # mem.v's storage[] has no reset (real SRAM content is undefined
    # until written, by design -- see mem.v's own header) -- these
    # elements read back as X in simulation, so just confirm the read
    # itself doesn't error, not that it converts to a clean int.
    for i in (0, 1, 511, 512, 1023):
        raw = mem.storage[i].value
        dut._log.info(f"storage[{i}] post-reset (undefined by design) = {raw}")

    for i in range(8):
        mode_v = int(pin_ctrl.mode[i].value)
        drv_v = int(pin_ctrl.drv[i].value)
        assert mode_v == 0b11, f"pin_ctrl.mode[{i}] expected SET_MODE_INPUT (3) post-reset, got {mode_v}"
        assert drv_v == 0, f"pin_ctrl.drv[{i}] expected 0 post-reset, got {drv_v}"

    # Write through the hierarchical path, confirm the write is
    # actually visible on the next read -- test_random.py's fast_boot()
    # depends on this exact mechanism to poke the full 1024-byte SRAM
    # image directly.
    mem.storage[7].value = 0xA5
    await RisingEdge(dut.clk)
    v = int(mem.storage[7].value)
    assert v == 0xA5, f"write-then-read through hierarchical path failed: got {v:#x}"

    dut._log.info("hierarchical signal access confirmed: plain regs, array reads, array writes")
