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
| Directed | `test/test.py` (28 tests) | Does this specific sequence/edge case work? One test per opcode plus targeted hazards (CALL/RET misuse, flag immunity, WAIT flag polarity, boot echo/saturation, uio[6] driver gate, the 5-phase host IN/OUT handshake, HALT holding every pin). Host-facing waits are bounded and require `uio_oe` to be driving. | `make -C test` |
| UART end-to-end | `test/test_uart.py` | Does the shipped UART firmware (`firmware/protocols/uart.asm` TX, `uart_rx.asm` RX, assembled by `firmware/isa/asm.py`) move bytes correctly on the real RTL? TX: Independent receiver model on the TX pin (start-edge detect, centre sampling, stop-bit check, re-arm after the stop sample), 115200 / 57600 / 9600 baud, per-edge timing deviation measured against docs/protocol_timing.md. RX: transmitter model on the RX pin, back-to-back frames, sender baud error +-2%, framing error + resync. | part of `make -C test` |
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
| `illegal_mix` | program: reserved opcodes 19-31 with random operands | ILLEGAL opcode and `illegal_op` flag bins; all 13 reserved opcodes executed |
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

Each suite run has a 300s timeout (a hang counts as killed) and the script refuses to run on a dirty `src/`. Current score: 17/17. Mutants 1-14 were also 14/14 under each stimulus profile
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

## 6. Formal / vacuity gate

Plain `yosys` + `yosys-smtbmc` + `z3`, no SymbiYosys. Immediate
assertions in `always @(posedge clk)` blocks only (this yosys build
can't parse concurrent SVA); every assert has a companion cover. `make
formal [TARGET_PROPS=...]` runs BMC at depth 20; `make vacuity` requires
100% of covers reached. Toolchain details: `formal/scripts/formal_common.py`
docstring.

| Props file | Target | Status |
|---|---|---|
| `agent_cycle_counter_timing_props.v` | DELAY/WAIT counter: no undercount + exact timing | proven, 2/2 covers |
| `agent_core_wait_props.v` | `protocol_cpu_core`: WAIT leaves EXECUTE on exactly the first k where the pin matches or the spec-computed timeout expires; tie clears the flag; mantissa 0 never times out; no side effects while waiting; exit fetches PC+1 | proven, 16/16 covers (timeout exit reached for exponent 0 only). Scratch mutants (#6, timeout from exponent, #1 in cycle_counter.v, inverted level) each fail |
| `agent_core_loop_flag_props.v` | `protocol_cpu_core`: ghost regfile (from the write port) matches both read ports every cycle; only CMP/TEST-bit/WAIT change the flag; LOOP writes Rd-1 mod 256 and branches iff nonzero (wrap covered); CMP/TEST-bit/WAIT write the specified value | proven, 22/22 covers. Scratch mutants (#11, SHIFT writes flag, LOOP taken iff flag, decrement by 2, CMP inverted) each fail |
| `agent_core_call_ret_misuse_props.v` | `protocol_cpu_core`: ghost `return_valid`/`retaddr` from identified CALL/RET commits match the core every cycle; misuse flag rises only after a nested CALL or orphan RET, always after one, sticky until reset; CALL saves PC+1 and jumps, RET (orphan included) jumps to `retaddr` | proven, 17/17 covers. Scratch mutants (#4, #5, RET not clearing return_valid, CALL saving PC) each fail |
| `agent_core_reset_props.v` | `protocol_cpu_core`: known state on the first cycle after any (incl. mid-trace) reset; nothing executes, drives pins or writes memory before START except host boot writes; first fetch byte 0; no leakage from a DELAY, blocked WAIT, FETCH_HI or pending LOAD/LOADX writeback interrupted by reset (a cover per scenario) | proven, 22/22 covers. Scratch mutants (dropped reset, surviving writeback, reset into FETCH_LO, regfile without reset) each fail |
| `agent_core_illegal_opcode_props.v` | `protocol_cpu_core`: illegal_op_flag rises only after a reserved-opcode commit, always rises after one, sticky until reset; reserved opcode is a true NOP (no mem/pin/regfile write, PC+1, other state unchanged). Opcode taken from `mem_rdata` at FETCH_HI->EXECUTE, not core.v's decode | proven, 9/9 covers. Agent scratch mutants (#8, flag never set, LDI also sets) each fail |
| `agent_pin_ctrl_direction_props.v` | `pin_ctrl`: no uio[6] drive in LOAD or before a real START fall (ghost model from ports), `cover(uio_oe[6])` + drives-once-allowed, fixed-role pins 4/5, protocol-pin oe only per sticky mode and never in LOAD, sticky mode matches a ghost model; output VALUES: `uio_out[5]` always the last value written to pin 5 (LOAD ack and runtime), `uio_out[6]` the latched HOST_ERROR once its gate opens, protocol pins drive their last written value (0 in open-drain) | proven, 47/47 covers. Validated by re-injecting the HOST_ERROR bug (142ab71): assert FAILS and the `uio_oe[6]` cover goes UNREACHED, so formal alone would have caught it |

## 7. Golden model

`test/golden_model.py`: from-scratch Python ISA model, one `step()` per
instruction, returning the new state and the exact cycle count. Covers
all 19 opcodes and reserved 19-31. WAIT/IN/INB take an `io_read(t)`
callback so the model consumes the same stimulus timeline as the RTL.
Trusted only after `test_golden_model_selfcheck.py` cross-checks it
against RTL-proven directed tests.

## 8. Agents and contracts

Three generic, project-agnostic subagents live at user level
(`~/.claude/agents/`). Each reads a contract in its write-scope directory
before doing anything:

| Agent | Contract | Writes | Accepted by |
|---|---|---|---|
| `stimulus-gen` | `test/stim/AGENT_CONTRACT.md` | new stimulus profiles | 200 seeds pass, target bins hit, mutation no drop |
| `coverage-closure` | `test/seq/AGENT_CONTRACT.md` | targeted bursts for thin bins | `make coverage` + `make mutate` no drop |
| `assertion-formal` | `formal/AGENT_CONTRACT.md` | props files | `make vacuity` 100% then `make formal` PASS, plus a scratch-copy bug injection |

Agents never grade their own output: the parent session runs every gate.
`.claude/settings.json` denies `Edit(src/**)`, and the parent runs `git diff
src/` after every invocation. The deny rule is defense in depth, not proof.

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

Not yet covered: SPI, I2C (firmware not written).

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

Planned next on the board: I2C (open-drain, clock stretching).
