/*
 * Copyright (c) 2026 Pankaj Nair
 * SPDX-License-Identifier: Apache-2.0
 *
 * Unified program+data SRAM interface. Renamed from prog_rom.v -- that
 * name was wrong once the memory split (docs/isa.md "Data memory") and
 * the LOAD-mode host boot stream (docs/architecture.md Pipeline) got
 * locked: this isn't a synthesized, compile-time-preloaded ROM, it's a
 * single 1024-byte SRAM macro split 512/512 between program (bytes
 * 0-511, 256 words) and data (bytes 512-1023), loaded by the host at
 * runtime, not by $readmemh at synthesis time.
 *
 * Wraps the real hard macro (RM_IHPSG13_1P_1024x8_c2_bm_bist -- see
 * macro/ in the reverted SRAM flow-validation commits, git history) for
 * tapeout, or a behavioral model for FPGA/sim builds -- sibling to
 * core.v (top.v's hierarchy), not nested under it, specifically so that
 * swap doesn't touch core.v: the interface (addr/we/wdata/rdata) stays
 * identical either way.
 *
 * Port is deliberately dumb: core.v does all 4 address-source /
 * 2 write-data-source muxing described above and presents one already-
 * resolved addr/we/wdata per cycle -- mem.v just reads/writes it,
 * matching the macro's own 1-cycle synchronous-read latency.
 *
 * TODO: implement the real body (wrap the hard macro for tapeout, or a
 * behavioral model for sim/FPGA). Stubbed for now -- correct port list,
 * always-0 rdata -- so core.v can be instantiated and lint-checked
 * against it while core.v itself is being built out.
 * Not included in source_files yet -- not wired into top.v.
 */

`default_nettype none

module mem (
    input  wire        clk,
    input  wire        rst_n,

    input  wire [9:0]  addr,   // byte address, full 0-1023 range
    input  wire        we,
    input  wire [7:0]  wdata,
    output wire [7:0]  rdata   // registered, matches the macro's 1-cycle latency
);

  // TODO: real macro/behavioral-model body. Stub keeps this lint-clean.
  assign rdata = 8'd0;

  wire _unused = &{clk, rst_n, addr, we, wdata, 1'b0};

endmodule
