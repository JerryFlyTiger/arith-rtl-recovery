module top(in1, in2, in3, in4, out1);
  input [5:0] in1;
  input [5:0] in2;
  input [7:0] in3;
  input [7:0] in4;
  output [11:0] out1;
  assign out1 = (in1 < in2) ? in3 : in4;
endmodule
