# Top-level Makefile. test/Makefile (cocotb sim) stays separate --
# this is just the formal-verification gate, mirroring the sibling
# 5-Stage-Pipelined-RISC-V-Processor project's `make formal`/
# `make vacuity` convention (see formal/AGENT_CONTRACT.md).

.PHONY: formal vacuity

# ---------------------------------------------------------------------------
# formal: BMC assert pass for formal/agent_*_props.v (yosys + z3, no
#         SymbiYosys -- see formal/scripts/formal_common.py's own
#         docstring for the full toolchain recipe and every gotcha
#         behind it)
#         TARGET_PROPS=<path> runs one file; unset runs all agent_*_props.v
# ---------------------------------------------------------------------------
formal:
ifdef TARGET_PROPS
	python3 formal/scripts/run_formal.py $(TARGET_PROPS)
else
	python3 formal/scripts/run_formal.py --all
endif

# ---------------------------------------------------------------------------
# vacuity: Vacuity-check gate -- run in the parent orchestrating
#          session, never delegated to a subagent (formal/AGENT_CONTRACT.md).
#          100% of covers (one per assert's antecedent) must be reached
#          before that property is trusted as a gate.
#          TARGET_PROPS=<path> runs one file; unset runs all agent_*_props.v
# ---------------------------------------------------------------------------
vacuity:
ifdef TARGET_PROPS
	python3 formal/scripts/vacuity_check.py $(TARGET_PROPS)
else
	python3 formal/scripts/vacuity_check.py --all
endif
