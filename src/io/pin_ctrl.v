/*
 * Copyright (c) 2026 Pankaj Nair
 * SPDX-License-Identifier: Apache-2.0
 *
 * Electrical/synchronization layer -- everything about physical pin
 * timing lives here, so core.v never has to know about it. Top-level
 * sibling to core.v and mem.v (top.v's hierarchy), owns:
 *
 *   - 2-flop synchronization for every external input, uniformly:
 *     ui_in[7:0] and all 8 uio_in bits (docs/architecture.md -- an
 *     earlier draft only synchronized the host-facing subset, fixed
 *     to cover the protocol pins too, since UART RX/I2C SCL,SDA/SPI
 *     MISO are equally externally-driven).
 *   - Sticky per-pin drive-mode register bank for the 5 protocol pins
 *     (uio[3:0], uio[7]): leave/push-pull/open-drain/input, set via
 *     SET's Rd field (docs/isa.md SET row), reset to input.
 *   - uio_oe/uio_out generation for all 8 uio bits, combining: current
 *     mode (LOAD vs running), the sticky drive-mode state above, and
 *     core.v's pin_index-addressed drive requests.
 *   - uio[6]'s START (LOAD) / HOST_ERROR (running) mode switch,
 *     including the pulse-edge detection and the "seen START fall"
 *     gate that prevents the driver-contention hazard documented in
 *     docs/architecture.md's LOAD section.
 *
 * Interface to core.v: pin_index-addressed drive/read requests (mirrors
 * SET/WAIT/OUTB/INB's pin_index field) plus edge-detected start_rise/
 * start_fall (START, LOAD-mode) and host_go_rise/host_go_fall (HOST_GO,
 * LOAD-mode write-strobe/ack-clear -- same treatment as START, found
 * missing from an earlier draft of this interface while wiring core.v's
 * LOAD-state write path: the write-strobe fires on HOST_GO's rise, the
 * HOST_STATUS ack clears on its fall, so core.v needs both edges, not
 * just the synchronized level). uio_out/HOST_STATUS during LOAD is
 * driven by core.v through the same generic set_en/pin_index=5 path
 * SET uses during running mode -- no dedicated port needed, core.v just
 * mixes that path's source based on FSM state.
 *
 * `pin_index` is exploited directly as the uio bit position throughout
 * this file (pin_index N <-> uio[N], architecture.md Pin map) -- no
 * lookup table, one flat 8-entry array per piece of per-pin state,
 * same "one flat table" property the pin map itself was designed for.
 *
 * Sticky state is a driven-*value* bit per uio pin (`drv[8]`, index =
 * pin_index, meaningful for 0-3/5/6/7 -- 4/HOST_GO is a fixed input,
 * SET/OUTB on it is a no-op by construction since nothing ever reads
 * drv[4]) plus a driven-*mode* 2-bit field per pin (`mode[8]`,
 * meaningful only for the 5 protocol pins 0-3/7 -- pins 4/5/6 have a
 * role-hardwired `oe`, no mode concept). HOST_STATUS/HOST_ERROR (5/6)
 * reuse `drv[]` for their own driven bit even though they have no
 * `mode[]` entry -- oe for those is hardwired/gated by role, not by
 * sticky mode, so only `drv[]` applies.
 *
 * Not included in source_files yet -- not wired into top.v.
 */

`default_nettype none

`include "cpu/isa_defs.v"

module pin_ctrl (
    // Raw TT pins.
    input  wire [7:0] ui_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire [7:0] uio_in,

    input  wire        clk,
    input  wire        rst_n,

    // core.v-facing.
    output wire [7:0]  ui_in_sync,   // synchronized ui_in, for IN

    input  wire [2:0]  pin_index,
    input  wire        set_en,
    input  wire [1:0]  set_mode,     // SET_MODE_* (isa_defs.v), sticky per-pin
    input  wire        set_value,

    input  wire        outb_en,
    input  wire        outb_bit,

    output wire         pin_read,     // synchronized level at current pin_index (WAIT/INB)

    output wire         start_rise,   // uio[6], LOAD mode only
    output wire         start_fall,
    output wire         host_go_rise, // uio[4], LOAD mode only
    output wire         host_go_fall,

    input  wire         mode_load     // LOAD vs running -- selects uio[6]'s START/HOST_ERROR role
);

  // -----------------------------------------------------------------
  // 2-flop synchronizers -- every external input, uniformly.
  // -----------------------------------------------------------------
  reg [7:0] ui_in_ff1,  ui_in_ff2;
  reg [7:0] uio_in_ff1, uio_in_ff2;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      ui_in_ff1  <= 8'd0;
      ui_in_ff2  <= 8'd0;
      uio_in_ff1 <= 8'd0;
      uio_in_ff2 <= 8'd0;
    end else begin
      ui_in_ff1  <= ui_in;
      ui_in_ff2  <= ui_in_ff1;
      uio_in_ff1 <= uio_in;
      uio_in_ff2 <= uio_in_ff1;
    end
  end

  assign ui_in_sync = ui_in_ff2;
  assign pin_read   = uio_in_ff2[pin_index];

  // -----------------------------------------------------------------
  // Edge detection on the already-synchronized START (uio[6]) and
  // HOST_GO (uio[4]) levels -- one extra register each for the "last
  // cycle's synchronized sample" to compare against; no extra sync
  // latency added beyond the 2-flop stage above. Gated to LOAD mode
  // only (docs/architecture.md -- both signals are mode-dependent
  // reinterpretations of the same physical pins) so core.v never sees
  // a stray pulse from, e.g., firmware toggling HOST_ERROR at runtime
  // looping back through uio_in on the same physical pin as START.
  // -----------------------------------------------------------------
  reg start_prev, host_go_prev;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      start_prev   <= 1'b0;
      host_go_prev <= 1'b0;
    end else begin
      start_prev   <= uio_in_ff2[6];
      host_go_prev <= uio_in_ff2[4];
    end
  end

  assign start_rise   = mode_load &&  uio_in_ff2[6] && !start_prev;
  assign start_fall   = mode_load && !uio_in_ff2[6] &&  start_prev;
  assign host_go_rise = mode_load &&  uio_in_ff2[4] && !host_go_prev;
  assign host_go_fall = mode_load && !uio_in_ff2[4] &&  host_go_prev;

  // "Seen START fall" latch -- gates uio_oe[6] closed until the host
  // has provably released the pin, closing the driver-contention
  // hazard documented in docs/architecture.md's LOAD section. Sticky:
  // set once by a real start_fall, never cleared except reset.
  reg seen_start_fall;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) seen_start_fall <= 1'b0;
    else if (start_fall) seen_start_fall <= 1'b1;
  end

  // -----------------------------------------------------------------
  // Sticky per-pin state, indexed directly by pin_index (== uio bit
  // position). set_en/outb_en are mutually exclusive by construction
  // (core.v only ever asserts one per cycle, for whichever opcode is
  // committing), so no priority between the two `drv` updates is ever
  // actually exercised.
  // -----------------------------------------------------------------
  reg [1:0] mode [0:7];
  reg       drv  [0:7];
  integer   i;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (i = 0; i < 8; i = i + 1) begin
        mode[i] <= `SET_MODE_INPUT;
        drv[i]  <= 1'b0;
      end
    end else begin
      if (set_en && set_mode != `SET_MODE_LEAVE) mode[pin_index] <= set_mode;
      if (set_en)                                drv[pin_index]  <= set_value;
      if (outb_en)                                drv[pin_index]  <= outb_bit;
    end
  end

  // -----------------------------------------------------------------
  // uio_oe/uio_out generation, per bit. pin_index 4 (HOST_GO) is a
  // fixed input in every mode; 5 (HOST_STATUS) a fixed output in
  // every mode (including LOAD, where it acks writes -- Pipeline's
  // LOAD section); 6 (START/HOST_ERROR) mode-muxes direction and gates
  // on `seen_start_fall`; 0-3/7 are the sticky-configured protocol
  // pins, forced Hi-Z throughout LOAD regardless of sticky mode
  // (architecture.md -- nothing should drive external protocol lines
  // before firmware is running).
  // -----------------------------------------------------------------
  genvar gi;
  generate
    for (gi = 0; gi < 8; gi = gi + 1) begin : PIN
      if (gi == 4) begin : G_HOST_GO
        assign uio_oe[gi]  = 1'b0;
        assign uio_out[gi] = 1'b0;
      end else if (gi == 5) begin : G_HOST_STATUS
        assign uio_oe[gi]  = 1'b1;
        assign uio_out[gi] = drv[gi];
      end else if (gi == 6) begin : G_START_ERROR
        assign uio_oe[gi]  = mode_load ? 1'b0 : seen_start_fall;
        assign uio_out[gi] = drv[gi];
      end else begin : G_PROTOCOL
        wire is_pp = (mode[gi] == `SET_MODE_PUSH_PULL);
        wire is_od = (mode[gi] == `SET_MODE_OPEN_DRAIN);
        assign uio_oe[gi]  = mode_load ? 1'b0 : (is_pp || (is_od && !drv[gi]));
        assign uio_out[gi] = drv[gi];
      end
    end
  endgenerate

endmodule
