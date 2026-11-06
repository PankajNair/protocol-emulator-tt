You are the spec-conformance agent. Your working tree is /private/tmp/claude-501/-Users-pankajnair-Desktop-Projects-protocol-emulator-tt/94aa3d8b-68df-46c3-bfde-829d41b278cb/scratchpad/spec_tree4 and you may
read and write ONLY inside it. Read /private/tmp/claude-501/-Users-pankajnair-Desktop-Projects-protocol-emulator-tt/94aa3d8b-68df-46c3-bfde-829d41b278cb/scratchpad/spec_tree4/spec_tests/AGENT_CONTRACT.md first:
it is your contract (write scope: spec_tests/; spec: the five files in
docs/; harness: test/tb.v).

Pass 4. The spec changed: a new document, docs/serial_engine.md, defines a
serial engine on protocol pins 0/1, driven by four new opcodes (docs/isa.md
opcode table rows 21-24: SCFG, STX, SRX, SRD); the reserved range is now
25-31. docs/isa.md also now says code may run in the data region (words
256-511). The suite in spec_tests/ is yours from earlier passes; two lines
were already updated by the parent to follow the doc (RESERVED_OPS /
ENGINE_OPS in spec_helpers.py and the "reserved 25" timing row in
test_spec_conformance.py), marked as parent edits.

Task:
1. Derive from the text alone what the engine and the four opcodes must
   do: configuration registers and commands, the line-level stuffing rule,
   NRZI, transmit (byte queueing, STX blocking, the end-of-data sequence:
   stuff bit, CRC, SE0 EOP, release), receive (start of packet, sampling
   S clocks after an edge and every P after, data/stuff/SE0 samples,
   end signalling only once the line is idle), SRX's three outcomes and
   flag, the timeout encoding, SRD's status bits and CRC bytes, pin
   ownership, the USB settings table.
2. Add black-box tests for them to spec_tests/, driving and observing only
   tb's pins. Include at least: exact transmit waveforms (bit times of P
   clocks, NRZI, stuffing at K for USB-like and CAN-like settings, EOP),
   differential drive, pins released after a packet, receive of packets
   you encode yourself (including one at a slightly different bit rate),
   a stuff error, SRX timeout vs end vs byte (flag and Rd), overrun,
   the CRC residual the doc gives for USB CRC16, STX blocking, and code
   executing from the data region. Cite the doc text each test relies on.
   Write your own reference encoder/decoder from the doc; do not copy any
   other source.
3. Check the rest of the suite against the updated docs and fix anything
   the change made wrong.
4. You cannot run the design (there is none here). py_compile your files
   and check your own encoders in pure Python. Keep each test under ~200k
   clock cycles.
5. If the spec is ambiguous or contradicts itself anywhere, do not guess
   silently: write the test for your best reading and list the ambiguity
   in your final message.

Finish with: the tests you added or changed (names, one line each), and
any spec ambiguities found.
