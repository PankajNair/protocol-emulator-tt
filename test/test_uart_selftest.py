# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""UART loopback self-test (firmware/protocols/uart_selftest.asm) on the
board testbench: one chip, TX (pin 0) jumpered to RX (pin 1) through a
net. The same image is the silicon bring-up check -- these tests are what
a pass and a fail look like from the host pins.

Negative cases matter as much as the pass: a self-test that can't fail
proves nothing, so a missing jumper and an RX line stuck high must both
be reported as failures.

Run: make -C test board
"""

import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

from board import contention_nets, drive_net, init_board

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

SELFTEST_ASM = Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "uart_selftest.asm"
PASS_CODE = 0xA5
TX, RX = 0, 1
HOST_STATUS, HOST_ERROR = 5, 6
N_PATTERNS = 8


def bit(sig, n):
    return (int(sig.value) >> n) & 1


async def run_selftest(dut, bit_cycles, wire):
    """Boot the self-test on chip A with RX wired per `wire`, wait for a
    verdict, return ('pass'|'fail', uo_out). `wire`: 'jumper' (TX and RX
    on one pulled-up net), 'open' (RX on nothing), 'stuck_high' (RX on a
    net the testbench holds at 1)."""
    a, _ = init_board(dut, nets_pullup=0b11)
    a.start_clock()
    if wire == "jumper":
        a.connect({TX: 0, RX: 0})
    elif wire == "open":
        a.connect({TX: 0})
    elif wire == "stuck_high":
        a.connect({TX: 0, RX: 1})
        drive_net(dut, 1, 1)
    prog = assembler.assemble_file(SELFTEST_ASM, {"BAUD_CYCLES": bit_cycles})
    for w in prog.warnings:
        dut._log.info(f"asm: {w}")
    await a.boot(image=prog.image())

    budget = (N_PATTERNS + 2) * 12 * bit_cycles
    for _ in range(budget):
        await RisingEdge(a.clk)
        status = bit(a.uio_oe, HOST_STATUS) & bit(a.uio_out, HOST_STATUS)
        error = bit(a.uio_oe, HOST_ERROR) & bit(a.uio_out, HOST_ERROR)
        if status or error:
            assert not (status and error), "self-test reported pass and fail at once"
            await ClockCycles(a.clk, 2)
            assert contention_nets(dut) == [], f"driver fight on nets {contention_nets(dut)}"
            return ("pass" if status else "fail"), int(a.uo_out.value)
    assert False, f"self-test gave no verdict within {budget} cycles"


@cocotb.test()
async def test_selftest_pass_115200(dut):
    """Jumper in place, 115200 baud: all 8 patterns loop back, pass code."""
    verdict, code = await run_selftest(dut, 434, "jumper")
    assert (verdict, code) == ("pass", PASS_CODE), f"got {verdict}, uo_out={code:#04x}"


@cocotb.test()
async def test_selftest_pass_57600(dut):
    """Same at 57600, where DELAY rounding is in play (~0.9%/bit)."""
    verdict, code = await run_selftest(dut, 868, "jumper")
    assert (verdict, code) == ("pass", PASS_CODE), f"got {verdict}, uo_out={code:#04x}"


@cocotb.test()
async def test_selftest_detects_missing_jumper(dut):
    """RX not connected (reads 0): the first frame comes back 0x00 with a
    0 stop bit -- must be reported as a failure, not a pass."""
    verdict, code = await run_selftest(dut, 434, "open")
    assert verdict == "fail", f"missing jumper not detected (uo_out={code:#04x})"


@cocotb.test()
async def test_selftest_detects_stuck_high_rx(dut):
    """RX stuck at 1: every byte reads back 0xFF -- the first pattern sent
    (0xC3) must mismatch and fail, with 0xFF shown on uo_out."""
    verdict, code = await run_selftest(dut, 434, "stuck_high")
    assert verdict == "fail", "stuck-high RX not detected"
    assert code == 0xFF, f"uo_out should show the received 0xFF, got {code:#04x}"
