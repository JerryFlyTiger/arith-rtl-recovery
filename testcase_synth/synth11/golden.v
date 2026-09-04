module top(in1, out1);
  input [19:0] in1;
  output out1;
  assign out1 = in1 >= 500000;
endmodule
