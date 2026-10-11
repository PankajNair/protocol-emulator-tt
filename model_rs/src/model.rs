//! Independent instruction-level model of the sequencer ISA, written from
//! docs/isa.md and docs/architecture.md only. See SPEC_NOTES.md for every
//! reading chosen where the spec is silent or ambiguous.

pub const MEM_BYTES: usize = 1024;
pub const DATA_BASE: usize = 512;
pub const DATA_BYTES: usize = 512;
/// PC is a 9-bit word index (branch `addr(9)` field width). SPEC_NOTES N3.
pub const PC_MASK: u16 = 0x1FF;

// Opcode values (isa.md "Opcode table").
pub const OP_NOP: u8 = 0;
pub const OP_HALT: u8 = 1;
pub const OP_RET: u8 = 2;
pub const OP_BRANCH: u8 = 3;
pub const OP_LDI: u8 = 4;
pub const OP_SET: u8 = 5;
pub const OP_OUT: u8 = 6;
pub const OP_IN: u8 = 7;
pub const OP_SHIFT: u8 = 8;
pub const OP_DELAY: u8 = 9;
pub const OP_WAIT: u8 = 10;
pub const OP_TEST: u8 = 11;
pub const OP_OUTB: u8 = 12;
pub const OP_INB: u8 = 13;
pub const OP_LOAD: u8 = 14;
pub const OP_STORE: u8 = 15;
pub const OP_LOOP: u8 = 16;
pub const OP_CMP: u8 = 17;
pub const OP_LOADX: u8 = 18;

// SET drive-mode encoding (isa.md SET row).
pub const MODE_KEEP: u8 = 0;
pub const MODE_PUSH_PULL: u8 = 1;
pub const MODE_OPEN_DRAIN: u8 = 2;
pub const MODE_INPUT: u8 = 3;

pub const PIN_HOST_GO: usize = 4;
pub const PIN_HOST_STATUS: usize = 5;
pub const PIN_HOST_ERROR: usize = 6;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct State {
    pub pc: u16,
    pub regs: [u8; 4],
    pub flag: u8,
    pub retaddr: u16,
    pub return_valid: u8,
    pub illegal_op_flag: u8,
    pub call_ret_misuse_flag: u8,
    pub halted: u8,
    pub uo_out: u8,
    pub pin_mode: [u8; 8],
    pub pin_drv: [u8; 8],
}

impl State {
    /// architecture.md "Reset values".
    pub fn reset() -> State {
        let mut pin_mode = [MODE_INPUT; 8];
        // Hardwired-role pins (architecture.md I/O register reset state):
        // HOST_GO fixed input, HOST_STATUS / HOST_ERROR fixed outputs.
        // Reported as their fixed role mode. SPEC_NOTES N8.
        pin_mode[PIN_HOST_GO] = MODE_INPUT;
        pin_mode[PIN_HOST_STATUS] = MODE_PUSH_PULL;
        pin_mode[PIN_HOST_ERROR] = MODE_PUSH_PULL;
        State {
            pc: 0,
            regs: [0; 4],
            flag: 0,
            retaddr: 0,
            return_valid: 0,
            illegal_op_flag: 0,
            call_ret_misuse_flag: 0,
            halted: 0,
            uo_out: 0,
            pin_mode,
            pin_drv: [0; 8], // uio_out resets to all-0
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Commit {
    pub pc: u16,
    pub word: u16,
    pub cycles: u64,
    pub state: State,
}

#[derive(Debug, Clone)]
pub struct RunResult {
    pub commits: Vec<Commit>,
    pub final_state: State,
    pub data_mem: Vec<u8>,
    pub halted: bool,
    pub timeout: bool,
}

/// Decoded fields (isa.md "Instruction formats"; MSB-first).
#[derive(Debug, Clone, Copy)]
pub struct Fields {
    pub opcode: u8,
    /// branch / LOOP addr = word[10:2]
    pub addr: u16,
    /// branch cond / LOOP Rd = word[1:0]
    pub low2: u8,
    /// reg+imm / reg-reg Rd = word[10:9]
    pub rd: usize,
    /// reg+imm imm = word[8:0]
    pub imm: u16,
    /// reg-reg Rs = word[8:7]
    pub rs: usize,
}

pub fn decode(word: u16) -> Fields {
    Fields {
        opcode: ((word >> 11) & 0x1F) as u8,
        addr: (word >> 2) & 0x1FF,
        low2: (word & 0x3) as u8,
        rd: ((word >> 9) & 0x3) as usize,
        imm: word & 0x1FF,
        rs: ((word >> 7) & 0x3) as usize,
    }
}

/// `mantissa << (exponent*5)` (isa.md DELAY / WAIT rows).
pub fn scaled(mantissa: u64, exponent: u64) -> u64 {
    mantissa << (exponent * 5)
}

/// Pin-index / one-bit sub-fields of a reg+imm `imm` for SET/OUTB/INB:
/// isa.md "The same MSB-first rule applies to sub-fields inside imm(9)":
/// pin_index = imm[8:6], value/bit_select = imm[5], imm[4:0] unused.
/// SPEC_NOTES N1 (resolved by spec).
fn pin_and_bit(imm: u16) -> (usize, u8) {
    (((imm >> 6) & 0x7) as usize, ((imm >> 5) & 1) as u8)
}

pub struct Machine<'a> {
    pub st: State,
    pub mem: [u8; MEM_BYTES],
    pub cycle: u64,
    ui: &'a [u8],
    uio: &'a [u8],
}

impl<'a> Machine<'a> {
    pub fn new(image: &[u8], ui: &'a [u8], uio: &'a [u8]) -> Machine<'a> {
        let mut mem = [0u8; MEM_BYTES];
        let n = image.len().min(MEM_BYTES);
        mem[..n].copy_from_slice(&image[..n]);
        Machine { st: State::reset(), mem, cycle: 0, ui, uio }
    }

    fn ui_at(&self, t: u64) -> u8 {
        if t < self.ui.len() as u64 { self.ui[t as usize] } else { 0 }
    }

    fn pad(&self, t: u64, pin: usize) -> u8 {
        let v = if t < self.uio.len() as u64 { self.uio[t as usize] } else { 0 };
        (v >> pin) & 1
    }

    /// Byte order: low byte at PC*2, high byte at PC*2+1 (isa.md).
    pub fn fetch(&self, pc: u16) -> u16 {
        let a = (pc as usize) * 2;
        (self.mem[a] as u16) | ((self.mem[a + 1] as u16) << 8)
    }

    /// Drive `value` onto pin per its current sticky mode (SET mode 00 / OUTB).
    fn drive(&mut self, pin: usize, value: u8) {
        match pin {
            PIN_HOST_GO => {} // oe hardwired 0: no-op
            PIN_HOST_STATUS | PIN_HOST_ERROR => self.st.pin_drv[pin] = value,
            _ => match self.st.pin_mode[pin] {
                MODE_PUSH_PULL | MODE_OPEN_DRAIN => self.st.pin_drv[pin] = value,
                _ => {} // input (unconfigured): harmless no-op
            },
        }
    }

    fn exec_set(&mut self, mode: u8, pin: usize, value: u8) {
        match pin {
            PIN_HOST_GO => {}
            PIN_HOST_STATUS | PIN_HOST_ERROR => {
                // oe fixed by role; mode field has no effect. SPEC_NOTES N9.
                self.st.pin_drv[pin] = value;
            }
            _ => match mode {
                MODE_KEEP => self.drive(pin, value),
                MODE_PUSH_PULL | MODE_OPEN_DRAIN => {
                    self.st.pin_mode[pin] = mode;
                    self.st.pin_drv[pin] = value;
                }
                _ => {
                    // input: oe forced 0, value ignored
                    self.st.pin_mode[pin] = MODE_INPUT;
                }
            },
        }
    }

    /// Execute one instruction. Returns Some(commit) if it completed within
    /// `max_cycles`, None if the cycle budget is exceeded first (the
    /// in-flight instruction is not committed). Must not be called once halted.
    pub fn step(&mut self, max_cycles: u64) -> Option<Commit> {
        let t0 = self.cycle;
        let pc = self.st.pc;
        let word = self.fetch(pc);
        let f = decode(word);
        let exec0 = t0 + 2; // first EXECUTE cycle (INTERFACE.md)
        let next_pc = (pc + 1) & PC_MASK;

        // 1) Determine occupancy without mutating state.
        let fits = |c: u64| t0.checked_add(c).map_or(false, |e| e <= max_cycles);
        let mut wait_flag = 0u8;
        let cycles: u64 = match f.opcode {
            OP_DELAY => 3 + scaled(f.imm as u64, f.rd as u64),
            OP_WAIT => {
                let pin = ((f.imm >> 6) & 0x7) as usize;
                let level = ((f.imm >> 5) & 1) as u8;
                let n = scaled((f.imm & 0x1F) as u64, f.rd as u64);
                // Budget: condition checked at exec0 + k, occupancy 3+k.
                if max_cycles < t0 + 3 {
                    return None;
                }
                let budget_k = max_cycles - t0 - 3;
                let k_limit = if n == 0 { budget_k } else { n.min(budget_k) };
                match self.find_level(exec0, pin, level, k_limit) {
                    Some(k) => {
                        wait_flag = 0; // condition met (wins ties)
                        3 + k
                    }
                    None => {
                        if n != 0 && n <= budget_k {
                            wait_flag = 1; // timed out
                            3 + n
                        } else {
                            return None; // ran out of cycle budget
                        }
                    }
                }
            }
            _ => 3,
        };
        if !fits(cycles) {
            return None;
        }

        // 2) Apply effects.
        let s = &mut self.st;
        let mut pc_next = next_pc;
        match f.opcode {
            OP_NOP => {}
            OP_HALT => {
                s.halted = 1;
                pc_next = pc; // SPEC_NOTES N4
            }
            OP_RET => {
                if s.return_valid == 0 {
                    s.call_ret_misuse_flag = 1;
                }
                s.return_valid = 0;
                pc_next = s.retaddr;
            }
            OP_BRANCH => match f.low2 {
                0 => pc_next = f.addr,
                1 => {
                    if s.flag == 1 {
                        pc_next = f.addr
                    }
                }
                2 => {
                    if s.flag == 0 {
                        pc_next = f.addr
                    }
                }
                _ => {
                    if s.return_valid == 1 {
                        s.call_ret_misuse_flag = 1;
                    }
                    s.retaddr = next_pc;
                    s.return_valid = 1;
                    pc_next = f.addr;
                }
            },
            OP_LDI => s.regs[f.rd] = (f.imm & 0xFF) as u8,
            OP_SET => {
                let (pin, value) = pin_and_bit(f.imm);
                let mode = f.rd as u8;
                self.exec_set(mode, pin, value);
            }
            OP_OUT => s.uo_out = s.regs[f.rd],
            OP_IN => {
                let v = self.ui_at(exec0);
                self.st.regs[f.rd] = v;
            }
            OP_SHIFT => {
                let r = s.regs[f.rd];
                s.regs[f.rd] = if f.imm & 1 == 0 { r << 1 } else { r >> 1 };
            }
            OP_DELAY => {}
            OP_WAIT => s.flag = wait_flag,
            OP_TEST => {
                let bit = (f.imm & 0x7) as u8;
                s.flag = (s.regs[f.rd] >> bit) & 1;
            }
            OP_OUTB => {
                let (pin, sel) = pin_and_bit(f.imm);
                let bit = if sel == 0 { 0 } else { 7 };
                let v = (s.regs[f.rd] >> bit) & 1;
                self.drive(pin, v);
            }
            OP_INB => {
                let (pin, sel) = pin_and_bit(f.imm);
                let bit = if sel == 0 { 0 } else { 7 };
                let v = self.pad(exec0, pin);
                let r = &mut self.st.regs[f.rd];
                *r = (*r & !(1 << bit)) | (v << bit);
            }
            OP_LOAD => {
                let v = self.mem[DATA_BASE + f.imm as usize];
                self.st.regs[f.rd] = v;
            }
            OP_STORE => {
                let v = s.regs[f.rd];
                self.mem[DATA_BASE + f.imm as usize] = v;
            }
            OP_LOOP => {
                let r = f.low2 as usize;
                s.regs[r] = s.regs[r].wrapping_sub(1);
                if s.regs[r] != 0 {
                    pc_next = f.addr;
                }
            }
            OP_CMP => s.flag = (s.regs[f.rd] == s.regs[f.rs]) as u8,
            OP_LOADX => {
                let idx = self.st.regs[f.rs] as usize;
                let v = self.mem[DATA_BASE + idx];
                self.st.regs[f.rd] = v;
            }
            _ => {
                // 19-31 reserved: NOP + sticky illegal-opcode flag.
                s.illegal_op_flag = 1;
            }
        }
        self.st.pc = pc_next;
        self.cycle = t0 + cycles;
        Some(Commit { pc, word, cycles, state: self.st.clone() })
    }

    /// Smallest k in 0..=k_limit with pad(exec0+k, pin) == level.
    fn find_level(&self, exec0: u64, pin: usize, level: u8, k_limit: u64) -> Option<u64> {
        let len = self.uio.len() as u64;
        let mut k = 0u64;
        while k <= k_limit {
            let t = exec0 + k;
            if t >= len {
                // Past the end every pad reads 0: constant from here on.
                return if level == 0 { Some(k) } else { None };
            }
            if self.pad(t, pin) == level {
                return Some(k);
            }
            k += 1;
        }
        None
    }
}

pub fn run(image: &[u8], ui: &[u8], uio: &[u8], max_cycles: u64) -> RunResult {
    let mut m = Machine::new(image, ui, uio);
    let mut commits = Vec::new();
    let mut timeout = false;
    while m.st.halted == 0 {
        match m.step(max_cycles) {
            Some(c) => commits.push(c),
            None => {
                timeout = true;
                break;
            }
        }
    }
    RunResult {
        commits,
        halted: m.st.halted == 1,
        final_state: m.st.clone(),
        data_mem: m.mem[DATA_BASE..DATA_BASE + DATA_BYTES].to_vec(),
        timeout,
    }
}

// ---------------------------------------------------------------- encoders
#[cfg(test)]
pub fn enc_branch(cond: u16, addr: u16) -> u16 {
    ((OP_BRANCH as u16) << 11) | ((addr & 0x1FF) << 2) | (cond & 3)
}
#[cfg(test)]
pub fn enc_loop(addr: u16, rd: u16) -> u16 {
    ((OP_LOOP as u16) << 11) | ((addr & 0x1FF) << 2) | (rd & 3)
}
#[cfg(test)]
pub fn enc_ri(op: u8, rd: u16, imm: u16) -> u16 {
    ((op as u16) << 11) | ((rd & 3) << 9) | (imm & 0x1FF)
}
#[cfg(test)]
pub fn enc_rr(op: u8, rd: u16, rs: u16) -> u16 {
    ((op as u16) << 11) | ((rd & 3) << 9) | ((rs & 3) << 7)
}
#[cfg(test)]
pub fn enc_op(op: u8) -> u16 {
    (op as u16) << 11
}
#[cfg(test)]
/// WAIT imm = pin(3) | level(1) | mantissa(5); Rd field = exponent.
pub fn enc_wait(pin: u16, level: u16, exp: u16, mant: u16) -> u16 {
    enc_ri(OP_WAIT, exp, ((pin & 7) << 6) | ((level & 1) << 5) | (mant & 0x1F))
}
#[cfg(test)]
/// SET/OUTB/INB imm = pin(3) | bit(1) | unused(5), MSB-first.
pub fn enc_pin(op: u8, rd_or_mode: u16, pin: u16, bit: u16) -> u16 {
    enc_ri(op, rd_or_mode, ((pin & 7) << 6) | ((bit & 1) << 5))
}

#[cfg(test)]
pub fn image_of(words: &[u16]) -> Vec<u8> {
    let mut img = vec![0u8; MEM_BYTES];
    for (i, w) in words.iter().enumerate() {
        img[2 * i] = (*w & 0xFF) as u8;
        img[2 * i + 1] = (*w >> 8) as u8;
    }
    img
}

#[cfg(test)]
mod tests {
    use super::*;

    const HALT: u16 = (OP_HALT as u16) << 11;

    fn go(words: &[u16]) -> RunResult {
        run(&image_of(words), &[], &[], 1_000_000)
    }

    // architecture.md "Reset values"
    #[test]
    fn reset_state() {
        let s = State::reset();
        assert_eq!(s.pc, 0);
        assert_eq!(s.regs, [0; 4]);
        assert_eq!((s.flag, s.retaddr, s.return_valid), (0, 0, 0));
        assert_eq!((s.illegal_op_flag, s.call_ret_misuse_flag, s.halted, s.uo_out), (0, 0, 0, 0));
        for p in [0, 1, 2, 3, 7] {
            assert_eq!(s.pin_mode[p], MODE_INPUT, "protocol pins reset to input");
        }
        assert_eq!(s.pin_drv, [0; 8], "uio_out resets to all-0");
    }

    // isa.md "Instruction formats": opcode = word[15:11]; byte order low byte at PC*2
    #[test]
    fn fetch_byte_order_and_field_positions() {
        let w = enc_ri(OP_LDI, 2, 0x5A);
        let img = image_of(&[w, HALT]);
        assert_eq!(img[0], (w & 0xFF) as u8);
        assert_eq!(img[1], (w >> 8) as u8);
        let r = run(&img, &[], &[], 100);
        assert_eq!(r.commits[0].word, w);
        assert_eq!(r.commits[0].pc, 0);
        assert_eq!(r.final_state.regs[2], 0x5A);
        let f = decode(0b10010_10_1100_1100_1u16);
        assert_eq!(f.opcode, 18);
        assert_eq!(f.rd, 2);
        assert_eq!(f.rs, 3);
    }

    // isa.md LDI: 8-bit value in imm(7:0), imm(8) unused; OUT drives uo_out; HALT
    #[test]
    fn ldi_out_halt() {
        let r = go(&[enc_ri(OP_LDI, 1, 0x1A5), enc_ri(OP_OUT, 1, 0x1FF), HALT, enc_ri(OP_LDI, 0, 9)]);
        assert_eq!(r.commits.len(), 3);
        assert_eq!(r.commits[0].state.regs[1], 0xA5);
        assert_eq!(r.commits[1].state.uo_out, 0xA5);
        assert!(r.halted && !r.timeout);
        assert_eq!(r.final_state.halted, 1);
        assert_eq!(r.final_state.regs[0], 0, "nothing executes after HALT");
    }

    // architecture.md: uniform 3 cycles/instruction
    #[test]
    fn uniform_three_cycles() {
        let r = go(&[
            0,
            enc_ri(OP_LDI, 0, 1),
            enc_branch(0, 3),
            enc_rr(OP_CMP, 0, 1),
            enc_pin(OP_SET, 1, 0, 1),
            enc_ri(OP_SHIFT, 0, 0),
            enc_ri(OP_STORE, 0, 0),
            enc_ri(OP_LOAD, 1, 0),
            enc_rr(OP_LOADX, 2, 3),
            enc_ri(OP_TEST, 0, 0),
            enc_op(25),
            HALT,
        ]);
        for c in &r.commits {
            assert_eq!(c.cycles, 3, "pc {}", c.pc);
        }
        assert!(r.halted);
    }

    // isa.md NOP row: low 11 bits don't-care
    #[test]
    fn nop_ignores_low_bits() {
        let r = go(&[0x07FF, HALT]);
        assert_eq!(r.commits[0].state, {
            let mut s = State::reset();
            s.pc = 1;
            s
        });
    }

    // isa.md "Undefined opcode behavior" + opcode table 19-31
    #[test]
    fn reserved_opcodes_nop_and_sticky_flag() {
        for op in 19u8..=31 {
            let r = go(&[enc_ri(OP_LDI, 0, 7), enc_op(op) | 0x7FF, 0, HALT]);
            let c = &r.commits[1];
            assert_eq!(c.cycles, 3);
            assert_eq!(c.state.illegal_op_flag, 1, "op {}", op);
            assert_eq!(c.state.regs[0], 7);
            assert_eq!(c.state.pc, 2);
            assert_eq!(r.final_state.illegal_op_flag, 1, "sticky");
        }
        assert_eq!(go(&[0, HALT]).final_state.illegal_op_flag, 0);
    }

    // isa.md branch cond table
    #[test]
    fn branch_conditions() {
        // JMP
        let r = go(&[enc_branch(0, 2), HALT, HALT]);
        assert_eq!(r.commits[0].state.pc, 2);
        // BEQ taken iff flag set; flag set via CMP R0,R0
        let r = go(&[enc_rr(OP_CMP, 0, 0), enc_branch(1, 5), HALT, 0, 0, HALT]);
        assert_eq!(r.commits[1].state.pc, 5);
        let r = go(&[enc_rr(OP_CMP, 0, 0), enc_branch(2, 5), HALT, 0, 0, HALT]);
        assert_eq!(r.commits[1].state.pc, 2);
        // flag clear (reset)
        let r = go(&[enc_branch(1, 5), HALT, 0, 0, 0, HALT]);
        assert_eq!(r.commits[0].state.pc, 1);
        let r = go(&[enc_branch(2, 5), HALT, 0, 0, 0, HALT]);
        assert_eq!(r.commits[0].state.pc, 5);
    }

    // isa.md CALL/RET, return-valid, misuse flag
    #[test]
    fn call_ret_and_misuse() {
        let r = go(&[enc_branch(3, 3), HALT, 0, enc_op(OP_RET)]);
        assert_eq!(r.commits[0].state.retaddr, 1);
        assert_eq!(r.commits[0].state.return_valid, 1);
        assert_eq!(r.commits[0].state.pc, 3);
        assert_eq!(r.commits[1].state.pc, 1);
        assert_eq!(r.commits[1].state.return_valid, 0);
        assert_eq!(r.final_state.call_ret_misuse_flag, 0);
        // nested CALL overwrites and flags
        let r = go(&[enc_branch(3, 2), HALT, enc_branch(3, 4), HALT, enc_op(OP_RET)]);
        assert_eq!(r.commits[1].state.retaddr, 3);
        assert_eq!(r.commits[1].state.call_ret_misuse_flag, 1);
        assert_eq!(r.commits[2].state.pc, 3);
        // orphan RET jumps to reset value 0 and flags
        let r = run(&image_of(&[0, enc_op(OP_RET)]), &[], &[], 9);
        assert_eq!(r.commits[1].state.pc, 0);
        assert_eq!(r.commits[1].state.call_ret_misuse_flag, 1);
        assert_eq!(r.commits[2].state.call_ret_misuse_flag, 1, "sticky");
        assert!(r.timeout);
    }

    // isa.md SHIFT: 0=left, 1=right, vacated bit 0, flag untouched
    #[test]
    fn shift_semantics() {
        let r = go(&[
            enc_rr(OP_CMP, 0, 0), // flag=1
            enc_ri(OP_LDI, 0, 0x81),
            enc_ri(OP_SHIFT, 0, 0),
            enc_ri(OP_LDI, 1, 0x81),
            enc_ri(OP_SHIFT, 1, 1),
            HALT,
        ]);
        assert_eq!(r.final_state.regs[0], 0x02);
        assert_eq!(r.final_state.regs[1], 0x40);
        assert_eq!(r.final_state.flag, 1);
    }

    // isa.md DELAY: cycles = mantissa << (exponent*5); architecture.md 3+N
    #[test]
    fn delay_cycles() {
        for (e, m, n) in [(0u16, 0u16, 0u64), (0, 511, 511), (1, 511, 16_352), (2, 3, 3072), (3, 511, 16_744_448)] {
            let r = run(&image_of(&[enc_ri(OP_DELAY, e, m), HALT]), &[], &[], 20_000_000);
            assert_eq!(r.commits[0].cycles, 3 + n, "e={} m={}", e, m);
            assert_eq!(r.commits[0].state.regs, [0; 4], "DELAY doesn't touch regfile");
        }
    }

    // isa.md WAIT: condition met -> flag 0, cycles 3+k
    #[test]
    fn wait_met() {
        let w = enc_wait(1, 1, 0, 10);
        // pin1 high from cycle 5: exec0=2, k=3 -> 6 cycles
        let uio = [0, 0, 0, 0, 0, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2];
        let img = image_of(&[enc_rr(OP_CMP, 0, 0), w, HALT]);
        // CMP occupies 0..3, WAIT starts at 3, exec0 = 5 -> met at k=0
        let r = run(&img, &[], &uio, 1000);
        assert_eq!(r.commits[1].cycles, 3);
        assert_eq!(r.commits[1].state.flag, 0);
        let r = run(&image_of(&[w, HALT]), &[], &uio, 1000);
        assert_eq!(r.commits[0].cycles, 6);
        assert_eq!(r.commits[0].state.flag, 0);
    }

    // isa.md WAIT: timeout -> flag 1, cycles 3+N
    #[test]
    fn wait_timeout() {
        let r = go(&[enc_wait(0, 1, 1, 2), HALT]); // N = 2<<5 = 64; pads all 0
        assert_eq!(r.commits[0].cycles, 3 + 64);
        assert_eq!(r.commits[0].state.flag, 1);
        assert!(r.halted);
    }

    // isa.md WAIT tie rule: condition-met wins on the expiring cycle
    #[test]
    fn wait_tie_condition_wins() {
        // N=4: exec cycles 2..=6; pin3 high only at cycle 6 (last).
        let mut uio = vec![0u8; 20];
        uio[6] = 1 << 3;
        let r = run(&image_of(&[enc_wait(3, 1, 0, 4), HALT]), &[], &uio, 100);
        assert_eq!(r.commits[0].cycles, 7);
        assert_eq!(r.commits[0].state.flag, 0);
        // one cycle later -> timeout
        let mut uio = vec![0u8; 20];
        uio[7] = 1 << 3;
        let r = run(&image_of(&[enc_wait(3, 1, 0, 4), HALT]), &[], &uio, 100);
        assert_eq!(r.commits[0].cycles, 7);
        assert_eq!(r.commits[0].state.flag, 1);
    }

    // isa.md WAIT: any computed-zero cycles (e.g. exp=2, mant=0) = unbounded
    #[test]
    fn wait_computed_zero_is_unbounded() {
        let mut uio = vec![0u8; 5000];
        uio[4000] = 1 << 7;
        let img = image_of(&[enc_rr(OP_CMP, 0, 0), enc_wait(7, 1, 2, 0), HALT]);
        let r = run(&img, &[], &uio, 100_000);
        assert_eq!(r.commits[1].cycles, 3 + (4000 - 5));
        assert_eq!(r.commits[1].state.flag, 0, "unbounded WAIT always writes 0");
        // never met -> run-level timeout, WAIT not committed
        let r = run(&image_of(&[enc_wait(0, 1, 0, 0), HALT]), &[], &[], 500);
        assert!(r.timeout && !r.halted);
        assert!(r.commits.is_empty());
    }

    // isa.md WAIT on level 0, and INTERFACE: index past end reads 0
    #[test]
    fn wait_level_zero_past_end() {
        let uio = [0xFF; 3];
        let r = run(&image_of(&[enc_wait(2, 0, 0, 0), HALT]), &[], &uio, 100);
        assert_eq!(r.commits[0].cycles, 4); // exec0=2 high, t=3 past end -> 0
    }

    // isa.md TEST-bit: flag=1 iff tested bit=1
    #[test]
    fn test_bit_polarity() {
        let r = go(&[enc_ri(OP_LDI, 2, 0x80), enc_ri(OP_TEST, 2, 7), enc_ri(OP_TEST, 2, 6), HALT]);
        assert_eq!(r.commits[1].state.flag, 1);
        assert_eq!(r.commits[2].state.flag, 0);
    }

    // isa.md CMP: flag = (Rd==Rs), last write wins
    #[test]
    fn cmp_equality() {
        let r = go(&[enc_ri(OP_LDI, 0, 5), enc_ri(OP_LDI, 3, 5), enc_rr(OP_CMP, 0, 3), enc_rr(OP_CMP, 0, 1), HALT]);
        assert_eq!(r.commits[2].state.flag, 1);
        assert_eq!(r.commits[3].state.flag, 0);
    }

    // isa.md LOOP: decrement Rd, branch if nonzero, flag untouched
    #[test]
    fn loop_counts() {
        let r = go(&[enc_rr(OP_CMP, 0, 0), enc_ri(OP_LDI, 1, 3), enc_loop(2, 1), HALT]);
        let loops: Vec<_> = r.commits.iter().filter(|c| c.pc == 2).collect();
        assert_eq!(loops.len(), 3);
        assert_eq!(loops[0].state.regs[1], 2);
        assert_eq!(loops[0].state.pc, 2);
        assert_eq!(loops[2].state.regs[1], 0);
        assert_eq!(loops[2].state.pc, 3);
        assert_eq!(r.final_state.flag, 1);
    }

    // isa.md SET drive modes; OUTB obeys sticky mode; unconfigured pin no-op
    #[test]
    fn set_modes_and_outb() {
        let r = go(&[
            enc_pin(OP_SET, 0, 0, 1),  // mode keep on input pin: no-op
            enc_pin(OP_SET, 1, 0, 1),  // pin0 push-pull, drive 1
            enc_pin(OP_SET, 2, 1, 0),  // pin1 open-drain, drive low
            enc_pin(OP_SET, 3, 2, 1),  // pin2 input
            enc_pin(OP_SET, 0, 0, 0),  // pin0 drive 0, keep pp
            enc_pin(OP_SET, 1, 4, 1),  // HOST_GO: no-op
            enc_pin(OP_SET, 0, 5, 1),  // HOST_STATUS = 1
            HALT,
        ]);
        let c = &r.commits;
        assert_eq!(c[0].state.pin_drv[0], 0);
        assert_eq!(c[0].state.pin_mode[0], MODE_INPUT);
        assert_eq!((c[1].state.pin_mode[0], c[1].state.pin_drv[0]), (MODE_PUSH_PULL, 1));
        assert_eq!((c[2].state.pin_mode[1], c[2].state.pin_drv[1]), (MODE_OPEN_DRAIN, 0));
        assert_eq!(c[3].state.pin_mode[2], MODE_INPUT);
        assert_eq!((c[4].state.pin_mode[0], c[4].state.pin_drv[0]), (MODE_PUSH_PULL, 0));
        assert_eq!(c[5].state.pin_drv[4], 0);
        assert_eq!(c[6].state.pin_drv[5], 1);

        // OUTB on unconfigured pin = no-op; after SET pp it drives Rd[bit]
        let r = go(&[
            enc_ri(OP_LDI, 0, 0x80),
            enc_pin(OP_OUTB, 0, 7, 1),
            enc_pin(OP_SET, 1, 7, 0),
            enc_pin(OP_OUTB, 0, 7, 1), // bit7 = 1
            enc_pin(OP_OUTB, 0, 7, 0), // bit0 = 0
            HALT,
        ]);
        assert_eq!(r.commits[1].state.pin_drv[7], 0);
        assert_eq!(r.commits[3].state.pin_drv[7], 1);
        assert_eq!(r.commits[4].state.pin_drv[7], 0);
    }

    // isa.md "Worked idioms" TX: OUTB before SHIFT sends MSB first
    #[test]
    fn tx_idiom() {
        // SET TXPIN,01,1 ; LDI R0,0xA5 ; LDI R1,8 ;
        // L: OUTB R0,TX,7 ; DELAY 10 ; SHIFT R0,0 ; LOOP L,R1 ; HALT
        let r = go(&[
            enc_pin(OP_SET, 1, 0, 1),
            enc_ri(OP_LDI, 0, 0xA5),
            enc_ri(OP_LDI, 1, 8),
            enc_pin(OP_OUTB, 0, 0, 1),
            enc_ri(OP_DELAY, 0, 10),
            enc_ri(OP_SHIFT, 0, 0),
            enc_loop(3, 1),
            HALT,
        ]);
        let bits: Vec<u8> = r.commits.iter().filter(|c| c.pc == 3).map(|c| c.state.pin_drv[0]).collect();
        assert_eq!(bits, vec![1, 0, 1, 0, 0, 1, 0, 1]);
        // per-bit loop overhead: OUTB+DELAY+SHIFT+LOOP = 12 fixed + N
        let per_bit: u64 = r.commits.iter().filter(|c| (3..=6).contains(&c.pc)).map(|c| c.cycles).sum::<u64>() / 8;
        assert_eq!(per_bit, 12 + 10);
    }

    // isa.md "Worked idioms" RX: SHIFT before INB reconstructs the byte (MSB first)
    #[test]
    fn rx_idiom() {
        // L: DELAY 0 ; SHIFT R0,0 ; INB R0,RX(1),0 ; LOOP L,R1
        let words = [
            enc_ri(OP_LDI, 1, 8),
            enc_ri(OP_DELAY, 0, 0),
            enc_ri(OP_SHIFT, 0, 0),
            enc_pin(OP_INB, 0, 1, 0),
            enc_loop(1, 1),
            HALT,
        ];
        // INB of iteration i (0-based) starts at 3 + 12*i + 6, exec at +2.
        let byte = 0xC6u8;
        let mut uio = vec![0u8; 200];
        for i in 0..8 {
            let t = 3 + 12 * i + 6 + 2;
            uio[t] = ((byte >> (7 - i)) & 1) << 1;
        }
        let r = run(&image_of(&words), &[], &uio, 10_000);
        assert_eq!(r.final_state.regs[0], byte);
    }

    // isa.md INB: set Rd[bit], other bits untouched
    #[test]
    fn inb_bit7_preserves_others() {
        let uio = vec![0x10u8; 10]; // pin 4 (HOST_GO) high
        let r = run(&image_of(&[enc_ri(OP_LDI, 2, 0x55), enc_pin(OP_INB, 2, 4, 1), HALT]), &[], &uio, 100);
        assert_eq!(r.final_state.regs[2], 0xD5);
    }

    // isa.md IN + INTERFACE first execute cycle = fetch + 2
    #[test]
    fn in_samples_execute_cycle() {
        let mut ui = vec![0u8; 10];
        ui[5] = 0x3C; // second instruction starts at 3, executes at 5
        ui[4] = 0xFF;
        ui[6] = 0xFF;
        let r = run(&image_of(&[0, enc_ri(OP_IN, 3, 0), HALT]), &ui, &[], 100);
        assert_eq!(r.final_state.regs[3], 0x3C);
    }

    // isa.md "Data memory": LOAD/STORE at 512+imm, LOADX at 512+Rs
    #[test]
    fn data_memory() {
        let mut img = image_of(&[
            enc_ri(OP_LDI, 0, 0x77),
            enc_ri(OP_STORE, 0, 511), // byte 1023
            enc_ri(OP_LOAD, 1, 3),    // byte 515 (preloaded)
            enc_ri(OP_LDI, 2, 3),
            enc_rr(OP_LOADX, 3, 2), // byte 512+3
            HALT,
        ]);
        img[515] = 0x42;
        let r = run(&img, &[], &[], 100);
        assert_eq!(r.data_mem.len(), 512);
        assert_eq!(r.data_mem[511], 0x77);
        assert_eq!(r.data_mem[3], 0x42);
        assert_eq!(r.final_state.regs[1], 0x42);
        assert_eq!(r.final_state.regs[3], 0x42);
    }

    // isa.md branch format: no guard against branching into data region (256-511);
    // fetch from PC 256 reads bytes 512/513, which STORE can write.
    #[test]
    fn branch_into_data_region_fetches_data() {
        let target = enc_ri(OP_LDI, 0, 0x33);
        let img = image_of(&[
            enc_ri(OP_LDI, 1, (target & 0xFF) as u16),
            enc_ri(OP_STORE, 1, 0),
            enc_ri(OP_LDI, 1, (target >> 8) as u16),
            enc_ri(OP_STORE, 1, 1),
            enc_ri(OP_LDI, 1, (HALT & 0xFF) as u16),
            enc_ri(OP_STORE, 1, 2),
            enc_ri(OP_LDI, 1, (HALT >> 8) as u16),
            enc_ri(OP_STORE, 1, 3),
            enc_branch(0, 256),
        ]);
        let r = run(&img, &[], &[], 1000);
        assert!(r.halted);
        assert_eq!(r.final_state.regs[0], 0x33);
        assert_eq!(r.commits[9].pc, 256);
    }

    // INTERFACE max_cycles: stop once total exceeds it without HALT
    #[test]
    fn max_cycles_budget() {
        let r = run(&image_of(&[enc_branch(0, 0)]), &[], &[], 30);
        assert!(r.timeout && !r.halted);
        assert_eq!(r.commits.len(), 10); // 10*3 = 30 not exceeding
        let r = run(&image_of(&[enc_ri(OP_DELAY, 3, 511), HALT]), &[], &[], 20_000);
        assert!(r.timeout && r.commits.is_empty());
    }

    // isa.md "The same MSB-first rule applies to sub-fields inside imm(9)":
    // SET/OUTB/INB pin_index = imm[8:6], value/bit_select = imm[5], imm[4:0] unused.
    // Raw literal words, not the test encoder, so the bit positions are pinned.
    #[test]
    fn imm_subfields_msb_first() {
        // SET mode=01 pin=7 value=1: imm = 0b111_1_00000, junk in imm[4:0] ignored.
        let set_pp_p7_1 = (5u16 << 11) | (1 << 9) | 0b1_1110_0000 | 0b1_1111;
        // SET mode=01 pin=0 value=0, but imm[4:0] = 0b00011 (would be pin1/val1 low-justified)
        let set_pp_p0_0 = (5u16 << 11) | (1 << 9) | 0b0_0000_0011;
        let r = go(&[set_pp_p7_1, set_pp_p0_0, HALT]);
        assert_eq!((r.commits[0].state.pin_mode[7], r.commits[0].state.pin_drv[7]), (MODE_PUSH_PULL, 1));
        assert_eq!((r.commits[1].state.pin_mode[0], r.commits[1].state.pin_drv[0]), (MODE_PUSH_PULL, 0));
        assert_eq!(r.commits[1].state.pin_mode[1], MODE_INPUT, "imm[4:0] unused");

        // OUTB R0, pin2, bit_select=1 (bit7): imm = 0b010_1_00000
        let outb = (12u16 << 11) | 0b0_1010_0000;
        let r = go(&[enc_ri(OP_LDI, 0, 0x80), (5u16 << 11) | (1 << 9) | 0b0_1000_0000, outb, HALT]);
        assert_eq!(r.commits[1].state.pin_drv[2], 0);
        assert_eq!(r.commits[2].state.pin_drv[2], 1);

        // INB R1, pin3, bit_select=0 (bit0): imm = 0b011_0_00000; WAIT pin3 shares imm[8:6]
        let inb = (13u16 << 11) | (1 << 9) | 0b0_1100_0000;
        let wait = (10u16 << 11) | 0b0_1110_0000; // pin3 level1 unbounded
        let r = run(&image_of(&[inb, wait, HALT]), &[], &[0x08; 20], 100);
        assert_eq!(r.final_state.regs[1], 1);
        assert_eq!(r.commits[1].cycles, 3);
        assert_eq!(r.commits[1].state.flag, 0);
    }
}
