module top(in1, in2, out1, out2);
  input [7:0] in1;
  input [11:0] in2;
  output [7:0] out1;
  output [7:0] out2;
  assign out1 = $signed(in1) + 3;
  assign out2 = $signed(in2) + 5;
endmodule
