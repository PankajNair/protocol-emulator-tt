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
- `protocols/uart_selftest.asm` -- UART loopback self-test: with TX
  jumpered to RX, sends 8 patterns and checks each comes back (data and
  stop bit) on the same bit grid. Pass: HOST_STATUS=1, uo_out=0xA5. Fail:
  HOST_ERROR=1, uo_out = byte received. Doubles as the silicon bring-up
  check. Verified on the board testbench (`test/test_uart_selftest.py`),
  including that a missing jumper and a stuck-high RX are reported as
  failures.
- `protocols/i2c.asm` -- I2C master, 7-bit addressing, read and write,
  host-driven through a lockstep protocol (address/len in, a status byte
  after the address and after each written byte, read data out; master
  ACKs every read byte but the last). SDA = pin 0, SCL = pin 1,
  open-drain. Handles clock stretching (`WAIT SCL,1,timeout`); a stuck
  SCL times out, reports status 3 and raises HOST_ERROR instead of
  hanging. 100 kHz by default, 400 kHz with `-D SCL_LOW=70 -D
  SCL_HIGH=55`. Verified on the board testbench against an independent
  I2C slave model (`test/test_i2c.py`).
- `protocols/spi.asm` -- SPI master, all four modes (`-D MODE=0..3`),
  MSB first, full duplex. MOSI/MISO/SCLK/CS = pins 0-3. Host sends a
  length, then per byte sends one and gets one back; CS stays low for the
  transaction. 1 MHz default; fastest SCLK half period is 12 cycles for
  CPHA 0 (~2.08 MHz), 15 for CPHA 1 (~1.67 MHz). The CPHA variants differ
  in instruction order, selected with the assembler's `.if`. Verified
  against an independent slave model for every mode (`test/test_spi.py`).
- `protocols/ws2812.asm` -- WS2812 / WS2812B LED driver: a frame of GRB
  bytes from the data region (up to 255 bytes, 85 LEDs) sent on pin 0,
  then a 300 us latch; the host can ask for a resend. Not a protocol the
  chip was designed around: a single-wire pulse-width code at 400/800 ns
  high times, written from the existing ISA. Every pulse is inside the
  datasheet's +-150 ns windows, including the byte-boundary bits, and
  cycle-exact to the budget in the file header. Verified by
  `test/test_ws2812.py` against an independent decoder written from the
  datasheet, over a 255-byte frame covering every bit pair.
- `protocols/spi_target.asm` -- SPI target (peripheral), mode 0: the chip
  follows an external controller's clock with `WAIT` on SCLK instead of
  pacing edges itself. MOSI/MISO/SCLK/CS = pins 0-3, MISO Hi-Z while CS is
  high. One register is the shift register, so each transaction replies
  0x96 and then echoes every byte one byte late; the last byte received
  is shown on `uo_out`. Survives clock pauses of any length while CS is
  low. Speed: exact up to a 14-cycle SCLK half period (1.79 MHz) in
  simulation, where the controller is phase-locked to the chip clock;
  rated 1.5 MHz for a real, asynchronous controller (one cycle of
  synchronizer margin per edge). Verified by `test/test_spi_target.py`
  against an independent mode-0 controller model, which also measures
  that limit (exact at 14, fails at 13).
- `protocols/onewire.asm` -- 1-Wire controller, standard speed (e.g. a
  DS18B20 temperature sensor): reset with presence detect, write byte,
  read byte, driven by the host in lockstep (command byte 1/2/3 over the
  usual handshake). DQ = pin 0, open-drain, external pull-up. Slot timing
  is Maxim AN126's recommended set; reads sample ~14 us into the slot.
  Not a protocol the chip was designed around. Verified by
  `test/test_onewire.py` against an independent device model that holds
  the spec's worst case (a 0 held only 15 us, presence at both ends of
  its window), reads a ROM code with a valid CRC8, and checks every
  controller pulse and slot against the spec. Late sampling, a long
  write-1 pulse, a short reset and an early presence sample each fail it.
- `protocols/stretch/` -- stretch goals: low-speed USB, 10Mbit Ethernet.

Order of work: ISA design → assembler → uart.asm (done: TX) → uart_rx.asm (done) →
i2c.asm (done) → spi.asm (done) → stretch goals.
