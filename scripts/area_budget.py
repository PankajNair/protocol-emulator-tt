#!/usr/bin/env python3
"""
Check synthesized cell count against the competition's area budget
(~1000 logic cells/tile; info.yaml `tiles:` sets the tile count).

TODO: wire this up to LibreLane's post-synth utilization report once
a GDS run has actually happened (runs/**/reports/*util*.rpt or similar --
exact path depends on the LibreLane flow version this template pins).
Not runnable yet -- placeholder.
"""

import sys
import yaml

CELLS_PER_TILE_BUDGET = 1000


def tile_count_from_spec(spec: str) -> int:
    w, h = spec.lower().split("x")
    return int(w) * int(h)


def main():
    with open("info.yaml") as f:
        info = yaml.safe_load(f)
    tiles = info["project"]["tiles"]
    budget = tile_count_from_spec(tiles) * CELLS_PER_TILE_BUDGET
    print(f"tiles={tiles} -> cell budget={budget}")
    print("TODO: parse actual synthesized cell count from LibreLane report "
          "and compare against budget above.")
    sys.exit(0)


if __name__ == "__main__":
    main()
