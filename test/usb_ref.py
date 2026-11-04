# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""USB low-speed packet encoding and decoding, written from the USB 2.0
specification (chapter 7.1 and 8), not from the chip's serial engine:
the reference the serial-engine tests and the USB host model check the
chip against.

Deliberately the textbook formulation, unlike the engine's:
- CRCs are the reflected, byte-wise CRC-5/USB and CRC-16/USB of the
  CRC catalogues (checked against their published check values below),
  where the engine runs a bit-serial LFSR;
- bit stuffing is "a 0 after six consecutive 1s" in the data, before
  NRZI, where the engine stuffs on line levels.

Line symbols: "J", "K", "SE0". Low-speed J is D+ low, D- high.
"""

PID = {"OUT": 0xE1, "IN": 0x69, "SOF": 0xA5, "SETUP": 0x2D,
       "DATA0": 0xC3, "DATA1": 0x4B, "ACK": 0xD2, "NAK": 0x5A, "STALL": 0x1E}
PID_NAME = {v: k for k, v in PID.items()}
SYNC = 0x80
LEVELS = {"J": (0, 1), "K": (1, 0), "SE0": (0, 0)}   # (D+, D-)


def crc16(data: bytes) -> int:
    """CRC-16/USB: poly 0x8005 reflected (0xA001), init 0xFFFF, xorout 0xFFFF.
    Appended to a data packet little-endian."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def crc5_bits(bits) -> int:
    """CRC-5/USB over a bit sequence in transmission order: poly 0x05
    reflected (0x14), init 0x1F, xorout 0x1F. Sent LSB first."""
    crc = 0x1F
    for b in bits:
        crc = (crc >> 1) ^ 0x14 if (crc ^ b) & 1 else crc >> 1
    return crc ^ 0x1F


def _check_catalogue():
    msg = b"123456789"
    assert crc16(msg) == 0xB4C8, hex(crc16(msg))
    bits = [(c >> i) & 1 for c in msg for i in range(8)]
    assert crc5_bits(bits) == 0x19, hex(crc5_bits(bits))


_check_catalogue()


def token(pid_name: str, addr: int, endp: int) -> bytes:
    """PID + 11 bits (addr LSB first, then endp) + CRC5, as 3 bytes."""
    field = [(addr >> i) & 1 for i in range(7)] + [(endp >> i) & 1 for i in range(4)]
    c = crc5_bits(field)
    bits = field + [(c >> i) & 1 for i in range(5)]
    v = sum(b << i for i, b in enumerate(bits))
    return bytes([PID[pid_name], v & 0xFF, v >> 8])


def data_packet(pid_name: str, payload: bytes) -> bytes:
    c = crc16(payload)
    return bytes([PID[pid_name]]) + bytes(payload) + bytes([c & 0xFF, c >> 8])


def handshake(pid_name: str) -> bytes:
    return bytes([PID[pid_name]])


def encode(packet: bytes) -> list[str]:
    """SYNC + packet bytes -> line symbols, including the EOP (SE0, SE0, J)."""
    bits = [(b >> i) & 1 for b in bytes([SYNC]) + packet for i in range(8)]
    stuffed, ones = [], 0
    for b in bits:
        stuffed.append(b)
        ones = ones + 1 if b else 0
        if ones == 6:
            stuffed.append(0)
            ones = 0
    sym, level = [], "J"
    for b in stuffed:
        if b == 0:
            level = "K" if level == "J" else "J"
        sym.append(level)
    return sym + ["SE0", "SE0", "J"]


class DecodeError(Exception):
    pass


def decode(symbols: list[str]) -> bytes:
    """Line symbols from SYNC's first bit up to the EOP -> packet bytes
    (SYNC stripped). Checks SYNC, stuffing, byte alignment and the EOP."""
    if "SE0" not in symbols:
        raise DecodeError("no EOP")
    end = symbols.index("SE0")
    eop = symbols[end:end + 3]
    if eop[:2] != ["SE0", "SE0"] or (len(eop) > 2 and eop[2] != "J"):
        raise DecodeError(f"bad EOP {eop}")
    bits, level, ones, i = [], "J", 0, 0
    body = symbols[:end]
    while i < len(body):
        s = body[i]
        b = 1 if s == level else 0
        level = s
        if ones == 6:
            if b != 0:
                raise DecodeError(f"stuff error at symbol {i}")
            ones = 0
            i += 1
            continue
        bits.append(b)
        ones = ones + 1 if b else 0
        i += 1
    if len(bits) % 8:
        raise DecodeError(f"{len(bits)} bits, not whole bytes")
    by = bytes(sum(bits[k + i] << i for i in range(8)) for k in range(0, len(bits), 8))
    if not by or by[0] != SYNC:
        raise DecodeError(f"bad SYNC {by[:1].hex()}")
    return by[1:]


def check_pid(b: int) -> bool:
    return (b & 0xF) == (~b >> 4) & 0xF


def check_data(packet: bytes) -> bool:
    """A data packet's CRC16 over the payload matches."""
    payload, c = packet[1:-2], packet[-2] | packet[-1] << 8
    return crc16(payload) == c


def check_token(packet: bytes) -> bool:
    v = packet[1] | packet[2] << 8
    bits = [(v >> i) & 1 for i in range(16)]
    return crc5_bits(bits[:11]) == sum(b << i for i, b in enumerate(bits[11:]))
