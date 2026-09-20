# Firmware

Protocol implementations as programs for the custom ISA (`src/cpu/isa_defs.v`),
not fixed hardware blocks. This is the "programmable, not fixed logic" part of
the brief.

- `isa/` — assembler for the custom ISA (turns `.asm` into a raw byte
  image the host streams into `src/mem/mem.v`'s SRAM at runtime via the
  `LOAD`-mode boot sequence, `docs/architecture.md` Pipeline section —
  not a `$readmemh`-preloaded synthesized ROM, that framing predates
  the SRAM/host-boot-stream design and is stale). ISA is locked
  (`src/cpu/isa_defs.v`, `docs/isa.md`) — not started yet regardless.
- `protocols/` — baseline protocol programs: `uart.asm`, `spi.asm`, `i2c.asm`.
- `protocols/stretch/` — stretch-goal protocols: low-speed USB, 10Mbit
  Ethernet.
- `tests/` — firmware-level simulation (assembler output run against a
  behavioral/ISA-level model, before touching RTL sim).

Order of work: ISA design → assembler → uart.asm (simplest, good ISA
smoke test) → spi.asm → i2c.asm → stretch goals.
