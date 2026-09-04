module top(in1, in2, in3, in4, out1);
  input [7:0] in1;
  input [7:0] in2;
  input [7:0] in3;
  input [7:0] in4;
  output [17:0] out1;
  wire signed [17:0] sp;
  assign sp = $signed(in3) * $signed(in4);
  assign out1 = in1 * in2 + sp;
endmodule
