# Protocol timing budget

Status: locked. `clock_hz = 50,000,000` (`info.yaml`), 3 cycles/instruction
baseline ([architecture.md](architecture.md) Pipeline) -> 60ns/instruction,
**16.67 MIPS**. This table is what an adversarial design review flagged as
missing *before* the pipeline got locked -- filling it in surfaced a real
finding (10M Ethernet) that changes a downstream decision, see below.

| Protocol | Target rate | Cycles/bit | Instructions/bit | `DELAY` encoding needed |
|---|---|---|---|---|
| UART 9600 | 9600 baud | 5208 | 1736 | exponent=1, mantissa=163 (5216, 0.15% err) |
| UART 19200 | 19200 baud | 2604 | 868 | exponent=1, mantissa=81 (2592, 0.46% err) |
| UART 38400 | 38400 baud | 1302 | 434 | exponent=1, mantissa=41 (1312, 0.77% err -- worst case in this table) |
| UART 57600 | 57600 baud | 868 | 289 | exponent=1, mantissa=27 (864, 0.46% err) |
| UART 115200 | 115200 baud | 434 | 145 | exponent=0, mantissa=434 (exact) |
| I2C standard | 100kHz | 500 | 167 | exponent=0, mantissa=500 (exact) |
| I2C fast | 400kHz | 125 | 42 | exponent=0, mantissa=125 (exact) |
| SPI | firmware-paced | ~18-24 (est.) | ~6-8 (est.) | exponent=0 -- ceiling estimate, not measured against real firmware yet |
| USB LS | 1.5Mbit/s | 33 | 11 | exponent=0, mantissa=33 (exact) -- encoding isn't the problem here, timing margin is |
| 10M Ethernet (Manchester) | 10Mbit/s, 2 transitions/bit @ 100ns period | 2.5 between transitions | -- | **Infeasible regardless of encoding.** Minimum bit-bang loop is ~4 instructions/bit = 12 cycles; even 1 instruction/transition needs 3 cycles vs. the 2.5 available. Direct bit-banging tops out around **4.2Mbit/s NRZ** at this clock -- 10Mbit Manchester would need ~240MHz to reach with this pipeline, not realistic on this flow/process. |

## Consequences, in order of what they block

1. **10M Ethernet: deferred, last resort.** Pure bit-banging cannot
   reach it, full stop -- default assumption is it's dropped from the
   stretch-goal list; only reconsidered (hardware serializer
   peripheral) if time permits after everything else is done. Baseline
   capability doesn't depend on this either way.
2. **`DELAY`'s 511-cycle max was too short for every UART baud except
   115200 -- fixed.** Repurposed `DELAY`'s otherwise-unused `Rd` field
   as an exponent (`cycles = mantissa << (exponent*5)`), see
   [isa.md](isa.md)'s `DELAY` row. Checked against every baseline rate
   in this table -- worst case is 38400 baud at 0.77% error, all
   comfortably inside UART's ~2% tolerance. (First draft used a shift
   of 6 instead of 5; that put 57600 baud at 3.2% error, over
   tolerance -- caught by rechecking the numbers before locking, not by
   a second review pass.) No register cost, unlike the `LOOP`-wrapped
   nested-delay workaround this replaces.
3. **USB LS has no margin** -- RX in particular is over budget as
   currently estimated. Worth re-checking once real USB-LS firmware
   exists, not blocking anything today (it's a stretch goal, tackled
   later).
4. **SPI's ceiling is an estimate**, not measured against real firmware.
   Good enough for planning; firm up when SPI firmware actually gets
   written.

## Why 50MHz, not higher

TT's documented spec is "at least 50MHz" (RP2040-generated `clk`) --
the real demo board might sustain more, but 50MHz is the only number
that's actually documented and it's what `src/config.json`'s
`CLOCK_PERIOD=20` has already been assuming through both
flow-validation runs. Using anything higher here would be an
unverified guess; if a specific chip run turns out to tolerate more,
this budget only gets *more* comfortable, never less.
