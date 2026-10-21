// agent_top_pin_contract_props.v
// Chip-level pin contract of tt_um_pankajnair_protocol_emulator (src/top.v)
// -- docs/architecture.md Pin map, "LOAD mode" and I/O reset state.
//
// Every other props file checks one module with its neighbours' signals
// free. This one checks the whole chip from its pins: the real core,
// pin_ctrl and the top.v wiring between them, so a crossed connection
// (start_rise <-> host_go_rise, mode_load misrouted, a set_* bit swapped)
// fails here even though each module is correct on its own. The SRAM is
// formal/models/sram_anyseq.v: read data unconstrained every cycle, so
// the chip runs ANY program and the properties hold for all of them.
//
// Method: a ghost model built from the pins only (uio_in, rst_n):
//   g_s1/g_s2/g_sp  START (uio[6]) through a 2-flop synchronizer + history
//   g_h1/g_h2/g_hp  HOST_GO (uio[4]) likewise
//   g_load          LOAD mode: set by reset, cleared by START's synchronized
//                   rise (architecture.md: "LOAD ends on START's rise")
//   g_seen          START seen falling after LOAD (HOST_ERROR's enable gate)
//   g_ack           the boot handshake ack: set on HOST_GO's synchronized
//                   rise, cleared on its fall, LOAD only
// Debug ports (formal_common.py) are only ever asserted equal to a ghost,
// never used to compute an expected value: mode_load (top.v's own wire),
// core_halted, core_load_ack, pc_seen_fall.
//
// Properties (checked out of reset):
//   P1  uio[4] (HOST_GO) is always an input, uio[5] (HOST_STATUS) always
//       an output
//   P2  throughout LOAD, protocol pins 0-3/7 and uio[6] are Hi-Z
//   P3  uio_oe[6] (HOST_ERROR) == run mode AND START has fallen since
//   P4  the first cycle out of reset: uo_out = 0, uio_oe = only HOST_STATUS,
//       HOST_STATUS low
//   P5  in LOAD, the HOST_STATUS pin is the ack one cycle late: rises
//       2 cycles after HOST_GO's synchronized rise, falls likewise
//   P6  (wiring / strengthening) mode_load == g_load, and the internal
//       ack/seen latches equal their ghosts

module top_pin_contract_props (
    input wire       clk,
    input wire       rst_n,
    input wire       ena,
    input wire [7:0] ui_in,
    input wire [7:0] uio_in,
    input wire [7:0] uo_out,
    input wire [7:0] uio_out,
    input wire [7:0] uio_oe,
    input wire       mode_load,
    input wire       core_halted,
    input wire       core_load_ack,
    input wire       pc_seen_fall
);

  initial assume (!rst_n);

  // ----------------------------------------------------------------------
  // Ghost model from the pins.
  // ----------------------------------------------------------------------
  reg       g_s1, g_s2, g_sp, g_h1, g_h2, g_hp;
  reg       g_load, g_seen, g_ack;
  reg       p_load, p_ack;   // last cycle's g_load / g_ack (drv[5] lags by one)
  reg [1:0] g_cnt;           // clean edges since reset, saturating

  wire g_start_rise = g_load && g_s2 && !g_sp;
  wire g_hg_rise    = g_load && g_h2 && !g_hp;
  wire g_hg_fall    = g_load && !g_h2 && g_hp;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      {g_s1, g_s2, g_sp, g_h1, g_h2, g_hp} <= 6'd0;
      g_load <= 1'b1;
      g_seen <= 1'b0;
      g_ack  <= 1'b0;
      p_load <= 1'b0;
      p_ack  <= 1'b0;
      g_cnt  <= 2'd0;
    end else begin
      g_s1 <= uio_in[6];
      g_s2 <= g_s1;
      g_sp <= g_s2;
      g_h1 <= uio_in[4];
      g_h2 <= g_h1;
      g_hp <= g_h2;
      if (g_start_rise) g_load <= 1'b0;
      if (!g_load && !g_s2 && g_sp) g_seen <= 1'b1;
      // START's rise takes priority over the handshake (it ends LOAD)
      if (g_load && !g_start_rise) begin
        if (g_ack) begin
          if (g_hg_fall) g_ack <= 1'b0;
        end else if (g_hg_rise) begin
          g_ack <= 1'b1;
        end
      end
      p_load <= g_load;
      p_ack  <= g_ack;
      if (g_cnt != 2'd3) g_cnt <= g_cnt + 2'd1;
    end
  end

  reg p_out5;
  always @(posedge clk) p_out5 <= uio_out[5];

  always @(posedge clk) begin
    if (rst_n) begin
      // ------------------------------------------------------------------
      // P1 -- fixed-direction host pins.
      // ------------------------------------------------------------------
      cover (uio_in[4] && !g_load);
      assert (!uio_oe[4]);
      assert (uio_oe[5]);

      // ------------------------------------------------------------------
      // P2 -- Hi-Z throughout LOAD. Covers: LOAD while the inputs
      // toggle; and run mode with a protocol pin driven (firmware's SET).
      // ------------------------------------------------------------------
      if (g_load) begin
        cover (g_h2 && uio_in[0]);
        assert (uio_oe[3:0] == 4'd0 && !uio_oe[7] && !uio_oe[6]);
      end
      cover (!g_load && uio_oe[0]);
      cover (!g_load && uio_oe[7]);

      // ------------------------------------------------------------------
      // P3 -- HOST_ERROR's enable, exactly. Covers: the gate opening.
      // ------------------------------------------------------------------
      cover (!g_load && g_seen);
      cover (!g_load && !g_seen && g_s2);
      assert (uio_oe[6] == (!g_load && g_seen));

      // ------------------------------------------------------------------
      // P4 -- pin state on the first cycle out of reset.
      // ------------------------------------------------------------------
      if (g_cnt == 2'd0) begin
        cover (uio_in == 8'hFF);
        assert (uo_out == 8'd0);
        assert (uio_oe == 8'b0010_0000);
        assert (!uio_out[5]);
      end

      // ------------------------------------------------------------------
      // P5 -- the boot ack on the HOST_STATUS pin, cycle-exact.
      // Covers: the pin rising and falling in LOAD.
      // ------------------------------------------------------------------
      if (p_load) begin
        cover (uio_out[5] && !p_out5);
        cover (!uio_out[5] && p_out5);
        assert (uio_out[5] == p_ack);
      end

      // ------------------------------------------------------------------
      // P6 -- top.v's mode_load wire is the pin-level LOAD, and the
      // internal latches track their ghosts (induction strengthening).
      // Covers: LOAD ending.
      // ------------------------------------------------------------------
      cover (g_start_rise);
      assert (mode_load == g_load);
      assert (!core_halted || !g_load);
      if (g_load) assert (core_load_ack == g_ack);
      assert (pc_seen_fall == g_seen);
    end
  end

endmodule

bind tt_um_pankajnair_protocol_emulator top_pin_contract_props u_top_pin_contract_props (
    .clk          (clk),
    .rst_n        (rst_n),
    .ena          (ena),
    .ui_in        (ui_in),
    .uio_in       (uio_in),
    .uo_out       (uo_out),
    .uio_out      (uio_out),
    .uio_oe       (uio_oe),
    .mode_load    (mode_load),
    .core_halted  (core_halted),
    .core_load_ack(core_load_ack),
    .pc_seen_fall (pc_seen_fall)
);
