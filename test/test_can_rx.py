# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""End-to-end protocol test: the CAN 2.0A receiver firmware
(firmware/protocols/can_rx.asm) on the RTL, fed by an independent CAN
transmitter node on a wired-AND bus.

The transmitter builds standard data frames from the CAN 2.0A rules (its
own CRC-15 and stuffing, shared with test_can.py's model), drives them
at a chosen bit time -- nominal 1000 cycles or +-1% off, which the chip
can only follow by resynchronizing on edges -- and checks at its own 75%
sample point that the chip drove ACK. Faults are injected to check the
chip's verdicts: a corrupted CRC, a missing stuff bit, a dominant CRC
delimiter.
"""

import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import test as T
from test_can import crc15, frame_bits, stuff

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

RX_ASM = Path(os.environ.get("CAN_RX_ASM") or
              Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "can_rx.asm")
TX, RX = 0, 1
LOOP_DELAY = 10
STATUS = {0: "good", 1: "stuff error", 2: "CRC error", 3: "form error"}


def frame_raw(ident, data, fault=None):
    """Raw bus bits SOF..EOF; fault in {None, 'crc', 'stuff', 'delim'}."""
    bits = frame_bits(ident, data)
    if fault == "crc":
        bits[-1] ^= 1                       # last CRC bit wrong
    raw = stuff(bits)
    if fault == "stuff":
        # the first stuff bit with the wrong polarity: six identical bits.
        # (Deleting it instead is not always a stuff error: if the next
        # data bit differs, a receiver rightly takes that as the stuff bit.)
        run, last = 0, None
        for i, b in enumerate(raw):
            run = run + 1 if b == last else 1
            last = b
            if run == 5:
                raw[i + 1] = b
                break
    tail = [1, 1, 1] + [1] * 7              # CRC delim, ACK slot, ACK delim, EOF
    if fault == "delim":
        tail[0] = 0
    return raw, len(raw) + 1                # index of the ACK slot


class CanTxNode:
    def __init__(self, dut, host_bits):
        self.dut, self.host = dut, host_bits
        self.t = 0
        self.hist = [1] * (LOOP_DELAY + 1)
        self.bits = None
        self.t0 = 0
        self.bit = 1000
        self.ack_idx = None
        self.acked = None

    def chip_level(self):
        if (int(self.dut.uio_oe.value) >> TX) & 1:
            return (int(self.dut.uio_out.value) >> TX) & 1
        return 1

    def drive(self):
        if self.bits is None:
            return 1
        k = (self.t - self.t0) // self.bit
        return self.bits[k] if k < len(self.bits) else 1

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            self.t += 1
            bus = self.drive() & self.chip_level()
            self.hist = self.hist[1:] + [bus]
            self.dut.uio_in.value = (self.host & ~(1 << RX)) | (self.hist[0] << RX)
            if self.bits is not None and self.ack_idx is not None:
                if self.t - self.t0 == self.ack_idx * self.bit + self.bit * 3 // 4:
                    self.acked = (bus == 0)

    async def send(self, ident, data, bit=1000, fault=None):
        raw, ack = frame_raw(ident, data, fault)
        tail = [1, 1, 1] + [1] * 7
        if fault == "delim":
            tail[0] = 0
        await ClockCycles(self.dut.clk, 12 * 1000)       # bus idle before SOF
        self.bits, self.bit, self.ack_idx, self.acked = raw + tail, bit, ack, None
        self.t0 = self.t + 1
        await ClockCycles(self.dut.clk, len(self.bits) * bit + 10)
        self.bits = None

    async def _go(self, level, status, max_cycles):
        self.host = (self.host | (1 << T.HOST_GO_BIT)) if level else (self.host & ~(1 << T.HOST_GO_BIT))
        await T.wait_host_status(self.dut, status, max_cycles=max_cycles)

    async def recv(self):
        await self._go(1, 1, 20000)
        v = int(self.dut.uo_out.value)
        await self._go(0, 0, 20000)
        return v


async def boot_rx(dut):
    prog = assembler.assemble_file(RX_ASM)
    assert not prog.warnings, prog.warnings
    img = prog.image()
    img += [0] * (len(img) % 2)
    words = [img[i] | (img[i + 1] << 8) for i in range(0, len(img), 2)]
    uio = await T.reset_and_boot(dut, words)
    node = CanTxNode(dut, uio)
    cocotb.start_soon(node.run())
    await ClockCycles(dut.clk, 20)
    return node


async def receive_and_check(dut, node, ident, data, bit=1000):
    await node.send(ident, data, bit=bit)
    st = await node.recv()
    assert st == 0, f"ID {ident:#x} at {bit} cycles/bit: status {st} ({STATUS.get(st)})"
    got = [await node.recv() for _ in range(3 + len(data))]
    assert (got[0] << 8 | got[1], got[2], got[3:]) == (ident, len(data), data), \
        f"received ID {got[0] << 8 | got[1]:#x} DLC {got[2]} data {[hex(x) for x in got[3:]]}"
    assert node.acked, f"ID {ident:#x}: the chip did not ACK a good frame"


@cocotb.test()
async def test_can_rx_frames(dut):
    """Frames of 0-8 bytes, incl. stuffing-heavy ones, at the nominal bit
    time: decoded exactly, ACKed, handed to the host."""
    node = await boot_rx(dut)
    rng = random.Random(5)
    for ident, data in [(0x123, [0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]),
                        (0x000, [0x00] * 8), (0x7FF, [0xFF] * 3), (0x2A5, []),
                        (0x0F0, [rng.randrange(256) for _ in range(6)])]:
        await receive_and_check(dut, node, ident, data)


@cocotb.test()
async def test_can_rx_clock_tolerance(dut):
    """Transmitters 1% slow and 1% fast (1010 and 990 cycles per bit): the
    chip resyncs on edges and still decodes and ACKs, including long
    stuffing-limited runs between edges."""
    node = await boot_rx(dut)
    for bit in (1010, 990):
        await receive_and_check(dut, node, 0x3C5, [0xF0, 0x0F, 0x00, 0xFF, 0x5A], bit=bit)
        await receive_and_check(dut, node, 0x000, [0x00] * 8, bit=bit)


@cocotb.test()
async def test_can_rx_errors(dut):
    """Bad frames are reported, not ACKed: CRC error (2), stuff error (1),
    dominant CRC delimiter (3). A good frame after them is received."""
    node = await boot_rx(dut)
    for fault, want in (("crc", 2), ("stuff", 1), ("delim", 3)):
        await node.send(0x155, [0xAB, 0xCD], fault=fault)
        st = await node.recv()
        assert st == want, f"{fault}: status {st} ({STATUS.get(st)}), want {want}"
        assert not node.acked, f"{fault}: a bad frame was ACKed"
    await receive_and_check(dut, node, 0x155, [0xAB, 0xCD])
