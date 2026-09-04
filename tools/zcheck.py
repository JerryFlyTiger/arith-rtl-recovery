"""Formal equivalence verification between a gate-level netlist and an arithmetic spec, using z3.

Requires z3's Python bindings: pip install z3-solver

Two ways to use it:

1. `check_bitwise()` -- builds a miter and solves it output bit by output bit. Multipliers hit an
   exponential wall; the value of this function is that it **measures which bit the wall falls at**,
   rather than the whole thing timing out with nothing learned.

2. The role of `check_exhaustive()` is played by netlist.Netlist.simulate: when some output's fanin
   cone is small enough (computed via support analysis), exhaustively enumerating that cone's input
   space is itself a complete proof.

Measured lessons (test15, a 13x18 multiplier): when case-splitting on the input, using a solver
assumption (`s.check(x == c)`) leaves bvmul in symbolic form; switching to `z3.substitute` to
actually substitute in the constant and then `simplify` lets the multiplication degrade into
shift-add, about 43x faster. But that only helps for constants with few set bits; dense constants
still explode, so case-splitting can't rescue multiplier equivalence.
"""
import os
import sys
import time

import z3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "solver"))
from netlist import Netlist  # noqa: E402


def build(nl: Netlist):
    """Encodes the netlist into z3.

    Returns (pi_bv, wire_bool): pi_bv has one BitVec per input port, wire_bool has one z3 Bool
    expression per wire.
    """
    pi_bv, env = {}, {}
    for nm, w in nl.inputs:
        bv = z3.BitVec(nm, w)
        pi_bv[nm] = bv
        for i in range(w):
            key = nm if w == 1 else f"{nm}[{i}]"
            env[key] = z3.Extract(i, i, bv) == z3.BitVecVal(1, 1)
    env["1'b0"], env["1'b1"] = z3.BoolVal(False), z3.BoolVal(True)

    pend = list(nl.gates)
    while pend:
        rest, prog = [], False
        for op, o, ins in pend:
            if all(i in env for i in ins):
                v = [env[i] for i in ins]
                if   op == "and":  r = z3.And(*v)
                elif op == "or":   r = z3.Or(*v)
                elif op == "not":  r = z3.Not(v[0])
                elif op == "buf":  r = v[0]
                elif op == "nand": r = z3.Not(z3.And(*v))
                elif op == "nor":  r = z3.Not(z3.Or(*v))
                elif op == "xor":  r = z3.Xor(v[0], v[1])
                elif op == "xnor": r = z3.Not(z3.Xor(v[0], v[1]))
                else: raise ValueError(f"unknown gate: {op}")
                env[o] = z3.simplify(r)
                prog = True
            else:
                rest.append((op, o, ins))
        if not prog:
            raise RuntimeError(f"undriven wires: {rest[:2]!r}")
        pend = rest
    return pi_bv, env


def bit_of(bv_expr, k):
    return z3.Extract(k, k, bv_expr) == z3.BitVecVal(1, 1)


def check_bitwise(nl: Netlist, spec_fn, out_name, out_width, timeout_ms=60000, verbose=True):
    """Proves, bit by bit, that the netlist's out_name equals the corresponding bit of spec_fn(pi).

    spec_fn(pi_bv) must return a BitVec at least out_width wide.
    Returns (proved_bits, wall); wall is the first unproved bit, or None if everything was proved.
    """
    pi, env = build(nl)
    spec = spec_fn(pi)
    proved, wall = 0, None
    for k in range(out_width):
        wire = out_name if out_width == 1 else f"{out_name}[{k}]"
        s = z3.Solver()
        s.set("timeout", timeout_ms)
        s.add(z3.Xor(env[wire], bit_of(spec, k)))   # miter: looking for a counterexample
        t0 = time.time()
        r = s.check()
        dt = time.time() - t0
        if r == z3.unsat:
            proved += 1
            if verbose:
                print(f"  bit {k:2}  unsat (proved) {dt:6.2f}s", flush=True)
        else:
            tag = "SAT <- counterexample!" if r == z3.sat else "unknown (timed out)"
            if verbose:
                print(f"  bit {k:2}  {tag} {dt:6.2f}s", flush=True)
                if r == z3.sat:
                    print("     ", s.model(), flush=True)
            wall = k
            break
    return proved, wall
