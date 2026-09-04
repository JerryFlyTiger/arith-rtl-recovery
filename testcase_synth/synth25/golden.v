module top(in1, in2, out1);
  input [6:0] in1;
  input [6:0] in2;
  output out1;
  assign out1 = ((in1 < in2) < in1);
endmodule
