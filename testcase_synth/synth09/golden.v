module top(in1, in2, in3, out1);
  input in1;
  input [7:0] in2;
  input [7:0] in3;
  output [8:0] out1;
  assign out1 = in1 * in2 + in3;
endmodule
