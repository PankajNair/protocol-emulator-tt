/*
 * Copyright (c) 2026 Pankaj Nair
 * SPDX-License-Identifier: Apache-2.0
 *
 * ISA encoding for the protocol-emulator CPU. Not yet designed.
 *
 * Open decisions (fill in as they're made):
 *   - word width (8-bit likely, to match ui_in/uo_out)
 *   - opcode width / instruction count needed for: pin read, pin write,
 *     pin direction set, wait-N-cycles, wait-until-pin, branch/jump,
 *     compare, loop counter dec/branch (for bit-banging protocol loops)
 *   - addressing mode for prog_rom (flat vs banked, given tile budget)
 *   - whether ui_in/uio bits map 1:1 to instruction pin-select field or
 *     go through a mux
 *
 * Not included in source_files yet -- not wired into top.v.
 */

`default_nettype none

// placeholder -- opcodes TBD
`define OP_NOP   4'h0
`define OP_PIN_RD 4'h1
`define OP_PIN_WR 4'h2
`define OP_WAIT  4'h3
`define OP_BRANCH 4'h4
