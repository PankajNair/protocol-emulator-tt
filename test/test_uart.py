# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""End-to-end protocol test: the real UART firmware
(firmware/protocols/uart.asm, built by firmware/isa/asm.py) running on the
RTL, decoded by an independent UART receiver model on the TX pin.

This is the chip doing its actual job -- emulating a protocol from
firmware -- rather than a single-instruction check. It also tests
docs/protocol_timing.md's per-baud timing claims against the RTL instead
of trusting them: each edge's deviation from the ideal bit grid is
measured and compared to the error that table predicts.

The receiver only sees what a real one would: the TX pin as driven
(uio_oe[0]=1 required; an undriven line reads as idle-high), sampled once
per clock, start-bit edge detection, centre-of-bit sampling, stop-bit
check. It never looks inside the chip.
"""

import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import test as T  # shared boot/handshake helpers

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

UART_ASM = Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "uart.asm"
TX_PIN = 0

# (baud, cycles/bit at 50 MHz, max allowed |edge deviation| / bit time).
# 115200 is exact by design (docs/protocol_timing.md); the others carry
# DELAY's 32-cycle quantization, predicted there at 0.92% (57600) and
# 0.23% (9600) per bit. A real UART receiver tolerates roughly +-2% of a
# bit per bit over a 10-bit frame (centre sample of the stop bit must
# land inside it); the per-baud limit below is the prediction plus a
# little slack, so a timing regression fails long before a receiver would.
BAUDS = [(115200, 434, 0.0), (57600, 868, 0.012), (9600, 5208, 0.004)]
PAYLOAD = [0x55, 0xA5, 0x00, 0xFF, 0x3C]


def tx_level(dut) -> int:
    """TX as the line sees it: driven value if uio_oe[0], else idle high
    (external pull-up)."""
    if (int(dut.uio_oe.value) >> TX_PIN) & 1:
        return (int(dut.uio_out.value) >> TX_PIN) & 1
    return 1


class UartRx:
    """Cycle-sampled UART 8N1 receiver. Records decoded frames and, per
    frame, every edge's offset from the ideal grid start + k*bit."""

    def __init__(self, dut, bit_cycles: int):
        self.dut, self.bit = dut, bit_cycles
        self.frames: list[int] = []
        self.errors: list[str] = []
        self.max_dev = 0.0  # worst |edge - ideal| / bit_cycles, per bit index

    async def run(self):
        dut, bit = self.dut, self.bit
        prev = tx_level(dut)
        while True:
            await RisingEdge(dut.clk)
            cur = tx_level(dut)
            if not (prev == 1 and cur == 0):
                prev = cur
                continue
            # Start edge at cycle 0; walk the frame cycle by cycle up to the
            # stop bit's centre sample, then re-arm for the next start edge
            # -- as a real receiver does. Waiting a full 10 bits instead
            # misses a following start bit whenever the transmitter runs
            # slightly fast (e.g. 9600 baud here, -0.23%/bit).
            edges, level, samples = [], 0, {}
            sample_at = {bit // 2 + k * bit: k for k in range(10)}  # 0=start,1..8 data,9 stop
            for c in range(1, bit // 2 + 9 * bit + 1):
                await RisingEdge(dut.clk)
                v = tx_level(dut)
                if v != level:
                    edges.append(c)
                    level = v
                if c in sample_at:
                    samples[sample_at[c]] = v
            fr = len(self.frames)
            if samples[0] != 0:
                self.errors.append(f"frame {fr}: false start bit (line high at mid-start), edges {edges}")
            if samples[9] != 1:
                self.errors.append(f"frame {fr}: framing error, stop bit sampled {samples[9]}; "
                                   f"samples {[samples[k] for k in range(10)]}, edges at {edges} (bit={bit})")
            byte = sum(samples[1 + i] << i for i in range(8))
            self.frames.append(byte)
            for e in edges:
                k = round(e / bit)
                if 1 <= k <= 9:
                    dev = abs(e - k * bit) / (k * bit) if k else 0.0
                    self.max_dev = max(self.max_dev, dev)
            prev = level


async def send_bytes(dut, uio, data, wait_cycles):
    """Host side of the IN handshake for each byte (level contract only)."""
    for b in data:
        dut.ui_in.value = b
        uio |= 1 << T.HOST_GO_BIT
        dut.uio_in.value = uio
        await T.wait_host_status(dut, 1, max_cycles=wait_cycles, what=f"(byte {b:#04x} accepted)")
        uio &= ~(1 << T.HOST_GO_BIT)
        dut.uio_in.value = uio
        await T.wait_host_status(dut, 0, max_cycles=wait_cycles, what=f"(byte {b:#04x} handshake done)")
    return uio


async def run_uart(dut, baud, bit_cycles, max_dev):
    prog = assembler.assemble_file(UART_ASM, {"BAUD_CYCLES": bit_cycles})
    for w in prog.warnings:
        dut._log.info(f"asm: {w}")
    uio = await T.reset_and_boot(dut, prog.words)
    await ClockCycles(dut.clk, 20)
    assert tx_level(dut) == 1 and (int(dut.uio_oe.value) >> TX_PIN) & 1, "TX should idle high, driven push-pull"

    rx = UartRx(dut, bit_cycles)
    cocotb.start_soon(rx.run())
    # firmware takes the next byte only after the current frame's stop bit
    await send_bytes(dut, uio, PAYLOAD, wait_cycles=12 * bit_cycles)
    await ClockCycles(dut.clk, 11 * bit_cycles)  # last frame

    assert not rx.errors, f"{baud} baud: {rx.errors}"
    assert rx.frames == PAYLOAD, f"{baud} baud: received {[hex(b) for b in rx.frames]}, sent {[hex(b) for b in PAYLOAD]}"
    assert rx.max_dev <= max_dev + 1e-9, \
        f"{baud} baud: worst edge deviation {rx.max_dev:.3%} of a bit, limit {max_dev:.2%}"
    dut._log.info(f"UART {baud}: {len(rx.frames)} frames decoded, worst edge deviation {rx.max_dev:.3%}/bit")


@cocotb.test()
async def test_uart_tx_115200(dut):
    """115200 baud (434 cycles/bit): decoded bytes match and timing is exact."""
    await run_uart(dut, *BAUDS[0])


@cocotb.test()
async def test_uart_tx_57600(dut):
    """57600 baud: decoded bytes match; edge deviation within the
    0.92%/bit docs/protocol_timing.md predicts."""
    await run_uart(dut, *BAUDS[1])


@cocotb.test()
async def test_uart_tx_9600(dut):
    """9600 baud: decoded bytes match; edge deviation within the
    0.23%/bit docs/protocol_timing.md predicts."""
    await run_uart(dut, *BAUDS[2])
