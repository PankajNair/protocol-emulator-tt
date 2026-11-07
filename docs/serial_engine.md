# Serial engine

A small bit-serial engine beside the CPU, for protocols whose bit time is
too short for bit-banging (USB low-speed: 1.5 Mbit/s, 33 clocks a bit, 11
instructions). The engine does only the bit level -- timing, NRZI,
bit stuffing, a CRC, an end-of-packet marker -- and moves whole bytes.
Everything above that (what a byte means, when to answer, what to send)
stays firmware. It is generic: its settings cover USB low-speed, CAN-style
stuffing and CRCs, or any NRZ/NRZI line with a fixed bit time.

Four opcodes from the reserved range drive it: `SCFG`, `STX`, `SRX`,
`SRD` (21-24; 25-31 stay reserved).

## Pins

The engine uses protocol pins 0 and 1 only.

- **Receive** reads one pin (`rx_pin`: 0 or 1), synchronized by the same
  2-flop path as every other input. In `se0` mode it also reads both
  pins to see SE0 (both low).
- **Transmit** drives pin 0 with the line level. In `diff` mode it also
  drives pin 1 with the opposite level (USB D+/D-). While it transmits
  the engine owns the pins it drives: they are push-pull and follow the
  engine, whatever `SET`/`OUTB` last did to them. When the packet is
  over the pins go back to their sticky `SET` mode and value.

## Configuration: `SCFG Rs, k`

Writes Rs to configuration register k (0-8), or a command (15).
Registers reset to 0 (engine off). A write or command takes effect one
clock after `SCFG` commits (the engine registers it), before the next
instruction can run.

| k | Bits | Meaning |
|---|---|---|
| 0 | 7:0 | bit period P, low 8 bits (clocks per bit) |
| 1 | 3:0 | bit period P, high 4 bits (P = 2-4095) |
| 2 | 7:0 | receive sample point S, low 8 bits (clocks after an edge) |
| 3 | 3:0 | sample point S, high 4 bits (1 <= S < P) |
| 4 | 0 | `nrzi`: data 0 = level change, 1 = no change |
|   | 1 | `diff`: transmit drives pin 1 with the opposite of pin 0 |
|   | 2 | `se0`: SE0 (both pins low) ends a received packet; a transmitted packet ends with 2 bit times of SE0, then 1 of idle (needs `diff`) |
|   | 3 | `idle`: the line level between packets |
|   | 4 | `rx_pin`: receive from pin 0 or pin 1 |
|   | 5 | `crc_tx`: append the CRC to a transmitted packet |
|   | 6 | `crc_inv`: the appended CRC is inverted |
|   | 7 | `rx_en`: receive enabled |
| 5 | 3:0 | `stuff`: K, stuff after K equal line levels (0 = no stuffing; USB 7, CAN 5) |
|   | 7:4 | CRC width - 1 (width 1-16) |
| 6 | 7:0 | CRC polynomial, low byte |
| 7 | 7:0 | CRC polynomial, high byte |
| 8 | 3:0 | `crc_start`: first byte of a packet the CRC covers (0-15) |
|   | 4 | `crc_ones`: CRC starts at all ones (else 0) |
| 15 | 0 | command: clear the sticky status bits and a pending end of packet (a waiting byte stays) |
|    | 1 | command: abandon the packet being received: drop a waiting byte, skip the rest of the packet (see Receive), signal no end |

Writing configuration while a packet is in flight is allowed but gives
the in-flight packet whatever bits it hits. Firmware sets the engine up
once. If a command and the event it clears happen in the same clock, the
event wins.

`HALT` stops the CPU, not the engine: a packet being sent or received
carries on, and the engine keeps driving the pins until it ends.

## Bit stuffing, in line levels

Stuffing is defined on line levels, not data bits, which makes one rule
cover USB and CAN: after K bits at the same level, the next bit time is
a stuff bit at the opposite level. It carries no data and is not in the
CRC. A transmitted packet's run starts with its first bit: the idle
level before it doesn't count. USB's "a 0 after six 1s" is K = 7 in line levels (the 0 that last
changed the level, then six 1s with no change); CAN's "the complement
after five equal bits" is K = 5.

On receive, a stuff bit is dropped; if it is not a level change, that is
a stuff error and the packet ends.

## Transmit: `STX Rs`

Queues Rs for transmission, LSB first. The engine holds one byte
waiting plus the one shifting out, so firmware has a whole byte time to
queue the next. `STX` blocks while the waiting slot is full.

A packet starts when a byte is queued with the engine idle, and ends
when the engine runs out of bytes: then, in order, it sends

1. the CRC, if `crc_tx`: width bits, most significant first, each
   inverted if `crc_inv`, stuffed like data;
2. a stuff bit, if the last bits (data or CRC) leave one due;
3. in `se0` mode, 2 bit times of SE0 and 1 of idle.

Then it releases the pins. Every bit lasts exactly P clocks. Receive is
off from the first transmitted bit until the line is idle again. A byte
queued after the packet has ended starts a new packet.

The CRC covers the data bits of bytes `crc_start` onward of the packet,
in transmission order (USB: SYNC and PID excluded, `crc_start` = 2).

## Receive: `SRX Rd, timeout`

A packet starts when, with `rx_en` and the engine idle, the receive pin
leaves the idle level. Every edge restarts the bit timer, and the line
is sampled S clocks after the last edge, then every P clocks while no
edge comes. Each sample is: SE0 (in `se0` mode: the packet ends), a
stuff bit (dropped, or a stuff error), or a data bit (NRZI-decoded if
`nrzi`), shifted in LSB first, in that order of precedence. Every 8 data
bits make a byte; bits left over at the end are not delivered. "SE0"
means both pins low, and only ends a packet in `se0` mode.

After the end the engine waits for the line to be idle, then signals the
end:

- after an SE0, idle is the receive pin at the idle level, not SE0 (the
  J after USB's EOP);
- after a stuff error, or an abandon (command 15 bit 1, which signals no
  end), the rest of the packet is still on the line and the idle level
  comes and goes inside it, so: in `se0` mode the engine waits for the
  packet's SE0 and then the idle level; otherwise for K+1 bit times
  (1 if K = 0) at the idle level without a break, longer than any level
  inside a stuffed packet. The packet's SE0 seen during this wait is
  not a new end: status bit 2 stays as it was.

`SRX Rd, timeout` blocks until one of:

- a received byte is waiting: Rd = the byte, flag = 0;
- the end of a packet is signalled: Rd unchanged, flag = 1 (the end is
  consumed: the next `SRX` waits for a new byte);
- the timeout expires: Rd unchanged, flag = 1.

`timeout` is `mantissa << (exponent * 5)` clocks, mantissa 0-127 and
exponent 0-3 (7 and 2 bits of the immediate); 0 waits forever, like
`WAIT`. A byte is reported before an end that came after it. If a byte
completes while the previous one is still waiting, the new one replaces
it and the sticky `overrun` bit is set.

## Status and CRC: `SRD Rd, k`

| k | Rd |
|---|---|
| 0 | status: bit 0 byte waiting, 1 in a received packet (until its end, before the wait for idle), 2 last packet ended by SE0, 3 stuff error, 4 overrun, 5 transmit busy, 6 both pins low now (in any mode), 7 the receive pin now |
| 1 | CRC, bits 7:0 |
| 2 | CRC, bits 15:8 |
| other | 0 |

Bits 2-4 are sticky: set by the event, cleared by command 15 bit 0 or
when the next received packet starts (a transmitted packet leaves them).

After a received packet, the CRC register holds the CRC over every data
bit of bytes `crc_start` onward up to the end, the CRC field included.
For USB CRC16, where the EOP follows the CRC directly, that is the
residual 0x800D when the packet is good. For a CRC that is 0 over a good
packet with its CRC (CAN's CRC-15) it is 0 only if nothing is received
after the CRC field: in CAN the recessive bits that follow are data bits
until the stuff error that ends the packet, so firmware there reads the
CRC itself instead. A `crc_start` past the end of the packet leaves the
CRC at its starting value.

## Settings for USB low-speed (50 MHz clock)

| Setting | Value | Why |
|---|---|---|
| P | 33 | 1.5 Mbit/s is 33.3 clocks: 33 is 1.0% fast, inside low-speed's 1.5% |
| S | 16 | mid-bit |
| `nrzi`, `diff`, `se0`, `rx_en`, `crc_tx`, `crc_inv` | 1 | |
| `idle` | 0 | receive and level on D+ (pin 0); idle (J) is D+ low |
| `rx_pin` | 0 | |
| K | 7 | six 1s |
| CRC | width 16, poly 0x8005, start byte 2, all ones | CRC16 of data packets; tokens' CRC5 is checked by firmware |
