# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""End-to-end protocol test: the SPI target firmware
(firmware/protocols/spi_target.asm) running on the RTL, driven by an
independent SPI mode-0 controller model.

Every other protocol here has the chip as controller, pacing each edge
with DELAY. As a target it must follow a clock it doesn't own, so this
also measures how fast that clock can be.

The controller sees only the pins: it drives CS (uio[3]), SCLK (uio[2])
and MOSI (uio[0]), and samples MISO (uio[1]) at each SCLK rising edge,
the mode-0 sample point. An undriven MISO while CS is low is an error.
"""

import random
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles

import test as T  # shared boot helpers

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

SPI_ASM = Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "spi_target.asm"
MOSI, MISO, SCLK, CS = 0, 1, 2, 3
REPLY0 = 0x96
GAP = 992
# Fastest SCLK the firmware follows (measured by test_spi_target_max_clock):
# half period in clock cycles, so SCLK = 50 MHz / (2 * MIN_HALF).
MIN_HALF = 14


class SpiController:
    """Mode 0, MSB first. Holds the uio_in state the test drives."""

    def __init__(self, dut, uio):
        self.dut, self.uio = dut, uio
        self.errors: list[str] = []

    def _set(self, bit, v):
        self.uio = (self.uio | (1 << bit)) if v else (self.uio & ~(1 << bit))
        self.dut.uio_in.value = self.uio

    def miso(self):
        if not (int(self.dut.uio_oe.value) >> MISO) & 1:
            self.errors.append("MISO not driven while selected")
            return 0
        return (int(self.dut.uio_out.value) >> MISO) & 1

    async def deselect(self):
        self._set(CS, 1)
        self._set(SCLK, 0)
        await ClockCycles(self.dut.clk, 1)

    async def transfer(self, tx, half, cs_setup=20, pause_after=None, pause=0):
        """Full-duplex transfer of `tx`; returns the bytes read on MISO.
        Optionally idles the clock for `pause` cycles (CS low) after byte
        index `pause_after`."""
        dut = self.dut
        self._set(SCLK, 0)
        self._set(CS, 0)
        await ClockCycles(dut.clk, cs_setup)
        rx = []
        for i, b in enumerate(tx):
            r = 0
            for k in range(7, -1, -1):
                self._set(MOSI, (b >> k) & 1)      # change on the falling side
                await ClockCycles(dut.clk, half)
                r = (r << 1) | self.miso()         # sample at the rising edge
                self._set(SCLK, 1)
                await ClockCycles(dut.clk, half)
                self._set(SCLK, 0)
            rx.append(r)
            if pause_after == i:
                await ClockCycles(dut.clk, pause)
        await ClockCycles(dut.clk, half)
        await self.deselect()
        return rx


async def boot_target(dut):
    prog = assembler.assemble_file(SPI_ASM)
    assert not prog.warnings, prog.warnings
    uio = await T.reset_and_boot(dut, prog.words)
    spi = SpiController(dut, uio)
    # boot leaves CS low (selected): deselect and let the firmware notice
    await spi.deselect()
    await ClockCycles(dut.clk, 2 * GAP + 50)
    assert not (int(dut.uio_oe.value) >> MISO) & 1, "MISO should be Hi-Z while CS is high"
    return spi


def expected_reply(tx):
    return [REPLY0] + tx[:-1]


@cocotb.test()
async def test_spi_target_echo_1mhz(dut):
    """1 MHz SCLK: replies REPLY0, then echoes each byte one byte late;
    uo_out shows the last byte received; MISO is released after CS."""
    spi = await boot_target(dut)
    tx = [0x3C, 0xA5, 0x00, 0xFF, 0x81]
    rx = await spi.transfer(tx, half=25)
    assert not spi.errors, spi.errors[:5]
    assert rx == expected_reply(tx), [hex(b) for b in rx]
    await ClockCycles(dut.clk, 10)
    assert int(dut.uo_out.value) == 0x81, f"uo_out {int(dut.uo_out.value):#x}, want last byte 0x81"
    await ClockCycles(dut.clk, 2 * GAP + 50)
    assert not (int(dut.uio_oe.value) >> MISO) & 1, "MISO still driven after CS rose"
    dut._log.info(f"SPI target 1 MHz: sent {[hex(b) for b in tx]}, read {[hex(b) for b in rx]}")


@cocotb.test()
async def test_spi_target_transactions_and_pause(dut):
    """Each transaction starts over with REPLY0; a clock pause much longer
    than GAP with CS still low does not lose the byte position."""
    spi = await boot_target(dut)
    rx1 = await spi.transfer([0x11, 0x22], half=25)
    await ClockCycles(dut.clk, 3 * GAP)
    tx2 = [0x33, 0x44, 0x55]
    rx2 = await spi.transfer(tx2, half=25, pause_after=0, pause=4 * GAP)
    assert not spi.errors, spi.errors[:5]
    assert rx1 == [REPLY0, 0x11], [hex(b) for b in rx1]
    assert rx2 == expected_reply(tx2), [hex(b) for b in rx2]


@cocotb.test()
async def test_spi_target_max_clock(dut):
    """Fastest clock: a 32-byte random transfer is exact at MIN_HALF
    cycles per half period, and fails one cycle faster -- so the documented
    limit is measured, not estimated."""
    rng = random.Random(7)
    spi = await boot_target(dut)
    tx = [rng.randrange(256) for _ in range(32)]
    rx = await spi.transfer(tx, half=MIN_HALF)
    assert not spi.errors and rx == expected_reply(tx), \
        f"fails at half period {MIN_HALF}: {spi.errors[:3]} {[hex(b) for b in rx[:8]]}"
    await ClockCycles(dut.clk, 3 * GAP)
    spi.errors.clear()
    rx = await spi.transfer(tx, half=MIN_HALF - 1)
    assert spi.errors or rx != expected_reply(tx), \
        f"also works at half period {MIN_HALF - 1}: MIN_HALF is not the limit"
    mhz = 50 / (2 * MIN_HALF)
    dut._log.info(f"SPI target: exact at {MIN_HALF}-cycle half period ({mhz:.2f} MHz), fails at {MIN_HALF - 1}")
