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
 * TODO: implement the real body (2-flop sync, sticky drive-mode bank,
 * uio_oe/uio_out generation, edge detection). Stubbed for now -- correct
 * port list, all outputs tied off -- so core.v can be instantiated and
 * lint-checked against it while core.v itself is being built out.
 * Not included in source_files yet -- not wired into top.v.
 */

`default_nettype none

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

  // TODO: real synchronizer/drive-mode/edge-detect body. Stub keeps
  // this lint-clean and every output at a safe, inactive default.
  assign uio_out     = 8'd0;
  assign uio_oe      = 8'd0;
  assign ui_in_sync  = ui_in;
  assign pin_read    = 1'b0;
  assign start_rise  = 1'b0;
  assign start_fall  = 1'b0;
  assign host_go_rise = 1'b0;
  assign host_go_fall = 1'b0;

  wire _unused = &{uio_in, clk, rst_n, pin_index, set_en, set_mode,
                    set_value, outb_en, outb_bit, mode_load, 1'b0};

endmodule
