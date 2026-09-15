# Architecture

Status: not designed yet. This doc tracks decisions as they're made.

## Open questions

- **ISA**: word width, opcode set, addressing mode for prog_rom. See
  `src/cpu/isa_defs.v` for current placeholder opcodes.
- **Pin mapping**: how `ui_in`/`uo_out`/`uio_*` map to instruction operands
  (direct bit-select vs indirect via a pin-index register).
- **Timing**: target baud rates for UART/SPI/I2C baseline, and how
  `clock_hz` (info.yaml) + cycle_counter prescaler hit them.
- **ROM vs SRAM** for firmware store (see `src/mem/prog_rom.v` TODO) --
  affects whether protocol can be changed post-tapeout or is baked in.
- **Area budget**: target tile allocation, see the `tiles:` TODO in
  `info.yaml` (6x4 per brief vs template's allowed values -- needs
  confirming). Budget ~1000 logic cells/tile per brief.

## Pipeline

TBD once ISA is settled.

## Pin map

TBD -- will mirror `pinout:` section of `info.yaml` once decided.
