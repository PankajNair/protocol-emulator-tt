# Architecture

Status: system-level decisions below. Full ISA spec split out to
[isa.md](isa.md) -- it's substantial enough to stand on its own; this
file stays focused on memory/area/pipeline/pin-level system integration.

## Decided

- **Core**: minimal sequencer ISA, not a general CPU. Full spec:
  [isa.md](isa.md) (16-bit word, 4x 8-bit registers, opcode table,
  undefined-opcode policy). Supersedes the vaguer "tiny CPU" framing
  `src/cpu/isa_defs.v`'s placeholder opcodes were written against --
  that file needs updating to match isa.md.
- **Memory**: SRAM-backed, not synthesized ROM (required, not just
  preferred -- the brief explicitly asks for reprogrammability after
  fabrication, which a synthesized ROM can't do). Macro:
  `RM_IHPSG13_1P_1024x8_c2_bm_bist` (the same macro
  `tt_um_urish_sram_test` wraps on the IHP shuttle) -- 1024x8 (1KB),
  single-port, byte-addressable, 10-bit address. **Split 512/512**:
  bytes 0-511 are program (256 words at the locked 16-bit instruction
  width -- [isa.md](isa.md) -- still 1.7-5x a realistic baseline
  program's size), bytes 512-1023 are data, reached via `LOAD`/`STORE`/
  `LOADX` ([isa.md](isa.md) "Data memory"). Not using the full capacity
  as program space, as an earlier version of this doc said -- an
  adversarial design review found the original all-program plan left
  no spill/accumulator space at all, which made the ISA's own "longer
  programs, not more opcodes" philosophy false for anything needing
  more live state than 4 registers (a CRC accumulator being the
  sharpest example). Being SRAM (volatile, unlike a synthesized ROM)
  means firmware has to be loaded in by the host every power-cycle --
  see the Pipeline section's `LOAD` state, which streams the full 1024
  bytes and needs no changes for this split (the second half is just
  treated as data once execution starts, and can double as a way to
  preload a CRC table or config constants through the same boot
  sequence).
- **Area budget**: `tiles: "6x4"` confirmed valid against
  `TinyTapeout/tt-support-tools` `tech/ihp-sg13g2/tile_sizes.yaml` (this
  template's own comment is stale, tops out at 8x2). Placed die area
  ~0.92mm^2 (1289.28 x 710.64 um), core ~0.902mm^2. The brief's
  ~1000 logic-cells/tile budget was unverified -- **now validated** via
  two flow-validation runs (git history: commits `03b7bf6`/`2e113b4`):
  a trivial adder confirmed the flow works end-to-end on CMOS5L (53
  cells, 565.6 um^2 post-place), and the SRAM macro instantiated
  standalone confirmed its real placed cost -- 50,172 um^2 total
  (macro + wrapper logic), just **5.6% of the core area**, matching its
  LEF-declared footprint exactly with no placement/routing inflation.
  **Area is not the binding constraint on ISA/instruction-count size.**

- **Pin map**: see the Pin map section below. Locked.
- **Pipeline**: see the Pipeline section below. Locked.
- **Timing**: `clock_hz = 50,000,000` locked, full per-protocol budget in
  [protocol_timing.md](protocol_timing.md). Confirmed sufficient for
  every baseline and stretch protocol except 10M Ethernet (deferred,
  see Open questions) -- including protocols not previously checked
  (JTAG/SWD are self-paced so trivially fine, CAN's blocker was never
  clock, PS/2 has huge margin). Surfaced that `DELAY`'s flat 511-cycle
  max couldn't reach most UART bauds -- fixed via an exponent/mantissa
  immediate encoding, see [isa.md](isa.md)'s `DELAY` row.

## Open questions

- **10M Ethernet: deferred, last resort.** Direct bit-banging cannot
  reach it (see [protocol_timing.md](protocol_timing.md)) -- default
  assumption is it's dropped from the stretch-goal list; only
  reconsidered (hardware serializer peripheral) if time permits after
  everything else, including the other stretch protocols, is done.
  Baseline capability does not depend on this.
- **CRC/LFSR peripheral**: for CAN/Ethernet stretch protocols -- see
  [isa.md](isa.md) Open questions. Deferred until those protocols are
  actually tackled.

## Pipeline

Locked. 4-state FSM: `LOAD -> FETCH_LO -> FETCH_HI -> EXECUTE -> (back to FETCH_LO)`.

### LOAD (boot)

Resolves a real gap a pre-RTL completeness pass caught: the SRAM
instruction memory is volatile -- it powers up empty and needs firmware
written in every power-cycle, and (deliberately) no opcode writes
program memory, to keep the ISA minimal. So loading happens in a
dedicated hardware state, before fetch/execute ever starts, driven
directly by the host rather than by firmware -- the SRAM write path
never touches the CPU datapath.

- **Reset -> `LOAD`**, not `FETCH_LO`.
- `ui_in[7:0]` = data byte. `HOST_GO` (`uio[4]`) pulses as a
  write-strobe -- each pulse writes `ui_in` to the SRAM at an internal
  byte-address counter, then increments it (0-1023, the macro's full
  byte range -- independent of the word-level `addr(9)` field
  `BRANCH`/`LOOP`/`CALL` use for jumps). No address bus needed: host
  streams the raw firmware byte image in order, low byte of instruction
  0 first, matching `FETCH_LO`/`FETCH_HI`'s own `PC*2`/`PC*2+1` order.
- `uio[6]` (previously reserved) = `START`. Host asserts once loading
  is done; FSM exits `LOAD`, jumps to `FETCH_LO` with `PC=0`. `uio[7]`
  stays reserved.
- Same physical pins as normal operation (`ui_in`, `HOST_GO`) -- this
  is a mode-dependent reinterpretation, not new pin budget.

### Reset values

PC=0, R0-R3=0, compare/test flag=0, return-address register=0, FSM
resets into `LOAD` (see above). A reset asserting mid-fetch (partway
through `FETCH_HI`, partial byte latched) unconditionally returns to
`LOAD` -- the in-flight fetch is simply discarded, no special-case
handling needed.

### FETCH_LO / FETCH_HI / EXECUTE

Driven by a real constraint that wasn't visible when the 16-bit
instruction word was picked ([isa.md](isa.md)): the instruction-memory
macro (`RM_IHPSG13_1P_1024x8_c2_bm_bist`) is **8 bits wide**, so every
16-bit instruction needs two sequential byte reads, not one. Not a
problem -- the design is `DELAY`-bound anyway, a couple of fetch cycles
are noise -- but real, and worth stating rather than assuming away.

- `FETCH_LO`: issue SRAM read for the low byte, address = `PC*2`.
- `FETCH_HI`: capture the low byte; issue SRAM read for the high byte,
  address = `PC*2+1`. Addresses can be issued back-to-back because the
  macro's read is synchronous with 1-cycle latency (address in on cycle
  N, data out on N+1).
- `EXECUTE`: capture the high byte (full 16-bit word now latched),
  decode combinationally (pure bit-slicing -- no reason to spend a
  cycle on it, so decode is merged into this state rather than given
  its own), and perform the operation.

**Uniform 3 cycles/instruction** for everything -- `NOP`, `BRANCH`,
`LOOP`, `CMP`, `SET`, register ops, all of it. Two exceptions, both by
design, and both share the same countdown-counter hardware since they
never execute simultaneously on a single-issue sequencer: `DELAY` stays
in `EXECUTE` decrementing it until it hits zero (3+N cycles, N from the
immediate), and `WAIT` stays in `EXECUTE` until its pin condition is
true *or* its own optional timeout counter expires (3+N if a timeout is
set, 3+unbounded if not -- `WAIT`'s default is still unbounded, timeout
is opt-in per [isa.md](isa.md)'s `WAIT` row, added after an adversarial
review pointed out an unbounded-only `WAIT` gives firmware no way to
detect or recover from a hung bus, e.g. an I2C slave holding SCL low
forever).

This also locks something [isa.md](isa.md) left implicit: the branch/
loop/call `addr(9)` field is a **word index**, not a byte address --
the fetch unit does the `x2` translation internally to reach the
physical SRAM byte address. Every fetch is therefore word-aligned by
construction; a jump target can never land mid-instruction. (The field
is 9 bits wide, 0-511, but only 0-255 are legitimate now that the SRAM
is split 512/512 between program and data -- see the Memory bullet
above and [isa.md](isa.md)'s "Data memory".)

Verification payoff, same pattern as the ISA audit: one clean, uniform
property -- "every instruction retires in exactly 3 cycles, except
`DELAY` (3+N, N known) and `WAIT` (3+N if timed, 3+unbounded if not,
both by design)" -- instead
of a different cycle-count rule per opcode.

## Pin map

Locked. Hard constraint driving the whole layout: TT's `ui_in` is
hardware input-only and `uo_out` is hardware output-only (neither can
be tri-stated or read back) -- only `uio[7:0]` supports bidirectional/
open-drain. Since I2C's SDA/SCL need open-drain, they're forced onto
`uio`; once that's true, putting *all* protocol signal pins on `uio` is
the natural move, since `uio` is a strict superset of what `ui_in`/
`uo_out` can do.

| pins | role |
|---|---|
| `ui_in[7:0]` | Host **DATA-IN** bus -- host writes a byte, firmware's `IN` reads it. Matches `IN`'s locked "whole byte, one shot" semantics ([isa.md](isa.md)) directly -- no muxing logic needed. |
| `uo_out[7:0]` | Host **DATA-OUT** bus -- firmware's `OUT` drives a byte, host reads it. Matches `OUT` directly, no muxing logic. |
| `uio[3:0]` | **Protocol pin bus** -- `pin_index` 0-3 in every `SET`/`WAIT`/`OUTB`/`INB`. Role is a pure firmware convention, same 4 physical pins for every protocol: UART uses 0=TX,1=RX; SPI uses 0=MOSI,1=MISO,2=SCLK,3=CS; I2C uses 0=SDA,1=SCL (open-drain via `SET`'s existing value-bit convention -- value=1 releases, value=0 drives low). Checked against every protocol on the list, not just baseline: UART needs 2, SPI 4, I2C 2, JTAG 4 (TCK/TMS/TDI/TDO), SWD 2, PS/2 2, CAN 2 -- all fit in 4. Zero protocol-specific wiring, ever; only the firmware's choice of `pin_index` values changes. |
| `uio[4]` | `HOST_GO`, `pin_index=4`, fixed input. During normal execution: host strobes it, firmware `WAIT`s on it. During `LOAD` (see Pipeline): doubles as the SRAM write-strobe -- same physical pin, mode-dependent meaning. |
| `uio[5]` | `HOST_STATUS`, `pin_index=5`, fixed output. Firmware `SET`s it when done. |
| `uio[6]` | `START`, fixed input, `LOAD`-mode only (see Pipeline) -- host asserts once firmware is loaded, to begin execution. Not `pin_index`-addressable; not meaningful once running. |
| `uio[7]` | reserved, unconnected. |

`pin_index` values 6-7 (in `SET`/`WAIT`/`OUTB`/`INB`) don't correspond
to `uio[6]`/`uio[7]` directly -- `uio[6]` is dedicated to `START`, not
exposed on the generic `pin_index` bus, and `uio[7]` is unconnected.
Both `pin_index` values read as 0, writes are no-ops -- same "safe by
construction" treatment as the illegal-opcode case in
[isa.md](isa.md), not undefined behavior.

Forward-compat note: `pin_index` is already 3 bits (0-7) in the locked
encoding, so if a future protocol genuinely needs more than 4 protocol
pins, widening to `uio[7:0]` costs zero ISA changes -- pure headroom,
not a redesign.

Verification payoff: `pin_index -> physical pin` is one flat table,
identical for every firmware image. Properties like "`pin_index` 6-7
never toggle a real pin" or "`HOST_STATUS` is only ever driven, never
read" get written once and hold across all seven protocols, rather than
needing a new pin-mapping property per protocol.

Still needed: an assembler translating protocol programs (using these
symbolic pin roles) to ISA bytecode, and `pin_ctrl.v`'s actual
direction-control logic for `uio[3:0]` (the only pins that need any --
`ui_in`/`uo_out` are hardwired, `uio[4:5]`/`uio[6]` are fixed-direction).

Host-facing inputs (`ui_in`, `uio_in` bits used for `HOST_GO`/`START`)
are driven by an external host asynchronous to `clk` -- `pin_ctrl.v`
needs to synchronize them (standard 2-flop synchronizer) before any
`WAIT`/`IN`/LOAD-write logic consumes them. Not an ISA-level decision,
just needs to land in `pin_ctrl.v` when it's written.
