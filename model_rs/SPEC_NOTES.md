# SPEC_NOTES -- ambiguities resolved by model_rs

Sources: `docs/isa.md`, `docs/architecture.md`, `model_rs/INTERFACE.md` only.
Each entry gives the spec text, the reading I implemented, and how much it matters.

## N1. Where SET / OUTB / INB sub-fields sit inside `imm(9)` -- RESOLVED BY SPEC
Originally ambiguous: isa.md only said "`imm` = pin_index(3) + value(1)", and I
had read it as low-justified (`imm[3:1]` / `imm[0]`), following LDI/SHIFT.
**Resolved by updated isa.md "Instruction formats"**: "The same MSB-first rule
applies to sub-fields inside `imm(9)` ... SET / OUTB / INB: `pin_index(3)` is
`imm[8:6]`, and `value`/`bit_select(1)` is `imm[5]`. `imm[4:0]` is unused."
The model now does exactly that. pin_index sits in `imm[8:6]` for SET, OUTB,
INB and WAIT alike, and `imm[4:0]` of SET/OUTB/INB is ignored. Test:
`imm_subfields_msb_first`, which uses raw literal words.

## N2. Other narrow imm fields
TEST-bit "`imm` = bit_index(3)": implemented as `imm[2:0]`. SHIFT direction:
`imm[0]`. Both, along with LDI `imm(7:0)`, are now confirmed by isa.md:
"Single-value immediates keep the low bits their rows name". Unused bits are ignored and never set the illegal flag, including
LDI `imm(8)`, OUT/IN "`imm` unused", the 7 reserved bits of the reg-reg
format, and NOP's low 11 bits. The illegal flag is set only by opcodes 19-31.

## N3. PC width / wrap
PC is a 9-bit word index, from the "`addr(9)`" field and "word index ... the
fetch unit does the x2 translation". `PC+1` and the CALL return address wrap
mod 512, so 511+1 = 0. The spec never says what happens past 511.
Fetching at PC 256-511 reads bytes 512-1023 from the same SRAM, which is the
data region. The spec says "There's no hardware guard against branching into
the data region", so STOREd bytes can be executed and the memory is modelled as
one 1024-byte array.

## N4. HALT's reported `pc` and cycles (MEDIUM)
"Freezes the FSM -- stops fetching, no further state changes." HALT's commit
reports `cycles = 3` (uniform 3 cycles/instruction) and `state.pc` = **HALT's own
address**, not +1. Nothing is ever fetched again, and a frozen FSM has not
advanced. The other reading (pc+1) is plausible too.

## N5. `max_cycles` and the in-flight instruction
INTERFACE: "stop ... once the total cycle count exceeds this". Implemented: an
instruction commits only if its end cycle (start + occupancy) is <= max_cycles.
An instruction still in flight when the budget is crossed, such as a long DELAY
or an unbounded WAIT, is **not** committed, and `timeout: true` is reported. A
HALT that ends exactly at max_cycles counts as halted.

## N6. WAIT timing / tie rule
Occupancy is 3+k when the condition is first true at execute cycle `start+2+k`.
For a timed WAIT with N = mantissa<<(exp*5), the condition is checked at k = 0..N,
which is N+1 samples, and a timeout gives occupancy 3+N with flag=1. This
matches architecture.md "3+N if a timeout is set" and DELAY's 3+N, which share
the counter. The tie rule ("condition-met wins") applies at k = N: if the pin
matches there, flag=0 with occupancy 3+N. If the counter is meant to expire one
cycle earlier (N samples), timeout occupancy would still be 3+N but the last
sample would be lost. I chose N+1 samples. When the computed N is 0 the wait is
unbounded, so for example exp=2/mant=0 never times out and flag=0.

## N7. LOAD/LOADX trailing writeback
architecture.md says the Rd write lands at the end of the *next* instruction's
FETCH_LO. Since that is always before the next EXECUTE, it is architecturally
invisible. I report Rd as already updated in the LOAD/LOADX commit's state,
and that commit is 3 cycles.

## N8. `pin_mode` for the hardwired pins 4/5/6 -- NO LONGER COMPARED
INTERFACE.md now says entries 4/5/6 are not compared ("report anything"). The
model's behaviour is unchanged and is kept here for the record.
INTERFACE asks for 8 modes "using the spec's 2-bit SET mode encoding", but the
spec says the oe of pins 4/5/6 is "fixed by its role (hardwired ...)" and is not
sticky-configurable. These are reported as fixed role modes, unchanged by SET:
pin4 (HOST_GO) = 3 (input), pin5 (HOST_STATUS) = 1 (push-pull output), pin6
(HOST_ERROR) = 1. The START-fall gating of pin 6 belongs to the boot loader and
is out of scope, so pin 6 is treated as already enabled. Protocol pins 0-3 and 7
reset to 3 (input). Mode 0 ("leave as-is") is never stored.

## N9. SET / OUTB on hardwired pins
pin 4: SET and OUTB do nothing at all, because oe is hardwired 0. pins 5/6: SET
drives `value` whatever the Rd mode field says, *including* mode `11`, whose
"value ignored" wording conflicts with "oe fixed by role, never by which opcode
currently targets it". I chose the role rule. OUTB drives Rd[bit].

## N10. `pin_drv` semantics
INTERFACE: "the value each pin drives / last driven". The value stored is the
**logical commanded value**:
- push-pull: drv = value.
- open-drain: drv = value, so 1 means released (oe=0) and 0 means driven low.
  The uio_out bit on release is not specified, so the commanded level is stored.
- input mode, or SET mode 00 / OUTB on an unconfigured input pin: a "harmless
  no-op", so drv is unchanged.
- SET mode 11 changes the mode only and leaves drv as it was.
- SET mode 01/10 sets the mode and drv together.
Reset is all 0 ("`uio_out` resets to all-0").

## N11. OUTB / INB bit_select
"bit is 0 or 7": bit_select 0 selects bit 0 and 1 selects bit 7.

## N12. LOOP on Rd = 0
"decrement Rd, branch if nonzero": the 8-bit decrement wraps, 0 becomes 255,
and the branch is taken. Wrap behaviour is not stated.

## N13. Pin sampling cycles
IN, INB and WAIT read `ui[t]` / `uio[t]` at their execute cycle(s). For IN and
INB that is the single cycle `start+2`; for WAIT it is every cycle from
`start+2` on (INTERFACE: first execute cycle = first fetch cycle + 2). `uio` bit
p is pin_index p, which includes 4/5/6: WAIT/INB on pin 5/6 is the
"self-loopback" read, taken from the given pad array. An index past the array
end reads 0.

## N14. CALL / RET details
CALL: retaddr = (PC+1) mod 512; misuse flag set if return_valid was already 1;
return_valid = 1. RET: jumps to retaddr even when return_valid = 0, which with
no prior CALL is the reset value 0. Misuse flag set if return_valid was 0;
return_valid cleared; retaddr itself is unchanged. Both flags are sticky.

## N15. Input handling (interface, not ISA)
Lines that are empty or whitespace-only are skipped and get no output line.
`image` must be exactly 1024 ints 0-255; `ui`/`uio` are optional and default to
[]; `max_cycles` is a required non-negative int. Any violation produces
`{"id":<id or null>,"error":...}`.

## Spec-internal inconsistencies noticed (no model impact)
- isa.md Register file says reg-reg has "13 spare opcode slots" for XOR; with
  19 opcodes, 13 of 32 slots are spare, which agrees with the opcode table.
  There is no actual conflict.
- isa.md's CALL section says LOAD/STORE/LOADX are "infrastructure". The SET row
  says pin_index 6/7 "now both have real hardware targets", which matches the
  architecture.md pin map. Consistent.
- architecture.md LOAD section says `uio[6:7]` are Hi-Z during LOAD, while the
  pin map says uio[7] is a protocol pin. Both are about boot, so this is out of
  scope.
