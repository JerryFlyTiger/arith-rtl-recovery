"""Family E: guard-product-sum (mux) extractor.

Target shape (the only target; see the manual solutions for test20/test30):

    out = Sum_i leftover_i + Sum_j (ctrl_j ? 0 : a_j*b_j)     ...kind "A"
                       or   (ctrl_j ? a_j*b_j : 0)         ...kind "B"

`ctrl_j` is an original 1-bit input port, `(a_j, b_j)` are original multi-bit input ports, each group
pairwise disjoint; `leftover_i` is an original port that isn't controlled by any guard and is simply
added in.

**Grouping relies on black-box toggle probing, not algebraic fitting** — this is the key to
feasibility: without grouping, directly enumerating combinations of `(a,b,ctrl)` assignments is on
the order of 10^8, infeasible; after grouping, each group has only 2 options ("guard polarity") left,
groups are mutually independent and can be verified one at a time, total work is O(k) (k = number of
groups), trivial.

## Probe design

A word's primary-input support is split into two buckets by port width: `G` (1-bit, guard
candidates), `M` (multi-bit, product-factor candidates).

1. **Classify by pulling each one high alone**: for each `p in M`, under both "all guards 0" and "all
   guards 1", pull `p` alone to all-1s while keeping everything else (including `p`'s own group
   partner) at 0, and observe the output. If it's 0 under both guard states -> `p` is a product
   candidate (the partner is still 0, so the product must be 0, regardless of whether its own
   group's guard is 0 or 1). If the two guard states give the same nonzero answer -> `p` is an
   unguarded bare addend. Any other case (the two states give different nonzero values) means the
   shape doesn't fit, and the whole word is abandoned (never forced through).

2. **Pairwise matching**: for each pair of candidate product factors, try pulling both of them high
   simultaneously under "all guards 0" and "all guards 1"; exactly one of the two states being
   nonzero identifies a genuine product pair, and which state is nonzero also determines the polarity
   candidate for that group (nonzero when all-0 -> candidate for kind A; nonzero when all-1 ->
   candidate for kind B; both nonzero or both zero -> not a genuine product pair, skip). Pairs are
   taken greedily in enumeration order, never reusing a factor already paired; an incomplete pairing
   (leftover unpaired product factors) abandons the whole word.

3. **Locate the controlling guard per group**: for each group (a,b), pull each candidate guard `g`
   high individually (all other guards stay 0, a,b stay all-1s) — since a guard only controls its own
   group (structurally disjoint), this probe can only produce one of two outcomes: `g` is this
   group's real guard (goes to zero for kind A, or equals the nonzero product value recorded in step
   2 for kind B), or `g` is unrelated to this group (stays nonzero for kind A, stays 0 for kind B).
   Adopt it if exactly one `g` hits; if the hit count != 1, or that `g` has already been claimed by
   another group, abandon the whole word.

Summing the bare addends and each group's product gives the candidate expression; finally, two
independent (train/accept) vector sets are each compared against `nl.simulate()`'s ground truth, and
any mismatch abandons the whole word — this step isn't "fitting" (grouping itself is a structural,
deterministic probe, not statistical fitting), it's purely a defensive double-check, echoing the
compare family's discipline of separating the training set from the acceptance set.

**This family treats product factors as unsigned throughout** (`render_expr`/`_formula` never
produces `$signed()`). If the real circuit's product is actually signed, the unsigned formula will
mismatch ground truth on signedness-related vectors, and the acceptance check above naturally rejects
it — but whether random verification vectors happen to hit that discrepancy is pure luck, especially
when the word width is smaller than the natural width of `a*b`, where high-bit truncation makes the
signed/unsigned formulas congruent over most of the input space. `build_context` therefore generates,
for each group `(a,b)`, extra boundary vectors where "both factors simultaneously approach the
signedness-flip point" (see `_sign_boundary_vectors`) and folds them into the acceptance set, turning
this line of defense from "luck" into "deterministic". Adding signedness-search capability (so this
family can actually solve signed products) is out of scope for this milestone; for now the goal is
just making "rejection" itself reliable.
"""
import itertools
from collections import namedtuple

from model import sized_literal

Group = namedtuple("Group", "guard a b kind active_val")

WordPlan = namedtuple("WordPlan", "groups leftover")

Context = namedtuple(
    "Context",
    "plans val_vectors val_results verify_vectors verify_results "
    "boundary_vectors boundary_results",
    defaults=({}, {}),
)


def split_bit(bitname):
    if "[" in bitname:
        p, rest = bitname.split("[", 1)
        return p, int(rest[:-1])
    return bitname, 0


def _support_ports(nl, support, name, width):
    s = set()
    for b in nl.bits([(name, width)]):
        s |= support.get(b, frozenset())
    return sorted({split_bit(x)[0] for x in s})


def _boundary_points(w):
    """The set of values near a single port's signedness-flip point (the same construction as
    `compare._gen_sign_boundary_vectors`, but compare is not imported here — to avoid creating an
    unnecessary cross-family dependency)."""
    if w <= 1:
        return [0, 1]
    half = 1 << (w - 1)
    pts = sorted({0, half - 1, half, half + 1, (1 << w) - 1})
    return [p for p in pts if 0 <= p < (1 << w)]


def _sign_boundary_vectors(zero_vec, port_widths, plan):
    """Gap 1: the mux family treats product factors as unsigned throughout; `try_mux`'s only line of
    defense is "if the real circuit is actually signed, the unsigned formula will mismatch ground
    truth on signedness-related vectors" — but whether random verification vectors happen to hit
    that discrepancy is pure luck (especially when the word width is smaller than the natural width
    of `a*b`, where high-bit truncation makes signed/unsigned formulas congruent over most of the
    input space, and the discrepancy only shows up in a small corner). Here, for **every** group
    `(a,b)` in the plan, boundary vectors are constructed where "both factors simultaneously approach
    the signedness-flip point" (one set per guard state), turning this line of defense from "luck"
    into "deterministic". This is a different shape from the compare family's boundary vectors: that
    one is a "linear combination of atoms", whereas here we need "both factors of the same group
    flipping simultaneously", so it can't be reused directly and is deliberately implemented
    independently.

    Gap 2 (once misjudged, since reverted): in `_formula`, kind A groups are only active when
    `gv==0`, kind B only when `gv==1` — when a guard is set to a non-active state, the Python-side
    model `_formula` indeed never evaluates that group's product term; that inference step is
    correct. But one version once jumped straight from this to "so this whole line of defense has
    zero discriminating power for signedness, testing only the active state is enough" — that
    conclusion only proves that "the `_formula` model itself" is insensitive to a/b, not that the
    "real circuit"'s inactive branch is also independent of a/b. The latter was only verified at the
    single probe corner in `_find_plan` step 1 (`p=full(p), q=full(q)`) to be constant 0 (kind A) or
    constant active_val (kind B) — that's a black-box probe, not an algebraic proof, and corners the
    probe never touched give no guarantee of consistent behavior. If the real circuit's inactive
    branch is not truly constant (matching exactly at the `full()` corner but not near
    `_boundary_points`), boundary vectors that only test the active state will never hit it, and the
    candidate will be wrongly accepted, producing non-equivalent RTL. So both guard states must be
    tested: the inactive-state vectors aren't testing signedness truncation, they're expanding the
    verification scope for the structural assumption "the real circuit's inactive branch really is
    constant" from a single corner to the entire set of boundary points."""
    vecs = []
    for g in plan.groups:
        pts_a = _boundary_points(port_widths[g.a])
        pts_b = _boundary_points(port_widths[g.b])
        for guard_val in (0, 1):
            for va in pts_a:
                for vb in pts_b:
                    vec = dict(zero_vec)
                    vec[g.guard] = guard_val
                    vec[g.a] = va
                    vec[g.b] = vb
                    vecs.append(vec)
    return vecs


def _find_plan(nl, name, width, support_ports, port_widths):
    """Runs the full grouping probe on a single output word, returning a `WordPlan` or None (shape
    doesn't fit)."""
    zero_vec = {n: 0 for n, _ in nl.inputs}
    G = [p for p in support_ports if port_widths[p] == 1]
    M = [p for p in support_ports if port_widths[p] > 1]
    if len(M) < 2:
        return None

    def full(p):
        return (1 << port_widths[p]) - 1

    def mk(guard_val, extra):
        v = dict(zero_vec)
        for g in G:
            v[g] = guard_val
        v.update(extra)
        return v

    # Step 1: classify by pulling each one high alone (product factor vs. bare addend).
    probe_vecs = []
    for p in M:
        probe_vecs.append(mk(0, {p: full(p)}))
        probe_vecs.append(mk(1, {p: full(p)}))
    rows = nl.simulate(probe_vecs) if probe_vecs else []

    zero_alone = []
    leftover = []
    for i, p in enumerate(M):
        v0 = rows[2 * i][name]
        v1 = rows[2 * i + 1][name]
        if v0 == 0 and v1 == 0:
            zero_alone.append(p)
        elif v0 == v1 != 0:
            leftover.append(p)
        else:
            return None  # The two guard states give different nonzero values: doesn't fit this shape.

    if len(zero_alone) < 2:
        return None

    # Step 2: pairwise matching, taken greedily in enumeration order.
    cand_pairs = list(itertools.combinations(zero_alone, 2))
    pair_vecs = []
    for p, q in cand_pairs:
        pair_vecs.append(mk(0, {p: full(p), q: full(q)}))
        pair_vecs.append(mk(1, {p: full(p), q: full(q)}))
    pair_rows = nl.simulate(pair_vecs) if pair_vecs else []

    used = set()
    pairs = []  # (p, q, kind, active_val)
    for i, (p, q) in enumerate(cand_pairs):
        if p in used or q in used:
            continue
        v0 = pair_rows[2 * i][name]
        v1 = pair_rows[2 * i + 1][name]
        if v0 != 0 and v1 == 0:
            kind, active_val = "A", v0
        elif v0 == 0 and v1 != 0:
            kind, active_val = "B", v1
        else:
            continue  # Both zero or both nonzero: not a genuine product pair.
        pairs.append((p, q, kind, active_val))
        used.add(p)
        used.add(q)

    if 2 * len(pairs) != len(zero_alone) or not pairs:
        return None  # There's a leftover unpaired product-factor candidate; shape doesn't fit.

    # Step 3: locate the controlling guard per group -- each candidate guard is probed once per group.
    guard_probe_vecs = []
    for (p, q, _kind, _active_val) in pairs:
        for g in G:
            v = dict(zero_vec)
            v[g] = 1
            v[p] = full(p)
            v[q] = full(q)
            guard_probe_vecs.append(v)
    guard_rows = nl.simulate(guard_probe_vecs) if guard_probe_vecs else []

    groups = []
    used_guards = set()
    idx = 0
    for (p, q, kind, active_val) in pairs:
        matched = []
        for g in G:
            val = guard_rows[idx][name]
            idx += 1
            if kind == "A" and val == 0:
                matched.append(g)
            elif kind == "B" and val == active_val:
                matched.append(g)
        if len(matched) != 1:
            return None
        g = matched[0]
        if g in used_guards:
            return None
        used_guards.add(g)
        groups.append(Group(g, p, q, kind, active_val))

    return WordPlan(tuple(groups), tuple(leftover))


def build_context(nl, val_vectors, val_results, verify_vectors, verify_results):
    """Runs grouping probes over every output word, once. The caller (`main.decide`) calls this exactly
    once, the same division of labor as the compare family's `build_context`: this only figures out
    "what the grouping looks like"; the actual acceptance check (comparing the train/accept vector
    sets) is left to `try_mux`."""
    support = nl.support()
    port_widths = dict(nl.inputs)
    port_widths.update(dict(nl.outputs))
    zero_vec = {n: 0 for n, _ in nl.inputs}

    plans = {}
    boundary_vectors = {}
    boundary_results = {}
    for name, width in nl.outputs:
        support_ports = _support_ports(nl, support, name, width)
        try:
            plans[name] = _find_plan(nl, name, width, support_ports, port_widths)
        except Exception:
            plans[name] = None
        if plans[name] is not None:
            bvecs = _sign_boundary_vectors(zero_vec, port_widths, plans[name])
        else:
            bvecs = []
        boundary_vectors[name] = bvecs
        boundary_results[name] = nl.simulate(bvecs) if bvecs else []
    return Context(plans, val_vectors, val_results, verify_vectors, verify_results,
                    boundary_vectors, boundary_results)


def render_expr(plan, width):
    """Folds a `WordPlan` into the text of an `assign` right-hand side expression, or None (no terms at
    all)."""
    parts = list(plan.leftover)
    zero_lit = sized_literal(0, width)
    for g in plan.groups:
        prod = f"{g.a} * {g.b}"
        if g.kind == "A":
            parts.append(f"({g.guard} ? {zero_lit} : {prod})")
        else:
            parts.append(f"({g.guard} ? {prod} : {zero_lit})")
    if not parts:
        return None
    return " + ".join(parts)


def _formula(plan, width, vec):
    """Python-side evaluation whose semantics match the text produced by `render_expr` (see `try_mux`'s
    acceptance section: unsigned integers are never truncated along the way, only once at the very
    end via `% modulus`, because a Verilog expression's context width is always >= the target word
    width, so truncating early can only lose precision, not change the final result — using exact
    integer arithmetic before exceeding the context width is mathematically equivalent to truncating
    step by step)."""
    total = 0
    for p in plan.leftover:
        total += vec.get(p, 0)
    for g in plan.groups:
        gv = vec.get(g.guard, 0)
        active = (gv == 0) if g.kind == "A" else (gv == 1)
        if active:
            total += vec.get(g.a, 0) * vec.get(g.b, 0)
    return total % (1 << width)


def try_mux(nl, name, width, ctx, pool):
    """Applies an already-grouped `WordPlan` to a single output word, and after acceptance returns
    (expr, signed_needs, pool_refs) or None. This shape only ever uses raw unsigned ports, never
    referencing any pool entry — `signed_needs` is always an empty set, `pool_refs` is always an
    empty tuple."""
    plan = ctx.plans.get(name)
    if plan is None:
        return None
    expr = render_expr(plan, width)
    if expr is None:
        return None

    for vectors, results in ((ctx.val_vectors, ctx.val_results),
                              (ctx.verify_vectors, ctx.verify_results),
                              (ctx.boundary_vectors.get(name, ()),
                               ctx.boundary_results.get(name, ()))):
        for vec, row in zip(vectors, results):
            if _formula(plan, width, vec) != row[name]:
                return None

    return expr, set(), ()


FAMILY = {
    "kind": "mux",
    "label": "solved by guard product",
    "order": 40,
    "stage": "primary",
    "build_context": lambda env: build_context(env.nl, env.val_vectors, env.val_results,
                                               env.verify_vectors, env.verify_results),
    "attempt": lambda env, ctx, name, width: try_mux(env.nl, name, width, ctx, env.pool),
    "shape": "expr3",
}
