# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""I2C master firmware (firmware/protocols/i2c.asm) against an
independent I2C slave model, on the board testbench: chip A's SDA (pin 0)
and SCL (pin 1) on two pulled-up nets, the slave on the same nets through
the testbench's own drivers. Open-drain everywhere: nobody ever drives a
line high, so a driver fight (x on a net) is a failure.

The slave model is the reference: it decodes the bus cycle by cycle
(START/STOP, address, data, ACK/NACK), only pulls low or releases, can
NACK a chosen byte, stretch the clock, or hold SCL low forever, and
checks the master against I2C timing minimums for the mode (SCL low/high
time, data setup before SCL rises, START hold) plus protocol rules (no
START/STOP inside a byte).

Run: make -C test board
"""

import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import test as T
import test_uart as U
from board import contention_nets, drive_net, init_board, net

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

I2C_ASM = Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "i2c.asm"
SDA_PIN, SCL_PIN = 0, 1
SDA, SCL = 0, 1  # nets
SLAVE_ADDR = 0x42

# I2C-bus spec (UM10204) minimums, in 50 MHz cycles, rounded up.
MODES = {
    "100k": dict(defs={"SCL_LOW": 250, "SCL_HIGH": 250}, t_low=235, t_high=200, t_su_dat=13, t_hd_sta=200),
    "400k": dict(defs={"SCL_LOW": 70, "SCL_HIGH": 55}, t_low=65, t_high=30, t_su_dat=5, t_hd_sta=30),
}


class I2CSlave:
    """Cycle-sampled I2C slave on nets SDA/SCL, open-drain drivers only."""

    def __init__(self, dut, clk, addr=SLAVE_ADDR, mem=None, nack_data_index=None,
                 stretch_cycles=0, hold_scl_forever=False):
        self.dut, self.clk, self.addr = dut, clk, addr
        self.mem = list(mem or [])
        self.nack_data_index = nack_data_index
        self.stretch_cycles, self.hold_scl_forever = stretch_cycles, hold_scl_forever
        self.received, self.addresses, self.master_acks = [], [], []
        self.starts = self.stops = 0
        self.errors: list[str] = []
        self.min_low = self.min_high = None
        self.min_setup = None
        self.start_holds: list[int] = []
        self.trace: list[str] = []  # event log, for diagnosing failures

    def _sda(self, level):  # 0 pulls low, None releases
        drive_net(self.dut, SDA, level)

    def _scl(self, level):
        drive_net(self.dut, SCL, level)

    async def run(self):
        state, bit, shreg, rw, acked = "idle", 0, 0, 0, False
        phase, sent, rd_idx, byte = "data", 0, 0, 0
        stretch_left = 0
        prev_sda, prev_scl = 1, 1
        last_sda_change = last_scl_edge = 0
        start_at = None
        c = 0
        while True:
            await RisingEdge(self.clk)
            c += 1
            sda, scl = net(self.dut, SDA), net(self.dut, SCL)
            if c <= 2:  # testbench registers still settling at time zero
                prev_sda, prev_scl = (sda, scl) if "x" not in (sda, scl) else (1, 1)
                continue
            if sda == "x" or scl == "x":
                self.errors.append(f"cycle {c}: line is x (driver fight or floating): SDA={sda} SCL={scl}")
                prev_sda, prev_scl = sda, scl
                continue
            if stretch_left:
                stretch_left -= 1
                if stretch_left == 0 and not self.hold_scl_forever:
                    self._scl(None)

            if sda != prev_sda:
                if prev_scl == 1 and scl == 1:
                    if sda == 0:  # START
                        # its own SCL rise was sampled as bit 1; >1 = mid-byte
                        if state in ("addr", "write") and 1 < bit < 9:
                            self.errors.append(f"cycle {c}: START inside a byte")
                        self.starts += 1
                        self.trace.append(f"{c} START")
                        state, bit, shreg = "addr", 0, 0
                        start_at = c
                        self._sda(None)
                    else:  # STOP
                        # a STOP's own SCL rise is sampled as bit 1 before the
                        # STOP is seen (as in a real slave); only 2..8 is mid-byte
                        if state in ("addr", "write") and 1 < bit < 9:
                            self.errors.append(f"cycle {c}: STOP inside a byte")
                        self.stops += 1
                        self.trace.append(f"{c} STOP state={state} bit={bit}")
                        state = "idle"
                        self._sda(None)
                else:
                    last_sda_change = c

            if scl != prev_scl:
                dur = c - last_scl_edge
                if prev_scl == 0 and last_scl_edge:  # low phase ended
                    self.min_low = dur if self.min_low is None else min(self.min_low, dur)
                elif prev_scl == 1 and last_scl_edge:
                    self.min_high = dur if self.min_high is None else min(self.min_high, dur)
                last_scl_edge = c

            if prev_scl == 0 and scl == 1:  # SCL rising: data valid
                if state in ("addr", "write") and bit < 8:
                    su = c - last_sda_change
                    self.min_setup = su if self.min_setup is None else min(self.min_setup, su)
                    shreg = (shreg << 1) | sda
                    bit += 1
                elif state == "read" and phase == "ack":
                    self.master_acks.append(sda)
                    self.trace.append(f"{c} master ack={sda}")
                if state == "read":
                    self.trace.append(f"{c} rise read phase={phase} sent={sent} sda={sda}")

            if prev_scl == 1 and scl == 0:  # SCL falling: slave may change SDA
                if start_at is not None:
                    self.start_holds.append(c - start_at)
                    start_at = None
                if state in ("addr", "write"):
                    if bit == 8:
                        if state == "addr":
                            rw, ok = shreg & 1, (shreg >> 1) == self.addr
                            self.addresses.append(shreg)
                        else:
                            ok = self.nack_data_index != len(self.received)
                            self.received.append(shreg)
                        acked = ok
                        if ok:
                            self._sda(0)
                        bit = 9
                    elif bit == 9:  # end of the ACK clock
                        self._sda(None)
                        bit, shreg = 0, 0
                        self.trace.append(f"{c} ack-clock end state={state} acked={acked} rw={rw}")
                        if self.stretch_cycles or self.hold_scl_forever:
                            self._scl(0)
                            stretch_left = self.stretch_cycles or 1
                        if not acked:
                            state = "wait_stop"
                        elif state == "addr" and rw:
                            state, phase, sent, rd_idx = "read", "data", 0, 0
                            byte = self.mem[0] if self.mem else 0xFF
                            self._sda(0 if not (byte >> 7) & 1 else None)
                        elif state == "addr":
                            state = "write"
                elif state == "read":
                    if phase == "data":
                        sent += 1
                        if sent < 8:
                            self._sda(0 if not (byte >> (7 - sent)) & 1 else None)
                        else:
                            self._sda(None)
                            phase = "ack"
                    else:  # end of the master's ACK clock
                        if self.master_acks and self.master_acks[-1] == 0:
                            rd_idx += 1
                            byte = self.mem[rd_idx] if rd_idx < len(self.mem) else 0xFF
                            phase, sent = "data", 0
                            self._sda(0 if not (byte >> 7) & 1 else None)
                        else:
                            self._sda(None)
                            state = "wait_stop"
                        if self.stretch_cycles:
                            self._scl(0)
                            stretch_left = self.stretch_cycles
            prev_sda, prev_scl = sda, scl


class Host:
    """Host side of i2c.asm's lockstep protocol on chip A."""

    def __init__(self, chip, ext, max_wait=120_000):
        self.chip, self.bus, self.max_wait = chip, U.UioBus(chip, ext), max_wait

    async def send(self, value):
        self.chip.ui_in.value = value
        self.bus.set(T.HOST_GO_BIT, 1)
        await T.wait_host_status(self.chip, 1, max_cycles=self.max_wait, what="(IN ack)")
        self.bus.set(T.HOST_GO_BIT, 0)
        await T.wait_host_status(self.chip, 0, max_cycles=self.max_wait, what="(IN done)")

    async def recv(self):
        self.bus.set(T.HOST_GO_BIT, 1)
        await T.wait_host_status(self.chip, 1, max_cycles=self.max_wait, what="(OUT ready)")
        v = int(self.chip.uo_out.value)
        self.bus.set(T.HOST_GO_BIT, 0)
        await T.wait_host_status(self.chip, 0, max_cycles=self.max_wait, what="(OUT done)")
        return v

    async def write(self, addr7, data):
        """Returns per-step statuses: address, then one per data byte sent."""
        await self.send(addr7 << 1)
        await self.send(len(data))
        st = [await self.recv()]
        for b in data:
            if st[-1] != 0:
                break
            await self.send(b)
            st.append(await self.recv())
        return st

    async def read(self, addr7, n):
        """Returns (address status, bytes read)."""
        await self.send((addr7 << 1) | 1)
        await self.send(n)
        st = await self.recv()
        if st != 0:
            return st, []
        return st, [await self.recv() for _ in range(n)]


async def setup(dut, mode="100k", **slave_kw):
    a, _ = init_board(dut, nets_pullup=0b11)
    a.start_clock()
    a.connect({SDA_PIN: SDA, SCL_PIN: SCL})
    prog = assembler.assemble_file(I2C_ASM, MODES[mode]["defs"])
    assert not prog.warnings, prog.warnings
    slave = I2CSlave(dut, a.clk, **slave_kw)
    cocotb.start_soon(slave.run())
    ext = await a.boot(prog.words)
    return a, Host(a, ext), slave


async def settle_and_check(dut, a, slave, mode="100k", expect_stops=1):
    # The host's last hand-off isn't the end of the bus activity: the
    # master still clocks the final ACK/NACK, may be stretched, then sends
    # STOP. Wait for the STOP (bounded), then require an idle bus.
    for _ in range(50_000):
        if slave.stops >= expect_stops:
            break
        await RisingEdge(a.clk)
    await ClockCycles(a.clk, 500)
    assert net(dut, SDA) == 1 and net(dut, SCL) == 1, "bus not released after the transaction"
    assert contention_nets(dut) == [], f"driver fight on nets {contention_nets(dut)}"
    assert slave.errors == [], slave.errors[:5]
    assert slave.stops == expect_stops, f"expected {expect_stops} STOP(s), saw {slave.stops}"
    m = MODES[mode]
    assert slave.min_low >= m["t_low"], f"SCL low {slave.min_low} < tLOW {m['t_low']}"
    assert slave.min_high >= m["t_high"], f"SCL high {slave.min_high} < tHIGH {m['t_high']}"
    assert slave.min_setup >= m["t_su_dat"], f"data setup {slave.min_setup} < tSU:DAT {m['t_su_dat']}"
    assert min(slave.start_holds) >= m["t_hd_sta"], f"START hold {min(slave.start_holds)} < tHD:STA"
    dut._log.info(f"{mode}: SCL low>={slave.min_low} high>={slave.min_high} setup>={slave.min_setup} "
                  f"start-hold>={min(slave.start_holds)} cycles")


@cocotb.test()
async def test_i2c_write_100k(dut):
    """Write 3 bytes to the slave at 100 kHz: every byte ACKed and received."""
    a, host, slave = await setup(dut, "100k")
    st = await host.write(SLAVE_ADDR, [0xDE, 0xAD, 0x5A])
    assert st == [0, 0, 0, 0], f"statuses {st}"
    await settle_and_check(dut, a, slave, "100k")
    assert slave.addresses == [SLAVE_ADDR << 1]
    assert slave.received == [0xDE, 0xAD, 0x5A]


@cocotb.test()
async def test_i2c_read_100k(dut):
    """Read 3 bytes: data matches the slave's memory, master ACKs the first
    two and NACKs the last, then STOP."""
    a, host, slave = await setup(dut, "100k", mem=[0x12, 0xF0, 0x81])
    st, data = await host.read(SLAVE_ADDR, 3)
    assert st == 0 and data == [0x12, 0xF0, 0x81], f"status {st}, data {[hex(x) for x in data]}"
    await settle_and_check(dut, a, slave, "100k")
    assert slave.master_acks == [0, 0, 1], f"master ACK/NACK sequence {slave.master_acks}"


@cocotb.test()
async def test_i2c_400k_write_then_read(dut):
    """400 kHz fast mode, back-to-back transactions on one boot: write then
    read, timing checked against fast-mode minimums."""
    a, host, slave = await setup(dut, "400k", mem=[0xA5, 0x3C])
    assert await host.write(SLAVE_ADDR, [0x01, 0x02]) == [0, 0, 0]
    st, data = await host.read(SLAVE_ADDR, 2)
    assert st == 0 and data == [0xA5, 0x3C]
    await settle_and_check(dut, a, slave, "400k", expect_stops=2)
    assert slave.received == [0x01, 0x02]


@cocotb.test()
async def test_i2c_address_nack(dut):
    """No device at the address: status 1 (NACK), master sends STOP and the
    bus is released; HOST_ERROR stays low (a NACK isn't a bus fault)."""
    a, host, slave = await setup(dut, "100k")
    st = await host.write(0x50, [0x11])
    assert st == [1], f"statuses {st}"
    await settle_and_check(dut, a, slave, "100k")
    assert U.host_error(a) == 0


@cocotb.test()
async def test_i2c_data_nack(dut):
    """The slave NACKs the 2nd data byte: status sequence 0, 0, 1; the
    master stops right there (the 3rd byte is never put on the bus)."""
    a, host, slave = await setup(dut, "100k", nack_data_index=1)
    st = await host.write(SLAVE_ADDR, [0x10, 0x20, 0x30])
    assert st == [0, 0, 1], f"statuses {st}"
    await settle_and_check(dut, a, slave, "100k")
    assert slave.received == [0x10, 0x20]


@cocotb.test()
async def test_i2c_clock_stretching(dut):
    """The slave holds SCL low for 1500 cycles (30 us) after every byte:
    the master waits it out (WAIT SCL,1,timeout) and both directions
    still complete correctly."""
    a, host, slave = await setup(dut, "100k", mem=[0x77, 0x66], stretch_cycles=1500)
    assert await host.write(SLAVE_ADDR, [0xC3, 0x3C]) == [0, 0, 0]
    st, data = await host.read(SLAVE_ADDR, 2)
    assert st == 0 and data == [0x77, 0x66]
    await settle_and_check(dut, a, slave, "100k", expect_stops=2)
    assert U.host_error(a) == 0


@cocotb.test()
async def test_i2c_stuck_scl(dut):
    """The slave holds SCL low forever after the address byte: the master's
    stretch timeout fires, it reports status 3, raises HOST_ERROR and
    returns to idle instead of hanging (docs/isa.md's WAIT-timeout case)."""
    a, host, slave = await setup(dut, "100k", hold_scl_forever=True)
    st = await host.write(SLAVE_ADDR, [0x99])
    assert st == [0, 3], f"statuses {st}"
    assert U.host_error(a) == 1, "stuck bus should raise HOST_ERROR"
    assert contention_nets(dut) == []
