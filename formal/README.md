# Formal verification

Competition judging criteria explicitly call out verification methodology
(formal methods, AI-assisted techniques), so this gets its own directory
rather than being buried in `test/`.

**Toolchain, running for real**: plain `yosys` (0.69) + `yosys-smtbmc` + `z3`
(4.16.0), no SymbiYosys/Verific -- ported from the sibling
`5-Stage-Pipelined-RISC-V-Processor` project's own confirmed-working
formal flow (same install, same recipe). Full derivation of every step
(why `async2sync`/`chformal -lower`/`dffunmap` are load-bearing, why
`bind` doesn't work under this yosys build and what replaces it) is in
`formal/scripts/formal_common.py`'s module docstring -- read that before
writing a new properties file, and `formal/AGENT_CONTRACT.md` for the
per-project conventions and write-scope.

```
make formal   [TARGET_PROPS=formal/<file>.v]   # BMC assert pass -- does every assert hold
make vacuity  [TARGET_PROPS=formal/<file>.v]   # cover-reachability pass -- is every assert's antecedent actually reachable
```

Properties files: `formal/agent_<target>_<property>_props.v`, plain
immediate assertions inside `always @(posedge clk)` blocks (concurrent
SVA doesn't parse under this yosys build's frontend), bound to their
target module via a trailing `bind` statement (documentation-only for
the real run -- `run_formal.py`/`vacuity_check.py` generate their own
sibling-instantiation wrapper instead, since `bind` is parsed but never
elaborated on this yosys build).

## Status

- **`cycle_counter.v` DELAY/WAIT timing -- done, proven.**
  `agent_cycle_counter_timing_props.v`: two properties (no-undercount,
  the named candidate below; and a tighter full-equality timing
  property that also catches over-counting/getting-stuck) against an
  independent ghost-model reconstruction of the counter's expected
  `expired` timing. Both `make formal` and `make vacuity` clean (2/2
  covers reached, both asserts PASSED). Confirmed genuinely
  bug-catching, not vacuous: caught a real width-truncation bug in the
  checker itself during development (`exponent * 3'd5` silently
  truncating the multiply), and separately confirmed both properties
  FAIL against a deliberately broken copy of the DUT (decrement-by-2
  instead of decrement-by-1) before being trusted -- see that file's
  own header and `formal/AGENT_CONTRACT.md` for the discipline this
  followed.

- **`pin_ctrl.v` pin direction + uio[6] contention gate -- done, proven.**
  `agent_pin_ctrl_direction_props.v` (assertion-formal agent, gated in
  the parent session): no uio[6] drive in LOAD or before a real START
  fall, `cover(uio_oe[6])`, fixed-role pins 4/5, protocol-pin oe only
  per sticky mode and never in LOAD, sticky mode vs. a ghost model.
  31/31 covers reached. Re-injecting the real HOST_ERROR bug fixed in
  142ab71 makes the assert FAIL and the `uio_oe[6]` cover UNREACHED --
  this file would have caught that bug without simulation.

- **`core.v` illegal-opcode detection -- done, proven.**
  `agent_core_illegal_opcode_props.v` (assertion-formal agent): the
  sticky `illegal_op_flag` rises only after a reserved-opcode (19-31)
  commit, always rises after one, stays set until reset, and a
  reserved-opcode commit is a true NOP (no mem write, no pin request, no
  regfile write, PC+1, other flags and `uo_out` unchanged). The
  committing opcode is read from `mem_rdata` at the FETCH_HI->EXECUTE
  step, never from core.v's own decode. 9/9 covers. Agent-side scratch
  mutants: mutate.py #8, flag-never-set, LDI-also-sets -- each fails.

- **`core.v` reset -- done, proven.** `agent_core_reset_props.v`
  (assertion-formal agent): first cycle after any reset (including
  mid-trace) is LOAD with every documented reset value; nothing executes,
  drives a pin or writes memory (except host boot writes) before START;
  first fetch is byte 0; and no effect of an interrupted DELAY, blocked
  WAIT, FETCH_HI or pending LOAD/LOADX writeback leaks past reset (a
  cover per scenario proves reset really lands there). 22/22 covers.
  Scratch mutants each fail: dropped pc/return_valid reset, surviving
  pending writeback (sharpened so only the real leak path can fail),
  reset into FETCH_LO, regfile without reset.

- **`core.v` CALL/RET misuse flag -- done, proven.**
  `agent_core_call_ret_misuse_props.v` (assertion-formal agent): ghost
  `return_valid`/`retaddr` built only from identified CALL/RET commits
  must match the core every cycle; the sticky misuse flag rises only after
  a nested CALL or orphan RET and always after one, stays set until reset;
  CALL saves PC+1 and jumps, RET (orphan included) jumps to `retaddr`.
  17/17 covers. Scratch mutants each fail: mutate.py #4 and #5, RET not
  clearing `return_valid`, CALL saving PC instead of PC+1.

- **`core.v` LOOP and the flag-writer set -- done, proven.**
  `agent_core_loop_flag_props.v` (assertion-formal agent): a ghost
  regfile built from the write port is checked against both read ports
  every cycle; any commit other than CMP/TEST-bit/WAIT leaves the flag
  unchanged (LOOP and SHIFT covered with flag 0 and 1); LOOP writes
  Rd-1 mod 256 and branches iff the result is nonzero (wrap from 0
  covered); CMP, TEST-bit and WAIT write the value the spec says, using
  ghost operands. 22/22 covers. Scratch mutants each fail: mutate.py
  #11, SHIFT writes flag, LOOP taken iff flag, LOOP decrements by 2, CMP
  inverted.

## Candidate properties, not yet written

None open from the original list. Natural next target: WAIT's met-wins-on-expiry rule at the
core level (the cycle_counter file proves the counter, not the rule).

## Rest of the verification environment

Coverage model, mutation-testing gate, golden model, stimulus profiles
and the three agent contracts are now in place -- see
[../docs/VAL.md](../docs/VAL.md).
