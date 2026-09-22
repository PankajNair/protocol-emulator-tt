# SPDX-FileCopyrightText: © 2026 Pankaj Nair
# SPDX-License-Identifier: Apache-2.0
"""Deterministic external pin/bus stimulus timeline for randomized
WAIT/IN/INB differential testing (test_random.py). Precomputed in full
BEFORE driving the RTL -- single source of truth both the RTL-driving
loop and golden_model.py's io_read callback read from. Never derive
this by reading pin_ctrl.v's own synchronizer registers: that's the
exact "ghost model fit to the DUT instead of the spec" mistake
formal/agent_cycle_counter_timing_props.v's header already documents
getting bitten by once in this project.

Timing convention -- confirmed by direct hierarchical probe against
real RTL (pin_ctrl.v's ui_in_ff1/ff2, uio_in_ff1/ff2), not assumed:
test_random.py's `cycle` counter increments immediately after each
`await RisingEdge(dut.clk)`; if the RTL-driving loop then sets
`dut.ui_in.value`/`dut.uio_in.value` to `raw_*(cycle)` at that same
point (before the next await), the value pin_ctrl.v's 2-flop
synchronizer exposes (`ui_in_sync`, and `pin_read`'s source
`uio_in_ff2`) during that SAME `cycle` phase is `raw_*(cycle - 3)` --
one cycle more than the textbook 2-flop count. Measured directly (not
derived): a throwaway probe driving a unique value per cycle and
reading `ui_in_ff1`/`ui_in_ff2` back the same way this module's own
callers do -- immediately after `await RisingEdge`, no `ReadOnly`
sync -- showed the same 3-cycle lag independently in both the probe
and the first live differential run that caught the original (wrong)
2-cycle assumption as a real RTL/golden mismatch. Most likely
explanation: cocotb's read/write region timing for a register driven
directly off a testbench-controlled input port (ui_in_ff1's D input)
differs from reading a register computed entirely from other
already-settled internal state (core.pc, core.state, etc., which this
project's other tests already read the same "immediately after
RisingEdge" way with no such offset) -- but the exact mechanism
doesn't matter here: what matters is this constant is measured against
the SAME read pattern golden_model.py's io_read is fed through, so it
stays correct regardless of which cocotb internals produce it. Both
ui_in and uio_in go through structurally identical 2-flop chains,
confirmed to share this exact relationship.

Only uio_in bits {0,1,2,3,7} (the 5 protocol pins, docs/architecture.md
Pin map) carry real per-cycle randomness. Bits 4/5/6 (HOST_GO/
HOST_STATUS/HOST_ERROR) are forced 0 throughout -- role-specific
self-loopback/hardwired-oe semantics belong to test.py's directed
tests, not this flat random generator (same scoping discipline
random_gen.py's own header already applies to nested control flow).
Note this test harness also can't model true self-loopback for those
pins even if it wanted to: uio_in here is testbench-supplied, wholly
separate from the DUT's own uio_out/uio_oe (no physical pad merge in
sim) -- another reason those 3 indices stay out of scope for this pass.

Cycles at or before `boot_exit_cycle` are NOT random: fast_boot()
(test_random.py) drives ui_in=0 throughout and holds uio_in at
1<<START_BIT for its entire 4-edge polling window before ever handing
control to this module's timeline -- those are the RTL's real driven
values for that window, not 0 and not this module's random bytes, and
`boot_exit_cycle` is always inside that unconditional 4-edge hold (the
loop always runs a full 4 iterations before checking anything). This
matters concretely: an instruction 0 that happens to be WAIT/IN/INB
looks back to exactly `boot_exit_cycle` at its very first (k=0) sample
(abs_cycle = boot_exit_cycle + 2, so abs_cycle + 0 - SYNC_DELAY ==
boot_exit_cycle) -- getting this one boundary cycle wrong would only
ever surface on instruction 0, making it exactly the kind of bug that
slips past most seeds. No later instruction's lookback ever reaches
this far back (each commit only pushes `last_commit_cycle`, and
therefore every later abs_cycle, strictly higher).
"""

from __future__ import annotations

import random

SYNC_DELAY = 3  # empirically measured, see module header -- NOT the textbook 2-flop count
START_BIT = 6  # uio[6], LOAD-mode role (docs/architecture.md Pin map) -- matches test_random.py's own constant
PROTOCOL_PIN_BITS = (0, 1, 2, 3, 7)  # the 5 protocol pins; 4/5/6 excluded, see module header


class IoStimulus:
    """seed: same per-seed determinism convention as random_gen.py
    (own random.Random instance, namespaced so it can never collide
    with random_gen's own seed->program mapping). boot_exit_cycle: from
    fast_boot()'s return value. max_cycle: size the timeline to the
    seed's MAX_CYCLES hard-failure budget (plus caller's own margin) --
    queries past it return 0, same as an unset dict key, never an
    error, since a WAIT/IN/INB lookahead landing past a program that's
    about to hard-fail on MAX_CYCLES anyway shouldn't itself crash the
    harness."""

    def __init__(self, seed: int, boot_exit_cycle: int, max_cycle: int):
        self.boot_exit_cycle = boot_exit_cycle
        self.max_cycle = max_cycle
        rng = random.Random(f"io_stim:{seed}")  # str seed, namespaced so it can never collide with random_gen.py's own int-seeded rng
        self._raw_ui: dict[int, int] = {}
        self._raw_uio: dict[int, int] = {}
        for t in range(boot_exit_cycle + 1, max_cycle + 1):
            self._raw_ui[t] = rng.randint(0, 255)
            uio_byte = 0
            for bit in PROTOCOL_PIN_BITS:
                if rng.random() < 0.5:
                    uio_byte |= 1 << bit
            self._raw_uio[t] = uio_byte

    def raw_ui_in(self, t: int) -> int:
        if t <= self.boot_exit_cycle:
            return 0  # fast_boot() never touches ui_in beyond its initial 0
        return self._raw_ui.get(t, 0)

    def raw_uio_in(self, t: int) -> int:
        if t <= self.boot_exit_cycle:
            return 1 << START_BIT if t >= 1 else 0  # fast_boot()'s START-held polling window
        return self._raw_uio.get(t, 0)

    def sync_ui_in(self, t: int) -> int:
        return self.raw_ui_in(t - SYNC_DELAY)

    def sync_uio_in(self, t: int) -> int:
        return self.raw_uio_in(t - SYNC_DELAY)

    def io_read(self, t: int) -> tuple[int, int]:
        """(ui_in_sync, uio_in_sync) as visible during cycle==t --
        golden_model.py's step() io_read callback shape."""
        return self.sync_ui_in(t), self.sync_uio_in(t)
