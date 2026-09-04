module top(in1, in2, in3, outB, outA);
  input [3:0] in1;
  input [3:0] in2;
  input [3:0] in3;
  output [4:0] outB;
  output outA;
  assign outB = (in1 ^ in2) + in3;
  assign outA = outB >= 16;
endmodule
