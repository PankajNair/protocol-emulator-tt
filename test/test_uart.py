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


# ---------------------------------------------------------------------------
# UART RX: firmware/protocols/uart_rx.asm, driven by a transmitter model.
# ---------------------------------------------------------------------------

UART_RX_ASM = Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "uart_rx.asm"
RX_PIN = 1
HOST_ERROR_BIT = 6


class UioBus:
    """uio_in is one 8-bit port shared by the transmitter model (RX pin)
    and the host (HOST_GO): each sets its own bit without clobbering the
    other's."""

    def __init__(self, dut, initial: int):
        self.dut, self.val = dut, initial

    def set(self, bit: int, level: int):
        self.val = (self.val & ~(1 << bit)) | ((level & 1) << bit)
        self.dut.uio_in.value = self.val


async def uart_send(dut, bus: UioBus, frames, period: float):
    """Transmit 8N1 frames back to back on RX. `frames`: list of
    (byte, stop_bit). `period` may be fractional (a mismatched baud):
    edges land at the nearest cycle to start + k*period."""
    t = 0.0
    now = 0

    async def hold(level, bits):
        nonlocal t, now
        bus.set(RX_PIN, level)
        t += bits * period
        n = round(t) - now
        now += n
        await ClockCycles(dut.clk, n)

    for byte, stop in frames:
        await hold(0, 1)
        for i in range(8):
            await hold((byte >> i) & 1, 1)
        await hold(stop, 1)
        if stop == 0:
            await hold(1, 3)  # line back to idle after a bad stop bit
    bus.set(RX_PIN, 1)


async def host_collect(dut, bus: UioBus, n: int, max_wait: int):
    """Host side: keep a request (HOST_GO) raised ahead of each byte, read
    it on HOST_STATUS, release -- the host-initiated OUT handshake."""
    got = []
    for _ in range(n):
        bus.set(T.HOST_GO_BIT, 1)
        await T.wait_host_status(dut, 1, max_cycles=max_wait, what="(byte ready)")
        got.append(int(dut.uo_out.value))
        bus.set(T.HOST_GO_BIT, 0)
        await T.wait_host_status(dut, 0, max_cycles=max_wait, what="(byte released)")
    return got


def host_error(dut) -> int:
    return (int(dut.uio_oe.value) >> HOST_ERROR_BIT) & (int(dut.uio_out.value) >> HOST_ERROR_BIT) & 1


async def run_uart_rx(dut, bit_cycles: int, period: float, frames):
    prog = assembler.assemble_file(UART_RX_ASM, {"BAUD_CYCLES": bit_cycles})
    uio = await T.reset_and_boot(dut, prog.words)
    bus = UioBus(dut, uio)
    bus.set(RX_PIN, 1)  # idle line
    await ClockCycles(dut.clk, 3 * bit_cycles)
    assert (int(dut.uio_oe.value) >> RX_PIN) & 1 == 0, "RX pin must be an input"

    sender = cocotb.start_soon(uart_send(dut, bus, frames, period))
    got = await host_collect(dut, bus, len(frames), max_wait=int(14 * period) + 200)
    await sender
    return got


RX_PAYLOAD = [0x55, 0xAA, 0x00, 0xFF, 0x3C, 0x81]


@cocotb.test()
async def test_uart_rx_115200(dut):
    """115200 baud, back-to-back frames: every byte decoded, no framing error."""
    got = await run_uart_rx(dut, 434, 434.0, [(b, 1) for b in RX_PAYLOAD])
    assert got == RX_PAYLOAD, f"received {[hex(b) for b in got]}"
    assert host_error(dut) == 0, "HOST_ERROR raised on clean frames"


@cocotb.test()
async def test_uart_rx_baud_tolerance(dut):
    """The sender's clock is off by +-2% (a typical UART tolerance budget):
    firmware built for 115200 still decodes every back-to-back frame --
    proves the sample points sit near bit centres, not at an edge."""
    for err in (+0.02, -0.02):
        got = await run_uart_rx(dut, 434, 434.0 * (1 + err), [(b, 1) for b in RX_PAYLOAD])
        assert got == RX_PAYLOAD, f"sender {err:+.0%}: received {[hex(b) for b in got]}"
        assert host_error(dut) == 0, f"sender {err:+.0%}: spurious framing error"


@cocotb.test()
async def test_uart_rx_9600(dut):
    """9600 baud (DELAY rounding in play): every byte decoded."""
    got = await run_uart_rx(dut, 5208, 5208.0, [(b, 1) for b in RX_PAYLOAD[:3]])
    assert got == RX_PAYLOAD[:3], f"received {[hex(b) for b in got]}"
    assert host_error(dut) == 0


@cocotb.test()
async def test_uart_rx_framing_error(dut):
    """A frame whose stop bit is 0: the byte is still delivered, HOST_ERROR
    goes high and stays high, and the receiver resynchronizes on the next
    good frame instead of misframing on the still-low line."""
    frames = [(0x5A, 1), (0xC3, 0), (0x7E, 1)]
    got = await run_uart_rx(dut, 434, 434.0, frames)
    assert got == [0x5A, 0xC3, 0x7E], f"received {[hex(b) for b in got]}"
    assert host_error(dut) == 1, "framing error should raise HOST_ERROR"
