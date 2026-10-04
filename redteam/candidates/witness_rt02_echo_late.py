# Red-team witness rt02: the LOAD-mode address echo must already be valid
# when HOST_STATUS acks the write.
#
# docs/architecture.md Pipeline, LOAD section:
#   host: drive ui_in, assert HOST_GO
#   chip: (internally) sync HOST_GO, write byte, increment counter,
#         update the uo_out echo, THEN assert HOST_STATUS
#   host: wait for HOST_STATUS=1 ... -> read uo_out, now guaranteed valid
#         -> lower HOST_GO
# and "uo_out[7:0] echoes the byte-address counter's low 8 bits
# (post-increment), every HOST_GO pulse".
#
# The host here reads uo_out at the moment the spec says it is valid
# (HOST_STATUS just went 1, HOST_GO still high), expecting k mod 256 after
# the k-th pulse. test_boot_echo_and_saturation reads the echo only after
# the whole handshake (ack already cleared), so an echo that lags the ack
# passes there.

import cocotb
from cocotb.triggers import ClockCycles

import isa_asm as asm
import test as T


@cocotb.test()
async def test_rt02_echo_valid_at_ack(dut):
    T._ensure_clock(dut)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    image = asm.to_bytes([asm.ldi(0, 0x3C), asm.out(0), asm.halt()])
    uio = 0
    for k, b in enumerate(image, start=1):
        dut.ui_in.value = b
        uio |= 1 << T.HOST_GO_BIT
        dut.uio_in.value = uio
        await T.wait_host_status(dut, 1, what="(boot write ack)")
        echo = int(dut.uo_out.value)
        assert echo == (k & 0xFF), (
            f"pulse {k}: uo_out={echo:#x} when HOST_STATUS acked, spec says the echo "
            f"({k & 0xFF:#x}) is already valid then")
        uio &= ~(1 << T.HOST_GO_BIT)
        dut.uio_in.value = uio
        await T.wait_host_status(dut, 0, what="(boot ack clear)")

    uio |= 1 << T.START_BIT
    dut.uio_in.value = uio
    await ClockCycles(dut.clk, 4)
    dut.uio_in.value = uio & ~(1 << T.START_BIT)
    await T.run_to_value(dut, 0x3C)
