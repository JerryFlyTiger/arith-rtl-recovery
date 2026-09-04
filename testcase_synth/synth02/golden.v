module top(in1, in2, out1);
  input [3:0] in1;
  input [5:0] in2;
  output [15:0] out1;
  assign out1 = 5 * $signed(in1) - 3 * $signed(in2) + 7;
endmodule
