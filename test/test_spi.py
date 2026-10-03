# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""SPI master firmware (firmware/protocols/spi.asm) against an independent
mode-0 SPI slave model on the board testbench: chip A's MOSI/MISO/SCLK/CS
(pins 0-3) on four nets, the slave on the same nets.

The slave model is the reference: selected only while CS is low, it
samples MOSI on SCLK rising edges and shifts its response out on MISO on
falling edges (MSB first), so a master that samples or drives on the wrong
edge, or in the wrong bit order, gets the wrong bytes. It also checks the
mode rules and timing -- including when MOSI is launched (which is what
actually distinguishes CPHA 0 from 1): SCLK idle low when CS falls, no SCLK edges while
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
_SLAVE_TASK = None


class SPISlave:
    """Cycle-sampled SPI slave for any mode (CPOL = mode // 2, CPHA = mode
    % 2). `responses`: bytes shifted out on MISO, in order, across all
    transactions (0xFF once exhausted).

    Leading edge = idle -> active level (first edge after CS falls),
    trailing = back to idle. CPHA 0: sample MOSI on leading, change MISO
    on trailing (first bit presented when CS falls). CPHA 1: change MISO
    on leading, sample on trailing. In both, the bit driven is bit
    (7 - bits sampled so far) of the current byte."""

    def __init__(self, dut, clk, responses, mode=0):
        self.dut, self.clk = dut, clk
        self.cpol, self.cpha = mode // 2, mode % 2
        self.responses = list(responses)
        self.transactions: list[list[int]] = []
        self.errors: list[str] = []
        self.min_active = self.min_idle = self.min_setup = None
        self.cs_setups: list[int] = []
        self.cs_holds: list[int] = []

    def _miso(self, level):
        drive_net(self.dut, MISO, level)

    def _next_response(self):
        return self.responses.pop(0) if self.responses else 0xFF

    async def run(self):
        prev = None
        bit = shreg = cur = 0
        c = last_mosi_change = last_sclk_edge = last_edge = cs_fall_at = 0
        first_edge_pending = need_new = prefetched = False
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
                # SCLK floats until the master configures the pin after boot;
                # a deselected slave ignores it.
                if cs == 0:
                    self.errors.append(f"cycle {c}: SCLK is x while CS low")
                continue
            if mosi != pmosi:
                last_mosi_change = c
                # CPHA is defined by when data is launched: CPHA 0 changes
                # MOSI while SCLK is idle (after the trailing edge), CPHA 1
                # while it is active (after the leading edge). Data alone
                # can't tell them apart here -- the master samples late
                # enough to read either slave type -- so check the launch.
                launch_level = self.cpol if self.cpha == 0 else 1 - self.cpol
                if cs == 0 and pcs == 0 and not first_edge_pending and sclk != launch_level:
                    self.errors.append(f"cycle {c}: MOSI changed while SCLK at {sclk}; "
                                       f"mode {2*self.cpol+self.cpha} launches data at SCLK={launch_level}")

            def present(edge_c):
                nonlocal cur, need_new, prefetched
                if need_new:
                    cur = self._next_response()
                    need_new, prefetched = False, True
                self._miso((cur >> (7 - bit)) & 1)

            if pcs == 1 and cs == 0:  # select
                if sclk != self.cpol:
                    self.errors.append(f"cycle {c}: SCLK at {sclk} when CS fell, mode idle level is {self.cpol}")
                self.transactions.append([])
                bit = shreg = 0
                need_new, cs_fall_at, first_edge_pending = True, c, True
                if self.cpha == 0:
                    present(c)  # CPHA 0: first bit valid before the first edge
            elif pcs == 0 and cs == 1:  # deselect
                if bit:
                    self.errors.append(f"cycle {c}: CS released after {bit} bits of a byte")
                if prefetched:  # presented but never clocked
                    self.responses.insert(0, cur)
                    prefetched = False
                self.cs_holds.append(c - last_edge)
                self._miso(None)

            edge = sclk != psclk and psclk in (0, 1)  # x -> level isn't an edge
            if edge:
                if cs == 1:
                    self.errors.append(f"cycle {c}: SCLK edge while CS high")
                dur = c - last_sclk_edge
                if last_sclk_edge and not first_edge_pending:
                    if psclk != self.cpol:   # an active phase just ended
                        self.min_active = dur if self.min_active is None else min(self.min_active, dur)
                    else:                    # an idle phase between edges
                        self.min_idle = dur if self.min_idle is None else min(self.min_idle, dur)
                last_sclk_edge = c

            if edge and cs == 0:
                last_edge = c
                leading = psclk == self.cpol
                if first_edge_pending:
                    self.cs_setups.append(c - cs_fall_at)
                    first_edge_pending = False
                sample_edge = leading if self.cpha == 0 else not leading
                if sample_edge:
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
                        need_new = True
                else:
                    present(c)


async def setup(dut, responses, sck_half=25, mode=0):
    # MISO pulled up so the line isn't left floating while no slave drives it.
    a, _ = init_board(dut, nets_pullup=(1 << MISO) | (1 << CS))
    a.start_clock()
    a.connect({MOSI: MOSI, MISO: MISO, SCLK: SCLK, CS: CS})
    prog = assembler.assemble_file(SPI_ASM, {"SCK_HALF": sck_half, "MODE": mode})
    assert not prog.warnings, prog.warnings
    slave = SPISlave(dut, a.clk, responses, mode)
    # Several setups can run inside one test (one per mode): stop the
    # previous slave, or it keeps driving MISO alongside the new one.
    global _SLAVE_TASK
    if _SLAVE_TASK is not None and not _SLAVE_TASK.done():
        _SLAVE_TASK.cancel()
    _SLAVE_TASK = cocotb.start_soon(slave.run())
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
    assert net(dut, CS) == 1 and net(dut, SCLK) == slave.cpol, \
        f"bus should idle with CS high, SCLK at the mode's idle level {slave.cpol}"
    assert contention_nets(dut) == [], f"driver fight on nets {contention_nets(dut)}"
    assert slave.errors == [], slave.errors[:5]
    assert slave.min_active >= sck_half and slave.min_idle >= sck_half, \
        f"SCLK active {slave.min_active} / idle {slave.min_idle}, half period {sck_half}"
    assert slave.min_setup >= MIN_MOSI_SETUP, f"MOSI setup {slave.min_setup} < {MIN_MOSI_SETUP}"
    assert min(slave.cs_setups) >= sck_half and min(slave.cs_holds) >= sck_half, \
        f"CS setup {min(slave.cs_setups)} / hold {min(slave.cs_holds)} < {sck_half}"
    dut._log.info(f"mode {2*slave.cpol+slave.cpha} SCK_HALF={sck_half}: SCLK active>={slave.min_active} idle>={slave.min_idle}, MOSI setup>="
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


@cocotb.test()
async def test_spi_all_modes(dut):
    """Modes 0-3, each a 4-byte transaction with edge-at-every-position
    patterns and a second transaction: the slave model samples and shifts
    on the edges that mode defines, so a master using the wrong edge or
    idle level for that mode gets the wrong bytes or a protocol error."""
    tx = [0x01, 0x80, 0x5A, 0xC3]
    resp = [0x7E, 0x81, 0x3C, 0xA5, 0x99]
    for mode in range(4):
        a, host, slave = await setup(dut, resp, mode=mode)
        rx = await transfer(host, tx)
        rx2 = await transfer(host, [0x24])
        await check(dut, a, slave)
        assert rx == resp[:4] and rx2 == [resp[4]], \
            f"mode {mode}: host got {[hex(x) for x in rx + rx2]}"
        assert slave.transactions == [tx, [0x24]], f"mode {mode}: slave got {slave.transactions}"


@cocotb.test()
async def test_spi_all_modes_max_clock(dut):
    """Each mode at its fastest SCLK: 12-cycle halves for CPHA 0, 15 for
    CPHA 1 (one more instruction sits in its idle phase)."""
    for mode in range(4):
        half = 15 if mode % 2 else 12
        a, host, slave = await setup(dut, [0x96, 0x69], sck_half=half, mode=mode)
        rx = await transfer(host, [0x0F, 0xF0])
        await check(dut, a, slave, sck_half=half)
        assert rx == [0x96, 0x69] and slave.transactions == [[0x0F, 0xF0]], f"mode {mode}"
