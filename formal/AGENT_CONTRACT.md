---
role: assertion-formal
write_scope: formal
protected_paths: [src, test]
read_context: [src, docs/isa.md, docs/architecture.md, formal/agent_cycle_counter_timing_props.v]
gate_command: "make vacuity TARGET_PROPS=formal/<your_file>.v && make formal TARGET_PROPS=formal/<your_file>.v"
gate_threshold: "vacuity: 100% of your cover statements reached; formal: every assert Status PASSED, at the toolchain's default BMC depth (20, see formal/scripts/formal_common.py -- raise it only if a specific property genuinely needs more depth to say anything meaningful)"
output_naming: "agent_<target>_<hazard_or_property>_props.v"
assertion_language: "Plain immediate assertions inside always @(posedge clk) blocks ONLY -- this project's yosys build (plain read_verilog -sv, no SymbiYosys/Verific) cannot parse concurrent SVA (`assert property`, `|->`, `##N`). Bind externally via a `bind <target_module> <props_module> u_<props_module> (...)` statement at the bottom of your file -- never write properties inline into src/ source."
existing_examples: [formal/agent_cycle_counter_timing_props.v]
---

Real project notes (this project's specific quirks a generic subagent
can't know on its own -- ported from the sibling
5-Stage-Pipelined-RISC-V-Processor project's own AGENT_CONTRACT.md,
same toolchain, same yosys/z3 install):

- `bind` does not elaborate DUT-internal state under this project's
  yosys build (confirmed -- bound checker cells vanish silently, no
  diagnostic). If your properties need to observe internal signals not
  already exposed as module ports, add an entry to
  `formal/scripts/formal_common.py`'s `DEBUG_PORTS` table (target
  module -> {signal_name: (packed_type_str, n_elems_or_None)}) rather
  than assuming you can reference internal state directly from a bound
  module. `n_elems` is only needed for a genuinely *unpacked* array
  (e.g. `pin_ctrl.v`'s `mode`/`drv`, declared `reg [1:0] mode [0:7]`) --
  None for an already-packed signal.
- Every `assert` needs a companion `cover` on the same antecedent --
  that companion cover is what `make vacuity` actually checks. A
  property with no matching cover cannot be vacuity-checked at all.
- Add `assume`s only to constrain the standalone-module BMC environment
  to a realistic regime (e.g. "reset actually happens in the BMC
  window" -- every existing props file needs `initial assume (!rst_n);`
  for this reason, since every module here uses async reset) -- never
  to hide a real bug by over-constraining away the exact input pattern
  that would trigger it.
- **Verify your own checker before trusting a PASSED result.** Confirmed
  directly on `agent_cycle_counter_timing_props.v`: its first draft used
  a sized shift-amount literal (`exponent * 3'd5`) that silently
  truncated the multiply's result width, giving wrong target values and
  a false FAIL against a *correct* DUT. Use an unsized literal (`* 5`)
  for this exact reason -- mirrors why `src/io/cycle_counter.v` itself
  uses the unsized `` `TIMEOUT_SHIFT` `` macro, not a sized constant.
  Before trusting any new props file's PASSED result: (1) confirm every
  cover is reached (`make vacuity`), and (2) temporarily inject a real
  bug into a SCRATCH COPY of the target module (never into `src/`
  itself -- protected_paths above) and confirm the property actually
  FAILS against it, then discard the scratch copy. A property that
  can't be made to fail on a broken DUT isn't actually checking
  anything.
- Target-module dependency wiring (`EXTRA_SRCS` in
  `formal_common.py`) is a hand-maintained allowlist, same as the
  sibling project -- add an entry there before writing a props file
  against `protocol_cpu_core` (needs `cpu/regfile.v` +
  `io/cycle_counter.v` alongside it) or any future target with real
  submodule instantiations.
- `isa_defs.v` is always read first (`formal_common.ISA_DEFS`) -- every
  target module in `src/` relies on its `` `define` ``s via file-order
  visibility, not `` `include` `` (see `src/cpu/core.v`'s own header for
  why). You don't need to do anything about this yourself, just know
  it's already handled.
- **Done so far**: `cycle_counter.v`'s DELAY/WAIT timing (no
  undercount + exact timing, both proven, `agent_cycle_counter_timing_props.v`).
- **Candidate next targets** (`formal/README.md`'s list, still open):
  illegal-opcode detection in `core.v` (needs the `DEBUG_PORTS` entries
  for `state`/`illegal_op_flag` already present in
  `formal_common.py`), pin-direction-never-contended in `pin_ctrl.v`
  (needs the `mode`/`drv` unpacked-array debug ports, also already
  present), reset brings `core.v` back to a known fetch state within N
  cycles.
