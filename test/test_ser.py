# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Directed tests of the serial engine (docs/serial_engine.md) and its
four opcodes, SCFG/STX/SRX/SRD, through firmware on the whole chip.

The line is checked against independent references: usb_ref.py (USB
2.0's own formulation: byte-wise reflected CRCs, stuffing on data bits)
for the USB settings, and test_can.py's stuffing and CRC-15 for the
CAN-style settings. Transmitted bits are checked for exact timing (every
bit exactly P clocks) as well as value.
"""

import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

import test as T
import usb_ref as U
from test_can import crc15, stuff as can_stuff

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

DP, DM = 0, 1
P_USB = 33

# USB low-speed settings (docs/serial_engine.md table)
USB_CFG = {0: 33, 1: 0, 2: 16, 3: 0, 5: 0xF7, 6: 0x05, 7: 0x80, 8: 0x12,
           4: 0b11100111}


def cfg_asm(cfg: dict) -> str:
    """SCFG writes, mode (k=4) last so receive is enabled only once the
    rest is set."""
    order = [k for k in cfg if k != 4] + ([4] if 4 in cfg else [])
    return "\n".join(f"LDI R0, {cfg[k]}\nSCFG R0, {k}" for k in order)


HANDSHAKE = """
getb:   WAIT HOST_GO, 1
        IN   R0
        SET  HOST_STATUS, 1
        WAIT HOST_GO, 0
        SET  HOST_STATUS, 0
        RET
putb:   WAIT HOST_GO, 1
        OUT  R0
        SET  HOST_STATUS, 1
        WAIT HOST_GO, 0
        SET  HOST_STATUS, 0
        RET
"""


class Bus:
    """Pins 0/1 as a bus: the chip's drive where it drives, else the
    host's, else idle (USB low-speed: D+ pulled down, D- pulled up = J;
    or a single-ended line idling at `idle`). uio_in gets the bus, so
    the chip sees its own transmissions too, as on the real pads."""

    def __init__(self, dut, host_bits, idle=(0, 1)):
        self.dut, self.host = dut, host_bits
        self.idle = idle
        self.ext = None          # (dp, dm) the far end drives, or None
        self.t = 0
        self.trace = []          # per cycle: (oe0, oe1, out0, out1)

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            self.t += 1
            oe, out = int(self.dut.uio_oe.value), int(self.dut.uio_out.value)
            self.trace.append((oe & 1, (oe >> 1) & 1, out & 1, (out >> 1) & 1))
            lv = list(self.ext if self.ext is not None else self.idle)
            for i in (0, 1):
                if (oe >> i) & 1:
                    lv[i] = (out >> i) & 1
            v = (self.host & ~0b11) | lv[0] | (lv[1] << 1)
            self.dut.uio_in.value = v

    async def drive(self, symbols, bit=1.5e6):
        """Drive USB symbols at an exact `bit` rate (50 MHz clock)."""
        per = 50e6 / bit
        t0 = self.t
        for i, s in enumerate(symbols):
            while self.t < t0 + round(i * per):
                await RisingEdge(self.dut.clk)
            self.ext = U.LEVELS[s]
        while self.t < t0 + round(len(symbols) * per):
            await RisingEdge(self.dut.clk)
        self.ext = None

    async def drive_levels(self, levels, per, pin=1):
        """Single-ended: drive `levels` on one pin, `per` clocks each."""
        for lv in levels:
            e = list(self.idle)
            e[pin] = lv
            self.ext = tuple(e)
            await ClockCycles(self.dut.clk, per)
        self.ext = None

    # ---- host side (HOST_GO/STATUS handshake)
    async def _go(self, level, status, max_cycles=200000):
        self.host = (self.host | (1 << T.HOST_GO_BIT)) if level else (self.host & ~(1 << T.HOST_GO_BIT))
        await T.wait_host_status(self.dut, status, max_cycles=max_cycles)

    async def recv(self, max_cycles=200000):
        await self._go(1, 1, max_cycles)
        v = int(self.dut.uo_out.value)
        await self._go(0, 0)
        return v

    async def send(self, b):
        self.dut.ui_in.value = b
        await self._go(1, 1)
        await self._go(0, 0)


async def boot(dut, src, idle=(0, 1)):
    prog = assembler.assemble(src, name="ser_test")
    assert not prog.warnings, prog.warnings
    img = prog.image()
    img += [0] * (len(img) % 2)
    words = [img[i] | (img[i + 1] << 8) for i in range(0, len(img), 2)]
    uio = await T.reset_and_boot(dut, words)
    bus = Bus(dut, uio, idle)
    cocotb.start_soon(bus.run())
    return bus


def tx_bits(trace, per, pins=(0,)):
    """From the cycle trace: the packet the chip drove, as per-bit tuples
    of pin levels. Checks the pins are driven for whole bits and each
    bit holds its level for exactly `per` clocks."""
    start = next(i for i, c in enumerate(trace) if c[0])
    end = next(i for i in range(start, len(trace)) if not trace[i][0])
    n, rem = divmod(end - start, per)
    assert rem == 0, f"driven for {end - start} clocks, not whole {per}-clock bits"
    bits = []
    for k in range(n):
        seg = trace[start + k * per:start + (k + 1) * per]
        vals = {tuple(c[2 + p] for p in pins) for c in seg}
        assert len(vals) == 1, f"bit {k} changes level inside its {per} clocks: {vals}"
        oes = {tuple(c[p] for p in pins) for c in seg}
        assert oes == {tuple(1 for _ in pins)}, f"bit {k}: pins not all driven {oes}"
        bits.append(vals.pop())
    return bits


def symbols_of(bits):
    name = {(0, 1): "J", (1, 0): "K", (0, 0): "SE0"}
    return [name.get(b, f"SE1{b}") for b in bits]


TX_USB = """
{cfg}
{body}
done:   JMP done
""" + HANDSHAKE


@cocotb.test()
async def test_ser_tx_usb_packet(dut):
    """USB settings: a DATA0 packet with a payload that forces stuffing
    goes out as SYNC, PID, payload, the engine's CRC16, EOP; D- is the
    complement of D+ for every data bit; usb_ref decodes it and its own
    CRC16 agrees; then both pins are released."""
    payload = bytes([0xFF, 0xFF, 0x3F, 0x00, 0x7E, 0x80])
    pkt = bytes([U.PID["DATA0"]]) + payload
    body = "\n".join(f"LDI R1, {b}\nSTX R1" for b in [U.SYNC, *pkt])
    bus = await boot(dut, TX_USB.format(cfg=cfg_asm(USB_CFG), body=body))
    await ClockCycles(dut.clk, 120 * P_USB)
    bits = tx_bits(bus.trace, P_USB, pins=(0, 1))
    sym = symbols_of(bits)
    got = U.decode(sym)
    assert got == U.data_packet("DATA0", payload), (got.hex(), sym)
    assert sym[-3:] == ["SE0", "SE0", "J"], sym[-3:]
    assert sym == U.encode(U.data_packet("DATA0", payload)), "line differs from usb_ref's encoding"


@cocotb.test()
async def test_ser_rx_usb_packets(dut):
    """USB settings: packets from an exact 1.5 Mbit/s host (the engine
    runs at 33 clocks, 1% fast, and resyncs on edges) are received byte
    for byte; SRX then reports the end; the CRC register holds CRC16's
    residual 0x800D for a good data packet, and status shows SE0 ended
    it. A token and a handshake too."""
    src = cfg_asm(USB_CFG) + """
loop:   SRX  R0
        BEQ  end
        CALL putb
        JMP  loop
end:    LDI  R0, 0xEE
        CALL putb
        SRD  R0, 0
        CALL putb
        SRD  R0, 1
        CALL putb
        SRD  R0, 2
        CALL putb
        LDI  R0, 1
        SCFG R0, 15
        JMP  loop
""" + HANDSHAKE
    bus = await boot(dut, src)
    await ClockCycles(dut.clk, 200)
    for pkt in (U.data_packet("DATA1", bytes(range(0x30, 0x38))),
                U.token("SETUP", 0x15, 1), U.handshake("ACK"),
                U.data_packet("DATA0", bytes([0xFF] * 8))):
        drv = cocotb.start_soon(bus.drive(U.encode(pkt)))
        got = []
        while True:
            v = await bus.recv()
            if v == 0xEE and len(got) == len(pkt) + 1:
                break
            got.append(v)
        st, lo, hi = [await bus.recv() for _ in range(3)]
        await drv
        assert bytes(got) == bytes([U.SYNC]) + pkt, (bytes(got).hex(), pkt.hex())
        assert st & 0b0001_1100 == 0b0000_0100, f"status {st:#010b}: want SE0 end, no stuff error/overrun"
        if pkt[0] in (U.PID["DATA0"], U.PID["DATA1"]):
            assert hi << 8 | lo == 0x800D, f"CRC residual {hi << 8 | lo:#06x}"
        await ClockCycles(dut.clk, 100)


@cocotb.test()
async def test_ser_rx_stuff_error_and_timeout(dut):
    """Seven 1s with no stuff bit end the packet with a stuff error
    (status bit 3, SRX flag 1, no SE0 bit); with no packet at all SRX
    times out (flag 1, Rd unchanged) after its timeout."""
    src = cfg_asm(USB_CFG) + """
        LDI  R0, 0x55
        SRX  R0, 2016
        BNE  bad
        CALL putb               ; 0x55: unchanged after the timeout
wait:   SRX  R0
        BNE  wait               ; skip the bytes
        SRD  R0, 0
        CALL putb
        JMP  wait
bad:    LDI  R0, 0xBB
        CALL putb
        JMP  wait
""" + HANDSHAKE
    bus = await boot(dut, src)
    t0 = bus.t
    assert await bus.recv() == 0x55
    assert bus.t - t0 >= 2016, f"SRX timed out after {bus.t - t0} cycles"
    # SYNC, then 8 ones as data with no stuff bit: NRZI holds the level
    sym = U.encode(bytes([U.PID["ACK"]]))[:8]
    sym += [sym[-1]] * 8 + ["SE0", "SE0", "J"]
    cocotb.start_soon(bus.drive(sym))
    st = await bus.recv()
    assert st & 0b1100 == 0b1000, f"status {st:#010b}: want a stuff error, no SE0 end"


TX_CAN = """
{cfg}
{body}
done:   JMP done
"""


@cocotb.test()
async def test_ser_tx_can_style(dut):
    """CAN-style settings (NRZ, idle 1, stuff after 5, CRC-15 0x4599 from
    byte 0, not inverted, single-ended on pin 0, 20-clock bits): the line
    matches test_can.py's stuffing of the data bits plus its CRC-15, and
    pin 1 is never driven."""
    cfg = {0: 20, 1: 0, 2: 10, 3: 0, 5: (14 << 4) | 5, 6: 0x99, 7: 0x45, 8: 0x00,
           4: 0b0010_1000}           # idle 1, crc_tx; no nrzi/diff/se0/rx
    data = [0x00, 0xFF, 0x0F, 0xC1]
    body = "\n".join(f"LDI R1, {b}\nSTX R1" for b in data)
    bus = await boot(dut, TX_CAN.format(cfg=cfg_asm(cfg), body=body), idle=(1, 1))
    await ClockCycles(dut.clk, 80 * 20)
    bits = [b[0] for b in tx_bits(bus.trace, 20, pins=(0,))]
    assert not any(c[1] for c in bus.trace), "pin 1 driven in single-ended mode"
    dbits = [(b >> i) & 1 for b in data for i in range(8)]
    c = crc15(dbits)
    want = can_stuff(dbits + [(c >> (14 - i)) & 1 for i in range(15)])
    assert bits == want, f"\n got {bits}\nwant {want}"


@cocotb.test()
async def test_ser_rx_can_style_and_overrun(dut):
    """CAN-style receive on pin 1 (NRZ, stuff after 5): bytes arrive
    destuffed; the CRC-15 register matches test_can.py's over the bytes.
    A firmware that reads late sees the newest byte and the overrun bit."""
    cfg = {0: 20, 1: 0, 2: 10, 3: 0, 5: (14 << 4) | 5, 6: 0x99, 7: 0x45, 8: 0x00,
           4: 0b1001_1000}           # rx_en, rx_pin 1, idle 1
    src = cfg_asm(cfg) + """
        SRX  R0
        CALL putb
        SRX  R0
        CALL putb
        DELAY 400               ; miss a byte: overrun
        SRX  R0
        CALL putb
        SRD  R0, 0
        CALL putb
        SRD  R0, 1
        CALL putb
        SRD  R0, 2
        CALL putb
done:   JMP done
""" + HANDSHAKE
    bus = await boot(dut, src, idle=(1, 1))
    data = [0x00, 0xF0, 0xAA, 0x3C]
    dbits = [(b >> i) & 1 for b in data for i in range(8)]
    line = can_stuff(dbits)
    # the packet starts at the first dominant bit: data byte 0 bit 0 must be 0
    await ClockCycles(dut.clk, 200)              # firmware has enabled receive
    cocotb.start_soon(bus.drive_levels(line + [1] * 10, 20, pin=1))
    got = [await bus.recv() for _ in range(3)]
    st, lo, hi = [await bus.recv() for _ in range(3)]
    assert got == [0x00, 0xF0, 0x3C], [hex(x) for x in got]
    assert st & 0b1_0000, f"status {st:#010b}: no overrun"
    # the idle 1s after the last byte are data until the sixth (a stuff
    # error): five of them are in the CRC
    want = crc15(dbits + [1] * 5)
    assert hi << 8 | lo == want, f"CRC {hi << 8 | lo:#06x} != {want:#06x}"


def line_stuff(bits, k, idle):
    """docs/serial_engine.md "Bit stuffing, in line levels" for NRZ data:
    after k bit times at one level the next bit time is the other level.
    Returns the line levels, including a stuff bit due after the last."""
    out, cur, run = [], idle, 0
    for b in bits:
        if k and run == k:
            cur = 1 - cur
            out.append(cur)
            run = 1
        run = min(15, run + 1) if b == cur else 1
        cur = b
        out.append(b)
    if k and run == k:
        out.append(1 - cur)
    return out


def lfsr(bits, width, poly, init):
    """docs/serial_engine.md CRC: bit-serial, transmission order."""
    mask = (1 << width) - 1
    c = init
    for b in bits:
        fb = b ^ ((c >> (width - 1)) & 1)
        c = ((c << 1) ^ (poly if fb else 0)) & mask
    return c


@cocotb.test()
async def test_ser_tx_generic_crc_long_packet(dut):
    """Settings far from USB's: NRZ, idle 0, stuff after 9, CRC width 16
    with polynomial 0x3A62 from byte 13 starting at 0, not inverted. A
    17-byte packet (past the byte index's saturation at 15) matches the
    doc's stuffing and CRC computed here."""
    cfg = {0: 8, 1: 0, 2: 4, 3: 0, 5: (15 << 4) | 9, 6: 0x62, 7: 0x3A, 8: 13,
           4: 0b0010_0000}           # crc_tx; idle 0; no nrzi/diff/se0/rx
    data = [0x00, 0xFF, 0x80, 0x7F, 0x01, 0xFE, 0x00, 0x00, 0x55, 0xAA,
            0xFF, 0xFF, 0x0F, 0xF0, 0x3C, 0x00, 0xC3]
    body = "\n".join(f"LDI R1, {b}\nSTX R1" for b in data)
    bus = await boot(dut, TX_CAN.format(cfg=cfg_asm(cfg), body=body), idle=(0, 1))
    await ClockCycles(dut.clk, 200 * 8)
    bits = [b[0] for b in tx_bits(bus.trace, 8, pins=(0,))]
    dbits = [(b >> i) & 1 for b in data for i in range(8)]
    c = lfsr(dbits[13 * 8:], 16, 0x3A62, 0)
    want = line_stuff(dbits + [(c >> (15 - i)) & 1 for i in range(16)], 9, 0)
    assert bits == want, f"\n got {bits}\nwant {want}"


@cocotb.test()
async def test_ser_slow_bits_no_stuffing(dut):
    """The longest bit time, 4095 clocks: two 0x00 bytes go out as 16
    bit times of the same level with no stuffing (K = 0), each exactly
    4095 clocks; then two bytes received at 4095 clocks a bit sampled late
    (S = 4069) on pin 1."""
    cfg = {0: 0xFF, 1: 0x0F, 2: 0xE5, 3: 0x0F, 5: 0, 8: 0, 4: 0b0000_1000}  # idle 1
    src = cfg_asm(cfg) + """
        LDI  R1, 0
        STX  R1
        STX  R1
wt:     SRD  R0, 0
        TEST R0, 5
        BEQ  wt
        LDI  R0, 0b10011000     ; receive on pin 1
        SCFG R0, 4
        SRX  R0
        CALL putb
        SRX  R0
        CALL putb
done:   JMP  done
""" + HANDSHAKE
    bus = await boot(dut, src, idle=(1, 1))
    await ClockCycles(dut.clk, 17 * 4095)
    bits = [b[0] for b in tx_bits(bus.trace, 4095, pins=(0,))]
    assert bits == [0] * 16, bits
    await ClockCycles(dut.clk, 200)
    data = [0x5A, 0x3C]
    cocotb.start_soon(bus.drive_levels([(b >> i) & 1 for b in data for i in range(8)] + [1], 4095, pin=1))
    got = [await bus.recv(max_cycles=300000) for _ in range(2)]
    assert got == data, [hex(x) for x in got]


@cocotb.test()
async def test_ser_abandon_and_cut_packet(dut):
    """Command 15 bit 1 abandons a packet mid-way: its other bytes never
    arrive and no end is signalled; a stuff error does the same but
    signals the end. Either way, in se0 mode the engine waits for the
    packet's own EOP, through the J levels inside it, and then receives
    the next packet from its SYNC."""
    src = cfg_asm(USB_CFG) + """
        SRX  R0                 ; SYNC
        CALL putb
        SRX  R0                 ; PID
        CALL putb
        LDI  R0, 2
        SCFG R0, 15             ; abandon
        LDI  R0, 0x77
        SRX  R0, 6144           ; nothing more of it, no end
        CALL putb
nxt:    SRX  R0
        BEQ  endp
        CALL putb
        JMP  nxt
endp:   LDI  R0, 0xEE
        CALL putb
        JMP  nxt
""" + HANDSHAKE
    bus = await boot(dut, src)
    await ClockCycles(dut.clk, 200)
    # payload with long J runs inside (many 1s in a row = no level change)
    pkt = U.data_packet("DATA0", bytes([0xFF, 0x7F, 0xFE, 0x00, 0xFF, 0x3F, 0xFF, 0xFF]))
    drv = cocotb.start_soon(bus.drive(U.encode(pkt)))
    assert [await bus.recv() for _ in range(2)] == [U.SYNC, pkt[0]]
    assert await bus.recv() == 0x77, "abandoned packet delivered more bytes"
    await drv
    await ClockCycles(dut.clk, 100)
    ack = U.handshake("ACK")
    cocotb.start_soon(bus.drive(U.encode(ack)))
    got = [await bus.recv() for _ in range(3)]
    assert got == [U.SYNC, ack[0], 0xEE], [hex(x) for x in got]
    # a stuff error mid-packet: the end comes only after the packet's EOP
    await ClockCycles(dut.clk, 100)
    bad = U.encode(pkt)
    k = bad.index("SE0")
    bad = bad[:12] + [bad[11]] * 8 + bad[12:]   # 8 bit times without a change
    t0 = bus.t
    cocotb.start_soon(bus.drive(bad))
    got = []
    while True:
        v = await bus.recv()
        if v == 0xEE:
            break
        got.append(v)
    assert bus.t - t0 >= (len(bad) - 3) * 33, "end signalled before the packet's EOP"
    await ClockCycles(dut.clk, 200)
    cocotb.start_soon(bus.drive(U.encode(ack)))
    got = [await bus.recv() for _ in range(3)]
    assert got == [U.SYNC, ack[0], 0xEE], [hex(x) for x in got]


@cocotb.test()
async def test_ser_abandon_waits_k_plus_1_idle_bits(dut):
    """Without se0 (CAN-style: idle 1, stuff after 5), an abandoned packet
    is skipped until K+1 = 6 bit times at the idle level: its own runs of
    up to 5 recessive bits don't end the wait, so nothing of it is
    received; the next packet after a real idle gap is."""
    cfg = {0: 20, 1: 0, 2: 10, 3: 0, 5: (14 << 4) | 5, 6: 0x99, 7: 0x45, 8: 0x00,
           4: 0b1001_1000}           # rx_en, rx_pin 1, idle 1
    src = cfg_asm(cfg) + """
        SRX  R0
        CALL putb
        LDI  R0, 2
        SCFG R0, 15             ; abandon
        LDI  R0, 0x77
        SRX  R0, 1984
        CALL putb               ; 0x77: nothing more arrived
nxt:    SRX  R0
        CALL putb
        JMP  nxt
""" + HANDSHAKE
    bus = await boot(dut, src, idle=(1, 1))
    await ClockCycles(dut.clk, 200)
    # many runs of exactly 5 recessive bits inside the packet
    data = [0x00, 0xE0, 0x83, 0x0F, 0x3E, 0xF8, 0xE0, 0x83]
    line = can_stuff([(b >> i) & 1 for b in data for i in range(8)])
    drv = cocotb.start_soon(bus.drive_levels(line, 20, pin=1))
    assert await bus.recv() == 0x00
    assert await bus.recv() == 0x77, "a byte of the abandoned packet arrived"
    await drv
    await ClockCycles(dut.clk, 6 * 20 + 40)
    data2 = [0x5A, 0x3C]
    cocotb.start_soon(bus.drive_levels(can_stuff([(b >> i) & 1 for b in data2 for i in range(8)]), 20, pin=1))
    assert [await bus.recv() for _ in range(2)] == data2
