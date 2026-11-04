# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""End-to-end protocol test: the JTAG host firmware
(firmware/protocols/jtag.asm) on the RTL, driving an independent scan
chain of two IEEE 1149.1 devices written from the standard.

Chain: chip TDI -> device A (4-bit IR, an ARM JTAG-DP) -> device B (6-bit
IR, an FPGA) -> chip TDO. Each device runs the full 16-state TAP
controller on TCK rising edges, captures 0b..01 into its IR, resets its
IR to IDCODE in Test-Logic-Reset, and moves TDO only on falling edges
(undriven outside Shift-xR: TDO has a pull-up). Device A also has a
32-bit USER data register. The model checks that TMS and TDI never move
while TCK is high, and that each scan ends with every TAP in
Run-Test/Idle.
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import test as T

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

# JTAG_ASM overrides the firmware, for checking that broken variants fail
JTAG_ASM = Path(os.environ.get("JTAG_ASM") or
                Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "jtag.asm")
TDI, TMS, TCK, TDO = 0, 1, 2, 3

# TAP states and their (TMS=0, TMS=1) successors, IEEE 1149.1 figure 6-1
NEXT = {
    "TLR": ("RTI", "TLR"), "RTI": ("RTI", "SELDR"),
    "SELDR": ("CAPDR", "SELIR"), "CAPDR": ("SHDR", "EX1DR"),
    "SHDR": ("SHDR", "EX1DR"), "EX1DR": ("PAUDR", "UPDR"),
    "PAUDR": ("PAUDR", "EX2DR"), "EX2DR": ("SHDR", "UPDR"),
    "UPDR": ("RTI", "SELDR"),
    "SELIR": ("CAPIR", "TLR"), "CAPIR": ("SHIR", "EX1IR"),
    "SHIR": ("SHIR", "EX1IR"), "EX1IR": ("PAUIR", "UPIR"),
    "PAUIR": ("PAUIR", "EX2IR"), "EX2IR": ("SHIR", "UPIR"),
    "UPIR": ("RTI", "SELDR"),
}


class TapDevice:
    def __init__(self, name, irlen, idcode, idcode_ir, user_ir=None):
        self.name, self.irlen, self.idcode = name, irlen, idcode
        self.idcode_ir, self.user_ir = idcode_ir, user_ir
        self.bypass_ir = (1 << irlen) - 1
        self.state = "TLR"
        self.ir = idcode_ir
        self.user = 0
        self.sr, self.srlen = 0, 0
        self.tdo = None                         # driven value, or None

    def _dr(self):
        if self.ir == self.idcode_ir:
            return "idcode", 32
        if self.user_ir is not None and self.ir == self.user_ir:
            return "user", 32
        return "bypass", 1                      # BYPASS, and unknown instructions

    def rise(self, tms, tdi):
        s = self.state
        if s in ("SHDR", "SHIR"):
            self.sr = (self.sr >> 1) | (tdi << (self.srlen - 1))
        elif s == "CAPIR":
            self.sr, self.srlen = 0b01, self.irlen
        elif s == "CAPDR":
            kind, n = self._dr()
            self.sr, self.srlen = {"idcode": self.idcode, "user": self.user, "bypass": 0}[kind], n
        elif s == "UPIR":                       # (the standard updates on the
            self.ir = self.sr                   # fall in Update-xR; the same here)
        elif s == "UPDR" and self._dr()[0] == "user":
            self.user = self.sr
        self.state = NEXT[s][tms]
        if self.state == "TLR":
            self.ir = self.idcode_ir

    def fall(self):
        self.tdo = (self.sr & 1) if self.state in ("SHDR", "SHIR") else None


class Chain:
    def __init__(self, dut, host_bits):
        self.dut, self.host = dut, host_bits
        self.devs = [TapDevice("A", 4, 0x4BA00477, 0b1110, user_ir=0b1010),
                     TapDevice("B", 6, 0x0362D093, 0b001001)]
        self.errors: list[str] = []
        self.prev = (0, 0, 0)

    def pin(self, i):
        oe = (int(self.dut.uio_oe.value) >> i) & 1
        return (int(self.dut.uio_out.value) >> i) & 1 if oe else None

    def tdo(self):
        v = self.devs[-1].tdo
        return 1 if v is None else v

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            tck, tms, tdi = self.pin(TCK) or 0, self.pin(TMS), self.pin(TDI)
            ptck, ptms, ptdi = self.prev
            if tck and ptck and (tms, tdi) != (ptms, ptdi):
                self.errors.append("TMS/TDI moved while TCK was high")
            if tck and not ptck:
                if tms is None or tdi is None:
                    self.errors.append("TCK rose with TMS or TDI undriven")
                # every device samples its TDI (the previous device's TDO,
                # as it stood before this edge) at once
                ins = [tdi or 0] + [1 if d.tdo is None else d.tdo for d in self.devs[:-1]]
                for d, b in zip(self.devs, ins):
                    d.rise(tms or 0, b)
            if ptck and not tck:
                for d in self.devs:
                    d.fall()
            self.prev = (tck, tms, tdi)
            self.dut.uio_in.value = (self.host & ~(1 << TDO)) | (self.tdo() << TDO)

    def at_idle(self):
        return all(d.state == "RTI" for d in self.devs)

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

    async def reset(self):
        await self.send(1)
        return await self.recv()

    async def scan(self, ir, nbits, value):
        """Shift nbits of value (LSB first) through IR (ir=True) or DR;
        return the nbits shifted out."""
        await self.send(2 if ir else 3)
        await self.send(nbits)
        out = 0
        for i in range((nbits + 7) // 8):
            await self.send((value >> 8 * i) & 0xFF)
            out |= await self.recv() << 8 * i
        assert out >> nbits == 0, f"TDO bits above the scan length: {out:#x}"
        return out


async def boot_jtag(dut):
    prog = assembler.assemble_file(JTAG_ASM)
    assert not prog.warnings, prog.warnings
    uio = await T.reset_and_boot(dut, prog.words)
    ch = Chain(dut, uio)
    cocotb.start_soon(ch.run())
    await ClockCycles(dut.clk, 20)
    return ch


@cocotb.test()
async def test_jtag_idcodes(dut):
    """TAP reset, then a 64-bit DR scan reads both IDCODEs (the device
    nearest TDO first); every TAP ends in Run-Test/Idle."""
    ch = await boot_jtag(dut)
    assert await ch.reset() == 0
    assert all(d.state == "RTI" for d in ch.devs), [d.state for d in ch.devs]
    out = await ch.scan(False, 64, 0)
    assert not ch.errors, ch.errors
    assert (out & 0xFFFFFFFF, out >> 32) == (0x0362D093, 0x4BA00477), hex(out)
    assert ch.at_idle(), [d.state for d in ch.devs]
    dut._log.info(f"JTAG: chain IDCODEs {out & 0xFFFFFFFF:#010x} {out >> 32:#010x}")


@cocotb.test()
async def test_jtag_ir_capture_and_bypass(dut):
    """A 10-bit IR scan shifts out both devices' capture values (0b01
    each) and loads BYPASS everywhere; a DR scan then comes back delayed
    by exactly 2 bits: the chain length."""
    ch = await boot_jtag(dut)
    await ch.reset()
    out = await ch.scan(True, 10, 0x3FF)
    assert out == (0b01 | 0b01 << 6), f"IR capture {out:#05x}"
    assert [d.ir for d in ch.devs] == [0xF, 0x3F]
    pat = 0b1011001110001011
    out = await ch.scan(False, 18, pat)
    assert out == (pat << 2) & 0x3FFFF, f"bypass scan {out:#x}"
    assert not ch.errors, ch.errors
    assert ch.at_idle()


@cocotb.test()
async def test_jtag_user_register(dut):
    """Select A's 32-bit USER register (B in BYPASS), write words with
    33-bit DR scans and read each back on the next scan; odd scan lengths
    (1, 7, 9 bits) right-align their TDO byte."""
    ch = await boot_jtag(dut)
    await ch.reset()
    await ch.scan(True, 10, 0x3F | 0b1010 << 6)   # B gets bits 0-5, A bits 6-9
    assert [d.ir for d in ch.devs] == [0b1010, 0x3F]
    old = 0
    for v in (0xDEADBEEF, 0x00000001, 0x80000000, 0x5A5AA5A5):
        out = await ch.scan(False, 33, v << 1)      # bit 0 is B's bypass
        assert out >> 1 == old, f"read back {out >> 1:#x}, want {old:#x}"
        assert ch.devs[0].user == v
        old = v
    # a 1-bit, 7-bit and 9-bit IR scan (A's IR stays whatever lands in it)
    for n in (1, 7, 9):
        await ch.reset()
        out = await ch.scan(True, n, 0)
        want = (0b01 | 0b01 << 6) & ((1 << n) - 1)
        assert out == want, f"{n}-bit IR scan {out:#x}, want {want:#x}"
        assert ch.at_idle()
    assert not ch.errors, ch.errors
