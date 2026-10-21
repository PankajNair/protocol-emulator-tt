# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Host side of the protocol emulator: load a program, start it, talk to it.

Runs on the Tiny Tapeout demo board in MicroPython (with the TT SDK,
tt-micropython-firmware), or anywhere else that can drive the pins.

    import pe_host
    h = pe_host.Host(pe_host.TTDemoBoardIO())
    h.load_hex("uart.hex")      # from firmware/isa/asm.py -o uart.hex
    h.send(0x41)                # IN handshake: byte to the firmware
    b = h.recv()                # OUT handshake: byte from the firmware
    h.error()                   # HOST_ERROR pin

The chip forgets its program when it is reset, powered down or
deselected, so call load_hex again after any of those.

The protocol (docs/architecture.md, LOAD and Host handshake) is written
once, as generators that yield pin operations; a small driver runs them
on a given IO object. TTDemoBoardIO runs them on the board;
test/test_host_loader.py runs the same generators against the RTL in
simulation, so this file is tested even though no chip exists yet.

Pin operations (what an IO object must implement):
    reset()                 hold rst_n low, then release it
    set_ui(byte)            drive ui_in
    get_uo() -> byte        read uo_out
    set_uio(bit, level)     drive a uio pin (host-driven: HOST_GO, START)
    release_uio(bit)        stop driving a uio pin
    get_uio(bit) -> level   read a uio pin as the chip drives it
    hold()                  wait long enough for the chip to see a pulse
                            (a few clock cycles; synchronizer + edge)
    wait_uio(bit, level, timeout_ms) -> bool
"""

HOST_GO, HOST_STATUS, START, HOST_ERROR = 4, 5, 6, 6
TIMEOUT_MS = 1000


class HostError(Exception):
    pass


# ---- protocol, as generators of pin operations ------------------------------

def _wait(bit, level, what):
    ok = yield ("wait_uio", bit, level, TIMEOUT_MS)
    if not ok:
        raise HostError("timeout waiting for HOST_STATUS=%d %s" % (level, what))


def load(image):
    """Reset the chip, stream `image` (bytes) into its SRAM, start it."""
    if not 0 < len(image) <= 1024:
        raise HostError("image must be 1..1024 bytes, got %d" % len(image))
    yield ("release_uio", START)
    yield ("set_uio", HOST_GO, 0)
    yield ("set_uio", START, 0)
    yield ("reset",)
    for k, b in enumerate(image, 1):
        yield ("set_ui", b)
        yield ("set_uio", HOST_GO, 1)
        yield from _wait(HOST_STATUS, 1, "(boot byte %d)" % k)
        echo = yield ("get_uo",)
        if echo != k & 0xFF:  # the chip echoes its byte count
            raise HostError("boot byte %d: chip echoed %d, expected %d" % (k, echo, k & 0xFF))
        yield ("set_uio", HOST_GO, 0)
        yield from _wait(HOST_STATUS, 0, "(boot byte %d)" % k)
    # START: a pulse, with HOST_GO low; once it falls, uio[6] becomes the
    # chip's HOST_ERROR output, so stop driving it
    yield ("set_uio", START, 1)
    yield ("hold",)
    yield ("set_uio", START, 0)
    yield ("hold",)
    yield ("release_uio", START)


def send(byte):
    """IN handshake: give the running firmware one byte."""
    yield ("set_ui", byte & 0xFF)
    yield ("set_uio", HOST_GO, 1)
    yield from _wait(HOST_STATUS, 1, "(send)")
    yield ("set_uio", HOST_GO, 0)
    yield from _wait(HOST_STATUS, 0, "(send)")


def recv():
    """OUT handshake (host-initiated): ask the firmware for one byte."""
    yield ("set_uio", HOST_GO, 1)
    yield from _wait(HOST_STATUS, 1, "(recv)")
    b = yield ("get_uo",)
    yield ("set_uio", HOST_GO, 0)
    yield from _wait(HOST_STATUS, 0, "(recv)")
    return b


def parse_hex(text):
    """asm.py's image format: one hex byte per line."""
    return [int(ln, 16) for ln in (l.strip() for l in text.splitlines()) if ln]


# ---- synchronous driver -----------------------------------------------------

def run(gen, io):
    """Run a protocol generator on a synchronous IO object."""
    result = None
    while True:
        try:
            op = gen.send(result)
        except StopIteration as e:
            return e.value
        result = getattr(io, op[0])(*op[1:])


class Host:
    def __init__(self, io):
        self.io = io

    def load(self, image):
        run(load(image), self.io)

    def load_hex(self, path):
        with open(path) as f:
            self.load(parse_hex(f.read()))

    def send(self, byte):
        run(send(byte), self.io)

    def recv(self):
        return run(recv(), self.io)

    def error(self):
        return self.io.get_uio(HOST_ERROR)


# ---- Tiny Tapeout demo board ------------------------------------------------

class TTDemoBoardIO:
    """IO on the Tiny Tapeout demo board through the TT MicroPython SDK.

    Not yet run on hardware (no chip exists before the shuttle); written
    against the SDK's documented API: tt.shuttle.<project>.enable(),
    tt.clock_project_PWM, tt.reset_project, tt.ui_in / tt.uo_out,
    tt.uio_in / tt.uio_out and tt.uio_oe_pico for direction.
    """

    PROJECT = "tt_um_pankajnair_protocol_emulator"

    def __init__(self, clock_hz=50_000_000):
        import time
        from ttboard.demoboard import DemoBoard
        from ttboard.mode import RPMode
        self._time = time
        tt = DemoBoard.get()
        tt.mode = RPMode.ASIC_RP_CONTROL
        getattr(tt.shuttle, self.PROJECT).enable()
        tt.clock_project_PWM(clock_hz)   # firmware timing assumes 50 MHz
        self.tt = tt
        self._oe = 0
        self._apply_oe()

    def _apply_oe(self):
        self.tt.uio_oe_pico.value = self._oe

    def reset(self):
        self.tt.reset_project(True)
        self._time.sleep_ms(1)
        self.tt.reset_project(False)
        self._time.sleep_ms(1)

    def set_ui(self, byte):
        self.tt.ui_in.value = byte

    def get_uo(self):
        return int(self.tt.uo_out.value)

    def set_uio(self, bit, level):
        if not (self._oe >> bit) & 1:
            self._oe |= 1 << bit
            self._apply_oe()
        self.tt.uio_in[bit] = level

    def release_uio(self, bit):
        self._oe &= ~(1 << bit)
        self._apply_oe()

    def get_uio(self, bit):
        return (int(self.tt.uio_out.value) >> bit) & 1

    def hold(self):
        self._time.sleep_us(10)          # 500 cycles at 50 MHz; >= 4 needed

    def wait_uio(self, bit, level, timeout_ms):
        t0 = self._time.ticks_ms()
        while self.get_uio(bit) != level:
            if self._time.ticks_diff(self._time.ticks_ms(), t0) > timeout_ms:
                return False
        return True
