# Top-level Makefile. test/Makefile (cocotb sim) stays separate --
# this is just the formal-verification gate, mirroring the sibling
# 5-Stage-Pipelined-RISC-V-Processor project's `make formal`/
# `make vacuity` convention (see formal/AGENT_CONTRACT.md).

.PHONY: formal vacuity coverage mutate

MUTATE_SEEDS ?= 50

# ---------------------------------------------------------------------------
# mutate: mutation-testing gate (scripts/mutate.py) -- parent session only,
#         never delegated to a subagent. Restores src/ unconditionally.
# ---------------------------------------------------------------------------
mutate:
	python3 scripts/mutate.py --seeds $(MUTATE_SEEDS)

COV_SEEDS    ?= 50
STIM_PROFILE ?=
COV_DIR      ?= /tmp/seq_coverage$(if $(STIM_PROFILE),_$(STIM_PROFILE))

# ---------------------------------------------------------------------------
# coverage: random differential regression with functional-coverage
#           sampling (test/seq_coverage.py), then merged closure report
#           (scripts/merge_coverage.py). Informational, always exits 0 on
#           a passing regression. Needs cocotb on PATH (e.g. the .venv).
# ---------------------------------------------------------------------------
coverage:
	rm -rf $(COV_DIR)
	STIM_PROFILE=$(STIM_PROFILE) COV_DIR=$(COV_DIR) SEEDS=$(COV_SEEDS) $(MAKE) -C test random
	python3 scripts/merge_coverage.py $(COV_DIR)

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
