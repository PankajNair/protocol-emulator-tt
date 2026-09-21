// agent_cycle_counter_timing_props.v
// Formal properties targeting src/io/cycle_counter.v's DELAY/WAIT
// countdown timing -- the candidate property named in this directory's
// README ("WAIT/cycle-counter never undercounts").
//
// Toolchain constraint (confirmed working syntax for this project's
// yosys-native formal flow -- no SymbiYosys, no Verific, see
// formal/scripts/formal_common.py's own docstring): concurrent SVA
// (`assert property`, `|->`, `##N`) does NOT parse under
// `read_verilog -sv`. Every property below is a plain immediate
// assertion inside an `always @(posedge clk)` block.
//
// Method: cycle_counter.v's own internal `count` register isn't a
// port, so this checker tracks an independent ghost model --
// `target`/`elapsed`, updated by the SAME rule as the DUT's `count`
// register (loaded fresh on `load`, held/incremented otherwise) but
// computed completely independently from the checker's own copy of
// the DUT's port-level inputs, never by reading DUT-internal state.
// This is a black-box check: it does not just re-assert the DUT's own
// `assign` statement, it independently re-derives when `expired`
// should fire and compares.
//
// Ghost-model timing was hand-derived against src/io/cycle_counter.v's
// exact update rule (its own header comment explains why `expired`
// looks at the about-to-be-loaded value during a `load` cycle, not the
// stale registered `count` -- a same-cycle-load-visibility fix found
// while wiring DELAY's exit condition in core.v). `dist_now` counts
// cycles since (and including) `load`, incrementing every cycle with
// no flat period: `elapsed` is seeded to 1 (not 0) on `load`, since by
// the very next cycle exactly one full cycle will have passed.
//
// This ghost model went through a real revision, not just a first
// derivation: an earlier version seeded `elapsed` to 0 on `load` (flat
// for 2 cycles before incrementing), matching what turned out to be a
// genuine off-by-one BUG in cycle_counter.v itself at the time (DELAY
// with mantissa>=1 cost one cycle more than the documented "3+N"
// property promises -- found by a cocotb functional test measuring
// real elapsed cycles, test/test.py's test_delay_timing, NOT by this
// formal property, which had PASSED against the buggy DUT because its
// ghost model was unknowingly fit to match the bug rather than
// independently derived from the "3+N" spec). After fixing
// cycle_counter.v (pre-decrementing on load: `count <= shifted - 1`,
// guarded at 0), this ghost model was corrected to match -- re-verified
// against a from-scratch Python re-derivation of both the fixed DUT
// and this exact ghost-model formula before trusting it again. Real
// lesson for any future props file here: a ghost model derived BY
// READING the DUT's own update logic (rather than independently from
// its documented spec) can silently prove the implementation matches
// itself, not that it matches the spec -- functional/cocotb testing
// and formal verification here are complementary for exactly this
// reason, neither replaces the other.
//
// Two properties, same ghost model: no-undercount (this file's named
// candidate property) and the tighter full-equality timing property
// (implies no-undercount, and also catches an over-count/stuck-false
// bug the weaker property alone would miss). Both get their own
// `cover` so vacuity-checking treats them as distinct antecedents.
//
// Scope: bounded-depth BMC (formal/scripts/formal_common.py
// DEFAULT_DEPTH), not an unbounded inductive proof -- a `target` value
// larger than the BMC depth is never actually reached to `expired==1`
// within a single trace, so this only exercises small target values
// directly. `mantissa`/`exponent` are otherwise fully free/unconstrained
// primary inputs (this checker imposes no `assume` narrowing them), so
// small-target coverage is still adversarially chosen by the solver,
// not a hand-picked test vector.
//
// Bound externally via `bind` at the bottom of this file (never written
// inline into src/io/cycle_counter.v itself).

module cycle_counter_timing_props (
    input wire        clk,
    input wire        rst_n,
    input wire        load,
    input wire [8:0]  mantissa,
    input wire [1:0]  exponent,
    input wire        expired
);

  // ======================================================================
  // Environment assumption -- a genuine reset actually occurs in the BMC
  // window. Without this, `target`/`elapsed`/`armed` below (and the DUT's
  // own `count`) simply start at solver-picked garbage at step 0, unrelated
  // to either module's real reset logic -- same category of fix as the
  // sibling RISC-V project's rename_waw_war_props.sv.
  // ======================================================================
  initial assume (!rst_n);

  // ----------------------------------------------------------------------
  // Ghost model.
  // ----------------------------------------------------------------------
  reg [23:0] target;
  reg [23:0] elapsed;
  reg        armed;

  // Unsized `5` deliberately, not e.g. `3'd5` -- a sized narrow literal
  // here truncates the multiply's *result* width to the operand's own
  // width before the shift ever sees it (confirmed the hard way: an
  // earlier draft used `exponent * 3'd5`, computing exponent=2's
  // `2*5=10` as a 3-bit product -- silently truncated to `3'b010`=2,
  // giving a shift amount of 2 instead of 10. cycle_counter.v itself
  // avoids this by using the unsized `TIMEOUT_SHIFT` macro; mirrored
  // here with a bare unsized literal for the exact same reason).
  wire [23:0] target_next = {15'd0, mantissa} << (exponent * 5);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      target  <= 24'd0;
      elapsed <= 24'd0;
      armed   <= 1'b0;
    end else if (load) begin
      target  <= target_next;
      elapsed <= 24'd1;  // by the next cycle, exactly 1 cycle has passed since load
      armed   <= 1'b1;
    end else if (armed) begin
      elapsed <= elapsed + 24'd1;
    end
  end

  // This cycle's true (target, elapsed) pair, load-adjusted -- see
  // header comment for why the registered values alone aren't valid
  // on the load cycle itself (they update at the END of this cycle).
  wire [23:0] target_now  = load ? target_next : target;
  wire [23:0] dist_now    = load ? 24'd0 : elapsed;
  wire        checked_now = load || armed;

  // ======================================================================
  // Property 1 (named candidate, formal/README.md) -- no undercount:
  // `expired` must never assert before `dist_now` cycles have actually
  // elapsed relative to `target_now`.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n && checked_now) begin
      cover (expired);
      assert (!(expired && (dist_now < target_now)));
    end
  end

  // ======================================================================
  // Property 2 -- exact timing (subsumes Property 1, also catches
  // over-counting / getting permanently stuck at expired==0): `expired`
  // is true iff `dist_now` has reached `target_now`.
  // ======================================================================
  always @(posedge clk) begin
    if (rst_n && checked_now) begin
      cover (dist_now >= target_now);
      assert (expired == (dist_now >= target_now));
    end
  end

endmodule

bind cycle_counter cycle_counter_timing_props u_cycle_counter_timing_props (
    .clk     (clk),
    .rst_n   (rst_n),
    .load    (load),
    .mantissa(mantissa),
    .exponent(exponent),
    .expired (expired)
);
