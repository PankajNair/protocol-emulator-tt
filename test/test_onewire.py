# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""End-to-end protocol test: the 1-Wire controller firmware
(firmware/protocols/onewire.asm) running on the RTL, talking to an
independent 1-Wire device model written from the 1-Wire spec (Maxim
AN126 / DS18B20 datasheet timing).

The device model and the chip share one open-drain line with a pull-up:
the line is low if either pulls it low. The model only sees the line and
the chip's own drive (to time the controller's pulses); it never looks
inside the chip. It checks every controller pulse against the spec:
reset low 480-960 us, write-0 low 60-120 us, write-1/read low 1-15 us,
time slot 60-120 us, recovery >= 1 us, and >= 480 us after a reset
before the next slot.
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import test as T  # shared boot helpers

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

# ONEWIRE_ASM overrides the firmware, for checking that broken variants fail
OW_ASM = Path(os.environ.get("ONEWIRE_ASM") or
              Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "onewire.asm")
DQ = 0
US = 50  # cycles per microsecond at 50 MHz
OP_RESET, OP_WRITE, OP_READ = 1, 2, 3


def crc8(data):
    """Dallas/Maxim 1-Wire CRC8 (x^8 + x^5 + x^4 + 1, reflected)."""
    crc = 0
    for b in data:
        for _ in range(8):
            mix = (crc ^ b) & 1
            crc >>= 1
            if mix:
                crc ^= 0x8C
            b >>= 1
    return crc


ROM_BODY = [0x28, 0x6B, 0x1F, 0x5A, 0x0C, 0x00, 0x00]  # DS18B20 family 0x28, serial
ROM = ROM_BODY + [crc8(ROM_BODY)]


class OneWireBus:
    """Drives uio_in every cycle: the host-controlled bits plus the
    resolved DQ line; models one device (or none) on the line."""

    def __init__(self, dut, host_bits, present=True, presence=(60, 60)):
        self.dut, self.host, self.present = dut, host_bits, present
        # presence pulse (wait after reset release, length), us. The spec
        # allows wait 15-60 and length 60-240; tests use both extremes.
        self.presence = presence
        self.errors: list[str] = []
        self.written: list[int] = []
        self._rx_bits: list[int] = []
        self._tx_bits: list[int] = []
        self.t = 0
        self.dev_low_until = -1       # device pulls the line low until this cycle
        self.presence_at = None       # (start, end) of a pending presence pulse
        self.m_low_since = None       # controller drive low since (cycle)
        self.last_slot_start = None
        self.last_release = None
        self.reset_release = None
        self.sample_at = None

    def master_low(self):
        oe = (int(self.dut.uio_oe.value) >> DQ) & 1
        out = (int(self.dut.uio_out.value) >> DQ) & 1
        return oe and not out

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            self.t += 1
            self._step(self.master_low())

    def _step(self, m_low):
        t = self.t
        if m_low and self.m_low_since is None:             # controller pulled low: slot or reset starts
            self.m_low_since = t
            if self.last_release is not None and t - self.last_release < 1 * US:
                self.errors.append(f"recovery {(t - self.last_release) / US:.2f} us < 1 us")
            if self.reset_release is not None and t - self.reset_release < 480 * US:
                self.errors.append(f"slot {(t - self.reset_release) / US:.1f} us after reset, want >= 480")
            if self.last_slot_start is not None and self.reset_release is None:
                slot = (t - self.last_slot_start) / US
                if not 60 <= slot <= 120:
                    self.errors.append(f"time slot {slot:.1f} us, want 60-120")
            self.last_slot_start = t
            self.reset_release = None
            if self.present and self._tx_bits:              # device answers in this slot
                if self._tx_bits.pop(0) == 0:
                    # worst case: a device need only hold a 0 for 15 us,
                    # so a controller sampling late reads a 1
                    self.dev_low_until = t + 15 * US
            elif self.present:
                self.sample_at = t + 30 * US                # device samples a written bit
        elif not m_low and self.m_low_since is not None:   # controller released
            low = (t - self.m_low_since) / US
            self.m_low_since = None
            self.last_release = t
            if low >= 480:
                if low > 960:
                    self.errors.append(f"reset low {low:.1f} us > 960")
                self.reset_release = t
                self.last_slot_start = None
                self.sample_at = None
                self._rx_bits, self._tx_bits = [], []
                if self.present:
                    w, n = self.presence
                    self.presence_at = (t + w * US, t + (w + n) * US)
            elif not (1 <= low <= 15 or 60 <= low <= 120):
                self.errors.append(f"controller low {low:.2f} us is no valid 1-Wire pulse")
        if self.sample_at is not None and t >= self.sample_at:
            self.sample_at = None
            self._rx_bits.append(0 if self.line_low() else 1)
            if len(self._rx_bits) == 8:
                byte = sum(b << i for i, b in enumerate(self._rx_bits))  # LSB first
                self._rx_bits = []
                self.written.append(byte)
                if byte == 0x33:  # READ ROM: send the 64-bit ROM code
                    self._tx_bits = [(b >> i) & 1 for b in ROM for i in range(8)]
        dev_low = t < self.dev_low_until
        if self.presence_at and self.presence_at[0] <= t < self.presence_at[1]:
            dev_low = True
        elif self.presence_at and t >= self.presence_at[1]:
            self.presence_at = None
        self._dev_low = dev_low
        line = 0 if (m_low or dev_low) else 1
        self.dut.uio_in.value = (self.host & ~(1 << DQ)) | (line << DQ)

    def line_low(self):
        return self.master_low() or getattr(self, "_dev_low", False)

    # ---- host side (lockstep handshake), via the host bits -----------------
    async def _go(self, level, wait_status, max_cycles):
        self.host = (self.host | (1 << T.HOST_GO_BIT)) if level else (self.host & ~(1 << T.HOST_GO_BIT))
        await T.wait_host_status(self.dut, wait_status, max_cycles=max_cycles)

    async def send(self, b, max_cycles=200_000):
        self.dut.ui_in.value = b
        await self._go(1, 1, max_cycles)
        await self._go(0, 0, max_cycles)

    async def recv(self, max_cycles=200_000):
        await self._go(1, 1, max_cycles)
        v = int(self.dut.uo_out.value)
        await self._go(0, 0, max_cycles)
        return v

    async def reset(self):
        await self.send(OP_RESET)
        return await self.recv()

    async def write(self, b):
        await self.send(OP_WRITE)
        await self.send(b)
        return await self.recv()

    async def read(self):
        await self.send(OP_READ)
        return await self.recv()


async def boot_onewire(dut, present=True, presence=(60, 60)):
    prog = assembler.assemble_file(OW_ASM)
    assert not prog.warnings, prog.warnings
    uio = await T.reset_and_boot(dut, prog.words)
    bus = OneWireBus(dut, uio, present, presence)
    bus.task = cocotb.start_soon(bus.run())
    await ClockCycles(dut.clk, 20)
    assert (int(dut.uio_oe.value) >> DQ) & 1 == 0, "DQ should be released (open-drain high) at idle"
    return bus


@cocotb.test()
async def test_onewire_presence(dut):
    """Reset reports presence 0 with a device on the line, for the
    earliest-shortest (15 us wait, 60 us) and latest (60 us wait, 60 us)
    presence pulse the spec allows."""
    for presence in ((15, 60), (60, 60)):
        bus = await boot_onewire(dut, present=True, presence=presence)
        assert await bus.reset() == 0, f"device present (pulse {presence} us) but presence byte != 0"
        assert not bus.errors, bus.errors
        bus.task.cancel()  # the next boot drives uio_in itself


@cocotb.test()
async def test_onewire_no_device(dut):
    """Reset reports presence 1 on an empty line."""
    bus = await boot_onewire(dut, present=False)
    assert await bus.reset() == 1, "no device but presence byte != 1"
    assert not bus.errors, bus.errors


@cocotb.test()
async def test_onewire_read_rom(dut):
    """READ ROM: reset, write 0x33, read 8 bytes; the ROM code arrives
    intact (CRC8 over all 8 bytes is 0) and every slot is in spec."""
    bus = await boot_onewire(dut)
    assert await bus.reset() == 0
    assert await bus.write(0x33) == 0
    rom = [await bus.read() for _ in range(8)]
    assert not bus.errors, bus.errors[:10]
    assert bus.written == [0x33], bus.written
    assert rom == ROM, [hex(b) for b in rom]
    assert crc8(rom) == 0, "CRC8 over the ROM code should be 0"
    dut._log.info(f"1-Wire READ ROM: {' '.join(f'{b:02x}' for b in rom)}")


@cocotb.test()
async def test_onewire_write_bytes(dut):
    """Writes arrive bit-exact (LSB first), every slot in spec, and a
    second reset clears the device's byte position."""
    bus = await boot_onewire(dut)
    assert await bus.reset() == 0
    for b in (0xCC, 0x44, 0xA5, 0x00, 0xFF):
        assert await bus.write(b) == 0
    assert await bus.reset() == 0
    assert await bus.write(0x5A) == 0
    assert not bus.errors, bus.errors[:10]
    assert bus.written == [0xCC, 0x44, 0xA5, 0x00, 0xFF, 0x5A], [hex(b) for b in bus.written]


@cocotb.test()
async def test_onewire_bad_command(dut):
    """An unknown command byte raises HOST_ERROR and is otherwise ignored:
    a reset afterwards still works."""
    bus = await boot_onewire(dut)
    await bus.send(7)
    await ClockCycles(dut.clk, 50)
    oe, out = int(dut.uio_oe.value), int(dut.uio_out.value)
    assert (oe >> 6) & 1 and (out >> 6) & 1, "HOST_ERROR not raised for an unknown command"
    assert await bus.reset() == 0
    assert not bus.errors, bus.errors
