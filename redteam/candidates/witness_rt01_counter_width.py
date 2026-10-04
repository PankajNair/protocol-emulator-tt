# Red-team witness rt01: DELAY at exponent 3 with a mantissa >= 32.
#
# docs/isa.md DELAY row: cycles = mantissa << (exponent*5); exponent=3
# reaches "up to ~16.7M (~335ms @ 50MHz)". DELAY 33,3 must therefore hold
# the sequencer for 33 << 15 = 1,081,344 cycles. A counter sized for
# WAIT's max reach (~1,015,808 cycles, fits in 20 bits) instead of
# DELAY's truncates that to ~32,768 cycles.
#
# Checks only "not early": 100,000 cycles after the marker before the
# DELAY, the marker written after the DELAY must not have appeared yet.

import cocotb
from cocotb.triggers import ClockCycles

import isa_asm as asm
import test as T


@cocotb.test()
async def test_rt01_delay_exp3_large_mantissa_not_early(dut):
    prog = [
        asm.ldi(0, 0x11),
        asm.out(0),
        asm.delay(33, 3),   # 33 << 15 = 1,081,344 cycles (isa.md DELAY row)
        asm.ldi(0, 0x22),
        asm.out(0),
        asm.halt(),
    ]
    await T.reset_and_boot(dut, prog)
    await T.run_to_value(dut, 0x11, max_cycles=60)
    for _ in range(10):
        await ClockCycles(dut.clk, 10_000)
        assert int(dut.uo_out.value) == 0x11, (
            "DELAY 33<<15 (1,081,344 cycles) ended within 100,000 cycles")
    dut._log.info("DELAY 33,3 still holding after 100k cycles, as the spec requires")
