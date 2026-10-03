# Firmware

Protocol implementations as programs for the custom ISA (`src/cpu/isa_defs.v`),
not fixed hardware blocks. This is the "programmable, not fixed logic" part of
the brief.

- `isa/asm.py` -- assembler: `.asm` -> the byte image the host streams
  into the SRAM through the LOAD-mode boot sequence (docs/architecture.md
  Pipeline). Encoding reuses `test/isa_asm.py`, the encoder every test
  uses. Syntax and CLI in its module docstring; unit tests in
  `test/test_assembler.py`.
- `protocols/uart.asm` -- UART 8N1 transmitter: bytes from the host (IN
  handshake) out on pin 0, baud set by `.equ BAUD_CYCLES` (override with
  `-D`). Verified end to end on the RTL by `test/test_uart.py` at 115200,
  57600 and 9600 baud with an independent UART receiver model.
- `protocols/uart_rx.asm` -- UART 8N1 receiver: frames on pin 1, sampled
  at bit centres, stop bit checked (framing error raises HOST_ERROR),
  each byte handed to the host with the host-initiated OUT handshake. No
  receive buffer: on back-to-back frames the host must take each byte
  within ~0.4 bit. Verified by `test/test_uart.py` (115200, 9600, sender
  baud error +-2%, framing error + resync).
- `protocols/spi.asm`, `i2c.asm` -- not written yet.
- `protocols/stretch/` -- stretch goals: low-speed USB, 10Mbit Ethernet.

Order of work: ISA design → assembler → uart.asm (done: TX) → uart_rx.asm (done) →
spi.asm → i2c.asm → stretch goals.
