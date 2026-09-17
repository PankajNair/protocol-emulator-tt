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
- **Instruction memory**: SRAM-backed, not synthesized ROM. Macro:
  `RM_IHPSG13_1P_1024x8_c2_bm_bist` (the same macro
  `tt_um_urish_sram_test` wraps on the IHP shuttle) -- 1024x8 (1KB),
  single-port, byte-addressable, 10-bit address. At the locked 16-bit
  instruction width ([isa.md](isa.md)), the macro's full capacity gives
  **512 words** -- using the full addressable space rather than
  artificially constraining instruction count, since area headroom is
  basically free (see Area budget below).
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

## Open questions

- **Pin mapping**: how `ui_in`/`uo_out`/`uio_*` map to instruction
  operands (direct bit-select vs indirect via a pin-index register).
  Needs open-drain support for I2C.
- **Timing**: target baud rates for UART/SPI/I2C baseline, and how
  `clock_hz` (info.yaml) + cycle_counter prescaler hit them.
- **CRC/LFSR peripheral**: for CAN/Ethernet stretch protocols -- see
  [isa.md](isa.md) Open questions. Deferred until those protocols are
  actually tackled.

## Pipeline

Instruction formats are mostly locked ([isa.md](isa.md)), but
fetch/decode/execute cycle timing -- cycles per instruction, whether
branch/loop cost extra cycles -- is still TBD. This is a core-RTL
question, not an ISA-encoding one; comes up in stage 2 (core RTL).

## Pin map

TBD -- will mirror `pinout:` section of `info.yaml` once decided. Needs a
register-based host interface to load/control the program, plus an
assembler translating protocol programs to ISA bytecode.
