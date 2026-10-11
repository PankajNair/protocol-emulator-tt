---
role: model-builder
write_scope: model_rs
protected_paths: [src, test, formal, scripts, firmware, macro, ledger, triage, redteam, docs]
read_context: [docs/isa.md, docs/architecture.md, model_rs/INTERFACE.md]
forbidden_reads: [test, src, formal, scripts, firmware, redteam, triage, ledger, docs/VAL.md, docs/protocol_timing.md, git history]
language: "Rust (stable, edition 2021), std only -- no external crates"
output: "a cargo crate in model_rs/ building a binary model_rs that implements model_rs/INTERFACE.md, plus SPEC_NOTES.md listing every spec ambiguity you had to resolve and how"
gate_command: "cd model_rs && cargo build --release && cargo test; then the parent runs python3 scripts/nversion.py --programs 20000 (differential fuzz against the existing model)"
gate_threshold: "builds and tests pass with no warnings in your own code; every interface line answered; no crash on the fuzz corpus. Divergences from the other model are expected findings, triaged by the parent against the spec -- they don't reject your crate"
---

Project notes:

- **Why this exists.** The project already has one reference model. A
  second one written from the same documents, by someone who has never
  seen the first, catches the failure mode a single model can't: the
  model and the harness agreeing with each other while both are wrong.
  Your value comes from your independence. **Do not read anything under
  `forbidden_reads`**, including through `git log`/`git show`, `grep -r`
  over the repo, or the tests. If you are unsure whether a file is
  allowed, it isn't.
- **The spec is the only authority.** That means `docs/isa.md` (formats,
  opcode table, data memory, undefined-opcode behaviour) and
  `docs/architecture.md` (pipeline, cycle costs, reset state, pins).
  Read both completely before writing code. Where they disagree with
  each other, note it in `SPEC_NOTES.md`.
- **Scope.** Model instruction execution from the moment instruction 0
  is fetched. The boot loader (LOAD mode, the HOST_GO handshake) is out
  of scope, since `image` arrives already loaded. Model every opcode,
  including reserved ones and every flag the spec defines.
- **Write unit tests from the spec.** These are `cargo test`s that
  encode spec statements: the worked idioms in isa.md, cycle costs, the
  WAIT tie rule, flag writers, reserved opcodes. Never write tests that
  try to match another model's output.
- **Timeouts on macOS:** there is no `timeout` command. Use
  `perl -e 'alarm 600; exec @ARGV' bash -c '...'`.
- **Files:** write only under `model_rs/`. Keep the write-up of your
  decisions in your final reply as text. `SPEC_NOTES.md` is the one
  document the parent's gate reads, so put the ambiguity list there.
