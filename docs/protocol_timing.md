# Protocol timing budget

Status: not filled in yet -- depends on `clock_hz` choice in `info.yaml`
and cycle_counter design (`src/io/cycle_counter.v`).

| Protocol | Target rate | Cycles/bit @ clock_hz TBD | Notes |
|----------|------------|---------------------------|-------|
| UART     | TBD        |                            | baseline |
| SPI      | TBD        |                            | baseline |
| I2C      | TBD        |                            | baseline (100kHz std-mode is the usual floor) |
| USB LS   | 1.5 Mbit/s  |                            | stretch |
| 10M Eth  | 10 Mbit/s   |                            | stretch, likely tightest timing budget |
