"""Counts operator cost directly from the text of the solution Verilog and reconciles it against
the README table.

cost is the score for this problem itself (`point = min(cost over all teams) / own cost`), so a
mismatch between a "hand-computed cost comment" and "the expression actually written" is a bug that
directly costs points. This tool exists to replace hand computation with machine computation.

Billing follows the problem's Appendix A / B:

    + - * / ** %  > >= < <=  == != === !==  ! && ||  << >> <<< >>>  ?:    1 each
    []  bit-select / part-select                                          1 per occurrence
    {}  concatenation                                                     1 per symbol
    ~ & | ^ used as **bitwise ops**                                       1 per bit (expensive)
    & | ^ ~& ~| ~^ used as **reduction**                                  1 each
    if 1, case 2 per item
    wire / assign / signed / input / output / for                         0

Two interpretation calls, written down here so we don't forget why later:

1. **Negative constants aren't billed.** The `-4` in `d2 < -4` is a literal constant, not
   subtraction; but `-X` (X a wire) is the negation operator and costs 1. Rule: a unary-position `-`
   followed by a number is a constant, followed by an identifier is an operator.
2. **`$signed()` / `$unsigned()` cost 0.** Appendix A's operator table doesn't list system
   functions; Appendix B explicitly lists the `signed` keyword as cost 0. (You could also just
   declare `wire signed` if you want to take the easy way out — Appendix B spells that path out more
   clearly.)

⚠ **`{}` counting was only added on 2026-08-21; before that it was a load-bearing
under-count gap.** The original comment said "none of the existing 27 solutions use concatenation,
add it when one actually does" — then M6a's `(an undisclosed family)` started using `{12'b0, x}` for
zero-extension, **19 of them in a single test24 output**, and one of the reasons that rendering
approach was chosen at the time was precisely "concatenation is billed at 0, so it's free to make
explicit". That "0" was this tool simply not having it implemented, not the problem saying it's
free — Appendix A explicitly bills `{}`.

**Project policy: avoid `{}` wherever possible.** When zero-extension is needed, use `$unsigned(x)`
(a system function, not in Appendix A's operator table, interpreted the same way as `$signed()`,
already relied on by 29 solutions); when bits need to be padded before converting to signed, declare
a widened wire. M6a's rendering has been changed from `{N'b0, x}` to `$unsigned(x)` accordingly —
same cost, same semantics, but no longer exposed to `{}`'s billing rule.

⚠ **The exact definition of "1 per symbol" is implemented by this tool as "the number of
elements in the concatenation"** (`{12'b0, x}` counts as 2). The problem statement doesn't spell
this out further; this is an interpretation. Because project policy avoids using `{}`, this number is
rarely actually exercised, but **it's recorded here so the next person doesn't assume it's 0**.

    python3 tools/costcount.py            # everything, reconciled against the README
    python3 tools/costcount.py 22         # a single case, printing an operator breakdown
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MULTI = ["<<<", ">>>", "===", "!==", "**", "<<", ">>", ">=", "<=", "==", "!=",
         "&&", "||", "~&", "~|", "~^", "^~"]
SINGLE = ["?", "+", "*", "/", "%", ">", "<", "!"]

# primitive gate instantiation: `and u_w1(w1, a, b);`. **Each instance costs 1.**
# this was added for the solver's fallback output: fallback carries an unsolved fanin cone over as gate instances verbatim,
# and a gate instance contains not a single Appendix A operator -- counting only operators would make a whole fallback output come out as
# cost 0 -- that would mean "handing the netlist back unchanged" is a perfect-score solution (point = min(cost over all teams) / own cost),
# but it's actually the worst possible solution. README's reduction column (1 - cost/gates) already relies
# on the premise that "fallback cost == gate count", folded into the cost tool itself here.
# none of the manual solutions under solutions/ contain any gate instances, so this doesn't change existing reconciliation results.
# instance names are **optional**: Verilog allows unnamed gate instances (`or (out, a, b);`),
# writing `\w+` would miss them and count those lines as 0 -- exactly the leak this rule is meant to plug.
# a mandatory space is required after the keyword, so an identifier starting with a keyword like `buffer_valid = ...` won't be misjudged.
GATE_INST = re.compile(
    r"^[ \t]*(and|or|nand|nor|not|buf|xor|xnor)\s+\w*\s*\(", re.M)


def _strip_comments(s):
    s = re.sub(r"//[^\n]*", "", s)
    return re.sub(r"/\*.*?\*/", "", s, flags=re.S)


def cost_of(path):
    """Returns (total cost, {operator: count}). Reads the file, then delegates to `cost_of_text`."""
    return cost_of_text(open(path).read())


def cost_of_text(text):
    """Returns (total cost, {operator: count}). Takes raw Verilog text (does not read a file)."""
    s = _strip_comments(text)
    s = s[s.index("output"):]                     # skip the module header and input declarations
    stmts = [t for t in s.split(";")
             if "=" in t and "endmodule" not in t.split("=")[0]]

    total, detail = 0, {}

    def bump(k, n=1):
        nonlocal total
        detail[k] = detail.get(k, 0) + n
        total += n

    n_gates = len(GATE_INST.findall(s))
    if n_gates:
        bump("gate instance", n_gates)

    for st in stmts:
        rhs = st.split("=", 1)[1]
        rhs = re.sub(r"\b\d+'s?[bdh][0-9a-fA-FxzXZ_]+", " K ", rhs)   # sized literal
                                                                        # ('s? recognizes a signed sized literal, e.g. 36'sd123)
        rhs = re.sub(r"\$signed|\$unsigned", " ", rhs)

        # {} concatenation: Appendix A explicitly bills it (1 per symbol).
        # implemented here as "the number of elements in the concatenation" -- see the module docstring for this interpretation.
        # ⚠ This section must run **after** sized literals have been replaced with ` K `, otherwise `{12'b0, x}`
        # the `'b0` inside would interfere with element splitting.
        concat_syms = 0
        for m in re.finditer(r"\{([^{}]*)\}", rhs):
            inner = m.group(1).strip()
            if inner:
                concat_syms += len([x for x in inner.split(",") if x.strip()])
        if concat_syms:
            bump("{}", concat_syms)
        rhs = re.sub(r"\{[^{}]*\}", " CAT ", rhs)

        n = len(re.findall(r"\w\s*\[", rhs))       # bit-select (a declared width isn't on the RHS, so it won't be miscounted)
        if n:
            bump("[]", n)
        rhs = re.sub(r"\[[^\]]*\]", " ", rhs)

        out, i = [], 0
        while i < len(rhs):
            c = rhs[i]
            if c == "-":
                prev = next((x for x in reversed(out) if not x.isspace()), None)
                unary = prev is None or prev in "(?:,+-*/%<>=&|^!"
                nxt = rhs[i + 1:].lstrip()[:1]
                if not (unary and (nxt.isdigit() or nxt == "K")):
                    bump("-")                      # subtraction or negation; negative constants are skipped
                out.append(" ")
                i += 1
                continue
            out.append(c)
            i += 1
        rhs = "".join(out)

        for t in MULTI:
            n = rhs.count(t)
            if n:
                bump(t, n)
                rhs = rhs.replace(t, " ")

        for t in "&|^":                            # reduction and bitwise operations must be billed separately
            for m in re.finditer(re.escape(t), rhs):
                prev = next((x for x in reversed(rhs[:m.start()]) if not x.isspace()), None)
                bump(("reduction " if (prev is None or prev in "(?:,") else "bitwise ") + t)
            rhs = rhs.replace(t, " ")

        for t in SINGLE:
            n = rhs.count(t)
            if n:
                bump(t, n)
                rhs = rhs.replace(t, " ")

    return total, detail


def readme_costs():
    txt = open(os.path.join(ROOT, "README.md")).read()
    return {m.group(1): (int(m.group(2).replace(",", "")), int(m.group(3)))
            for m in re.finditer(
                r"^\| test(\d\d) \| ([\d,]+) \|.*?\| (\d+) \| [\d.]+% \|", txt, re.M)}


if __name__ == "__main__":
    want = readme_costs()
    sols = os.path.join(ROOT, "solutions")
    args = sys.argv[1:]
    cases = ([f"{int(a):02d}" for a in args] if args
             else sorted(f[4:6] for f in os.listdir(sols) if f.endswith(".v")))
    bad = 0
    for c in cases:
        path = os.path.join(sols, f"test{c}.v")
        if not os.path.exists(path):
            print(f"test{c}: no solution")
            continue
        total, detail = cost_of(path)
        g, w = want.get(c, (None, None))
        mark = "  " if w == total else "!!"
        if w != total:
            bad += 1
        red = f"  reduction {100 * (1 - total / g):.2f}%" if g else ""
        print(f"{mark} test{c}: cost {total:3d}  README {w}{red}")
        if args or w != total:
            print("     " + "  ".join(f"{k}×{v}" for k, v in sorted(detail.items())))
    if not args:
        print(f"\ndoesn't match README: {bad}")
    sys.exit(1 if bad else 0)
