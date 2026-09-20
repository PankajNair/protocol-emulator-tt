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
 * Body below is a behavioral model (plain 1024x8 array, synchronous
 * write, registered read, write-through on a same-cycle read-after-
 * write) -- matches the real macro's actual timing exactly, not just
 * its port list, so the swap-in is drop-in. Recovered from the
 * reverted SRAM flow-validation run (git history, commit `2e113b4`,
 * `test/models/RM_IHPSG13_1P_core_behavioral_bm_bist.v`) rather than
 * guessed: `A_MEN`/`A_WEN`/`A_REN` are active-high (not the traditional
 * active-low a "WEN" name suggests -- confirmed from that model's own
 * `MEN_MUX==1'b1 && WEN_MUX==1'b1` write condition), and `dr_r`
 * (A_DOUT's register) has no reset -- real SRAM content and its output
 * latch are both undefined until first written/read, which this model
 * matches on purpose rather than inventing a reset-to-0 that the real
 * macro doesn't have (consistent with "volatile, host reloads every
 * power-cycle" already being the documented design rationale).
 *
 * TODO for the actual tapeout swap: instantiate
 * `RM_IHPSG13_1P_1024x8_c2_bm_bist` here instead of the array below,
 * tied off exactly as validated in that same flow-validation run:
 * `A_CLK(clk), A_MEN(rst_n), A_WEN(we), A_REN(1'b1), A_ADDR(addr),
 * A_DIN(wdata), A_DLY(1'b1), A_DOUT(rdata), A_BM(8'hFF)`, all
 * `A_BIST_*` tied to 0 -- full byte writes only, no sub-byte masking
 * needed anywhere in this ISA. Deferred until the real gate-level/GDS
 * flow is being re-run for the actual design, not RTL-sim/lint work.
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

  reg [7:0] storage [0:1023];
  reg [7:0] rdata_r;

  always @(posedge clk) begin
    if (we) begin
      storage[addr] <= wdata;
      rdata_r       <= wdata;    // write-through, matches the real macro's REN-during-write behavior
    end else begin
      rdata_r <= storage[addr];
    end
  end

  assign rdata = rdata_r;

  wire _unused = &{rst_n, 1'b0};

endmodule
