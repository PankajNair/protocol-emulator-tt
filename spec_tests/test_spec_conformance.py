"""Black-box spec-conformance suite for protocol-emulator-tt.

Written from docs/isa.md, docs/architecture.md, docs/protocol_timing.md and
docs/info.md only, against tb.v's pins. Every expected value cites the spec
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
    table '21-31 reserved ... Executes as NOP'. Each reserved opcode (with
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
        # isa.md: reserved range is now `21-31` (19 and 20 became XOR/ADD).
        ("reserved 21", None, lambda a: a.raw(enc_0op(21, 0x7FF)), None),
        # isa.md rows 19/20 are reg-reg ops; architecture.md: 'Uniform 3
        # cycles/instruction for everything -- ... register ops, all of it'.
        ("XOR", None, lambda a: a.xor(1, 2), None),
        ("XOR Rd,Rd", None, lambda a: a.xor(1, 1), None),
        ("ADD no carry", None, lambda a: a.add(1, 2), None),
        ("ADD carry", lambda a: (a.ldi(1, 0xFF), a.ldi(2, 0x01)), lambda a: a.add(1, 2), None),
        ("reserved 31", None, lambda a: a.raw(enc_0op(31, 0x123)), None),
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
    """isa.md Reg-reg format: 'opcode(5) | Rd(2) | Rs(2) | reserved(7)' --
    '7 bits still unused/reserved'.
    AMBIGUOUS: the doc never says what nonzero reserved bits do for a
    reg-reg op (ignored, like NOP's 'low 11 bits are don't-care', or an
    illegal encoding). Tested reading: ignored -- XOR/ADD with reserved =
    0x7F/0x55/0x2A give the same results and flag as with 0, and the pins
    are untouched (an illegal encoding would 'execute as NOP')."""
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
