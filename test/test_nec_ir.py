# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""End-to-end protocol test: the NEC IR transmitter firmware
(firmware/protocols/nec_ir.asm) running on the RTL, decoded by an
independent IR receiver model.

The model works like an IR receiver module plus a decoder: it measures
the carrier (frequency and duty), demodulates it into marks (carrier on)
and spaces, and decodes NEC from the mark/space lengths with typical
decoder tolerances. It sees only the driven pin.

NEC: 9 ms leader mark, 4.5 ms space; 32 bits LSB first (address,
~address, command, ~command), each a 562.5 us mark and a 562.5 us (0) or
1687.5 us (1) space; a final mark.
"""

import os
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles
from cocotb.utils import get_sim_time

import test as T

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "isa"))
import asm as assembler  # noqa: E402

# NEC_IR_ASM overrides the firmware, for checking that broken variants fail
IR_ASM = Path(os.environ.get("NEC_IR_ASM") or
              Path(__file__).resolve().parents[1] / "firmware" / "protocols" / "nec_ir.asm")
IR = 0
CARRIER_HZ, CARRIER_TOL = 38_000, 0.02   # receiver modules: ~38 kHz band-pass
GAP_US = 100                              # no carrier edge for this long = space


class NecRx:
    def __init__(self, dut):
        self.dut = dut
        self.rises: list[float] = []   # rising-edge times, us
        self.falls: list[float] = []

    def level(self):
        if not (int(self.dut.uio_oe.value) >> IR) & 1:
            return 0
        return (int(self.dut.uio_out.value) >> IR) & 1

    async def run(self):
        prev = self.level()
        while True:
            await self.dut.uio_out.value_change
            v = self.level()
            if v != prev:
                (self.rises if v else self.falls).append(get_sim_time("ns") / 1000)
                prev = v

    def bursts(self):
        """Demodulate: group carrier pulses into marks. Returns
        [(mark_start, mark_end)] in us."""
        marks = []
        for r, f in zip(self.rises, self.falls):
            if marks and r - marks[-1][1] < GAP_US:
                marks[-1][1] = f
            else:
                marks.append([r, f])
        return marks

    def carrier(self):
        periods = [b - a for a, b in zip(self.rises, self.rises[1:]) if b - a < GAP_US]
        highs = [f - r for r, f in zip(self.rises, self.falls)]
        p = sum(periods) / len(periods)
        return 1e6 / p, sum(highs) / len(highs) / p, min(periods), max(periods)

    def decode(self):
        """Returns (address, command) or raises AssertionError with why."""
        m = self.bursts()
        assert len(m) == 34, f"{len(m)} marks, NEC frame has 34 (leader, 32 bits, stop)"
        lead_mark, lead_space = m[0][1] - m[0][0], m[1][0] - m[0][1]
        assert 8100 <= lead_mark <= 9900, f"leader mark {lead_mark:.0f} us, want 9000 +-10%"
        assert 4050 <= lead_space <= 4950, f"leader space {lead_space:.0f} us, want 4500 +-10%"
        bits = []
        for i in range(1, 33):
            mark, space = m[i][1] - m[i][0], m[i + 1][0] - m[i][1]
            assert 450 <= mark <= 675, f"bit {i - 1} mark {mark:.0f} us, want 562.5 +-20%"
            if 450 <= space <= 675:
                bits.append(0)
            elif 1350 <= space <= 2025:
                bits.append(1)
            else:
                raise AssertionError(f"bit {i - 1} space {space:.0f} us is neither 562.5 nor 1687.5 (+-20%)")
        stop = m[33][1] - m[33][0]
        assert 450 <= stop <= 675, f"stop mark {stop:.0f} us"
        b = [sum(bits[8 * k + j] << j for j in range(8)) for k in range(4)]
        assert b[1] == b[0] ^ 0xFF, f"address {b[0]:#04x} but ~address {b[1]:#04x}"
        assert b[3] == b[2] ^ 0xFF, f"command {b[2]:#04x} but ~command {b[3]:#04x}"
        return b[0], b[2], (lead_mark, lead_space, m)


async def send(dut, uio, b):
    dut.ui_in.value = b
    uio |= 1 << T.HOST_GO_BIT
    dut.uio_in.value = uio
    await T.wait_host_status(dut, 1, max_cycles=2000, what="(byte accepted)")
    uio &= ~(1 << T.HOST_GO_BIT)
    dut.uio_in.value = uio
    await T.wait_host_status(dut, 0, max_cycles=2000)
    return uio


@cocotb.test()
async def test_nec_ir_frame(dut):
    """One NEC frame (address 0xA5, command 0x3C: both bit values in both
    the true and the complemented bytes) decodes, with every carrier
    period at 38 kHz +-2% and a receiver-friendly duty; the host gets its
    ack."""
    prog = assembler.assemble_file(IR_ASM)
    assert not prog.warnings, prog.warnings
    uio = await T.reset_and_boot(dut, prog.words)
    rx = NecRx(dut)
    cocotb.start_soon(rx.run())
    await ClockCycles(dut.clk, 20)
    assert (int(dut.uio_oe.value) >> IR) & 1 and rx.level() == 0, "IR LED should be driven off at idle"

    uio = await send(dut, uio, 0xA5)
    uio = await send(dut, uio, 0x3C)
    # one frame is ~67.5 ms; the ack follows the final mark
    uio |= 1 << T.HOST_GO_BIT
    dut.uio_in.value = uio
    await T.wait_host_status(dut, 1, max_cycles=4_500_000, what="(frame sent)")  # 90 ms
    assert int(dut.uo_out.value) == 0, "ack byte should be 0"

    addr, cmd, (lm, ls, _) = rx.decode()
    freq, duty, pmin, pmax = rx.carrier()
    assert (addr, cmd) == (0xA5, 0x3C), (hex(addr), hex(cmd))
    # every period in band, not just the average: a receiver needs each burst
    lo, hi = 1e6 / (CARRIER_HZ * (1 + CARRIER_TOL)), 1e6 / (CARRIER_HZ * (1 - CARRIER_TOL))
    assert lo <= pmin and pmax <= hi, f"carrier periods {pmin:.2f}-{pmax:.2f} us, want {lo:.2f}-{hi:.2f} (38 kHz +-2%)"
    assert abs(freq - CARRIER_HZ) <= CARRIER_HZ * CARRIER_TOL, f"carrier {freq:.0f} Hz"
    assert 0.25 <= duty <= 0.5, f"carrier duty {duty:.2f}"
    dut._log.info(f"NEC: address {addr:#04x} command {cmd:#04x}; leader {lm:.0f}/{ls:.0f} us; "
                  f"carrier {freq:.0f} Hz (periods {pmin:.2f}-{pmax:.2f} us), duty {duty:.2f}")
