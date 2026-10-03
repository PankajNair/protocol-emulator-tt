# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""SPI master firmware (firmware/protocols/spi.asm) against an independent
mode-0 SPI slave model on the board testbench: chip A's MOSI/MISO/SCLK/CS
(pins 0-3) on four nets, the slave on the same nets.

The slave model is the reference: selected only while CS is low, it
samples MOSI on SCLK rising edges and shifts its response out on MISO on
falling edges (MSB first), so a master that samples or drives on the wrong
edge, or in the wrong bit order, gets the wrong bytes. It also checks the
mode-0 rules and timing: SCLK idle low when CS falls, no SCLK edges while
CS is high, CS released only on a byte boundary, MOSI setup before each
rising edge, CS setup before the first edge and hold after the last.

Run: make -C test board
"""

import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

from board import HostPort, contention_nets, drive_net, init_board, net

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

SPI_ASM = Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "spi.asm"
MOSI, MISO, SCLK, CS = 0, 1, 2, 3  # pin numbers == net numbers here
MIN_MOSI_SETUP = 5  # cycles (100 ns) -- a typical slave's minimum


class SPISlave:
    """Cycle-sampled mode-0 SPI slave. `responses`: bytes to shift out on
    MISO, in order, across all transactions (0xFF once exhausted)."""

    def __init__(self, dut, clk, responses):
        self.dut, self.clk = dut, clk
        self.responses = list(responses)
        self.transactions: list[list[int]] = []
        self.errors: list[str] = []
        self.min_high = self.min_low = self.min_setup = None
        self.cs_setups: list[int] = []
        self.cs_holds: list[int] = []

    def _miso(self, level):
        drive_net(self.dut, MISO, level)

    def _next_response(self):
        return self.responses.pop(0) if self.responses else 0xFF

    async def run(self):
        prev = None
        bit = shreg = cur = 0
        c = last_mosi_change = last_sclk_edge = last_fall = cs_fall_at = 0
        first_rise_pending = False
        prefetched = False  # next response's MSB driven at a byte boundary, not yet clocked
        while True:
            await RisingEdge(self.clk)
            c += 1
            cs, sclk, mosi = net(self.dut, CS), net(self.dut, SCLK), net(self.dut, MOSI)
            if prev is None or c <= 2:
                prev = (cs, sclk, mosi)
                continue
            pcs, psclk, pmosi = prev
            prev = (cs, sclk, mosi)
            if cs == "x":
                self.errors.append(f"cycle {c}: CS is x")
                continue
            if sclk == "x":
                # A deselected slave ignores SCLK; it floats until the master
                # configures the pin after boot. Only a problem while selected.
                if cs == 0:
                    self.errors.append(f"cycle {c}: SCLK is x while CS low")
                continue
            if mosi != pmosi:
                last_mosi_change = c

            if pcs == 1 and cs == 0:  # select
                if sclk != 0:
                    self.errors.append(f"cycle {c}: SCLK not idle low when CS fell (mode 0)")
                self.transactions.append([])
                bit = shreg = 0
                cur = self._next_response()
                self._miso((cur >> 7) & 1)
                cs_fall_at, first_rise_pending = c, True
            elif pcs == 0 and cs == 1:  # deselect
                if bit:
                    self.errors.append(f"cycle {c}: CS released after {bit} bits of a byte")
                if prefetched:  # transaction ended before that byte was used
                    self.responses.insert(0, cur)
                    prefetched = False
                self.cs_holds.append(c - last_fall)
                self._miso(None)

            if sclk != psclk and psclk in (0, 1):  # x -> level (pin being configured) isn't an edge
                if cs == 1:
                    self.errors.append(f"cycle {c}: SCLK edge while CS high")
                dur = c - last_sclk_edge
                if last_sclk_edge:
                    if psclk == 1:
                        self.min_high = dur if self.min_high is None else min(self.min_high, dur)
                    elif not first_rise_pending:  # low time between edges, not CS setup
                        self.min_low = dur if self.min_low is None else min(self.min_low, dur)
                last_sclk_edge = c

            if cs == 0 and psclk == 0 and sclk == 1:  # rising: sample MOSI
                if first_rise_pending:
                    self.cs_setups.append(c - cs_fall_at)
                    first_rise_pending = False
                prefetched = False
                su = c - last_mosi_change
                self.min_setup = su if self.min_setup is None else min(self.min_setup, su)
                if mosi == "x":
                    self.errors.append(f"cycle {c}: MOSI is x when sampled")
                    mosi = 0
                shreg = ((shreg << 1) | mosi) & 0xFF
                bit += 1
                if bit == 8:
                    self.transactions[-1].append(shreg)
                    bit = shreg = 0
            elif cs == 0 and psclk == 1 and sclk == 0:  # falling: shift MISO
                last_fall = c
                if bit == 0:  # byte boundary: present the next byte's MSB
                    cur = self._next_response()
                    prefetched = True
                    self._miso((cur >> 7) & 1)
                else:
                    self._miso((cur >> (7 - bit)) & 1)


async def setup(dut, responses, sck_half=25):
    # MISO pulled up so the line isn't left floating while no slave drives it.
    a, _ = init_board(dut, nets_pullup=(1 << MISO) | (1 << CS))
    a.start_clock()
    a.connect({MOSI: MOSI, MISO: MISO, SCLK: SCLK, CS: CS})
    prog = assembler.assemble_file(SPI_ASM, {"SCK_HALF": sck_half})
    assert not prog.warnings, prog.warnings
    slave = SPISlave(dut, a.clk, responses)
    cocotb.start_soon(slave.run())
    ext = await a.boot(prog.words)
    return a, HostPort(a, ext), slave


async def transfer(host, tx):
    """One CS-low transaction: returns the bytes clocked in on MISO."""
    await host.send(len(tx))
    rx = []
    for b in tx:
        await host.send(b)
        rx.append(await host.recv())
    return rx


async def check(dut, a, slave, sck_half=25):
    await ClockCycles(a.clk, 4 * sck_half + 50)  # CS hold + release
    assert net(dut, CS) == 1 and net(dut, SCLK) == 0, "bus should idle with CS high, SCLK low"
    assert contention_nets(dut) == [], f"driver fight on nets {contention_nets(dut)}"
    assert slave.errors == [], slave.errors[:5]
    assert slave.min_high >= sck_half and slave.min_low >= sck_half, \
        f"SCLK high {slave.min_high} / low {slave.min_low}, half period {sck_half}"
    assert slave.min_setup >= MIN_MOSI_SETUP, f"MOSI setup {slave.min_setup} < {MIN_MOSI_SETUP}"
    assert min(slave.cs_setups) >= sck_half and min(slave.cs_holds) >= sck_half, \
        f"CS setup {min(slave.cs_setups)} / hold {min(slave.cs_holds)} < {sck_half}"
    dut._log.info(f"SCK_HALF={sck_half}: SCLK high>={slave.min_high} low>={slave.min_low}, MOSI setup>="
                  f"{slave.min_setup}, CS setup>={min(slave.cs_setups)} hold>={min(slave.cs_holds)} cycles")


@cocotb.test()
async def test_spi_single_byte(dut):
    """One byte each way at 1 MHz: the slave gets 0xA5, the host gets 0x3C."""
    a, host, slave = await setup(dut, [0x3C])
    rx = await transfer(host, [0xA5])
    await check(dut, a, slave)
    assert rx == [0x3C], f"host received {[hex(x) for x in rx]}"
    assert slave.transactions == [[0xA5]]


@cocotb.test()
async def test_spi_multi_byte(dut):
    """Five bytes in one CS-low transaction, full duplex, patterns with an
    edge at every position (catches bit-order and wrong-edge sampling)."""
    tx = [0x01, 0x80, 0xFF, 0x00, 0x5A]
    resp = [0x10, 0x02, 0x7F, 0xC3, 0xA5]
    a, host, slave = await setup(dut, resp)
    rx = await transfer(host, tx)
    await check(dut, a, slave)
    assert rx == resp, f"host received {[hex(x) for x in rx]}"
    assert slave.transactions == [tx]


@cocotb.test()
async def test_spi_two_transactions(dut):
    """Two transactions: CS rises between them, the slave sees two separate
    frames, responses continue in order."""
    a, host, slave = await setup(dut, [0x11, 0x22, 0x33])
    assert await transfer(host, [0xAA, 0xBB]) == [0x11, 0x22]
    assert await transfer(host, [0xCC]) == [0x33]
    await check(dut, a, slave)
    assert slave.transactions == [[0xAA, 0xBB], [0xCC]]


@cocotb.test()
async def test_spi_max_clock(dut):
    """Fastest SCLK the firmware can generate (SCK_HALF=12, ~2.08 MHz):
    still correct, every half period exactly 12 cycles."""
    a, host, slave = await setup(dut, [0x96, 0x69], sck_half=12)
    rx = await transfer(host, [0x0F, 0xF0])
    await check(dut, a, slave, sck_half=12)
    assert rx == [0x96, 0x69] and slave.transactions == [[0x0F, 0xF0]]
