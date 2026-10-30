# How AI tools were used

The competition rules allow AI tools and ask entrants to describe how
they used them. This project used them heavily, so this page is specific:
what the AI did, what stayed human, and what stopped AI mistakes from
reaching the chip.

**Tool:** Anthropic's Claude, through Claude Code (a terminal coding
agent), across design, RTL, verification, firmware and documentation.

## Who did what

| | Human (Pankaj Nair) | AI (Claude Code) |
|---|---|---|
| Goals and scope | chose the architecture direction, the protocols, what to build next | proposed options, with costs and risks |
| Design decisions | approved or rejected each one (ISA, pin map, memory split, boot protocol) | drafted the specs in `docs/`, ran adversarial reviews of them |
| RTL (`src/`) | signed off every change | wrote most of it: 9 of 12 RTL commits are co-authored |
| Verification environment | decided what "done" means (the sign-off gates, `docs/VAL.md` section 14) | wrote the tests, models, formal properties and tooling |
| Firmware and assembler | reviewed | wrote |

Of the 92 commits by the author, 81 carry a `Co-Authored-By: Claude`
trailer.

## Guard rails

The point of the setup is that an AI mistake has to get past a check
that doesn't depend on the AI's own judgement.

- **RTL is write-protected.** A deny rule in `.claude/settings.json`
  blocks AI edits under `src/`. Every RTL change needed the author's
  explicit approval. Real RTL bugs the environment found were fixed
  that way, among them: the HOST_ERROR pin could never be driven
  (commit `142ab71`), and boot writes past the end of SRAM weren't
  blocked (`boot_full`).
- **The checks are executable.** `make signoff` runs 16 gates (directed,
  spec-only, board, coverage, determinism, formal proof, vacuity,
  tie-offs, RTL coverage, mutation) and writes one verdict. The AI
  can't declare its own work done; the gates do, and they run in the
  main session, never inside a subagent.
- **Checks of the checks.** A test suite that passes everything proves
  nothing, so the environment is itself tested:
  - a mutation gate of 29 bugs injected into the RTL, all of which
    must be caught;
  - every formal assertion paired with a cover that must be reachable,
    so no property holds vacuously;
  - each new formal property file shown to fail on a deliberately
    broken copy of the design before it is trusted;
  - a blind census of 469 machine-generated mutants: all 442 that
    change behaviour are caught, and the 24 survivors are formally
    proven equivalent to the original.
- **Independence.** The 75-test spec-conformance suite was written by an
  agent that could read only the documentation and the testbench, never
  the RTL; a script audits its transcript for any file access outside
  that tree. It found five places where the docs were ambiguous or
  disagreed with the RTL, all since resolved in the docs.

## Specialist agents

Six agent definitions, kept in the repo (`.claude/agents/`), each
confined by an `AGENT_CONTRACT.md` that names the only directory it may
write to. Every run is recorded in `ledger/runs.jsonl` with its task,
the gates it had to pass, and the outcome (`make metrics`).

| Agent | Runs | Accepted | What it did |
|---|---|---|---|
| assertion-formal | 12 | 12 | formal properties (`formal/`) |
| triage-debug | 29 | 22 | diagnoses a failing test as RTL, model or harness bug |
| stimulus-gen | 4 | 4 | new random-stimulus profiles; found a harness slowdown |
| red-team | 2 | 2 | proposes bugs the gates would miss; 5 confirmed, all now caught |
| spec-conformance | 2 | 2 | the spec-only test suite |
| coverage-closure | 1 | 1 | stimulus for coverage holes |

The 7 rejected triage runs were benchmark runs, voided because the
benchmark's directory names gave the bug class away. A second leak (the
bug catalogue sitting inside the agent's tree) was found at the same
time; an audit of every earlier transcript showed no agent had read it.
Both were fixed and the round re-run in opaque trees. The benchmark
stands at 7/7 correct in each of three rounds of increasing difficulty.

## What the AI got wrong

Recorded because it shows why the guard rails matter. Examples:

- A first-draft formal checker used a sized constant that truncated a
  multiply; it would have failed a correct design. Caught by requiring
  every property to pass on the real design and fail on a broken one.
- A test meant to cover reset during a memory load checked the wrong
  register and missed the bug it targeted. Caught by the mutation gate.
- The equivalence prover's first negative control passed for the wrong
  reason: internal-state checks, not the pins, were what rejected the
  known-different mutants. Claims and helper invariants were split so a
  control now has to fail at the pins.
- During this port the AI edited a protected hardening file through a
  script instead of asking first. It disclosed this itself, and the
  change was reviewed and approved before being committed.
