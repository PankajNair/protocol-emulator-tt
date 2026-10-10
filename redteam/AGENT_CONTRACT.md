---
role: red-team
write_scope: redteam/candidates
protected_paths: [src, test, formal, scripts, firmware, docs, macro, ledger, triage]
read_context: [docs/isa.md, docs/architecture.md, docs/protocol_timing.md, docs/VAL.md, src, test/test.py, test/test_random.py, test/golden_model.py, test/random_gen.py, test/io_stimulus.py, test/seq_coverage.py, test/stim, test/seq, test/test_board.py, test/test_uart.py, test/test_i2c.py, test/test_spi.py, formal, scripts/mutate.py, scripts/merge_coverage.py]
candidate_format: "redteam/candidates/<id>.json: {id, file (relative to src/), edits: [[old, new], ...] (each old occurs exactly once in that file), claim (one line: the bug), spec_ref ('doc:section' + quoted text the mutant violates), why_survives (object: gate -> reason), witness}"
witness_types: "either {program: <file in redteam/candidates/>, seed: N, profile: null|name} -- a random-harness program (one hex word per line), replayed RTL-vs-golden with that seed's pin stimulus; or {cocotb_test: <file in redteam/candidates/>, board: false|true} -- a cocotb test module, run on the tb.v toplevel (or tb_board.v with board: true), which must import only from test/ (reuse test.py helpers such as reset_and_boot) and assert only spec-defined behaviour"
eval_command: "python3 scripts/redteam_eval.py redteam/candidates/<id>.json [--seeds 200]  (about 2-3 min per candidate; writes <id>.result.json)"
gate_threshold: "confirmed finding = parent re-run with --full returns SURVIVED_WITNESSED; mutants already in scripts/mutate.py MUTANTS don't count"
budget: "at most 8 candidates evaluated per invocation; run one eval at a time"
---

Project notes (quirks a generic subagent can't know on its own):

- **What already exists, so don't resubmit it.** Read
  `scripts/mutate.py` MUTANTS: there are 22 curated mutants (#18-22 came
  from red-team passes) and all of them are killed. `docs/VAL.md` section 5 also records a blind-mutation
  sample.
- **What the gates are.** `redteam_eval.py` runs these on a copy with the
  mutant applied:
  - directed: `test/test.py` plus the UART end-to-end test;
  - random differential: 200 seeds with the default generator, plus
    the 4 stimulus profiles with `--full`;
  - board testbench: two chips, UART link, I2C and SPI firmware against
    slave models;
  - formal: 8 props files at BMC depth 20.

  Any failing gate kills the mutant.
- **Known out-of-scope areas for the random harness** (from
  `random_gen.py` and the `test/stim` contract). The golden model does
  not model:
  - pin_index 4/5/6 semantics (HOST_GO, HOST_STATUS, HOST_ERROR);
  - the per-byte LOAD boot handshake (`fast_boot` pokes SRAM
    directly);
  - unbounded WAIT.

  Directed tests and formal cover these, but more thinly. Witness gaps
  there with a cocotb test.
- **Spec authority.** `docs/isa.md` (the opcode table) and
  `docs/architecture.md` define correct behaviour. Where they are silent,
  behaviour is unspecified, and a witness must not pin it down. Comments
  in `src/` are not the spec.
- **Program witnesses** are replayed with `scripts/triage.py replay
  --seed N --program FILE`. The program must obey the generator's legality
  rules: forward branches, a terminating LOOP, WAIT with a nonzero
  timeout, a final HALT. Pin stimulus comes from the seed.
- **Cocotb witnesses** run as `make COCOTB_TEST_MODULES=<module>` inside
  `test/` of a mutant copy and of a clean copy. Name the module
  `witness_<id>.py`, and keep it self-contained apart from importing
  helpers from `test.py`.
- **Writing files.** Candidate JSON, witness `.txt` and `.py` files go in
  `redteam/candidates/` only. Your write-up goes in your final reply as
  text; the harness blocks report files.
- **Timeouts on macOS:** there is no `timeout` command. Use `perl -e
  'alarm 900; exec @ARGV' bash -c '...'`.
