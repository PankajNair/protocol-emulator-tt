# Custom PDN configuration for the SRAM macro on IHP SG13CMOS5L.
#
# A CMOS5L tile has Metal1-Metal4 only (TopMetal belongs to the chip
# harness), so the grid is vertical Metal4 stripes over Metal1 rails, and
# the macro (RM_IHPSG13_1P_1024x8_c2_bm_bist) brings VDD!, VDDARRAY! and
# VSS! out as vertical Metal4 columns: the same layer, so the grid can
# only reach them by touching them. pdngen keeps the core grid off the
# (FIXED) macro; this file then bridges any gap.
#
# Column map (macro LEF, macro coordinates, unflipped):
#   VSS!       12 columns, full height
#   VDD!       4 full-height columns in the middle band, plus 8 short
#              ones (0 .. 38.825) under the left and right arrays
#   VDDARRAY!  8 columns (45.465 .. 336.46) above those short VDD! ones;
#              the 6.6 um between them holds macro-internal Metal4, so no
#              stripe may run straight through a split column
# config.json phases the grid so stripe pairs sit inside the left array's
# column pairs. After pdngen builds the grid, `pe_macro_power`:
#   1. fails if any Metal4 supply shape overlaps the macro anywhere other
#      than inside a same-net column (it would short or float),
#   2. for every column that reaches the macro's top or bottom edge, finds
#      the nearest same-net stripe beyond the halo whose width lies inside
#      the column and draws a Metal4 bridge between them,
#   3. fails unless every supply pin (VDD!, VDDARRAY!, VSS!) got a bridge.
# `pe_trim_power_pins` then keeps the tile's VPWR/VGND pins only on
# stripes that span the tile, as precheck requires.
# A misaligned grid therefore stops the run here instead of producing a
# chip with an unpowered SRAM array.
#
# Based on librelane's default pdn_cfg.tcl. Bridge idea after the CMOS5L
# SRAM walkthrough (kdp1965.github.io/ihp-um-janestreet-prism) and
# 2AMLogic/sg13cmos5l-protocol-emulator (Apache-2.0).

source $::env(SCRIPTS_DIR)/openroad/common/set_global_connections.tcl
set_global_connections

set secondary []
foreach vdd $::env(VDD_NETS) gnd $::env(GND_NETS) {
    if { $vdd != $::env(VDD_NET)} {
        lappend secondary $vdd

        set db_net [[ord::get_db_block] findNet $vdd]
        if {$db_net == "NULL"} {
            set net [odb::dbNet_create [ord::get_db_block] $vdd]
            $net setSpecial
            $net setSigType "POWER"
        }
    }

    if { $gnd != $::env(GND_NET)} {
        lappend secondary $gnd

        set db_net [[ord::get_db_block] findNet $gnd]
        if {$db_net == "NULL"} {
            set net [odb::dbNet_create [ord::get_db_block] $gnd]
            $net setSpecial
            $net setSigType "GROUND"
        }
    }
}

set_voltage_domain -name CORE -power $::env(VDD_NET) -ground $::env(GND_NET) \
    -secondary_power $secondary

define_pdn_grid \
    -name stdcell_grid \
    -starts_with POWER \
    -voltage_domain CORE \
    -pins $::env(PDN_VERTICAL_LAYER)

add_pdn_stripe \
    -grid stdcell_grid \
    -layer $::env(PDN_VERTICAL_LAYER) \
    -width $::env(PDN_VWIDTH) \
    -pitch $::env(PDN_VPITCH) \
    -offset $::env(PDN_VOFFSET) \
    -spacing $::env(PDN_VSPACING) \
    -starts_with POWER -extend_to_core_ring

if { $::env(PDN_ENABLE_RAILS) == 1 } {
    add_pdn_stripe \
        -grid stdcell_grid \
        -layer $::env(PDN_RAIL_LAYER) \
        -width $::env(PDN_RAIL_WIDTH) \
        -followpins

    add_pdn_connect \
        -grid stdcell_grid \
        -layers "$::env(PDN_RAIL_LAYER) $::env(PDN_VERTICAL_LAYER)"
}

# No macro grid: pdngen rejects one with no shapes (PDN-0233), and a
# FIXED macro already keeps the core grid off it. The connection is the
# bridges below, not a pdngen layer connect.

proc pe_macro_power {} {
    set block [ord::get_db_block]
    set m4 [[$block getTech] findLayer Metal4]
    set dbu [$block getDbUnitsPerMicron]
    set um [expr {1.0 / $dbu}]
    set overlap [expr {int(0.5 * $dbu)}]       ;# bridge reaches 0.5 um into pin and stripe
    set max_reach [expr {int(40.0 * $dbu)}]    ;# stripes end at or near the macro edge; farther is a misfit
    set nmacros 0
    foreach inst [$block getInsts] {
        if { ![[$inst getMaster] isBlock] } { continue }
        incr nmacros
        set iname [$inst getName]
        set ib [$inst getBBox]
        set bx0 [$ib xMin]; set by0 [$ib yMin]; set bx1 [$ib xMax]; set by1 [$ib yMax]

        # supply pin columns, in die coordinates (getGeometries is transformed)
        set pins [list]
        set bridged [dict create]
        foreach iterm [$inst getITerms] {
            set sig [[$iterm getMTerm] getSigType]
            if { $sig ne "POWER" && $sig ne "GROUND" } { continue }
            set pname [[$iterm getMTerm] getName]
            set net [$iterm getNet]
            if { $net eq "NULL" } {
                error "PEPDN: $iname/$pname has no net (check PDN_MACRO_CONNECTIONS)"
            }
            dict set bridged $pname 0
            foreach geom [$iterm getGeometries] {
                lassign $geom glayer grect
                if { [$glayer getName] ne "Metal4" } { continue }
                lappend pins [list $net $pname [$grect xMin] [$grect yMin] [$grect xMax] [$grect yMax]]
            }
        }

        # 1. no supply shape may cross the macro except inside a same-net column
        foreach net [$block getNets] {
            if { ![$net isSpecial] } { continue }
            foreach sw [$net getSWires] {
                foreach sb [$sw getWires] {
                    if { [$sb isVia] || [$sb getTechLayer] ne $m4 } { continue }
                    set sx0 [$sb xMin]; set sy0 [$sb yMin]; set sx1 [$sb xMax]; set sy1 [$sb yMax]
                    if { $sx1 <= $bx0 || $sx0 >= $bx1 || $sy1 <= $by0 || $sy0 >= $by1 } { continue }
                    set inside 0
                    foreach p $pins {
                        lassign $p pnet pname px0 py0 px1 py1
                        if { $pnet eq $net && $sx0 >= $px0 && $sx1 <= $px1 && $sy0 >= $py0 && $sy1 <= $py1 } {
                            set inside 1
                            break
                        }
                    }
                    if { !$inside } {
                        error [format "PEPDN: %s Metal4 shape x %.3f..%.3f y %.3f..%.3f crosses %s outside a same-net column" \
                            [$net getName] [expr {$sx0 * $um}] [expr {$sx1 * $um}] [expr {$sy0 * $um}] [expr {$sy1 * $um}] $iname]
                    }
                }
            }
        }

        # 2. bridge every edge-reaching column to the nearest stripe that fits inside it
        foreach p $pins {
            lassign $p net pname px0 py0 px1 py1
            foreach side {top bottom} {
                if { $side eq "top" && $py1 < $by1 - 1 } { continue }
                if { $side eq "bottom" && $py0 > $by0 + 1 } { continue }
                set best ""
                set best_d 0
                foreach sw [$net getSWires] {
                    foreach sb [$sw getWires] {
                        if { [$sb isVia] || [$sb getTechLayer] ne $m4 } { continue }
                        if { [$sb xMin] < $px0 || [$sb xMax] > $px1 } { continue }
                        if { $side eq "top" } {
                            if { [$sb yMin] < $by1 } { continue }
                            set d [expr {[$sb yMin] - $py1}]
                        } else {
                            if { [$sb yMax] > $by0 } { continue }
                            set d [expr {$py0 - [$sb yMax]}]
                        }
                        if { $d > $max_reach } { continue }
                        if { $best eq "" || $d < $best_d } {
                            set best $sb
                            set best_d $d
                        }
                    }
                }
                if { $best eq "" } { continue }
                set x0 [$best xMin]; set x1 [$best xMax]
                if { $side eq "top" } {
                    set y0 [expr {$py1 - $overlap}]; set y1 [expr {[$best yMin] + $overlap}]
                } else {
                    set y0 [expr {[$best yMax] - $overlap}]; set y1 [expr {$py0 + $overlap}]
                }
                set swire [odb::dbSWire_create $net ROUTED]
                odb::dbSBox_create $swire $m4 $x0 $y0 $x1 $y1 STRIPE
                dict incr bridged $pname
                puts [format "PEPDN bridge %s %s/%s %s x %.3f..%.3f y %.3f..%.3f" [$net getName] $iname $pname \
                    $side [expr {$x0 * $um}] [expr {$x1 * $um}] [expr {$y0 * $um}] [expr {$y1 * $um}]]
            }
        }

        # 3. every supply pin must be connected
        set missing [list]
        dict for {pname n} $bridged {
            puts "PEPDN $iname/$pname: $n bridge(s)"
            if { $n == 0 } { lappend missing $pname }
        }
        if { [llength $missing] > 0 } {
            error "PEPDN: no grid stripe reaches $iname supply pin(s) [join $missing {, }]; fix FP_PDN_VOFFSET/VPITCH or the macro location"
        }
    }
    puts "PEPDN checked $nmacros macro(s)"
}

# Tiny Tapeout precheck requires every power pin to run from the bottom
# edge of the tile to the top (within 10 um). pdngen puts a pin on every
# stripe, including the pieces of the stripes the macro cuts in two; drop
# those pins (the stripes stay, joined to the grid through the rails and
# the bridges) and fail if a supply is left with no full-height pin.
proc pe_trim_power_pins {} {
    set block [ord::get_db_block]
    set dbu [$block getDbUnitsPerMicron]
    set die [$block getDieArea]
    set reach [expr {int(10.0 * $dbu)}]
    foreach bterm [$block getBTerms] {
        set net [$bterm getNet]
        if { $net eq "NULL" } { continue }
        set sig [$net getSigType]
        if { $sig ne "POWER" && $sig ne "GROUND" } { continue }
        set kept 0
        set dropped 0
        foreach bpin [$bterm getBPins] {
            set full 1
            foreach box [$bpin getBoxes] {
                if { [$box yMin] > [$die yMin] + $reach || [$box yMax] < [$die yMax] - $reach } {
                    set full 0
                }
            }
            if { $full } {
                incr kept
            } else {
                odb::dbBPin_destroy $bpin
                incr dropped
            }
        }
        puts "PEPDN pins [$bterm getName]: kept $kept full-height, dropped $dropped partial"
        if { $kept == 0 } {
            error "PEPDN: no full-height pin left for [$bterm getName]"
        }
    }
}

# Run the bridge pass right after librelane's own pdngen call.
if { [info commands ::pe_pdngen_orig] eq "" } {
    rename ::pdngen ::pe_pdngen_orig
    proc ::pdngen { args } {
        set rc [catch { ::pe_pdngen_orig {*}$args } msg opts]
        if { $rc } { return -options $opts $msg }
        pe_macro_power
        pe_trim_power_pins
        return $msg
    }
}
