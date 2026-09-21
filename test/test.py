# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

import isa_asm as asm

# uio bit positions -- pin_index doubles as the uio bit position
# throughout this design (docs/architecture.md Pin map).
HOST_GO_BIT = 4
HOST_STATUS_BIT = 5
START_BIT = 6


def _bit(value: int, n: int) -> int:
    return (value >> n) & 1


async def load_byte(dut, byte_val, uio_in_state):
    """One LOAD-mode boot write: host asserts HOST_GO, waits for the
    HOST_STATUS ack, then releases and waits for the ack to clear
    (docs/architecture.md Pipeline LOAD section's write-commit
    handshake) -- not a fixed cycle count, just level transitions."""
    dut.ui_in.value = byte_val
    uio_in_state |= 1 << HOST_GO_BIT
    dut.uio_in.value = uio_in_state

    while not _bit(int(dut.uio_out.value), HOST_STATUS_BIT):
        await RisingEdge(dut.clk)

    uio_in_state &= ~(1 << HOST_GO_BIT)
    dut.uio_in.value = uio_in_state

    while _bit(int(dut.uio_out.value), HOST_STATUS_BIT):
        await RisingEdge(dut.clk)

    return uio_in_state


async def reset_and_boot(dut, words, ui_in_for_run=None):
    """Starts the clock, resets the DUT, streams `words` in via the
    LOAD-mode boot handshake, then pulses START (uio[6]). Returns the
    uio_in state after START is released (HOST_GO/START both low) so
    callers that need to keep driving other uio bits (WAIT/INB tests)
    continue from the right base instead of reconstructing it.

    ui_in is overwritten byte-by-byte by the LOAD-mode boot stream
    itself (load_byte sets it per boot byte) -- `ui_in_for_run`, if
    given, is applied AFTER the boot stream but BEFORE pulsing START,
    not after this function returns: START's synchronized rising edge
    (and the very first instruction after it) can land only ~2 cycles
    into the START-hold window this function does internally, so a
    caller setting ui_in only after this returns can race a program
    whose first instruction is IN -- confirmed the hard way, an earlier
    version of test_in raced exactly this and read a stale boot-stream
    leftover byte instead."""
    clock = Clock(dut.clk, 20, unit="ns")  # 50MHz, matches info.yaml clock_hz
    cocotb.start_soon(clock.start())

    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    uio_in_state = 0
    for b in asm.to_bytes(words):
        uio_in_state = await load_byte(dut, b, uio_in_state)

    if ui_in_for_run is not None:
        dut.ui_in.value = ui_in_for_run

    uio_in_state |= 1 << START_BIT
    dut.uio_in.value = uio_in_state
    await ClockCycles(dut.clk, 4)
    uio_in_state &= ~(1 << START_BIT)
    dut.uio_in.value = uio_in_state
    return uio_in_state


async def run_to_value(dut, target_value, max_cycles=300):
    """Clocks the DUT until uo_out == target_value, returning the cycle
    count at which it first does. A real hang/bug shows up as an
    assertion failure here, not infinite patience -- target_value must
    never be uo_out's reset value (0) for this to be a meaningful
    check; every test below picks a nonzero, distinct marker."""
    assert target_value != 0, "target_value must be nonzero (0 is uo_out's reset value)"
    for c in range(1, max_cycles + 1):
        await RisingEdge(dut.clk)
        if int(dut.uo_out.value) == target_value:
            return c
    assert False, f"uo_out never reached {target_value:#x} within {max_cycles} cycles"


@cocotb.test()
async def test_boot_load_execute_halt(dut):
    """Streams LDI R0,42 / OUT R0 / HALT in via the LOAD-mode boot
    handshake, pulses START, and checks the CPU actually runs it end to
    end: uo_out should reach 42 and then freeze there (HALT is
    documented as resumable only by external reset)."""
    prog = [asm.ldi(0, 42), asm.out(0), asm.halt()]
    await reset_and_boot(dut, prog)

    await run_to_value(dut, 42, max_cycles=60)
    dut._log.info("uo_out == 42, LDI/OUT ran correctly")

    await ClockCycles(dut.clk, 20)
    assert dut.uo_out.value == 42, "uo_out drifted after HALT -- FSM should be frozen"
    dut._log.info("uo_out held steady post-HALT, FSM frozen as expected")


@cocotb.test()
async def test_branch_taken(dut):
    """JMP, and BEQ/BNE when their condition IS met, all actually jump
    -- each poison instruction below is only reachable if the branch
    right before it failed to take."""
    prog = asm.assemble([
        (None, lambda L: asm.ldi(0, 1)),
        (None, lambda L: asm.jmp(L["after_jmp"])),
        (None, lambda L: asm.out(0)),          # poison: JMP didn't take
        (None, lambda L: asm.halt()),          # poison path halts early
        ("after_jmp", lambda L: asm.ldi(0, 5)),
        (None, lambda L: asm.ldi(1, 5)),
        (None, lambda L: asm.cmp_(0, 1)),      # flag = (5==5) = 1
        (None, lambda L: asm.beq(L["after_beq"])),
        (None, lambda L: asm.ldi(2, 0xAA)),    # poison: BEQ didn't take
        (None, lambda L: asm.halt()),
        ("after_beq", lambda L: asm.ldi(2, 0x11)),
        (None, lambda L: asm.ldi(1, 6)),
        (None, lambda L: asm.cmp_(0, 1)),      # flag = (5==6) = 0
        (None, lambda L: asm.bne(L["after_bne"])),
        (None, lambda L: asm.ldi(2, 0xBB)),    # poison: BNE didn't take
        (None, lambda L: asm.halt()),
        ("after_bne", lambda L: asm.out(2)),
        (None, lambda L: asm.halt()),
    ])
    await reset_and_boot(dut, prog)
    await run_to_value(dut, 0x11)
    dut._log.info("JMP, BEQ-taken, and BNE-taken all correct")


@cocotb.test()
async def test_branch_not_taken(dut):
    """BEQ/BNE do NOT jump when their condition is false -- each poison
    instruction is only reachable if the branch incorrectly took."""
    prog = asm.assemble([
        (None, lambda L: asm.ldi(0, 5)),
        (None, lambda L: asm.ldi(1, 6)),
        (None, lambda L: asm.cmp_(0, 1)),      # flag = (5==6) = 0
        (None, lambda L: asm.beq(L["poison1"])),  # needs flag=1 -- must NOT take
        (None, lambda L: asm.ldi(1, 5)),
        (None, lambda L: asm.cmp_(0, 1)),      # flag = (5==5) = 1
        (None, lambda L: asm.bne(L["poison1"])),  # needs flag=0 -- must NOT take
        (None, lambda L: asm.ldi(2, 0x33)),
        (None, lambda L: asm.jmp(L["done"])),
        ("poison1", lambda L: asm.ldi(2, 0x77)),
        (None, lambda L: asm.halt()),
        ("done", lambda L: asm.out(2)),
        (None, lambda L: asm.halt()),
    ])
    await reset_and_boot(dut, prog)
    await run_to_value(dut, 0x33)
    dut._log.info("BEQ-not-taken and BNE-not-taken both correct")


@cocotb.test()
async def test_call_ret(dut):
    """CALL pushes the correct return address (the word right after the
    CALL itself), RET returns to it."""
    prog = asm.assemble([
        (None, lambda L: asm.call(L["sub"])),
        (None, lambda L: asm.out(0)),          # return lands here
        (None, lambda L: asm.halt()),
        ("sub", lambda L: asm.ldi(0, 0x42)),
        (None, lambda L: asm.ret()),
    ])
    await reset_and_boot(dut, prog)
    await run_to_value(dut, 0x42)
    dut._log.info("CALL/RET round-trip correct")


@cocotb.test()
async def test_loop(dut):
    """LOOP actually repeats the right number of times -- measured
    differentially (count=3 vs count=1) rather than against a hand-
    derived absolute cycle count, so this test can't be wrong about the
    same kind of off-by-one it's trying to catch in the DUT."""

    def build(count):
        return asm.assemble([
            (None, lambda L: asm.ldi(0, count)),
            ("loop", lambda L: asm.ldi(1, 0xAA)),  # loop-body marker
            (None, lambda L: asm.loop_(L["loop"], 0)),
            (None, lambda L: asm.out(1)),
            (None, lambda L: asm.halt()),
        ])

    await reset_and_boot(dut, build(1))
    cycles_1 = await run_to_value(dut, 0xAA)

    await reset_and_boot(dut, build(3))
    cycles_3 = await run_to_value(dut, 0xAA)

    extra = cycles_3 - cycles_1
    dut._log.info(f"count=1: {cycles_1} cycles, count=3: {cycles_3} cycles, extra={extra}")
    # 2 extra passes through the loop body (LDI + LOOP, 3 cycles each).
    assert extra == 12, f"expected 2 extra passes (12 cycles), got {extra}"


@cocotb.test()
async def test_delay_timing(dut):
    """DELAY adds exactly N extra cycles -- measured differentially
    (mantissa=6 vs mantissa=0) against the same OUT-to-OUT gap, so this
    test verifies the *added* delay without needing to independently
    derive the absolute baseline gap by hand."""

    def build(mantissa):
        return [
            asm.ldi(0, 0x11),
            asm.out(0),
            asm.delay(mantissa, 0),
            asm.ldi(0, 0x22),
            asm.out(0),
            asm.halt(),
        ]

    async def measure(mantissa):
        await reset_and_boot(dut, build(mantissa))
        t1 = await run_to_value(dut, 0x11)
        # run_to_value clocks from 0, so re-measure the gap to 0x22 from here.
        for c in range(1, 300):
            await RisingEdge(dut.clk)
            if int(dut.uo_out.value) == 0x22:
                return c
        assert False, "uo_out never reached 0x22"

    gap0 = await measure(0)
    gap6 = await measure(6)
    dut._log.info(f"mantissa=0 gap={gap0}, mantissa=6 gap={gap6}")
    assert gap6 - gap0 == 6, f"expected exactly 6 extra cycles, got {gap6 - gap0}"


@cocotb.test()
async def test_wait_condition_met(dut):
    """WAIT genuinely blocks until its pin condition is met, not just
    coincidentally fast -- confirmed both ways: no output for a healthy
    margin before HOST_GO is asserted, and a prompt one after."""
    prog = [
        asm.wait_(HOST_GO_BIT, 1),  # unbounded: block until HOST_GO==1
        asm.ldi(0, 0x55),
        asm.out(0),
        asm.halt(),
    ]
    uio_in_state = await reset_and_boot(dut, prog)

    await ClockCycles(dut.clk, 50)
    assert int(dut.uo_out.value) == 0, "uo_out changed before HOST_GO was ever asserted -- WAIT didn't block"

    uio_in_state |= 1 << HOST_GO_BIT
    dut.uio_in.value = uio_in_state
    cycles = await run_to_value(dut, 0x55, max_cycles=50)
    dut._log.info(f"WAIT unblocked {cycles} cycles after HOST_GO asserted")


@cocotb.test()
async def test_wait_timeout(dut):
    """WAIT with a bounded timeout on a pin that's never driven times
    out and sets the shared flag -- observed via a following BEQ, since
    the flag has no direct port."""
    prog = asm.assemble([
        (None, lambda L: asm.wait_(0, 1, mantissa=5, exponent=0)),  # pin_index=0 never driven high
        (None, lambda L: asm.beq(L["timed_out"])),  # flag=1 (timeout) -> should take
        (None, lambda L: asm.ldi(0, 0xAA)),          # poison: condition wrongly seen as met
        (None, lambda L: asm.halt()),
        ("timed_out", lambda L: asm.ldi(0, 0xCC)),
        (None, lambda L: asm.out(0)),
        (None, lambda L: asm.halt()),
    ])
    await reset_and_boot(dut, prog)
    await run_to_value(dut, 0xCC)
    dut._log.info("WAIT timeout correctly set the flag, BEQ consumed it")


@cocotb.test()
async def test_set_outb_pin_drive(dut):
    """SET configures a protocol pin push-pull and drives an initial
    value; OUTB then drives it from a register bit."""
    prog = [
        asm.ldi(0, 0x81),  # 0b10000001 -- bit0=1, bit7=1
        asm.set_pin(asm.SET_MODE_PUSH_PULL, pin_index=0, value=0),
        asm.outb(0, pin_index=0, bitsel=asm.BITSEL_BIT0),
        asm.halt(),
    ]
    await reset_and_boot(dut, prog)
    await ClockCycles(dut.clk, 40)

    uio_oe = int(dut.uio_oe.value)
    uio_out = int(dut.uio_out.value)
    assert _bit(uio_oe, 0) == 1, "pin_index=0 should be driven (push-pull) after SET"
    assert _bit(uio_out, 0) == 1, "OUTB should have driven bit0 of R0 (=1) onto the pin"


@cocotb.test()
async def test_open_drain_release(dut):
    """SET open-drain + value=1 (release) leaves the pin Hi-Z -- an
    external pull-up, not this chip, would pull it high."""
    prog = [
        asm.set_pin(asm.SET_MODE_OPEN_DRAIN, pin_index=1, value=1),
        asm.halt(),
    ]
    await reset_and_boot(dut, prog)
    await ClockCycles(dut.clk, 40)
    assert _bit(int(dut.uio_oe.value), 1) == 0, "open-drain release should leave oe=0 (Hi-Z)"


@cocotb.test()
async def test_open_drain_drive_low(dut):
    """SET open-drain + value=0 actively drives the pin low."""
    prog = [
        asm.set_pin(asm.SET_MODE_OPEN_DRAIN, pin_index=1, value=0),
        asm.halt(),
    ]
    await reset_and_boot(dut, prog)
    await ClockCycles(dut.clk, 40)
    assert _bit(int(dut.uio_oe.value), 1) == 1, "open-drain drive-low should assert oe"
    assert _bit(int(dut.uio_out.value), 1) == 0, "open-drain drive-low should output 0"


@cocotb.test()
async def test_inb_reads_pin(dut):
    """INB captures an externally-driven protocol pin's synchronized
    level into a register bit; OUT then reveals it."""
    prog = [
        asm.ldi(0, 0),
        asm.inb(0, pin_index=1, bitsel=asm.BITSEL_BIT0),
        asm.out(0),
        asm.halt(),
    ]
    uio_in_state = 1 << 1  # drive uio[1] high before/through the whole run
    await reset_and_boot(dut, prog)
    dut.uio_in.value = int(dut.uio_in.value) | uio_in_state
    await run_to_value(dut, 0x01)
    dut._log.info("INB correctly captured the externally-driven pin")


@cocotb.test()
async def test_store_load(dut):
    """STORE writes the data region; a LOAD from the same address on
    the VERY NEXT instruction already sees the fresh value -- the
    documented hazard-free trailing-writeback case
    (docs/architecture.md Pipeline FETCH_LO/FETCH_HI/EXECUTE section)."""
    prog = [
        asm.ldi(0, 0x77),
        asm.store(0, 0),
        asm.load(1, 0),
        asm.out(1),
        asm.halt(),
    ]
    await reset_and_boot(dut, prog)
    await run_to_value(dut, 0x77)
    dut._log.info("STORE/LOAD round-trip correct, including same-cycle-adjacent hazard")


@cocotb.test()
async def test_loadx(dut):
    """LOADX indexes the data region by a register's value."""
    prog = [
        asm.ldi(0, 5),
        asm.store(0, 3),   # mem[512+3] = 5
        asm.ldi(2, 3),     # index register
        asm.loadx(1, 2),   # R1 <= mem[512 + R2] = mem[515] = 5
        asm.out(1),
        asm.halt(),
    ]
    await reset_and_boot(dut, prog)
    await run_to_value(dut, 5)
    dut._log.info("LOADX register-indexed addressing correct")


@cocotb.test()
async def test_shift(dut):
    """SHIFT left and right both work, in that order (both directions
    verified via the sequence they appear on uo_out, not just the
    final value)."""
    prog = [
        asm.ldi(0, 0b00000011),
        asm.shift(0, asm.SHIFT_LEFT),
        asm.out(0),                      # expect 6
        asm.ldi(1, 0b10000000),
        asm.shift(1, asm.SHIFT_RIGHT),
        asm.out(1),                      # expect 64
        asm.halt(),
    ]
    await reset_and_boot(dut, prog)
    await run_to_value(dut, 6)
    await run_to_value(dut, 64)
    dut._log.info("SHIFT left then right both correct, in order")


@cocotb.test()
async def test_testbit(dut):
    """TEST-bit reads a register bit into the shared flag; a following
    BEQ consumes it."""
    prog = asm.assemble([
        (None, lambda L: asm.ldi(0, 0b00000100)),  # bit2=1
        (None, lambda L: asm.testbit(0, 2)),        # flag <= R0[2] = 1
        (None, lambda L: asm.beq(L["bit_set"])),
        (None, lambda L: asm.ldi(1, 0x00)),          # poison
        (None, lambda L: asm.halt()),
        ("bit_set", lambda L: asm.ldi(1, 0xFF)),
        (None, lambda L: asm.out(1)),
        (None, lambda L: asm.halt()),
    ])
    await reset_and_boot(dut, prog)
    await run_to_value(dut, 0xFF)
    dut._log.info("TEST-bit correctly read a set bit and BEQ consumed it")


@cocotb.test()
async def test_illegal_opcode(dut):
    """An unassigned opcode value (19) decodes as NOP -- execution
    continues normally past it, not stuck or crashed."""
    prog = [
        asm.ldi(0, 0x11),
        asm.out(0),
        asm.raw(19),
        asm.ldi(0, 0x22),
        asm.out(0),
        asm.halt(),
    ]
    await reset_and_boot(dut, prog)
    await run_to_value(dut, 0x11)
    await run_to_value(dut, 0x22)
    dut._log.info("Execution continued normally past an unassigned opcode")


@cocotb.test()
async def test_in(dut):
    """IN captures the host-facing parallel data bus (ui_in) into a
    register; OUT echoes it back out uo_out."""
    prog = [
        asm.in_(0),
        asm.out(0),
        asm.halt(),
    ]
    await reset_and_boot(dut, prog, ui_in_for_run=0x5A)
    await run_to_value(dut, 0x5A)
    dut._log.info("IN correctly captured the host data-in bus")
