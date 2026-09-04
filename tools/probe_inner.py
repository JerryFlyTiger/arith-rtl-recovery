"""Observes **any internal wire** of a gate-level netlist -- `simulate()` can't do this, but
`eval()` always could.

## Why this tool exists

The README once recorded "`Netlist.simulate()` has no API to query the simulated value of an internal
wire", and on that basis estimated milestone 3c's prerequisite work as "needs `netlist.py` changed
first". **That statement was wrong**: `simulate()` (`solver/netlist.py:59-80`) simply discards the
return value of `eval()`, and `eval(assign, mask)` itself already returns the complete `env`,
containing bit-parallel simulated values for every internal net. The only gap was "the caller has to
assemble that `assign` dict itself".

This tool is the **executable evidence** for that correction -- no family under `solver/` uses this
capability (M3c's second round switched to the "output as comparison oracle" technique, never
touching internal wires), so without this tool, the README's correction would just be an assertion
with no code-backed evidence. This project has already been bitten three times by "the assertion
stays, the evidence disappears"; it shouldn't happen a fourth time.

## Usage

    python3 tools/probe_inner.py test28

Prints three sections: total internal net count, results of grouping by support, and the support
ports of each output.

## Why grouping by support is worth running first

**Grouping itself often already lays the circuit structure bare.** test28's measured results
directly reproduce the first step of the manual-solution derivation -- the six 3-port groups are the
six triples (`normsq`'s operands), the six 6-port groups are the six comparators, and each one's port
set is exactly the union of two triples:

    3 ports ('in1','in2','in3'): 339 entries      6 ports ('in2','in3','in4','in5','in6','in7'): 93 entries
    3 ports ('in2','in3','in4'): 339 entries      ...

⚠ **The grouping key should be "ports touched by support", not "ports with full coverage".**
Low bits only depend on the low bits of each port (`normsq`'s bit 0 only touches
`in1[0],in2[0],in3[0]`); a full-coverage criterion would compute their key as `()` and dump them all
into the wrong group -- M3c's first round measured that grouping by full coverage misses 11 of 24
low bits.
"""
import collections
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "solver"))
from netlist import Netlist  # noqa: E402


def eval_inner(nl, vectors):
    """Runs `vectors` and returns `(env, mask)` -- `env` contains bit-parallel values for **all**
    internal nets.

    The input encoding in the first half is exactly the same as `Netlist.simulate()` (one column per
    primary input bit, the k-th bit corresponding to the k-th vector); the only difference is that
    `eval()`'s return value isn't discarded. To look up a net's value under the k-th vector:
    `(env[wire] >> k) & 1`.
    """
    N = len(vectors)
    mask = (1 << N) - 1
    assign = {}
    for n, w in nl.inputs:
        for i in range(w):
            col = 0
            for k, vec in enumerate(vectors):
                if (vec[n] >> i) & 1:
                    col |= 1 << k
            assign[n if w == 1 else f"{n}[{i}]"] = col
    return nl.eval(assign, mask), mask


def support_map(nl):
    """The support of every net (including internal ones), traced back to primary input bits.
    Self-memoizing.

    `Netlist.support()` only computes bits of declared outputs, and **has no cache** -- grouping
    internal nets requires computing this separately.
    """
    pin = set(nl.bits(nl.inputs))
    memo = {}
    sys.setrecursionlimit(200000)

    def sup(n):
        if n in pin:
            return frozenset([n])
        if n in memo:
            return memo[n]
        if n not in nl.drv:
            return frozenset()
        memo[n] = frozenset()          # cycle guard
        s = set()
        for i in nl.drv[n][1]:
            s |= sup(i)
        memo[n] = frozenset(s)
        return memo[n]

    return sup


def touched_ports(bits):
    """Support bit set -> tuple of port names it touches (**no "full coverage" filtering**, see the
    module comment)."""
    return tuple(sorted(collections.Counter(b.split("[")[0] for b in bits).keys()))


def main(case):
    nl = Netlist(os.path.join(ROOT, "testcase", case, "top_primitive.v"))
    print(f"== {case}: {len(nl.gates)} gates, inputs {len(nl.inputs)}, outputs {len(nl.outputs)}")

    rng = random.Random(20260817)
    vectors = [{n: rng.randrange(1 << w) for n, w in nl.inputs} for _ in range(200)]
    env, _mask = eval_inner(nl, vectors)
    pin = set(nl.bits(nl.inputs))
    inner = [w for w in env if w not in pin and not w.startswith("1'b")]
    print(f"   total net count in env {len(env)}, internal nets after subtracting primary input bits {len(inner)}")

    sup = support_map(nl)
    groups = collections.defaultdict(list)
    for w in nl.drv:
        groups[touched_ports(sup(w))].append(w)

    print(f"   internal nets grouped by 'the set of ports touched by support': {len(groups)} groups")
    for k, v in sorted(groups.items(), key=lambda kv: (len(kv[0]), -len(kv[1]))):
        print(f"     {len(k)} ports {k}: {len(v)} entries")

    print("   support ports of each output:")
    for n, _w in nl.outputs:
        print(f"     {n}: {touched_ports(sup(n))}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "test28")
