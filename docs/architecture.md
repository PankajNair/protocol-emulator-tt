# Architecture

Status: not designed yet. This doc tracks decisions as they're made.

## Decided

- **Core**: minimal sequencer ISA, not a general CPU. Op classes:
  SET / WAIT / IN / OUT / DELAY / JMP / LOOP-style. Supersedes the vaguer
  "tiny CPU" framing `src/cpu/isa_defs.v`'s placeholder opcodes were
  written against -- that file needs updating to match.
- **Instruction memory**: SRAM-backed, not synthesized ROM. Candidate
  macro: `tt_um_urish_sram_test` on the IHP shuttle -- 1024x8 (1KB),
  single-port, byte-addressable, 10-bit address (6 direct + 4 banked,
  bank_sel register internal to the design). Ceiling 1024 instructions at
  8-bit width / 512 at 16-bit. Bank-crossing costs an extra cycle --
  staying <=64 instructions (no banking) is the low-complexity default
  unless the protocol set needs more. Area/cell cost of this macro: not
  yet known, needs the flow-validation run below.

## Open questions

- **ISA encoding detail**: word width, exact opcode bit layout, operand
  addressing now that the op-class list above is fixed.
- **Pin mapping**: how `ui_in`/`uo_out`/`uio_*` map to instruction
  operands (direct bit-select vs indirect via a pin-index register).
  Needs open-drain support for I2C.
- **Timing**: target baud rates for UART/SPI/I2C baseline, and how
  `clock_hz` (info.yaml) + cycle_counter prescaler hit them.
- **Area budget**: `tiles: "6x4"` confirmed valid against
  `TinyTapeout/tt-support-tools` `tech/ihp-sg13g2/tile_sizes.yaml` (this
  template's own comment is stale, tops out at 8x2). Placed area
  ~0.92mm^2 (1289.28 x 710.64 um), a bit over the brief's naive
  ~0.7mm^2/24-tile estimate due to TT's margin on larger footprints.
  ~1000 logic cells/tile budget is itself unverified -- **next step is a
  flow-validation run** (trivial design through synth -> OpenROAD P&R ->
  GDS on CMOS5L) to get real cell-area numbers before locking ISA width /
  SRAM sizing. Not yet done. Prior OpenROAD experience was on Sky130, not
  CMOS5L -- doesn't carry over directly.

## Pipeline

TBD once ISA encoding is settled.

## Pin map

TBD -- will mirror `pinout:` section of `info.yaml` once decided. Needs a
register-based host interface to load/control the program, plus an
assembler translating protocol programs to ISA bytecode.
