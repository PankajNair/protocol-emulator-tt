// agent_core_loop_flag_props.v
// Formal properties targeting src/cpu/core.v's LOOP semantics and the
// shared-flag writer set -- the "LOOP never writes the flag" candidate in
// formal/README.md.
//
// Spec: docs/isa.md opcode table -- LOOP (16) "decrement Rd, branch if
// nonzero. Does not touch the shared flag"; SHIFT (8) "does not touch the
// shared flag"; CMP (17) flag = (Rd==Rs); TEST-bit (11) flag = Rd[bit];
// WAIT (10) writes flag on exit, 0 if the pin condition was met, 1 on
// timeout (condition-met wins on the expiry cycle). Branch format section:
// the flag-writers are CMP, TEST-bit and WAIT and nothing else.
// Formats (word = {high byte, low byte}, opcode = word[15:11]):
//   LOOP     opcode | addr(9)=word[10:2] | Rd(2)=word[1:0]
//   CMP      opcode | Rd=word[10:9] | Rs=word[8:7] | reserved
//   TEST-bit opcode | Rd=word[10:9] | imm; bit_index = word[2:0]
//   WAIT     opcode | exp=word[10:9] | pin(3)=word[8:6] | level=word[5] | mant
//
// Toolchain constraint: plain immediate assertions inside
// `always @(posedge clk)` only (formal/scripts/formal_common.py).
//
// Independence: never reads core.v's `opcode`, `word`, `ir`, `ir_lo`,
// `w_*`, `loop_taken`, `loop_result`, `next_pc`, `branch_taken`,
// `entering_execute`, `exec_done`, `reg_w_*`. Internals read, and why
// they are not circular:
//   - `state`: architectural FSM named by the spec. Locates FETCH_HI
//     (mem_rdata = low byte, mem_addr = PC*2+1) and the FETCH_HI->EXECUTE
//     step (mem_rdata = high byte), as in the other core props files; it
//     is also asserted on (LOOP returns to FETCH_LO in one cycle).
//   - `rf_we`/`rf_waddr`/`rf_wdata`: the regfile's literal write-port
//     inputs. They build a ghost copy of R0-R3 (g_rf). The ghost is
//     itself cross-checked every cycle against BOTH read ports
//     (ra_data == g_rf[reg_a_sel], rb_data == g_rf[reg_b_sel]), so it
//     cannot drift from the real regfile unnoticed. At a LOOP commit the
//     write port is the OBJECT of the counter assert.
//   - `reg_a_sel`/`reg_b_sel`: only select which ghost entry the read-port
//     consistency check compares against. The LOOP / CMP / TEST-bit
//     reference values use g_rf indexed by the register field decoded
//     from mem_rdata -- never by the core's own mux choice. The core's
//     LOOP mux choice is separately asserted (reg_a_sel == field).
//   - `flag`: asserted on; its own previous value is the frame reference.
//   - `pin_read` (free input), `pc`, `mem_addr`: port/asserted only.
//
// Environment: the one assume is "reset at step 0". rst_n and all other
// inputs are free afterwards (mid-trace resets explored).
// Scope: bounded BMC at the default depth 20.

module core_loop_flag_props (
    input wire        clk,
    input wire        rst_n,
    input wire [9:0]  mem_addr,
    input wire [7:0]  mem_rdata,
    input wire        pin_read,
    input wire [1:0]  state,       // DEBUG_PORTS
    input wire [8:0]  pc,          // DEBUG_PORTS
    input wire        flag,        // DEBUG_PORTS
    input wire        rf_we,       // DEBUG_PORTS
    input wire [1:0]  rf_waddr,    // DEBUG_PORTS
    input wire [7:0]  rf_wdata,    // DEBUG_PORTS
    input wire [1:0]  reg_a_sel,   // DEBUG_PORTS
    input wire [1:0]  reg_b_sel,   // DEBUG_PORTS
    input wire [7:0]  ra_data,     // DEBUG_PORTS
    input wire [7:0]  rb_data      // DEBUG_PORTS
);

  localparam S_FETCH_LO = 2'd1;
  localparam S_FETCH_HI = 2'd2;
  localparam S_EXECUTE  = 2'd3;

  // docs/isa.md opcode table.
  localparam OP_BRANCH  = 5'd3;
  localparam OP_LDI     = 5'd4;
  localparam OP_SHIFT   = 5'd8;
  localparam OP_WAIT    = 5'd10;
  localparam OP_TESTBIT = 5'd11;
  localparam OP_LOOP    = 5'd16;
  localparam OP_CMP     = 5'd17;

  // ======================================================================
  // Environment: genuine reset at step 0 (async reset). Free after.
  // ======================================================================
  initial assume (!rst_n);

  // ----------------------------------------------------------------------
  // Commit identification (port level).
  // ----------------------------------------------------------------------
  reg       p_valid;
  reg [1:0] p_state;
  reg [7:0] g_lo;        // low byte, mem_rdata during FETCH_HI
  reg [8:0] g_fpc;       // mem_addr[9:1] during FETCH_HI
  reg [4:0] cur_op_r;    // opcode of the EXECUTE in progress (after 1st cycle)
  reg       cur_lvl_r;   // WAIT level bit of the EXECUTE in progress

  wire        entering = rst_n && p_valid && (p_state == S_FETCH_HI) && (state == S_EXECUTE);
  wire [15:0] g_word   = {mem_rdata, g_lo};
  wire [4:0]  g_op     = g_word[15:11];
  wire [8:0]  g_addr   = g_word[10:2];
  wire [1:0]  g_lrd    = g_word[1:0];     // LOOP counter register
  wire [1:0]  g_rd     = g_word[10:9];    // CMP / TEST-bit Rd
  wire [1:0]  g_rs     = g_word[8:7];     // CMP Rs
  wire [2:0]  g_bit    = g_word[2:0];     // TEST-bit index

  wire [4:0]  cur_op   = entering ? g_op : cur_op_r;
  wire        cur_lvl  = entering ? g_lo[5] : cur_lvl_r;
  wire        in_exec  = rst_n && (state == S_EXECUTE);
  wire        is_writer = (cur_op == OP_CMP) || (cur_op == OP_TESTBIT) || (cur_op == OP_WAIT);

  // ----------------------------------------------------------------------
  // Ghost regfile, built from the regfile's write port (reset 0).
  // ----------------------------------------------------------------------
  reg [7:0] g_rf [0:3];

  wire [7:0] old_lp   = g_rf[g_lrd];
  wire [7:0] dec_lp   = old_lp - 8'd1;
  wire       c_loop   = entering && (g_op == OP_LOOP);
  wire       c_cmp    = entering && (g_op == OP_CMP);
  wire       c_test   = entering && (g_op == OP_TESTBIT);
  wire       c_shift  = entering && (g_op == OP_SHIFT);

  // Previous-cycle snapshots.
  reg       p_flag, p_exec, p_writer, p_loop, p_cmp, p_test, p_shift, p_wait, p_lvl, p_pin;
  reg [4:0] p_op;
  reg [7:0] p_old;
  reg [8:0] p_target, p_fpc;
  reg       p_exp_flag;   // expected flag after a CMP / TEST-bit commit
  reg       p_cmp_same;   // CMP had Rd == Rs (field-wise)

  integer i;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      p_valid    <= 1'b0;
      p_state    <= 2'd0;
      g_lo       <= 8'd0;
      g_fpc      <= 9'd0;
      cur_op_r   <= 5'd0;
      cur_lvl_r  <= 1'b0;
      for (i = 0; i < 4; i = i + 1) g_rf[i] <= 8'd0;
      p_flag     <= 1'b0;
      p_exec     <= 1'b0;
      p_writer   <= 1'b0;
      p_loop     <= 1'b0;
      p_cmp      <= 1'b0;
      p_test     <= 1'b0;
      p_shift    <= 1'b0;
      p_wait     <= 1'b0;
      p_lvl      <= 1'b0;
      p_pin      <= 1'b0;
      p_op       <= 5'd0;
      p_old      <= 8'd0;
      p_target   <= 9'd0;
      p_fpc      <= 9'd0;
      p_exp_flag <= 1'b0;
      p_cmp_same <= 1'b0;
    end else begin
      p_valid <= 1'b1;
      p_state <= state;
      if (state == S_FETCH_HI) begin
        g_lo  <= mem_rdata;
        g_fpc <= mem_addr[9:1];
      end
      cur_op_r  <= cur_op;
      cur_lvl_r <= cur_lvl;
      if (rf_we) g_rf[rf_waddr] <= rf_wdata;
      p_flag     <= flag;
      p_exec     <= in_exec;
      p_writer   <= in_exec && is_writer;
      p_op       <= cur_op;
      p_loop     <= c_loop;
      p_cmp      <= c_cmp;
      p_test     <= c_test;
      p_shift    <= c_shift;
      p_wait     <= in_exec && (cur_op == OP_WAIT);
      p_lvl      <= cur_lvl;
      p_pin      <= pin_read;
      p_old      <= old_lp;
      p_target   <= (dec_lp != 8'd0) ? g_addr : (g_fpc + 9'd1);
      p_fpc      <= g_fpc;
      p_exp_flag <= c_cmp ? (g_rf[g_rd] == g_rf[g_rs]) : g_rf[g_rd][g_bit];
      p_cmp_same <= (g_rd == g_rs);
    end
  end

  wire chk = rst_n && p_valid;

  // ======================================================================
  // Property 0 -- ghost regfile tracks the real one (both read ports).
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n) begin
      cover (in_exec && ra_data != 8'd0);
      assert (ra_data == g_rf[reg_a_sel]);
      assert (rb_data == g_rf[reg_b_sel]);
    end
  end

  // ======================================================================
  // Property 1 -- frame: the flag can only change on the cycle after an
  // EXECUTE cycle of CMP / TEST-bit / WAIT. Every other commit (LOOP,
  // SHIFT, BRANCH, reserved, ...) and every non-EXECUTE cycle holds it.
  // ======================================================================
  always @(posedge clk) begin
    if (chk && !p_writer) begin
      cover (p_loop  && !p_flag);
      cover (p_loop  &&  p_flag);
      cover (p_shift && !p_flag);
      cover (p_shift &&  p_flag);
      cover (p_exec && p_op == OP_BRANCH &&  p_flag);
      cover (p_exec && p_op >= 5'd19     &&  p_flag);
      assert (flag == p_flag);
    end
  end

  // ======================================================================
  // Property 2 -- LOOP counter: writes its own register (low 2 bits of
  // the word) with old-1 mod 256, in its single EXECUTE cycle, reading
  // the old value through read port A.
  // ======================================================================
  always @(posedge clk) begin
    if (c_loop) begin
      cover (old_lp == 8'd0);
      cover (old_lp == 8'd1);
      cover (old_lp >  8'd1 && g_lrd != 2'd0);
      assert (reg_a_sel == g_lrd);
      assert (ra_data == old_lp);
      assert (rf_we);
      assert (rf_waddr == g_lrd);
      assert (rf_wdata == dec_lp);
    end
  end

  // ======================================================================
  // Property 3 -- LOOP branch: next fetch at addr if old-1 != 0, else at
  // PC+1. One EXECUTE cycle.
  // ======================================================================
  always @(posedge clk) begin
    if (chk && p_loop) begin
      cover (p_old == 8'd1 && p_target != p_fpc + 9'd1 + 9'd1);    // falls through
      cover (p_old >  8'd1 && p_target != p_fpc + 9'd1);           // taken
      cover (p_old == 8'd0 && p_target != p_fpc + 9'd1);           // wrap: taken
      cover (p_old == 8'd0 && p_target != p_fpc + 9'd1 && !p_flag);
      cover (p_old == 8'd1 && p_flag);
      assert (state == S_FETCH_LO);
      assert (pc == p_target);
      assert (mem_addr == {p_target, 1'b0});
    end
  end

  // ======================================================================
  // Property 4 -- the named writers do write. CMP: flag = (Rd == Rs);
  // TEST-bit: flag = Rd[bit]; WAIT (on its exit cycle): flag = !(pin ==
  // level), i.e. 0 if met, 1 on timeout. Reference values come from the
  // ghost regfile indexed by the fields in mem_rdata.
  // ======================================================================
  always @(posedge clk) begin
    if (chk && p_cmp) begin
      cover (!p_cmp_same &&  p_exp_flag);
      cover (!p_cmp_same && !p_exp_flag &&  p_flag);
      cover (!p_cmp_same &&  p_exp_flag && !p_flag);
      assert (flag == p_exp_flag);
    end
    if (chk && p_test) begin
      cover ( p_exp_flag && !p_flag);
      cover (!p_exp_flag &&  p_flag);
      assert (flag == p_exp_flag);
    end
    if (chk && p_wait && state == S_FETCH_LO) begin
      cover (p_pin == p_lvl &&  p_flag);
      cover (p_pin != p_lvl && !p_flag);
      assert (flag == (p_pin != p_lvl));
    end
  end

endmodule

bind protocol_cpu_core core_loop_flag_props u_core_loop_flag_props (
    .clk      (clk),
    .rst_n    (rst_n),
    .mem_addr (mem_addr),
    .mem_rdata(mem_rdata),
    .pin_read (pin_read),
    .state    (state),
    .pc       (pc),
    .flag     (flag),
    .rf_we    (rf_we),
    .rf_waddr (rf_waddr),
    .rf_wdata (rf_wdata),
    .reg_a_sel(reg_a_sel),
    .reg_b_sel(reg_b_sel),
    .ra_data  (ra_data),
    .rb_data  (rb_data)
);
