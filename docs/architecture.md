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
- **Host handshake**: see the Host handshake section below. Locked.
- **Scope: master/host-role emulation only, slave/device emulation
  deferred.** Decided explicitly after a deep audit pointed out the
  brief itself doesn't say master-only and this repo never stated it
  either -- see [isa.md](isa.md)'s Known limitations section for the
  reasoning (externally-driven clock breaks the `DELAY`-self-pacing
  assumption everything else here relies on). Same "last resort, not
  dropped" treatment as 10M Ethernet below.

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
directly by the host rather than by firmware.

(Stale as of `LOAD`/`STORE`/`LOADX` landing: this used to say "the SRAM
write path never touches the CPU datapath," true when the boot stream
was the *only* thing that ever wrote SRAM. `STORE` also writes SRAM now,
with `Rd`'s value as write-data -- that's the CPU datapath. The SRAM
address mux has 4 sources in the finished design: boot byte-counter
(`LOAD` state), `PC*2`/`PC*2+1` (`FETCH_LO`/`FETCH_HI`), and
`512+imm`/`512+Rs` (`EXECUTE`, for `LOAD`/`STORE`/`LOADX`); the
write-data mux has 2: `ui_in` during boot, `Rd` during `STORE`. Worth
having this concrete before `core.v` gets written, not discovered while
writing it.)

- **Reset -> `LOAD`**, not `FETCH_LO`.
- `ui_in[7:0]` = data byte. `HOST_GO` (`uio[4]`) pulses as a
  write-strobe -- each pulse writes `ui_in` to the SRAM at an internal
  byte-address counter, then increments it (0-1023, the macro's full
  byte range -- independent of the word-level `addr(9)` field
  `BRANCH`/`LOOP`/`CALL` use for jumps). No address bus needed: host
  streams the raw firmware byte image in order, low byte of instruction
  0 first, matching `FETCH_LO`/`FETCH_HI`'s own `PC*2`/`PC*2+1` order.
- `uio[6]` (previously reserved) = `START` during `LOAD` only, then
  repurposed as `HOST_ERROR` during normal execution -- see Pin map and
  "Host handshake" below. **`START` is a pulse** (same convention as the
  `HOST_GO` write-strobe above), not a held level -- this matters: FSM
  exits `LOAD` on `START`'s synchronized *rising* edge (`PC=0`,
  execution begins immediately), but `uio_oe[6]` stays 0 (input) until
  `START`'s synchronized *falling* edge is also observed, only then
  enabling the `HOST_ERROR` output driver. This closes a real driver-
  contention hazard an adversarial review found: if the chip started
  driving `HOST_ERROR` the instant it saw `START` go high, it would be
  actively driving the same wire the host is still physically holding
  high (the host has no way to know exactly when the chip sampled it,
  so it can't release in perfect sync) -- two push-pull drivers fighting
  on one pin. Gating on the confirmed-low edge means the chip only ever
  takes over a wire the host has provably stopped driving, by
  construction, not by timing luck. If firmware `SET`s `HOST_ERROR`
  before that gate opens (unlikely -- would need an error path in the
  first few cycles of execution), the value still latches internally
  and reaches the pin once the gate opens; no CPU-path special-casing,
  the gate lives entirely in `pin_ctrl.v`'s output stage. Two host-side
  requirements this adds: `HOST_GO` must be low before pulsing `START`
  (otherwise the first runtime `WAIT HOST_GO,1` a program executes could
  fire on a stale high with no real handshake behind it), and the host
  must release `START` promptly after pulsing it -- same class of pulse-
  timing contract as `HOST_GO`'s boot-mode write-strobe, exact cycle
  count TBD until `pin_ctrl.v` exists to measure against. `uio[7]` stays
  a protocol pin (Pin map).
- Same physical pins as normal operation (`ui_in`, `HOST_GO`) -- this
  is a mode-dependent reinterpretation, not new pin budget.
- **`uo_out[7:0]` echoes the byte-address counter's low 8 bits
  (post-increment)**, every `HOST_GO` pulse. `uo_out` is completely idle
  during `LOAD` (firmware isn't running yet), so this costs nothing new.
  Echoes the *address*, not the data byte -- an earlier draft echoed the
  data byte instead, which an adversarial review caught as not actually
  catching what it claimed: on a stable synchronous bus a write lands
  correctly whenever it happens, so data-echo is blind to the real
  failure mode (a dropped or doubled `HOST_GO` pulse), since it echoes
  the same value whether the pulse fired once, twice, or got silently
  absorbed by the next real one. Address-echo fixes this: host tracks
  its own byte count `k`, expects `echo == k mod 256` after each pulse.
  A single dropped or doubled pulse always shifts the counter by
  exactly +-1 relative to what the host expects, and consecutive
  integers are never congruent mod 256 -- so the 8-bit truncation never
  hides a single-pulse error, it only loses information at exactly
  256-pulse-aligned discrepancies, which isn't the failure mode being
  guarded against. Same cost as the data-echo it replaces, strictly
  better detection. (The "chosen over an end-of-load checksum" reasoning
  still holds regardless of which value gets echoed: per-byte feedback
  catches errors as they occur, a checksum only gives an aggregate
  pass/fail at the end.)
- **Byte-address counter saturates at 1023, does not wrap.** A
  `HOST_GO` pulse at address 1023 writes/echoes that byte and stops
  advancing -- further pulses are harmless no-ops, not silent
  corruption of address 0 (which a wrapping counter would cause). No
  separate overflow flag needed on top of this -- the host already
  tracks its own byte count.
- **`uio[3:0]` (protocol bus) and `uio[6:7]` are Hi-Z (`oe=0`)
  throughout `LOAD`.** Nothing should drive external protocol lines
  before firmware is actually running -- resolves what was previously
  unspecified pin state during boot.
- **Write-commit is `HOST_STATUS`-acked, not a fixed-cycle-count
  contract.** An earlier draft said "host holds `HOST_GO` >=2 cycles
  per phase" and left it there -- an adversarial review caught that
  this doesn't actually tell the host *when the echo is valid*: the
  synchronizer, the write itself, and a registered `uo_out` all add
  latency between the physical edge and a stable echo, and none of that
  depth is knowable before `pin_ctrl.v` exists to measure it. Fixed by
  reusing the exact mechanism already locked for the runtime handshake
  (Host handshake, below) instead of inventing a cycle-counting
  contract: `HOST_STATUS` also acks each write during `LOAD`, in
  hardware (no opcodes execute yet, so this is pure FSM/pin_ctrl logic,
  not firmware) --
  ```
  host: drive ui_in, assert HOST_GO
  chip: (internally) sync HOST_GO, write byte, increment counter,
        update the uo_out echo, THEN assert HOST_STATUS
  host: wait for HOST_STATUS=1 (no cycle-counting -- just wait for the
        level) -> read uo_out, now guaranteed valid -> lower HOST_GO
  chip: sees synchronized HOST_GO=0 -> clears HOST_STATUS
  host: sees HOST_STATUS=0 -> ready for next byte
  ```
  Same logical shape as the runtime `IN` handshake (wait/act/ack/wait/
  clear), just realized as hardware during `LOAD` instead of opcodes.
  Also answers a second previously-unspecified question this same
  finding surfaced: what `HOST_STATUS` does during `LOAD` -- nothing
  before now, this ack role from here on. Host never needs to know
  synchronizer depth or count cycles anywhere in this design; it just
  waits for level transitions, consistently.

### Reset values

PC=0, R0-R3=0, compare/test flag=0, return-address register=0,
`return-valid`=0, illegal-opcode flag=0, `CALL`/`RET` misuse flag=0
(the latter two are sticky debug flags -- see [isa.md](isa.md)'s
Undefined opcode behavior and Branch format sections -- cleared only on
reset, same as everything else here). **All 5 protocol pins' drive-mode
resets to input** ([isa.md](isa.md) `SET` row) -- `uio_oe` is 0 for
`uio[3:0]`/`uio[7]` until firmware explicitly configures a pin, so
nothing drives the bus before firmware decides to. `uio[6]`'s "seen
`START` fall" latch also resets to 0, so `HOST_ERROR`'s driver starts
gated closed exactly as if the chip had just booted through `LOAD` for
the first time -- same rule applies after any reset, not just power-up.
FSM resets into `LOAD` (see above). A reset asserting mid-fetch
(partway through `FETCH_HI`, partial byte latched) unconditionally
returns to `LOAD` -- the in-flight fetch is simply discarded, no
special-case handling
needed.

**I/O register reset state, filled in after an audit found it only
existed piecemeal** (inferable by cross-referencing the Pin map, `LOAD`,
and `pin_ctrl.v` notes, never stated together):
- `uio_out` resets to all-0, uniformly, across every bit -- register
  hygiene (avoids X-propagation in gate-level sim) regardless of
  whether a given bit is electrically driven at the time.
- `uio_oe`: 0 for the protocol pins (already covered above) and for
  `pin_index=4`/`HOST_GO` (permanently hardwired input in every mode,
  not a resettable register -- there's no scenario where the chip ever
  drives this pin); permanently 1 for `pin_index=5`/`HOST_STATUS`
  (hardwired output in every mode, including `LOAD`, now that it acts
  as the write-commit ack there too -- Pipeline's `LOAD` section); 0
  for `uio[6]` until its "seen `START` fall" gate opens (already
  covered above).
- `uo_out` resets to 0, **and is explicitly re-cleared at the
  `LOAD`->`FETCH_LO` transition** -- without that second clear, the
  last boot-time address echo (Pipeline's `LOAD` section) would still
  be sitting on `uo_out` when execution starts, and could be
  misread as real `OUT` data before firmware ever executes its first
  one.

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
`LOOP`, `CMP`, `SET`, register ops, all of it. This means **FSM
occupancy** (how long an instruction holds the fetch/execute loop
before the next one starts fetching), not "every architectural effect
of the instruction is 100% complete by the end of its own `EXECUTE`" --
`LOAD`/`LOADX` are the one case where that distinction matters, see
below. Two timing exceptions, both by design, and both share the same
countdown-counter hardware since they never execute simultaneously on a
single-issue sequencer: `DELAY` stays in `EXECUTE` decrementing it
until it hits zero (3+N cycles, N from the immediate), and `WAIT` stays
in `EXECUTE` until its pin condition is true *or* its own optional
timeout counter expires (3+N if a timeout is set, 3+unbounded if not --
`WAIT`'s default is still unbounded, timeout is opt-in per
[isa.md](isa.md)'s `WAIT` row, added after an adversarial review
pointed out an unbounded-only `WAIT` gives firmware no way to detect or
recover from a hung bus, e.g. an I2C slave holding SCL low forever).

**`LOAD`/`LOADX` writeback trails by exactly 1 cycle, into the
*following* instruction's `FETCH_LO` -- by design, not an FSM stall.**
Found while re-checking the design as a whole after several rounds of
individually-reasonable fixes: the data-region address (`512+imm` or
`512+Rs`) is only known once `EXECUTE` starts (decode happens there,
merged in as already described), and the SRAM macro's read has the same
1-cycle latency as every other access on it -- so `LOAD`/`LOADX` issue
their address during `EXECUTE`, and the result is only valid the
*following* cycle, which is the next instruction's `FETCH_LO`. That's
not a conflict with `FETCH_LO`'s own address issue that cycle (`PC*2`
for the *next* fetch) -- a synchronous single-port SRAM already handles
"read last cycle's result while accepting a new address" every cycle,
which is exactly how `FETCH_LO`->`FETCH_HI` already works one state
earlier. So `Rd` gets written at the end of that `FETCH_LO`. Checked for
a hazard: if the *immediately following* instruction reads the same
register `LOAD` just wrote, is the value stale? No -- the write lands
at the end of that instruction's `FETCH_LO`, which is always before
that instruction's own `EXECUTE` (where it would actually read the
register). Hazard-free by construction; needed to be *stated* rather
than left for an RTL author to either correctly infer or -- more likely
-- get wrong by adding a dedicated 4th pipeline state for `LOAD`/
`LOADX`, which would silently break the 3-cycle property above.
`STORE` has no such trailing: a synchronous SRAM write completes in the
cycle it's issued (no return trip the way a read has one), so `STORE`
writes cleanly within its own `EXECUTE` -- `LOAD` and `STORE` are not
timing-symmetric, worth not assuming they are.

This also locks something [isa.md](isa.md) left implicit: the branch/
loop/call `addr(9)` field is a **word index**, not a byte address --
the fetch unit does the `x2` translation internally to reach the
physical SRAM byte address. Every fetch is therefore word-aligned by
construction; a jump target can never land mid-instruction. (The field
is 9 bits wide, 0-511, but only 0-255 are legitimate now that the SRAM
is split 512/512 between program and data -- see the Memory bullet
above and [isa.md](isa.md)'s "Data memory".)

Verification payoff, same pattern as the ISA audit: one clean, uniform
property -- "the FSM occupies exactly 3 cycles per instruction, except
`DELAY` (3+N, N known) and `WAIT` (3+N if timed, 3+unbounded if not,
both by design)" -- instead of a different cycle-count rule per opcode.
`LOAD`/`LOADX`'s trailing writeback (above) doesn't weaken this: FSM
occupancy is still exactly 3, the writeback is a stated, hazard-free
detail of *when within that timing* the register update lands, not an
exception to the count itself.

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
| `uio[3:0]` | **Protocol pin bus** -- `pin_index` 0-3 in every `SET`/`WAIT`/`OUTB`/`INB`. Role is a pure firmware convention, same physical pins for every protocol: UART uses 0=TX,1=RX; SPI uses 0=MOSI,1=MISO,2=SCLK,3=CS; I2C uses 0=SDA,1=SCL. Each pin's electrical behavior (push-pull, open-drain, or input) is a **sticky per-pin drive-mode** firmware configures via `SET`'s `Rd` field ([isa.md](isa.md) `SET` row) -- resets to input. Not a fixed hardware assignment: the same physical pin is push-pull as UART TX and open-drain as I2C SDA, depending only on which firmware image configured it. Extended to 5 pins total with `uio[7]`/`pin_index=7` below -- see that row for why. |
| `uio[4]` | `HOST_GO`, `pin_index=4`, fixed input. During normal execution: host strobes it, firmware `WAIT`s on it. During `LOAD` (see Pipeline): doubles as the SRAM write-strobe -- same physical pin, mode-dependent meaning. |
| `uio[5]` | `HOST_STATUS`, `pin_index=5`, fixed output. During normal execution: firmware `SET`s it when done, per the Host handshake protocol below. During `LOAD` (see Pipeline): acks each byte write in hardware (no opcodes execute yet) -- same role, same mechanism, just driven by `pin_ctrl.v`/the boot FSM instead of firmware. |
| `uio[6]` | Mode-dependent, same pattern as `HOST_GO`: during `LOAD` (see Pipeline) it's `START`, fixed input, **pulse-triggered** (rising edge starts execution, falling edge -- confirmed host has released the pin -- unlocks the `HOST_ERROR` driver; see Pipeline's `LOAD` section for why the pulse convention and the driver-contention hazard it fixes). During normal execution it's **`HOST_ERROR`**, fixed *output*, `pin_index=6` -- firmware `SET`s it on entering an error-handling path (a `WAIT` timeout, specifically). Was previously idle for the entire operational life of the chip after boot; now gives the host a dedicated, unambiguous "something went wrong" signal instead of needing to infer one from a stuck `STATUS`. (An earlier draft of this row said "not `pin_index`-addressable" -- wrong, caught when wiring `uio[7]` forced a re-check of the whole `pin_index` table: `SET`'s only mechanism to drive any pin *is* `pin_index`, so `HOST_ERROR` couldn't be `SET`-able without it. `pin_index=6` is meaningful during normal execution, moot during `LOAD` since no opcodes execute then.) |
| `uio[7]` | **5th protocol pin**, `pin_index=7`, same firmware-convention model as `uio[3:0]`. Was reserved/unconnected -- the review flagged that SPI and JTAG both use all 4 of `uio[3:0]` with zero spare for a debug/trigger line, second chip-select, or scope-sync pin on exactly the two protocols where you'd most want one. Full pin count checked against every protocol on the list, not just those two: UART 2, SPI 4, I2C 2, JTAG 4 (TCK/TMS/TDI/TDO), SWD 2, PS/2 2, CAN 2 -- SPI and JTAG were the only ones with zero spare at 4 pins, now have 1 spare each at 5. Wiring `uio[7]` in costs one mux input and removes a pin that was doing nothing. |

`pin_index` values are non-contiguous by role, not by accident: 0-3 and
7 are the generic protocol bus (`uio[0:3]`, `uio[7]`), 4 is `HOST_GO`,
5 is `HOST_STATUS`, 6 is `HOST_ERROR` (during normal execution -- see
its row above). Non-contiguity costs nothing in hardware (a lookup/case
handles arbitrary values as cheaply as contiguous ones) and each value
having exactly one real physical target is easier to verify than
leaving gaps that "read as 0, write as no-op" would have been.

**Every `pin_index` x opcode combination is well-defined, not just the
common cases.** An earlier draft called 4 "`WAIT`-only in practice" and
5/6 "`SET`-only," which reads as if the other operations are undefined
there -- they're not, checked against all 32 combinations. Two rules,
already implicit in how `SET`/`WAIT`/`INB`/`OUTB` and the pin roles
were each locked individually, cover every case without 32 special
ones: (1) a pin's `oe` is fixed by its *role* (hardwired for 4/5/6,
sticky-configured via `SET` for the protocol pins), never by which
opcode currently targets it, so `SET`/`OUTB` on a hardwired-input pin
is a safe no-op, not undefined; (2) TT's `uio_in` always reflects the
physical pad regardless of `uio_oe`, even on a pin the chip itself is
driving, so `WAIT`/`INB` always read *something* meaningful everywhere.

| `pin_index` | `SET` | `OUTB` | `WAIT` | `INB` |
|---|---|---|---|---|
| 0-3, 7 (protocol) | drives per configured mode | drives per configured mode | reads pad | reads pad |
| 4 (`HOST_GO`) | no-op, `oe` hardwired 0 | no-op, `oe` hardwired 0 | primary use: block on host strobe | valid: sample without blocking |
| 5 (`HOST_STATUS`) | primary use: drive status | valid: drive from a live bit | reads back own driven value -- self-loopback, uncommon but defined, not a hazard | reads back own driven value |
| 6 (`HOST_ERROR`, running only) | primary use: drive error | valid: drive from a live bit | reads back own driven value | reads back own driven value |

**All 8 `uio` bits are now allocated** (4 protocol + `GO` + `STATUS` +
`START`/`ERROR` + 1 more protocol) -- unlike the earlier "widening to
`uio[7:0]` is pure headroom" framing, there's no spare physical pin
left. If a future protocol genuinely needs a 6th protocol pin, that
means giving up `HOST_ERROR`, not just wiring in something unused --
worth knowing before it comes up, not discovering it mid-stretch-goal.

Verification payoff: `pin_index -> physical pin` is one flat table,
identical for every firmware image. Properties like "`pin_index=6` only
ever toggles a real pin during normal execution, never during `LOAD`"
get written once and hold across all seven protocols, rather than
needing a new pin-mapping property per protocol. (An earlier draft used
"`HOST_STATUS` is only ever driven, never read" as the example property
-- dropped once the `pin_index`x`opcode` matrix above showed that's not
actually a hard invariant: `WAIT`/`INB` on `pin_index=5` are
well-defined self-loopback reads, just not a common pattern. Don't
state a property that isn't true even if nothing currently exercises
the counterexample.)

Still needed: an assembler translating protocol programs (using these
symbolic pin roles) to ISA bytecode, and `pin_ctrl.v`'s actual
direction-control logic for `uio[3:0]`/`uio[7]` (the pins that need
firmware-driven oe control -- `ui_in`/`uo_out` are hardwired, `uio[4:5]`
are fixed-direction, `uio[6]` needs a direction *mux* keyed on
`LOAD`-vs-running mode -- input as `START`, output as `HOST_ERROR` --
plus one extra gate beyond the mode switch itself: `uio_oe[6]` stays 0
even after entering running mode until `START`'s falling edge confirms
the host has released the pin (Pipeline's `LOAD` section) -- a one-bit
"seen `START` fall" latch, not just a mode mux).

**Every external input needs synchronizing, not just the host-facing
ones.** An earlier draft only called this out for `ui_in`/`HOST_GO`/
`START` -- an adversarial review pointed out the protocol pins
(`uio[3:0]`/`uio[7]`) are equally driven by an external, asynchronous
source whenever they're configured as inputs: UART RX from a remote
transmitter, I2C SCL/SDA from a slave (clock-stretching is *defined* by
the slave asynchronously holding the line), SPI MISO from a peripheral
device. `WAIT`/`INB` reading a metastable value there is exactly as
real a hazard as reading one on `HOST_GO`, just less obvious because
those pins' role is firmware-configured rather than fixed. Fix: every
`uio` input goes through the same standard 2-flop synchronizer,
uniformly, not just the host-facing subset -- one synchronizer design
applied to every input pin, not a special case for some of them. This
adds a uniform +2-cycle latency to `WAIT`'s reaction time that
[protocol_timing.md](protocol_timing.md)'s budget doesn't currently
include; small against every margin already checked there (the
tightest, USB LS, already has *no* margin per that doc's own
Consequences section -- this makes that worse, not newly broken, and is
one more reason that row needs revisiting once real firmware exists,
not a new blocker). Not an ISA-level decision, just needs to land in
`pin_ctrl.v` uniformly when it's written.

## Host handshake

Locked. Resolves a real gap the adversarial review found: `WAIT` blocks
on a pin *level*, but `HOST_GO` was only ever described as "host
strobes it" -- a level-triggered strobe with no defined protocol means
naive firmware double-reads the same byte if `HOST_GO` is still high
when the loop comes back around. Fixed with a fully-interlocked
4-phase handshake, not a partial one -- the point is firmware
*physically cannot* see `HOST_GO` high twice for the same byte, since
it explicitly waits for the low phase before a new high phase can
start.

**Host -> firmware (`IN`):**
```
host: drive ui_in, assert HOST_GO
fw:   WAIT HOST_GO,1[,timeout]   ; wait for host
      IN   Rd                     ; capture byte
      SET  HOST_STATUS,1           ; ack: "got it"
      WAIT HOST_GO,0                ; wait for host to see the ack and drop GO
      SET  HOST_STATUS,0             ; clear, ready for next
host: sees HOST_STATUS,1 -> lowers HOST_GO -> sees HOST_STATUS,0 -> ready for next byte
```
Host-side timing contract, stated explicitly rather than left implied:
**the host must hold `ui_in` stable until it observes `HOST_STATUS=1`**
-- that's firmware's confirmation the byte has actually been captured
by `IN`; changing `ui_in` any earlier risks the host's *next* byte
racing firmware's read of the current one.

**Firmware -> host (`OUT`)** mirrors the same 5-phase structure, not
just "the same pattern, roles reversed" in prose -- an earlier draft
said that and left out the last phase, which an adversarial review
caught: without it, firmware's *next* `OUT` could fire while `HOST_GO`
is still sitting high from the previous ack, recreating on this side
the exact double-advance hazard the whole handshake exists to prevent.
```
fw:   OUT  Rd                    ; drive byte onto uo_out
      SET  HOST_STATUS,1          ; data ready
      WAIT HOST_GO,1                ; wait for host's ack
      SET  HOST_STATUS,0             ; clear -- host has acked, data consumed
      WAIT HOST_GO,0                   ; wait for host to release its ack
host: sees HOST_STATUS,1 -> reads uo_out -> asserts HOST_GO (ack) -> sees HOST_STATUS,0 -> lowers HOST_GO -> ready for next byte
```
`HOST_GO`/`HOST_STATUS` are reused for both directions (rather than a
dedicated pair per direction) -- which direction a given exchange is
follows from what firmware and host have already agreed to do next, the
same way it does in any real protocol; not actually ambiguous in
practice.

**Not this protocol's job: bulk, non-real-time data.** A fixed
test-vector sequence, a lookup table, config constants -- these don't
need per-byte interlock at all. Push them through the existing `LOAD`
boot stream, or `STORE` them into the data region ahead of time and
have firmware `LOAD` from memory with zero handshake overhead. This
handshake is specifically for the genuinely real-time case: the next
byte to bit-bang out, right now. Stated explicitly as a criterion for
which mechanism to reach for -- leaving that ambiguous is exactly the
kind of thing that trips up firmware later.

**`WAIT`'s timeout (see [isa.md](isa.md)) applies directly here**:
`WAIT HOST_GO,1,timeout` turns a host that never responds (crashed,
disconnected) into a detectable, branchable condition -- and combined
with `HOST_ERROR` (`uio[6]`, Pin map above), firmware has both a way to
notice the failure and a dedicated pin to report it on, rather than
just hanging. **Caveat, don't skip it**: `WAIT`'s max reach is ~20.3ms,
and a real host (e.g. Python-over-USB through the RP2040) can routinely
take longer than that to respond even when it's working fine -- a
single `WAIT HOST_GO,1,timeout` here risks a false `HOST_ERROR` against
a merely-slow host, not just a genuinely dead one. Use a `LOOP`-wrapped
multi-attempt bound instead of one long timeout for this specific
`WAIT`, per [isa.md](isa.md)'s `WAIT` row.

**Considered and rejected: pulse-pattern side-channel signaling** (e.g.
double-pulsing `HOST_STATUS` to mean "error" instead of a single pulse
for "normal"). Timing-pattern-based signaling is hard to formally
verify and easy to get a fencepost wrong on -- exactly what this whole
design has been avoiding. `HOST_ERROR` gives the same information
cleanly instead.

**Overhead, checked rather than assumed**: 5 instructions/byte against
even the fastest baseline case (UART 115200 baud, ~1450 instructions of
budget per byte at 50MHz -- see [protocol_timing.md](protocol_timing.md))
is ~0.3% -- unmeasurable. No pressure to pipeline or shortcut it.

**Explicitly not pipelined** -- one byte is fully acked before the next
one starts, no overlap. Consistent with this design's bias toward
verification simplicity over throughput; revisit only if something
concrete demands it, which the overhead check above suggests won't
happen.
