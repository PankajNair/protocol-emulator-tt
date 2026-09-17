# Instruction Set Architecture

Status: word width, register file, instruction formats, and the full
16-opcode table are **locked**. Validated by a 100-scenario adversarial
generate/implement/verify audit (two-role agent workflow) across UART/
SPI/I2C master-mode operation -- 85/100 clean, 15 flagged issues, all
traced to firmware-authoring bugs or documentation gaps, zero requiring
a new opcode or an opcode-semantics change. See "Worked idioms" below
for the two conventions the audit surfaced as unspecified and the bug
pattern they fix.

## Overview

- **16-bit instruction word**, chosen over 8-bit: an 8-bit word leaves an
  opcode+operand too cramped (4-bit opcode only leaves 4 bits for
  operands). 16-bit gives room for a 9-bit address field spanning the
  full program space while keeping a workable immediate field.
- Design philosophy: keep the opcode count small and fixed; protocol
  complexity (including stretch protocols) should mostly show up as
  longer *programs*, not more opcodes -- a small ISA can express
  arbitrary protocol timing/logic given enough program memory. Area/
  instruction-memory headroom is cheap (SRAM macro costs only 5.6% of
  the 6x4 tile budget -- see `architecture.md`), so this design uses the
  macro's full addressable program space rather than artificially
  constraining instruction count.

## Register file

- **4x general-purpose 8-bit registers** (R0-R3).
- Chosen over more: covers baseline UART/SPI/I2C/JTAG/SWD/PS2
  comfortably (shift/data reg, bit counter, byte counter, one temp --
  ACK/status lives in the compare flag, not a register). CAN/Ethernet's
  CRC need (15-bit/32-bit accumulators) is the outlier -- addressed via
  a future dedicated CRC/LFSR peripheral instead of inflating the
  regfile (see Open questions).
- Encoding-friendly: 2 bits addresses one register, so even two-operand
  ops (CMP Ra,Rb) only cost 4 bits total.

## Undefined opcode behavior

- Reserved/unused opcode encodings execute as **NOP** -- firmware never
  hangs, protocol timing never glitches from a stray illegal fetch.
- Also sets a **sticky illegal-opcode flag**, cleared only on reset.
  Register-readable, deliberately not wired to a pin (keeps the pinout
  budget free).
- Purpose: gives the assertion-formal agent (stage 3 verification, TBD)
  a clean provable invariant -- "illegal-opcode flag unreachable from
  any encoding path a real compiled program can produce" -- and gives
  the coverage-closure agent something concrete to target.

## Instruction formats

Three formats share the 16-bit word; opcode field is always 5 bits (32
encodings, well over the ~15 currently in use).

### Branch format -- `opcode(5) | addr(9) | cond(2)`

Unifies JMP / BEQ / BNE / CALL into a single opcode with a 2-bit
condition select:

| cond | behavior |
|---|---|
| `00` | always (JMP) |
| `01` | if flag set (BEQ) |
| `10` | if flag clear (BNE) |
| `11` | call (push return address to the depth-1 return register, then jump) |

`addr(9)` reaches the full 512-word program space (16-bit instructions
x 512 = 1024 bytes = the SRAM macro's full 1KB capacity).

`RET` is a separate 0-operand opcode (jumps to the return-address
register, no operand needed).

**CALL/RET: depth-1 return-address register, no return stack.** Locked
for now -- cheap, covers "one shared subroutine, called in a loop,
never nested" (e.g. I2C's `send_byte` routine, reused across UART/SPI/
I2C too). Widen to a small 2-4 level return stack later only if a
stretch protocol actually needs nested calls (e.g. a CAN frame routine
calling a bit-stuff-check routine calling a CRC-shift routine).

### Reg+immediate format -- `opcode(5) | Rd(2) | imm(9)`

For `LDI`, `DELAY`, `SET`, `WAIT`, shift-amount, bit-test-index, and
similar single-register+constant ops. The 9-bit immediate covers DELAY
cycle counts and 0-511 range values with room to spare (LDI's 8-bit
load value fits with 1 bit unused).

### LOOP -- `opcode(5) | addr(9) | Rd(2)`

Same bit shape as the branch format (5+9+2=16) but the trailing 2 bits
select the decrement/counter register (R0-R3) instead of a condition.
Decrements Rd, branches to `addr` if nonzero. Decoder can share
addr-extract logic with the branch format.

### Reg-reg format -- `opcode(5) | Rd(2) | Rs(2) | reserved(7)`

For `CMP` (sets flag from Rd/Rs comparison) and any future two-register
op (e.g. MOV). 7 bits currently unused/reserved.

## Opcode table

| op | format | status | notes |
|---|---|---|---|
| NOP | 0-operand | locked | |
| HALT | 0-operand | locked | |
| RET | 0-operand | locked | jumps to return-addr register |
| BRANCH (JMP/BEQ/BNE/CALL) | branch | locked | cond field selects behavior |
| LDI | reg+imm | locked | load immediate -> Rd |
| LOOP | addr+Rd | locked | decrement Rd, branch if nonzero |
| CMP | reg-reg | locked (speculative) | sets flag from Rd vs Rs. Not used by any baseline UART/SPI/I2C master-mode path found so far (`LOOP` covers counters, `TEST-bit`+branch covers the I2C ACK check) -- kept for future protocols (JTAG state compare, CAN ID filter), not pulling weight yet on baseline. |
| SET | reg+imm | locked | drive pin to a COMPILE-TIME constant: `imm` = pin_index(3) + value(1). On an open-drain pin (I2C SDA/SCL), value=1 means *release* (oe=0, pulled high externally), value=0 means *drive low* (oe=1). No separate direction opcode -- direction is either fixed per-pin by the pinout, or derived from SET's value bit for open-drain pins. |
| OUT | reg+imm | locked | drive Rd (all 8 bits) onto the host-facing **parallel** data bus in one shot -- not the serial protocol pin. `imm` unused. |
| IN | reg+imm | locked | read the host-facing parallel data bus into Rd (all 8 bits) in one shot. `imm` unused. |
| SHIFT | reg+imm | locked | shift Rd by exactly 1 bit; `imm(0)` = direction, **`0` = shift left, `1` = shift right** (locked from unanimous convention across the 100-scenario audit corpus -- previously unspecified). No shift-amount field -- bit-bang code shifts 1 bit/instruction anyway (shift, wait, repeat via `LOOP`), matching the "complexity lives in program length" rule. Replaces the earlier separate SHL/SHR idea. |
| DELAY | reg+imm | locked | fixed-cycle wait, full 9-bit immediate (0-511 cycles). `imm`-only, Rd unused. No register-sourced variant -- compose via `LOOP` if a variable count is ever needed. |
| WAIT | reg+imm | locked | block until pin==level; `imm` = pin_index(3) + level(1). Also how I2C clock-stretching gets handled for free: `WAIT SCL,1` blocks until the slave actually releases the clock, not just until we do. |
| TEST-bit | reg+imm | locked | test bit N of Rd, set flag; `imm` = bit_index(3). **Flag polarity: flag=1 iff the tested bit=1** (locked from unanimous convention across the audit corpus -- previously unspecified). |
| OUTB | reg+imm | **locked** | drive pin = Rd[bit] (bit is 0 or 7, matching shift direction -- read bit0 before a right-shift discards it, bit7 before a left-shift discards it). `imm` = pin_index(3) + bit_select(1). Closes a gap found by hand-tracing UART/SPI/I2C: neither `SET` (compile-time constant only) nor `OUT` (whole-byte, whole-bus) can drive one pin from one *live* register bit every bit-period -- the actual core operation of bit-banging any of the three baseline protocols. Validated by the 100-scenario audit; see "Worked idioms" for the correct ordering with `SHIFT`. |
| INB | reg+imm | **locked** | set Rd[bit] = pin's current value, other bits of Rd untouched. `imm` = pin_index(3) + bit_select(1). Mirror of `OUTB` for the receive path. Validated by the 100-scenario audit -- **ordering with `SHIFT` is the opposite of `OUTB`'s**, see "Worked idioms"; this asymmetry, undocumented until now, caused the single most common bug across the whole audit (7/15 flagged scenarios made the identical mistake). |

**16 opcodes total** (14 original + `OUTB`/`INB` found missing when
tracing UART TX/RX, SPI mode-0, and I2C start/address/ACK/stop by hand)
-- comfortably inside the 32-slot 5-bit opcode space, 16 slots spare.

Validated by a 100-scenario adversarial generate/implement/verify audit
(two agent roles: one wrote firmware for a scenario using only these 16
opcodes, a second independently traced the code instruction-by-
instruction and tried to disprove it). Zero of the 15 flagged scenarios
needed a new opcode or a semantics change -- see "Worked idioms" below
and the "Known limitations" note for what the audit actually found.

## Worked idioms

Two bugs recurred across the audit corpus because these compositions
weren't spelled out anywhere. Both are otherwise-correct uses of
already-locked opcodes -- get the order right and nothing else changes.

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
- Fetch/decode/execute cycle timing (how many cycles per instruction,
  whether branch/loop cost extra cycles) -- a core-RTL question, not an
  ISA-encoding one; tracked in `architecture.md`'s Pipeline section.
