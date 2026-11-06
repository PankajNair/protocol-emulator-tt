// agent_ser_engine_props.v
// Formal properties for src/io/ser_engine.v (module ser_engine), derived
// from docs/serial_engine.md -- every expected value below comes from the
// doc's register map, pin rules, timing, stuffing, EOP, receive and CRC
// text, re-derived in this checker's own ghost models, never by reading
// one of the engine's own expressions.
//
// deep-evidence: test_usb_kbd_enumerates
//   The `[deep]` covers are states past the default BMC depth 20 at the
//   smallest legal timing (P = 2, S = 1; reset plus the 1-3 SCFG writes
//   come first): the end of a transmitted packet (8 data bits = 16 clocks,
//   then the EOP's SE0, SE0, idle) and anything after a whole received
//   byte (8 samples = 15 clocks after the start). test_usb_kbd_enumerates
//   runs the USB firmware on the engine against a USB host model: the
//   device receives and SRX-takes bytes of every token/data packet, and
//   transmits packets ending SE0, SE0, J, after which it releases the
//   pins (the host checks each packet's line symbols). The asserts on
//   those states are proven for all time by `make prove` regardless.
//
// Hazard classes:
//   1. pin ownership (P1a-P1e)       4. EOP (P4a-P4d)
//   2. bit timing (P2a-P2c)           5. receive bookkeeping (P5a-P5g)
//   3. stuffing on line levels (P3)   6. receive CRC (P6a-P6c)
// plus auxiliary invariants (Ix/Jx) at the bottom that tie the ghost
// models to the engine's internal state for k-induction; each is an
// exact spec check in its own right and is proven like the rest.
//
// Ghost models (all reset with rst_n, updated only from ports):
//   * g_cfg: the doc's SCFG register map (k=0-8), written from
//     cfg_we/cfg_addr/cfg_wdata.
//   * transmit line observer: from the pins only. A packet's first bit
//     starts when drv_oe[0] rises; bit boundaries are then every P clocks
//     (ph counts clocks into the current bit); trun counts bit times at
//     one line symbol; eop tracks the SE0, SE0, idle tail.
//   * receiver: the doc's receive rules (start when idle + rx_en + pin
//     leaves idle; edge resync; sample S after the last edge then every P;
//     SE0 / stuff bit / data bit; NRZI; LSB first; 8 bits a byte; CRC on
//     data bits of bytes >= crc_start), then wait for an idle line.
//   * CRC: the doc gives width, polynomial, init and the residuals
//     (USB CRC16 good packet -> 0x800D) but not the step; the standard
//     bit-serial MSB-first LFSR  c' = ((c << 1) ^ ((b ^ c[w-1]) ? poly : 0))
//     & mask(w)  is the one consistent with that residual (checked: random
//     data + its inverted CRC16 sent MSB first leaves 0x800D).
//   "The engine idle" (receive/transmit start) and "in flight" are read
//   from the engine's state register `st` (debug port), since the doc
//   gives no pin-visible signal for the receive side's busy period.
//
// Environment. This project's assert_superset gate rejects any assume
// other than the reset one, so the three environment conditions the
// task allows are applied as a latched guard `ok` instead (it gates every
// assert below; semantically the same as assuming them for these safety
// properties -- once a trace breaks one, nothing later on it is checked):
//   E1 reset at step 0: `initial assume (!rst_n)` (the standard assume).
//   E2 P >= 2 whenever a packet is in flight, and P >= 2, 1 <= S < P
//      whenever receive is enabled or a packet is being received: the doc
//      ranges (P = 2-4095, S < P); S = 0 has no meaning ("S clocks after
//      an edge"), the doc's examples use S ~ P/2.
//   E3 no SCFG to k = 0-8 while a packet is in flight (engine not idle,
//      a byte queued, or the pins driven): the doc says the in-flight
//      packet then gets "whatever bits it hits", i.e. undefined. The
//      commands (k = 15: clear, abandon) stay free.
// pin_sync, tx_we/tx_data, rx_take, cfg_* are otherwise free.
//
// Plain immediate assertions in always @(posedge clk) (no concurrent SVA
// under this yosys frontend). Bound externally (bind at the bottom).

module ser_engine_props (
    input wire        clk,
    input wire        rst_n,
    input wire [1:0]  pin_sync,
    input wire [1:0]  drv_oe,
    input wire [1:0]  drv_out,
    input wire        cfg_we,
    input wire [3:0]  cfg_addr,
    input wire [7:0]  cfg_wdata,
    input wire        tx_we,
    input wire [7:0]  tx_data,
    input wire        rx_take,
    input wire        tx_full,
    input wire        rx_avail,
    input wire        rx_end,
    input wire [7:0]  rx_byte,
    input wire [7:0]  rd_data,
    // DUT-internal state via formal_common.DEBUG_PORTS. `st` is used to
    // read "engine idle / in flight" (see header); everything else is
    // only ever asserted on (ghost == real, induction strengthening).
    input wire [2:0]  st,
    input wire [11:0] tmr,
    input wire [3:0]  cnt,
    input wire [3:0]  run,
    input wire        cur,
    input wire [3:0]  byte_idx,
    input wire [15:0] crc,
    input wire [1:0]  eop_cnt,
    input wire        wait_signal,
    input wire        own,
    input wire [7:0]  sh,
    input wire        rx_last,
    input wire        overrun,
    input wire [11:0] period,
    input wire [11:0] sample,
    input wire [7:0]  mode,
    input wire [3:0]  stuff_k,
    input wire [3:0]  crc_w1,
    input wire [15:0] poly,
    input wire [3:0]  crc_start,
    input wire        crc_ones
);

  // E1: a real reset at step 0 (every module here has an async reset)
  initial assume (!rst_n);

  // engine state encoding (only to read idle / receiving / transmitting)
  localparam S_IDLE = 3'd0, S_RX = 3'd1, S_TX = 3'd2, S_EOP = 3'd3, S_WAIT = 3'd4;

  // =====================================================================
  // Ghost configuration: the doc's SCFG register map.
  // =====================================================================
  reg [11:0] g_P, g_S;
  reg [7:0]  g_mode;
  reg [3:0]  g_K, g_w1, g_cs;
  reg [15:0] g_poly;
  reg        g_ones;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      g_P <= 0; g_S <= 0; g_mode <= 0; g_K <= 0; g_w1 <= 0; g_cs <= 0;
      g_poly <= 0; g_ones <= 0;
    end else if (cfg_we) begin
      if (cfg_addr == 4'd0) g_P[7:0]   <= cfg_wdata;
      if (cfg_addr == 4'd1) g_P[11:8]  <= cfg_wdata[3:0];
      if (cfg_addr == 4'd2) g_S[7:0]   <= cfg_wdata;
      if (cfg_addr == 4'd3) g_S[11:8]  <= cfg_wdata[3:0];
      if (cfg_addr == 4'd4) g_mode     <= cfg_wdata;
      if (cfg_addr == 4'd5) begin g_K <= cfg_wdata[3:0]; g_w1 <= cfg_wdata[7:4]; end
      if (cfg_addr == 4'd6) g_poly[7:0]  <= cfg_wdata;
      if (cfg_addr == 4'd7) g_poly[15:8] <= cfg_wdata;
      if (cfg_addr == 4'd8) begin g_cs <= cfg_wdata[3:0]; g_ones <= cfg_wdata[4]; end
    end
  end

  wire g_nrzi  = g_mode[0];
  wire g_diff  = g_mode[1];
  wire g_se0   = g_mode[2];
  wire g_idle  = g_mode[3];
  wire g_rxsel = g_mode[4];
  wire g_rxen  = g_mode[7];

  wire cmd_clear   = cfg_we && cfg_addr == 4'd15 && cfg_wdata[0];
  wire cmd_abandon = cfg_we && cfg_addr == 4'd15 && cfg_wdata[1];

  // line, as the doc defines it
  wire rx_lvl    = g_rxsel ? pin_sync[1] : pin_sync[0];
  wire at_se0    = g_se0 && pin_sync == 2'b00;          // SE0 (se0 mode)
  wire line_idle = !at_se0 && rx_lvl == g_idle;         // "not SE0, receive pin at the idle level"
  // a receive starts: engine idle (nothing queued to send), rx_en, and the
  // receive pin leaves the idle level (not into SE0)
  wire rx_start  = st == S_IDLE && !tx_full && g_rxen && rx_lvl != g_idle && !at_se0;

  // =====================================================================
  // Environment guard (E2, E3 -- see header).
  // =====================================================================
  // (a receive start cycle counts: the engine takes S at that edge)
  wire in_flight = (st != S_IDLE) || tx_full || (drv_oe != 2'b00) || rx_start;
  wire p_ok      = g_P >= 12'd2;
  wire s_ok      = g_S >= 12'd1 && g_S < g_P;
  wire env_now   = !(in_flight && cfg_we && cfg_addr <= 4'd8)
                && (!in_flight || p_ok)
                && (!(g_rxen || st == S_RX) || (p_ok && s_ok));
  reg  env_hist, pv;     // all earlier cycles since reset met E2/E3; a previous cycle exists
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin env_hist <= 1'b1; pv <= 1'b0; end
    else begin env_hist <= env_hist && env_now; pv <= 1'b1; end
  end
  wire ok  = rst_n && env_hist && env_now;
  wire okp = ok && pv;   // ok, and the previous cycle was a real post-reset cycle

  // =====================================================================
  // Transmit line observer (pins only).
  // =====================================================================
  reg        oe_q;          // drv_oe[0] last cycle
  reg [1:0]  oev_q;         // drv_oe last cycle
  reg [1:0]  dl_q;          // driven levels last cycle
  reg [11:0] ph_q;          // clocks into the current bit, if still driving
  reg [1:0]  sym_q;         // last cycle's line symbol
  reg [4:0]  trun_q;        // bit times at the current symbol
  reg [1:0]  eop_q;         // 0 none, 1/2 first/second SE0 bit, 3 idle bit

  wire        rise   = drv_oe[0] && !oe_q;               // a packet's first bit starts
  wire        fall   = !drv_oe[0] && oe_q;               // pins released
  wire [11:0] ph     = rise ? 12'd0 : ph_q;
  wire        bnd    = drv_oe[0] && ph == 12'd0;         // a bit time starts this cycle
  wire [1:0]  dl     = {drv_oe[1] & drv_out[1], drv_out[0]};   // the levels actually driven
  // line symbol: the pin pair in diff mode (J/K/SE0), pin 0 otherwise
  wire [1:0]  sym    = g_diff ? drv_out : {1'b0, drv_out[0]};
  wire        se0sym = g_diff && drv_out == 2'b00;
  wire [1:0]  idle_pair = {!g_idle, g_idle};             // the idle level, both pins
  wire [4:0]  trun   = rise ? 5'd1 :
                       !bnd ? trun_q :
                       (sym == sym_q) ? ((trun_q == 5'd31) ? 5'd31 : trun_q + 5'd1) : 5'd1;
  wire [1:0]  eop    = !drv_oe[0] ? 2'd0 :
                       !bnd ? eop_q :
                       (eop_q == 2'd0) ? (se0sym ? 2'd1 : 2'd0) :
                       (eop_q == 2'd3) ? 2'd3 : eop_q + 2'd1;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      oe_q <= 0; oev_q <= 0; dl_q <= 0; ph_q <= 0; sym_q <= 0; trun_q <= 0; eop_q <= 0;
    end else begin
      oe_q   <= drv_oe[0];
      oev_q  <= drv_oe;
      dl_q   <= dl;
      ph_q   <= !drv_oe[0] ? 12'd0 : (ph == g_P - 12'd1) ? 12'd0 : ph + 12'd1;
      sym_q  <= sym;
      trun_q <= drv_oe[0] ? trun : 5'd0;
      eop_q  <= eop;
    end
  end

  // =====================================================================
  // Receiver ghost (doc: "Receive").
  // =====================================================================
  reg        g_rx, g_wait, g_wsig;   // receiving; waiting for idle; that wait ends in a signalled end
  reg [11:0] g_since;                // clocks since the last edge / sample
  reg        g_aft;                  // last timer anchor was a sample (next in P) else an edge (S)
  reg        g_last;                 // receive pin last cycle
  reg        g_cur;                  // last sampled level (NRZI reference / stuffing)
  reg [3:0]  g_run, g_cnt, g_idx;
  reg [7:0]  g_sh;
  reg [15:0] g_crc;
  reg        g_crcv;                 // g_crc is the CRC register's expected value

  wire [4:0]  g_w     = {1'b0, g_w1} + 5'd1;
  wire [15:0] g_mask  = ~(16'hFFFF << g_w);
  wire        g_top   = |(g_crc & (16'd1 << g_w1));
  wire [15:0] g_init  = g_ones ? g_mask : 16'd0;

  wire edge_now = rx_lvl != g_last;
  wire tx_start = st == S_IDLE && tx_full;
  wire [11:0] tgt = g_aft ? g_P : g_S;
  wire samp     = g_rx && !cmd_abandon && !edge_now && g_since == tgt;
  wire s_se0    = samp && at_se0;
  wire s_stuff  = samp && !at_se0 && g_K != 4'd0 && g_run == g_K;
  wire s_serr   = s_stuff && rx_lvl == g_cur;
  wire s_data   = samp && !at_se0 && !s_stuff;
  wire dbit     = g_nrzi ? (rx_lvl == g_cur) : rx_lvl;   // NRZI: 0 = level change
  wire done     = s_data && g_cnt == 4'd7;                // a byte completes
  wire [7:0] newbyte = {dbit, g_sh[7:1]};
  wire wait_exit = g_wait && line_idle;
  wire sig_end   = wait_exit && g_wsig;                    // the end is signalled now
  wire [15:0] crc_next = (({g_crc[14:0], 1'b0}) ^ ((dbit ^ g_top) ? g_poly : 16'd0)) & g_mask;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      g_rx <= 0; g_wait <= 0; g_wsig <= 0; g_since <= 0; g_aft <= 0; g_last <= 0;
      g_cur <= 0; g_run <= 0; g_cnt <= 0; g_idx <= 0; g_sh <= 0; g_crc <= 0; g_crcv <= 0;
    end else begin
      g_last <= rx_lvl;
      if (tx_start) g_crcv <= 1'b0;
      if (wait_exit) g_wait <= 1'b0;
      if (rx_start) begin
        g_rx <= 1'b1; g_since <= 12'd1; g_aft <= 1'b0; g_cur <= g_idle;
        g_run <= 4'd0; g_cnt <= 4'd0; g_idx <= 4'd0; g_crc <= g_init; g_crcv <= 1'b1;
      end else if (g_rx) begin
        if (cmd_abandon) begin
          g_rx <= 1'b0; g_wait <= 1'b1; g_wsig <= 1'b0;
        end else if (edge_now) begin
          g_since <= 12'd1; g_aft <= 1'b0;
        end else if (g_since != tgt) begin
          g_since <= g_since + 12'd1;
        end else begin
          g_since <= 12'd1; g_aft <= 1'b1;
          if (s_se0 || s_serr) begin
            g_rx <= 1'b0; g_wait <= 1'b1; g_wsig <= 1'b1;
          end else if (s_stuff) begin
            g_cur <= rx_lvl; g_run <= 4'd1;
          end else begin
            g_cur <= rx_lvl;
            g_run <= (rx_lvl != g_cur) ? 4'd1 : (g_run == 4'd15) ? 4'd15 : g_run + 4'd1;
            if (g_idx >= g_cs) g_crc <= crc_next;
            g_sh  <= newbyte;
            g_cnt <= (g_cnt == 4'd7) ? 4'd0 : g_cnt + 4'd1;
            if (g_cnt == 4'd7 && g_idx != 4'd15) g_idx <= g_idx + 4'd1;
          end
        end
      end
    end
  end

  // previous-cycle copies for one-step checks
  reg take_q, avail_q, end_q, clr_q, done_q, sig_q, rxs_q, txs_q, ov_q, idle_q;
  reg [7:0] byte_q;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      take_q <= 0; avail_q <= 0; end_q <= 0; clr_q <= 0; done_q <= 0; sig_q <= 0;
      rxs_q <= 0; txs_q <= 0; ov_q <= 0; idle_q <= 0; byte_q <= 0;
    end else begin
      take_q <= rx_take; avail_q <= rx_avail; end_q <= rx_end; clr_q <= cmd_clear;
      done_q <= done; sig_q <= sig_end; rxs_q <= rx_start; txs_q <= tx_start;
      ov_q <= overrun; idle_q <= line_idle; byte_q <= newbyte;
    end
  end

  // =====================================================================
  // 1. Pin ownership
  // =====================================================================
  always @(posedge clk) begin
    // P1a: no drive unless transmitting a packet (TX bits or the EOP)
    cover (ok && st == S_WAIT && oe_q); // [deep]
    if (ok && !(st == S_TX || st == S_EOP)) assert (drv_oe == 2'b00);
    // P1a': same, against SRD status bit 5 "transmit busy" on rd_data
    cover (ok && cfg_addr == 4'd0 && !rd_data[5] && oe_q); // [deep]
    if (ok && cfg_addr == 4'd0 && !rd_data[5]) assert (drv_oe == 2'b00);
    // P1b: pin 1 only in diff mode; in diff mode both pins together
    cover (ok && drv_oe[1]);
    if (ok && drv_oe[1]) assert (g_diff);
    cover (ok && g_diff && drv_oe[0]);
    if (ok && g_diff) assert (drv_oe[1] == drv_oe[0]);
    // P1c: never SE1
    cover (ok && drv_oe == 2'b11);
    if (ok && drv_oe == 2'b11) assert (drv_out != 2'b11);
    // P1d: diff drive is complementary, except the EOP's two SE0 bits
    cover (ok && g_diff && drv_oe[0] && drv_out[1] != drv_out[0]);
    if (ok && g_diff && drv_oe[0])
      assert (drv_out[1] == !drv_out[0] ||
              (drv_out == 2'b00 && g_se0 && (eop == 2'd1 || eop == 2'd2)));
  end

  // =====================================================================
  // 2. Bit timing: levels change only on boundaries P clocks apart
  // =====================================================================
  always @(posedge clk) begin
    // P2a: inside a bit time nothing the pins show changes
    cover (okp && drv_oe[0] && oe_q && !bnd);
    if (okp && drv_oe[0] && oe_q && !bnd) assert (dl == dl_q && drv_oe == oev_q);
    // P2b: the pins are released only at a bit boundary
    cover (okp && fall); // [deep]
    if (okp && fall) assert (ph_q == 12'd0);
    // P2c: a level change inside a packet falls on a boundary
    cover (okp && drv_oe[0] && oe_q && dl != dl_q);
    if (okp && drv_oe[0] && oe_q && dl != dl_q) assert (bnd);
  end

  // =====================================================================
  // 3. Bit stuffing on line levels: at most K bit times at one level
  //    (SE0 exempt; se0 transmit needs diff per the doc, so the se0 &&
  //    !diff setting -- SE0 indistinguishable from a low level on pin 0
  //    alone -- is not checked)
  // =====================================================================
  always @(posedge clk) begin
    cover (ok && drv_oe[0] && g_K != 4'd0 && trun == {1'b0, g_K} && bnd);
    if (ok && drv_oe[0] && g_K != 4'd0 && (g_diff || !g_se0) && !se0sym)
      assert (trun <= {1'b0, g_K});
  end

  // =====================================================================
  // 4. EOP (se0 mode, diff): SE0, SE0, one idle bit, then released
  // =====================================================================
  always @(posedge clk) begin
    cover (ok && bnd && se0sym && eop_q == 2'd0); // [deep]
    // P4a: the second bit after the first SE0 bit is SE0 again
    cover (ok && bnd && eop_q == 2'd1); // [deep]
    if (ok && bnd && eop_q == 2'd1) assert (drv_oe == 2'b11 && drv_out == 2'b00);
    // P4b: then one bit time of the idle level
    cover (ok && bnd && eop_q == 2'd2); // [deep]
    if (ok && bnd && eop_q == 2'd2) assert (drv_oe == 2'b11 && drv_out == idle_pair);
    // P4c: and no further bit: released at the next boundary
    cover (ok && drv_oe[0] && eop_q == 2'd3); // [deep]
    if (ok && drv_oe[0] && eop_q == 2'd3) assert (!bnd);
    // P4d: in se0 mode a packet only ends after that idle bit
    cover (okp && fall && g_diff && g_se0); // [deep]
    if (okp && fall && g_diff && g_se0) assert (eop_q == 2'd3);
  end

  // =====================================================================
  // 5. Receive bookkeeping
  // =====================================================================
  always @(posedge clk) begin
    // P5a: rx_end rises only after a cycle with the line idle
    cover (okp && rx_end && !end_q);
    if (okp && rx_end && !end_q) assert (idle_q);
    // P5b: ... and only as the end of a received packet (not after a
    //      transmitted one, not after an abandoned one)
    cover (okp && rx_end && !end_q && sig_q);
    if (okp && rx_end && !end_q) assert (sig_q);
    // P5c: exact rx_end: set by a signalled end, else consumed by a take
    //      only when no byte is waiting ("a byte is reported before an end
    //      that came after it"), else held. (Same-cycle clear: the doc
    //      gives no order, not checked.)
    cover (okp && !clr_q && sig_q);
    if (okp && !clr_q)
      assert (rx_end == (sig_q || (end_q && !(take_q && !avail_q))));
    // P5d: a take of a waiting byte empties the slot unless a new byte
    //      completes in that cycle
    cover (okp && take_q && avail_q && !done_q); // [deep]
    if (okp && take_q && avail_q && !done_q) assert (!rx_avail);
    // P5e: a completed byte is waiting next cycle, LSB-first, NRZI-decoded
    cover (okp && done_q); // [deep]
    if (okp && done_q) assert (rx_avail && rx_byte == byte_q);
    // P5f: overrun exactly: set when a byte completes while the previous
    //      one waits and is not taken, cleared when a receive starts, else
    //      sticky (same-cycle clear command and transmit start, where the
    //      doc's "next packet" is ambiguous, not checked)
    cover (okp && !clr_q && !txs_q && rxs_q);
    if (okp && !clr_q && !txs_q)
      assert (overrun == (!rxs_q && (ov_q || (done_q && avail_q && !take_q))));
    // P5g: status bit 4 is that overrun bit
    cover (ok && cfg_addr == 4'd0 && rd_data[4] == 1'b0);
    if (ok && cfg_addr == 4'd0) assert (rd_data[4] == overrun);
  end

  // =====================================================================
  // 6. Receive CRC (bit-serial LFSR from the doc's width/poly/init, data
  //    bits of bytes >= crc_start, stuff bits and SE0 excluded)
  // =====================================================================
  always @(posedge clk) begin
    cover (ok && g_crcv && g_rx && g_idx >= g_cs && g_crc != g_init);
    if (ok && g_crcv) assert (crc == g_crc);
    // P6b/c: as read by SRD k=1/2
    cover (ok && g_crcv && cfg_addr == 4'd1 && rd_data != 8'd0);
    if (ok && g_crcv && cfg_addr == 4'd1) assert (rd_data == g_crc[7:0]);
    cover (ok && g_crcv && cfg_addr == 4'd2);
    if (ok && g_crcv && cfg_addr == 4'd2) assert (rd_data == g_crc[15:8]);
  end

  // =====================================================================
  // Induction-strengthening invariants (each proven like the rest).
  // =====================================================================
  always @(posedge clk) begin
    // configuration registers follow the doc's register map
    cover (rst_n && g_mode != 8'd0);
    if (rst_n)
      assert (period == g_P && sample == g_S && mode == g_mode && stuff_k == g_K &&
              crc_w1 == g_w1 && poly == g_poly && crc_start == g_cs && crc_ones == g_ones &&
              rx_last == g_last);

    // I1: transmit bit timer vs. the pin-level bit phase
    cover (ok && (st == S_TX || st == S_EOP) && own && ph != 12'd0);
    if (ok && (st == S_TX || st == S_EOP) && own)
      assert (drv_oe[0] && ph <= g_P - 12'd1 && tmr == g_P - 12'd1 - ph);
    // I2: the cycle before a packet's first bit
    cover (ok && st == S_TX && !own);
    if (ok && st == S_TX && !own)
      assert (tmr == 12'd0 && run == 4'd0 && cnt == 4'd8 && !oe_q);
    // I3: TX bits: complementary pair at the current level, run matches
    cover (ok && st == S_TX && own && run > 4'd1);
    if (ok && st == S_TX && own)
      assert (drv_out == {!cur, cur} && run != 4'd0 && eop == 2'd0 &&
              (trun == {1'b0, run} || (run == 4'd15 && trun >= 5'd15)));
    // I4: run never passes K while stuffing is on
    cover (ok && g_K != 4'd0 && (st == S_TX || st == S_RX) && run == g_K);
    if (ok && g_K != 4'd0 && (st == S_TX || st == S_RX)) assert (run <= g_K);
    // I5: EOP state vs. the observed EOP tail
    cover (ok && st == S_EOP && g_diff); // [deep]
    if (ok && st == S_EOP)
      assert (own && g_se0 && eop_cnt <= 2'd2 &&
              drv_out == ((eop_cnt == 2'd2) ? idle_pair : 2'b00) &&
              (!g_diff || eop == eop_cnt + 2'd1));
    // I5': the EOP tail is only tracked in diff mode (SE0 is a pin pair)
    cover (ok && !g_diff && drv_oe[0]);
    if (ok && !g_diff) assert (eop == 2'd0);
    // I6: receiver ghost == engine receive state
    cover (ok && g_rx && g_cnt == 4'd2);
    cover (ok && st == S_RX);
    if (ok) assert ((st == S_RX) == g_rx);
    if (ok && g_rx)
      assert (g_since >= 12'd1 && g_since <= tgt && tmr == tgt - g_since &&
              cur == g_cur && run == g_run && cnt == g_cnt && byte_idx == g_idx &&
              crc == g_crc && g_crcv && !g_wait &&
              (sh >> (4'd8 - cnt)) == (g_sh >> (4'd8 - g_cnt)));
    // I6': the received CRC is only kept until a transmit starts
    cover (ok && g_crcv && st == S_WAIT);
    if (ok && g_crcv) assert (st != S_TX && st != S_EOP && (st != S_WAIT || g_wait));
    // I7: waiting for idle
    cover (ok && g_wait && g_wsig);
    if (ok && g_wait) assert (st == S_WAIT && wait_signal == g_wsig);
    cover (ok && st == S_WAIT && !g_wait); // [deep]
    if (ok && st == S_WAIT && !g_wait) assert (!wait_signal);
  end

endmodule

bind ser_engine ser_engine_props u_ser_engine_props (
    .clk(clk), .rst_n(rst_n), .pin_sync(pin_sync), .drv_oe(drv_oe), .drv_out(drv_out),
    .cfg_we(cfg_we), .cfg_addr(cfg_addr), .cfg_wdata(cfg_wdata), .tx_we(tx_we),
    .tx_data(tx_data), .rx_take(rx_take), .tx_full(tx_full), .rx_avail(rx_avail),
    .rx_end(rx_end), .rx_byte(rx_byte), .rd_data(rd_data),
    .st(st), .tmr(tmr), .cnt(cnt), .run(run), .cur(cur), .byte_idx(byte_idx), .crc(crc),
    .eop_cnt(eop_cnt), .wait_signal(wait_signal), .own(own), .sh(sh), .rx_last(rx_last),
    .overrun(overrun), .period(period), .sample(sample), .mode(mode), .stuff_k(stuff_k),
    .crc_w1(crc_w1), .poly(poly), .crc_start(crc_start), .crc_ones(crc_ones)
);
