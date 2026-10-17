//! RustDV pilot, phase A: one directed test on test/tb.v, written without
//! reusing anything from the cocotb harness. It boots a program through the
//! real LOAD-mode handshake (docs/architecture.md Pipeline, LOAD section),
//! pulses START and waits for the program's marker on uo_out.
//!
//! Timing convention (this file's own, from the spec): inputs change on
//! the falling clock edge, outputs are sampled on the falling edge, so
//! nothing ever races a rising edge.

use rustdv::prelude::*;

rustdv::vpi_bootstrap!();

const HOST_GO: u64 = 1 << 4;
const HOST_STATUS: u64 = 1 << 5;
const START: u64 = 1 << 6;

struct Pins {
    clk: LogicHandle,
    rst_n: LogicHandle,
    ena: LogicHandle,
    ui_in: LogicHandle,
    uio_in: LogicHandle,
    uo_out: LogicHandle,
    uio_out: LogicHandle,
}

impl Pins {
    fn new(dut: &HierarchyHandle) -> Result<Pins, HandleError> {
        Ok(Pins {
            clk: dut.signal("clk")?,
            rst_n: dut.signal("rst_n")?,
            ena: dut.signal("ena")?,
            ui_in: dut.signal("ui_in")?,
            uio_in: dut.signal("uio_in")?,
            uo_out: dut.signal("uo_out")?,
            uio_out: dut.signal("uio_out")?,
        })
    }

    fn read(h: &LogicHandle) -> u64 {
        h.get_u64().unwrap_or(u64::MAX) // X/Z reads as an impossible value
    }

    async fn cycles(&self, n: u32) {
        for _ in 0..n {
            self.clk.falling_edge().await;
        }
    }

    /// Wait (bounded) until HOST_STATUS == level, sampled at falling edges.
    async fn wait_status(&self, level: bool, what: &str) -> Result<(), TestError> {
        for _ in 0..64 {
            self.clk.falling_edge().await;
            if (Self::read(&self.uio_out) & HOST_STATUS != 0) == level {
                return Ok(());
            }
        }
        Err(TestError::new(format!("HOST_STATUS never went {} ({what})", level as u8)))
    }
}

/// Little-endian word order of the boot stream (isa.md: low byte first).
fn to_bytes(words: &[u16]) -> Vec<u8> {
    words.iter().flat_map(|w| [(*w & 0xFF) as u8, (*w >> 8) as u8]).collect()
}

/// Encoders straight from docs/isa.md formats (opcode = word[15:11],
/// reg+imm: Rd = [10:9], imm = [8:0]).
fn ldi(rd: u16, v: u16) -> u16 { (4 << 11) | (rd << 9) | (v & 0xFF) }
fn out(rd: u16) -> u16 { (6 << 11) | (rd << 9) }
fn halt() -> u16 { 1 << 11 }

#[rustdv::test(timeout_time = 200, timeout_unit = "us")]
async fn boot_handshake_and_marker(ctx: RustdvCtx) -> Result<(), TestError> {
    let p = Pins::new(&ctx.dut()).map_err(|e| TestError::new(format!("{e:?}")))?;
    let _clk = Clock::new(&p.clk, SimDuration::ns(20)).start(); // 50 MHz

    p.ena.set_u64(1);
    p.ui_in.set_u64(0);
    p.uio_in.set_u64(0);
    p.rst_n.set_u64(0);
    p.cycles(5).await;
    p.rst_n.set_u64(1);
    p.cycles(5).await;

    let image = to_bytes(&[ldi(0, 0x5A), out(0), halt()]);
    for (k, b) in image.iter().enumerate() {
        p.ui_in.set_u64(*b as u64);
        p.uio_in.set_u64(HOST_GO);
        p.wait_status(true, "boot write ack").await?;
        // architecture.md LOAD: echo of the post-increment address is valid at the ack
        let echo = Pins::read(&p.uo_out);
        if echo != ((k + 1) & 0xFF) as u64 {
            return Err(TestError::new(format!("boot echo {echo:#x} at byte {k}")));
        }
        p.uio_in.set_u64(0);
        p.wait_status(false, "ack clear").await?;
    }

    p.uio_in.set_u64(START);
    p.cycles(4).await;
    p.uio_in.set_u64(0);

    for c in 0..200 {
        p.clk.falling_edge().await;
        if Pins::read(&p.uo_out) == 0x5A {
            ctx.info(&format!("marker 0x5A on uo_out {c} cycles after START release"));
            return Ok(());
        }
    }
    Err(TestError::new("marker 0x5A never appeared on uo_out"))
}
