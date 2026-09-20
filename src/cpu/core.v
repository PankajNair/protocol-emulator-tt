/*
 * Copyright (c) 2026 Pankaj Nair
 * SPDX-License-Identifier: Apache-2.0
 *
 * Sequencer FSM: LOAD -> FETCH_LO -> FETCH_HI -> EXECUTE -> (back to
 * FETCH_LO). Decode is combinational, merged into EXECUTE (no separate
 * decode cycle -- docs/architecture.md Pipeline). Owns PC, the
 * instruction register, the shared flag (CMP/TEST-bit/WAIT write it,
 * BRANCH reads it), return-address register + return-valid bit, the
 * illegal-opcode and CALL/RET-misuse sticky debug flags, and the
 * boot-mode byte-address counter used while in LOAD.
 *
 * Instantiates regfile.v and cycle_counter.v as internal datapath
 * children (tightly coupled to EXECUTE, narrow interfaces, no reason
 * to expose at the top level). Talks to mem.v (addr/we/wdata/rdata)
 * and pin_ctrl.v (pin_index-addressed drive/read + synchronized
 * host_go/start signals) as top-level siblings via top.v -- this
 * module doesn't know about physical pin timing or the memory macro's
 * technology, just logical read/write/drive requests.
 *
 * Instruction word timing: FETCH_LO issues addr=PC*2, so mem_rdata
 * holds the low byte throughout FETCH_HI (captured into `ir_lo`).
 * FETCH_HI issues addr=PC*2+1, so mem_rdata holds the high byte on
 * EXECUTE's first cycle only -- decode reads `{mem_rdata, ir_lo}`
 * live that one cycle (`entering_execute`), then latches it into `ir`
 * for any further cycles the same EXECUTE occupies (DELAY/WAIT
 * holding) -- mem_addr moves on to the next instruction's LOAD/STORE/
 * LOADX address that same first cycle, so mem_rdata can't be trusted
 * as "this instruction's high byte" past it.
 *
 * uo_out is driven directly here, not routed through pin_ctrl.v -- a
 * TT output-only pin needs no sync or direction logic. HOST_STATUS
 * during LOAD reuses the same set_en/pin_index/set_value path SET
 * uses at runtime (pin_index=5, driven continuously off `load_ack`
 * rather than pulsed) -- no dedicated port needed, pin_ctrl.v just
 * sees the same request from two different sources depending on
 * `mode_load`. "Hi-Z during LOAD" for the protocol pins falls out for
 * free: sticky drive-mode resets to input and no opcode (SET
 * included) executes during LOAD to change that.
 *
 * Uses src/cpu/isa_defs.v for every opcode value, field position, and
 * cond/mode value.
 * Not included in source_files yet -- not wired into top.v.
 */

`default_nettype none

// Relies on src/cpu/isa_defs.v's `define`s already being visible --
// no `include` here (portability: some synth flows don't share
// test/Makefile's -I search path). info.yaml/test/Makefile both list
// isa_defs.v first in source order so its macros are defined before
// this file is read, within the same compile invocation.

module protocol_cpu_core (
    input  wire        clk,
    input  wire        rst_n,

    // mem.v
    output wire [9:0]  mem_addr,
    output wire        mem_we,
    output wire [7:0]  mem_wdata,
    input  wire [7:0]  mem_rdata,

    // pin_ctrl.v
    output wire [2:0]  pin_index,
    output wire        set_en,
    output wire [1:0]  set_mode,
    output wire        set_value,
    output wire        outb_en,
    output wire        outb_bit,
    input  wire        pin_read,
    input  wire [7:0]  ui_in_sync,
    input  wire        start_rise,
    input  wire        start_fall,   // unused here -- pin_ctrl.v's own driver-contention gate, not core.v's concern
    input  wire        host_go_rise,
    input  wire        host_go_fall,
    output wire        mode_load,

    // TT output-only pin, driven directly (docs/architecture.md).
    output wire [7:0]  uo_out
);

  // -----------------------------------------------------------------
  // FSM state.
  // -----------------------------------------------------------------
  localparam S_LOAD     = 2'd0;
  localparam S_FETCH_LO = 2'd1;
  localparam S_FETCH_HI = 2'd2;
  localparam S_EXECUTE  = 2'd3;

  reg [1:0] state;

  // -----------------------------------------------------------------
  // Core registers.
  // -----------------------------------------------------------------
  reg [8:0] pc;
  reg [7:0] ir_lo;
  reg [15:0] ir;
  reg        entering_execute;  // true for exactly the first cycle of an EXECUTE occupancy

  reg        flag;
  reg [8:0]  retaddr;
  reg        return_valid;
  reg        illegal_op_flag;
  reg        call_ret_misuse_flag;
  reg        halted;  // OP_HALT -- freezes the FSM, resumable only by
                       // external reset (docs/isa.md HALT row)

  reg [9:0]  boot_addr;
  reg        load_ack;

  reg [7:0]  uo_out_reg;

  reg        pending_lx_writeback;
  reg [1:0]  pending_lx_rd;

  // -----------------------------------------------------------------
  // Instruction word for decode: live {mem_rdata, ir_lo} on EXECUTE's
  // first cycle, latched `ir` thereafter (see header comment).
  // -----------------------------------------------------------------
  wire [15:0] word = entering_execute ? {mem_rdata, ir_lo} : ir;

  wire [4:0] opcode      = word[`OPCODE_HI:`OPCODE_LO];
  wire [8:0] w_addr       = word[`ADDR_HI:`ADDR_LO];
  wire [1:0] w_cond       = word[`COND_HI:`COND_LO];
  wire [1:0] w_rd         = word[`RD_HI:`RD_LO];
  wire [8:0] w_imm        = word[`IMM_HI:`IMM_LO];
  wire [7:0] w_ldi_value  = word[`LDI_VALUE_HI:`LDI_VALUE_LO];
  wire [2:0] w_pin_index  = word[`PIN_INDEX_HI:`PIN_INDEX_LO];
  wire       w_aux_bit    = word[`PIN_AUX_BIT];
  wire [2:0] w_bitidx     = word[`BITIDX_HI:`BITIDX_LO];
  wire [1:0] w_exponent   = word[`RD_HI:`RD_LO];  // EXPONENT_W == RD_W
  wire [8:0] w_delay_mant = word[`IMM_HI:`IMM_LO];
  wire [4:0] w_wait_mant  = word[`WAIT_MANTISSA_HI:`WAIT_MANTISSA_LO];
  wire [1:0] w_loop_rd    = word[`LOOP_RD_HI:`LOOP_RD_LO];
  wire [1:0] w_rs         = word[`RS_HI:`RS_LO];
  wire       w_shift_dir  = word[`SHIFT_DIR_BIT];

  wire opcode_recognized = (opcode <= 5'd18);

  // -----------------------------------------------------------------
  // Shared DELAY/WAIT countdown counter. Declared before the regfile
  // write mux below (which reads `exec_done`) -- icarus enforces
  // declare-before-use for a wire referenced inside another wire's
  // continuous assignment within the same module, verilator doesn't;
  // keeping true source order rather than relying on the more lenient
  // tool's forward-reference tolerance.
  // -----------------------------------------------------------------
  wire        wait_has_timeout   = (w_wait_mant != 5'd0);
  wire        wait_condition_met = (pin_read == w_aux_bit);

  wire        cnt_load     = entering_execute &&
                              ((opcode == `OP_DELAY) ||
                               (opcode == `OP_WAIT && wait_has_timeout));
  wire [8:0]  cnt_mantissa = (opcode == `OP_DELAY) ? w_delay_mant : {4'd0, w_wait_mant};
  wire [1:0]  cnt_exponent = w_exponent;
  wire        cnt_expired;

  cycle_counter u_cycle_counter (
      .clk     (clk),
      .rst_n   (rst_n),
      .load    (cnt_load),
      .mantissa(cnt_mantissa),
      .exponent(cnt_exponent),
      .expired (cnt_expired)
  );

  wire delay_pending = (opcode == `OP_DELAY) && !cnt_expired;
  wire wait_pending   = (opcode == `OP_WAIT) && !wait_condition_met &&
                          !(wait_has_timeout && cnt_expired);
  wire exec_done = !delay_pending && !wait_pending;
  wire exec_commit = (state == S_EXECUTE) && exec_done;

  // -----------------------------------------------------------------
  // Regfile.
  // -----------------------------------------------------------------
  reg  [1:0] reg_a_sel;
  reg  [1:0] reg_b_sel;
  wire [7:0] ra_data;
  wire [7:0] rb_data;

  always @(*) begin
    reg_a_sel = w_rd;
    reg_b_sel = w_rs;
    case (opcode)
      `OP_LOOP: reg_a_sel = w_loop_rd;
      default:  ;
    endcase
  end

  // Regfile write mux: EXECUTE-cycle writebacks (most opcodes) or a
  // deferred LOAD/LOADX writeback landing in the *next* instruction's
  // FETCH_LO (docs/architecture.md -- SRAM read has 1-cycle latency,
  // address only known once EXECUTE decodes it).
  reg  [1:0] reg_w_sel;
  reg  [7:0] reg_w_data;
  reg        reg_w_en;

  always @(*) begin
    reg_w_sel  = w_rd;
    reg_w_data = 8'd0;
    reg_w_en   = 1'b0;
    case (opcode)
      `OP_LDI: begin
        reg_w_en   = 1'b1;
        reg_w_data = w_ldi_value;
      end
      `OP_IN: begin
        reg_w_en   = 1'b1;
        reg_w_data = ui_in_sync;
      end
      `OP_SHIFT: begin
        reg_w_en   = 1'b1;
        reg_w_data = (w_shift_dir == `SHIFT_LEFT) ? {ra_data[6:0], 1'b0}
                                                   : {1'b0, ra_data[7:1]};
      end
      `OP_INB: begin
        reg_w_en   = 1'b1;
        reg_w_data = (w_aux_bit == `BITSEL_BIT7) ? {pin_read, ra_data[6:0]}
                                                  : {ra_data[7:1], pin_read};
      end
      `OP_LOOP: begin
        reg_w_en   = 1'b1;
        reg_w_sel  = w_loop_rd;
        reg_w_data = ra_data - 8'd1;
      end
      default: ;
    endcase
  end

  wire       rf_we    = (state == S_EXECUTE) ? (exec_done && reg_w_en)
                                              : (state == S_FETCH_LO && pending_lx_writeback);
  wire [1:0] rf_waddr = (state == S_EXECUTE) ? reg_w_sel : pending_lx_rd;
  wire [7:0] rf_wdata = (state == S_EXECUTE) ? reg_w_data : mem_rdata;

  regfile u_regfile (
      .clk    (clk),
      .rst_n  (rst_n),
      .ra_addr(reg_a_sel),
      .ra_data(ra_data),
      .rb_addr(reg_b_sel),
      .rb_data(rb_data),
      .rw_addr(rf_waddr),
      .rw_data(rf_wdata),
      .rw_en  (rf_we)
  );

  // -----------------------------------------------------------------
  // Branch / LOOP / CALL / RET target resolution.
  // -----------------------------------------------------------------
  wire branch_taken = (opcode == `OP_BRANCH) &&
                       ((w_cond == `COND_JMP) ||
                        (w_cond == `COND_BEQ && flag) ||
                        (w_cond == `COND_BNE && !flag) ||
                        (w_cond == `COND_CALL));
  wire is_call = (opcode == `OP_BRANCH) && (w_cond == `COND_CALL);
  wire is_ret  = (opcode == `OP_RET);

  wire [7:0] loop_result = ra_data - 8'd1;
  wire       loop_taken  = (opcode == `OP_LOOP) && (loop_result != 8'd0);

  wire [8:0] next_pc = branch_taken ? w_addr :
                       is_ret       ? retaddr :
                       loop_taken   ? w_addr :
                                      pc + 9'd1;

  // -----------------------------------------------------------------
  // mem.v address/write-data mux.
  // -----------------------------------------------------------------
  reg [9:0] mem_addr_c;
  reg       mem_we_c;
  reg [7:0] mem_wdata_c;

  always @(*) begin
    mem_addr_c  = 10'd0;
    mem_we_c    = 1'b0;
    mem_wdata_c = 8'd0;
    case (state)
      S_LOAD: begin
        mem_addr_c  = boot_addr;
        mem_we_c    = host_go_rise && !load_ack;
        mem_wdata_c = ui_in_sync;
      end
      S_FETCH_LO: mem_addr_c = {pc, 1'b0};
      S_FETCH_HI: mem_addr_c = {pc, 1'b1};
      S_EXECUTE: begin
        case (opcode)
          `OP_LOAD:  mem_addr_c = `DATA_BASE + {1'b0, w_imm};
          `OP_LOADX: mem_addr_c = `DATA_BASE + {2'b0, rb_data};
          `OP_STORE: begin
            mem_addr_c  = `DATA_BASE + {1'b0, w_imm};
            mem_we_c    = 1'b1;
            mem_wdata_c = ra_data;
          end
          default: ;
        endcase
      end
      default: ;
    endcase
  end

  assign mem_addr  = mem_addr_c;
  assign mem_we    = mem_we_c;
  assign mem_wdata = mem_wdata_c;

  // -----------------------------------------------------------------
  // pin_ctrl.v request mux: LOAD-mode HOST_STATUS ack (continuous,
  // pin_index=5) vs. running-mode SET/OUTB (pulsed on exec_commit).
  // -----------------------------------------------------------------
  assign pin_index = (state == S_LOAD) ? 3'd5 : w_pin_index;
  assign set_en    = (state == S_LOAD) ? 1'b1 : (exec_commit && opcode == `OP_SET);
  assign set_mode  = (state == S_LOAD) ? 2'd0 : w_rd;
  assign set_value = (state == S_LOAD) ? load_ack : w_aux_bit;
  assign outb_en   = exec_commit && (opcode == `OP_OUTB);
  assign outb_bit  = (w_aux_bit == `BITSEL_BIT7) ? ra_data[7] : ra_data[0];
  assign mode_load = (state == S_LOAD);

  // -----------------------------------------------------------------
  // uo_out: boot-mode address echo, or runtime OUT.
  // -----------------------------------------------------------------
  wire [9:0] boot_addr_next = (boot_addr == 10'd1023) ? boot_addr : boot_addr + 10'd1;

  assign uo_out = uo_out_reg;

  // -----------------------------------------------------------------
  // Main sequential FSM.
  // -----------------------------------------------------------------
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state                 <= S_LOAD;
      pc                    <= 9'd0;
      ir_lo                 <= 8'd0;
      ir                    <= 16'd0;
      entering_execute      <= 1'b0;
      flag                  <= 1'b0;
      retaddr               <= 9'd0;
      return_valid          <= 1'b0;
      illegal_op_flag       <= 1'b0;
      call_ret_misuse_flag  <= 1'b0;
      boot_addr             <= 10'd0;
      load_ack              <= 1'b0;
      uo_out_reg            <= 8'd0;
      pending_lx_writeback  <= 1'b0;
      pending_lx_rd         <= 2'd0;
      halted                <= 1'b0;
    end else if (!halted) begin
      case (state)

        S_LOAD: begin
          if (start_rise) begin
            state      <= S_FETCH_LO;
            pc         <= 9'd0;
            uo_out_reg <= 8'd0;  // re-clear -- docs/architecture.md I/O reset section
          end else if (load_ack) begin
            if (host_go_fall) load_ack <= 1'b0;
          end else if (host_go_rise) begin
            boot_addr  <= boot_addr_next;
            uo_out_reg <= boot_addr_next[7:0];
            load_ack   <= 1'b1;
          end
        end

        S_FETCH_LO: begin
          state <= S_FETCH_HI;
          if (pending_lx_writeback) pending_lx_writeback <= 1'b0;
        end

        S_FETCH_HI: begin
          ir_lo             <= mem_rdata;
          state             <= S_EXECUTE;
          entering_execute  <= 1'b1;
        end

        S_EXECUTE: begin
          if (entering_execute) ir <= word;
          entering_execute <= 1'b0;

          if (exec_done) begin
            if (opcode == `OP_HALT) begin
              // Freeze exactly here -- no state/pc/other update, ever
              // again until reset (docs/isa.md HALT row: "stops
              // fetching, no further state changes"). Every other
              // register (uio_oe/uio_out included, via pin_ctrl.v's
              // own sticky state that core.v simply stops updating)
              // holds its last-driven value by construction.
              halted <= 1'b1;
            end else begin
              state <= S_FETCH_LO;
              pc    <= next_pc;

              case (opcode)
                `OP_CMP:     flag <= (ra_data == rb_data);
                `OP_TESTBIT: flag <= ra_data[w_bitidx];
                `OP_WAIT:    flag <= wait_condition_met ? 1'b0 : 1'b1;
                default: ;
              endcase

              if (is_call) begin
                retaddr <= pc + 9'd1;
                if (return_valid) call_ret_misuse_flag <= 1'b1;
                return_valid <= 1'b1;
              end else if (is_ret) begin
                if (!return_valid) call_ret_misuse_flag <= 1'b1;
                return_valid <= 1'b0;
              end

              if (opcode == `OP_LOAD || opcode == `OP_LOADX) begin
                pending_lx_writeback <= 1'b1;
                pending_lx_rd        <= w_rd;
              end

              if (opcode == `OP_OUT) uo_out_reg <= ra_data;

              if (!opcode_recognized) illegal_op_flag <= 1'b1;
            end
          end
        end

        default: state <= S_LOAD;
      endcase
    end
  end

  // illegal_op_flag/call_ret_misuse_flag are formal/debug-visibility
  // only (docs/isa.md Undefined opcode behavior, Branch format) -- no
  // port exposes them yet, hence no reader. start_fall is pin_ctrl.v's
  // own driver-contention gate (docs/architecture.md LOAD section),
  // not something core.v acts on.
  wire _unused = &{start_fall, illegal_op_flag, call_ret_misuse_flag, 1'b0};

endmodule
