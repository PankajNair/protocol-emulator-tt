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
| Assembler and example firmware | [firmware/](firmware/README.md) |
| Verification approach | [docs/verification.md](docs/verification.md) (full reference: [docs/VAL.md](docs/VAL.md)) |
| How AI tools were used | [docs/ai_usage.md](docs/ai_usage.md) |

## Status

- **RTL:** complete (`src/`): sequencer core, pin control, boot loader,
  IHP 1 KB SRAM macro.
- **Firmware:** UART TX and RX, UART self-test, SPI (modes 0-3), I2C
  (100/400 kHz), and a WS2812 LED driver, a protocol the chip wasn't
  designed for. Each is verified in simulation against an independent
  model of the other end.
- **Verification:** `make signoff`, 15 gates, `STABLE` on the nightly
  run. See [docs/verification.md](docs/verification.md).
- **Physical design:** hardened through the Tiny Tapeout flow on IHP
  SG13G2 (timing met at all corners, DRC/LVS clean, precheck passing).
  The port to SG13CMOS5L, the competition process, is in progress on
  branch `cmos5l-port`.

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
