#!/usr/bin/env python3
"""
check_tieoffs.py -- structural check of the SRAM macro's tie-offs
(docs/VAL.md section 20).

The blind mutation campaign found that no simulation or formal gate can
see a change to the macro's static configuration pins: IHP's behavioral
model declares A_DLY but never reads it, and reads the A_BIST_* inputs
only when A_BIST_EN is 1. On silicon, though, A_DLY is the macro's
delay-selection input (the model's own comment: "Delay selection
signals") and the BIST pins are real inputs. The only defence is to
pin the connections themselves to the recipe validated in the macro
flow-validation run
(src/mem/mem.v header: commit 2e113b4, from urish/ttihp-sram-test).

Exit 0 iff every macro port in src/mem/mem.v is connected exactly as
EXPECTED (whitespace-insensitive) and no port is missing or extra.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEM = ROOT / "src" / "mem" / "mem.v"
MACRO = "RM_IHPSG13_1P_1024x8_c2_bm_bist"

EXPECTED = {
    # functional ports, driven by core.v through mem.v's own ports
    "A_CLK": "clk", "A_ADDR": "addr", "A_WEN": "we", "A_DIN": "wdata", "A_DOUT": "rdata",
    # validated tie-offs
    "A_MEN": "rst_n",
    "A_REN": "1'b1",
    "A_BM": "8'hFF",
    "A_DLY": "1'b1",
    "A_BIST_CLK": "1'b0",
    "A_BIST_EN": "1'b0",
    "A_BIST_MEN": "1'b0",
    "A_BIST_WEN": "1'b0",
    "A_BIST_REN": "1'b0",
    "A_BIST_ADDR": "10'd0",
    "A_BIST_DIN": "8'd0",
    "A_BIST_BM": "8'd0",
}


def main() -> int:
    text = re.sub(r"//[^\n]*|/\*.*?\*/", "", MEM.read_text(), flags=re.S)
    m = re.search(rf"\b{MACRO}\s+\w+\s*\((.*?)\)\s*;", text, re.S)
    if not m:
        print(f"[check_tieoffs] FAIL: no {MACRO} instance in {MEM.relative_to(ROOT)}")
        return 1
    got = {p: "".join(v.split()) for p, v in re.findall(r"\.(\w+)\s*\(([^()]*)\)", m.group(1))}
    bad = []
    for port, want in EXPECTED.items():
        if port not in got:
            bad.append(f"{port}: unconnected (want {want})")
        elif got[port] != want:
            bad.append(f"{port}: {got[port]} (want {want})")
    bad += [f"{p}: unexpected port" for p in got if p not in EXPECTED]
    for b in bad:
        print(f"    {b}")
    print(f"[check_tieoffs] {'PASS' if not bad else 'FAIL'}: {len(EXPECTED) - len(bad)}/{len(EXPECTED)} "
          f"{MACRO} ports as expected")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
