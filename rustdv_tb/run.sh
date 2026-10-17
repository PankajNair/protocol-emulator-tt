#!/usr/bin/env bash
# Build the rustdv testbench and run it on Icarus against test/tb.v.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$PWD/target}"
BUILD="${SIM_BUILD_DIR:-$PWD/sim_build}"
mkdir -p "$BUILD"
cargo build --release -q
LIB="$CARGO_TARGET_DIR/release/librustdv_tb.so"
[ -f "$LIB" ] || LIB="$CARGO_TARGET_DIR/release/librustdv_tb.dylib"
cp "$LIB" "$BUILD/rustdv_tb.vpi"
SRC="${RTL_SRC:-$ROOT/src}"   # RTL_SRC: run against a scratch copy (mutant checks)
iverilog -g2012 -DFUNCTIONAL -I"$SRC" -s tb -o "$BUILD/tb.vvp" \
  "$SRC/cpu/isa_defs.v" "$SRC/cpu/core.v" "$SRC/cpu/regfile.v" "$SRC/io/cycle_counter.v" \
  "$SRC/io/pin_ctrl.v" "$SRC/mem/mem.v" "$SRC/top.v" \
  "$ROOT/test/models/RM_IHPSG13_1P_core_behavioral_bm_bist.v" \
  "$ROOT/test/models/RM_IHPSG13_1P_1024x8_c2_bm_bist.v" "$ROOT/test/tb.v"
export RUSTDV_TOP=tb RUSTDV_RANDOM_SEED="${RUSTDV_RANDOM_SEED:-1}" RUSTDV_RESULTS_XML="$BUILD/results.xml"
cd "$BUILD" && vvp -M "$BUILD" -m rustdv_tb "$BUILD/tb.vvp"
