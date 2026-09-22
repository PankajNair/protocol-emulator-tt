---
role: coverage-closure
write_scope: test/seq
protected_paths: [src, formal, test/golden_model.py, test/test_random.py, test/seq_coverage.py, test/io_stimulus.py, test/test.py, test/isa_asm.py]
read_context: [src, docs/isa.md, docs/architecture.md, test/random_gen.py, test/seq_coverage.py, test/golden_model.py, test/io_stimulus.py]
gate_command: "make mutate && make coverage COV_SEEDS=100"
gate_threshold: "mutate: score in mutation_results.json no lower than the pre-change baseline (and >= 0.80 once the baseline itself clears it); coverage: your targeted bin HIT (or its count materially raised, if the task was an under-hit bin), no previously-HIT bin now OPEN"
output_naming: "cov_closure_<bin_name>.py"
output_signature: "gen_cov_closure_<bin_name>_burst(rng: random.Random, tag: str) -> list[tuple[str | None, int | Callable[[dict], int]]]"
coverage_source: "scripts/merge_coverage.py over $COV_DIR/seed_*.json (default /tmp/seq_coverage), produced by `make coverage` -- plain JSON from test/seq_coverage.py's SeqCoverage, see that file for what each bin means"
existing_examples: [test/random_gen.py]
---

Real project notes (quirks a generic subagent can't know on its own):

- **Output shape**: return a list of `isa_asm.assemble()` entries --
  `(label_or_None, word_or_callable)`, one instruction per entry,
  callables take the final labels dict (see `random_gen.py`'s LOOP/CALL
  blocks for the pattern). Every label you define must start with
  `tag` (the parent passes a unique string per burst site) so two
  copies of your burst in one program never collide. Use `isa_asm`'s
  encoders (`asm.wait_`, `asm.set_pin`, ...), never hand-roll bits.
- **You do not wire your burst in.** The parent session adds it as a new
  block kind in `random_gen.generate_program` (same place as
  `_BLOCK_LOOP`/`_BLOCK_CALL`). Describe the wiring you expect (weight,
  max sites per program) in your report.
- **Termination by construction is mandatory** (`random_gen.py` header):
  no backward branch except a LOOP whose counter is LDI-seeded 1-8
  inside your burst and never written by the body; no unbounded WAIT
  (mantissa must be nonzero); no CALL/RET (the generator owns the one
  shared subroutine); no HALT. Branches may only target labels inside
  your own burst, strictly forward. A MAX_CYCLES trip is always treated
  as a real bug.
- **Stay inside the golden model's scope**: WAIT/INB pin_index only
  from the 5 protocol pins (0,1,2,3,7) -- see `io_stimulus.py`'s header
  for why 4/5/6 are out. Program must still fit the 256-word limit with
  your burst included; keep bursts short (<= ~20 words).
- **External stimulus is random per cycle and you cannot control it**
  (`io_stimulus.py`: each protocol pin is a fresh 50/50 bit every
  cycle, 3-cycle sync lag). Bins depending on pin values (e.g.
  `wait_bins.timeout`, `wait_bins.tie`) must be targeted through what
  you DO control -- WAIT level/timeout length, repeated sampling -- and
  your report must say honestly how the hit rate depends on stimulus
  luck. Do not modify `io_stimulus.py` (protected).
- **Anti-gaming, project-specific**: `outb_mode_bins` counts OUTB against
  the pin's current sticky mode -- an OUTB on a pin still in reset
  input mode is a legal no-op; the real hazard is OUTB actually driving
  under push-pull / open-drain, so a construct for that must `SET` the
  mode first, then OUTB. For `wait_bins.tie`, the real condition is the
  pin match landing on exactly the expiry cycle; state how often your
  construct really lands there, measured, not assumed.
- **Currently known open / thin bins** (from a 100-seed `make coverage`):
  `set_mode_x_pin_bins.leave.p7` (open -- rarity), `wait_bins.timeout`
  (3 of 375 WAITs), `wait_bins.tie` (1), `flag_writer_bins.WAIT.1` (3),
  `outb_mode_bins.pp`/`.od` (21 of 255 OUTBs actually drive a pin).
  `EXPECTED_OPEN` bins in `scripts/merge_coverage.py` are out of scope
  unless the parent session explicitly asks.
