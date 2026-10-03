#!/usr/bin/env python3
# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Assembler for the protocol-emulator sequencer ISA (docs/isa.md).

Turns a .asm program into the LOAD-mode boot image the host streams into
the SRAM (docs/architecture.md Pipeline): program words at bytes 0-511
(low byte first), optional data at 512-1023. Encoding is delegated to
test/isa_asm.py, the single encoder every test already uses, so the
assembler and the testbench can't disagree about a bit position.

Syntax (follows docs/isa.md's own examples):

    ; comment                     # also accepted
    .equ NAME, expr               constant (expr: ints, names, + - * / // ( ))
    label:                        word address of the next instruction
    .data                         following .byte/.ascii go to the data region
    .byte 1, 0x2A, NAME           .ascii "text"

    NOP | HALT | RET
    JMP label | BEQ label | BNE label | CALL label
    LDI Rd, imm8
    SET pin, value                drive value under the pin's current mode
    SET pin, mode, value          mode: pp | od | in | leave | 00 01 10 11
    OUT Rd | IN Rd
    SHIFT Rd, dir                 dir: L | R | 0 (left) | 1 (right)
    DELAY cycles                  N extra cycles; nearest encodable value is
                                  used and a warning printed if not exact
    DELAY mantissa, exponent      explicit encoding (cycles = m << 5e)
    WAIT pin, level[, timeout]    timeout in cycles (nearest encodable,
                                  must be > 0); omitted = unbounded
    TEST Rd, bit                  (TEST-bit in isa.md)
    OUTB Rd, pin, bit             bit: 0 or 7
    INB  Rd, pin, bit
    LOAD Rd, addr | STORE Rd, addr   addr: data-region offset 0-511
    LOOP label, Rd
    CMP Rd, Rs | LOADX Rd, Rs

Pins are numbers 0-7 or names; HOST_GO=4, HOST_STATUS=5, HOST_ERROR=6
are predefined. Mnemonics and register/mode names are case-insensitive.

CLI:
    asm.py prog.asm [-D NAME=value ...] [-o image.hex] [--list]
image.hex: one byte per line, hex, program then (if any) data at 512+.
"""

from __future__ import annotations

import argparse
import ast
import operator
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "test"))
import isa_asm as asm  # noqa: E402

TIMEOUT_SHIFT = 5
PROGRAM_WORDS = 256
DATA_BYTES = 512
PREDEFINED = {"HOST_GO": 4, "HOST_STATUS": 5, "HOST_ERROR": 6}
MODES = {"leave": asm.SET_MODE_LEAVE, "pp": asm.SET_MODE_PUSH_PULL,
         "od": asm.SET_MODE_OPEN_DRAIN, "in": asm.SET_MODE_INPUT,
         "00": 0, "01": 1, "10": 2, "11": 3}


class AsmError(Exception):
    pass


@dataclass
class Program:
    words: list[int]
    data: list[int]
    labels: dict[str, int]
    warnings: list[str] = field(default_factory=list)
    listing: list[str] = field(default_factory=list)

    def image(self) -> list[int]:
        """Boot-stream bytes: program, then the data region if used."""
        img = asm.to_bytes(self.words)
        if self.data:
            img += [0] * (2 * PROGRAM_WORDS - len(img))
            img += self.data
        return img


_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.FloorDiv: operator.floordiv, ast.Div: operator.floordiv,
           ast.LShift: operator.lshift, ast.RShift: operator.rshift}


def _eval(expr: str, names: dict[str, int], where: str) -> int:
    """Integer expression over constants/labels. Restricted AST eval --
    no names outside `names`, no calls."""
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, int):
            return n.value
        if isinstance(n, ast.Name):
            key = n.id.upper()
            if key not in names:
                raise AsmError(f"{where}: undefined name {n.id!r}")
            return names[key]
        if isinstance(n, ast.BinOp) and type(n.op) in _BINOPS:
            return _BINOPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -ev(n.operand)
        raise AsmError(f"{where}: bad expression {expr!r}")
    try:
        return ev(ast.parse(expr.strip(), mode="eval").body)
    except SyntaxError:
        raise AsmError(f"{where}: bad expression {expr!r}") from None


def encode_cycles(cycles: int, mant_bits: int) -> tuple[int, int, int]:
    """(mantissa, exponent, actual_cycles) closest to `cycles` with
    cycles = mantissa << (exponent*5), mantissa < 2**mant_bits. Prefers
    the smallest exponent among equally close encodings (finest grain)."""
    if cycles < 0:
        raise AsmError(f"negative cycle count {cycles}")
    best = None
    for e in range(4):
        unit = 1 << (e * TIMEOUT_SHIFT)
        m = min((cycles + unit // 2) // unit, (1 << mant_bits) - 1)
        actual = m << (e * TIMEOUT_SHIFT)
        if best is None or abs(actual - cycles) < abs(best[2] - cycles):
            best = (m, e, actual)
    return best


def _split_ops(s: str) -> list[str]:
    return [o.strip() for o in s.split(",")] if s.strip() else []


def _reg(tok: str, where: str) -> int:
    m = re.fullmatch(r"[rR]([0-3])", tok)
    if not m:
        raise AsmError(f"{where}: expected register R0-R3, got {tok!r}")
    return int(m.group(1))


def assemble(text: str, defines: dict[str, int] | None = None, name: str = "<asm>") -> Program:
    consts = dict(PREDEFINED)
    for k, v in (defines or {}).items():
        consts[k.upper()] = v
    cli_defined = set(k.upper() for k in (defines or {}))

    # Pass 1: strip, collect labels and .equ, keep instruction lines.
    lines = []  # (lineno, mnemonic, operands, word_index)
    data_lines = []
    in_data = False
    pc = 0
    labels: dict[str, int] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        where = f"{name}:{lineno}"
        s = re.split(r"[;#]", raw, maxsplit=1)[0].strip()
        if not s:
            continue
        while True:
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$", s)
            if not m:
                break
            lab = m.group(1).upper()
            if lab in labels or lab in consts:
                raise AsmError(f"{where}: duplicate name {m.group(1)!r}")
            if in_data:
                raise AsmError(f"{where}: labels in .data not supported")
            labels[lab] = pc
            s = m.group(2).strip()
        if not s:
            continue
        parts = s.split(None, 1)
        mn = parts[0].upper()
        ops = parts[1] if len(parts) > 1 else ""
        if mn == ".EQU":
            o = _split_ops(ops)
            if len(o) != 2:
                raise AsmError(f"{where}: .equ NAME, value")
            key = o[0].upper()
            if key in cli_defined:
                continue  # -D overrides the file's default
            if key in consts or key in labels:
                raise AsmError(f"{where}: duplicate name {o[0]!r}")
            consts[key] = _eval(o[1], consts, where)
            continue
        if mn == ".DATA":
            in_data = True
            continue
        if mn in (".BYTE", ".ASCII"):
            if not in_data:
                raise AsmError(f"{where}: {mn.lower()} only allowed after .data")
            data_lines.append((where, mn, ops))
            continue
        if in_data:
            raise AsmError(f"{where}: instructions not allowed after .data")
        lines.append((where, mn, ops, pc))
        pc += 1
    if pc > PROGRAM_WORDS:
        raise AsmError(f"{name}: {pc} words, program region holds {PROGRAM_WORDS}")

    names = dict(consts)
    for k, v in labels.items():
        names[k] = v
    prog = Program(words=[], data=[], labels=labels)

    def val(tok, where):
        return _eval(tok, names, where)

    def label(tok, where):
        key = tok.strip().upper()
        if key not in labels:
            raise AsmError(f"{where}: unknown label {tok!r}")
        return labels[key]

    def pin(tok, where):
        p = val(tok, where)
        if not 0 <= p <= 7:
            raise AsmError(f"{where}: pin {p} out of range 0-7")
        return p

    def bitsel(tok, where):
        b = val(tok, where)
        if b not in (0, 7):
            raise AsmError(f"{where}: OUTB/INB bit must be 0 or 7, got {b}")
        return asm.BITSEL_BIT7 if b == 7 else asm.BITSEL_BIT0

    def need(o, n, where, form):
        if len(o) not in (n if isinstance(n, tuple) else (n,)):
            raise AsmError(f"{where}: expected {form}")

    # Pass 2: encode.
    for where, mn, ops, wpc in lines:
        o = _split_ops(ops)
        if mn in ("NOP", "HALT", "RET"):
            need(o, 0, where, mn)
            w = {"NOP": asm.nop, "HALT": asm.halt, "RET": asm.ret}[mn]()
        elif mn in ("JMP", "BEQ", "BNE", "CALL"):
            need(o, 1, where, f"{mn} label")
            w = getattr(asm, mn.lower() if mn != "CALL" else "call")(label(o[0], where))
        elif mn == "LDI":
            need(o, 2, where, "LDI Rd, imm8")
            v = val(o[1], where)
            if not 0 <= v <= 255:
                raise AsmError(f"{where}: LDI value {v} out of range 0-255")
            w = asm.ldi(_reg(o[0], where), v)
        elif mn == "SET":
            need(o, (2, 3), where, "SET pin, [mode,] value")
            mode = asm.SET_MODE_LEAVE
            if len(o) == 3:
                mk = o[1].lower()
                if mk not in MODES:
                    raise AsmError(f"{where}: SET mode must be pp/od/in/leave or 00-11, got {o[1]!r}")
                mode = MODES[mk]
            v = val(o[-1], where)
            if v not in (0, 1):
                raise AsmError(f"{where}: SET value must be 0 or 1")
            w = asm.set_pin(mode, pin(o[0], where), v)
        elif mn in ("OUT", "IN"):
            need(o, 1, where, f"{mn} Rd")
            w = (asm.out if mn == "OUT" else asm.in_)(_reg(o[0], where))
        elif mn == "SHIFT":
            need(o, 2, where, "SHIFT Rd, L|R")
            d = o[1].upper()
            if d not in ("L", "R", "0", "1"):
                raise AsmError(f"{where}: SHIFT direction must be L/R/0/1")
            w = asm.shift(_reg(o[0], where), asm.SHIFT_LEFT if d in ("L", "0") else asm.SHIFT_RIGHT)
        elif mn == "DELAY":
            need(o, (1, 2), where, "DELAY cycles | DELAY mantissa, exponent")
            if len(o) == 2:
                m, e = val(o[0], where), val(o[1], where)
                if not (0 <= m < 512 and 0 <= e < 4):
                    raise AsmError(f"{where}: DELAY mantissa 0-511, exponent 0-3")
            else:
                want = val(o[0], where)
                m, e, actual = encode_cycles(want, 9)
                if actual != want:
                    prog.warnings.append(f"{where}: DELAY {want} not encodable, using {actual} "
                                         f"(mantissa {m}, exponent {e}, {actual - want:+d} cycles)")
            w = asm.delay(m, e)
        elif mn == "WAIT":
            need(o, (2, 3), where, "WAIT pin, level[, timeout_cycles]")
            lvl = val(o[1], where)
            if lvl not in (0, 1):
                raise AsmError(f"{where}: WAIT level must be 0 or 1")
            m = e = 0
            if len(o) == 3:
                want = val(o[2], where)
                if want <= 0:
                    raise AsmError(f"{where}: WAIT timeout must be > 0 (omit it for an unbounded wait)")
                m, e, actual = encode_cycles(want, 5)
                if m == 0:
                    raise AsmError(f"{where}: WAIT timeout {want} rounds to 0 (would mean unbounded)")
                if actual != want:
                    prog.warnings.append(f"{where}: WAIT timeout {want} not encodable, using {actual} "
                                         f"(mantissa {m}, exponent {e})")
            w = asm.wait_(pin(o[0], where), lvl, mantissa=m, exponent=e)
        elif mn == "TEST":
            need(o, 2, where, "TEST Rd, bit")
            b = val(o[1], where)
            if not 0 <= b <= 7:
                raise AsmError(f"{where}: TEST bit 0-7")
            w = asm.testbit(_reg(o[0], where), b)
        elif mn in ("OUTB", "INB"):
            need(o, 3, where, f"{mn} Rd, pin, bit")
            w = (asm.outb if mn == "OUTB" else asm.inb)(_reg(o[0], where), pin(o[1], where), bitsel(o[2], where))
        elif mn in ("LOAD", "STORE"):
            need(o, 2, where, f"{mn} Rd, addr")
            a = val(o[1], where)
            if not 0 <= a < DATA_BYTES:
                raise AsmError(f"{where}: data address {a} out of range 0-511")
            w = (asm.load if mn == "LOAD" else asm.store)(_reg(o[0], where), a)
        elif mn == "LOOP":
            need(o, 2, where, "LOOP label, Rd")
            w = asm.loop_(label(o[0], where), _reg(o[1], where))
        elif mn in ("CMP", "LOADX"):
            need(o, 2, where, f"{mn} Rd, Rs")
            w = (asm.cmp_ if mn == "CMP" else asm.loadx)(_reg(o[0], where), _reg(o[1], where))
        else:
            raise AsmError(f"{where}: unknown mnemonic {mn!r}")
        prog.words.append(w)
        prog.listing.append(f"{wpc:3d}: {w:04x}  {mn} {ops}".rstrip())

    for where, mn, ops in data_lines:
        if mn == ".ASCII":
            m = re.fullmatch(r'\s*"(.*)"\s*', ops)
            if not m:
                raise AsmError(f'{where}: .ascii "text"')
            prog.data += list(m.group(1).encode().decode("unicode_escape").encode("latin-1"))
        else:
            for tok in _split_ops(ops):
                b = val(tok, where)
                if not 0 <= b <= 255:
                    raise AsmError(f"{where}: .byte {b} out of range")
                prog.data.append(b)
    if len(prog.data) > DATA_BYTES:
        raise AsmError(f"{name}: {len(prog.data)} data bytes, region holds {DATA_BYTES}")
    return prog


def assemble_file(path: str | Path, defines: dict[str, int] | None = None) -> Program:
    p = Path(path)
    return assemble(p.read_text(), defines, name=p.name)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("-D", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("-o", "--output")
    ap.add_argument("--list", action="store_true", help="print the listing")
    a = ap.parse_args()
    defines = {}
    for d in a.D:
        k, _, v = d.partition("=")
        defines[k] = int(v, 0)
    try:
        prog = assemble_file(a.src, defines)
    except AsmError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for w in prog.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if a.list:
        print("\n".join(prog.listing))
    if a.output:
        Path(a.output).write_text("".join(f"{b:02x}\n" for b in prog.image()))
    print(f"{len(prog.words)} words, {len(prog.data)} data bytes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
