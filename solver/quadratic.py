"""Family C: word-level quadratic model extractor.

Model (mod 2^W, W = output word width):

    out = K + Sum_j c_j*A_j + Sum_{j<k} e_jk*A_j*A_k + Sum_j d_j*A_j^2

`A_j` is `in_j` or `$signed(in_j)`. Solved in two stages:

1. **Coefficient fitting**: probe vectors are fixed at x in {0, 1, 3} (all 0; each port individually
   set to 1; each port individually set to 3, only for ports with width > 1) plus pairs of ports
   simultaneously set to 1. These values read the same whether interpreted as signed or unsigned, so
   coefficient fitting is independent of signedness — coefficients can be fitted first, then
   signedness searched.
2. **Signedness search**: start from whichever of the two uniform assignments (all unsigned / all
   signed) has fewer mismatches, then repeatedly try flipping a single port's signedness, taking
   whichever step reduces mismatches the most, until 0 or no further improvement is possible. Pure
   greedy search can get stuck on a plateau for port combinations that "only appear in products, with
   no linear term" (test12's 12 pure-product terms), so both uniform assignments must be candidate
   starting points — it can't start from all-unsigned alone.

No CSE, no factoring (see README milestone 2's "explicitly out of scope for this milestone").
"""
from collections import namedtuple

from model import Term

MAX_PAIRS = 20000

# build_plan() only decides "what to probe", it never runs a simulation -- the caller (main.py) is
# responsible for running plan.vectors in its own nl.simulate() schedule, and only feeds the results into fit_word()/try_quadratic() afterward.
# only this way stays consistent with linear.py's division of labor between build_weight_vectors/compute_weights:
# "probe vector planning" is kept separate from "the actual simulation" so the caller can run a single simulate() covering all the vectors every family needs.
Plan = namedtuple("Plan", "word_ports word_pairs vectors idx_e idx_3e idx_pair port_width")


def split_bit(bitname):
    if "[" in bitname:
        p, rest = bitname.split("[", 1)
        return p, int(rest[:-1])
    return bitname, 0


def _word_ports_and_pairs(nl, support, name, width):
    """Returns (ports: set[str], pairs: set[(str,str)]).

    ports is the set of ports that appear in the fanin cone of any output bit of this word; pairs only
    collects port pairs that co-occur in the fanin cone of the same output bit — this pruning cuts the
    O(P^2) probe size down to something tied to the actual circuit structure, rather than to the total
    number of ports.
    """
    ports = set()
    pairs = set()
    for b in nl.bits([(name, width)]):
        bit_support = support.get(b, frozenset())
        ports_here = sorted({split_bit(x)[0] for x in bit_support})
        ports.update(ports_here)
        for i in range(len(ports_here)):
            for j in range(i + 1, len(ports_here)):
                pairs.add((ports_here[i], ports_here[j]))
    # **Must return a sorted list, never a set.** A set's iteration order is affected by PYTHONHASHSEED
    # affected, it propagates all the way through the insertion order of c/d/e, the term order of `to_terms`, and finally to render_word
    # the printed expression text -- the same solver run on the same input would produce different RTL.
    # that's not just ugly: render_word's rule for picking the "first term" depends on term order, and a word with all-negative coefficients
    # would end up differing by 1 point between runs, and cost is the score itself.
    return sorted(ports), sorted(pairs)


def build_plan(nl):
    """Returns a `Plan`, only planning the probe vectors, without running any simulation (see the module
    docstring above).

    word_pairs[name] being None means this word's pair count is still > MAX_PAIRS after pruning, and
    the caller should give up on the quadratic family outright. The probe vectors are shared across
    all words; the caller only needs to run `simulate()` once over `plan.vectors`.
    """
    support = nl.support()
    port_width = {n: w for n, w in nl.inputs}

    word_ports, word_pairs = {}, {}
    global_pairs = set()
    for name, width in nl.outputs:
        ports, pairs = _word_ports_and_pairs(nl, support, name, width)
        word_ports[name] = ports
        if len(pairs) > MAX_PAIRS:
            word_pairs[name] = None
            continue
        word_pairs[name] = pairs
        global_pairs.update(pairs)

    zero = {n: 0 for n, _ in nl.inputs}
    vectors = [dict(zero)]
    idx_e, idx_3e, idx_pair = {}, {}, {}
    for n, w in nl.inputs:
        v = dict(zero)
        v[n] = 1
        idx_e[n] = len(vectors)
        vectors.append(v)
    for n, w in nl.inputs:
        if w > 1:
            v = dict(zero)
            v[n] = 3
            idx_3e[n] = len(vectors)
            vectors.append(v)
    for p, q in sorted(global_pairs):        # ordering: see the comment on `_word_ports_and_pairs`
        v = dict(zero)
        v[p] = 1
        v[q] = 1
        idx_pair[(p, q)] = len(vectors)
        vectors.append(v)

    return Plan(word_ports, word_pairs, vectors, idx_e, idx_3e, idx_pair, port_width)


def fit_word(name, width, plan, results):
    """Returns (K, c, d, e) or None (pairs is None, or some port's deg-2 coefficient can't be folded
    out).

    c/d: {port: coeff}; e: {(port,port): coeff} (keys keep the (p,q) ordering given by build_plan).
    `results` is the return value of `nl.simulate(plan.vectors)` (the caller is responsible for
    running it, because it needs to share the same Netlist with other families' simulations).
    """
    ports = plan.word_ports.get(name, set())
    pairs = plan.word_pairs.get(name)
    idx_e, idx_3e, idx_pair, port_width = plan.idx_e, plan.idx_3e, plan.idx_pair, plan.port_width
    if pairs is None:
        return None
    modulus = 1 << width
    inv3 = pow(3, -1, modulus)
    K = results[0][name]
    c, d = {}, {}
    for p in ports:
        w = port_width[p]
        L1 = (results[idx_e[p]][name] - K) % modulus
        if w > 1:
            L3 = (results[idx_3e[p]][name] - K) % modulus
            two_d = ((L3 - 3 * L1) * inv3) % modulus
            if two_d % 2 != 0:
                return None
            dp = two_d >> 1
            c[p] = (L1 - dp) % modulus
            d[p] = dp
        else:
            c[p] = L1
            d[p] = 0

    e = {}
    for p, q in pairs:
        val = (results[idx_pair[(p, q)]][name] - results[idx_e[p]][name]
               - results[idx_e[q]][name] + K) % modulus
        if val != 0:
            e[(p, q)] = val
    return K, c, d, e


def _eval(K, c, d, e, precomp, signed_assign, modulus):
    def A(p):
        u, s = precomp[p]
        return s if signed_assign[p] else u

    acc = K
    for p, cc in c.items():
        if cc:
            acc += cc * A(p)
    for p, dd in d.items():
        if dd:
            a = A(p)
            acc += dd * a * a
    for (p, q), ee in e.items():
        if ee:
            acc += ee * A(p) * A(q)
    return acc % modulus


def _count_mismatches(K, c, d, e, precomp_vecs, signed_assign, targets, modulus):
    bad = 0
    for precomp, want in zip(precomp_vecs, targets):
        if _eval(K, c, d, e, precomp, signed_assign, modulus) != want:
            bad += 1
    return bad


def _precompute_vectors(vectors, ports, port_width):
    """The (unsigned value, signed value) for each port on each verification vector, reused repeatedly
    inside the signedness search loop; precomputed to avoid redoing the two's-complement conversion
    on every candidate."""
    out = []
    for vec in vectors:
        pc = {}
        for p in ports:
            w = port_width[p]
            u = vec.get(p, 0) % (1 << w)
            s = u - (1 << w) if u >= (1 << (w - 1)) else u
            pc[p] = (u, s)
        out.append(pc)
    return out


def search_signs(name, width, K, c, d, e, ports, port_width, val_vectors, val_results):
    """Returns signed_assign (dict port->bool) or None (no matter how the flips are tried, 0 mismatch is
    never reached)."""
    modulus = 1 << width
    ports = sorted(ports)
    if not ports:
        # No port participates at all (a purely constant word), signedness is meaningless, just verify the constant itself directly.
        ok = all(K % modulus == r[name] for r in val_results)
        return {} if ok else None

    precomp = _precompute_vectors(val_vectors, ports, port_width)
    targets = [r[name] for r in val_results]

    all_unsigned = {p: False for p in ports}
    all_signed = {p: True for p in ports}
    m0 = _count_mismatches(K, c, d, e, precomp, all_unsigned, targets, modulus)
    if m0 == 0:
        return all_unsigned
    m1 = _count_mismatches(K, c, d, e, precomp, all_signed, targets, modulus)
    if m1 == 0:
        return all_signed

    cur = dict(all_unsigned) if m0 <= m1 else dict(all_signed)
    best = min(m0, m1)
    while best > 0:
        best_flip, best_score = None, best
        for p in ports:
            cand = dict(cur)
            cand[p] = not cand[p]
            mm = _count_mismatches(K, c, d, e, precomp, cand, targets, modulus)
            if mm < best_score:
                best_score, best_flip = mm, p
        if best_flip is None:
            return None
        cur[best_flip] = not cur[best_flip]
        best = best_score
    return cur


MAX_TERMS = 40


def to_terms(K, c, d, e, signed_assign, port_width):
    """Folds (K,c,d,e,signed_assign) into [Term,...]. Returns None to mean there are too many terms to
    be worth it."""
    terms = []
    for p, cc in c.items():
        if cc:
            terms.append(Term("word", p, port_width[p], cc, signed=signed_assign[p]))
    for p, dd in d.items():
        if dd:
            terms.append(Term("sq", p, port_width[p], dd, signed=signed_assign[p]))
    for (p, q), ee in e.items():
        if ee:
            terms.append(Term("prod", p, port_width[p], ee, signed=signed_assign[p],
                               port2=q, port2_width=port_width[q], signed2=signed_assign[q]))
    if len(terms) > MAX_TERMS:
        return None
    return terms


def try_quadratic(name, width, plan, probe_results, val_vectors, val_results):
    """Runs the full quadratic extraction flow on a single output word.

    plan is the return value of `build_plan(nl)`; probe_results is `nl.simulate(plan.vectors)` (the
    caller is responsible for running it, sharing the same Netlist with other families).

    Returns (K, [Term,...]) on success; None means this word shouldn't go through the quadratic
    family.
    """
    fit = fit_word(name, width, plan, probe_results)
    if fit is None:
        return None
    K, c, d, e = fit
    ports = plan.word_ports.get(name, set())
    signed_assign = search_signs(name, width, K, c, d, e, ports, plan.port_width,
                                  val_vectors, val_results)
    if signed_assign is None:
        return None
    terms = to_terms(K, c, d, e, signed_assign, plan.port_width)
    if terms is None:
        return None
    return K, terms


def _family_attempt(env, ctx, name, width):
    return try_quadratic(name, width, env.quad_plan, env.quad_probe_results,
                          env.val_vectors, env.val_results)


FAMILY = {
    "kind": "quadratic",
    "label": "solved by quadratic",
    "order": 20,
    "stage": "primary",
    "build_context": lambda env: None,
    "attempt": _family_attempt,
    "shape": "res",
    "uses_known_words": True,
}
