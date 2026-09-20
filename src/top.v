/*
 * Copyright (c) 2026 Pankaj Nair
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Instantiates cpu/core.v + io/pin_ctrl.v + mem/mem.v as three
// top-level siblings (docs/architecture.md hierarchy) -- top.v itself
// does no logic, just the wiring core.v<->pin_ctrl.v/mem.v shown in
// each module's own header comment. Port connections here match the
// throwaway wiring-check harness those three modules were already
// elaborated/simulated against (commits 01fbd12/2a522cc).
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

  wire [9:0] mem_addr;
  wire       mem_we;
  wire [7:0] mem_wdata;
  wire [7:0] mem_rdata;

  wire [2:0] pin_index;
  wire       set_en;
  wire [1:0] set_mode;
  wire       set_value;
  wire       outb_en;
  wire       outb_bit;
  wire       pin_read;
  wire [7:0] ui_in_sync;
  wire       start_rise, start_fall;
  wire       host_go_rise, host_go_fall;
  wire       mode_load;

  protocol_cpu_core u_core (
      .clk         (clk),
      .rst_n       (rst_n),
      .mem_addr    (mem_addr),
      .mem_we      (mem_we),
      .mem_wdata   (mem_wdata),
      .mem_rdata   (mem_rdata),
      .pin_index   (pin_index),
      .set_en      (set_en),
      .set_mode    (set_mode),
      .set_value   (set_value),
      .outb_en     (outb_en),
      .outb_bit    (outb_bit),
      .pin_read    (pin_read),
      .ui_in_sync  (ui_in_sync),
      .start_rise  (start_rise),
      .start_fall  (start_fall),
      .host_go_rise(host_go_rise),
      .host_go_fall(host_go_fall),
      .mode_load   (mode_load),
      .uo_out      (uo_out)
  );

  mem u_mem (
      .clk  (clk),
      .rst_n(rst_n),
      .addr (mem_addr),
      .we   (mem_we),
      .wdata(mem_wdata),
      .rdata(mem_rdata)
  );

  pin_ctrl u_pin_ctrl (
      .ui_in       (ui_in),
      .uio_out     (uio_out),
      .uio_oe      (uio_oe),
      .uio_in      (uio_in),
      .clk         (clk),
      .rst_n       (rst_n),
      .ui_in_sync  (ui_in_sync),
      .pin_index   (pin_index),
      .set_en      (set_en),
      .set_mode    (set_mode),
      .set_value   (set_value),
      .outb_en     (outb_en),
      .outb_bit    (outb_bit),
      .pin_read    (pin_read),
      .start_rise  (start_rise),
      .start_fall  (start_fall),
      .host_go_rise(host_go_rise),
      .host_go_fall(host_go_fall),
      .mode_load   (mode_load)
  );

  // ena: TT convention, always 1 when powered -- no power-sequencing
  // logic in this design, nothing to gate on it.
  wire _unused = &{ena, 1'b0};

endmodule
