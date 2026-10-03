# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for firmware/isa/asm.py (plain pytest, no simulator).

Every mnemonic is checked against test/isa_asm.py's encoder -- the
encoder the whole testbench already trusts -- so the assembler can't
drift from the bit positions the RTL tests were written against. Plus
the assembler's own logic: labels, .equ, -D overrides, DELAY/WAIT cycle
encoding, data region, and the errors it must raise.
"""

import sys
from pathlib import Path

import pytest

import isa_asm as A

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def one(line, **defs):
    return asm.assemble(line, defs).words[0]


@pytest.mark.parametrize("src,expected", [
    ("NOP", A.nop()), ("HALT", A.halt()), ("RET", A.ret()),
    ("LDI R2, 0xA5", A.ldi(2, 0xA5)),
    ("SET 3, 1", A.set_pin(A.SET_MODE_LEAVE, 3, 1)),
    ("SET 0, pp, 1", A.set_pin(A.SET_MODE_PUSH_PULL, 0, 1)),
    ("SET SDA, 10, 1", A.set_pin(A.SET_MODE_OPEN_DRAIN, 0, 1)),   # isa.md's own example form
    ("SET 7, in, 0", A.set_pin(A.SET_MODE_INPUT, 7, 0)),
    ("SET HOST_STATUS, 1", A.set_pin(A.SET_MODE_LEAVE, 5, 1)),
    ("OUT R1", A.out(1)), ("IN R3", A.in_(3)),
    ("SHIFT R0, L", A.shift(0, A.SHIFT_LEFT)), ("SHIFT R0, 1", A.shift(0, A.SHIFT_RIGHT)),
    ("DELAY 422", A.delay(422, 0)), ("DELAY 27, 1", A.delay(27, 1)),
    ("WAIT HOST_GO, 1", A.wait_(4, 1)), ("WAIT 1, 0, 20", A.wait_(1, 0, mantissa=20, exponent=0)),
    ("TEST R2, 7", A.testbit(2, 7)),
    ("OUTB R0, 0, 7", A.outb(0, 0, A.BITSEL_BIT7)), ("INB R1, 7, 0", A.inb(1, 7, A.BITSEL_BIT0)),
    ("LOAD R1, 300", A.load(1, 300)), ("STORE R2, 5", A.store(2, 5)),
    ("CMP R0, R3", A.cmp_(0, 3)), ("LOADX R1, R2", A.loadx(1, 2)),
])
def test_encodings_match_isa_asm(src, expected):
    defs = {"SDA": 0}
    assert one(src, **defs) == expected


def test_labels_branches_loop():
    p = asm.assemble("""
        start: LDI R1, 3
        top:   NOP
               LOOP top, R1
               BEQ done
               JMP start
               CALL sub
        done:  HALT
        sub:   RET
    """)
    assert p.labels == {"START": 0, "TOP": 1, "DONE": 6, "SUB": 7}
    assert p.words[2] == A.loop_(1, 1)
    assert p.words[3] == A.beq(6)
    assert p.words[4] == A.jmp(0)
    assert p.words[5] == A.call(7)


def test_equ_expressions_and_define_override():
    src = ".equ BAUD, 434\n.equ N, BAUD - 12\nDELAY N"
    assert asm.assemble(src).words[0] == A.delay(422, 0)
    assert asm.assemble(src, {"BAUD": 5208}).words[0] == A.delay(162, 1)  # 5196 -> 5184


def test_delay_nearest_encoding_and_warning():
    p = asm.assemble("DELAY 856")
    assert p.words[0] == A.delay(27, 1)  # 864, docs/protocol_timing.md 57600 row
    assert p.warnings and "+8 cycles" in p.warnings[0]
    assert not asm.assemble("DELAY 2592").warnings  # 81 << 5, exact


def test_wait_timeout_encoding():
    assert one("WAIT 0, 1, 31") == A.wait_(0, 1, mantissa=31, exponent=0)
    assert one("WAIT 0, 1, 64") == A.wait_(0, 1, mantissa=2, exponent=1)


def test_data_region_image():
    p = asm.assemble('HALT\n.data\n.byte 1, 2\n.ascii "Hi"')
    img = p.image()
    assert img[:2] == A.to_bytes([A.halt()])
    assert img[512:516] == [1, 2, ord("H"), ord("i")]


@pytest.mark.parametrize("src,msg", [
    ("FOO R1", "unknown mnemonic"),
    ("LDI R4, 1", "register"),
    ("LDI R0, 256", "out of range"),
    ("JMP nowhere", "unknown label"),
    ("OUTB R0, 0, 3", "0 or 7"),
    ("SET 8, 1", "pin 8"),
    ("WAIT 0, 1, 0", "timeout must be > 0"),
    ("LOAD R0, 512", "out of range"),
    ("a: NOP\na: NOP", "duplicate"),
    ("SET 0, xx, 1", "mode"),
])
def test_errors(src, msg):
    with pytest.raises(asm.AsmError, match=msg):
        asm.assemble(src)


def test_program_too_long():
    with pytest.raises(asm.AsmError, match="program region"):
        asm.assemble("NOP\n" * 257)


@pytest.mark.parametrize("name", ["uart.asm", "uart_rx.asm", "uart_selftest.asm"])
def test_shipped_firmware_assembles(name):
    """The shipped protocol firmware must always assemble cleanly at its
    default baud (115200 -- exact, no warnings)."""
    p = asm.assemble_file(ROOT / "firmware" / "protocols" / name)
    assert not p.warnings
    assert len(p.words) <= asm.PROGRAM_WORDS
