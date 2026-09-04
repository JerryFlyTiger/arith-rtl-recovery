module top(in1, in2, in3, out1);
  input [7:0] in1;
  input [7:0] in2;
  input [7:0] in3;
  output [7:0] out1;
  assign out1 = (in1 < in2) ? 1 : in3;
endmodule
