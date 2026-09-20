# Instruction Set Architecture

Status: word width, register file, instruction formats, and the full
19-opcode table are **locked**. The first 16 were validated by a
100-scenario adversarial generate/implement/verify audit (two-role
agent workflow) across UART/SPI/I2C master-mode operation -- 85/100
clean, 15 flagged issues, all traced to firmware-authoring bugs or
documentation gaps, zero requiring a new opcode or an opcode-semantics
change. See "Worked idioms" below for the two conventions the audit
surfaced as unspecified and the bug pattern they fix. `LOAD`/`STORE`/
`LOADX` were added afterward by a separate adversarial design review
(see Overview) -- not yet re-run through the 100-scenario audit
machinery, since they're infrastructure (data memory) rather than
protocol-bit-banging primitives the audit's scenario generator targets.

## Overview

- **16-bit instruction word**, chosen over 8-bit: an 8-bit word leaves an
  opcode+operand too cramped (4-bit opcode only leaves 4 bits for
  operands). 16-bit gives room for a 9-bit address field spanning the
  full program space while keeping a workable immediate field.
- Design philosophy: keep the opcode count small and fixed; protocol
  complexity (including stretch protocols) should mostly show up as
  longer *programs*, not more opcodes. Area/instruction-memory headroom
  is cheap (SRAM macro costs only 5.6% of the 6x4 tile budget -- see
  `architecture.md`).
- **This philosophy was false as originally stated, and an adversarial
  design review caught it**: a small opcode set can only express
  arbitrary logic given enough program memory *and* somewhere to put
  state that doesn't fit in 4 registers. With no `MOV`, no ALU op, and
  no data memory, there was nowhere to spill to -- a register-to-
  register copy needed ~48 instructions routed through a pin, and any
  protocol needing an accumulator wider than what's live in R0-R3 (a
  CRC being the sharpest example) was structurally impossible no matter
  how long the program was allowed to be. Fixed by adding real data
  memory -- see "Data memory" below -- which is what makes "longer
  programs" an actually-true escape valve instead of an aspiration.

## Register file

- **4x general-purpose 8-bit registers** (R0-R3).
- Chosen over more: covers baseline UART/SPI/I2C/JTAG/SWD/PS2
  comfortably (shift/data reg, bit counter, byte counter, one temp --
  ACK/status lives in the compare flag, not a register). CAN/Ethernet's
  CRC need (15-bit/32-bit accumulators) is the outlier -- two candidate
  fixes now exist rather than inflating the regfile: a CRC lookup table
  in the data region (see "Data memory") using `LOADX`, or a future
  dedicated CRC/LFSR peripheral (see Open questions). Which one's
  actually better is a question for when CAN/Ethernet get tackled, not
  now.
- Encoding-friendly: 2 bits addresses one register, so even two-operand
  ops (CMP Ra,Rb) only cost 4 bits total.
- **Ports: plain 2-read/1-write.** Checked every opcode format against
  this -- `CMP` is the only op reading two different registers at once
  (2 reads, 0 writes); `LOOP`/`INB`/`SHIFT` read-modify-write a single
  register (1 read + 1 write to the *same* address, which a standard
  synchronous-write/combinational-read regfile handles with no hazard).
  No opcode ever needs 2 reads + 1 write simultaneously. A conventional
  2R1W regfile is sufficient -- `regfile.v` is still an empty stub, this
  is what it should be built as. Re-checked after adding `LOAD`/`STORE`/
  `LOADX`: `LOAD` writes Rd (0 reads, 1 write), `STORE` reads Rd (1
  read, 0 writes), `LOADX` reads Rs and writes Rd (1 read, 1 write) --
  none of the three change the port-count conclusion.
- **Reset value: all registers 0.**

## Undefined opcode behavior

- Reserved/unused opcode encodings execute as **NOP** -- firmware never
  hangs, protocol timing never glitches from a stray illegal fetch.
- Also sets a **sticky illegal-opcode flag**, cleared only on reset.
  This is a formal/debug-visibility signal only -- no opcode reads it
  into R0-R3 or branches on it (a previous version of this doc said
  "register-readable," which was the wrong word; firmware cannot
  observe this flag, only external formal properties / a debug probe
  can). Deliberately not wired to a pin either, to keep the pinout
  budget free.
- Purpose: gives the assertion-formal agent (stage 3 verification, TBD)
  a clean provable invariant -- "illegal-opcode flag unreachable from
  any encoding path a real compiled program can produce" -- and gives
  the coverage-closure agent something concrete to target.

## Instruction formats

Three formats share the 16-bit word; opcode field is always 5 bits (32
encodings, well over the 19 currently in use -- see the Opcode table's
`value` column for the numeric assignment).

**Field order is MSB-first**, matching every format header below
(`opcode(5) | addr(9) | cond(2)` means opcode occupies the *top* 5
bits): `opcode = word[15:11]`, remaining fields pack down from
`word[10:0]` in the order shown. This was implicit in every format
header already; stated explicitly here because an assembler or `core.v`
author needs the exact bit positions, not just the field widths -- there
was no single place that said so before an adversarial review caught
the gap.

**Byte order matches the boot stream's own convention** ("low byte of
instruction 0 first", Pipeline's `LOAD` section): `word[7:0]` is the
low byte, fetched first (`FETCH_LO`, address `PC*2`); `word[15:8]` is
the high byte, fetched second (`FETCH_HI`, address `PC*2+1`). Since
`opcode = word[15:11]` lives entirely in the high byte, this is also
*why* decode has to wait for `FETCH_HI` to complete and gets merged
into `EXECUTE` rather than starting during `FETCH_HI` itself
(architecture.md Pipeline) -- the opcode simply isn't known yet before
then.

### Branch format -- `opcode(5) | addr(9) | cond(2)`

Unifies JMP / BEQ / BNE / CALL into a single opcode with a 2-bit
condition select:

| cond | behavior |
|---|---|
| `00` | always (JMP) |
| `01` | if flag set (BEQ) |
| `10` | if flag clear (BNE) |
| `11` | call (push return address to the depth-1 return register, then jump) |

`addr(9)` is 9 bits wide (0-511 encodable), but only word-addresses
0-255 are legitimate jump targets now that the SRAM is split 512/512
between program and data ("Data memory" below) -- program is 256
words, not the full 512 the field could technically reach. There's no
hardware guard against branching into the data region (256-511); it's
a firmware-discipline requirement. (This used to be analogized to
`pin_index` 6-7 being similarly unused/no-op -- that's no longer true,
both now have real hardware targets, see
[architecture.md](architecture.md)'s Pin map; `addr(9)`'s spare range
is the one genuinely unused encoding space left in the design.) The
spare encoding range is headroom, not currently meaningful.

`RET` is a separate 0-operand opcode (jumps to the return-address
register, no operand needed).

**CALL/RET: depth-1 return-address register, no return stack.** Locked
for now -- cheap, covers "one shared subroutine, called in a loop,
never nested" (e.g. I2C's `send_byte` routine, reused across UART/SPI/
I2C too). Widen to a small 2-4 level return stack later only if a
stretch protocol actually needs nested calls (e.g. a CAN frame routine
calling a bit-stuff-check routine calling a CRC-shift routine).

**A second `CALL` while one return address is already pending silently
overwrites it -- no protection, no flag.** This is the obvious
consequence of "no return stack," not a new decision, but it wasn't
written down as an explicit limitation like the full-duplex/CRC-LFSR
ones were, so: stated here. Firmware must not nest calls; nothing in
hardware stops it from trying.

**`CALL` also silently clobbers the shared flag if the subroutine uses
`CMP`/`TEST-bit`/`WAIT` internally** (every `WAIT` writes the flag, not
just a timed one -- see its row). Concretely: `TEST-bit R0,7` / `CALL
send_byte` / `BEQ was_one` -- if `send_byte` does its own `TEST-bit`/
`CMP`/`WAIT` anywhere (likely, since those are the only flag-writers),
`BEQ` branches on *that* result, not
the one the caller set up. This is the one real flag-clobber hazard in
the ISA -- confirmed to be CALL-shaped specifically, not a general "any
intervening instruction" hazard, now that `LOOP`/`SHIFT` are locked to
not touch the flag (their rows in the Opcode table) and `WAIT`'s
flag-write is a deliberate, documented tradeoff (its own row) rather
than an oversight. `LOAD`/`STORE`/`LOADX` (see "Data memory") now provide
the primitives to build a *software* stack in the data region if a
future protocol genuinely needs nested calls or needs to save the flag
across a `CALL` -- not designed here (that's a firmware/assembler
convention, not an ISA-level decision), just noting the capability now
exists where it didn't before.

**Both `CALL`/`RET` misuse hazards above are now diagnosable, not just
documented.** One bit of internal state, `return-valid` -- set by
`CALL` (a return address is now pending), cleared by `RET` (consumed),
reset value 0 -- plus a second sticky debug flag, same class as the
illegal-opcode flag (Undefined opcode behavior section): formal/
debug-visibility only, not firmware-readable, cleared only on reset.
Set when either:
- `CALL` executes while `return-valid=1` -- the nested-call overwrite
  hazard documented above.
- `RET` executes while `return-valid=0` -- an orphan `RET` with no
  pending address, which would otherwise silently jump to whatever the
  return-address register happens to hold (its reset value, 0, if
  nothing's ever called yet). Symmetric case, same tracking bit, added
  alongside the reviewed finding rather than as a separate later fix --
  near-zero marginal cost once `return-valid` exists for the first
  condition.

Cost: one state bit, one sticky flag, two trivial comparisons -- same
gate count as the illegal-opcode flag, catching a bug that can actually
happen in real firmware rather than one that (per its own row) is
"unreachable from any encoding path a real compiled program can
produce."

### Reg+immediate format -- `opcode(5) | Rd(2) | imm(9)`

For `LDI`, `DELAY`, `SET`, `WAIT`, `LOAD`, `STORE`, shift-amount,
bit-test-index, and similar single-register+constant ops. The 9-bit
immediate covers 0-511 range values with room to spare (LDI's 8-bit
load value occupies `imm(7:0)`, `imm(8)` unused). `DELAY`, `WAIT`, and
`SET` are the exceptions -- all three repurpose their otherwise-unused
`Rd` field (a timeout/delay exponent for `DELAY`/`WAIT`, a per-pin
drive-mode select for `SET`) rather than a register select; see their
rows in the Opcode table.

### LOOP -- `opcode(5) | addr(9) | Rd(2)`

Same bit shape as the branch format (5+9+2=16) but the trailing 2 bits
select the decrement/counter register (R0-R3) instead of a condition.
Decrements Rd, branches to `addr` if nonzero. Decoder can share
addr-extract logic with the branch format.

### Reg-reg format -- `opcode(5) | Rd(2) | Rs(2) | reserved(7)`

For `CMP` (sets flag from Rd/Rs comparison) and `LOADX` (register-
indexed data-memory read, address = `512 + Rs`, result to Rd) -- both
fit the existing `Rd`/`Rs` fields exactly, no encoding change needed.
7 bits still unused/reserved (available for a future two-register op,
e.g. MOV, or a `STOREX` if a movable-address indexed write ever turns
out to be needed).

## Opcode table

| value | op | format | status | notes |
|---|---|---|---|---|
| `0` | NOP | 0-operand | locked | Deliberately 0 -- makes the all-zero 16-bit word (and any word with just the top 5 bits zero, since the low 11 bits are don't-care for a 0-operand op) a valid NOP encoding. Not a special case: a natural consequence of the assignment, consistent with "undefined opcode -> NOP" already being the safe default elsewhere. |
| `1` | HALT | 0-operand | locked | Freezes the FSM -- stops fetching, no further state changes. All outputs (including `uio_oe`) hold their last-driven state; does **not** release pins or touch `HOST_STATUS`. Matters concretely since a mid-protocol `HALT` could leave e.g. SPI `CS` asserted or I2C `SCL` held low. Resumable only by external reset -- `HOST_GO` does not resume from `HALT`. |
| `2` | RET | 0-operand | locked | jumps to return-addr register |
| `3` | BRANCH (JMP/BEQ/BNE/CALL) | branch | locked | cond field selects behavior |
| `4` | LDI | reg+imm | locked | load immediate -> Rd |
| `5` | SET | reg+imm | locked | drive pin to a COMPILE-TIME constant: `imm` = pin_index(3) + value(1). `Rd(2)` repurposed as a **sticky per-pin drive-mode**: `00`=leave mode as-is (just drive `value` per whatever mode is currently configured), `01`=configure **push-pull** and drive `value` immediately (oe=1, out=value), `10`=configure **open-drain** and drive `value` immediately (value=1 -> release, oe=0, pulled high externally; value=0 -> drive low, oe=1, out=0), `11`=configure **input** (oe forced 0, `value` ignored). Reset: all 5 protocol pins (`uio[3:0]`, `uio[7]`) start in input mode -- same "safe by construction" default as everywhere else in this design (illegal-opcode->NOP, unwired `pin_index`->no-op); a `SET`/`OUTB` targeting an unconfigured pin is a harmless no-op, not undefined behavior. Fixes a real hole an adversarial review found: earlier drafts said direction was "fixed per-pin by the pinout, or derived from SET's value bit for open-drain pins" -- but the pin map makes every protocol pin serve *different* protocols with *different* drive requirements at different times (pin 0 is UART TX, push-pull, in one firmware and I2C SDA, open-drain, in another), and nothing distinguished which mode hardware should use. `OUTB` (see its row) reads this same sticky mode -- it has no mode field of its own. Needs an external pull-up on any protocol pin a firmware image configures open-drain -- updates `docs/info.md`'s "External hardware" section, previously "None". |
| `6` | OUT | reg+imm | locked | drive Rd (all 8 bits) onto the host-facing **parallel** data bus in one shot -- not the serial protocol pin. `imm` unused. |
| `7` | IN | reg+imm | locked | read the host-facing parallel data bus into Rd (all 8 bits) in one shot. `imm` unused. |
| `8` | SHIFT | reg+imm | locked | shift Rd by exactly 1 bit; `imm(0)` = direction, **`0` = shift left, `1` = shift right** (locked from unanimous convention across the 100-scenario audit corpus -- previously unspecified). **Vacated bit is always 0** (plain logical shift -- no rotate, no carry-through-flag) and **`SHIFT` does not touch the shared flag**. Considered making it rotate-through-flag instead (vacated bit = old flag, flag = bit shifted out), which would let two `SHIFT`s chain across a register pair as a carry and fix multi-byte shift -- rejected: that makes `SHIFT` a third flag-writer, reopening the "no implicit flag writers" finding (Branch format section) to solve a problem baseline protocols don't have (multi-byte shift only matters for CRC, already deferred behind the CRC/LFSR-peripheral-or-lookup-table escape valve). No shift-amount field -- bit-bang code shifts 1 bit/instruction anyway (shift, wait, repeat via `LOOP`), matching the "complexity lives in program length" rule. Replaces the earlier separate SHL/SHR idea. |
| `9` | DELAY | reg+imm | locked | fixed-cycle wait. `Rd(2)` repurposed as **exponent** (not a register select -- `DELAY` doesn't touch the regfile), `imm(9)` is the **mantissa**: `cycles = mantissa << (exponent*5)`. Reach: exponent=0 -> 0-511 (1-cycle granularity, identical to a flat 9-bit immediate -- nothing that already fit changes), exponent=1 -> up to 16,352 (32-cycle granularity), exponent=2 -> up to ~523K (1024-cycle granularity), exponent=3 -> up to ~16.7M (~335ms @ 50MHz). Locked after [protocol_timing.md](protocol_timing.md) found a flat 511-cycle max couldn't reach 9600/19200/38400/57600 baud UART (only 115200 fit) without a register-burning `LOOP`-wrapped nested delay -- checked against every baseline rate in that table, worst case is 0.77% error (38400 baud), all comfortably inside UART's ~2% tolerance (an earlier draft of this shift constant, 6 instead of 5, put 57600 baud at 3.2% error -- caught before locking). No register-sourced variant -- compose via `LOOP` if a firmware-computed (not compile-time-constant) count is ever needed. |
| `10` | WAIT | reg+imm | locked | block until pin==level, **with an optional timeout**. `imm` = pin_index(3) + level(1) + timeout_mantissa(5); `Rd(2)` repurposed as timeout_exponent (same pattern as `DELAY`, and literally the same hardware countdown counter -- `WAIT` and `DELAY` never execute simultaneously on a single-issue sequencer, and both reload the counter on entry to `EXECUTE`; a stale leftover count from a previous `DELAY`/`WAIT` never matters since every use reloads fresh). `cycles = mantissa << (exponent*5)`; **any encoding whose computed `cycles` is 0 -- not just the literal `exponent=0,mantissa=0` case, e.g. `exponent=2,mantissa=0` too -- means no timeout** (unbounded wait, today's behavior). One uniform "computed-zero means unbounded" rule beats special-casing a single bit pattern -- avoids a firmware footgun where a mistyped nonzero-exponent/zero-mantissa silently becomes an instant timeout instead of the unbounded wait probably intended. Otherwise max reach ~1,015,808 cycles (~20.3ms @ 50MHz) -- comfortably above realistic I2C clock-stretch durations, but shorter than some real hosts' response latency (a `WAIT HOST_GO,1,timeout` in the host handshake can false-trigger against a slow host -- compose via `LOOP` for a coarser multi-attempt bound instead of a single long timeout there). If the pin condition becomes true on the exact same cycle the counter would expire, **condition-met wins** -- a real bus event landing on the boundary should count as success, not a spurious failure; also simpler to implement, check the condition first. **Every `WAIT` writes the shared flag on exit, uniformly, no exceptions**: 0 if the pin condition was met, 1 if it timed out. For an unbounded `WAIT` this is always 0 (no timeout mechanism is active to ever produce a 1) -- not a special hardware case, just what the same logic naturally produces. (An earlier draft of this row and the Branch format section's CALL discussion disagreed -- one said every `WAIT` writes, the other said only "`WAIT`-with-timeout" does. This row is the corrected, single source of truth; the CALL section has been fixed to match.) This makes `WAIT` a third flag-writer (alongside `CMP`/`TEST-bit`), the tradeoff for making a hang actually detectable/recoverable by firmware (`WAIT SCL,1,timeout` / `BEQ error_handler` -- note `BEQ`, not `BNE`: cond `01`/flag-set means timed-out, an earlier draft of this exact idiom had the polarity backwards) rather than permanent with no escape, which was the original design's real flaw: `WAIT SCL,1` for I2C clock-stretch is sold as the killer feature, but a slave holding SCL low forever hung the chip with no detection path and no error recovery possible. Also how I2C clock-stretching gets handled: `WAIT SCL,1` blocks until the slave actually releases the clock, not just until we do -- now bounded rather than potentially forever. |
| `11` | TEST-bit | reg+imm | locked | test bit N of Rd, set flag; `imm` = bit_index(3). **Flag polarity: flag=1 iff the tested bit=1** (locked from unanimous convention across the audit corpus -- previously unspecified). Same shared flag register as `CMP` -- see that row. |
| `12` | OUTB | reg+imm | **locked** | drive pin = Rd[bit] (bit is 0 or 7, matching shift direction -- read bit0 before a right-shift discards it, bit7 before a left-shift discards it). `imm` = pin_index(3) + bit_select(1). Closes a gap found by hand-tracing UART/SPI/I2C: neither `SET` (compile-time constant only) nor `OUT` (whole-byte, whole-bus) can drive one pin from one *live* register bit every bit-period -- the actual core operation of bit-banging any of the three baseline protocols. Validated by the 100-scenario audit; see "Worked idioms" for the correct ordering with `SHIFT`. **Obeys the pin's current sticky drive-mode** (`SET`'s row) -- has no mode field of its own, so on an open-drain pin, `Rd[bit]=1` releases and `Rd[bit]=0` drives low, same as `SET`'s value semantics; on push-pull it drives directly; on an unconfigured (input) pin it's a no-op. This is what makes I2C's live bit-shifting actually open-drain without per-bit mode juggling: `SET SDA,10,1` once to configure, then every `OUTB` in the bit-loop is automatically correct. |
| `13` | INB | reg+imm | **locked** | set Rd[bit] = pin's current value, other bits of Rd untouched. `imm` = pin_index(3) + bit_select(1). Mirror of `OUTB` for the receive path. Validated by the 100-scenario audit -- **ordering with `SHIFT` is the opposite of `OUTB`'s**, see "Worked idioms"; this asymmetry, undocumented until now, caused the single most common bug across the whole audit (7/15 flagged scenarios made the identical mistake). |
| `14` | LOAD | reg+imm | locked | read data memory into Rd. Address = `512 + imm` (imm(9) reaches the full 512-byte data region -- see "Data memory"). Added alongside `STORE`/`LOADX` to fix a real gap an adversarial design review found: no ALU, no `MOV`, and no data memory meant registers alone couldn't hold cross-subroutine or accumulator state, making the "longer programs, not more opcodes" philosophy (Overview) false for anything needing more live state than 4 registers -- a CRC accumulator being the sharpest example. |
| `15` | STORE | reg+imm | locked | write Rd's value to data memory, address = `512 + imm`. Note: the `Rd` field holds the *source* register here, same field position as `LOAD`'s destination, just read instead of written. |
| `16` | LOOP | addr+Rd | locked | decrement Rd, branch if nonzero. **Does not touch the shared flag** -- the branch decision is internal to `LOOP` itself, no reason to also expose it through the flag `CMP`/`TEST-bit`/`BRANCH` share. Locked alongside `SHIFT`'s flag behavior above, for the same reason: keeps the set of flag-writers small and *named* (`CMP`, `TEST-bit`, and `WAIT` -- see its row -- not "any instruction"), so the "flag-clobber hazard is CALL-shaped/WAIT-shaped, not general-instruction-shaped" finding (Branch format section) stays true rather than becoming conditional. |
| `17` | CMP | reg-reg | locked (speculative) | Equality only: sets the shared flag to `(Rd==Rs)`. Same physical flag as `TEST-bit` -- one flag register, last write wins, not separate state per opcode. Not used by any baseline UART/SPI/I2C master-mode path found so far (`LOOP` covers counters, `TEST-bit`+branch covers the I2C ACK check) -- kept for future protocols (JTAG state compare, CAN ID filter), not pulling weight yet on baseline. |
| `18` | LOADX | reg-reg | locked | read data memory into Rd, address = `512 + Rs` (register-indexed -- Rs's full 8-bit range reaches the first 256 bytes of the data region). What makes a runtime-indexed lookup table (a CRC table, most concretely) actually usable -- `LOAD`/`STORE`'s `imm` is compile-time-only, can't be indexed by a runtime value. |
| `19-31` | *reserved* | -- | -- | Unassigned. Executes as `NOP` + sets the illegal-opcode flag if ever fetched (Undefined opcode behavior, above) -- same "safe by construction" treatment as every other unimplemented encoding in this design. |

**19 opcodes total** (16 validated by the 100-scenario audit, above,
plus `LOAD`/`STORE`/`LOADX` added by a later adversarial design review)
-- comfortably inside the 32-slot 5-bit opcode space, 13 slots spare.
Numeric values assigned by format grouping for readability (`0-2`
0-operand, `3` branch, `4-15` reg+imm, `16` addr+Rd, `17-18` reg-reg) --
hardware cost is identical for any assignment, a 5-bit decoder doesn't
care about grouping, this is purely for a human reading the table.

The first 16 were validated by a 100-scenario adversarial generate/
implement/verify audit (two agent roles: one wrote firmware for a
scenario using only those 16 opcodes, a second independently traced
the code instruction-by-instruction and tried to disprove it). Zero of
the 15 flagged scenarios needed a new opcode or a semantics change --
see "Worked idioms" below and the "Known limitations" note for what
the audit actually found. `LOAD`/`STORE`/`LOADX` postdate that audit
and haven't been re-run through it (they're data-memory
infrastructure, not protocol-bit-banging primitives the audit's
scenario generator targets) -- see "Data memory" for what motivated
them.

## Data memory

Locked. The 1024-byte SRAM macro is split **512/512**: bytes 0-511 are
program (256 words -- see [architecture.md](architecture.md)'s memory
map), bytes 512-1023 are data, reached via `LOAD`/`STORE`/`LOADX`.

- 256 words of program space is still 1.7-5x the review's own estimate
  of a realistic 50-150 word baseline protocol program -- not a real
  constraint.
- Of the 512 data bytes: the first 256 (512-767) are reachable both by
  fixed address (`LOAD`/`STORE`) and by register index (`LOADX`, since
  `Rs` is an 8-bit register and can address exactly that range) -- a
  clean fit for a standard 256-entry byte-wide CRC lookup table. The
  remaining 256 (768-1023) are fixed-address-only, for spill/scratch
  use.
- **No hardware bounds checking.** `imm`/`Rs` values that don't fit the
  addressing scheme they're used with (e.g. an `LOADX` `Rs` value,
  which only ever reaches 512-767) simply can't express an out-of-range
  address -- there's no invalid encoding to guard against, the field
  width itself is the bound.
- **`LOAD`-mode boot needs no changes.** It already streams the full
  1024 bytes sequentially into the SRAM ([architecture.md](architecture.md)
  Pipeline); the second half is simply treated as data once execution
  starts rather than being fetched as instructions. A nice side effect:
  the host can preload a CRC table or config constants through the
  exact same boot sequence used for firmware, no separate mechanism
  needed.
- Enables (not designed here -- these are firmware/assembler
  conventions, not ISA-level decisions): CRC lookup tables, spill space
  for values that don't fit in 4 live registers, and a software stack
  if a future protocol needs saved state across a `CALL` (see the
  Branch format section's `CALL` notes) or nested subroutine calls
  beyond the depth-1 hardware return register.

## Worked idioms

Two bugs recurred across the audit corpus because these compositions
weren't spelled out anywhere. Both are otherwise-correct uses of
already-locked opcodes -- get the order right and nothing else changes.

**Both idioms below assume the pin is already configured.** Neither
shows the one-time `SET pin,mode,idle_value` that has to run once at
protocol init before the bit-loop -- `OUTB` obeys whatever drive-mode
`SET` last configured (`SET` row) and is a no-op against a pin still in
its reset input state. E.g. for TX: `SET TXPIN,01,1` (push-pull,
idle-high) before entering `TX_BIT_LOOP` the first time.

**TX (shift a register out one bit at a time):** output *before*
shifting -- `OUTB` reads the boundary bit (bit7 for a left-shifting/
MSB-first send) before the next `SHIFT` would discard it.

```
TX_BIT_LOOP:
  OUTB  R0, TXPIN, 7      ; drive TXPIN = R0[bit7]
  DELAY BIT_PERIOD
  SHIFT R0, 0              ; shift left; bit7 is now consumed, safe to discard
  LOOP  TX_BIT_LOOP, R1
```

**RX (capture a register in one bit at a time):** shift *before*
capturing -- the opposite order from TX. Shifting first vacates the
target bit position; capturing into an already-shifted register is what
correctly reconstructs the byte. Doing this in TX's order (capture then
shift) silently drops the first-received bit off the end of the
register on every single use -- this exact mistake caused 7 of the
audit's 15 flagged scenarios.

```
RX_BIT_LOOP:
  DELAY BIT_PERIOD
  SHIFT R0, 0               ; shift left to vacate bit0 first
  INB   R0, RXPIN, 0         ; then capture RXPIN into R0[bit0]
  LOOP  RX_BIT_LOOP, R1
```

## Known limitations

- **Fully independent-phase full-duplex** (e.g. simultaneous UART TX and
  RX on two unrelated timing sources, with no shared bit-period
  reference) is **not supported** and is an intentional scope boundary,
  not a bug to fix later. Reason: `DELAY`/`WAIT` block the whole fetch
  stream and there's no interrupt/preemption mechanism, so servicing two
  truly independent bit streams needs per-channel oversampling state
  (shift reg + bit position + sub-bit tick countdown, ~3 registers each,
  6 total) that the 4-register file can't hold. Full-duplex where TX and
  RX share a common timing grid (arbitrary but bounded phase offset --
  what real bit-banged UART use cases actually need) works fine via
  interleaved polling. Same treatment as the CRC/LFSR deferral below:
  a real limitation, explicitly scoped out rather than silently assumed
  away.

## Open questions

- CRC/LFSR peripheral design (for CAN/Ethernet) -- deferred until those
  stretch protocols are actually tackled, not blocking core ISA lock.
