module top(in1, in2, out1);
  input [3:0] in1;
  input [9:0] in2;
  output [9:0] out1;
  wire signed [9:0] s_in1;
  assign s_in1 = $signed(in1);
  assign out1 = s_in1 + in2;
endmodule
