# Firmware

## Host side

`host/pe_host.py` -- the host library: load a program, start it, talk to
it. Runs on the Tiny Tapeout demo board in MicroPython (TT SDK):

```python
import pe_host
h = pe_host.Host(pe_host.TTDemoBoardIO())   # selects the project, 50 MHz clock
h.load_hex("uart.hex")                      # asm.py -o uart.hex; copy to the board
h.send(0x41)                                # byte to the firmware
b = h.recv()                                # byte from the firmware
```

Reload after any reset, power-down or deselect: the program lives in
SRAM. The boot and handshake protocols are written once, as generators
of pin operations; `test/test_host_loader.py` runs the same generators
against the RTL (loading, talking, reloading, a 522-byte image with a
data region, and a timeout instead of a hang when the firmware doesn't
answer), and catches a wrong boot echo (mutation #19) and a dead
HOST_STATUS (#15). Only the thin SDK adapter, `TTDemoBoardIO`, is
untested until a chip exists.

## Programs

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
- `protocols/nec_ir.asm` -- NEC infrared remote transmitter: the host
  gives an address and a command, the firmware sends one NEC frame on
  pin 0 on a 38.11 kHz, 33% duty carrier generated in firmware (drive an
  IR LED through a transistor). The complement bytes go through a copy
  of the send routine with the branch flipped (there is no NOT).
  Verified by `test/test_nec_ir.py` against an independent receiver
  model (demodulates the carrier, decodes NEC with typical tolerances,
  checks every carrier period within 38 kHz +-2%): leader 8983/4503 us.
  A frame takes a minute to simulate, so this runs as its own sign-off
  gate (`make -C test slow`), not per mutant.
- `protocols/ps2_host.asm` -- PS/2 host, receive side (keyboard or
  mouse): follows the device's clock, checks start/parity/stop (parity
  counted with LOOP as a decrement, no XOR), and pulls CLK low after each
  frame -- PS/2's inhibit -- until the host has taken the byte, so the
  device buffers instead of the chip. DATA = pin 0, CLK = pin 1,
  open-drain. Verified by `test/test_ps2_host.py` against an independent
  keyboard model that honours the inhibit: back-to-back scan codes, and
  bad parity / bad stop frames dropped with HOST_ERROR. The test found a
  real firmware bug: after releasing CLK the firmware must wait to see it
  high, or its own low (still in the input synchronizer) reads as the
  next start bit.
- `protocols/can_tx.asm` -- CAN 2.0A transmitter, 50 kbit/s: standard
  data frame (11-bit ID, 0-8 data bytes) with bit stuffing, CRC-15, ACK
  check, arbitration and bit-error detection; status back to the host (0
  sent, 1 no ACK, 2 lost arbitration, 3 bit error). TX = pin 0 to a CAN
  transceiver's TXD, RX = pin 1 from its RXD. Needs XOR/ADD (CRC-15 across
  two bytes, counters). Every per-bit branch is padded so each bit is
  exactly 1000 cycles; the work per bit (381 cycles) is why it runs at 50
  rather than 125 kbit/s with a 75% sample point. 252 of the 256 program
  words. Verified by `test/test_can.py` on a wired-AND bus with an
  independent receiver node (destuffing, its own CRC-15, ACK only for a
  good frame), a competing transmitter and an error injector: frames of
  0-8 bytes incl. stuffing-heavy ones, no ACK, lost arbitration, bit
  error, and every chip edge exactly on the bit grid. An unbalanced pad
  (3 cycles), a wrong polynomial, stuffing after 6, and RTL mutants #27/#28
  each fail it.
- `protocols/stretch/` -- stretch goals: low-speed USB, 10Mbit Ethernet.

Order of work: ISA design → assembler → uart.asm (done: TX) → uart_rx.asm (done) →
i2c.asm (done) → spi.asm (done) → stretch goals.
