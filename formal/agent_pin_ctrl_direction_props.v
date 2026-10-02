// agent_pin_ctrl_direction_props.v
// Formal properties targeting src/io/pin_ctrl.v's pin-direction safety
// and the uio[6] START/HOST_ERROR driver-contention gate -- the
// "pin-direction-never-contended" candidate in formal/README.md.
//
// Spec: docs/architecture.md -- Pin map (uio[4] fixed input, uio[5]
// fixed output, uio[6] START in LOAD / HOST_ERROR when running),
// Pipeline's LOAD section (uio_oe[6] stays 0 until START's synchronized
// FALLING edge has been observed), "Reset values" (protocol pins reset
// to input mode, the seen-START-fall gate resets closed).
//
// Motivation: commit 142ab71 fixed a real bug nothing formal covered.
// `seen_start_fall` used to latch from the mode_load-gated `start_fall`;
// START's rise is what ends LOAD, so the fall is always seen after
// mode_load drops, the latch never set, and uio_oe[6] was stuck at 0
// forever (HOST_ERROR undrivable). Property 2's cover below is
// UNREACHABLE against that old RTL (confirmed against a scratch mutant,
// see the report accompanying this file), and its companion assert
// fails there too.
//
// Toolchain constraint: plain immediate assertions inside
// `always @(posedge clk)` only -- no concurrent SVA under this yosys
// build (formal/scripts/formal_common.py docstring).
//
// Method: the uio[6] gate is checked against an independent ghost model
// built ONLY from port-level inputs (uio_in, rst_n, mode_load): the
// checker runs its own 2-flop synchronizer + previous-sample register on
// uio_in[6] and latches `g_seen_fall` when a synchronized high->low
// transition is seen while !mode_load. It never reads the DUT's
// internal `seen_start_fall`. Protocol-pin properties read the DUT's
// sticky `mode`/`drv` arrays via formal_common.DEBUG_PORTS (dbg_ ports
// on a scratch copy -- src/ untouched), and additionally cross-check
// `mode` for the protocol pins against a ghost sticky-mode model derived
// from the set_en/set_mode/pin_index ports, so a wrong reset value or
// wrong LEAVE handling can't hide behind the debug-port read.
//
// Extension (driven-VALUE properties, Properties 5-7): the direction
// properties above never checked what value goes OUT on uio_out. Two
// sim-surviving mutants motivated this: uio_out[5] tied to 0 outside
// LOAD (runtime SET HOST_STATUS never reaches the pin), and a wrong-index
// drive source. Ghost model 3 (`g_val`) is the last value written to each
// pin_index, built ONLY from the set_en/set_value and outb_en/outb_bit
// ports -- the DUT's `drv` debug port is never used to decide what is
// expected on uio_out.
//
// Scope: bounded-depth BMC (DEFAULT_DEPTH=20), not an inductive proof.
// All inputs (uio_in, mode_load, set_*, outb_*, pin_index) are free;
// the only assume is the standard "reset happens at step 0".

module pin_ctrl_direction_props (
    input wire             clk,
    input wire             rst_n,
    input wire [7:0]       uio_in,
    input wire [7:0]       uio_oe,
    input wire             mode_load,
    input wire [2:0]       pin_index,
    input wire             set_en,
    input wire [1:0]       set_mode,
    input wire             set_value,
    input wire             outb_en,
    input wire             outb_bit,
    input wire [7:0]       uio_out,
    input wire [7:0][1:0]  mode,   // DEBUG_PORTS: pin_ctrl.mode[0:7]
    input wire [7:0]       drv     // DEBUG_PORTS: pin_ctrl.drv[0:7]
);

  // ======================================================================
  // Environment: a genuine reset occurs at step 0 (every module here uses
  // async reset; without this all state starts at solver garbage).
  // ======================================================================
  initial assume (!rst_n);

  // ----------------------------------------------------------------------
  // Ghost model 1: START fall observed after leaving LOAD.
  // ----------------------------------------------------------------------
  reg g_ff1, g_ff2, g_prev, g_seen_fall;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      g_ff1       <= 1'b0;
      g_ff2       <= 1'b0;
      g_prev      <= 1'b0;
      g_seen_fall <= 1'b0;
    end else begin
      g_ff1  <= uio_in[6];
      g_ff2  <= g_ff1;
      g_prev <= g_ff2;
      if (!mode_load && g_prev && !g_ff2) g_seen_fall <= 1'b1;
    end
  end

  // ----------------------------------------------------------------------
  // Ghost model 2: sticky per-pin drive mode (docs/isa.md SET row: LEAVE
  // keeps the current mode, reset value is INPUT).
  // ----------------------------------------------------------------------
  reg [1:0] g_mode [0:7];
  integer k;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (k = 0; k < 8; k = k + 1) g_mode[k] <= `SET_MODE_INPUT;
    end else if (set_en && set_mode != `SET_MODE_LEAVE) begin
      g_mode[pin_index] <= set_mode;
    end
  end

  // ======================================================================
  // Property 1 -- no contention on uio[6]: never driven in LOAD, and never
  // driven before a synchronized START high->low has been observed while
  // not in LOAD.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n && mode_load) begin
      cover (1'b1);
      assert (!uio_oe[6]);
    end
    if (rst_n && !g_seen_fall) begin
      cover (!mode_load);
      assert (!uio_oe[6]);
    end
  end

  // ======================================================================
  // Property 2 -- uio[6] liveness-as-reachability: HOST_ERROR's driver
  // actually turns on in some trace (the cover the old RTL could never
  // reach), plus the companion assert: once the fall has been observed
  // and we're running, the driver IS enabled (catches stuck-at-0).
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n) begin
      cover (uio_oe[6]);
    end
    if (rst_n && g_seen_fall && !mode_load) begin
      cover (1'b1);
      assert (uio_oe[6]);
    end
  end

  // ======================================================================
  // Property 3 -- fixed-role pins: HOST_GO (uio[4]) always input,
  // HOST_STATUS (uio[5]) always output, in every mode incl. LOAD.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n) begin
      cover (mode_load);
      cover (!mode_load);
      assert (!uio_oe[4]);
      assert (uio_oe[5]);
    end
  end

  // ======================================================================
  // Property 4 -- protocol pins (0-3, 7): uio_oe[n] is 1 only if not in
  // LOAD and (push-pull, or open-drain driving 0). Plus the converse
  // (catches a stuck-Hi-Z pin) and the sticky-mode ghost cross-check.
  // ======================================================================
  genvar n;
  generate
    for (n = 0; n < 8; n = n + 1) begin : P
      if (n <= 3 || n == 7) begin : G_PROTO
        wire is_pp   = (mode[n] == `SET_MODE_PUSH_PULL);
        wire is_od   = (mode[n] == `SET_MODE_OPEN_DRAIN);
        wire allowed = !mode_load && (is_pp || (is_od && !drv[n]));

        always @(posedge clk) begin
          if (rst_n) begin
            // 4a: never driven in LOAD / only per sticky mode.
            cover (uio_oe[n]);
            assert (!uio_oe[n] || allowed);
            // 4a': LOAD with a driving mode configured is still Hi-Z.
            if (mode_load && (is_pp || is_od)) begin
              cover (1'b1);
              assert (!uio_oe[n]);
            end
            // 4b: converse -- configured-driving pins really drive.
            if (allowed) begin
              cover (is_od);
              cover (is_pp);
              assert (uio_oe[n]);
            end
            // 4c: DUT's sticky mode matches the spec-derived ghost.
            cover (g_mode[n] != `SET_MODE_INPUT);
            assert (mode[n] == g_mode[n]);
          end
        end
      end
    end
  endgenerate

  // ----------------------------------------------------------------------
  // Ghost model 3: last value written to each pin_index via SET or OUTB
  // (docs/isa.md SET/OUTB rows; architecture.md pin_index table rows 5/6).
  // Reset value 0 (architecture.md "Reset values"). If set_en and outb_en
  // ever fire together (core.v never does this) OUTB is taken as the later
  // write -- irrelevant to the spec, just keeps the ghost total.
  // Also records, per write, side-band facts the covers need:
  //   g_wr5_run/g_wr5_val : a running-mode write to pin 5 happened last
  //                         cycle, and its value
  //   g_out5_prev         : uio_out[5] last cycle
  //   g_wr6_closed        : the most recent write to pin 6 happened while
  //                         HOST_ERROR's gate (ghost-derived) was closed
  // ----------------------------------------------------------------------
  reg       g_val [0:7];
  reg       g_wr5_run, g_wr5_val, g_out5_prev, g_wr6_closed;
  integer   j;
  wire      g_gate6_open = !mode_load && g_seen_fall;
  wire      wr_any = set_en || outb_en;
  wire      wr_bit = outb_en ? outb_bit : set_value;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (j = 0; j < 8; j = j + 1) g_val[j] <= 1'b0;
      g_wr5_run    <= 1'b0;
      g_wr5_val    <= 1'b0;
      g_out5_prev  <= 1'b0;
      g_wr6_closed <= 1'b0;
    end else begin
      if (wr_any) g_val[pin_index] <= wr_bit;
      g_wr5_run   <= wr_any && pin_index == 3'd5 && !mode_load;
      g_wr5_val   <= wr_bit;
      g_out5_prev <= uio_out[5];
      if (wr_any && pin_index == 3'd6) g_wr6_closed <= !g_gate6_open;
    end
  end

  // ======================================================================
  // Property 5 -- HOST_STATUS (uio[5]) always shows the last value written
  // to pin 5, in LOAD (write ack) and running mode alike. Covers: LOAD-mode
  // ack visible; a running-mode write of 1 reaching the pin; a running-mode
  // write of 0 pulling a previously-high pin low.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n) begin
      cover (mode_load && uio_out[5]);
      cover (!mode_load && uio_out[5]);
      assert (uio_out[5] == g_val[5]);
      if (g_wr5_run) begin
        cover (g_wr5_val && !g_out5_prev);
        cover (!g_wr5_val && g_out5_prev);
        assert (uio_out[5] == g_wr5_val);
      end
    end
  end

  // ======================================================================
  // Property 6 -- HOST_ERROR (uio[6]): whenever its driver is enabled, the
  // pin shows the last value written to pin 6. Cover: a 1 written while
  // the gate was still closed appears once the gate opens.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n && uio_oe[6]) begin
      cover (g_val[6]);
      assert (uio_out[6] == g_val[6]);
      // Value latched pre-gate, shown post-gate (assert above covers it).
      cover (g_wr6_closed && g_val[6]);
    end
  end

  // ======================================================================
  // Property 7 -- protocol pins (0-3, 7): whenever driven, the pin shows
  // the last value written; in open-drain mode (ghost sticky mode) a driven
  // pin is always driving 0.
  // ======================================================================
  generate
    for (n = 0; n < 8; n = n + 1) begin : V
      if (n <= 3 || n == 7) begin : G_PROTO_VAL
        always @(posedge clk) begin
          if (rst_n && uio_oe[n]) begin
            cover (uio_out[n]);
            assert (uio_out[n] == g_val[n]);
            if (g_mode[n] == `SET_MODE_OPEN_DRAIN) begin
              cover (1'b1);
              assert (!uio_out[n]);
            end
          end
        end
      end
    end
  endgenerate

endmodule

bind pin_ctrl pin_ctrl_direction_props u_pin_ctrl_direction_props (
    .clk      (clk),
    .rst_n    (rst_n),
    .uio_in   (uio_in),
    .uio_oe   (uio_oe),
    .mode_load(mode_load),
    .pin_index(pin_index),
    .set_en   (set_en),
    .set_mode (set_mode),
    .set_value(set_value),
    .outb_en  (outb_en),
    .outb_bit (outb_bit),
    .uio_out  (uio_out),
    .mode     (mode),
    .drv      (drv)
);
