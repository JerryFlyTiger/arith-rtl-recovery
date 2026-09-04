module top(in1, comp_a, comp_b, in2, in3, out1);
  input in1;
  input [2:0] comp_a;
  input [2:0] comp_b;
  input [1:0] in2;
  input [1:0] in3;
  output [1:0] out1;
  assign out1 = (comp_a < comp_b) ? in2 : in3;
endmodule
