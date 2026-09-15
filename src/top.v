/*
 * Copyright (c) 2026 Pankaj Nair
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// TODO: this is still the template's passthrough example. Replace with
// instantiation of cpu/core.v + io/pin_ctrl.v + mem/prog_rom.v once the
// ISA (src/cpu/isa_defs.v) is settled. Keeping this working (not stubbed
// to zero) so `make -C test` and the GDS action stay green while the
// real design is built out incrementally.
module tt_um_pankajnair_protocol_emulator (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  assign uo_out  = ui_in + uio_in;
  assign uio_out = 0;
  assign uio_oe  = 0;

  wire _unused = &{ena, clk, rst_n, 1'b0};

endmodule
