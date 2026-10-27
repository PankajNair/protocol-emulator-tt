![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg)

# Protocol Emulator ASIC

Entry for the [Jane Street protocol emulator ASIC competition](https://blog.janestreet.com/protocol-emulator-asic-competition/),
built on the [Tiny Tapeout](https://tinytapeout.com) flow for the IHP
SG13CMOS5L shuttle (6x4 tiles).

A small, cycle-exact sequencer CPU that bit-bangs serial protocols from
firmware. UART, SPI and I2C are programs, not hardware blocks: a new
protocol is a new program, loaded by the host after fabrication.

| | |
|---|---|
| Datasheet (pins, loading, host protocol, external parts) | [docs/info.md](docs/info.md) |
| Instruction set | [docs/isa.md](docs/isa.md) |
| Architecture | [docs/architecture.md](docs/architecture.md) |
| Protocol timing budget | [docs/protocol_timing.md](docs/protocol_timing.md) |
| Assembler, example firmware, demo-board host library | [firmware/](firmware/README.md) |
| Verification approach | [docs/verification.md](docs/verification.md) (full reference: [docs/VAL.md](docs/VAL.md)) |
| How AI tools were used | [docs/ai_usage.md](docs/ai_usage.md) |

## Status

- **RTL:** complete (`src/`): sequencer core, pin control, boot loader,
  IHP 1 KB SRAM macro.
- **Firmware:** UART TX and RX, UART self-test, SPI (modes 0-3), I2C
  (100/400 kHz), SPI target (the chip as the peripheral, following an
  external clock), and five protocols the chip wasn't designed for: a
  CAN 2.0A transmitter and receiver (stuffing, CRC-15, arbitration,
  clock resync), a WS2812 LED
  driver, a 1-Wire controller, an NEC IR remote transmitter and a PS/2
  keyboard receiver. Each is verified in simulation against an independent
  model of the other end.
- **Verification:** `make signoff`, 16 gates, `STABLE` on the nightly
  run. See [docs/verification.md](docs/verification.md).
- **Physical design:** hardened through the Tiny Tapeout flow on IHP
  SG13CMOS5L, the competition process: timing met at 50 MHz in all
  corners with no slew or load violations, LVS clean, precheck and
  gate-level test passing. The SRAM's Metal4 supply columns are bridged
  to the Metal4-only tile grid by a checked PDN script
  ([docs/VAL.md](docs/VAL.md) section 11).

## Quick start

```sh
python3.13 -m venv .venv && .venv/bin/pip install -r test/requirements.txt
source .venv/bin/activate
make -C test                 # directed tests (Icarus Verilog)
make signoff                 # every verification gate (~75 min)
python3 firmware/isa/asm.py firmware/protocols/uart.asm -o uart.hex   # boot image, one hex byte per line
```

Formal and RTL-coverage gates also need yosys, yosys-smtbmc, z3 and
Verilator ([OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build)).

## About Tiny Tapeout

Tiny Tapeout is an educational project that makes it easier and cheaper
than ever to get digital and analog designs manufactured on a real chip.
See https://tinytapeout.com.
