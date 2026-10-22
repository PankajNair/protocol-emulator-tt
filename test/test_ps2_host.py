# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""End-to-end protocol test: the PS/2 host firmware
(firmware/protocols/ps2_host.asm) running on the RTL, fed by an
independent PS/2 keyboard model.

The keyboard owns the clock: 11-bit frames (start 0, 8 data LSB first,
odd parity, stop 1) at 12.5 kHz, DATA set while CLK is high, CLK pulled
low for half a period per bit. It follows the spec's flow control: it
starts a frame only after CLK has been released for 50 us, and a host
pulling CLK low before the 11th clock is an error. Both lines are
open-drain with pull-ups: low if either side pulls them low.
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import test as T

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

# PS2_ASM overrides the firmware, for checking that broken variants fail
PS2_ASM = Path(os.environ.get("PS2_ASM") or
               Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "ps2_host.asm")
DATA, CLK = 0, 1
US = 50
HALF = 40 * US   # 12.5 kHz device clock


class Ps2Bus:
    """Resolves both open-drain lines every cycle and drives uio_in."""

    def __init__(self, dut, host_bits):
        self.dut, self.host = dut, host_bits
        self.dev_data_low = False
        self.dev_clk_low = False
        self.errors: list[str] = []
        self.inhibits = 0          # frames after which the chip held CLK low

    def chip_low(self, bit):
        return bool((int(self.dut.uio_oe.value) >> bit) & 1 and not (int(self.dut.uio_out.value) >> bit) & 1)

    def line(self, bit):
        dev = self.dev_data_low if bit == DATA else self.dev_clk_low
        return 0 if (dev or self.chip_low(bit)) else 1

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            v = self.host & ~((1 << DATA) | (1 << CLK))
            v |= self.line(DATA) << DATA
            v |= self.line(CLK) << CLK
            self.dut.uio_in.value = v

    async def keyboard_send(self, b, bad_parity=False, bad_stop=False):
        clk = self.dut.clk
        # wait for CLK released (not inhibited) for 50 us
        free = 0
        held = False
        while free < 50 * US:
            await RisingEdge(clk)
            if self.chip_low(CLK):
                free, held = 0, True
            else:
                free += 1
        self.inhibits += held
        parity = 1 - (bin(b).count("1") & 1)        # odd parity
        if bad_parity:
            parity ^= 1
        bits = [0] + [(b >> i) & 1 for i in range(8)] + [parity] + [0 if bad_stop else 1]
        for i, bit in enumerate(bits):
            self.dev_data_low = not bit
            await ClockCycles(clk, HALF // 2)          # data settles while CLK is high
            if i and self.chip_low(CLK):
                self.errors.append(f"host pulled CLK low before clock {i + 1} of 11")
            await ClockCycles(clk, HALF // 2)
            self.dev_clk_low = True                     # falling edge: host samples
            await ClockCycles(clk, HALF)
            self.dev_clk_low = False
        await ClockCycles(clk, HALF // 2)
        self.dev_data_low = False

    # ---- host side
    async def recv(self, max_cycles=200_000):
        self.host |= 1 << T.HOST_GO_BIT
        await T.wait_host_status(self.dut, 1, max_cycles=max_cycles, what="(byte ready)")
        v = int(self.dut.uo_out.value)
        self.host &= ~(1 << T.HOST_GO_BIT)
        await T.wait_host_status(self.dut, 0, max_cycles=max_cycles)
        return v


async def boot_ps2(dut):
    prog = assembler.assemble_file(PS2_ASM)
    assert not prog.warnings, prog.warnings
    uio = await T.reset_and_boot(dut, prog.words)
    bus = Ps2Bus(dut, uio)
    cocotb.start_soon(bus.run())
    await ClockCycles(dut.clk, 20)
    assert not bus.chip_low(CLK) and not bus.chip_low(DATA), "both lines should be released at idle"
    return bus


@cocotb.test()
async def test_ps2_scan_codes(dut):
    """A keyboard sends 'A' pressed, released, then Enter (0x1C, 0xF0,
    0x1C, 0x5A) back to back: every byte reaches the host in order. The
    chip inhibits the clock after each frame until the host has taken
    the byte, and never mid-frame."""
    bus = await boot_ps2(dut)
    codes = [0x1C, 0xF0, 0x1C, 0x5A]

    async def keyboard():
        for c in codes:
            await bus.keyboard_send(c)
    cocotb.start_soon(keyboard())
    got = []
    for _ in codes:
        await ClockCycles(dut.clk, 2 * 11 * 2 * HALF)  # host is slow: the keyboard must wait
        got.append(await bus.recv())
    assert not bus.errors, bus.errors
    assert got == codes, [hex(b) for b in got]
    assert bus.inhibits >= len(codes) - 1, f"keyboard was held off only {bus.inhibits} times"
    assert (int(dut.uio_oe.value) >> 6) & 1 == 0 or not (int(dut.uio_out.value) >> 6) & 1, "HOST_ERROR raised"
    dut._log.info(f"PS/2: received {[hex(b) for b in got]}; keyboard held off {bus.inhibits} times")


@cocotb.test()
async def test_ps2_bad_frames(dut):
    """A frame with bad parity and one with a bad stop bit are dropped and
    raise HOST_ERROR; the good frames around them still arrive."""
    bus = await boot_ps2(dut)

    async def keyboard():
        await bus.keyboard_send(0x29)
        await bus.keyboard_send(0x33, bad_parity=True)
        await bus.keyboard_send(0x44, bad_stop=True)
        await bus.keyboard_send(0x55)
    cocotb.start_soon(keyboard())
    first = await bus.recv()
    second = await bus.recv(max_cycles=400_000)
    assert (first, second) == (0x29, 0x55), (hex(first), hex(second))
    oe, out = int(dut.uio_oe.value), int(dut.uio_out.value)
    assert (oe >> 6) & 1 and (out >> 6) & 1, "HOST_ERROR not raised for the bad frames"
    assert not bus.errors, bus.errors
