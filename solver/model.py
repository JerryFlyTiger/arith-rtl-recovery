"""Hypothesis data structure, Verilog generation, and cost estimation.

The hypothesis produced by `solver/linear.py` (K plus a series of word/bit terms)
is folded into Verilog text here. The layout rules are aligned directly with the
pricing rules in `tools/costcount.py` (see the rule table at the top of that file):

- A term with coefficient ±1 doesn't print `1 *`.
- A negative coefficient is written as "subtraction" (`a - 3*in2`), not a unary
  minus attached to an identifier (`-in2`); this is more expensive than
  `a - in2` because a unary `-` attached to an identifier costs 1, while attached
  to a numeric literal it's free.
- So a positive-coefficient term is preferred as the first term; if every term's
  coefficient is negative, only then does the first term use a unary minus
  attached to a numeric literal (free), and every other term uses subtraction.
- Every numeric constant is always printed as a sized literal (`W'd<value>`): this
  path is recognized as free by `costcount._strip_comments`/`cost_of_text`'s
  sized-literal regex, and it also avoids the misjudgment that would arise from an
  unsized decimal constant's default width (32-bit) in a wide context — K or a
  coefficient can easily exceed 2^31 in a wide-word case.
- Signed operands are always pre-extended first into a `wire signed` of the target
  word width W; the main expression only uses the pre-extended wire, never mixing
  signed/unsigned inside one expression (mixing them breaks the sign extension of
  the whole expression).
"""


def sized_literal(value, width):
    """Prints a nonnegative integer as a sized decimal literal, which costcount
    treats as free."""
    return f"{width}'d{value}"


def signed_wire_name(port, width, prefix="s_"):
    return f"{prefix}{port}_w{width}"


class Term:
    """A linear/quadratic term.

    kind == 'word': the whole port as one term, coeff * port (or coeff * signed(port)).
    kind == 'bit':  a single bit, coeff * port[bit].
    kind == 'sq':   a square term, coeff * port**2 (or coeff * signed(port)**2).
    kind == 'prod': the product of two ports, coeff * port * port2 (each may
                    independently be signed/unsigned). port2/port2_width/signed2
                    are only meaningful when kind == 'prod'.
    kind == 'pow':  the D-th power of a single port (D >= 3), coeff * port**exp (or
                    coeff * signed(port)**exp). Added in M2c (single-variable
                    polynomial family); `exp` is only meaningful when kind == 'pow'.
    kind == 'zext': coeff * port, but port's bit pattern is **always zero-extended**
                    to `width` bits (achieved with `$unsigned()`, **never with `{}`
                    concatenation** — see render_word), never going through
                    `$signed()` and ignoring the `signed` field. Added in M6a
                    (weighted-sum-of-pairs family): `port` may be a
                    `wire signed [pw:0]` (representing an intermediate value that
                    "has been signed-extended to pw+1 bits and is then read as
                    unsigned", the uext pool item of `(unpublished family).py`) or
                    a naturally unsigned port (a raw read) — either way, the
                    extension must be a **zero-extension** and must **not drift
                    depending on the surrounding expression's signedness context**.
                    Rendered as `$unsigned(port)`
                    (when `port_width >= width` no extension is needed and `port` is printed directly).
                    ⚠ The self-determined width of `$unsigned(x)` is the port width, not W; it is
                    equivalent to zero-extending to W only in context-determined positions
                    (the right side of `assign`, the parent expression of add/sub/mul); in a
                    self-determined position (e.g. an addition entirely wrapped by
                    `$unsigned(...)`/`$signed(...)`, or a concatenation element)
                    it would drop the carry. All current consumers are in safe positions.
                    This path is deliberately kept entirely separate from the existing `word_var`
                    (which pre-extends into a signed wire only when `signed and port_width <
                    width`), sharing no branch with it
                    — `word_var` prints a **bare identifier**, and its zero-/sign-extension is
                    decided by Verilog itself based on the whole expression's signedness context
                    at that point, a context that can turn the entire expression signed when
                    "all coefficients are ±1" (the module docstring of `(unpublished family).py`
                    has the full derivation); `zext` uses `$unsigned()` to explicitly specify
                    unsigned, a behavior that does not depend on the surrounding signedness
                    context, and the two must never be mixed.

    Signedness semantics ('word'/'sq'/'prod'/'pow' share the same rule — see
    `render_word`): pre-extension into an `s_<port>_w<width>` wire is only needed
    when `signed and port_width < width`; when `port_width >= width`, signedness
    doesn't matter after truncation and the raw port is used directly. `zext` is
    not subject to this rule (see above).
    """

    __slots__ = ("kind", "port", "port_width", "coeff", "signed", "bit",
                 "port2", "port2_width", "signed2", "exp")

    def __init__(self, kind, port, port_width, coeff, signed=False, bit=None,
                 port2=None, port2_width=None, signed2=False, exp=None):
        self.kind = kind
        self.port = port
        self.port_width = port_width
        self.coeff = coeff
        self.signed = signed
        self.bit = bit
        self.port2 = port2
        self.port2_width = port2_width
        self.signed2 = signed2
        self.exp = exp

    def bit_count(self):
        return 1 if self.kind == "bit" else 0


def _to_signed_coeff(c, modulus):
    """c lies within [0, modulus); if > modulus/2, converted to its negative
    representation so the printed number is smaller."""
    return c - modulus if c > modulus // 2 else c


def _assemble_pairs(pairs, width):
    """Folds a series of `(coeff_signed, magnitude_expr)` into tightly laid-out
    add/subtract sequence text (constant term K not included). `pairs` is non-empty.

    This was originally the "pick the first term" assembly logic inline in
    `render_word`, pulled out into its own function in M9 Stage 1: the inner
    parentheses of **common-factor extraction**
    (`g * ( r1 ± r2 ± ... )`) use exactly the same layout rules as the outer
    add/subtract sequence (a positive term is preferred as the first term; when all
    are negative, a term with |coefficient| != 1 is picked so the unary minus can
    attach to a numeric literal), so there's no reason to write it twice — see the
    pricing-rule comment at the top of `render_word`."""
    positive = [i for i, (c, _) in enumerate(pairs) if c > 0]
    if positive:
        first_i = positive[0]
    else:
        # All-negative coefficients: the first term's unary minus **is only free when attached to a numeric literal** (costcount's rule:
        # a unary `-` followed by a number is a negative constant, followed by an identifier is a negation operator costing 1). So the priority is to pick
        # a term with |coefficient| != 1 is chosen as the first term, so it prints as `-8'd3 * x`; if a |coefficient| == 1 term were picked instead, it would
        # prints as `-x`, paying an extra point for nothing.
        # e.g.: terms = [-1*a, -3*b] -> "-a - 8'd3 * b" cost 3, "-8'd3 * b - a" cost 2.
        nonunit = [i for i, (c, _) in enumerate(pairs) if abs(c) != 1]
        first_i = nonunit[0] if nonunit else 0
    order = [first_i] + [i for i in range(len(pairs)) if i != first_i]

    pieces = []
    for pos, idx in enumerate(order):
        c, var = pairs[idx]
        ac = abs(c)
        mag = var if ac == 1 else f"{sized_literal(ac, width)} * {var}"
        if pos == 0:
            pieces.append(mag if c >= 0 else f"-{mag}")
        else:
            pieces.append(f"{'+' if c >= 0 else '-'} {mag}")
    return " ".join(pieces)


# M9 Stage 1 cap: aligned with `quadratic.MAX_TERMS` (the cap on terms fed to
# term cap); see `_common_factor_candidate`'s docstring for the order-of-magnitude discussion of candidate factor counts.
_MAX_EXTRACT_TERMS = 40


def _term_factor_info(t, c, word_var):
    """Returns `(coeff_mag, factors)` or `None` (`sq`/`pow` don't participate in
    extraction — see the factor table in M9_spec.md Stage 1).

    `factors` is `[(key, text), ...]`: `key` is used to decide "whether this factor
    is shared by another term" (equal values count as shared; `var`-kind keys
    include port/port_width/signed, and `word_var` uses that same set of fields, so
    it prints the same text — the two are naturally consistent); `text` is how this
    factor looks printed on its own (`word_var` has already handled signed
    pre-extension).

    ⚠ **The `word`/`zext`/`bit` branches must always call through to
    `_term_var_text`; don't write a second copy here.** Caught during the M9
    tail-end code review: these three branches used to be a verbatim rewrite of
    `_term_var_text`, and **which copy a given term goes through depends on whether
    it got picked for extraction by R1** — a picked term goes through this one
    (`_extract_one` takes `text` straight from `factor_info`), an unpicked one goes
    through `rendered[i]` (`_term_var_text`). If the two copies were ever to drift,
    the same variable could print two different texts within the same expression —
    that's a real risk of producing semantically wrong RTL, not just duplication.
    (This project has already had such a precedent: the change from `zext` printing
    `{N'b0, x}` to `$unsigned()`, where missing one spot cost 37 points.)

    `prod` is a **genuinely** different use case and keeps its own copy:
    `_term_var_text` returns a merged `"(var1 * var2)"` for `prod`, whereas here
    the two operands are needed **independently** as extractable factors, and the
    merged string can't be pulled apart.
    """
    if t.kind in ("word", "zext", "bit"):
        var = _term_var_text(t, word_var)
        if t.kind == "word":
            return abs(c), [(("var", t.port, t.port_width, t.signed), var)]
        if t.kind == "zext":
            return abs(c), [(("zvar", t.port), var)]
        return abs(c), [(("bitvar", t.port, t.bit), var)]
    if t.kind == "prod":
        var1 = word_var(t.port, t.port_width, t.signed)
        var2 = word_var(t.port2, t.port2_width, t.signed2)
        return abs(c), [(("var", t.port, t.port_width, t.signed), var1),
                         (("var", t.port2, t.port2_width, t.signed2), var2)]
    return None  # sq / pow: do not participate in extraction at this stage (see M9_spec.md)


def _term_keys(coeff_mag, factors):
    """The set of factor keys a single term contributes (each term counts only
    once, regardless of how many times the same factor occurs inside that term —
    `a` in `prod(a,a)` counts as 1 shared occurrence; "shared by >=2 terms" counts
    terms, not occurrences)."""
    keys = set()
    if coeff_mag != 1:
        keys.add(("coeff", coeff_mag))
    for key, _text in factors:
        keys.add(key)
    return keys


def _extract_one(key, idxs, rendered, factor_info, width):
    """Extracts factor `key` from `idxs` (a set of term indices that all contain the
    factor `key`), and returns `(g_text, group_inner_pairs)`: `g_text` is how the
    factor itself is printed, and `group_inner_pairs` is what's left of each term
    after extraction, as `(coeff_signed, mag_text)` — the same shape as `rendered`,
    so it can be fed straight back into `_assemble_pairs` (the sum inside the
    parentheses uses the exact same layout rules as the outer add/subtract sequence).
    When `key[0] == "coeff"`, `g_text` is `key[1]` printed as a sized literal."""
    residual_pairs = []
    g_text = sized_literal(key[1], width) if key[0] == "coeff" else None
    for i in idxs:
        c, _mag = rendered[i]
        coeff_mag, factors = factor_info[i]
        if key[0] == "coeff":
            residual_coeff_mag = 1
            residual_factors = factors
        else:
            residual_coeff_mag = coeff_mag
            residual_factors = list(factors)
            for j, (k2, _t) in enumerate(residual_factors):
                if k2 == key:
                    del residual_factors[j]
                    break
            if g_text is None:
                for k2, txt in factors:
                    if k2 == key:
                        g_text = txt
                        break
        if not residual_factors:
            # after extraction this term is left with just a constant (e.g. after pulling x out of `word`'s c*x,
            # leaving c): signed_c uses ±1, and the magnitude text carries the actual |c| directly, so
            # `_assemble_pairs`'s `ac == 1` branch prints this literal as-is, so it won't
            # multiplied again.
            residual_var_text = sized_literal(residual_coeff_mag, width)
            residual_signed_c = 1 if c >= 0 else -1
        elif len(residual_factors) == 1:
            residual_var_text = residual_factors[0][1]
            residual_signed_c = residual_coeff_mag if c >= 0 else -residual_coeff_mag
        else:
            residual_var_text = "(" + " * ".join(t for _k, t in residual_factors) + ")"
            residual_signed_c = residual_coeff_mag if c >= 0 else -residual_coeff_mag
        residual_pairs.append((residual_signed_c, residual_var_text))
    return g_text, residual_pairs


def _common_factor_candidate(word_name, width, k_const, rendered, factor_info,
                              modulus, cost_of_text, base_cost):
    """R1 (common-factor extraction): finds a factor `g` shared by >=2 terms, and
    pulls it out into `g * ( r1 ± r2 ± ... )`, iterating greedily until there's no
    more improvement. Returns `(expr, cost)` or `None` (no factor appears >=2
    times, or extraction didn't strictly lower the cost).

    **Performance guard**: `(unpublished family).py` calls `render_word` inside a
    loop that searches over k (test27 originally took 11.2s), so this first does an
    O(n) pass counting factor occurrences, and returns `None` right away — skipping
    the whole search/pricing loop below — if none occurs >=2 times; the vast
    majority of words take this fast exit at close to zero extra cost.

    Order of magnitude of candidate factors: each term contributes at most 3 keys
    (`prod` contributes 1 coeff key + 2 var keys; `word`/`zext`/`bit` fewer), so
    `_MAX_EXTRACT_TERMS` terms give at most `3 * _MAX_EXTRACT_TERMS` keys — the same
    order of magnitude as the spec's "candidate factors <= 2x MAX_TERMS". Each round
    of the greedy iteration keeps only the factor that "actually saves the most",
    without enumerating subsets.
    """
    n = len(rendered)
    if n > _MAX_EXTRACT_TERMS:
        return None

    def key_counts(idx_list):
        counts = {}
        for i in idx_list:
            info = factor_info[i]
            if info is None:
                continue
            for k in _term_keys(*info):
                counts[k] = counts.get(k, 0) + 1
        return counts

    if not any(v >= 2 for v in key_counts(range(n)).values()):
        return None

    def cost_of(pairs):
        expr = _assemble_pairs(pairs, width)
        k = k_const % modulus
        if k != 0:
            expr += f" + {sized_literal(k, width)}"
        return expr, cost_of_word_assign(word_name, width, expr, cost_of_text)

    remaining = list(range(n))
    extra_pairs = []  # each extracted group contributes exactly one (coeff=+1, "g * (...)") pair
    best_cost = base_cost
    changed = False

    while True:
        counts = key_counts(remaining)
        candidates = sorted(k for k, v in counts.items() if v >= 2)
        if not candidates:
            break

        idx_by_key = {}
        for i in remaining:
            info = factor_info[i]
            if info is None:
                continue
            for k in _term_keys(*info):
                idx_by_key.setdefault(k, []).append(i)

        best = None  # (gain, new_remaining, group_pair, new_cost)
        for key in candidates:
            idxs = idx_by_key[key]
            g_text, group_inner_pairs = _extract_one(key, idxs, rendered, factor_info, width)
            group_inner_text = _assemble_pairs(group_inner_pairs, width)
            group_pair = (1, f"{g_text} * ({group_inner_text})")
            new_remaining = [i for i in remaining if i not in idxs]
            cand_pairs = [rendered[i] for i in new_remaining] + extra_pairs + [group_pair]
            _cand_expr, cand_cost = cost_of(cand_pairs)
            gain = best_cost - cand_cost
            if gain > 0 and (best is None or gain > best[0]):
                best = (gain, new_remaining, group_pair, cand_cost)

        if best is None:
            break
        gain, new_remaining, group_pair, new_cost = best
        remaining = new_remaining
        extra_pairs.append(group_pair)
        best_cost = new_cost
        changed = True

    if not changed:
        return None
    final_pairs = [rendered[i] for i in remaining] + extra_pairs
    expr, cost = cost_of(final_pairs)
    return expr, cost


def _term_var_text(t, word_var):
    """The "bare product text" (no coefficient) of a single term. `render_word`'s
    main loop and M9 Stage 2's mux recovery (after reconstructing a `(kind,key)`
    back into a `Term`) share this rule.

    ⚠ **`word`/`zext`/`bit`/`sq`/`pow` have only this one copy** — the
    corresponding branches of `_term_factor_info` always call through to this one,
    to avoid maintaining two copies that could drift apart over time. The only
    exception is `prod`: `_term_factor_info` needs the two operands to be
    **independently** extractable factors, and can't work from the merged
    `"(var1 * var2)"` produced here, so that branch has its own copy.
    (This used to say "only one copy exists", but the M9 tail-end code review
    found that `_term_factor_info` did in fact have its own verbatim rewrite of
    `word`/`zext`/`bit` — that has since been deduplicated, and this note has been
    updated to match reality.)"""
    if t.kind == "word":
        return word_var(t.port, t.port_width, t.signed)
    if t.kind == "zext":
        # M6a: always zero-extend, never going through `word_var`/`$signed()` (see the Term docstring).
        #
        # ⚠ **Use `$unsigned()`, never `{N'b0, x}` concatenation.** The first version used concatenation,
        # because `costcount` reported 0 points for `{}` back then — but that was **the tool itself not being implemented yet**,
        # is not what the spec says is free: Appendix A explicitly lists `{}` as chargeable. After adding the count, testing showed
        # test24's output went from 91 to **129** (38 concatenation operators), from "beats the hand-written solution by 1 point"
        # turns into "losing by 37 points". `$unsigned()` is a system function and isn't in Appendix A's
        # operator table (the same reading `$signed()` gets — all 29 solutions rely on it),
        # cost 0, and the semantics are exactly the same: an operand becoming unsigned -> zero-extended in a wider context,
        # also pulls the whole expression to unsigned, which is exactly what's needed here.
        return f"$unsigned({t.port})"
    if t.kind == "sq":
        # parentheses are free (costcount doesn't count `()`), but without them `-a**2` would be parsed by Verilog as
        # `(-a)**2` rather than `-(a**2)` — the two are only numerically equal when a is an odd negative number, and the risk isn't worth taking.
        return f"({word_var(t.port, t.port_width, t.signed)} ** 2)"
    if t.kind == "prod":
        var1 = word_var(t.port, t.port_width, t.signed)
        var2 = word_var(t.port2, t.port2_width, t.signed2)
        return f"({var1} * {var2})"
    if t.kind == "pow":
        # the parentheses copy `sq`'s pattern (see the comment above); the exponent is printed as an unsized decimal literal —
        # The interaction between `**`'s precedence and unary minus carries exactly the same risk as `sq`.
        return f"({word_var(t.port, t.port_width, t.signed)} ** {t.exp})"
    if t.kind == "known":
        # M10 (R3): this is a reference to a substituted-in 'known word'; `t.port` is exactly that word's
        # the identifier itself (already an existing output port), printed as-is, without going through `word_var`
        # — G4 already guarantees `wq >= width` (a wider word truncated in a narrower context mod
        # 2**width is naturally correct), so there's no signed pre-extension issue (G5 already blocks those cases).
        return t.port
    return f"{t.port}[{t.bit}]"


def _term_key_for_merge(t):
    """Folds a `Term` into a tuple usable as a dict key — needed by M9 Stage 2's
    mux recovery to merge "terms that don't involve the selector s" (the
    classification rule's "everything else -> goes into N"). It includes full
    information such as port_width/signed precisely to avoid mistakenly merging
    the same port under different signed pre-extension contexts into a single
    term (which would print the wrong var text)."""
    if t.kind == "word":
        return ("word", t.port, t.port_width, t.signed)
    if t.kind == "zext":
        return ("zext", t.port)
    if t.kind == "bit":
        return ("bit", t.port, t.bit)
    if t.kind == "prod":
        return ("prod", t.port, t.port_width, t.signed, t.port2, t.port2_width, t.signed2)
    if t.kind == "sq":
        return ("sq", t.port, t.port_width, t.signed)
    return ("pow", t.port, t.port_width, t.signed, t.exp)


def _term_from_key(key, coeff):
    """The inverse of `_term_key_for_merge`: reconstructs a `Term` from a key and a
    coefficient, and feeds it to `_term_var_text` to render. `port_width` doesn't
    affect the rendered text for `zext`/`bit`/`known`, so it's safe to fill it with
    `None` when reconstructing.

    `kind == "known"` is not a key `_term_key_for_merge` would ever produce — it's
    a pseudo key `("known", q_name)` synthesized by M10 R3
    (`_apply_known_word_matches`), meaning "this slot is replaced by a reference to
    some existing output port"; only this function and
    `_term_var_text`/`_term_factor_info` (which naturally falls through to None —
    see that function) know about it."""
    kind = key[0]
    if kind == "word":
        _, port, pw, signed = key
        return Term("word", port, pw, coeff, signed=signed)
    if kind == "zext":
        _, port = key
        return Term("zext", port, None, coeff)
    if kind == "known":
        _, port = key
        return Term("known", port, None, coeff)
    if kind == "bit":
        _, port, bit = key
        return Term("bit", port, None, coeff, bit=bit)
    if kind == "prod":
        _, p1, pw1, s1, p2, pw2, s2 = key
        return Term("prod", p1, pw1, coeff, signed=s1, port2=p2, port2_width=pw2, signed2=s2)
    if kind == "sq":
        _, port, pw, signed = key
        return Term("sq", port, pw, coeff, signed=signed)
    _, port, pw, signed, exp = key
    return Term("pow", port, pw, coeff, signed=signed, exp=exp)


def _mux_selector_candidates(terms, modulus):
    """M9 Stage 2: finds the list of ports that can serve as the selector `s`
    (deduplicated by order of appearance in terms, deterministic — does not depend
    on any set iteration order; see the lesson in git log 8e21d13).

    Three conditions (Stage 2 spec, ⚠⚠ the third is a hard correctness requirement,
    not a performance tradeoff):
    - appears as a `word` kind, or as an operand of some `prod`
    - `port_width == 1` at that occurrence
    - the `signed` flag at that occurrence is **False**

    `search_signs()` in `solver/quadratic.py:190-224` greedily flips signedness for
    **all** ports (`all_signed` is one of the first two starting points tried) and
    excludes no 1-bit ports whatsoever (contrast with `solver/linear.py:83`'s
    `if pwidth > 1:`, which only applies to `linear.py`'s own path — `quadratic.py`
    has no corresponding guard). So "a 1-bit port gets marked signed" is not merely
    a theoretical possibility — `(unpublished family).py`/`(unpublished family).py`
    both go through `quadratic.py`'s fitting, and this really does show up in real
    data. When a 1-bit port is marked signed, `word_var` prints `$signed(s)`, and
    the value of s=1 is **-1**, not +1, so the identity
    `x*s + y - y*s ≡ s?x:y` simply fails to hold — this guard must not be removed,
    nor can we decide based on whether the corpus happens to trigger it.

    Also, `sq(s)`/`pow(s)`/`prod(s,s)` (even though s² = s does hold) are not
    handled at this stage; when they occur, this `s` is abandoned entirely (see how
    the caller of `_classify_for_selector` handles it).
    """
    eligible = []
    seen = set()
    disqualified = set()
    for t in terms:
        c = _to_signed_coeff(t.coeff % modulus, modulus)
        if c == 0:
            continue
        if t.kind in ("sq", "pow"):
            disqualified.add(t.port)
        elif t.kind == "prod" and t.port == t.port2:
            disqualified.add(t.port)
        elif t.kind == "word":
            if t.port_width == 1 and not t.signed and t.port not in seen:
                seen.add(t.port)
                eligible.append(t.port)
        elif t.kind == "prod":
            if t.port_width == 1 and not t.signed and t.port not in seen:
                seen.add(t.port)
                eligible.append(t.port)
            if t.port2_width == 1 and not t.signed2 and t.port2 not in seen:
                seen.add(t.port2)
                eligible.append(t.port2)
    return [p for p in eligible if p not in disqualified]


def _classify_for_selector(terms, s, k_const, modulus):
    """M9 Stage 2: folds `terms` (together with `k_const`) into two `{key: coeff}`
    buckets `(N, P)` per the spec's classification rule.

    `N` starts out pre-loaded with `k_const` (key is the string `"K"`, which
    cannot collide with any tuple key from `_term_key_for_merge`) — this is the
    direct embodiment of the spec's statement that "N includes the constant term
    K": `expr = N + s·P` must equal the original `k_const + Σterms`, and `k_const`
    doesn't belong to any single term, so it can only go into N up front.

    A term is classified into P only if *that particular occurrence itself*
    satisfies the three conditions of `_mux_selector_candidates`
    (`port_width==1` and `signed is False`) — if the same port appears in another
    term with `signed=True` or `port_width!=1`, that term always falls into `N`
    ("everything else -> goes into N"), and is **never** mistaken for a
    contribution to s — otherwise that would be exactly the correctness risk the
    spec calls out.
    """
    N = {"K": k_const % modulus}
    P = {}
    for t in terms:
        c = _to_signed_coeff(t.coeff % modulus, modulus)
        if c == 0:
            continue
        if t.kind == "word" and t.port == s and t.port_width == 1 and not t.signed:
            P["K"] = P.get("K", 0) + c
        elif t.kind == "prod" and t.port == s and t.port_width == 1 and not t.signed:
            key = ("word", t.port2, t.port2_width, t.signed2)
            P[key] = P.get(key, 0) + c
        elif t.kind == "prod" and t.port2 == s and t.port2_width == 1 and not t.signed2:
            key = ("word", t.port, t.port_width, t.signed)
            P[key] = P.get(key, 0) + c
        else:
            key = _term_key_for_merge(t)
            N[key] = N.get(key, 0) + c
    N = {k: v for k, v in ((k, _to_signed_coeff(v % modulus, modulus)) for k, v in N.items())
         if v != 0}
    P = {k: v for k, v in ((k, _to_signed_coeff(v % modulus, modulus)) for k, v in P.items())
         if v != 0}
    return N, P


def _terms_to_bucket(k_const, terms, width):
    """Folds (K, terms) into a normalized `{key: coeff}` bucket (`"K"` is the
    constant term); every coefficient is in `_to_signed_coeff`'s signed
    representation, with zero values already excluded. M10 (R3) uses this to build
    both "bucket B to be rendered" and "Q of known words", and both always use the
    same modulus — the consumer's target width `width`, not the width of the word
    that `terms`/`k_const` originally belonged to. For Q this is correct precisely
    because of the G4 guard in M10_spec.md section 3 (`wq >= width`):
    `(X mod 2**wq) mod 2**width == X mod 2**width` (shrinking the modulus lets you
    take the mod directly against the final modulus, without first taking it
    against wq), so a known word's "correct bit pattern" in a narrower context is
    the same thing as re-normalizing its coefficients directly against `width`."""
    modulus = 1 << width
    raw = {}
    kc = k_const % modulus
    if kc != 0:
        raw["K"] = kc
    for t in terms:
        c = t.coeff % modulus
        if c == 0:
            continue
        key = _term_key_for_merge(t)
        raw[key] = raw.get(key, 0) + c
    return {k: v for k, v in ((k, _to_signed_coeff(v % modulus, modulus)) for k, v in raw.items())
            if v != 0}


def _key_needs_signed_prextension(key, width):
    """M10 G5: for the term this bucket key represents, will `render_word`'s main
    loop's `word_var` pre-extend an orphan `wire signed` for it
    (`signed and port_width < width`)? `zext`/`bit`/`known`/`"K"` are not subject
    to the signed pre-extension rule and are always False (see the signedness
    semantics subsection of the `Term` docstring)."""
    kind = key[0]
    if kind in ("word", "sq"):
        _, _port, pw, signed = key
        return bool(signed) and pw < width
    if kind == "pow":
        _, _port, pw, signed, _exp = key
        return bool(signed) and pw < width
    if kind == "prod":
        _, _p1, pw1, s1, _p2, pw2, s2 = key
        return (bool(s1) and pw1 < width) or (bool(s2) and pw2 < width)
    return False


def _apply_known_word_matches(bucket, width, known_word_dicts):
    """R3 (M10): signed subset matching. Greedily iterates over
    `known_word_dicts` (`[(q_name, q_dict), ...]`, already sorted in declaration
    order — see how `render_word` builds this list): for each `q`, try
    `sigma` in (+1, -1); if `sigma * q_dict` is a subset of the current `bucket`
    (every key's coefficient matches, keys `q_dict` doesn't have are treated as 0)
    and none of the keys that would be replaced triggers G5, remove those keys from
    the bucket and replace them with a single `("known", q_name)` term (coefficient
    = sigma), and on a hit keep trying the next q (greedy, deterministic, no
    combinatorial search — see M10_spec.md 2.4). Returns a new dict without
    mutating the original `bucket`."""
    if not known_word_dicts:
        return bucket
    modulus = 1 << width
    result = dict(bucket)
    for q_name, q_dict in known_word_dicts:
        if not q_dict:
            continue
        for sigma in (1, -1):
            ok = True
            for k, v in q_dict.items():
                want = _to_signed_coeff((sigma * v) % modulus, modulus)
                have = _to_signed_coeff(result.get(k, 0) % modulus, modulus)
                if have != want:
                    ok = False
                    break
            if not ok:
                continue
            if any(_key_needs_signed_prextension(k, width) for k in q_dict):
                continue
            for k in q_dict:
                del result[k]
            key = ("known", q_name)
            result[key] = result.get(key, 0) + sigma
            break
    return result


def _bucket_to_text(bucket, width, word_var):
    """Flattens a `{key: coeff}` bucket straight into add/subtract sequence text,
    **without running R1** (dedicated to M10 top-level R3 candidates — see
    M10_spec.md 2.3 point 1: "flatten and render after substitution, no need to
    rerun R1"). Returns `None` if the bucket is entirely empty (even K is 0)."""
    modulus = 1 << width
    pairs = []
    k_val = 0
    for key, coeff in bucket.items():
        if key == "K":
            k_val = coeff
            continue
        if coeff == 0:
            continue
        t = _term_from_key(key, coeff)
        pairs.append((coeff, _term_var_text(t, word_var)))
    if not pairs:
        if k_val == 0:
            return None
        return sized_literal(k_val % modulus, width)
    text = _assemble_pairs(pairs, width)
    if k_val != 0:
        text += f" + {sized_literal(k_val % modulus, width)}"
    return text


def _render_bucket(bucket, width, word_var, word_name=None, cost_of_text=None,
                    known_word_dicts=None):
    """Renders a `{key: coeff}` bucket into a piece of add/subtract sequence text (no
    enclosing parentheses). The `"K"` key is printed directly as a sized literal
    (the rule for the spec's "constant term" is exactly the same as `render_word`'s
    handling of `k_const`). Returns `None` if the bucket is entirely empty (even K
    is 0) — the caller uses this to decide whether R should be omitted entirely.

    When `word_name`/`cost_of_text` are given, an extra pass of R1 (common-factor
    extraction) is run inside the bucket: the section of M9_spec.md Stage 2 titled
    "cost-driven selection together with Stage 1" explicitly lists R1, R2, and
    "both" as candidates — the R/E0/E1 buckets split off by R2 are independent of
    each other, and each of them may still have its own common factor worth
    extracting again (test21's hand-written solution is exactly "common-factor
    extraction on coefficients 2/4" happening together with "in3 mux recovery" —
    R2 alone isn't enough; see the Stage 2 acceptance report). K (the constant)
    inside the bucket happens to correspond exactly to the `k_const` role in
    `_common_factor_candidate`'s interface, and is passed straight in without
    extra handling.

    When `known_word_dicts` is given, an initial pass of R3
    (`_apply_known_word_matches`, M10) is run first — this is the **single
    insertion point** shared by `_mux_candidate`'s three buckets R/E0/E1 (see
    M10_spec.md 2.3 point 2), and R1 runs only after the substitution, so the
    `known` terms produced by R3 also get a chance to be packed together by R1
    (although currently neither test04 nor test23 needs this extra layering)."""
    if known_word_dicts:
        bucket = _apply_known_word_matches(bucket, width, known_word_dicts)
    modulus = 1 << width
    pairs = []
    factor_info_list = []
    k_val = 0
    for key, coeff in bucket.items():
        if key == "K":
            k_val = coeff
            continue
        if coeff == 0:
            continue
        t = _term_from_key(key, coeff)
        pairs.append((coeff, _term_var_text(t, word_var)))
        factor_info_list.append(_term_factor_info(t, coeff, word_var))
    if not pairs:
        if k_val == 0:
            return None
        return sized_literal(k_val % modulus, width)
    text = _assemble_pairs(pairs, width)
    if k_val != 0:
        text += f" + {sized_literal(k_val % modulus, width)}"
    if word_name is None or cost_of_text is None:
        return text
    flat_cost = cost_of_word_assign(word_name, width, text, cost_of_text)
    r1 = _common_factor_candidate(word_name, width, k_val, pairs, factor_info_list,
                                   modulus, cost_of_text, flat_cost)
    if r1 is not None:
        r1_expr, r1_cost = r1
        if r1_cost < flat_cost:
            return r1_expr
    return text


def _render_bucket_or_zero(bucket, width, word_var, word_name=None, cost_of_text=None,
                            known_word_dicts=None):
    """Same as `_render_bucket`, but an empty bucket prints `sized_literal(0, width)`
        (free) instead of `None` — `E0`/`E1` are branches of a ternary and can't be
        omitted."""
    text = _render_bucket(bucket, width, word_var, word_name=word_name, cost_of_text=cost_of_text,
                           known_word_dicts=known_word_dicts)
    return text if text is not None else sized_literal(0, width)


def _mux_candidate(word_name, width, k_const, terms, modulus, cost_of_text, word_var,
                    known_word_dicts=None):
    """R2 (mux recovery, M9 Stage 2): finds a selector `s` that rewrites
    `expr = N + s·P` into `render(R) + ( s ? render(E1) : render(E0) )`.
    Cost-driven: every candidate `s` is actually rendered and priced with
    `cost_of_word_assign`, and the one with the lowest cost is taken; returns
    `(expr, cost)` or `None` (no usable selector exists).

    **Correctness argument (mixed signedness — M9_spec.md Stage 2 spells this out
    explicitly; it cannot rest on "Stage 1 never had an issue")**: every operand
    inside `R`/`E0`/`E1` is either a `sized_literal` (W bits, free under costcount,
    already the correct mod 2^W bit pattern), or comes from the existing rule
    produced by `_term_var_text`/`word_var` — when `port_width >= width` the raw
    port is used directly (its mod 2^W bit pattern after truncation doesn't depend
    on signedness), and when `port_width < width` and `signed` it is pre-extended
    into a `width`-bit `wire signed` (whose bit pattern is itself the correct
    mod 2^W signed value). In other words, **the mod-2^W bit pattern of every leaf
    operand never changes depending on whether the surrounding expression is judged
    signed or unsigned by Verilog** — this is exactly the invariant the rule table
    at the top of `model.py` already relies on ("signed operands are always
    pre-extended first into a wire signed of the target word width W; the main
    expression only uses the pre-extended wire, never mixing signed/unsigned inside
    one expression").

    The LRM rule for `?:` (if either branch is unsigned, the whole ternary's
    **interpretation** is treated as unsigned) only decides how Verilog interprets
    the sign of this bit pattern — it does not change the bit pattern itself; fixed
    -width two's-complement addition is inherently indifferent to "whether this bit
    range should be interpreted as signed or unsigned" (this is exactly why the
    problem's mod 2^W semantics holds in the first place). So even if one of `E0`/
    `E1` is entirely `$unsigned()`/raw ports (unsigned) and the other contains a
    signed pre-extension wire (signed), as long as every leaf operand's width
    follows the rule above, the final W-bit bit pattern produced by the `assign`
    is still correct — the only real failure mode is "an operand narrower than
    width that was not correctly extended per the `word_var`/`zext` rule", and
    Stage 2 fully reuses the existing `word_var`/`_term_var_text` and introduces no
    new operand-selection rule, so this invariant continues to hold and needs no
    extra handling.
    """
    candidates = _mux_selector_candidates(terms, modulus)
    if not candidates:
        return None
    best = None
    for s in candidates:
        N, P = _classify_for_selector(terms, s, k_const, modulus)
        if not P:
            continue
        NP = dict(N)
        for k, v in P.items():
            NP[k] = NP.get(k, 0) + v
        R = {k: v for k, v in N.items() if k not in P}
        E0 = {k: N.get(k, 0) for k in P}
        E1 = {k: NP.get(k, 0) for k in P}
        s_var = word_var(s, 1, False)
        r_text = _render_bucket(R, width, word_var, word_name=word_name, cost_of_text=cost_of_text,
                                 known_word_dicts=known_word_dicts)
        e0_text = _render_bucket_or_zero(E0, width, word_var, word_name=word_name,
                                          cost_of_text=cost_of_text,
                                          known_word_dicts=known_word_dicts)
        e1_text = _render_bucket_or_zero(E1, width, word_var, word_name=word_name,
                                          cost_of_text=cost_of_text,
                                          known_word_dicts=known_word_dicts)
        mux_text = f"({s_var} ? {e1_text} : {e0_text})"
        expr = f"{r_text} + {mux_text}" if r_text is not None else mux_text
        cost = cost_of_word_assign(word_name, width, expr, cost_of_text)
        if best is None or cost < best[1]:
            best = (expr, cost)
    return best


def render_word(word_name, width, k_const, terms, signed_prefix="s_", known_words=None):
    """Folds (K, terms) into a single `assign` right-hand-side expression text.

    `signed_prefix` must match the prefix the caller later passes to
    `signed_wire_name` when generating the `wire signed ...` declaration,
    otherwise the name referenced inside the expression won't match the declared
    name (see `fresh_prefix`: with no collision, the default is still "s_", exactly
    as before this change).

    M9 Stage 1: besides the original flattened add/subtract sequence, this also tries
    "common-factor extraction" (`_common_factor_candidate`), actually prices both,
    and takes whichever has the lower cost; on a tie, the original flattened
    rendering wins (preserving byte-identity for the existing 42 outputs).

    `known_words` (M10, R3): `{q_name: (k_const_q, terms_q, wq)}`; the caller
    (`main._candidate`) has already run the four eligibility filters G1
    (declaration order)/G2 (cone check)/G3 (has a model)/G4 (width) for this
    consumer, and it's **sorted in declaration order** (dict insertion order).
    Defaults to `None`, which behaves the same as passing `{}` — callers that don't
    wire up this eligibility table (`share.py`/`compare.py`/... see M10_spec.md
    section 4) are completely unaffected. This is folded into
    `_terms_to_bucket`'s normalized shape (modulus uses this word's own `width` —
    see the derivation in that function's docstring), and handed to R3
    (`_apply_known_word_matches`, with G5 guarding inline) to try one substitution
    on this flattened top-level pass, and it's also passed into R2
    (`_mux_candidate`) so the R/E0/E1 buckets each get one chance at substitution
    too (the single insertion point in `_render_bucket` — see that function's
    docstring).

    Returns (expr_text, signed_wire_needs):
    signed_wire_needs is a set of (port, port_width, width) tuples, from which the
    caller must generate
    `wire signed [width-1:0] <signed_prefix><port>_w<width>;` +
    `assign ... = $signed(port);`.
    """
    modulus = 1 << width
    signed_needs = set()
    known_word_dicts = [(q_name, _terms_to_bucket(kc_q, terms_q, width))
                         for q_name, (kc_q, terms_q, _wq) in (known_words or {}).items()]

    def word_var(port, port_width, signed):
        """Shared operand-selection rule for 'word'/'sq'/'prod': when port_width < width
        and signed is needed, pre-extend into a width-bit signed wire; otherwise,
        after truncation signedness doesn't matter, so use the raw port directly."""
        if signed and port_width < width:
            signed_needs.add((port, port_width, width))
            return signed_wire_name(port, width, prefix=signed_prefix)
        return port

    rendered = []  # (coeff_signed, magnitude_expr)
    factor_info = []  # same index as rendered; word/zext/prod/bit are (coeff_mag, factors), sq/pow are None
    for t in terms:
        c = _to_signed_coeff(t.coeff % modulus, modulus)
        if c == 0:
            continue
        var = _term_var_text(t, word_var)
        rendered.append((c, var))
        factor_info.append(_term_factor_info(t, c, word_var))

    if not rendered:
        # a pure constant (shouldn't happen for a word that has support, but this path is kept as a fallback).
        return sized_literal(k_const % modulus, width), signed_needs

    expr = _assemble_pairs(rendered, width)
    k = k_const % modulus
    if k != 0:
        expr += f" + {sized_literal(k, width)}"

    # R1 (common-factor extraction, Stage 1) + R2 (mux recovery, Stage 2). `cost_of_text`
    # obtained via lazy import (the same existing convention as `(unpublished family).py._leaf_cost`), in order to
    # so that `model.py` doesn't hard-bind `tools/` onto sys.path at module load time; if it can't be obtained, it
    # skips both stages directly, falling back to the original flattened rendering (doesn't affect correctness, just loses a chance to save points).
    #
    # **Cost-driven selection, not a fixed order**: the existing flattened rendering is always one of the candidates; R1, R2
    # Each is actually priced against "the current best candidate", and only a strict improvement replaces it — on a tie, the earlier
    # the candidate wins (priority order flatten > R1 > R2, all guaranteed by strict `<`, and won't happen just because
    # "do both" and end up double-counting savings without actually saving anything). R2 currently only compares "flatten vs R1"
    # compares against the current best candidate in it, and does not recursively rerun R1 inside R2's R/E0/E1
    # (M9_spec.md allows but does not require "doing both"; the target for test21/test04/test23
    # a number doesn't need this recursion — see the Stage 2 acceptance report).
    try:
        from costcount import cost_of_text
    except ImportError:
        cost_of_text = None
    if cost_of_text is not None:
        best_cost = cost_of_word_assign(word_name, width, expr, cost_of_text)
        best_expr = expr

        r1 = _common_factor_candidate(word_name, width, k_const, rendered,
                                       factor_info, modulus, cost_of_text, best_cost)
        if r1 is not None:
            r1_expr, r1_cost = r1
            if r1_cost < best_cost:
                best_cost, best_expr = r1_cost, r1_expr

        # R3 (M10): signed subset matching, tried once as a substitution on this flattened top-level pass only,
        # after substitution, flatten and render directly, **without rerunning R1** (see M10_spec.md 2.3 point 1;
        # R2's R/E0/E1 buckets each have their own insertion point — see `_render_bucket`).
        if known_word_dicts:
            bucket0 = _terms_to_bucket(k_const, terms, width)
            bucket1 = _apply_known_word_matches(bucket0, width, known_word_dicts)
            if bucket1 != bucket0:
                r3_text = _bucket_to_text(bucket1, width, word_var)
                if r3_text is not None:
                    r3_cost = cost_of_word_assign(word_name, width, r3_text, cost_of_text)
                    if r3_cost < best_cost:
                        best_cost, best_expr = r3_cost, r3_text

        r2 = _mux_candidate(word_name, width, k_const, terms, modulus,
                             cost_of_text, word_var, known_word_dicts=known_word_dicts)
        if r2 is not None:
            r2_expr, r2_cost = r2
            if r2_cost < best_cost:
                best_cost, best_expr = r2_cost, r2_expr

        expr = best_expr

    return expr, signed_needs


def atom_safe(expr):
    """Wraps `render_word`'s return value into a form that can be safely inserted
    into another expression.

    In Verilog, `?:` has lower precedence than `+`/`-`, so concatenating directly
    would get reassociated (`q + s ? x : y` == `(q + s) ? x : y`). Parentheses are
    **free** in `tools/costcount.py` (there is no rule anywhere in the file that
    charges for `(` `)`), so wrapping costs nothing.

    Wrapping happens only when the string contains `?`, so that Stage 0 stays
    **byte-identical** on the existing 42 outputs — currently no family ever prints
    a `?`.
    """
    return f"({expr})" if "?" in expr else expr


def cost_of_word_assign(word_name, width, expr, cost_of_text):
    """Wraps a single assign as the smallest possible snippet and feeds it to
    `cost_of_text` to estimate cost (reusing the existing pricing rules)."""
    snippet = f"output [{width - 1}:0] {word_name};\nassign {word_name} = {expr};\n"
    total, _ = cost_of_text(snippet)
    return total
