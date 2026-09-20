# Formal verification

Competition judging criteria explicitly call out verification methodology
(formal methods, AI-assisted techniques), so this gets its own directory
rather than being buried in `test/`.

Plan: SymbiYosys (`sby`) properties written in SVA against `src/cpu/core.v`
once it exists. Candidate properties once the ISA is settled:

- fetch/decode never produces an undefined opcode from valid prog_rom content
- pin direction (`uio_oe`) never contended -- no cycle where firmware drives
  a pin configured as input. This was genuinely undefined until an
  adversarial review caught it (no per-pin mode existed at all, so the
  property had nothing to check against) -- now that `SET`'s sticky
  drive-mode field is locked (`docs/isa.md` `SET` row), this is a real,
  checkable invariant: `uio_oe[n]` only ever asserts for a `pin_index`
  whose mode is push-pull or open-drain-driving-low, never input
- WAIT/cycle-counter never undercounts (protocol timing correctness)
- reset brings core back to a known fetch state within N cycles

Nothing here yet -- blocked on `src/cpu/core.v` existing.
