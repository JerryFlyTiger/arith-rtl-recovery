"""Uses Icarus Verilog to run `solutions/testNN.v` and the original gate-level netlist side by
side in the same testbench.

Why this layer is needed: every prior verification was "Python model vs. netlist"; the step in
between -- **whether the Verilog text I actually wrote truly implements that Python model** -- had
never been executed, and relied purely on reasoning about Verilog's width/signedness rules. And
that's exactly where bugs love to hide:

- `<<`'s left operand width is determined by context (test08's `in1 << in4` only works correctly
  because of this)
- `**` under a wide context is not first truncated to the operand width (test06 / test11)
- if an addition sequence contains even **one** unsigned operand, the whole expression becomes
  unsigned, and sign extension of signed terms fails (test22 mixes signed and unsigned products,
  which only works via a `wire signed` intermediate)

This tool renames the two modules, instantiates them both together, feeds them identical stimulus,
and compares vector by vector, so it doesn't depend on any Python model.

    python3 tools/rtlcheck.py 22            # a single case
    python3 tools/rtlcheck.py               # every case with a solution
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "solver"))
from netlist import Netlist  # noqa: E402


def _rename_module(src, dst, newname):
    text = open(src).read()
    text, n = re.subn(r"\bmodule\s+top\s*\(", f"module {newname}(", text, count=1)
    if n != 1:
        # if the name isn't changed, iverilog reports "can't find dut/ref" at the link stage, a message far removed from the root cause.
        # for instance a form like `module top #(parameter ...) (` isn't picked up. Stated explicitly here.
        raise RuntimeError(f"{src}: `module top(` declaration not found, can't rename to {newname}")
    open(dst, "w").write(text)


def _decl(width, name, kind):
    return f"  {kind} {f'[{width-1}:0] ' if width > 1 else ''}{name};"


# ⚠ Every random call must carry a `seed`. Argument-less `$random` uses the simulator's **global**
# generator,
# unaffected by the `$random(seed)` seeding done once inside `initial` -- measured that iverilog produces
# an identical byte-for-byte sequence (12153524, c0895e81, 8484d609) for seed=1/7/4242. Before 2026-08-22, this tool's `seed`
# the seed parameter has no effect at all, and every recorded "three independent seeds" run is actually the same set of vectors run three times.
RND = "$random(seed)"


def _rand_expr(width):
    """Assembles a width-wide random-value expression, weighted toward boundary values (all 0 / all 1
    / only the top bit).

    When `narrow` is true, it instead emits only small values via `$random & nmask`. Reason: **purely
    random wide-word vectors can't tell `>` apart from `>=`** -- the two only differ when both sides
    are exactly equal, and the probability of a wide random collision approaches 0. Measured: changing
    test28's `sC > sB` to `sC >= sB`, 5000 purely random vectors produced 0 mismatches (completely
    missed).

    `nmask` rotates between 3/15/63/255 for each vector, covering both kinds of gaps: mask 3 makes
    intermediate values like sums of squares collide frequently (catching `>` vs `>=`), while masks
    15/63/255 are needed to clear thresholds with constants (test17's `d2 > 7` needs
    `in2 - in1 == 9`, which is never reached if the value range is only 0..3).
    """
    chunks = [RND for _ in range((width + 31) // 32)]
    rnd = f"{{{', '.join(chunks)}}}" if len(chunks) > 1 else RND
    allone = f"{{{width}{{1'b1}}}}" if width > 1 else "1'b1"
    msb = f"{{1'b1, {width - 1}'b0}}" if width > 1 else "1'b1"
    wide = (
        f"(({RND} % 8) == 0) ? {width}'d0 : "
        f"(({RND} % 8) == 1) ? {allone} : "
        f"(({RND} % 8) == 2) ? {msb} : "
        f"(({RND} % 8) == 3) ? {width}'d1 : {rnd}"
    )
    return f"narrow ? ({RND} & nmask) : ({wide})"


def build_tb(nl, nvec, seed):
    ins, outs = nl.inputs, nl.outputs
    L = ["`timescale 1ns/1ps", "module tb;",
         "  integer i, errors, xhits, seed;", "  reg narrow;", "  reg [31:0] nmask;"]
    for nm, w in ins:
        L.append(_decl(w, nm, "reg"))
    for nm, w in outs:
        L.append(_decl(w, "d_" + nm, "wire"))
        L.append(_decl(w, "r_" + nm, "wire"))

    conn = ", ".join([f".{nm}({nm})" for nm, _ in ins])
    dconn = ", ".join([f".{nm}(d_{nm})" for nm, _ in outs])
    rconn = ", ".join([f".{nm}(r_{nm})" for nm, _ in outs])
    L.append(f"  dut  u_d({conn}, {dconn});")
    L.append(f"  ref  u_r({conn}, {rconn});")

    L += ["  initial begin", "    errors = 0;", "    xhits = 0;", f"    seed = {seed};",
          "    i = $random(seed);", f"    for (i = 0; i < {nvec}; i = i + 1) begin"]
    L.append(f"      narrow = (({RND} % 4) == 0);   // one quarter of the vectors take the small-value range, to force out equality boundaries")
    L.append(f"      nmask  = 32'd3 << (4 * ({RND} % 3)); nmask = nmask | (nmask - 1);")
    for nm, w in ins:
        L.append(f"      {nm} = {_rand_expr(w)};")
    L.append("      #1;")
    # `!==` treats both sides as equal when "both sides are x at the same time", which would be a silent miss. The gold-standard netlist is pure combinational logic,
    # every input is fully driven, so x should never appear at all; if it does, it means this comparison's premise is broken and needs to be flagged loudly.
    xcond = " || ".join([f"(^r_{nm} === 1'bx) || (^d_{nm} === 1'bx)" for nm, _ in outs])
    L.append(f"      if ({xcond}) xhits = xhits + 1;")
    cond = " || ".join([f"(d_{nm} !== r_{nm})" for nm, _ in outs])
    L.append(f"      if ({cond}) begin")
    L.append("        errors = errors + 1;")
    L.append("        if (errors <= 3) begin")
    fmt = " ".join([f"{nm}=%h" for nm, _ in ins])
    args = ", ".join([nm for nm, _ in ins])
    L.append(f'          $display("  MISMATCH i=%0d {fmt}", i, {args});')
    for nm, _ in outs:
        L.append(f'          $display("    {nm}: rtl=%h netlist=%h", d_{nm}, r_{nm});')
    L += ["        end", "      end", "    end",
          '    $display("VECTORS %0d ERRORS %0d XHITS %0d", i, errors, xhits);',
          "    $finish;", "  end", "endmodule"]
    return "\n".join(L) + "\n"


def check(case, nvec=20000, seed=1, workdir=None, sol=None, net=None):
    """Uses sol if it's given (e.g. the solver's output), otherwise falls back to the default
    solutions/testNN.v. Uses net if it's given (e.g. a synth case's top_primitive.v), otherwise falls
    back to the default testcase/<tag>/top_primitive.v."""
    m = re.fullmatch(r"(?:test)?0*(\d{1,2})", str(case).strip())
    if m:
        tag = f"test{int(m.group(1)):02d}"
    elif net is not None:
        # a synth case (e.g. "synth01") isn't bound by the testNN numbering rule, but since the caller has already
        # when net is explicitly given, there's no need to guess a number -- the case string is used directly as the tag (workdir/messages).
        tag = str(case).strip()
    else:
        return False, f"{case}: unrecognized case number (give 5, 05, or test05)"
    sol = sol or os.path.join(ROOT, "solutions", f"{tag}.v")
    net = net or os.path.join(ROOT, "testcase", tag, "top_primitive.v")
    if not os.path.exists(sol):
        return None, f"{tag}: no solution, skipped"

    wd = workdir or os.path.join(ROOT, ".rtlcheck", tag)
    os.makedirs(wd, exist_ok=True)
    # a failure during the preparation stage must be folded into a FAIL for this case, not allowed to let the exception blow through the batch loop --
    # otherwise the cases that come later would never be checked at all, and the output would just look like "it stopped partway through".
    try:
        _rename_module(sol, os.path.join(wd, "dut.v"), "dut")
        _rename_module(net, os.path.join(wd, "ref.v"), "ref")
        nl = Netlist(net)
        open(os.path.join(wd, "tb.v"), "w").write(build_tb(nl, nvec, seed))
    except Exception as e:
        return False, f"{tag}: failed to prepare the testbench: {type(e).__name__}: {e}"

    r = subprocess.run(
        ["iverilog", "-g2005", "-o", "sim", "tb.v", "dut.v", "ref.v"],
        cwd=wd, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False, f"{tag}: compilation failed\n{r.stdout}{r.stderr}"
    r = subprocess.run(["vvp", "sim"], cwd=wd, capture_output=True, text=True)
    out = r.stdout + r.stderr
    m = re.search(r"VECTORS (\d+) ERRORS (\d+) XHITS (\d+)", out)
    if not m:
        return False, f"{tag}: did not finish running\n{out[-2000:]}"
    n, e, x = (int(g) for g in m.groups())
    ok = e == 0 and x == 0
    msg = f"{tag}: {n} vectors, {e} mismatch"
    if x:
        msg += f", and {x} additional vector(s) produced x/z (`!==` misjudges equality when both sides are x at the same time; not trustworthy)"
    if not ok:
        msg += "\n" + out[:1500]
    return ok, msg


if __name__ == "__main__":
    args = sys.argv[1:]
    cases = args or sorted(
        f[4:6] for f in os.listdir(os.path.join(ROOT, "solutions")) if f.endswith(".v")
    )
    bad = 0
    for c in cases:
        ok, msg = check(c)
        print(("  OK  " if ok else ("SKIP  " if ok is None else "FAIL  ")) + msg, flush=True)
        if ok is False:
            bad += 1
    sys.exit(1 if bad else 0)
