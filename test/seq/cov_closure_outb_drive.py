# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Coverage-closure burst: outb_mode_bins.pp / .od (OUTB actually driving
a configured pin), plus bitop_x_pin_bins OUTB.bit0/bit7 x protocol pin.

Real condition: the bit-bang TX idiom (docs/isa.md "Worked idioms"):
one-time `SET pin,mode,idle` to configure the pin's sticky drive mode,
then OUTB/DELAY/SHIFT/LOOP shifting a live register out one bit per
pass, then a `SET pin,leave,1` return-to-idle (stop bit / bus release).
OUTB has no mode field of its own and must obey SET's sticky mode --
push-pull drives the bit, open-drain drives 0 / releases on 1 (I2C SDA).
The default generator's OUTBs almost always land on pins still in reset
input mode (a legal no-op), so the drive path is barely exercised.

Bit-select matches shift direction exactly as isa.md requires: bit7
with SHIFT left (MSB-first: SPI/I2C), bit0 with SHIFT right (LSB-first:
UART). Each pass therefore drives a different, data-dependent bit, so
pin_drv toggles and an OUTB that reads the wrong bit, ignores the mode,
or drives in the wrong mode shows up in the per-commit pin_drv/pin_mode
compare, not just in the counter.

Termination by construction (random_gen.py header): LOOP counter `rc`
LDI-seeded 1-8 immediately before the body, never written in the body
(SHIFT writes only `rd`, rd != rc); the only backward edge is that
LOOP; no branch, CALL/RET, HALT, or WAIT. Length: 8 words. Worst-case
cycles: 9 + 8*(3+3+15+3+3) = 225.
"""

from __future__ import annotations

import random
from typing import Callable

import isa_asm as asm

PROTOCOL_PIN_INDICES = (0, 1, 2, 3, 7)
MAX_BIT_DELAY = 15  # exponent 0 -- a fast "baud" keeps per-burst cost small


def gen_cov_closure_outb_drive_burst(
    rng: random.Random, tag: str
) -> list[tuple[str | None, int | Callable[[dict], int]]]:
    pin = rng.choice(PROTOCOL_PIN_INDICES)
    mode = rng.choice([asm.SET_MODE_PUSH_PULL, asm.SET_MODE_OPEN_DRAIN])
    rd, rc = rng.sample(range(4), 2)  # data reg, loop counter reg (distinct)
    if rng.random() < 0.5:
        bitsel, direction = asm.BITSEL_BIT7, asm.SHIFT_LEFT  # MSB-first
    else:
        bitsel, direction = asm.BITSEL_BIT0, asm.SHIFT_RIGHT  # LSB-first
    nbits = rng.choice([8, 8, rng.randint(1, 8)])
    body = f"{tag}_txbit"
    return [
        (None, asm.ldi(rd, rng.randint(0, 255))),       # byte to send
        (None, asm.set_pin(mode, pin, 1)),              # configure, idle high / released
        (None, asm.ldi(rc, nbits)),                     # bit counter, 1-8
        (body, asm.outb(rd, pin, bitsel)),              # drive pin = rd[bit]
        (None, asm.delay(rng.randint(0, MAX_BIT_DELAY), 0)),  # bit period
        (None, asm.shift(rd, direction)),               # consume the bit just sent
        (None, (lambda L, body=body, rc=rc: asm.loop_(L[body], rc))),
        (None, asm.set_pin(asm.SET_MODE_LEAVE, pin, 1)),  # return to idle / release
    ]
