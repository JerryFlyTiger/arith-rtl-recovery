module top(in1, in2, in3, out1);
  input [6:0] in1;
  input [6:0] in2;
  input [2:0] in3;
  output out1;
  assign out1 = (((in1 < in2) + in3) >= 4);
endmodule
