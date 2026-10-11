# N-version model interface

The contract between `scripts/nversion.py` (the differential fuzzer) and
any independent ISA model. It fixes **only** the input/output formats and
how pin-stimulus arrays are indexed by cycle. It deliberately says
nothing about instruction semantics: those come from `docs/isa.md` and
`docs/architecture.md` alone.

## Invocation

    model_rs/target/release/model_rs < batch.jsonl > results.jsonl

One JSON object per input line, one JSON object per output line, in the
same order. Read until end of input, exit 0. A malformed input line is
answered with `{"id": ..., "error": "..."}` rather than a crash.

## Input line

```json
{"id": "s17", "image": [1024 ints 0-255], "ui": [ints 0-255], "uio": [ints 0-255], "max_cycles": 20000}
```

- **`image`**: the full 1024-byte memory as the boot loader would leave
  it, with the program at the start and zeros elsewhere. Fetch from it
  using the byte order and word layout `docs/isa.md` specifies. Data
  memory is the region `docs/isa.md` assigns to data. LOAD/STORE
  addresses map into that region exactly as the spec says.
- **`ui[t]` / `uio[t]`**: the *synchronized* `ui_in` / `uio_in` values
  the core sees at cycle `t` (after the input synchronizer, which is not
  your concern). An index past the end of an array reads 0.
- **Cycle numbering:**
  - Cycle 0 is the first cycle of instruction 0 (its first fetch
    cycle).
  - Cycles are counted continuously from there, using the per-
    instruction cycle costs the spec defines.
  - An instruction that reads the pins uses the value at the cycle(s)
    the spec says it samples. The first execute-phase cycle of an
    instruction is its first fetch cycle + 2. This line is the only
    timing fact given here; everything else comes from the spec.
- **`max_cycles`**: stop and report `"timeout": true` once the total
  cycle count exceeds this without a HALT.
- **Pads:** the model never computes pad values from its own pin
  drives. `uio` is given, so loopback is board-level and out of scope.

## Output line

```json
{"id": "s17",
 "commits": [{"pc": 0, "word": 18432, "cycles": 3, "state": {STATE}}, ...],
 "final": {STATE, "data_mem": [512 ints]},
 "halted": true, "timeout": false}
```

One `commits` entry per executed instruction, in order:
- **`pc`:** the address the instruction was fetched from, in the
  spec's PC units.
- **`word`:** the 16-bit instruction word.
- **`cycles`:** total cycles the instruction occupied.
- **`state`:** architectural state right after it commits.

`STATE` has these keys, with reset values and meanings as the spec
defines them:

| key | type |
|---|---|
| `pc` | int, next PC |
| `regs` | list of 4 ints |
| `flag` | 0/1 |
| `retaddr` | int |
| `return_valid` | 0/1 |
| `illegal_op_flag` | 0/1 |
| `call_ret_misuse_flag` | 0/1 |
| `halted` | 0/1 |
| `uo_out` | int |
| `pin_mode` | list of 8 ints, using the spec's 2-bit SET mode encoding. Entries 4/5/6 (pins with a fixed role) are not compared, because their direction isn't configurable and so their stored mode isn't observable; report anything |
| `pin_drv` | list of 8 ints (0/1), the value each pin drives / last driven |

`data_mem` (final only) is the 512-byte data region.

Where the spec is genuinely silent or ambiguous on something these
fields need, pick a reading, keep going, and **list it** in the crate's
`SPEC_NOTES.md`. A disagreement there is a finding about the spec, not
a defect in your model.
