module top(in1, in2, out1);
  input [7:0] in1;
  input [7:0] in2;
  output [9:0] out1;
  assign out1 = -3 * in1 - 5 * in2;
endmodule
