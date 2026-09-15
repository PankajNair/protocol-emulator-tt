/*
 * Copyright (c) 2026 Pankaj Nair
 * SPDX-License-Identifier: Apache-2.0
 *
 * Fetch/decode/execute core. TODO: implement once isa_defs.v is finalized.
 * Not included in source_files yet -- not wired into top.v.
 */

`default_nettype none

module protocol_cpu_core (
    input  wire       clk,
    input  wire       rst_n,
    // TODO: prog_rom read port, regfile port, pin I/O bus
    input  wire [7:0] pins_in,
    output wire [7:0] pins_out,
    output wire [7:0] pins_oe
);

  // TODO

endmodule
