/*
 * Copyright (c) 2026 Pankaj Nair
 * SPDX-License-Identifier: Apache-2.0
 *
 * Shared DELAY/WAIT countdown counter (docs/isa.md, both opcodes'
 * rows) -- DELAY and WAIT never execute simultaneously on a
 * single-issue sequencer, so one counter serves both. Reloads on entry
 * to EXECUTE with `mantissa << (exponent*5)` (isa_defs.v
 * `TIMEOUT_SHIFT`); a stale leftover count never matters since every
 * use reloads fresh. Needs 24 bits: DELAY's max reach is
 * exponent=3,mantissa=511 -> ~16.7M cycles (~335ms @ 50MHz,
 * docs/isa.md's DELAY row) -- 2^24 = ~16.8M, just clears it.
 *
 * For WAIT specifically: any encoding whose computed value is 0 (not
 * just the literal exponent=0,mantissa=0 case) means unbounded -- the
 * counter's expiry output should never fire in that case, not fire
 * immediately.
 *
 * Nested under core.v (not a top-level sibling) -- tightly coupled to
 * EXECUTE's DELAY/WAIT handling, narrow interface, no reason to expose
 * further.
 *
 * Deliberately no dec_en: free-running once loaded (decrements every
 * cycle, holds at 0 once reached, until the next load) -- simpler than
 * gating decrement to "core.v is in EXECUTE on a DELAY/WAIT" and
 * harmless, since the header comment above already establishes every
 * use reloads fresh; a residual count ticking down in the background
 * between uses is never read.
 *
 * The "0 means unbounded" WAIT rule lives in core.v, not here: this
 * counter has no concept of unbounded, it just counts down and reports
 * `expired = (count == 0)`. If loaded with 0, `expired` is already true
 * on the very cycle `load` is asserted (see below) -- correct for DELAY
 * (0 cycles = no extra wait), wrong for an unbounded WAIT, so core.v
 * must check the computed mantissa itself before deciding whether to
 * ever load an unbounded WAIT's timeout into this counter at all, or
 * consult `expired` for one.
 *
 * `expired` looks at the about-to-be-loaded value during a `load`
 * cycle, not the stale registered count -- `count` itself only updates
 * on the following clock edge, so a naive `count == 0` would miss a
 * same-cycle `DELAY 0` (mantissa 0) for exactly one cycle, silently
 * turning a 3-cycle instruction into 4 and breaking the uniform
 * 3-cycles-per-instruction property (docs/architecture.md Pipeline).
 * Found wiring DELAY's exit condition in core.v, not caught at the
 * doc-level audit -- an RTL-timing-specific bug, not a spec gap.
 */

`default_nettype none

// Relies on src/cpu/isa_defs.v's `TIMEOUT_SHIFT` already being visible
// -- no `include` here, see core.v's header for why.

module cycle_counter (
    input  wire        clk,
    input  wire        rst_n,

    input  wire        load,       // pulse: load mantissa << (exponent*TIMEOUT_SHIFT) this cycle
    input  wire [8:0]  mantissa,   // DELAY uses the full 9 bits; WAIT zero-extends its 5-bit field
    input  wire [1:0]  exponent,

    output wire         expired    // level: count == 0
);

  reg [23:0] count;

  wire [23:0] shifted = {15'd0, mantissa} << (exponent * `TIMEOUT_SHIFT);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      count <= 24'd0;
    end else if (load) begin
      count <= shifted;
    end else if (count != 24'd0) begin
      count <= count - 24'd1;
    end
  end

  assign expired = load ? (shifted == 24'd0) : (count == 24'd0);

endmodule
