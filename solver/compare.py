"""Family D: comparison-expression (linear difference + threshold comparison) extractor.

Model: `y = [f REL C]`, `f = Σ_j c_j·A_j` (coefficients `c_j` ∈ {+1,-1}, the first term
normalized to +1), `A_j` is an atom's integer value. Internally only four forms are kept:
`f >= C`, `f <= C`, `f == C`, `f != C` (`>`/`<` are, on integers, equivalent to `>= C+1`/
`<= C-1` respectively; rendering later decides by cost whether to fold them into an open-
interval form).

atom set = {primary-input ports in this word's support} × {unsigned, signed}
         ∪ {earlier-declared output ports that satisfy the reference rules} × {unsigned, signed}
(see `build_context`'s `atoms` construction and the reference rules in the README/handoff docs
for details.)

**Rendering rule**: split `f REL C` into `Lexpr REL2 Rexpr` (`Lexpr - Rexpr == f`, `REL2` is
derived from `REL` plus a constant adjustment), brute-force all `2^k` ways of "assigning atoms
to the left/right side" (k<=3, at most 8 ways), and for each try both open/closed interval
spellings `>=`/`>` (or `<=`/`<`) (exploiting `f >= C+1 ⇔ f > C` to absorb ±1 into the relational
operator, giving the constant term embedded in one side a chance to become 0 and save a `+`/`-`),
then pick the combination with the fewest operators. An empty side uses a plain literal directly
(see below); a non-empty side is materialized through `SharePool`'s `"portview"` (a single atom,
coefficient +1, cost 0) or `"affine"` (a general linear combination, reusing `model.render_word`'s
layout, see `share.py`).

**The comparison's own threshold constant uses plain decimal** (not a sized literal): iverilog
experiments (see the `share.py` docstring) confirm that when both operands are already
pre-resolved `wire signed`, a plain decimal unsized literal (Verilog defaults to signed) is
enough to keep the comparison's "both sides signed" semantics, and it's free (costcount doesn't
charge for any plain numeric literal). Only when the magnitude exceeds the 32-bit signed range
does it fall back to `W'sd<value>` (declared width follows that side's provider width) — and
only when the magnitude needs it does it use a unary minus followed by `W'sd<magnitude>`
(Verilog doesn't allow a negative decimal directly after `W'sd`; iverilog experiments confirm
`-W'sd<magnitude>` works and produces the correct result). Constants **inside** a pool entry
(an affine pool entry's own K/coefficients) take a different path: those constants only ever
appear in composite expressions mixed with operands that have already been pre-extended to
`wire signed`, so reusing `render_word`'s existing unsigned sized literal is safe, for the same
reason documented in `share.py`.

**Signedness overfitting** (test16's `in1 - $signed(in3) == 1` vs.
`$signed(in1) - $signed(in3) == 1` can both show 0 mismatches on the standard 512 verification
vectors): `build_context` additionally generates, for each word, a batch of "signedness
flip-point" boundary vectors for each atom pair (each primary-input atom port's value is pushed
near ≈ 2^(w-1), combined pairwise), merged into both the training and final acceptance vector
sets, so this kind of candidate gets eliminated during filtering instead of getting away with it
by enumeration order.

**Threshold refinement is mandatory**: the 512(+boundary) vectors usually give an interval
rather than a single value. `_refine` uses constructed probe vectors (pure primary-input atoms
can be solved directly; those involving output atoms use that word's per-bit weight model as a
**proposer** for "which direction is more likely to flip the bit" — guessing the wrong direction
only costs extra refinement rounds or eventually gives up, it never produces non-equivalent RTL,
since ground truth is always `nl.simulate()`) to squeeze the interval down; if it still hasn't
converged to width 1 after N rounds, the candidate is abandoned (moving on to the next candidate,
or the whole word falls back).

**Support-coverage filter**: the union of the candidate atoms' (transitive) primary-input
support must ⊇ this word's own support, otherwise it's rejected even if all 512 vectors happen
to match (test25's out20 `in2==0` trap).

**Decision-boundary verification** (milestone 20): a candidate that has passed the 557 samples
(512 acceptance + boundary) must also clear the `_decision_boundary_vectors` gate before it's
allowed to `return`. This was prompted by two counterexamples — `$signed(in4+in2) <
$signed(in3)` (`in3` is 1-bit) got replaced by the wrong model `in2+in3+in4>=129`, and
`$signed(in3*in1+in2) < $signed(in2)` (involving a product) got replaced by the wrong model
`in1+in2+in3>=202` — **both showed zero errors on `val_vectors`/`verify_vectors`**, because the
actual error only occurs at the decision boundary `in2+in4==128`, which is only 0.732% of the
full input space (30 out of 4096 combinations), a different thing from the **input-space**
boundary swept by `_gen_sign_boundary_vectors` (that one sweeps each port's own signedness
flip point, not the neighborhood of the threshold of the fitted linear expression `f(x)`). The
fix goes the other way: construct, from the **fitted** `f` and `C`, vectors that make
`f(x) ∈ {C-1, C, C+1}` and verify once more (see `_decision_boundary_vectors`).

⚠ The deeper root cause this round deliberately leaves unfixed: the correct answer
`in2+in4>=128` **was** actually enumerated by `enumerate_terms`, but got filtered out by
`_covers_support` — `in3` is in `out1`'s **static** fan-in, but it's a 1-bit signed value that
saturates (`$signed(in3)` can only be 0 or −1, the threshold doesn't change), so it has no
**functional** effect on the output. `_covers_support` checks static dependence rather than
functional dependence, and structurally forces the search into a hypothesis space that doesn't
contain the correct answer. This filter was added to guard against a different kind of error
(see the "support-coverage filter" section above); loosening it would change the decisions for
30 existing cases, and is left to a future, separate milestone; this round's decision-boundary
verification only guarantees "safely fall back when the correct answer can't be selected", it
doesn't try to make these two cases solve correctly.

**Derived atoms (milestone 27)**: besides referencing input ports/earlier-declared output words,
a condition can also reference "the comparison bit of two atoms in this word's own atom pool"
(`A < B`, where `A`/`B` are atoms in `atoms[name]` that have already passed the two-layer
anti-cycle guards) — corresponding to corpus shapes like `out1 = ((in1<in2) < in1)`, where "the
result of a comparison feeds into another comparison". The implementation just computes the
derived atom's value and injects it into every `row` under a synthetic name
(`~cmp_<word>_<idx>`, `~` isn't a legal Verilog identifier character, guaranteeing it never
collides with a real port/output-word name), which automatically turns it into an "output word
atom" — `_atom_value`/`eval_f`/`classify`/`_covers_support`/`_refine`/`_hillclimb` all remain
unaware of its existence (see `_DerivedSim`/`_augment_rows`). **Rendering** is a separate matter:
a synthetic name isn't a real identifier, so a finally-selected candidate must first materialize
`A < B` as a `"raw"` pool entry in `share.py` (the only tag that passes `refs` on to `SharePool`'s
dependency tracking), then replace the synthetic name in the outer key with this pool entry's
real wire name (see the `derived_lookup` parameter of `_materialize_one_derived`/`build_expr`)
— `portview`/`affine`'s `_refs_of` always returns an empty tuple, so this indirect dependency
has to be explicitly stuffed into the outermost `refs`; skip that step and `emit()` will fail to
print this dependency's declaration (an undeclared wire — costcount won't complain, but only
iverilog will catch it at compile time).

Derived atoms are only used by `compare.try_compare` itself, and are deliberately kept out of
`ctx.atoms` — `(undisclosed family)._find_pairs` is O(n²·T) with no upper bound on n, so folding
them in would blow up its cost quadratically (see the comment on the `Context.derived_atoms`
field).

**Wraparound/truncation arithmetic derived atoms (milestone 30)**: besides "the comparison bit
of a single atom" (milestone 27), a comparison operand can also be a "mod-2^w wraparound/
truncated value" — `(a-b) mod 2^w`, `(-a) mod 2^w`, `a mod 2^w`, corresponding to corpus shapes
where the comparison operand is actually a subtraction/negation/truncation before being
compared. Same structural payoff as milestone 27: inject the "already-modded, not-yet-
signed-converted" **unsigned value** into the simulation row, and hand the metadata to
`_atom_value`'s existing signed readout (`raw &= (1<<width)-1; if signed and raw>=1<<(width-1):
raw-=1<<width`) — not a single line of `eval_f`/`classify`/`_covers_support`/`_refine`/
`_hillclimb`/`_decision_boundary_vectors` needs to change. The same row value serves both
signed/unsigned readings via two synthetic names (`_wrap_operand`'s `(portview, ...)` key with
different `width`).

⚠ **Rendering-layer landmine**: a wraparound atom must be materialized as two layers of raw
pool entries, not just one — the width of the layer
`("raw", "<va> - <vb>", wrap_w, (key_a, key_b), False)` is the one responsible for the actual
truncation (without this layer, `share._port_width` can't look up the synthetic wire's name,
falls back to returning the target width, wrongly decides "no pre-extension needed", and prints
a bare narrow wire in a composite expression, degrading the signed readout to zero-extension);
the first layer's wire `signed` field **must be `False`** (declaring it `wire signed` would make
`assign e = r;` do sign extension, and the unsigned readout would be wrong). Both of these are
only caught by iverilog — the Python model layer can't see them.
"""
import random
from collections import namedtuple

from model import Term, render_word
from costcount import cost_of_text
import fallback

RNG_SEED = 20260813
REFINE_ROUNDS = 24  # each round at least halves the gap (binary search); 24 rounds are enough to eat through a gap 30+ bits wide
HILLCLIMB_OUTER = 10
HILLCLIMB_BATCH = 96
MAX_ATOMS = 26          # Safety cap: if the atom pool is too large, the whole word gives up on the comparison family
INT32_MARGIN = 1 << 30  # safe zone for plain-decimal threshold constants (avoiding the 32-bit trap of unsized literals)
# Milestone 27: at most this many derived (comparison-bit) atoms are generated per word.
# WARNING no trailing comment on this line -- `tools/mutate.py` does an exact comparison against it, see below
# same reason as the `ordered = sorted(...)` section.
DERIVED_MAX = 16
# WARNING measured on 2026-09-02, 12 wasn't enough: the pairing `random_920`'s out1 needs (`in3 < in2`,
# two different ports) was ranked after the 12th and got cut, falling back (see `_build_derived_atoms`
# see the pairing-order explanation in the docstring). 16 is the measured value of "after adjusting the pairing order, it just barely covers this batch of
# the deepest one among 10 target cases" is a measured value, not a theoretical upper bound -- `DERIVED_MAX ×
# (1 + 2n)` candidate count is still far smaller than `enumerate_terms`'s k=3 enumeration size, see
# see module docstring.
DERIVED_NAME_PREFIX = "~cmp_"  # `~` isn't a legal Verilog identifier character, can never collide with a real port/output name
_WRAP_OP_COST = cost_of_text("output x;\nassign x = w0 - w1;\n")[0]

# Milestone 31: the two thresholds for stage 2 (relaxed coefficients) enumeration. WARNING neither of these two lines has a trailing comment --
# `tools/mutate.py` does an exact comparison against them. Stage 2 only triggers when stage 1 (`enumerate_terms`
# 's ±1, k<=3) finds no candidate at all, and the atom pool doesn't exceed this cap, see
# see `enumerate_terms_wide` docstring.
COMPARE_WIDE_POOL_MAX = 6
COMPARE_COEF_MAX = 15
# the number of times stage 2 is triggered (at most +1 per word) -- `tools/probe_m31.py` uses it as
# Threshold lock/performance evidence: a word from the real corpus should never once make it tick. Caller (`tools/covsweep.py`
# and other batch scripts) need to re-measure, they should zero it themselves before reading it.
wide_stage_calls = 0
# the number of times the "pool is small enough" condition holds, regardless of whether stage 1 solved it. Reason: the threshold lock
# If it only asserts `wide_stage_calls == 0`, it would always be green on the real corpus -- but that could be
# because "pool <= COMPARE_WIDE_POOL_MAX" never once held (measured: test17's pools are all >6,
# test29/test30 never even call `try_compare`), so the `not scored` guard never
# never got tested. With this counter, the probe can also assert "blocked N times", matching this project's
# the discipline of "0 counterexamples must be paired with N actual hits".
wide_pool_seen = 0

REL_TEXT = {"GE": ">=", "LE": "<=", "EQ": "==", "NE": "!="}
REL_MIRROR = {">=": "<=", "<=": ">=", ">": "<", "<": ">", "==": "==", "!=": "!="}


def split_bit(bitname):
    if "[" in bitname:
        p, rest = bitname.split("[", 1)
        return p, int(rest[:-1])
    return bitname, 0


Context = namedtuple(
    "Context",
    "atoms atom_support word_support port_widths provider_width "
    "val_vectors val_results verify_vectors verify_results "
    "boundary_vectors boundary_results weights_by_word rng zero_vec "
    "derived_atoms derived_val_results derived_verify_results",
)
# `derived_atoms` (milestone 27): `dict[word -> [(synth_name, atom_a, atom_b, cost), ...]]`
# -- **deliberately not folded into `atoms`**. `(undisclosed family)._find_pairs` is `O(n²·T)` with no
# upper bound; folding derived atoms into `ctx.atoms[name]` would blow up its cost quadratically; `(undisclosed family).py`
# 's docstring has already explained that "reusing `ctx.atoms[name]`" itself exists to isolate the two families'
# 's search space; this continues the same reasoning by opening a separate field, which only `compare.try_compare`
# 's condition enumeration reads it.
#
# `derived_val_results`/`derived_verify_results`: `dict[word -> [row, ...]]` --
# for words with derived atoms, `val_results`/`verify_results` (built once by `main.decide`,
# all families share the same batch of list/dict objects) can't have a synthetic key stuffed into it in place: those row objects are
# **shared**, there's no reason a synthetic name injected for one word should be visible to other words/other families.
# here a `ChainMap` (see `_augment_rows`) layers on each word's own derived values,
# underneath it's still the originally shared row -- no need for a full `dict()` copy (most words have no derived
# atom, these two fields aren't used, so the value is an empty dict).


def _boundary_points(w):
    if w <= 1:
        return [0, 1]
    half = 1 << (w - 1)
    pts = sorted({0, half - 1, half, half + 1, (1 << w) - 1})
    return [p for p in pts if 0 <= p < (1 << w)]


def _gen_sign_boundary_vectors(nl, port_widths, ports, zero_vec, max_ports=8):
    """Generates "signedness flip-point" boundary vectors for the primary-input ports that
    appear in a word's atom pool: each single port sweeps `_boundary_points` on its own;
    multiple ports are also swept pairwise (test16's signedness overfitting trap only shows up
    when at least two ports *simultaneously* land near the boundary). `max_ports` is a
    performance safety cap — beyond it, only the first few are kept (by declaration order,
    deterministic).
    """
    ports = ports[:max_ports]
    vecs = []
    if len(ports) == 1:
        p = ports[0]
        for v in _boundary_points(port_widths[p]):
            vec = dict(zero_vec)
            vec[p] = v
            vecs.append(vec)
    else:
        for i in range(len(ports)):
            for j in range(i + 1, len(ports)):
                pi, pj = ports[i], ports[j]
                for vi in _boundary_points(port_widths[pi]):
                    for vj in _boundary_points(port_widths[pj]):
                        vec = dict(zero_vec)
                        vec[pi] = vi
                        vec[pj] = vj
                        vecs.append(vec)
    return vecs


def build_context(nl, weights_by_word, val_vectors, val_results, verify_vectors, verify_results):
    """Builds, once, the atom pool, support sets, and boundary vectors that the comparison
    family needs. The caller (`main.decide`) calls this exactly once, the same division of labor
    as linear/quadratic's `build_weight_vectors`/`build_plan`: this only computes "what to
    probe", `nl.simulate` is run by the caller (or by this function itself, except for the
    one-shot boundary vectors)."""
    support = nl.support()
    decl_index = {n: i for i, (n, _w) in enumerate(nl.outputs)}
    port_widths = dict(nl.inputs)
    port_widths.update(dict(nl.outputs))

    word_support = {}
    for name, width in nl.outputs:
        s = set()
        for b in nl.bits([(name, width)]):
            s |= support.get(b, frozenset())
        word_support[name] = frozenset(s)

    atom_support = {}
    for p, w in nl.inputs:
        atom_support[(p, False)] = frozenset(nl.bits([(p, w)]))
    for q, _wq in nl.outputs:
        atom_support[(q, True)] = word_support[q]

    zero_vec = {n: 0 for n, _ in nl.inputs}

    real_names = {p for p, _w in nl.inputs} | {q for q, _w in nl.outputs}

    atoms = {}
    boundary_vectors = {}
    boundary_results = {}
    derived_atoms = {}
    derived_val_results = {}
    derived_verify_results = {}
    for name, width in nl.outputs:
        my_bits = set(nl.bits([(name, width)]))
        sup = word_support[name]
        support_ports = {split_bit(b)[0] for b in sup}
        pool_atoms = []
        input_ports = [p for p, _w in nl.inputs if p in support_ports]
        for p in input_ports:
            w = port_widths[p]
            pool_atoms.append((p, False, w, False))
            pool_atoms.append((p, True, w, False))
        for q, wq in nl.outputs:
            if decl_index[q] >= decl_index[name]:
                continue
            cone_q = fallback.cone_nets(nl, nl.bits([(q, wq)]), set())
            if my_bits & cone_q:
                continue
            pool_atoms.append((q, False, wq, True))
            pool_atoms.append((q, True, wq, True))
        atoms[name] = pool_atoms if len(pool_atoms) <= 2 * MAX_ATOMS else []

        bvecs = _gen_sign_boundary_vectors(nl, port_widths, input_ports, zero_vec)
        boundary_vectors[name] = bvecs
        boundary_results[name] = nl.simulate(bvecs) if bvecs else []

        # Milestone 27: derived (comparison-bit) atom -- only uses "atoms that have already passed the two-layer anti-cycle guards
        # atoms[name]" pairwise, must not borrow another word's pool across words (decl_index baseline
        # is different, that would reopen the path the anti-cycle rule is meant to block).
        derived = _build_derived_atoms(atoms[name], atom_support, name, real_names)
        # Milestone 30: wraparound/truncation arithmetic derived atom -- `_bool_valued` is a performance gate
        # (see its docstring), only words whose value range might be ⊆ {0,1} are worth spending this enumeration on.
        if atoms[name] and _bool_valued(name, val_results, verify_results):
            derived = derived + _build_wrap_atoms(atoms[name], atom_support, name,
                                                   real_names, port_widths)
        derived_atoms[name] = derived
        if derived:
            specs_triples = [(s, a, b) for s, a, b, _c in derived]
            # `boundary_results[name]` is a row specific to this word (unlike
            # val_results/verify_results are shared by all words), so it can be injected in place directly.
            _inject_derived_inplace(boundary_vectors[name], boundary_results[name], specs_triples)
            derived_val_results[name] = _augment_rows(val_vectors, val_results, specs_triples)
            derived_verify_results[name] = _augment_rows(verify_vectors, verify_results, specs_triples)

    max_w = max([w for _, w in nl.inputs] + [w for _, w in nl.outputs] + [1])
    provider_width = max_w + 8

    return Context(atoms, atom_support, word_support, port_widths, provider_width,
                    val_vectors, val_results, verify_vectors, verify_results,
                    boundary_vectors, boundary_results, weights_by_word,
                    random.Random(RNG_SEED), zero_vec,
                    derived_atoms, derived_val_results, derived_verify_results)


# --------------------------------------------------------------------------
# candidate enumeration / f-value computation
# --------------------------------------------------------------------------

def _atom_value(port, signed, width, is_output, vec, row):
    raw = row[port] if is_output else vec.get(port, 0)
    raw &= (1 << width) - 1
    if signed and raw >= (1 << (width - 1)):
        raw -= 1 << width
    return raw


def eval_f(terms, vec, row):
    total = 0
    for port, signed, width, is_output, coeff in terms:
        total += coeff * _atom_value(port, signed, width, is_output, vec, row)
    return total


# --------------------------------------------------------------------------
# Milestone 27: derived (comparison-bit) atom
# --------------------------------------------------------------------------

def _derived_value(atom_a, atom_b, vec, row):
    if atom_a[0] == WRAP_TAG:
        return _wrap_value(atom_a, vec, row)
    va = _atom_value(atom_a[0], atom_a[1], atom_a[2], atom_a[3], vec, row)
    vb = _atom_value(atom_b[0], atom_b[1], atom_b[2], atom_b[3], vec, row)
    return 1 if va < vb else 0


def _build_derived_atoms(pool_atoms, atom_support, name, real_names):
    """Does an "ordered" pairwise pairing of `pool_atoms` (= `atoms[name]`, already past the
    two-layer anti-cycle guards of declaration order + cone check), treating `A < B` as a new
    boolean atom (`A < B` and `B < A` are different functions, both directions must be listed —
    same reasoning as `derive_atoms(kind="cmp")` in `tools/probe_m27.py`). Stops at
    `DERIVED_MAX` (see the milestone 27 section of the module docstring).

    ⚠ The pairing order is deliberately **not** a plain double loop over `pool_atoms`'s
    original indices: `pool_atoms` is interleaved as "each port's unsigned followed by signed",
    so enumerating in original-index order would let the small `DERIVED_MAX` budget get
    exhausted early by a single port's own unsigned/signed pairing (`in1 vs $signed(in1)`),
    pushing "comparing different ports against each other" (the more common shape in practice,
    see `random_920`'s `in3 < in2`) past the budget and never enumerated (hit in practice on
    2026-09-02: `random_920`'s `out1` fell back, and switching to this order solved it).
    Changed to two layers: first do a stable sort of `pool_atoms` by the signed field (all
    unsigned first, then all signed, each group keeping its original port declaration order),
    then use "unordered pairing of different ports/atoms" as the outer loop and "both
    directions" as the inner loop — this way the budget is spent first on **breadth** (covering
    more atom pairs) rather than insisting on both directions of a single pair.

    Returns `[(synth_name, atom_a, atom_b, intrinsic_cost), ...]`. `intrinsic_cost` is this
    derived atom's own `<` operator cost (once materialized as a `share.py` raw pool entry it's
    always "one `<` between two already-materialized identifiers", computed live with
    `cost_of_text`, never by hand — following this project's existing discipline that pricing
    and rendering share the same source, see `share.py._op_count`).
    """
    # all unsigned(False) sorted first, signed after, each group keeping port declaration order.
    # WARNING no trailing comment on this line, deliberately: `tools/mutate.py`'s
    # `compare_derived_pair_order` does an exact comparison against it; mixing in prose would, next time,
    # a text polish would break the comparison, and when `apply_mutation` loses the match it's a `SystemExit`,
    # would make every entry that comes after it not even get printed in the summary table (hit in M25).
    ordered = sorted(pool_atoms, key=lambda a: a[1])
    n = len(ordered)
    specs = []
    cost, _detail = cost_of_text("output x;\nassign x = w0 < w1;\n")
    for idx1 in range(n):
        if len(specs) >= DERIVED_MAX:
            break
        for idx2 in range(idx1 + 1, n):
            if len(specs) >= DERIVED_MAX:
                break
            for a_idx, b_idx in ((idx1, idx2), (idx2, idx1)):
                if len(specs) >= DERIVED_MAX:
                    break
                atom_a, atom_b = ordered[a_idx], ordered[b_idx]
                synth_name = f"{DERIVED_NAME_PREFIX}{name}_{len(specs)}"
                assert synth_name not in real_names, (
                    f"derived atom synthetic name collides with a real identifier: {synth_name!r}")
                sup = (atom_support.get((atom_a[0], atom_a[3]), frozenset())
                       | atom_support.get((atom_b[0], atom_b[3]), frozenset()))
                atom_support[(synth_name, True)] = sup
                specs.append((synth_name, atom_a, atom_b, cost))
    return specs


# --------------------------------------------------------------------------
# Milestone 30: wraparound/truncation arithmetic derived atom
# --------------------------------------------------------------------------

# Candidate width family: `nat` = natural width (`(a-b)` uses max(w_a,w_b); `(-a)`/`a mod 2^w` uses
# w_a), `narrow` = `nat-1`, `trunc` = pure truncation `a mod 2^k` (all k<w_a),
# `isign` = the variant that also tries reading the inner operand as signed. Measured, the biggest payoff is `nat`
# (see the milestone 30 section of the module docstring), `wide` (nat+1) and `allw` (sweeps all
# width) is deliberately not enabled -- the former was, in measurement, "the comparison needs a ±2 coefficient" misread as a wraparound, the latter doesn't buy
# any extra coverage, yet wall time rose from 41s to 74s.
WRAP_MODE = frozenset({"nat", "narrow", "trunc", "isign"})
# at most this many wraparound atoms are generated for one word (a budget, not a theoretical upper bound). Measured,
# 32→12, 64→18, 96→19, 192→20, 512→20 (512 is wasteful).
# WARNING no trailing comment on this line -- `tools/mutate.py` does an exact comparison against it.
# 2026-09-03 measured tradeoff (all run individually, same conditions, `test25`'s user time):
#   baseline (without this mechanism) 183.9s -- gain 0
#   WRAP_MAX =  96  220.4s (+20%) -- covsweep corpus newly solves **15** words
#   WRAP_MAX = 192  285.3s (+55%) -- newly solves **16** (only one more, `random_907`)
# raising it from 96 to 192 only buys one more word, at the cost of the slowest case taking 65 more seconds -- clearly not worth it at the margin,
# so 96 is chosen (36% of the cost for 94% of the gain).
# WARNING all three earlier reports underestimated the cost (the implementer reported +28.7%, the selection prototype reported +26%),
# the reason is it was measured while there was other load. **Before changing this constant, run it once alone, with no other load.**
WRAP_MAX = 96
WRAP_TAG = "#wrap"
WRAP_NAME_PREFIX = "~wrp_"  # `~` isn't a legal Verilog identifier character, same reason as DERIVED_NAME_PREFIX


def _bool_valued(name, val_results, verify_results):
    """A performance gate, not a correctness gate: only words whose value range is ⊆ {0,1}
    ever reach `try_compare` (see `_values_subset_bool`), so only they need wraparound atoms.
    `_values_subset_bool` also checks `boundary_results`, which is skipped here — if a word is
    wrongly judged here as "possibly boolean" and a batch of unused wraparound atoms gets
    computed for it, the cost is just a bit of wasted time; conversely, if it's wrongly judged
    "not boolean" and skipped, `try_compare`'s own `_values_subset_bool` will reject this word
    earlier anyway — the mismatch between the two criteria doesn't affect correctness."""
    for rows in (val_results, verify_results):
        for r in rows:
            if r[name] not in (0, 1):
                return False
    return True


def _wrap_value(payload, vec, row):
    _tag, kind, a, b, ww, isa, isb, _rs = payload
    va = _atom_value(a[0], isa, a[2], a[3], vec, row) if a is not None else 0
    vb = _atom_value(b[0], isb, b[2], b[3], vec, row) if b is not None else 0
    return (va - vb) & ((1 << ww) - 1)


def _wrap_bases(pool_atoms, port_widths):
    """Returns the deduplicated set of "atoms that can serve as a wraparound operand": only
    one is kept per port/word (the unsigned reading), and its width is its own declared width."""
    seen = []
    got = set()
    for (p, s, w, o) in pool_atoms:
        if (p, o) in got:
            continue
        got.add((p, o))
        seen.append((p, False, w, o))
    return seen


def _wrap_widths(kind, wa, wb, all_widths, flags):
    nat = wa if kind in ("neg", "id") else max(wa, wb)
    out = []

    # WARNING the reason and cost of the cap of 32 (a 2026-09-03 independent code review pointed out it originally had no explanation at all):
    # `trunc` lists all of `1..nat-1`, which is linear blowup for a wide word -- `test22 out1`
    # is **69 bits**, without a limit a single atom alone would spawn 68 candidates, exhausting `WRAP_MAX=192`
    # 's budget gets used up all at once, crowding out the actually useful `nat`/`narrow` candidates.
    # **The cost must be reported honestly**: real test data does have words wider than 32 bits (`test22`),
    # for them, `nat`/`narrow` would also be **silently dropped** by this cap, and those words would never
    # generates wraparound atoms. The direction is conservative (generating fewer candidates only means solving fewer, never computing wrong),
    # and measured, `test22` is byte-for-byte unchanged, cost is still 25.
    # To loosen it, **just raising 32 isn't enough** -- `trunc`'s enumeration needs to first be changed to a capped sampling,
    # otherwise a wide word would blow straight through the budget.
    def push(k):
        if 1 <= k <= 32 and k not in out:
            out.append(k)

    if kind == "id":
        # Pure truncation: a natural width equal to the original atom itself is meaningless, only list ones narrower than it
        if "trunc" in flags:
            for k in range(nat - 1, 0, -1):
                push(k)
        return out
    if "nat" in flags:
        push(nat)
    if "narrow" in flags:
        push(nat - 1)
    return out


def _build_wrap_atoms(pool_atoms, atom_support, name, real_names, port_widths):
    """Milestone 30: `(a - b) mod 2^w` / `(-a) mod 2^w` / `a mod 2^w`, one atom each for the
    signed/unsigned reading. Returns `(synth_name, payload, None, cost)` — the same shape as
    milestone 27's `(synth, atom_a, atom_b, cost)`, with payload taking atom_a's slot; its first
    element is `WRAP_TAG`, which `_derived_value` dispatches on."""
    flags = WRAP_MODE
    if not flags:
        return []
    bases = _wrap_bases(pool_atoms, port_widths)
    all_widths = sorted({b[2] for b in bases})
    inner_variants = [(False, False)]
    if "isign" in flags:
        inner_variants += [(True, False), (False, True), (True, True)]

    specs = []

    def emit(kind, a, b, ww, isa, isb):
        if len(specs) >= WRAP_MAX:
            return
        for rs in (False, True):
            if len(specs) >= WRAP_MAX:
                return
            synth = f"{WRAP_NAME_PREFIX}{name}_{len(specs)}"
            assert synth not in real_names
            payload = (WRAP_TAG, kind, a, b, ww, isa, isb, rs)
            sup = frozenset()
            for at in (a, b):
                if at is not None:
                    sup = sup | atom_support.get((at[0], at[3]), frozenset())
            atom_support[(synth, True)] = sup
            cost = 0 if kind == "id" else _WRAP_OP_COST
            specs.append((synth, payload, None, cost))

    # Order = priority (WRAP_MAX is the budget): neg natural width first, then sub natural width,
    # only afterward come the extra candidate widths and truncation.
    order = []
    for a in bases:
        for isa, _ in [(False, False)] + ([(True, False)] if "isign" in flags else []):
            for ww in _wrap_widths("neg", a[2], None, all_widths, flags):
                order.append(("neg", None, a, ww, False, isa, 0 if ww == a[2] else 1))
    for i, a in enumerate(bases):
        for j, b in enumerate(bases):
            if i == j:
                continue
            for isa, isb in inner_variants:
                nat = max(a[2], b[2])
                for ww in _wrap_widths("sub", a[2], b[2], all_widths, flags):
                    order.append(("sub", a, b, ww, isa, isb, 0 if ww == nat else 1))
    for a in bases:
        for isa, _ in [(False, False)] + ([(True, False)] if "isign" in flags else []):
            for ww in _wrap_widths("id", a[2], None, all_widths, flags):
                order.append(("id", a, None, ww, isa, False, 2))
    order.sort(key=lambda t: t[6])  # Stable sort: natural width first
    for kind, a, b, ww, isa, isb, _rank in order:
        emit(kind, a, b, ww, isa, isb)
    return specs


def _wrap_operand(pool, atom, inner_signed, ww):
    key = ("portview", atom[0], ww, bool(inner_signed))
    return pool.request(key), key


def _materialize_wrap(payload, pool, provider_width):
    """Materializes as two layers of `"raw"` pool entries: the first layer's width `ww` is the
    one actually responsible for truncation (`signed=False`, otherwise `assign e = r;` would do
    sign extension and misread the unsigned value), the second layer extends it to
    `provider_width` (`rs` decides whether to apply `$signed()`). See the rendering-layer
    landmine described in the milestone 30 section of the module docstring."""
    _tag, kind, a, b, ww, isa, isb, rs = payload
    refs = []
    if kind == "id":
        na, ka = _wrap_operand(pool, a, isa, ww)
        refs.append(ka)
        expr = na
    elif kind == "neg":
        nb, kb = _wrap_operand(pool, b, isb, ww)
        refs.append(kb)
        expr = f"-{nb}"
    else:
        na, ka = _wrap_operand(pool, a, isa, ww)
        nb, kb = _wrap_operand(pool, b, isb, ww)
        refs.extend([ka, kb])
        expr = f"{na} - {nb}"
    r_key = ("raw", expr, ww, tuple(refs), False)
    rn = pool.request(r_key)
    ext_expr = f"$signed({rn})" if rs else rn
    e_key = ("raw", ext_expr, provider_width, (r_key,), True)
    en = pool.request(e_key)
    return en, e_key


def _inject_derived_inplace(vecs, rows, specs_triples):
    """`rows` belongs exclusively to this word (unlike val_results/verify_results, which are
    shared by all words), so derived values can be injected in place."""
    for vec, row in zip(vecs, rows):
        for synth_name, atom_a, atom_b in specs_triples:
            row[synth_name] = _derived_value(atom_a, atom_b, vec, row)


def _augment_rows(vecs, rows, specs_triples):
    """Returns a new list where each element is `ChainMap(derived-value dict, original row)`
    — O(1) per row (not a full `dict()` copy at O(len(row))), because `rows` (val_results/
    verify_results) is an object **shared by all words**, so a word-specific synthetic key can't
    be injected into it in place."""
    from collections import ChainMap
    out = []
    for vec, row in zip(vecs, rows):
        derived_vals = {s: _derived_value(a, b, vec, row) for s, a, b in specs_triples}
        out.append(ChainMap(derived_vals, row))
    return out


class _DerivedSim:
    """Wraps `nl` so that every row returned by `.simulate()` automatically includes this
    word's derived atom values — `_atom_value` looks values up by name, so derived atoms can
    seamlessly participate in `eval_f`/`classify`/`_covers_support`/`_refine`/`_hillclimb`/
    `_decision_boundary_vectors`; none of these functions need to know derived atoms exist, nor
    change their signature to pass an extra `derived_specs` parameter."""
    __slots__ = ("_nl", "_specs")

    def __init__(self, nl, specs_triples):
        self._nl = nl
        self._specs = specs_triples

    def simulate(self, vectors):
        rows = self._nl.simulate(vectors)
        if self._specs:
            for vec, row in zip(vectors, rows):
                for synth_name, atom_a, atom_b in self._specs:
                    row[synth_name] = _derived_value(atom_a, atom_b, vec, row)
        return rows

    def __getattr__(self, attr):
        return getattr(self._nl, attr)


# Milestone 27: k<=2 budget -- how many extra base atoms a derived candidate can carry at most.
# WARNING same as above, no trailing comment on this line.
DERIVED_TERM_MAX_BASE = 1
# base atom. Naming it as a constant (rather than hardcoding the limit into the loop structure) is deliberate, so that
# `tools/mutate.py` can remove this budget on its own, verifying its signal separately from `DERIVED_MAX`.


def _enumerate_derived_terms(atoms, derived_specs):
    """Derived candidates: `(one derived atom) × (0 or 1 base atoms) × coefficient ±1`,
    **not** folded into `enumerate_terms`'s general n (see the milestone 27 section of the
    module docstring). The derived atom's coefficient is fixed at +1 (the existing convention of
    normalizing the first term; a base atom free to be ±1 already covers both the `{+1,+1}`/
    `{+1,-1}` equivalence classes — the same argument as `_sign_assignments` fixing only the
    first term at +1: `(a,d)` and `(-a,-d)` are the same hyperplane via `classify`'s GE/LE swap,
    so only one representative needs to be kept)."""
    for synth_name, _atom_a, _atom_b, _cost in derived_specs:
        if _atom_a[0] == WRAP_TAG:
            # a wraparound atom's metadata is just its own `(read_signed, wrap_w)` -- see
            # see the milestone 30 section of the module docstring, "the same row value uses two synthetic names
            # serves both signed/unsigned readings at once".
            d = (synth_name, _atom_a[7], _atom_a[4], True)
        else:
            d = (synth_name, False, 1, True)
        yield [(d[0], d[1], d[2], d[3], 1)]
        if DERIVED_TERM_MAX_BASE < 1:
            continue
        for port, signed, width, is_output in atoms:
            for c in (1, -1):
                yield [(d[0], d[1], d[2], d[3], 1),
                       (port, signed, width, is_output, c)]


def _covers_support(terms, atom_support, word_sup):
    covered = set()
    for port, _signed, _width, is_output, _coeff in terms:
        covered |= atom_support.get((port, is_output), frozenset())
        if word_sup <= covered:
            return True
    return word_sup <= covered


def enumerate_terms(atoms, max_k=3):
    """Enumerates in a fixed order of (k, atom-index combination, coefficient assignment) —
    uses no set/dict iteration at all, guaranteeing a deterministic order independent of
    PYTHONHASHSEED (see the determinism test in selftest)."""
    n = len(atoms)
    for k in range(1, min(max_k, n) + 1):
        for combo in _combinations(range(n), k):
            for signs in _sign_assignments(k):
                terms = [(atoms[i][0], atoms[i][1], atoms[i][2], atoms[i][3], s)
                         for i, s in zip(combo, signs)]
                yield terms


def _combinations(iterable, k):
    from itertools import combinations
    return combinations(iterable, k)


def _sign_assignments(k):
    from itertools import product
    for signs in product((1, -1), repeat=k):
        if signs[0] == 1:
            yield signs


def enumerate_terms_wide(atoms, max_k=2, coef_max=None):
    """Milestone 31 stage 2: `enumerate_terms` only gives coefficients ±1, and can't solve
    comparison expressions like `2*in1 < in2`/`9*in1 < 6*in3` that need scaled-up coefficients.
    The caller only calls this function (see `try_compare`) when `enumerate_terms` (±1, k<=3)
    finds no candidate at all, and `len(atoms) <= COMPARE_WIDE_POOL_MAX` — the pool has to be
    small because the scale is `k<=2 combos × coef_max^k coefficient magnitudes × 2^(k-1) sign
    combinations`, which explodes as n grows (see the enumeration-size comparison table in the
    module docstring).

    Same "first term fixed positive" normalization as `enumerate_terms`: the first atom's
    coefficient magnitude is free to be 1..coef_max, but its sign is fixed at +1 (an overall
    negative is equivalent to a GE/LE swap, already handled by `classify`); the remaining atoms'
    magnitudes and signs are both free.

    ⚠ Determinism: same discipline as `enumerate_terms` — a fixed nested loop order (k → combo
    → magnitude combination → sign combination), with no set/dict iteration at all.
    `itertools.product` isn't affected by `PYTHONHASHSEED`, so it's safe to use."""
    if coef_max is None:
        # WARNING don't write this as a default parameter `coef_max=COMPARE_COEF_MAX`: Python only
        # evaluated once at def time; monkeypatching `COMPARE_COEF_MAX` within the same process wouldn't
        # take effect, producing a false-green test that "looks like it's there, but actually tests nothing".
        coef_max = COMPARE_COEF_MAX
    from itertools import product
    n = len(atoms)
    for k in range(1, min(max_k, n) + 1):
        for combo in _combinations(range(n), k):
            for mags in product(range(1, coef_max + 1), repeat=k):
                for signs in _sign_assignments(k):
                    terms = [(atoms[i][0], atoms[i][1], atoms[i][2], atoms[i][3], s * m)
                             for i, s, m in zip(combo, signs, mags)]
                    yield terms


# --------------------------------------------------------------------------
# REL / C determination
# --------------------------------------------------------------------------

def classify(fs, ys):
    """Returns (rel, lo, hi) or None. `rel` is in {"GE","LE","EQ","NE"}; the valid range for C
    is [lo, hi] (inclusive), lo==hi means it has already been pinned down exactly."""
    Z = sorted({f for f, y in zip(fs, ys) if not y})
    O = sorted({f for f, y in zip(fs, ys) if y})
    if not Z or not O:
        return None
    if Z[-1] < O[0]:
        return "GE", Z[-1] + 1, O[0]
    if O[-1] < Z[0]:
        return "LE", O[-1], Z[0] - 1
    if len(O) == 1 and O[0] not in Z:
        return "EQ", O[0], O[0]
    if len(Z) == 1 and Z[0] not in O:
        return "NE", Z[0], Z[0]
    return None


def _apply_rel(rel, f, C):
    if rel == "GE":
        return f >= C
    if rel == "LE":
        return f <= C
    if rel == "EQ":
        return f == C
    return f != C


# --------------------------------------------------------------------------
# Threshold refinement: construct vectors that push f into the gap, converge using nl.simulate() as ground truth
# --------------------------------------------------------------------------

def _mutation_bits(nl, terms, ctx):
    bits = set()
    for port, _signed, width, is_output, _coeff in terms:
        if not is_output:
            for i in range(width):
                bits.add((port, i))
        else:
            for b in ctx.atom_support.get((port, True), ()):
                p, i = split_bit(b)
                bits.add((p, i))
    # sorted descending by "this bit's |weight| in the linear weight model of the word this atom belongs to",
    # only used as a priority order for hillclimb sampling (see `_hillclimb`), not a precise model --
    # ground truth is always verified with nl.simulate(); guessing the wrong direction only slows convergence, it never causes an error.
    def score(pb):
        p, i = pb
        best = 0
        for port, _signed, _width, is_output, _coeff in terms:
            if not is_output or port not in ctx.weights_by_word:
                continue
            _k, weights = ctx.weights_by_word[port]
            key = p if ctx.port_widths.get(p, 1) == 1 else f"{p}[{i}]"
            w = weights.get(key, 0)
            w = w if w < (1 << 30) else (1 << 31) - w  # roughly uses "close to 0" as a magnitude proxy
            best = max(best, abs(w))
        return best
    return sorted(bits, key=lambda pb: (-score(pb), pb[0], pb[1]))


def _direct_seed(terms, target, zero_vec):
    """A pure primary-input atom can be solved directly: since the coefficient is only ±1
    and the atom is the port itself, the value the port should be set to can be computed
    directly to hit (or get as close as possible to) `target`, without relying on random search
    fumbling its way from all-0 (a 20-bit port randomly walking from 0 to near 500000 by pure
    random flip is almost impossible to converge on within a limited budget). Terms involving an
    output atom are kept at 0 (that part is left to `_hillclimb`'s random search, using the
    weight model as a direction hint)."""
    vec = dict(zero_vec)
    remaining = target
    for port, signed, width, is_output, coeff in terms:
        if is_output:
            continue
        lo = -(1 << (width - 1)) if signed else 0
        hi = (1 << (width - 1)) - 1 if signed else (1 << width) - 1
        # Milestone 31: coefficients are no longer limited to ±1 (`enumerate_terms_wide` can go up to
        # `COMPARE_COEF_MAX`), so "divide by coeff" can no longer be lazily written as "multiply by
        # multiply by coeff". For coeff in {1,-1} the two are bit-for-bit equivalent, existing behavior unchanged.
        desired = remaining // coeff
        clamped = max(lo, min(hi, desired))
        vec[port] = clamped & ((1 << width) - 1)
        remaining -= coeff * clamped
    return vec


def _random_ports(mut_bits, port_widths, rng):
    """The set of ports involved in `mut_bits` (order of first appearance preserved, deterministic)."""
    seen = []
    seen_set = set()
    for port, _i in mut_bits:
        if port not in seen_set:
            seen_set.add(port)
            seen.append(port)
    return seen


def _hillclimb(nl, terms, target, base_vec, mut_bits, port_widths, rng):
    if not mut_bits:
        return None, None
    cur = _direct_seed(terms, target, base_vec)
    row = nl.simulate([cur])[0]
    best_f = eval_f(terms, cur, row)
    best_vec = cur
    if best_f == target:
        return best_vec, best_f
    top_bits = mut_bits[: max(4, len(mut_bits) // 2)]
    ports = _random_ports(mut_bits, port_widths, rng)
    for _ in range(HILLCLIMB_OUTER):
        cands = []
        for _ in range(HILLCLIMB_BATCH):
            roll = rng.random()
            if roll < 0.15:
                # a fully random reroll of the ports involved (not a tweak from best_vec): pure bit-flip
                # hillclimbing easily gets stuck at a local optimum when "the atom is a nonlinear function (e.g. a product)",
                # once it's some distance from target it can never flip again -- bail out here to avoid getting stuck.
                v2 = dict(base_vec)
                for p in ports:
                    v2[p] = rng.getrandbits(port_widths.get(p, 1))
            else:
                v2 = dict(best_vec)
                nflip = rng.choice((1, 1, 2, 2, 3))
                pool = top_bits if roll < 0.15 + 0.7 * 0.85 else mut_bits
                for _ in range(nflip):
                    port, i = pool[rng.randrange(len(pool))]
                    v2[port] = v2.get(port, 0) ^ (1 << i)
            cands.append(v2)
        rows = nl.simulate(cands)
        for v2, row2 in zip(cands, rows):
            f2 = eval_f(terms, v2, row2)
            if abs(f2 - target) < abs(best_f - target):
                best_f, best_vec = f2, v2
        if best_f == target:
            break
    return best_vec, best_f


DECISION_PIVOT_BATCH = 400  # the number of random base vectors (shared with stage 1 pivot solving) -- only costs an extra
# one `nl.simulate()` call (batched), the measured cost of raising it from 40 to 400 is nearly 0
# (rerunning the three cases test13/test25/test28, the total time shows no observable
# difference), yet it drove up the boundary-vector count for the two wrong models random_450/random_54 from
# raised 86~112/6 to a more robust scale (see the latest measurement in `tools/probe_m20.py`).
DECISION_PERTURB_TRIES = 6  # the number of times `_hillclimb` is called per target (stage 2 fallback,
# the only way to generate vectors for shapes where `terms` contains an output atom -- pivot solving is
# is_output atom, it's powerless here, see `_pivot_boundary_vectors` docstring).

# WARNING milestone 20's first version once put a `MIN_BOUNDARY` production rejection clause here -- "can't generate
# rejects the candidate once there are enough vectors" -- after shipping it turned out this design itself had a problem: `_decision_boundary_
# vectors` for the "single output atom" shape (e.g. `$signed(out1) <= -5`, pivot
# solving is completely useless, and only `_hillclimb` near-miss perturbation) can only produce 4~10 entries, the count
# the threshold wrongly judged the **correct** candidate in test17/test25 as "can't be tested" and rejected it, forcing
# a candidate with the same cost, different rendered text, but equally correct (already verified with rtlcheck 20000 vectors × 3
# seeds verified the two equivalent). "The construction method can't produce a vector" and "the candidate is wrong" are two different things; conflating them
# mixing them into the same boolean criterion would systematically wrongly kill shapes that are inherently weaker at construction, not just wrongly kill
# a genuinely wrong model.
#
# Fix: production only rejects a candidate when `_decision_boundary_vectors` **actually observes a mismatch**
# only then reject the candidate (see how it's used in `try_compare`) -- the two real counterexamples (`random_450`/
# `random_54`) was already caught by mismatch > 0 (35/182, 1/5, see milestone 20
# the original report and `tools/probe_m20.py`), never needed to rely on a count threshold. "Must not silently
# degrade into a no-op" is now placed in the coverage assertions of `tools/selftest.py` and
# in `tools/probe_m20.py`'s measurement table -- that's the right place for "the construction method has degraded" to light up,
# a place, not the production candidate accept/reject logic.
#
# The name `MIN_BOUNDARY` is reserved for the test/evidence layer to use as "the lower bound on generated count for a known shape"
# (both `tools/selftest.py`/`tools/probe_m20.py` import this constant to assert
# regression), no longer a production constant that `try_compare` reads.
MIN_BOUNDARY = 8


def _encode_signed(value, width):
    lo = -(1 << (width - 1))
    hi = (1 << (width - 1)) - 1
    if value < lo or value > hi:
        return None
    return value & ((1 << width) - 1)


def _encode_unsigned(value, width):
    if value < 0 or value >= (1 << width):
        return None
    return value


def _pivot_boundary_vectors(terms, target, base_vecs, base_rows):
    """For each random base vector, picks each non-output atom as a pivot and solves backward
    for that port's value to make `f(x)` hit `target` (`others` excludes every term that
    references the same port — the same port may appear in `terms` as both an unsigned and a
    signed atom, and both move together as the port value changes, so it's not enough to
    subtract only the pivot's own term). The value solved backward is only a proposal; the
    caller filters it afterward with `nl.simulate()` + `eval_f` for correctness (the same
    "ground truth is always via simulation" discipline as `_hillclimb`) — getting the direction
    wrong here only causes the proposal to be filtered out, it never produces a non-equivalent
    verification vector."""
    cands = []
    for vec, row in zip(base_vecs, base_rows):
        for pivot_i, (port, signed, width, is_output, coeff) in enumerate(terms):
            if is_output:
                continue
            others = 0
            for j, (p2, s2, w2, o2, c2) in enumerate(terms):
                if p2 == port:
                    continue
                others += c2 * _atom_value(p2, s2, w2, o2, vec, row)
            # Milestone 31: same as `_direct_seed`, but coefficients may be >1, so solving backward requires actual division.
            # not dividing evenly means this pivot has no integer solution, just skip it -- what's produced here is only
            # a proposal, one fewer proposal never lets a wrong candidate get accepted (see this function's docstring).
            num = target - others
            if num % coeff:
                continue
            desired = num // coeff
            enc = _encode_signed(desired, width) if signed else _encode_unsigned(desired, width)
            if enc is None:
                continue
            cand = dict(vec)
            cand[port] = enc
            cands.append(cand)
    return cands


def _decision_boundary_vectors(nl, ctx, terms, C):
    """Constructs vectors that make the fitted `f(x)` (`eval_f`) land in `{C-1, C, C+1}` —
    this is the true **decision boundary**, a different thing from the **input-space boundary**
    swept by `_gen_sign_boundary_vectors` (see the milestone 20 section of the module docstring).
    Two construction methods:

    1. Pivot solving (`_pivot_boundary_vectors`): randomly pick a batch of base vectors, and for
       each non-output atom, treat it as a pivot and solve backward. Shapes with only
       primary-input atoms (e.g. `random_54`) rely mainly on this.
    2. Near-miss perturbation (fallback): reuses the existing `_hillclimb` (the only option when
       output atoms are involved — an output atom's value can't be set directly like a port).

    Returns `[(vec, row), ...]`, where `row` is the ground truth already computed by
    `nl.simulate`, so the caller doesn't need to simulate again. Any given entry only has "f
    landing near C", it isn't guaranteed to exactly equal some target — afterward they're
    filtered by `abs(f - C) <= 1`, independent of whether the construction was aiming precisely
    at a hit.
    """
    rng = ctx.rng
    port_widths = ctx.port_widths
    input_ports = [p for p, _ in nl.inputs]

    base_vecs = []
    for _ in range(DECISION_PIVOT_BATCH):
        vec = dict(ctx.zero_vec)
        for p in input_ports:
            w = port_widths[p]
            vec[p] = rng.getrandbits(w) if w > 0 else 0
        base_vecs.append(vec)
    base_rows = nl.simulate(base_vecs) if base_vecs else []

    targets = (C - 1, C, C + 1)
    cands = []
    for target in targets:
        cands.extend(_pivot_boundary_vectors(terms, target, base_vecs, base_rows))

    mut_bits = _mutation_bits(nl, terms, ctx)
    for target in targets:
        for _ in range(DECISION_PERTURB_TRIES):
            vec, _f = _hillclimb(nl, terms, target, ctx.zero_vec, mut_bits, port_widths, rng)
            if vec is not None:
                cands.append(vec)

    if not cands:
        return []
    rows = nl.simulate(cands)
    out = []
    seen = set()
    for vec, row in zip(cands, rows):
        f = eval_f(terms, vec, row)
        if abs(f - C) > 1:
            continue
        key = tuple(sorted(vec.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append((vec, row))
    return out


def _refine(nl, name, terms, rel, lo, hi, ctx, fs, ys):
    if rel in ("EQ", "NE") or lo == hi:
        return lo
    mut_bits = _mutation_bits(nl, terms, ctx)
    fs, ys = list(fs), list(ys)
    for _ in range(REFINE_ROUNDS):
        if lo == hi:
            return lo
        target = lo + (hi - lo) // 2
        vec, f_val = _hillclimb(nl, terms, target, ctx.zero_vec, mut_bits, ctx.port_widths, ctx.rng)
        if vec is None:
            break
        row = nl.simulate([vec])[0]
        y = row[name]
        fs.append(f_val)
        ys.append(y)
        cls2 = classify(fs, ys)
        if cls2 is None or cls2[0] != rel:
            return None
        _rel2, lo, hi = cls2
    return lo if lo == hi else None


# --------------------------------------------------------------------------
# Rendering: folds (terms, rel, C) into `Lexpr REL2 Rexpr` (or `Lexpr REL2 literal`)
# --------------------------------------------------------------------------

def _side_render(terms, K, width, port_widths):
    """terms is non-empty. Returns (cost, key): `key` is a pool-entry tuple that can be handed
    directly to `SharePool.request()`. For a single atom with coefficient +1 and K==0, uses the
    cost-0 "portview"; otherwise uses "affine", with cost computed through `render_word` (the
    same rendering source as `share.py`, guaranteeing the cost estimated here matches the text
    actually emitted at the end)."""
    if len(terms) == 1 and terms[0][2] == 1 and K == 0:
        port, signed, _c = terms[0]
        pw = port_widths.get(port, width)
        return 0, ("portview", port, width, signed)
    word_terms = [Term("word", p, port_widths.get(p, width), c, signed=s) for p, s, c in terms]
    expr, _needs = render_word("_x", width, K, word_terms, signed_prefix="s_")
    cost, _detail = cost_of_text(f"output x;\nassign x = {expr};\n")
    key = ("affine", tuple(sorted(terms)), K, width)
    return cost, key


def _threshold_literal(value, width):
    if -INT32_MARGIN <= value <= INT32_MARGIN:
        return str(value)
    if value >= 0:
        return f"{width}'sd{value}"
    return f"-{width}'sd{-value}"


def _rel_variants(rel, C):
    if rel == "GE":
        return [(">=", C), (">", C - 1)]
    if rel == "LE":
        return [("<=", C), ("<", C + 1)]
    return [(REL_TEXT[rel], C)]


def _best_rendering(terms, rel, C, width, port_widths):
    """terms: [(port,signed,width,is_output,coeff), ...], coeff is already the final coefficient.
    Returns (cost, kind, rel2, key_or_None, other) or None:
      kind == "expr_expr": (cost, "expr_expr", rel2, key_l, key_r)
      kind == "expr_lit":  (cost, "expr_lit", rel2, key, literal_value)
    """
    n = len(terms)
    best = None
    for mask in range(1 << n):
        Lt = [(p, s, c) for i, (p, s, _w, _o, c) in enumerate(terms) if not (mask >> i) & 1]
        Rt = [(p, s, -c) for i, (p, s, _w, _o, c) in enumerate(terms) if (mask >> i) & 1]
        for rel2, c2 in _rel_variants(rel, C):
            if Lt and Rt:
                cost_l, key_l = _side_render(Lt, 0, width, port_widths)
                cost_r, key_r = _side_render(Rt, c2, width, port_widths)
                cand = (cost_l + cost_r + 1, "expr_expr", rel2, key_l, key_r)
            elif Lt and not Rt:
                cost_l, key_l = _side_render(Lt, 0, width, port_widths)
                cand = (cost_l + 1, "expr_lit", rel2, key_l, c2)
            elif Rt and not Lt:
                cost_r, key_r = _side_render(Rt, 0, width, port_widths)
                cand = (cost_r + 1, "expr_lit", REL_MIRROR[rel2], key_r, -c2)
            else:
                continue
            if best is None or cand[0] < best[0]:
                best = cand
    return best


def _key_width(key):
    return key[2] if key[0] == "portview" else key[3]


def _materialize_one_derived(synth_name, pool, derived_lookup, provider_width):
    """Materializes a derived atom `A < B` into a real wire: `A` and `B` are each first
    materialized as `"portview"` pool entries (preserving the signedness-safety precondition
    from the milestone 20/M3b section of the module docstring, "both sides are already
    pre-resolved `wire signed`" — `portview` always declares `wire signed`, regardless of
    whether the `signed` field is True or False), then a `"raw"` pool entry wraps `"wa < wb"`
    (`raw` is the only tag that passes `refs` on to `SharePool`'s dependency tracking — see
    `share.py._refs_of`). Returns `(wire_name, raw_key)`."""
    atom_a, atom_b = derived_lookup[synth_name]
    if atom_a[0] == WRAP_TAG:
        return _materialize_wrap(atom_a, pool, provider_width)
    port_a, signed_a, _wa, _oa = atom_a
    port_b, signed_b, _wb, _ob = atom_b
    key_a = ("portview", port_a, provider_width, signed_a)
    key_b = ("portview", port_b, provider_width, signed_b)
    wa = pool.request(key_a)
    wb = pool.request(key_b)
    raw_key = ("raw", f"{wa} < {wb}", provider_width, (key_a, key_b), False)
    wn = pool.request(raw_key)
    return wn, raw_key


def _materialize_derived_key(key, pool, derived_lookup, provider_width):
    """Replaces any derived atom's synthetic name in `key` (a `"portview"` or `"affine"`) with
    the real materialized wire name, returning `(new_key, extra_refs)` — `extra_refs` are the
    newly materialized raw pool entries; the caller must merge them into the outermost `refs`
    (`portview`/`affine`'s `_refs_of` always returns an empty tuple, see `share.py`, so it won't
    automatically pass this indirect dependency on to `SharePool`'s liveness tracking — skip
    this step and `emit()` will fail to print the declaration)."""
    if key[0] == "portview":
        _, port, width, signed = key
        if port not in derived_lookup:
            return key, []
        wn, raw_key = _materialize_one_derived(port, pool, derived_lookup, provider_width)
        return ("portview", wn, width, signed), [raw_key]
    if key[0] == "affine":
        _, terms, k_const, width = key
        if not any(p in derived_lookup for p, _s, _c in terms):
            return key, []
        extra = []
        new_terms = []
        for p, s, c in terms:
            if p in derived_lookup:
                wn, raw_key = _materialize_one_derived(p, pool, derived_lookup, provider_width)
                new_terms.append((wn, s, c))
                extra.append(raw_key)
            else:
                new_terms.append((p, s, c))
        return ("affine", tuple(new_terms), k_const, width), extra
    return key, []


def build_expr(pool, rendering, derived_lookup=None, provider_width=None):
    """`derived_lookup`/`provider_width` (milestone 27, optional): only
    `compare.try_compare` itself ever passes them; `(undisclosed family).py` keeps using the
    existing 2-arg call, and when `None`, behavior is exactly the same as before the change
    (see `_materialize_derived_key`: when no key contains a derived atom's synthetic name, it
    passes through unchanged, at no extra cost)."""
    _cost, kind, rel2, a, b = rendering
    extra = []
    if derived_lookup:
        a, extra_a = _materialize_derived_key(a, pool, derived_lookup, provider_width)
        extra.extend(extra_a)
        if kind == "expr_expr":
            b, extra_b = _materialize_derived_key(b, pool, derived_lookup, provider_width)
            extra.extend(extra_b)
    if kind == "expr_expr":
        name_l = pool.request(a)
        name_r = pool.request(b)
        return f"{name_l} {rel2} {name_r}", (a, b) + tuple(extra)
    name_l = pool.request(a)
    lit = _threshold_literal(b, _key_width(a))
    return f"{name_l} {rel2} {lit}", (a,) + tuple(extra)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def _values_subset_bool(name, ctx):
    """Checks whether this word's value is ⊆ {0,1} across **all vectors currently
    available** (training set + independent acceptance set + boundary vectors). M26 replaced the
    proxy "width == 1" with this actual property that needs preserving — the width filter was
    originally there to block "the raw value of a multi-bit word isn't boolean, and `classify()`
    would misjudge by treating truthiness as y" (an earlier implementation missed this and once
    wrongly "solved" test18's out1/out3/out5 as cost-1 comparison expressions, caught by
    rtlcheck); but the actual root cause is the value range, not the width itself — a D0-wide
    word (`out1` declared as multi-bit but whose value range is still {0,1}) should also be able
    to go this route (golden cost 1, Verilog assignment auto-zero-extends).

    ⚠ Looking only at random training vectors can be fooled by a word whose "third value is
    very rare" (see the M26 note in the module/`(undisclosed family).py`: `random_484`'s
    `(in2<in3)<<1` only takes its third value when `in2=0 and in3=1`, probability 1/512, never
    hit even once across 4600 random vectors) — so at least three batches must be used here:
    `ctx.val_results` (training set), `ctx.verify_results` (independent acceptance set), and
    `ctx.boundary_results[name]` (signedness boundary vectors, already computed by
    `build_context`) — looking at only one of them isn't enough."""
    for results in (ctx.val_results, ctx.verify_results, ctx.boundary_results.get(name, [])):
        for r in results:
            if r[name] not in (0, 1):
                return False
    return True


def try_compare(nl, name, width, ctx, pool):
    """Runs the full comparison-expression extraction flow for a single output word. Returns
    (expr, signed_needs, pool_refs) or None. `signed_needs` is always an empty set — the
    top-level comparison expression only references pool entries' materialized names, and all
    `$signed()` calls are confined inside the pool entries' own independent assigns (see
    `share.py`)."""
    if not _values_subset_bool(name, ctx):
        # value range isn't {0,1}: this word's raw value isn't boolean, `classify()` taking its
        # treating truthiness as y would misjudge (see `_values_subset_bool` docstring).
        return None
    atoms = ctx.atoms.get(name, [])
    if not atoms:
        return None
    word_sup = ctx.word_support.get(name)
    if not word_sup:
        return None  # a constant word, shouldn't go through the comparison family

    # Milestone 27: this word's derived (comparison-bit) atoms -- only used by `compare` itself,
    # `(undisclosed family)` can't see it at all (see the comment on the `Context.derived_atoms` field). When the list is empty
    # every step below is a zero-overhead no-op.
    derived_specs = ctx.derived_atoms.get(name, [])
    derived_triples = [(s, a, b) for s, a, b, _c in derived_specs]
    derived_cost = {s: c for s, _a, _b, c in derived_specs}
    derived_lookup = {s: (a, b) for s, a, b, _c in derived_specs} or None
    dnl = _DerivedSim(nl, derived_triples) if derived_triples else nl

    val_results = ctx.derived_val_results.get(name, ctx.val_results) if derived_specs else ctx.val_results
    verify_results_full = (ctx.derived_verify_results.get(name, ctx.verify_results)
                            if derived_specs else ctx.verify_results)

    train_vectors = ctx.val_vectors + ctx.boundary_vectors.get(name, [])
    train_results = val_results + ctx.boundary_results.get(name, [])
    ys = [r[name] for r in train_results]

    scored = []
    order_idx = 0
    for terms in enumerate_terms(atoms):
        order_idx += 1
        if not _covers_support(terms, ctx.atom_support, word_sup):
            continue
        fs = [eval_f(terms, v, r) for v, r in zip(train_vectors, train_results)]
        cls = classify(fs, ys)
        if cls is None:
            continue
        rel, lo, hi = cls
        provisional_C = hi
        rendering = _best_rendering(terms, rel, provisional_C, ctx.provider_width,
                                     ctx.port_widths)
        if rendering is None:
            continue
        scored.append((rendering[0], order_idx, terms, rel, lo, hi, fs))

    pool_ok = len(atoms) <= COMPARE_WIDE_POOL_MAX
    if pool_ok:
        global wide_pool_seen
        wide_pool_seen += 1
    if not scored and pool_ok:
        # Milestone 31 stage 2: only triggers when stage 1 (±1, k<=3) finds no candidate at all,
        # see `enumerate_terms_wide` docstring. `wide_stage_calls` is for
        # the threshold-lock evidence read by `tools/probe_m31.py` -- a word from the real corpus should never even once
        # to make this counter tick.
        global wide_stage_calls
        wide_stage_calls += 1
        for terms in enumerate_terms_wide(atoms):
            order_idx += 1
            if not _covers_support(terms, ctx.atom_support, word_sup):
                continue
            fs = [eval_f(terms, v, r) for v, r in zip(train_vectors, train_results)]
            cls = classify(fs, ys)
            if cls is None:
                continue
            rel, lo, hi = cls
            provisional_C = hi
            rendering = _best_rendering(terms, rel, provisional_C, ctx.provider_width,
                                         ctx.port_widths)
            if rendering is None:
                continue
            scored.append((rendering[0], order_idx, terms, rel, lo, hi, fs))

    for terms in _enumerate_derived_terms(atoms, derived_specs):
        order_idx += 1
        if not _covers_support(terms, ctx.atom_support, word_sup):
            continue
        fs = [eval_f(terms, v, r) for v, r in zip(train_vectors, train_results)]
        cls = classify(fs, ys)
        if cls is None:
            continue
        rel, lo, hi = cls
        provisional_C = hi
        rendering = _best_rendering(terms, rel, provisional_C, ctx.provider_width,
                                     ctx.port_widths)
        if rendering is None:
            continue
        # add back the derived atom's own `<` operator cost -- `_side_render` for a derived atom
        # uses the synthetic name as a placeholder identifier for pricing, which can't measure this cost (see
        # see `_build_derived_atoms` docstring). Only affects the priority ordering among candidates,
        # doesn't affect correctness: the actual cost is determined by the finally-rendered Verilog text.
        cost = rendering[0] + derived_cost[terms[0][0]]
        scored.append((cost, order_idx, terms, rel, lo, hi, fs))

    scored.sort(key=lambda t: (t[0], t[1]))

    verify_vectors = ctx.verify_vectors + ctx.boundary_vectors.get(name, [])
    verify_results = verify_results_full + ctx.boundary_results.get(name, [])

    for _cost, _idx, terms, rel, lo, hi, fs in scored:
        final_C = _refine(dnl, name, terms, rel, lo, hi, ctx, fs, ys)
        if final_C is None:
            continue
        ok = True
        for v, r in zip(verify_vectors, verify_results):
            f = eval_f(terms, v, r)
            if _apply_rel(rel, f, final_C) != bool(r[name]):
                ok = False
                break
        if not ok:
            continue
        # Decision-boundary verification: only rejects when a "mismatch is observed" -- being unable to construct a vector (`bvecs`
        # is empty) doesn't mean the candidate has a problem, it just means this gate has no opinion on it, and it proceeds as usual.
        # "rejecting a correct model" costs more than "missing an incorrect model": this project's principle is "rather have the whole
        # a word falling back must still not be non-equivalent", but if this gate itself makes the correct answer fall back too, that is
        # trading a correct solution for a risk that doesn't actually exist (see the milestone 20 fix report: `MIN_BOUNDARY`
        # once misjudged the correct candidate for the "single output atom" shape in test17/test25 as
        # rejected as "can't be tested", forcing a candidate with the same cost, different rendered text, but equally correct
        # -- "can't be tested" and "is wrong" are two different things, they can't be conflated with a count threshold).
        # this discipline of "the construction method must not silently degrade into a no-op" moved to `tools/selftest.py` and
        # `tools/probe_m20.py` (the test/evidence layer), not a production rejection clause.
        bvecs = _decision_boundary_vectors(dnl, ctx, terms, final_C)
        for v, r in bvecs:
            f = eval_f(terms, v, r)
            if _apply_rel(rel, f, final_C) != bool(r[name]):
                ok = False
                break
        if not ok:
            continue
        rendering = _best_rendering(terms, rel, final_C, ctx.provider_width, ctx.port_widths)
        if rendering is None:
            continue
        expr, refs = build_expr(pool, rendering, derived_lookup, ctx.provider_width)
        return expr, set(), tuple(refs)
    return None


FAMILY = {
    "kind": "compare",
    "label": "comparison expression solved",
    "order": 30,
    "stage": "primary",
    "build_context": lambda env: build_context(env.nl, env.weights_by_word, env.val_vectors,
                                               env.val_results, env.verify_vectors,
                                               env.verify_results),
    "attempt": lambda env, ctx, name, width: try_compare(env.nl, name, width, ctx, env.pool),
    "shape": "expr3",
}
