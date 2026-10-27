# Verification Environment Reference

Ground-truthed against the code in `test/`, `scripts/`, `formal/`, and the
agent contracts. Same section layout as the sibling
`5-Stage-Pipelined-RISC-V-Processor` project's `files/VAL.md`, which this
environment is ported from. See [isa.md](isa.md) and
[architecture.md](architecture.md) for the spec every check here is
derived from.

Setup: cocotb 2.0.1 needs Python <= 3.13. Locally, build a venv on
`python3.13` (`python3.13 -m venv .venv && .venv/bin/pip install -r
test/requirements.txt`) and `source .venv/bin/activate` before any `make`
that runs simulation. CI uses Python 3.11 (`.github/workflows/test.yaml`).

## 1. Test structure

Plain cocotb on Icarus (Verilator also works: `SIM=verilator`). No
pyuvm: the DUT is small enough that the extra layering adds ceremony
without adding checking power.

| Suite | File | What it asks | Run |
|---|---|---|---|
| Directed | `test/test.py` (37 tests) | Does this specific sequence/edge case work? One test per opcode plus targeted hazards (CALL/RET misuse, flag immunity, WAIT flag polarity, boot echo/saturation, uio[6] driver gate, the 5-phase host IN/OUT handshake, HALT holding every pin). Host-facing waits are bounded and require `uio_oe` to be driving. | `make -C test` |
| UART end-to-end | `test/test_uart.py` | Does the shipped UART firmware (`firmware/protocols/uart.asm` TX, `uart_rx.asm` RX, assembled by `firmware/isa/asm.py`) move bytes correctly on the real RTL? TX: Independent receiver model on the TX pin (start-edge detect, centre sampling, stop-bit check, re-arm after the stop sample), 115200 / 57600 / 9600 baud, per-edge timing deviation measured against docs/protocol_timing.md. RX: transmitter model on the RX pin, back-to-back frames, sender baud error +-2%, framing error + resync. | part of `make -C test` |
| WS2812 end-to-end | `test/test_ws2812.py` (2 tests) | Does `ws2812.asm` drive an LED chain correctly? An independent decoder written from the WS2812B datasheet (each pulse in its +-150 ns window, latch on > 50 us low), plus the exact cycle budget from the firmware header. Demo frame with host resend; a 255-byte frame with every bit pair inside a byte and across a boundary. | part of `make -C test` |
| SPI target end-to-end | `test/test_spi_target.py` (3 tests) | Does `spi_target.asm` follow an external controller's clock? Independent mode-0 controller model; 1 MHz echo, back-to-back transactions, a long clock pause with CS low, and the measured speed limit (exact at a 14-cycle half period, fails at 13). | part of `make -C test` |
| 1-Wire end-to-end | `test/test_onewire.py` (5 tests) | Does `onewire.asm` work against a spec-worst-case device (a 0 held only 15 us, presence at both ends of its window)? Presence, no device, READ ROM with CRC8, byte writes, unknown command; every controller pulse and slot checked against the spec. | part of `make -C test` |
| PS/2 host end-to-end | `test/test_ps2_host.py` (2 tests) | Does `ps2_host.asm` receive from a keyboard model that owns the clock and honours the inhibit? Back-to-back scan codes with the chip holding the keyboard off; bad parity and bad stop frames dropped with HOST_ERROR. | part of `make -C test` |
| NEC IR end-to-end | `test/test_nec_ir.py` (1 test) | Does `nec_ir.asm` send a frame an IR receiver decodes? Receiver model demodulates the carrier (every period within 38 kHz +-2%) and decodes NEC with typical tolerances. | `make -C test slow` (own sign-off gate) |
| Host library | `test/test_host_loader.py` (4 tests) | Does the demo-board host library (`firmware/host/pe_host.py`) load and talk to the chip? The same protocol generators the board runs, driven on the DUT's pins: load + send/recv, a 522-byte image with a data region, reload, timeout instead of hang. | part of `make -C test` |
| Board self-tests | `test/test_board.py` on `tb_board.v` | Does the two-chip board testbench resolve nets like real wires (floating = x, pull-ups, wired-AND, contention flagged), wire loopback correctly, and run each chip on its own clock? Checked before anything is built on it. | `make -C test board` |
| Assembler | `test/test_assembler.py` (pytest) | Every mnemonic encodes identically to `isa_asm`; labels, `.equ`/`-D`, DELAY/WAIT cycle encoding, data region, error cases. | `pytest test/test_assembler.py` |
| Random differential | `test/test_random.py` | Does every generated program agree with the golden model, instruction by instruction? | `make -C test random SEEDS=n` |
| Golden self-check | `test/test_golden_model_selfcheck.py` | Does the golden model agree with the RTL-proven directed tests before being trusted as an oracle? | `pytest test/test_golden_model_selfcheck.py` |
| Hierarchy smoke | `test/test_hierarchy_smoke.py` | Does hierarchical signal access (the scoreboard's foundation) still work on this simulator? | `make -C test COCOTB_TEST_MODULES=test_hierarchy_smoke` |

CI (`.github/workflows/test.yaml`) runs: the directed suite, the golden-model self-check, the hierarchy smoke test, the random regression with a coverage gate for the default generator (100 seeds), and the same for each stimulus profile (50 seeds each). All steps run even if one fails. Mutation and formal stay local (mutation needs ~10 min; formal needs a yosys/z3 build CI doesn't have). On this fork push-triggered workflows are disabled (GitHub's fork default), so it currently runs only via manual dispatch.

## 2. Scoreboard

`test/test_random.py::run_one_seed` detects each instruction commit on the
RTL (EXECUTE -> FETCH_LO, or HALT) and steps the golden model once. At
every commit it compares: fetched instruction word, cycles used (exact,
so DELAY/WAIT timing is checked, not just results), PC, all four GPRs,
flag, return address, `return_valid`, both sticky debug flags, `halted`,
`uo_out`, every pin's sticky `mode`/`drv`, and the physical pins: `uio_oe`/`uio_out` for the protocol pins and HOST_STATUS, expected values derived from the spec's drive-mode table rather than pin_ctrl's own arrays (`uio_oe[6]` excluded -- boot-timing dependent, covered by its directed test and formal). STOREs are spot-checked on
the written byte; the full 512-byte data region is diffed at the end.

One documented skip: a LOAD/LOADX's destination register is not compared
at its own commit, because the RTL write lands one cycle later
(architecture.md Pipeline). It is compared at the next commit.

Failing seeds dump `test/regression_artifacts/seed_<n>/{program,failure}.txt`.

## 3. Coverage model

`test/seq_coverage.py` (`SeqCoverage`), sampled once per commit from the
golden model's pre/post state (already proven equal to the RTL at that
point). Writes `$COV_DIR/seed_<n>.json` (default `/tmp/seq_coverage`).
`scripts/merge_coverage.py` merges and reports. `make coverage
[COV_SEEDS=n] [STIM_PROFILE=x]`.

Bin groups, each a proxy for a condition named in the spec:
opcodes (19 + reserved), branch cond x outcome, WAIT exit path (met
immediately / later / timeout / tie on the expiry cycle / unbounded),
WAIT blocking duration (0 / 1-31 / 32-479 / 480+), DELAY and WAIT
exponent, SET mode x pin, OUTB/INB bit-select x protocol pin, OUTB against
the pin's sticky mode, data-region half, every flag writer x polarity,
LOOP outcome, sticky debug-flag set conditions, LOAD/LOADX trailing-
writeback hazard, and dynamic loop nesting (depth, branch/CALL inside a
live loop body).

`EXPECTED_OPEN` in `merge_coverage.py` lists bins the default generator
structurally can't reach, each with its reason. It is scoped to the
default generator: most of those bins are reached by a stimulus profile
(section 4).

`make coverage-gate` (and CI) turns the report into a gate: for the
default generator every bin outside `EXPECTED_OPEN` must be hit; for a
profile, every bin in `PROFILE_REQUIRED[profile]` (the bins it exists
to reach) must be hit. Without the per-profile list, a profile that
stopped reaching its targets would just show them as expected-open.

## 4. Stimulus

Default: `test/random_gen.py` (programs) + `test/io_stimulus.py` (pins).
Termination is guaranteed by construction: forward-only branches, LOOP
counters seeded 1-8 and protected from body writes, one leaf subroutine,
mandatory nonzero WAIT timeouts, trailing HALT. A `MAX_CYCLES` trip is
always a real bug.

`io_stimulus.py` drives a deterministic per-seed raw timeline every
cycle and re-derives the synchronizer delay independently of the RTL.
The delay constant is `SYNC_DELAY = 3`, measured against real RTL through
cocotb's read pattern (the RTL has a textbook 2-flop synchronizer; the
extra cycle is where cocotb samples relative to the write). WAIT/INB are
restricted to the five protocol pins (0-3, 7); pins 4/5/6 have role
semantics the golden model doesn't cover and are handled by directed
tests.

Stimulus profiles (`test/stim/profile_<name>.py`, `STIM_PROFILE=<name>`)
replace the program generator, the pin timeline, or both. All written by
the stimulus-gen agent (section 8) and gated in the parent session:

| Profile | Replaces | Opens |
|---|---|---|
| `protocol_pins` | pin timeline: UART / SPI / I2C-shaped waveforms, clock-stretch, stuck lines | WAIT timeout 3 -> 143 per 100 seeds; blocking of 32+ cycles |
| `wide_timing` | program: DELAY/WAIT exponents 2/3, cycle-budgeted | exponent 2/3 bins (delay exp 3 needs `MAX_CYCLES=50000`) |
| `illegal_mix` | program: reserved opcodes 21-31 with random operands | ILLEGAL opcode and `illegal_op` flag bins; all 11 reserved opcodes executed |
| `nested_flow` | program: nested LOOPs, branches and CALLs inside loop bodies | all loop-nesting bins, per 100 seeds: depth2 9712, depth3+ 6781, branch-in-loop 7128, CALL-in-loop 910 (default: 0 each) |

Not covered by any random stimulus (directed tests only): the per-byte
LOAD boot handshake (the random harness pokes SRAM directly for speed),
HOST_GO/STATUS/ERROR pin stimulus, unbounded WAIT, nested CALL.

## 5. Mutation-testing gate

`scripts/mutate.py`, `make mutate [MUTATE_SEEDS=n]`. Threshold 80%.
Baseline must pass first. Each mutant is applied to `src/`, the directed
suite plus n random seeds run, and the file is restored in `try/finally`.
Runs in the parent session only, never delegated.

| # | File | Mutation | Why it's in the list |
|---|---|---|---|
| 1 | cycle_counter.v | revert counter pre-decrement | real bug, commit 1121ccc |
| 2 | pin_ctrl.v | drop one synchronizer flop | sync-depth invariant |
| 3 | core.v | INB bit7 captures inverted pin | receive-path capture |
| 4 | core.v | nested CALL doesn't set misuse flag | isa.md CALL hazard |
| 5 | core.v | orphan RET doesn't set misuse flag | isa.md RET hazard |
| 6 | core.v | WAIT tie: timeout wins | isa.md WAIT tie rule |
| 7 | core.v | LOADX writeback dropped | trailing-writeback path |
| 8 | core.v | opcode 19 treated as recognized | illegal-opcode boundary |
| 9 | pin_ctrl.v | open-drain oe polarity inverted | SET/OUTB drive semantics |
| 10 | core.v | boot counter wraps instead of saturating | architecture.md LOAD |
| 11 | core.v | LOOP writes the flag | flag-writer set |
| 12 | core.v | SHIFT rotates instead of zero-fill | isa.md SHIFT |
| 13 | pin_ctrl.v | HOST_ERROR driven before START falls | uio[6] contention guard |
| 14 | pin_ctrl.v | latch START fall from mode_load-gated edge | real bug, commit 142ab71 |
| 15 | pin_ctrl.v | HOST_STATUS `uio_oe[5]` stuck 0 | blind-sample survivor |
| 16 | pin_ctrl.v | runtime SET HOST_STATUS never reaches pin | targeted hypothesis survivor |
| 17 | pin_ctrl.v | `host_go_fall` misfires, boot ack drops early | blind-sample survivor |

Suites per mutant: directed (incl. UART end-to-end), random
differential, and the board testbench (loopback self-test, two-chip UART
link, I2C, SPI against slave models; skip with `--no-board`). Each suite
run has a 300s timeout (a hang counts as killed) and the script refuses
to run on a dirty `src/`.

Per-suite kills (17 mutants): directed 13, random 10, board 7 (#3, #7,
#9, #14-17). No mutant is killed by the board suite alone -- today it is
a second, protocol-level line of defense, not a gap-closer. #2 (one
synchronizer flop dropped) passes the board suite: the protocol
tests' timing margins absorb one extra cycle of input latency, and only
the cycle-exact random scoreboard catches it. Current score: 22/22:
#18-22 are red-team survivors (section 17), killed by the tests added
for them. Mutants 1-14 were also 14/14 under each stimulus profile
(`STIM_PROFILE=x python3 scripts/mutate.py`).

History worth keeping: the first run scored 11/13. Survivors #10 and #13
led to new directed tests, and the #13 test found a real RTL bug
(HOST_ERROR could never be driven: the START falling edge was only
detected in LOAD mode, which START's own rising edge ends). #14 now
guards that fix.

The curated list is biased toward bugs we already knew about, so it was
cross-checked with a **blind** sample: 45 seeded random operator
mutations (`==`/`!=`, `&&`/`||`, constants, ternaries) across all RTL.
Raw score 80%; of the 9 survivors, 7 were equivalent or dead logic and 2
were real gaps (HOST_STATUS output enable; boot-ack withdrawn early).
Those, plus a targeted "runtime HOST_STATUS never reaches the pin"
mutant that also survived, became #15-17 and are now killed by the
host-handshake test, oe-aware boot helper and physical-pin scoreboard.
The same sample hung the suite on a boot mutant (`load_byte` had an
unbounded wait), which is why waits are now bounded.

#24 (uo_out reset value) and #25 (reset not clearing a pending LOAD
writeback) came from the full blind census (section 20). They survived
every simulation gate, and only formal killed them. The new directed
reset tests (section 21) now kill both in simulation as well.

#26 (sequential next-PC computed in 8 bits) came from RTL coverage
(section 22): `pc[8]` never toggled, and the mutant passed directed,
spec and 100 random seeds. `test_pc_upper_half` kills it.

## 6. Formal / vacuity gate

Plain `yosys` + `yosys-smtbmc` + `z3`, no SymbiYosys. Immediate
assertions in `always @(posedge clk)` blocks only (this yosys build
can't parse concurrent SVA); every assert has a companion cover. `make
formal [TARGET_PROPS=...]` runs BMC at depth 20, `make prove` adds k-induction
(k=20) so a file holds for all time, not just 20 cycles; `make vacuity` requires
100% of covers reached. Toolchain details: `formal/scripts/formal_common.py`
docstring.

| Props file | Target | Status |
|---|---|---|
| `agent_cycle_counter_timing_props.v` | DELAY/WAIT counter: no undercount + exact timing | proven unbounded (k-induction), 2/2 covers |
| `agent_core_wait_props.v` | `protocol_cpu_core`: WAIT leaves EXECUTE on exactly the first k where the pin matches or the spec-computed timeout expires; tie clears the flag; mantissa 0 never times out; no side effects while waiting; exit fetches PC+1 | proven unbounded (k-induction), 16/16 covers (timeout exit reached for exponent 0 only). Scratch mutants (#6, timeout from exponent, #1 in cycle_counter.v, inverted level) each fail |
| `agent_core_loop_flag_props.v` | `protocol_cpu_core`: ghost regfile (from the write port) matches both read ports every cycle; only CMP/TEST-bit/WAIT change the flag; LOOP writes Rd-1 mod 256 and branches iff nonzero (wrap covered); CMP/TEST-bit/WAIT write the specified value | proven unbounded (k-induction), 22/22 covers. Scratch mutants (#11, SHIFT writes flag, LOOP taken iff flag, decrement by 2, CMP inverted) each fail |
| `agent_core_call_ret_misuse_props.v` | `protocol_cpu_core`: ghost `return_valid`/`retaddr` from identified CALL/RET commits match the core every cycle; misuse flag rises only after a nested CALL or orphan RET, always after one, sticky until reset; CALL saves PC+1 and jumps, RET (orphan included) jumps to `retaddr` | proven unbounded (k-induction), 17/17 covers. Scratch mutants (#4, #5, RET not clearing return_valid, CALL saving PC) each fail |
| `agent_core_reset_props.v` | `protocol_cpu_core`: known state on the first cycle after any (incl. mid-trace) reset; nothing executes, drives pins or writes memory before START except host boot writes; first fetch byte 0; no leakage from a DELAY, blocked WAIT, FETCH_HI or pending LOAD/LOADX writeback interrupted by reset (a cover per scenario) | proven unbounded (k-induction), 22/22 covers. Scratch mutants (dropped reset, surviving writeback, reset into FETCH_LO, regfile without reset) each fail |
| `agent_core_illegal_opcode_props.v` | `protocol_cpu_core`: illegal_op_flag rises only after a reserved-opcode commit, always rises after one, sticky until reset; reserved opcode is a true NOP (no mem/pin/regfile write, PC+1, other state unchanged). Opcode taken from `mem_rdata` at FETCH_HI->EXECUTE, not core.v's decode | proven unbounded (k-induction), 9/9 covers. Agent scratch mutants (#8, flag never set, LDI also sets) each fail |
| `agent_core_datapath_props.v` | `protocol_cpu_core`: every regfile write carries the value docs/isa.md defines (LDI, IN, SHIFT zero-fill both ways, INB single-bit merge, LOOP Rd-1, LOAD/LOADX from `512+addr` with the 1-cycle trailing writeback), to the named Rd, at the named cycle; no other opcode or cycle writes. Instruction rebuilt from `mem_rdata`, operands from a ghost regfile asserted equal to `u_regfile.regs` | proven unbounded (k-induction), 17 asserts, 28/28 covers. Formal-only kills #3, #7, #12 |
| `agent_core_boot_props.v` | `protocol_cpu_core` LOAD mode: write strobe exactly once per accepted HOST_GO pulse, at the ghost counter's address with `ui_in`; counter saturates at 1023 (that byte written once, over-run pulses no-ops); uo_out echo = counter low byte, valid when HOST_STATUS rises; ack cleared on HOST_GO fall. Ghost model from ports only, asserted equal to `boot_addr`/`boot_full`/`load_ack` | proven unbounded (k-induction), 29 asserts, 10/10 covers + 4 `[deep]` covers (address 1023 is ~2047 cycles deep) with simulation evidence `test_boot_echo_and_saturation`. Formal-only kills #10, #19, #22, #23 |
| `agent_pin_ctrl_direction_props.v` | `pin_ctrl`: no uio[6] drive in LOAD or before a real START fall (ghost model from ports), `cover(uio_oe[6])` + drives-once-allowed, fixed-role pins 4/5, protocol-pin oe only per sticky mode and never in LOAD, sticky mode matches a ghost model; output VALUES: `uio_out[5]` always the last value written to pin 5 (LOAD ack and runtime), `uio_out[6]` the latched HOST_ERROR once its gate opens, protocol pins drive their last written value (0 in open-drain) | proven unbounded (k-induction), 47/47 covers. Validated by re-injecting the HOST_ERROR bug (142ab71): assert FAILS and the `uio_oe[6]` cover goes UNREACHED, so formal alone would have caught it |
| `agent_top_pin_contract_props.v` | the whole chip from its pins (core + pin_ctrl + top.v wiring), SRAM read data free every cycle (`formal/models/sram_anyseq.v`): HOST_GO always input, HOST_STATUS always output; protocol pins and uio[6] Hi-Z throughout LOAD; HOST_ERROR enable exactly "run mode and START has fallen"; pin reset state; boot ack on HOST_STATUS 2 cycles after HOST_GO's synchronized edge; `mode_load` equal to the pin-level LOAD. Ghost from pins only | proven unbounded (k-induction), 12 asserts, 10/10 covers. Kills 5/6 top.v wiring slips in scratch copies; `pin_index` bit order reversed survives (LOAD's pin 5 is a palindrome, run-mode pins depend on the program) and is killed by `test_halt_holds_pins` |
| `agent_pin_ctrl_sync_depth_props.v` | `pin_ctrl`: every consumer of an external input sees exactly the value from 2 cycles earlier (expected values rebuilt from ports only): all 8 `ui_in_sync` bits, the `pin_read` tap feeding WAIT/INB (pin_index taken in the current cycle), the START/HOST_GO edge outputs with their `mode_load` gating, and the post-reset window while the flops still hold 0 | proven unbounded (k-induction), 66/66 covers. Agent mutants (pin_read on ff1, extra flop on both paths, edge detectors on ff1, ui_in_sync on ff1) each fail, also with the reset-phase asserts removed; parent re-ran the first two independently on full-repo copies |

**k-induction (2026-10-17).** Every props file proves unbounded with
`make prove`, which the sign-off formal gate now runs.
- **As written:** 3 of the 8 files proved directly.
- **The other 5:** the assertion-formal agent strengthened them with
  auxiliary invariants that tie ghost state to real state. Those added 57
  asserts, and every added assert is proven too.
- **Anti-weakening:** `scripts/assert_superset.py` checks that every
  original assert is still present verbatim and that no assume was added.
- **Tooling:** the core files needed state inside child instances
  (`u_regfile.regs`, `u_cycle_counter.count`). That needed a
  `CHILD_DEBUG_PORTS` extension in `formal_common.py`, which the agent
  proposed in scratch and the parent reviewed.
- **Ghost-model bug:** the cycle-counter checker's ghost `elapsed`
  counter wrapped after 2^24 cycles, which would have false-failed a
  correct design. It now saturates.

Coverage is 273/273 reached plus 4 `[deep]` (263 before the chip-level
pin contract, 2026-10-21). Formal alone, without
simulation, now kills all 23 of the original mutation-gate mutants
(`scripts/formal_mutants.py`), up from 16 before the datapath and boot
files: the 7 former blind spots (#3 INB, #7 LOADX, #12 SHIFT; boot-path
writes and echo #10, #19, #22, #23) are each killed by a proven
property. It also kills #24 and #25 (section 21) and #26 (section 22), which
makes 26/26.

**`[deep]` covers (2026-10-18).** A cover whose state lies beyond BMC
depth may be tagged `[deep]` on its source line. `make vacuity` counts an
unreached cover as deep only if the file also carries a
`// deep-evidence: <test>` header naming an `async def <test>` that exists
in `test/*.py` and drives that state in simulation; otherwise it fails as
unreached. The asserts those covers guard are still proven for all
reachable states by k-induction; the tag only answers "is the antecedent
ever true?", which simulation shows. A fake test name fails the gate.

## 7. Golden model

`test/golden_model.py`: from-scratch Python ISA model, one `step()` per
instruction, returning the new state and the exact cycle count. Covers
all 21 opcodes and reserved 21-31. WAIT/IN/INB take an `io_read(t)`
callback so the model consumes the same stimulus timeline as the RTL.
Trusted only after `test_golden_model_selfcheck.py` cross-checks it
against RTL-proven directed tests.

## 8. Agents and contracts

Five generic, project-agnostic subagents are vendored in the repo
(`.claude/agents/`; a project-level definition takes precedence over a
user-level one of the same name), so a clone runs exactly the agents the
ledger recorded. Each reads a contract in its write-scope directory
before doing anything:

| Agent | Contract | Writes | Accepted by |
|---|---|---|---|
| `stimulus-gen` | `test/stim/AGENT_CONTRACT.md` | new stimulus profiles | 200 seeds pass, target bins hit, mutation no drop |
| `coverage-closure` | `test/seq/AGENT_CONTRACT.md` | targeted bursts for thin bins | `make coverage` + `make mutate` no drop |
| `assertion-formal` | `formal/AGENT_CONTRACT.md` | props files | `make vacuity` 100%, `make prove` PROVEN, `assert_superset`, plus a scratch-copy bug injection |
| `triage-debug` | `triage/AGENT_CONTRACT.md` | `verdict.json` | triage bench score (section 16) |
| `red-team` | `redteam/AGENT_CONTRACT.md` | mutant candidates + witnesses | parent re-evaluates every candidate (section 17) |

Agents never grade their own output: the parent session runs every gate.
`.claude/settings.json` denies `Edit(src/**)` and writes to
`.claude/agents/**`, and the parent runs `git diff src/` after every
invocation. `ledger.py finish` also refuses `accepted` if the agent's own
definition changed during the run. The deny rule is defense in depth, not proof.

Coverage-closure bursts accepted so far (`test/seq/`, wired into
`random_gen.CLOSURE_BURSTS`): bit-bang TX loop (OUTB on a pin actually
driving: 21 -> 687 per 100 seeds) and short-timeout WAIT retry (tie on
the expiry cycle 1 -> 15, timeout 3 -> 18). Every remaining open bin under
the default generator is now in `EXPECTED_OPEN` with a stated reason.

Findings from agent runs so far: the stimulus-gen agent found a harness
Clock leak (a new clock per seed, making wall time roughly quadratic in
SEEDS; fixed in 0636edf, 150 seeds 82.8s -> 12.4s).

## 9. Not ported

- **pyuvm**: plain cocotb is enough at this DUT size.
- **constraint_dsl** (Rust/PyO3/Bitwuzla SMT stimulus solver): no
  SMT-hard constraints here (9-bit addresses, 512-byte data region, 19
  opcodes); a seeded RNG covers the space.

## 10. Resolved spec gap: OUT->IN handshake turnaround

Found by `test_host_handshake_in_out`. The original OUT sequence was
firmware-initiated, so the host's final `HOST_GO` fall was the last edge
and nothing acked it. Because `HOST_GO` is also the IN request, a host
that dropped and re-raised `HOST_GO` before any chip clock edge saw it
low deadlocked both sides (reproduced). With two wires taking turns the
side that didn't start an exchange always makes its last edge, so extra
phases alone couldn't fix it. Resolution (docs/architecture.md Host
handshake): OUT is now host-initiated like IN -- host raises `HOST_GO`
to request a byte, firmware `OUT`s it and raises `HOST_STATUS`, host
reads and drops `HOST_GO`, firmware drops `HOST_STATUS`. Every exchange
ends on a firmware edge made after it saw `HOST_GO` low; still 5
instructions per byte. The test now turns every transfer around with
zero gap, the exact case that used to deadlock.

## 11. Physical implementation (hardening) and gate-level simulation

The design hardens through the TinyTapeout GDS flow (LibreLane 3.0.5,
IHP SG13G2 PDK) with the real `RM_IHPSG13_1P_1024x8_c2_bm_bist` SRAM
macro (src/mem/mem.v; until then info.yaml shipped a behavioral array
that synthesized to 8192 flops). First full run, 2026-10-02:

| Metric | Result |
|---|---|
| Std cells / sequential / macros | 1564 / 179 / 1 |
| Utilization (6x4 tiles) | 8.3% |
| Setup slack, 20 ns clock (slow 1.08V/125C, typ, fast) | +5.99 / +9.54 / +11.59 ns, 0 violations |
| Hold slack (slow, typ, fast) | +0.64 / +0.31 / +0.12 ns, 0 violations |
| Routing DRC / LVS / antenna | 0 / 0 / 0 |
| TinyTapeout precheck (KLayout SG13G2 DRC, pins, layers, ...) | all pass |
| Max-slew warnings | 3, slow corner only |

Magic DRC reports ~246k errors from the SRAM macro's own internals; the
macro-integration recipe disables that check (`ERROR_ON_MAGIC_DRC`
false) and the authoritative KLayout SG13G2 DRC in precheck passes.

Gate-level simulation: the GDS action runs the directed suite on the
hardened netlist (`GATES=yes`), with IHP's SRAM model. Tests that only
observe internal RTL state skip there (`GL` flag in test/test.py); the
rest run unchanged. First run: 24/28 pass, the 4 failures were all
internal-signal probes, not design bugs. After the `GL` fix (run
37068387932): 26 pass, 2 skipped, 0 fail.

The workflow's `viewer` job (GitHub Pages deploy) fails because Pages
isn't enabled on this repo; unrelated to the design.

Not done: hardening runs only in the GDS workflow (dispatched manually),
and the random/profile regressions don't run at gate level.

**Port to IHP SG13CMOS5L (2026-10-21), the competition's process.** The
numbers above are SG13G2. The competition shuttle is CMOS5L
(`tt-gds-action@ihp-cmos5l`, `pdk: ihp-sg13cmos5l`), whose tile routes
only Metal1-Metal4: no TopMetal, so the power grid is vertical Metal4
stripes, and the SRAM brings its supplies out as vertical Metal4
columns on that same layer. The macro itself is unchanged (the PDK's
CMOS5L SRAM library is the SG13G2 one); the RTL is unchanged.

- Floorplan: macro flipped (`FS`) at (3.36, 60.48) so its signal pins face
  the logic; grid phased onto its left-array column pairs (width 2.1,
  spacing 3.52, pitch 33.72, offset 6.145).
- `src/pdn_cfg.tcl` bridges grid stripes to every supply column that
  reaches the macro's top or bottom edge, and fails the run if a stripe
  crosses the macro outside a same-net column or if VDD!, VDDARRAY! or
  VSS! is left unconnected. The split VDD!/VDDARRAY! columns have
  macro-internal Metal4 in the gap between them, so no stripe may run
  straight through. First result: VSS! 4 bridges, VDD! 2, VDDARRAY! 2.
- It then keeps the tile's power pins only on full-height stripes
  (precheck requires each to reach both tile edges).

First full CMOS5L hardening (run 39606563369): no setup or hold
violations in any corner at 50 MHz (worst setup slack 3.36 ns, slow
corner), LVS clean, routing DRC clean, gate-level test passing. Precheck
failed only on the power-pin rule, fixed in the pin pass above. STA
flags max slew on three SRAM inputs (A_DIN[0], A_WEN, A_MEN; 0.88 ns vs
the macro's 0.595 ns characterization limit, slow corner) and max load
on A_DOUT[6] (3-5% over): long routes to the macro's pins, ~250-310 um
from minimum-size drivers by the top-edge I/O. Repairing again after
global routing alone (`RUN_POST_GRT_DESIGN_REPAIR`) found the violations
but inserted nothing, because long-wire buffering is off by default
(max wire length 0); with `DESIGN_REPAIR_MAX_WIRE_LENGTH` and
`GRT_DESIGN_REPAIR_MAX_WIRE_LENGTH` at 200 um, run 39845603878 has no
max slew or max cap violations in any corner, no setup or hold
violations, LVS clean, precheck and the gate-level test passing.

## 12. Protocol-level verification

`test/test_uart.py` runs the real UART transmitter firmware end to end:
the host hands bytes over through the IN handshake, the firmware
bit-bangs 8N1 frames on pin 0, and an independent receiver model decodes
the pin as a real UART would. It also checks docs/protocol_timing.md's
timing table against the RTL instead of trusting it:

| Baud | Predicted per-bit error | Measured worst edge deviation |
|---|---|---|
| 115200 | 0 (exact) | 0.000% |
| 57600 | 0.92% | 0.883% |
| 9600 | 0.23% (data bits) | 0.288% (start bit carries -15 cycles) |

Frames decode correctly at all three. Found while writing it: a receiver
must re-arm right after the stop bit's centre sample, not after 10 full
bit times -- at 9600 the transmitter runs 0.23% fast per bit, so the next
start edge arrives inside a naive 10-bit window.

UART RX (`firmware/protocols/uart_rx.asm`) is checked the other way
round: a transmitter model drives the RX pin with back-to-back frames
and the host collects bytes through the host-initiated OUT handshake.
Decodes at 115200 and 9600; a stop bit of 0 raises HOST_ERROR and the
receiver resynchronizes on the next frame. Measured sender-baud margin
at 115200 (probe, not a CI test): slow sender OK to +5.5%, fails at +6%
(stop sample leaves the stop bit -- matches the analytical ~5.3%); fast
sender OK to -4%, loses frames at -4.5%. The fast side is tighter
because a fast sender shortens the gap between the stop-bit sample and
the next start edge, which the unbuffered hand-over must fit in. Both
are well beyond the usual +-2% budget, which CI tests at both ends.
Found while writing it: without an idle-wait (`WAIT RX,1`) before
hunting for the start edge, the frame after a framing error is misframed
(0x7E received as 0xF3) because the line is still low.

SPI and I2C controllers run on the board testbench (section 13).

**Protocols the chip wasn't designed for, and the target role
(2026-10-21).** The architecture planned UART, SPI and I2C, controller
role only. Five more, each from the existing ISA with no hardware
change, each checked against an independent model of the other end and
shown to fail on injected bugs:

| Firmware | Checked against | Result | Fails on |
|---|---|---|---|
| `ws2812.asm` (LED chain, 20 instructions) | decoder written from the WS2812B datasheet | every pulse inside its +-150 ns window, incl. byte-boundary bits; cycle-exact to the header's budget | DELAY one cycle long (mutation #1: still inside the datasheet windows, caught by the exact budget), OUTB on the wrong bit |
| `spi_target.asm` (SPI peripheral, mode 0) | mode-0 controller model | exact up to a 28-cycle SCLK period (1.79 MHz, phase-locked); rated 1.5 MHz for an asynchronous controller | inverted WAIT timeout flag. Dropping a synchronizer flop does not move the limit (instruction count bounds it) and is caught elsewhere |
| `onewire.asm` (1-Wire controller) | device model at the spec's worst case | presence, READ ROM with a valid CRC8, writes; every pulse and slot in spec | sampling 4 us late, a long write-1 pulse, a 450 us reset, an early presence sample; mutations #3, #9 |
| `nec_ir.asm` (NEC IR transmitter) | IR receiver + NEC decoder model | frame decodes; carrier 38.11 kHz, 33% duty, every period in band; leader 8983/4503 us | complement bytes sent uninverted; a carrier 3% fast in one burst (caught only once every period is checked, not the average) |
| `ps2_host.asm` (PS/2 keyboard receiver) | keyboard model that owns the clock and honours the inhibit | scan codes in order, keyboard held off while the host is busy; bad frames dropped | no inhibit (bytes lost); parity check inverted. Found a real firmware race: re-arming before its own CLK release had passed the input synchronizer |

The SPI target supersedes the architecture's "target roles deferred"
decision for SPI (docs/architecture.md); I2C target, which needs two
pins watched at once, stays deferred.

The host side is covered too: `firmware/host/pe_host.py` writes the boot
and handshake protocols once, as generators of pin operations, so the
code the demo board runs is the code `test/test_host_loader.py` runs on
the DUT. Mutation #19 (late boot echo) and #15 (dead HOST_STATUS) fail it.

## 13. Board testbench (loopback and chip-to-chip)

`test/tb_board.v` instantiates two chips (A, B), each with its own clock
and reset, and lets cocotb attach any chip pin to one of 8 nets at
runtime (`board.Chip.connect`). A net resolves like a real wire: a 0
driver wins, else a 1 driver, else the pull-up if the net has one, else
it floats (x). A 0 and a 1 at once is contention: the net reads x and is
recorded in `contention_seen`, so a driver fight fails a test instead of
passing as a silent wired-AND. Unmapped pins behave like a lone TT pad
(the chip reads back what it drives, else what cocotb sets). The
testbench can also drive any net itself, for bus models.

Clocks are independent: `Chip.start_clock(ppm, phase_ps)` sets each
chip's frequency offset (positive = faster) and phase.
`test/test_board.py` checks all of this on its own first. The
single-chip `tb.v` is unchanged, because the TinyTapeout gate-level flow
depends on it; board tests run RTL only.

Loopback self-test (`firmware/protocols/uart_selftest.asm`,
`test/test_uart_selftest.py`): one chip, TX and RX on one net, sends 8
patterns and checks each comes back on the same bit grid. Passes at
115200 and 57600; a missing jumper and an RX stuck high are both reported
as failures (the stuck-high case shows the 0xFF received on uo_out). The
image is also the bring-up test for real silicon with a TX-RX jumper.

Two-chip UART link (`test/test_uart_link.py`): chip A runs `uart.asm`,
chip B runs `uart_rx.asm`, A.TX and B.RX on one pulled-up net, each chip
on its own clock. One host feeds A, another collects from B,
concurrently. CI checks the baseline plus B's clock at +-1% and +-2% off
A's, each at three phases: all 8 payload bytes cross, no framing error,
no driver fight. Measured limit (probe, not CI): B +5% OK / +6% framing
errors, B -4% OK / -5% frames lost, phase-independent -- matching the
single-chip margin found with the Python transmitter model, now with two
real asynchronous clock domains. This complements the independent
receiver/transmitter models rather than replacing them: two copies of
our own firmware could share a wrong assumption and still agree.

Found while building it: cocotb 2.0's Clock runs at simulator level, so
re-starting a chip's clock inside one test left the old one driving the
same signal; it surfaced as a phase-dependent failure only when a later
run changed frequency. `board.Chip.start_clock` now stops the previous
Clock object (cancelling the wrapper task isn't enough), and rounds the
period to an even ps (cocotb rejects odd periods).

I2C (`firmware/protocols/i2c.asm`, `test/test_i2c.py`): chip A's SDA/SCL
on two pulled-up nets with an independent I2C slave model that only ever
pulls low or releases. The model decodes the bus cycle by cycle, ACKs its
own address, can NACK a chosen byte, stretch the clock or hold SCL low
forever, and checks the master against the I2C spec minimums for the
mode (SCL low/high time, data setup, START hold) and against
START/STOP inside a byte. Seven cases pass:
- write and read at 100 kHz; write then read back-to-back at 400 kHz
  (master ACKs all read bytes but the last);
- address NACK (status 1, STOP, bus released, no HOST_ERROR);
- data NACK on the 2nd byte (statuses 0,0,1, master stops there);
- 1500-cycle clock stretch after every byte, both directions;
- SCL held low forever: stretch timeout, status 3, HOST_ERROR, no hang.

Measured minimums at 100 kHz: SCL low 250 / high 247 / data setup 241 /
START hold 256 cycles (spec 235 / 200 / 13 / 200); at 400 kHz 70 / 52 /
61 / 61 (spec 65 / 30 / 5 / 30). Checked the timing checks can fail: an
SCL low of 150 cycles is rejected (`SCL low 150 < tLOW 235`).

SPI (`firmware/protocols/spi.asm`, `test/test_spi.py`): mode 0, MSB
first, full duplex, against an independent slave model selected by CS
that samples MOSI on SCLK rise and shifts MISO on fall -- a master on the
wrong edge or bit order gets the wrong bytes (checked: sampling MISO
after the falling edge turns 0x10 0x02 0x7F 0xC3 0xA5 into 0x20 0x04 0xFF
0x87 0x4B). The model also checks SCLK idle low at CS fall, no SCLK
edges while CS is high, CS released only on byte boundaries, MOSI setup,
and CS setup/hold. Four cases pass: single byte, 5 bytes in one
transaction, two transactions, and the fastest SCLK (12-cycle halves,
~2.08 MHz). Measured: SCLK halves exactly 25 (1 MHz) / 12 cycles, MOSI
setup >= 19 / 6 cycles, CS setup >= 69 / 43, hold >= 53 / 40.

SPI modes 1-3 (same file, `-D MODE=n`; CPHA picks the bit loop through
the assembler's `.if`): the slave model was generalized to any mode
(leading vs trailing edge by CPOL, sample/launch by CPHA), and every mode
passes at the default and the fastest clock (12-cycle halves for CPHA 0,
15 for CPHA 1). Checked that the mode tests discriminate by running
firmware for each mode against a slave expecting each other mode: all
12 mismatches fail. Two of them (0 vs 1, 2 vs 3) first passed -- the
master samples MISO late enough to read either CPHA's data, so data
alone can't tell CPHA apart -- which led to the check that actually
defines CPHA: MOSI may only change while SCLK is at the mode's launch
level (idle for CPHA 0, active for CPHA 1).

All three baseline protocols (UART, I2C, SPI) now have firmware verified
end to end on the RTL against independent models.

Found while building I2C: `board.drive_net` did a read-modify-write of the
external-driver registers, and cocotb applies writes at the end of the
time step, so a second call in the same cycle (the slave driving SDA and
stretching SCL together) silently undid the first. It now keeps a Python
shadow of the driver state.

## 14. Sign-off and the definition of "stable"

`make signoff` (`scripts/signoff.py`) runs every gate and writes
`signoff_report.json`; it exits non-zero if any gate fails.

| Gate | Pass criterion |
|---|---|
| `directed` | `make -C test -B`: all directed tests, the protocol firmware end to end (UART, WS2812, SPI target, 1-Wire, PS/2) and the host library pass (60) |
| `slow_protocols` | `make -C test slow`: protocol tests too slow to rerun per mutant (NEC IR, one 68 ms frame) |
| `pytest` | golden-model self-check + assembler unit tests pass |
| `board` | `make -C test board`: loopback, two-chip link, I2C, SPI pass |
| `cov_default` | `make coverage-gate`, 100 seeds |
| `cov_<profile>` | `make coverage-gate STIM_PROFILE=<profile>`, 50 seeds, each of the 4 profiles |
| `determinism` | 25 random seeds run twice give byte-identical per-seed coverage JSON |
| `formal` | `make prove`: every props file PROVEN (BMC + k-induction) |
| `vacuity` | `make vacuity`: every cover reached, or `[deep]` with simulation evidence |
| `tieoffs` | `scripts/check_tieoffs.py`: SRAM macro static pins as validated (section 20) |
| `spec` | `make -C test spec`: the spec-only conformance suite passes (section 21) |
| `rtl_coverage` | `make rtl-coverage`: Verilator line/toggle coverage of `src/`, every hole closed or waived (section 22) |
| `mutation` | `scripts/mutate.py` (directed + random + board), score >= threshold, nothing errored |

The determinism gate exists because a failing random seed is only useful if
it replays: anything that leaks wall-clock time, global RNG state or
iteration-order dependence into stimulus shows up there first.

The report says `STABLE` only when every gate ran and passed on a tree with
no uncommitted changes under `src/ test/ scripts/ formal/ firmware/`.
`--skip` is for iterating locally; a skipped gate makes the run partial,
not a sign-off.

**The environment counts as stable when all of these hold:**

1. `make signoff` reports `STABLE` on 3 consecutive nightly runs
   (`.github/workflows/nightly.yaml`) with no change to the gates in between.
2. The per-push `test` workflow is green on the same commit.
3. The `gds` workflow (hardening, precheck, gate-level test) is green on
   the latest RTL change.
4. No open harness or model bug from the scrub list.

Any change to `src/` resets the count. New work that depends on stability
(stretch protocols, a second N-version model) waits for it.

Nightly: `.github/workflows/nightly.yaml` runs `make signoff` at 03:00 UTC
and on manual dispatch, with OSS CAD Suite for yosys/yosys-smtbmc/z3, and
uploads both JSON reports. On this fork, scheduled runs only fire once
workflows are enabled in the repo's Actions tab; until then dispatch it
with `gh workflow run nightly.yaml -R PankajNair/protocol-emulator-tt`.

Scrub items cleared without a code change: `test.py`'s `_ensure_clock` was
suspected of stacking clocks across tests like `fast_boot` and
`board.start_clock` did. A probe showed 50 edges/us within a test after
a second call, 0 edges/us at the start of the next test, and 49 after
`_ensure_clock`. cocotb stops the clock between tests and the helper
never starts a second one, so it does not stack.

## 15. Agent run ledger

`ledger/runs.jsonl` is the append-only record of every agent run, written by
`scripts/ledger.py` from the parent session (agents never write it). It is
what the agent metrics are computed from, and it turns "the gates passed"
from a claim in a commit message into recorded exit codes.

```
RUN=$(python3 scripts/ledger.py start --agent stimulus-gen \
        --contract test/stim/AGENT_CONTRACT.md --task "..." [--prompt-file p.md])
# ... run the agent ...
python3 scripts/ledger.py gate $RUN --name seeds -- \
        bash -c 'make -C test random SEEDS=200 && ! grep -q failure test/results.xml'
python3 scripts/ledger.py gate $RUN --signoff        # after make signoff
python3 scripts/ledger.py finish $RUN --verdict accepted|rejected --reason "..." \
        [--commit SHA] [--bugs-found "..."] [--exclude-commit SHA] \
        [--tokens N --tool-uses N]                   # from the agent's completion notice
make ledger                                          # stats + recent runs
```

Each record holds:
- the agent, plus hashes of its definition (`.claude/agents/<agent>.md`,
  vendored) and of its contract, so results can be split by prompt version;
- tokens and tool calls, copied from the agent's completion notice
  (recorded from 2026-10-18; earlier runs have none);
- the task, plus the full prompt when `--prompt-file` is given (copied to
  `ledger/prompts/`);
- the base commit and the files the run changed;
- any scope violations, the gates with their real exit codes, the verdict
  and reason, the commit and the bugs found.

`start` snapshots the working tree, so files that were already dirty
don't count against the agent unless they change again. An agent
working in its own copy outside the repo is started with `--tree DIR`,
and the scope check then hashes that tree. Commits the parent itself
makes while a run is open are excluded with `--exclude-commit`, since
the ledger can't tell who wrote a commit. Likewise, in tree mode,
`--exclude-file` excludes files the parent itself put into the agent's
tree. Tree mode skips build output (`sim_build/`, `target/`).

`finish` refuses `accepted` when:
- no gate was recorded;
- any gate failed;
- a changed file is outside the contract's `write_scope` or under
  `protected_paths`;
- the agent's definition file changed since `start`.

`--override REASON` records a deliberate exception instead. `gate --signoff`
refuses a `signoff_report.json` older than the run's start.

cocotb's `make` can exit 0 with failing tests, so a simulation gate must
also check `results.xml`, as in the example above.

Backfill: the 11 agent commits made before the ledger existed are recorded
with `"source": "backfill"`:
- 4 stimulus-gen;
- 1 coverage-closure;
- 6 assertion-formal.

They are reconstructed from git: verdict `accepted`, no gate records, no
per-file scope check (those commits mix agent output with parent wiring).
Rejected attempts from that period weren't kept, so the acceptance rates
in `make ledger` only mean something for live runs.

## 16. Failure triage

A failing seed becomes a classified, minimized, evidence-backed verdict
through three parts:
- deterministic tooling owned by this session;
- the user-level `triage-debug` agent, under `triage/AGENT_CONTRACT.md`;
- a planted-bug benchmark that measures the agent.

**Harness support (`test/test_random.py`).** `PROGRAM_FILE` runs a given
program instead of generating one; pin stimulus still comes from the
seed. Every failure writes `regression_artifacts/seed_N/failure.json`
with:
- the failing check and the RTL and golden values;
- the instruction index and cycle;
- the last 16 commits, each with its disassembly and the RTL and golden
  cycle counts;
- the golden state after the failing instruction.

`random_gen.disassemble` decodes every field. It is derived from the
assembler's encoders, not from the golden model's decoder, so a decode
bug in the model can't hide in its own trace.

**`scripts/triage.py`:**
- `replay` runs one seed, or a program file, and prints the failure
  record.
- `minimize` is a ddmin over instructions. It replaces instructions with
  NOP so addresses stay put, and keeps a candidate only if it still
  fails the same check. The golden model pre-screens candidates that
  would not halt, so those are never simulated. Typical result: 72
  instructions down to 2-4 in 35-45 simulations, about 1.5 minutes.
- `bisect` keeps today's harness and model, swaps in `src/` from each
  commit, and finds the first RTL commit that fails. It runs in a
  throwaway worktree.
- Runs in one tree hold a lock. Two overlapping minimizer runs in one
  tree once produced a "minimal" program that then passed.

**Agent.** `.claude/agents/triage-debug.md` is generic. The contract
gives the classes (DUT, REFERENCE_MODEL, HARNESS, SPEC_AMBIGUITY,
NOT_REPRODUCIBLE) and the verdict JSON format. The spec is the referee:
the agent derives the correct value by hand, then decides which side
departs from it. It never fixes anything. The verdict JSON goes in
`triage/reports/<id>/`. The write-up comes back as text, because the
harness blocks subagent report files.

**Benchmark (`scripts/triage_bench.py`).** Seven planted bugs:
- 3 in the DUT;
- 3 in the reference model;
- 1 in the harness.

Each is planted in a standalone copy of the repo. The copy has no git
history, no copy of the bench file, and has comments that would narrate
the fix scrubbed. Two pairs share a symptom but have different owners,
and only discriminating evidence separates them. `score` checks the
classification and the culprit file against ground truth. Each run is
recorded in the ledger with `start --tree <copy>`, so the scope check
covers the agent's own tree.

First round (2026-10-04): 7/7 classified correctly, 7/7 right culprit
file, all at high confidence, and every stated replay reproduced. The
round also found:
- the overlapping-run minimizer bug (reported by the agent; fixed with
  the lock);
- two answer leaks through code comments (now scrubbed);
- harness-blocked report files (contract changed);
- a grader regex bug;
- the ledger blaming the parent's own mid-run commits on the agent
  (fixed with `--exclude-commit` and `--tree`).

That round ran before the comment scrub, so treat it as an upper bound.

Round 2 (2026-10-04, scrubbed trees, ledger in `--tree` mode): again
7/7 classification, 7/7 culprit file, all at high confidence, every
replay reproduced, no scope violations. Each verdict rests on
discriminating evidence rather than comments:
- INB: a bit-0 control on the same pin and cycle passes.
- SHIFT: the golden model run alone gives 0x01 -> 128.
- DELAY: `DELAY 0` correct, `DELAY 1` +1, in an IO-free program.
- Synchronizer: the ui path validates `SYNC_DELAY` while the uio path is
  one cycle fast. Both cycle counts are hand-derived from the raw
  stimulus.
- SYNC_DELAY: the IN value equals raw(t-3) against an exact 2-flop RTL.

Honest limit: this repo records correct values in several places
(`docs/VAL.md`, the stim contract, formal props headers, the model's
docstring). So the DELAY and SYNC_DELAY cases still measure *regression*
triage: code contradicting its own docs, which is also what a real
revert looks like. The INB, CMP and SHIFT cases have no history
anywhere and measure reasoning from the spec alone.

The round found two real gaps in the live environment:
- The golden-model self-check and the directed `test_shift` only
  shifted values whose vacated bit was 0, so a rotating SHIFT passed
  both. Fixed: `test_shift_vacated_bit_is_zero` (RTL) and
  `test_shift_vacated_bit_is_zero_not_rotated` (model). The model test
  fails against the planted rotating model.
- `SYNC_DELAY` was calibrated from simulation, so nothing pinned the
  absolute synchronizer depth. A mutant with an extra flop on both paths
  and `SYNC_DELAY` recalibrated to 4 did fail the random regression, but
  only incidentally, through `fast_boot`'s fixed START hold window and
  then cycle counts. Closed with two independent checks:
  - The directed test `test_sync_depth_matches_harness_sync_delay`
    measures the port's own lag (1) and the synchronized lag (3) under
    the harness's read pattern. It asserts a depth of exactly 2 on the
    ui and uio paths and that `SYNC_DELAY` equals the measured lag. It
    fails on both the `SYNC_DELAY=2` mutant and the common-mode mutant.
  - `formal/agent_pin_ctrl_sync_depth_props.v` proves the 2-cycle depth
    for every consumer, including the `pin_read` tap.

**Round 3 (2026-10-21): harder cases, opaque trees.** Seven new cases
(`r3_*` in `scripts/triage_bench.py`):
- **Timing-only symptoms** (a `cycles used` mismatch, no wrong value),
  as a same-symptom pair: OUTB taking 4 cycles in the DUT, or in the
  model.
- **A rare trigger:** the WAIT tie rule, again as a DUT/model pair.
  Only seeds where the pin matches on the expiry cycle fail.
- **A harness bug that reads as a DUT bug:** the harness skips Rs
  instead of Rd at a LOADX commit, so Rd is compared before its
  documented trailing writeback lands.
- **A DUT bug that reads as a harness bug:** a third synchronizer flop
  on `pin_read`, which looks like `SYNC_DELAY` being off.
- **Two live bugs:** a TEST-bit index reversal, plus a decoy (the model
  WAIT tie). Setup picks a seed that fails the same check with the
  primary alone and passes with the decoy alone, and checks that the
  decoy still fails some other seed.

Building the round exposed two leaks that also affected rounds 1 and 2:
- **Bug catalogues in the tree.** `scripts/mutate.py` holds several
  planted edits verbatim (`dut_wait_tie` is mutant #6, and round 1's
  `dut_inb_invert`, `dut_sync_drop` and `dut_delay_offbyone` are #3, #2
  and #1). `docs/VAL.md` and `formal/README.md` describe them. Bench
  trees now leave out the mutation and red-team tooling and these docs,
  and the tree's contract says so. An audit of all 14 round-1/2
  transcripts found no agent that read any of those files.
- **The tree's own path.** Directories named `bench/model_cmp` or
  `r3_two_bugs` name the answer. The first round-3 launch was voided
  when the two-bug agent remarked on its tree's name. Its 7 ledger runs
  are recorded as rejected `VOID (parent error)`, and metrics leave void
  runs out. Trees are now `case_01`..`case_07` in shuffled order, with
  the map kept outside them. Rounds 1-2 cannot be re-checked for this
  leak: their reasoning cites spec and code evidence throughout, but a
  path hint can't be ruled out.

**Result: 7/7 classification, 7/7 culprit file**, all high confidence:
- The DUT/model pairs were separated by the spec's 3-cycle rule and its
  tie rule.
- The LOADX agent wrote three discriminating programs (LOAD; LOADX with
  Rd=Rs; LOADX with Rd!=Rs) showing that the failure follows the
  harness's skip index.
- One WAIT-tie agent removed the tie (timeout 31) to show the sampling
  paths agree.
- The two-bug agent found the primary without being misled by the decoy.

Every replay reproduces. The independence audit is clean once two of
my own audit bugs are fixed:
- It flagged the agent's own background-task output.
- It flagged the tree's deliberate `.venv` symlink.

The ledger keeps the first, failing gate runs and records the
overrides. Cost (`make metrics`): 265,557 tokens for the 7 runs, about
38k per accepted triage.

Also noticed by an agent: `test_sync_depth_matches_harness_sync_delay`
measures `uio_in_ff2`, not `pin_read`, so it cannot see an extra flop
on the WAIT/INB tap. The formal sync-depth props and the random
scoreboard still catch it (case_01 failed in random within seed 1).

## 17. Red team

The red-team agent attacks the verification environment itself. It
proposes small, realistic RTL mutants that change spec-defined
behaviour and that it argues every gate misses. Each mutant comes with a
witness: a test that does catch it, which proves the mutant is not an
equivalent rewrite.

**Tooling: `scripts/redteam_eval.py CANDIDATE.json [--full]`.**
1. Apply the mutant to a scratch copy of the repo (this repo's `src/` is
   never touched).
2. Run every gate on the copy: directed, 200 random seeds, board, and
   formal. `--full` adds the 4 stimulus profiles. Any failing gate
   means the mutant is KILLED.
3. If every gate passes, check the witness. It must FAIL on the mutant
   and PASS on a clean copy.

Witnesses come in two forms:
- a random-harness program, replayed with `triage.py`;
- a cocotb test module, for behaviour the golden model doesn't cover
  (boot, host pins, unbounded WAIT).

Verdicts: KILLED, SURVIVED_WITNESSED (a real gap), SURVIVED_NO_WITNESS
(possibly equivalent, so not a finding), INVALID. The agent may
pre-screen with this tool. The parent re-runs it with `--full` as the
gate and trusts only that run.

**Agent.** The agent definition is `.claude/agents/red-team.md`; the
contract is `redteam/AGENT_CONTRACT.md`. The agent writes only
`redteam/candidates/` and evaluates at most 8 candidates per run. It
must:
- map what each gate checks and what it can't see before proposing
  anything;
- quote the spec text each mutant violates;
- keep witnesses to spec-defined behaviour.

**First pass (2026-10-04, ledger 20261004-094108).** 2 candidates
evaluated, both SURVIVED_WITNESSED on the parent's `--full` re-run. The
agent rejected 6 more ideas itself before evaluation, as already covered
or equivalent.

- **rt01: counter 20 bits wide instead of 24.**
  - Spec: isa.md says DELAY reaches about 16.7M cycles at exponent 3.
  - Why every gate missed it: no test, generator profile or board
    firmware used an exponent-3 mantissa above 1, and every WAIT
    timeout fits in 20 bits. Formal at depth 20 can't see a count that
    ends early but still exceeds 32767.
  - Killed by `test_delay_exp3_full_counter_width`, which runs DELAY
    511 at exponent 3. It checks that the counter loads exactly N-1 (a
    one-cycle check, independent of width) and that the DELAY hasn't
    ended after 100k cycles.
- **rt02: boot echo updated on the HOST_GO fall instead of with the
  write.**
  - Spec: architecture.md's LOAD handshake says the echo is "now
    guaranteed valid" when HOST_STATUS rises.
  - Why every gate missed it: every test read the echo only after the
    handshake had finished. Random boots by poking SRAM, and no formal
    property looks at `uo_out` in LOAD.
  - Killed by `load_byte(..., expect_echo=)`. Both boot paths
    (`reset_and_boot` and the board's `Chip.boot`) now check the echo
    at the ack, so every booting test exercises it.

Both are now mutants #18 and #19 in `scripts/mutate.py`, killed.

**Second pass, the orchestrator's first real cycle (C20261010-064758,
ledger 20261010-211840).** Signoff was clean at 64dcc61. 3 candidates
were evaluated, and all 3 were SURVIVED_WITNESSED on the parent's
`--full` re-run, so the cycle halted as designed.

- **rt03: WAIT's has-timeout check computed in 16 bits.**
  - Effect: WAIT with exponent 3 and an even mantissa (2 << 15 has
    zero low bits) becomes unbounded.
  - Why every gate missed it: the only expiring wide WAIT anywhere was
    the board's 1 << 15.
  - Killed by `test_wait_timeout_exp3_even_mantissa`, which checks
    that the timeout fires within the exact cycle window.
- **rt04: `expired` compares `count[19:0]`.**
  - Effect: DELAY exponent 3 ends about 1M cycles early. rt01's fix
    pinned only the counter register, not its comparator.
  - Killed by `test_delay_counter_expires_only_at_zero`. It sets the
    counter to each power of two from bit 8 to bit 23 in the middle of
    a long DELAY and checks the DELAY keeps running, so a narrow
    comparator of any width fails within a cycle.
- **rt05: boot write dropped at address 1023.** This is the off-by-one
  that "saturate, then no-op" invites.
  - Killed by `test_boot_writes_last_byte`: exactly 1024 boot pulses,
    then LOAD byte 1023 back.

All three are now mutants #20-22, killed. The mutation gate is 22/22.

**Spec deviation raised by the agent: resolved (RTL fix, signed off
2026-10-11).** After the boot counter saturated, every over-run HOST_GO
pulse still rewrote byte 1023 (`mem_we = host_go_rise && !load_ack`).
architecture.md says over-run pulses are "harmless no-ops", which is
also the industry convention for loaders and FIFOs: drop the write and
keep the data already written. Wrapping and rewriting the last location
are the other two conventions, and the spec rejects both.

The fix adds a 1-bit `boot_full` register to `core.v`. The write to
address 1023 sets it, reset clears it, and once it is set boot writes
stop. Over-run pulses are still acked and echoed (the echo holds at
0xFF), so the host never hangs and can see the counter has stopped
advancing. Gating on `boot_addr == 1023` instead would have dropped the
1024th byte (rt05), which `test_boot_writes_last_byte` and mutant #22
now catch.

`test_boot_echo_and_saturation` now puts 0x5A at byte 1023 and checks
that it survives the over-run pulses. Mutant #23 removes the gate and is
killed.

## 18. Orchestrator

`scripts/orchestrate.py` (`make orchestrate`) runs the agentic DV
pipeline as a resumable state machine. Its state is kept in
`orchestrate_state.json` (gitignored), and each finished cycle is
appended to `ledger/cycles.jsonl`.

| Stage | Who | What | Gate (run by the script, recorded in the ledger) |
|---|---|---|---|
| signoff | script | `scripts/signoff.py`, every gate (§14) | the report itself |
| triage | `triage-debug` agent, one ticket per failing random seed | classify a failure as DUT, model or harness (§16) | verdict schema; the replay command reproduces the stated check; the culprit lines exist |
| redteam | `red-team` agent, only after a clean signoff | propose surviving, witnessed mutants (§17) | `redteam_eval.py --full` on each candidate |
| close | script | cycle summary | -- |

**Tickets.** A stage that needs an agent does three things, then
pauses:
1. opens a ledger run (with `--tree` when the cycle runs on another
   tree);
2. writes a ticket holding the agent type and the full prompt;
3. stops.

The parent session then:
1. runs `orchestrate.py ticket` to read the ticket;
2. launches that agent with that prompt, unchanged;
3. runs `orchestrate.py complete T<n>`.

`complete` runs the stage's gates, records `accepted` or `rejected` in
the ledger, and advances the cycle. The agent never sees or runs a gate.

**Stop rules.** The cycle halts and waits for a human. It never fixes
anything. It halts when:
- `src/` is dirty at start (the cycle refuses to begin);
- a triage verdict is accepted, i.e. a confirmed bug that needs a
  human fix;
- a non-random gate fails (directed, board, formal or vacuity);
- a red-team candidate is confirmed as SURVIVED_WITNESSED;
- an agent ticket is rejected;
- the ticket budget (`--max-tickets`, default 3) runs out.

`--root DIR` runs a cycle against another tree, such as a triage
benchmark copy. `--signoff-skip` and `--reuse-signoff` are for testing
and for re-entering a cycle.

## 19. Agent metrics

`make metrics` (`scripts/metrics.py`, `--json` for machine-readable
output) derives every number on demand from data the ledger already
holds:
- `ledger/runs.jsonl`;
- `ledger/cycles.jsonl`;
- `scripts/mutate.py` MUTANTS;
- `redteam/candidates/`.

Nothing extra is stored, so the metrics can't drift from their sources.

| Group | Metric | Definition |
|---|---|---|
| per agent | acceptance | accepted / live runs. Backfilled runs only record accepted work, so they are excluded from rates. |
| | first-pass | every gate passed on its first attempt: no override, no re-run gate |
| | overrides, scope flags | raw counts. A flag with an override was a deliberate exception, and its reason is in the ledger. |
| | median minutes, findings | findings = `bugs_found` entries plus parent-confirmed red-team survivors |
| agent cost | tokens, per accept, per finding | over the runs whose `finish` recorded `--tokens`; `tok_runs` says how many that is |
| triage | benchmark | bench-score gate passes per round |
| | replay reproduced | the agent's stated replay command reproduced its stated check |
| red team | yield | parent-confirmed survivors / candidates the parent evaluated |
| | precision | parent-confirmed / agent-claimed survivors |
| | in mutation gate | confirmed survivors now in MUTANTS |
| environment | mutants by origin, cycle outcomes | origin parsed from each mutant's `bug_ref` |

Snapshot (2026-10-11):
- **triage-debug:** 15 live runs, 100% accepted, 73% first-pass.
  - The 4 that weren't first-pass are all from round 1: 3 ledger scope
    false positives (the parent's own mid-run commits, before `--tree`
    and `--exclude-commit` existed), and 1 grader regex bug.
  - Benchmark 7/7 in both rounds. Replay reproduced 14/15; the
    exception is the grader-bug run, which reproduced on re-run.
- **red-team:** 2 passes. 5/5 candidates were confirmed survivors, so
  yield and precision are both 100%. All 5 are now mutation-gate
  entries (#18-22).
- **Mutation gate (23 entries):**
  - 13 curated;
  - 6 from the red-team agent (#18-23, counting the spec deviation it
    raised);
  - 2 past real bugs;
  - 2 blind-mutation survivors.
- **Orchestrator:** 1 real cycle (halted on red-team survivors), plus 1
  end-to-end test cycle.

Caveats:
- The samples are small. A 100% red-team yield on 5 candidates says the
  agent pre-screens well, not that every pass will find gaps.
- Every red-team survivor so far is a width or long-range timing gap.
  When the yield drops, that class has been worked out.
- Token cost is recorded only from 2026-10-18 (`finish --tokens`). Older
  runs could only be matched to their completion notices by time and
  agent type, so they were not backfilled.

## 20. Blind mutation campaign

The mutation gate's 23 mutants (section 5) were each chosen by someone
who knew the gates, so killing them shows the known gaps are closed, not
how many are left. `scripts/blind_mutate.py` (`make blind-mutate`)
generates mutants mechanically from `src/` with standard operator rules
and no human choosing sites:
- relational, logical, bitwise and arithmetic operator swaps;
- sized constants ±1;
- negated `if` conditions;
- deleted assignments.

Each mutant runs in a private copy of the tree through compile, then
simulation (directed, 50 random seeds, board; first failing suite
wins), then formal alone (`make prove`). Results go to
`blind_mutation_results.jsonl` (gitignored) and the campaign resumes
where it stopped.

Sites inside `for (...)` headers are excluded. Those loops only
replicate logic; a step mutated from `i + 1` to `i - 1` never ends, and
the simulator then spins at time 0 allocating memory (one reached 3 GB
in 3 minutes).

**Census, 2026-10-19: all 455 sites, no sampling.**

| | kills |
|---|---|
| live mutants (3 more did not compile) | 452 |
| simulation | 381 (84.3%) |
| formal alone | 370 (81.9%) |
| tie-off check (below) | 15 |
| any gate | 427 (94.5%) |
| survivors, all **proven** equivalent (below) | 25 |
| **score excluding equivalent mutants** | **427/427 (100%)** |

The **25 equivalent survivors** are listed with reasons in
`blind_mutate.EQUIVALENT`. Each entry is pinned to its source line's
exact text, so an RTL edit there makes the survivor count as unexplained
again, and `report` then exits 1. The mutants fall into four groups:
- defaults in combinational muxes that are overridden whenever they
  matter;
- reset values of registers that are rewritten before they are read;
- lint tie-offs with no reader;
- `isa_defs.v` macros referenced only in comments.

**All 25 are proven equivalent, not just traced**
(`scripts/prove_equiv.py`, `make prove-equiv`, about 25 minutes). Per
mutant, the prover builds a sequential miter of the whole chip:
- **Two copies:** the original and the mutant, given the same inputs.
- **One shared SRAM:** the macro is cut out of both copies
  (`expose -evert`) and replaced by a model of IHP's behaviour: enabled
  while `rst_n`, a read every cycle, write-through. Its contents start
  arbitrary and identical in both copies.
- **The claim, asserted every cycle:**
  - `uo_out` and `uio_oe` are equal;
  - `uio_out` is equal wherever its enable is on (a disabled output
    never reaches the pad);
  - both copies issue the same SRAM writes. This also makes the
    single shared array exact.
  - These claims are checked while `rst_n` is high. `async2sync`
    models the async reset synchronously, so in a reset's first cycle
    the flops still hold arbitrary values that silicon would already
    have forced. That once produced a bogus "difference" during reset.
    Nothing is lost: a reset-value difference still shows on the pins
    the cycle after release, and the SRAM ignores writes during reset.
- **Strengthening asserts:** every flop is equal, except a per-mutant
  `ALLOWED_DIFF` list. Each entry comes with invariants that are
  asserted, never assumed, so they are proven too.
- **Proof:** BMC from reset plus 20-step induction.

`core.v:281` (the default SRAM address) needed three design invariants:
- the read data may differ only where nothing reads it;
- the deferred LOAD/LOADX writeback flag is only ever seen in FETCH_LO;
- a halted core never decodes the live SRAM byte, because HALT clears
  `entering_execute` on the same edge it commits.

Results are recorded in `formal/equiv_proofs.json` with a hash of the
RTL and of the prover. `report` labels a survivor "proven" only while
that hash matches; after any RTL change it falls back to "traced".

**Negative control** (`--controls`): a prover that proves everything is
worthless. Four campaign-killed mutants, one per part of the miter, must
each be refuted by a real counterexample from reset, using only the pin
and write asserts:

| Control mutant | Refuted at | By the assert on |
|---|---|---|
| `uo_out` reset value | step 0 | `uo_out` |
| HOST_STATUS output enable | step 0 | `uio_oe` |
| OUT taken for every other opcode | step 10, a program running from the shared SRAM | `uo_out` |
| STORE data deleted | step 9 | the writes |

All four are refuted. Refutation searches only to depth 12: with two
chip copies and the SRAM array, z3's search time grows about 5× every
2 cycles (depth 14 takes ~2 minutes).

One survivor shows the gates are stricter than the pins.
`core.v:318 2'd0 -> 2'd1` is proven equivalent at the pins, yet the
random test kills it. The random test compares `pin_ctrl`'s internal
`mode[]` against the golden model, and that mutant leaves `mode[5]` (a
pin whose role is hardwired) holding push-pull instead of input.

**Real gap found: the SRAM macro's static pins.** Before the tie-off
check, 11 `mem.v` mutants survived every gate: `A_DLY` and the
`A_BIST_*` inputs. IHP's behavioral model declares `A_DLY` but never
reads it, and reads the BIST inputs only when `A_BIST_EN` is 1. On
silicon they are real inputs, and no simulation or formal gate here can
see them. `scripts/check_tieoffs.py` (`make tieoffs`, now a sign-off
gate) checks every macro port against the validated recipe in the
`mem.v` header. It kills all 15 `mem.v` mutants.

**Killed by formal only (35).** These mark where simulation is blind
and formal is the only gate. Nearly all are reset behaviour that
simulation never varies:
- `uo_out`'s reset value, which is visible on the pins in LOAD before
  the first boot pulse;
- the synchronizer and edge-detector flops in `pin_ctrl.v`, which
  matter when START or HOST_GO is already high as reset releases;
- a reset that lands while a LOAD/LOADX writeback is still pending;
- the PC reset, which START overwrites anyway.

Simulation always resets the same way, from a quiet bus. A directed
test that holds START and HOST_GO high across reset release, and resets
in the middle of a pending writeback, would make simulation catch these
on its own; for now `agent_core_reset_props.v` and the `pin_ctrl`
props cover them.

`isa_defs.v` shows formal's scope: it kills 36 of 60 opcode and field
mutants, against 56 for simulation. The props check behaviours, not
the whole decode table, while the random differential test exercises
every opcode against the golden model.

## 21. Spec-only independence check

Every other check here was written by the same team that read the spec
and also read the design. If the spec was misread, the RTL, golden
model, tests and formal props agree and are all wrong together, and
mutation testing cannot see it. So a fresh agent, `spec-conformance`
(`.claude/agents/spec-conformance.md`, contract
`spec_tests/AGENT_CONTRACT.md`), wrote a black-box suite from the spec
alone.

**What it got:**
- `isa.md`, `architecture.md`, `protocol_timing.md` and `info.md`;
- `tb.v`, which exposes pins only.

**What it did not get:** `src/`, the model, the existing tests, the
formal props, this document, or git history.

**How it worked:**
- It wrote its own encoder, assembler, boot-handshake driver and pad
  model.
- It cites a spec sentence for every expected value.
- It never ran anything against the design.

**Independence is audited, not assumed.** `scripts/audit_agent_paths.py`
checks every tool call in the agent's transcript for paths outside its
tree. That covers Read/Write paths, absolute paths in Bash, and `../`
paths resolved against the command's own `cd`. Run
`20261020-125357-spec-conformance` made 4 out-of-tree calls, all to a
stub directory it made itself, which imported only its own two files
under a fake `cocotb`. The ledger records the audit as failed, with an
explicit override.

**Result: 62 tests, 62/62 pass on the RTL at first contact** (`make -C
test spec`, now a sign-off gate). On its own, the suite kills 16 of the
23 curated mutants:
- **Correctly missed (#4, #5, #8):** the debug flags reach no pin.
- **Missed (#18, #20, #21):** counts past 2^20 cycles, beyond its cycle
  budget.
- **Missed (#2), one synchronizer flop dropped:** the spec states only
  a relative "+2 cycles" of synchronizer latency, never the total time
  from a pin edge to the WAIT exit. Our own test pins that total
  against a value calibrated from the RTL.

**Spec findings.** These are items the agent had to assume, or found
ambiguous; the RTL's choice agrees with the agent's reading in every
case. All five are now written into `isa.md` and `architecture.md` as
the RTL behaves (2026-10-20). F3's number was measured at the pins,
and the measurement tells the RTL (6 edges) apart from curated
mutant #2 (5 edges).

| | Item | Spec |
|---|---|---|
| F1 | OUTB/INB `bit_select`: which value selects bit 7 | missing (width only; only `isa_defs.v` pins it) |
| F2 | HOST_GO pulse after saturation: is it acked? | ambiguous ("harmless no-op" vs "acks each write") |
| F3 | total WAIT reaction latency, pin edge to exit | missing (relative "+2" only) |
| F4 | SET's mode field on fixed-role pins 5/6 | silent |
| F5 | LOOP with Rd = 0 | implicit (8-bit decrement wraps: 256 iterations) |

**Pass 2** (run `20261020-175337-spec-conformance`): the same agent
re-read the clarified docs, still in its own tree, and replaced every
ASSUMPTION/AMBIGUOUS marker on F1-F5 with a citation of the new text,
tightening each check to what the spec now states.
- Its WAIT test now checks the exact 6-edge reaction, for rising and
  falling levels, on pins 1, 7 and HOST_GO. That kills #2, so the suite
  alone now kills **17/23** curated mutants.
- The 6 it misses are the 3 debug-flag mutants, which no pin shows, and
  the 3 counter-width mutants, which need more than 2^20 cycles.
- The pass-2 audit is clean: 7 tool calls, all inside the tree
  (`audit_agent_paths.py --skip 18` skips pass 1's calls in the shared
  transcript).

**Which formal-only kills are visible at the pins.** These are the 35
blind mutants that only formal killed (section 20). The equivalence
prover classifies each one: it tries to prove equivalence at the pins,
or failing that, to refute it with a pin-level counterexample within 12
cycles.

| Result | Count | Mutants |
|---|---|---|
| pin-visible (refuted) | 5 | `uo_out` reset value ×3; `pending_lx_writeback` reset deleted; HOST_GO's rise detector firing one cycle late (`pin_ctrl.v:138`, `!host_go_prev` → `host_go_prev`) |
| proven pin-equivalent | 30 | below; 3 of them are `start_fall` (`pin_ctrl.v:137`), which has no consumer |

**The other 27 (2026-10-21).** At first these were undetermined: no pin
difference within 12 cycles, and no allowed-difference list to prove
them with. `prove_equiv.py --formal-only` now proves all of them
never reach the pins (`make prove-equiv`; recorded in
`formal/equiv_proofs.json`, 55 proofs in all). Formal kills them because
a prop checks an internal register directly. Each site in
`FORMAL_ONLY` has two parts:
- an allowed-difference set;
- an invariant explaining why nothing reads the difference.

| Site | Mutants | Why the pins never see it |
|---|---|---|
| `core.v:337`, `:359` | 4 | `pc`'s reset value: LOAD never reads `pc`, and START's rise zeroes it (or it is still 0 from reset) |
| `core.v:338` | 3 | `ir_lo`'s reset value: overwritten by the first FETCH_HI before EXECUTE reads it |
| `core.v:340` | 1 | `entering_execute` not reset: a stale 1 before the first EXECUTE only reloads the cycle counter |
| `core.v:178` | 1 | `exec_commit` true outside EXECUTE: the SET/OUTB held in `ir` is re-applied in FETCH, a no-op |
| `cycle_counter.v:88`, `:90` | 6 | `count`'s reset value, or its load value when `shifted == 0`: the instruction exits on the load cycle, and every later reader reloads first |
| `pin_ctrl.v:99-102` | 8 | synchronizer reset values: flushed within 2 edges, while the FSM is still in LOAD with no HOST_GO edge |
| `pin_ctrl.v:128`, `:129` | 4 | edge-detector history reset values: a stale 1 can only fake a fall, and LOAD consumes neither fall |

Two extensions to the miter make this possible:
- a ghost counter `rcnt` of edges since reset was released;
- `RESET_FACTS`, design facts about the gold copy right after reset (the
  flops hold their reset values; the synchronizer reads 0; the FSM is
  in LOAD; `entering_execute` only in EXECUTE; `ir` is 0 in LOAD).

Like every other invariant, these are asserted, never assumed. The base
case checks them from reset, and induction keeps them.

**Negative control.** `--formal-only --controls` runs 8 pin-visible
mutants through the same kind of entries, mostly the entry of their
own site. Examples:
- `uio_in_ff1` reset to 8'hFF rather than 8'd1;
- `count` loaded with `shifted + 1`;
- `uo_out`'s reset value;
- the late HOST_GO rise.

Each must be rejected by a real counterexample, never by a tool error,
and all 8 are.

So every formal-only kill is now explained: 30 never reach the pins,
4 are reset mutants that the directed reset tests now kill, and
`pin_ctrl.v:138` is pin-visible but spec-legal (below).

The spec suite kills the 3 `uo_out` mutants, which our own simulation
missed.

**Closed in simulation (2026-10-20).** Two new directed tests make
simulation kill all four reset mutants on its own; mutation-gate
entries #24 (uo_out reset value) and #25 (pending writeback not cleared
by reset) guard them:
- `test_reset_pin_state` checks the pins' reset values during and
  after a power-up reset, and again after a reset that lands mid-run,
  while `uo_out` shows a value and a pin is driven.
- `test_reset_during_pending_writeback` sweeps a reset across every
  cycle of a loop that keeps a LOAD's writeback in flight. It then
  boots a program that OUTs R0-R3; every register must read 0.

The first version of that test checked only R1 and missed the mutant.
The stale writeback lands in whatever register the reset left in its
destination latch, which is R0.

**`pin_ctrl.v:138` is pin-visible but spec-legal.** A probe on the RTL
and on the mutant shows exactly what changes:
- the HOST_STATUS ack comes after 5 edges instead of 4;
- a 1-cycle HOST_GO pulse is never acked.

The boot handshake is level-based: the host holds HOST_GO until it
sees the ack, and the spec never states the ack's cycle latency. A
directed test pinning 4 edges would over-specify the design, so this
mutant stays a formal-only kill: `agent_pin_ctrl_sync_depth_props.v`
is deliberately stricter than the spec.

## 22. RTL (structural) coverage

The functional bins (section 3) measure what the stimulus meant to
exercise. `scripts/rtl_coverage.py` measures what the RTL actually did,
under Verilator 5 (`--coverage-line --coverage-toggle`):
- every line/branch block executed;
- every bit of every signal toggled both ways.

A hole is a line no test reached, or a bit no test moved: a place where
a bug could hide from every simulation gate.

`make rtl-coverage` (sign-off gate `rtl_coverage`, about 30 s):
1. builds the testbench under Verilator with coverage on;
2. runs the directed suite, the spec suite, random (100 seeds) and each
   stimulus profile (50 seeds);
3. merges the per-suite `.dat` files across hierarchy and scores only
   `src/`;
4. fails on any hole not in `WAIVED`, and on any waiver that no longer
   matches a hole (stale).

The board suite is not included. `tb_board.v` models floating nets and
pull-ups as X/Z, which 2-state Verilator cannot.

**First run (2026-10-21).** Line 132/134, toggle 1379/1406, 29 holes.

| Holes | Verdict |
|---|---|
| `pc[8]`, `retaddr[8]`, `next_pc[8]` (6) | **real gap**: no test ran code above word 255 |
| `start_fall` in core, pin_ctrl, top (6) | unreachable, proven (below) |
| `uio_out[4]`/`uio_oe[4]` (8), `uio_oe[5]` 1→0 (2) | constant by construction (HOST_GO fixed input, HOST_STATUS fixed output) |
| `state` case defaults, core.v:304/427 (2) | unreachable: all four 2-bit encodings have their own arm |
| `_unused` lint sinks (4), `ena` 1→0 (1) | drive nothing; the harness holds `ena` high |

**The real gap.** The PC, the return register and next-PC are 9 bits.
Words 256-511 fetch bytes 512-1023 (the data region) and execute them.
docs/isa.md says no hardware guard stops this. No test put code there,
so a next-PC computed in 8 bits passed every simulation gate:
directed 43/43, spec 62/62, random 100 seeds. Formal did kill it, via 3
of the 10 props files.

`test_pc_upper_half` boots a padded image that:
- falls through from word 255 to 256;
- CALLs and RETs above 255;
- JMPs back to the low half and CALLs from there, so bit 8 clears in
  both `pc` and `retaddr`.

It kills all three truncations tried (fall-through, return address,
branch target). Mutation-gate entry #26 guards it. docs/isa.md now
states the 9-bit behaviour.

**The `start_fall` proof.** `rtl_coverage.py prove` injects
`assert (!start_fall)` into a scratch copy of `src/` and runs BMC (depth
30) plus k-induction. The SRAM is a blackbox whose read data is a free
input, an over-approximation, so unreachability carries over to the
chip. Induction needs two invariants, both asserted, never assumed:
- `!(mode_load && start_prev)`;
- `!(halted && state == S_LOAD)` (the FSM freezes while halted).

Negative control: with the `mode_load` gate removed, BMC finds a
counterexample. `gate` re-runs the proof every time, so the waiver
cannot outlive the RTL it describes.

**After.** Line 132/134 and toggle 1385/1406. All 23 remaining holes are
waived, each with its reason in `WAIVED`. Removing the directed suite's
data makes the gate fail with 22 unwaived holes (pc[8], DELAY's top
counter bits, and others): the random and spec suites alone do not
reach them.


## 23. ISA extension: XOR and ADD (2026-10-23)

The one RTL change after the CMOS5L port, approved by the author: `XOR
Rd,Rs` (opcode 19, flag untouched) and `ADD Rd,Rs` (opcode 20, flag =
carry out), in the reg-reg format; reserved opcodes are now 21-31.
Reason: flexibility, the first judging criterion. Most protocols carry a
checksum or CRC, and without XOR or ADD every one was a workaround
(parity counted with `LOOP`, complements via duplicated code). isa.md
had deferred exactly this choice ("add a one-instruction `XOR Rd,Rs` ...
when CAN/Ethernet get tackled").

The change was built and verified in a scratch copy of the repo before
`src/` was touched, layer by layer:

| Layer | Change | Result |
|---|---|---|
| RTL | `isa_defs.v` defines; `core.v`: recognized up to 20, two regfile-write cases, ADD's carry into the flag | 2 files |
| Toolchain, model | `isa_asm.py` encoders, `asm.py` mnemonics, golden model | pytest 68/68 |
| Directed | `test_xor` (result, flag preserved, XOR-self), `test_add_carry` (carry 1 and 0, branch on it); the illegal-opcode tests moved from 19 to 21 | 62/62 |
| Random | both ops in the default leaf pool; coverage: opcode bins, `ADD.carry`/`ADD.nocarry` flag-writer bins | default 111/111 real bins; 4 profiles pass |
| Formal | datapath props: XOR/ADD write values, 5 new covers; illegal-opcode props: boundary 21; loop/flag props: ADD is a flag writer (flag = carry), XOR holds it | 11/11 proven, 282/282 covers + 4 deep |
| Formal check of the checks | carry inverted, XOR writing the flag, XOR as OR, ADD +1: each fails a proof | 4/4 |
| Mutation gate | #8 boundary moved to 21; new #27 XOR as OR, #28 ADD without carry, #29 XOR clears the flag | 4/4 killed |
| Spec suite | reserved list follows the doc (21-31), marked as a parent edit; spec tests for XOR/ADD left to an independent pass | 62/62 |
| RTL coverage | waivers remapped to the shifted line numbers | line 135/137, toggle 1403/1424, 0 unwaived, 0 stale |

Two harness issues the change exposed, both fixed:
- `nested_flow` keeps its own list of register-writing opcodes, to keep
  loop bodies off their counters. XOR/ADD weren't in it, so a program
  overwrote a live counter and never halted (caught by the random run:
  no mismatch, 6665 instructions, then MAX_CYCLES).
- `protocol_pins` was gated on `wait_k_bins.480+`, which the default
  generator could reach only with its single longest WAIT timeout: 1 hit
  in 50 seeds before, 0 after the opcode pool shifted the random stream.
  The profile now allows exponent-1 WAIT timeouts up to 992 cycles: 7
  hits.

Line-keyed tables (RTL-coverage waivers, prove_equiv's allowed-difference
and formal-only entries, blind_mutate's equivalence list, and the
control mutant IDs, which embed line and byte offset) were remapped by
aligning old and new core.v, not by hand. Still to re-run on the new
RTL: the full mutation gate (29 entries), the blind census and the
equivalence proofs (section 20), and CMOS5L hardening.
