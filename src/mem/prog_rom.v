/*
 * Copyright (c) 2026 Pankaj Nair
 * SPDX-License-Identifier: Apache-2.0
 *
 * Firmware store for protocol programs. TODO: decide ROM (synthesized
 * from a $readmemh'd firmware/*.hex, cheapest in tile budget) vs small
 * SRAM (reloadable at runtime, costs more area -- needed if protocol
 * switching without re-tapeout/re-flash is a goal).
 * Not included in source_files yet -- not wired into top.v.
 */

`default_nettype none
