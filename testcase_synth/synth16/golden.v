module top(in1, in2, in3, in4, in5, outB, outA);
  input [7:0] in1;
  input [7:0] in2;
  input [7:0] in3;
  input [7:0] in4;
  input [8:0] in5;
  output [8:0] outB;
  output [8:0] outA;
  assign outB = in1 + in2;
  assign outA = (in3 < in4) ? outB : in5;
endmodule
