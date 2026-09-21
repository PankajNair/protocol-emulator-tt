# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Cross-checks golden_model.py against ALREADY RTL-proven ground truth
-- plain pytest, no simulator involved -- before it's ever trusted as a
live comparison gate in test_random.py.

Why this file exists at all, not just "trust the model because it looks
right": this project's own formal-verification harness (formal/
agent_cycle_counter_timing_props.v) once had a checker that PASSED
against a genuinely buggy DUT, because its ghost model had been
independently hand-derived by reading the DUT's own (buggy) update
logic rather than the documented spec -- it had quietly learned to
expect the bug instead of catching it. This file's job is to make sure
golden_model.py can't make that same mistake silently: every check here
is derived from either docs/isa.md's own prose (the DELAY reach
numbers) or from test.py's directed tests (already independently
verified against real RTL on both icarus and verilator), never by
reading golden_model.py's own source and confirming it agrees with
itself.

Run standalone: `pytest test/test_golden_model_selfcheck.py` (no `make`,
no simulator).
"""

import isa_asm as asm
from golden_model import SequencerState, step


def test_delay_reach_numbers_match_docs():
    """docs/isa.md's DELAY row: exponent=0 -> 0-511, exponent=1 -> up to
    16352, exponent=2 -> up to ~523264, exponent=3 -> up to ~16744448.
    Recomputed here from the raw `mantissa << (exponent*5)` formula
    (mantissa max = 511, the field's full 9-bit range), not by calling
    golden_model's own DELAY branch -- an independent re-derivation."""
    TIMEOUT_SHIFT = 5
    max_mantissa = 511
    expected = {0: 511, 1: 16352, 2: 523264, 3: 16744448}
    for exponent, expected_max in expected.items():
        computed_max = max_mantissa << (exponent * TIMEOUT_SHIFT)
        assert computed_max == expected_max, (
            f"exponent={exponent}: computed max {computed_max}, "
            f"docs/isa.md says {expected_max}"
        )


def test_delay_zero_mantissa_costs_exactly_3_cycles():
    """The specific off-by-one this session found and fixed in
    cycle_counter.v (commit 1121ccc) was that mantissa>=1 cost one
    cycle too many -- mantissa==0 was the one case that was ALWAYS
    correct (3 cycles flat). Confirms the golden model's formula agrees
    at this boundary before checking the mantissa>=1 cases below."""
    state = SequencerState.reset()
    _new_state, cycles = step(state, asm.delay(0, 0))
    assert cycles == 3


def test_delay_timing_matches_directed_rtl_test():
    """Ports test.py's test_delay_timing exactly: mantissa=6 vs
    mantissa=0 must differ by exactly 6 cycles -- that directed test
    was independently proven against real RTL (both icarus and
    verilator) after the cycle_counter.v fix. If this fails, the golden
    model's DELAY formula disagrees with proven-correct hardware."""
    state = SequencerState.reset()
    _s0, cycles0 = step(state, asm.delay(0, 0))
    _s6, cycles6 = step(state, asm.delay(6, 0))
    assert cycles6 - cycles0 == 6


def test_call_ret_round_trip_matches_directed_rtl_test():
    """Ports test.py's test_call_ret: CALL sub; OUT R0; HALT / sub: LDI
    R0,0x42; RET -- program-counter round-trips correctly and R0 ends
    at 0x42, matching the already-RTL-proven directed test."""
    prog = asm.assemble([
        (None, lambda L: asm.call(L["sub"])),
        (None, lambda L: asm.out(0)),
        (None, lambda L: asm.halt()),
        ("sub", lambda L: asm.ldi(0, 0x42)),
        (None, lambda L: asm.ret()),
    ])

    state = SequencerState.reset()
    for _ in range(20):
        if state.halted:
            break
        state, _cycles = step(state, prog[state.pc])

    assert state.halted
    assert state.uo_out == 0x42


def test_loop_iterates_exact_count_matches_directed_rtl_test():
    """Ports test.py's test_loop: LOOP with count=3 must take exactly
    12 more cycles (2 extra passes x 6 cycles/pass: LDI+LOOP) than
    count=1, matching the already-RTL-proven directed test's
    differential measurement."""

    def build(count):
        return asm.assemble([
            (None, lambda L: asm.ldi(0, count)),
            ("loop", lambda L: asm.ldi(1, 0xAA)),
            (None, lambda L: asm.loop_(L["loop"], 0)),
            (None, lambda L: asm.out(1)),
            (None, lambda L: asm.halt()),
        ])

    def run_and_count_cycles(prog):
        state = SequencerState.reset()
        total_cycles = 0
        for _ in range(200):
            if state.halted:
                break
            state, cycles = step(state, prog[state.pc])
            total_cycles += cycles
        assert state.halted
        assert state.uo_out == 0xAA
        return total_cycles

    cycles_1 = run_and_count_cycles(build(1))
    cycles_3 = run_and_count_cycles(build(3))
    assert cycles_3 - cycles_1 == 12


def test_set_and_outb_pin_drive_matches_directed_rtl_test():
    """Ports test.py's test_set_outb_pin_drive: SET push-pull + OUTB
    bit0 of 0x81 (=1) onto pin_index=0."""
    state = SequencerState.reset()
    state, _ = step(state, asm.ldi(0, 0x81))
    state, _ = step(state, asm.set_pin(asm.SET_MODE_PUSH_PULL, pin_index=0, value=0))
    state, _ = step(state, asm.outb(0, pin_index=0, bitsel=asm.BITSEL_BIT0))
    assert state.pin_mode[0] == asm.SET_MODE_PUSH_PULL
    assert state.pin_drv[0] == 1


def test_open_drain_release_and_drive_low_match_directed_rtl_tests():
    """Ports test.py's test_open_drain_release / test_open_drain_drive_low."""
    state = SequencerState.reset()
    state, _ = step(state, asm.set_pin(asm.SET_MODE_OPEN_DRAIN, pin_index=1, value=1))
    assert state.pin_mode[1] == asm.SET_MODE_OPEN_DRAIN
    assert state.pin_drv[1] == 1

    state2 = SequencerState.reset()
    state2, _ = step(state2, asm.set_pin(asm.SET_MODE_OPEN_DRAIN, pin_index=1, value=0))
    assert state2.pin_drv[1] == 0


def test_store_load_hazard_free_matches_directed_rtl_test():
    """Ports test.py's test_store_load: STORE then an immediately
    adjacent LOAD from the same address sees the fresh value."""
    prog = [asm.ldi(0, 0x77), asm.store(0, 0), asm.load(1, 0), asm.out(1), asm.halt()]
    state = SequencerState.reset()
    for _ in range(20):
        if state.halted:
            break
        state, _ = step(state, prog[state.pc])
    assert state.uo_out == 0x77


def test_loadx_matches_directed_rtl_test():
    """Ports test.py's test_loadx."""
    prog = [asm.ldi(0, 5), asm.store(0, 3), asm.ldi(2, 3), asm.loadx(1, 2), asm.out(1), asm.halt()]
    state = SequencerState.reset()
    for _ in range(20):
        if state.halted:
            break
        state, _ = step(state, prog[state.pc])
    assert state.uo_out == 5


def test_shift_left_then_right_matches_directed_rtl_test():
    """Ports test.py's test_shift."""
    state = SequencerState.reset()
    state, _ = step(state, asm.ldi(0, 0b00000011))
    state, _ = step(state, asm.shift(0, asm.SHIFT_LEFT))
    assert state.regs[0] == 6
    state, _ = step(state, asm.ldi(1, 0b10000000))
    state, _ = step(state, asm.shift(1, asm.SHIFT_RIGHT))
    assert state.regs[1] == 64


def test_testbit_matches_directed_rtl_test():
    """Ports test.py's test_testbit."""
    state = SequencerState.reset()
    state, _ = step(state, asm.ldi(0, 0b00000100))
    state, _ = step(state, asm.testbit(0, 2))
    assert state.flag is True


def test_wait_in_inb_raise_without_io_read():
    """The deliberate extension seam: these three opcodes must fail
    loudly, not silently mis-model, until a future io_read callback is
    implemented."""
    import pytest

    state = SequencerState.reset()
    with pytest.raises(NotImplementedError):
        step(state, asm.wait_(0, 1))
    with pytest.raises(NotImplementedError):
        step(state, asm.in_(0))
    with pytest.raises(NotImplementedError):
        step(state, asm.inb(0, pin_index=0, bitsel=asm.BITSEL_BIT0))
