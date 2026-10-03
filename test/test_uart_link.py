# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Two-chip UART link on the board testbench: chip A runs the TX firmware
(firmware/protocols/uart.asm, pin 0), chip B runs the RX firmware
(uart_rx.asm, pin 1), A.TX and B.RX on one pulled-up net. Each chip has
its own clock; B's is swept in frequency and phase against A's -- the
way two real boards' crystals differ -- so both synchronizers and both
firmwares' bit timing meet a genuinely asynchronous partner.

One host feeds A through the IN handshake while another collects from B
through the host-initiated OUT handshake, concurrently. Every byte must
cross intact, B must report no framing error, and the net must see no
driver fight.

This complements, not replaces, test_uart.py's independent receiver /
transmitter models: two copies of our own firmware can share a wrong
assumption (bit order, say) and still agree with each other.

Run: make -C test board
"""

import sys
from pathlib import Path

import cocotb

import test_uart as U
from board import contention_nets, init_board

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

PROTOCOLS = Path(__file__).resolve().parents[1] / "firmware" / "protocols"
TX_PIN, RX_PIN = 0, 1
PAYLOAD = [0x55, 0xAA, 0x00, 0xFF, 0x3C, 0x81, 0xC3, 0x7E]


async def run_link(dut, ppm_b: float, phase_ps: int, payload=PAYLOAD, bit_cycles=434):
    """Returns (bytes received by B, B's HOST_ERROR, contended nets)."""
    a, b = init_board(dut, nets_pullup=0b1)
    a.start_clock()
    b.start_clock(ppm=ppm_b, phase_ps=phase_ps)
    a.connect({TX_PIN: 0})
    b.connect({RX_PIN: 0})
    tx = assembler.assemble_file(PROTOCOLS / "uart.asm", {"BAUD_CYCLES": bit_cycles})
    rx = assembler.assemble_file(PROTOCOLS / "uart_rx.asm", {"BAUD_CYCLES": bit_cycles})

    boot_a = cocotb.start_soon(a.boot(tx.words))
    boot_b = cocotb.start_soon(b.boot(rx.words))
    ext_a = await boot_a
    ext_b = await boot_b

    sender = cocotb.start_soon(U.send_bytes(a, ext_a, payload, wait_cycles=14 * bit_cycles))
    got = await U.host_collect(b, U.UioBus(b, ext_b), len(payload), max_wait=16 * bit_cycles)
    await sender
    return got, U.host_error(b), contention_nets(dut)


@cocotb.test()
async def test_uart_link_same_clock(dut):
    """Both chips at nominal 50 MHz (phase offset only): the baseline."""
    got, herr, fights = await run_link(dut, 0, 9_000)
    assert got == PAYLOAD, f"received {[hex(x) for x in got]}"
    assert herr == 0 and fights == []


@cocotb.test()
async def test_uart_link_clock_skew(dut):
    """B's clock +-1% and +-2% off A's, at three phases each: every byte
    crosses, no framing error, no fight. +-2% is a typical end-to-end
    UART budget; real crystals are usually within +-100 ppm."""
    for ppm in (10_000, -10_000, 20_000, -20_000):
        for phase in (0, 6_667, 13_333):
            got, herr, fights = await run_link(dut, ppm, phase)
            tag = f"B {ppm/1e4:+.0f}%, phase {phase} ps"
            assert got == PAYLOAD, f"{tag}: received {[hex(x) for x in got]}"
            assert herr == 0, f"{tag}: spurious framing error"
            assert fights == [], f"{tag}: driver fight on {fights}"
            dut._log.info(f"link OK: {tag}")
