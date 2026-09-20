# Protocol timing budget

Status: locked. `clock_hz = 50,000,000` (`info.yaml`), 3 cycles/instruction
baseline ([architecture.md](architecture.md) Pipeline) -> 60ns/instruction,
**16.67 MIPS**. This table is what an adversarial design review flagged as
missing *before* the pipeline got locked -- filling it in surfaced a real
finding (10M Ethernet) that changes a downstream decision, see below.

**Master/host-role only.** Every row below assumes this design generates
and self-paces the clock/timing (`DELAY`-driven). Slave/device emulation
-- reacting within an externally-driven clock edge instead -- is
deferred scope ([isa.md](isa.md) Known limitations); no rows here for
it yet, not an oversight.

**UART rows account for real loop overhead, not just `DELAY`'s own
count.** The locked TX worked idiom ([isa.md](isa.md) "Worked idioms")
is `OUTB`(3) + `DELAY`(3+N) + `SHIFT`(3) + `LOOP`(3) = **12 fixed
cycles + N** per bit, not N alone -- an earlier draft of this table
computed `DELAY`'s mantissa against the *whole* bit period, silently
absorbing 12 cycles of error into every row, which is what made
115200's "exact" actually 2.7% slow (446 actual cycles against a
434-cycle target) once the omitted overhead is counted. Caught by a
later audit; `N` below is `bit_period - 12`.

| Protocol | Target rate | Cycles/bit (period) | Instructions/bit | `DELAY` encoding (N = period - 12 overhead) |
|---|---|---|---|---|
| UART 9600 | 9600 baud | 5208 | 1736 | N=5196: exponent=1, mantissa=162 (5184, total 5196, 0.23% err) |
| UART 19200 | 19200 baud | 2604 | 868 | N=2592: exponent=1, mantissa=81 (2592 exact, total 2604, 0% err) |
| UART 38400 | 38400 baud | 1302 | 434 | N=1290: exponent=1, mantissa=40 (1280, total 1292, 0.77% err) |
| UART 57600 | 57600 baud | 868 | 289 | N=856: exponent=1, mantissa=27 (864, total 876, 0.92% err -- worst case in this table) |
| UART 115200 | 115200 baud | 434 | 145 | N=422: exponent=0, mantissa=422 (exact, total 434, 0% err) |
| I2C standard | 100kHz | 500 | 167 | **Approximate, not tied to a locked idiom.** 500 is the *full* SCL period; I2C's actual bit-loop (`SET`/`WAIT` for clock-stretch/two `DELAY` half-periods/`SHIFT`/`LOOP`, sketched during pin-mapping but never locked as a canonical worked idiom the way UART's TX loop was) has its own overhead and asymmetric high/low timing this row doesn't yet account for. Don't treat `mantissa=500` as precise the way the UART rows above are -- revisit once I2C firmware is actually written. |
| I2C fast | 400kHz | 125 | 42 | Same caveat as I2C standard, more acutely -- 125 total cycles leaves very little room for whatever the real loop overhead turns out to be. Revisit with the same idiom-locking pass. |
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
   in this table, including the 12-cycle loop overhead a later audit
   caught being silently dropped from the first pass -- worst case is
   57600 baud at 0.92% error, all comfortably inside UART's ~2%
   tolerance. (First draft used a shift of 6 instead of 5; that put
   57600 baud at 3.2% error even before the overhead fix, over
   tolerance -- caught by rechecking the numbers before locking, not by
   a review pass.) No register cost, unlike the `LOOP`-wrapped
   nested-delay workaround this replaces.
3. **I2C's rows are approximate, not precise like UART's.** UART's
   mantissas are checked against a locked worked idiom
   ([isa.md](isa.md) "Worked idioms"); I2C's bit-loop was only ever
   sketched during pin-mapping discussion, never locked the same way,
   so its real per-bit overhead (clock-stretch `WAIT`, asymmetric
   high/low `DELAY`s) isn't accounted for yet. Revisit when I2C
   firmware actually gets written and its idiom gets locked -- same
   "don't overclaim precision that doesn't exist yet" standard the
   `DELAY` fix above was held to.
4. **USB LS has no margin** -- RX in particular is over budget as
   currently estimated. Worth re-checking once real USB-LS firmware
   exists, not blocking anything today (it's a stretch goal, tackled
   later). Gets slightly worse, not newly broken: every protocol-pin
   input now goes through the same 2-flop synchronizer as host inputs
   ([architecture.md](architecture.md), fixed a gap where only host
   inputs were specified as synchronized), adding +2 cycles to `WAIT`'s
   reaction time that this row's estimate doesn't yet reflect.
5. **SPI's ceiling is an estimate**, not measured against real firmware.
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
