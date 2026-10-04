---
role: triage-debug
write_scope: triage/reports
protected_paths: [src, test, formal, scripts, firmware, docs, macro, ledger]
read_context: [docs/isa.md, docs/architecture.md, docs/protocol_timing.md, docs/VAL.md, src, test/golden_model.py, test/test_random.py, test/io_stimulus.py, test/random_gen.py, test/stim, test/seq]
tools: "python3 scripts/triage.py replay --seed N [--profile P] [--program FILE]; python3 scripts/triage.py minimize --seed N [--profile P] --out triage/reports/<id>/min; python3 scripts/triage.py bisect --seed N --good SHA (only when the tree is a git checkout with history)"
classes: [DUT, REFERENCE_MODEL, HARNESS, SPEC_AMBIGUITY, NOT_REPRODUCIBLE]
report_format: "triage/reports/<id>/verdict.json with keys classification, confidence (high|medium|low), culprit_file (repo-relative), culprit_lines ([start, end]), check (failing check name), minimal_repro (path), replay_command, spec_refs (list of 'doc:section' strings), evidence (list of strings, each citing a command+result or file:line), ruled_out (object: class -> reason), suggested_fix. The human write-up goes in your final reply as text, not in a file (the harness blocks subagent report files)"
gate_command: "parent re-runs the replay_command (must reproduce the stated check), reads the culprit lines, and on benchmark cases scores verdict.json with python3 scripts/triage_bench.py score <case> <dir>"
gate_threshold: "replay reproduces; classification and culprit_file match ground truth on benchmark cases; every evidence item checkable"
---

Real project notes (quirks a generic subagent can't know on its own):

- **What the random harness compares.** `test/test_random.py` runs one
  random program per seed on the RTL and on `test/golden_model.py` in
  lockstep, comparing the full architectural state at every instruction
  commit (registers, flag, PC, cycles used, pin modes/drives, physical
  uio pins, data memory). `failure.json` (under
  `test/regression_artifacts/seed_N/`) names the first check that
  failed, the RTL and golden values, and the last 16 committed
  instructions with RTL vs. golden cycle counts.
- **Three places a mismatch can come from:**
  - `src/` is the DUT.
  - `test/golden_model.py` is the reference model.
  - The harness: `test/io_stimulus.py` produces external pin stimulus
    and the golden model's view of it. `SYNC_DELAY` there is the
    cycle offset between the raw pin and what the core sees. Also in the
    harness: `test/test_random.py` (sampling/commit detection),
    `test/random_gen.py` and `test/stim/` (program and stimulus
    generators, which must only produce documented-legal input).
- **The spec is `docs/isa.md` and `docs/architecture.md`.** Opcode
  semantics are the opcode table in isa.md. Pin input synchronization,
  pipeline timing and cycle costs are in architecture.md. An instruction
  costs 3 cycles (FETCH_LO, FETCH_HI, EXECUTE); DELAY/WAIT add their
  count. LOAD/LOADX's register write lands one cycle late, and the
  harness already skips that register at that commit.
- **Pin stimulus is random every cycle.** Any IN/INB/WAIT result
  depends on which cycle the pin was sampled. A symptom confined to
  IN/INB/WAIT values or WAIT durations points at the sampling path,
  which runs DUT synchronizer -> harness SYNC_DELAY -> model. Decide which
  of those disagrees with architecture.md.
- **Minimizer.** It replaces instructions with NOP (opcode 0) and keeps
  addresses fixed. The minimal program usually has no HALT, and stray
  words after the failure point are irrelevant. Read `minimal_core.txt`
  for the instructions that matter. A typical seed minimizes in 1-5
  minutes (about 1.6 s per simulation).
- **One simulation per tree at a time.** All runs in a tree share
  `test/sim_build/` and `test/regression_artifacts/seed_N/`. triage.py
  takes a lock on `test/.triage.lock` and waits if another run holds
  it. Don't run raw `make -C test random` alongside it. A minimizer
  result that passes on its final re-run means two runs overlapped (or
  real nondeterminism), not a minimal repro.
- **Benchmark trees.** For benchmark cases the parent hands you a
  standalone copy of the repo (no `.git`, so no bisect) with one known
  bug planted somewhere. Treat it like any real failure, and work only
  inside that directory.
- **Timeouts on macOS:** there is no `timeout` command. Wrap long runs as
  `perl -e 'alarm 900; exec @ARGV' bash -c '...'`.
