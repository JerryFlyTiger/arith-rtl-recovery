"""Fallback: carries the fanin cone of an unsolved output word into the output file as-is.

For a word that no family managed to solve, correctness outranks everything else — the corresponding
subset of the gate-level netlist is rewritten verbatim as primitive gate instantiations, cost = gate
count, reduction 0%, but equivalence is guaranteed.

Handling of intermediate signals shared across words: `cone_nets`'s backward walk stops as soon as it
reaches a "primary output bit of a word already solved by the linear family" and does not walk back
through its original gates (those gates have already been replaced by an assign; pulling them in again
would make the same net driven by both sides at once). Everywhere else it walks back following
`Netlist.drv`.
"""


def cone_nets(nl, roots, stop_bits):
    """Walks backward from roots (a set of net names) along `nl.drv`, returning every net name visited
    (including the roots).

    Nets in stop_bits are treated as leaves: added to the returned set, but their driver is not
    expanded further (it's already driven by an assign from another family). roots and stop_bits are
    guaranteed disjoint, so this rule can never wrongly cut off a root itself.
    """
    seen = set()
    stack = list(roots)
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        if n in stop_bits:
            continue
        if n not in nl.drv:
            continue
        for i in nl.drv[n][1]:
            if i not in seen:
                stack.append(i)
    return seen


def cone_gate_count(nl, roots):
    """The gate count within a single word's own cone (regardless of whether other words share it or
    have solved it), used purely to compare cost against a linear candidate."""
    nets = cone_nets(nl, roots, stop_bits=frozenset())
    return sum(1 for g in nl.gates if g[1] in nets)


def build_fallback_body(nl, fallback_roots, solved_roots):
    """Returns (wire_decl_lines, gates), covering the union cone of all fallback_roots,
    truncating the backward walk at solved_roots (output bits of words already solved).

    Call this exactly once, covering all fallback words in "this batch" — do not call it once per
    word, or shared intermediate signals and gates will get declared/instantiated twice. gates is a
    list of (op, out, ins), handed to `render_gates` to turn into text.
    """
    if not fallback_roots:
        return [], []
    nets = cone_nets(nl, fallback_roots, stop_bits=solved_roots)
    gate_out_names = {g[1] for g in nl.gates if g[1] in nets and g[1] not in solved_roots}
    gates = [g for g in nl.gates if g[1] in gate_out_names]

    output_bits = set(nl.bits(nl.outputs))
    input_bits = set(nl.bits(nl.inputs))
    consts = {"1'b0", "1'b1"}
    wire_names = sorted(
        n for n in gate_out_names
        if n not in output_bits and n not in input_bits and n not in consts
    )
    wire_lines = [f"  wire {n};" for n in wire_names]
    return wire_lines, gates


def render_gates(gates):
    """Turns a list of (op, out, ins) gates into primitive gate instantiation text, verbatim.

    Netlist currently doesn't retain the original instance names (it only stores (op, out, ins)), so
    an instance name is minted here as `<op>_<legal identifier for out>` — guaranteed unique within
    the same module, because every net has exactly one driver and out values are pairwise distinct.
    """
    lines = []
    for op, o, ins in gates:
        inst = "u_" + o.replace("[", "_").replace("]", "")
        lines.append(f"  {op} {inst}({o}, {', '.join(ins)});")
    return lines
