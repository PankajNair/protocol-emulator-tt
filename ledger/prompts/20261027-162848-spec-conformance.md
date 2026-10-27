You are the spec-conformance agent. Your working tree is /private/tmp/claude-501/-Users-pankajnair-Desktop-Projects-protocol-emulator-tt/94aa3d8b-68df-46c3-bfde-829d41b278cb/scratchpad/spec_tree3 and you may
read and write ONLY inside it. Read /private/tmp/claude-501/-Users-pankajnair-Desktop-Projects-protocol-emulator-tt/94aa3d8b-68df-46c3-bfde-829d41b278cb/scratchpad/spec_tree3/spec_tests/AGENT_CONTRACT.md first:
it is your contract (write scope: spec_tests/; spec: the four files in
docs/; harness: test/tb.v).

Pass 3. The spec changed: docs/isa.md now defines two new opcodes, XOR
(19) and ADD (20), in the reg-reg format, and the reserved range is now
21-31. The suite in spec_tests/ is yours from earlier passes; one line in
spec_helpers.py (RESERVED_OPS) was already updated by the parent to follow
the doc, and is marked as such.

Task:
1. Read the updated docs/isa.md (opcode table rows 19 and 20, the
   Register file section's "Resolved" note, the flag-writer and CALL
   sections) and derive from the text alone what XOR and ADD must do:
   result, which register is written, flag behaviour, encoding.
2. Add black-box tests for XOR and ADD to spec_tests/: results across
   edge values (wrap, carry in both directions, Rd == Rs, zero), the flag
   behaviour each row states (and its interaction with BEQ/BNE and with
   the other flag writers), encoding per the reg-reg format, and timing
   if the spec states it. Cite the doc text each test relies on, as the
   existing tests do.
3. Check the rest of the suite against the updated docs and fix anything
   the change made wrong (e.g. anything still assuming 19-31 reserved or a
   flag-writer set without ADD).
4. You cannot run the design (there is none here). py_compile your files
   and check your own encoder in pure Python.
5. If the spec is ambiguous or contradicts itself anywhere about XOR/ADD,
   do not guess silently: write the test for your best reading and list the
   ambiguity in your final message.

Finish with: the tests you added or changed (names, one line each), and
any spec ambiguities found.
