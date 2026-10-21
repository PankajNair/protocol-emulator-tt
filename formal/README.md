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

- **All 8 props files are proven unbounded (k-induction, `make prove`)**
  since 2026-10-17; see docs/VAL.md section 6. Strengthen with
  auxiliary invariants only -- `scripts/assert_superset.py` rejects any
  removed/edited assert or new assume.

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

- **`pin_ctrl.v` synchronizer depth -- done, proven.**
  `agent_pin_ctrl_sync_depth_props.v` (assertion-formal agent, ledger run
  20261004-092239): every consumer of ui_in/uio_in sees exactly the
  2-cycle-old value -- `ui_in_sync`, the `pin_read` tap (WAIT/INB), the
  START/HOST_GO edge outputs -- plus the post-reset window. 66/66
  covers. Fails on a 1-flop `pin_read` tap and on an extra flop in both
  paths (the common-mode change a sim-calibrated `SYNC_DELAY` can't see).

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

- **`core.v` WAIT exit timing + met-wins-on-expiry -- done, proven.**
  `agent_core_wait_props.v` (assertion-formal agent): the core leaves
  EXECUTE on exactly the first k where the pin matches or (mantissa != 0
  and k >= mantissa << exponent*5) -- target from the spec, k counted by
  the checker; a tie clears the flag; mantissa 0 never times out whatever
  the exponent; no side effects while waiting; exit fetches PC+1. 16/16
  covers. Scratch mutants each fail: mutate.py #6 (timeout wins), timeout
  enable from the exponent, mutate.py #1 in cycle_counter.v (the real
  DELAY/WAIT off-by-one from 1121ccc, caught end to end at core level),
  inverted pin level. Bounded: timeout exits only reached for exponent 0
  (target <= ~16); exponent >= 1 is checked only for "not early".

- **`core.v` regfile write values -- done, proven.**
  `agent_core_datapath_props.v` (assertion-formal agent): every regfile
  write carries the value docs/isa.md defines (LDI, IN, SHIFT zero-fill,
  INB single-bit merge, LOOP Rd-1, LOAD/LOADX from `512+addr` with the
  trailing writeback) to the named Rd at the named cycle; no other opcode
  or cycle writes. Instruction rebuilt from `mem_rdata`; ghost regfile
  asserted equal to `u_regfile.regs`. 17 asserts, 28/28 covers. Formal
  alone kills mutate.py #3, #7, #12.
- **`core.v` LOAD-mode boot path -- done, proven.**
  `agent_core_boot_props.v` (assertion-formal agent): one write per
  accepted HOST_GO pulse at the counter's address; saturation at 1023
  (written once, over-run pulses no-ops); echo = counter low byte when
  HOST_STATUS rises. 29 asserts, 10/10 covers + 4 `[deep]` covers
  (address 1023, ~2047 cycles deep; evidence `test_boot_echo_and_saturation`,
  see below). Formal alone kills mutate.py #10, #19, #22, #23.

With these two, formal alone kills all 23 mutation-gate mutants
(`python3 scripts/formal_mutants.py`).

- **Chip-level pin contract -- done, proven.**
  `agent_top_pin_contract_props.v` (written in the parent session): the
  whole chip (core + pin_ctrl + top.v wiring) checked from its pins, with
  the SRAM replaced by `formal/models/sram_anyseq.v` (read data free every
  cycle, so every program). From a pin-only ghost: HOST_GO always an
  input and HOST_STATUS always an output; protocol pins and uio[6] Hi-Z
  throughout LOAD; HOST_ERROR's enable exactly "run mode and START has
  fallen"; reset pin state; the boot ack on HOST_STATUS cycle-exact
  (2 cycles after HOST_GO's synchronized edge); top.v's `mode_load` equal
  to the pin-level LOAD. 12 asserts, 10/10 covers. Fails on 5 of 6 top.v
  wiring slips injected in scratch copies (START/HOST_GO edges crossed,
  HOST_GO rise/fall crossed, `mode_load` tied off, `set_value`/`outb_bit`
  crossed, `start_fall` into `start_rise`); the sixth, `pin_index` bit
  order reversed, leaves LOAD's pin 5 (`101`) unchanged and only moves
  run-mode pins, which depend on the program -- out of scope here,
  killed by `test_halt_holds_pins`.

**`[deep]` covers.** A cover beyond BMC depth may carry `[deep]` on its
line if the file has a `// deep-evidence: <test>` header naming an
existing `async def <test>` in `test/*.py` that reaches the state.
`vacuity_check.py` then reports it as deep instead of unreached; without
a valid evidence test it fails.

## Candidate properties, not yet written

None open. Every candidate on the original list, plus the follow-ups
(CALL/RET misuse, LOOP/flag-writer set, WAIT tie rule), is done.

## Rest of the verification environment

Coverage model, mutation-testing gate, golden model, stimulus profiles
and the three agent contracts are now in place -- see
[../docs/VAL.md](../docs/VAL.md).
