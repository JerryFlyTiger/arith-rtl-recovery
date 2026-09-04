"""Uses iverilog to pin down "the Verilog width/signedness facts this project's guards rely on" as
executable assertions.

    python3 tools/probe_signedness.py

## Why this tool is needed

`tools/selftest.py` asserts things like `(an undisclosed family)._expr_signed("a ** 2") is True`
-- that only proves **the implementation matches our understanding**, not that **our understanding
matches Verilog**. The real evidence is an iverilog experiment, and this project has already
committed "the assertion stays, the evidence disappears" four times. This tool gathers the scattered
experiments into a single script in the repo.

The M17 cold read caught three signedness semantic bugs precisely by relying on these facts (one of
them, the `**` one, **is reachable in production**: `render_word` prints exactly `x ** 2` for `sq`/
`pow` terms).

## Discrimination method

Looking directly at `E`'s numeric value can't tell signedness apart -- width gets mixed in. So the
same trick is used every time: treat `E` as an operand of a parent expression (`E + b`, where `b` is
**a narrow signed**), then compare it against "a known-signed reference + b" and "a known-unsigned
reference + b". How `b` gets extended (sign vs. zero) exposes `E`'s signedness.

⚠ Every case also asserts that **the two reference values differ from each other** -- if the
reference values were the same, that case would be a no-op run, telling us nothing. This is the same
rule as this project's "0 counterexamples must be paired with N genuine hits".
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (name, extra declarations, expression under test E, expected signedness)
CASES = [
    ("a ** 2 (the base is signed, the exponent is an unsized constant)", "", "(a ** 2)", "SIGNED"),
    ("u ** 2 (the base is unsigned)", "", "(u ** 2)", "UNSIGNED"),
    ("~a (bitwise NOT follows the operand)", "", "(~a)", "SIGNED"),
    ("a * 4 (an unsized decimal constant is signed)", "", "(a * 4)", "SIGNED"),
    ("a * 4'd4 (a sized literal is unsigned)", "", "(a * 4'd4)", "UNSIGNED"),
    ("a + u (mixing in one unsigned operand makes the whole expression unsigned)", "", "(a + u)", "UNSIGNED"),
    ("(a > u) (the comparison result is 1-bit unsigned)", "", "((a > u) + a)", "UNSIGNED"),
]


def _run(src, tag):
    d = tempfile.mkdtemp(prefix="probe_sgn_")
    open(os.path.join(d, "t.v"), "w").write(src)
    r = subprocess.run(["iverilog", "-g2005", "-o", "s", "t.v"],
                       cwd=d, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("%s compilation failed:\n%s%s" % (tag, r.stdout, r.stderr))
    r = subprocess.run(["vvp", "s"], cwd=d, capture_output=True, text=True)
    return r.stdout


def signedness_of(expr, extra_decl=""):
    """Returns 'SIGNED' / 'UNSIGNED', or raises an exception when it can't be determined (to avoid a
    no-op run).

    Approach: `E + b` is compared against "signed reference + b" and "unsigned reference + b". Both
    references are fed the bit pattern computed from `E` under the **same context width (8 bits)**,
    so all three agree numerically and only differ in signedness.
    """
    src = """module t;
  reg signed [3:0] a, b;
  reg [3:0] u;
  wire [7:0] v8;
  wire signed [7:0] rs8;
  wire [7:0] ru8;
  wire [7:0] e8, refs, refu;
%s
  // ⚠ The reference must always be evaluated at the **same context width** as the expression
  // under test. An earlier version used a 4-bit reference, while `E + b` is actually computed
  // under an 8-bit context, so the two values never matched at all -> 5 of 7 came out
  // "indeterminate".
  assign v8  = %s;           // the value of E under an 8-bit context (bit pattern)
  assign rs8 = v8;           // the same bits, declared signed
  assign ru8 = v8;           // the same bits, declared unsigned
  assign e8   = %s + b;
  assign refs = rs8 + b;
  assign refu = ru8 + b;
  initial begin
    a = -3; u = 4'd13; b = -1; #1;
    $display("REFS %%0d REFU %%0d GOT %%0d", refs, refu, e8);
    $finish;
  end
endmodule
""" % (extra_decl, expr, expr)
    out = _run(src, expr)
    m = re.search(r"REFS (\d+) REFU (\d+) GOT (\d+)", out)
    if not m:
        raise RuntimeError("failed to produce a result: %s\n%s" % (expr, out))
    refs, refu, got = (int(x) for x in m.groups())
    # ⚠ The two references must differ, otherwise this case can't tell anything apart (a no-op run)
    if refs == refu:
        raise AssertionError(
            "Reference values are identical (refs=refu=%d); `%s` can't discriminate signedness -- **this is a no-op run**,"
            "please pick a different set of values that make the MSB 1" % (refs, expr))
    if got == refs:
        return "SIGNED"
    if got == refu:
        return "UNSIGNED"
    raise AssertionError("`%s` could not be determined: got=%d refs=%d refu=%d"
                         % (expr, got, refs, refu))


def main():
    bad = 0
    for name, extra, expr, want in CASES:
        try:
            got = signedness_of(expr, extra)
        except AssertionError as e:
            print("  FAIL %s -> %s" % (name, e))
            bad += 1
            continue
        ok = got == want
        print("  %s %s -> %s" % ("OK  " if ok else "FAIL", name, got))
        if not ok:
            print("       expected %s" % want)
            bad += 1
    print()
    if bad:
        print("Verilog semantic evidence failed: %d case(s)" % bad)
        return 1
    print("All %d Verilog signedness facts were verified live by iverilog and passed." % len(CASES))
    print("These are exactly what this project's signedness guards rely on.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
