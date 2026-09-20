/*
 * Copyright (c) 2026 Pankaj Nair
 * SPDX-License-Identifier: Apache-2.0
 *
 * ISA encoding for the protocol-emulator sequencer. Synced to the
 * locked spec in docs/isa.md -- that file is the source of truth for
 * *why* each of these exists; this file is where the semantics (field
 * widths, opcode meaning, cond/mode values) become concrete bit
 * positions for core.v to slice.
 *
 * Two kinds of thing live here:
 *   1. Straight from docs/isa.md, no discretion involved: word width,
 *      opcode field position, the 19 opcode values, format field
 *      widths, BRANCH's cond values, SET's mode values.
 *   2. Bit-packing choices docs/isa.md deliberately left open (it locks
 *      field *widths* and *semantics*, not exact bit positions within
 *      a field -- that's an assembler/decoder-level detail). Where this
 *      file makes such a choice, it's marked below and applied
 *      consistently across every opcode that shares the same kind of
 *      sub-field, so `core.v` can share one piece of decode logic
 *      instead of one per opcode.
 */

`default_nettype none

// ---------------------------------------------------------------------
// Instruction word: 16 bits, opcode is the top 5 bits (word[15:11]).
// MSB-first field order throughout, matching every format header in
// docs/isa.md ("opcode(5) | addr(9) | cond(2)" means opcode occupies
// the *top* 5 bits, fields pack down from there).
// ---------------------------------------------------------------------
`define OPCODE_HI  15
`define OPCODE_LO  11
`define OPCODE_W   word[`OPCODE_HI:`OPCODE_LO]

// ---------------------------------------------------------------------
// Opcode values (docs/isa.md Opcode table, `value` column). Grouped by
// format for human readability -- hardware cost is identical for any
// assignment, a 5-bit case statement doesn't care about grouping.
// NOP=0 is deliberate: makes the all-zero 16-bit word (and any word
// with just the top 5 bits zero) a valid NOP by construction, not a
// special case -- consistent with undefined opcodes already executing
// as NOP (Undefined opcode behavior, below).
// ---------------------------------------------------------------------

// 0-operand (word[10:0] unused/don't-care)
`define OP_NOP    5'd0
`define OP_HALT   5'd1
`define OP_RET    5'd2

// Branch format: opcode(5) | addr(9) | cond(2)
`define OP_BRANCH 5'd3

// Reg+immediate format: opcode(5) | Rd(2) | imm(9)
`define OP_LDI    5'd4
`define OP_SET    5'd5
`define OP_OUT    5'd6
`define OP_IN     5'd7
`define OP_SHIFT  5'd8
`define OP_DELAY  5'd9
`define OP_WAIT   5'd10
`define OP_TESTBIT 5'd11
`define OP_OUTB   5'd12
`define OP_INB    5'd13
`define OP_LOAD   5'd14
`define OP_STORE  5'd15

// addr+Rd format: opcode(5) | addr(9) | Rd(2)
`define OP_LOOP   5'd16

// Reg-reg format: opcode(5) | Rd(2) | Rs(2) | reserved(7)
`define OP_CMP    5'd17
`define OP_LOADX  5'd18

// 5'd19-5'd31: reserved/unimplemented. Decode as OP_NOP (see
// "Undefined opcode behavior" below) -- never add a case for these,
// let them fall through to the illegal-opcode default.

// ---------------------------------------------------------------------
// Branch format field positions and cond values.
// word[10:2] = addr(9), word[1:0] = cond(2).
// LOOP (addr+Rd) shares the same addr position -- word[10:2] = addr(9)
// -- with word[1:0] = Rd(2) instead of cond; decoder can share the
// addr-extract slice between BRANCH and LOOP (docs/isa.md).
// ---------------------------------------------------------------------
`define ADDR_HI    10
`define ADDR_LO    2
`define ADDR_W     word[`ADDR_HI:`ADDR_LO]

`define COND_HI    1
`define COND_LO    0
`define COND_W     word[`COND_HI:`COND_LO]

`define COND_JMP   2'b00  // always
`define COND_BEQ   2'b01  // if flag set
`define COND_BNE   2'b10  // if flag clear
`define COND_CALL  2'b11  // push return address, then jump

// ---------------------------------------------------------------------
// Reg+immediate format field positions.
// word[10:9] = Rd(2), word[8:0] = imm(9).
// DELAY and WAIT repurpose Rd(2) as an exponent instead of a register
// select -- same bit position, different meaning, see their sections
// below.
// ---------------------------------------------------------------------
`define RD_HI      10
`define RD_LO      9
`define RD_W       word[`RD_HI:`RD_LO]

`define IMM_HI     8
`define IMM_LO     0
`define IMM_W      word[`IMM_HI:`IMM_LO]

// LDI: imm(9) = load value(8) + unused(1). Value in the low 8 bits.
`define LDI_VALUE_HI 7
`define LDI_VALUE_LO 0
`define LDI_VALUE_W  word[`LDI_VALUE_HI:`LDI_VALUE_LO]

// ---------------------------------------------------------------------
// Pin-index sub-field, shared bit-packing chosen here (docs/isa.md
// locks the field *widths* -- pin_index(3) + a 1-bit value/level/
// bit_select -- not their position within imm(9); this file picks one
// packing and uses it for all four "pin-touching" reg+imm opcodes --
// SET, WAIT, OUTB, INB -- so core.v can share one pin_index/aux-bit
// extraction across all of them instead of four different slices).
//
//   imm[8:6] = pin_index(3)   -- which of the 5 protocol pins,
//                                 or a fixed pin_index 4/5/6 role
//                                 (architecture.md Pin map)
//   imm[5]   = value/level/bit_select(1), meaning is opcode-specific:
//                - SET:  value      (1=release/high, 0=drive low)
//                - WAIT: level      (block until pin == this)
//                - OUTB: bit_select (which bit of Rd to drive from)
//                - INB:  bit_select (which bit of Rd to capture into)
//   imm[4:0] = opcode-specific:
//                - SET/OUTB/INB: reserved, must be 0
//                - WAIT:         timeout_mantissa(5) -- see below
// ---------------------------------------------------------------------
`define PIN_INDEX_HI 8
`define PIN_INDEX_LO 6
`define PIN_INDEX_W  word[`PIN_INDEX_HI:`PIN_INDEX_LO]

`define PIN_AUX_BIT  5   // value / level / bit_select -- word[PIN_AUX_BIT]

// SET's Rd(2) sticky per-pin drive-mode (docs/isa.md SET row).
`define SET_MODE_LEAVE      2'b00
`define SET_MODE_PUSH_PULL  2'b01
`define SET_MODE_OPEN_DRAIN 2'b10
`define SET_MODE_INPUT      2'b11

// TEST-bit: imm(9) = bit_index(3) + reserved(6). Low 3 bits, distinct
// from the pin_index packing above since TEST-bit tests a bit of a
// *register*, not a pin -- no pin_index involved, so no reason to
// share that sub-field's position.
`define BITIDX_HI  2
`define BITIDX_LO  0
`define BITIDX_W   word[`BITIDX_HI:`BITIDX_LO]

// ---------------------------------------------------------------------
// DELAY / WAIT: Rd(2) repurposed as exponent, imm(9) as mantissa
// (DELAY) or pin_index+level+timeout_mantissa (WAIT). Both compute
// cycles = mantissa << (exponent*5) and share the same hardware
// countdown counter (docs/isa.md -- DELAY and WAIT never execute
// simultaneously on a single-issue sequencer).
//
// DELAY: mantissa = imm(9) in full, no sub-packing -- DELAY doesn't
// touch a pin, so the pin_index/aux-bit split above doesn't apply.
//
// WAIT: mantissa is only 5 bits (imm[4:0], see pin_index packing
// above) since imm[8:6]/imm[5] are spent on pin_index/level. Reach is
// correspondingly smaller than DELAY's -- see docs/isa.md's WAIT row
// for the worked cycle-count table.
// ---------------------------------------------------------------------
`define EXPONENT_W   `RD_W
`define DELAY_MANTISSA_W `IMM_W
`define WAIT_MANTISSA_HI 4
`define WAIT_MANTISSA_LO 0
`define WAIT_MANTISSA_W  word[`WAIT_MANTISSA_HI:`WAIT_MANTISSA_LO]
`define TIMEOUT_SHIFT     5   // cycles = mantissa << (exponent * TIMEOUT_SHIFT)

// ---------------------------------------------------------------------
// addr+Rd format (LOOP): word[10:2] = addr(9) (shares ADDR_* above),
// word[1:0] = Rd(2) -- the decrement/counter register, not a cond.
// ---------------------------------------------------------------------
`define LOOP_RD_HI 1
`define LOOP_RD_LO 0
`define LOOP_RD_W  word[`LOOP_RD_HI:`LOOP_RD_LO]

// ---------------------------------------------------------------------
// Reg-reg format: opcode(5) | Rd(2) | Rs(2) | reserved(7).
// word[10:9] = Rd(2) (shares RD_* above), word[8:7] = Rs(2),
// word[6:0] = reserved(7), must be 0.
// ---------------------------------------------------------------------
`define RS_HI 8
`define RS_LO 7
`define RS_W  word[`RS_HI:`RS_LO]

// ---------------------------------------------------------------------
// Shared flag register: written by CMP, TEST-bit, WAIT only (docs/isa.md
// Branch format section -- keeping this set small and named, not "any
// instruction", is what keeps the flag-clobber hazard CALL/WAIT-shaped
// instead of general). Single bit, last-write-wins.
//
// TEST-bit polarity: flag = 1 iff the tested bit == 1.
// CMP: flag = (Rd == Rs), equality only.
// WAIT: flag = 0 if the pin condition was met, 1 if it timed out --
// every WAIT writes this, uniformly, including an unbounded one
// (always writes 0 there, since there's no timeout mechanism active
// to ever produce a 1).
// ---------------------------------------------------------------------

// ---------------------------------------------------------------------
// Data memory: 1024-byte SRAM split 512/512 (docs/isa.md "Data
// memory"). Program is bytes 0-511 (256 words); data is bytes
// 512-1023, reached via LOAD/STORE (address = 512 + imm) and LOADX
// (address = 512 + Rs, register-indexed, reaches the first 256 bytes
// of the data region only -- Rs is 8 bits).
// ---------------------------------------------------------------------
`define DATA_BASE 10'd512

// ---------------------------------------------------------------------
// Undefined opcode behavior (docs/isa.md): any of the 13 unassigned
// 5-bit values (19-31) decodes as OP_NOP and sets the sticky
// illegal-opcode flag (formal/debug-visibility only, not
// firmware-readable). core.v's opcode case statement should have an
// explicit `default` arm that does exactly this -- not rely on
// synthesis inferring it, and not add cases for 19-31.
// ---------------------------------------------------------------------
