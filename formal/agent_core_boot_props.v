// agent_core_boot_props.v
// Formal properties targeting src/cpu/core.v's LOAD-mode boot path: the
// boot memory writes, the byte-address counter and the uo_out address
// echo.
//
// Spec: docs/architecture.md "LOAD (boot)":
//   - each HOST_GO pulse writes ui_in to SRAM at an internal byte-address
//     counter, then increments it (0-1023);
//   - write-commit is HOST_STATUS-acked: the chip writes, increments,
//     updates the uo_out echo, THEN raises HOST_STATUS; it clears
//     HOST_STATUS on the synchronized HOST_GO fall, and only then is the
//     next pulse accepted;
//   - uo_out echoes the counter's low 8 bits, post-increment, valid when
//     HOST_STATUS rises;
//   - the counter saturates at 1023, does not wrap: the pulse at 1023
//     writes/echoes that byte, further pulses are harmless no-ops.
//
// "Accepted pulse" = host_go_rise in LOAD while no ack is pending. A
// START rise in the same cycle leaves LOAD with priority: the write
// strobe of that cycle still fires (mem_we is combinational on the
// rise), but the counter/ack/echo do not advance -- the core never
// returns to LOAD before a reset, so this can never double-write.
//
// Toolchain constraint: plain immediate assertions inside
// `always @(posedge clk)` only (formal/scripts/formal_common.py).
//
// Independence: the expected write strobe, write address, echo value
// and ack level are all computed from the checker's own ghost model,
// driven ONLY by ports (host_go_rise/fall, start_rise, ui_in_sync) plus
// a port-level observation of the 1023 write (mem_we && mem_addr ==
// 1023). DUT internals boot_addr / boot_full / load_ack / state /
// halted (DEBUG_PORTS) are asserted on only -- ghost == real -- so that
// k-induction cannot start from a state where the DUT's counter
// disagrees with the model; they never feed a prediction.
//
// Environment: the one assume is "reset at step 0". rst_n, host_go_*,
// start_rise and ui_in_sync are free afterwards (mid-trace resets are
// explored; ghost state resets with the DUT).
//
// deep-evidence: test_boot_echo_and_saturation
//   (test/test.py: 1026 boot pulses -- the write at 1023, the pulse at
//   1023, over-run pulses and their acks are all reached in simulation;
//   formal/scripts/vacuity_check.py counts the [deep] covers from it)
//
// Reachability: the counter starts at 0 and needs one full handshake
// (rise, then fall: >= 2 cycles) per increment, so address 1023 is
// first reachable at step ~2047 -- far past BMC depth 20. The covers
// marked [deep] below are therefore NOT reached by `make vacuity` at
// depth 20 and are kept on purpose (not faked with an assume). The
// saturation / over-run asserts they guard are proven for ALL reachable
// states by k-induction: the auxiliary invariants (ghost == boot_addr,
// ghost_full == boot_full == "1023 has been written", full -> addr ==
// 1023, ack/echo/mode equalities) make every invariant-consistent
// state -- including addr == 1023, full or not -- a legal induction
// start state, so the induction step checks the 1023 write and the
// over-run no-op from exactly those states.

module core_boot_props (
    input wire        clk,
    input wire        rst_n,
    input wire [9:0]  mem_addr,
    input wire        mem_we,
    input wire [7:0]  mem_wdata,
    input wire [7:0]  ui_in_sync,
    input wire        host_go_rise,
    input wire        host_go_fall,
    input wire        start_rise,
    input wire        mode_load,
    input wire [2:0]  pin_index,
    input wire        set_en,
    input wire        set_value,
    input wire [7:0]  uo_out,
    // k-induction auxiliary-invariant observation only (asserted on,
    // never used to predict anything):
    input wire [1:0]  state,      // DEBUG_PORTS
    input wire        halted,     // DEBUG_PORTS
    input wire [9:0]  boot_addr,  // DEBUG_PORTS
    input wire        boot_full,  // DEBUG_PORTS
    input wire        load_ack    // DEBUG_PORTS
);

  localparam S_LOAD = 2'd0;

  // ======================================================================
  // Environment: genuine reset at step 0 (async reset). Free after.
  // ======================================================================
  initial assume (!rst_n);

  // ----------------------------------------------------------------------
  // Ghost boot model (spec, port-driven).
  // ----------------------------------------------------------------------
  reg       g_load;   // still in LOAD (cleared by START's rise)
  reg [9:0] g_addr;   // byte-address counter
  reg       g_full;   // the write at 1023 has been accepted
  reg       g_ack;    // HOST_STATUS ack pending
  reg [7:0] g_echo;   // expected uo_out during LOAD
  reg       g_w1023;  // port-observed: a LOAD write to 1023 has happened

  wire       in_load  = rst_n && g_load;
  wire       acc      = in_load && host_go_rise && !g_ack;   // accepted pulse
  wire       exp_we   = acc && !g_full;                       // spec write strobe
  wire       adv      = acc && !start_rise;                   // counter/ack/echo step
  wire [9:0] addr_nx  = (g_addr == 10'd1023) ? 10'd1023 : g_addr + 10'd1;
  wire       load_we  = in_load && mem_we;                    // observed LOAD write

  // Previous-cycle snapshots.
  reg       p_valid, p_load, p_adv, p_set_value, p_acc, p_we;
  reg [9:0] p_maddr;
  reg [9:0] p_addr;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      g_load      <= 1'b1;
      g_addr      <= 10'd0;
      g_full      <= 1'b0;
      g_ack       <= 1'b0;
      g_echo      <= 8'd0;
      g_w1023     <= 1'b0;
      p_valid     <= 1'b0;
      p_load      <= 1'b0;
      p_adv       <= 1'b0;
      p_acc       <= 1'b0;
      p_we        <= 1'b0;
      p_maddr     <= 10'd0;
      p_set_value <= 1'b0;
      p_addr      <= 10'd0;
    end else begin
      p_valid     <= 1'b1;
      p_load      <= g_load;
      p_adv       <= adv;
      p_acc       <= acc;
      p_we        <= load_we;
      p_maddr     <= mem_addr;
      p_set_value <= set_value;
      p_addr      <= g_addr;
      if (load_we && mem_addr == 10'd1023) g_w1023 <= 1'b1;
      if (g_load) begin
        if (start_rise) begin
          g_load <= 1'b0;
        end else if (g_ack) begin
          if (host_go_fall) g_ack <= 1'b0;
        end else if (host_go_rise) begin
          g_addr <= addr_nx;
          if (g_addr == 10'd1023) g_full <= 1'b1;
          g_echo <= addr_nx[7:0];
          g_ack  <= 1'b1;
        end
      end
    end
  end

  // ======================================================================
  // Property 1 -- write strobe: in LOAD, a memory write happens exactly
  // on an accepted pulse before the 1023 write (no write while an ack is
  // pending, no write after full, no spurious write), to the current
  // boot address, with ui_in_sync as data.
  // ======================================================================
  always @(posedge clk) begin
    if (in_load) begin
      assert (mem_we == exp_we);
    end
    if (in_load && exp_we) begin
      cover (g_addr == 10'd0);
      cover (g_addr == 10'd3);                         // normal write, 4th pulse
      cover (g_addr == 10'd1023);                      // [deep] saturation write
      assert (mem_addr == g_addr);
      assert (mem_wdata == ui_in_sync);
    end
    if (in_load && g_ack) begin
      cover (host_go_rise);                            // re-rise while ack pending
      assert (!mem_we);
    end
  end

  // ======================================================================
  // Property 2 -- exactly once: the cycle after a LOAD write (still in
  // LOAD) the ack is pending -- so no second write until HOST_GO falls
  // (P1) -- and the counter has moved past the written address, or the
  // write was the 1023 one and boot is full (P4). With the counter
  // never decreasing during LOAD, no address is ever written twice.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n && p_valid && p_load && g_load && p_we) begin
      cover (p_maddr == 10'd1);
      assert (load_ack);
      assert (boot_addr == p_maddr + 10'd1 || (p_maddr == 10'd1023 && boot_full));
    end
    if (rst_n && p_valid && p_load && g_load) begin
      cover (boot_addr != p_addr);
      assert (boot_addr >= p_addr);
    end
  end

  // ======================================================================
  // Property 3 -- counter: starts at 0 after reset, +1 per accepted
  // pulse, unchanged otherwise, saturates at 1023 (never wraps).
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n && !p_valid) begin
      cover (1'b1);
      assert (boot_addr == 10'd0);
    end
    if (rst_n && p_valid && p_load && g_load) begin
      cover (p_adv && p_addr == 10'd2);
      assert (boot_addr == (p_adv ? ((p_addr == 10'd1023) ? 10'd1023 : p_addr + 10'd1)
                                  : p_addr));
    end
    if (rst_n && p_valid && p_load && g_load && p_adv && p_addr == 10'd1023) begin
      cover (1'b1);                                    // [deep] pulse at 1023
      assert (boot_addr == 10'd1023);
    end
  end

  // ======================================================================
  // Property 4 -- saturation / over-run: the pulse at 1023 writes (P1);
  // once a LOAD write to 1023 has happened, no further LOAD write ever
  // happens, and over-run pulses are still acked and echo 1023's low
  // byte (harmless no-op, not a wrap to 0).
  // ======================================================================
  always @(posedge clk) begin
    if (in_load && g_w1023) begin
      cover (host_go_rise && !g_ack);                  // [deep] over-run pulse
      assert (!mem_we);
    end
    if (rst_n && p_valid && p_load && g_load && p_adv && g_full) begin
      cover (p_addr == 10'd1023);                      // [deep] over-run acked
      assert (set_value);
      assert (uo_out == 8'hFF);
      assert (boot_addr == 10'd1023);
    end
  end

  // ======================================================================
  // Property 5 -- echo at the ack: when HOST_STATUS rises in LOAD,
  // uo_out already equals the post-increment address's low 8 bits, and
  // the rise is caused by the accepted pulse of the previous cycle.
  // HOST_STATUS in LOAD is the continuous pin-5 request.
  // ======================================================================
  always @(posedge clk) begin
    if (in_load) begin
      assert (pin_index == 3'd5);
      assert (set_en);
      assert (set_value == g_ack);
      assert (uo_out == g_echo);
    end
    if (rst_n && p_valid && p_load && g_load && !p_set_value && set_value) begin
      cover (uo_out == 8'd1);
      cover (uo_out == 8'd3);                          // echo after 3 pulses
      assert (p_acc);
      assert (uo_out == g_addr[7:0]);
      assert (g_addr == ((p_addr == 10'd1023) ? 10'd1023 : p_addr + 10'd1));
    end
  end

  // ======================================================================
  // Auxiliary invariants for k-induction (make prove). Ghost == real.
  // Without these, induction may start from a state where boot_addr,
  // boot_full or load_ack disagree with the model (e.g. full set at
  // address 5), which no bounded depth can rule out since 1023 is
  // ~2047 steps away.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n) begin
      assert (mode_load == g_load);
      assert (!g_full || g_addr == 10'd1023);
    end
    if (in_load) begin
      // (Not outside LOAD: a 1023 write coinciding with START's rise
      // writes but leaves LOAD before boot_full is set.)
      assert (g_full == g_w1023);
      cover (g_ack && g_addr == 10'd2);
      assert (state == S_LOAD);
      assert (!halted);
      assert (boot_addr == g_addr);
      assert (boot_full == g_full);
      assert (load_ack  == g_ack);
    end
  end

endmodule

bind protocol_cpu_core core_boot_props u_core_boot_props (
    .clk         (clk),
    .rst_n       (rst_n),
    .mem_addr    (mem_addr),
    .mem_we      (mem_we),
    .mem_wdata   (mem_wdata),
    .ui_in_sync  (ui_in_sync),
    .host_go_rise(host_go_rise),
    .host_go_fall(host_go_fall),
    .start_rise  (start_rise),
    .mode_load   (mode_load),
    .pin_index   (pin_index),
    .set_en      (set_en),
    .set_value   (set_value),
    .uo_out      (uo_out),
    .state       (state),
    .halted      (halted),
    .boot_addr   (boot_addr),
    .boot_full   (boot_full),
    .load_ack    (load_ack)
);
