"""Recovers the operator graph from wire name prefixes.

When a synthesizer flattens RTL down to gate level, **it often leaves the DesignWare instance name
in the wire prefix**, in the form `<op>_<line>_<col>_n_123`: `sub_32_21`, `mul_84_22`, `lt_42_22`,
`csa_tree_add_117_21_pad_groupi`... `<op>` is the operator from the original RTL, `<line>_<col>` is
its position in the original source.

This beats black-box probing because **it directly gives the arity and operands of each operation**
-- whereas a pairwise-matching probe method can never, in principle, find an operation with three
operands. test13 was stuck for three rounds precisely because one path was mistaken for a two-operand
comparison, when it was actually a three-operand test of `in2` against the midpoint of `in1`/`in7`;
after reading the names instead, a single command revealed "2 subtractions + 2 negations + 3
comparators".

⚠⚠ **This tool's output isn't the complete circuit diagram; there are two known ways it
under-reports** (hit on 2026-08-21 while reverse-engineering test26):

1. **Loose glue gates that fall outside any naming cluster don't show up in any region.** test26's
   group A narrow ports total 10 bits (`in10[0:4]` + `in11[0:4]`), but the named region only picks up
   5 bits (`in10[0]`, `in10[4]`, `in11[1:3]`) -- the other 5 bits aren't in any region's input list,
   yet they genuinely affect the output (measured: the full signed 5 bits of `in11` are in play).
   **Inferring the operand set from the region list alone will miss things.**
2. **`region_of()` only lists loose wires in scan order for a given region, with no guarantee of bit
   significance order.** Reverse-engineering test26's `mul_113_23` output with the wrong order
   produced a completely malformed noise function. Getting the correct order requires following
   `nl.drv` backward to the consuming side's input list.

**So: region names can be used to infer the "kind" and "approximate operands" of an operation, but
not "the complete set of input bits" or "bit order".** test26 has a third pitfall too -- group E's
multiplier has both a different name and a different gate count from the other four groups
(`csa_tree_add_110_49_pad_groupi` at 1540 gates vs `mul_*` at 1142 gates), which fooled two rounds of
reconnaissance into concluding "E has no multiplier", when functional probing proved all five groups
are isomorphic. **Trust functional probing, not the surface consistency of region naming.**

**So: always run this tool first on any new case, and only decide whether to do black-box probing
afterward.** (Not every case has names: out of the 30 measured cases, test16/17/18/20/27/28/29/30
have netlists already stripped clean, and only then do we fall back to support.)

    python3 tools/regions.py 26
"""
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "solver"))
from netlist import Netlist  # noqa: E402

PREFIX = re.compile(r"^(?P<pfx>[A-Za-z_][A-Za-z0-9_]*)_n_\d+$")


def region_of(wire):
    m = PREFIX.match(wire)
    return m.group("pfx") if m else None


def analyse(path):
    """Returns [(region, gate count, external input nets, external output nets)], sorted by region
    name.

    External input = a net consumed by this region but not produced within it (including primary
    inputs). External output = a net produced by this region and consumed outside it (or that is
    directly a primary output).
    """
    nl = Netlist(path)
    pos = {nm if w == 1 else f"{nm}[{i}]"
           for nm, w in nl.outputs for i in range(w)}

    produced_by = {}
    members = defaultdict(list)
    for op, o, ins in nl.gates:
        r = region_of(o)
        produced_by[o] = r
        members[r].append((op, o, ins))

    consumers = defaultdict(set)
    for op, o, ins in nl.gates:
        for i in ins:
            consumers[i].add(region_of(o))

    rows = []
    for r, gates in members.items():
        own = {o for _, o, _ in gates}
        ext_in, ext_out = set(), set()
        for _, o, ins in gates:
            ext_in.update(i for i in ins if i not in own)
            if o in pos or (consumers[o] - {r}):
                ext_out.add(o)
        rows.append((r, len(gates), sorted(ext_in), sorted(ext_out)))
    return sorted(rows, key=lambda t: (t[0] is None, t[0] or ""))


def _group(nets):
    """Compresses a net list into the compact form `port:lo-hi(n)`; non-bus nets are listed as-is."""
    d, loose = defaultdict(list), []
    for x in nets:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]$", x)
        (d[m.group(1)].append(int(m.group(2))) if m else loose.append(x))
    parts = [f"{k}:{min(v)}-{max(v)}({len(v)})" for k, v in sorted(d.items())]
    return " ".join(parts + (["| " + " ".join(loose[:8])
                              + (f" …+{len(loose)-8}" if len(loose) > 8 else "")]
                             if loose else []))


if __name__ == "__main__":
    for case in sys.argv[1:] or [f"{i:02d}" for i in range(1, 31)]:
        tag = f"test{int(case):02d}"
        path = os.path.join(ROOT, "testcase", tag, "top_primitive.v")
        if not os.path.exists(path):
            continue
        rows = analyse(path)
        named = [r for r in rows if r[0]]
        print(f"===== {tag}: {len(named)} named region(s)")
        if not named:
            print("  (the netlist has had its names stripped; falling back to support analysis)")
        for r, ng, ei, eo in named:
            print(f"  {r}  gates={ng}")
            print(f"    in : {_group(ei)}")
            print(f"    out: {_group(eo)}")
