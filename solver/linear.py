"""Family B: word-linear (bit-weight linear) extractor.

Flow (decided separately for each output word, but the simulation itself is shared across all words):

1. Run one `simulate()` over n_bits+1 vectors — "all 0" plus "each primary input bit individually set
   to 1" — and fold out, per word, the constant term K and the per-bit weight
   `w_b = (out(single bit set to 1) - K) mod M`.
2. Run `simulate()` once more with another shared set of verification vectors (see `vectors.py`), and
   for each word check whether "K + Sum of w_b (for bits set to 1 in x)" equals the actual simulated
   value. Any mismatch -> this word's linear hypothesis fails, and the whole word can't use this
   family.
3. Once verification passes, fold the per-bit weights into word-level terms (unsigned / signed); bits
   that can't be folded stay as per-bit terms. If the total number of per-bit terms exceeds 24, treat
   it as "not worth it" and give up outright (return None), letting the caller fall back — there's no
   point spending time carving out a giant expression that's destined to lose the cost comparison
   anyway.
"""
from model import Term


def split_bit(bitname):
    """`"in2[3]"` -> ("in2", 3); `"in2"` -> ("in2", 0) (1-bit port)."""
    if "[" in bitname:
        p, rest = bitname.split("[", 1)
        return p, int(rest[:-1])
    return bitname, 0


def build_weight_vectors(nl):
    """Returns (bit_names, vectors): vectors[0] is all 0, vectors[1+i] has only bit_names[i] set to 1."""
    bit_names = nl.bits(nl.inputs)
    zero = {n: 0 for n, _ in nl.inputs}
    vecs = [dict(zero)]
    for b in bit_names:
        p, idx = split_bit(b)
        v = dict(zero)
        v[p] = v[p] | (1 << idx)
        vecs.append(v)
    return bit_names, vecs


def compute_weights(nl, weight_results):
    """Returns {word_name: (K, {bit_name: weight})}. weight_results is the result of
    `nl.simulate(build_weight_vectors(nl)[1])` (the caller is responsible for running it, because it
    needs to share the same Netlist with other simulations)."""
    bit_names, _ = build_weight_vectors(nl)
    out = {}
    for name, width in nl.outputs:
        modulus = 1 << width
        base = weight_results[0][name]
        weights = {}
        for i, b in enumerate(bit_names):
            weights[b] = (weight_results[1 + i][name] - base) % modulus
        out[name] = (base, weights)
    return out


def validate_linear(nl, name, width, k_const, weights, vec_results, vectors):
    """Checks the "K + Sum w_b" hypothesis against each verification vector in turn. Returns True only
    if all of them match."""
    modulus = 1 << width
    for vec, res in zip(vectors, vec_results):
        acc = k_const
        for n, w in nl.inputs:
            val = vec.get(n, 0)
            for i in range(w):
                if (val >> i) & 1:
                    acc += weights[n if w == 1 else f"{n}[{i}]"]
        if (acc % modulus) != res[name]:
            return False
    return True


MAX_BIT_TERMS = 24


def fold_terms(nl, name, width, k_const, weights):
    """Folds per-bit weights into word/bit terms. Returns (K, [Term,...]) or None (too many bit terms,
    not worth it)."""
    modulus = 1 << width
    terms = []
    bit_term_count = 0
    for pname, pwidth in nl.inputs:
        wlist = [weights[pname if pwidth == 1 else f"{pname}[{k}]"] for k in range(pwidth)]
        if all(w == 0 for w in wlist):
            continue
        c = wlist[0]
        if all(wlist[k] == (c * (1 << k)) % modulus for k in range(pwidth)):
            terms.append(Term("word", pname, pwidth, c, signed=False))
            continue
        if pwidth > 1:
            ok = (all(wlist[k] == (c * (1 << k)) % modulus for k in range(pwidth - 1))
                  and wlist[pwidth - 1] == ((-c * (1 << (pwidth - 1))) % modulus))
            if ok:
                terms.append(Term("word", pname, pwidth, c, signed=True))
                continue
        for k in range(pwidth):
            if wlist[k] != 0:
                terms.append(Term("bit", pname, pwidth, wlist[k], bit=k))
                bit_term_count += 1
    if bit_term_count > MAX_BIT_TERMS:
        return None
    return k_const, terms


def try_linear(nl, name, width, k_const, weights, vec_results, vectors):
    """Runs the full linear extraction flow on a single output word.

    Returns (K, [Term,...]) on success; returns None to mean this word shouldn't go through the linear
    family (the hypothesis failed verification, or the folded per-bit terms are too numerous to be
    worth it).
    """
    if not validate_linear(nl, name, width, k_const, weights, vec_results, vectors):
        return None
    return fold_terms(nl, name, width, k_const, weights)


def _family_attempt(env, ctx, name, width):
    k_const, weights = env.weights_by_word[name]
    return try_linear(env.nl, name, width, k_const, weights, env.val_results, env.val_vectors)


FAMILY = {
    "kind": "linear",
    "label": "solved by linear",
    "order": 10,
    "stage": "primary",
    "build_context": lambda env: None,
    "attempt": _family_attempt,
    "shape": "res",
    "uses_known_words": True,
}
