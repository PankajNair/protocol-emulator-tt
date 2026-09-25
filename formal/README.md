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

## Candidate properties, not yet written

- **Illegal-opcode detection in `core.v`**: `illegal_op_flag` sets iff
  the executed opcode was actually outside the 19 recognized values
  (0-18). `formal_common.py`'s `DEBUG_PORTS` already has the
  `protocol_cpu_core` entries this needs (`state`, `illegal_op_flag`,
  and friends).
- **Reset brings `core.v` back to a known fetch state within N cycles.**

## Rest of the verification environment

Coverage model, mutation-testing gate, golden model, stimulus profiles
and the three agent contracts are now in place -- see
[../docs/VAL.md](../docs/VAL.md).
