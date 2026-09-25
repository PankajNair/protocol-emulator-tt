# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""STIM_PROFILE=nested_flow -- LOOPs, forward branches and CALLs nested
inside LOOP bodies (test/stim/AGENT_CONTRACT.md requested profile #4).

Gap this closes: random_gen.py is flat-only by design (its header):
LOOP bodies and the shared subroutine are leaf-only, so
seq_coverage.loop_nest_bins depth2 / depth3+ / branch_in_loop /
call_in_loop are structurally 0.

Real-world condition modelled: bit-banging firmware's control shape --
a byte loop around a bit loop (around a per-bit settle/oversample
loop), a flag test (TESTBIT on the shifted data, CMP, WAIT-with-timeout
on an ACK/clock-stretch) followed by BEQ/BNE that either skips the rest
of the body ("continue" -> jump to the LOOP instruction), leaves the
loop early ("break" -> jump just past the LOOP), or skips a few
instructions; and a retry/byte loop that CALLs one shared send/settle
routine.

Termination BY CONSTRUCTION (same guarantee as random_gen.py):
  - Counter registers: a random permutation `perm` of R0-R3 per seed;
    a loop at nesting depth d (1..3) always uses perm[d-1], LDI-seeded
    with C in 1..8 immediately before its body. Every word inside a
    depth-d body is generated with forbid-set {perm[0..d-1]} (all
    counters live there), enforced on every GPR-writing opcode
    (LDI/SHIFT/LOAD/LOADX/IN/INB) by `_leaf_word`, which draws from
    random_gen._gen_leaf_word and rejects words whose destination is in
    the forbid set (deterministic, bounded retries, NOP fallback). At
    depth 3 only perm[3] is writable -- body writes all funnel into
    that one scratch register (read-only ops still use all four).
    Flag-setters in branch idioms (TESTBIT/CMP/WAIT) write no GPR.
  - The subroutine forbids writes to perm[0..2] (every possible
    counter), so a CALL from any depth is counter-safe. It contains no
    CALL/LOOP (only one pending return), only leaves + at most one
    forward branch to its own RET.
  - Every branch is strictly forward, and targets only (a) the start of
    a later sibling item in the same sequence, (b) the enclosing LOOP
    instruction ("continue": still decrements), or (c) the word just
    after the enclosing LOOP ("break"). Never into the middle of a
    nested body, so no LOOP can ever run without its LDI seed first.
  - Hence each loop's body runs <= C passes per entry and every word
    runs at most once per pass of its enclosing sequence.

Cycle bound: generation is budget-driven. A sequence gets a worst-case
cycle budget; each item is charged its static worst case (3/op, DELAY
3+n, WAIT 4+timeout, CALL 3+whole-subroutine, loop 3+C*(body+3)) and a
loop's body is generated against (loop_budget//C - 3). The total static
worst case for the whole program is <= CYCLE_BUDGET (asserted), well
under the default MAX_CYCLES=20000 (leaves room for boot).

Determinism: own random.Random(f"stim:nested_flow:{seed}").
"""

from __future__ import annotations

import random

import isa_asm as asm
import random_gen

CYCLE_BUDGET = 15000
SUB_BUDGET = 90           # worst-case cycles of one subroutine call incl. CALL/RET
MAX_DEPTH = 3
_WRITERS = (asm.OP_LDI, asm.OP_SHIFT, asm.OP_LOAD, asm.OP_LOADX, asm.OP_IN, asm.OP_INB)
_TIMEOUT_SHIFT = 5


def _op(w: int) -> int:
    return (w >> 11) & 0x1F


def word_cost(w: int) -> int:
    """Static worst-case cycles of one execution of word w."""
    op = _op(w)
    if op == asm.OP_DELAY:
        return 3 + ((w & 0x1FF) << (((w >> 9) & 3) * _TIMEOUT_SHIFT))
    if op == asm.OP_WAIT:
        return 4 + ((w & 0x1F) << (((w >> 9) & 3) * _TIMEOUT_SHIFT))
    return 3


def _clamp(caps: dict, extra: int, lo: int) -> dict:
    out = {e: min(m, extra >> (e * _TIMEOUT_SHIFT)) for e, m in caps.items()}
    out = {e: m for e, m in out.items() if m >= lo}
    return out or {0: lo}


def _leaf_word(rng, forbid: set, extra: int, dcaps: dict, wcaps: dict) -> int:
    """random_gen._gen_leaf_word with a SET of forbidden destination
    registers and a worst-case extra-cycle cap (rejection sampling)."""
    d = _clamp(dcaps, extra, 0)
    wt = _clamp(wcaps, max(extra - 1, 0), 1)
    for _ in range(64):
        w = random_gen._gen_leaf_word(rng, d, wt)
        if _op(w) in _WRITERS and ((w >> 9) & 3) in forbid:
            continue
        if word_cost(w) - 3 > extra:
            continue
        return w
    return asm.nop()


class _Gen:
    def __init__(self, seed, dcaps, wcaps):
        self.rng = random.Random(f"stim:nested_flow:{seed}")
        self.dcaps, self.wcaps = dcaps, wcaps
        self.perm = self.rng.sample(range(4), 4)
        self.uid = 0

    def label(self, p):
        self.uid += 1
        return f"{p}{self.uid}"

    def flag_setter(self, extra):
        r = self.rng
        k = r.choice(("testbit", "testbit", "cmp", "wait"))
        if k == "testbit":
            return asm.testbit(r.randint(0, 3), r.choice((0, 7, r.randint(0, 7))))
        if k == "cmp":
            return asm.cmp_(r.randint(0, 3), r.randint(0, 3))
        cap = max(1, min(31, extra - 1))
        return asm.wait_(r.choice(random_gen.PROTOCOL_PIN_INDICES), r.randint(0, 1),
                         mantissa=r.randint(1, cap), exponent=0)

    def seq(self, depth, budget, words_left, n_max, has_sub):
        """Items for one sequence at nesting `depth` (0 = top level).
        Returns (items, worst_cost_one_pass, words)."""
        r = self.rng
        forbid = set(self.perm[:depth])
        items, cost, words = [], 0, 0
        loops_here = 0
        while len(items) < n_max and budget - cost >= 3 and words_left - words >= 1:
            left, wl = budget - cost, words_left - words
            x = r.random()
            if depth == 0:
                p_loop, p_br, p_call = 0.18, 0.08, 0.04
            else:
                p_loop = 0.45 if depth < MAX_DEPTH and loops_here == 0 else 0.0
                p_br, p_call = 0.22, 0.12
            if depth < MAX_DEPTH and x < p_loop and left >= 30 and wl >= 4:
                lb = left if depth else int(left * r.uniform(0.3, 0.7))
                if depth:
                    lb = int(left * r.uniform(0.6, 0.95))
                c = r.randint(1, 8) if r.random() < 0.6 else 8
                while c > 1 and lb // c - 3 < 9:
                    c -= 1
                per_pass = lb // c - 6
                body, bcost, bw = self.seq(depth + 1, per_pass, wl - 2,
                                           r.randint(2, 6), has_sub)
                if not body:
                    body, bcost, bw = [{"kind": "leaf", "w": asm.nop()}], 3, 1
                icost = 3 + c * (bcost + 3)
                items.append({"kind": "loop", "reg": self.perm[depth], "count": c,
                              "body": body})
                cost += icost
                words += bw + 2
                loops_here += 1
            elif x < p_loop + p_br and left >= 10 and wl >= 2:
                fs = self.flag_setter(min(left - 6, 40))
                items.append({"kind": "branch", "fs": fs,
                              "cond": r.choice((asm.COND_BEQ, asm.COND_BNE, asm.COND_BEQ,
                                                asm.COND_BNE, asm.COND_JMP))})
                cost += word_cost(fs) + 3
                words += 2
            elif has_sub and x < p_loop + p_br + p_call and left >= SUB_BUDGET:
                items.append({"kind": "call"})
                cost += SUB_BUDGET
                words += 1
            else:
                extra = min(left - 3, 480 if depth == 0 else (left - 3) // 3)
                w = _leaf_word(r, forbid, max(extra, 0), self.dcaps, self.wcaps)
                items.append({"kind": "leaf", "w": w})
                cost += word_cost(w)
                words += 1
        return items, cost, words

    def emit(self, items, out, pending, cont, brk):
        starts = [self.label("s") for _ in items]
        for i, it in enumerate(items):
            pending.append(starts[i])
            k = it["kind"]
            if k == "leaf":
                out.append((pending[:], it["w"])); pending.clear()
            elif k == "call":
                out.append((pending[:], lambda L: asm.call(L["sub"]))); pending.clear()
            elif k == "branch":
                out.append((pending[:], it["fs"])); pending.clear()
                choices = starts[i + 2:] + [cont] * 2 + ([brk] * 2 if brk else [])
                tgt = self.rng.choice(choices)
                cond = it["cond"]
                out.append(([], lambda L, t=tgt, c=cond: asm.branch(c, L[t])))
            else:  # loop
                reg, c = it["reg"], it["count"]
                out.append((pending[:], asm.ldi(reg, c))); pending.clear()
                body_l, lp_l, brk_l = self.label("b"), self.label("lp"), self.label("x")
                pending.append(body_l)
                self.emit(it["body"], out, pending, lp_l, brk_l)
                pending.append(lp_l)
                out.append((pending[:], lambda L, b=body_l, rg=reg: asm.loop_(L[b], rg)))
                pending.clear()
                pending.append(brk_l)


def _sub(g: _Gen) -> list:
    """Counter-safe leaf subroutine: writes only perm[3]; <= SUB_BUDGET."""
    r = g.rng
    forbid = set(g.perm[:3])
    body, cost = [], 6  # CALL + RET
    n = r.randint(2, 6)
    for _ in range(n):
        left = SUB_BUDGET - cost - 3 * (n - len(body))
        body.append(_leaf_word(r, forbid, max(0, min(left - 3, 24)), g.dcaps, g.wcaps))
        cost += word_cost(body[-1])
    entries = [(["sub"], body[0])] + [([], w) for w in body[1:]]
    if cost + 8 <= SUB_BUDGET and r.random() < 0.5:
        fs = g.flag_setter(1)
        pos = r.randint(1, len(entries))
        cond = r.choice((asm.COND_BEQ, asm.COND_BNE))
        entries[pos:pos] = [([], fs), ([], lambda L, c=cond: asm.branch(c, L["sub_ret"]))]
        cost += 3 + word_cost(fs)
    entries.append((["sub_ret"], asm.ret()))
    assert cost <= SUB_BUDGET
    return entries


def generate_program(seed: int, max_delay_mantissa: dict | None = None,
                     max_wait_mantissa: dict | None = None) -> list[int]:
    dcaps = dict(max_delay_mantissa or random_gen.DEFAULT_MAX_DELAY_MANTISSA)
    wcaps = dict(max_wait_mantissa or random_gen.DEFAULT_MAX_WAIT_MANTISSA)
    g = _Gen(seed, dcaps, wcaps)
    sub = _sub(g)
    main_budget = CYCLE_BUDGET - 3  # HALT
    items, cost, _ = g.seq(0, main_budget, random_gen.PROGRAM_WORD_LIMIT - len(sub) - 1,
                           g.rng.randint(20, 45), True)
    if not any(it["kind"] == "call" for it in _walk(items)):
        sub = []
    out: list = []
    pending: list = []
    g.emit(items, out, pending, "halt", None)
    pending.append("halt")
    out.append((pending[:], asm.halt()))
    out += sub
    labels = {}
    for i, (ls, _w) in enumerate(out):
        for lab in ls:
            labels[lab] = i
    words = [w(labels) if callable(w) else w for _ls, w in out]
    assert len(words) <= random_gen.PROGRAM_WORD_LIMIT, (seed, len(words))
    assert cost + 3 <= CYCLE_BUDGET, (seed, cost)
    return words


def _walk(items):
    for it in items:
        yield it
        if it["kind"] == "loop":
            yield from _walk(it["body"])
