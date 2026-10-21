# Verification approach

A summary for reviewers. The full reference, with every number's
provenance, is [VAL.md](VAL.md); section numbers below point into it.

## The idea

A test suite that passes is evidence only if it would have failed on a
broken design. So the design is checked from several independent angles,
and then **the checks themselves are attacked**: bugs are injected into
the RTL on purpose, and every one has to be caught.

`make signoff` runs all of it and gives one verdict, `STABLE` or not
(§14). A nightly CI job runs it.

## What checks the design

| Layer | What it does | Size |
|---|---|---|
| Directed tests | cocotb tests of each instruction, the boot loader, the host handshake, reset and HALT, with cycle-exact timing checks; plus UART, WS2812, SPI-target and 1-Wire firmware end to end against independent models (§1) | 54 tests |
| Random differential | random programs and live pin stimulus run on the RTL and on an independent Python golden model; the full machine state is compared after every instruction (§2, §4) | 100 seeds, plus 4 stimulus profiles of 50 |
| Functional coverage | bins for every opcode, operand class, pin mode, timing range and control-flow shape; the gate fails if any reachable bin is missed (§3) | 107/107 for the default generator |
| Spec-only suite | 62 tests written only from the documentation, by an agent that could not read the RTL (an audit of its file accesses proves it) (§21) | 62 tests |
| Board tests | two chips on shared nets: loopback, chip-to-chip UART, I2C and SPI against independent peripheral models (§13) | 23 tests |
| Formal | properties proven for all time (k-induction) with yosys + z3: datapath results, boot loader, reset, pin direction, synchronizer depth, timing, illegal opcodes, and a chip-level pin contract (§6) | 11 property files |
| RTL coverage | Verilator line and toggle coverage of `src/`; every line executed and every bit moved both ways, or a written reason why it can't (§22) | 132/134 lines, 1385/1406 toggles, rest proven unreachable or constant |
| Structural | the SRAM macro's static pins (BIST off, delay setting) tied as the datasheet requires, which no simulation can observe (§20) | 17/17 ports |

## What checks the checks

- **Mutation gate (§5).** 26 deliberate RTL bugs, each from a past real
  bug, a hazard review, or a gap one of the methods below found. Every
  one must be caught by simulation; formal alone also catches all 26.
- **Vacuity (§6).** Every formal assertion has a cover on its
  precondition that must be reachable, so no property passes by never
  applying. 273/273 reached.
- **Blind mutation census (§20).** Every mutable operator in the RTL is
  mutated by machine, with no sampling: 455 mutants, 452 that compile.
  All 427 that change behaviour are caught. The other 25 are formally
  proven equivalent to the original at the pins, with a negative
  control showing the prover does reject real differences.
- **Formal-only kills (§20-21).** The 35 mutants that only formal caught
  are each explained: 30 are proven never to reach the pins, 4 led to
  new directed tests, and 1 is a deliberately stricter-than-spec formal
  property.
- **Red team (§17).** An agent tries to write bugs every gate misses. It
  found 5; each became a new test and a mutation-gate entry.

## What this found

Real bugs, each now guarded by a test and a mutation-gate entry:

- **RTL:** the HOST_ERROR output could never be enabled; boot writes past
  the end of SRAM overwrote the last byte; a counter off-by-one made
  every DELAY or timed WAIT with a nonzero count one cycle too long (§5).
- **Gaps no test covered:** code above instruction 255 never ran (found by
  RTL coverage); reset during a pending memory load; the uo_out reset
  value; the DELAY counter's full width (found by the red team).
- **Spec:** five places where the docs were ambiguous or disagreed with
  the RTL, found by the spec-only suite and resolved in the docs (§21).

## Limits

- Simulation and formal model the SRAM with IHP's behavioural model;
  silicon timing, the real macro and the pads are covered only by the
  Tiny Tapeout flow's static timing, DRC, LVS and gate-level test.
- Formal proofs hold for the model yosys builds (asynchronous reset is
  treated as synchronous).
- The board tests use floating nets and pull-ups, which the 2-state
  Verilator coverage run cannot model, so they are not in RTL coverage.

How AI tools were used to build all of this: [ai_usage.md](ai_usage.md).
