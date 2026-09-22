# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""STIM_PROFILE=protocol_pins -- level-holding protocol-pin waveforms
(test/stim/AGENT_CONTRACT.md requested profile #1).

Gap this closes: io_stimulus.IoStimulus redraws every protocol pin as a
fresh 50/50 bit every cycle, so any WAIT is satisfied within ~2 cycles
and a timeout (or the met-on-expiry tie) is essentially unreachable.
Real buses hold levels. Per seed this profile picks ONE bus scenario
for uio[3:0], using the pin roles docs/architecture.md's Pin map
assigns, plus an independent slow line on uio[7]:

  - uart: pin0/pin1 = two independent UART lines, idle-high, 8N1
    frames (start 0, 8 LSB-first data bits, stop 1) at a per-line
    baud-in-cycles, separated by idle gaps that are often long (a
    quiet line between messages). pin2/pin3 = slow GPIO.
  - spi (mode 0): pin3 = CS, idle high, dropped low for bursts of 1-4
    bytes; pin2 = SCLK, idle low, toggling at a per-seed half-period
    only while CS is low; pin0/pin1 = MOSI/MISO, change only on SCLK
    falling edges (hold across the rising sample edge), hold between
    bursts.
  - i2c: pin1 = SCL, pin0 = SDA, both idle-high (pull-ups). START
    (SDA falls while SCL high), 1-3 bytes of 9 clocks (8 data + ACK),
    SDA changing only while SCL is low, STOP (SDA rises while SCL
    high). Any SCL-low phase may be clock-stretched by a slave for a
    long time (tens to ~1500 cycles) -- the exact condition docs/isa.md
    sells `WAIT SCL,1,timeout` for. pin2/pin3 = slow GPIO.
  - uio[7] (debug/trigger/second-CS line): slow GPIO in every scenario.

  Slow GPIO = random level with log-uniform dwell (1..2000 cycles);
  with small probability the line is stuck at one level for the whole
  run (slave holding a line / missing pull-up / unconnected input) --
  a real fault condition WAIT's timeout exists to detect.

ui_in (host DATA-IN bus, sampled by IN): a host writes a byte and holds
it for a log-uniform dwell (1..600 cycles) before the next write, rather
than a new byte every cycle.

Scale: real baud/SCL periods at 50 MHz are 434-5208 cycles
(docs/protocol_timing.md). Periods here are drawn from 4..434 so both
fast-toggling and slow lines appear against the default WAIT timeout
range (1..31 at exponent 0, 32..480 at exponent 1). Design choice to
note honestly: the lower end of the range is faster than any real
UART/I2C rate at this clock; it's there so "met_later with small k"
and edge-on-expiry ties stay reachable, not because a real bus looks
like that. No choice here targets a bin beyond that.

Timing conventions are NOT re-derived: this subclasses IoStimulus and
only replaces how `_raw_ui`/`_raw_uio` are filled; SYNC_DELAY, the
`t <= boot_exit_cycle` fast_boot window, and uio bits 4/5/6 = 0 all come
from the base class unchanged.

Determinism: own random.Random(f"stim:protocol_pins:{seed}"), no global
RNG, no wall-clock. Boundedness: the timeline is a finite precomputed
table exactly like the base class; stimulus never affects termination,
since random_gen keeps every WAIT's timeout mandatory and nonzero.
"""

from __future__ import annotations

import math
import random

from io_stimulus import IoStimulus

SCENARIOS = ("uart", "spi", "i2c")
BAUD_CHOICES = (4, 8, 12, 16, 24, 33, 48, 64, 96, 128, 200, 434)  # cycles/bit
STUCK_PROB = 0.08


def _log_uniform(rng: random.Random, lo: int, hi: int) -> int:
    return int(math.exp(rng.uniform(math.log(lo), math.log(hi + 1)))) if hi > lo else lo


class _Wave:
    """Append-only level timeline for one pin, length-capped at n."""

    def __init__(self, n: int):
        self.n = n
        self.levels: list[int] = []

    def full(self) -> bool:
        return len(self.levels) >= self.n

    def hold(self, level: int, cycles: int) -> None:
        room = self.n - len(self.levels)
        if room > 0:
            self.levels.extend([level & 1] * min(max(cycles, 1), room))

    def pad(self, level: int) -> None:
        self.hold(level, self.n - len(self.levels))


def _slow_gpio(rng: random.Random, n: int) -> list[int]:
    w = _Wave(n)
    if rng.random() < STUCK_PROB:
        w.pad(rng.randint(0, 1))
        return w.levels
    level = rng.randint(0, 1)
    while not w.full():
        w.hold(level, _log_uniform(rng, 1, 2000))
        level ^= 1
    return w.levels


def _uart_line(rng: random.Random, n: int) -> list[int]:
    w = _Wave(n)
    baud = rng.choice(BAUD_CHOICES)
    w.hold(1, _log_uniform(rng, 1, 20 * baud))  # idle-high before first frame
    while not w.full():
        for _ in range(rng.randint(1, 6)):  # a message of back-to-back frames
            byte = rng.randint(0, 255)
            w.hold(0, baud)  # start bit
            for i in range(8):
                w.hold((byte >> i) & 1, baud)
            w.hold(1, baud * rng.choice((1, 1, 1, 2)))  # stop bit(s)
        w.hold(1, _log_uniform(rng, baud, 3000))  # inter-message idle
    return w.levels


def _spi_bus(rng: random.Random, n: int):
    mosi, miso, sclk, cs = _Wave(n), _Wave(n), _Wave(n), _Wave(n)
    half = rng.choice((2, 3, 4, 6, 8, 12, 16, 32, 64))

    def all_hold(cycles, m, s, k, c):
        for wv, lv in ((mosi, m), (miso, s), (sclk, k), (cs, c)):
            wv.hold(lv, cycles)

    m = s = 0
    while not cs.full():
        all_hold(_log_uniform(rng, 4, 2500), m, s, 0, 1)  # CS idle high
        setup = rng.randint(1, half)
        m, s = rng.randint(0, 1), rng.randint(0, 1)
        all_hold(setup, m, s, 0, 0)  # CS falls, first bit set up
        for _ in range(8 * rng.randint(1, 4)):
            all_hold(half, m, s, 1, 0)  # SCLK high: sampling edge, data holds
            m, s = rng.randint(0, 1), rng.randint(0, 1)
            all_hold(half, m, s, 0, 0)  # SCLK low: data shifts on falling edge
        all_hold(rng.randint(1, half), m, s, 0, 0)  # CS hold before release
    return mosi.levels, miso.levels, sclk.levels, cs.levels


def _i2c_bus(rng: random.Random, n: int):
    sda, scl = _Wave(n), _Wave(n)
    half = rng.choice((4, 8, 12, 16, 25, 50, 125, 250))
    stretch_prob = rng.choice((0.05, 0.15, 0.4))

    def both(cycles, d, c):
        sda.hold(d, cycles)
        scl.hold(c, cycles)

    while not scl.full():
        both(_log_uniform(rng, half, 3000), 1, 1)  # bus idle, both released
        both(half, 0, 1)  # START: SDA falls while SCL high
        d = 0
        for _ in range(9 * rng.randint(1, 3)):  # bytes of 8 data + ACK
            low = half
            if rng.random() < stretch_prob:
                low += _log_uniform(rng, 20, 1500)  # slave clock-stretch
            q = rng.randint(1, max(1, half // 2))
            both(q, d, 0)  # SCL low, SDA still old
            d = rng.randint(0, 1)
            both(max(1, low - q), d, 0)  # SDA changes only while SCL low
            both(half, d, 1)  # SCL high, SDA stable
        both(half, 0, 0)  # prep STOP: SDA low under SCL low
        both(half, 0, 1)  # SCL rises
        both(half, 1, 1)  # STOP: SDA rises while SCL high
    return sda.levels, scl.levels


def _host_bus(rng: random.Random, n: int) -> list[int]:
    out: list[int] = []
    while len(out) < n:
        out.extend([rng.randint(0, 255)] * _log_uniform(rng, 1, 600))
    return out[:n]


class ProtocolPinsStimulus(IoStimulus):
    def __init__(self, seed: int, boot_exit_cycle: int, max_cycle: int):
        # Deliberately NOT calling IoStimulus.__init__: only the timeline
        # fill changes; every lookup method is inherited untouched.
        self.boot_exit_cycle = boot_exit_cycle
        self.max_cycle = max_cycle
        rng = random.Random(f"stim:protocol_pins:{seed}")
        n = max(0, max_cycle - boot_exit_cycle)
        self.scenario = rng.choice(SCENARIOS)
        if self.scenario == "uart":
            pins = {0: _uart_line(rng, n), 1: _uart_line(rng, n),
                    2: _slow_gpio(rng, n), 3: _slow_gpio(rng, n)}
        elif self.scenario == "spi":
            mosi, miso, sclk, cs = _spi_bus(rng, n)
            pins = {0: mosi, 1: miso, 2: sclk, 3: cs}
        else:
            sda, scl = _i2c_bus(rng, n)
            pins = {0: sda, 1: scl, 2: _slow_gpio(rng, n), 3: _slow_gpio(rng, n)}
        pins[7] = _slow_gpio(rng, n)
        ui = _host_bus(rng, n)

        self._raw_ui: dict[int, int] = {}
        self._raw_uio: dict[int, int] = {}
        for i in range(n):
            t = boot_exit_cycle + 1 + i
            self._raw_ui[t] = ui[i]
            byte = 0
            for bit, lv in pins.items():
                byte |= lv[i] << bit  # bits 4/5/6 never set -- same as base class
            self._raw_uio[t] = byte


def make_stimulus(seed: int, boot_exit_cycle: int, max_cycle: int) -> ProtocolPinsStimulus:
    return ProtocolPinsStimulus(seed, boot_exit_cycle, max_cycle)
