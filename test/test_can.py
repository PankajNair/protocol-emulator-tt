# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""End-to-end protocol test: the CAN 2.0A transmitter firmware
(firmware/protocols/can_tx.asm) on the RTL, on a bus with independent CAN
nodes written from the CAN 2.0A / ISO 11898-1 frame rules.

Bus: wired-AND (dominant 0 wins) of the chip's TX pin and every model
node; the chip's RX pin sees the bus after a transceiver loop delay. The
receiver node hard-syncs on SOF after bus idle, samples each bit at 75%,
removes stuff bits (a missing one is a stuff error), checks the CRC-15 with
its own implementation and drives ACK only for a good frame. A competing
transmitter node (arbitration test) and an error injector can join.

Every edge the chip drives must fall exactly on the bit grid set by its
SOF: the firmware balances every branch so each bit is BIT cycles, and
this is where that is checked.
"""

import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import test as T

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

CAN_ASM = Path(os.environ.get("CAN_ASM") or
               Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "can_tx.asm")
TX, RX = 0, 1
BIT = 1000                 # 50 kbit/s at 50 MHz
SAMPLE = BIT * 3 // 4
LOOP_DELAY = 10            # transceiver TXD -> bus -> RXD, cycles
STATUS = {0: "sent", 1: "no ACK", 2: "lost arbitration", 3: "bit error"}


def crc15(bits):
    crc = 0
    for b in bits:
        nxt = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= 0x4599
    return crc


def frame_bits(ident, data):
    """Unstuffed SOF..CRC of a standard data frame."""
    dlc = len(data)
    bits = [0] + [(ident >> i) & 1 for i in range(10, -1, -1)] + [0, 0, 0]
    bits += [(dlc >> i) & 1 for i in range(3, -1, -1)]
    for d in data:
        bits += [(d >> i) & 1 for i in range(7, -1, -1)]
    c = crc15(bits)
    return bits + [(c >> i) & 1 for i in range(14, -1, -1)]


def stuff(bits):
    out, run, last = [], 0, None
    for b in bits:
        out.append(b)
        run = run + 1 if b == last else 1
        last = b
        if run == 5:
            out.append(1 - b)
            run, last = 1, 1 - b
    return out


class CanBus:
    def __init__(self, dut, host_bits, ack=True, competitor=None, force_dominant_bit=None):
        self.dut, self.host = dut, host_bits
        self.ack = ack
        # competitor: stuffed bit list driven from the chip's SOF on the same grid
        self.comp = stuff(frame_bits(*competitor)) if competitor else None
        self.force_bit = force_dominant_bit  # raw bit index forced dominant
        self.t = 0
        self.hist = [1] * (LOOP_DELAY + 1)
        self.idle = 0
        self.t0 = None
        self.raw: list[int] = []
        self.errors: list[str] = []
        self.chip_edges: list[int] = []
        self.prev_chip = 1
        self.ack_from = None
        self.frames: list[tuple] = []
        self.done = False

    def chip_level(self):
        if (int(self.dut.uio_oe.value) >> TX) & 1:
            return (int(self.dut.uio_out.value) >> TX) & 1
        return 1

    def node_drive(self):
        if self.t0 is None:
            return 1
        k = (self.t - self.t0) // BIT
        if self.comp is not None and k < len(self.comp) and self.comp[k] == 0:
            return 0
        if self.force_bit is not None and k == self.force_bit:
            return 0
        if self.ack_from is not None and self.ack_from <= self.t < self.ack_from + BIT:
            return 0
        return 1

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            self.t += 1
            c = self.chip_level()
            if c != self.prev_chip:
                self.chip_edges.append(self.t)
                self.prev_chip = c
            bus = c & self.node_drive()
            self.hist = self.hist[1:] + [bus]
            rx = self.hist[0]  # bus as the chip sees it, LOOP_DELAY cycles late
            self.dut.uio_in.value = (self.host & ~(1 << RX)) | (rx << RX)
            self._receive(bus)

    def _receive(self, bus):
        if self.t0 is None:
            if bus == 1:
                self.idle += 1
            else:
                if self.idle >= 11 * BIT:
                    self.t0, self.raw, self.ack_from = self.t, [], None
                self.idle = 0
            return
        if (self.t - self.t0) % BIT == SAMPLE:
            self.raw.append(bus)
            self._decode()

    def _decode(self):
        """Destuff SOF..CRC as bits arrive; ACK a good frame; finish at EOF."""
        un, run, last, i, n = [], 0, None, 0, len(self.raw)
        need = None
        while i < n:
            b = self.raw[i]
            if run == 5:                 # this raw bit must be a stuff bit
                if b == last:
                    self._end(f"stuff error at raw bit {i}")
                    return
                run, last, i = 1, b, i + 1
                continue
            if need is not None and len(un) == need:
                break
            un.append(b)
            run = run + 1 if b == last else 1
            last = b
            i += 1
            if len(un) == 19:
                dlc = sum(un[15 + j] << (3 - j) for j in range(4))
                need = 19 + 8 * min(dlc, 8) + 15
        if need is None or len(un) < need:
            return
        if run == 5 and i == n:          # a stuff bit is still due after the CRC
            return
        tail = self.raw[i:]              # CRC delimiter, ACK, ACK delimiter, EOF
        if len(tail) == 1:               # delimiter just sampled: ACK the next bit?
            ok = crc15(un[:need - 15]) == sum(un[need - 15 + j] << (14 - j) for j in range(15))
            if tail[0] != 1:
                self.errors.append("CRC delimiter dominant")
            if ok and self.ack:
                self.ack_from = self.t0 + (n) * BIT   # start of the next bit
            if not ok:
                self.errors.append("CRC mismatch")
        if len(tail) == 10:
            if tail[1] != 0 and self.ack:
                self.errors.append("ACK slot not dominant")
            if tail[2] != 1 or any(x != 1 for x in tail[3:]):
                self.errors.append(f"ACK delimiter / EOF not recessive: {tail}")
            ident = sum(un[1 + j] << (10 - j) for j in range(11))
            dlc = sum(un[15 + j] << (3 - j) for j in range(4))
            data = [sum(un[19 + 8 * k + j] << (7 - j) for j in range(8)) for k in range(min(dlc, 8))]
            self.frames.append((ident, data))
            self._end(None)

    def _end(self, err):
        if err:
            self.errors.append(err)
        self.t0, self.idle, self.done = None, 0, True

    # ---- host side
    async def _go(self, level, status, max_cycles):
        self.host = (self.host | (1 << T.HOST_GO_BIT)) if level else (self.host & ~(1 << T.HOST_GO_BIT))
        await T.wait_host_status(self.dut, status, max_cycles=max_cycles)

    async def send_byte(self, b):
        self.dut.ui_in.value = b
        await self._go(1, 1, 4000)
        await self._go(0, 0, 4000)

    async def transmit(self, ident, data):
        await self.send_byte(ident >> 8)
        await self.send_byte(ident & 0xFF)
        await self.send_byte(len(data))
        for k in range(8):
            await self.send_byte(data[k] if k < len(data) else 0)
        await self._go(1, 1, 200 * BIT)          # status, after the frame
        st = int(self.dut.uo_out.value)
        await self._go(0, 0, 4000)
        return st


async def boot_can(dut, **kw):
    prog = assembler.assemble_file(CAN_ASM)
    assert not prog.warnings, prog.warnings
    words = []
    img = prog.image() + [0] * (prog.image().__len__() % 2)
    words = [img[i] | (img[i + 1] << 8) for i in range(0, len(img), 2)]
    uio = await T.reset_and_boot(dut, words)
    bus = CanBus(dut, uio, **kw)
    cocotb.start_soon(bus.run())
    await ClockCycles(dut.clk, 20)
    assert bus.chip_level() == 1, "TX should idle recessive"
    return bus


def check_grid(bus):
    """Every chip edge on the BIT grid from its own frame's SOF edge (a new
    frame starts after >= 11 idle bits, so each frame has its own phase)."""
    e = bus.chip_edges
    assert e, "chip never drove TX"
    bad, sof, frames = [], e[0], 1
    for prev, x in zip([None] + e, e):
        if prev is not None and x - prev > 11 * BIT:
            sof, frames = x, frames + 1
        if (x - sof) % BIT:
            bad.append((x - sof, (x - sof) % BIT))
    assert not bad, f"chip edges off the {BIT}-cycle grid (time from SOF, offset): {bad[:5]}"
    return frames


@cocotb.test()
async def test_can_frame_8_bytes(dut):
    """ID 0x123, 8 bytes: the receiver decodes it (stuffing, CRC-15) and
    ACKs; status 0; every chip edge on the 1000-cycle grid."""
    bus = await boot_can(dut)
    data = [0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]
    st = await bus.transmit(0x123, data)
    assert not bus.errors, bus.errors
    assert bus.frames == [(0x123, data)], bus.frames
    assert st == 0, f"status {st} ({STATUS.get(st)})"
    check_grid(bus)
    n_stuff = len(bus.raw) - len(frame_bits(0x123, data)) - 10
    dut._log.info(f"CAN: ID 0x123 + 8 bytes decoded and ACKed, {len(bus.raw)} bits "
                  f"({n_stuff} stuff), {len(bus.chip_edges)} chip edges all on the grid")


@cocotb.test()
async def test_can_stuffing_and_lengths(dut):
    """Stuffing-heavy frames (all-0 and all-1 data, an ID with long runs)
    and the shortest frames (DLC 0 and 1) decode exactly; stuff bits match
    the rule, including one right after the CRC when it ends a run."""
    bus = await boot_can(dut)
    rng = random.Random(11)
    cases = [(0x000, [0x00] * 8), (0x7FF, [0xFF] * 8), (0x0F0, []), (0x555, [0xA5]),
             (0x3C1, [rng.randrange(256) for _ in range(5)])]
    for ident, data in cases:
        bus.done = False
        st = await bus.transmit(ident, data)
        assert st == 0, f"ID {ident:#x}: status {st} ({STATUS.get(st)}); errors {bus.errors}"
        await ClockCycles(dut.clk, 100)
    assert not bus.errors, bus.errors
    assert bus.frames == cases, bus.frames
    assert check_grid(bus) == len(cases)


@cocotb.test()
async def test_can_no_ack(dut):
    """No node ACKs: status 1, TX released."""
    bus = await boot_can(dut, ack=False)
    st = await bus.transmit(0x123, [1, 2])
    assert st == 1, f"status {st} ({STATUS.get(st)})"
    assert bus.chip_level() == 1


@cocotb.test()
async def test_can_lost_arbitration(dut):
    """Another node sends ID 0x120 at the same time; 0x123 loses at the
    first recessive bit where 0x120 is dominant: status 2, and the winner's
    frame still gets through intact."""
    bus = await boot_can(dut, competitor=(0x120, [0xDE, 0xAD]))
    st = await bus.transmit(0x123, [1, 2, 3])
    assert st == 2, f"status {st} ({STATUS.get(st)})"
    await ClockCycles(dut.clk, 60 * BIT)
    assert (0x120, [0xDE, 0xAD]) in bus.frames, bus.frames
    assert bus.chip_level() == 1


@cocotb.test()
async def test_can_bit_error(dut):
    """The bus is forced dominant during a recessive data bit (outside
    arbitration): status 3, TX released at once."""
    data = [0xFF, 0x00]
    raw = stuff(frame_bits(0x123, data))
    k = next(i for i in range(30, len(raw)) if raw[i] == 1)
    bus = await boot_can(dut, force_dominant_bit=k)
    st = await bus.transmit(0x123, data)
    assert st == 3, f"status {st} ({STATUS.get(st)})"
    assert bus.chip_level() == 1
