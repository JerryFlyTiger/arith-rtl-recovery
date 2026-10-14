"""solver CLI: recovers arithmetic RTL from a gate-level netlist.

    python3 solver/main.py -input <netlist.v> -output <rtl.v>

Flow: parse -> try the linear family for each output word -> verify by simulation -> take the lowest cost (linear
vs. fallback) -> emit. Uses no threading / multiprocessing / subprocess, only the standard library; the outer layer
has a try/except fallback: any extraction failure or exception falls back to "rewrite the whole netlist as-is" —
it must never crash, and must never emit non-equivalent RTL.
"""
import argparse
import os
import random
import re
import sys
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from netlist import Netlist  # noqa: E402
from model import render_word, cost_of_word_assign, signed_wire_name  # noqa: E402
from share import fresh_prefix, SharePool  # noqa: E402
import linear  # noqa: E402
import quadratic  # noqa: E402
# M21: compare/mux/(undisclosed family)/(undisclosed family)/(undisclosed family)/(undisclosed family)/(undisclosed family)/(undisclosed family)/(undisclosed family)/(undisclosed family)/
# (undisclosed family)/(undisclosed family) are no longer directly `import`ed by `main.py` — `decide()` goes entirely through
# dynamically loaded by `registry.discover()` (see the `FAMILY` dict at the end of each family file); any family's file
# When missing from the filesystem it's just one fewer candidate, other families unaffected. `linear`/`quadratic` are the exception:
# outside the word loop, `decide()` calls `linear.build_weight_vectors`/
# `quadratic.build_plan` builds `weights_by_word`/`quad_plan` (`quad_plan` also needs to
# fed to `(undisclosed family)`/`(undisclosed family)`'s `build_context`), so these two families are structurally inseparable
# base module, at the same layer as `model`/`netlist`/`share`.
try:
    import polycse  # noqa: E402
except ImportError:
    polycse = None
try:
    import exprcse  # noqa: E402
except ImportError:
    exprcse = None
import fallback  # noqa: E402
import registry  # noqa: E402
from costcount import cost_of_text  # noqa: E402

VALIDATION_SEED = 20260812
VALIDATION_VECTORS = 512
SMALL_MASKS = (3, 15, 63, 255)


def gen_validation_vectors(nl, seed=VALIDATION_SEED, total=VALIDATION_VECTORS):
    """512 shared verification vectors: all-0, all-1, each port individually all-1/individually MSB/individually
    value 1, the rest random (1/4 of them stick to a narrow value range, to flush out equality boundaries in the
    comparison family — the linear family in this milestone doesn't need it, but the verifier has to get this right
    once since later families depend on it)."""
    rng = random.Random(seed)
    zero = {n: 0 for n, _ in nl.inputs}
    vecs = [dict(zero)]
    vecs.append({n: (1 << w) - 1 for n, w in nl.inputs})
    for n, w in nl.inputs:
        v = dict(zero)
        v[n] = (1 << w) - 1
        vecs.append(v)
    for n, w in nl.inputs:
        v = dict(zero)
        v[n] = 1 << (w - 1)
        vecs.append(v)
    for n, w in nl.inputs:
        v = dict(zero)
        v[n] = 1
        vecs.append(v)
    while len(vecs) < total:
        if rng.random() < 0.25:
            mask = rng.choice(SMALL_MASKS)
            v = {n: rng.getrandbits(w) & mask for n, w in nl.inputs}
        else:
            v = {n: rng.getrandbits(w) for n, w in nl.inputs}
        vecs.append(v)
    return vecs[:total]


def extract_module_text(src):
    """Copies the port order and input/output declaration lines back into the header verbatim — do not reorder
    them yourself; this guarantees the port names/widths/order exactly match the original netlist (`rtlcheck.py`
    relies on this for instantiation)."""
    m = re.search(r"module\s+\w+\s*\(([^)]*)\)\s*;", src)
    if not m:
        raise RuntimeError("no module declaration found")
    header_ports = m.group(1)
    decl_lines = [m2.group(0).rstrip()
                  for m2 in re.finditer(r"^[ \t]*(input|output)\b[^;]*;", src, re.M)]
    return header_ports, decl_lines


_DISCOVERED_FAMILIES = registry.discover()
SOLVED_KINDS = tuple(f["kind"] for f in _DISCOVERED_FAMILIES)
LABELS = {f["kind"]: f["label"] for f in _DISCOVERED_FAMILIES}

# (kind, expr, signed_needs, pool_refs, cost, k_const, terms).
#
# WARNING `k_const`/`terms` used to be here, removed as dead fields ("assemble() never consumed
# them"), M8 stage 0 wires them back in — **this time with a clear consumer**: cross-word polynomial
# common subexpressions (CSE) must see the term list **before rendering**, but the decision originally only kept the rendered text.
# the difference between the two is "had no use back then" vs. "has one now", not that the earlier judgment was wrong.
#
# only the linear/quadratic families (going through `_candidate()`) fill in these two fields; the other families assemble
# `expr` text, can't produce a term list, stays `None` — the CSE pass sees `None` and skips it.
# positional-argument compatible: the old 5-argument call `Cand(kind, expr, needs, refs, cost)` still works
# (one spot in `tools/selftest.py`), the new field has a default value.
Cand = namedtuple("Cand", "kind expr signed_needs pool_refs cost k_const terms",
                  defaults=(None, None))

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# `[sS]?` can't be dropped: a signed sized literal like `36'sd123`, `8'sh1F`, if not stripped out,
# leftover `sd123` after stripping the digits gets mistaken by `_IDENT_RE` for "an undriven identifier" and falsely trips I7.
_SIZED_LIT_RE = re.compile(r"\d+'[sS]?[bodhBODH][0-9a-fA-FxXzZ_?]*")
_NUM_RE = re.compile(r"\b\d+\b")
# WARNING `unsigned` must be listed together with `signed`. `$signed(x)` has been used since M1, so
# `signed` has always been there; `$unsigned(x)` only started being used in the M6a fix round (splitting zero extension out of `{}`
# changed over, because `{}` is explicitly billed in Appendix A while system functions aren't billed), missing it would make I7
# treats `unsigned` as an undriven identifier and the whole case falls back. **This is exactly what I7 should do**
# (fail-closed, doesn't emit broken RTL), but what it's blocking is our own legitimate code, so it needs to be added to the allowlist.
_TEXT_KEYWORDS = {"signed", "unsigned", "wire", "assign", "input", "output",
                   "module", "endmodule"}


def _cost_candidate(kind, name, width, expr, signed_needs=(), pool_refs=(), extra=0,
                    k_const=None, terms=None):
    """Folds an already-rendered expression into a `Cand`, or None (cost estimation failed).

    `extra` is the part to add on top of the candidate's cost (M3a: the caller passes `pool.price(pool_refs)`,
    which is always 0 because no caller in M3a ever requests a pool entry).

    `k_const`/`terms` are only passed by the linear/quadratic families (see the comment on `Cand`) — they are the
    sole input to M8's CSE pass, **they do not affect the cost computed by this function, nor `expr`**. The
    stage-0 acceptance criterion was "after wiring this in, the 42 outputs are byte-identical."
    """
    try:
        cost = cost_of_word_assign(name, width, expr, cost_of_text) + extra
    except Exception:
        return None
    return Cand(kind, expr, set(signed_needs), tuple(pool_refs), cost, k_const, terms)


def _known_words_for_consumer(nl, name, width, decl_index, known_words, known_word_cone):
    """M10 (R3) eligibility filter: filters `known_words` accumulated so far by the `decide()` loop (see that
    function's docstring) down to the subset usable by this consumer (`name`/`width`), via G1 (declaration order) /
    G2 (cone check) / G4 (width). G3 (has a model) is already satisfied when `known_words` is written, so it isn't
    re-checked here. Split out into its own function so `tools/selftest.py` can bypass `decide()` entirely and test
    these four guards on their own (see M10_spec.md section 6, item 5)."""
    my_bits = set(nl.bits([(name, width)]))
    return {q: entry for q, entry in known_words.items()
            if decl_index[q] < decl_index[name] and entry[2] >= width
            and not (my_bits & known_word_cone[q])}


def _candidate(kind, name, width, res, signed_prefix="s_", known_words=None):
    """Thin wrapper dedicated to the linear/quadratic families: (K, terms) -> render_word -> `_cost_candidate`.

    `known_words` (M10, R3) is only ever passed to `render_word` here — the other families assemble their own
    `expr` text (going through `_cost_candidate`) and can't produce a term list, see the comment on `Cand` and
    M10_spec.md section 4. The caller (`decide()`) has already run all four G1/G2/G3/G4 eligibility filters on
    this `name`; this just forwards it as-is, without filtering again."""
    if res is None:
        return None
    k_const, terms = res
    expr, signed_needs = render_word(name, width, k_const, terms, signed_prefix=signed_prefix,
                                      known_words=known_words)
    return _cost_candidate(kind, name, width, expr, signed_needs,
                           k_const=k_const, terms=tuple(terms))


def _try_family(family, env, ctx, name, width, signed_prefix, consumer_known):
    """Runs a registered family's `attempt` on one word, folding the result into a `Cand` or `None`.

    `attempt` itself wraps `try/except Exception` (copying the original 14 hand-written dispatch blocks, each with
    its own `try/except`); the failure handling inside `_candidate`/`_cost_candidate` is unchanged, and isn't
    wrapped a second time. `shape` decides which convergence path to take (see the design notes in
    `registry.py`/`FAMILY`):
    - `"res"`: `attempt` returns `(k_const, terms)` or `None` -> `_candidate()`; when `uses_known_words` is True,
      `consumer_known` is passed, otherwise `None` (the asymmetry of `(undisclosed family)` — `main.py` never
      passed `known_words` to it either).
    - `"expr3"`: `attempt` returns `(expr, signed_needs, pool_refs)` or `None` -> `_cost_candidate()`.
    """
    try:
        res = family["attempt"](env, ctx, name, width)
    except Exception:
        res = None

    if family["shape"] == "res":
        kw = consumer_known if family.get("uses_known_words") else None
        return _candidate(family["kind"], name, width, res, signed_prefix=signed_prefix,
                          known_words=kw)

    if res is None:
        return None
    expr, signed_needs, pool_refs = res
    return _cost_candidate(family["kind"], name, width, expr,
                           signed_needs=signed_needs, pool_refs=pool_refs)


def decide(nl, pool, verbose=False):
    """Returns {word_name: decision}, where decision is a `Cand` (`kind` in SOLVED_KINDS) or ("fallback", None).

    Each word tries the linear family, then the quadratic family, in order, estimating cost for each (including
    operator counts for pool entries it needs that haven't been amortized yet), then takes the minimum together
    with the fallback; once a word is adopted, the pool entries it references are billed to `pool` (so other words
    that reference the same pool entry later don't pay for it again)."""
    _, weight_vecs = linear.build_weight_vectors(nl)
    weight_results = nl.simulate(weight_vecs)
    weights_by_word = linear.compute_weights(nl, weight_results)

    quad_plan = quadratic.build_plan(nl)
    quad_probe_results = nl.simulate(quad_plan.vectors)

    val_vectors = gen_validation_vectors(nl)
    val_results = nl.simulate(val_vectors)

    # the comparison family's acceptance requires the "training set" and "acceptance set" to use different seeds (README already lists
    # "treating the training set as the test set" as a moderate risk): here a separate, independent set of vectors is run, compare/mux/(undisclosed family)/...
    # families' `build_context` all use this.
    verify_vectors = gen_validation_vectors(nl, seed=VALIDATION_SEED + 1)
    verify_results = nl.simulate(verify_vectors)

    signed_prefix = fresh_prefix(nl.src, base="s")

    # M21: the family registry (`solver/registry.py`) replaces the original 14 hand-written dispatch blocks. `Env` is
    # the read-only shared environment passed to each family's `build_context`/`attempt`; `discover()` scans
    # `solver/*.py`, sorted by `order` (the order exactly replicates the original call sequence: linear 10,
    # quadratic 20, compare 30, mux 40 are primary; (undisclosed family) 50, (undisclosed family) 60,
    # (undisclosed family) 70, (undisclosed family) 80, (undisclosed family) 90, (undisclosed family) 100, (undisclosed family) 110, (undisclosed family) 120,
    # (undisclosed family) 130, (undisclosed family) 140 are secondary), and reads the `SOLVER_FAMILIES` allowlist.
    # When a family module is missing (`ImportError`), `discover()` silently skips it without affecting the rest.
    env = registry.Env(nl=nl, pool=pool, weights_by_word=weights_by_word,
                        quad_plan=quad_plan, quad_probe_results=quad_probe_results,
                        val_vectors=val_vectors, val_results=val_results,
                        verify_vectors=verify_vectors, verify_results=verify_results,
                        signed_prefix=signed_prefix)
    families = registry.discover()
    # `build_context` is all built once, outside the word loop (keeps each family's original context
    # Division of labor: one-shot probing unrelated to a specific word goes here; per-word fitting is left to
    # `attempt`). `build_context` itself has no try/except wrapper — same as before.
    family_ctx = {f["kind"]: f["build_context"](env) for f in families}
    primary_families = [f for f in families if f["stage"] == "primary"]
    secondary_families = [f for f in families if f["stage"] == "secondary"]

    # M10 (R3): lets an expression reference an existing output port. `known_words`/`known_word_cone`
    # accumulated only within this loop, filled in incrementally **in `nl.outputs`'s declaration order** — processing
    # when processing `name`, `known_words` can structurally only contain words processed before it (see below
    # the loop's tail write point). G1 (declaration order) also explicitly compares once more using `decl_index`
    # (the same trick as `compare.py:155-163`/`(undisclosed family).py:789-797`/`(undisclosed family).py:362-369`)
    # the "structural guarantee + explicit guard" belt-and-suspenders style), not because the structural guarantee isn't
    # guards can be pinned independently by mutate/tests, without depending on a hard-to-assert property like loop order.
    # `known_word_cone` caches each candidate word's `cone_nets` (used by G2), computed once per q
    # once — same trick as `fallback.cone_nets`/`compare.py:155-163`.
    decl_index = {n: i for i, (n, _w) in enumerate(nl.outputs)}
    known_words = {}       # q_name -> (k_const, terms, width)
    known_word_cone = {}   # q_name -> frozenset(cone_nets(q))

    decisions = {}
    for name, width in nl.outputs:
        candidates = []

        # G1 (declaration order) + G2 (cone check) + G4 (width): see `_known_words_for_consumer`.
        consumer_known = _known_words_for_consumer(nl, name, width, decl_index,
                                                     known_words, known_word_cone)

        for family in primary_families:
            c = _try_family(family, env, family_ctx[family["kind"]], name, width,
                             signed_prefix, consumer_known)
            if c is not None:
                candidates.append(c._replace(cost=c.cost + pool.price(c.pool_refs)))

        # Since M5a: secondary families are only tried once all the earlier (primary) ones have no candidate — this
        # "only tried when candidates is empty" is a non-negotiable safety design, not a performance
        # opportunistic optimization. Two reasons: (1) provably no regression — a word that already has a candidate won't call
        # secondary families, the output must be byte-identical; (2) performance — these families mostly need to run
        # the extra probing `simulate()`, only fallback words pay this cost.
        # `tools/selftest.py`'s `test_(undisclosed family)_only_tried_when_candidates_empty`
        # assert that this guard in `decide()` appears exactly once.
        if not candidates:
            for family in secondary_families:
                c = _try_family(family, env, family_ctx[family["kind"]], name, width,
                                 signed_prefix, consumer_known)
                if c is not None:
                    candidates.append(c._replace(cost=c.cost + pool.price(c.pool_refs)))

        fb_cost = fallback.cone_gate_count(nl, nl.bits([(name, width)]))
        best = min(candidates, key=lambda c: c.cost) if candidates else None

        if best is not None and best.cost <= fb_cost:
            decisions[name] = best
            pool.commit(best.pool_refs)
            if verbose:
                print(f"  {name}: {LABELS[best.kind]}, cost {best.cost} vs fallback {fb_cost}",
                      file=sys.stderr)
        else:
            decisions[name] = ("fallback", None)
            if verbose:
                if candidates:
                    print(f"  {name}: fallback (best candidate cost {best.cost} lost to fallback {fb_cost})",
                          file=sys.stderr)
                else:
                    print(f"  {name}: fallback (neither the linear nor quadratic hypothesis passed)", file=sys.stderr)

        # M10 (R3): G1 (declaration order, naturally guaranteed by this loop's traversal order) + G3 (has a model,
        # only the linear/quadratic families' final decision fills in `terms` — see the comment on `Cand`) are all handled here
        # satisfied once. Afterward each consumer filters G2/G4 on its own (see the top of the loop).
        final = decisions[name]
        if isinstance(final, Cand) and final.terms is not None:
            known_words[name] = (final.k_const, final.terms, width)
            known_word_cone[name] = frozenset(fallback.cone_nets(nl, nl.bits([(name, width)]), set()))

    # M8 stage 1: cross-word polynomial common subexpression (CSE) — a global cleanup pass, unlike "only when
    # only tried when candidates is empty" is unrelated (that guard is for fallback candidate families
    # 's — here it's a cleanup pass over all already-solved linear/quadratic candidates), so deliberately placed on that
    # outside the candidates-empty check block, before `return decisions`.
    if polycse is not None:
        saved = polycse.run(nl, decisions, pool, signed_prefix=signed_prefix, verbose=verbose)
        if verbose and saved:
            print(f"  polycse: saved {saved} points this round", file=sys.stderr)

        # M12: a cleanup pass between poly pool entries (see the M12 section of the `(undisclosed family).py` module docstring).
        polycse.run_pool(decisions, pool, signed_prefix=signed_prefix, verbose=verbose)

    # M13: expression DAG-CSE + implication simplification (cleanup pass, see the `(undisclosed family).py` module docstring).
    if exprcse is not None:
        exprcse.run(decisions, pool, signed_prefix=signed_prefix, verbose=verbose)

    return decisions


def _refs_in_text(text):
    """Pulls "looks like a read" identifiers out of a chunk of Verilog text — not a general Verilog parser, it
    only serves the I7 invariant check: first strips out sized/decimal literals so `8'd3` isn't mistaken for the
    identifier `d3`, then filters out a few keywords."""
    s = _SIZED_LIT_RE.sub(" ", text)
    s = _NUM_RE.sub(" ", s)
    return {i for i in _IDENT_RE.findall(s) if i not in _TEXT_KEYWORDS}


def _assert_no_cycle(adj):
    """`adj`: node -> the set of nodes it references. In stage M3a `adj` is always empty, so the loop ends
    immediately."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in adj}
    for start in adj:
        if color[start] != WHITE:
            continue
        stack = [(start, iter(adj.get(start, ())))]
        color[start] = GRAY
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt not in color:
                    continue
                if color[nxt] == GRAY:
                    raise AssertionError(f"I5 violation: cycle detected, involving {node} -> {nxt}")
                if color[nxt] == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, iter(adj.get(nxt, ()))))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()


_SIZED_LIT_ONLY_RE = re.compile(r"\d+'[sS]?[bodhBODH][0-9a-fA-FxXzZ_?]*")
_READ_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_$]*)(?:\s*\[\s*(\d+)\s*\])?")
_ASSIGN_RE = re.compile(r"^\s*assign\s+(\S+)\s*=\s*(.*?);\s*$")

# Record of global demotions: every time `assemble` demotes a solved word to fallback because of the global cycle check,
# the word name is appended here. Used by selftest to assert that "demotion really fired", and also
# by `tools/probe_m33.py --corpus-demotions` (the current corpus should never demote even once);
# it takes no part in any output decision.
GLOBAL_DEMOTIONS = []


def _global_adj(nl, assign_lines, signed_lines, pool_lines, gates):
    """Global, bit-level dependency graph: node (a driven net name) -> the set of nodes it reads.

    The node granularity is the output **bit** (`nn[0]` and `nn[1]` differ); a pool wire / signed wire
    is a single node as a whole. Reading the whole output word name `X` depends on all bits of X, while
    reading `X[i]` depends on only that one bit. Input ports and constants are not keys; reading them
    is a sink. Fallback gates: a gate's output net depends on the gate's input nets."""
    word_bits = {}
    for name, width in nl.outputs:
        word_bits[name] = nl.bits([(name, width)])
    bit_set = {b for bs in word_bits.values() for b in bs}

    def reads_of(text):
        text = _SIZED_LIT_ONLY_RE.sub(" ", text)
        out = set()
        for m in _READ_RE.finditer(text):
            ident, idx = m.group(1), m.group(2)
            if ident in _TEXT_KEYWORDS:
                continue
            if ident in word_bits:
                bs = word_bits[ident]
                bit = f"{ident}[{idx}]" if idx is not None else None
                if bit is not None and bit in bit_set:
                    out.add(bit)
                else:
                    out.update(bs)
            else:
                bit = f"{ident}[{idx}]" if idx is not None else None
                out.add(bit if bit is not None and bit in bit_set else ident)
        return out

    adj = {}

    def add(lhs, text):
        lhs_nodes = word_bits.get(lhs, [lhs])
        rd = reads_of(text)
        for n in lhs_nodes:
            adj.setdefault(n, set()).update(rd)

    for block in (assign_lines, signed_lines, pool_lines):
        for chunk in block:
            for line in chunk.splitlines():
                m = _ASSIGN_RE.match(line)
                if m:
                    add(m.group(1), m.group(2))
    for _op, o, ins in gates:
        adj.setdefault(o, set()).update(
            x for i in ins for x in reads_of(i))
    return adj


def _find_cycle(adj):
    """Return one cycle in the graph (a node list, first and last not repeated), or None if there is no cycle.
    Deterministic: the start node and the neighbors are both visited in string-sorted order."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}
    for start in sorted(adj):
        if color.get(start, WHITE) != WHITE:
            continue
        path = [start]
        color[start] = GRAY
        stack = [iter(sorted(n for n in adj.get(start, ()) if n in adj))]
        while stack:
            advanced = False
            for nxt in stack[-1]:
                c = color.get(nxt, WHITE)
                if c == GRAY:
                    return path[path.index(nxt):]
                if c == WHITE:
                    color[nxt] = GRAY
                    path.append(nxt)
                    stack.append(iter(sorted(n for n in adj.get(nxt, ()) if n in adj)))
                    advanced = True
                    break
            if not advanced:
                color[path[-1]] = BLACK
                path.pop()
                stack.pop()
    return None


def _cycle_victim(nl, decisions, cycle):
    """The solved output word on the cycle that is latest in declaration order; None if the cycle has no solved word."""
    bit_word = {}
    for name, width in nl.outputs:
        for b in nl.bits([(name, width)]):
            bit_word[b] = name
    on_cycle = {bit_word[n] for n in cycle if n in bit_word}
    victim = None
    for name, _w in nl.outputs:           # declaration order, the latter overrides the former
        if name in on_cycle and decisions.get(name, ("fallback", None))[0] in SOLVED_KINDS:
            victim = name
    return victim


def _check_invariants(nl, decisions, solved_roots, fallback_roots, gates,
                       assign_lines, signed_lines, pool_lines, wire_lines,
                       signed_names, pool_names, pool, live_refs):
    output_bits = set(nl.bits(nl.outputs))
    gate_lhs = {g[1] for g in gates}

    # I1: partition — solved/fallback are disjoint and their union covers all output bits.
    assert not (solved_roots & fallback_roots), "I1 violation: solved_roots and fallback_roots overlap"
    assert (solved_roots | fallback_roots) == output_bits, "I1 violation: the union doesn't cover all output bits"

    # I2: a fallback gate never drives an output bit of a word already solved by an assign.
    assert not (gate_lhs & solved_roots), "I2 violation: a fallback gate drives an already-solved output bit"

    # I3: single driver — word assign / signed wire / pool entries combined with gate outputs, no duplicates allowed.
    word_lhs = [name for name, _w in nl.outputs
                if decisions.get(name, ("fallback", None))[0] in SOLVED_KINDS]
    all_lhs = word_lhs + list(signed_names) + list(pool_names) + list(gate_lhs)
    assert len(all_lhs) == len(set(all_lhs)), "I3 violation: the same name is driven from two places"

    # I4: fresh naming — identifiers generated by the solver must not collide with identifiers already in the netlist.
    generated_names = set(signed_names) | set(pool_names)
    src_idents = set(_IDENT_RE.findall(nl.src))
    assert not (generated_names & src_idents), "I4 violation: a generated identifier collides with an existing netlist identifier"

    # I5: acyclic — build a digraph from (pool entry -> the pool entries it references). In stage M3a `_refs_of` always returns
    # empty tuple, no edges in the graph, covers only pool entries that actually appear in this emit (the rest are unrelated to the output).
    live_keys = pool._live_keys(live_refs)
    graph = {pool._names[k]: {pool._names[j] for j in pool._refs_of(k) if j in pool._names}
             for k in live_keys}
    _assert_no_cycle(graph)

    # I8: globally acyclic -- output bits / pool wires / signed wires / fallback gates taken together form a bit-level
    # dependency graph. I5 only looks at the pool-term graph and cannot see the edges between output words, or from fallback gates to output words.
    # After the demotion loop of `assemble` this must always hold; it is the last line of defense.
    cyc = _find_cycle(_global_adj(nl, assign_lines, signed_lines, pool_lines, gates))
    assert cyc is None, f"I8 violation: global dependency graph has a cycle {cyc}"

    # I6: both operand names of a comparison candidate (M3b wiring) must be in the pool's set of materialized names —
    # "every operand of a comparison expression must be a `wire signed` from the pool, never a raw port directly"
    # this discipline uses this invariant as an assertion. What's checked is **all adopted decisions'**
    # pool_refs** (not limited to compare: linear/quadratic currently never reference pool entries, this line for them
    # naturally passes, this more general form is kept for future family wiring).
    instantiated = set(pool_names)
    for d in decisions.values():
        kind = d[0]
        if kind == "fallback":
            continue
        for ref in getattr(d, "pool_refs", ()):
            name = pool._names.get(pool._normalize(ref))
            assert name is not None and name in instantiated, (
                f"I6 violation: candidate references a non-materialized pool entry {ref!r}")

    # I7: every read is driven.
    fallback_wire_names = set()
    for line in wire_lines:
        m = re.match(r"\s*wire\s+(\S+);", line)
        if m:
            fallback_wire_names.add(m.group(1))
    allowed_reads = (set(n for n, _w in nl.inputs) | set(nl.bits(nl.inputs))
                      | set(n for n, _w in nl.outputs) | output_bits
                      | generated_names | fallback_wire_names | {"1'b0", "1'b1"})
    reads = set()
    for line in assign_lines:
        reads |= _refs_in_text(line)
    for line in signed_lines:
        reads |= _refs_in_text(line)
    for line in pool_lines:
        reads |= _refs_in_text(line)
    for _op, _o, ins in gates:
        for i in ins:
            reads |= _refs_in_text(i)
    missing = reads - allowed_reads
    assert not missing, f"I7 violation: read of an undriven identifier {missing}"


def assemble(nl, decisions, pool, verbose=False):
    """Assemble the final text. After assembly, run one global bit-level cycle check (`_global_adj`): the
    cross-word reference guards of the families (declaration order + cone check) are local and not
    transitive, and a fallback word keeps its structural edges to later-declared words, so the whole
    may form a cycle. When a cycle is detected, demote the solved word that is latest in declaration
    order on the cycle to fallback and reassemble, until acyclic (each round demotes at least one;
    all-fallback is the original netlist, which is acyclic). The demotion is semantically safe: a
    demoted word is still driven by the fallback gates, and other words referencing its name are unaffected."""
    decisions = dict(decisions)
    # Loop limit = number of output words + 1: each round demotes at least one word, and demoting them all gives the original netlist (acyclic).
    # Do not rely on assert to guarantee termination (`python -O` strips asserts and would loop forever); on exceeding the limit or no victim,
    # always raise explicitly, so that the emergency fallback of `main()` takes over.
    for _round in range(len(nl.outputs) + 1):
        built = _assemble_parts(nl, decisions, pool)
        cyc = _find_cycle(_global_adj(nl, built["assign_lines"], built["signed_lines"],
                                      built["pool_lines"], built["gates"]))
        if cyc is None:
            break
        victim = _cycle_victim(nl, decisions, cyc)
        if victim is None:
            raise RuntimeError(f"I8 violation: global dependency graph has a cycle but no demotable solved word on it {cyc}")
        decisions[victim] = ("fallback", None)
        GLOBAL_DEMOTIONS.append(victim)
        if verbose:
            print(f"  global cycle check: {victim} is on the cycle ({' -> '.join(cyc)}), demoting to fallback",
                  file=sys.stderr)
    else:
        raise RuntimeError("I8 violation: still cyclic after the demotion loop exceeded its limit")
    _check_invariants(nl, decisions, built["solved_roots"], built["fallback_roots"],
                      built["gates"], built["assign_lines"], built["signed_lines"],
                      built["pool_lines"], built["wire_lines"], built["signed_names"],
                      built["pool_names"], pool, built["live_refs"])
    return built["text"]


def _assemble_parts(nl, decisions, pool):
    header_ports, decl_lines = extract_module_text(nl.src)

    solved_roots = set()
    fallback_roots = set()
    for name, width in nl.outputs:
        d = decisions.get(name, ("fallback", None))
        bits = nl.bits([(name, width)])
        if d[0] in SOLVED_KINDS:
            solved_roots.update(bits)
        else:
            fallback_roots.update(bits)

    wire_lines, gates = fallback.build_fallback_body(nl, fallback_roots, solved_roots)
    gate_lines = fallback.render_gates(gates)

    signed_needs_all = set()
    assign_lines = []
    live_refs = []
    for name, width in nl.outputs:
        d = decisions.get(name, ("fallback", None))
        if d[0] in SOLVED_KINDS:
            signed_needs_all.update(d.signed_needs)
            assign_lines.append(f"  assign {name} = {d.expr};")
            live_refs.extend(d.pool_refs)

    pool_lines = pool.emit(live_refs)
    pool_names = pool.names(live_refs)

    # the comparison family's affine pool entries may also need a signed pre-extension wire (see share.py's
    # see `signed_needs()` docstring); merged into the same signed_needs_all so word-level candidates and
    # pool entries share the same declaration and dedup key.
    signed_needs_all.update(pool.signed_needs(live_refs))

    signed_prefix = fresh_prefix(nl.src, base="s")
    signed_lines = []
    signed_names = []
    for port, port_width, width in sorted(signed_needs_all):
        wn = signed_wire_name(port, width, prefix=signed_prefix)
        signed_names.append(wn)
        signed_lines.append(f"  wire signed [{width - 1}:0] {wn};")
        signed_lines.append(f"  assign {wn} = $signed({port});")

    lines = [f"module top({header_ports});"]
    lines.extend(decl_lines)
    lines.extend(signed_lines)
    lines.extend(pool_lines)
    lines.extend(wire_lines)
    lines.extend(assign_lines)
    lines.extend(gate_lines)
    lines.append("endmodule")
    return dict(text="\n".join(lines) + "\n", solved_roots=solved_roots,
                fallback_roots=fallback_roots, gates=gates, assign_lines=assign_lines,
                signed_lines=signed_lines, pool_lines=pool_lines, wire_lines=wire_lines,
                signed_names=signed_names, pool_names=pool_names, live_refs=live_refs)


def whole_netlist_fallback_text(nl):
    """Every output word falls back (every output word goes through fallback): the last line of defense,
    correctness first."""
    decisions = {name: ("fallback", None) for name, _ in nl.outputs}
    pool = SharePool(fresh_prefix(nl.src, base="cse"))
    return assemble(nl, decisions, pool)


def _all_port_widths(nl):
    widths = dict(nl.inputs)
    widths.update(dict(nl.outputs))
    return widths


def run(input_path, verbose=False):
    nl = Netlist(input_path)
    pool = SharePool(fresh_prefix(nl.src, base="cse"),
                      signed_prefix=fresh_prefix(nl.src, base="s"),
                      port_widths=_all_port_widths(nl))
    decisions = decide(nl, pool, verbose=verbose)
    return assemble(nl, decisions, pool, verbose=verbose)


def emergency_fallback(input_path):
    """Outer safety net: no matter what blew up above, it must emit equivalent RTL — it must never crash, must
    never be non-equivalent."""
    try:
        nl = Netlist(input_path)
        return whole_netlist_fallback_text(nl)
    except Exception:
        try:
            return open(input_path, errors="replace").read()
        except Exception:
            return "module top();\nendmodule\n"


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="Recovers arithmetic RTL from a gate-level netlist (must not use parallelism, standard library only)")
    ap.add_argument("-input", dest="input", required=True, help="gate-level netlist path")
    ap.add_argument("-output", dest="output", required=True, help="output RTL path")
    ap.add_argument("--verbose", action="store_true", help="debug: print each word's decision")
    ap.add_argument("--time-budget", dest="time_budget", type=float, default=None,
                     help="debug: for future extension reference only, not enforced yet")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        text = run(args.input, verbose=args.verbose)
    except Exception as e:
        if args.verbose:
            import traceback
            traceback.print_exc(file=sys.stderr)
            print(f"main flow failed ({type(e).__name__}: {e}), falling back to whole-netlist fallback", file=sys.stderr)
        text = emergency_fallback(args.input)
    with open(args.output, "w") as f:
        f.write(text)


if __name__ == "__main__":
    main()
