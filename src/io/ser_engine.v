/*
 * Copyright (c) 2026 Pankaj Nair
 * SPDX-License-Identifier: Apache-2.0
 *
 * Serial engine (docs/serial_engine.md): the bit level of a serial
 * protocol -- bit timing with edge resync, NRZI, bit stuffing, a
 * configurable CRC, an SE0 end-of-packet -- moving whole bytes to and
 * from the CPU. Top-level sibling of core.v and pin_ctrl.v (top.v):
 * core.v drives the configuration/byte strobes from SCFG/STX/SRX/SRD,
 * pin_ctrl.v hands over the synchronized levels of protocol pins 0/1
 * and lets this module drive them while it transmits.
 *
 * One state machine, never receiving and transmitting at once, so the
 * bit timer, the shift register, the run counter, the byte index and
 * the CRC are shared between the two directions.
 *
 * Stuffing is on line levels (see the doc): after `stuff_k` bit times
 * at one level, the next bit time is a level change carrying no data.
 */

`default_nettype none

module ser_engine (
    input  wire        clk,
    input  wire        rst_n,

    // pin_ctrl.v: synchronized levels of protocol pins 1:0
    input  wire [1:0]  pin_sync,
    // pin_ctrl.v: drive request for pins 1:0 (oe=1 overrides the pin)
    output wire [1:0]  drv_oe,
    output wire [1:0]  drv_out,

    // core.v
    input  wire        cfg_we,      // SCFG commit
    input  wire [3:0]  cfg_addr,    // SCFG/SRD k
    input  wire [7:0]  cfg_wdata,
    input  wire        tx_we,       // STX commit (only while !tx_full)
    input  wire [7:0]  tx_data,
    input  wire        rx_take,     // SRX commit: consume a byte, else a pending end
    output wire        tx_full,
    output wire        rx_avail,
    output wire        rx_end,      // end of packet signalled, not yet consumed
    output wire [7:0]  rx_byte,
    output wire [7:0]  rd_data      // SRD k = cfg_addr
);

  // -----------------------------------------------------------------
  // Configuration (SCFG k = 0-8; 15 = command).
  // -----------------------------------------------------------------
  reg [11:0] period;
  reg [11:0] sample;
  reg [7:0]  mode;
  reg [3:0]  stuff_k;
  reg [3:0]  crc_w1;      // CRC width - 1
  reg [15:0] poly;
  reg [3:0]  crc_start;
  reg        crc_ones;

  wire nrzi     = mode[0];
  wire diff     = mode[1];
  wire se0_mode = mode[2];
  wire idle_lvl = mode[3];
  wire rx_sel   = mode[4];
  wire crc_tx   = mode[5];
  wire crc_inv  = mode[6];
  wire rx_en    = mode[7];

  wire cmd_clear   = cfg_we && cfg_addr == 4'd15 && cfg_wdata[0];
  wire cmd_abandon = cfg_we && cfg_addr == 4'd15 && cfg_wdata[1];

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      period    <= 12'd0;
      sample    <= 12'd0;
      mode      <= 8'd0;
      stuff_k   <= 4'd0;
      crc_w1    <= 4'd0;
      poly      <= 16'd0;
      crc_start <= 4'd0;
      crc_ones  <= 1'b0;
    end else if (cfg_we) begin
      case (cfg_addr)
        4'd0: period[7:0]   <= cfg_wdata;
        4'd1: period[11:8]  <= cfg_wdata[3:0];
        4'd2: sample[7:0]   <= cfg_wdata;
        4'd3: sample[11:8]  <= cfg_wdata[3:0];
        4'd4: mode          <= cfg_wdata;
        4'd5: {crc_w1, stuff_k} <= cfg_wdata;
        4'd6: poly[7:0]     <= cfg_wdata;
        4'd7: poly[15:8]    <= cfg_wdata;
        4'd8: {crc_ones, crc_start} <= cfg_wdata[4:0];
        default: ;
      endcase
    end
  end

  // -----------------------------------------------------------------
  // Line.
  // -----------------------------------------------------------------
  wire rx_lvl  = rx_sel ? pin_sync[1] : pin_sync[0];
  wire se0_now = !pin_sync[0] && !pin_sync[1];
  wire at_se0  = se0_mode && se0_now;
  reg  rx_last;            // rx_lvl one cycle ago, for edge detection
  wire rx_edge = rx_lvl != rx_last;

  // -----------------------------------------------------------------
  // CRC: bit-serial, transmission order, width crc_w1+1.
  // -----------------------------------------------------------------
  reg  [15:0] crc;
  wire [15:0] crc_mask = 16'hFFFF >> (4'd15 - crc_w1);
  wire        crc_msb  = crc[crc_w1];
  wire [15:0] crc_init = crc_ones ? crc_mask : 16'd0;

  function [15:0] crc_step;
    input [15:0] c;
    input        b;
    begin
      crc_step = ({c[14:0], 1'b0} ^ ((b ^ c[crc_w1]) ? poly : 16'd0)) & crc_mask;
    end
  endfunction

  // -----------------------------------------------------------------
  // State.
  // -----------------------------------------------------------------
  localparam E_IDLE = 3'd0;
  localparam E_RX   = 3'd1;
  localparam E_TX   = 3'd2;
  localparam E_EOP  = 3'd3;
  localparam E_WAIT = 3'd4;   // wait for an idle line, then maybe signal the end

  reg [2:0]  st;
  reg [11:0] tmr;           // counts down to the next sample / bit boundary
  reg [7:0]  sh;            // shift register (receive or transmit)
  reg [3:0]  cnt;           // receive: bits in sh; transmit: bits left in sh
  reg [3:0]  run;           // bit times at the current level, saturating
  reg        cur;           // current line level (last sampled / last sent)
  reg [3:0]  byte_idx;      // byte of the packet, saturating at 15
  reg [4:0]  crc_left;      // transmit: CRC bits still to append
  reg [1:0]  eop_cnt;
  reg        wait_signal;   // E_WAIT ends in a signalled end of packet

  reg [7:0]  tx_hold;
  reg        tx_full_r;
  reg        own;           // driving the pins
  reg [1:0]  out;

  reg [7:0]  rx_byte_r;
  reg        rx_avail_r;
  reg        rx_end_r;
  reg        eop_seen, stuff_err, overrun;

  wire stuff_due  = (stuff_k != 4'd0) && (run == stuff_k);
  wire in_crc     = byte_idx >= crc_start;
  wire [3:0] run_inc = (run == 4'd15) ? run : run + 4'd1;
  wire [3:0] idx_inc = (byte_idx == 4'd15) ? byte_idx : byte_idx + 4'd1;

  // transmit: the bit this slot sends (data or appended CRC)
  wire tx_data_bit = (cnt != 4'd0) ? sh[0] : (crc_msb ^ crc_inv);
  wire tx_lvl      = nrzi ? (tx_data_bit ? cur : !cur) : tx_data_bit;

  // receive: the data bit this sample carries
  wire rx_data_bit = nrzi ? (rx_lvl == cur) : rx_lvl;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      st          <= E_IDLE;
      tmr         <= 12'd0;
      sh          <= 8'd0;
      cnt         <= 4'd0;
      run         <= 4'd0;
      cur         <= 1'b0;
      byte_idx    <= 4'd0;
      crc_left    <= 5'd0;
      eop_cnt     <= 2'd0;
      wait_signal <= 1'b0;
      crc         <= 16'd0;
      tx_hold     <= 8'd0;
      tx_full_r   <= 1'b0;
      own         <= 1'b0;
      out         <= 2'b00;
      rx_byte_r   <= 8'd0;
      rx_avail_r  <= 1'b0;
      rx_end_r    <= 1'b0;
      eop_seen    <= 1'b0;
      stuff_err   <= 1'b0;
      overrun     <= 1'b0;
      rx_last     <= 1'b0;
    end else begin
      rx_last <= rx_lvl;

      if (tx_we) begin
        tx_hold   <= tx_data;
        tx_full_r <= 1'b1;
      end

      if (rx_take) begin
        if (rx_avail_r)    rx_avail_r <= 1'b0;
        else if (rx_end_r) rx_end_r   <= 1'b0;
      end

      if (cmd_clear) begin
        eop_seen  <= 1'b0;
        stuff_err <= 1'b0;
        overrun   <= 1'b0;
        rx_end_r  <= 1'b0;
      end

      case (st)

        E_IDLE: begin
          if (tx_full_r) begin
            // start a packet: the first bit goes out on the next cycle
            sh        <= tx_hold;
            tx_full_r <= 1'b0;
            cnt       <= 4'd8;
            cur       <= idle_lvl;
            run       <= 4'd0;
            byte_idx  <= 4'd0;
            crc       <= crc_init;
            crc_left  <= crc_tx ? {1'b0, crc_w1} + 5'd1 : 5'd0;
            tmr       <= 12'd0;
            st        <= E_TX;
          end else if (rx_en && rx_lvl != idle_lvl && !at_se0) begin
            // start of packet: this cycle is the first edge
            cur       <= idle_lvl;
            run       <= 4'd0;
            cnt       <= 4'd0;
            byte_idx  <= 4'd0;
            crc       <= crc_init;
            eop_seen  <= 1'b0;
            stuff_err <= 1'b0;
            overrun   <= 1'b0;
            tmr       <= sample - 12'd1;
            st        <= E_RX;
          end
        end

        E_RX: begin
          if (cmd_abandon) begin
            st          <= E_WAIT;
            wait_signal <= 1'b0;
            rx_avail_r  <= 1'b0;
          end else if (rx_edge) begin
            tmr <= sample - 12'd1;                   // resync
          end else if (tmr != 12'd0) begin
            tmr <= tmr - 12'd1;
          end else begin
            tmr <= period - 12'd1;
            if (at_se0) begin
              eop_seen    <= 1'b1;
              st          <= E_WAIT;
              wait_signal <= 1'b1;
            end else if (stuff_due) begin
              if (rx_lvl == cur) begin
                stuff_err   <= 1'b1;
                st          <= E_WAIT;
                wait_signal <= 1'b1;
              end else begin
                cur <= rx_lvl;
                run <= 4'd1;
              end
            end else begin
              cur <= rx_lvl;
              run <= (rx_lvl == cur) ? run_inc : 4'd1;
              if (in_crc) crc <= crc_step(crc, rx_data_bit);
              if (cnt == 4'd7) begin
                rx_byte_r  <= {rx_data_bit, sh[7:1]};
                rx_avail_r <= 1'b1;
                if (rx_avail_r && !rx_take) overrun <= 1'b1;
                cnt        <= 4'd0;
                byte_idx   <= idx_inc;
              end else begin
                cnt <= cnt + 4'd1;
              end
              sh <= {rx_data_bit, sh[7:1]};
            end
          end
        end

        E_TX: begin
          if (tmr != 12'd0) begin
            tmr <= tmr - 12'd1;
          end else begin
            tmr <= period - 12'd1;
            if (stuff_due) begin
              own <= 1'b1;
              out <= {cur, !cur};
              cur <= !cur;
              run <= 4'd1;
            end else if (cnt != 4'd0 || crc_left != 5'd0) begin
              own <= 1'b1;
              out <= {!tx_lvl, tx_lvl};
              cur <= tx_lvl;
              run <= (tx_lvl == cur) ? run_inc : 4'd1;
              if (cnt != 4'd0) begin
                if (in_crc) crc <= crc_step(crc, tx_data_bit);
                if (cnt == 4'd1 && tx_full_r) begin
                  sh        <= tx_hold;
                  tx_full_r <= 1'b0;
                  cnt       <= 4'd8;
                  byte_idx  <= idx_inc;
                end else begin
                  sh  <= {1'b0, sh[7:1]};
                  cnt <= cnt - 4'd1;
                end
              end else begin
                // appending: shift the CRC out, MSB first
                crc      <= {crc[14:0], 1'b0} & crc_mask;
                crc_left <= crc_left - 5'd1;
              end
            end else if (se0_mode) begin
              own     <= 1'b1;
              out     <= 2'b00;                      // SE0
              eop_cnt <= 2'd0;
              st      <= E_EOP;
            end else begin
              own         <= 1'b0;
              st          <= E_WAIT;
              wait_signal <= 1'b0;
            end
          end
        end

        E_EOP: begin
          if (tmr != 12'd0) begin
            tmr <= tmr - 12'd1;
          end else begin
            tmr <= period - 12'd1;
            if (eop_cnt == 2'd0) begin
              eop_cnt <= 2'd1;                       // second SE0 bit time
            end else if (eop_cnt == 2'd1) begin
              out     <= {!idle_lvl, idle_lvl};      // one bit time of idle
              eop_cnt <= 2'd2;
            end else begin
              own         <= 1'b0;
              st          <= E_WAIT;
              wait_signal <= 1'b0;
            end
          end
        end

        E_WAIT: begin
          if (!at_se0 && rx_lvl == idle_lvl) begin
            st <= E_IDLE;
            if (wait_signal) rx_end_r <= 1'b1;
          end
        end

        default: st <= E_IDLE;
      endcase
    end
  end

  assign drv_oe   = {own && diff, own};
  assign drv_out  = out;
  assign tx_full  = tx_full_r;
  assign rx_avail = rx_avail_r;
  assign rx_end   = rx_end_r;
  assign rx_byte  = rx_byte_r;

  wire tx_busy = tx_full_r || st == E_TX || st == E_EOP;
  wire [7:0] status = {rx_lvl, se0_now, tx_busy, overrun, stuff_err, eop_seen,
                       st == E_RX, rx_avail_r};

  assign rd_data = (cfg_addr == 4'd0) ? status :
                   (cfg_addr == 4'd1) ? crc[7:0] :
                   (cfg_addr == 4'd2) ? crc[15:8] : 8'd0;

endmodule
