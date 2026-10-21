<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

A programmable protocol emulator: a small, cycle-exact sequencer CPU
that bit-bangs serial protocols from firmware, rather than fixed UART,
SPI or I2C blocks. A new protocol is a new program, not new silicon.

- **Core.** 16-bit instructions, four 8-bit registers, 19 opcodes, one
  flag. Every instruction takes 3 cycles plus its own wait time, so
  pin timing is exactly predictable from the program. Full ISA:
  [isa.md](isa.md); microarchitecture: [architecture.md](architecture.md).
- **Pin-oriented instructions.** `SET` drives a pin and sets its drive
  mode (push-pull, open-drain or input, sticky per pin). `OUTB`/`INB`
  move one register bit to or from a pin. `WAIT` blocks until a pin
  reaches a level, with an optional timeout. `DELAY` waits an exact
  number of cycles, up to about 16.7 M (`mantissa << 5*exponent`).
- **Memory.** One IHP 1 KB SRAM macro: bytes 0-511 hold the program (256
  instructions), bytes 512-1023 are data (`LOAD`, `STORE`, `LOADX`).
- **Loading.** The SRAM is empty at power-up. After reset the chip is in
  LOAD mode and the host streams the program in a byte at a time over
  `ui_in`, with a two-wire handshake. A pulse on START begins execution
  at instruction 0. The chip loses its program when powered down or
  deselected, so the host reloads it each time.
- **Host interface.** Running firmware exchanges bytes with the host over
  `ui_in` (host to chip) and `uo_out` (chip to host), with a 4-phase
  handshake on HOST_GO / HOST_STATUS. Firmware raises HOST_ERROR on
  failures such as a timeout.

Programs are written in assembly and built with `firmware/isa/asm.py`.
Example firmware in `firmware/protocols/`, each verified in simulation
against an independent model of the other end:

| Program | What it does |
|---|---|
| `uart.asm` | UART 8N1 transmitter, baud set at assembly time (tested at 9600, 57600 and 115200) |
| `uart_rx.asm` | UART 8N1 receiver with stop-bit check; framing errors raise HOST_ERROR |
| `uart_selftest.asm` | loopback self-test (TX jumpered to RX), for bring-up |
| `spi.asm` | SPI controller, modes 0-3, full duplex, about 1 MHz by default |
| `i2c.asm` | I2C controller, 7-bit addressing, reads and writes, clock stretching, 100 or 400 kHz |
| `ws2812.asm` | WS2812 LED chain driver (up to 85 LEDs): a protocol the chip wasn't designed for, in 20 instructions |

## How to test

All signals are 3.3 V logic. The clock is 50 MHz.

**Load a program.**
1. Hold `rst_n` low, then release it. The chip starts in LOAD mode with
   HOST_STATUS (`uio[5]`) low and all protocol pins Hi-Z.
2. For each byte of the image (from `asm.py`), in order:
   1. put the byte on `ui_in`;
   2. raise HOST_GO (`uio[4]`) and wait for HOST_STATUS to go high
      (byte written; `uo_out` echoes the byte count);
   3. lower HOST_GO and wait for HOST_STATUS to go low.
3. With HOST_GO low, pulse START (`uio[6]`) high for a few clock cycles,
   then release it. Execution begins at instruction 0. After START
   falls, `uio[6]` becomes the HOST_ERROR output.

**Talk to the running program.**
- *Host to chip:* put a byte on `ui_in`, raise HOST_GO. Firmware reads it
  and raises HOST_STATUS. Lower HOST_GO; firmware lowers HOST_STATUS.
- *Chip to host:* raise HOST_GO to ask for a byte. Firmware puts it on
  `uo_out` and raises HOST_STATUS. Read `uo_out`, lower HOST_GO;
  firmware lowers HOST_STATUS.

**Quick check without a peripheral.** Load `uart_selftest.asm` with a
jumper from `uio[0]` (TX) to `uio[1]` (RX) and pulse START. Pass:
HOST_STATUS high and `uo_out` = `0xA5`. Fail: HOST_ERROR high and
`uo_out` = the byte that came back.

## External hardware

Protocol pins are `uio[0]`-`uio[3]` and `uio[7]`. Their role is set by
the firmware:

| Protocol | `uio[0]` | `uio[1]` | `uio[2]` | `uio[3]` |
|---|---|---|---|---|
| UART | TX | RX | | |
| SPI | MOSI | MISO | SCLK | CS |
| I2C | SDA | SCL | | |
| WS2812 | DIN | | | |

- **I2C needs pull-up resistors** on SDA and SCL (for example 4.7 kΩ to
  3.3 V at 100 kHz, 2.2 kΩ at 400 kHz): the chip drives them open-drain.
- UART, SPI and the host pins need no external parts at 3.3 V. Other
  voltage levels need a level shifter on a Pmod.
- **WS2812 LEDs powered from 5 V need a level shifter** on DIN (for
  example a 74AHCT125): their input high threshold is 0.7 x VDD = 3.5 V,
  above the chip's 3.3 V output.
- The host can be any microcontroller with 19 GPIOs: 8 to drive `ui_in`,
  8 to read `uo_out`, plus HOST_GO, HOST_STATUS and START/HOST_ERROR.
  The microcontroller on the Tiny Tapeout demo board can drive and read
  all of the design's pins.
