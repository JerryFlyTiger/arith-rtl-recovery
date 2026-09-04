module top(in1, in3, out1);
  input [5:0] in1;
  input [6:0] in3;
  output out1;
  // in1 must first be zero-extended into a separate wire signed before taking part in the subtraction: writing it directly as
  // `in1 - $signed(in3)` (unsigned in1 embedded directly in the same expression) would make the whole expression's
  // signedness degrades, causing $signed(in3) to be wrongly zero-extended instead of sign-extended -- this is exactly
  // is exactly what synth12 is meant to force out, and is also the existing discipline that solver/compare.py relies on render_word for
  // the pitfall avoided by (signed operands are always pre-extended in a separate assign first). Measured (iverilog):
  // once caught a version of golden.v that embedded `$signed(in3)` directly, computing f=-127 instead of 1.
  wire signed [8:0] s_in1, s_in3, f;
  assign s_in1 = in1;
  assign s_in3 = $signed(in3);
  assign f = s_in1 - s_in3;
  assign out1 = (f == 1);
endmodule
