// agent_pin_ctrl_sync_depth_props.v
// Formal properties targeting src/io/pin_ctrl.v's input synchronizer
// depth -- docs/architecture.md, Pin map, "Every external input needs
// synchronizing": every ui_in/uio_in bit goes through the same standard
// 2-flop synchronizer, uniformly, a +2-cycle latency. Every consumer of
// an external input must see exactly the value from 2 cycles earlier --
// never 1 (metastability exposure) or 3 (timing drift vs. the ISA's
// cycle-exact WAIT/INB accounting).
//
// Motivation: the triage benchmark's planted bug tapped pin_read off
// uio_in_ff1 (a 1-flop path feeding WAIT/INB). Nothing formal pinned the
// depth down; the direction props only checked uio[6]'s gate.
//
// Toolchain constraint: plain immediate assertions inside
// `always @(posedge clk)` only -- no concurrent SVA under this yosys
// build (formal/scripts/formal_common.py docstring).
//
// Method: independent ghost delay line built ONLY from port-level inputs
// (ui_in, uio_in, rst_n). g_ui1/g_ui2 and g_uio1/g_uio2/g_uio3 are plain
// past-value registers (NOT reset -- the checker never relies on their
// contents until they have been loaded post-reset). `g_cnt` counts clean
// post-reset clock edges, saturating at 3:
//   g_cnt >= 2 : the 2-cycle-old samples g_*2 are real inputs
//   g_cnt == 3 : the 3-cycle-old sample g_uio3 is real too (needed to
//                define "the 2-cycle-old level changed" for edges)
// No DUT-internal signal is observed; every checked signal is a port, so
// no DEBUG_PORTS entry is used.
//
// Properties:
//   P1  ui_in_sync == ui_in from 2 cycles ago (all 8 bits)
//   P2  pin_read == uio_in[pin_index] from 2 cycles ago, pin_index taken
//       in the CURRENT cycle
//   P3  start_rise/start_fall/host_go_rise/host_go_fall == mode_load (current
//       cycle, as the RTL gates them) AND a 0->1 / 1->0 change of the
//       2-cycle-old level of uio[6] / uio[4] (i.e. uio[n] 2 vs 3 cycles ago)
//   P4  reset phase (spec "Reset values": sync flops reset to 0): during
//       the first 2 post-reset cycles ui_in_sync/pin_read read 0 and no
//       edge fires; on the 3rd (g_cnt==2) the edge detectors compare
//       against the reset-0 previous sample, so only a rise can fire.
//
// Scope: bounded-depth BMC (DEFAULT_DEPTH=20), not an inductive proof.
// All inputs are free (rst_n may re-assert mid-trace); the only assume is
// the standard "reset happens at step 0".

module pin_ctrl_sync_depth_props (
    input wire       clk,
    input wire       rst_n,
    input wire [7:0] ui_in,
    input wire [7:0] uio_in,
    input wire [7:0] ui_in_sync,
    input wire [2:0] pin_index,
    input wire       pin_read,
    input wire       mode_load,
    input wire       start_rise,
    input wire       start_fall,
    input wire       host_go_rise,
    input wire       host_go_fall
);

  // ======================================================================
  // Environment: a genuine reset occurs at step 0 (async reset everywhere;
  // without this all state starts at solver garbage).
  // ======================================================================
  initial assume (!rst_n);

  // ----------------------------------------------------------------------
  // Ghost delay line + clean-post-reset-cycle counter.
  // ----------------------------------------------------------------------
  reg [1:0] g_cnt;
  reg [7:0] g_ui1, g_ui2, g_ui3;
  reg [7:0] g_uio1, g_uio2, g_uio3;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) g_cnt <= 2'd0;
    else if (g_cnt != 2'd3) g_cnt <= g_cnt + 2'd1;
  end

  always @(posedge clk) begin
    g_ui1  <= ui_in;
    g_ui2  <= g_ui1;
    g_ui3  <= g_ui2;
    g_uio1 <= uio_in;
    g_uio2 <= g_uio1;
    g_uio3 <= g_uio2;
  end

  wire v2 = rst_n && (g_cnt >= 2'd2);   // 2-cycle-old samples valid
  wire v3 = rst_n && (g_cnt == 2'd3);   // 3-cycle-old sample valid too

  // Spec-derived expected edges (only meaningful under v3).
  wire e_start_rise = mode_load &&  g_uio2[6] && !g_uio3[6];
  wire e_start_fall = mode_load && !g_uio2[6] &&  g_uio3[6];
  wire e_hg_rise    = mode_load &&  g_uio2[4] && !g_uio3[4];
  wire e_hg_fall    = mode_load && !g_uio2[4] &&  g_uio3[4];

  // ======================================================================
  // P1 -- ui_in_sync is exactly ui_in delayed 2 cycles, all 8 bits.
  // Covers (per bit): a 0->1 and a 1->0 transition arriving at the output
  // (needs v3 so the "previous" 2-cycle-old value is real input too).
  // ======================================================================
  genvar b;
  generate
    for (b = 0; b < 8; b = b + 1) begin : P1
      always @(posedge clk) begin
        if (v2) begin
          cover (ui_in_sync[b]);
          cover (!ui_in_sync[b]);
          assert (ui_in_sync[b] == g_ui2[b]);
        end
        if (v3) begin
          cover (g_ui2[b] && !g_ui3[b]);
          cover (!g_ui2[b] && g_ui3[b]);
        end
      end
    end
  endgenerate

  // ======================================================================
  // P2 -- pin_read is uio_in[pin_index] delayed 2 cycles, index taken in
  // the current cycle. Covers: every pin_index observing both 0 and 1, and
  // (per index) a transition of the 2-cycle-old level being observed.
  // ======================================================================
  genvar p;
  generate
    for (p = 0; p < 8; p = p + 1) begin : P2
      always @(posedge clk) begin
        if (v2 && pin_index == p) begin
          cover (pin_read);
          cover (!pin_read);
          assert (pin_read == g_uio2[p]);
        end
        if (v3 && pin_index == p) begin
          cover (g_uio2[p] != g_uio3[p]);
        end
      end
    end
  endgenerate

  // Index is taken in the current cycle, not a registered one: cover a
  // cycle where pin_index changed and the two selected 2-cycle-old bits
  // differ (a stale-index tap would read the wrong one).
  reg [2:0] g_idx_prev;
  always @(posedge clk) g_idx_prev <= pin_index;
  always @(posedge clk) begin
    if (v3) begin
      cover (pin_index != g_idx_prev && g_uio2[pin_index] != g_uio2[g_idx_prev]);
    end
  end

  // ======================================================================
  // P3 -- edge detectors fire exactly when the 2-cycle-old level of
  // uio[6] (START) / uio[4] (HOST_GO) changes, gated by current mode_load.
  // Covers: each edge firing; and a real level change suppressed by
  // !mode_load (the gate is actually exercised).
  // ======================================================================
  always @(posedge clk) begin
    if (v3) begin
      cover (e_start_rise);
      cover (e_start_fall);
      cover (e_hg_rise);
      cover (e_hg_fall);
      cover (!mode_load && g_uio2[6] != g_uio3[6]);
      cover (!mode_load && g_uio2[4] != g_uio3[4]);
      assert (start_rise   == e_start_rise);
      assert (start_fall   == e_start_fall);
      assert (host_go_rise == e_hg_rise);
      assert (host_go_fall == e_hg_fall);
    end
  end

  // ======================================================================
  // P4 -- reset phase. Sync flops reset to 0 (architecture.md "Reset
  // values"): for the first two post-reset cycles both synchronized
  // outputs read 0 regardless of the pins and no edge fires. On the third
  // (g_cnt==2) the edge-detector's previous sample is still the reset 0,
  // so a rise fires iff the 2-cycle-old level is 1, a fall never fires.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n && g_cnt < 2'd2) begin
      cover (ui_in != 8'd0 && uio_in != 8'd0 && mode_load);
      assert (ui_in_sync == 8'd0);
      assert (!pin_read);
      assert (!start_rise && !start_fall && !host_go_rise && !host_go_fall);
    end
    if (rst_n && g_cnt == 2'd2) begin
      cover (start_rise);
      cover (host_go_rise);
      assert (start_rise   == (mode_load && g_uio2[6]));
      assert (host_go_rise == (mode_load && g_uio2[4]));
      assert (!start_fall && !host_go_fall);
    end
  end

endmodule

bind pin_ctrl pin_ctrl_sync_depth_props u_pin_ctrl_sync_depth_props (
    .clk         (clk),
    .rst_n       (rst_n),
    .ui_in       (ui_in),
    .uio_in      (uio_in),
    .ui_in_sync  (ui_in_sync),
    .pin_index   (pin_index),
    .pin_read    (pin_read),
    .mode_load   (mode_load),
    .start_rise  (start_rise),
    .start_fall  (start_fall),
    .host_go_rise(host_go_rise),
    .host_go_fall(host_go_fall)
);
