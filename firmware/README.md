# Firmware

Protocol implementations as programs for the custom ISA (`src/cpu/isa_defs.v`),
not fixed hardware blocks. This is the "programmable, not fixed logic" part of
the brief.

- `isa/` — assembler for the custom ISA (turns `.asm` into a hex image for
  `src/mem/prog_rom.v` via `$readmemh`). Not started — depends on ISA being
  finalized first.
- `protocols/` — baseline protocol programs: `uart.asm`, `spi.asm`, `i2c.asm`.
- `protocols/stretch/` — stretch-goal protocols: low-speed USB, 10Mbit
  Ethernet.
- `tests/` — firmware-level simulation (assembler output run against a
  behavioral/ISA-level model, before touching RTL sim).

Order of work: ISA design → assembler → uart.asm (simplest, good ISA
smoke test) → spi.asm → i2c.asm → stretch goals.
