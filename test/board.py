# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Helpers for tb_board.v (two chips on shared nets).

`Chip` presents one chip's signals under the names test.py's helpers
expect (clk, ui_in, uio_in, uio_out, uio_oe, uo_out), so the LOAD-mode
boot and host handshakes are reused, not reimplemented. On the board,
`uio_in` is the chip's ext_in_* -- what an unmapped pin reads when the
chip isn't driving it (HOST_GO, START) -- while mapped pins read their
net.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Timer

import test as T

NONE = 0xF
NOMINAL_PERIOD_PS = 20_000  # 50 MHz
_CLOCKS: dict = {}  # chip name -> (start task, Clock) currently driving it


class Chip:
    def __init__(self, tb, name: str):
        self.tb, self.name = tb, name
        s = name.lower()
        self.clk = getattr(tb, f"clk_{s}")
        self.rst_n = getattr(tb, f"rst_n_{s}")
        self.ui_in = getattr(tb, f"ui_in_{s}")
        self.uio_in = getattr(tb, f"ext_in_{s}")
        self.uio_out = getattr(tb, f"uio_out_{s}")
        self.uio_oe = getattr(tb, f"uio_oe_{s}")
        self.uo_out = getattr(tb, f"uo_out_{s}")
        self._map = getattr(tb, f"map_{s}")
        self.core = getattr(tb, f"chip_{s}").u_core
        self.ena = None  # test.py helpers never touch it on the board

    def start_clock(self, ppm: float = 0.0, phase_ps: int = 0):
        """Own clock: frequency `ppm` parts-per-million off nominal 50 MHz
        (positive = faster), first edge after `phase_ps`."""
        # cocotb's Clock needs an even period (equal high/low halves): nearest
        # even ps, i.e. within 1 ps (50 ppm) of the requested frequency.
        period = 2 * round(NOMINAL_PERIOD_PS / (1 + ppm * 1e-6) / 2)
        self.period_ps = period
        # One clock per chip signal: re-starting (several runs inside one
        # cocotb test) must stop the previous one, or two Clock tasks drive
        # the same signal -- the leak fixed in test_random.py (0636edf),
        # here producing garbage edges and phase-dependent failures.
        # cocotb 2.0's Clock.start() hands off to a simulator-level clock and
        # returns, so cancelling a wrapper task does NOT stop it -- the old
        # Clock object itself must be stopped.
        old = _CLOCKS.pop(self.name, None)
        if old is not None:
            task, clock = old
            if not task.done():
                task.cancel()
            clock.stop()
        clock = Clock(self.clk, period, unit="ps")

        async def run():
            if phase_ps:
                await Timer(phase_ps, unit="ps")
            clock.start()
        _CLOCKS[self.name] = (cocotb.start_soon(run()), clock)

    def connect(self, pin_to_net: dict[int, int]):
        """Attach pins to nets; pins not listed are unmapped."""
        v = 0
        for p in range(8):
            v |= (pin_to_net.get(p, NONE) & 0xF) << (4 * p)
        self._map.value = v

    def pin_driven(self, pin: int) -> int:
        return (int(self.uio_oe.value) >> pin) & 1

    async def boot(self, words=None, ui_in_for_run=None, image=None) -> int:
        """Reset + LOAD-mode boot + START pulse, same sequence as
        test.reset_and_boot but on this chip's own clock. Pass program
        `words`, or a full boot `image` (bytes, e.g. asm.Program.image()
        including the data region). Returns the ext_in state
        (HOST_GO/START low)."""
        self.ui_in.value = 0
        self.uio_in.value = 0
        self.rst_n.value = 0
        await ClockCycles(self.clk, 5)
        self.rst_n.value = 1
        await ClockCycles(self.clk, 5)
        ext = 0
        for k, b in enumerate(image if image is not None else T.asm.to_bytes(words), start=1):
            ext = await T.load_byte(self, b, ext, expect_echo=k & 0xFF)
        if ui_in_for_run is not None:
            self.ui_in.value = ui_in_for_run
        ext |= 1 << T.START_BIT
        self.uio_in.value = ext
        await ClockCycles(self.clk, 4)
        ext &= ~(1 << T.START_BIT)
        self.uio_in.value = ext
        return ext

    async def run_to_value(self, target, max_cycles=300):
        return await T.run_to_value(self, target, max_cycles)


def init_board(tb, nets_pullup: int = 0):
    """Everything idle: both chips in reset, no pin on a net, no external
    drivers, contention record cleared."""
    for s in ("a", "b"):
        getattr(tb, f"rst_n_{s}").value = 0
        getattr(tb, f"ui_in_{s}").value = 0
        getattr(tb, f"ext_in_{s}").value = 0
        getattr(tb, f"map_{s}").value = 0xFFFF_FFFF
    tb.net_pullup.value = nets_pullup
    _EXT[id(tb)] = [0, 0]
    tb.net_ext_oe.value = 0
    tb.net_ext_val.value = 0
    tb.contention_seen.value = 0
    return Chip(tb, "A"), Chip(tb, "B")


def net(tb, n: int):
    """Resolved value of net n: 0, 1, or 'x' (floating or contention)."""
    s = str(tb.net_val.value)  # MSB first
    c = s[len(s) - 1 - n].lower()
    return int(c) if c in "01" else "x"


_EXT: dict = {}  # id(tb) -> [oe, val]: shadow of net_ext_oe / net_ext_val


def drive_net(tb, n: int, level):
    """External driver on net n: 0/1 drives, None releases.

    Read-modify-write against a Python shadow, never the signals: cocotb
    applies writes at the end of the time step, so reading net_ext_* back
    after a write in the same step returns the stale value and a second
    drive_net call that step (SDA and SCL changing together) would
    silently undo the first. Found by the I2C slave model."""
    oe, val = _EXT.setdefault(id(tb), [0, 0])
    if level is None:
        oe &= ~(1 << n)
    else:
        oe |= 1 << n
        val = (val & ~(1 << n)) | ((level & 1) << n)
    _EXT[id(tb)] = [oe, val]
    tb.net_ext_val.value = val
    tb.net_ext_oe.value = oe


def contention_nets(tb) -> list[int]:
    v = tb.contention_seen.value
    return [n for n in range(8) if str(v)[7 - n] == "1"]


class _UioBus:
    """Sets one bit of a chip's ext_in without clobbering the others."""

    def __init__(self, chip, initial: int):
        self.chip, self.val = chip, initial

    def set(self, bit: int, level: int):
        self.val = (self.val & ~(1 << bit)) | ((level & 1) << bit)
        self.chip.uio_in.value = self.val


class HostPort:
    """Host side of the lockstep IN / host-initiated OUT handshakes
    (docs/architecture.md Host handshake) on one chip: send() a byte in,
    recv() a byte out. Protocol firmwares build on these two."""

    def __init__(self, chip, ext, max_wait=120_000):
        self.chip, self.bus, self.max_wait = chip, _UioBus(chip, ext), max_wait

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
