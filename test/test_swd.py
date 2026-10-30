# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""End-to-end protocol test: the SWD host firmware
(firmware/protocols/swd.asm) on the RTL, talking to an independent SWD
target model (an ARM ADIv5 debug port) written from the SWD protocol.

The target clocks everything on SWCLK rising edges: it needs the
JTAG-to-SWD switch (>= 50 ones, 0xE79E, >= 50 ones) and idle cycles
before it answers; it checks each request's start, parity, stop and park
bits and stays silent on a bad one; it drives ACK and read data just
after rising edges, with a turnaround cycle each way, and checks the
parity of write data. If the chip drives SWDIO while the target owns it,
that is contention: an error. SWDIO has a pull-up (an undriven line
reads 1).
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import test as T

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

# SWD_ASM overrides the firmware, for checking that broken variants fail
SWD_ASM = Path(os.environ.get("SWD_ASM") or
               Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "swd.asm")
DIO, CLK = 0, 2
IDCODE = 0x2BA01477          # a real Cortex-M3 DP IDCODE
ACK_OK, ACK_WAIT = 0b001, 0b010


def parity(x):
    return bin(x).count("1") & 1


class SwdTarget:
    def __init__(self, dut, host_bits):
        self.dut, self.host = dut, host_bits
        self.errors: list[str] = []
        self.regs = {0x4: 0, 0x8: 0}          # CTRL/STAT, SELECT
        self.ack = ACK_OK
        self.corrupt_parity = False
        self.swd = False                       # in SWD mode (after the switch)
        self.ones = 0
        self.hist: list[int] = []              # bits since the last >= 50 ones
        self.state = "reset"
        self.drive: int | None = None          # value the target drives, or None
        self.q: list = []                      # actions for the coming rising edges
        self.req: list[int] = []
        self.wbits: list[int] = []
        self.write_log: list[tuple] = []
        self.prev_clk = 0

    def chip_drives(self):
        return (int(self.dut.uio_oe.value) >> DIO) & 1

    def line(self):
        chip = (int(self.dut.uio_out.value) >> DIO) & 1 if self.chip_drives() else None
        if chip is not None and self.drive is not None:
            self.errors.append("contention on SWDIO")
        if chip is not None:
            return chip
        return self.drive if self.drive is not None else 1

    def clk(self):
        if not (int(self.dut.uio_oe.value) >> CLK) & 1:
            return 0
        return (int(self.dut.uio_out.value) >> CLK) & 1

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            c = self.clk()
            if c and not self.prev_clk:
                self._rise(self.line())
            self.prev_clk = c
            v = self.line()
            self.dut.uio_in.value = (self.host & ~(1 << DIO)) | (v << DIO)

    def _rise(self, b):
        # line reset / mode switch tracking (only while the host drives)
        if self.drive is None and not self.q:
            if b:
                self.ones += 1
            else:
                if self.ones >= 50:
                    self.state = "idle_after_reset"
                self.ones = 0
            if self.ones == 50:
                # hist holds every bit since the previous >= 50 ones: the
                # switch code must be its first 16 (its trailing 1s already
                # count toward this run, so look from the front)
                if len(self.hist) >= 16:
                    seq = sum(bit << i for i, bit in enumerate(self.hist[:16]))
                    if seq == 0xE79E:
                        self.swd = True
            if self.ones >= 50:
                self.hist, self.state, self.req = [], "reset", []
                return
            self.hist.append(b)
        if self.q:                              # a transfer in progress
            act = self.q.pop(0)
            act(b)
            return
        if not self.swd or self.state == "reset":
            return
        if self.state == "idle_after_reset":
            if b == 0:
                self.state = "idle"
            return
        # idle: a 1 is a start bit
        if not self.req and b == 0:
            return
        self.req.append(b)
        if len(self.req) == 8:
            r, self.req = self.req, []
            self._request(r)

    def _request(self, r):
        start, apndp, rnw, a2, a3, par, stop, park = r
        if start != 1 or stop != 0 or park != 1 or par != (apndp ^ rnw ^ a2 ^ a3):
            self.errors.append(f"bad request {r}")
            return                              # no response
        addr = (a3 << 3) | (a2 << 2)
        ack = self.ack
        ackbits = [(ack >> i) & 1 for i in range(3)]

        def drive(v):
            return lambda b: self._set(v)
        # after the turnaround rise the target drives ACK bit 0; each later
        # rise moves to the next bit
        q = [drive(ackbits[0]), drive(ackbits[1]), drive(ackbits[2])]
        # each turnaround is a full period: the target releases SWDIO on one
        # rising edge and must ignore the next (the line just floats high)
        trn = [drive(None), lambda b: None]
        if ack != ACK_OK:
            self.q = q + trn
            return
        if rnw:
            val = IDCODE if (addr == 0 and not apndp) else self.regs.get(addr, 0)
            p = parity(val) ^ (1 if self.corrupt_parity else 0)
            bits = [(val >> i) & 1 for i in range(32)] + [p]
            q += [drive(x) for x in bits] + trn
        else:
            q += trn                            # turnaround: host drives next
            got = []

            def take(b, got=got, addr=addr):
                got.append(b)
                if len(got) == 33:
                    val = sum(x << i for i, x in enumerate(got[:32]))
                    if got[32] != parity(val):
                        self.errors.append(f"write parity wrong for {val:#x}")
                    self.regs[addr] = val
                    self.write_log.append((addr, val))
            q += [take] * 33
        self.q = q

    def _set(self, v):
        self.drive = v

    # ---- host side
    async def _go(self, level, status):
        self.host = (self.host | (1 << T.HOST_GO_BIT)) if level else (self.host & ~(1 << T.HOST_GO_BIT))
        await T.wait_host_status(self.dut, status, max_cycles=60000)

    async def send(self, b):
        self.dut.ui_in.value = b
        await self._go(1, 1)
        await self._go(0, 0)

    async def recv(self):
        await self._go(1, 1)
        v = int(self.dut.uo_out.value)
        await self._go(0, 0)
        return v

    async def line_reset(self):
        await self.send(1)
        return await self.recv()

    async def read(self, apndp, addr):
        await self.send(2)
        await self.send(apndp | 1 << 1 | ((addr >> 2) & 3) << 2)
        ack = await self.recv()
        if ack != ACK_OK:
            return ack, None, None
        data = [await self.recv() for _ in range(4)]
        ok = await self.recv()
        return ack, sum(d << 8 * i for i, d in enumerate(data)), ok

    async def write(self, apndp, addr, val):
        await self.send(2)
        await self.send(apndp | 0 << 1 | ((addr >> 2) & 3) << 2)
        for i in range(4):
            await self.send((val >> 8 * i) & 0xFF)
        return await self.recv()


async def boot_swd(dut):
    prog = assembler.assemble_file(SWD_ASM)
    assert not prog.warnings, prog.warnings
    uio = await T.reset_and_boot(dut, prog.words)
    tgt = SwdTarget(dut, uio)
    cocotb.start_soon(tgt.run())
    await ClockCycles(dut.clk, 20)
    return tgt


@cocotb.test()
async def test_swd_read_idcode(dut):
    """JTAG-to-SWD switch, then a DP IDCODE read (request 0xA5): ACK OK,
    0x2BA01477, data parity right, no contention."""
    tgt = await boot_swd(dut)
    assert await tgt.line_reset() == 0
    assert tgt.swd, "target never saw a valid JTAG-to-SWD switch sequence"
    ack, val, ok = await tgt.read(0, 0x0)
    assert not tgt.errors, tgt.errors
    assert (ack, val, ok) == (ACK_OK, IDCODE, 1), (ack, hex(val or 0), ok)
    dut._log.info(f"SWD: IDCODE {val:#010x}")


@cocotb.test()
async def test_swd_write_then_read(dut):
    """Write DP CTRL/STAT (power-up requests), read it back; the target
    checked the write's parity; then IDCODE again."""
    tgt = await boot_swd(dut)
    await tgt.line_reset()
    for v in (0x50000000, 0x0000F0F1, 0xFFFFFFFF):
        assert await tgt.write(0, 0x4, v) == ACK_OK
        ack, val, ok = await tgt.read(0, 0x4)
        assert (ack, val, ok) == (ACK_OK, v, 1), (ack, hex(val or 0), ok)
    assert tgt.write_log == [(0x4, 0x50000000), (0x4, 0x0000F0F1), (0x4, 0xFFFFFFFF)]
    assert not tgt.errors, tgt.errors


@cocotb.test()
async def test_swd_wait_and_bad_parity(dut):
    """A WAIT ack ends the transfer with no data phase (and no contention);
    a read whose data parity the target corrupts is flagged (ok = 0)."""
    tgt = await boot_swd(dut)
    await tgt.line_reset()
    tgt.ack = ACK_WAIT
    ack, _, _ = await tgt.read(0, 0x0)
    assert ack == ACK_WAIT, ack
    tgt.ack = ACK_OK
    tgt.corrupt_parity = True
    ack, val, ok = await tgt.read(0, 0x0)
    assert (ack, val, ok) == (ACK_OK, IDCODE, 0), (ack, hex(val or 0), ok)
    tgt.corrupt_parity = False
    assert (await tgt.read(0, 0x0))[2] == 1
    assert not tgt.errors, tgt.errors


@cocotb.test()
async def test_swd_no_switch_no_answer(dut):
    """Without the JTAG-to-SWD switch the target never answers: the chip
    reads ACK 0b111 (the pull-up), not a made-up OK."""
    tgt = await boot_swd(dut)
    ack, _, _ = await tgt.read(0, 0x0)
    assert ack == 0b111, ack
