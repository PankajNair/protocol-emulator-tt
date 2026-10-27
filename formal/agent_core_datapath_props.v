// agent_core_datapath_props.v
// Formal properties targeting src/cpu/core.v's register-file write
// VALUES: every regfile write carries exactly the value docs/isa.md
// defines for the instruction being executed, lands in the register the
// spec names, at the cycle the spec / docs/architecture.md names -- and
// no other opcode (and no other cycle) writes the regfile at all.
//
// Spec (docs/isa.md opcode table; word = {high byte, low byte},
// opcode = word[15:11], Rd = word[10:9], Rs = word[8:7]):
//   LDI   (4)  Rd <= word[7:0]
//   IN    (7)  Rd <= ui_in (synchronized parallel bus, all 8 bits)
//   SHIFT (8)  word[0]=0: Rd <= {Rd[6:0],0}; word[0]=1: Rd <= {0,Rd[7:1]}
//              ("vacated bit is always 0 -- no rotate")
//   INB   (13) pin_index = word[8:6], bit_select = word[5]
//              (isa_defs.v: 1 = bit7, 0 = bit0). Rd[bit] <= pin value,
//              other bits of Rd untouched.
//   LOOP  (16) Rd = word[1:0]; Rd <= Rd - 1 (mod 256)
//   LOAD  (14) Rd <= dmem[512 + word[8:0]]
//   LOADX (18) Rd <= dmem[512 + Rs]
//   XOR   (19) Rd <= Rd ^ Rs
//   ADD   (20) Rd <= Rd + Rs (mod 256)   (its carry goes to the flag,
//              checked in agent_core_loop_flag_props.v, not here)
//   every other opcode (NOP, HALT, RET, BRANCH, SET, OUT, DELAY, WAIT,
//   TEST-bit, OUTB, STORE, CMP, reserved 21-31): no register write.
//   (XOR/ADD added 2026-10-23 with the ISA change; before it this file
//   held 19-31 to no write.)
// Timing (docs/architecture.md Pipeline): single EXECUTE cycle for every
// writer above; LOAD/LOADX issue the data address during EXECUTE and
// their writeback "trails by exactly 1 cycle, into the following
// instruction's FETCH_LO", data = what the SRAM returns that cycle
// (mem_rdata). mem_rdata is a free input here: the property checks the
// address issued and that the returned byte is what lands in Rd.
//
// Toolchain constraint: plain immediate assertions inside
// `always @(posedge clk)` only (formal/scripts/formal_common.py).
//
// Independence: never reads core.v's `opcode`, `word`, `w_*`,
// `reg_w_*`, `exec_done`, `reg_a_sel`/`reg_b_sel` or `ra_data`/`rb_data`.
// The instruction is rebuilt from mem_rdata as fetched:
//   - `state` (DEBUG_PORTS): locates FETCH_HI (mem_rdata = low byte) and
//     the FETCH_HI->EXECUTE step (mem_rdata = high byte), as in
//     agent_core_illegal_opcode_props.v.
//   - Operand values come from a ghost regfile g_rf built from the
//     regfile's write port (rf_we/rf_waddr/rf_wdata), which is itself
//     the OBJECT of every assert below; g_rf is asserted equal to the
//     real regfile contents (CHILD_DEBUG_PORTS rf_regs) every cycle, so
//     it cannot drift unnoticed.
//   - ir/ir_lo/entering_execute/halted/pending_lx_writeback/
//     pending_lx_rd are asserted on only (k-induction strengthening),
//     never used to compute an expected value.
//
// Environment: the one assume is "reset at step 0". rst_n and every
// other input are free afterwards (mid-trace resets explored).

module core_datapath_props (
    input wire        clk,
    input wire        rst_n,
    input wire [9:0]  mem_addr,
    input wire [7:0]  mem_rdata,
    input wire [7:0]  ui_in_sync,
    input wire        pin_read,
    input wire [2:0]  pin_index,
    input wire [1:0]  state,       // DEBUG_PORTS
    input wire        rf_we,       // DEBUG_PORTS
    input wire [1:0]  rf_waddr,    // DEBUG_PORTS
    input wire [7:0]  rf_wdata,    // DEBUG_PORTS
    // k-induction auxiliary-invariant observation only:
    input wire        halted,               // DEBUG_PORTS
    input wire [15:0] ir,                   // DEBUG_PORTS
    input wire [7:0]  ir_lo,                // DEBUG_PORTS
    input wire        entering_execute,     // DEBUG_PORTS
    input wire        pending_lx_writeback, // DEBUG_PORTS
    input wire [1:0]  pending_lx_rd,        // DEBUG_PORTS
    input wire [31:0] rf_regs               // CHILD_DEBUG_PORTS ({R3,R2,R1,R0})
);

  localparam S_LOAD     = 2'd0;
  localparam S_FETCH_LO = 2'd1;
  localparam S_FETCH_HI = 2'd2;
  localparam S_EXECUTE  = 2'd3;

  // docs/isa.md opcode table.
  localparam OP_NOP     = 5'd0;
  localparam OP_HALT    = 5'd1;
  localparam OP_BRANCH  = 5'd3;
  localparam OP_LDI     = 5'd4;
  localparam OP_IN      = 5'd7;
  localparam OP_SHIFT   = 5'd8;
  localparam OP_DELAY   = 5'd9;
  localparam OP_WAIT    = 5'd10;
  localparam OP_OUTB    = 5'd12;
  localparam OP_INB     = 5'd13;
  localparam OP_LOAD    = 5'd14;
  localparam OP_STORE   = 5'd15;
  localparam OP_LOOP    = 5'd16;
  localparam OP_CMP     = 5'd17;
  localparam OP_LOADX   = 5'd18;
  localparam OP_XOR     = 5'd19;
  localparam OP_ADD     = 5'd20;

  // ======================================================================
  // Environment: genuine reset at step 0 (async reset). Free after.
  // ======================================================================
  initial assume (!rst_n);

  // ----------------------------------------------------------------------
  // Instruction identification (port level, from mem_rdata as fetched).
  // ----------------------------------------------------------------------
  reg       p_valid;
  reg [1:0] p_state;
  reg [7:0] g_lo;        // low byte, mem_rdata during FETCH_HI
  reg [4:0] cur_op_r;    // opcode of the EXECUTE in progress (after 1st cycle)

  wire        entering = rst_n && p_valid && (p_state == S_FETCH_HI) && (state == S_EXECUTE);
  wire [15:0] g_word   = {mem_rdata, g_lo};
  wire [4:0]  g_op     = g_word[15:11];
  wire [1:0]  g_rd     = g_word[10:9];
  wire [1:0]  g_rs     = g_word[8:7];
  wire [8:0]  g_imm    = g_word[8:0];
  wire [7:0]  g_ldi    = g_word[7:0];
  wire [2:0]  g_pin    = g_word[8:6];
  wire        g_bit7   = g_word[5];       // INB bit_select: 1 = bit7, 0 = bit0
  wire        g_right  = g_word[0];       // SHIFT direction: 1 = right
  wire [1:0]  g_lrd    = g_word[1:0];     // LOOP counter register

  wire [4:0]  cur_op   = entering ? g_op : cur_op_r;
  wire        in_exec  = rst_n && (state == S_EXECUTE);

  // ----------------------------------------------------------------------
  // Ghost regfile, built from the regfile's write port (reset 0).
  // ----------------------------------------------------------------------
  reg [7:0] g_rf [0:3];

  wire [7:0] old_rd = g_rf[g_rd];
  wire [7:0] old_lp = g_rf[g_lrd];
  wire [7:0] val_rs = g_rf[g_rs];

  wire c_ldi   = entering && (g_op == OP_LDI);
  wire c_in    = entering && (g_op == OP_IN);
  wire c_shift = entering && (g_op == OP_SHIFT);
  wire c_inb   = entering && (g_op == OP_INB);
  wire c_loop  = entering && (g_op == OP_LOOP);
  wire c_load  = entering && (g_op == OP_LOAD);
  wire c_loadx = entering && (g_op == OP_LOADX);
  wire c_xor   = entering && (g_op == OP_XOR);
  wire c_add   = entering && (g_op == OP_ADD);

  // Trailing-writeback bookkeeping: previous cycle was a LOAD / LOADX
  // EXECUTE (single cycle, commits on entry).
  reg       p_lx, p_load, p_loadx;
  reg [1:0] p_lx_rd;
  reg [7:0] p_lx_old;    // Rd's value before the writeback (cover only)

  // ----------------------------------------------------------------------
  // Spec-defined expected write for this cycle.
  // ----------------------------------------------------------------------
  reg       exp_we;
  reg [1:0] exp_addr;
  reg [7:0] exp_data;

  always @(*) begin
    exp_we   = 1'b0;
    exp_addr = g_rd;
    exp_data = 8'd0;
    if (c_ldi) begin
      exp_we = 1'b1; exp_data = g_ldi;
    end else if (c_in) begin
      exp_we = 1'b1; exp_data = ui_in_sync;
    end else if (c_shift) begin
      exp_we = 1'b1;
      exp_data = g_right ? (old_rd >> 1) : (old_rd << 1);
    end else if (c_inb) begin
      exp_we = 1'b1;
      exp_data = g_bit7 ? {pin_read, old_rd[6:0]} : {old_rd[7:1], pin_read};
    end else if (c_loop) begin
      exp_we = 1'b1; exp_addr = g_lrd; exp_data = old_lp - 8'd1;
    end else if (c_xor) begin
      exp_we = 1'b1; exp_data = old_rd ^ val_rs;
    end else if (c_add) begin
      exp_we = 1'b1; exp_data = old_rd + val_rs;
    end else if (rst_n && p_lx && state == S_FETCH_LO) begin
      exp_we = 1'b1; exp_addr = p_lx_rd; exp_data = mem_rdata;
    end
  end

  integer i;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      p_valid  <= 1'b0;
      p_state  <= 2'd0;
      g_lo     <= 8'd0;
      cur_op_r <= 5'd0;
      for (i = 0; i < 4; i = i + 1) g_rf[i] <= 8'd0;
      p_lx     <= 1'b0;
      p_load   <= 1'b0;
      p_loadx  <= 1'b0;
      p_lx_rd  <= 2'd0;
      p_lx_old <= 8'd0;
    end else begin
      p_valid  <= 1'b1;
      p_state  <= state;
      if (state == S_FETCH_HI) g_lo <= mem_rdata;
      cur_op_r <= cur_op;
      if (rf_we) g_rf[rf_waddr] <= rf_wdata;
      p_lx     <= c_load || c_loadx;
      p_load   <= c_load;
      p_loadx  <= c_loadx;
      p_lx_rd  <= g_rd;
      p_lx_old <= old_rd;
    end
  end

  // ======================================================================
  // Property 1 -- write enable: the regfile is written on exactly the
  // cycles the spec defines (writer EXECUTE cycle, or the FETCH_LO right
  // after LOAD/LOADX) and on no other cycle -- no other opcode, no
  // DELAY/WAIT holding cycle, no FETCH_HI/LOAD-mode cycle.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n) begin
      cover (entering && g_op == OP_STORE);
      cover (entering && g_op == OP_OUTB);
      cover (entering && g_op == OP_CMP);
      cover (entering && g_op == OP_BRANCH);
      cover (entering && g_op == OP_NOP);
      cover (entering && g_op >= 5'd21);
      cover (in_exec && !entering && cur_op_r == OP_DELAY);
      cover (state == S_FETCH_LO && !p_lx);
      cover (state == S_LOAD && p_valid);
      assert (rf_we == exp_we);
    end
  end

  // ======================================================================
  // Property 2 -- write address and value equal the spec's, per opcode.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n && exp_we) begin
      assert (rf_waddr == exp_addr);
      assert (rf_wdata == exp_data);
    end
  end

  // Per-path covers on the same antecedent (exp_we), each chosen so the
  // spec value differs from the obvious wrong variant.
  always @(posedge clk) begin
    if (rst_n) begin
      cover (c_ldi && g_ldi != old_rd && g_rd != 2'd0);
      cover (c_in && ui_in_sync != old_rd);
      // SHIFT left with bit7 set: rotate vs zero-fill differ.
      cover (c_shift && !g_right && old_rd[7]);
      // SHIFT right with bit0 set.
      cover (c_shift &&  g_right && old_rd[0]);
      // INB bit7 / bit0, pin value differs from the bit it replaces and
      // the other bits are nonzero (keep-vs-clobber visible).
      cover (c_inb &&  g_bit7 &&  pin_read && !old_rd[7] && old_rd[6:0] != 7'd0);
      cover (c_inb &&  g_bit7 && !pin_read &&  old_rd[7]);
      cover (c_inb && !g_bit7 &&  pin_read && !old_rd[0] && old_rd[7:1] != 7'd0);
      cover (c_inb && !g_bit7 && !pin_read &&  old_rd[0]);
      // XOR: Rd = Rs (clears) and Rd != Rs with differing values;
      // ADD: with and without carry out, and Rd = Rs (doubles).
      cover (c_xor && g_rd == g_rs && old_rd != 8'd0);
      cover (c_xor && g_rd != g_rs && old_rd != val_rs && val_rs != 8'd0);
      cover (c_add && g_rd != g_rs && ({1'b0, old_rd} + {1'b0, val_rs}) > 9'd255);
      cover (c_add && g_rd != g_rs && ({1'b0, old_rd} + {1'b0, val_rs}) <= 9'd255 && val_rs != 8'd0);
      cover (c_add && g_rd == g_rs && old_rd != 8'd0);
      cover (c_loop && old_lp == 8'd0);
      cover (c_loop && old_lp >  8'd1);
      // Trailing writebacks.
      cover (state == S_FETCH_LO && p_load  && mem_rdata != p_lx_old);
      cover (state == S_FETCH_LO && p_loadx && mem_rdata != p_lx_old);
    end
  end

  // ======================================================================
  // Property 3 -- LOAD/LOADX data address (docs/isa.md: 512+imm,
  // 512+Rs) issued during their EXECUTE cycle, so the byte returned the
  // next cycle is the one the spec names. INB reads the pin its field
  // names.
  // ======================================================================
  always @(posedge clk) begin
    if (c_load) begin
      cover (g_imm != 9'd0);
      assert (mem_addr == 10'd512 + {1'b0, g_imm});
    end
    if (c_loadx) begin
      cover (val_rs != 8'd0 && g_rs != g_rd);
      assert (mem_addr == 10'd512 + {2'b0, val_rs});
    end
    if (c_inb) begin
      cover (g_pin != 3'd0);
      assert (pin_index == g_pin);
    end
  end

  // ======================================================================
  // Auxiliary invariants for k-induction (make prove). Each links ghost
  // state to the core's real state and is itself proven.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n) begin
      cover (pending_lx_writeback);
      assert (rf_regs == {g_rf[3], g_rf[2], g_rf[1], g_rf[0]});
      assert (ir_lo == g_lo);
      assert (!halted || state == S_EXECUTE);
      assert (!halted || !entering_execute);
      assert (!entering_execute || state == S_EXECUTE);
      assert (p_valid || state == S_LOAD);
      assert (pending_lx_writeback == (p_lx && state == S_FETCH_LO));
      if (pending_lx_writeback) assert (pending_lx_rd == p_lx_rd);
    end
    if (rst_n && p_valid && state == S_EXECUTE) begin
      cover (!entering_execute);
      assert (entering_execute == (p_state == S_FETCH_HI));
    end
    if (in_exec && !entering) begin
      cover (cur_op_r == OP_WAIT);
      cover (cur_op_r == OP_HALT);
      assert (ir[15:11] == cur_op_r);
      assert (cur_op_r == OP_DELAY || cur_op_r == OP_WAIT || cur_op_r == OP_HALT);
    end
  end

endmodule

bind protocol_cpu_core core_datapath_props u_core_datapath_props (
    .clk       (clk),
    .rst_n     (rst_n),
    .mem_addr  (mem_addr),
    .mem_rdata (mem_rdata),
    .ui_in_sync(ui_in_sync),
    .pin_read  (pin_read),
    .pin_index (pin_index),
    .state     (state),
    .rf_we     (rf_we),
    .rf_waddr  (rf_waddr),
    .rf_wdata  (rf_wdata),
    .halted    (halted),
    .ir        (ir),
    .ir_lo     (ir_lo),
    .entering_execute    (entering_execute),
    .pending_lx_writeback(pending_lx_writeback),
    .pending_lx_rd       (pending_lx_rd),
    .rf_regs   (u_regfile.regs)
);
