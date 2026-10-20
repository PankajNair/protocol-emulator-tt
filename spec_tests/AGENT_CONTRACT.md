---
role: spec-conformance
write_scope: spec_tests
protected_paths: [docs, test]
spec: [docs/isa.md, docs/architecture.md, docs/protocol_timing.md, docs/info.md]
harness: [test/tb.v]
interface: "tb's regs/wires only: clk, rst_n, ena, ui_in[7:0], uio_in[7:0] (drive); uo_out[7:0], uio_out[7:0], uio_oe[7:0] (observe). Nothing below user_project."
output_naming: "spec_tests/test_spec_conformance.py (one cocotb module; helpers may go in spec_tests/spec_helpers.py)"
framework: "cocotb 2.0.1 on Icarus, Python 3.13. TOPLEVEL is tb (test/tb.v). Use cocotb 2.0 APIs: from cocotb.clock import Clock; cocotb.start_soon(Clock(dut.clk, 20, unit='ns').start()); from cocotb.triggers import ClockCycles, RisingEdge, FallingEdge, Timer; drive with dut.ui_in.value = x; read with int(dut.uo_out.value). Every @cocotb.test must start its own clock and reset. Keep each test under ~200k clock cycles."
gate_command: "parent copies spec_tests/*.py into test/ and runs make -C test COCOTB_TEST_MODULES=test_spec_conformance against the real RTL"
---

# protocol-emulator-tt: spec-conformance contract

The design is a TinyTapeout chip: a small sequencer CPU that runs
firmware out of an on-chip SRAM to bit-bang protocols on the `uio`
pins. The four documents under `spec` are everything you get. They
define the instruction set, the encoding (exact bit positions), the
pin map, the LOAD-mode boot handshake by which a host writes the
firmware into SRAM, the START handover to execution, and the timing
of each pipeline state.

Practicalities:
- The firmware must reach the chip through the boot handshake the spec
  describes; there is no backdoor. Write that driver yourself.
- Inputs are synchronized inside the chip; the spec states how many
  cycles that takes. Use it, and cite it.
- `ena` is always 1.
- You cannot run anything against the design here (there is none in
  this tree). `python3 -m py_compile` and pure-Python checks of your own
  encoder are fine.
- Aim for breadth over depth: every opcode, every documented edge case
  (wrap, saturation, ties, reset mid-operation, illegal/reserved
  encodings, the boot handshake's own rules), exact cycle counts where
  the spec states them. Roughly 30-60 tests.
