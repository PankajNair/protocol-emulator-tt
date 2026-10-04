# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

import isa_asm as asm

# uio bit positions -- pin_index doubles as the uio bit position
# throughout this design (docs/architecture.md Pin map).
HOST_GO_BIT = 4
HOST_STATUS_BIT = 5
START_BIT = 6

# Gate-level simulation (`GATES=yes make`, run by the TinyTapeout GDS
# action on the hardened netlist): internal RTL signals (u_core.*) don't
# exist in the flattened netlist. Tests that only observe internal state
# are skipped there; tests with a behavioral part keep it and drop only
# the internal probes.
GL = os.environ.get("GATES") == "yes"


def _bit(value: int, n: int) -> int:
    return (value >> n) & 1


def host_status(dut) -> int:
    """HOST_STATUS as the host physically sees it: the chip must be
    driving uio[5] (uio_oe[5]=1) for its value to mean anything. Reading
    uio_out alone let a stuck-at-0 uio_oe[5] -- the chip unable to talk
    to the host at all -- pass every test."""
    assert _bit(int(dut.uio_oe.value), HOST_STATUS_BIT) == 1, "HOST_STATUS (uio[5]) not driven: uio_oe[5]=0"
    return _bit(int(dut.uio_out.value), HOST_STATUS_BIT)


async def wait_host_status(dut, level: int, max_cycles: int = 200, what: str = ""):
    """Bounded wait for HOST_STATUS == level. An unbounded loop here hung
    the whole suite (and make mutate) on a boot-handshake bug instead of
    failing it."""
    for _ in range(max_cycles):
        if host_status(dut) == level:
            return
        await RisingEdge(dut.clk)
    assert False, f"HOST_STATUS never reached {level} within {max_cycles} cycles {what}".rstrip()


async def load_byte(dut, byte_val, uio_in_state):
    """One LOAD-mode boot write: host asserts HOST_GO, waits for the
    HOST_STATUS ack, then releases and waits for the ack to clear
    (docs/architecture.md Pipeline LOAD section's write-commit
    handshake) -- not a fixed cycle count, just level transitions.
    The ack must also HOLD until the host drops HOST_GO: the host is
    entitled to sample it late (architecture.md), so an ack that
    withdraws early is a protocol violation, checked here."""
    dut.ui_in.value = byte_val
    uio_in_state |= 1 << HOST_GO_BIT
    dut.uio_in.value = uio_in_state

    await wait_host_status(dut, 1, what="(boot write ack)")
    for _ in range(4):  # host deliberately slow to react; ack must persist
        await RisingEdge(dut.clk)
        assert host_status(dut) == 1, "boot write ack dropped while HOST_GO still high"

    uio_in_state &= ~(1 << HOST_GO_BIT)
    dut.uio_in.value = uio_in_state

    await wait_host_status(dut, 0, what="(boot ack clear)")
    return uio_in_state


_clock_task = None


def _ensure_clock(dut):
    """One clock per test. Some tests call reset_and_boot twice; starting
    a second Clock on the same signal is the same leak fixed in
    test_random.py (0636edf). cocotb cancels tasks at test end, so a
    finished task means a new test."""
    global _clock_task
    if _clock_task is None or _clock_task.done():
        _clock_task = cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())  # 50MHz, info.yaml clock_hz


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
    _ensure_clock(dut)

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
async def test_shift_vacated_bit_is_zero(dut):
    """isa.md SHIFT row: the vacated bit is always 0 (no rotate). The
    values in test_shift shift out a 0, so a rotate would pass there;
    0x81 shifts out a 1 in both directions."""
    prog = [
        asm.ldi(0, 0x81),
        asm.shift(0, asm.SHIFT_RIGHT),
        asm.out(0),                      # expect 0x40, a rotate gives 0xC0
        asm.ldi(1, 0x81),
        asm.shift(1, asm.SHIFT_LEFT),
        asm.out(1),                      # expect 0x02, a rotate gives 0x03
        asm.halt(),
    ]
    await reset_and_boot(dut, prog)
    await run_to_value(dut, 0x40)
    await run_to_value(dut, 0x02)
    dut._log.info("SHIFT fills the vacated bit with 0 in both directions")


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


@cocotb.test()
async def test_call_ret_nested_misuse_flag(dut):
    """A nested CALL (executed while a return address is already pending)
    silently overwrites `retaddr` rather than being protected or nesting
    (docs/isa.md Branch format section's 'no return stack' limitation) --
    and sets the sticky `call_ret_misuse_flag` (formal/debug-visibility
    only per isa.md, no opcode can read it -- 'now diagnosable, not just
    documented'). Confirmed two ways: behaviorally (RET returns to the
    INNER call's continuation, not the outer one -- proof the outer
    return address was actually lost, not just theoretically at risk)
    and via direct hierarchical probe of the sticky flag itself, the
    same debug-probe access test_hierarchy_smoke.py already established
    works identically on icarus/verilator."""
    prog = asm.assemble([
        (None, lambda L: asm.call(L["sub1"])),
        (None, lambda L: asm.halt()),          # outer return address -- poison, unreachable if the overwrite is real
        ("sub1", lambda L: asm.call(L["sub2"])),
        (None, lambda L: asm.ldi(0, 0x5E)),     # inner return lands here, not at the poison HALT above
        (None, lambda L: asm.out(0)),
        (None, lambda L: asm.halt()),
        ("sub2", lambda L: asm.ret()),
    ])
    await reset_and_boot(dut, prog)
    core = None if GL else dut.user_project.u_core
    await run_to_value(dut, 0x5E)
    if GL:
        return  # behavioral half (return lands at the inner continuation) checked above
    assert int(core.call_ret_misuse_flag.value) == 1, "nested CALL (return_valid already 1) should have set the sticky misuse flag"
    assert int(core.return_valid.value) == 0, "RET should have cleanly consumed the (overwritten) pending return"
    dut._log.info("nested CALL overwrote the outer return address and set call_ret_misuse_flag, as documented")


@cocotb.test(skip=GL)  # observes only an internal flag
async def test_ret_without_call_misuse_flag(dut):
    """An 'orphan' RET -- executed with no pending return address
    (return_valid=0) -- silently jumps to the return-address register's
    reset value, 0 (docs/isa.md Branch format section), and sets the same
    sticky call_ret_misuse_flag. Confirmed via direct hierarchical probe;
    the flag has no opcode-visible port by design."""
    prog = [asm.ret()]  # address 0: orphan RET, no CALL has ever executed
    await reset_and_boot(dut, prog)
    core = dut.user_project.u_core
    await ClockCycles(dut.clk, 30)  # RET->PC0->RET loops harmlessly; flag is sticky either way
    assert int(core.call_ret_misuse_flag.value) == 1, "orphan RET (return_valid=0) should set the sticky misuse flag"
    assert int(core.return_valid.value) == 0, "return_valid should remain 0 -- RET consumes nothing when nothing was pending"
    dut._log.info("orphan RET correctly flagged via call_ret_misuse_flag")


@cocotb.test(skip=GL)  # observes only an internal flag; behavior covered by test_illegal_opcode
async def test_illegal_opcode_sets_flag(dut):
    """An unassigned opcode (19) decodes as NOP for execution purposes
    (already covered by test_illegal_opcode) but ALSO sets the sticky
    illegal_op_flag (docs/isa.md Undefined opcode behavior) -- confirmed
    via direct hierarchical probe, since firmware cannot observe this
    flag by design (formal/debug-visibility only)."""
    prog = [
        asm.ldi(0, 0x11),
        asm.out(0),
        asm.raw(19),
        asm.halt(),
    ]
    await reset_and_boot(dut, prog)
    core = dut.user_project.u_core
    await run_to_value(dut, 0x11)
    assert int(core.illegal_op_flag.value) == 0, "flag should still be clear before the illegal opcode executes"
    await ClockCycles(dut.clk, 10)  # let the raw(19) instruction finish its EXECUTE cycle
    assert int(core.illegal_op_flag.value) == 1, "unassigned opcode 19 should have set the sticky illegal_op_flag"
    dut._log.info("illegal opcode correctly set illegal_op_flag while still executing as NOP")


@cocotb.test()
async def test_loop_does_not_touch_flag(dut):
    """LOOP's branch decision is entirely internal to LOOP itself -- it
    must not write the shared CMP/TEST-bit/WAIT flag (docs/isa.md LOOP
    row: 'Does not touch the shared flag'). Verified by setting flag=1
    via CMP immediately before a LOOP, then checking a following BEQ
    still takes -- if LOOP silently touched the flag, BEQ would not."""
    prog = asm.assemble([
        (None, lambda L: asm.ldi(0, 5)),
        (None, lambda L: asm.ldi(1, 5)),
        (None, lambda L: asm.cmp_(0, 1)),          # flag <= (5==5) = 1
        (None, lambda L: asm.ldi(2, 2)),            # loop counter
        ("loop", lambda L: asm.nop()),              # trivial loop body
        (None, lambda L: asm.loop_(L["loop"], 2)),
        (None, lambda L: asm.beq(L["flag_still_set"])),  # takes only if LOOP left flag alone
        (None, lambda L: asm.ldi(3, 0x00)),          # poison: flag was clobbered
        (None, lambda L: asm.halt()),
        ("flag_still_set", lambda L: asm.ldi(3, 0xF1)),
        (None, lambda L: asm.out(3)),
        (None, lambda L: asm.halt()),
    ])
    await reset_and_boot(dut, prog)
    await run_to_value(dut, 0xF1)
    dut._log.info("LOOP left the shared flag untouched, as documented")


@cocotb.test()
async def test_shift_does_not_touch_flag(dut):
    """SHIFT must not write the shared flag (docs/isa.md SHIFT row:
    'SHIFT does not touch the shared flag') -- verified the same way as
    LOOP's equivalent property above: set flag=1 via CMP, SHIFT a
    register, confirm a following BEQ still takes."""
    prog = asm.assemble([
        (None, lambda L: asm.ldi(0, 5)),
        (None, lambda L: asm.ldi(1, 5)),
        (None, lambda L: asm.cmp_(0, 1)),          # flag <= 1
        (None, lambda L: asm.shift(0, asm.SHIFT_LEFT)),
        (None, lambda L: asm.beq(L["flag_still_set"])),
        (None, lambda L: asm.ldi(3, 0x00)),          # poison
        (None, lambda L: asm.halt()),
        ("flag_still_set", lambda L: asm.ldi(3, 0xF2)),
        (None, lambda L: asm.out(3)),
        (None, lambda L: asm.halt()),
    ])
    await reset_and_boot(dut, prog)
    await run_to_value(dut, 0xF2)
    dut._log.info("SHIFT left the shared flag untouched, as documented")


@cocotb.test()
async def test_wait_success_clears_flag(dut):
    """Every WAIT writes the shared flag on exit, uniformly -- 0 if the
    pin condition was met, 1 if it timed out (docs/isa.md WAIT row).
    test_wait_timeout already covers the timeout=1 path; this covers the
    success=0 path, which is otherwise never directly checked -- verified
    by pre-poisoning flag=1 via CMP, then a successful (non-timeout) WAIT,
    then a BNE that only takes if flag is genuinely 0."""
    prog = asm.assemble([
        (None, lambda L: asm.ldi(0, 5)),
        (None, lambda L: asm.ldi(1, 5)),
        (None, lambda L: asm.cmp_(0, 1)),             # flag <= 1 (poison state)
        (None, lambda L: asm.wait_(HOST_GO_BIT, 1)),  # unbounded; met once HOST_GO asserted
        (None, lambda L: asm.bne(L["flag_cleared"])), # needs flag=0 to take
        (None, lambda L: asm.ldi(3, 0x00)),            # poison: flag still 1
        (None, lambda L: asm.halt()),
        ("flag_cleared", lambda L: asm.ldi(3, 0xF3)),
        (None, lambda L: asm.out(3)),
        (None, lambda L: asm.halt()),
    ])
    uio_in_state = await reset_and_boot(dut, prog)
    await ClockCycles(dut.clk, 20)  # let LDI/LDI/CMP run; confirm still blocked at WAIT
    assert int(dut.uo_out.value) == 0, "shouldn't have progressed past WAIT yet"
    uio_in_state |= 1 << HOST_GO_BIT
    dut.uio_in.value = uio_in_state
    await run_to_value(dut, 0xF3, max_cycles=50)
    dut._log.info("successful WAIT correctly cleared the shared flag to 0")


@cocotb.test()
async def test_boot_echo_and_saturation(dut):
    """LOAD-mode boot stream over-run (docs/architecture.md Pipeline LOAD):
    uo_out echoes the post-increment byte address mod 256 after every
    HOST_GO pulse, and the counter SATURATES at 1023 rather than
    wrapping. Streams a full 1024-byte image plus 2 extra bytes encoding
    HALT -- a wrapping counter would write them to addresses 0/1,
    replacing instruction 0 with HALT so the marker never appears; a
    saturating one just rewrites byte 1023 (last data byte, harmless)."""
    _ensure_clock(dut)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    image = asm.to_bytes([asm.ldi(0, 0x3C), asm.out(0), asm.halt()])
    image += [0] * (1024 - len(image))
    image += asm.to_bytes([asm.halt()])  # 2 over-run bytes

    uio_in_state = 0
    for k, b in enumerate(image, start=1):
        uio_in_state = await load_byte(dut, b, uio_in_state)
        expected = min(k, 1023) & 0xFF
        echo = int(dut.uo_out.value)
        assert echo == expected, f"boot echo after pulse {k}: got {echo:#x}, expected {expected:#x}"

    uio_in_state |= 1 << START_BIT
    dut.uio_in.value = uio_in_state
    await ClockCycles(dut.clk, 4)
    dut.uio_in.value = uio_in_state & ~(1 << START_BIT)
    await run_to_value(dut, 0x3C)
    dut._log.info("boot echo correct for all 1026 pulses; counter saturated, instruction 0 intact")


@cocotb.test()
async def test_host_error_gated_until_start_fall(dut):
    """uio[6] driver-contention guard (docs/architecture.md LOAD): the
    chip must not drive HOST_ERROR while the host may still be holding
    START high -- uio_oe[6] stays 0 until START's synchronized falling
    edge, even though execution already began on its rising edge and
    firmware has already SET HOST_ERROR=1. Once START falls, the latched
    value reaches the pin with no firmware involvement."""
    _ensure_clock(dut)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    prog = [
        asm.set_pin(asm.SET_MODE_LEAVE, pin_index=START_BIT, value=1),  # HOST_ERROR = 1
        asm.ldi(0, 0x6B),
        asm.out(0),
        asm.halt(),
    ]
    uio_in_state = 0
    for b in asm.to_bytes(prog):
        uio_in_state = await load_byte(dut, b, uio_in_state)

    uio_in_state |= 1 << START_BIT
    dut.uio_in.value = uio_in_state
    # Hold START high well past the program finishing (marker + HALT).
    for c in range(60):
        await RisingEdge(dut.clk)
        assert _bit(int(dut.uio_oe.value), START_BIT) == 0, \
            f"uio_oe[6] asserted {c} cycles into the START-high window -- driver contention with host"
    assert int(dut.uo_out.value) == 0x6B, "program should already have run while START was held"

    dut.uio_in.value = uio_in_state & ~(1 << START_BIT)
    await ClockCycles(dut.clk, 6)  # 2-flop sync + edge detect + latch
    assert _bit(int(dut.uio_oe.value), START_BIT) == 1, "HOST_ERROR driver should enable after START falls"
    assert _bit(int(dut.uio_out.value), START_BIT) == 1, "latched HOST_ERROR=1 should reach the pin once gated open"
    dut._log.info("HOST_ERROR driver held off until START fell, then drove the latched value")


@cocotb.test()
async def test_host_handshake_in_out(dut):
    """The runtime host protocol, end to end (docs/architecture.md Host
    handshake): both directions host-initiated 4-phase exchanges ending
    on firmware's HOST_STATUS fall. Firmware echoes each host byte back,
    three times in a LOOP. The host follows only the level contract --
    wait for HOST_STATUS, never count cycles -- requires HOST_STATUS to
    be actually driven (uio_oe[5]), and turns every transfer around with
    ZERO gap: it raises HOST_GO for the next exchange the instant it sees
    the previous one complete. Under the old firmware-initiated OUT
    sequence that exact turnaround deadlocked both sides (nothing acked
    the host's final HOST_GO fall), which is what made the spec change."""
    go = HOST_GO_BIT
    prog = asm.assemble([
        (None, lambda L: asm.ldi(1, 3)),
        # host -> firmware (IN)
        ("byte", lambda L: asm.wait_(go, 1)),
        (None, lambda L: asm.in_(0)),
        (None, lambda L: asm.set_pin(asm.SET_MODE_LEAVE, HOST_STATUS_BIT, 1)),
        (None, lambda L: asm.wait_(go, 0)),
        (None, lambda L: asm.set_pin(asm.SET_MODE_LEAVE, HOST_STATUS_BIT, 0)),
        # firmware -> host (OUT), host-initiated
        (None, lambda L: asm.wait_(go, 1)),
        (None, lambda L: asm.out(0)),
        (None, lambda L: asm.set_pin(asm.SET_MODE_LEAVE, HOST_STATUS_BIT, 1)),
        (None, lambda L: asm.wait_(go, 0)),
        (None, lambda L: asm.set_pin(asm.SET_MODE_LEAVE, HOST_STATUS_BIT, 0)),
        (None, lambda L: asm.loop_(L["byte"], 1)),
        (None, lambda L: asm.halt()),
    ])
    uio = await reset_and_boot(dut, prog)
    await ClockCycles(dut.clk, 10)
    assert host_status(dut) == 0, "HOST_STATUS should idle low after boot"

    async def exchange(uio):
        uio |= 1 << go
        dut.uio_in.value = uio
        await wait_host_status(dut, 1)
        return uio

    async def release(uio):
        uio &= ~(1 << go)
        dut.uio_in.value = uio
        await wait_host_status(dut, 0)
        return uio

    for byte in (0xA5, 0x3C, 0x81):
        dut.ui_in.value = byte          # host -> firmware
        uio = await exchange(uio)
        uio = await release(uio)
        uio = await exchange(uio)       # firmware -> host: request, zero gap
        got = int(dut.uo_out.value)
        uio = await release(uio)
        assert got == byte, f"echoed {got:#x}, sent {byte:#x}"
    dut._log.info("3 bytes round-tripped, zero-gap turnarounds, both directions host-initiated")


@cocotb.test()
async def test_halt_holds_pins(dut):
    """HALT freezes everything (docs/isa.md HALT row): all outputs,
    uio_oe included, hold their last-driven state; pins are NOT released
    and HOST_STATUS is not touched. Configures a mix of drive modes and
    host pins, halts, then wiggles every input for 100 cycles and checks
    no output moves. Previously only uo_out was checked after HALT."""
    prog = [
        asm.set_pin(asm.SET_MODE_PUSH_PULL, 0, 1),
        asm.set_pin(asm.SET_MODE_OPEN_DRAIN, 1, 0),   # actively pulling low
        asm.set_pin(asm.SET_MODE_PUSH_PULL, 7, 0),
        asm.set_pin(asm.SET_MODE_LEAVE, HOST_STATUS_BIT, 1),
        asm.set_pin(asm.SET_MODE_LEAVE, START_BIT, 1),  # HOST_ERROR
        asm.ldi(0, 0x5D),
        asm.out(0),
        asm.halt(),
    ]
    await reset_and_boot(dut, prog)
    await run_to_value(dut, 0x5D, max_cycles=80)
    await ClockCycles(dut.clk, 10)
    if not GL:
        assert int(dut.user_project.u_core.halted.value) == 1

    oe, out = int(dut.uio_oe.value), int(dut.uio_out.value)
    expect_oe = {0: 1, 1: 1, 4: 0, 5: 1, 6: 1, 7: 1}
    expect_out = {0: 1, 1: 0, 5: 1, 6: 1, 7: 0}
    for b, v in expect_oe.items():
        assert _bit(oe, b) == v, f"uio_oe[{b}]={_bit(oe, b)}, expected {v} at HALT"
    for b, v in expect_out.items():
        assert _bit(out, b) == v, f"uio_out[{b}]={_bit(out, b)}, expected {v} at HALT"

    import random
    rng = random.Random(7)
    for c in range(100):
        dut.uio_in.value = rng.randint(0, 255) & ~(1 << START_BIT)
        dut.ui_in.value = rng.randint(0, 255)
        await RisingEdge(dut.clk)
        assert int(dut.uio_oe.value) == oe, f"uio_oe moved after HALT (cycle {c})"
        assert int(dut.uio_out.value) == out, f"uio_out moved after HALT (cycle {c})"
        assert int(dut.uo_out.value) == 0x5D, f"uo_out moved after HALT (cycle {c})"
    dut._log.info("all pins held their driven state through 100 cycles of input noise after HALT")
