"""Black-box spec-conformance suite for protocol-emulator-tt.

Written from docs/isa.md, docs/architecture.md, docs/protocol_timing.md,
docs/info.md and docs/serial_engine.md only, against tb.v's pins. Every expected value cites the spec
sentence it comes from. `ASSUMPTION:` marks a reading the spec does not
force; `AMBIGUOUS:` marks a place where the spec admits two readings (the
test encodes the one judged intended).

Timing convention: exact cycle counts are always measured as the distance
between two edges of the *same kind* (e.g. two SET KEEP value changes on a
push-pull pin), so the constant output-stage latency cancels.
"""

import cocotb

from spec_helpers import (Asm, Chip, KEEP, PP, OD, INPUT, PIN_GO, PIN_STATUS,
                          PIN_ERROR, PROTOCOL_PINS, RESERVED_OPS, OP, enc_0op,
                          delay_cycles, fw_send, fw_recv, fw_ack, fw_out_flag,
                          ref_add, ref_xor, crc8, crc8_table)

PROTO_AND_ERR = (0, 1, 2, 3, 6, 7)


def data_pattern(i):
    """Deterministic data-region preload; distinct at the boundaries tested."""
    return (i * 37 + 11 + 0x5A * (i >> 8)) & 0xFF


DATA = [data_pattern(i) for i in range(512)]


async def new_chip(dut):
    return await Chip.create(dut)


def gap_prog(mid, pre=None, tail=None):
    """SET p0 PP 0 (configure) ... SET p0 KEEP 1 | mid | SET p0 KEEP 0 | HALT.
    The rise->fall distance is SET's own 3 cycles plus mid's occupancy."""
    a = Asm()
    a.set(0, 0, PP)
    a.delay(10)
    if pre:
        pre(a)
    a.set(0, 1, KEEP)
    mid(a)
    a.set(0, 0, KEEP)
    a.halt()
    if tail:
        tail(a)
    return a


async def measure_gap(chip, prog, timeout=60000, ext=None, during=None):
    await chip.run(prog, ext=ext)
    await chip.wait_drive(0, "0", 500, "p0 configured push-pull low")
    r = await chip.wait_drive(0, "1", 500, "marker rise")
    if during:
        await during(chip)
    f = await chip.wait_drive(0, "0", timeout, "marker fall")
    return f - r


# ===========================================================================
# Reset and LOAD-mode boot
# ===========================================================================
@cocotb.test()
async def test_reset_pin_state(dut):
    """architecture.md I/O register reset state: uio_out all-0; uio_oe 0 for
    protocol pins, uio[4], uio[6]; 1 for uio[5] (HOST_STATUS hardwired output
    'in every mode, including LOAD'); uo_out resets to 0."""
    chip = await new_chip(dut)
    await chip.reset()
    for _ in range(20):
        assert chip.uo() == 0, f"uo_out after reset = {chip.s_uo}"
        assert chip.out_int() == 0, f"uio_out after reset = {chip.s_out}"
        assert chip.oe_int() == 0x20, f"uio_oe after reset = {chip.s_oe}, spec 0b00100000"
        await chip.clk()


@cocotb.test()
async def test_boot_echo_ack_and_hiz(dut):
    """architecture.md LOAD: each HOST_GO pulse writes, increments, echoes
    the counter's low 8 bits post-increment on uo_out ('host ... expects
    echo == k mod 256'); HOST_STATUS acks each write and clears after
    HOST_GO is seen low; protocol pins and uio[6] Hi-Z throughout LOAD.
    300 bytes crosses the 8-bit echo wrap at 256."""
    chip = await new_chip(dut)
    await chip.reset()
    for k in range(1, 301):
        echo = await chip.load_byte((k * 13) & 0xFF)
        assert echo == (k & 0xFF), f"pulse {k}: echo {echo}, spec {k & 0xFF}"
        assert chip.drive(PIN_STATUS) == "0"
    assert not chip.load_violations, f"pin driven during LOAD: {chip.load_violations[:5]}"


@cocotb.test()
async def test_boot_held_strobe_is_one_write(dut):
    """architecture.md LOAD: 'chip: sees synchronized HOST_GO=0 -> clears
    HOST_STATUS' -- the ack holds while GO is held; a long-held strobe is
    one pulse ('each pulse writes ... then increments'), so the echo does
    not advance while held and the next pulse advances by exactly 1."""
    chip = await new_chip(dut)
    await chip.reset()
    assert await chip.load_byte(0x11) == 1
    chip.set_ui(0x22)
    await chip.clk(2)
    chip.set_ext(PIN_GO, 1)
    await chip.wait_until(lambda: chip.drive(PIN_STATUS) == "1", 200, "ack")
    for _ in range(80):
        await chip.clk()
        assert chip.drive(PIN_STATUS) == "1", "HOST_STATUS dropped while HOST_GO still high"
        assert chip.uo() == 2, f"echo moved while strobe held: {chip.s_uo}"
    chip.set_ext(PIN_GO, 0)
    await chip.wait_until(lambda: chip.drive(PIN_STATUS) == "0", 200, "ack clear")
    assert await chip.load_byte(0x33) == 3
    assert not chip.load_violations


@cocotb.test()
async def test_boot_counter_saturates_no_wrap(dut):
    """architecture.md: 'Byte-address counter saturates at 1023, does not
    wrap. A HOST_GO pulse at address 1023 writes/echoes that byte and stops
    advancing -- further pulses are harmless no-ops, not silent corruption
    of address 0'. Echo after pulse k = min(k,1023) & 0xFF. Extra pulses
    carry 0x00 which, if they landed on address 0, would turn the first
    LDI's value byte into 0x00. Each extra pulse 'is still acked like any
    other: HOST_STATUS rises with the echo showing 1023's low byte (0xFF)
    and clears on the HOST_GO fall' (load_byte waits for both edges)."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0xA5)
    a.out(0)
    a.halt()
    img = a.image() + [0] * (1024 - len(a.image()))
    await chip.reset()
    await chip.boot(img)  # checks every echo incl. 256/512/768 wraps
    assert chip.last_echo == 0xFF
    for _ in range(4):
        echo = await chip.load_byte(0x00)
        # architecture.md saturation bullet: 'A no-op pulse ... is still
        # acked like any other: HOST_STATUS rises with the echo showing
        # 1023's low byte (0xFF) and clears on the HOST_GO fall'.
        assert echo == 0xFF, f"echo after saturation {echo}"
        assert chip.drive(PIN_STATUS) == "0", "ack not cleared after HOST_GO fall"
    await chip.clk(3)
    await chip.start()
    hist = await chip.wait_uo(1)
    assert hist == [0xA5], f"program at address 0 corrupted by post-saturation pulses: {hist}"


@cocotb.test()
async def test_boot_saturated_pulses_do_not_rewrite_1023(dut):
    """architecture.md saturation bullet: 'A no-op pulse writes nothing
    (byte 1023 keeps its value) but is still acked like any other'. Byte
    1023 is data offset 511 (isa.md: LOAD address = 512 + imm); extra
    pulses carry 0xC3 and must not land there."""
    chip = await new_chip(dut)
    a = Asm()
    a.load(0, 511)
    a.out(0)
    a.halt()
    img = a.image() + [0] * (1023 - len(a.image())) + [0x5A]
    await chip.reset()
    await chip.boot(img)
    for _ in range(3):
        echo = await chip.load_byte(0xC3)  # returns only after ack rise+clear
        assert echo == 0xFF, f"no-op pulse echo {echo}, spec 0xFF"
    await chip.clk(3)
    await chip.start()
    hist = await chip.wait_uo(1)
    assert hist == [0x5A], f"byte 1023 after extra pulses: {hist} (0xC3 = reading B)"


@cocotb.test()
async def test_boot_byte_order_low_first(dut):
    """architecture.md LOAD: 'low byte of instruction 0 first, matching
    FETCH_LO/FETCH_HI's own PC*2/PC*2+1 order'. LDI R0,0x0F = 0x200F streams
    0x0F,0x20; swapped it would decode as opcode 1 (HALT) and never OUT."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0x0F)
    a.out(0)
    a.halt()
    assert a.image()[:2] == [0x0F, 0x20]
    await chip.run(a)
    assert await chip.wait_uo(1) == [0x0F]


@cocotb.test()
async def test_uo_cleared_at_start(dut):
    """architecture.md: uo_out 'is explicitly re-cleared at the LOAD->FETCH_LO
    transition' so the last echo is not misread as OUT data."""
    chip = await new_chip(dut)
    await chip.reset()
    a = Asm()
    a.halt()
    img = a.image() + [0] * 5  # 7 bytes -> last echo 7
    await chip.boot(img)
    assert chip.uo() == 7
    await chip.clk(3)
    await chip.start()
    await chip.wait_until(lambda: chip.uo() == 0, 60, "uo_out cleared after START")
    for _ in range(200):
        await chip.clk()
        assert chip.uo() == 0, "uo_out changed though program never executes OUT"


@cocotb.test()
async def test_start_begins_execution_and_error_driver_gate(dut):
    """architecture.md LOAD: 'FSM exits LOAD on START's synchronized rising
    edge (PC=0, execution begins immediately), but uio_oe[6] stays 0 (input)
    until START's synchronized falling edge is also observed'; a SET of
    HOST_ERROR before the gate opens 'still latches internally and reaches
    the pin once the gate opens'.
    ASSUMPTION: holding START for 60 cycles is a legal pulse (the release
    deadline is 'TBD'); the gate opening within 20 cycles of release is a
    harness bound, not a spec number."""
    chip = await new_chip(dut)
    a = Asm()
    a.set(0, 1, PP)
    a.set(PIN_ERROR, 1)
    a.halt()
    await chip.reset()
    await chip.boot(a.image())
    await chip.clk(3)
    chip.mark()
    chip.set_ext(PIN_ERROR, 1)
    chip.in_load = False
    began = None
    for i in range(60):
        await chip.clk()
        assert chip.oe(PIN_ERROR) == "0", f"uio_oe[6] rose while host still drives START (cycle {i})"
        if began is None and chip.drive(0) == "1":
            began = i
    assert began is not None, "execution did not begin on START's rising edge (p0 never driven)"
    chip.set_ext(PIN_ERROR, 0)
    await chip.wait_until(lambda: chip.oe(PIN_ERROR) == "1", 20, "uio_oe[6] after START fall")
    await chip.clk(2)
    assert chip.drive(PIN_ERROR) == "1", "SET HOST_ERROR,1 issued before gate opened did not reach pin"


@cocotb.test()
async def test_error_gate_recloses_on_reset(dut):
    """architecture.md Reset values: the 'seen START fall' latch resets to 0
    'so HOST_ERROR's driver starts gated closed ... same rule applies after
    any reset'; uio_out resets 0, so an unset HOST_ERROR drives 0."""
    chip = await new_chip(dut)
    a = Asm()
    a.halt()
    await chip.run(a)
    await chip.wait_until(lambda: chip.oe(PIN_ERROR) == "1", 40, "gate open (first boot)")
    assert chip.drive(PIN_ERROR) == "0"
    await chip.reset()
    assert chip.oe(PIN_ERROR) == "0", "gate not closed by reset"
    await chip.boot(a.image())
    await chip.clk(20)
    assert chip.oe(PIN_ERROR) == "0"
    await chip.start()
    await chip.wait_until(lambda: chip.oe(PIN_ERROR) == "1", 40, "gate open (second boot)")
    assert not chip.load_violations


@cocotb.test()
async def test_host_go_after_start_is_not_a_boot_strobe(dut):
    """architecture.md: HOST_GO is the write-strobe 'During LOAD'; HOST_STATUS
    'During normal execution: firmware SETs it'. With a HALT program, GO
    pulses after START produce no echo and no ack."""
    chip = await new_chip(dut)
    a = Asm()
    a.halt()
    await chip.run(a)
    await chip.clk(20)
    for v in (0x12, 0x34, 0x56):
        chip.set_ui(v)
        chip.set_ext(PIN_GO, 1)
        for _ in range(30):
            await chip.clk()
            assert chip.uo() == 0 and chip.drive(PIN_STATUS) == "0"
        chip.set_ext(PIN_GO, 0)
        await chip.clk(30)
        assert chip.uo() == 0 and chip.drive(PIN_STATUS) == "0"


@cocotb.test()
async def test_reset_during_load_restarts_counter(dut):
    """architecture.md: 'Reset -> LOAD'; reset values apply to everything.
    Reset mid-boot (with HOST_GO high mid-handshake) restarts the byte
    counter: the first echo after reset is 1 again."""
    chip = await new_chip(dut)
    await chip.reset()
    for b in range(10):
        await chip.load_byte(b)
    chip.set_ui(0x77)
    chip.set_ext(PIN_GO, 1)
    await chip.clk(3)
    await chip.reset()
    a = Asm()
    a.ldi(1, 0x3C)
    a.out(1)
    a.halt()
    await chip.boot(a.image())  # echo checked from 1
    await chip.clk(3)
    await chip.start()
    assert await chip.wait_uo(1) == [0x3C]


def _dirty_prog():
    a = Asm()
    a.ldi(1, 0x99)
    a.ldi(2, 0x98)
    a.ldi(3, 0x97)
    a.cmp(1, 1)          # flag = 1
    a.set(0, 1, PP)
    a.set(2, 0, OD)
    a.set(PIN_ERROR, 1)
    a.set(PIN_STATUS, 1)
    a.call("sub")        # return-valid = 1
    a.label("sub")
    a.ldi(0, 0x77)
    a.out(0)
    a.label("spin")
    a.delay(300)
    a.jmp("spin")
    return a


def _clean_check_prog():
    """Fails (OUT 0xEE) unless flag == 0 and R0..R3 == 0 at entry."""
    a = Asm()
    a.beq("bad")                     # flag reset value 0 -> not taken
    a.cmp(0, 1); a.bne("bad")
    a.cmp(1, 2); a.bne("bad")
    a.cmp(2, 3); a.bne("bad")        # R0==R1==R2==R3
    a.ldi(0, 0)
    a.cmp(0, 1); a.bne("bad")        # ... == 0
    a.ldi(0, 0x3C); a.out(0); a.halt()
    a.label("bad")
    a.ldi(0, 0xEE); a.out(0); a.halt()
    return a


@cocotb.test()
async def test_reset_mid_execution_sweep(dut):
    """architecture.md Reset values: 'PC=0, R0-R3=0, compare/test flag=0 ...
    A reset asserting mid-fetch ... unconditionally returns to LOAD -- the
    in-flight fetch is simply discarded'. Reset is asserted at a sweep of
    offsets after START (covering FETCH_LO/FETCH_HI/EXECUTE of the first
    instructions and the middle of a DELAY); each time the pins must return
    to the reset state and a fresh boot must see clean registers and flag."""
    chip = await new_chip(dut)
    dirty, clean = _dirty_prog(), _clean_check_prog()
    for d in list(range(0, 16)) + [17, 19, 23, 29, 40, 60, 200, 450]:
        await chip.run(dirty)
        await chip.clk(d)
        await chip.reset()
        assert chip.uo() == 0, f"d={d}: uo_out {chip.s_uo}"
        assert chip.out_int() == 0, f"d={d}: uio_out {chip.s_out}"
        assert chip.oe_int() == 0x20, f"d={d}: uio_oe {chip.s_oe}"
        await chip.boot(clean.image())
        await chip.clk(3)
        await chip.start()
        hist = await chip.wait_uo(1, settle=20)
        assert hist == [0x3C], f"reset at d={d}: state not clean after reset ({hist})"
        assert chip.pin_hist(0) == ["Z"] and chip.pin_hist(2) == ["Z"], \
            f"d={d}: drive mode survived reset"


@cocotb.test()
async def test_registers_reset_to_zero_each(dut):
    """isa.md Register file: 'Reset value: all registers 0.' Each register is
    read out separately through the OUT handshake (zero would be invisible on
    uo_out's history)."""
    chip = await new_chip(dut)
    a = Asm()
    for r in range(4):
        fw_send(a, r)
    a.halt()
    await chip.run(_dirty_prog())
    await chip.clk(50)
    await chip.reset()
    await chip.boot(a.image())
    await chip.clk(3)
    await chip.start()
    vals = await chip.host_read_n(4)
    assert vals == [0, 0, 0, 0], f"R0..R3 after reset = {vals}"


# ===========================================================================
# Single opcodes
# ===========================================================================
@cocotb.test()
async def test_nop_any_low_bits(dut):
    """isa.md NOP row: 'the all-zero 16-bit word (and any word with just the
    top 5 bits zero ...) a valid NOP encoding'. NOPs with arbitrary low bits
    change no register or flag."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0x12)
    a.ldi(1, 0x34)
    a.cmp(0, 0)                       # flag 1
    for low in (0x7FF, 0x555, 0x2AA, 0x001, 0x400):
        a.nop(low)
    a.out(0)
    a.out(1)
    fw_out_flag(a, 2, 0x50)
    a.cmp(0, 1)                       # flag 0
    a.nop(0x7FF)
    fw_out_flag(a, 2, 0x60)
    a.halt()
    await chip.run(a)
    assert await chip.wait_uo(4) == [0x12, 0x34, 0x51, 0x60]


@cocotb.test()
async def test_ldi_all_registers(dut):
    """isa.md LDI: 'load immediate -> Rd', 8-bit value in imm(7:0).
    ASSUMPTION: imm(8), stated 'unused', is ignored (tested with it set)."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0xFF)
    a.ldi(1, 0x80)
    a.ldi(2, 0x01)
    a.ldi(3, 0x7F)
    for r in range(4):
        a.out(r)
    a.ldi(0, 0x00); a.out(0)
    a.ldi(1, 0xA5, imm8=1); a.out(1)
    a.halt()
    await chip.run(a)
    assert await chip.wait_uo(6) == [0xFF, 0x80, 0x01, 0x7F, 0x00, 0xA5]


@cocotb.test()
async def test_out_drives_all_bits_and_holds(dut):
    """isa.md OUT: 'drive Rd (all 8 bits) onto the host-facing parallel data
    bus in one shot'; architecture.md: uo_out is the DATA-OUT bus. HALT row:
    outputs hold their last-driven state."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(2, 0xA5); a.out(2)
    a.ldi(3, 0x5A); a.out(3)
    a.halt()
    await chip.run(a)
    assert await chip.wait_uo(2) == [0xA5, 0x5A]
    for _ in range(300):
        await chip.clk()
        assert chip.uo() == 0x5A


@cocotb.test()
async def test_out_back_to_back_three_cycles(dut):
    """architecture.md: 'Uniform 3 cycles/instruction'. Three consecutive
    OUTs change uo_out exactly 3 cycles apart."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0x01); a.ldi(1, 0x02); a.ldi(2, 0x03)
    a.out(0); a.out(1); a.out(2)
    a.halt()
    await chip.run(a)
    await chip.wait_uo(3)
    e = [c for (c, v) in chip.uo_edges() if v in (1, 2, 3)]
    assert len(e) == 3 and e[1] - e[0] == 3 and e[2] - e[1] == 3, f"OUT edge cycles {e}"


@cocotb.test()
async def test_in_reads_ui_in(dut):
    """isa.md IN: 'read the host-facing parallel data bus into Rd (all 8
    bits)'; architecture.md: ui_in is the DATA-IN bus. ui_in is changed long
    before the read so the stated 2-flop synchronizer has settled."""
    chip = await new_chip(dut)
    a = Asm()
    a.in_(1); a.out(1)
    a.wait(7, 1)
    a.in_(3); a.out(3)
    a.halt()
    await chip.run(a, ui=0xA5)
    assert await chip.wait_uo(1) == [0xA5]
    chip.set_ui(0x3C)
    await chip.clk(8)
    chip.set_ext(7, 1)
    assert await chip.wait_uo(2) == [0xA5, 0x3C]


@cocotb.test()
async def test_host_handshake_in_out_turnaround(dut):
    """architecture.md Host handshake: both directions host-initiated; 'back-
    to-back transfers in any order are safe with no minimum HOST_GO low
    time'; the host may change ui_in once it observes HOST_STATUS=1. The
    firmware is the spec's own IN and OUT sequences in a loop (the IN->OUT->
    IN turnaround that deadlocked in the earlier design)."""
    chip = await new_chip(dut)
    a = Asm()
    a.label("top")
    fw_recv(a, 0)
    fw_send(a, 0)
    a.jmp("top")
    await chip.run(a)
    for v in (0x00, 0xFF, 0xA5, 0x5A, 0x01, 0x80, 0x7E):
        await chip.host_write(v)
        got = await chip.host_read()
        assert got == v, f"echo of {v:#04x} came back {got!r}"


@cocotb.test()
async def test_set_push_pull(dut):
    """isa.md SET: mode 01 = 'configure push-pull and drive value
    immediately (oe=1, out=value)'; mode 00 = 'just drive value per whatever
    mode is currently configured'."""
    chip = await new_chip(dut)
    a = Asm()
    for v, m in ((1, PP), (0, PP), (1, KEEP), (0, KEEP), (1, KEEP)):
        a.set(0, v, m)
        a.delay(30)
    a.halt()
    await chip.run(a)
    await chip.clk(400)
    assert chip.pin_hist(0) == ["Z", "1", "0", "1", "0", "1"], chip.pin_hist(0)


@cocotb.test()
async def test_set_open_drain(dut):
    """isa.md SET: mode 10 = open-drain, 'value=1 -> release, oe=0 ...;
    value=0 -> drive low, oe=1, out=0'; mode 00 then follows open-drain."""
    chip = await new_chip(dut)
    a = Asm()
    for v, m in ((0, OD), (1, OD), (0, KEEP), (1, KEEP), (0, KEEP)):
        a.set(1, v, m)
        a.delay(30)
    a.halt()
    await chip.run(a, ext={1: 1})  # external pull-up
    await chip.clk(400)
    assert chip.pin_hist(1) == ["Z", "0", "Z", "0", "Z", "0"], chip.pin_hist(1)


@cocotb.test()
async def test_set_input_mode_is_sticky(dut):
    """isa.md SET: mode 11 = 'configure input (oe forced 0, value
    ignored)'; mode is 'sticky', so a later mode-00 SET on that pin drives
    nothing until a mode is configured again."""
    chip = await new_chip(dut)
    a = Asm()
    for v, m in ((1, PP), (1, INPUT), (1, KEEP), (0, KEEP), (0, PP)):
        a.set(3, v, m)
        a.delay(30)
    a.halt()
    await chip.run(a)
    await chip.clk(400)
    assert chip.pin_hist(3) == ["Z", "1", "Z", "0"], chip.pin_hist(3)


@cocotb.test()
async def test_unconfigured_pins_are_inputs(dut):
    """isa.md SET: 'Reset: all 5 protocol pins ... start in input mode ...
    a SET/OUTB targeting an unconfigured pin is a harmless no-op'."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0xFF)
    a.set(0, 1, KEEP)
    a.set(7, 0, KEEP)
    a.outb(0, 1, 0)
    a.outb(0, 3, 7)
    a.set(2, 1, PP)
    a.halt()
    await chip.run(a)
    await chip.wait_drive(2, "1", 200)
    await chip.clk(50)
    for p in (0, 1, 3, 7):
        assert chip.pin_hist(p, min_dwell=1) == ["Z"], f"unconfigured pin {p}: {chip.pin_hist(p, 1)}"


@cocotb.test()
async def test_pin_index_map_and_independence(dut):
    """architecture.md Pin map: pin_index 0-3 -> uio[3:0], 7 -> uio[7];
    'sticky per-pin drive-mode'. Configuring one pin touches no other."""
    chip = await new_chip(dut)
    for p in PROTOCOL_PINS:
        a = Asm()
        a.set(p, 1, PP)
        a.delay(20)
        a.set(p, 0, KEEP)
        a.halt()
        await chip.run(a)
        await chip.clk(150)
        assert chip.pin_hist(p) == ["Z", "1", "0"], f"pin_index {p}: {chip.pin_hist(p)}"
        for q in PROTOCOL_PINS:
            if q != p:
                assert chip.pin_hist(q, 1) == ["Z"], f"SET pin {p} disturbed uio[{q}]"
        assert chip.oe(PIN_GO) == "0" and chip.oe(PIN_STATUS) == "1"


@cocotb.test()
async def test_set_drive_is_atomic_at_pin(dut):
    """isa.md SET: mode change and value happen in the same SET ('configure
    push-pull and drive value immediately (oe=1, out=value)'). Checks that no
    intermediate drive state appears even for one cycle.
    ASSUMPTION: 'immediately' means oe and out reach the pin on the same
    cycle (a one-cycle 0 glitch before driving 1 would matter on a real bus)."""
    chip = await new_chip(dut)
    a = Asm()
    a.set(0, 1, PP); a.delay(10)
    a.set(0, 1, INPUT); a.delay(10)
    a.set(0, 1, PP); a.delay(10)
    a.set(1, 0, OD); a.delay(10)
    a.set(1, 1, KEEP); a.delay(10)
    a.halt()
    await chip.run(a, ext={1: 1})
    await chip.clk(150)
    assert chip.pin_hist(0, min_dwell=1) == ["Z", "1", "Z", "1"], chip.pin_hist(0, 1)
    assert chip.pin_hist(1, min_dwell=1) == ["Z", "0", "Z"], chip.pin_hist(1, 1)


@cocotb.test()
async def test_set_fixed_role_pins(dut):
    """architecture.md Pin map rule (1): 'a pin's oe is fixed by its role
    (hardwired for 4/5/6 ...), never by which opcode currently targets it,
    so SET/OUTB on a hardwired-input pin is a safe no-op'; SET on 5 is the
    'primary use: drive status'. isa.md SET row: 'On the fixed-role pins
    the mode field is ignored: their direction is set by role ..., so
    SET/OUTB to pin_index 5 (HOST_STATUS) or 6 (HOST_ERROR) drives only the
    value, and to pin_index 4 (HOST_GO, always an input) has no effect.'
    So every mode (00/01/10/11) on 5/6 drives the value with oe=1, leaves
    no sticky mode behind for a later OUTB, and nothing reaches pin 4."""
    chip = await new_chip(dut)
    a = Asm()
    a.set(PIN_GO, 1, PP)
    a.set(PIN_GO, 1, KEEP)
    a.set(PIN_STATUS, 1, KEEP); a.delay(20)
    a.set(PIN_STATUS, 0, KEEP); a.delay(20)
    a.set(PIN_STATUS, 1, PP); a.delay(20)
    a.set(PIN_STATUS, 0, INPUT); a.delay(20)   # mode ignored: drives 0, oe stays 1
    a.set(PIN_STATUS, 1, INPUT); a.delay(20)   # drives 1 (value not ignored)
    a.set(PIN_STATUS, 0, OD); a.delay(20)      # drives 0
    a.set(PIN_STATUS, 1, OD); a.delay(20)      # drives 1 (not a release)
    a.ldi(0, 0x00)
    a.outb(0, PIN_STATUS, 0); a.delay(20)      # OUTB after OD/INPUT SETs: drives 0
    a.ldi(0, 0x80)
    a.outb(0, PIN_STATUS, 7); a.delay(20)      # drives 1
    a.set(PIN_STATUS, 0, KEEP); a.delay(20)
    a.delay(40)                                # HOST_ERROR gate (START fall) open by now
    a.set(PIN_ERROR, 1, INPUT); a.delay(20)    # drives 1, oe stays 1
    a.set(PIN_ERROR, 0, OD); a.delay(20)       # drives 0
    a.set(PIN_ERROR, 1, OD); a.delay(20)       # drives 1 (not a release)
    a.ldi(1, 0x01)
    a.outb(1, PIN_GO, 0)
    a.set(PIN_GO, 1, OD)
    a.set(PIN_GO, 0, INPUT)
    a.halt()
    await chip.run(a)
    oe5_low = oe4_high = False
    for _ in range(700):
        await chip.clk()
        oe4_high |= chip.oe(PIN_GO) != "0"
        oe5_low |= chip.oe(PIN_STATUS) != "1"
    assert not oe4_high, "uio_oe[4] (HOST_GO, hardwired input) went high"
    assert not oe5_low, "uio_oe[5] (HOST_STATUS, hardwired output) went low"
    h = chip.pin_hist(PIN_STATUS)
    assert h == ["0", "1", "0", "1", "0", "1", "0", "1", "0", "1", "0"], f"HOST_STATUS history {h}"
    e = chip.pin_hist(PIN_ERROR)
    assert e[-3:] == ["1", "0", "1"] and "Z" not in e[e.index("1"):], f"HOST_ERROR history {e}"
    assert chip.drive(PIN_STATUS) == "0"
    assert chip.drive(PIN_ERROR) == "1"
    assert chip.pin_hist(PIN_GO, 1) == ["Z"]


@cocotb.test()
async def test_outb_bit_select_push_pull(dut):
    """isa.md OUTB: 'drive pin = Rd[bit] (bit is 0 or 7)'; obeys the sticky
    push-pull mode. Rd is not modified. Encoding: isa.md sub-field packing,
    'For OUTB/INB, bit_select = 0 selects bit 0 and 1 selects bit 7'."""
    chip = await new_chip(dut)
    a = Asm()
    a.set(0, 0, PP); a.delay(20)
    a.ldi(0, 0x80)
    a.outb(0, 0, 7); a.delay(20)   # 1
    a.outb(0, 0, 0); a.delay(20)   # 0
    a.ldi(0, 0x01)
    a.outb(0, 0, 0); a.delay(20)   # 1
    a.outb(0, 0, 7); a.delay(20)   # 0
    a.out(0)
    a.halt()
    await chip.run(a)
    assert await chip.wait_uo(1) == [0x01]
    assert chip.pin_hist(0) == ["Z", "0", "1", "0", "1", "0"], chip.pin_hist(0)


@cocotb.test()
async def test_outb_open_drain(dut):
    """isa.md OUTB: 'on an open-drain pin, Rd[bit]=1 releases and Rd[bit]=0
    drives low'."""
    chip = await new_chip(dut)
    a = Asm()
    a.set(3, 1, OD); a.delay(20)
    a.ldi(1, 0x7E)
    a.outb(1, 3, 0); a.delay(20)   # 0 -> low
    a.ldi(1, 0x81)
    a.outb(1, 3, 7); a.delay(20)   # 1 -> release
    a.outb(2, 3, 7); a.delay(20)   # R2 = 0 -> low
    a.halt()
    await chip.run(a, ext={3: 1})
    await chip.clk(200)
    assert chip.pin_hist(3) == ["Z", "0", "Z", "0"], chip.pin_hist(3)


@cocotb.test()
async def test_outb_noop_on_input_and_host_go(dut):
    """isa.md OUTB: 'on an unconfigured (input) pin it's a no-op';
    architecture.md matrix: OUTB on pin 4 'no-op, oe hardwired 0'."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0xFF)
    a.set(1, 0, PP); a.delay(20)
    a.set(1, 0, INPUT); a.delay(20)
    a.outb(0, 1, 7)
    a.outb(0, 2, 0)
    a.outb(0, PIN_GO, 0)
    a.outb(0, PIN_GO, 7)
    a.set(0, 1, PP)
    a.halt()
    await chip.run(a)
    await chip.wait_drive(0, "1", 300)
    await chip.clk(30)
    assert chip.pin_hist(1) == ["Z", "0", "Z"], chip.pin_hist(1)
    assert chip.pin_hist(2, 1) == ["Z"]
    assert chip.pin_hist(PIN_GO, 1) == ["Z"]


@cocotb.test()
async def test_outb_status_and_error_pins(dut):
    """architecture.md matrix: OUTB on 5 and 6 is 'valid: drive from a live
    bit'."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0x80)
    a.outb(0, PIN_STATUS, 7); a.delay(20)
    a.outb(0, PIN_STATUS, 0); a.delay(20)
    a.delay(60)
    a.outb(0, PIN_ERROR, 7); a.delay(20)
    a.halt()
    await chip.run(a)
    await chip.clk(250)
    assert chip.pin_hist(PIN_STATUS)[:3] == ["0", "1", "0"], chip.pin_hist(PIN_STATUS)
    assert chip.drive(PIN_ERROR) == "1"


@cocotb.test()
async def test_inb_sets_one_bit(dut):
    """isa.md INB: 'set Rd[bit] = pin's current value, other bits of Rd
    untouched'. Pins: p1=1, p2=0, p7=1 (stable long before the reads)."""
    chip = await new_chip(dut)
    a = Asm()
    cases = [(0, 0x5A, 1, 0, 0x5B), (0, 0x5A, 1, 7, 0xDA), (1, 0xFF, 2, 0, 0xFE),
             (1, 0xFF, 2, 7, 0x7F), (2, 0x00, 7, 7, 0x80), (3, 0x00, 7, 0, 0x01),
             (0, 0xFF, 1, 0, 0xFF), (1, 0x00, 2, 7, 0x00)]
    for (r, init, pin, bit, _exp) in cases:
        a.ldi(r, init)
        a.inb(r, pin, bit)
        fw_send(a, r)
    a.halt()
    await chip.run(a, ext={1: 1, 2: 0, 7: 1})
    got = await chip.host_read_n(len(cases))
    assert got == [c[4] for c in cases], f"INB results {[hex(g) for g in got]}"


@cocotb.test()
async def test_inb_host_go(dut):
    """architecture.md matrix: INB on pin 4 'valid: sample without
    blocking'."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(2, 0x41)
    a.inb(2, PIN_GO, 0)       # GO = 0 at start (host rule) -> 0x40
    a.out(2)
    a.wait(7, 1)
    a.ldi(3, 0x02)
    a.inb(3, PIN_GO, 7)       # GO = 1 -> 0x82
    a.out(3)
    a.halt()
    await chip.run(a)
    assert await chip.wait_uo(1) == [0x40]
    chip.set_ext(PIN_GO, 1)
    await chip.clk(8)
    chip.set_ext(7, 1)
    assert await chip.wait_uo(2) == [0x40, 0x82]


@cocotb.test()
async def test_inb_and_wait_status_loopback(dut):
    """architecture.md matrix: WAIT/INB on pin 5 'reads back own driven value
    -- self-loopback ... defined'. Rule (2): uio_in reflects the pad even on
    a pin the chip drives (emulated by the harness pad model). DELAYs give
    the 2-flop synchronizer time; no loopback latency is stated."""
    chip = await new_chip(dut)
    a = Asm()
    a.set(PIN_STATUS, 1)
    a.delay(20)
    a.ldi(0, 0x10)
    a.inb(0, PIN_STATUS, 0)          # -> 0x11
    a.out(0)
    a.wait(PIN_STATUS, 1, 31, 1)     # met -> flag 0
    a.beq("bad")
    a.wait(PIN_STATUS, 0, 20, 0)     # STATUS is 1 -> timeout -> flag 1
    a.bne("bad")
    a.set(PIN_STATUS, 0)
    a.delay(20)
    a.ldi(1, 0xFF)
    a.inb(1, PIN_STATUS, 7)          # -> 0x7F
    a.out(1)
    a.halt()
    a.label("bad")
    a.ldi(0, 0xEE); a.out(0); a.halt()
    await chip.run(a)
    assert await chip.wait_uo(2) == [0x11, 0x7F]


@cocotb.test()
async def test_shift(dut):
    """isa.md SHIFT: 'shift Rd by exactly 1 bit; imm(0) = direction, 0 =
    shift left, 1 = shift right ... Vacated bit is always 0 (plain logical
    shift -- no rotate'."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0x81); a.shift(0, False); a.out(0)        # 0x02
    a.ldi(0, 0x81); a.shift(0, True); a.out(0)         # 0x40
    a.ldi(1, 0xFF); a.shift(1, False); a.out(1)        # 0xFE
    a.shift(1, True); a.out(1)                         # 0x7F
    a.ldi(2, 0x01)
    for _ in range(7):
        a.shift(2, False)
    a.out(2)                                           # 0x80
    a.shift(2, False); a.out(2)                        # 0x00
    a.ldi(3, 0x80)
    for _ in range(7):
        a.shift(3, True)
    a.out(3)                                           # 0x01
    a.shift(3, True); a.out(3)                         # 0x00
    a.halt()
    await chip.run(a)
    assert await chip.wait_uo(8) == [0x02, 0x40, 0xFE, 0x7F, 0x80, 0x00, 0x01, 0x00]


@cocotb.test()
async def test_shift_does_not_touch_flag(dut):
    """isa.md SHIFT: 'SHIFT does not touch the shared flag' (rotate-through-
    flag was considered and rejected)."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0x01)
    a.ldi(1, 0x80)
    a.cmp(0, 0)              # flag 1
    a.shift(0, False)        # shifts out 0
    a.shift(0, True)
    fw_out_flag(a, 2, 0x10)  # 0x11
    a.cmp(0, 1)              # flag 0
    a.shift(1, False)        # shifts out a 1
    fw_out_flag(a, 2, 0x20)  # 0x20
    a.halt()
    await chip.run(a)
    assert await chip.wait_uo(2) == [0x11, 0x20]


@cocotb.test()
async def test_test_bit_every_index(dut):
    """isa.md TEST-bit: 'test bit N of Rd, set flag; imm = bit_index(3).
    Flag polarity: flag=1 iff the tested bit=1'."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0xA5)
    for b in range(8):
        a.test(0, b)
        fw_out_flag(a, 1, 0x10 * (b + 1))
    a.ldi(2, 0x5A)
    a.test(2, 0); fw_out_flag(a, 3, 0x90)
    a.test(2, 7); fw_out_flag(a, 3, 0xA0)
    a.test(2, 6); fw_out_flag(a, 3, 0xB0)
    a.halt()
    await chip.run(a)
    exp = [0x10 * (b + 1) | ((0xA5 >> b) & 1) for b in range(8)] + [0x90, 0xA0, 0xB1]
    assert await chip.wait_uo(len(exp)) == exp


@cocotb.test()
async def test_cmp_equality(dut):
    """isa.md CMP: 'Equality only: sets the shared flag to (Rd==Rs)'.
    Pairs differing in a single (top or bottom) bit must compare unequal."""
    chip = await new_chip(dut)
    cases = [(0, 1, 0x00, 0x00, 1), (0, 1, 0x80, 0x00, 0), (0, 1, 0x01, 0x00, 0),
             (2, 3, 0xFF, 0xFF, 1), (2, 3, 0xFF, 0x7F, 0), (3, 2, 0xFE, 0xFF, 0),
             (1, 2, 0x5A, 0x5A, 1), (1, 1, 0x33, 0x33, 1), (3, 0, 0x10, 0x01, 0)]
    a = Asm()
    exp = []
    for i, (ra, rb, x, y, f) in enumerate(cases):
        s = next(r for r in range(4) if r not in (ra, rb))
        a.ldi(ra, x)
        if rb != ra:
            a.ldi(rb, y)
        a.cmp(ra, rb)
        fw_out_flag(a, s, 0x10 * (i + 1))
        exp.append(0x10 * (i + 1) | f)
    a.halt()
    await chip.run(a)
    assert await chip.wait_uo(len(exp)) == exp


@cocotb.test()
async def test_flag_last_writer_wins(dut):
    """isa.md CMP: 'Same physical flag as TEST-bit -- one flag register,
    last write wins'."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0x02)
    a.ldi(1, 0x03)
    a.cmp(0, 0); a.test(0, 0); fw_out_flag(a, 3, 0x10)   # 1 then 0 -> 0x10
    a.test(0, 1); a.cmp(0, 1); fw_out_flag(a, 3, 0x20)   # 1 then 0 -> 0x20
    a.cmp(0, 1); a.test(0, 1); fw_out_flag(a, 3, 0x30)   # 0 then 1 -> 0x31
    a.test(0, 0); a.cmp(1, 1); fw_out_flag(a, 3, 0x40)   # 0 then 1 -> 0x41
    a.halt()
    await chip.run(a)
    assert await chip.wait_uo(4) == [0x10, 0x20, 0x31, 0x41]


@cocotb.test()
async def test_branch_conditions(dut):
    """isa.md Branch format: cond 00 always, 01 if flag set (BEQ), 10 if
    flag clear (BNE). architecture.md: flag resets to 0."""
    chip = await new_chip(dut)
    a = Asm()
    a.beq("l1")                         # flag 0: not taken
    a.ldi(0, 1); a.out(0)
    a.label("l1")
    a.bne("l2")                         # taken
    a.ldi(0, 0xEE); a.out(0)
    a.label("l2")
    a.ldi(0, 2); a.out(0)
    a.cmp(0, 0)                         # flag 1
    a.beq("l3")                         # taken
    a.ldi(0, 0xEE); a.out(0)
    a.label("l3")
    a.bne("l4")                         # not taken
    a.ldi(0, 3); a.out(0)
    a.label("l4")
    a.jmp("l5")
    a.ldi(0, 0xEE); a.out(0)
    a.label("l5")
    a.ldi(0, 4); a.out(0)
    a.halt()
    await chip.run(a)
    assert await chip.wait_uo(4) == [1, 2, 3, 4]


@cocotb.test()
async def test_jump_target_is_word_index(dut):
    """architecture.md: 'the branch/loop/call addr(9) field is a word index,
    not a byte address -- the fetch unit does the x2'. Literal targets 7 and
    4 (odd and even word indices)."""
    chip = await new_chip(dut)
    a = Asm()
    a.jmp(7)                           # 0
    a.ldi(0, 0xE1); a.out(0); a.halt()  # 1-3
    a.ldi(0, 0x44); a.out(0); a.halt()  # 4-6
    a.ldi(0, 0x77); a.out(0)           # 7-8
    a.jmp(4)                           # 9
    assert len(a.words()) == 10
    await chip.run(a)
    assert await chip.wait_uo(2) == [0x77, 0x44]


@cocotb.test()
async def test_call_ret_and_flag(dut):
    """isa.md: cond 11 = 'call (push return address ... then jump)'; RET
    'jumps to return-addr register'. CALL itself does not write the flag,
    but a subroutine's CMP does ('CALL also silently clobbers the shared
    flag if the subroutine uses CMP')."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(1, 1)
    a.cmp(0, 0)                       # flag 1
    a.call("s1")
    fw_out_flag(a, 2, 0x10)           # 0x11 (preserved)
    a.cmp(0, 0)
    a.call("s2")
    fw_out_flag(a, 2, 0x20)           # 0x20 (clobbered by s2's CMP)
    a.ldi(2, 0x5A); a.out(2)
    a.halt()
    a.label("s1")
    a.ldi(3, 0xC3); a.out(3); a.ret()
    a.label("s2")
    a.cmp(0, 1)
    a.ldi(3, 0xC4); a.out(3); a.ret()
    await chip.run(a)
    assert await chip.wait_uo(5) == [0xC3, 0x11, 0xC4, 0x20, 0x5A]


@cocotb.test()
async def test_nested_call_overwrites_return(dut):
    """isa.md: 'A second CALL while one return address is already pending
    silently overwrites it -- no protection'. Outer->inner->RET returns
    after the inner CALL; the outer RET then returns there again (the
    register still holds it), looping forever; the main path (0x11) is
    never reached.
    ASSUMPTION: RET does not clear the return-address register (isa.md: an
    orphan RET jumps to 'whatever the return-address register happens to
    hold')."""
    chip = await new_chip(dut)
    a = Asm()
    a.set(0, 0, PP)
    a.call("outer")
    a.ldi(0, 0x11); a.out(0); a.halt()
    a.label("outer")
    a.call("inner")
    a.ldi(0, 0x22); a.out(0)
    a.set(0, 1, KEEP)
    a.set(0, 0, KEEP)
    a.ret()
    a.label("inner")
    a.ret()
    await chip.run(a)
    await chip.clk(600)
    assert chip.uo_hist() == [0x22], f"uo history {chip.uo_hist()} (0x11 = return stack behaviour)"
    rises = [c for (c, s) in chip.pin_edges(0) if s == "1"]
    assert len(rises) >= 10, f"loop not repeating: {len(rises)} marker pulses"


@cocotb.test()
async def test_orphan_ret_jumps_to_reset_return_address(dut):
    """isa.md: orphan RET 'would otherwise silently jump to whatever the
    return-address register happens to hold (its reset value, 0, if nothing's
    ever called yet)'; architecture.md: return-address register resets to 0.
    SET/SET/RET from word 0 therefore repeats with a 9-cycle period."""
    chip = await new_chip(dut)
    a = Asm()
    a.set(0, 1, PP)
    a.set(0, 0, KEEP)
    a.ret()
    a.ldi(0, 0xEE); a.out(0)
    a.halt()
    await chip.run(a)
    await chip.clk(400)
    assert chip.uo_hist() == [], f"fell through orphan RET: {chip.uo_hist()}"
    rises = [c for (c, s) in chip.pin_edges(0) if s == "1"]
    assert len(rises) >= 20
    d = {rises[i + 1] - rises[i] for i in range(1, len(rises) - 1)}
    assert d == {9}, f"loop period {d}, spec 3 x 3 cycles"


@cocotb.test()
async def test_loop_counts(dut):
    """isa.md LOOP: 'Decrements Rd, branches to addr if nonzero'; trailing 2
    bits select Rd. isa.md LOOP row: 'Rd is 8 bits and the decrement wraps:
    entered with Rd = 0, LOOP writes 255 and branches, so a loop body runs
    256 times.'"""
    chip = await new_chip(dut)
    for (r, n, expect) in [(1, 1, 1), (1, 2, 2), (0, 5, 5), (3, 3, 3), (2, 255, 255), (2, 0, 256)]:
        s = (r + 1) % 4
        a = Asm()
        a.set(0, 0, PP)
        a.ldi(r, n)
        a.ldi(s, 0)
        a.label("top")
        a.set(0, 1, KEEP)
        a.set(0, 0, KEEP)
        a.loop(r, "top")
        a.cmp(r, s)                   # Rd must now be 0
        fw_out_flag(a, s, 0x70)
        a.halt()
        await chip.run(a)
        hist = await chip.wait_uo(1, timeout=10000)
        rises = [c for (c, st) in chip.pin_edges(0) if st == "1"]
        assert len(rises) == expect, f"LOOP R{r} from {n}: {len(rises)} iterations, spec {expect}"
        if len(rises) > 2:
            assert {rises[i + 1] - rises[i] for i in range(len(rises) - 1)} == {9}
        assert hist == [0x71], f"R{r} not 0 after LOOP exit ({hist})"
    # isa.md LOOP row: 'entered with Rd = 0, LOOP writes 255 and branches'.
    for r in range(4):
        a = Asm()
        a.ldi(r, 0)
        a.loop(r, "t")
        a.ldi(r, 0xEE); a.out(r); a.halt()      # fall-through = wrong
        a.label("t")
        a.out(r)                                # must show 255
        a.halt()
        await chip.run(a)
        hist = await chip.wait_uo(1, timeout=500)
        assert hist == [0xFF], f"LOOP R{r} entered at 0: {hist}, spec writes 255 and branches"


@cocotb.test()
async def test_loop_does_not_touch_flag(dut):
    """isa.md LOOP: 'Does not touch the shared flag'."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(1, 3)
    a.cmp(0, 0)                       # flag 1
    a.label("l1")
    a.loop(1, "l1")                   # taken twice, falls once
    fw_out_flag(a, 2, 0x10)           # 0x11
    a.ldi(1, 2)
    a.cmp(0, 1)                       # flag 0
    a.label("l2")
    a.loop(1, "l2")
    fw_out_flag(a, 2, 0x20)           # 0x20
    a.halt()
    await chip.run(a)
    assert await chip.wait_uo(2) == [0x11, 0x20]


@cocotb.test()
async def test_halt_freezes_everything(dut):
    """isa.md HALT: 'Freezes the FSM -- stops fetching, no further state
    changes. All outputs (including uio_oe) hold their last-driven state;
    does not release pins or touch HOST_STATUS ... HOST_GO does not resume
    from HALT'."""
    chip = await new_chip(dut)
    a = Asm()
    a.set(0, 1, PP)
    a.set(2, 0, OD)
    a.set(PIN_STATUS, 1)
    a.ldi(0, 0x77)
    a.out(0)
    a.halt()
    a.set(0, 0, KEEP)
    a.ldi(0, 0x11); a.out(0)
    a.halt()
    await chip.run(a)
    assert await chip.wait_uo(1) == [0x77]
    await chip.clk(40)
    snap = (chip.s_uo, chip.s_oe, chip.s_out)
    for i in range(6):
        chip.set_ext(PIN_GO, i & 1)
        chip.set_ui(0x5A ^ i)
        chip.set_ext(1, i & 1)
        for _ in range(100):
            await chip.clk()
            assert (chip.s_uo, chip.s_oe, chip.s_out) == snap, \
                f"state changed after HALT: {snap} -> {(chip.s_uo, chip.s_oe, chip.s_out)}"
    assert chip.drive(0) == "1" and chip.drive(2) == "0" and chip.drive(PIN_STATUS) == "1"


@cocotb.test()
async def test_reserved_opcodes_execute_as_nop(dut):
    """isa.md: 'Reserved/unused opcode encodings execute as NOP'; opcode
    table '25-31 reserved ... Executes as NOP' (21-24 are the serial
    engine's since 2026-11-04 and are tested below). Each reserved opcode (with
    all-ones and all-zeros operand bits) leaves R0-R3, the flag and the pins
    untouched, falls through to the next word and takes 3 cycles
    (architecture.md: NOP is 3 cycles). The sticky illegal-opcode flag is
    not pin-visible and is not tested."""
    chip = await new_chip(dut)
    fails = []
    for op in RESERVED_OPS:
        for low in (0x7FF, 0x000):
            flag = (op + low) & 1
            a = Asm()
            a.ldi(0, 0x11); a.ldi(1, 0x22); a.ldi(2, 0x33); a.ldi(3, 0x44)
            a.set(0, 0, PP); a.delay(10)
            a.cmp(0, 0) if flag else a.cmp(0, 1)
            a.set(0, 1, KEEP)
            a.raw(enc_0op(op, low))
            a.set(0, 0, KEEP)
            for r in range(4):
                a.out(r)
            a.beq("t")
            a.ldi(0, 0x50); a.out(0); a.halt()
            a.label("t")
            a.ldi(0, 0x51); a.out(0); a.halt()
            await chip.run(a)
            await chip.wait_drive(0, "0", 500)
            r_ = await chip.wait_drive(0, "1", 500)
            f_ = await chip.wait_drive(0, "0", 500)
            hist = await chip.wait_uo(5, timeout=500, settle=20)
            exp = [0x11, 0x22, 0x33, 0x44, 0x50 | flag]
            other = [p for p in (1, 2, 3, 7) if chip.pin_hist(p, 1) != ["Z"]]
            if f_ - r_ != 6 or hist != exp or other:
                fails.append((op, hex(low), f_ - r_, hist, other))
    assert not fails, f"reserved opcodes not NOP (op, low, gap!=6, uo, pins): {fails}"


# ===========================================================================
# Timing (architecture.md Pipeline)
# ===========================================================================
def _uniform_cases():
    nopskip = lambda a, br: (br(a), a.nop(), a.label("tgt"))  # noqa: E731
    c = [
        ("NOP", None, lambda a: a.nop(), None),
        ("LDI", None, lambda a: a.ldi(1, 0x55), None),
        ("SET other pin", None, lambda a: a.set(1, 1, PP), None),
        ("OUT", None, lambda a: a.out(1), None),
        ("IN", None, lambda a: a.in_(1), None),
        ("SHIFT", None, lambda a: a.shift(1, False), None),
        ("TEST-bit", None, lambda a: a.test(1, 3), None),
        ("OUTB (input pin)", None, lambda a: a.outb(1, 2, 0), None),
        ("INB", None, lambda a: a.inb(1, 1, 0), None),
        ("LOAD", None, lambda a: a.load(1, 5), None),
        ("STORE", None, lambda a: a.store(1, 5), None),
        ("LOADX", None, lambda a: a.loadx(1, 2), None),
        ("CMP", None, lambda a: a.cmp(1, 2), None),
        ("JMP taken", None, lambda a: nopskip(a, lambda x: x.jmp("tgt")), None),
        ("BEQ not taken", None, lambda a: (a.beq("far")), None),
        ("BNE taken", None, lambda a: nopskip(a, lambda x: x.bne("tgt")), None),
        ("BEQ taken", lambda a: a.cmp(0, 0), lambda a: nopskip(a, lambda x: x.beq("tgt")), None),
        ("BNE not taken", lambda a: a.cmp(0, 0), lambda a: a.bne("far"), None),
        ("CALL", None, lambda a: nopskip(a, lambda x: x.call("tgt")), None),
        ("LOOP falls through", lambda a: a.ldi(1, 1), lambda a: a.loop(1, "far"), None),
        ("LOOP taken", lambda a: a.ldi(1, 2), lambda a: nopskip(a, lambda x: x.loop(1, "tgt")), None),
        ("DELAY 0", None, lambda a: a.delay(0, 0), None),
        # isa.md: reserved range is now `25-31` (parent edit 2026-11-04:
        # 21-24 became the serial engine's opcodes).
        ("reserved 25", None, lambda a: a.raw(enc_0op(25, 0x7FF)), None),
        # isa.md rows 19/20 are reg-reg ops; architecture.md: 'Uniform 3
        # cycles/instruction for everything -- ... register ops, all of it'.
        ("XOR", None, lambda a: a.xor(1, 2), None),
        ("XOR Rd,Rd", None, lambda a: a.xor(1, 1), None),
        ("ADD no carry", None, lambda a: a.add(1, 2), None),
        ("ADD carry", lambda a: (a.ldi(1, 0xFF), a.ldi(2, 0x01)), lambda a: a.add(1, 2), None),
        ("reserved 31", None, lambda a: a.raw(enc_0op(31, 0x123)), None),
        # isa.md rows 21/24: SCFG and SRD do not block (only STX and SRX
        # 'hold EXECUTE', architecture.md Serial engine), so 3 cycles.
        # Engine registers are 0 here (engine off); SCFG writes 0 to the
        # polynomial / an empty command, SRD reads status and CRC.
        ("SCFG k=6", None, lambda a: a.scfg(1, 6), None),
        ("SCFG cmd 0", None, lambda a: a.scfg(1, 15), None),
        ("SRD status", None, lambda a: a.srd(1, 0), None),
        ("SRD crc hi", None, lambda a: a.srd(1, 2), None),
        ("SRD other k", None, lambda a: a.srd(1, 9), None),
    ]
    return [(n, p, m, 6) for (n, p, m, _t) in c]


@cocotb.test()
async def test_uniform_three_cycles_per_instruction(dut):
    """architecture.md: 'Uniform 3 cycles/instruction for everything -- NOP,
    BRANCH, LOOP, CMP, SET, register ops, all of it'; LOAD/LOADX trailing
    writeback 'doesn't weaken this: FSM occupancy is still exactly 3'.
    Gap between two marker SETs around one instruction = 3 + 3. A branch
    that should be taken skips a NOP (which would add 3). 'far' is a trap
    label after HALT; reaching it means a wrongly-taken branch."""
    chip = await new_chip(dut)
    fails = []
    for (name, pre, mid, exp) in _uniform_cases():
        def tail(a):
            a.label("far")
            a.set(0, 1, KEEP)
            a.halt()
        prog = gap_prog(mid, pre=pre, tail=tail)
        try:
            g = await measure_gap(chip, prog, timeout=300)
        except AssertionError as e:
            fails.append((name, str(e)))
            continue
        if g != exp:
            fails.append((name, g))
    # RET: SET1 / CALL sub / SET0 ... sub: RET -> 3 + 3 + 3
    prog = gap_prog(lambda a: a.call("sub"), tail=lambda a: (a.label("sub"), a.ret()))
    g = await measure_gap(chip, prog, timeout=300)
    if g != 9:
        fails.append(("CALL+RET", g))
    assert not fails, f"instructions not 3 cycles (name, gap): {fails}"


@cocotb.test()
async def test_delay_exact_cycles(dut):
    """architecture.md: 'DELAY stays in EXECUTE ... (3+N cycles, N from the
    immediate)'; isa.md DELAY: 'cycles = mantissa << (exponent*5)', Rd field
    is the exponent. Gap = SET(3) + DELAY(3+N)."""
    chip = await new_chip(dut)
    fails = []
    for (m, e) in [(0, 0), (1, 0), (2, 0), (100, 0), (511, 0), (0, 1), (0, 3),
                   (1, 1), (3, 1), (511, 1), (1, 2), (2, 2), (1, 3)]:
        n = delay_cycles(m, e)
        g = await measure_gap(chip, gap_prog(lambda a, m=m, e=e: a.delay(m, e)), timeout=n + 200)
        if g != 6 + n:
            fails.append(((m, e), g, 6 + n))
    assert not fails, f"DELAY (mant,exp), gap, expected: {fails}"


@cocotb.test()
async def test_repurposed_rd_fields_leave_registers(dut):
    """isa.md: DELAY/WAIT/SET 'repurpose their otherwise-unused Rd field ...
    rather than a register select'; DELAY 'doesn't touch the regfile'."""
    chip = await new_chip(dut)
    a = Asm()
    for r in range(4):
        a.ldi(r, 0x10 * (r + 1))
    a.delay(2, 1)
    a.delay(0, 2)
    a.delay(0, 3)
    a.delay(5, 0)
    a.wait(1, 1, 1, 2)       # pin1 high -> met
    a.wait(2, 1, 3, 3)       # pin2 high -> met
    a.set(3, 1, PP)
    a.set(3, 0, OD)
    a.set(3, 1, INPUT)
    for r in range(4):
        a.out(r)
    a.halt()
    await chip.run(a, ext={1: 1, 2: 1})
    assert await chip.wait_uo(4) == [0x10, 0x20, 0x30, 0x40]


@cocotb.test()
async def test_wait_blocks_on_level(dut):
    """isa.md WAIT: 'block until pin==level'. architecture.md synchronizer
    paragraph, 'Exact reaction, at the pins: count rising clock edges from
    the first one that samples the pin at its new level as edge 1. Edges 1
    and 2 load the two synchronizer flops, and WAIT leaves EXECUTE at edge
    3. ... a SET straight after the WAIT changes its pin at edge 6. The same
    holds for a rising and a falling level.' Harness timing: the input is
    changed just after falling edge t, so edge 1 is the next rising edge and
    a pin change at edge 6 is first sampled at falling edge t+6. Checked on
    a protocol pin (1 and 7) and on HOST_GO, since 'every uio input goes
    through the same standard 2-flop synchronizer, uniformly'."""
    chip = await new_chip(dut)
    for pin in (1, 7, PIN_GO):
        a = Asm()
        a.set(0, 1, PP)
        a.wait(pin, 1)
        a.set(0, 0, KEEP)
        a.wait(pin, 0)
        a.set(0, 1, KEEP)
        a.halt()
        await chip.run(a)
        await chip.wait_drive(0, "1", 200)
        for _ in range(300):
            await chip.clk()
            assert chip.drive(0) == "1", f"WAIT p{pin},1 did not block"
        chip.set_ext(pin, 1)
        t1 = chip.cycle
        l1 = await chip.wait_drive(0, "0", 50) - t1
        for _ in range(300):
            await chip.clk()
            assert chip.drive(0) == "0", f"WAIT p{pin},0 did not block"
        chip.set_ext(pin, 0)
        t2 = chip.cycle
        l2 = await chip.wait_drive(0, "1", 50) - t2
        assert l1 == 6, f"pin {pin}: rising-level reaction {l1} cycles, spec edge 6"
        assert l2 == 6, f"pin {pin}: falling-level reaction {l2} cycles, spec edge 6"


@cocotb.test()
async def test_wait_timeout_exact_cycles(dut):
    """architecture.md: WAIT 'stays in EXECUTE until its pin condition is true
    or its own optional timeout counter expires (3+N if a timeout is set)';
    isa.md WAIT: cycles = mantissa << (exponent*5), mantissa = imm[4:0],
    exponent = Rd. Pin never reaches level -> gap = SET(3) + 3 + N."""
    chip = await new_chip(dut)
    fails = []
    for (m, e) in [(1, 0), (2, 0), (20, 0), (31, 0), (1, 1), (3, 1), (1, 2), (2, 2)]:
        n = delay_cycles(m, e)
        g = await measure_gap(chip, gap_prog(lambda a, m=m, e=e: a.wait(1, 1, m, e)), timeout=n + 200)
        if g != 6 + n:
            fails.append(((m, e), g, 6 + n))
    # level-0 wait on a pin held high
    g = await measure_gap(chip, gap_prog(lambda a: a.wait(2, 0, 7, 0)), ext={2: 1})
    if g != 13:
        fails.append(("level0 (7,0)", g, 13))
    assert not fails, f"WAIT timeout (mant,exp), gap, expected: {fails}"


@cocotb.test()
async def test_wait_writes_flag_always(dut):
    """isa.md WAIT: 'Every WAIT writes the shared flag on exit, uniformly, no
    exceptions: 0 if the pin condition was met, 1 if it timed out. For an
    unbounded WAIT this is always 0'."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(1, 1)
    a.cmp(0, 0)                    # flag 1
    a.wait(7, 1)                   # unbounded, met when host raises p7
    fw_out_flag(a, 3, 0x10)        # 0x10
    a.cmp(0, 1)                    # flag 0
    a.wait(2, 1, 10, 0)            # p2 low -> timeout
    fw_out_flag(a, 3, 0x20)        # 0x21
    a.cmp(0, 0)                    # flag 1
    a.wait(7, 1, 31, 1)            # already met
    fw_out_flag(a, 3, 0x30)        # 0x30
    a.cmp(0, 0)
    a.wait(2, 0, 4, 0)             # level 0 met immediately (p2 low)
    fw_out_flag(a, 3, 0x40)        # 0x40
    a.halt()
    await chip.run(a)
    await chip.clk(100)
    assert chip.uo_hist() == [], "passed an unbounded WAIT with the pin low"
    chip.set_ext(7, 1)
    assert await chip.wait_uo(4) == [0x10, 0x21, 0x30, 0x40]


@cocotb.test()
async def test_wait_computed_zero_is_unbounded(dut):
    """isa.md WAIT: 'any encoding whose computed cycles is 0 -- not just the
    literal exponent=0,mantissa=0 case, e.g. exponent=2,mantissa=0 too --
    means no timeout (unbounded wait)'. Each exponent with mantissa 0 must
    stay blocked well past any timeout it could be mistaken for, then
    proceed with flag 0 once the pin rises."""
    chip = await new_chip(dut)
    for e, hold in ((0, 1500), (1, 2500), (2, 5000), (3, 40000)):
        a = Asm()
        a.cmp(0, 0)              # flag 1, so a 'met' exit must clear it
        a.wait(1, 1, 0, e)
        fw_out_flag(a, 3, 0x60)
        a.halt()
        await chip.run(a)
        await chip.clk(hold)
        assert chip.uo_hist() == [], f"WAIT exp={e} mant=0 exited without the pin: {chip.uo_hist()}"
        chip.set_ext(1, 1)
        assert await chip.wait_uo(1) == [0x60], f"exp={e}: flag after met unbounded WAIT"


@cocotb.test()
async def test_wait_tie_condition_wins(dut):
    """isa.md WAIT: 'If the pin condition becomes true on the exact same cycle
    the counter would expire, condition-met wins'. The pin rise is swept one
    cycle at a time across the expiry. Timeout exits occur at gap 6+N; met
    exits move 1:1 with the rise. Condition-wins means some run exits at
    exactly the timeout cycle with flag 0 (a timeout-first design would show
    flag 1 there and its latest met exit would be one cycle earlier).
    ASSUMPTION: both exits resolve in the same EXECUTE cycle, so 'same cycle
    as expiry' is observable as 'met-exit at the timeout exit time'."""
    chip = await new_chip(dut)
    m = 24
    t_to = 6 + m
    res = []
    for d in range(0, 46):
        a = Asm()
        a.set(0, 0, PP)
        a.delay(10)
        a.set(0, 1, KEEP)
        a.wait(1, 1, m, 0)
        a.set(0, 0, KEEP)
        a.beq("to")
        a.ldi(0, 0x11); a.out(0); a.halt()
        a.label("to")
        a.ldi(0, 0x22); a.out(0); a.halt()
        await chip.run(a)
        await chip.wait_drive(0, "0", 300)
        r = await chip.wait_drive(0, "1", 300)
        while chip.cycle < r + d:
            await chip.clk()
        chip.set_ext(1, 1)
        await chip.wait_drive(0, "0", 200)
        f = next(c for (c, st) in chip.pin_edges(0) if c > r and st == "0")
        hist = await chip.wait_uo(1, timeout=100, settle=5)
        res.append((d, hist[0], f - r))
    met = [(d, g) for (d, o, g) in res if o == 0x11]
    tos = [(d, g) for (d, o, g) in res if o == 0x22]
    assert met and tos, f"sweep did not span both outcomes: {res}"
    assert all(g == t_to for (_d, g) in tos), f"timeout exits not at 6+N={t_to}: {tos}"
    assert max(d for d, _ in met) < min(d for d, _ in tos), f"outcome not monotonic: {res}"
    gs = [g for _, g in met]
    assert all(gs[i + 1] - gs[i] in (0, 1) for i in range(len(gs) - 1)), f"met exits {met}"
    assert max(gs) == t_to, (f"no met exit at the expiry cycle ({t_to}); latest met exit "
                             f"{max(gs)} -> timeout won the tie. sweep: {res}")


@cocotb.test()
async def test_wait_open_drain_clock_stretch(dut):
    """isa.md WAIT row / SET open-drain: 'WAIT SCL,1 blocks until the slave
    actually releases the clock, not just until we do'. architecture.md
    rule (2): WAIT reads the pad. Pin 1 released (open-drain 1) while the
    'slave' holds it low; also, when the chip itself drives low, a timed
    WAIT for high times out (flag 1)."""
    chip = await new_chip(dut)
    a = Asm()
    a.set(1, 1, OD)
    a.set(0, 1, PP)
    a.wait(1, 1)
    a.set(0, 0, KEEP)
    a.set(1, 0, KEEP)          # chip drives SCL low
    a.delay(20)
    a.wait(1, 1, 30, 0)        # pad low (own drive) -> timeout
    fw_out_flag(a, 2, 0x40)    # 0x41
    a.halt()
    await chip.run(a, ext={1: 0})
    await chip.wait_drive(0, "1", 200)
    for _ in range(300):
        await chip.clk()
        assert chip.drive(1) == "Z", "open-drain 1 should release the pin"
        assert chip.drive(0) == "1", "WAIT passed while slave holds the line low"
    chip.set_ext(1, 1)  # slave releases, pull-up wins
    await chip.wait_drive(0, "0", 50)
    assert await chip.wait_uo(1) == [0x41]


@cocotb.test()
async def test_load_preloaded_data_region(dut):
    """isa.md Data memory: bytes 512-1023 are data; LOAD 'Address = 512 +
    imm (imm(9) reaches the full 512-byte data region)'; architecture.md:
    LOAD-mode boot 'streams the full 1024 bytes' and 'the host can preload a
    CRC table or config constants through the exact same boot sequence'.
    Also: a STORE over a preloaded byte is read back."""
    chip = await new_chip(dut)
    offs = [0, 1, 7, 8, 255, 256, 300, 510, 511]
    assert DATA[0] != DATA[256] and DATA[255] != DATA[511]
    a = Asm()
    for i, o in enumerate(offs):
        r = i % 4
        a.load(r, o)
        fw_send(a, r)
    a.ldi(2, 0xC6)
    a.store(2, 7)
    a.load(3, 7)
    fw_send(a, 3)
    a.load(1, 8)       # neighbour untouched
    fw_send(a, 1)
    a.halt()
    await chip.run(a, data=DATA)
    got = await chip.host_read_n(len(offs) + 2)
    assert got == [DATA[o] for o in offs] + [0xC6, DATA[8]], f"LOAD results {got}"


@cocotb.test()
async def test_store_load_roundtrip(dut):
    """isa.md STORE: 'write Rd's value to data memory, address = 512 + imm';
    LOADX address = 512 + Rs. architecture.md: 'STORE writes cleanly within
    its own EXECUTE', so a LOAD of the same address in the very next
    instruction sees the new value. Offsets 0 and 256 must not alias."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0x3C); a.store(0, 300); a.load(1, 300); fw_send(a, 1)       # 3C
    a.ldi(0, 0xC3); a.store(0, 511); a.load(2, 511); fw_send(a, 2)       # C3
    a.ldi(0, 0x99); a.store(0, 0); a.ldi(1, 0); a.loadx(3, 1); fw_send(a, 3)       # 99
    a.ldi(0, 0x42); a.store(0, 255); a.ldi(1, 255); a.loadx(2, 1); fw_send(a, 2)   # 42
    a.ldi(0, 0x11); a.store(0, 256)
    a.ldi(0, 0x22); a.store(0, 0)
    a.load(1, 256); fw_send(a, 1)                                         # 11
    a.load(1, 0); fw_send(a, 1)                                           # 22
    a.ldi(3, 0x5E); a.store(3, 400); a.ldi(3, 0); a.load(3, 400); fw_send(a, 3)    # 5E
    a.halt()
    await chip.run(a)
    got = await chip.host_read_n(7)
    assert got == [0x3C, 0xC3, 0x99, 0x42, 0x11, 0x22, 0x5E], f"{[hex(x) for x in got]}"


@cocotb.test()
async def test_loadx_register_index(dut):
    """isa.md LOADX: 'read data memory into Rd, address = 512 + Rs ...
    Rs's full 8-bit range reaches the first 256 bytes of the data region'.
    Rd==Rs is allowed (1 read + 1 write of the same register)."""
    chip = await new_chip(dut)
    a = Asm()
    exp = []
    for (rd, rs, idx) in [(0, 1, 0), (0, 1, 1), (2, 1, 255), (3, 2, 0x80), (1, 1, 0x40), (0, 3, 0x7F)]:
        a.ldi(rs, idx)
        a.loadx(rd, rs)
        fw_send(a, rd)
        exp.append(DATA[idx])
    a.halt()
    await chip.run(a, data=DATA)
    got = await chip.host_read_n(len(exp))
    assert got == exp, f"LOADX results {got}, expected {exp}"


@cocotb.test()
async def test_load_writeback_no_hazard(dut):
    """architecture.md: LOAD/LOADX writeback lands at the end of the next
    instruction's FETCH_LO, 'always before that instruction's own EXECUTE
    ... Hazard-free by construction'. The loaded register is consumed by the
    very next instruction (CMP, OUT, STORE, TEST, SHIFT); and a following
    LDI to the same register wins (it writes later)."""
    chip = await new_chip(dut)
    data = list(DATA)
    data[10], data[11] = 0x5A, 0xA5
    a = Asm()
    a.ldi(2, 0x5A)
    a.load(1, 10); a.cmp(1, 2); fw_out_flag(a, 3, 0x10); fw_ack(a)        # read 0x11
    a.load(1, 11); a.out(1); fw_ack(a)                                     # A5
    a.load(1, 10); a.store(1, 20); a.load(0, 20); fw_send(a, 0)            # 5A
    a.load(1, 11); a.ldi(1, 0x33); fw_send(a, 1)                           # 33
    a.load(1, 10); a.test(1, 1); fw_out_flag(a, 3, 0x20); fw_ack(a)        # 21
    a.load(1, 11); a.shift(1, True); fw_send(a, 1)                         # 52
    a.ldi(3, 11); a.loadx(0, 3); a.out(0); fw_ack(a)                       # A5
    a.load(1, 10); a.load(2, 11); a.cmp(1, 2); fw_out_flag(a, 3, 0x30); fw_ack(a)  # 30
    fw_send(a, 1); fw_send(a, 2)                                           # 5A A5
    a.halt()
    await chip.run(a, data=data)
    got = await chip.host_read_n(10)
    assert got == [0x11, 0xA5, 0x5A, 0x33, 0x21, 0x52, 0xA5, 0x30, 0x5A, 0xA5], \
        f"{[hex(x) for x in got]}"


# ===========================================================================
# Worked idioms / protocol timing
# ===========================================================================
@cocotb.test()
async def test_uart_tx_idiom_bit_period(dut):
    """isa.md Worked idioms TX loop; protocol_timing.md: 'OUTB(3) +
    DELAY(3+N) + SHIFT(3) + LOOP(3) = 12 fixed cycles + N per bit'; UART
    115200 row: N=422, total 434, 'exact'. 0x55 sent MSB-first toggles every
    bit, so all 8 edges must be exactly 434 cycles apart."""
    chip = await new_chip(dut)
    a = Asm()
    a.set(0, 1, PP)
    a.ldi(1, 8)
    a.ldi(0, 0x55)
    a.label("tx")
    a.outb(0, 0, 7)
    a.delay(422)
    a.shift(0, False)
    a.loop(1, "tx")
    a.set(0, 1, KEEP)
    a.halt()
    await chip.run(a)
    await chip.clk(8 * 434 + 200)
    e = chip.pin_edges(0)
    assert e[0][1] == "1"
    bits = e[1:]
    assert [s for _, s in bits] == ["0", "1"] * 4, f"TX levels {bits}"
    gaps = {bits[i + 1][0] - bits[i][0] for i in range(len(bits) - 1)}
    assert gaps == {434}, f"bit periods {gaps}, spec 434"


@cocotb.test()
async def test_uart_rx_idiom(dut):
    """isa.md Worked idioms RX loop ('shift before capturing'): DELAY/
    SHIFT/INB/LOOP, period 12+N cycles (N=100 -> 112). The harness drives
    bits MSB-first, one per 112 cycles, phased so each INB samples mid-bit
    (margin about +-50 cycles, far more than the 2-cycle synchronizer)."""
    chip = await new_chip(dut)
    p = 112
    a = Asm()
    a.set(0, 1, PP)
    a.ldi(1, 8)
    a.ldi(0, 0)
    a.label("rx")
    a.delay(100)
    a.shift(0, False)
    a.inb(0, 1, 0)
    a.loop(1, "rx")
    a.out(0)
    a.halt()
    await chip.run(a)
    c0 = await chip.wait_drive(0, "1", 200)
    data = 0xB2
    for k in range(8):
        while chip.cycle < c0 + 55 + k * p:
            await chip.clk()
        chip.set_ext(1, (data >> (7 - k)) & 1)
    assert await chip.wait_uo(1, timeout=400) == [data]


# ===========================================================================
# XOR (opcode 19) and ADD (opcode 20) -- isa.md, added 2026-10-23
# ===========================================================================
# Spec text these tests rely on (isa.md):
#  * Register file, Resolved note: "XOR and ADD added (opcodes 19 and 20,
#    reg-reg format) ... `ADD Rd,Rs` (flag = carry out) gives increments,
#    multi-byte sums and additive checksums, and `MOV` is now `XOR Rd,Rd` +
#    `ADD Rd,Rs`".
#  * Row 19: "`Rd <= Rd ^ Rs`. Does not touch the shared flag (like
#    LOOP/SHIFT), so a flag tested before an XOR survives it ... `XOR Rd,Rd`
#    clears Rd."
#  * Row 20: "`Rd <= (Rd + Rs) mod 256`; sets the shared flag to the carry
#    out (1 iff Rd + Rs > 255), so BEQ/BNE after an ADD branch on carry ...
#    A fourth flag-writer alongside CMP/TEST-bit/WAIT. With `LDI Rx,1` it is
#    the increment the ISA lacked".
#  * Reg-reg format: `opcode(5) | Rd(2) | Rs(2) | reserved(7)`, MSB-first
#    (so Rd = word[10:9], Rs = word[8:7]).
#  * Branch format, CALL: "CALL also silently clobbers the shared flag if the
#    subroutine uses CMP/TEST-bit/WAIT/ADD internally".
# Flag observation uses fw_out_flag (BEQ = cond 01 = "if flag set").

def _others(*regs):
    return [r for r in range(4) if r not in regs]


def _emit_rr_case(a, op, i, rd, rs, x, y, pre_flag):
    """LDI operands, force the flag to pre_flag, run op, then report
    (0x10*(i%16)) | flag, Rd, and Rs (if distinct) over the OUT handshake.
    The flag is reported first: fw_send's WAITs write the flag."""
    s, t = _others(rd, rs)[:2]
    if pre_flag:
        a.cmp(s, s)                   # isa.md CMP: flag = (Rd==Rs) = 1
    else:
        a.ldi(s, 0); a.ldi(t, 1)
        a.cmp(s, t)                   # flag = 0
    a.ldi(rd, x)
    if rs != rd:
        a.ldi(rs, y)
    getattr(a, op)(rd, rs)
    fw_out_flag(a, s, 0x10 * (i % 16))
    fw_ack(a)
    fw_send(a, rd)
    if rs != rd:
        fw_send(a, rs)


def _expect_rr_case(op, i, rd, rs, x, y, pre_flag):
    if rd == rs:
        y = x
    if op == "xor":
        res, flag = ref_xor(x, y), pre_flag      # row 19: flag untouched
    else:
        res, flag = ref_add(x, y)                # row 20: flag = carry out
    out = [0x10 * (i % 16) | flag, res]
    if rs != rd:
        out.append(y)                            # Rs is only read
    return out


async def _run_rr_cases(chip, op, cases, chunk=7):
    """cases: (rd, rs, x, y, pre_flag). Returns list of mismatches."""
    fails = []
    for base in range(0, len(cases), chunk):
        part = cases[base:base + chunk]
        a = Asm()
        exp = []
        for j, c in enumerate(part):
            _emit_rr_case(a, op, base + j, *c)
            exp.append(_expect_rr_case(op, base + j, *c))
        a.halt()
        assert len(a.words()) <= 256, "harness bug: program exceeds program space"
        await chip.run(a)
        for c, e in zip(part, exp):
            got = await chip.host_read_n(len(e))
            if got != e:
                fails.append((op, c, [hex(v) for v in got], [hex(v) for v in e]))
    return fails


@cocotb.test()
async def test_xor_values(dut):
    """isa.md row 19: 'Rd <= Rd ^ Rs'; 'XOR Rd,Rd clears Rd'; 'Does not touch
    the shared flag' (pre-flag alternated, must survive -- also for a zero
    result, i.e. XOR sets no zero flag). Rs is only read (reg-reg format:
    the result goes to Rd). Edge values: all-zero, all-ones, equal values in
    different registers, complementary patterns, single top/bottom bits."""
    chip = await new_chip(dut)
    cases = [(0, 1, 0x00, 0x00, 1), (0, 1, 0xFF, 0xFF, 0), (2, 3, 0xA5, 0x5A, 1),
             (3, 2, 0xFF, 0x00, 0), (1, 0, 0x80, 0x01, 1), (0, 2, 0x0F, 0xFF, 0),
             (1, 3, 0x01, 0x01, 1), (2, 0, 0x7E, 0x81, 0), (3, 1, 0x00, 0xC3, 1),
             # Rd == Rs: 'XOR Rd,Rd clears Rd'
             (1, 1, 0xA5, None, 0), (3, 3, 0xFF, None, 1), (0, 0, 0x00, None, 1),
             (2, 2, 0x80, None, 0)]
    fails = await _run_rr_cases(chip, "xor", cases)
    assert not fails, f"XOR (op, case(rd,rs,x,y,preflag), got, expected): {fails}"


@cocotb.test()
async def test_add_values_and_carry(dut):
    """isa.md row 20: 'Rd <= (Rd + Rs) mod 256; sets the shared flag to the
    carry out (1 iff Rd + Rs > 255)'. The flag is preset to the opposite of
    the expected carry so ADD must write it in both directions (0->1 and
    1->0). Edge values: sum exactly 255 (no carry), 256 (wraps to 0 with
    carry), 510 (max), 0+0 (zero result, no carry: the flag is carry, not
    zero), and the 0x7F/0x80 sign boundary (no overflow flag). Rd == Rs
    doubles Rd (carry = old bit 7)."""
    chip = await new_chip(dut)
    raw = [(0, 1, 0x00, 0x00), (0, 1, 0xFF, 0x01), (1, 0, 0x01, 0xFF), (2, 3, 0xFF, 0xFF),
           (3, 2, 0x80, 0x80), (0, 2, 0x7F, 0x01), (1, 3, 0x7F, 0x80), (2, 1, 0x80, 0x7F),
           (3, 0, 0xFF, 0x00), (0, 3, 0x00, 0xFF), (1, 2, 0x01, 0xFE), (2, 0, 0x02, 0xFE),
           (3, 1, 0xF0, 0x0F), (0, 1, 0xF0, 0x11), (1, 0, 0x5A, 0xA5), (2, 3, 0x5A, 0xA6),
           # Rd == Rs: Rd <= 2*Rd mod 256, carry = old bit 7
           (1, 1, 0x80, None), (3, 3, 0x7F, None), (0, 0, 0xC0, None), (2, 2, 0x00, None),
           (0, 0, 0xFF, None)]
    cases = []
    for (rd, rs, x, y) in raw:
        _r, c = ref_add(x, x if rd == rs else y)
        cases.append((rd, rs, x, y, 1 - c))
    fails = await _run_rr_cases(chip, "add", cases)
    assert not fails, f"ADD (op, case(rd,rs,x,y,preflag), got, expected): {fails}"


@cocotb.test()
async def test_xor_add_register_field_matrix(dut):
    """isa.md Reg-reg format 'opcode(5) | Rd(2) | Rs(2) | reserved(7)',
    MSB-first ('remaining fields pack down from word[10:0] in the order
    shown'), and rows 19/20 write Rd only. All 16 (Rd, Rs) pairs for both
    opcodes with R0..R3 = 0x11/0x22/0x44/0x88 (disjoint bits), reading all
    four registers back: the right register is written, with the right
    value, and no other register changes."""
    chip = await new_chip(dut)
    init = [0x11, 0x22, 0x44, 0x88]
    fails = []
    for op in ("xor", "add"):
        pairs = [(rd, rs) for rd in range(4) for rs in range(4)]
        for base in (0, 8):
            a = Asm()
            exp = []
            for (rd, rs) in pairs[base:base + 8]:
                for r in range(4):
                    a.ldi(r, init[r])
                getattr(a, op)(rd, rs)
                for r in range(4):
                    fw_send(a, r)
                regs = list(init)
                regs[rd] = (ref_xor if op == "xor" else lambda p, q: ref_add(p, q)[0])(init[rd], init[rs])
                exp.append(((rd, rs), regs))
            a.halt()
            await chip.run(a)
            for (pair, regs) in exp:
                got = await chip.host_read_n(4)
                if got != regs:
                    fails.append((op.upper(), pair, [hex(v) for v in got], [hex(v) for v in regs]))
    assert not fails, f"(op, (Rd,Rs), R0..R3 got, expected): {fails}"


@cocotb.test()
async def test_xor_preserves_tested_flag_idiom(dut):
    """isa.md row 19: 'a flag tested before an XOR survives it -- the shape a
    bitwise CRC loop needs (TEST-bit the bit, XOR the polynomial in, branch
    on the earlier test)'. Both flag polarities, with XOR results that are
    zero and nonzero, and a run of several XORs."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0x80); a.ldi(1, 0x80); a.ldi(2, 0x01)
    a.test(0, 7)                 # flag 1
    a.xor(0, 1)                  # result 0
    fw_out_flag(a, 3, 0x10)      # 0x11
    a.test(0, 7)                 # R0 = 0 -> flag 0
    a.xor(0, 2)                  # result 0x01 (nonzero)
    a.xor(1, 1)                  # result 0
    a.xor(2, 0)                  # R2 = 0x01 ^ 0x01 = 0
    fw_out_flag(a, 3, 0x20)      # 0x20
    a.test(0, 0)                 # R0 = 0x01 -> flag 1
    a.xor(0, 0)
    a.xor(0, 0)
    fw_out_flag(a, 3, 0x30)      # 0x31
    a.halt()
    await chip.run(a)
    assert await chip.wait_uo(3) == [0x11, 0x20, 0x31]


@cocotb.test()
async def test_add_flag_shared_with_other_writers(dut):
    """isa.md: ADD is 'A fourth flag-writer alongside CMP/TEST-bit/WAIT';
    CMP row: 'one flag register, last write wins'; WAIT row: 'Every WAIT
    writes the shared flag on exit ... 0 if the pin condition was met, 1 if
    it timed out'. LOOP/SHIFT/XOR 'locked to not touch the flag' (Branch
    format CALL notes), so a carry survives them; LDI/OUT/STORE/LOAD are not
    in the named writer set (LOOP row: 'keeps the set of flag-writers small
    and named ... not any instruction')."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0xFF); a.ldi(1, 0x01); a.ldi(2, 0x00)
    # ADD carry then CMP unequal -> 0
    a.ldi(3, 0xFF); a.add(3, 1); a.cmp(0, 1); fw_out_flag(a, 3, 0x10)        # 0x10
    # CMP equal then ADD no carry -> 0
    a.cmp(0, 0); a.ldi(3, 0x10); a.add(3, 1); fw_out_flag(a, 3, 0x20)        # 0x20
    # TEST bit=1 then ADD no carry -> 0
    a.test(0, 3); a.ldi(3, 0x00); a.add(3, 2); fw_out_flag(a, 3, 0x30)       # 0x30
    # ADD carry then TEST bit=0 -> 0
    a.ldi(3, 0x80); a.add(3, 3); a.test(2, 0); fw_out_flag(a, 3, 0x40)       # 0x40
    # CMP unequal then ADD carry -> 1
    a.cmp(0, 1); a.ldi(3, 0x01); a.add(3, 0); fw_out_flag(a, 3, 0x50)        # 0x51
    # WAIT timeout (pin 2 low) -> 1, then ADD no carry -> 0
    a.wait(2, 1, 5, 0); a.ldi(3, 0x01); a.add(3, 1); fw_out_flag(a, 3, 0x60)  # 0x60
    # ADD carry -> 1, then WAIT met (pin 7 high) -> 0
    a.ldi(3, 0xFF); a.add(3, 0); a.wait(7, 1, 5, 0); fw_out_flag(a, 3, 0x70)  # 0x70
    # WAIT met -> 0, then ADD carry -> 1
    a.wait(7, 1); a.ldi(3, 0xFF); a.add(3, 0); fw_out_flag(a, 3, 0x80)       # 0x81
    # ADD carry survives XOR, SHIFT, LOOP (falls through), LDI, OUT, STORE, LOAD
    a.ldi(3, 0xFF); a.add(3, 1)
    a.xor(3, 0); a.shift(3, False); a.ldi(2, 1); a.loop(2, "far1"); a.ldi(2, 0)
    a.out(2); a.store(0, 3); a.load(2, 3)
    fw_out_flag(a, 3, 0x90)                                                    # 0x91
    # ADD no-carry (0) survives the same instructions
    a.ldi(3, 0x01); a.add(3, 1)
    a.xor(3, 0); a.shift(3, True); a.ldi(2, 1); a.loop(2, "far1"); a.ldi(2, 0)
    a.out(2); a.store(0, 3); a.load(2, 3)
    fw_out_flag(a, 3, 0xA0)                                                    # 0xA0
    a.halt()
    a.label("far1")
    a.ldi(0, 0xEE); a.out(0); a.halt()
    await chip.run(a, ext={7: 1})
    exp = [0x10, 0x20, 0x30, 0x40, 0x51, 0x60, 0x70, 0x81, 0x00, 0x91, 0x00, 0xA0]
    # (uo_hist compresses repeats; each 'OUT R2' of 0 shows as one 0x00)
    hist = await chip.wait_uo(len(exp), timeout=4000)
    assert hist == exp, f"flag sequence {[hex(v) if isinstance(v, int) else v for v in hist]}"


@cocotb.test()
async def test_call_flag_clobber_add_not_xor(dut):
    """isa.md Branch format: 'CALL also silently clobbers the shared flag if
    the subroutine uses CMP/TEST-bit/WAIT/ADD internally ... BEQ branches on
    that result, not the one the caller set up'; 'now that LOOP/SHIFT/XOR are
    locked to not touch the flag'. TEST-bit sets flag 1; a subroutine doing a
    carry-free ADD returns flag 0; one doing only XORs returns flag 1; one
    doing a carrying ADD after the caller set flag 0 returns 1."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0x80); a.ldi(1, 0x01)
    a.test(0, 7); a.call("s_add"); fw_out_flag(a, 3, 0x10)     # 0x10 (clobbered)
    a.test(0, 7); a.call("s_xor"); fw_out_flag(a, 3, 0x20)     # 0x21 (preserved)
    a.test(0, 0); a.call("s_addc"); fw_out_flag(a, 3, 0x30)    # 0x31 (clobbered)
    a.halt()
    a.label("s_add")
    a.ldi(2, 0x01); a.add(2, 1); a.ret()
    a.label("s_xor")
    a.ldi(2, 0xFF); a.xor(2, 2); a.xor(2, 0); a.ret()
    a.label("s_addc")
    a.ldi(2, 0xFF); a.add(2, 1); a.ret()
    await chip.run(a)
    assert await chip.wait_uo(3) == [0x10, 0x21, 0x31]


@cocotb.test()
async def test_add_increment_wraps_once_in_256(dut):
    """isa.md row 20: 'With LDI Rx,1 it is the increment the ISA lacked';
    'mod 256' and 'carry out (1 iff Rd + Rs > 255)'. 256 increments from 0
    (LOOP with Rd=0 runs the body 256 times, LOOP row) must bring R0 back
    to 0 with exactly one carry, on the 256th increment (0xFF -> 0x00).
    The carry path records the LOOP counter: on the 256th body pass it is 1
    (entered 0, the LOOP row says 'writes 255 and branches', so pass k sees
    (1 - k) mod 256)."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0); a.ldi(1, 1); a.ldi(2, 0); a.ldi(3, 0)
    a.store(3, 0)                     # carry-position record, starts 0
    a.label("body")
    a.add(0, 1)
    a.bne("nc")                       # cond 10: flag clear -> no carry
    a.add(3, 1)                       # carry count
    a.store(2, 0)                     # LOOP counter at the carry
    a.label("nc")
    a.loop(2, "body")
    fw_send(a, 0)
    fw_send(a, 3)
    a.load(2, 0)
    fw_send(a, 2)
    a.halt()
    await chip.run(a)
    got = await chip.host_read_n(3, timeout=40000)
    assert got == [0x00, 0x01, 0x01], f"(R0 after 256 incs, carries, counter at carry) = {got}"


@cocotb.test()
async def test_add_multibyte_sum(dut):
    """isa.md Resolved note / row 20: ADD 'gives increments, multi-byte sums
    and additive checksums'; 'BEQ/BNE after an ADD branch on carry'. A 16-bit
    sum of data bytes (R1:R0) propagates each carry with BNE + ADD R1,1.
    Bytes come from the data region preloaded at boot (isa.md Data memory)."""
    chip = await new_chip(dut)
    msg = [0xFF, 0xFF, 0x01, 0x80, 0x80, 0x7F, 0x00, 0xFE, 0x02, 0xFF, 0x01, 0xFF]
    data = list(DATA)
    data[:len(msg)] = msg
    a = Asm()
    a.ldi(0, 0); a.ldi(1, 0); a.ldi(3, 1)
    for k in range(len(msg)):
        a.load(2, k)
        a.add(0, 2)
        n = a.fresh()
        a.bne(n)
        a.add(1, 3)
        a.label(n)
    fw_send(a, 0)
    fw_send(a, 1)
    a.halt()
    await chip.run(a, data=data)
    s = sum(msg)
    got = await chip.host_read_n(2)
    assert got == [s & 0xFF, (s >> 8) & 0xFF], f"16-bit sum {got}, expected {s:#06x}"


@cocotb.test()
async def test_mov_idiom_xor_then_add(dut):
    """isa.md Register file Resolved note: 'MOV is now XOR Rd,Rd + ADD Rd,Rs
    (2 instructions, no memory round trip)'. Rd ends equal to Rs, Rs is
    unchanged, and (row 20) the flag is the carry of 0 + Rs, i.e. 0 -- even
    if it was 1 before."""
    chip = await new_chip(dut)
    a = Asm()
    exp = []
    for i, (rd, rs, v) in enumerate([(0, 1, 0xA5), (2, 3, 0xFF), (3, 0, 0x80), (1, 2, 0x00)]):
        s = _others(rd, rs)[0]
        a.ldi(rd, 0x5C)
        a.ldi(rs, v)
        a.cmp(s, s)                   # flag 1
        a.xor(rd, rd)
        a.add(rd, rs)
        fw_out_flag(a, s, 0x10 * (i + 1))
        fw_ack(a)
        fw_send(a, rd)
        fw_send(a, rs)
        exp += [0x10 * (i + 1), v, v]
    a.halt()
    await chip.run(a)
    got = await chip.host_read_n(len(exp))
    assert got == exp, f"MOV idiom {[hex(x) for x in got]}, expected {[hex(x) for x in exp]}"


MSG = list(b"123456789")


@cocotb.test()
async def test_crc8_bitwise_idiom(dut):
    """isa.md row 19: XOR 'makes table-driven and bitwise CRC direct'; the
    bitwise shape it names: 'TEST-bit the bit, XOR the polynomial in, branch
    on the earlier test'. CRC-8 (poly 0x07, MSB-first) over "123456789"
    (check value 0xF4), reporting the running CRC after each byte. Relies on
    SHIFT left with 0 fill (SHIFT row), XOR not touching the flag, and
    LOOP's count."""
    chip = await new_chip(dut)
    data = list(DATA)
    data[:len(MSG)] = MSG
    a = Asm()
    a.ldi(0, 0)                       # crc
    a.ldi(2, 0x07)                    # polynomial
    for k in range(len(MSG)):
        a.load(1, k)
        a.xor(0, 1)
        a.ldi(3, 8)
        top, keep = a.fresh(), a.fresh()
        a.label(top)
        a.test(0, 7)                  # flag = msb
        a.shift(0, False)
        a.xor(0, 2)                   # tentatively XOR the polynomial in
        a.beq(keep)                   # msb was 1: keep it
        a.xor(0, 2)                   # msb was 0: undo
        a.label(keep)
        a.loop(3, top)
        fw_send(a, 0)
    a.halt()
    assert len(a.words()) <= 256
    await chip.run(a, data=data)
    exp = [crc8(MSG[:k + 1]) for k in range(len(MSG))]
    assert exp[-1] == 0xF4
    got = await chip.host_read_n(len(MSG))
    assert got == exp, f"running CRC-8 {[hex(x) for x in got]}, expected {[hex(x) for x in exp]}"


@cocotb.test()
async def test_crc8_table_driven_idiom(dut):
    """isa.md Register file CRC note: 'Table-driven CRC needs crc ^ byte to
    index the table'; Resolved note: 'XOR Rd,Rs makes table-driven and
    bitwise CRC direct'; LOADX: 'address = 512 + Rs ... the first 256 bytes
    of the data region' (Data memory: 'a clean fit for a standard 256-entry
    byte-wide CRC lookup table'), preloaded through the boot stream. The
    message sits at data offset 256+ (LOAD's fixed-address range). The LOADX
    reading the XOR result in the very next instruction also checks that
    XOR's write is visible immediately (no trailing writeback is stated for
    it, unlike LOAD/LOADX)."""
    chip = await new_chip(dut)
    data = crc8_table() + MSG + [0] * (256 - len(MSG))
    a = Asm()
    a.ldi(0, 0)
    for k in range(len(MSG)):
        a.load(1, 256 + k)
        a.xor(1, 0)                   # index = byte ^ crc
        a.loadx(0, 1)                 # crc = T[index]
        fw_send(a, 0)
    a.halt()
    await chip.run(a, data=data)
    exp = [crc8(MSG[:k + 1]) for k in range(len(MSG))]
    got = await chip.host_read_n(len(MSG))
    assert got == exp, f"table CRC-8 {[hex(x) for x in got]}, expected {[hex(x) for x in exp]}"


@cocotb.test()
async def test_xor_add_result_visible_to_next_instruction(dut):
    """architecture.md: 'Uniform 3 cycles/instruction'; the only stated
    trailing writeback is LOAD/LOADX's ('LOAD/LOADX are the one case where
    that distinction matters').
    ASSUMPTION: XOR/ADD, as register ops not named there, complete within
    their own EXECUTE, so the very next instruction (OUT, CMP, STORE, TEST,
    OUTB, SHIFT, another XOR/ADD, LOADX) reads the new value, and a BEQ right
    after ADD sees the new carry."""
    chip = await new_chip(dut)
    a = Asm()
    a.set(0, 0, PP); a.delay(10)
    a.ldi(0, 0x0F); a.ldi(1, 0xF0); a.ldi(2, 0x01)
    a.xor(0, 1); a.out(0); fw_ack(a)                            # FF
    a.ldi(3, 0)
    a.add(0, 2); a.cmp(0, 3); fw_out_flag(a, 3, 0x10); fw_ack(a)  # R0=0==R3(0)->0x11
    a.ldi(3, 0)
    a.add(1, 1); a.store(1, 9); a.load(3, 9); fw_send(a, 3)     # E0
    a.xor(1, 2); a.test(1, 0); fw_out_flag(a, 3, 0x20); fw_ack(a)  # E1 bit0=1 -> 0x21
    a.ldi(3, 0x7F); a.add(3, 2); a.outb(3, 0, 7); a.delay(20)   # 0x80 -> pin0 = 1
    a.add(3, 3); a.shift(3, True); a.add(3, 2); a.xor(3, 2); fw_send(a, 3)  # 00 ->00->01->00
    a.ldi(3, 0xFF); a.add(3, 2); a.beq("c"); a.ldi(3, 0xEE)
    a.label("c")
    fw_send(a, 3)                                               # 00 (carry taken)
    a.ldi(3, 0x10); a.ldi(2, 0x05); a.add(3, 2); a.loadx(1, 3); fw_send(a, 1)  # DATA[0x15]
    a.halt()
    await chip.run(a, data=DATA)
    got = await chip.host_read_n(7)
    assert chip.pin_hist(0)[-1] == "1", f"OUTB after ADD: pin0 {chip.pin_hist(0)}"
    assert got == [0xFF, 0x11, 0xE0, 0x21, 0x00, 0x00, DATA[0x15]], f"{[hex(x) for x in got]}"


@cocotb.test()
async def test_xor_add_reserved_field_ignored(dut):
    """isa.md Reg-reg format: 'opcode(5) | Rd(2) | Rs(2) | reserved(7)';
    'The low 7 bits are reserved and ignored on decode: any value executes
    exactly like 0'. XOR/ADD with reserved = 0x7F/0x55/0x2A give the same
    results and flag as with 0, and the pins are untouched. (Earlier passes
    marked this AMBIGUOUS; the updated isa.md settles it.)"""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(0, 0xC3); a.ldi(1, 0x3C); a.ldi(2, 0x80)
    a.xor(0, 1, 0x7F); fw_send(a, 0)                            # FF
    a.cmp(3, 3)                                                 # flag 1
    a.xor(1, 1, 0x55); fw_out_flag(a, 3, 0x10); fw_ack(a)       # 0x11 (untouched)
    fw_send(a, 1)                                               # 00
    a.add(2, 2, 0x7F); fw_out_flag(a, 3, 0x20); fw_ack(a)       # 0x80+0x80 -> carry 0x21
    fw_send(a, 2)                                               # 00
    a.ldi(2, 0x40); a.add(2, 0, 0x2A); fw_out_flag(a, 3, 0x30); fw_ack(a)  # 0x40+0xFF=0x13F -> 0x31
    fw_send(a, 2)                                               # 3F
    a.halt()
    await chip.run(a)
    got = await chip.host_read_n(7)
    assert got == [0xFF, 0x11, 0x00, 0x21, 0x00, 0x31, 0x3F], f"{[hex(x) for x in got]}"
    for p in (0, 1, 2, 3, 7):
        assert chip.pin_hist(p, 1) == ["Z"], f"XOR/ADD with reserved bits touched uio[{p}]"


# ===========================================================================
# Serial engine: SCFG / STX / SRX / SRD (isa.md rows 21-24,
# docs/serial_engine.md, added 2026-11-04)
# ===========================================================================
# Spec text these tests rely on (serial_engine.md unless noted):
#  * Pins: 'The engine uses protocol pins 0 and 1 only.' Receive 'reads one
#    pin (rx_pin: 0 or 1), synchronized by the same 2-flop path'; transmit
#    'drives pin 0 with the line level. In diff mode it also drives pin 1
#    with the opposite level ... While it transmits the engine owns the pins
#    it drives: they are push-pull ... When the packet is over the pins go
#    back to their sticky SET mode and value.'
#  * The SCFG register table (k 0-8, command 15); 'Registers reset to 0'.
#  * Bit stuffing in line levels; transmit end sequence; 'Every bit lasts
#    exactly P clocks. Receive is off from the first transmitted bit until
#    the line is idle again.'
#  * Receive sampling, SRX outcomes and timeout encoding, SRD table.
# The reference encoder/decoder is spec_helpers.ser_tx_bits/ser_rx_decode,
# written from that text and self-tested (python3 spec_helpers.py).
#
# Harness conventions: pins 0/1 are left in input mode by the firmware unless
# a test says otherwise, so the engine's drive is visible as Z -> driven ->
# Z, and the harness can drive receive waveforms on them. Protocol pin 2
# (MK) and 3 are firmware markers, pin 7 is a harness->firmware 'go' input.
# Firmware never HALTs while the engine may be busy (it spins instead): see
# the AMBIGUOUS note on HALT in test_ser_tx_usb_waveform.

from spec_helpers import (SerCfg, USB_CFG, CAN_CFG, ser_tx_bits,  # noqa: E402
                          ser_rx_decode, ser_rx_summary, bits_to_wave,
                          rx_wave_robust, crc_of_data, crc_field_bits,
                          usb_stuff_error_wave)

MK = 2      # firmware marker pin
MK2 = 3     # second marker pin
GO7 = 7     # harness -> firmware 'go' pin
J = (0, 1)  # USB idle (serial_engine.md: 'idle (J) is D+ low'; D- the opposite)
K_ = (1, 0)


def fw_ser_config(a, cfg):
    """SCFG every register k = 0-8 (k = 4 last). Registers rotate R0..R3 as
    the source (isa.md SCFG row: 'Rd is the *source*, as for STORE')."""
    for i, (k, v) in enumerate(cfg.regs()):
        r = i % 4
        a.ldi(r, v)
        a.scfg(r, k)


def fw_store_flag(a, scratch, off):
    """data[off] = flag (LDI/STORE do not touch it; BNE = cond 10)."""
    s = a.fresh()
    a.ldi(scratch, 0)
    a.bne(s)
    a.ldi(scratch, 1)
    a.label(s)
    a.store(scratch, off)


def fw_report(a, n, base=0):
    """Send data[base .. base+n-1] to the host, one OUT handshake each."""
    a.ldi(1, base)
    a.ldi(2, n)
    a.ldi(3, 1)
    t = a.fresh()
    a.label(t)
    a.loadx(0, 1)
    fw_send(a, 0)
    a.add(1, 3)
    a.loop(2, t)


def fw_rx_collect(a, nmax, base, cnt, done_hook=None):
    """Unrolled SRX loop: data[base+i] = byte i until SRX returns flag 1
    (isa.md SRX row: byte -> flag 0; end of packet or timeout -> flag 1);
    data[cnt] = number of bytes. Uses R0, R1."""
    done = a.fresh()
    for i in range(nmax):
        a.ldi(1, i)
        a.srx(0)
        a.beq(done)
        a.store(0, base + i)
    a.ldi(1, nmax)
    a.label(done)
    if done_hook:
        done_hook(a)
    a.store(1, cnt)


def fw_drain(a):
    """SRX until flag 1 (the end of the packet)."""
    t = a.fresh()
    a.label(t)
    a.srx(0)
    a.bne(t)


def fw_spin(a):
    t = a.fresh()
    a.label(t)
    a.jmp(t)


def drive_trace(chip, b, c0, n):
    """Per-cycle drive state of uio[b] for cycles c0 .. c0+n-1."""
    segs = chip.pin_segments(b)
    out, i, cur = [], 0, segs[0][1]
    for c in range(c0, c0 + n):
        while i < len(segs) and segs[i][0] <= c:
            cur = segs[i][1]
            i += 1
        out.append(cur)
    return out


def expected_trace(bits, P, p1_when_undriven):
    exp = []
    for (p0, p1) in bits:
        exp += [(str(p0), p1_when_undriven if p1 is None else str(p1))] * P
    return exp


async def tx_capture(chip, pre, n_cycles, timeout=6000):
    """Wait for pins 0/1 to reach `pre` (their SET state), then for the
    first change (the first transmitted bit); clock n_cycles more. Returns
    the start cycle t0."""
    st = lambda: (chip.drive(0), chip.drive(1))  # noqa: E731
    await chip.wait_until(lambda: st() == pre, timeout, f"pins 0/1 at {pre}")
    t0 = await chip.wait_until(lambda: st() != pre, timeout, "first transmitted bit")
    await chip.clk(n_cycles)
    return t0


def tx_mismatch(chip, t0, exp, post, tail, P):
    got = list(zip(drive_trace(chip, 0, t0, len(exp) + tail),
                   drive_trace(chip, 1, t0, len(exp) + tail)))
    full = exp + [post] * tail
    for i, (g, e) in enumerate(zip(got, full)):
        if g != e:
            where = (f"bit {i // P} phase {i % P}" if i < len(exp)
                     else f"{i - len(exp)} cycles after the last bit time")
            return (f"(p0,p1) at t0+{i} ({where}): got {g}, spec {e}; "
                    f"got around: {got[max(0, i - 3):i + 4]}")
    return None


async def play_wave(chip, wave):
    """Drive pins 0/1 (outside world) one level pair per clock."""
    prev = None
    for lv in wave:
        if lv != prev:
            chip.set_ext(0, lv[0])
            chip.set_ext(1, lv[1])
            prev = lv
        await chip.clk()


def usb_packet_bytes(data):
    """What the receiver should deliver for a good USB packet sent with the
    doc's settings: every data byte, then the 16 CRC field bits as 2 bytes
    ('Every 8 data bits make a byte')."""
    f = crc_field_bits(USB_CFG, crc_of_data(USB_CFG, data))
    return list(data) + [sum(f[i] << i for i in range(8)), sum(f[8 + i] << i for i in range(8))]


# ----------------------------------------------------------------- transmit
@cocotb.test()
async def test_ser_tx_nrz_bit_period_exact(dut):
    """serial_engine.md: STX 'Queues Rs for transmission, LSB first'; 'Every
    bit lasts exactly P clocks'; P = k1[3:0]:k0 ('P = 2-4095'); 'Transmit
    drives pin 0 with the line level' (pin 1 only 'In diff mode'); 'Then it
    releases the pins' and they go back to their SET state (here input).
    P = 2 (minimum; byte time 16 clocks, so STX must also keep up), 7, 261
    (needs the high nibble) and 4095 (maximum). Back-to-back STXs make one
    contiguous packet ('A packet starts when a byte is queued with the
    engine idle, and ends when the engine runs out of bytes').
    ASSUMPTION: with nrzi = 0 the line level is the data bit (1 = high)."""
    chip = await new_chip(dut)
    fails = []
    for P, data in ((2, [0xA5, 0x0F, 0x3C]), (7, [0x96, 0x01]), (261, [0x5A]), (4095, [0xC9])):
        cfg = SerCfg(P=P, S=P // 2)
        a = Asm()
        fw_ser_config(a, cfg)
        for i, b in enumerate(data):
            a.ldi(i % 4, b)
            a.stx(i % 4)
        fw_spin(a)
        bits, _ = ser_tx_bits(cfg, data)
        exp = expected_trace(bits, P, "Z")
        await chip.run(a)
        t0 = await tx_capture(chip, ("Z", "Z"), len(exp) + 100)
        m = tx_mismatch(chip, t0, exp, ("Z", "Z"), 100, P)
        if m:
            fails.append((P, m))
    assert not fails, f"NRZ transmit (P, first mismatch): {fails}"


@cocotb.test()
async def test_ser_tx_usb_waveform(dut):
    """The doc's USB low-speed settings: NRZI ('data 0 = level change'),
    diff (pin 1 'the opposite level (USB D+/D-)'), stuffing K = 7 ('a 0
    after six 1s is K = 7 in line levels'), CRC16 poly 0x8005 from all ones
    over bytes 2 onward ('USB: SYNC and PID excluded, crc_start = 2'), sent
    'most significant first, each inverted if crc_inv, stuffed like data',
    then '2 bit times of SE0 and 1 of idle', then the pins are released.
    Payload with 0xFF/0x7F/0xFE forces stuff bits mid-packet. Exact, cycle
    by cycle, against the reference encoder; P = 33.
    AMBIGUOUS: isa.md HALT 'Freezes the FSM ... no further state changes'
    does not say whether the engine freezes too; firmware here spins
    instead of halting so the question never arises."""
    chip = await new_chip(dut)
    data = [0x80, 0xC3, 0xFF, 0x00, 0xFE, 0x7F, 0x01]
    bits, info = ser_tx_bits(USB_CFG, data)
    assert info["stuff"] and not info["crc_tail_stuff"]
    a = Asm()
    fw_ser_config(a, USB_CFG)
    for i, b in enumerate(data):
        a.ldi(i % 4, b)
        a.stx(i % 4)
    fw_spin(a)
    await chip.run(a, ext={1: 1})
    exp = expected_trace(bits, 33, "Z")
    t0 = await tx_capture(chip, ("Z", "Z"), len(exp) + 150)
    m = tx_mismatch(chip, t0, exp, ("Z", "Z"), 150, 33)
    assert m is None, f"USB transmit waveform: {m}"
    # the end, spelled out: SE0, SE0, J, then released
    tail = exp[-3 * 33:]
    assert tail == [("0", "0")] * 66 + [("0", "1")] * 33


@cocotb.test()
async def test_ser_tx_stuff_bit_before_eop(dut):
    """serial_engine.md end of packet: '1. a stuff bit, if the last bits
    leave one due; ... 3. in se0 mode, 2 bit times of SE0 and 1 of idle'.
    USB settings without CRC, bytes [0x80, 0xFC]: SYNC ends K K; 0xFC (LSB
    first 0,0,1,1,1,1,1,1) gives J K then six 1s at K -- seven equal levels
    on the last data bit, so a stuff bit (J) is due before SE0. Expected
    D+ levels hand-derived: 10101011 01111111 0, then SE0 SE0 J."""
    chip = await new_chip(dut)
    cfg = USB_CFG.but(crc_tx=0)
    a = Asm()
    fw_ser_config(a, cfg)
    a.ldi(0, 0x80); a.stx(0)
    a.ldi(1, 0xFC); a.stx(1)
    fw_spin(a)
    dp = "10101011" + "01111111" + "0"
    bits = [(int(c), 1 - int(c)) for c in dp] + [(0, 0), (0, 0), (0, 1)]
    assert bits == ser_tx_bits(cfg, [0x80, 0xFC])[0]
    exp = expected_trace(bits, 33, "Z")
    await chip.run(a, ext={1: 1})
    t0 = await tx_capture(chip, ("Z", "Z"), len(exp) + 100)
    m = tx_mismatch(chip, t0, exp, ("Z", "Z"), 100, 33)
    assert m is None, f"stuff bit before EOP: {m}"


@cocotb.test()
async def test_ser_tx_stuff_after_crc_before_eop(dut):
    """AMBIGUOUS: serial_engine.md's end list puts '1. a stuff bit, if the
    last bits leave one due' before '2. the CRC ... stuffed like data', so
    it does not say whether a stuff bit due after the *last CRC bit* is sent
    before the SE0. Tested reading: yes ('stuffed like data'; USB requires
    it). Payload [0x80, 0xC3, 0x00, 0xB9] (found by search) makes the
    inverted CRC16 end with seven equal levels."""
    chip = await new_chip(dut)
    data = [0x80, 0xC3, 0x00, 0xB9]
    bits, info = ser_tx_bits(USB_CFG, data)
    assert info["crc_tail_stuff"]
    a = Asm()
    fw_ser_config(a, USB_CFG)
    for i, b in enumerate(data):
        a.ldi(i % 4, b)
        a.stx(i % 4)
    fw_spin(a)
    await chip.run(a, ext={1: 1})
    exp = expected_trace(bits, 33, "Z")
    t0 = await tx_capture(chip, ("Z", "Z"), len(exp) + 100)
    m = tx_mismatch(chip, t0, exp, ("Z", "Z"), 100, 33)
    assert m is None, f"stuff after the CRC field: {m}"


@cocotb.test()
async def test_ser_tx_can_like_stuffing(dut):
    """serial_engine.md: 'CAN's "the complement after five equal bits" is
    K = 5'; stuffing 'in line levels'; a stuff bit 'carries no data and is
    not in the CRC'. NRZ, idle 1, K = 5, CRC-15 (width field 14, poly
    0x4599, start 0, init 0), no diff (pin 1 untouched), no SE0 (released
    right after the CRC). The payload makes stuff bits that start the next
    run (00000 s1 1111 -> s0) and long runs on both levels."""
    chip = await new_chip(dut)
    cfg = CAN_CFG
    data = [0x00, 0xFF, 0x00, 0xE0, 0x01, 0xF8, 0x3F]
    bits, info = ser_tx_bits(cfg, data)
    assert len(info["stuff"]) >= 8 and not info["crc_tail_stuff"]
    a = Asm()
    fw_ser_config(a, cfg)
    for i, b in enumerate(data):
        a.ldi(i % 4, b)
        a.stx(i % 4)
    fw_spin(a)
    await chip.run(a)
    exp = expected_trace(bits, cfg.P, "Z")
    t0 = await tx_capture(chip, ("Z", "Z"), len(exp) + 100)
    m = tx_mismatch(chip, t0, exp, ("Z", "Z"), 100, cfg.P)
    assert m is None, f"CAN-like transmit: {m}"


@cocotb.test()
async def test_ser_tx_crc_settings(dut):
    """serial_engine.md k = 4 bits 5/6 (crc_tx, crc_inv), k = 5 bits 7:4
    ('CRC width - 1 (width 1-16)'), k = 6/7 (polynomial), k = 8 (crc_start
    0-15, crc_ones); 'The CRC covers the data bits of bytes crc_start
    onward'. One packet per setting, exact waveform:
    CRC-8 from byte 1; a 5-bit all-ones inverted CRC (USB CRC5 shape);
    CRC-16/0x1021 with NRZI; width 1 (parity); crc_tx = 0 with a CRC set up
    (nothing appended).
    ASSUMPTION (last case): crc_start past the end of the packet covers no
    bits, so the CRC is its start value (all ones, inverted: 8 zeros)."""
    chip = await new_chip(dut)
    cases = [
        ("crc8 from byte 1", SerCfg(P=5, S=2, crc_tx=1, width=8, poly=0x07, crc_start=1),
         [0x81, 0x31, 0x32, 0x33]),
        ("crc5 ones inv", SerCfg(P=4, S=2, crc_tx=1, crc_inv=1, width=5, poly=0x05, crc_ones=1),
         [0x81, 0x5A]),
        ("crc16 0x1021 nrzi", SerCfg(P=3, S=1, nrzi=1, crc_tx=1, width=16, poly=0x1021),
         [0x02, 0x80, 0x55]),
        ("width 1", SerCfg(P=3, S=1, crc_tx=1, width=1, poly=1), [0x07, 0x01]),
        ("crc_tx off", SerCfg(P=3, S=1, crc_tx=0, crc_inv=1, width=8, poly=0x07), [0x5A]),
        ("crc_start past end", SerCfg(P=3, S=1, crc_tx=1, crc_inv=1, crc_ones=1, width=8,
                                      poly=0x07, crc_start=3), [0x11, 0x22]),
    ]
    fails = []
    for name, cfg, data in cases:
        bits, info = ser_tx_bits(cfg, data)
        a = Asm()
        fw_ser_config(a, cfg)
        for i, b in enumerate(data):
            a.ldi(i % 4, b)
            a.stx(i % 4)
        fw_spin(a)
        await chip.run(a)
        exp = expected_trace(bits, cfg.P, "Z")
        t0 = await tx_capture(chip, ("Z", "Z"), len(exp) + 60)
        m = tx_mismatch(chip, t0, exp, ("Z", "Z"), 60, cfg.P)
        if m:
            fails.append((name, hex(info["crc"]), m))
    assert not fails, f"CRC settings (name, model CRC, mismatch): {fails}"


@cocotb.test()
async def test_ser_tx_owns_pins_then_restores_set_state(dut):
    """serial_engine.md Pins: 'While it transmits the engine owns the pins
    it drives: they are push-pull and follow the engine, whatever SET/OUTB
    last did to them. When the packet is over the pins go back to their
    sticky SET mode and value.' Pin 0 is SET push-pull 0, pin 1 open-drain
    released (Z, pulled up). During the packet pin 1 is driven push-pull
    (both levels, not released on 1). Mid-packet the firmware does SET p0
    KEEP 1 and OUTB p1 <- 0: no effect while the engine owns the pins;
    afterwards pin 0 drives 1 (push-pull) and pin 1 drives 0 (open-drain
    low)."""
    chip = await new_chip(dut)
    cfg = USB_CFG.but(crc_tx=0)
    data = [0x80, 0xD2, 0x5A]
    a = Asm()
    a.set(0, 0, PP)
    a.set(1, 1, OD)
    fw_ser_config(a, cfg)
    for i, b in enumerate(data):
        a.ldi(i % 4, b)
        a.stx(i % 4)
    a.delay(100)
    a.set(0, 1, KEEP)
    a.ldi(3, 0)
    a.outb(3, 1, 0)
    fw_spin(a)
    bits, _ = ser_tx_bits(cfg, data)
    exp = expected_trace(bits, 33, "Z")
    await chip.run(a, ext={1: 1})
    t0 = await tx_capture(chip, ("0", "Z"), len(exp) + 100)
    m = tx_mismatch(chip, t0, exp, ("1", "0"), 100, 33)
    assert m is None, f"pin ownership / restore: {m}"


@cocotb.test()
async def test_ser_stx_blocks_while_slot_full(dut):
    """serial_engine.md STX: 'The engine holds one byte waiting plus the one
    shifting out ... STX blocks while the waiting slot is full.' (isa.md
    STX row: 'Blocks (like WAIT, holding EXECUTE)'.) NRZ, P = 33, no stuffing
    or CRC: four STXs back to back. STX 1 and 2 do not wait a byte time;
    STX 3 waits until byte 1 has gone (about 8P after the first bit); STX 4
    waits exactly one more byte time (8P) after STX 3. The four bytes go
    out as one contiguous 32-bit packet.
    ASSUMPTION: the waiting slot frees at the same point of every byte, so
    the STX3 -> STX4 unblock distance is exactly 8P; 'not a byte time' for
    STX 1-2 is bounded as < 6 + P."""
    chip = await new_chip(dut)
    P = 33
    cfg = SerCfg(P=P, S=16)
    data = [0x35, 0xCA, 0x0F, 0xF1]
    a = Asm()
    a.set(MK, 0, PP)
    a.set(MK2, 0, PP)
    fw_ser_config(a, cfg)
    for i, b in enumerate(data):
        a.ldi(i, b)
    a.set(MK2, 1, KEEP)
    a.stx(0)
    a.stx(1)
    a.set(MK2, 0, KEEP)
    a.stx(2)
    a.set(MK, 1, KEEP)
    a.stx(3)
    a.set(MK, 0, KEEP)
    fw_spin(a)
    bits, _ = ser_tx_bits(cfg, data)
    exp = expected_trace(bits, P, "Z")
    await chip.run(a)
    t0 = await tx_capture(chip, ("Z", "Z"), len(exp) + 100)
    m = tx_mismatch(chip, t0, exp, ("Z", "Z"), 100, P)
    assert m is None, f"4 queued bytes not one contiguous packet: {m}"
    e2 = chip.pin_edges(MK2)
    e1 = chip.pin_edges(MK)
    r2 = [c for c, s in e2 if s == "1"][-1]
    f2 = [c for c, s in e2 if s == "0" and c > r2][0]
    assert f2 - r2 < 6 + P, f"STX 1-2 took {f2 - r2 - 3} cycles: the waiting slot was not free"
    r1 = [c for c, s in e1 if s == "1"][-1]
    f1 = [c for c, s in e1 if s == "0" and c > r1][0]
    assert 6 * P <= r1 - t0 <= 8 * P + 12, \
        f"STX 3 unblocked {r1 - t0} cycles after the first bit; spec: when byte 1 leaves (~{8 * P})"
    assert f1 - r1 == 8 * P, f"STX 4 unblocked {f1 - r1} cycles after STX 3; spec one byte time {8 * P}"


@cocotb.test()
async def test_ser_tx_whole_byte_time_then_separate_packet(dut):
    """serial_engine.md: 'firmware has a whole byte time to queue the next';
    a packet 'ends when the engine runs out of bytes'. Byte 2 queued 6 bit
    times after byte 1 still continues the same packet (16 contiguous
    bits); byte 3, queued long after the first packet ended, starts a new
    packet (pins released in between). NRZ, P = 33."""
    chip = await new_chip(dut)
    P = 33
    cfg = SerCfg(P=P, S=16)
    a = Asm()
    fw_ser_config(a, cfg)
    a.ldi(0, 0x69); a.ldi(1, 0x96); a.ldi(2, 0xB3)
    a.stx(0)
    a.delay(6 * P)
    a.stx(1)
    a.delay(42, 1)                    # 1344 clocks: packet 1 (528) is long over
    a.stx(2)
    fw_spin(a)
    b1, _ = ser_tx_bits(cfg, [0x69, 0x96])
    b2, _ = ser_tx_bits(cfg, [0xB3])
    e1 = expected_trace(b1, P, "Z")
    await chip.run(a)
    t0 = await tx_capture(chip, ("Z", "Z"), len(e1) + 20)
    m = tx_mismatch(chip, t0, e1, ("Z", "Z"), 20, P)
    assert m is None, f"byte queued within a byte time did not continue the packet: {m}"
    e2 = expected_trace(b2, P, "Z")
    t1 = await tx_capture(chip, ("Z", "Z"), len(e2) + 50)
    m = tx_mismatch(chip, t1, e2, ("Z", "Z"), 50, P)
    assert m is None, f"second packet: {m}"


# ------------------------------------------------------------------ receive
def rx_collect_prog(cfg, nmax=12, extra=None):
    """Configure; MK = 1 (ready); collect bytes until the end; data[0] =
    count, [1] = status, [2]/[3] = CRC bits 7:0 / 15:8 (SRD k = 0/1/2),
    [4..] = bytes; report 4 + nmax bytes."""
    a = Asm()
    fw_ser_config(a, cfg)
    a.set(MK, 1, PP)
    fw_rx_collect(a, nmax, 4, 0, done_hook=extra)
    a.srd(2, 0); a.store(2, 1)
    a.srd(2, 1); a.store(2, 2)
    a.srd(2, 2); a.store(2, 3)
    fw_report(a, 4 + nmax)
    a.halt()
    return a


async def rx_run(chip, prog, wave, idle_ext, n):
    await chip.run(prog, data=[0] * 32, ext={0: idle_ext[0], 1: idle_ext[1]})
    await chip.wait_drive(MK, "1", 3000, "firmware ready marker")
    await chip.clk(30)
    t_play = chip.cycle
    await play_wave(chip, wave)
    return t_play, await chip.host_read_n(n, timeout=30000)


@cocotb.test()
async def test_ser_rx_usb_packet_and_crc_residual(dut):
    """serial_engine.md Receive with the USB settings: a packet starts when
    the receive pin 'leaves the idle level'; samples S after each edge, then
    every P; stuff bits dropped; NRZI-decoded data bits 'shifted in LSB
    first. Every 8 data bits make a byte' (so SYNC and the 2 CRC field
    bytes are delivered too); SE0 ends the packet; the end is signalled once
    the line is idle (J). SRX: byte -> flag 0, end -> flag 1. SRD: status
    after the end = 0x04 ('2 last packet ended by SE0'; nothing waiting, not
    in a packet, line J so bits 6/7 = 0); 'for USB CRC16 that is the
    residual 0x800D when the packet is good' (SRD k = 1 -> 0x0D, k = 2 ->
    0x80). Waveform from the reference encoder (stuff bits included)."""
    chip = await new_chip(dut)
    data = [0x80, 0xC3, 0xFF, 0xFF, 0x00, 0x7F, 0xFE, 0x01]
    bits, info = ser_tx_bits(USB_CFG, data)
    assert len(info["stuff"]) >= 3
    wave = bits_to_wave(bits, 33, lead=0, tail=300, idle_pins=J)
    exp_bytes = usb_packet_bytes(data)
    summ, crcs = rx_wave_robust(USB_CFG, wave)
    assert summ == [("byte", b) for b in exp_bytes] + [("end", "se0")] and crcs == [0x800D]
    _t, got = await rx_run(chip, rx_collect_prog(USB_CFG), wave, J, 16)
    n = len(exp_bytes)
    assert got[0] == n, f"bytes before the end: {got[0]}, spec {n} ({got})"
    assert got[4:4 + n] == exp_bytes, f"bytes {[hex(x) for x in got[4:4 + n]]} vs {[hex(x) for x in exp_bytes]}"
    assert got[1] == 0x04, f"status after a good packet {got[1]:#04x}, spec 0x04"
    assert (got[2], got[3]) == (0x0D, 0x80), f"CRC register {got[3]:02x}{got[2]:02x}, spec residual 800d"


@cocotb.test()
async def test_ser_rx_resyncs_to_other_bit_rates(dut):
    """serial_engine.md: 'Every edge restarts the bit timer, and the line is
    sampled S clocks after the last edge'; USB settings table: 'P 33 ...
    1.5 Mbit/s is 33.3 clocks: 33 is 1.0% fast'. Packets sent at 33.333
    (the real USB rate), 32 and 34 clocks a bit (about 3% either way) are
    received exactly, residual 0x800D. (The expected result is checked in
    the reference model to not depend on a +-2 cycle sample phase or a 1-4
    cycle synchronizer.)"""
    chip = await new_chip(dut)
    data = [0x80, 0x4B, 0x3F, 0xFF, 0xA5]
    bits, _ = ser_tx_bits(USB_CFG, data)
    exp_bytes = usb_packet_bytes(data)
    fails = []
    for per in (33.333, 32.0, 34.0):
        wave = bits_to_wave(bits, per, lead=0, tail=300, idle_pins=J)
        assert rx_wave_robust(USB_CFG, wave)[0][-1] == ("end", "se0")
        _t, got = await rx_run(chip, rx_collect_prog(USB_CFG, nmax=8), wave, J, 12)
        n = len(exp_bytes)
        if got[0] != n or got[4:4 + n] != exp_bytes or got[1] != 0x04 or got[2:4] != [0x0D, 0x80]:
            fails.append((per, got))
    assert not fails, f"(bit period, report [count, status, crc lo, crc hi, bytes...]): {fails}"


@cocotb.test()
async def test_ser_rx_stuff_error_end_waits_for_idle(dut):
    """serial_engine.md: 'On receive, a stuff bit is dropped; if it is not a
    level change, that is a stuff error and the packet ends.' 'After the end
    (SE0 or a stuff error) the engine waits for the line to be idle (not
    SE0, receive pin at the idle level), then signals the end.' SRD bit 3
    'stuff error' (sticky); bit 2 stays 0. SYNC + PID, then the line stays
    at K (D+ high) for 1500 clocks -- no stuff bit after 7 equal levels --
    then J. The bytes before the error are delivered; the 4 data bits after
    the PID are not a byte. The end (SRX flag 1, marker on pin 3) must come
    only after J returns."""
    chip = await new_chip(dut)
    wave, t_idle = usb_stuff_error_wave(hold=1500, lead=0)
    summ, _ = rx_wave_robust(USB_CFG, wave)
    assert summ == [("byte", 0x80), ("byte", 0xC3), ("end", "stuff")]
    prog = rx_collect_prog(USB_CFG, nmax=6, extra=lambda a: a.set(MK2, 1, PP))
    t_play, got = await rx_run(chip, prog, wave, J, 10)
    assert got[0] == 2 and got[4:6] == [0x80, 0xC3], f"report {got}"
    assert got[1] == 0x08, f"status after a stuff error {got[1]:#04x}, spec 0x08"
    rise = [c for c, s in chip.pin_edges(MK2) if s == "1"]
    assert rise, "SRX never signalled the end"
    assert rise[0] > t_play + t_idle, \
        f"end signalled at {rise[0] - t_play}, before the line was idle (J at {t_idle})"
    assert rise[0] < t_play + t_idle + 120, f"end signalled {rise[0] - t_play - t_idle} cycles after idle"


@cocotb.test()
async def test_ser_rx_can_like_on_pin1_crc15_residual(dut):
    """serial_engine.md: 'rx_pin: receive from pin 0 or pin 1'; CAN-style
    stuffing K = 5 (NRZ); 'for CAN CRC-15, 0' residual. Receive on pin 1,
    idle level 0, while pin 0 toggles (it must be ignored: no se0 mode).
    After the CRC field the line returns to 0: five equal levels and then
    no stuff bit is a stuff error, which ends the packet; the line is
    already idle, so the end is signalled. Status 0x08.
    AMBIGUOUS: the doc does not say how the CRC field is delimited when a
    packet ends by stuff error; the trailing bits here are zeros, which
    leave a zero CRC unchanged, so the residual is 0 under either reading
    (with a real CAN frame -- idle 1, EOF ones -- it would not be)."""
    chip = await new_chip(dut)
    cfg = CAN_CFG.but(idle=0, rx_pin=1, rx_en=1)
    data = [0x35, 0x00, 0xFF, 0x80]
    bits, _ = ser_tx_bits(cfg, data)
    lv = [p0 for p0, _ in bits]
    wave = []
    for i, l in enumerate(lv):
        for j in range(cfg.P):
            wave.append((((i * cfg.P + j) // 7) & 1, l))
    wave += [(1, 0)] * 400
    summ, crcs = rx_wave_robust(cfg, wave)
    assert summ[:4] == [("byte", b) for b in data] and summ[-1] == ("end", "stuff") and crcs == [0]
    exp_bytes = [v for k, v in summ if k == "byte"]
    _t, got = await rx_run(chip, rx_collect_prog(cfg, nmax=10), wave, (1, 0), 14)
    n = len(exp_bytes)
    assert got[0] == n and got[4:4 + n] == exp_bytes, \
        f"bytes {got[:4 + n]}, spec count {n} {exp_bytes}"
    assert got[1] == 0x08, f"status {got[1]:#04x}, spec 0x08 (stuff error)"
    assert got[2:4] == [0, 0], f"CRC-15 residual {got[2:4]}, spec 0"


@cocotb.test()
async def test_ser_rx_sample_point_and_edge_resync(dut):
    """serial_engine.md: 'the line is sampled S clocks after the last edge,
    then every P clocks while no edge comes'; 'Every edge restarts the bit
    timer'; S = k3[3:0]:k2, P = k1[3:0]:k0. NRZ, idle 0, no stuffing, P =
    600. The line goes high for 400 clocks, low for 4500, high for 4800,
    then low. With S = 100 the short first level is sampled: bytes 0x01,
    0xFE. With S = 500 (needs k3) the edge at 400 restarts the timer before
    any sample: bytes 0x80, 0x7F. (S = 244, i.e. k3 ignored, would give
    0x01.)"""
    chip = await new_chip(dut)
    I_, H_ = (0, 1), (1, 1)
    wave = [H_] * 400 + [I_] * 4500 + [H_] * 4800 + [I_] * 3000
    fails = []
    for S, exp in ((100, [0x01, 0xFE]), (500, [0x80, 0x7F])):
        cfg = SerCfg(P=600, S=S, rx_en=1)
        summ, _ = rx_wave_robust(cfg, wave, ds=(1, 2, 3, 4, 5), s_adjs=(-3, 0, 3))
        assert summ == [("byte", b) for b in exp]
        a = Asm()
        fw_ser_config(a, cfg)
        a.set(MK, 1, PP)
        a.srx(0); a.store(0, 0)
        a.srx(0); a.store(0, 1)
        fw_report(a, 2)
        a.halt()
        _t, got = await rx_run(chip, a, wave, I_, 2)
        if got != exp:
            fails.append((S, [hex(x) for x in got], [hex(x) for x in exp]))
    assert not fails, f"(S, got, spec): {fails}"


@cocotb.test()
async def test_ser_srx_outcomes_and_flag(dut):
    """serial_engine.md / isa.md SRX row. A handshake packet SYNC, 0xD2:
      * byte waiting: 'Rd = the byte, flag = 0' (flag preset 1 by CMP);
      * SRD status mid-packet: bit 1 'in a received packet' = 1, bit 0 = 0
        (the byte was taken);
      * after the packet, before reading on: bit 0 (PID waiting) + bit 2
        (SE0) = 0x05;
      * 'A byte is reported before an end that came after it': SRX -> PID,
        flag 0;
      * the end: 'Rd unchanged, flag = 1', quickly (well inside its 4000-
        clock timeout);
      * 'the end is consumed: the next SRX waits for a new byte' -> its 992-
        clock timeout expires: Rd unchanged, flag = 1, >= 992 clocks;
      * status afterwards 0x04."""
    chip = await new_chip(dut)
    a = Asm()
    fw_ser_config(a, USB_CFG)
    a.set(MK2, 0, PP)
    a.ldi(0, 0x5C)
    a.set(MK, 1, PP)
    a.cmp(1, 1)
    a.srx(0)
    fw_store_flag(a, 1, 0)
    a.store(0, 1)
    a.srd(2, 0); a.store(2, 2)
    a.delay(94, 1)                    # 3008 clocks: packet and EOP are over
    a.srd(2, 0); a.store(2, 3)
    a.cmp(1, 1)
    a.srx(0)
    fw_store_flag(a, 1, 4)
    a.store(0, 5)
    a.ldi(0, 0x5C)
    a.set(MK2, 1, KEEP)
    a.srx(0, 125, 1)                  # 4000 clocks
    a.set(MK2, 0, KEEP)
    fw_store_flag(a, 1, 6)
    a.store(0, 7)
    a.set(MK2, 1, KEEP)
    a.srx(0, 31, 1)                   # 992 clocks
    a.set(MK2, 0, KEEP)
    fw_store_flag(a, 1, 8)
    a.store(0, 9)
    a.srd(2, 0); a.store(2, 10)
    fw_report(a, 11)
    a.halt()
    bits, _ = ser_tx_bits(USB_CFG.but(crc_tx=0), [0x80, 0xD2])
    wave = bits_to_wave(bits, 33, tail=200, idle_pins=J)
    assert rx_wave_robust(USB_CFG, wave)[0] == [("byte", 0x80), ("byte", 0xD2), ("end", "se0")]
    _t, got = await rx_run(chip, a, wave, J, 11)
    assert got[0:2] == [0, 0x80], f"first SRX (flag, Rd) = {got[0:2]}, spec (0, 0x80)"
    assert got[2] & 0x3F == 0x02, f"status mid-packet {got[2]:#04x}: spec bit 1 set, bit 0 clear"
    assert got[3] == 0x05, f"status with PID waiting after the end {got[3]:#04x}, spec 0x05"
    assert got[4:6] == [0, 0xD2], f"byte before end: (flag, Rd) = {got[4:6]}, spec (0, 0xD2)"
    assert got[6:8] == [1, 0x5C], f"end: (flag, Rd) = {got[6:8]}, spec (1, unchanged 0x5C)"
    assert got[8:10] == [1, 0x5C], f"after consumed end: (flag, Rd) = {got[8:10]}, spec (1, 0x5C)"
    assert got[10] == 0x04, f"final status {got[10]:#04x}, spec 0x04"
    e = chip.pin_edges(MK2)
    rises = [c for c, s in e if s == "1"]
    falls = [c for c, s in e if s == "0" and c > rises[0]]
    g_end, g_to = falls[0] - rises[0], falls[1] - rises[1]
    assert g_end < 100, f"SRX with an end pending took {g_end} cycles (timeout 4000)"
    assert g_to >= 992, f"SRX after the end was consumed returned after {g_to} cycles (< timeout 992)"


@cocotb.test()
async def test_ser_srx_timeout_encoding_exact(dut):
    """serial_engine.md: 'timeout is mantissa << (exponent * 5) clocks,
    mantissa 0-127 and exponent 0-3 (7 and 2 bits of the immediate)'; isa.md
    SRX row: 'Timeout = imm[6:0] << (imm[8:7] * 5) cycles ... same countdown
    counter as DELAY/WAIT'. Nothing arrives (line idle J), so each SRX
    times out: Rd unchanged (0x5C), flag 1.
    ASSUMPTION: like WAIT ('3+N if a timeout is set', architecture.md) an
    SRX that times out occupies exactly 3 + N cycles, so the marker gap is
    SET (3) + 3 + N."""
    chip = await new_chip(dut)
    cases = [(1, 0), (64, 0), (127, 0), (1, 1), (5, 1), (1, 2), (3, 2), (1, 3)]
    a = Asm()
    fw_ser_config(a, USB_CFG)
    a.set(MK, 0, PP)
    a.delay(10)
    a.ldi(0, 0x5C)
    for m, e in cases:
        a.set(MK, 1, KEEP)
        a.srx(0, m, e)
        a.set(MK, 0, KEEP)
        a.delay(10)
    fw_store_flag(a, 3, 0)
    a.store(0, 1)
    fw_report(a, 2)
    a.halt()
    await chip.run(a, data=[0] * 8, ext={1: 1})
    got = await chip.host_read_n(2, timeout=60000)
    assert got == [1, 0x5C], f"(flag, Rd) after timeouts = {got}, spec (1, 0x5C)"
    e = chip.pin_edges(MK)
    rises = [c for c, s in e if s == "1"]
    falls = [c for c, s in e if s == "0" and c > rises[0]]
    gaps = [f - r for r, f in zip(rises, falls)]
    exp = [6 + delay_cycles(m, ex) for m, ex in cases]
    assert gaps == exp, f"SRX timeout gaps {gaps}, spec {exp} for (mant, exp) {cases}"


@cocotb.test()
async def test_ser_srx_zero_mantissa_waits_forever(dut):
    """serial_engine.md: '0 waits forever, like WAIT'; isa.md SRX row:
    'mantissa 0 = unbounded (WAIT's rule)'. For each exponent 0-3 with
    mantissa 0, SRX stays blocked well past any timeout it could be
    mistaken for, then returns the first byte of a packet (0x80, flag 0)."""
    chip = await new_chip(dut)
    a = Asm()
    fw_ser_config(a, USB_CFG)
    a.set(MK, 0, PP)
    a.set(MK2, 0, PP)
    for e in range(4):
        a.set(MK, 1, KEEP)            # round e: waiting
        a.ldi(0, 0x5C)
        a.cmp(0, 0)
        a.srx(0, 0, e)
        a.set(MK2, 1, KEEP)           # SRX returned
        fw_store_flag(a, 3, 2 * e)
        a.store(0, 2 * e + 1)
        fw_drain(a)
        a.set(MK2, 0, KEEP)
        a.set(MK, 0, KEEP)            # round e done
    fw_report(a, 8)
    a.halt()
    bits, _ = ser_tx_bits(USB_CFG.but(crc_tx=0), [0x80, 0xD2])
    wave = bits_to_wave(bits, 33, tail=100, idle_pins=J)
    await chip.run(a, data=[0] * 16, ext={1: 1})
    rises = lambda: sum(1 for _c, st in chip.pin_edges(MK) if st == "1")  # noqa: E731
    for e, hold in ((0, 2000), (1, 2000), (2, 5000), (3, 40000)):
        await chip.wait_until(lambda: rises() >= e + 1, 4000, f"round {e} ready")
        await chip.clk(20)
        for _ in range(hold):
            await chip.clk()
            assert chip.drive(MK2) == "0", f"SRX mant=0 exp={e} returned with no packet"
        await play_wave(chip, wave)
    got = await chip.host_read_n(8)
    assert got == [0, 0x80] * 4, f"(flag, Rd) per exponent: {got}, spec (0, 0x80) each"


@cocotb.test()
async def test_ser_rx_overrun_and_clear_command(dut):
    """serial_engine.md: 'If a byte completes while the previous one is
    still waiting, the new one replaces it and the sticky overrun bit is
    set.' Firmware reads nothing until a 6-byte packet is over: status =
    0x15 (bit 0 waiting, 2 SE0, 4 overrun). SCFG 15 bit 0: 'clear the
    sticky status bits and a pending end of packet' -> status 0x01 (the
    byte still waits: the command does not name it). SRX then returns the
    *last* byte (the CRC high byte), flag 0; the next SRX gets no end (it
    was cleared) and runs to its 992-clock timeout: flag 1, Rd unchanged.
    Final status 0x00."""
    chip = await new_chip(dut)
    data = [0x80, 0xC3, 0x11, 0x22]
    exp_bytes = usb_packet_bytes(data)
    bits, _ = ser_tx_bits(USB_CFG, data)
    wave = bits_to_wave(bits, 33, tail=200, idle_pins=J)
    a = Asm()
    fw_ser_config(a, USB_CFG)
    a.set(MK2, 0, PP)
    a.set(MK, 1, PP)
    a.wait(GO7, 1)
    a.srd(2, 0); a.store(2, 0)
    a.ldi(3, 1); a.scfg(3, 15)
    a.srd(2, 0); a.store(2, 1)
    a.cmp(0, 0)
    a.srx(0)
    fw_store_flag(a, 3, 2)
    a.store(0, 3)
    a.set(MK2, 1, KEEP)
    a.srx(0, 31, 1)
    a.set(MK2, 0, KEEP)
    fw_store_flag(a, 3, 4)
    a.store(0, 5)
    a.srd(2, 0); a.store(2, 6)
    fw_report(a, 7)
    a.halt()
    await chip.run(a, data=[0] * 8, ext={1: 1})
    await chip.wait_drive(MK, "1", 3000)
    await chip.clk(30)
    await play_wave(chip, wave)
    chip.set_ext(GO7, 1)
    got = await chip.host_read_n(7)
    last = exp_bytes[-1]
    assert got[0] == 0x15, f"status after overrun {got[0]:#04x}, spec 0x15"
    assert got[1] == 0x01, f"status after command 15 bit 0 {got[1]:#04x}, spec 0x01"
    assert got[2:4] == [0, last], f"SRX after overrun (flag, Rd) {got[2:4]}, spec (0, last byte {last:#04x})"
    assert got[4:6] == [1, last], f"SRX after the end was cleared (flag, Rd) {got[4:6]}, spec (1, {last:#04x})"
    assert got[6] == 0x00, f"final status {got[6]:#04x}"
    e = chip.pin_edges(MK2)
    r = [c for c, s in e if s == "1"][0]
    f = [c for c, s in e if s == "0" and c > r][0]
    assert f - r >= 992, f"pending end not cleared by command 15: SRX returned after {f - r} cycles"


@cocotb.test()
async def test_ser_sticky_bits_cleared_when_next_packet_starts(dut):
    """serial_engine.md: 'Bits 2-4 are sticky: set by the event, cleared by
    command 15 bit 0 or when the next packet starts.' Packet A overruns
    (status after draining 0x14); packet B is read in time and ends by SE0
    (0x04: A's overrun gone); packet C ends by a stuff error (0x08: B's SE0
    bit gone). No clear command is used."""
    chip = await new_chip(dut)
    bits_a, _ = ser_tx_bits(USB_CFG, [0x80, 0xC3, 0x11, 0x22])
    wa = bits_to_wave(bits_a, 33, tail=200, idle_pins=J)
    bits_b, _ = ser_tx_bits(USB_CFG.but(crc_tx=0), [0x80, 0xD2])
    wb = bits_to_wave(bits_b, 33, tail=200, idle_pins=J)
    wc, _ = usb_stuff_error_wave(hold=300, lead=0, tail=200)
    a = Asm()
    fw_ser_config(a, USB_CFG)
    a.set(MK, 1, PP)
    a.wait(GO7, 1)
    fw_drain(a)
    a.srd(2, 0); a.store(2, 0)
    a.set(MK, 0, KEEP)
    fw_drain(a)
    a.srd(2, 0); a.store(2, 1)
    a.set(MK, 1, KEEP)
    fw_drain(a)
    a.srd(2, 0); a.store(2, 2)
    fw_report(a, 3)
    a.halt()
    await chip.run(a, data=[0] * 8, ext={1: 1})
    await chip.wait_drive(MK, "1", 3000)
    await chip.clk(30)
    await play_wave(chip, wa)
    chip.set_ext(GO7, 1)
    await chip.wait_drive(MK, "0", 3000, "packet A drained")
    await chip.clk(30)
    await play_wave(chip, wb)
    await chip.wait_drive(MK, "1", 3000, "packet B drained")
    await chip.clk(30)
    await play_wave(chip, wc)
    got = await chip.host_read_n(3)
    assert got == [0x14, 0x04, 0x08], f"status after A, B, C = {[hex(x) for x in got]}, spec 0x14 0x04 0x08"


@cocotb.test()
async def test_ser_rx_abandon_command(dut):
    """serial_engine.md k = 15 bit 1: 'abandon the packet being received
    (wait for idle)'. NRZ, idle 0, P = 40, no stuffing (so the packet only
    ends by abandon). Byte 0xF5 arrives, then the line stays high for 40 bit
    times (it would make five 0xFF bytes) and then returns to idle. After
    the abandon no byte completes: status bits 0/1/3/4 are 0 and an SRX
    with a 100-clock timeout returns flag 1 with Rd unchanged. After the
    line is idle a new packet (0x6B, 0x0D) is received normally.
    AMBIGUOUS: whether an abandoned packet signals an end once idle is not
    stated; the firmware clears any pending end (command 15 bit 0) before
    the second packet, and the status mask leaves out bit 2."""
    chip = await new_chip(dut)
    P = 40
    cfg = SerCfg(P=P, S=20, rx_en=1)
    I_, H_ = (0, 1), (1, 1)
    w1 = bits_to_wave([(b, 1) for b in [(0xF5 >> i) & 1 for i in range(8)]], P)
    w1 += [H_] * (40 * P) + [I_] * 600
    w2 = bits_to_wave([(b, 1) for b in [(x >> i) & 1 for x in (0x6B, 0x0D) for i in range(8)]], P)
    w2 += [I_] * 400
    assert rx_wave_robust(cfg, [I_] * 40 + w2)[0][:2] == [("byte", 0x6B), ("byte", 0x0D)]
    assert ser_rx_summary(ser_rx_decode(cfg, [I_] * 40 + w1)[0])[:3] == \
        [("byte", 0xF5), ("byte", 0xFF), ("byte", 0xFF)]   # what abandon must stop
    a = Asm()
    fw_ser_config(a, cfg)
    a.set(MK, 1, PP)
    a.srx(0); a.store(0, 0)
    a.ldi(3, 2); a.scfg(3, 15)
    a.wait(GO7, 1)
    a.srd(2, 0); a.store(2, 1)
    a.ldi(0, 0x5C)
    a.srx(0, 100, 0)
    fw_store_flag(a, 3, 2)
    a.store(0, 3)
    a.ldi(3, 1); a.scfg(3, 15)
    a.set(MK, 0, KEEP)
    a.srx(0); a.store(0, 4)
    a.srx(0); a.store(0, 5)
    fw_report(a, 6)
    a.halt()
    await chip.run(a, data=[0] * 8, ext={0: 0, 1: 1})
    await chip.wait_drive(MK, "1", 3000)
    await chip.clk(30)
    await play_wave(chip, w1)
    chip.set_ext(GO7, 1)
    await chip.wait_drive(MK, "0", 3000, "ready for packet 2")
    await chip.clk(30)
    await play_wave(chip, w2)
    got = await chip.host_read_n(6)
    assert got[0] == 0xF5, f"first byte {got[0]:#04x}"
    assert got[1] & 0x1B == 0, f"status after abandon {got[1]:#04x}: bytes kept arriving (bits 0/1/3/4)"
    assert got[2:4] == [1, 0x5C], f"SRX after abandon (flag, Rd) {got[2:4]}, spec (1, 0x5C)"
    assert got[4:6] == [0x6B, 0x0D], f"next packet {[hex(x) for x in got[4:6]]}"


@cocotb.test()
async def test_ser_rx_off_while_transmitting(dut):
    """serial_engine.md: 'Receive is off from the first transmitted bit
    until the line is idle again.' USB settings (rx_en on); the pads carry
    the engine's own transmission back to the receiver (architecture.md
    rule (2): uio_in reflects the pad). During the packet SRD bit 5
    'transmit busy' = 1 and bits 0/1 = 0; afterwards status 0x00 and SRX
    with a 992-clock timeout gets nothing (flag 1, Rd unchanged, full
    timeout). Receive works again after: a packet sent by the harness then
    arrives (0x80, 0xD2, end)."""
    chip = await new_chip(dut)
    a = Asm()
    fw_ser_config(a, USB_CFG)
    a.set(MK, 0, PP)
    a.set(MK2, 0, PP)
    for i, b in enumerate([0x80, 0xC3, 0x01, 0x02]):
        a.ldi(1, b)
        a.stx(1)
    a.srd(2, 0); a.store(2, 0)
    a.delay(94, 1)
    a.srd(2, 0); a.store(2, 1)
    a.ldi(0, 0x5C)
    a.set(MK, 1, KEEP)
    a.srx(0, 31, 1)
    a.set(MK, 0, KEEP)
    fw_store_flag(a, 3, 2)
    a.store(0, 3)
    a.set(MK2, 1, KEEP)
    a.srx(0); a.store(0, 4)
    a.srx(0); a.store(0, 5)
    a.ldi(0, 0x5C)
    a.srx(0)
    fw_store_flag(a, 3, 6)
    fw_report(a, 7)
    a.halt()
    bits, _ = ser_tx_bits(USB_CFG.but(crc_tx=0), [0x80, 0xD2])
    wave = bits_to_wave(bits, 33, tail=200, idle_pins=J)
    await chip.run(a, data=[0] * 8, ext={1: 1})
    await chip.wait_drive(MK2, "1", 20000, "firmware ready to receive")
    await chip.clk(30)
    await play_wave(chip, wave)
    got = await chip.host_read_n(7)
    assert got[0] & 0x23 == 0x20, f"status during transmit {got[0]:#04x}: spec busy (bit 5), not receiving"
    assert got[1] == 0x00, f"status after transmit {got[1]:#04x}, spec 0x00 (own packet not received)"
    assert got[2:4] == [1, 0x5C], f"SRX after own packet (flag, Rd) {got[2:4]}, spec (1, 0x5C)"
    e = chip.pin_edges(MK)
    r = [c for c, s in e if s == "1"][0]
    f = [c for c, s in e if s == "0" and c > r][0]
    assert f - r >= 992, f"SRX after own packet returned after {f - r} cycles: something was received"
    assert got[4:7] == [0x80, 0xD2, 1], f"receive after transmit: {got[4:7]}, spec 0x80 0xD2 end"


@cocotb.test()
async def test_ser_srd_live_pins_and_other_k(dut):
    """serial_engine.md SRD table: status bit 6 'SE0 on the pins now', bit 7
    'the receive pin now'; 'other | 0'. USB settings with rx_en = 0 (no
    packet can start); the host sets (D+, D-) to J, K, SE0 and (1,1) and
    reads status: rx_pin = 0 -> 0x00 0x80 0x40 0x80; after SCFG k = 4 with
    rx_pin = 1 -> 0x80 0x00 0x40 0x80. SRD k = 3, 7, 15 write 0 to a
    register holding 0xFF.
    AMBIGUOUS: whether bit 6 reports SE0 outside se0 mode is not stated
    (se0 mode is on here)."""
    chip = await new_chip(dut)
    cfg = USB_CFG.but(rx_en=0)

    def srd_send(a):
        a.wait(PIN_GO, 1)
        a.srd(0, 0)
        a.out(0)
        a.set(PIN_STATUS, 1)
        a.wait(PIN_GO, 0)
        a.set(PIN_STATUS, 0)

    a = Asm()
    fw_ser_config(a, cfg)
    for _ in range(4):
        srd_send(a)
    a.ldi(1, cfg.but(rx_pin=1).flags())
    a.scfg(1, 4)
    for _ in range(4):
        srd_send(a)
    for k in (3, 7, 15):
        a.ldi(1, 0xFF)
        a.srd(1, k)
        fw_send(a, 1)
    a.halt()
    await chip.run(a, ext={1: 1})
    combos = [(0, 1), (1, 0), (0, 0), (1, 1)]
    got = []
    for _rx in (0, 1):
        for p0, p1 in combos:
            chip.set_ext(0, p0)
            chip.set_ext(1, p1)
            await chip.clk(12)
            got.append(await chip.host_read())
    got += await chip.host_read_n(3)
    exp = [0x00, 0x80, 0x40, 0x80, 0x80, 0x00, 0x40, 0x80, 0, 0, 0]
    assert got == exp, f"SRD {[hex(x) if x is not None else x for x in got]}, spec {[hex(x) for x in exp]}"


@cocotb.test()
async def test_ser_flag_and_register_effects(dut):
    """isa.md: SRD 'Flag untouched'; SRX is 'A fifth flag-writer' (CALL
    notes list CMP/TEST-bit/WAIT/ADD/SRX as the flag writers, so SCFG and
    STX do not write it); SCFG/STX: 'Rd is the *source*'; SRD writes Rd
    only. With flag 1 and then flag 0, SCFG, SRD and STX run; the flag
    survives; R0/R1 (sources) are unchanged; SRD k = 3 gives 0 and SRD k = 0
    gives the status (pin 0 high: 0x80) in their destination registers."""
    chip = await new_chip(dut)
    cfg = SerCfg(P=10, S=5)
    a = Asm()
    fw_ser_config(a, cfg)
    a.ldi(0, 0x11); a.ldi(1, 0x22); a.ldi(2, 0x33); a.ldi(3, 0x44)
    a.srd(3, 0)
    a.cmp(0, 0)
    a.scfg(1, 6)
    a.srd(2, 3)
    a.stx(0)
    a.store(3, 10)
    fw_store_flag(a, 3, 0)
    a.ldi(3, 0x99)
    a.cmp(0, 1)
    a.scfg(1, 6)
    a.srd(3, 3)
    a.stx(1)
    a.store(3, 11)
    fw_store_flag(a, 3, 1)
    a.store(0, 2); a.store(1, 3); a.store(2, 4)
    a.load(3, 10); a.store(3, 5)
    a.load(3, 11); a.store(3, 6)
    fw_report(a, 7)
    a.halt()
    await chip.run(a, data=[0] * 16, ext={0: 1, 1: 1})
    got = await chip.host_read_n(7)
    assert got == [1, 0, 0x11, 0x22, 0x00, 0x80, 0x00], \
        f"(flag1, flag0, R0, R1, SRD k3, SRD k0, SRD k3) = {[hex(x) for x in got]}"


@cocotb.test()
async def test_ser_reset_mid_packet(dut):
    """serial_engine.md: 'Registers reset to 0 (engine off)'; architecture.md
    reset state: protocol pins input (uio_oe 0x20); 'Reset -> LOAD'. A reset
    in the middle of a USB transmission releases the pins at once, nothing
    resumes after a new boot, and the status reads 0x00 (not busy, no sticky
    bits; line J)."""
    chip = await new_chip(dut)
    a = Asm()
    fw_ser_config(a, USB_CFG)
    for i, b in enumerate([0x80, 0xC3, 0xFF, 0x00, 0x55, 0xAA]):
        a.ldi(i % 4, b)
        a.stx(i % 4)
    fw_spin(a)
    await chip.run(a, ext={1: 1})
    await chip.wait_until(lambda: chip.drive(1) != "Z", 3000, "transmission started")
    await chip.clk(100)
    await chip.reset()
    for _ in range(20):
        assert chip.oe_int() == 0x20, f"uio_oe {chip.s_oe} after reset mid-packet"
        await chip.clk()
    b = Asm()
    b.wait(PIN_GO, 1)
    b.srd(0, 0)
    b.out(0)
    b.set(PIN_STATUS, 1)
    b.wait(PIN_GO, 0)
    b.set(PIN_STATUS, 0)
    b.halt()
    await chip.boot(b.image())
    chip.set_ext(1, 1)
    await chip.clk(3)
    await chip.start()
    for _ in range(1500):
        await chip.clk()
        assert chip.drive(0) == "Z" and chip.drive(1) == "Z", "engine drove pins after reset"
    assert await chip.host_read() == 0x00


# ===========================================================================
# Code in the data region (isa.md Branch format, 2026-11-04 text)
# ===========================================================================
@cocotb.test()
async def test_code_runs_in_data_region(dut):
    """isa.md Branch format: 'A branch, CALL, RET or fall-through past word
    255 lands in words 256-511, fetches the data region (bytes 512-1023) and
    executes it like any other code. Falling through from word 255 goes to
    256, not 0, and a CALL from there returns to the full 9-bit address.'
    'word w is fetched from bytes 2w/2w+1', so LOAD offset 2*(300-256)+1
    reads the high byte of word 300. Also 3 cycles per instruction there
    (architecture.md 'Uniform 3 cycles/instruction').
    AMBIGUOUS: architecture.md Pipeline still says 'only 0-255 are
    legitimate'; isa.md (newer) is followed."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(1, 0x21)                 # 0
    a.set(0, 0, PP)                # 1
    a.jmp(255)                     # 2
    a.org(10)
    a.ldi(2, 0x23); a.out(2); a.ret()          # 10-12
    a.org(255)
    a.out(1)                       # 255
    a.ldi(1, 0x22)                 # 256 (fall-through)
    a.out(1)                       # 257
    a.call(10)                     # 258 -> returns to 259
    a.load(3, 2 * (300 - 256) + 1)  # 259: high byte of word 300
    a.out(3)                       # 260
    a.jmp(300)                     # 261
    a.org(300)
    a.ldi(0, 0x24)                 # 300 = 0x2024
    a.out(0)                       # 301
    a.set(0, 1, KEEP)              # 302
    a.nop()                        # 303
    a.set(0, 0, KEEP)              # 304
    a.jmp(509)                     # 305
    a.org(509)
    a.ldi(0, 0x25); a.out(0); a.halt()         # 509-511
    words = a.words()
    assert len(words) == 512 and words[300] == 0x2024
    await chip.run(a)
    hist = await chip.wait_uo(6, timeout=3000)
    assert hist == [0x21, 0x22, 0x23, 0x20, 0x24, 0x25], f"uo history {[hex(x) for x in hist]}"
    e = chip.pin_edges(0)
    r = [c for c, s in e if s == "1"][0]
    f = [c for c, s in e if s == "0" and c > r][0]
    assert f - r == 6, f"SET/NOP/SET in the data region: {f - r} cycles, spec 6"


@cocotb.test()
async def test_code_stored_into_data_region_executes(dut):
    """isa.md: words 256-511 are 'the data region's bytes, and the CPU
    fetches and runs them like any other code'; STORE writes data byte 512 +
    imm. Firmware STOREs LDI R3,0x5A / OUT R3 / HALT over words 400-402
    (preloaded as LDI R3,0xEE / OUT R3 / HALT) and jumps there."""
    chip = await new_chip(dut)
    new = Asm(); new.ldi(3, 0x5A); new.out(3); new.halt()
    old = Asm(); old.ldi(3, 0xEE); old.out(3); old.halt()
    a = Asm()
    for i, byte in enumerate(new.image()):
        a.ldi(0, byte)
        a.store(0, 2 * (400 - 256) + i)
    a.jmp(400)
    a.org(400)
    for w in old.words():
        a.raw(w)
    await chip.run(a)
    assert await chip.wait_uo(1, timeout=2000) == [0x5A]


@cocotb.test()
async def test_pc_wraps_from_511_to_0(dut):
    """isa.md: 'the PC, the CALL return register and next-PC are all 9
    bits'. ASSUMPTION: falling through word 511 therefore wraps to word 0
    (not stated in words). Word 0-7 count passes in R1; word 511 is a NOP."""
    chip = await new_chip(dut)
    a = Asm()
    a.ldi(2, 1)        # 0
    a.add(1, 2)        # 1
    a.out(1)           # 2
    a.ldi(3, 2)        # 3
    a.cmp(1, 3)        # 4
    a.beq(7)           # 5
    a.jmp(511)         # 6
    a.halt()           # 7
    a.org(511)
    a.nop()
    await chip.run(a)
    assert await chip.wait_uo(2, timeout=2000) == [1, 2]
