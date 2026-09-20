<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

Flow-validation placeholder: an 8-bit adder (`uo_out = ui_in + uio_in`),
the template's original example logic. Used to get real synth/P&R/GDS
numbers for the IHP CMOS5L process before locking the actual protocol
emulator's sequencer ISA (see `docs/architecture.md`). Not the final
design -- expect this file to be rewritten once that's built.

## How to test

Drive `ui_in` and `uio_in` with two 8-bit values, check `uo_out` equals
their sum on the next clock edge after reset.

## External hardware

None for this flow-validation placeholder. TODO once the real design
lands: any protocol pin firmware configures open-drain (I2C SDA/SCL,
`SET`'s drive-mode field -- see `docs/isa.md`'s `SET` row) needs an
external pull-up. Don't lose this when this file gets rewritten.
