module top(in1, in2, out1);
  input [7:0] in1;
  input [7:0] in2;
  output [7:0] out1;
  assign out1 = ($signed(in1) < $signed(in2)) ? in2 : in1;
endmodule
