# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""The demo-board host library (firmware/host/pe_host.py), run against
the RTL.

pe_host.py writes the boot and handshake protocols once, as generators
of pin operations. On the board a synchronous driver runs them through
the TT MicroPython SDK; here SimIO runs the very same generators on the
DUT's pins. So what a judge runs on the board is the code tested here,
apart from the thin SDK adapter (TTDemoBoardIO).
"""

import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import test as T

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware" / "host"))
sys.path.insert(0, str(ROOT / "firmware" / "isa"))
import asm as assembler  # noqa: E402
import pe_host  # noqa: E402

CYCLES_PER_MS = 50_000
MAX_WAIT = 60_000  # cap on any one wait in simulation (cycles; WS2812 latches take ~34k)

# Plain echo: replies with the byte it was sent.
ECHO = """
loop:   WAIT HOST_GO, 1
        IN   R0
        SET  HOST_STATUS, 1
        WAIT HOST_GO, 0
        SET  HOST_STATUS, 0
        WAIT HOST_GO, 1
        OUT  R0
        SET  HOST_STATUS, 1
        WAIT HOST_GO, 0
        SET  HOST_STATUS, 0
        JMP  loop
"""


class SimIO:
    """pe_host's pin operations on the DUT."""

    def __init__(self, dut):
        self.dut = dut
        self.uio = 0
        self.driven = 0

    def _drive(self):
        self.dut.uio_in.value = self.uio & self.driven

    async def reset(self):
        T._ensure_clock(self.dut)
        self.dut.ena.value = 1
        self.dut.rst_n.value = 0
        await ClockCycles(self.dut.clk, 5)
        self.dut.rst_n.value = 1
        await ClockCycles(self.dut.clk, 5)

    async def set_ui(self, byte):
        self.dut.ui_in.value = byte

    async def get_uo(self):
        return int(self.dut.uo_out.value)

    async def set_uio(self, bit, level):
        self.driven |= 1 << bit
        self.uio = (self.uio | (1 << bit)) if level else (self.uio & ~(1 << bit))
        self._drive()

    async def release_uio(self, bit):
        self.driven &= ~(1 << bit)
        self._drive()

    async def get_uio(self, bit):
        if not (int(self.dut.uio_oe.value) >> bit) & 1:
            return 0  # undriven: the board reads its pull-down
        return (int(self.dut.uio_out.value) >> bit) & 1

    async def hold(self):
        await ClockCycles(self.dut.clk, 10)

    async def wait_uio(self, bit, level, timeout_ms):
        for _ in range(min(timeout_ms * CYCLES_PER_MS, MAX_WAIT)):
            if await self.get_uio(bit) == level:
                return True
            await RisingEdge(self.dut.clk)
        return False


async def run(gen, io):
    """pe_host.run, for an async IO object."""
    result = None
    while True:
        try:
            op = gen.send(result)
        except StopIteration as e:
            return e.value
        result = await getattr(io, op[0])(*op[1:])


def image_of(text):
    """Assemble, then round-trip through asm.py's hex format, as the
    board would read it from a file."""
    prog = assembler.assemble(text)
    hex_text = "\n".join(f"{b:02x}" for b in prog.image()) + "\n"
    img = pe_host.parse_hex(hex_text)
    assert img == prog.image()
    return img


@cocotb.test()
async def test_host_load_and_talk(dut):
    """load() boots a program; send()/recv() exchange bytes with it."""
    io = SimIO(dut)
    await run(pe_host.load(image_of(ECHO)), io)
    for b in (0x00, 0x41, 0xA5, 0xFF):
        await run(pe_host.send(b), io)
        got = await run(pe_host.recv(), io)
        assert got == b, f"sent {b:#04x}, firmware replied {got:#04x}"
    assert await io.get_uio(pe_host.HOST_ERROR) == 0


@cocotb.test()
async def test_host_load_image_with_data(dut):
    """A full image with a data region (the WS2812 demo: 522 bytes, past
    the echo counter's 8-bit wrap at byte 256) loads with every echo
    checked, and the firmware then runs: it latches its frame and raises
    HOST_STATUS."""
    io = SimIO(dut)
    img = assembler.assemble_file(ROOT / "firmware" / "protocols" / "ws2812.asm").image()
    assert len(img) > 512
    await run(pe_host.load(img), io)
    assert await io.wait_uio(pe_host.HOST_STATUS, 1, 1), "WS2812 firmware never latched its frame"


@cocotb.test()
async def test_host_reload(dut):
    """The chip forgets its program on reset (or when deselected): load()
    again, a different program, and it runs that one."""
    io = SimIO(dut)
    await run(pe_host.load(image_of(ECHO)), io)
    await run(pe_host.send(0x12), io)
    assert await run(pe_host.recv(), io) == 0x12
    await run(pe_host.load(image_of("SET HOST_STATUS, 1\nHALT\n")), io)
    assert await io.wait_uio(pe_host.HOST_STATUS, 1, 1), "second program did not run"


@cocotb.test()
async def test_host_timeout(dut):
    """A firmware that never answers makes send() raise HostError instead
    of hanging the host."""
    io = SimIO(dut)
    await run(pe_host.load(image_of("HALT\n")), io)
    try:
        await run(pe_host.send(0x55), io)
    except pe_host.HostError as e:
        dut._log.info(f"send to a halted chip: {e}")
    else:
        assert False, "send() to a halted chip did not raise"
