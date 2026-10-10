# Red-team witness rt04: DELAY 33,3 must not end early.
#
# docs/isa.md DELAY row: cycles = mantissa << (exponent*5), so DELAY 33,3
# holds the sequencer for 33 << 15 = 1,081,344 cycles. Its count
# (0x107FFF after the load) has low 20 bits 0x07FFF, so an expiry check on
# the low 20 bits only fires after ~32,768 cycles.
#
# Checks only "not early": 100,000 cycles after the marker written before
# the DELAY, the marker written after it must not have appeared yet.

import cocotb
from cocotb.triggers import ClockCycles

import isa_asm as asm
import test as T


@cocotb.test()
async def test_rt04_delay_33_exp3_not_early(dut):
    prog = [
        asm.ldi(0, 0x11),
        asm.out(0),
        asm.delay(33, 3),   # 1,081,344 cycles
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
    dut._log.info("DELAY 33,3 still holding after 100k cycles")
