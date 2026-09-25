// agent_core_call_ret_misuse_props.v
// Formal properties targeting src/cpu/core.v's CALL/RET misuse
// diagnosability -- the "CALL/RET misuse-flag soundness/completeness"
// candidate in formal/README.md.
//
// Spec: docs/isa.md "Instruction formats" (opcode = word[15:11], fields
// MSB-first; low byte fetched in FETCH_LO at PC*2, high byte in FETCH_HI
// at PC*2+1), "Branch format -- opcode(5) | addr(9) | cond(2)" (so
// addr = word[10:2], cond = word[1:0]; cond 11 = CALL: push PC+1 to the
// depth-1 return register, then jump), opcode table (RET = 2 jumps to the
// return-address register, BRANCH = 3), and "Both CALL/RET misuse
// hazards above are now diagnosable": return-valid is set by CALL,
// cleared by RET, reset 0; the sticky misuse flag (reset-only clear) is
// set by a CALL while return-valid=1 or a RET while return-valid=0.
// docs/architecture.md: return-address reset value 0.
//
// Toolchain constraint: plain immediate assertions inside
// `always @(posedge clk)` only (formal/scripts/formal_common.py).
//
// Independence: never reads core.v's `opcode`, `word`, `ir`, `ir_lo`,
// `w_cond`, `w_addr`, `is_call`, `is_ret`, `branch_taken`, `next_pc`,
// `entering_execute` or `exec_done`. The committing instruction is
// rebuilt at port level, as in agent_core_illegal_opcode_props.v:
//   - `state` (DEBUG_PORTS): used ONLY to locate FETCH_HI (mem_rdata =
//     low byte, mem_addr = PC*2+1) and the FETCH_HI->EXECUTE step
//     (mem_rdata = high byte). Architectural FSM named by the spec, not
//     decode; it is also asserted on (back to FETCH_LO after the commit).
//   - `mem_rdata` (free input): word = {high, low}; opcode/addr/cond from
//     the spec's bit positions.
//   - `mem_addr[9:1]` during FETCH_HI: the instruction's own word address.
// The ghost return-valid (g_rv) and ghost return address (g_ra) are
// built only from those identified commits. `return_valid`, `retaddr`,
// `pc`, `halted` are only ever the OBJECT of asserts. The only
// antecedent use of `call_ret_misuse_flag` is its own previous value in
// the soundness/stickiness checks on that same flag (a rise / a hold of
// the object itself), exactly the pattern in the illegal-opcode file.
//
// Environment: the one assume is "reset at step 0". rst_n, start_rise
// and mem_rdata are otherwise free (mid-trace resets explored).
// Scope: bounded BMC at the default depth 20. Deepest cover (orphan RET
// jumping to a non-zero retaddr: CALL, RET, RET) commits by ~step 10.

module core_call_ret_misuse_props (
    input wire        clk,
    input wire        rst_n,
    input wire [9:0]  mem_addr,
    input wire [7:0]  mem_rdata,
    input wire [1:0]  state,                 // DEBUG_PORTS
    input wire [8:0]  pc,                    // DEBUG_PORTS
    input wire        return_valid,          // DEBUG_PORTS
    input wire [8:0]  retaddr,               // DEBUG_PORTS
    input wire        call_ret_misuse_flag,  // DEBUG_PORTS
    input wire        halted                 // DEBUG_PORTS
);

  // FSM encoding per docs/architecture.md's state order.
  localparam S_FETCH_LO = 2'd1;
  localparam S_FETCH_HI = 2'd2;
  localparam S_EXECUTE  = 2'd3;

  // docs/isa.md opcode table / Branch format cond table.
  localparam OP_RET    = 5'd2;
  localparam OP_BRANCH = 5'd3;
  localparam COND_CALL = 2'b11;

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

  wire        entering = rst_n && p_valid && (p_state == S_FETCH_HI) && (state == S_EXECUTE);
  wire [15:0] g_word   = {mem_rdata, g_lo};
  wire [4:0]  g_op     = g_word[15:11];
  wire [8:0]  g_addr   = g_word[10:2];
  wire [1:0]  g_cond   = g_word[1:0];
  wire        c_call   = entering && (g_op == OP_BRANCH) && (g_cond == COND_CALL);
  wire        c_ret    = entering && (g_op == OP_RET);

  // ----------------------------------------------------------------------
  // Ghost state, from identified commits only.
  // ----------------------------------------------------------------------
  reg       g_rv;        // ghost return-valid
  reg [8:0] g_ra;        // ghost return address

  wire mis_call = c_call &&  g_rv;   // nested CALL
  wire mis_ret  = c_ret  && !g_rv;   // orphan RET

  // Previous-cycle snapshots.
  reg       p_call, p_ret, p_mis_call, p_mis_ret, p_crm, p_rv_before;
  reg [8:0] p_target, p_fpc, p_ra_before;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      p_valid     <= 1'b0;
      p_state     <= 2'd0;
      g_lo        <= 8'd0;
      g_fpc       <= 9'd0;
      g_rv        <= 1'b0;
      g_ra        <= 9'd0;
      p_call      <= 1'b0;
      p_ret       <= 1'b0;
      p_mis_call  <= 1'b0;
      p_mis_ret   <= 1'b0;
      p_crm       <= 1'b0;
      p_rv_before <= 1'b0;
      p_target    <= 9'd0;
      p_fpc       <= 9'd0;
      p_ra_before <= 9'd0;
    end else begin
      p_valid <= 1'b1;
      p_state <= state;
      if (state == S_FETCH_HI) begin
        g_lo  <= mem_rdata;
        g_fpc <= mem_addr[9:1];
      end
      if (c_call) begin
        g_rv <= 1'b1;
        g_ra <= g_fpc + 9'd1;
      end else if (c_ret) begin
        g_rv <= 1'b0;
      end
      p_call      <= c_call;
      p_ret       <= c_ret;
      p_mis_call  <= mis_call;
      p_mis_ret   <= mis_ret;
      p_crm       <= call_ret_misuse_flag;
      p_rv_before <= g_rv;
      p_target    <= g_addr;
      p_fpc       <= g_fpc;
      p_ra_before <= g_ra;
    end
  end

  wire chk   = rst_n && p_valid;
  wire p_mis = p_mis_call || p_mis_ret;

  // ======================================================================
  // Property 1 -- DUT return_valid matches the ghost built from commits;
  // likewise the return-address register (reset 0, written only by CALL).
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n) begin
      cover (g_rv);
      cover (p_valid && !g_rv && p_ret);   // cleared by a RET
      assert (return_valid == g_rv);
      assert (retaddr == g_ra);
    end
  end

  // ======================================================================
  // Property 2 -- soundness: the flag rises only on the cycle right after
  // a misusing commit.
  // ======================================================================
  always @(posedge clk) begin
    if (chk && !p_crm && call_ret_misuse_flag) begin
      cover (1'b1);
      assert (p_mis);
    end
  end

  // ======================================================================
  // Property 3 -- completeness: every misusing commit sets the flag,
  // visible the next cycle. Separate covers per kind.
  // ======================================================================
  always @(posedge clk) begin
    if (chk && p_mis) begin
      cover (p_mis_call);
      cover (p_mis_ret);
      cover (p_mis_call && !p_crm);   // nested CALL is the FIRST misuse
      cover (p_mis_ret  && !p_crm);   // orphan RET is the FIRST misuse
      assert (call_ret_misuse_flag);
    end
  end

  // ======================================================================
  // Property 4 -- stickiness + reset value.
  // ======================================================================
  always @(posedge clk) begin
    if (chk && p_crm) begin
      cover (1'b1);
      cover (!p_mis);                  // held across a non-misusing cycle
      assert (call_ret_misuse_flag);
    end
    if (rst_n && !p_valid) begin
      cover (1'b1);
      assert (!call_ret_misuse_flag);
      assert (!return_valid);
      assert (retaddr == 9'd0);
    end
  end

  // ======================================================================
  // Property 5 -- control flow. CALL: one EXECUTE cycle, next fetch at its
  // target, retaddr = own PC+1, return_valid = 1. RET: next fetch at the
  // return address (ghost value, incl. orphan RET), return_valid = 0,
  // retaddr unchanged. A correct CALL/RET leaves the flag untouched.
  // ======================================================================
  always @(posedge clk) begin
    if (chk && p_call) begin
      cover (1'b1);
      cover (p_target != p_fpc + 9'd1);
      assert (state == S_FETCH_LO);
      assert (!halted);
      assert (pc == p_target);
      assert (mem_addr == {p_target, 1'b0});
      assert (retaddr == p_fpc + 9'd1);
      assert (return_valid);
    end
    if (chk && p_ret) begin
      cover (1'b1);
      cover (p_mis_ret && p_ra_before != 9'd0);  // orphan RET, stale non-zero retaddr
      cover (p_mis_ret && p_ra_before == 9'd0);  // orphan RET straight after reset
      assert (state == S_FETCH_LO);
      assert (!halted);
      assert (pc == p_ra_before);
      assert (mem_addr == {p_ra_before, 1'b0});
      assert (retaddr == p_ra_before);
      assert (!return_valid);
    end
    // Correct usage is not misuse.
    if (chk && (p_call || p_ret) && !p_mis) begin
      cover (p_call);
      cover (p_ret && p_rv_before && !p_crm);    // matched CALL...RET, clean flag
      assert (call_ret_misuse_flag == p_crm);
    end
  end

endmodule

bind protocol_cpu_core core_call_ret_misuse_props u_core_call_ret_misuse_props (
    .clk                 (clk),
    .rst_n               (rst_n),
    .mem_addr            (mem_addr),
    .mem_rdata           (mem_rdata),
    .state               (state),
    .pc                  (pc),
    .return_valid        (return_valid),
    .retaddr             (retaddr),
    .call_ret_misuse_flag(call_ret_misuse_flag),
    .halted              (halted)
);
