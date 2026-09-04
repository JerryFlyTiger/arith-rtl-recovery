module top(in1, in2, in3, out1);
  input [5:0] in1;
  input [5:0] in2;
  input [7:0] in3;
  output [16:0] out1;
  assign out1 = $signed(in1) * $signed(in2) + $signed(in3);
endmodule
