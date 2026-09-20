# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

# LDI R0,42 ; OUT R0 ; HALT, as raw bytes in LOAD-mode boot-stream
# order (low byte of instruction 0 first -- docs/architecture.md
# Pipeline LOAD section). Encoding is opcode(5)|Rd(2)|imm(9),
# MSB-first (src/cpu/isa_defs.v):
#   LDI  R0,42 -> word 0x202A -> bytes 0x2A, 0x20
#   OUT  R0    -> word 0x3000 -> bytes 0x00, 0x30
#   HALT       -> word 0x0800 -> bytes 0x00, 0x08
PROG_BYTES = [0x2A, 0x20, 0x00, 0x30, 0x00, 0x08]

# uio bit positions -- pin_index doubles as the uio bit position
# throughout this design (docs/architecture.md Pin map).
HOST_GO_BIT = 4
HOST_STATUS_BIT = 5
START_BIT = 6


async def load_byte(dut, byte_val, uio_in_state):
    """One LOAD-mode boot write: host asserts HOST_GO, waits for the
    HOST_STATUS ack, then releases and waits for the ack to clear
    (docs/architecture.md Pipeline LOAD section's write-commit
    handshake) -- not a fixed cycle count, just level transitions."""
    dut.ui_in.value = byte_val
    uio_in_state |= 1 << HOST_GO_BIT
    dut.uio_in.value = uio_in_state

    while not (int(dut.uio_out.value) >> HOST_STATUS_BIT) & 1:
        await RisingEdge(dut.clk)

    uio_in_state &= ~(1 << HOST_GO_BIT)
    dut.uio_in.value = uio_in_state

    while (int(dut.uio_out.value) >> HOST_STATUS_BIT) & 1:
        await RisingEdge(dut.clk)

    return uio_in_state


@cocotb.test()
async def test_boot_load_execute_halt(dut):
    """Streams a tiny program in via the LOAD-mode boot handshake,
    pulses START, and checks the CPU actually runs it end to end:
    LDI R0,42 ; OUT R0 ; HALT should land 42 on uo_out and then freeze
    there (HALT is documented as resumable only by external reset)."""

    clock = Clock(dut.clk, 20, unit="ns")  # 50MHz, matches info.yaml clock_hz
    cocotb.start_soon(clock.start())

    dut._log.info("Reset")
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    dut._log.info("Streaming firmware via LOAD-mode boot handshake")
    uio_in_state = 0
    for b in PROG_BYTES:
        uio_in_state = await load_byte(dut, b, uio_in_state)

    dut._log.info("Pulsing START")
    uio_in_state |= 1 << START_BIT
    dut.uio_in.value = uio_in_state
    await ClockCycles(dut.clk, 4)
    uio_in_state &= ~(1 << START_BIT)
    dut.uio_in.value = uio_in_state

    await ClockCycles(dut.clk, 40)

    assert dut.uo_out.value == 42, f"uo_out = {int(dut.uo_out.value)}, expected 42"
    dut._log.info("uo_out == 42, LDI/OUT ran correctly")

    # HALT should freeze the FSM -- confirm it actually holds.
    await ClockCycles(dut.clk, 20)
    assert dut.uo_out.value == 42, "uo_out drifted after HALT -- FSM should be frozen"
    dut._log.info("uo_out held steady post-HALT, FSM frozen as expected")
