# Red-team witness rt05: a full 1024-byte boot image reaches the last data byte.
#
# docs/architecture.md LOAD: each HOST_GO pulse writes ui_in at the byte
# counter (0-1023) and "A HOST_GO pulse at address 1023 writes/echoes that
# byte". docs/isa.md Data memory: the boot stream fills the data region
# too, so firmware can LOAD preloaded constants. Data offset 511 is byte
# address 1023, the 1024th boot byte (no over-run pulses here).

import cocotb
from cocotb.triggers import ClockCycles

import isa_asm as asm
import test as T


@cocotb.test()
async def test_rt05_boot_writes_byte_1023(dut):
    T._ensure_clock(dut)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    image = asm.to_bytes([asm.load(0, 511), asm.out(0), asm.halt()])
    image += [0] * (1024 - len(image))
    image[1023] = 0x5A   # data region offset 511

    uio = 0
    for k, b in enumerate(image, start=1):
        uio = await T.load_byte(dut, b, uio, expect_echo=min(k, 1023) & 0xFF)

    uio |= 1 << T.START_BIT
    dut.uio_in.value = uio
    await ClockCycles(dut.clk, 4)
    dut.uio_in.value = uio & ~(1 << T.START_BIT)
    await T.run_to_value(dut, 0x5A, max_cycles=100)
    dut._log.info("byte 1023 written by the 1024th boot pulse and LOADed back")
