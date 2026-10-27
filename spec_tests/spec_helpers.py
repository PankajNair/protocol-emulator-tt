"""Spec-derived helpers for the black-box conformance suite.

Everything here is written from docs/isa.md, docs/architecture.md and
docs/protocol_timing.md alone. Nothing is taken from the RTL, the
reference model, the assembler or the existing tests.

Two parts:
  * a pure-Python encoder/assembler (no cocotb needed; self-test with
    `python3 spec_helpers.py`)
  * `Chip`, a pin-level driver: clock, reset, the LOAD-mode boot
    handshake, the START pulse, the runtime host handshake, a pad model
    for uio, and an event log of every output change.
"""

try:  # the encoder half must work without cocotb (self-test)
    import cocotb
    from cocotb.clock import Clock
    from cocotb.triggers import FallingEdge
except ImportError:  # pragma: no cover
    cocotb = None

# ---------------------------------------------------------------------------
# Encoding (isa.md "Instruction formats" and "Opcode table")
# ---------------------------------------------------------------------------
# isa.md Opcode table, `value` column.
OP = dict(NOP=0, HALT=1, RET=2, BRANCH=3, LDI=4, SET=5, OUT=6, IN=7,
          SHIFT=8, DELAY=9, WAIT=10, TEST=11, OUTB=12, INB=13, LOAD=14,
          STORE=15, LOOP=16, CMP=17, LOADX=18)
RESERVED_OPS = list(range(21, 32))  # isa.md: `21-31` reserved
# (parent edit 2026-10-23: was 19-31 until XOR/ADD took 19 and 20; this list
# follows the doc mechanically. Spec tests FOR XOR/ADD are left to an
# independent spec-conformance pass, not written here.)

# isa.md Branch format cond table.
COND_ALWAYS, COND_FLAG_SET, COND_FLAG_CLEAR, COND_CALL = 0, 1, 2, 3

# isa.md SET row: Rd(2) repurposed as sticky drive mode.
KEEP, PP, OD, INPUT = 0, 1, 2, 3

# architecture.md Pin map: pin_index -> uio bit (identity), roles.
PIN_GO, PIN_STATUS, PIN_ERROR = 4, 5, 6
PROTOCOL_PINS = (0, 1, 2, 3, 7)


def _chk(v, bits, name):
    if not isinstance(v, int) or not 0 <= v < (1 << bits):
        raise ValueError(f"{name}={v!r} does not fit in {bits} bits")
    return v


def enc_0op(op, low11=0):
    """0-operand format: opcode = word[15:11]; low 11 bits don't-care."""
    return (_chk(op, 5, "op") << 11) | _chk(low11, 11, "low11")


def enc_branch(op, addr, low2):
    """Branch / LOOP format: opcode(5) | addr(9) | cond-or-Rd(2), MSB-first."""
    return (_chk(op, 5, "op") << 11) | (_chk(addr, 9, "addr") << 2) | _chk(low2, 2, "low2")


def enc_ri(op, rd, imm):
    """Reg+immediate format: opcode(5) | Rd(2) | imm(9)."""
    return (_chk(op, 5, "op") << 11) | (_chk(rd, 2, "rd") << 9) | _chk(imm, 9, "imm")


def enc_rr(op, rd, rs, reserved7=0):
    """Reg-reg format: opcode(5) | Rd(2) | Rs(2) | reserved(7)."""
    return ((_chk(op, 5, "op") << 11) | (_chk(rd, 2, "rd") << 9)
            | (_chk(rs, 2, "rs") << 7) | _chk(reserved7, 7, "reserved7"))


def pin_imm(pin, bit, low5=0):
    """isa.md: pin_index(3) = imm[8:6], value/bit_select/level(1) = imm[5],
    (WAIT only) timeout_mantissa(5) = imm[4:0]."""
    return (_chk(pin, 3, "pin") << 6) | (_chk(bit, 1, "bit") << 5) | _chk(low5, 5, "low5")


def delay_cycles(mantissa, exponent):
    """isa.md DELAY/WAIT rows: cycles = mantissa << (exponent*5)."""
    return mantissa << (exponent * 5)


def words_to_bytes(words):
    """isa.md: word[7:0] is the low byte, streamed/fetched first (PC*2),
    word[15:8] second (PC*2+1)."""
    out = []
    for w in words:
        _chk(w, 16, "word")
        out += [w & 0xFF, (w >> 8) & 0xFF]
    return out


class Asm:
    """Tiny two-pass assembler. Branch targets are word indices
    (architecture.md: addr(9) is a word index, fetch does the x2)."""

    def __init__(self):
        self.items = []
        self.labels = {}
        self._n = 0

    # -- plumbing ---------------------------------------------------------
    def here(self):
        return len(self.items)

    def fresh(self):
        self._n += 1
        return f"_L{self._n}"

    def label(self, name):
        if name in self.labels:
            raise ValueError(f"duplicate label {name}")
        self.labels[name] = len(self.items)
        return name

    def _addr(self, t):
        return t if isinstance(t, int) else self.labels[t]

    def raw(self, w):
        self.items.append(_chk(w, 16, "word"))

    def words(self):
        return [x() if callable(x) else x for x in self.items]

    def image(self):
        return words_to_bytes(self.words())

    # -- 0-operand ----------------------------------------------------------
    def nop(self, low11=0):
        self.raw(enc_0op(OP["NOP"], low11))

    def halt(self, low11=0):
        self.raw(enc_0op(OP["HALT"], low11))

    def ret(self, low11=0):
        self.raw(enc_0op(OP["RET"], low11))

    # -- branch ---------------------------------------------------------------
    def branch(self, cond, target):
        self.items.append(lambda: enc_branch(OP["BRANCH"], self._addr(target), cond))

    def jmp(self, t):
        self.branch(COND_ALWAYS, t)

    def beq(self, t):  # isa.md: cond 01 = if flag set
        self.branch(COND_FLAG_SET, t)

    def bne(self, t):  # isa.md: cond 10 = if flag clear
        self.branch(COND_FLAG_CLEAR, t)

    def call(self, t):
        self.branch(COND_CALL, t)

    def loop(self, rd, t):
        self.items.append(lambda: enc_branch(OP["LOOP"], self._addr(t), _chk(rd, 2, "rd")))

    # -- reg+imm ----------------------------------------------------------------
    def ldi(self, rd, value, imm8=0):
        self.raw(enc_ri(OP["LDI"], rd, (_chk(imm8, 1, "imm8") << 8) | _chk(value, 8, "value")))

    def set(self, pin, value, mode=KEEP):
        self.raw(enc_ri(OP["SET"], mode, pin_imm(pin, value)))

    def out(self, rd):
        self.raw(enc_ri(OP["OUT"], rd, 0))

    def in_(self, rd):
        self.raw(enc_ri(OP["IN"], rd, 0))

    def shift(self, rd, right):
        # isa.md SHIFT: imm(0) = direction, 0 = left, 1 = right
        self.raw(enc_ri(OP["SHIFT"], rd, 1 if right else 0))

    def delay(self, mantissa, exponent=0):
        self.raw(enc_ri(OP["DELAY"], exponent, _chk(mantissa, 9, "mantissa")))

    def wait(self, pin, level, mantissa=0, exponent=0):
        self.raw(enc_ri(OP["WAIT"], exponent, pin_imm(pin, level, mantissa)))

    def test(self, rd, bit):
        self.raw(enc_ri(OP["TEST"], rd, _chk(bit, 3, "bit")))

    @staticmethod
    def _bitsel(bit):
        # isa.md OUTB/INB: "bit is 0 or 7"; bit_select(1) = imm[5].
        # isa.md sub-field packing: "For OUTB/INB, bit_select = 0 selects
        # bit 0 and 1 selects bit 7".
        if bit not in (0, 7):
            raise ValueError("OUTB/INB bit must be 0 or 7")
        return 1 if bit == 7 else 0

    def outb(self, rd, pin, bit):
        self.raw(enc_ri(OP["OUTB"], rd, pin_imm(pin, self._bitsel(bit))))

    def inb(self, rd, pin, bit):
        self.raw(enc_ri(OP["INB"], rd, pin_imm(pin, self._bitsel(bit))))

    def load(self, rd, off):
        self.raw(enc_ri(OP["LOAD"], rd, off))

    def store(self, rd, off):
        self.raw(enc_ri(OP["STORE"], rd, off))

    # -- reg-reg ----------------------------------------------------------------
    def cmp(self, rd, rs, reserved7=0):
        self.raw(enc_rr(OP["CMP"], rd, rs, reserved7))

    def loadx(self, rd, rs, reserved7=0):
        self.raw(enc_rr(OP["LOADX"], rd, rs, reserved7))


# ---------------------------------------------------------------------------
# Firmware macros (architecture.md "Host handshake")
# ---------------------------------------------------------------------------
def fw_send(a, r):
    """Firmware -> host (OUT), host-initiated, exactly as architecture.md
    lists it: WAIT GO,1 / OUT Rd / SET STATUS,1 / WAIT GO,0 / SET STATUS,0."""
    a.wait(PIN_GO, 1)
    a.out(r)
    a.set(PIN_STATUS, 1)
    a.wait(PIN_GO, 0)
    a.set(PIN_STATUS, 0)


def fw_ack(a):
    """Same handshake without the OUT (host reads whatever uo_out holds)."""
    a.wait(PIN_GO, 1)
    a.set(PIN_STATUS, 1)
    a.wait(PIN_GO, 0)
    a.set(PIN_STATUS, 0)


def fw_recv(a, r):
    """Host -> firmware (IN): WAIT GO,1 / IN Rd / SET STATUS,1 / WAIT GO,0 /
    SET STATUS,0."""
    a.wait(PIN_GO, 1)
    a.in_(r)
    a.set(PIN_STATUS, 1)
    a.wait(PIN_GO, 0)
    a.set(PIN_STATUS, 0)


def fw_out_flag(a, scratch, base):
    """OUT (base | flag) using only BEQ (cond 01 = flag set)."""
    t, d = a.fresh(), a.fresh()
    a.beq(t)
    a.ldi(scratch, base)
    a.jmp(d)
    a.label(t)
    a.ldi(scratch, base | 1)
    a.label(d)
    a.out(scratch)


# ---------------------------------------------------------------------------
# Pin-level driver
# ---------------------------------------------------------------------------
class SpecTimeout(AssertionError):
    pass


class Chip:
    """Drives only tb's clk/rst_n/ena/ui_in/uio_in and observes only
    uo_out/uio_out/uio_oe.

    Pad model: architecture.md Pin map, rule (2): "TT's uio_in always
    reflects the physical pad regardless of uio_oe, even on a pin the chip
    itself is driving". tb.v wires uio_in straight from a reg, so this class
    emulates the pad: uio_in[b] = uio_out[b] if uio_oe[b] else ext[b], where
    ext is what the outside world (host, slave, pull-up) puts on the wire.

    All activity happens on the falling clock edge: outputs are sampled,
    logged, then the pad/inputs are updated. Cycle numbers are counts of
    falling edges since the Chip was created.
    """

    def __init__(self, dut):
        self.dut = dut
        self.cycle = 0
        self.ext = 0
        self.s_uo = self.s_oe = self.s_out = "x" * 8
        self.events = []  # (cycle, uo_s, oe_s, out_s) on every change
        self.mark_idx = 0
        self.mark_cycle = 0
        self.mark_state = None
        self.last_echo = None
        self.in_load = False
        self.load_violations = []

    @classmethod
    async def create(cls, dut):
        c = cls(dut)
        cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
        dut.ena.value = 1  # contract: ena is always 1
        dut.ui_in.value = 0
        dut.uio_in.value = 0
        dut.rst_n.value = 0
        return c

    # -- sampling ---------------------------------------------------------------
    @staticmethod
    def _bit(s, b):
        return s[len(s) - 1 - b].lower()

    def _sample(self):
        uo = str(self.dut.uo_out.value)
        oe = str(self.dut.uio_oe.value)
        out = str(self.dut.uio_out.value)
        if (uo, oe, out) != (self.s_uo, self.s_oe, self.s_out):
            self.events.append((self.cycle, uo, oe, out))
        self.s_uo, self.s_oe, self.s_out = uo, oe, out
        if self.in_load:
            # architecture.md LOAD: "uio[3:0] (protocol bus) and uio[6:7]
            # are Hi-Z (oe=0) throughout LOAD"; uio[4] hardwired input;
            # uio[5] HOST_STATUS "permanently 1 ... including LOAD".
            bad = [b for b in (0, 1, 2, 3, 4, 6, 7) if self._bit(oe, b) != "0"]
            if bad or self._bit(oe, 5) != "1":
                self.load_violations.append((self.cycle, oe))

    def oe(self, b):
        return self._bit(self.s_oe, b)

    def out(self, b):
        return self._bit(self.s_out, b)

    @staticmethod
    def _drive_of(oe_s, out_s, b):
        o = Chip._bit(oe_s, b)
        if o == "0":
            return "Z"
        if o == "1":
            v = Chip._bit(out_s, b)
            return v if v in "01" else "X"
        return "X"

    def drive(self, b):
        """'Z' (oe=0), '0'/'1' (driven), or 'X'."""
        return self._drive_of(self.s_oe, self.s_out, b)

    @staticmethod
    def _int(s):
        return int(s, 2) if s and all(c in "01" for c in s) else None

    def uo(self):
        return self._int(self.s_uo)

    def oe_int(self):
        return self._int(self.s_oe)

    def out_int(self):
        return self._int(self.s_out)

    # -- input driving ----------------------------------------------------------
    def _drive_pads(self):
        v = 0
        for b in range(8):
            if self.oe(b) == "1":
                bit = 1 if self.out(b) == "1" else 0
            else:
                bit = (self.ext >> b) & 1
            v |= bit << b
        self.dut.uio_in.value = v

    def set_ext(self, b, v):
        """What the outside world drives on uio[b] (or the pull-up level)."""
        if v:
            self.ext |= 1 << b
        else:
            self.ext &= ~(1 << b)
        self._drive_pads()

    def pad(self, b):
        if self.oe(b) == "1":
            return 1 if self.out(b) == "1" else 0
        return (self.ext >> b) & 1

    def set_ui(self, v):
        self.dut.ui_in.value = v & 0xFF

    async def clk(self, n=1):
        for _ in range(n):
            await FallingEdge(self.dut.clk)
            self.cycle += 1
            self._sample()
            self._drive_pads()

    async def wait_until(self, pred, timeout, what):
        for _ in range(timeout):
            if pred():
                return self.cycle
            await self.clk()
        if pred():
            return self.cycle
        raise SpecTimeout(f"timed out after {timeout} cycles waiting for {what}")

    async def wait_drive(self, b, state, timeout, what=None):
        return await self.wait_until(lambda: self.drive(b) == state, timeout,
                                     what or f"uio[{b}] drive state {state}")

    # -- reset / boot / start ----------------------------------------------------
    async def reset(self, cycles=5):
        self.dut.ena.value = 1
        self.dut.ui_in.value = 0
        self.ext = 0
        self.in_load = False
        self.dut.rst_n.value = 0
        self._drive_pads()
        await self.clk(cycles)
        self.dut.rst_n.value = 1
        await self.clk(1)
        # architecture.md: "Reset -> LOAD"
        self.in_load = True
        await self.clk(1)

    async def load_byte(self, b, settle=2, timeout=200):
        """One write of the LOAD-mode handshake (architecture.md LOAD):
        host drives ui_in, asserts HOST_GO; waits HOST_STATUS=1; reads the
        uo_out echo; lowers HOST_GO; waits HOST_STATUS=0. Returns the echo."""
        self.set_ui(b)
        await self.clk(settle)
        self.set_ext(PIN_GO, 1)
        await self.wait_until(lambda: self.drive(PIN_STATUS) == "1", timeout,
                              "HOST_STATUS=1 ack of a boot byte")
        echo = self.uo()
        self.set_ext(PIN_GO, 0)
        await self.wait_until(lambda: self.drive(PIN_STATUS) == "0", timeout,
                              "HOST_STATUS=0 after HOST_GO dropped (boot)")
        return echo

    @staticmethod
    def expected_echo(k):
        """Echo after the k-th pulse (1-based). architecture.md: echo is the
        counter's low 8 bits post-increment; counter saturates at 1023."""
        return min(k, 1023) & 0xFF

    async def boot(self, image, check_echo=True):
        for k, b in enumerate(image, start=1):
            echo = await self.load_byte(b)
            exp = self.expected_echo(k)
            if check_echo and echo != exp:
                raise AssertionError(f"boot byte #{k}: uo_out echo {echo!r}, "
                                     f"spec expects {exp:#04x}")
            self.last_echo = echo

    def mark(self):
        self.mark_idx = len(self.events)
        self.mark_cycle = self.cycle
        self.mark_state = (self.cycle, self.s_uo, self.s_oe, self.s_out)

    async def start(self, hold=4):
        """START pulse on uio[6] (architecture.md LOAD). Host requirement:
        HOST_GO low before START."""
        assert not (self.ext >> PIN_GO) & 1, "harness bug: HOST_GO high at START"
        self.mark()
        self.set_ext(PIN_ERROR, 1)
        self.in_load = False
        await self.clk(hold)
        self.set_ext(PIN_ERROR, 0)

    async def run(self, prog, data=None, ui=0, hold=4, ext=None):
        """reset -> boot program (+ optional data region) -> START."""
        img = prog.image() if hasattr(prog, "image") else list(prog)
        if data is not None:
            assert len(img) <= 512
            img = img + [0] * (512 - len(img)) + list(data)
            assert len(img) <= 1024
        await self.reset()
        await self.boot(img)
        self.set_ui(ui)
        if ext:
            for b, v in ext.items():
                self.set_ext(b, v)
        await self.clk(3)
        await self.start(hold)

    # -- histories since mark() ------------------------------------------------------
    def _log(self):
        base = [self.mark_state] if self.mark_state else []
        return base + self.events[self.mark_idx:]

    def pin_segments(self, b):
        segs = []
        for (c, _uo, oe, out) in self._log():
            st = self._drive_of(oe, out, b)
            if not segs or segs[-1][1] != st:
                segs.append((c, st))
        return segs

    def pin_hist(self, b, min_dwell=2):
        """Compressed drive-state history of uio[b] since mark(). States that
        last fewer than min_dwell cycles (other than the current one) are
        dropped, so a functional check is not confused with a one-cycle
        oe/out skew; a dedicated test checks atomicity with min_dwell=1."""
        segs = self.pin_segments(b)
        keep = []
        for i, (c, st) in enumerate(segs):
            end = segs[i + 1][0] if i + 1 < len(segs) else None
            if end is not None and end - c < min_dwell:
                continue
            if not keep or keep[-1] != st:
                keep.append(st)
        return keep

    def pin_edges(self, b):
        """(cycle, new_state) for each change of uio[b] after mark()."""
        segs = self.pin_segments(b)
        return segs[1:]

    def uo_hist(self):
        """Compressed uo_out values since mark(), with the value at mark (the
        boot echo) and then a leading 0 stripped (architecture.md: uo_out
        re-cleared at LOAD->FETCH_LO). Values written by OUT follow."""
        vals = []
        for (_c, uo, _oe, _out) in self._log():
            v = self._int(uo)
            v = "X" if v is None else v
            if not vals or vals[-1] != v:
                vals.append(v)
        if vals:
            vals.pop(0)          # the state at mark(): the last boot echo
        if vals and vals[0] == 0:
            vals.pop(0)          # the LOAD->FETCH_LO clear
        return vals

    def uo_edges(self):
        out = []
        prev = None
        for (c, uo, _oe, _out) in self._log():
            if prev is not None and uo != prev:
                out.append((c, self._int(uo)))
            prev = uo
        return out

    async def wait_uo(self, n, timeout=20000, settle=80):
        await self.wait_until(lambda: len(self.uo_hist()) >= n, timeout,
                              f"{n} distinct uo_out values (have {self.uo_hist()})")
        await self.clk(settle)
        return self.uo_hist()

    # -- runtime host handshake (architecture.md Host handshake) -------------------
    async def host_write(self, b, timeout=20000, scramble_after_ack=True):
        """Host -> firmware: drive ui_in, assert GO, wait STATUS=1, drop GO,
        wait STATUS=0. "the host must hold ui_in stable until it observes
        HOST_STATUS=1" -- so after the ack it is legal to change ui_in."""
        self.set_ui(b)
        await self.clk(2)
        self.set_ext(PIN_GO, 1)
        await self.wait_until(lambda: self.drive(PIN_STATUS) == "1", timeout, "IN ack STATUS=1")
        self.set_ext(PIN_GO, 0)
        if scramble_after_ack:
            self.set_ui(b ^ 0xFF)
        await self.wait_until(lambda: self.drive(PIN_STATUS) == "0", timeout, "IN STATUS=0")

    async def host_read(self, timeout=20000):
        """Firmware -> host: assert GO, wait STATUS=1, read uo_out, drop GO,
        wait STATUS=0."""
        if self.drive(PIN_STATUS) != "0":
            raise AssertionError("host_read: HOST_STATUS not idle-low before request")
        self.set_ext(PIN_GO, 1)
        await self.wait_until(lambda: self.drive(PIN_STATUS) == "1", timeout, "OUT STATUS=1")
        v = self.uo()
        self.set_ext(PIN_GO, 0)
        await self.wait_until(lambda: self.drive(PIN_STATUS) == "0", timeout, "OUT STATUS=0")
        return v

    async def host_read_n(self, n, timeout=20000):
        return [await self.host_read(timeout) for _ in range(n)]


# ---------------------------------------------------------------------------
# Self-test of the pure encoder (hand-derived from the spec's bit layout)
# ---------------------------------------------------------------------------
def _decode(w):
    """Independent field slicer straight from isa.md's stated positions."""
    return dict(op=w >> 11, rd=(w >> 9) & 3, imm=w & 0x1FF, addr=(w >> 2) & 0x1FF,
                low2=w & 3, rs=(w >> 7) & 3, res=w & 0x7F, pin=(w >> 6) & 7,
                bit5=(w >> 5) & 1, mant=w & 0x1F)


def _selftest():
    a = Asm()
    a.nop(); a.halt(); a.ret()
    a.jmp(5); a.beq(5); a.bne(5); a.call(5)
    a.branch(3, 511)
    a.ldi(1, 0xA5)
    a.set(7, 1, PP)
    a.out(3); a.in_(2)
    a.shift(0, True)
    a.delay(162, 1)
    a.wait(4, 1)
    a.wait(7, 1, 31, 3)
    a.test(2, 5)
    a.outb(0, 0, 7)
    a.inb(1, 1, 0)
    a.load(3, 511)
    a.store(0, 256)
    a.loop(1, 2)
    a.cmp(1, 2)
    a.loadx(0, 3)
    a.raw(enc_0op(19)); a.raw(enc_0op(31))
    expect = [0x0000, 0x0800, 0x1000,
              0x1814, 0x1815, 0x1816, 0x1817, 0x1FFF,
              0x22A5, 0x2BE0, 0x3600, 0x3C00, 0x4001, 0x4AA2,
              0x5120, 0x57FF, 0x5C05, 0x6020, 0x6A40, 0x77FF, 0x7900,
              0x8009, 0x8B00, 0x9180, 0x9800, 0xF800]
    got = a.words()
    assert got == expect, [f"{g:#06x}/{e:#06x}" for g, e in zip(got, expect) if g != e]
    # field-level cross-check against the independent slicer
    d = _decode(enc_ri(OP["WAIT"], 2, pin_imm(6, 0, 17)))
    assert (d["op"], d["rd"], d["pin"], d["bit5"], d["mant"]) == (10, 2, 6, 0, 17)
    d = _decode(enc_branch(OP["LOOP"], 300, 3))
    assert (d["op"], d["addr"], d["low2"]) == (16, 300, 3)
    d = _decode(enc_rr(OP["CMP"], 3, 1, 0x55))
    assert (d["op"], d["rd"], d["rs"], d["res"]) == (17, 3, 1, 0x55)
    assert words_to_bytes([0x22A5, 0x0800]) == [0xA5, 0x22, 0x00, 0x08]
    # labels resolve to word indices
    b = Asm(); b.jmp("t"); b.nop(); b.label("t"); b.halt()
    assert b.words()[0] == enc_branch(3, 2, 0)
    # protocol_timing.md DELAY table and isa.md reach numbers
    assert delay_cycles(162, 1) == 5184 and delay_cycles(81, 1) == 2592
    assert delay_cycles(40, 1) == 1280 and delay_cycles(27, 1) == 864
    assert delay_cycles(422, 0) == 422 and delay_cycles(511, 1) == 16352
    assert delay_cycles(31, 3) == 1015808  # isa.md WAIT max reach
    # echo model
    assert [Chip.expected_echo(k) for k in (1, 255, 256, 257, 1022, 1023, 1024, 1030)] == \
        [1, 255, 0, 1, 254, 255, 255, 255]
    # range checks
    for bad in (lambda: Asm().ldi(4, 0), lambda: Asm().delay(512),
                lambda: Asm().outb(0, 0, 3), lambda: Asm().set(8, 0)):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("range check missing")
    print("spec_helpers self-test: OK")


if __name__ == "__main__":
    _selftest()
