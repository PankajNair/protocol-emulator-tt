# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Coverage-closure burst: wait_bins.tie (and .timeout,
flag_writer_bins.WAIT.1).

Real condition: docs/isa.md WAIT row -- if the pin condition becomes
true on exactly the cycle the timeout expires, condition-met wins
(flag=0). Guarded in RTL by core.v's flag mux (mutation #6 in
scripts/mutate.py makes timeout win instead).

Construct: the bounded-retry poll isa.md itself recommends ("compose
via LOOP for a coarser multi-attempt bound"), e.g. polling for an ACK
or SCL release with a short per-attempt timeout:

    LDI   rc, C                ; attempts, 1-8
  retry:
    WAIT  pin, level, T        ; exponent 0, T in 1..3
    BNE   done                 ; flag=0 -> condition met -> leave
    LOOP  retry, rc            ; timed out -> try again
  done:
    INB   rc, pin, bit0        ; sample the line after the handshake

The flag WAIT writes is consumed by BNE on the very next instruction, so
a tie resolved the wrong way (mutation #6) diverges in control flow
(pc) as well as in the flag compare.

Why short T: default stimulus redraws each protocol pin 50/50 every
cycle (io_stimulus.py), and consecutive WAIT samples read distinct
raw cycles, so they are independent. A tie needs samples k=0..T-1 all
non-matching and k=T matching: P(tie) = 2^-(T+1); P(timeout) is the
same 2^-(T+1); P(met_immediate) = 1/2. T=1: 25% each; T=2: 12.5%;
T=3: 6.25%. With T drawn from (1,1,2,3): 17.2% tie / 17.2% timeout per
WAIT. Any T >= 6 or exponent 1 (T >= 32) makes the tie essentially
unreachable under this stimulus.

Termination by construction: counter `rc` LDI-seeded 1-8, never written
inside the retry body (WAIT/BNE write no GPR); BNE target is strictly
forward and inside this burst; WAIT timeout nonzero; no CALL/RET/HALT.
The INB after the loop may write rc freely (counter is dead). Length:
5 words. Worst case cycles: 3 + 8*(3+3+3+3) + 3 = 102.
"""

from __future__ import annotations

import random
from typing import Callable

import isa_asm as asm

PROTOCOL_PIN_INDICES = (0, 1, 2, 3, 7)
TIMEOUT_CHOICES = (1, 1, 2, 3)  # exponent 0, see module header for the probabilities


def gen_cov_closure_wait_tie_burst(
    rng: random.Random, tag: str
) -> list[tuple[str | None, int | Callable[[dict], int]]]:
    pin = rng.choice(PROTOCOL_PIN_INDICES)
    level = rng.randint(0, 1)
    timeout = rng.choice(TIMEOUT_CHOICES)
    rc = rng.randint(0, 3)
    retry, done = f"{tag}_retry", f"{tag}_done"
    return [
        (None, asm.ldi(rc, rng.randint(1, 8))),
        (retry, asm.wait_(pin, level, mantissa=timeout, exponent=0)),
        (None, (lambda L, done=done: asm.bne(L[done]))),
        (None, (lambda L, retry=retry, rc=rc: asm.loop_(L[retry], rc))),
        (done, asm.inb(rc, pin, asm.BITSEL_BIT0)),
    ]
