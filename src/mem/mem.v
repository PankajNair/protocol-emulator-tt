/*
 * Copyright (c) 2026 Pankaj Nair
 * SPDX-License-Identifier: Apache-2.0
 *
 * Unified program+data SRAM: one IHP RM_IHPSG13_1P_1024x8_c2_bm_bist hard
 * macro (1024x8, single-port), split 512/512 between program (bytes
 * 0-511, 256 words) and data (bytes 512-1023) -- docs/isa.md "Data
 * memory". Loaded by the host at runtime through the LOAD-mode boot
 * stream (docs/architecture.md Pipeline), not preloaded at synthesis.
 *
 * Sibling to core.v under top.v. Port is deliberately dumb: core.v does
 * all address/write-data muxing and presents one resolved addr/we/wdata
 * per cycle; reads have the macro's 1-cycle synchronous latency.
 *
 * Tie-offs, as validated in the macro flow-validation run (commit
 * 2e113b4, recipe from urish/ttihp-sram-test):
 *   - A_MEN = rst_n: memory enabled whenever out of reset.
 *   - A_REN = 1: a read every cycle; during a write the macro returns
 *     the written data (write-through), which core.v relies on nowhere
 *     but matches.
 *   - A_BM = 8'hFF: full-byte writes only (no sub-byte ops in the ISA).
 *   - A_DLY = 1, all A_BIST_* = 0 (BIST unused).
 * Enables are active-high (MEN/WEN/REN), despite the WEN name.
 *
 * Contents and A_DOUT are undefined until written -- real SRAM has no
 * reset, the host reloads it every power cycle.
 *
 * Synthesis: src/RM_IHPSG13_1P_1024x8_c2_bm_bist.v is a port-only stub;
 * placement/LEF/GDS/LIB come from src/config.json MACROS. Simulation:
 * test/Makefile uses IHP's own behavioral model (test/models/, compiled
 * with -DFUNCTIONAL), so every RTL test runs against the vendor's model
 * of the macro rather than a hand-written stand-in.
 */

`default_nettype none

module mem (
    input  wire        clk,
    input  wire        rst_n,

    input  wire [9:0]  addr,   // byte address, full 0-1023 range
    input  wire        we,
    input  wire [7:0]  wdata,
    output wire [7:0]  rdata   // registered by the macro: 1-cycle read latency
);

  RM_IHPSG13_1P_1024x8_c2_bm_bist sram (
      .A_CLK      (clk),
      .A_MEN      (rst_n),
      .A_WEN      (we),
      .A_REN      (1'b1),
      .A_ADDR     (addr),
      .A_DIN      (wdata),
      .A_DLY      (1'b1),
      .A_DOUT     (rdata),
      .A_BM       (8'hFF),
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(10'd0),
      .A_BIST_DIN (8'd0),
      .A_BIST_BM  (8'd0)
  );

endmodule
