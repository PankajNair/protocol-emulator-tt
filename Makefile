# Top-level Makefile: the verification gates (docs/VAL.md). Simulation
# itself lives in test/Makefile (cocotb); these targets wrap it with
# coverage (coverage, coverage-gate), mutation testing (mutate), and the
# formal flow (formal, vacuity), mirroring the sibling
# 5-Stage-Pipelined-RISC-V-Processor project's conventions.

.PHONY: formal vacuity coverage coverage-gate mutate signoff ledger orchestrate

MUTATE_SEEDS ?= 50

# ---------------------------------------------------------------------------
# signoff: every gate (directed, pytest, board, coverage gates incl.
#          profiles, determinism, formal, vacuity, mutation) -> one verdict
#          and signoff_report.json. The definition of "stable": docs/VAL.md.
# ---------------------------------------------------------------------------
signoff:
	python3 scripts/signoff.py --mutate-seeds $(MUTATE_SEEDS)

# ledger: per-agent run counts, acceptance, scope violations, bugs found
#         (ledger/runs.jsonl; workflow in docs/VAL.md section 15)
ledger:
	python3 scripts/ledger.py stats
	python3 scripts/ledger.py show -n 10

# orchestrate: start one agentic DV cycle (signoff -> triage -> red-team ->
#              close); docs/VAL.md section 18 for the ticket workflow
orchestrate:
	python3 scripts/orchestrate.py start

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
# coverage-gate: same run, but fails on a coverage regression (default
#                generator: any non-EXPECTED_OPEN bin open; profile: any of
#                its PROFILE_REQUIRED bins open). Used by CI.
# ---------------------------------------------------------------------------
coverage-gate:
	rm -rf $(COV_DIR)
	STIM_PROFILE=$(STIM_PROFILE) COV_DIR=$(COV_DIR) SEEDS=$(COV_SEEDS) $(MAKE) -C test random
	python3 scripts/merge_coverage.py $(COV_DIR) --gate --profile "$(STIM_PROFILE)"

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
