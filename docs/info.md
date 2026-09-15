<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

Flow-validation placeholder: wires up the IHP 1024x8 SRAM hard macro
(`RM_IHPSG13_1P_1024x8_c2_bm_bist`) directly to the pins, wiring borrowed
from github.com/urish/ttihp-sram-test. Used to measure the macro's real
placement/routing/area cost within our 6x4 floorplan before locking the
protocol emulator's actual instruction-memory design (see
`docs/architecture.md`). Not the final design -- expect this file to be
rewritten once that's built.

## How to test

Set `bank_sel` (`ui_in[6]`) high with the target address on `addr[9:6]`
(`uio_in[3:0]`) and `addr[5:0]` (`ui_in[5:0]`) to latch an address, then
drop `bank_sel` and pulse `wen` (`ui_in[7]`) with data on `din`
(`uio_in`) to write, or read `dout` (`uo_out`) with `wen` low.

## External hardware

None.
