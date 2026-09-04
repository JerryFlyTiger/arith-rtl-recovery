module top(in1, in2, out1, out2);
  input [3:0] in1;
  input [3:0] in2;
  output [7:0] out1;
  output [7:0] out2;
  assign out1 = 8'd200;
  assign out2 = in2 + 1;
endmodule
