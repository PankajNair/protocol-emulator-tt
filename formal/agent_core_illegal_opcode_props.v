// agent_core_illegal_opcode_props.v
// Formal properties targeting src/cpu/core.v's illegal-opcode detection
// -- the "illegal-opcode detection" candidate in formal/README.md.
//
// Spec: docs/isa.md "Undefined opcode behavior" + opcode table row
// `19-31` (reserved encodings execute as NOP and set a sticky flag,
// cleared only by reset; "Reset value: all registers 0"), and
// docs/architecture.md "FETCH_LO / FETCH_HI / EXECUTE" (FETCH_HI issues
// the high-byte read at PC*2+1, the SRAM has 1-cycle read latency, so
// EXECUTE's first cycle sees the high byte -- opcode = word[15:11] =
// high_byte[7:3] -- on mem_rdata; NOP is "uniform 3 cycles", i.e. one
// EXECUTE cycle, then back to FETCH_LO).
//
// Toolchain constraint: plain immediate assertions inside
// `always @(posedge clk)` only -- no concurrent SVA under this yosys
// build (formal/scripts/formal_common.py docstring).
//
// Independence (see agent_cycle_counter_timing_props.v's header for the
// ghost-model-fitted-to-the-DUT failure this avoids): the checker never
// reads core.v's `opcode`, `word`, `ir`, `entering_execute`,
// `opcode_recognized` or `exec_done`. "Which instruction is committing
// and what is its opcode" is rebuilt from:
//   - `state` (DEBUG_PORTS): used ONLY to locate the FETCH_HI->EXECUTE
//     transition, i.e. which cycle mem_rdata carries a high byte. It is
//     the architectural FSM the spec itself names, not decode logic.
//   - `mem_rdata[7:3]` (port) on that cycle: the opcode, per the spec's
//     1-cycle SRAM latency. mem_rdata is a free input here, so the
//     solver can present any of the 32 opcodes -- nothing assumed away.
//   - `mem_addr[9:1]` (port) during FETCH_HI: the instruction's address,
//     used to check the next fetch goes to address+1 at port level.
// Everything else read from DEBUG_PORTS (illegal_op_flag, flag, pc,
// return_valid, retaddr, call_ret_misuse_flag, halted, rf_we) is only
// ever the OBJECT of an assertion, never part of an antecedent that
// decides whether an assertion fires -- so a DUT bug in any of them
// cannot also silently disarm the check on it.
//
// Scope: bounded-depth BMC (DEFAULT_DEPTH=20), not an inductive proof.
// Regfile contents aren't exposed; "no register write" is checked via
// the regfile's write-enable (`rf_we`) in the commit cycle and in the
// following FETCH_LO (where a deferred LOAD/LOADX writeback would land).
// The only assume is the standard "reset happens at step 0".

module core_illegal_opcode_props (
    input wire        clk,
    input wire        rst_n,
    input wire [9:0]  mem_addr,
    input wire        mem_we,
    input wire [7:0]  mem_rdata,
    input wire        set_en,
    input wire        outb_en,
    input wire [7:0]  uo_out,
    input wire [1:0]  state,                 // DEBUG_PORTS
    input wire        flag,                  // DEBUG_PORTS
    input wire        illegal_op_flag,       // DEBUG_PORTS
    input wire        call_ret_misuse_flag,  // DEBUG_PORTS
    input wire        halted,                // DEBUG_PORTS
    input wire [8:0]  pc,                    // DEBUG_PORTS
    input wire        return_valid,          // DEBUG_PORTS
    input wire [8:0]  retaddr,               // DEBUG_PORTS
    input wire        rf_we                  // DEBUG_PORTS
);

  // FSM encoding per docs/architecture.md's state order (LOAD, FETCH_LO,
  // FETCH_HI, EXECUTE).
  localparam S_FETCH_LO = 2'd1;
  localparam S_FETCH_HI = 2'd2;
  localparam S_EXECUTE  = 2'd3;

  // ======================================================================
  // Environment: a genuine reset occurs at step 0 (async reset; without
  // this all DUT/ghost state starts at solver garbage). rst_n is
  // otherwise free, so mid-trace resets are explored too.
  // ======================================================================
  initial assume (!rst_n);

  // ----------------------------------------------------------------------
  // Ghost model: previous-cycle snapshots, all from ports / observed
  // values, reset to 0.
  // ----------------------------------------------------------------------
  reg       p_valid;      // previous cycle was out of reset
  reg [1:0] p_state;
  reg       p_rexec;      // previous cycle was a reserved-opcode commit
  reg [8:0] g_fetch_pc;   // mem_addr[9:1] seen during the last FETCH_HI
  reg [8:0] p_fetch_pc;
  reg       p_ill, p_flag, p_rv, p_crm;
  reg [8:0] p_pc, p_retaddr;
  reg [7:0] p_uo;

  // First EXECUTE cycle of an instruction: high byte is on mem_rdata.
  wire       entering = p_valid && (p_state == S_FETCH_HI) && (state == S_EXECUTE);
  wire [4:0] g_op     = mem_rdata[7:3];
  wire       r_exec   = rst_n && entering && (g_op >= 5'd19);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      p_valid    <= 1'b0;
      p_state    <= 2'd0;
      p_rexec    <= 1'b0;
      g_fetch_pc <= 9'd0;
      p_fetch_pc <= 9'd0;
      p_ill      <= 1'b0;
      p_flag     <= 1'b0;
      p_rv       <= 1'b0;
      p_crm      <= 1'b0;
      p_pc       <= 9'd0;
      p_retaddr  <= 9'd0;
      p_uo       <= 8'd0;
    end else begin
      p_valid    <= 1'b1;
      p_state    <= state;
      p_rexec    <= r_exec;
      if (state == S_FETCH_HI) g_fetch_pc <= mem_addr[9:1];
      p_fetch_pc <= g_fetch_pc;
      p_ill      <= illegal_op_flag;
      p_flag     <= flag;
      p_rv       <= return_valid;
      p_crm      <= call_ret_misuse_flag;
      p_pc       <= pc;
      p_retaddr  <= retaddr;
      p_uo       <= uo_out;
    end
  end

  // Both this cycle and the previous one are out of reset.
  wire chk = rst_n && p_valid;

  // ======================================================================
  // Property 1 -- soundness: the flag rises only on the cycle right
  // after a reserved-opcode commit.
  // ======================================================================
  always @(posedge clk) begin
    if (chk && !p_ill && illegal_op_flag) begin
      cover (1'b1);
      assert (p_rexec);
    end
  end

  // ======================================================================
  // Property 2 -- completeness: a reserved-opcode commit sets the flag,
  // visible the next cycle. Boundary covers: 19 (lowest reserved,
  // mutate.py #8's edge) and 31 (highest).
  // ======================================================================
  always @(posedge clk) begin
    if (r_exec) begin
      cover (g_op == 5'd19);
      cover (g_op == 5'd31);
    end
    if (chk && p_rexec) begin
      cover (1'b1);
      assert (illegal_op_flag);
    end
  end

  // ======================================================================
  // Property 3 -- stickiness + reset value.
  // ======================================================================
  always @(posedge clk) begin
    if (chk && p_ill) begin
      cover (1'b1);
      assert (illegal_op_flag);
    end
    // first cycle after a reset: flag is 0
    if (rst_n && !p_valid) begin
      cover (1'b1);
      assert (!illegal_op_flag);
    end
  end

  // ======================================================================
  // Property 4 -- NOP behavior. (4a) in the commit cycle itself: no
  // memory write, no pin request, no regfile write. (4b) on the next
  // cycle: straight back to FETCH_LO (one EXECUTE cycle), not halted,
  // PC+1 (both the pc register and the port-level fetch address), and
  // flag / return_valid / retaddr / call_ret_misuse_flag / uo_out
  // unchanged, no deferred regfile writeback.
  // ======================================================================
  always @(posedge clk) begin
    if (r_exec) begin
      cover (1'b1);
      assert (!mem_we);
      assert (!set_en);
      assert (!outb_en);
      assert (!rf_we);
    end
    if (chk && p_rexec) begin
      cover (p_flag);
      cover (p_rv);
      assert (state == S_FETCH_LO);
      assert (!halted);
      assert (pc == p_pc + 9'd1);
      assert (mem_addr == {p_fetch_pc + 9'd1, 1'b0});
      assert (flag == p_flag);
      assert (return_valid == p_rv);
      assert (retaddr == p_retaddr);
      assert (call_ret_misuse_flag == p_crm);
      assert (uo_out == p_uo);
      assert (!rf_we);
    end
  end

endmodule

bind protocol_cpu_core core_illegal_opcode_props u_core_illegal_opcode_props (
    .clk                 (clk),
    .rst_n               (rst_n),
    .mem_addr            (mem_addr),
    .mem_we              (mem_we),
    .mem_rdata           (mem_rdata),
    .set_en              (set_en),
    .outb_en             (outb_en),
    .uo_out              (uo_out),
    .state               (state),
    .flag                (flag),
    .illegal_op_flag     (illegal_op_flag),
    .call_ret_misuse_flag(call_ret_misuse_flag),
    .halted              (halted),
    .pc                  (pc),
    .return_valid        (return_valid),
    .retaddr             (retaddr),
    .rf_we               (rf_we)
);
