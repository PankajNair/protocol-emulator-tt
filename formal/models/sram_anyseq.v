// sram_anyseq.v -- formal-only stand-in for the IHP SRAM macro, used by
// chip-level props (agent_top_*_props.v). Read data is unconstrained on
// every cycle (`anyseq`): the chip is checked against ANY memory
// contents and ANY program, an over-approximation of the real macro, so
// a property proven here holds for every program the chip could run.
// Port list matches src/RM_IHPSG13_1P_1024x8_c2_bm_bist.v exactly.
module RM_IHPSG13_1P_1024x8_c2_bm_bist (
    input        A_CLK,
    input        A_MEN,
    input        A_WEN,
    input        A_REN,
    input  [9:0] A_ADDR,
    input  [7:0] A_DIN,
    input        A_DLY,
    output [7:0] A_DOUT,
    input  [7:0] A_BM,
    input        A_BIST_CLK,
    input        A_BIST_EN,
    input        A_BIST_MEN,
    input        A_BIST_WEN,
    input        A_BIST_REN,
    input  [9:0] A_BIST_ADDR,
    input  [7:0] A_BIST_DIN,
    input  [7:0] A_BIST_BM
);
  (* anyseq *) wire [7:0] dout;
  assign A_DOUT = dout;
endmodule
