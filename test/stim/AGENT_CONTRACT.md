---
role: stimulus-gen
write_scope: test/stim
protected_paths: [src, formal, test/golden_model.py, test/test_random.py, test/seq_coverage.py, test/io_stimulus.py, test/random_gen.py, test/test.py, test/isa_asm.py, test/seq, scripts]
read_context: [docs/isa.md, docs/architecture.md, docs/protocol_timing.md, src/io/pin_ctrl.v, test/random_gen.py, test/io_stimulus.py, test/golden_model.py, test/seq_coverage.py, test/test_random.py]
seam: "STIM_PROFILE=<name> env var -> test_random.py imports test/stim/profile_<name>.py; each exported hook it finds replaces the default for that seed, anything not exported falls back to random_gen.generate_program / io_stimulus.IoStimulus"
output_naming: "profile_<name>.py"
output_signature: "optional generate_program(seed: int, max_delay_mantissa: dict, max_wait_mantissa: dict) -> list[int]; optional make_stimulus(seed: int, boot_exit_cycle: int, max_cycle: int) -> object with raw_ui_in(t), raw_uio_in(t), io_read(t) -> (ui_in_sync, uio_in_sync) -- export at least one"
gate_command: "SEEDS=200 STIM_PROFILE=<name> make -C test random && make coverage STIM_PROFILE=<name> COV_SEEDS=100 && make mutate"
gate_threshold: "200/200 seeds pass; targeted bins HIT (or materially raised, with numbers); mutation score no lower than baseline; git diff on protected_paths empty"
reference_model_scope: "test/golden_model.py predicts all 19 opcodes + reserved 19-31 (NOP + illegal_op_flag), given io_read. It does NOT model: pin_index 4/5/6 (HOST_GO/STATUS/ERROR role semantics, self-loopback), the per-byte LOAD boot handshake (test_random.py's fast_boot pokes SRAM directly), or unbounded WAIT termination."
existing_examples: [test/random_gen.py, test/io_stimulus.py]
---

Real project notes (quirks a generic subagent can't know on its own):

- **Pin stimulus must subclass `io_stimulus.IoStimulus`** (override how
  the `_raw_ui`/`_raw_uio` timelines are filled) rather than rewriting
  its lookup methods. Its `SYNC_DELAY = 3` and the `t <= boot_exit_cycle`
  special case were measured against real RTL through cocotb's read
  pattern -- re-deriving either is exactly how a profile silently
  mis-predicts. Keep uio bits 4/5/6 at 0 after boot, same as the base
  class (outside `reference_model_scope`).
- **Program profiles must keep `random_gen.py`'s termination-by-
  construction rules** (its module header): forward-only branches,
  LOOP counters LDI-seeded 1-8 and protected from body writes
  (`forbid_write_reg`, extended to IN/INB), exactly one leaf CALL
  subroutine ending in RET, mandatory nonzero WAIT timeout, trailing
  HALT, <= 256 words. Reuse `random_gen`'s helpers (`_gen_leaf_word`,
  `generate_program` itself with different knobs) wherever possible;
  never import-and-monkeypatch its module state.
- **Cycle budget**: default `MAX_CYCLES=20000` per seed. A profile that
  needs more (e.g. exponent-2/3 DELAY/WAIT) must say so; the parent can
  pass `MAX_CYCLES` for that profile's runs, but a profile that blows the
  default budget on typical seeds is a design problem, not a knob.
- **Determinism namespacing**: `random_gen` seeds `random.Random(seed)`,
  `io_stimulus` uses `random.Random(f"io_stim:{seed}")`. Use your own
  prefix, e.g. `random.Random(f"stim:<name>:{seed}")`.
- **Requested profiles** (one per invocation, parent names which):
  1. `protocol_pins` -- level-holding waveforms with random dwell per
     protocol pin (UART idle-high + start/data bits at a plausible
     baud-in-cycles, I2C-like long SCL-low clock stretch, SPI bursts).
     Today each pin is a fresh 50/50 bit every cycle, so any WAIT is
     satisfied within a few cycles: 3/375 WAITs timed out, 1 tie in 100
     seeds. Targets `wait_bins.timeout`, `wait_bins.tie`,
     `flag_writer_bins.WAIT.1`, `wait_bins.met_later` with long k.
  2. `wide_timing` -- DELAY/WAIT exponents 2/3 with small mantissas
     (exp 2 x mantissa 1 = 1024 cycles). Opens `delay_exp_bins.2/3`,
     `wait_exp_bins.2/3` (currently EXPECTED_OPEN).
  3. `illegal_mix` -- reserved opcodes 19-31 (random operand bits too)
     sprinkled into otherwise-normal programs. Opens
     `opcode_bins.ILLEGAL`, `debug_flag_bins.illegal_op`.
  4. `nested_flow` -- branches/LOOPs inside LOOP bodies, termination
     still by construction. Needs a nesting-depth coverage bin the
     parent will add; describe what you'd want it to count.
