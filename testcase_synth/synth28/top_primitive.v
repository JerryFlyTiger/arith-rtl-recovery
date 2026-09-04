
module top(in1, comp_a, comp_b, in2, in3, out1);
  input in1;
  input [2:0] comp_a;
  input [2:0] comp_b;
  input [1:0] in2;
  input [1:0] in3;
  output [1:0] out1;

  wire xa2;
  wire eq2;
  wire na2;
  wire lt2;
  xor g0(xa2, comp_a[2], comp_b[2]);
  not g1(eq2, xa2);
  not g2(na2, comp_a[2]);
  and g3(lt2, na2, comp_b[2]);
  wire res2;
  buf g4(res2, lt2);

  wire xa1;
  wire xeq1;
  wire na1;
  wire gt1and;
  wire lt1;
  xor g5(xa1, comp_a[1], comp_b[1]);
  not g6(xeq1, xa1);
  not g7(na1, comp_a[1]);
  and g8(gt1and, na1, comp_b[1]);
  and g9(lt1, eq2, gt1and);
  wire eq1;
  and g10(eq1, eq2, xeq1);
  wire res1;
  or g11(res1, res2, lt1);

  wire na0;
  wire gt0and;
  wire lt0;
  not g12(na0, comp_a[0]);
  and g13(gt0and, na0, comp_b[0]);
  and g14(lt0, eq1, gt0and);
  wire c;
  or g15(c, res1, lt0);

  wire nc;
  not g16(nc, c);

  wire t3_0;
  wire t3_1;
  buf g17(t3_0, in1);
  buf g18(t3_1, in1);

  wire p1_0;
  wire p2_0;
  wire t4_0;
  and g19(p1_0, c, in2[0]);
  and g20(p2_0, nc, t3_0);
  or g21(t4_0, p1_0, p2_0);

  wire p1_1;
  wire p2_1;
  wire t4_1;
  and g22(p1_1, c, in2[1]);
  and g23(p2_1, nc, t3_1);
  or g24(t4_1, p1_1, p2_1);

  wire q1_0;
  wire q2_0;
  and g25(q1_0, c, t4_0);
  and g26(q2_0, nc, in3[0]);
  or g27(out1[0], q1_0, q2_0);

  wire q1_1;
  wire q2_1;
  and g28(q1_1, c, t4_1);
  and g29(q2_1, nc, in3[1]);
  or g30(out1[1], q1_1, q2_1);
endmodule
