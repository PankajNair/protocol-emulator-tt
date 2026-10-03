# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Self-tests for the board testbench (tb_board.v + board.py): the net
resolution, loopback wiring, two-chip wired-AND and independent clocks
that the loopback / chip-to-chip / I2C tests will rely on. Checked first,
on their own, so a later protocol failure can't be the board's fault.

Run: make -C test board
"""

import cocotb
from cocotb.triggers import ClockCycles, Timer
from cocotb.utils import get_sim_time

import isa_asm as asm
from board import contention_nets, drive_net, init_board, net


@cocotb.test()
async def test_net_resolution(dut):
    """Wire semantics with no chip involved: floating -> x, pull-up -> 1,
    an external driver wins over the pull-up, 0 and 1 both drive."""
    a, b = init_board(dut, nets_pullup=0b0000_0010)
    a.start_clock()
    await ClockCycles(a.clk, 2)
    assert net(dut, 0) == "x", "undriven net without pull-up must float (x)"
    assert net(dut, 1) == 1, "pulled-up net must read 1"
    drive_net(dut, 1, 0)
    await Timer(1, unit="ns")
    assert net(dut, 1) == 0, "driver 0 must beat the pull-up"
    drive_net(dut, 2, 1)
    await Timer(1, unit="ns")
    assert net(dut, 2) == 1
    drive_net(dut, 1, None)
    await Timer(1, unit="ns")
    assert net(dut, 1) == 1, "released net returns to its pull-up"
    await ClockCycles(a.clk, 2)
    assert contention_nets(dut) == []


@cocotb.test()
async def test_loopback_wiring(dut):
    """One chip, TX (pin 0) and RX (pin 1) on the same net: what the chip
    drives on pin 0 it reads back on pin 1 -- the wiring the UART
    self-test (loopback) will use."""
    a, b = init_board(dut, nets_pullup=0b1)
    a.start_clock()
    a.connect({0: 0, 1: 0})
    prog = [
        asm.ldi(0, 0x01),
        asm.set_pin(asm.SET_MODE_PUSH_PULL, 0, 1),
        asm.delay(4, 0),                       # > synchronizer latency
        asm.inb(0, 1, asm.BITSEL_BIT7),        # R0 = 0x81 if loopback saw 1
        asm.out(0),
        asm.ldi(1, 0x02),
        asm.set_pin(asm.SET_MODE_PUSH_PULL, 0, 0),
        asm.delay(4, 0),
        asm.inb(1, 1, asm.BITSEL_BIT7),        # R1 stays 0x02 if it saw 0
        asm.out(1),
        asm.halt(),
    ]
    await a.boot(prog)
    await a.run_to_value(0x81)
    await a.run_to_value(0x02)
    assert contention_nets(dut) == []


@cocotb.test()
async def test_two_chip_open_drain_wired_and(dut):
    """Two chips' open-drain pins on one pulled-up net: wired-AND, no
    contention, whoever pulls low wins. Then a push-pull high against an
    open-drain low -- a real driver fight -- must be flagged."""
    a, b = init_board(dut, nets_pullup=0b1)
    a.start_clock()
    b.start_clock(ppm=100, phase_ps=7_000)
    a.connect({1: 0})
    b.connect({1: 0})
    await a.boot([asm.set_pin(asm.SET_MODE_OPEN_DRAIN, 1, 1), asm.halt()])   # A releases
    await b.boot([asm.set_pin(asm.SET_MODE_OPEN_DRAIN, 1, 0), asm.halt()])   # B pulls low
    await ClockCycles(a.clk, 20)
    assert net(dut, 0) == 0, "open-drain low on either chip must pull the net low"
    assert contention_nets(dut) == [], "open-drain wired-AND is not contention"

    await a.boot([asm.set_pin(asm.SET_MODE_PUSH_PULL, 1, 1), asm.halt()])   # A now drives high
    await ClockCycles(a.clk, 20)
    assert net(dut, 0) == "x"
    assert contention_nets(dut) == [0], "push-pull 1 against open-drain 0 must be flagged"


@cocotb.test()
async def test_independent_clocks(dut):
    """Each chip runs on its own clock: B at +5000 ppm, phase-shifted.
    The same DELAY-timed program takes proportionally less real time on
    B -- proof the two clock domains are genuinely independent."""
    a, b = init_board(dut)
    a.start_clock()
    b.start_clock(ppm=5000, phase_ps=3_333)
    prog = [asm.ldi(0, 0x11), asm.out(0), asm.delay(500, 0), asm.ldi(0, 0x22), asm.out(0), asm.halt()]

    async def span(chip):
        await chip.boot(prog)
        await chip.run_to_value(0x11)
        t0 = get_sim_time("ps")
        await chip.run_to_value(0x22, max_cycles=700)
        return get_sim_time("ps") - t0

    ta = await span(a)
    tb_ = await span(b)
    ratio = ta / tb_
    dut._log.info(f"A {ta} ps, B {tb_} ps: A/B = {ratio:.5f}, clock periods {a.period_ps}/{b.period_ps} ps")
    assert b.period_ps < a.period_ps, "+ppm must mean a faster clock"
    assert abs(ratio - a.period_ps / b.period_ps) < 1e-3, "chip time should scale with its own clock period"
