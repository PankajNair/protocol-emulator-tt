// agent_core_wait_props.v
// Formal properties targeting src/cpu/core.v's WAIT exit timing and the
// met-wins-on-expiry rule, end to end at the core level (formal/README.md
// candidate "WAIT met-wins-on-expiry at the core level").
//
// Spec: docs/isa.md WAIT row. imm = pin_index(3) + level(1) +
// timeout_mantissa(5); Rd(2) = timeout exponent; cycles = mantissa <<
// (exponent*5). Computed-zero cycles (i.e. mantissa 0, any exponent) =
// unbounded. Exit flag = 0 if pin==level was met, 1 on timeout; if both
// land on the same cycle, condition-met wins. docs/architecture.md
// Pipeline: WAIT stays in EXECUTE, 3+N cycles total (FETCH_LO + FETCH_HI
// + N+1 EXECUTE cycles), so with k = 0 on the first EXECUTE cycle the
// timeout exit is the EXECUTE cycle with k == N.
// Field packing (isa_defs.v, word = {high byte, low byte}):
//   opcode=word[15:11] exp=word[10:9] pin=word[8:6] level=word[5]
//   mantissa=word[4:0]
//
// Toolchain constraint: plain immediate assertions inside
// `always @(posedge clk)` only (formal/scripts/formal_common.py).
//
// Independence: never reads core.v's `opcode`, `word`, `ir`, `ir_lo`,
// `w_*`, `entering_execute`, `exec_done`, `wait_pending`,
// `wait_condition_met`, `wait_has_timeout`, `cnt_*`, or any
// cycle_counter internal. The target cycle count is recomputed from the
// spec formula with an UNSIZED `* 5` (see agent_cycle_counter_timing
// _props.v for the sized-literal truncation lesson). k is the checker's
// own count of cycles since the FETCH_HI->EXECUTE step. Internals read:
//   - `state`: the architectural FSM the spec names. Used to locate
//     FETCH_HI (mem_rdata = low byte, mem_addr = PC*2+1) and the
//     FETCH_HI->EXECUTE step (mem_rdata = high byte). Otherwise it is the
//     OBJECT of the exit-timing asserts (EXECUTE held / FETCH_LO next).
//     Identification needs only "one FETCH_HI followed by EXECUTE";
//     WHEN EXECUTE is left is never derived from `state`.
//   - `pc`, `flag`, `rf_we`: asserted on only (`flag`'s own previous
//     value is the frame reference for "unchanged").
//   - ports `pin_read` (free input), `mem_rdata` (free input),
//     `mem_addr`, `mem_we`, `set_en`, `outb_en`, `pin_index`.
//
// Environment: the one assume is "reset at step 0". rst_n, pin_read,
// mem_rdata, start/host inputs are free afterwards (mid-trace resets
// explored; ghost state resets with the DUT).
//
// Scope: bounded BMC at the default depth 20. Reset + LOAD exit + fetch
// consume ~3 steps, so a single WAIT's EXECUTE window reaches k ~ 16.
// Timeout exits are therefore only reached for exponent 0, mantissa
// <= ~16; exponent >= 1 (target >= 32) is only exercised as "must still
// be waiting" (no early exit), never as a reached timeout. The encoding
// space itself is NOT clipped by any assume.

module core_wait_props (
    input wire        clk,
    input wire        rst_n,
    input wire [9:0]  mem_addr,
    input wire [7:0]  mem_rdata,
    input wire        mem_we,
    input wire [2:0]  pin_index,
    input wire        set_en,
    input wire        outb_en,
    input wire        pin_read,
    input wire [1:0]  state,       // DEBUG_PORTS
    input wire [8:0]  pc,          // DEBUG_PORTS
    input wire        flag,        // DEBUG_PORTS
    input wire        rf_we        // DEBUG_PORTS
);

  localparam S_FETCH_LO = 2'd1;
  localparam S_FETCH_HI = 2'd2;
  localparam S_EXECUTE  = 2'd3;

  localparam OP_WAIT = 5'd10;   // docs/isa.md opcode table

  // ======================================================================
  // Environment: genuine reset at step 0 (async reset). Free after.
  // ======================================================================
  initial assume (!rst_n);

  // ----------------------------------------------------------------------
  // Instruction identification (port level).
  // ----------------------------------------------------------------------
  reg       p_valid;
  reg [1:0] p_state;
  reg [7:0] g_lo;     // low byte: mem_rdata during FETCH_HI
  reg [8:0] g_fpc;    // mem_addr[9:1] during FETCH_HI

  wire        entering = rst_n && p_valid && (p_state == S_FETCH_HI) && (state == S_EXECUTE);
  wire [15:0] g_word   = {mem_rdata, g_lo};
  wire        ent_wait = entering && (g_word[15:11] == OP_WAIT);

  // ----------------------------------------------------------------------
  // Spec model of one WAIT's EXECUTE window.
  // ----------------------------------------------------------------------
  reg        w_act_r;   // spec says the WAIT is still in EXECUTE this cycle
  reg [23:0] k_r;
  reg        w_lvl_r;
  reg [4:0]  w_mant_r;
  reg [1:0]  w_exp_r;
  reg [2:0]  w_pin_r;
  reg [8:0]  w_pc_r;

  wire        act  = ent_wait || (rst_n && w_act_r);
  wire [23:0] k    = ent_wait ? 24'd0       : k_r;
  wire        lvl  = ent_wait ? g_word[5]   : w_lvl_r;
  wire [4:0]  mant = ent_wait ? g_word[4:0] : w_mant_r;
  wire [1:0]  expn = ent_wait ? g_word[10:9] : w_exp_r;
  wire [2:0]  pinf = ent_wait ? g_word[8:6] : w_pin_r;
  wire [8:0]  wpc  = ent_wait ? g_fpc       : w_pc_r;

  // Unsized `5` on purpose (32-bit product, no truncation).
  wire [31:0] target    = {27'd0, mant} << (expn * 5);
  wire        met       = (pin_read == lvl);
  wire        timed_out = (mant != 5'd0) && ({8'd0, k} >= target);
  wire        exit_now  = met || timed_out;
  wire        tie       = met && timed_out;

  // Previous-cycle snapshots.
  reg        p_act, p_exit, p_met, p_tie, p_flag, p_mant0;
  reg [1:0]  p_exp;
  reg [4:0]  p_mant;
  reg [8:0]  p_pc;
  reg [23:0] p_k;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      p_valid  <= 1'b0;
      p_state  <= 2'd0;
      g_lo     <= 8'd0;
      g_fpc    <= 9'd0;
      w_act_r  <= 1'b0;
      k_r      <= 24'd0;
      w_lvl_r  <= 1'b0;
      w_mant_r <= 5'd0;
      w_exp_r  <= 2'd0;
      w_pin_r  <= 3'd0;
      w_pc_r   <= 9'd0;
      p_act    <= 1'b0;
      p_exit   <= 1'b0;
      p_met    <= 1'b0;
      p_tie    <= 1'b0;
      p_flag   <= 1'b0;
      p_mant0  <= 1'b0;
      p_exp    <= 2'd0;
      p_mant   <= 5'd0;
      p_pc     <= 9'd0;
      p_k      <= 24'd0;
    end else begin
      p_valid <= 1'b1;
      p_state <= state;
      if (state == S_FETCH_HI) begin
        g_lo  <= mem_rdata;
        g_fpc <= mem_addr[9:1];
      end
      w_act_r  <= act && !exit_now;
      k_r      <= k + 24'd1;
      w_lvl_r  <= lvl;
      w_mant_r <= mant;
      w_exp_r  <= expn;
      w_pin_r  <= pinf;
      w_pc_r   <= wpc;
      p_act    <= act;
      p_exit   <= exit_now;
      p_met    <= met;
      p_tie    <= tie;
      p_flag   <= flag;
      p_mant0  <= (mant == 5'd0);
      p_exp    <= expn;
      p_mant   <= mant;
      p_pc     <= wpc;
      p_k      <= k;
    end
  end

  wire chk = rst_n && p_valid;

  // ======================================================================
  // Property 1 -- exit timing. (a) never early: every cycle of the spec
  // window the core is in EXECUTE. (b) never late: the cycle after the
  // first k with met || (mant != 0 && k >= target), the core is back in
  // FETCH_LO fetching PC+1.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n && act) begin
      cover (!exit_now && k >= 24'd2 && mant != 5'd0);
      cover (!exit_now && k >= 24'd2 && expn != 2'd0 && mant != 5'd0);
      assert (state == S_EXECUTE);
    end
    if (chk && p_act && p_exit) begin
      cover (!p_met && p_mant >= 5'd3);                   // pure timeout, k = 3+
      cover (!p_met && p_mant == 5'd1);
      cover ( p_met && p_k >= 24'd3 && p_mant != 5'd0);   // met before timeout
      cover ( p_met && p_k == 24'd0);                     // met on first cycle
      assert (state == S_FETCH_LO);
      assert (pc == p_pc + 9'd1);
      assert (mem_addr == {p_pc + 9'd1, 1'b0});
    end
  end

  // ======================================================================
  // Property 2 -- exit flag and tie rule: flag = 0 if met, 1 if timed
  // out; on a tie (met and expiry first true on the same cycle) flag = 0.
  // ======================================================================
  always @(posedge clk) begin
    if (chk && p_act && p_exit) begin
      cover (!p_met &&  !p_flag);
      cover ( p_met &&   p_flag);
      assert (flag == !p_met);
    end
    if (chk && p_act && p_exit && p_tie) begin
      cover (p_flag && p_exp == 2'd0 && p_mant >= 5'd2);  // real tie at k = mant >= 2
      cover (p_flag && p_mant == 5'd1);
      assert (flag == 1'b0);
      assert (state == S_FETCH_LO);
    end
  end

  // ======================================================================
  // Property 3 -- unbounded: mantissa 0 (any exponent) and pin != level
  // => still in EXECUTE next cycle, flag unchanged.
  // ======================================================================
  always @(posedge clk) begin
    if (chk && p_act && p_mant0 && !p_met) begin
      cover (p_k >= 24'd12 && p_exp != 2'd0);
      cover (p_k >= 24'd4  && p_exp == 2'd3 && p_flag);
      cover (p_k >= 24'd4  && p_exp == 2'd0);
      assert (state == S_EXECUTE);
      assert (flag == p_flag);
    end
    // Long mantissa-0 wait that only ends when the pin matches.
    if (chk && p_act && p_exit && p_mant0) begin
      cover (p_k >= 24'd10 && p_exp != 2'd0);
      assert (p_met);            // spec-model sanity: mant 0 only exits on met
      assert (flag == 1'b0);
    end
  end

  // ======================================================================
  // Property 4 -- no side effects while WAIT occupies EXECUTE (including
  // its exit cycle): no regfile / mem write, no SET/OUTB pin request, PC
  // held, pin_index selects the WAIT's own pin, flag held until exit.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n && act) begin
      cover (k >= 24'd3 && pinf != 3'd0);
      assert (!rf_we);
      assert (!mem_we);
      assert (!set_en);
      assert (!outb_en);
      assert (pc == wpc);
      assert (pin_index == pinf);
    end
    if (chk && p_act && !p_exit) begin
      cover (p_flag);
      assert (flag == p_flag);
    end
  end

endmodule

bind protocol_cpu_core core_wait_props u_core_wait_props (
    .clk      (clk),
    .rst_n    (rst_n),
    .mem_addr (mem_addr),
    .mem_rdata(mem_rdata),
    .mem_we   (mem_we),
    .pin_index(pin_index),
    .set_en   (set_en),
    .outb_en  (outb_en),
    .pin_read (pin_read),
    .state    (state),
    .pc       (pc),
    .flag     (flag),
    .rf_we    (rf_we)
);
