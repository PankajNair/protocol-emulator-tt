# Red-team witness rt03: a timed WAIT at exponent 3 with an even mantissa.
#
# docs/isa.md WAIT row: timeout cycles = mantissa << (exponent*5); only a
# computed value of 0 means unbounded. WAIT pin0==1 with mantissa 2,
# exponent 3 therefore times out after 2 << 15 = 65,536 cycles and sets
# the flag (1 = timed out), which a following BEQ consumes. pin 0 is never
# driven high, so only the timeout can end the WAIT.

import cocotb

import isa_asm as asm
import test as T


@cocotb.test()
async def test_rt03_wait_exp3_even_mantissa_times_out(dut):
    prog = asm.assemble([
        (None, lambda L: asm.wait_(0, 1, mantissa=2, exponent=3)),  # 65,536-cycle timeout
        (None, lambda L: asm.beq(L["timed_out"])),   # flag=1 (timeout) -> taken
        (None, lambda L: asm.ldi(0, 0xAA)),          # poison: condition wrongly seen as met
        (None, lambda L: asm.out(0)),
        (None, lambda L: asm.halt()),
        ("timed_out", lambda L: asm.ldi(0, 0xCC)),
        (None, lambda L: asm.out(0)),
        (None, lambda L: asm.halt()),
    ])
    await T.reset_and_boot(dut, prog)
    # 65,536-cycle timeout plus a few instructions; generous margin.
    await T.run_to_value(dut, 0xCC, max_cycles=70_000)
    dut._log.info("WAIT 2<<15 timed out and set the flag, as the spec requires")
