"""A helper tool for reverse-inferring arithmetic/comparison operations from a netlist.

Usage (PYTHONPATH=tools):

    from netlist import Netlist
    from classify import survey, classify_bit_output

    survey()                      # scans every testcase, prints cone size and exhaustibility classification
    classify_bit_output(n, "out1", [("in1","in2")])   # classifies a single 1-bit output

**Boundary sampling is required, not optional.** Purely random sampling over a wide word can't
distinguish comparison offsets: `in1 < in3` and `in1 < in3 - 9` only differ with probability 9 in
2^32, so 4000 random vectors will never land in that interval, and the wrong hypothesis would pass
completely. So every candidate operand pair must also generate vectors where "the two differ by
\u00b1k". See README's test17 for a concrete instance.

Likewise, the candidate set must cover **mixed signedness** (one side zero-extended unsigned, the
other side signed) — test16 is exactly that: in1 is 34-bit unsigned, in2/in3/in4 are 35-bit signed;
trying only "both sides the same signedness" would miss it entirely.
"""
import glob
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "solver"))
from netlist import Netlist  # noqa: E402

OPS = [("<",  lambda x, y: x < y),   ("<=", lambda x, y: x <= y),
       (">",  lambda x, y: x > y),   (">=", lambda x, y: x >= y),
       ("==", lambda x, y: x == y),  ("!=", lambda x, y: x != y)]


def signed(v, w):
    """Interprets a w-bit unsigned value as two's complement."""
    v &= (1 << w) - 1
    return v - (1 << w) if v >> (w - 1) else v


def survey(pattern="testcase/test*"):
    """Scans every case, reporting gate count, input width, and the fanin cone size of each output
    word.

    Cone size decides the strategy: if it's small enough, exhaustively enumerate the truth table
    (that's a complete proof); only when it's too large is structural analysis needed.
    """
    rows = []
    for d in sorted(glob.glob(pattern)):
        n = Netlist(os.path.join(d, "top_primitive.v"))
        sup = n.support()
        cones = []
        for nm, w in n.outputs:
            u = set()
            for i in range(w):
                u |= sup[nm if w == 1 else f"{nm}[{i}]"]
            cones.append(len(u))
        ib = sum(w for _, w in n.inputs)
        kind = ("globally exhaustible" if ib <= 24 else
                "exhaustible per cone" if max(cones) <= 24 else "needs structural analysis")
        rows.append(dict(case=os.path.basename(d), gates=len(n.gates), in_bits=ib,
                         min_cone=min(cones), max_cone=max(cones), kind=kind))
    return rows


def make_vectors(n, pairs, fixed=None, n_random=1500, n_boundary=600, spread=8, seed=0):
    """Random vectors plus boundary vectors for each pair. fixed is used to pin down control bits."""
    rng = random.Random(seed)
    W = {nm: w for nm, w in n.inputs}
    fixed = fixed or {}
    vs = []
    for _ in range(n_random):
        v = {nm: rng.getrandbits(w) for nm, w in n.inputs}
        v.update(fixed)
        vs.append(v)
    for a, b in pairs:                       # Every pair matters, not just the first one
        for _ in range(n_boundary):
            v = {nm: rng.getrandbits(w) for nm, w in n.inputs}
            base = rng.getrandbits(min(W[a], W[b]))
            v[a] = base & ((1 << W[a]) - 1)
            v[b] = (base + rng.randint(-spread, spread)) & ((1 << W[b]) - 1)
            v.update(fixed)
            vs.append(v)
    return vs


def classify_bit_output(n, out_name, pairs, fixed=None, ks=range(-70, 71), seed=0):
    """Matches a 1-bit output against the form `A op B + k`, covering unsigned/signed/mixed signedness.

    Returns every description that passes (more than one means sampling isn't enough to distinguish
    them, and spread or n_boundary needs to be increased).
    """
    W = {nm: w for nm, w in n.inputs}
    vs = make_vectors(n, pairs, fixed, seed=seed)
    rs = n.simulate(vs)
    vals = {r[out_name] for r in rs}
    if len(vals) == 1:
        return [f"constant {vals.pop()}"]

    hits = []
    for a, b in pairs:
        for k in ks:
            for opn, f in OPS:
                suffix = f"{k:+d}" if k else ""
                # Three signedness combinations: both unsigned / both signed / mixed (a zero-extended, b signed)
                for tag, fa, fb in (
                        ("",        lambda v: v[a],              lambda v: v[b]),
                        ("$s/$s",   lambda v: signed(v[a], W[a]), lambda v: signed(v[b], W[b])),
                        ("u/$s",    lambda v: v[a],              lambda v: signed(v[b], W[b]))):
                    if all(r[out_name] == int(f(fa(v), fb(v) + k)) for v, r in zip(vs, rs)):
                        hits.append(f"{tag} {a} {opn} {b}{suffix}".strip())
            if hits:
                return hits
    return hits
