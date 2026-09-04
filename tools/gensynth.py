"""Synthetic test case generator: fills in signed paths that none of the 30
hand-made/official cases ever exercise.

Testing shows that across the 30 existing cases, `solver/linear.py` never once
produced a `Term(signed=True)` -- `model.py`'s `s_<port>_w<W>` pre-extension
wire, its `$signed()` cast, and `main.py`'s signed wire declaration are all dead
code, with zero guarantee from rtlcheck. This module uses `tools/refgen.py`'s
`Builder` to build a batch of synthetic cases deliberately designed to force out
signed paths, negative coefficients, constant outputs, unused ports, squares,
mixed signed/unsigned products, and other shapes, for `tools/solverbench.py
--synth` regression.

    python3 tools/gensynth.py                # generate all synthetic cases
    python3 tools/gensynth.py --selfcheck     # after generating, verify each case
                                               # with rtlcheck: "Builder netlist ==
                                               # golden.v spec"

Each case directory:

    testcase_synth/synthNN/top_primitive.v    the primitive-gate netlist built by Builder
    testcase_synth/synthNN/golden.v           the equivalent behavioral-level Verilog
                                               (hand-written, i.e. the spec)
    testcase_synth/synthNN/spec.txt           a one-line human-readable spec

`--selfcheck` uses `rtlcheck.check(tag, sol=golden.v, net=top_primitive.v)`:
golden.v is the independent source of truth for "what the spec should look
like" -- the Builder netlist must first prove itself equivalent to it before it
is eligible to be used to verify the solver's output
(tag=synthNN, sol=solver output, net=the same top_primitive.v).
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "solver"))

from refgen import Builder  # noqa: E402
import rtlcheck  # noqa: E402

OUTDIR = os.path.join(ROOT, "testcase_synth")


def smul(b, A, B):
    """Signed (two's-complement) multiplication: |A|*|B| (an unsigned array
    multiplier), with the sign flipped afterward according to signedness.

    Correctness argument: the magnitude range of a wa-bit two's-complement A is
    [0, 2^(wa-1)] (including the most negative value -2^(wa-1), whose neg() under
    mod 2^wa equals exactly 2^(wa-1), which still fits within what a wa-bit
    unsigned value can represent), so |A| and |B|, each expressed at their
    original width, are exact magnitudes, and the (wa+wb)-bit unsigned product
    equals the exact value of |A|*|B|; flipping the sign afterward gives the
    exact two's-complement product (mod 2^(wa+wb)) -- exactly matching the
    semantics of Verilog's `$signed(a)*$signed(b)`.
    """
    wa, wb = len(A), len(B)
    sa, sb = A[-1], B[-1]
    mag_a = b.mux_bus(sa, b.neg(A), A)
    mag_b = b.mux_bus(sb, b.neg(B), B)
    prod = b.mul(mag_a, mag_b)[: wa + wb]
    sign = b.XOR(sa, sb)
    return b.mux_bus(sign, b.neg(prod), prod)[: wa + wb]


def build(tag, spec, ins, outs, build_fn):
    """ins: [(name,width)]; outs: [(name,width)]; build_fn(b) -> {name: bits}."""
    b = Builder()
    for n, w in ins:
        b.add_input(n, w)
    results = build_fn(b)
    for n, w in outs:
        b.set_output(n, w, results[n])
    d = os.path.join(OUTDIR, tag)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "top_primitive.v"), "w").write(b.emit("top"))
    open(os.path.join(d, "spec.txt"), "w").write(spec.strip() + "\n")


def golden(tag, text):
    d = os.path.join(OUTDIR, tag)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "golden.v"), "w").write(text)


# ---------------------------------------------------------------------------
# synth01: a narrow signed port going into a wide word -- forces out Term(signed=True) with port_width < width.
# ---------------------------------------------------------------------------
def gen_synth01():
    tag = "synth01"
    build(
        tag, "out1[9:0] = $signed(in1[3:0]) + in2[9:0]",
        [("in1", 4), ("in2", 10)], [("out1", 10)],
        lambda b: {"out1": b.add(b.sext(b.port("in1"), 10), b.port("in2"))},
    )
    golden(tag, """\
module top(in1, in2, out1);
  input [3:0] in1;
  input [9:0] in2;
  output [9:0] out1;
  wire signed [9:0] s_in1;
  assign s_in1 = $signed(in1);
  assign out1 = s_in1 + in2;
endmodule
""")


# ---------------------------------------------------------------------------
# synth02: signed linear with coefficients -- forces out render_word's subtraction layout and multiple signed pre-extensions.
# ---------------------------------------------------------------------------
def gen_synth02():
    tag = "synth02"
    build(
        tag, "out1[15:0] = 5*$signed(in1[3:0]) - 3*$signed(in2[5:0]) + 7",
        [("in1", 4), ("in2", 6)], [("out1", 16)],
        lambda b: {
            "out1": b.add(
                b.sub(b.const_mul(b.sext(b.port("in1"), 16), 5),
                      b.const_mul(b.sext(b.port("in2"), 16), 3)),
                b.const_val(7, 16),
            )
        },
    )
    golden(tag, """\
module top(in1, in2, out1);
  input [3:0] in1;
  input [5:0] in2;
  output [15:0] out1;
  assign out1 = 5 * $signed(in1) - 3 * $signed(in2) + 7;
endmodule
""")


# ---------------------------------------------------------------------------
# synth03: signed with port_width == width and port_width > width -- after truncation signedness doesn't matter,
# should not produce an s_<port>_w<W> pre-extension wire.
# ---------------------------------------------------------------------------
def gen_synth03():
    tag = "synth03"
    build(
        tag,
        "out1[7:0] = $signed(in1[7:0]) + 3; out2[7:0] = $signed(in2[11:0]) + 5",
        [("in1", 8), ("in2", 12)], [("out1", 8), ("out2", 8)],
        lambda b: {
            "out1": b.add(b.port("in1"), b.const_val(3, 8)),
            "out2": b.add(b.zext(b.port("in2"), 8), b.const_val(5, 8)),
        },
    )
    golden(tag, """\
module top(in1, in2, out1, out2);
  input [7:0] in1;
  input [11:0] in2;
  output [7:0] out1;
  output [7:0] out2;
  assign out1 = $signed(in1) + 3;
  assign out2 = $signed(in2) + 5;
endmodule
""")


# ---------------------------------------------------------------------------
# synth04: all-negative coefficients -- forces out the path where render_word's first term uses a unary minus attached to a numeric literal.
# ---------------------------------------------------------------------------
def gen_synth04():
    tag = "synth04"
    build(
        tag, "out1[9:0] = -3*in1 - 5*in2",
        [("in1", 8), ("in2", 8)], [("out1", 10)],
        lambda b: {
            "out1": b.neg(b.add(
                b.const_mul(b.zext(b.port("in1"), 10), 3),
                b.const_mul(b.zext(b.port("in2"), 10), 5),
            ))
        },
    )
    golden(tag, """\
module top(in1, in2, out1);
  input [7:0] in1;
  input [7:0] in2;
  output [9:0] out1;
  assign out1 = -3 * in1 - 5 * in2;
endmodule
""")


# ---------------------------------------------------------------------------
# synth05: constant output + a completely unused input port -- both must still appear in the port list / declarations.
# ---------------------------------------------------------------------------
def gen_synth05():
    tag = "synth05"
    build(
        tag, "out1[7:0] = 200 (constant); out2[7:0] = in2 + 1; in1 is entirely unused",
        [("in1", 4), ("in2", 4)], [("out1", 8), ("out2", 8)],
        lambda b: {
            "out1": b.const_val(200, 8),
            "out2": b.add(b.zext(b.port("in2"), 8), b.const_val(1, 8)),
        },
    )
    golden(tag, """\
module top(in1, in2, out1, out2);
  input [3:0] in1;
  input [3:0] in2;
  output [7:0] out1;
  output [7:0] out2;
  assign out1 = 8'd200;
  assign out2 = in2 + 1;
endmodule
""")


# ---------------------------------------------------------------------------
# synth06: sum of signed x signed products.
# ---------------------------------------------------------------------------
def gen_synth06():
    tag = "synth06"
    build(
        tag, "out1[16:0] = $signed(in1)*$signed(in2) + $signed(in3)",
        [("in1", 6), ("in2", 6), ("in3", 8)], [("out1", 17)],
        lambda b: {
            "out1": b.add(b.sext(smul(b, b.port("in1"), b.port("in2")), 17),
                          b.sext(b.port("in3"), 17))
        },
    )
    golden(tag, """\
module top(in1, in2, in3, out1);
  input [5:0] in1;
  input [5:0] in2;
  input [7:0] in3;
  output [16:0] out1;
  assign out1 = $signed(in1) * $signed(in2) + $signed(in3);
endmodule
""")


# ---------------------------------------------------------------------------
# synth07: mixing signed/unsigned product sums (test22's shape).
# ---------------------------------------------------------------------------
def gen_synth07():
    tag = "synth07"
    build(
        tag,
        "out1[17:0] = in1*in2 + $signed(in3)*$signed(in4)",
        [("in1", 8), ("in2", 8), ("in3", 8), ("in4", 8)], [("out1", 18)],
        lambda b: {
            "out1": b.add(
                b.zext(b.mul(b.port("in1"), b.port("in2")), 18),
                b.sext(smul(b, b.port("in3"), b.port("in4")), 18),
            )
        },
    )
    golden(tag, """\
module top(in1, in2, in3, in4, out1);
  input [7:0] in1;
  input [7:0] in2;
  input [7:0] in3;
  input [7:0] in4;
  output [17:0] out1;
  wire signed [17:0] sp;
  assign sp = $signed(in3) * $signed(in4);
  assign out1 = in1 * in2 + sp;
endmodule
""")


# ---------------------------------------------------------------------------
# synth08: square.
# ---------------------------------------------------------------------------
def gen_synth08():
    tag = "synth08"
    build(
        tag, "out1[16:0] = in1**2 + in2",
        [("in1", 8), ("in2", 8)], [("out1", 17)],
        lambda b: {
            "out1": b.add(b.zext(b.mul(b.port("in1"), b.port("in1")), 17),
                          b.port("in2"))
        },
    )
    golden(tag, """\
module top(in1, in2, out1);
  input [7:0] in1;
  input [7:0] in2;
  output [16:0] out1;
  assign out1 = in1 ** 2 + in2;
endmodule
""")


# ---------------------------------------------------------------------------
# synth09: 1-bit control bit x word (test19's shape).
# ---------------------------------------------------------------------------
def gen_synth09():
    tag = "synth09"
    build(
        tag, "out1[8:0] = in1*in2 + in3 (in1 is 1-bit)",
        [("in1", 1), ("in2", 8), ("in3", 8)], [("out1", 9)],
        lambda b: {
            "out1": b.add(b.zext(b.mul(b.port("in1"), b.port("in2")), 9),
                          b.port("in3"))
        },
    )
    golden(tag, """\
module top(in1, in2, in3, out1);
  input in1;
  input [7:0] in2;
  input [7:0] in3;
  output [8:0] out1;
  assign out1 = in1 * in2 + in3;
endmodule
""")


# ---------------------------------------------------------------------------
# synth10 (M3b): a comparison references an output word that "itself falls back" -- forces out "what's referenced is
# a port name, not another word's expression": outB = (in1 ^ in2) + in3
# (XOR mixes in a nonlinear term that neither linear's nor quadratic's probabilistic probing can solve, so it can only fall back),
# outA is a comparison that takes outB as an atom; outB falling back shouldn't block outA from being solved by the compare family.
# using XOR (rather than a product) is a deliberate choice: a product's set of reachable values is very sparse (the product of three factors over 0..15
# in the middle range, "the value itself is often unreachable", forcing refinement to never converge to width 1, turning it into a test of
# "the gap is inherently unreachable" rather than the rule "referencing a fallback word" itself); `(in1^in2)+in3`
# has densely reachable values for any threshold (in3 itself can linearly sweep a contiguous range), so refinement converges easily,
# so this case focuses solely on the rule it's actually meant to test.
# ---------------------------------------------------------------------------
def gen_synth10():
    tag = "synth10"

    def build_fn(b):
        xor_bus = [b.XOR(x, y) for x, y in zip(b.port("in1"), b.port("in2"))]
        outb = b.add(b.zext(xor_bus, 5), b.zext(b.port("in3"), 5))[:5]
        outa = [b.NOT(b.ult(outb, b.const_val(16, 5)))]
        return {"outB": outb, "outA": outa}

    build(
        tag, "outB[4:0] = (in1^in2)+in3 (falls back); outA = outB >= 16 (references outB)",
        [("in1", 4), ("in2", 4), ("in3", 4)], [("outB", 5), ("outA", 1)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, in3, outB, outA);
  input [3:0] in1;
  input [3:0] in2;
  input [3:0] in3;
  output [4:0] outB;
  output outA;
  assign outB = (in1 ^ in2) + in3;
  assign outA = outB >= 16;
endmodule
""")


# ---------------------------------------------------------------------------
# synth11 (M3b): the threshold falls in a narrow range that 512 (+boundary) standard vectors can almost never hit exactly
# -- forces out the refinement path for "gap width > 1 and a value inside the gap is reachable" (should give up rather than forcing convergence
# candidates; once it converges it should hit the threshold exactly).
# ---------------------------------------------------------------------------
def gen_synth11():
    tag = "synth11"
    build(
        tag, "out1 = (in1 >= 500000), 20-bit port, threshold falls in a narrow range the standard vectors almost never hit",
        [("in1", 20)], [("out1", 1)],
        lambda b: {"out1": [b.NOT(b.ult(b.port("in1"), b.const_val(500000, 20)))]},
    )
    golden(tag, """\
module top(in1, out1);
  input [19:0] in1;
  output out1;
  assign out1 = in1 >= 500000;
endmodule
""")


# ---------------------------------------------------------------------------
# synth12 (M3b): two candidates (`in1 unsigned` and `in1 signed`) agree on the standard 512 vectors
# could both end up with 0 mismatches, and only diverge when "both atoms simultaneously approach their respective sign-flip points"
# (isomorphic to test16's in1/in3 trap): the correct solution is `in1(unsigned) - $signed(in3) == 1`.
# ---------------------------------------------------------------------------
def gen_synth12():
    tag = "synth12"

    def build_fn(b):
        f = b.sub(b.zext(b.port("in1"), 9), b.sext(b.port("in3"), 9))
        diff = b.sub(f, b.const_val(1, 9))
        notbits = [b.NOT(x) for x in diff]
        allzero = notbits[0]
        for x in notbits[1:]:
            allzero = b.AND(allzero, x)
        return {"out1": [allzero]}

    build(
        tag, "out1 = (in1(unsigned) - $signed(in3)) == 1; the correct solution must not mistake in1 for signed",
        [("in1", 6), ("in3", 7)], [("out1", 1)], build_fn,
    )
    golden(tag, """\
module top(in1, in3, out1);
  input [5:0] in1;
  input [6:0] in3;
  output out1;
  // in1 must first be zero-extended into an independent wire signed before
  // taking part in the subtraction: writing `in1 - $signed(in3)` directly
  // (with unsigned in1 embedded in the same expression) degrades the whole
  // expression's signedness, causing $signed(in3) to be wrongly
  // zero-extended instead of sign-extended -- this is exactly what synth12
  // is meant to force out, and exactly the pitfall that solver/compare.py
  // avoids via render_word's existing discipline (signed operands are
  // always pre-extended in their own independent assign first). Testing
  // (iverilog) once caught a version of golden.v that embedded
  // `$signed(in3)` directly and computed f=-127 instead of 1.
  wire signed [8:0] s_in1, s_in3, f;
  assign s_in1 = in1;
  assign s_in3 = $signed(in3);
  assign f = s_in1 - s_in3;
  assign out1 = (f == 1);
endmodule
""")


# ---------------------------------------------------------------------------
# synth13 (M23): unsigned max -- the target shape of `(unpublished family)` Stage 1, X/Y are both unsigned
# input port atom. `ult`/`slt` return a single wire name (`str`), not a list --
# writing `[0]` would create an undeclared wire, output all X, and rtlcheck would still PASS (see
# the return type of `tools/refgen.py:177,185`; only `mux_bus` takes a single-bit selector).
# ---------------------------------------------------------------------------
def gen_synth13():
    tag = "synth13"

    def build_fn(b):
        c = b.ult(b.port("in1"), b.port("in2"))
        return {"out1": b.mux_bus(c, b.port("in2"), b.port("in1"))}

    build(
        tag, "out1 = (in1 < in2) ? in2 : in1 (unsigned max)",
        [("in1", 8), ("in2", 8)], [("out1", 8)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, out1);
  input [7:0] in1;
  input [7:0] in2;
  output [7:0] out1;
  assign out1 = (in1 < in2) ? in2 : in1;
endmodule
""")


# ---------------------------------------------------------------------------
# synth14 (M23): unsigned min -- the same pair of atoms as synth13, with the two `?:` branches swapped.
# ---------------------------------------------------------------------------
def gen_synth14():
    tag = "synth14"

    def build_fn(b):
        c = b.ult(b.port("in1"), b.port("in2"))
        return {"out1": b.mux_bus(c, b.port("in1"), b.port("in2"))}

    build(
        tag, "out1 = (in1 < in2) ? in1 : in2 (unsigned min)",
        [("in1", 8), ("in2", 8)], [("out1", 8)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, out1);
  input [7:0] in1;
  input [7:0] in2;
  output [7:0] out1;
  assign out1 = (in1 < in2) ? in1 : in2;
endmodule
""")


# ---------------------------------------------------------------------------
# synth15 (M23): signed max -- X/Y are both `$signed(port)` atoms, forcing out "the condition and X/Y
# both need a signed reading" shape ((unpublished family)'s docstring "rendering" section is meant to verify exactly this:
# the portview widths of the X and Y branches must match, otherwise Verilog's `?:` sign extension goes wrong).
# ---------------------------------------------------------------------------
def gen_synth15():
    tag = "synth15"

    def build_fn(b):
        c = b.slt(b.port("in1"), b.port("in2"))
        return {"out1": b.mux_bus(c, b.port("in2"), b.port("in1"))}

    build(
        tag, "out1 = ($signed(in1) < $signed(in2)) ? in2 : in1 (signed max)",
        [("in1", 8), ("in2", 8)], [("out1", 8)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, out1);
  input [7:0] in1;
  input [7:0] in2;
  output [7:0] out1;
  assign out1 = ($signed(in1) < $signed(in2)) ? in2 : in1;
endmodule
""")


# ---------------------------------------------------------------------------
# synth16 (M23): X is an output word declared earlier (`outB`), verifying the two-layer anti-cycle guard
# (rule 1: declaration order, rule 2: cone check) being reused from `compare.build_context` --
# `outB` is declared first and its cone doesn't intersect `outA`'s, so this is a legal reference.
# ---------------------------------------------------------------------------
def gen_synth16():
    tag = "synth16"

    def build_fn(b):
        outb = b.add(b.zext(b.port("in1"), 9), b.zext(b.port("in2"), 9))[:9]
        c = b.ult(b.port("in3"), b.port("in4"))
        outa = b.mux_bus(c, outb, b.port("in5"))
        return {"outB": outb, "outA": outa}

    build(
        tag,
        "outB[8:0] = in1+in2 (a word declared earlier);"
        "outA[8:0] = (in3<in4) ? outB : in5 (references outB as X)",
        [("in1", 8), ("in2", 8), ("in3", 8), ("in4", 8), ("in5", 9)],
        [("outB", 9), ("outA", 9)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, in3, in4, in5, outB, outA);
  input [7:0] in1;
  input [7:0] in2;
  input [7:0] in3;
  input [7:0] in4;
  input [8:0] in5;
  output [8:0] outB;
  output [8:0] outA;
  assign outB = in1 + in2;
  assign outA = (in3 < in4) ? outB : in5;
endmodule
""")


def gen_synth17():
    """M23 fix round (code review F3): **an atom's declared width != the output word's
    width**.

    In synth13-16, every atom's width happened to exactly equal the word's width,
    so the invariant in `(unpublished family)._render_atom` that "a portview's
    width always takes the output word's `width`" was **never actually
    exercised** -- when the two branches are equal width, `?:` needs no
    extension at all, and the `wire signed` that `SharePool` prints
    unconditionally has no effect on the value either. This case makes out1 4
    bits wider than both branch ports, forcing out the real extension behavior:
    the branches' semantics are **unsigned**, so zero-extension is required; if
    the width is ever changed to take the atom's own width, or the signedness
    handling gets it wrong, sign extension will show up here and rtlcheck will
    go red.
    """
    tag = "synth17"

    def build_fn(b):
        c = b.ult(b.port("in1"), b.port("in2"))
        out = b.mux_bus(c, b.zext(b.port("in3"), 12), b.zext(b.port("in4"), 12))
        return {"out1": out}

    build(
        tag,
        "out1[11:0] = (in1<in2) ? in3 : in4 -- the branches are 8-bit ports, the output is 12 bits,"
        "atom width != word width, forces the zero-extension path",
        [("in1", 6), ("in2", 6), ("in3", 8), ("in4", 8)],
        [("out1", 12)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, in3, in4, out1);
  input [5:0] in1;
  input [5:0] in2;
  input [7:0] in3;
  input [7:0] in4;
  output [11:0] out1;
  assign out1 = (in1 < in2) ? in3 : in4;
endmodule
""")


# ---------------------------------------------------------------------------
# synth18 (M24): abs -- one of the target shapes of `(unpublished family)` Stage 2. X/Y are both "the
# the difference of support ports", no longer a single atom. Each branch's own subtraction, within its own
# never wraps within its own subdomain (`in2-in1` is always nonnegative when `in1<in2` is true, and vice versa) -- this is exactly
# The premise that makes Stage 2's approach of "solving equations over ℚ, without reducing mod 2^W" valid,
# see the "why we don't do bit-weight probing" section of `solver/(unpublished family).py`'s module docstring.
# ---------------------------------------------------------------------------
def gen_synth18():
    tag = "synth18"

    def build_fn(b):
        c = b.ult(b.port("in1"), b.port("in2"))
        d1 = b.sub(b.port("in2"), b.port("in1"))
        d2 = b.sub(b.port("in1"), b.port("in2"))
        return {"out1": b.mux_bus(c, d1, d2)}

    build(
        tag, "out1 = (in1 < in2) ? in2 - in1 : in1 - in2 (abs difference)",
        [("in1", 8), ("in2", 8)], [("out1", 8)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, out1);
  input [7:0] in1;
  input [7:0] in2;
  output [7:0] out1;
  assign out1 = (in1 < in2) ? in2 - in1 : in1 - in2;
endmodule
""")


# ---------------------------------------------------------------------------
# synth19 (M24): guard_add -- another target shape of `(unpublished family)` Stage 2, where one branch
# is an expression (`in1+in3`), while the other branch is still a single atom (`in3`). out1 is deliberately chosen
# `max(w(in1),w(in3))+1` bits, so that the `in1+in3` branch never
# exceeds out1's width -- not "constrained by the condition", but structurally impossible to overflow,
# to avoid this synthetic case itself getting mixed up in the argument over "whether it should wrap around".
# ---------------------------------------------------------------------------
def gen_synth19():
    tag = "synth19"

    def build_fn(b):
        c = b.ult(b.port("in1"), b.port("in2"))
        s = b.add(b.port("in1"), b.port("in3"))  # 4+4-bit inputs -> 5 bits, inherently can't overflow
        return {"out1": b.mux_bus(c, s, b.port("in3"))}

    build(
        tag, "out1 = (in1 < in2) ? in1 + in3 : in3 (guard_add)",
        [("in1", 4), ("in2", 4), ("in3", 4)], [("out1", 5)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, in3, out1);
  input [3:0] in1;
  input [3:0] in2;
  input [3:0] in3;
  output [4:0] out1;
  assign out1 = (in1 < in2) ? in1 + in3 : in3;
endmodule
""")


# ---------------------------------------------------------------------------
# synth20 (M25): the target shape after `(unpublished family)` Stage 2 was relaxed -- the true branch is a single atom (`in1`),
# the false branch is the constant 0 (that branch is constant across the whole support, unlike synth18/19 where both X/Y are expressions
# differs).
# ---------------------------------------------------------------------------
def gen_synth20():
    tag = "synth20"

    def build_fn(b):
        c = b.ult(b.port("in1"), b.port("in2"))
        z = b.const_val(0, 8)
        return {"out1": b.mux_bus(c, b.port("in1"), z)}

    build(
        tag, "out1 = (in1 < in2) ? in1 : 0 (comparison-driven mux, constant branch)",
        [("in1", 8), ("in2", 8)], [("out1", 8)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, out1);
  input [7:0] in1;
  input [7:0] in2;
  output [7:0] out1;
  assign out1 = (in1 < in2) ? in1 : 0;
endmodule
""")


# ---------------------------------------------------------------------------
# synth21 (M25): a constant branch placed on the true side (`1`), the false branch is a third port unrelated to the condition
# (`in3`), making sure both "the constant is on the true branch" and "the false branch is another port, not the condition itself" are
# is exercised.
# ---------------------------------------------------------------------------
def gen_synth21():
    tag = "synth21"

    def build_fn(b):
        c = b.ult(b.port("in1"), b.port("in2"))
        one = b.const_val(1, 8)
        return {"out1": b.mux_bus(c, one, b.port("in3"))}

    build(
        tag, "out1 = (in1 < in2) ? 1 : in3 (comparison-driven mux, constant branch, true side)",
        [("in1", 8), ("in2", 8), ("in3", 8)], [("out1", 8)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, in3, out1);
  input [7:0] in1;
  input [7:0] in2;
  input [7:0] in3;
  output [7:0] out1;
  assign out1 = (in1 < in2) ? 1 : in3;
endmodule
""")


# ---------------------------------------------------------------------------
# synth22 (M25 fix round): `out1 = (in1 < in2) ? in1 : (in1 < in2)` -- **the same
# the comparator's output wire doubles as both the mux's selector and the false branch's value**. Functionally equivalent to synth20's
# `? in1 : 0` (that wire is 0 when the condition is false), but **the gate-level topology differs**: that wire has two fanouts
# role. Code review pointed out that neither synth20 nor synth21 exercises this topology, even though it's the most common
# variant in the corpus (`probe_m25.py --repro`'s A2) -- if the atom pool or support analysis ever mishandles a shared wire
# 's handling changes, without this case no light would turn red.
# ---------------------------------------------------------------------------
def gen_synth22():
    tag = "synth22"

    def build_fn(b):
        c = b.ult(b.port("in1"), b.port("in2"))   # returns a str, not a list --
        # must be wrapped as `[c]` when used as a "value" -- feeding it directly to `zext` would get split by `list(str)`
        # splits it into individual characters, creating an undeclared wire (output all x). Hit live on 2026-09-01.
        return {"out1": b.mux_bus(c, b.port("in1"), b.zext([c], 8))}

    build(
        tag, "out1 = (in1 < in2) ? in1 : (in1 < in2) (the condition wire doubles as both the selector and a branch value)",
        [("in1", 8), ("in2", 8)], [("out1", 8)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, out1);
  input [7:0] in1;
  input [7:0] in2;
  output [7:0] out1;
  assign out1 = (in1 < in2) ? in1 : (in1 < in2);
endmodule
""")


# ---------------------------------------------------------------------------
# synth23 (M26): `(unpublished family)` Stage 2/boundary admission -- both the true and false branches are **nonzero constants**
# (`5`/`3`), exactly the "both branches are constants" shape that M25 left behind and M26 finally admitted, golden
# cost 2.
# ---------------------------------------------------------------------------
def gen_synth23():
    tag = "synth23"

    def build_fn(b):
        c = b.ult(b.port("in1"), b.port("in2"))
        five = b.const_val(5, 8)
        three = b.const_val(3, 8)
        return {"out1": b.mux_bus(c, five, three)}

    build(
        tag, "out1 = (in1 < in2) ? 5 : 3 (comparison-driven mux, both branches constant)",
        [("in1", 8), ("in2", 8)], [("out1", 8)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, out1);
  input [7:0] in1;
  input [7:0] in2;
  output [7:0] out1;
  assign out1 = (in1 < in2) ? 5 : 3;
endmodule
""")


# ---------------------------------------------------------------------------
# synth24 (M26): `compare.try_compare`'s width filter is replaced with a value-range check -- `out1` is declared
# multi-bit (a D0-wide word), but its value range is still ⊆ {0,1}, golden cost 1 (`assign out1 =
# (in1 < in2);`, Verilog auto zero-extends).
# ---------------------------------------------------------------------------
def gen_synth24():
    tag = "synth24"

    def build_fn(b):
        c = b.ult(b.port("in1"), b.port("in2"))
        return {"out1": b.zext([c], 8)}

    build(
        tag, "out1 = (in1 < in2) (out1 is declared multi-bit, but its value range is still {0,1})",
        [("in1", 8), ("in2", 8)], [("out1", 8)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, out1);
  input [7:0] in1;
  input [7:0] in2;
  output [7:0] out1;
  assign out1 = (in1 < in2);
endmodule
""")


# ---------------------------------------------------------------------------
# synth25 (M27): the result of a comparison feeds into another comparison -- `compare`'s condition must be able to reference "this word
# the comparison bit between two atoms in its own atom pool" -- this derived atom (see `solver/compare.py`
# the milestone-27 paragraph of the module docstring). golden cost 2 (`(in1<in2) < in1` directly
# nested rendering). ⚠ `Builder.ult` returns a **single bit (str), not a list** -- feeding it directly
# the value argument fed to another `ult` must be wrapped as [c], otherwise it gets split into characters by `list(str)`,
# creates an undeclared wire, output all x, and `rtlcheck` would still PASS (a pitfall M25 already hit,
# the other cases at the top of `tools/gensynth.py` all follow this rule too).
# ---------------------------------------------------------------------------
def gen_synth25():
    tag = "synth25"

    def build_fn(b):
        c = b.ult(b.port("in1"), b.port("in2"))
        out = b.ult([c], b.port("in1"))
        return {"out1": [out]}

    build(
        tag, "out1 = ((in1 < in2) < in1) (the comparison result feeds into another comparison, derived atom)",
        [("in1", 7), ("in2", 7)], [("out1", 1)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, out1);
  input [6:0] in1;
  input [6:0] in2;
  output out1;
  assign out1 = ((in1 < in2) < in1);
endmodule
""")


# ---------------------------------------------------------------------------
# synth26 (M27 fix round): rendering where a derived atom **merges with another atom into an affine expression**
# path. `synth25`'s winning rendering is "two independently materialized portviews compared directly", which never
# goes into `_side_render`'s affine branch (`len(terms)==2`) -- code review hand-crafted this shape and verified it
# it's correct, but no case in the repo ever triggers it, so breaking it wouldn't turn any light red.
# ⚠ `in3` is deliberately narrow (3-bit), with a sufficiently wide output, so that `(in1<in2)+in3` never wraps around --
# when a derived atom goes through the affine path, it avoids wraparound by widening the pool item (`provider_width`),
# what this case is meant to verify is the rendering wiring, not wraparound semantics.
# ---------------------------------------------------------------------------
def gen_synth26():
    tag = "synth26"

    def build_fn(b):
        c = b.ult(b.port("in1"), b.port("in2"))     # returns a str; when used as a "value" it must be wrapped as [c]
        s = b.add(b.zext([c], 4), b.port("in3"))
        return {"out1": [b.NOT(b.ult(s, b.const_val(4, len(s))))]}

    build(
        tag, "out1 = ((in1 < in2) + in3) >= 4 (derived atom folded into an affine expression)",
        [("in1", 7), ("in2", 7), ("in3", 3)], [("out1", 1)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, in3, out1);
  input [6:0] in1;
  input [6:0] in2;
  input [2:0] in3;
  output out1;
  assign out1 = (((in1 < in2) + in3) >= 4);
endmodule
""")


# ---------------------------------------------------------------------------
# synth27 (M27 fix round): makes `_build_derived_atoms`'s **pairing order** observable.
# the implementer changed the order from "original index" to "all unsigned entries first" for the sake of `random_920`,
# and bumped `DERIVED_MAX` from 12 to 16 -- but `synth25` only has 4 atoms (12 candidates),
# both orders are kept entirely within budget, **that fix was invisible to any test** (caught in code review).
#
# this case has three ports (6 atoms, 30 candidates > DERIVED_MAX=16), and the answer needs
# **cross-port `in2 < in3`**: the original index order (u/s interleaved) only gets around to
# pairs starting with `in1`, so `(in2,in3)` never makes the cut; after sorting it lands at position 11-12, which is covered.
# 2026-09-02 testing: with the current ordering it's solved at cost 2; reverting to unsorted order **falls back**.
# ---------------------------------------------------------------------------
def gen_synth27():
    tag = "synth27"

    def build_fn(b):
        inner = b.ult(b.port("in2"), b.port("in3"))   # str
        return {"out1": [b.ult(b.zext([inner], 7), b.port("in1"))]}

    build(
        tag, "out1 = ((in2 < in3) < in1) (sensitive to derived-atom pairing order)",
        [("in1", 7), ("in2", 7), ("in3", 7)], [("out1", 1)], build_fn,
    )
    golden(tag, """\
module top(in1, in2, in3, out1);
  input [6:0] in1;
  input [6:0] in2;
  input [6:0] in3;
  output out1;
  assign out1 = ((in2 < in3) < in1);
endmodule
""")


GENERATORS = [
    gen_synth01, gen_synth02, gen_synth03, gen_synth04, gen_synth05,
    gen_synth06, gen_synth07, gen_synth08, gen_synth09,
    gen_synth10, gen_synth11, gen_synth12,
    gen_synth13, gen_synth14, gen_synth15, gen_synth16, gen_synth17,
    gen_synth18, gen_synth19, gen_synth20, gen_synth21, gen_synth22,
    gen_synth23, gen_synth24, gen_synth25, gen_synth26, gen_synth27,
]


def generate_all():
    for fn in GENERATORS:
        fn()


def selfcheck(nvec=20000):
    bad = 0
    tags = sorted(d for d in os.listdir(OUTDIR) if d.startswith("synth"))
    for tag in tags:
        d = os.path.join(OUTDIR, tag)
        net = os.path.join(d, "top_primitive.v")
        sol = os.path.join(d, "golden.v")
        ok, msg = rtlcheck.check(tag, nvec=nvec, sol=sol, net=net,
                                  workdir=os.path.join(ROOT, ".rtlcheck", tag))
        print(("  OK  " if ok else "FAIL  ") + msg, flush=True)
        if not ok:
            bad += 1
    print(f"\nselfcheck failed: {bad}/{len(tags)}")
    return bad


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true",
                     help="after generation, separately verify Builder netlist == golden.v")
    ap.add_argument("--nvec", type=int, default=20000)
    args = ap.parse_args(argv)

    os.makedirs(OUTDIR, exist_ok=True)
    generate_all()
    print(f"generated {len(GENERATORS)} synthetic cases at {OUTDIR}")

    if args.selfcheck:
        bad = selfcheck(nvec=args.nvec)
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
