# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""End-to-end protocol test: the USB low-speed HID keyboard firmware
(firmware/protocols/usb_kbd.asm) on the RTL, enumerated and read by an
independent USB host model.

The host follows USB 2.0 chapters 7-9 and HID 1.11: it drives the bus at
exactly 1.5 Mbit/s (the chip's engine runs 1% fast), resets the device,
enumerates it the way an operating system does (GET_DESCRIPTOR device
with wLength 64 at address 0, SET_ADDRESS, the descriptors again at the
new address, SET_CONFIGURATION, SET_IDLE, the report descriptor), then
polls the interrupt endpoint and turns the reports into text.

Every packet the device sends is decoded by usb_ref.py from its line
symbols, with the host's own clock recovery (run lengths at the nominal
bit time), and checked for: PID check bits, CRC16, data toggle, a bit
rate within low-speed's 1.5%, and a turnaround of 2-7.5 bit times after
the host's EOP. The host never accepts a packet the spec would reject.
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import test as T
import usb_ref as U

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

KBD_ASM = Path(os.environ.get("USB_KBD_ASM") or
               Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "usb_kbd.asm")
BIT = 50e6 / 1.5e6                  # clocks per bit at exactly 1.5 Mbit/s
SYM = {(0, 1): "J", (1, 0): "K", (0, 0): "SE0", (1, 1): "SE1"}

# HID usage -> character (unshifted, shifted), HID usage tables 1.12 sec. 10
USAGE = {**{0x04 + i: (chr(97 + i), chr(65 + i)) for i in range(26)},
         **{0x1E + i: (str((i + 1) % 10), "!@#$%^&*()"[i]) for i in range(10)},
         0x28: ("\n", "\n"), 0x2C: (" ", " "), 0x36: (",", "<"), 0x37: (".", ">")}


class UsbHost:
    def __init__(self, dut, host_bits):
        self.dut, self.host = dut, host_bits
        self.t = 0
        self.ext = None                    # host drive (dp, dm) or None
        self.dev = []                      # (t, (dp, dm)) when the device's drive changes
        self.dev_on = False
        self.errors: list[str] = []
        self.eop_end = None                # time the host's last EOP ended (SE0 -> J)
        self.log: list[str] = []
        self.turns: list[float] = []

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            self.t += 1
            oe, out = int(self.dut.uio_oe.value), int(self.dut.uio_out.value)
            on = bool(oe & 1)
            if (oe & 3) not in (0, 3):
                self.errors.append(f"t={self.t}: device drives only one of D+/D- (oe={oe & 3:02b})")
            lv = (out & 1, (out >> 1) & 1) if on else None
            if on and self.ext is not None:
                self.errors.append(f"t={self.t}: contention, host and device both drive")
            if on and (not self.dev_on or lv != self.dev[-1][1]):
                self.dev.append((self.t, lv))
            if self.dev_on and not on:
                self.dev.append((self.t, None))
            self.dev_on = on
            bus = lv if on else (self.ext if self.ext is not None else (0, 1))
            self.dut.uio_in.value = (self.host & ~0b11) | bus[0] | (bus[1] << 1)

    # ---- bus
    async def _clocks(self, n):
        t0 = self.t
        while self.t < t0 + n:
            await RisingEdge(self.dut.clk)

    async def send(self, packet: bytes):
        sym = U.encode(packet)
        t0 = self.t
        for i, s in enumerate(sym):
            while self.t < t0 + round(i * BIT):
                await RisingEdge(self.dut.clk)
            self.ext = U.LEVELS[s]
            if s == "J" and i == len(sym) - 1:
                self.eop_end = self.t
        await self._clocks(round(BIT))
        self.ext = None
        self.log.append(f"H> {U.PID_NAME.get(packet[0], hex(packet[0]))} {packet[1:].hex()}")

    async def reset(self, us=200):
        # USB holds SE0 for >= 10 ms; shortened here to save simulation
        # time, but kept well above the firmware's worst-case detection
        # latency (an 80 us SRX timeout plus a 5 us recheck)
        self.ext = (0, 0)
        await self._clocks(int(us * 50))
        self.ext = None
        await self._clocks(round(20 * BIT))

    async def keepalive(self):
        self.ext = (0, 0)
        await self._clocks(round(2 * BIT))
        self.ext = (0, 1)
        self.eop_end = self.t
        await self._clocks(round(BIT))
        self.ext = None

    async def recv(self, within_bits=18):
        """The device's next packet, or None if it doesn't start within
        `within_bits` bit times of the host's EOP."""
        start = len(self.dev)
        limit = self.eop_end + round(within_bits * BIT)
        while len(self.dev) == start or self.dev[start][1] is None:
            if self.t > limit:
                return None
            await RisingEdge(self.dut.clk)
        while self.dev[-1][1] is not None:          # until the device releases the bus
            await RisingEdge(self.dut.clk)
        ev = self.dev[start:]
        turn = (ev[0][0] - self.eop_end) / BIT
        self.turns.append(turn)
        # the last edge seen at the pins is 3 cycles (2 sync flops + 1)
        # after the device's internal edge; both ends see the same lag
        if not 2 <= turn <= 7.5:
            self.errors.append(f"turnaround {turn:.2f} bit times, outside 2-7.5")
        sym = []
        for (t, lv), (t2, _) in zip(ev, ev[1:]):
            n = round((t2 - t) / BIT)
            if n < 1 or abs((t2 - t) - n * BIT) > 0.25 * BIT:
                self.errors.append(f"run of {t2 - t} clocks is not a whole number of bits")
            sym += [SYM[lv]] * max(n, 1)
        dur = ev[-1][0] - ev[0][0]
        rate = len(sym) / dur * 50e6
        if abs(rate / 1.5e6 - 1) > 0.015:
            self.errors.append(f"device bit rate {rate / 1e6:.4f} Mbit/s, outside 1.5% of 1.5")
        try:
            pkt = U.decode(sym)
        except U.DecodeError as e:
            self.errors.append(f"undecodable device packet: {e}")
            return b""
        if not U.check_pid(pkt[0]):
            self.errors.append(f"bad PID check bits {pkt[0]:#04x}")
        self.log.append(f"D> {U.PID_NAME.get(pkt[0], hex(pkt[0]))} {pkt[1:].hex()}")
        return pkt

    async def gap(self, bits=4):
        await self._clocks(round(bits * BIT))

    # ---- transactions
    async def setup(self, addr, req: bytes):
        for _ in range(3):
            await self.send(U.token("SETUP", addr, 0))
            await self.gap()
            await self.send(U.data_packet("DATA0", req))
            r = await self.recv()
            if r == U.handshake("ACK"):
                return
            await self.gap(20)
        raise AssertionError(f"SETUP {req.hex()} to {addr} not ACKed: {r}")

    async def in_(self, addr, ep, tries=20):
        """One IN transaction (retrying NAKs): ('DATA0'|'DATA1', payload), or ('STALL', None)."""
        for _ in range(tries):
            await self.send(U.token("IN", addr, ep))
            r = await self.recv()
            assert r is not None, f"no answer to IN {addr}.{ep}"
            name = U.PID_NAME.get(r[0])
            if name == "NAK":
                await self.gap(40)
                continue
            if name == "STALL":
                return "STALL", None
            assert name in ("DATA0", "DATA1"), f"IN answered with {r.hex()}"
            assert U.check_data(r), f"bad CRC16 in {r.hex()}"
            await self.gap()
            await self.send(U.handshake("ACK"))
            return name, r[1:-2]
        return "NAK", None

    async def control_read(self, addr, req: bytes):
        wlen = req[6] | req[7] << 8
        await self.setup(addr, req)
        data, want = b"", "DATA1"
        while True:
            pid, chunk = await self.in_(addr, 0)
            if pid == "STALL":
                return None
            assert pid == want, f"data toggle {pid}, want {want}"
            want = "DATA0" if want == "DATA1" else "DATA1"
            data += chunk
            assert len(chunk) <= 8
            if len(chunk) < 8 or len(data) >= wlen:
                break
        await self.send(U.token("OUT", addr, 0))
        await self.gap()
        await self.send(U.data_packet("DATA1", b""))
        assert await self.recv() == U.handshake("ACK"), "status stage not ACKed"
        await self.gap(40)
        return data

    async def control_nodata(self, addr, req: bytes):
        await self.setup(addr, req)
        pid, chunk = await self.in_(addr, 0)
        assert (pid, chunk) == ("DATA1", b""), f"status stage {pid} {chunk}"
        await self.gap(100)


def req(bmrt, breq, wvalue=0, windex=0, wlength=0):
    return bytes([bmrt, breq, wvalue & 0xFF, wvalue >> 8, windex & 0xFF, windex >> 8,
                  wlength & 0xFF, wlength >> 8])


async def boot_kbd(dut):
    prog = assembler.assemble_file(KBD_ASM)
    assert not prog.warnings, prog.warnings
    img = prog.image()
    img += [0] * (len(img) % 2)
    words = [img[i] | (img[i + 1] << 8) for i in range(0, len(img), 2)]
    uio = await T.reset_and_boot(dut, words)
    host = UsbHost(dut, uio)
    cocotb.start_soon(host.run())
    await ClockCycles(dut.clk, 200)
    return host


async def enumerate_(host, addr=7):
    await host.reset()
    dev = await host.control_read(0, req(0x80, 6, 0x0100, 0, 64))
    assert dev is not None and len(dev) == 18, f"device descriptor {dev}"
    await host.reset()
    await host.control_nodata(0, req(0x00, 5, addr))
    dev = await host.control_read(addr, req(0x80, 6, 0x0100, 0, 18))
    cfg9 = await host.control_read(addr, req(0x80, 6, 0x0200, 0, 9))
    total = cfg9[2] | cfg9[3] << 8
    cfg = await host.control_read(addr, req(0x80, 6, 0x0200, 0, total))
    await host.control_nodata(addr, req(0x00, 9, 1))
    await host.control_nodata(addr, req(0x21, 0x0A, 0, 0))          # HID SET_IDLE
    hid = cfg[18:27]
    rep = await host.control_read(addr, req(0x81, 6, 0x2200, 0, hid[7] | hid[8] << 8))
    return dev, cfg, rep


@cocotb.test()
async def test_usb_kbd_enumerates(dut):
    """Enumeration as an OS does it; the descriptors say: USB 1.1, EP0
    of 8 bytes, VID:PID 1209:0001, one configuration of one HID boot
    keyboard interface with one interrupt IN endpoint (0x81, 8 bytes);
    the report descriptor is the HID boot keyboard's. A descriptor the
    device doesn't have is STALLed. Every device packet checked."""
    host = await boot_kbd(dut)
    dev, cfg, rep = await enumerate_(host)
    assert dev[:2] == bytes([18, 1]) and dev[2:4] == bytes([0x10, 0x01]) and dev[7] == 8
    assert (dev[8] | dev[9] << 8, dev[10] | dev[11] << 8) == (0x1209, 0x0001)
    assert dev[17] == 1
    dut._log.info(f"turnaround {min(host.turns):.2f}-{max(host.turns):.2f} bit times "
                  f"over {len(host.turns)} device packets")
    assert len(cfg) == (cfg[2] | cfg[3] << 8) == 34 and cfg[4] == 1
    intf, hid, ep = cfg[9:18], cfg[18:27], cfg[27:34]
    assert intf[1] == 4 and intf[4:8] == bytes([1, 3, 1, 1]), intf.hex()     # 1 EP, HID boot keyboard
    assert hid[1] == 0x21 and hid[6] == 0x22 and hid[7] == len(rep)
    assert ep[1] == 5 and ep[2] == 0x81 and ep[3] == 3 and ep[4] == 8, ep.hex()
    assert rep[:4] == bytes([0x05, 0x01, 0x09, 0x06]) and rep[-1] == 0xC0      # keyboard collection
    # a string descriptor: not there, STALL
    await host.setup(7, req(0x80, 6, 0x0301, 0x0409, 255))
    pid, _ = await host.in_(7, 0)
    assert pid == "STALL", pid
    assert not host.errors, host.errors[:5]


@cocotb.test()
async def test_usb_kbd_types(dut):
    """After enumeration the interrupt endpoint delivers press/release
    reports with alternating toggles; the host's text is 'Hello, TT!'
    and Enter. Before SET_CONFIGURATION EP1 only NAKs; a keep-alive EOP
    and a token for another address are ignored; a bus reset sends the
    device back to address 0."""
    host = await boot_kbd(dut)
    await host.reset()
    await host.control_nodata(0, req(0x00, 5, 9))
    # not configured: EP1 NAKs
    await host.send(U.token("IN", 9, 1))
    assert await host.recv() == U.handshake("NAK")
    await host.gap(40)
    await host.control_nodata(9, req(0x00, 9, 1))
    text, want, mods_keys = "", "DATA0", []
    for _ in range(60):
        await host.keepalive()
        await host.gap(10)
        await host.send(U.token("IN", 5, 1))                 # someone else's: no answer
        assert await host.recv(within_bits=20) is None
        await host.gap(10)
        pid, rpt = await host.in_(9, 1, tries=1)
        if pid == "NAK":
            break
        assert pid == want, f"EP1 toggle {pid}, want {want}"
        want = "DATA0" if want == "DATA1" else "DATA1"
        assert len(rpt) == 8 and rpt[1] == 0 and rpt[3:] == bytes(5), rpt.hex()
        mods_keys.append((rpt[0], rpt[2]))
        if rpt[2]:
            text += USAGE[rpt[2]][1 if rpt[0] & 0x22 else 0]
        await host.gap(40)
    assert text == "Hello, TT!\n", repr(text)
    # every press is followed by a release
    assert all(k == (0, 0) for k in mods_keys[1::2]), mods_keys
    # bus reset: back at address 0
    await host.reset()
    dev = await host.control_read(0, req(0x80, 6, 0x0100, 0, 8))
    assert dev == bytes([18, 1, 0x10, 0x01, 0, 0, 0, 8])
    assert not host.errors, host.errors[:5]
