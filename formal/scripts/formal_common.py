"""Shared plumbing for run_formal.py / vacuity_check.py.

Toolchain recipe ported from the sibling 5-Stage-Pipelined-RISC-V-Processor
project's ooo/scripts/formal_common.py -- same yosys/z3 install, same
confirmed-working recipe (yosys 0.69, z3 4.16.0), no SymbiYosys/Verific:

    yosys: read_verilog -sv <srcs>; hierarchy -top <top>; prep -flatten;
           async2sync; chformal -lower; dffunmap; write_smt2 -wires <out.smt2>
    yosys-smtbmc -s z3 --presat -t <depth> <out.smt2>   (assert/BMC pass)
    yosys-smtbmc -s z3 -c -t <depth> <out.smt2>          (cover/vacuity pass)

`async2sync; chformal -lower; dffunmap` before `write_smt2` is NOT
optional -- confirmed in the sibling project via a deliberately wrong
assertion (assert(1'b0) silently reported PASSED without this exact
pass sequence, since write_smt2 scans for legacy $assert/$cover cells,
not the newer $check cells SV immediate assertions compile to).
`dffunmap` is required for any module with an async-reset always block
(`always @(posedge clk or negedge rst_n)`, which every module in this
project uses) -- without it, write_smt2 errors outright on $sdff cells.
Every props file added to this project gets the same "confirmed FAILED
on a deliberately broken mutant" treatment before being trusted --
see formal/AGENT_CONTRACT.md.

`bind` is parsed by this yosys build but never elaborated -- a bound
checker's cells silently vanish, no diagnostic. So this project never
uses `bind` for the actual formal run either (a props file's trailing
`bind` statement is documentation-only, mirroring how a real DUT
instantiation would wire it): a sibling-instantiation wrapper
instantiates DUT and checker as siblings, wired by same-named top-level
wires. A checker needing DUT-internal (non-port) state gets it via a
scratch debug-copy of the DUT's source with extra `dbg_<signal>`
output ports added (see DEBUG_PORTS below) -- the real src/ is never
modified, the copy lives under SCRATCH_DIR, regenerated fresh every run.

Trimmed relative to the sibling project's version: this project's DUT
sources are plain ANSI Verilog with packed-only ports (no unpacked-
array *ports* to normalize), and target-module identification comes
from parsing the props file's trailing `bind <target> <checker> ...`
line directly (matching the sibling project's own convention) rather
than a separate marker comment.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = REPO_ROOT / "src"
FORMAL_DIR = REPO_ROOT / "formal"
SCRATCH_DIR = Path("/tmp/protocol_emulator_formal_scratch")

# isa_defs.v has no module, just `define`s core.v/cycle_counter.v/
# pin_ctrl.v rely on via file-order visibility, not `include` (see
# src/cpu/core.v's own header for why) -- always read first, same
# convention as info.yaml's source_files / test/Makefile's
# PROJECT_SOURCES.
ISA_DEFS = SRC_DIR / "cpu" / "isa_defs.v"

# BMC depth. Bounded-depth BMC, not an unbounded inductive proof --
# same caveat the sibling project's toolchain carries. 20 is enough to
# exercise a handful of full LOAD-cycle/reload sequences on
# cycle_counter.v's own timescale without the query blowing up; revisit
# per-property if a future props file's antecedent needs more depth to
# say anything meaningful (sibling project's own DEFAULT_DEPTH
# comment explains the tradeoff this mirrors).
DEFAULT_DEPTH = 20

# Modules a target needs alongside it to elaborate (submodules it
# instantiates, transitively). Mirrors info.yaml/test/Makefile's own
# source lists. Empty by default -- most targets here are leaves.
EXTRA_SRCS = {
    "protocol_cpu_core": ["cpu/regfile.v", "io/cycle_counter.v"],
}

# Debug output ports a checker is permitted to observe on a target
# module's INTERNAL (non-port) state. {target_module: {signal_name:
# (packed_type_str, n_elems_or_None)}} -- n_elems is an int only for a
# genuinely unpacked array (e.g. pin_ctrl.v's `mode`/`drv`, declared
# `reg [1:0] mode [0:7]` / `reg drv [0:7]`), None for an
# already-packed signal. Add an entry here before writing a props file
# that needs it.
DEBUG_PORTS = {
    "protocol_cpu_core": {
        "state":                ("logic [1:0]", None),
        "flag":                 ("logic", None),
        "illegal_op_flag":      ("logic", None),
        "call_ret_misuse_flag": ("logic", None),
        "halted":                ("logic", None),
        "pc":                   ("logic [8:0]", None),
        "return_valid":         ("logic", None),
        # Added for agent_core_illegal_opcode_props.v's NOP-behavior
        # property: observed (asserted on), never used to identify the
        # committing instruction.
        "retaddr":              ("logic [8:0]", None),
        "rf_we":                ("logic", None),
        # Added for agent_core_reset_props.v. rf_waddr/rf_wdata are the
        # regfile's literal write-port inputs (ghost "written since
        # reset" tracking); reg_a_sel/reg_b_sel + ra_data/rb_data are
        # its two read ports -- regfile.v's `regs` array lives in a
        # child instance and isn't reachable by a dbg_ assign, so R0-R3
        # are observed through whatever the read ports select.
        # pending_lx_writeback is only ever asserted on.
        "rf_waddr":             ("logic [1:0]", None),
        "rf_wdata":             ("logic [7:0]", None),
        "reg_a_sel":            ("logic [1:0]", None),
        "reg_b_sel":            ("logic [1:0]", None),
        "ra_data":              ("logic [7:0]", None),
        "rb_data":              ("logic [7:0]", None),
        "pending_lx_writeback": ("logic", None),
    },
    "pin_ctrl": {
        "mode": ("logic [7:0][1:0]", 8),
        "drv":  ("logic [7:0]", 8),
    },
}


class FormalError(Exception):
    pass


def _strip_comments(text: str) -> str:
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def _find_matching_paren(text: str, open_idx: int) -> int:
    assert text[open_idx] == "("
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise FormalError(f"unbalanced parens starting at offset {open_idx}")


def _find_port_list_span(text: str, module_name: str) -> tuple[int, int]:
    m = re.search(r"\bmodule\s+" + re.escape(module_name) + r"\b", text)
    if not m:
        raise FormalError(f"module {module_name} not found")
    pos = m.end()
    rest = text[pos:]
    hash_m = re.match(r"\s*#\s*\(", rest)
    if hash_m:
        open_idx = pos + hash_m.end() - 1
        close_idx = _find_matching_paren(text, open_idx)
        pos = close_idx + 1
        rest = text[pos:]
    paren_m = re.match(r"\s*\(", rest)
    if not paren_m:
        raise FormalError(f"module {module_name}: no port list found")
    open_idx = pos + paren_m.end() - 1
    close_idx = _find_matching_paren(text, open_idx)
    return open_idx, close_idx


def _split_top_level_commas(s: str) -> list[str]:
    parts = []
    depth = 0
    cur = []
    for ch in s:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


_PORT_RE = re.compile(
    r"^\s*(input|output|inout)\s+"
    r"(?:(logic|wire|reg)\s+)?"
    r"((?:\[[^\]]*\]\s*)*)"
    r"(\w+)\s*$"
)


def parse_port_decls(port_list_text: str) -> dict[str, str]:
    """Returns {port_name: full_type_string_for_a_wire_decl}. Packed-only
    -- this project's checker files never declare unpacked-array ports,
    unlike the sibling project's rename_unit checker."""
    result = {}
    for entry in _split_top_level_commas(port_list_text):
        m = _PORT_RE.match(entry)
        if not m:
            raise FormalError(f"could not parse port declaration: {entry!r}")
        _direction, _kind, packed, name = m.groups()
        packed = packed.strip()
        type_str = "logic" + (f" {packed}" if packed else "")
        result[name] = type_str
    return result


def find_module_file(module_name: str) -> Path:
    for f in sorted(SRC_DIR.rglob("*.v")):
        if f == ISA_DEFS:
            continue
        if re.search(r"\bmodule\s+" + re.escape(module_name) + r"\b", f.read_text()):
            return f
    raise FormalError(f"no .v file under {SRC_DIR} declares module {module_name}")


def parse_props_file(props_path: Path) -> dict:
    """Returns {'checker_name': str, 'target_module': str, 'ports': {name: type_str}}.
    Target/checker pair comes from the props file's own trailing
    `bind <target> <checker> <inst> (` statement (documentation-only
    for the real run -- see module docstring), same convention as the
    sibling project."""
    text = props_path.read_text()
    stripped = _strip_comments(text)

    bind_m = re.search(r"\bbind\s+(\w+)\s+(\w+)\s+\w+\s*\(", stripped)
    if not bind_m:
        raise FormalError(f"{props_path}: no `bind <target> <checker> <inst> (` statement found")
    target_module, checker_name = bind_m.group(1), bind_m.group(2)

    open_idx, close_idx = _find_port_list_span(stripped, checker_name)
    port_list_text = stripped[open_idx + 1: close_idx]
    ports = parse_port_decls(port_list_text)
    return {"checker_name": checker_name, "target_module": target_module, "ports": ports}


def generate_debug_copy(target_module: str, dbg_ports: dict[str, str]) -> Path:
    """Writes a scratch copy of target_module's real source with extra
    `dbg_<name>` output ports added, each continuously assigned from
    the real internal signal. Never touches src/."""
    src_path = find_module_file(target_module)
    text = _strip_comments(src_path.read_text())

    open_idx, close_idx = _find_port_list_span(text, target_module)

    extra_ports = ",\n".join(
        f"    output {type_str} dbg_{name}" for name, (type_str, _n) in dbg_ports.items()
    )
    new_text = text[:close_idx] + ",\n" + extra_ports + "\n" + text[close_idx:]

    assign_lines = []
    for name, (_type_str, n_elems) in dbg_ports.items():
        if n_elems is None:
            assign_lines.append(f"    assign dbg_{name} = {name};")
        else:
            for i in range(n_elems):
                assign_lines.append(f"    assign dbg_{name}[{i}] = {name}[{i}];")
    assigns = "\n".join(assign_lines)
    endmodule_idx = new_text.rindex("endmodule")
    new_text = new_text[:endmodule_idx] + assigns + "\n\n" + new_text[endmodule_idx:]

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SCRATCH_DIR / f"{target_module}_dbgcopy.v"
    out_path.write_text(new_text)
    return out_path


def generate_wrapper(checker_name: str, target_module: str, ports: dict[str, str],
                      debug_ports: dict[str, str]) -> tuple[Path, str]:
    """Sibling-instantiation wrapper: instantiates the (debug-augmented)
    target module and the checker module as siblings, connecting every
    checker port to a same-named top-level wire. Real ports connect
    straight to the target's own same-named port; debug ports connect
    to the target's added dbg_<name> port. See module docstring for
    why this replaces `bind`."""
    wire_decls = []
    dut_conns = []
    chk_conns = []
    for name, type_str in ports.items():
        wire_decls.append(f"    {type_str} {name};")
        chk_conns.append(f"        .{name}({name})")
        if name in debug_ports:
            dut_conns.append(f"        .dbg_{name}({name})")
        else:
            dut_conns.append(f"        .{name}({name})")

    wrapper_name = f"formal_wrapper_{checker_name}"
    text = (
        f"// AUTO-GENERATED by run_formal.py / vacuity_check.py -- do not edit by hand.\n"
        f"module {wrapper_name};\n"
        + "\n".join(wire_decls) + "\n\n"
        f"    {target_module} u_dut (\n" + ",\n".join(dut_conns) + "\n    );\n\n"
        f"    {checker_name} u_chk (\n" + ",\n".join(chk_conns) + "\n    );\n\n"
        "endmodule\n"
    )

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SCRATCH_DIR / f"{wrapper_name}.v"
    out_path.write_text(text)
    return out_path, wrapper_name


def build_smt2(props_path: Path) -> tuple[Path, str]:
    """Full pipeline: parse props file, generate debug copy + wrapper,
    drive yosys to produce an SMT2 file. Returns (smt2_path, wrapper_top_name)."""
    info = parse_props_file(props_path)
    target_module = info["target_module"]
    checker_name = info["checker_name"]
    ports = info["ports"]

    debug_ports = DEBUG_PORTS.get(target_module, {})
    used_debug_ports = {n: t for n, t in debug_ports.items() if n in ports}

    dbg_copy_path = None
    if used_debug_ports:
        dbg_copy_path = generate_debug_copy(target_module, used_debug_ports)

    wrapper_path, wrapper_name = generate_wrapper(checker_name, target_module, ports, used_debug_ports)

    extra_srcs = [SRC_DIR / name for name in EXTRA_SRCS.get(target_module, [])]
    target_src = dbg_copy_path if dbg_copy_path else find_module_file(target_module)

    all_srcs = [ISA_DEFS] + extra_srcs + [target_src, props_path, wrapper_path]
    smt2_path = SCRATCH_DIR / f"{wrapper_name}.smt2"

    src_args = " ".join(str(p) for p in all_srcs)
    script = (
        f"read_verilog -sv {src_args}; "
        f"hierarchy -top {wrapper_name}; "
        f"prep -flatten; "
        f"async2sync; "
        f"chformal -lower; "
        f"dffunmap; "
        f"write_smt2 -wires {smt2_path}"
    )
    r = subprocess.run(["yosys", "-p", script], cwd=REPO_ROOT, capture_output=True, text=True)
    if r.returncode != 0 or not smt2_path.exists():
        raise FormalError(
            f"yosys failed building {props_path.name}:\n{r.stdout}\n{r.stderr}"
        )
    return smt2_path, wrapper_name


def run_smtbmc(smt2_path: Path, depth: int, cover_mode: bool) -> tuple[bool, str]:
    """Returns (status_passed, full_output_text)."""
    cmd = ["yosys-smtbmc", "-s", "z3", "-t", str(depth)]
    if cover_mode:
        cmd.append("-c")
    else:
        cmd.append("--presat")
    cmd.append(str(smt2_path))
    r = subprocess.run(cmd, capture_output=True, text=True)
    output = r.stdout + r.stderr
    passed = "Status: PASSED" in output
    return passed, output
