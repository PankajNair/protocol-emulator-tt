/*
 * Copyright (c) 2026 Pankaj Nair
 * SPDX-License-Identifier: Apache-2.0
 *
 * 4x 8-bit registers (R0-R3), locked in docs/isa.md's Register file
 * section. Plain 2-read/1-write: synchronous write, combinational
 * read, no forwarding needed -- checked against every opcode format,
 * nothing ever needs 2 reads + 1 write simultaneously (CMP is the only
 * 2-different-register read, 0 writes; LOOP/INB/SHIFT read-modify-write
 * the *same* register, which this port shape handles with no hazard).
 * Reset: all four registers to 0.
 *
 * Nested under core.v (not a top-level sibling) -- tightly coupled to
 * EXECUTE's datapath, narrow interface, no reason to expose further.
 */

`default_nettype none

module regfile (
    input  wire       clk,
    input  wire       rst_n,

    input  wire [1:0] ra_addr,
    output wire [7:0] ra_data,

    input  wire [1:0] rb_addr,
    output wire [7:0] rb_data,

    input  wire [1:0] rw_addr,
    input  wire [7:0] rw_data,
    input  wire       rw_en
);

  reg [7:0] regs [0:3];
  integer   i;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (i = 0; i < 4; i = i + 1) regs[i] <= 8'd0;
    end else if (rw_en) begin
      regs[rw_addr] <= rw_data;
    end
  end

  assign ra_data = regs[ra_addr];
  assign rb_data = regs[rb_addr];

endmodule
