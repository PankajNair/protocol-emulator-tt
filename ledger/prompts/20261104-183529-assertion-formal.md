You are the assertion-formal agent for /Users/pankajnair/Desktop/Projects/protocol-emulator-tt. Your contract is formal/AGENT_CONTRACT.md: read it first and obey it exactly (write scope formal/ only; src/ and test/ are protected; plain immediate assertions in always @(posedge clk); bind at the bottom; every assert gets a companion cover; DEBUG_PORTS entries in formal/scripts/formal_common.py for any internal signal you observe).

Target: the new serial engine, src/io/ser_engine.v (module ser_engine). Its specification is docs/serial_engine.md -- derive every expected behaviour from that document, not from the RTL's own expressions. You may read src/ to find port and signal names.

Write formal/agent_ser_engine_props.v covering these hazard classes (choose the exact properties; each must fail on a plausible broken engine):

1. Pin ownership: drv_oe is 0 while the engine is not transmitting a packet (including after the EOP's idle bit); drv_oe[1] is never 1 unless `diff` is set; the engine never drives SE1 (both pins high) when diff is set; while it drives in diff mode, pin 1 is the complement of pin 0 except during the EOP's SE0.
2. Bit timing: while transmitting, the driven level only changes on bit boundaries that are exactly P clocks apart (P = the configured period at packet start; you may assume configuration is not written while a packet is in flight, which the doc allows to be undefined).
3. Bit stuffing on line levels: with stuff K != 0, a transmitted packet never holds one level for more than K consecutive bit times (except the EOP's SE0, which is 2 bit times and only in se0 mode).
4. EOP: in se0 mode a transmitted packet ends with exactly 2 bit times of SE0 then 1 of the idle level, then the pins are released.
5. Receive bookkeeping: rx_end is never set while the receive pin is not at the idle level or SE0 is on the pins (the end is signalled only once the line is idle again); rx_take consumes a waiting byte before a pending end (doc: "A byte is reported before an end that came after it"); overrun is set exactly when a byte completes while the previous one is still waiting and not being taken that cycle.
6. CRC: the CRC register follows the bit-serial definition in the doc for the configured width/polynomial, for received data bits of bytes at index >= crc_start (a ghost LFSR from the spec is fine).

Environment assumes allowed (state each one's justification in a comment): reset at step 0; configuration with P >= 2 and 1 <= S < P; configuration not written while a packet is in flight. Small P values are fine for BMC reach. Do not assume anything about pin_sync beyond being synchronized inputs (free).

Gate (run it yourself until it passes; the parent re-runs it independently):
  make vacuity TARGET_PROPS=formal/agent_ser_engine_props.v
  make prove TARGET_PROPS=formal/agent_ser_engine_props.v
  python3 scripts/assert_superset.py HEAD formal/agent_ser_engine_props.v
Use the project venv: `source .venv/bin/activate` first. k-induction must be PROVEN; strengthen with auxiliary invariants, never by weakening.

Before finishing, show each property is not vacuous in the strong sense: for at least 4 of the hazard classes, make a scratch copy of ser_engine.v (in /tmp, never in src/) with a plausible bug and show the property fails on it; report which.

Return as text (do not write report files): the properties, the assumes and why, the gate results, and the broken-copy results.
