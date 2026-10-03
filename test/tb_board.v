`default_nettype none
`timescale 1ns / 1ps

/* Board-level testbench: two chips (A, B) on shared nets, for loopback
   and chip-to-chip tests. The single-chip tb.v stays as-is -- the
   TinyTapeout gate-level flow depends on it.

   Each chip pin can be attached at runtime to one of NNETS nets
   (map_a/map_b, 4 bits per pin packed into 32 bits so cocotb writes one
   integer and @* sees the change; NONE = 0xF = not on a net). A net resolves
   like a real wire:
     - any driver driving 0           -> 0
     - else any driver driving 1      -> 1
     - else pulled up (net_pullup)    -> 1
     - else                           -> x (floating)
   A 0 and a 1 driven at once is contention: the net reads x and the net
   is recorded in contention_seen (sticky until cleared by the test), so
   a driver fight shows up as a failure, not a silent wired-AND.

   Drivers on a net: chip pins with uio_oe=1 (value uio_out), and the
   testbench itself via net_ext_oe/net_ext_val (bus models).
   A pin NOT on a net behaves like a lone TT pad: the chip reads back its
   own driven value, otherwise ext_in_* (set by cocotb: HOST_GO, START,
   ...). TT's uio_in always reflects the pad, even for a pin the chip
   drives -- so does this. */
module tb_board ();

  localparam NNETS = 8;
  localparam [3:0] NONE = 4'hF;

  initial begin
    $dumpfile("tb_board.fst");
    $dumpvars(0, tb_board);
    #1;
  end

  // -- chip A -----------------------------------------------------------
  reg        clk_a, rst_n_a;
  reg  [7:0] ui_in_a, ext_in_a;
  wire [7:0] uo_out_a, uio_out_a, uio_oe_a, uio_in_a;
  reg  [31:0] map_a;   // pin p -> map_a[4p+3:4p]

  // -- chip B -----------------------------------------------------------
  reg        clk_b, rst_n_b;
  reg  [7:0] ui_in_b, ext_in_b;
  wire [7:0] uo_out_b, uio_out_b, uio_oe_b, uio_in_b;
  reg  [31:0] map_b;

  // -- nets -------------------------------------------------------------
  reg  [NNETS-1:0] net_pullup;
  reg  [NNETS-1:0] net_ext_oe, net_ext_val;
  reg  [NNETS-1:0] net_val;
  reg  [NNETS-1:0] drive0, drive1;
  wire [NNETS-1:0] contention = drive0 & drive1;
  reg  [NNETS-1:0] contention_seen;

  integer p, n;
  always @* begin
    drive0 = net_ext_oe & ~net_ext_val;
    drive1 = net_ext_oe &  net_ext_val;
    for (p = 0; p < 8; p = p + 1) begin
      if (map_a[4*p +: 4] != NONE && uio_oe_a[p] === 1'b1) begin
        if (uio_out_a[p] === 1'b1) drive1[map_a[4*p +: 4]] = 1'b1;
        else                       drive0[map_a[4*p +: 4]] = 1'b1;
      end
      if (map_b[4*p +: 4] != NONE && uio_oe_b[p] === 1'b1) begin
        if (uio_out_b[p] === 1'b1) drive1[map_b[4*p +: 4]] = 1'b1;
        else                       drive0[map_b[4*p +: 4]] = 1'b1;
      end
    end
    for (n = 0; n < NNETS; n = n + 1) begin
      if (drive0[n] && drive1[n]) net_val[n] = 1'bx;
      else if (drive0[n])         net_val[n] = 1'b0;
      else if (drive1[n])         net_val[n] = 1'b1;
      else if (net_pullup[n])     net_val[n] = 1'b1;
      else                        net_val[n] = 1'bx;
    end
  end

  // Sampled on either chip's clock, so combinational settling glitches
  // inside one delta cycle can't register as contention.
  always @(posedge clk_a or posedge clk_b) contention_seen <= contention_seen | contention;

  genvar gp;
  generate
    for (gp = 0; gp < 8; gp = gp + 1) begin : PAD
      assign uio_in_a[gp] = (map_a[4*gp +: 4] != NONE) ? net_val[map_a[4*gp +: 4]]
                          : (uio_oe_a[gp] ? uio_out_a[gp] : ext_in_a[gp]);
      assign uio_in_b[gp] = (map_b[4*gp +: 4] != NONE) ? net_val[map_b[4*gp +: 4]]
                          : (uio_oe_b[gp] ? uio_out_b[gp] : ext_in_b[gp]);
    end
  endgenerate

  tt_um_pankajnair_protocol_emulator chip_a (
      .ui_in(ui_in_a), .uo_out(uo_out_a), .uio_in(uio_in_a),
      .uio_out(uio_out_a), .uio_oe(uio_oe_a), .ena(1'b1),
      .clk(clk_a), .rst_n(rst_n_a));

  tt_um_pankajnair_protocol_emulator chip_b (
      .ui_in(ui_in_b), .uo_out(uo_out_b), .uio_in(uio_in_b),
      .uio_out(uio_out_b), .uio_oe(uio_oe_b), .ena(1'b1),
      .clk(clk_b), .rst_n(rst_n_b));

endmodule
