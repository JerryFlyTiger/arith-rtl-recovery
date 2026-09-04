"""Feeds constructed vectors where "the two sides of the comparison are exactly equal" into
iverilog, comparing the solver's produced RTL against the original netlist.

    python3 tools/probe_eqvec.py [solver-produced test13.v]      # defaults to .solverout/test13.v

Why this tool is needed: `>` and `>=` **can only be told apart at the points where the two sides are
exactly equal**, and `tools/rtlcheck.py` generates its own random (including small-range) vectors with
**no interface to feed custom vectors**, so it never hits that set of equal points. This tool fills
that gap.

⚠ **It verifies the "emitted Verilog text", not the Python model.** This project's three-part
verification (training / independent seeds / boundary vectors) all runs against the model; nobody
watches the gap between the model and the text, and M3c once got bitten there (three-part check all
green, then `rtlcheck` found 585/20000 mismatches). Like `rtlcheck`, this tool is an instrument that
crosses that gap.

⚠ **Vector construction is specific to test13** (it knows the two forms `A = in2-in7`,
`B = in1-in2`, and uses that to solve for points where the two sides are equal). Using it on another
case requires modifying `_gen()`. This is deliberate: a general-purpose "solve for inputs that make
the two sides equal" would need to know what the two compared expressions actually are.

Measured at the M3d wrap-up: 1249 constructed vectors, 0 mismatches, 0 x-hits.
"""
import os
import random
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "solver"))
import rtlcheck  # noqa: E402

NET = os.path.join(ROOT, "testcase", "test13", "top_primitive.v")
WIDE = ["in1", "in2", "in7"]
NARROW = ["in3", "in4", "in5", "in6", "in8", "in9",
          "in10", "in11", "in12", "in13", "in14", "in15"]
OUTS = ["out1", "out2", "out3", "out4", "out5"]
SEED = 777
N_TRY = 1500


def _s12(v):
    return v - 4096 if v >= 2048 else v


def _gen():
    """Constructs vectors where out1's comparison sides are exactly equal. Three families:

    - `X == Y`  <=>  `in1 = 2*in2 - in7` (must fall within the 12-bit signed range)
    - `X == -Y` <=>  `in1 == in7`
    - `X == Y == 0` (all three equal)
    """
    rnd = random.Random(SEED)
    vecs = []
    for _ in range(N_TRY):
        v = {p: rnd.randrange(2) for p in NARROW}
        kind = rnd.randrange(3)
        if kind == 0:
            i2, i7 = rnd.randrange(4096), rnd.randrange(4096)
            i1s = 2 * _s12(i2) - _s12(i7)
            if not (-2048 <= i1s <= 2047):
                continue
            v.update(in1=i1s % 4096, in2=i2, in7=i7)
        elif kind == 1:
            a = rnd.randrange(4096)
            v.update(in1=a, in2=rnd.randrange(4096), in7=a)
        else:
            a = rnd.randrange(4096)
            v.update(in1=a, in2=a, in7=a)
        vecs.append(v)
    return vecs


def _pack(v):
    bits = 0
    for p in WIDE:
        bits = (bits << 12) | v[p]
    for p in NARROW:
        bits = (bits << 1) | v[p]
    return bits


def _build_tb(n):
    assigns = []
    sh = 48
    for p in WIDE:
        sh -= 12
        assigns.append(f"      {p} = V[i][{sh + 11}:{sh}];")
    for p in NARROW:
        sh -= 1
        assigns.append(f"      {p} = V[i][{sh}];")
    order = ["in1", "in2", "in3", "in4", "in5", "in6", "in7", "in8", "in9",
             "in10", "in11", "in12", "in13", "in14", "in15"]
    conn = ", ".join(order)
    d = ", ".join(f"d_{o}" for o in OUTS)
    r = ", ".join(f"r_{o}" for o in OUTS)
    cmp_expr = " || ".join(f"(d_{o} !== r_{o})" for o in OUTS)
    x_expr = " || ".join(f"(d_{o} === 1'bx) || (r_{o} === 1'bx)" for o in OUTS)
    return f"""
module tb;
  reg [11:0] in1, in2, in7;
  reg in3, in4, in5, in6, in8, in9, in10, in11, in12, in13, in14, in15;
  wire {d};
  wire {r};
  reg [47:0] V [0:{n - 1}];
  integer i; integer errors; integer xhits;
  dut u_d({conn}, {d});
  ref u_r({conn}, {r});
  initial begin
    $readmemh("vec.hex", V);
    errors = 0; xhits = 0;
    for (i = 0; i < {n}; i = i + 1) begin
{chr(10).join(assigns)}
      #1;
      if ({x_expr}) xhits = xhits + 1;
      else if ({cmp_expr}) begin
        errors = errors + 1;
        if (errors < 5)
          $display("MISMATCH i=%0d in1=%0d in2=%0d in7=%0d", i, in1, in2, in7);
      end
    end
    $display("VECTORS %0d ERRORS %0d XHITS %0d", {n}, errors, xhits);
  end
endmodule
"""


def main():
    sol = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, ".solverout", "test13.v")
    if not os.path.exists(sol):
        print(f"{sol} not found -- run `python3 tools/solverbench.py 13` first", file=sys.stderr)
        return 1
    vecs = _gen()
    with tempfile.TemporaryDirectory() as wd:
        rtlcheck._rename_module(sol, os.path.join(wd, "dut.v"), "dut")
        rtlcheck._rename_module(NET, os.path.join(wd, "ref.v"), "ref")
        with open(os.path.join(wd, "vec.hex"), "w") as f:
            for v in vecs:
                f.write(f"{_pack(v):012x}\n")
        open(os.path.join(wd, "tb.v"), "w").write(_build_tb(len(vecs)))
        r = subprocess.run(["iverilog", "-g2005", "-o", "sim", "tb.v", "dut.v", "ref.v"],
                            cwd=wd, capture_output=True, text=True)
        if r.returncode != 0:
            print("iverilog compilation failed:", r.stdout, r.stderr, file=sys.stderr)
            return 1
        r = subprocess.run(["vvp", "sim"], cwd=wd, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    print(f"file under test: {sol}")
    print(f"constructed {len(vecs)} vectors where \"the two sides of the comparison are exactly equal\"")
    print(out)
    # ⚠ if the result line isn't captured, treat it as a failure -- don't print a report that looks clean but is actually empty --
    # the first version of this tool almost fooled everyone this way: all 30 cases threw exceptions yet it still printed "(none)".
    if "ERRORS 0 XHITS 0" not in out:
        print("⚠ there were mismatches or x/z, or it didn't finish running at all", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
