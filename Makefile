# Top-level Makefile: the verification gates (docs/VAL.md). Simulation
# itself lives in test/Makefile (cocotb); these targets wrap it with
# coverage (coverage, coverage-gate), mutation testing (mutate), and the
# formal flow (formal, vacuity), mirroring the sibling
# 5-Stage-Pipelined-RISC-V-Processor project's conventions.

.PHONY: formal vacuity coverage coverage-gate mutate signoff ledger orchestrate metrics prove tieoffs blind-mutate prove-equiv rtl-coverage

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

# metrics: agent and pipeline metrics derived from the ledger (nothing
#          stored); docs/VAL.md section 19
metrics:
	python3 scripts/metrics.py

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

# prove: BMC base case plus K-step induction (unbounded proof) per props
#        file; PROVEN only if both pass. K defaults to the BMC depth.
PROVE_K ?= 20
prove:
ifdef TARGET_PROPS
	python3 formal/scripts/run_formal.py --induct $(PROVE_K) $(TARGET_PROPS)
else
	python3 formal/scripts/run_formal.py --induct $(PROVE_K) --all
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

# ---------------------------------------------------------------------------
# tieoffs: structural check of the SRAM macro's static pins (A_DLY, BIST),
#          which no simulation or formal gate can observe (section 20)
# blind-mutate: operator-based mutation census of src/ against every gate
#          (several hours; resumable; parent session only)
# ---------------------------------------------------------------------------
tieoffs:
	python3 scripts/check_tieoffs.py

BLIND_JOBS ?= 6
blind-mutate:
	python3 scripts/blind_mutate.py run -j $(BLIND_JOBS)

# prove-equiv: prove the blind-mutation survivors in blind_mutate.EQUIVALENT
#          really are equivalent at the pins (sequential miter, BMC +
#          induction), then the negative control; then the same for the
#          formal-only kills in prove_equiv.FORMAL_ONLY (section 20)
prove-equiv:
	python3 scripts/prove_equiv.py
	python3 scripts/prove_equiv.py --controls
	python3 scripts/prove_equiv.py --formal-only
	python3 scripts/prove_equiv.py --formal-only --controls

# ---------------------------------------------------------------------------
# rtl-coverage: structural line/toggle coverage of src/ under Verilator,
#          merged over directed, spec and random suites; fails on any hole
#          not in scripts/rtl_coverage.py's WAIVED (section 22)
# ---------------------------------------------------------------------------
RTL_COV_SEEDS ?= 100
rtl-coverage:
	python3 scripts/rtl_coverage.py run --seeds $(RTL_COV_SEEDS)
	python3 scripts/rtl_coverage.py gate
