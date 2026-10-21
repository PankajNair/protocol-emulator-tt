# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""End-to-end protocol test: the WS2812 LED firmware
(firmware/protocols/ws2812.asm) running on the RTL, decoded by an
independent model of the LED's input, written from the WS2812B datasheet.

WS2812 is not one of the protocols this chip was designed around: this is
the "reprogrammable for a protocol it wasn't built for" check, so the
decoder holds the firmware to the datasheet, not to the firmware's own
timing comments.

The decoder sees only the data pin as driven (uio_oe[0] must be 1; an
undriven line is an error, the LED input has no defined idle). Datasheet
timing, each +-150 ns: T0H 400 ns, T1H 800 ns, T0L 850 ns, T1L 450 ns;
a low longer than 50 us is the reset that latches the frame.
"""

import random
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import test as T  # shared boot/handshake helpers

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

WS_ASM = Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "ws2812.asm"
D_PIN = 0
NS_PER_CYCLE = 20  # 50 MHz
TOL = 150
T0H, T1H, T0L, T1L = 400, 800, 850, 450
RESET_NS = 50_000
# The firmware's own budget (ws2812.asm header, from the ISA's cycle
# rules): exact, not just inside the datasheet windows. A one-cycle drift
# in DELAY or an instruction's cost is inside the datasheet tolerance but
# breaks the ISA's cycle-exact promise, so it fails here too.
EXACT_HIGHS = {400, 800}
EXACT_LOWS = {760, 360, 940, 540}  # T0L, T1L, then both at a byte boundary


class Ws2812Rx:
    """One LED chain input. Collects frames (lists of bytes, MSB first);
    every pulse that misses a datasheet window is an error."""

    def __init__(self, dut):
        self.dut = dut
        self.frames: list[list[int]] = []
        self.errors: list[str] = []
        self.highs: set[int] = set()   # distinct high times seen (ns)
        self.lows: set[int] = set()    # distinct in-frame low times seen (ns)

    def level(self):
        if not (int(self.dut.uio_oe.value) >> D_PIN) & 1:
            return None
        return (int(self.dut.uio_out.value) >> D_PIN) & 1

    async def run(self):
        bits: list[int] = []
        level, run = self.level(), 0
        last_bit = None
        while True:
            await RisingEdge(self.dut.clk)
            v = self.level()
            if v is None:
                self.errors.append("data line undriven")
                return
            run += 1
            if level == 0 and run * NS_PER_CYCLE > RESET_NS and bits:
                self._end_frame(bits)  # reset: latch what was received
                bits, last_bit = [], None
            if v == level:
                continue
            ns = run * NS_PER_CYCLE
            if level == 1:  # a high pulse ended: classify the bit
                self.highs.add(ns)
                if abs(ns - T0H) <= TOL:
                    last_bit = 0
                elif abs(ns - T1H) <= TOL:
                    last_bit = 1
                else:
                    self.errors.append(f"high pulse {ns} ns is neither a 0 nor a 1 (bit {len(bits)})")
                    last_bit = None
                if last_bit is not None:
                    bits.append(last_bit)
            elif bits and ns <= RESET_NS:  # a low inside the frame
                self.lows.add(ns)
                want = T0L if last_bit == 0 else T1L
                if abs(ns - want) > TOL:
                    self.errors.append(f"low {ns} ns after a {last_bit} bit, want {want}+-{TOL} (bit {len(bits)})")
            level, run = v, 0

    def _end_frame(self, bits):
        if len(bits) % 8:
            self.errors.append(f"frame of {len(bits)} bits is not whole bytes")
        self.frames.append([int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits) - 7, 8)])


def image_words(prog):
    img = prog.image()
    img += [0] * (len(img) % 2)
    return [img[i] | (img[i + 1] << 8) for i in range(0, len(img), 2)]


async def boot_ws2812(dut, frame=None):
    """Boot the firmware; `frame` (bytes in send order) replaces the demo
    frame in the data region. Returns (uio state, sent frame, receiver)."""
    defines = {"NBYTES": len(frame)} if frame is not None else {}
    prog = assembler.assemble_file(WS_ASM, defines)
    if frame is not None:
        prog.data = [0] + frame[::-1]  # offset NBYTES is sent first
    sent = prog.data[1:][::-1]
    rx = Ws2812Rx(dut)
    uio = await T.reset_and_boot(dut, image_words(prog))
    await ClockCycles(dut.clk, 20)
    assert (int(dut.uio_oe.value) >> D_PIN) & 1, "data pin should be driven push-pull"
    cocotb.start_soon(rx.run())
    return uio, sent, rx


async def wait_frames(dut, rx, n, sent):
    # frame time + the 300 us latch, generously
    budget = len(sent) * 8 * 80 + 40_000
    for _ in range(budget):
        await RisingEdge(dut.clk)
        if len(rx.frames) >= n:
            return
    assert False, f"only {len(rx.frames)} of {n} frames latched; errors {rx.errors[:5]}"


@cocotb.test()
async def test_ws2812_demo_frame(dut):
    """The demo frame (3 LEDs: red, green, blue) decodes exactly, inside
    every datasheet window; HOST_STATUS rises once it is latched; the host
    handshake sends it again."""
    uio, sent, rx = await boot_ws2812(dut)
    assert sent == [0x00, 0x20, 0x00, 0x20, 0x00, 0x00, 0x00, 0x00, 0x20], sent
    await wait_frames(dut, rx, 1, sent)
    # the decoder latches at 50 us of low; firmware holds 300 us, then acks
    await T.wait_host_status(dut, 1, max_cycles=16_000, what="(frame latched)")

    uio |= 1 << T.HOST_GO_BIT
    dut.uio_in.value = uio
    await T.wait_host_status(dut, 0, max_cycles=200, what="(resend accepted)")
    uio &= ~(1 << T.HOST_GO_BIT)
    dut.uio_in.value = uio
    await wait_frames(dut, rx, 2, sent)

    assert not rx.errors, rx.errors[:10]
    assert rx.frames == [sent, sent], [[hex(b) for b in f] for f in rx.frames]
    assert rx.highs == EXACT_HIGHS and rx.lows <= EXACT_LOWS, (sorted(rx.highs), sorted(rx.lows))
    dut._log.info(f"WS2812: 2 frames decoded; high times {sorted(rx.highs)} ns, low times {sorted(rx.lows)} ns")


@cocotb.test()
async def test_ws2812_full_length_frame(dut):
    """The longest frame LOADX can reach (255 bytes, 85 LEDs), random
    data plus 0x00/0xFF/0xA5/0x5A so every bit pair occurs inside a byte
    and across a byte boundary: decodes exactly, every pulse in spec."""
    rng = random.Random(2812)
    frame = [0x00, 0xFF, 0xA5, 0x5A, 0x01, 0x80] + [rng.randrange(256) for _ in range(249)]
    _, sent, rx = await boot_ws2812(dut, frame)
    await wait_frames(dut, rx, 1, sent)
    assert not rx.errors, rx.errors[:10]
    assert rx.frames[0] == frame, "decoded frame differs from the one in the data region"
    # both byte-boundary variants of each low time were exercised
    assert rx.highs == EXACT_HIGHS, f"high times {sorted(rx.highs)} ns, budget {sorted(EXACT_HIGHS)}"
    assert rx.lows == EXACT_LOWS, f"low times {sorted(rx.lows)} ns, budget {sorted(EXACT_LOWS)} (T0L, T1L, each at a byte boundary)"
    dut._log.info(f"WS2812: 255-byte frame decoded; high {sorted(rx.highs)} ns, low {sorted(rx.lows)} ns")
