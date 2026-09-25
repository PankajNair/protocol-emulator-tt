// agent_core_reset_props.v
// Formal properties targeting src/cpu/core.v's reset behavior -- the
// "reset brings core.v back to a known fetch state" candidate in
// formal/README.md. Hazard class: reset returns the core to a known
// state, and nothing leaks across it.
//
// Spec: docs/architecture.md "Reset values" (PC=0, R0-R3=0, flag=0,
// return-address=0, return_valid=0, both sticky debug flags 0, FSM
// resets into LOAD, uo_out=0 and re-cleared at LOAD->FETCH_LO; a reset
// mid-FETCH_HI returns unconditionally to LOAD, in-flight fetch
// discarded), "LOAD (boot)" (exit on START's rising edge with PC=0;
// only the HOST_STATUS ack (pin_index=5) and the host-strobed boot
// write happen in LOAD), "FETCH_LO / FETCH_HI / EXECUTE" (1-cycle SRAM
// latency; LOAD/LOADX writeback trails into the next FETCH_LO), and
// docs/isa.md's DELAY row (EXECUTE held mantissa<<(exp*5) extra cycles).
//
// Toolchain constraint: plain immediate assertions inside
// `always @(posedge clk)` only (formal/scripts/formal_common.py).
//
// Independence: never reads core.v's `opcode`, `word`, `ir`,
// `entering_execute`, `exec_done`, `cnt_*`. Which instruction is
// executing is rebuilt at port level exactly as in
// agent_core_illegal_opcode_props.v: `state` (DEBUG_PORTS, the
// architectural FSM) locates the FETCH_HI->EXECUTE step; mem_rdata in
// FETCH_HI is the low byte and in that first EXECUTE cycle the high byte
// (opcode = [7:3]). Antecedent-side internals and why they're not
// circular:
//   - state: architectural FSM named by the spec; it is ALSO asserted on
//     (== LOAD after reset / before START), so a state bug cannot hide.
//   - rf_we/rf_waddr: the regfile's literal write-port inputs, used to
//     track "written since reset". Any spurious rf_we in the checked
//     window is itself an assert failure.
//   - reg_a_sel/reg_b_sel: only choose WHICH register the read-data
//     check observes; the claim "unwritten-since-reset reads 0" holds
//     for every index, and per-register covers show all four exercised.
// Everything else (pc, flag, retaddr, return_valid, illegal_op_flag,
// call_ret_misuse_flag, halted, pending_lx_writeback, ra/rb_data,
// rf_wdata) is only the object of an assert or part of a cover.
//
// Environment: the one assume is the standard "reset at step 0".
// rst_n is otherwise free every cycle -- mid-trace resets (the point of
// this file) are explored, and the landing covers below prove the
// solver really resets mid-DELAY, mid-WAIT, mid-FETCH_HI and right
// after a LOAD/LOADX commit. All other inputs (mem_rdata, start_rise,
// host_go_*, pin_read) are free.
//
// Scope: bounded BMC at the default depth 20 (deepest cover: a DELAY
// completing after a reset that landed mid-DELAY, ~14 steps).

module core_reset_props (
    input wire        clk,
    input wire        rst_n,
    input wire [9:0]  mem_addr,
    input wire        mem_we,
    input wire [7:0]  mem_rdata,
    input wire [2:0]  pin_index,
    input wire        set_en,
    input wire        outb_en,
    input wire        start_rise,
    input wire        host_go_rise,
    input wire        mode_load,
    input wire [7:0]  uo_out,
    input wire [1:0]  state,                 // DEBUG_PORTS
    input wire [8:0]  pc,                    // DEBUG_PORTS
    input wire        flag,                  // DEBUG_PORTS
    input wire [8:0]  retaddr,               // DEBUG_PORTS
    input wire        return_valid,          // DEBUG_PORTS
    input wire        illegal_op_flag,       // DEBUG_PORTS
    input wire        call_ret_misuse_flag,  // DEBUG_PORTS
    input wire        halted,                // DEBUG_PORTS
    input wire        pending_lx_writeback,  // DEBUG_PORTS
    input wire        rf_we,                 // DEBUG_PORTS
    input wire [1:0]  rf_waddr,              // DEBUG_PORTS
    input wire [7:0]  rf_wdata,              // DEBUG_PORTS
    input wire [1:0]  reg_a_sel,             // DEBUG_PORTS
    input wire [1:0]  reg_b_sel,             // DEBUG_PORTS
    input wire [7:0]  ra_data,               // DEBUG_PORTS
    input wire [7:0]  rb_data                // DEBUG_PORTS
);

  // FSM encoding per docs/architecture.md's state order.
  localparam S_LOAD     = 2'd0;
  localparam S_FETCH_LO = 2'd1;
  localparam S_FETCH_HI = 2'd2;
  localparam S_EXECUTE  = 2'd3;

  // docs/isa.md opcode values (the spec's table, not core.v's decode).
  localparam OP_DELAY = 5'd9;
  localparam OP_WAIT  = 5'd10;
  localparam OP_LOAD  = 5'd14;
  localparam OP_LOADX = 5'd18;

  // What the core was doing on the cycle just before a reset fell.
  localparam K_NONE  = 3'd0;
  localparam K_DELAY = 3'd1;   // EXECUTE of a DELAY, held past its first cycle
  localparam K_WAIT  = 3'd2;   // EXECUTE of a WAIT, held past its first cycle (blocked)
  localparam K_FHI   = 3'd3;   // FETCH_HI, low byte in flight
  localparam K_LX    = 3'd4;   // LOAD/LOADX commit cycle -> writeback pending next cycle
  localparam K_OTHER = 3'd5;

  // ======================================================================
  // Environment: a genuine reset at step 0 (async reset). Free after.
  // ======================================================================
  initial assume (!rst_n);

  // ----------------------------------------------------------------------
  // Post-reset ghost state (async reset, same reset as the DUT).
  // ----------------------------------------------------------------------
  reg       p_valid;     // previous cycle was out of reset
  reg [1:0] p_state;
  reg       seen_start;  // a start_rise has been seen since reset
  reg       seen_flo;    // a FETCH_LO has been seen since reset
  reg       exec_seen;   // an instruction has entered EXECUTE since reset
  reg [4:0] cur_op;      // opcode of the EXECUTE in progress
  reg [7:0] g_lo;        // low byte seen on mem_rdata during FETCH_HI
  reg [3:0] written;     // regfile index written since reset
  reg       dl_on;       // previous cycle was an EXECUTE cycle of a DELAY
  reg [5:0] dl_idx;      // ... and its index within that EXECUTE (0 = first)
  reg [23:0] dl_S;       // that DELAY's extra-cycle count, from the spec

  wire       entering = rst_n && p_valid && (p_state == S_FETCH_HI) && (state == S_EXECUTE);
  wire [4:0] g_op     = mem_rdata[7:3];
  wire       g_is_lx  = (g_op == OP_LOAD) || (g_op == OP_LOADX);
  wire       held     = rst_n && (state == S_EXECUTE) && !entering;

  // DELAY: mantissa = word[8:0] = {hi[0], lo}, exponent = word[10:9] = hi[2:1].
  // Unsized `* 5` (AGENT_CONTRACT.md: sized literal truncated once before).
  wire [23:0] S_now = {15'd0, mem_rdata[0], g_lo} << (mem_rdata[2:1] * 5);

  wire       cur_dl_on  = rst_n && ((entering && g_op == OP_DELAY) ||
                                    (dl_on && state == S_EXECUTE && !entering));
  wire [5:0] cur_dl_idx = entering ? 6'd0 : ((dl_idx == 6'd63) ? 6'd63 : dl_idx + 6'd1);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      p_valid    <= 1'b0;
      p_state    <= S_LOAD;
      seen_start <= 1'b0;
      seen_flo   <= 1'b0;
      exec_seen  <= 1'b0;
      cur_op     <= 5'd0;
      g_lo       <= 8'd0;
      written    <= 4'd0;
      dl_on      <= 1'b0;
      dl_idx     <= 6'd0;
      dl_S       <= 24'd0;
    end else begin
      p_valid <= 1'b1;
      p_state <= state;
      if (start_rise) seen_start <= 1'b1;
      if (state == S_FETCH_LO) seen_flo <= 1'b1;
      if (entering) begin
        exec_seen <= 1'b1;
        cur_op    <= g_op;
      end
      if (state == S_FETCH_HI) g_lo <= mem_rdata;
      if (rf_we) written[rf_waddr] <= 1'b1;
      dl_on  <= cur_dl_on;
      dl_idx <= cur_dl_idx;
      if (entering && g_op == OP_DELAY) dl_S <= S_now;
    end
  end

  // ----------------------------------------------------------------------
  // Cross-reset ghost state: plain (non-reset) flops, so they remember
  // what was going on when the reset fell. Used ONLY in covers.
  // ----------------------------------------------------------------------
  wire [2:0] kind_now =
      !rst_n                                ? K_NONE  :
      (state == S_FETCH_HI)                 ? K_FHI   :
      (entering && g_is_lx)                 ? K_LX    :
      (held && cur_op == OP_DELAY)          ? K_DELAY :
      (held && cur_op == OP_WAIT)           ? K_WAIT  :
                                              K_OTHER;

  reg       q_rstn       = 1'b0;
  reg [2:0] q_kind       = K_NONE;
  reg [2:0] rst_from     = K_NONE;  // kind at the most recent mid-trace reset
  reg [3:0] dirty        = 4'd0;    // register holds a nonzero value
  reg [3:0] dirty_at_rst = 4'd0;
  reg [8:0] g_fpc        = 9'd0;    // last fetch word address (port level)
  reg [8:0] fpc_at_rst   = 9'd0;

  wire rst_fall = !rst_n && q_rstn;

  always @(posedge clk) begin
    q_rstn <= rst_n;
    q_kind <= kind_now;
    if (rst_fall) begin
      rst_from     <= q_kind;
      dirty_at_rst <= dirty;
      fpc_at_rst   <= g_fpc;
    end
    if (!rst_n) dirty <= 4'd0;
    else if (rf_we) dirty[rf_waddr] <= (rf_wdata != 8'd0);
    if (rst_n && state == S_FETCH_LO) g_fpc <= mem_addr[9:1];
  end

  wire chk = rst_n && p_valid;

  // ======================================================================
  // Property 1 -- known state on the first cycle after any reset
  // (step 0 or mid-trace).
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n && !p_valid) begin
      cover (1'b1);
      cover (rst_from != K_NONE);   // a mid-trace reset, not just step 0
      assert (state == S_LOAD);
      assert (mode_load);
      assert (pc == 9'd0);
      assert (flag == 1'b0);
      assert (retaddr == 9'd0);
      assert (return_valid == 1'b0);
      assert (illegal_op_flag == 1'b0);
      assert (call_ret_misuse_flag == 1'b0);
      assert (halted == 1'b0);
      assert (pending_lx_writeback == 1'b0);
      assert (uo_out == 8'd0);
      assert (ra_data == 8'd0);
      assert (rb_data == 8'd0);
    end
  end

  // Property 1b -- R0-R3: any register not written since the last reset
  // reads 0 on either read port, at any later cycle. Covers show each
  // register read back after a reset that interrupted it holding a
  // nonzero value.
  always @(posedge clk) begin
    if (rst_n && !written[reg_a_sel]) assert (ra_data == 8'd0);
    if (rst_n && !written[reg_b_sel]) assert (rb_data == 8'd0);
    if (rst_n) begin
      cover (!written[0] && (reg_a_sel == 2'd0 || reg_b_sel == 2'd0) && dirty_at_rst[0]);
      cover (!written[1] && (reg_a_sel == 2'd1 || reg_b_sel == 2'd1) && dirty_at_rst[1]);
      cover (!written[2] && (reg_a_sel == 2'd2 || reg_b_sel == 2'd2) && dirty_at_rst[2]);
      cover (!written[3] && (reg_a_sel == 2'd3 || reg_b_sel == 2'd3) && dirty_at_rst[3]);
    end
  end

  // ======================================================================
  // Property 2 -- nothing executes before START: stay in LOAD; pin
  // requests only the HOST_STATUS ack on pin_index 5; no OUTB; memory
  // writes only on a host_go rising edge; no regfile write.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n && !seen_start) begin
      cover (p_valid);
      cover (mem_we);
      cover (p_valid && set_en);
      assert (state == S_LOAD);
      assert (mode_load);
      assert (!set_en || pin_index == 3'd5);
      assert (!outb_en);
      assert (!mem_we || host_go_rise);
      assert (!rf_we);
    end
    // and START really leaves LOAD the next cycle, straight to FETCH_LO
    if (chk && p_state == S_LOAD && seen_start) begin
      cover (1'b1);
      assert (state == S_FETCH_LO);
    end
  end

  // ======================================================================
  // Property 3 -- the first fetch after reset is from byte address 0,
  // whatever the pre-reset PC was.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n && state == S_FETCH_LO && !seen_flo) begin
      cover (1'b1);
      cover (fpc_at_rst != 9'd0);
      assert (mem_addr == 10'd0);
      assert (pc == 9'd0);
    end
  end

  // ======================================================================
  // Property 4 -- no leakage: from reset until the first post-reset
  // instruction enters EXECUTE, none of the pre-reset operation's
  // effects appear -- no regfile write (incl. a trailing LOAD/LOADX
  // writeback in the first FETCH_LO), no flag/return/debug-flag change,
  // no halt, no pin/memory side effect, no stray EXECUTE, fetch from
  // 0/1 with uo_out cleared.
  // ======================================================================
  wire fresh = rst_n && !exec_seen && !entering;

  always @(posedge clk) begin
    // Reset really lands in each interesting state.
    if (rst_fall) begin
      cover (q_kind == K_DELAY);
      cover (q_kind == K_WAIT);
      cover (q_kind == K_FHI);
      cover (q_kind == K_LX);
    end
    if (fresh) begin
      // ... and the checked window is then exercised through fetch.
      cover (state == S_FETCH_HI && rst_from == K_DELAY);
      cover (state == S_FETCH_HI && rst_from == K_WAIT);
      cover (state == S_FETCH_HI && rst_from == K_FHI);
      cover (state == S_FETCH_LO && rst_from == K_LX);
      assert (state != S_EXECUTE);
      assert (!rf_we);
      assert (!pending_lx_writeback);
      assert (flag == 1'b0);
      assert (return_valid == 1'b0);
      assert (retaddr == 9'd0);
      assert (illegal_op_flag == 1'b0);
      assert (call_ret_misuse_flag == 1'b0);
      assert (halted == 1'b0);
      assert (!outb_en);
      assert (!set_en || (state == S_LOAD && pin_index == 3'd5));
      assert (!mem_we || (state == S_LOAD && host_go_rise));
      if (state != S_LOAD) begin
        assert (pc == 9'd0);
        assert (uo_out == 8'd0);
        assert (mem_addr == {9'd0, state == S_FETCH_HI});
      end
    end
  end

  // Property 4b -- no stale countdown: every DELAY holds EXECUTE for
  // exactly S = mantissa<<(exp*5) extra cycles (checked within depth).
  // The covers pin this to a DELAY run after a reset that landed
  // mid-DELAY.
  wire [23:0] dl_idx_w = {18'd0, dl_idx};

  always @(posedge clk) begin
    if (chk && dl_on && dl_idx != 6'd63 && dl_idx_w < dl_S) begin
      cover (rst_from == K_DELAY);
      assert (state == S_EXECUTE);
    end
    if (chk && dl_on && dl_idx_w == dl_S) begin
      cover (rst_from == K_DELAY && dl_S != 24'd0);
      assert (state == S_FETCH_LO);
    end
  end

endmodule

bind protocol_cpu_core core_reset_props u_core_reset_props (
    .clk                 (clk),
    .rst_n               (rst_n),
    .mem_addr            (mem_addr),
    .mem_we              (mem_we),
    .mem_rdata           (mem_rdata),
    .pin_index           (pin_index),
    .set_en              (set_en),
    .outb_en             (outb_en),
    .start_rise          (start_rise),
    .host_go_rise        (host_go_rise),
    .mode_load           (mode_load),
    .uo_out              (uo_out),
    .state               (state),
    .pc                  (pc),
    .flag                (flag),
    .retaddr             (retaddr),
    .return_valid        (return_valid),
    .illegal_op_flag     (illegal_op_flag),
    .call_ret_misuse_flag(call_ret_misuse_flag),
    .halted              (halted),
    .pending_lx_writeback(pending_lx_writeback),
    .rf_we               (rf_we),
    .rf_waddr            (rf_waddr),
    .rf_wdata            (rf_wdata),
    .reg_a_sel           (reg_a_sel),
    .reg_b_sel           (reg_b_sel),
    .ra_data             (ra_data),
    .rb_data             (rb_data)
);
