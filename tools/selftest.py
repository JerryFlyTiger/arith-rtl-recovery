"""Unit tests: pin down a few behaviors that "the existing test
    infrastructure cannot catch".

    python3 tools/selftest.py

Why this is needed: `solverbench.py` / `costcount.py` /
`gensynth.py --selfcheck` are all **corpus-driven** -- they can only exercise
the paths that the 30 real cases + 9 synthetic cases happen to hit. Cold
review found in practice that two fixes are never triggered anywhere in the
corpus; reverting them leaves all three of the above tools fully green:

- `render_word`'s "when all coefficients are negative, the first term must
  pick one with |coefficient| != 1" -- the only corpus item that reaches the
  all-negative branch is `synth04`, and both of its coefficients happen to
  be non-±1, so the old and new logic give the same answer.
- `costcount.GATE_INST`'s "the instance name is optional" -- there is no
  unnamed gate instance anywhere in the corpus (`fallback.render_gates`
  always auto-generates a `u_<net>` name).

**A fix with no test protecting it is the same as no fix**: the next person
who refactors that code and reverts it will trigger no red light at all. So
these two are pinned down with direct-call assertions that do not depend on
the corpus.
"""
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "solver"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from model import Term, render_word, cost_of_word_assign, signed_wire_name  # noqa: E402
from costcount import cost_of_text  # noqa: E402
from share import fresh_prefix, SharePool  # noqa: E402
from netlist import Netlist  # noqa: E402
import compare  # noqa: E402
import mux  # noqa: E402
import main as solver_main  # noqa: E402
import registry  # noqa: E402
import rtlcheck  # noqa: E402
from refgen import Builder  # noqa: E402
FAILED = []
def check(name, got, want):
    ok = got == want
    print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"         got  {got!r}\n         want {want!r}")
        FAILED.append(name)
def test_all_negative_first_term():
    """When all coefficients are negative, the unary minus must attach to a
    **numeric literal** (free), not to an identifier (which costs 1).

    Reverting to `first_i = positive[0] if positive else 0` would make
    [-1*a, -3*b] print as `-a - 8'd3 * b` (cost 3) instead of
    `-8'd3 * b - a` (cost 2).
    """
    M = 1 << 8
    for order in (["a", "b"], ["b", "a"]):
        coeff = {"a": -1 % M, "b": -3 % M}
        terms = [Term("word", p, 8, coeff[p]) for p in order]
        expr, _ = render_word("o", 8, 0, terms)
        cost = cost_of_word_assign("o", 8, expr, cost_of_text)
        check(f"all-negative mixed coefficients, input order {order} -> cost 2", cost, 2)
        check(f"all-negative mixed coefficients, input order {order} -> text determinism", expr, "-8'd3 * b - a")

    # all-negative and **all** ±1: there is no non-±1 term to pick, it has to pay that 1 point, but must not break because of it.
    terms = [Term("word", p, 8, -1 % M) for p in ("a", "b")]
    expr, _ = render_word("o", 8, 0, terms)
    check("all-negative and all ±1 -> a valid expression is still produced", expr, "-a - b")
def test_unnamed_gate_instance_costs_one():
    """Verilog allows unnamed gate instances; miscounting them would underestimate the fallback output's cost to 0."""
    named = ("module top(a,b,c);\n  input a,b;\n  output c;\n"
             "  and u_n1(c, a, b);\nendmodule\n")
    unnamed = ("module top(a,b,c);\n  input a,b;\n  output c;\n"
               "  and (c, a, b);\nendmodule\n")
    for label, text in (("named", named), ("unnamed", unnamed)):
        total, detail = cost_of_text(text)
        check(f"{label} gate instances count as cost 1", detail.get("gate instance", 0), 1)

    # negative case: an identifier starting with a gate keyword must **not** be misjudged as a gate instance.
    decoy = ("module top(a,b,c);\n  input a,b;\n  output c;\n"
             "  wire and_flag;\n  assign and_flag = a & b;\n"
             "  assign c = and_flag;\nendmodule\n")
    _, detail = cost_of_text(decoy)
    check("an identifier starting with a keyword is not misjudged", detail.get("gate instance", 0), 0)
def test_fresh_prefix_avoids_collision():
    """If the src that `fresh_prefix` scans already has identifiers like
    `cse_xxx`, `cse_z_xxx`, the returned prefix must genuinely not collide
    with any of them, and it must terminate (no infinite loop)."""
    src = ("module top(a,b,c);\n  input a,b;\n  output c;\n"
           "  wire cse_foo, cse_z_bar;\n"
           "  and u1(cse_foo, a, b);\n  and u2(cse_z_bar, a, cse_foo);\n"
           "  assign c = cse_z_bar;\nendmodule\n")
    p = fresh_prefix(src, base="cse")
    ids = set(__import__("re").findall(r"[A-Za-z_][A-Za-z0-9_$]*", src))
    collided = any(i.startswith(p) for i in ids)
    check("fresh_prefix does not collide with any identifier in src that already has a collision", collided, False)
    check("fresh_prefix, on collision, the returned prefix is not equal to the default base", p != "cse_", True)

    # a normal src with no collision: the default prefix must keep exactly the same behavior as before the change ("s_").
    clean_src = "module top(a,b,c);\n  input a,b;\n  output c;\n  assign c = a;\nendmodule\n"
    check("fresh_prefix returns base_ when there is no collision", fresh_prefix(clean_src, base="s"), "s_")
def test_signed_wire_name_prefix():
    """`signed_wire_name` gained an optional prefix parameter; the default value must stay exactly as it was before the change."""
    check("signed_wire_name's default prefix is unchanged", signed_wire_name("in1", 8), "s_in1_w8")
    check("signed_wire_name with a custom prefix", signed_wire_name("in1", 8, prefix="csez_"),
          "csez_in1_w8")
def test_share_pool_price_commit():
    """The same pool item requested by two words: the first is charged, and
    after commit the second is charged 0; a request that is never committed
    does not affect subsequent pricing."""
    pool = SharePool("cse_")
    item = ("portview", "in1", 8, True)
    name1 = pool.request(item)
    name2 = pool.request(item)
    check("requesting the same pool item twice returns the same name", name1 == name2, True)

    affine = ("affine", (("in1", True, 3),), 5, 8)
    ref = pool.request(affine)
    check("the affine pool item has a name", isinstance(ref, str), True)
    price1 = pool.price([affine])
    # the exact value, not `> 0`: the hand-computed `_op_count` returns 1 for this term (it only counted the `+`),
    # the actual rendered `3 * $signed(in1) + 5` is 2 points (`*` and `+` each count 1).
    # asserting only `> 0` would miss this systematic underestimate, and cost is the score itself.
    check("the affine pool item's pricing includes multiplication", price1, 2)

    # a unary `-` on a negative leading coefficient: attaching it to an identifier costs 1 point (attaching it to a numeric literal is free).
    neg = ("affine", (("in2", False, -1),), 0, 8)
    pool.request(neg)
    check("a unary minus on a leading negative coefficient must be charged for", pool.price([neg]), 1)

    price_again = pool.price([affine])
    check("pricing the same thing again before commit still charges for it", price_again, price1)

    pool.commit([affine])
    check("after commit, pricing the same pool item again returns 0", pool.price([affine]), 0)
    check("committing one pool item does not affect another", pool.price([neg]), 1)

    # a port-view pool item's operator count is always 0 (pure renaming/pre-extension).
    check("a port-view pool item that was never committed prices at 0", pool.price([item]), 0)
def test_share_pool_price_matches_render():
    """Pricing must share the same source as rendering -- `price()`'s result
    must equal the actual cost of the text `emit()` produces. If the two
    ever drift apart, the solver would make trade-offs using the wrong
    numbers (not producing an inequivalent result, but picking a
    worse-scoring solution).

    WARNING this test can **only** guard against the class of regression
    where "someone hand-computed `_op_count` again" (which is exactly the
    first bug it caught). It cannot guard against "the rendering
    `_render_expr` chooses is not itself the cheapest" -- both sides go
    through the same `_render_expr`, so for ordering problems it's
    tautological. That gap is recorded in `solver/share.py`'s class
    docstring, to be settled together with constant literals once the
    compare family is wired in.
    """
    from costcount import cost_of_text
    cases = [
        ("portview", "in1", 8, True),
        ("affine", (("in1", True, 3),), 5, 8),
        ("affine", (("in1", True, 1), ("in2", False, -1)), 0, 12),
        ("affine", (("in2", False, -1),), 0, 8),
        ("affine", (("in1", True, 7), ("in2", True, -2)), -4, 16),
    ]
    for item in cases:
        pool = SharePool("cse_")
        pool.request(item)
        text = pool.emit([item])[0]
        rhs = text.split("assign", 1)[1].split("=", 1)[1].rsplit(";", 1)[0].strip()
        actual, _ = cost_of_text(f"output x;\nassign x = {rhs};\n")
        check(f"price and rendering share the same source: {rhs}", pool.price([item]), actual)
def test_share_pool_portview_signedness_in_name():
    """`("portview", p, w, True)` and `(..., False)` are two different pool
    items; their names must differ -- a name collision would produce broken
    RTL where the same identifier is driven by two assigns."""
    pool = SharePool("cse_")
    n_signed = pool.request(("portview", "in1", 8, True))
    n_unsigned = pool.request(("portview", "in1", 8, False))
    check("signed/unsigned port views have different names", n_signed != n_unsigned, True)
    lines = pool.emit([("portview", "in1", 8, True), ("portview", "in1", 8, False)])
    decls = [ln for ln in "\n".join(lines).splitlines() if "assign" in ln]
    lhs = [ln.split("assign", 1)[1].split("=", 1)[0].strip() for ln in decls]
    check("the left-hand sides of the two assigns do not collide", len(set(lhs)), len(lhs))
def test_share_pool_names_matches_emit():
    """`names()` and `emit()` must return the same set of pool items in the same
    order -- `names()` feeds the I3/I4 invariant checks, `emit()` feeds the
    actually-output text, and if the two decouple, the invariant checks end
    up checking a list unrelated to the output. M3a's only call site is
    always `names([])`, which the corpus cannot exercise."""
    pool = SharePool("cse_")
    items = [
        ("affine", (("in1", True, 1), ("in2", False, -1)), 0, 12),
        ("portview", "in1", 8, True),
        ("affine", (("in3", False, 2),), 7, 16),
    ]
    for it in items:
        pool.request(it)

    live = [items[2], items[0]]          # deliberately not passed in insertion order
    names = pool.names(live)
    lhs = [ln.split("assign", 1)[1].split("=", 1)[0].strip()
           for ln in "\n".join(pool.emit(live)).splitlines() if "assign" in ln]
    check("names() and emit() have the same pool items in the same order", names, lhs)
    check("an unreferenced pool item does not appear", any("v" in n and "in1_w8" in n for n in names), False)
    check("names([]) is empty", pool.names([]), [])
def test_share_pool_rejects_unrequested():
    """A pool item that was never `request()`-ed appearing in the closure must
    raise an error immediately, rather than later blowing up with a
    KeyError whose cause is impossible to trace."""
    pool = SharePool("cse_")
    try:
        pool.emit([("portview", "in1", 8, True)])
    except ValueError as e:
        check("a pool item that was never requested reports ValueError", "without going through request()" in str(e), True)
    else:
        check("a pool item that was never requested should raise an error", "no error", "ValueError")
def test_share_pool_poly_dedup_and_render():
    """Poly pool items (M5a: used by `(undisclosed family).py`, `share.py`'s
    third pool-item tag): `terms` are sorted before comparison, so the same
    set of terms in a different input order counts as the same pool item
    (consistent with affine's dedup discipline); rendering goes through
    `render_word`, and pricing must share the same source as rendering --
    this catches the class of regression where "someone hand-computed
    `_op_count` again", the same spirit as
    `test_share_pool_price_matches_render`, but that test does not cover the
    poly tag (the parenthesization/multiplication pricing path in
    `render_word` must be correct when `kind` mixes `{"word","sq","prod"}`)."""
    pool = SharePool("cse_")
    terms_a = (
        ("word", "in2", False, None, False, 1),
        ("prod", "in1", False, "in3", False, 1),
    )
    terms_b = (  # the same set of terms, with the input order reversed
        ("prod", "in1", False, "in3", False, 1),
        ("word", "in2", False, None, False, 1),
    )
    item_a = ("poly", terms_a, 0, 46)
    item_b = ("poly", terms_b, 0, 46)
    name_a = pool.request(item_a)
    name_b = pool.request(item_b)
    check("poly pool items dedup regardless of order", name_a, name_b)

    from costcount import cost_of_text
    price = pool.price([item_a])
    text = pool.emit([item_a])[0]
    rhs = text.split("assign", 1)[1].split("=", 1)[1].rsplit(";", 1)[0].strip()
    actual, _ = cost_of_text(f"output x;\nassign x = {rhs};\n")
    check("the poly pool item's pricing and rendering share the same source", price, actual)
    check("the poly pool item's pricing includes multiplication and addition (1 each for * and +)", price, 2)
def test_share_pool_poly_name_prefix_no_collision():
    """A poly pool item's naming prefix `p<idx>` must not collide with affine's
    `a<idx>` or portview's `v[su]_...` -- `_name_for` (`share.py`) uses a
    different prefix character for each of its three branches (`p`/`a`/`v`).

    WARNING cold review caught a trap: `_name_for`'s `idx` is **globally
    monotonically increasing** (`share.py`'s `idx = len(self._order)` is
    shared across tags), so even if the poly branch were mistakenly written
    to reuse affine's `f"{self.prefix}a{idx}"`, the three pool items would
    still each get a different `idx`, and the names would never collide by
    construction -- the assertion "three requests return three different
    strings" cannot tell whether the poly branch really prints the `p`
    prefix. So this test asserts the prefix letter itself directly, not just
    the size of the name set."""
    pool = SharePool("cse_")
    poly_item = ("poly", (("word", "in1", False, None, False, 1),), 0, 8)
    affine_item = ("affine", (("in1", False, 1),), 0, 8)
    view_item = ("portview", "in1", 8, True)
    name_poly = pool.request(poly_item)
    name_affine = pool.request(affine_item)
    name_view = pool.request(view_item)
    check("the poly pool item's naming prefix is cse_p", name_poly, "cse_p0")
    check("the affine pool item's naming prefix is cse_a", name_affine, "cse_a1")
    check("the three pool-item kinds poly/affine/portview never collide in naming",
          len({name_poly, name_affine, name_view}), 3)
def test_share_pool_raw_refs_of_and_others_empty():
    """M6c 3.4(a): `raw`'s `_refs_of` returns its `refs`; the other three kinds
    (portview/affine/poly) still always return `()` (`raw` is the first
    non-empty tag)."""
    pool = SharePool("cse_")
    b_item = ("raw", "in1 + 8'd1", 8, (), False)
    pool.request(b_item)
    key_b = pool._normalize(b_item)
    a_item = ("raw", f"{pool._names[key_b]} * 8'd2", 8, (b_item,), False)
    pool.request(a_item)
    key_a = pool._normalize(a_item)
    check("raw's _refs_of returns its refs (normalized)", pool._refs_of(key_a), (key_b,))
    check("raw (with no refs) _refs_of returns an empty tuple", pool._refs_of(key_b), ())
    check("portview's _refs_of always returns ()",
          pool._refs_of(pool._normalize(("portview", "in1", 8, True))), ())
    check("affine's _refs_of always returns ()",
          pool._refs_of(pool._normalize(("affine", (("in1", False, 1),), 0, 8))), ())
    check("poly's _refs_of always returns ()",
          pool._refs_of(pool._normalize(
              ("poly", (("word", "in1", False, None, False, 1),), 0, 8))), ())
def test_share_pool_raw_live_keys_transitive_closure():
    """M6c 3.4(b): `_live_keys`'s transitive closure over "A references B, B
    references C" is correct -- starting from A alone must still collect
    both B and C into the closure (`emit`/`names`/`signed_needs` all rely on
    this closure to decide which pool items to print)."""
    pool = SharePool("cse_")
    c_item = ("raw", "in1", 8, (), False)
    wire_c = pool.request(c_item)
    b_item = ("raw", f"{wire_c} + 8'd1", 8, (c_item,), False)
    wire_b = pool.request(b_item)
    a_item = ("raw", f"{wire_b} * 8'd2", 8, (b_item,), False)
    pool.request(a_item)

    keys = pool._live_keys([a_item])
    names = {pool._names[k] for k in keys}
    check("A refers to B refers to C: all three collected by transitive closure", names,
          {pool._names[pool._normalize(a_item)], wire_b, wire_c})

    emitted = pool.emit([a_item])
    check("starting only from A, emit still prints C (transitive closure, not just direct references)",
          any(wire_c in line for line in emitted), True)
def test_share_pool_raw_unsigned_renders_without_signed():
    """M6c 3.4(c): a `raw` with `signed=False` renders as `wire [W-1:0]`
    (without `signed`) -- only `raw` reads the `signed` field, the other
    three kinds still unconditionally render `wire signed` (`_render` must
    not be changed so that portview/affine/poly also look at this field)."""
    pool = SharePool("cse_")
    item = ("raw", "in1 & in2", 4, (), False)
    pool.request(item)
    text = pool.emit([item])[0]
    check("raw signed=False renders as wire [3:0] (without signed)",
          text.splitlines()[0].strip().startswith("wire [3:0]"), True)

    item_signed = ("raw", "in1 - in2", 4, (), True)
    pool.request(item_signed)
    text_signed = pool.emit([item_signed])[0]
    check("raw signed=True renders as wire signed [3:0]",
          text_signed.splitlines()[0].strip().startswith("wire signed [3:0]"), True)

    view_item = ("portview", "in1", 4, False)
    pool.request(view_item)
    view_text = pool.emit([view_item])[0]
    check("portview is unaffected by raw's signed-field branch and still unconditionally renders wire signed",
          view_text.splitlines()[0].strip().startswith("wire signed [3:0]"), True)
def test_share_pool_raw_mutation_refs_of_returns_empty():
    """Corresponds to the entry added in mutate.py: after changing `_refs_of`'s
    `raw` branch back to `return ()`, transitive-closure test (b) must fail
    -- this directly calls a "broken version" of the same logic, confirming
    the test genuinely has discriminating power (rather than both sides
    happening to pass)."""
    pool = SharePool("cse_")
    c_item = ("raw", "in1", 8, (), False)
    wire_c = pool.request(c_item)
    b_item = ("raw", f"{wire_c} + 8'd1", 8, (c_item,), False)
    pool.request(b_item)

    def broken_refs_of(key):
        return ()

    saved = pool._refs_of
    pool._refs_of = broken_refs_of
    try:
        keys = pool._live_keys([b_item])
    finally:
        pool._refs_of = saved
    names = {pool._names[k] for k in keys}
    check("mutation (_refs_of always returns empty): the transitive closure misses C, and the test can tell the difference",
          wire_c not in names, True)
def test_share_pool_raw_cycle_triggers_i5():
    """M6c Stage 0 acceptance: actually build a cycle and assert that
    `_check_invariants` (going through I5) really does raise
    `AssertionError` -- I5 has never actually blocked anything since M3a,
    so this cannot be settled just by reading the code.

    WARNING **a genuine A<->B mutual reference cannot be constructed through
    the legitimate `request()` flow**: `raw`'s key is content-addressed
    (`_normalize` folds `refs` into the key itself); for A's key to contain
    "references B", B's key must already exist in full; and for B's key to
    also contain "references A", A's key would in turn need to already exist
    in full -- this is an unsolvable chicken-and-egg problem, and Python's
    immutable tuples cannot build a structure that is both genuinely
    self-referential and hashable (`hash()` would recurse forever). This is
    actually a benefit of the content-addressing design: the legitimate path
    can never produce a cycle by construction.

    The situation that would genuinely go wrong is if `_refs_of` itself has a
    bug (for example, if someone in the future changes it from "read the
    key's own refs field directly" to "look up a side table keyed by wire
    name", which is the kind of change that could accidentally produce a
    real cycle) and reports a cycle -- here, the pool's `_refs_of` is
    monkeypatched directly into a version that reports a cycle (simulating
    that kind of bug), confirming that the same graph-construction code that
    `main._check_invariants` relies on, plus `_assert_no_cycle`, really does
    `raise AssertionError` when fed such a graph (not just reading
    `_assert_no_cycle`'s source and inferring that it "should" block it).

    WARNING this deliberately does **not** try to make `cyc_a`/`cyc_b`'s
    `refs` fields genuinely nest inside each other (an earlier version tried
    this, and as soon as `_live_keys` walked it, it turned out that what
    `cyc_a[3]` actually stored was the cycle-free leaf node `key_b`, not
    `cyc_b` -- because for `cyc_a`'s key content to literally contain
    `cyc_b`, `cyc_b` would have to already exist in full, and for `cyc_b` to
    contain `cyc_a`, `cyc_a` would have to already exist in full first, an
    unsolvable chicken-and-egg problem)."""
    pool = SharePool("cse_")
    name_a = pool.request(("raw", "ea", 8, (), False))
    name_b = pool.request(("raw", "eb", 8, (), False))
    key_a = pool._normalize(("raw", "ea", 8, (), False))
    key_b = pool._normalize(("raw", "eb", 8, (), False))

    def cyclic_refs_of(key):
        return {key_a: (key_b,), key_b: (key_a,)}.get(key, ())

    pool._refs_of = cyclic_refs_of

    live_keys = pool._live_keys([("raw", "ea", 8, (), False)])
    check("building the cycle: both mutually-referencing keys are in the transitive closure", live_keys, {key_a, key_b})
    graph = {pool._names[k]: {pool._names[j] for j in pool._refs_of(k) if j in pool._names}
             for k in live_keys}
    check("building the cycle: the graph forms a bidirectional edge", graph, {name_a: {name_b}, name_b: {name_a}})

    raised = False
    try:
        solver_main._assert_no_cycle(graph)
    except AssertionError:
        raised = True
    check("I5: _assert_no_cycle raises AssertionError when the pool reference graph has a real cycle", raised, True)
def test_compare_classify():
    """`classify()` must correctly determine the four REL types and C's valid range (including endpoints)."""
    ge = compare.classify([0, 5, 10, 20], [0, 0, 1, 1])
    check("GE: C falls in (maxZ, minO]", ge, ("GE", 6, 10))

    le = compare.classify([0, 5, 10, 20], [1, 1, 0, 0])
    check("LE: C falls in [maxO, minZ)", le, ("LE", 5, 9))

    eq = compare.classify([0, 5, 5, 10], [0, 1, 1, 0]),
    check("EQ: O is a single value", eq[0], ("EQ", 5, 5))

    ne = compare.classify([0, 5, 5, 10], [1, 0, 0, 1])
    check("NE: Z is a single value", ne, ("NE", 5, 5))

    check("Z/O either one empty -> None (a constant word, should not go through the compare family)",
          compare.classify([0, 0, 0], [0, 0, 0]), None)
    check("Z/O both non-single-valued and interleaved -> None (not separable)",
          compare.classify([0, 1, 0, 1], [0, 1, 1, 0]), None)
def test_compare_covers_support():
    """The support-coverage filter: the union of a candidate's primary-input
    support must be a superset of the word's own support, and this must
    still block even if all 512 vectors happen to agree (a structurally
    isomorphic version of test25 out20's `in2==0` trap)."""
    atom_support = {
        ("in1", False): frozenset({"in1[0]", "in1[1]"}),
        ("in2", False): frozenset({"in2[0]", "in2[1]"}),
    }
    word_sup = frozenset({"in1[0]", "in1[1]", "in2[0]", "in2[1]"})
    only_in1 = [("in1", False, 2, False, 1)]
    check("covering only half the support -> rejected",
          compare._covers_support(only_in1, atom_support, word_sup), False)
    both = [("in1", False, 2, False, 1), ("in2", False, 2, False, -1)]
    check("two atoms together cover the whole support -> accepted",
          compare._covers_support(both, atom_support, word_sup), True)
class _FakeValuesCtx:
    """`_values_subset_bool` only reads `val_results`/`verify_results`/
    `boundary_results`; here a minimal fake ctx is fed to it, with no need to
    build a real `compare.Context`."""
    def __init__(self, val_results, verify_results, boundary_results_out1):
        self.val_results = val_results
        self.verify_results = verify_results
        self.boundary_results = {"out1": boundary_results_out1}
def test_compare_values_subset_bool_needs_all_three_batches():
    """M26 Part 2: the value-range check must use **every available batch of
    vectors** -- looking only at the training set (`val_results`) can be
    fooled by a word whose third value only shows up in the boundary
    vectors, so `_values_subset_bool` must scan `val_results`/
    `verify_results`/`boundary_results` together; missing any one batch
    makes this test fail."""
    val = [{"out1": 0}, {"out1": 1}, {"out1": 0}]
    verify = [{"out1": 1}, {"out1": 0}]
    # the third value appears only in the boundary-vector batch (simulating the "1/512 probability" kind of case in `random_484`).
    # the third value: not hit once across 4600 random training vectors, only caught by the boundary vectors).
    boundary = [{"out1": 0}, {"out1": 2}]
    ctx_clean = _FakeValuesCtx(val, verify, [{"out1": 0}])
    ctx_dirty = _FakeValuesCtx(val, verify, boundary)
    check("when the value range is genuinely subset of {0,1} (a clean boundary batch) -> True",
          compare._values_subset_bool("out1", ctx_clean), True)
    check("the third value appears only in the boundary-vector batch, and it must still be caught -> False",
          compare._values_subset_bool("out1", ctx_dirty), False)
    ctx_dirty_verify = _FakeValuesCtx(val, [{"out1": 3}], [{"out1": 0}])
    check("the third value appears only in the independent verification batch, and it must still be caught -> False",
          compare._values_subset_bool("out1", ctx_dirty_verify), False)
def test_compare_best_rendering_isolates_cheapest_atom():
    """A structurally isomorphic version of test14: f = in1 - in2 - signed(in3),
    where the cheapest rendering puts in3 alone on one side
    (`signed(in3) < in1 - in2`, 2 points), rather than mechanically sorting
    by sign into `in1 REL (in2+signed(in3)+C)`, which would pay for an extra
    `+`."""
    terms = [("in1", False, 8, False, 1), ("in2", False, 8, False, -1),
             ("in3", True, 9, False, -1)]
    port_widths = {"in1": 8, "in2": 8, "in3": 9}
    rendering = compare._best_rendering(terms, "GE", 1, 10, port_widths)
    check("isolating a single atom, the cheapest rendering has cost == 2", rendering[0], 2)
def test_compare_threshold_literal():
    """Comparison threshold constants: plain decimal is used (free) when the
    value falls within the 32-bit signed safe zone; only outside that range
    does it fall back to `W'sd<magnitude>` (with a leading unary minus if
    needed)."""
    check("a small positive value -> plain decimal", compare._threshold_literal(7, 40), "7")
    check("a small negative value -> plain decimal (with a minus sign, still a constant, not an operator)",
          compare._threshold_literal(-4, 40), "-4")
    big = 1 << 35
    check("a positive value outside the safe zone -> W'sd", compare._threshold_literal(big, 40),
          f"40'sd{big}")
    check("a negative value outside the safe zone -> a unary minus followed by W'sd", compare._threshold_literal(-big, 40),
          f"-40'sd{big}")
def test_compare_direct_seed_hits_target_exactly():
    """A pure primary-input atom: `_direct_seed` must compute the exact port
    value that makes f hit the target (coefficients are only ±1, so in
    theory this is a one-step solution), without relying on random
    hill-climbing."""
    terms = [("in1", False, 20, False, 1)]
    zero = {"in1": 0}
    seed = compare._direct_seed(terms, 500037, zero)
    check("a single unsigned atom hits the target in one step", seed, {"in1": 500037})

    terms2 = [("in1", True, 8, False, -1)]  # f = -signed(in1), the target requires in1 to reach -100
    seed2 = compare._direct_seed(terms2, 100, {"in1": 0})
    val = seed2["in1"]
    signed_val = val - 256 if val >= 128 else val
    check("a negative-coefficient signed atom hits the target in one step (f = -signed(in1) == 100)",
          -signed_val, 100)
def _write_tmp_netlist(name, text):
    path = os.path.join("/tmp", f"selftest_compare_{name}.v")
    with open(path, "w") as f:
        f.write(text)
    return path
_DECL_ORDER_NET = """\
module top(in1, outA, outB);
  input [3:0] in1;
  output outA;
  output outB;
  buf u1(outA, in1[0]);
  buf u2(outB, in1[1]);
endmodule
"""
_CONE_GUARD_NET = """\
module top(in1, outEarly, outLate);
  input [3:0] in1;
  output outEarly;
  output outLate;
  buf u1(outLate, in1[0]);
  buf u2(outEarly, outLate);
endmodule
"""
def test_compare_atom_pool_decl_order_guard():
    """Rule 1 (declaration-order cycle guard, `compare.py`'s
    `decl_index[q] >= decl_index[name]`): a word's atom pool must not
    contain an output port whose declaration order is after itself
    (including itself) -- this is a **constructive** cycle guard, not
    reliant on cone analysis. test25's out3/out4 fitting each other as each
    other's complement, forming a 2-cycle, is exactly the instance that
    cone analysis alone cannot block and only declaration order can. With
    this rule removed, it is completely inert on the existing 30+12 cases
    (rtlcheck still passes fully, output is byte-identical), so it must be
    pinned down with a direct assertion."""
    path = _write_tmp_netlist("decl_order", _DECL_ORDER_NET)
    nl = Netlist(path)
    ctx = compare.build_context(nl, {}, [], [], [], [])

    ref_ports_a = {port for port, _s, _w, is_out in ctx.atoms["outA"] if is_out}
    check("outA's atom pool does not contain outB, which is declared later", "outB" in ref_ports_a, False)
    check("outA's atom pool does not contain itself (a self-reference degenerate case)", "outA" in ref_ports_a, False)

    ref_ports_b = {port for port, _s, _w, is_out in ctx.atoms["outB"] if is_out}
    check("outB's atom pool contains outA, declared earlier with no structural dependency", "outA" in ref_ports_b, True)
    check("outB's atom pool does not contain itself", "outB" in ref_ports_b, False)
def test_compare_atom_pool_cone_guard():
    """Rule 2 (cone check, `compare.py`'s `my_bits & cone_q`): even when
    declaration order comes first (passing rule 1), if the referrer's bit
    falls within the referenced item's cone, the reference must still be
    disallowed -- the direction is "A's bit is inside B's cone -> A cannot
    reference B", not the reverse. With this rule removed, it is likewise
    completely inert on the existing corpus, so it must be pinned down with
    a direct assertion."""
    path = _write_tmp_netlist("cone_guard", _CONE_GUARD_NET)
    nl = Netlist(path)
    ctx = compare.build_context(nl, {}, [], [], [], [])

    ref_ports_late = {port for port, _s, _w, is_out in ctx.atoms["outLate"] if is_out}
    check("outLate's bit falls within outEarly's cone -> outEarly is excluded (even though declared earlier)",
          "outEarly" in ref_ports_late, False)
def test_compare_refine_rejects_when_not_converged():
    """When threshold refinement (`_refine`) exhausts its budget without
    converging to width 1, it must return `None` (so the caller rejects the
    candidate and falls back), never forcing in a guessed value --
    changing `compare.py:385`'s `return lo if lo == hi else None` to an
    unconditional `return lo` still leaves rtlcheck fully passing on the
    existing 30+12 cases (because every candidate in the existing corpus
    happens to converge anyway); violating this guard does not produce
    inequivalent output, only a "got lucky" fluke, so it must be pinned
    down with a direct assertion.

    Construction: the atom is an output atom that `ctx.atom_support` cannot
    find a matching entry for, so `_mutation_bits` returns an empty set,
    `_hillclimb` gives up on its very first round (returning
    `(None, None)`), and the gap can never converge -- there's no need to
    actually run `nl.simulate()`; passing `nl` as `None` is enough (this
    path is never reached)."""
    ctx = compare.Context(
        atoms={}, atom_support={}, word_support={}, port_widths={},
        provider_width=8, val_vectors=[], val_results=[], verify_vectors=[],
        verify_results=[], boundary_vectors={}, boundary_results={},
        weights_by_word={}, rng=__import__("random").Random(0), zero_vec={},
        derived_atoms={}, derived_val_results={}, derived_verify_results={},
    )
    terms = [("ghost_out", False, 8, True, 1)]  # atom_support has no entry -> no controllable bit
    result = compare._refine(None, "dummy", terms, "GE", 10, 20, ctx, [0, 30], [0, 1])
    check("when refinement cannot converge (the gap stays stuck at [10,20]) it returns None instead of guessing a value", result, None)
def test_compare_rng_seed_is_fixed_constant():
    """`compare.RNG_SEED` must be a hard-coded constant, not a value decided at
    runtime (e.g. `random.randrange(...)`) -- which candidates `_hillclimb`
    explores and whether refinement converges all grow out of this seed.

    WARNING this deliberately avoids the indirect approach of "run the solver
    twice and diff the output": measured in practice (swapping RNG_SEED for
    `random.randrange(1<<30)` and running test17/test18/test25/synth10/
    synth11/synth12 twice each on a scratchpad copy) -- **the outputs were
    all byte-identical**, because for the current corpus every candidate's C
    converges to, and every winning candidate is, the unique correct answer
    regardless of seed -- only the exploration path differs. Output diffing
    like `test_output_is_deterministic` cannot catch this mutation. Only
    directly asserting the constant itself, and asserting that
    `build_context` really does derive `ctx.rng` from this constant (rather
    than drawing a fresh random seed on every call), can catch it."""
    check("RNG_SEED is a hard-coded constant (not a runtime random value like random.randrange)",
          compare.RNG_SEED, 20260813)

    path = _write_tmp_netlist("decl_order", _DECL_ORDER_NET)
    nl = Netlist(path)
    ctx1 = compare.build_context(nl, {}, [], [], [], [])
    ctx2 = compare.build_context(nl, {}, [], [], [], [])
    seq1 = [ctx1.rng.random() for _ in range(20)]
    seq2 = [ctx2.rng.random() for _ in range(20)]
    check("build_context's ctx.rng sequence is consistent across two calls (it really does derive from RNG_SEED,"
          "not re-drawing a seed each time)", seq1, seq2)
class _FakeCompareNL:
    """`_decision_boundary_vectors` only needs `nl.inputs` and `nl.simulate()`
    -- there's no need to actually assemble a gate-level netlist; feeding it
    a plain Python function as ground truth is enough. Same spirit as
    `test_compare_refine_rejects_when_not_converged` using `nl=None` to
    bypass the simulation path (here it's the reverse: simulation is really
    needed, so a minimal viable fake object is supplied)."""

    def __init__(self, inputs, fn):
        self.inputs = inputs
        self._fn = fn

    def simulate(self, vecs):
        return [self._fn(v) for v in vecs]
def _random450_shaped_ctx():
    inputs = [("in2", 7), ("in3", 1), ("in4", 4)]
    port_widths = dict(inputs)
    zero_vec = {n: 0 for n, _w in inputs}
    ctx = compare.Context(
        atoms={}, atom_support={}, word_support={}, port_widths=port_widths,
        provider_width=8, val_vectors=[], val_results=[], verify_vectors=[],
        verify_results=[], boundary_vectors={}, boundary_results={},
        weights_by_word={}, rng=random.Random(0), zero_vec=zero_vec,
        derived_atoms={}, derived_val_results={}, derived_verify_results={},
    )
    return inputs, ctx
def test_compare_decision_boundary_rejects_random450_shaped_model():
    """Milestone 20's direct counterexample: the `random_450` shape -- the true
    value is `in2+in4>=128` (`in3` does not functionally affect the output,
    structurally isomorphic to the 1-bit `in3` saturation trap in
    `$signed(in4+in2)<$signed(in3)`); the wrong model `in2+in3+in4>=129` can
    be entirely correct on the 512 `val_vectors`/`verify_vectors` (the
    decision boundary occupies only 0.732% of the full input space, so
    ordinary random sampling almost never hits it), yet the decision-
    boundary vectors must catch it.

    What is asserted is **the actual mismatch count > 0**, not just "there
    was a rejection" -- to avoid `_decision_boundary_vectors` degenerating
    into an inert guard that always returns an empty list and still "looks
    like" a rejection (see the "inert guard" lesson in `README`/
    `CLAUDE.md`).

    What removing this turns red: stubbing `_decision_boundary_vectors` to
    always return `[]` (simulating "the construction method fails
    entirely"), `bvecs` becomes empty, `mismatches` becomes 0, and this
    assertion turns from PASS to FAIL (measured in practice, see this
    milestone's report)."""
    inputs, ctx = _random450_shaped_ctx()

    def true_fn(vec):
        return {"out1": 1 if vec["in2"] + vec["in4"] >= 128 else 0}

    nl = _FakeCompareNL(inputs, true_fn)
    wrong_terms = [("in2", False, 7, False, 1), ("in3", False, 1, False, 1),
                   ("in4", False, 4, False, 1)]
    bvecs = compare._decision_boundary_vectors(nl, ctx, wrong_terms, 129)
    mismatches = sum(
        1 for v, r in bvecs
        if compare._apply_rel("GE", compare.eval_f(wrong_terms, v, r), 129) != bool(r["out1"])
    )
    check(f"the wrong model `in2+in3+in4>=129` of the random_450 shape, on the decision-boundary vectors"
          f"({len(bvecs)} total) caught a mismatch", mismatches > 0, True)
def test_compare_decision_boundary_accepts_correct_model():
    """Control group: the correct model `in2+in4>=128` (the same
    `_random450_shaped_ctx`) must pass decision-boundary verification --
    otherwise the "reject" assertion would stay green even if the new gate
    always rejected everything, becoming a false-positive guard that tests
    nothing.

    WARNING the first version of milestone 20 once treated `MIN_BOUNDARY`
    inside `try_compare` as a **production** rejection clause (rejecting a
    candidate outright if not enough vectors could be generated) -- after
    shipping, this turned out to wrongly kill correct candidates with a
    "single output atom" shape (test17/test25's `$signed(out1)<=-5` can only
    generate 4~10 vectors, which is not itself wrong, yet got blocked by the
    quantity threshold). After the fix, `MIN_BOUNDARY` is used only at the
    test/evidence layer as "the minimum generation count for a known shape
    (the correct-answer shape of random_450, `in2+in4>=128`)"; `try_compare`
    no longer reads it -- this coverage assertion tests "whether the
    construction method itself has degraded", not "whether a candidate would
    be rejected in production".

    What removing each part turns red:
    - the `mismatches == 0` assertion: replacing `_apply_rel` with the
      mirrored relation (misusing `"GE"` as `"LE"`) turns this assertion
      from PASS to FAIL (measured in practice, see this milestone's report).
    - the coverage assertion: temporarily setting `compare.MIN_BOUNDARY` far
      above the actually-measured generation count (e.g. 10000) turns this
      assertion from PASS to FAIL (measured in practice, see this
      milestone's report) -- this is exactly what it guards: if
      `_decision_boundary_vectors` is later weakened so it can no longer
      generate enough vectors, this must light up, and must not be masked by
      `try_compare` silently letting it through."""
    inputs, ctx = _random450_shaped_ctx()

    def true_fn(vec):
        return {"out1": 1 if vec["in2"] + vec["in4"] >= 128 else 0}

    nl = _FakeCompareNL(inputs, true_fn)
    correct_terms = [("in2", False, 7, False, 1), ("in4", False, 4, False, 1)]
    bvecs = compare._decision_boundary_vectors(nl, ctx, correct_terms, 128)
    mismatches = sum(
        1 for v, r in bvecs
        if compare._apply_rel("GE", compare.eval_f(correct_terms, v, r), 128) != bool(r["out1"])
    )
    check(f"the correct model `in2+in4>=128`, on the decision-boundary vectors ({len(bvecs)} total)"
          f"zero mismatches (must not be wrongly rejected by the new gate)", mismatches, 0)
    check("the number of boundary vectors generated for the correct-answer shape of random_450 is >= MIN_BOUNDARY"
          "(coverage assertion: a red light must fire when the construction degenerates into a no-op or produces only a handful of vectors,"
          "not a production-path rejection clause)", len(bvecs) >= compare.MIN_BOUNDARY, True)
def test_compare_try_compare_rejects_wrong_model_end_to_end():
    """End-to-end integration test (cold-review gap fill): the two tests above
    both call `compare._decision_boundary_vectors(...)` directly (bypassing
    the real netlist with `_FakeCompareNL`), which tests the helper function
    itself -- **no test actually exercises `try_compare`**. If someone
    deleted the entire decision-boundary-verification wiring in
    `solver/compare.py` (from `bvecs = _decision_boundary_vectors(...)` to
    `if not ok: continue`), the two tests above would still be fully green,
    and `solverbench` on the 30 existing cases would not catch it either
    (the `random_450` bug was only found through random scanning in the
    first place and is not in the corpus) -- this is the gap where "the
    evidence is kept, but whether the wiring got unplugged is never
    asserted". This test goes through the full `compare.try_compare(...)`
    without bypassing any part of it.

    Builds the `random_450` shape live with `refgen.Builder`:
    `out1 = $signed(in4+in2) < $signed(in3)` (`in3` is 1-bit). `Builder.add`
    returns `max(width) + 1` bits (including carry) for two operands -- this
    "one extra carry bit" of width is not a coincidence but the key: as long
    as `in2+in4`'s sum stays below this width's sign-flip point (i.e. the
    sum never gets large enough for a second flip to occur near it), `in3`'s
    two values (signed 0/-1) never change the comparison result over the
    entire reachable range -- so although `in3` is in the **static** fan-in,
    it is a trap that does not affect the output **functionally**;
    `_covers_support` (see the "support coverage filter" docstring in
    `compare.py`) therefore filters out the true answer `in2+in4>=2^20`,
    forcing the search to consider only candidates that reference `in3` --
    and every one of those candidates is wrong near the decision boundary,
    which must be blocked by the gate fixed in this round.

    WARNING the widths are deliberately chosen as `in2=20 bit`/`in4=12 bit`
    (rather than copying the real random_450's `7/4 bit`): measured in
    practice, rebuilding this shape with the real `7/4 bit` widths caused
    one of the 557 training vectors generated by
    `main.gen_validation_vectors` to happen to land exactly on the decision
    boundary when `in3=1`, causing `classify()` to return `None` directly at
    the **classification stage** (`f=128` maps to both `y=0` and `y=1`) --
    the candidate never even survives the 557-sample verification, so the
    decision-boundary gate is never called at all, and a test written this
    way has no discriminating power for "whether the wiring got unplugged"
    (the result is `None` whether or not the wiring is removed, which is
    exactly the gap this round needs to fill). After widening (so the
    decision boundary occupies a smaller fraction, and the 557 vectors from
    `main.VALIDATION_SEED`'s fixed seed no longer happen to hit it),
    measurement confirmed: the candidate first passes the existing 557-sample
    verification (`ok=True`), and is only blocked by the decision-boundary
    gate -- verified in practice by removing the wiring (see "what removing
    each part turns red" below) that without this gate the candidate really
    would be accepted by `try_compare` and returned as an inequivalent
    expression.

    WARNING `Builder.slt()`/`ult()` return a **single wire name (str)**, not
    a list -- do not write `[0]` to index it, that would take the string's
    first character and create an undeclared wire.

    The control group cannot be skipped: the same test also includes a pure
    linear shape that **should** be solved, `in1+in2>=10` (no support trap,
    the only candidate is the correct answer), asserting that `try_compare`
    does not return `None` for it -- otherwise the "returns None" assertion
    would stay green even if `try_compare` were completely broken (e.g.
    always `return None`).

    What removing each part turns red: deleting the entire decision-boundary
    verification section in `solver/compare.py` (from `bvecs =
    _decision_boundary_vectors(...)` to `if not ok: continue`) turns this
    test from PASS to FAIL -- `try_compare` no longer returns `None` for the
    random_450 shape, instead returning a wrong expression that references
    `in3` (e.g. `cse_a0 >= 1048577`, where `cse_a0` is `in2+in3+in4`);
    measured in practice, see this milestone's report; after deletion,
    confirmed it turned red, then restored and confirmed byte-identical
    with `diff`."""
    # --- random_450 shape: out1 = $signed(in4+in2) < $signed(in3) ---
    # see the WARNING section in the docstring above for the widths: 20/12 bit is a deliberate choice, not copied from the real
    # random_450's 7/4 bit.
    b = Builder()
    b.add_input("in2", 20)
    b.add_input("in3", 1)
    b.add_input("in4", 12)
    total = b.add(b.port("in4"), b.port("in2"))  # 21 bits (including carry), see docstring
    lt = b.slt(total, b.port("in3"))             # a single wire name (str), not a list
    b.set_output("out1", 1, [lt])
    path_wrong = _write_tmp_netlist("random450_shape_e2e", b.emit("top"))
    nl_wrong = Netlist(path_wrong)

    val_vectors = solver_main.gen_validation_vectors(nl_wrong)
    val_results = nl_wrong.simulate(val_vectors)
    verify_vectors = solver_main.gen_validation_vectors(
        nl_wrong, seed=solver_main.VALIDATION_SEED + 1)
    verify_results = nl_wrong.simulate(verify_vectors)
    ctx_wrong = compare.build_context(nl_wrong, {}, val_vectors, val_results,
                                       verify_vectors, verify_results)
    result_wrong = compare.try_compare(nl_wrong, "out1", 1, ctx_wrong, SharePool("cse_"))
    check("the random_450 shape goes through try_compare end-to-end -> falls back (returns None)",
          result_wrong is None, True)

    # --- control group: purely linear in1+in2>=10, no support trap ---
    b2 = Builder()
    b2.add_input("in1", 4)
    b2.add_input("in2", 4)
    total2 = b2.add(b2.port("in1"), b2.port("in2"))       # 5 bits
    ge = b2.NOT(b2.ult(total2, b2.const_val(10, 5)))       # a single wire name (str)
    b2.set_output("out1", 1, [ge])
    path_ok = _write_tmp_netlist("linear_ge_control_e2e", b2.emit("top"))
    nl_ok = Netlist(path_ok)

    val_vectors2 = solver_main.gen_validation_vectors(nl_ok)
    val_results2 = nl_ok.simulate(val_vectors2)
    verify_vectors2 = solver_main.gen_validation_vectors(
        nl_ok, seed=solver_main.VALIDATION_SEED + 1)
    verify_results2 = nl_ok.simulate(verify_vectors2)
    ctx_ok = compare.build_context(nl_ok, {}, val_vectors2, val_results2,
                                    verify_vectors2, verify_results2)
    result_ok = compare.try_compare(nl_ok, "out1", 1, ctx_ok, SharePool("cse_"))
    check("control group (purely linear in1+in2>=10, no support trap)"
          "try_compare must solve it (not None)", result_ok is not None, True)
_DET_SEEDS = tuple((os.environ.get("SELFTEST_DET_SEEDS") or "1,2,3,7,42").split(","))
_DET_CASES = os.environ.get("SELFTEST_DET_CASES")
def _det_skip(label):
    """Returning True means this case was excluded by `SELFTEST_DET_CASES`."""
    return _DET_CASES is not None and label not in _DET_CASES.split(",")
def _check_deterministic_across_hashseeds(label, net):
    if _det_skip(label):
        print(f"  SKIP determinism test {label} (SELFTEST_DET_CASES deliberately shrunk)")
        return
    outs = []
    for seed in _DET_SEEDS:
        dst = os.path.join("/tmp", f"selftest_det_{label}_{seed}.v")
        env = dict(os.environ, PYTHONHASHSEED=seed)
        subprocess.run([sys.executable, os.path.join(ROOT, "solver", "main.py"),
                        "-input", net, "-output", dst],
                       check=True, env=env, capture_output=True)
        outs.append(open(dst).read())
    check(f"{label} is byte-identical across {len(_DET_SEEDS)} PYTHONHASHSEED values",
          len(set(outs)), 1)
def test_output_is_deterministic():
    """The same input must produce byte-identical RTL across different
    PYTHONHASHSEED values.

    A set's iteration order propagates into an expression's term order, and
    term order affects cost.

    test12's sole output is a 26-bit word whose value range is far larger
    than {0,1}, so `compare.try_compare`'s `_values_subset_bool` immediately
    returns False (since M26 this guard is a value-range check, not a width
    check, see `solver/compare.py`) -- no code path in the compare family is
    ever executed by this test.
    test18 is added: its out2/out4 go through `_refine`/`_hillclimb`
    (involving output atoms, not a pure primary-input direct solve), filling
    in determinism coverage for the compare family under PYTHONHASHSEED
    changes (e.g. whether `_mutation_bits`/`enumerate_terms` accidentally
    missed a spot of set iteration).

    WARNING this cannot catch "whether RNG_SEED itself is a hard-coded
    constant" -- see `test_compare_rng_seed_is_fixed_constant`'s note on
    what was actually measured: changing PYTHONHASHSEED does not cause the
    value drawn from `random.randrange(1<<30)` to change twice (that value
    is computed only once within the same process), and even though
    different processes get different random seeds, with the current corpus
    the candidate/threshold each ultimately converges to is the unique
    correct answer regardless of the seed -- so this indirect approach is
    blunt for that mutation, and the direct assertion above is required
    instead. test18 is added here purely to fill in hashseed-determinism
    coverage for the compare family, which was previously never exercised at
    all."""
    net = os.path.join(ROOT, "testcase", "test12", "top_primitive.v")
    if not os.path.exists(net):
        print("  SKIP determinism test (testcase/test12 does not exist)")
        return
    _check_deterministic_across_hashseeds("test12", net)

    net18 = os.path.join(ROOT, "testcase", "test18", "top_primitive.v")
    if not os.path.exists(net18):
        print("  SKIP determinism test (testcase/test18 does not exist)")
        return
    _check_deterministic_across_hashseeds("test18", net18)

    # M6c: `(undisclosed family)` is currently the largest family module, and `_fit_channel_h`/`_detect_group`
    # both pick the hub via `set` iteration -- cold review (M6c) pointed out `_fit_channel_h` once missed
    # leaf-adjacency check: under a triangular topology, PYTHONHASHSEED determines which hub gets picked.
    # WARNING test26 alone takes about 95 seconds to solve x two hashseeds, making it the slowest single item in this test,
    # but it is the only corpus item that exercises (undisclosed family); without it there would be no hashseed-determinism coverage at all.
    net26 = os.path.join(ROOT, "testcase", "test26", "top_primitive.v")
    if not os.path.exists(net26):
        print("  SKIP determinism test (testcase/test26 does not exist)")
        return
    _check_deterministic_across_hashseeds("test26", net26)

    # M8 Stage 0 caught this M7 defect: `(undisclosed family)._quad_subplan` and `(undisclosed family).py`'s
    # `sub_ports` both pass `word_ports` as a `set`, and `quadratic.fit_word` directly
    # `for p in ports:` iterates over it -- the insertion order of the two dicts `c`/`d` determines render_word's
    # the printed item order, so the output varies with `PYTHONHASHSEED` (cost and length are both the same,
    # only the order differs, so none of rtlcheck / cost / reduction can see it).
    # WARNING **neither round of independent code review caught this**, this was the acceptance criterion of M8 Stage 0's
    # only surfaces as a red light between two consecutive runs -- at the time this test only covered test12/test18/test26,
    # none of the three would execute (undisclosed family) or (undisclosed family). **The instrument is fine, what's missing is the input.**
    # M8 Stage 1: `(undisclosed family).py` is currently the only heavyweight corpus for this family (215 -> 92, extracting
    # two blocks of 3 terms each), and `candidate_sets = sorted({frozenset(v) for v in
    # occ.values() ...}, key=...)` -- this kind of set-of-frozenset traversal is exactly the M7
    # the same shell as that defect -- added here to fill in hashseed-determinism coverage for this family.
    for tag in ("test27", "test15", "test29", "test19"):
        netx = os.path.join(ROOT, "testcase", tag, "top_primitive.v")
        if not os.path.exists(netx):
            print(f"  SKIP determinism test (testcase/{tag} does not exist)")
            continue
        _check_deterministic_across_hashseeds(tag, netx)
class _FakeNL:
    """Feeds only the two interfaces `mux._find_plan` needs: `inputs` (used for
    zero_vec) and `simulate(vectors)` (used for probing). Substituting a
    hand-computed `formula` for real gate-level simulation makes it possible
    to construct "shape mismatch" malformed scenarios directly, without
    having to build a gate-level circuit from scratch for every boundary
    case."""

    def __init__(self, inputs, formula):
        self.inputs = inputs
        self._formula = formula

    def simulate(self, vectors):
        return [{"out": self._formula(v)} for v in vectors]
def _smul_bits(b, A, B):
    """Signed (two's-complement) multiplication: the same construction as
    `tools/gensynth.py::smul` (an unsigned array multiplier on |A|*|B|,
    followed by sign correction) -- duplicated independently here, since
    selftest does not depend on gensynth, keeping module boundaries clean."""
    wa, wb = len(A), len(B)
    sa, sb = A[-1], B[-1]
    mag_a = b.mux_bus(sa, b.neg(A), A)
    mag_b = b.mux_bus(sb, b.neg(B), B)
    prod = b.mul(mag_a, mag_b)[: wa + wb]
    sign = b.XOR(sa, sb)
    return b.mux_bus(sign, b.neg(prod), prod)[: wa + wb]
def test_mux_signed_product_rejected():
    """Gap 1: `mux.py` treats product factors as unsigned throughout
    (`render_expr`/`_formula` never produce `$signed()`). This constructs a
    genuine synthesized signed-product netlist (`|a|*|b|` unsigned array
    multiplier + sign correction, structurally the same as
    `tools/gensynth.py::smul`), guard-muxed to "enabled only when ctrl==0",
    with the output width deliberately truncated narrower than `wa+wb`
    (`out_w=5 < wa+wb=8` but `> min(wa,wb)=4`) -- this is the key: when
    `out_w<=min(wa,wb)`, the low bits of a signed and an unsigned product
    are mathematically identical (truncation eats all the difference terms
    between them), which is not what's being tested here; only when `out_w`
    falls in the window `(min(wa,wb), wa+wb)` does the output actually
    diverge between signed and unsigned (exhaustively enumerating all 512
    `(a,b,ctrl)` combinations for wa=wb=4, out_w=5, 96 of them are genuinely
    unequal -- see the exploration record from when the patch was made).

    Structural probing (steps 1-3) looks only at zero/nonzero patterns, not
    signedness, and still groups correctly and locates the guard correctly;
    the only thing that can reject this candidate is the formula comparison
    in `try_mux`'s acceptance stage.

    WARNING an honest note: exhaustively enumerating multiple (wa, wb,
    out_w, kind) combinations for this specific construction found that the
    fixed seed `main.gen_validation_vectors` uses (`VALIDATION_SEED
    =20260812`) together with 512+512 training/verification vectors *was
    already* stably rejecting this candidate *before* boundary vectors were
    added (the measured divergence probability between signed/unsigned falls
    between 25% and 75%, so 512+512 vectors are almost certain not to miss
    it entirely). This does not match the original assumption that "random
    vectors are unreliable, purely down to luck". This test was added
    *after* boundary vectors went live, and verifies "this guard behaves as
    expected", not "adding it turned an unstable case stable". Adding
    boundary vectors is still a reasonable strengthening on its own: it
    converges this window's rejection from "almost certain probabilistically"
    to "certain structurally", without changing the output of any existing
    case."""
    wa = wb = 4
    out_w = 5  # min(wa, wb) < out_w < wa + wb: the window where signed/unsigned genuinely diverge.
    b = Builder()
    b.add_input("a", wa)
    b.add_input("b", wb)
    b.add_input("ctrl", 1)
    prod = _smul_bits(b, b.port("a"), b.port("b"))
    zero_bits = [0] * (wa + wb)
    out_bits = b.mux_bus(b.port("ctrl")[0], zero_bits, prod)  # kind A: enabled when ctrl==0
    b.set_output("out", out_w, out_bits)
    path = _write_tmp_netlist("mux_signed_product", b.emit("top"))

    nl = Netlist(path)
    val_vectors = solver_main.gen_validation_vectors(nl)
    val_results = nl.simulate(val_vectors)
    verify_vectors = solver_main.gen_validation_vectors(nl, seed=solver_main.VALIDATION_SEED + 1)
    verify_results = nl.simulate(verify_vectors)
    ctx = mux.build_context(nl, val_vectors, val_results, verify_vectors, verify_results)
    check("structural probing still groups correctly (black-box toggle probing looks only at zero/nonzero patterns, independent of signedness)",
          ctx.plans["out"] is not None, True)
    check("verification must reject this signed-product candidate (the unsigned formula disagrees with the true value inside the truncation window)",
          mux.try_mux(nl, "out", out_w, ctx, pool=None), None)
def test_mux_pair_dedup_rejected():
    """Gap 2: nothing pins down `mux._find_plan` step 2's
    `if p in used or q in used: continue` (a factor that's already paired
    must not be reused). Construction: `zero_alone = {a,b,c,d}`, the formula
    only has `+= a*b when ctrl1==0` and `+= a*c when ctrl2==0` -- `d` never
    appears at all, a purely unpaired candidate; probing both `(a,b)` and
    `(a,c)` shows the "valid product pair" signal (because the formula
    genuinely has a term for each), but both share the factor `a`.

    Correct code: `cand_pairs` in enumeration order is
    (a,b),(a,c),(a,d),(b,c),(b,d),(c,d) -- `(a,b)` is taken first, `a` goes
    into `used`, and `(a,c)` is skipped by the dedup check (the `continue`
    happens before "measuring v0/v1 to determine kind", so it is never
    misjudged as "not a pair" -- it is explicitly skipped). The other four
    combinations have a formula that is always 0 (some factor unrelated to
    them is 0), so they don't pair up either. The result is `pairs=[(a,b)]`,
    `2*len(pairs)=2 != len(zero_alone)=4` -> the completeness check fails ->
    `_find_plan` returns None (the whole word abandoned, correctly: `d`
    having no partner is a genuine shape mismatch).

    The mutated code with dedup removed: `(a,c)` is not skipped, and the
    measured signal is likewise "a valid product pair",
    `pairs=[(a,b),(a,c)]`, `2*2==4` sneaks past the completeness check --
    then step 3 uses two independent candidate guards (`ctrl1`, `ctrl2`)
    that each hit one group (not conflicting with each other, so they aren't
    blocked by the `mux_guard_exclusive` guard), producing a **non-None**
    `WordPlan` where `a` is counted into both product groups and `d`
    silently vanishes entirely -- this is exactly why the dedup check
    exists."""
    def formula(v):
        a, b_, c, d = v.get("a", 0), v.get("b", 0), v.get("c", 0), v.get("d", 0)
        ctrl1, ctrl2 = v.get("ctrl1", 0), v.get("ctrl2", 0)
        total = 0
        if ctrl1 == 0:
            total += a * b_
        if ctrl2 == 0:
            total += a * c
        return total

    nl = _FakeNL([("a", 2), ("b", 2), ("c", 2), ("d", 2), ("ctrl1", 1), ("ctrl2", 1)], formula)
    plan = mux._find_plan(nl, "out", 8, ["a", "b", "c", "d", "ctrl1", "ctrl2"], dict(nl.inputs))
    check("d is left unpaired; a is measured as an 'effective product pair' by both (a,b) and (a,c) ->"
          "the dedup check must block a's second use, failing the completeness check (plan is None)",
          plan, None)
_MUX_AMBIGUOUS_GUARD_NET = """\
module top(a, b, ctrl1, ctrl2, out);
  input [1:0] a, b;
  input ctrl1, ctrl2;
  output [3:0] out;
  and u1(pp00, a[0], b[0]);
  and u2(pp01, a[0], b[1]);
  and u3(pp10, a[1], b[0]);
  and u4(pp11, a[1], b[1]);
  xor u5(s1, pp01, pp10);
  and u6(cc1, pp01, pp10);
  xor u7(s2, pp11, cc1);
  and u8(cc2, pp11, cc1);
  nor u9(ctrl_n, ctrl1, ctrl2);
  and u10(out[0], pp00, ctrl_n);
  and u11(out[1], s1, ctrl_n);
  and u12(out[2], s2, ctrl_n);
  and u13(out[3], cc2, ctrl_n);
endmodule
"""
def test_mux_ambiguous_guard_rejected():
    """`if len(matched) != 1: return None` (`mux._find_plan` step 3): if a
    product group satisfies the zeroing condition for two or more candidate
    guards at once (in this example, raising either ctrl1 or ctrl2 alone
    zeroes it), then "which one is the real guard" cannot be uniquely
    determined, and the whole word must be abandoned -- it must not blindly
    pick the first one. This check is completely inert on test20/test30 --
    every product group in them happens to have exactly one guard that
    satisfies the condition."""
    path = _write_tmp_netlist("mux_ambiguous_guard", _MUX_AMBIGUOUS_GUARD_NET)
    nl = Netlist(path)
    vecs = [{"a": a, "b": b, "ctrl1": c1, "ctrl2": c2}
            for a in range(4) for b in range(4) for c1 in range(2) for c2 in range(2)]
    results = nl.simulate(vecs)
    ctx = mux.build_context(nl, vecs[:48], results[:48], vecs[48:], results[48:])
    check("two guards can each independently zero the same product group -> the whole word is abandoned (plan is None)",
          ctx.plans["out"], None)
    check("try_mux returns None directly once build_context gives up",
          mux.try_mux(nl, "out", 4, ctx, pool=None), None)
def test_mux_shared_guard_reused_rejected():
    """`if g in used_guards: return None` (`mux._find_plan` step 3): two
    product groups must not share the same physical guard port -- when
    `ctrl` zeroes both the (a1,b1) and (a2,b2) groups at once, the first
    group correctly pairs with `ctrl`, and the second group's candidate set
    also contains only `ctrl` (len(matched)==1, so it isn't blocked by the
    previous check), and only this "guard already used" exclusivity check
    can block it -- otherwise a candidate would be produced where "two
    groups share the same guard control", which violates the shape
    assumption (and may not even be equivalent)."""
    def formula(v):
        ctrl = v.get("ctrl", 0)
        a1, b1 = v.get("a1", 0), v.get("b1", 0)
        a2, b2 = v.get("a2", 0), v.get("b2", 0)
        return 0 if ctrl else (a1 * b1 + a2 * b2)

    nl = _FakeNL([("a1", 2), ("b1", 2), ("a2", 2), ("b2", 2), ("ctrl", 1)], formula)
    plan = mux._find_plan(nl, "out", 8, ["a1", "b1", "a2", "b2", "ctrl"], dict(nl.inputs))
    check("the same guard controls two product groups at once -> the second group's pairing is blocked by the exclusivity check (plan is None)",
          plan, None)
def test_mux_unpaired_factor_rejected():
    """`if 2 * len(pairs) != len(zero_alone) or not pairs: return None`
    (`mux._find_plan` step 2): if the candidate product-factor pool has an
    unpaired factor (`r` has no effect on the output at all; raising either
    guard state alone only probes 0, looking exactly like a real product
    factor, but it finds no partner), the whole word must be abandoned -- it
    must not be quietly dropped while patching together a candidate from
    only the successfully paired groups that "happens to fit the
    verification vectors"."""
    def formula(v):
        ctrl = v.get("ctrl", 0)
        a, b = v.get("a", 0), v.get("b", 0)
        return 0 if ctrl else (a * b)  # r never appears in the formula at all

    nl = _FakeNL([("a", 2), ("b", 2), ("r", 2), ("ctrl", 1)], formula)
    plan = mux._find_plan(nl, "out", 8, ["a", "b", "r", "ctrl"], dict(nl.inputs))
    check("the candidate product-factor pool has an unpaired r (no partner found) -> the whole word is abandoned (plan is None)",
          plan, None)
def test_mux_classify_mismatch_rejected():
    """Step 1 classification's `else: return None` (`mux._find_plan`): a
    candidate port `p` raised alone under the two states "all guards 0" and
    "all guards 1" -- if it gives two *different* nonzero values (in this
    example `p` contributes `p` at ctrl=0 and `2*p` at ctrl=1), it is
    neither a clean "product factor" (which should be 0) nor a clean "bare
    addend" (which should be the same in both states); the shape doesn't
    fit, and the whole word must be abandoned rather than casually folded
    into either category.

    Here, a genuinely clean, solvable product pair `(a, b)` (controlled by
    the same `ctrl`, using kind A) is deliberately included alongside it --
    if `p`'s mismatch were wrongly folded into the bare-addend category
    (instead of causing the whole word to be abandoned), the clean `(a, b)`
    pairing would still successfully assemble a `WordPlan` (just silently
    losing `p`'s real dependency on `ctrl`). Only when `p` alone has a
    mismatched shape but the rest of the candidates are cleanly solvable can
    the two reasons for abandoning -- "the whole word is abandoned" versus
    "abandoned merely for lack of candidates" -- be told apart: a previous
    version of this test (where neither `p` nor `q` formed any product pair)
    ends up back at None either way, whether this else branch returns None
    or wrongly folds into leftover, because of `len(zero_alone) < 2` --
    completely insensitive to this mutation (caught in practice by
    mutate.py: selftest was green and needed a fix)."""
    def formula(v):
        ctrl = v.get("ctrl", 0)
        a, b, p = v.get("a", 0), v.get("b", 0), v.get("p", 0)
        prod = 0 if ctrl else a * b       # a clean product group (kind A)
        ambiguous = p if ctrl == 0 else 2 * p  # p's shape does not fit: both states are nonzero and different
        return prod + ambiguous

    nl = _FakeNL([("a", 2), ("b", 2), ("p", 2), ("ctrl", 1)], formula)
    plan = mux._find_plan(nl, "out", 8, ["a", "b", "p", "ctrl"], dict(nl.inputs))
    check("the rest of the candidates are all cleanly solvable, but p's two states give different nonzero values -> the whole word must still be abandoned (plan is None)",
          plan, None)
def test_mux_verify_split_catches_wrong_kind():
    """`try_mux`'s acceptance loop must run both `ctx.val_vectors` *and*
    `ctx.verify_vectors` (echoing the compare family's "separate
    training/verification seeds" discipline): construct a `WordPlan` whose
    kind was guessed wrong (declared as kind "A", but the true value is kind
    "B"); `val_vectors` deliberately makes `a*b == 0` (both kind guesses
    coincidentally give the same answer on this vector set, masking the
    wrong guess), and only `verify_vectors` (where both `a` and `b` are
    nonzero) exposes the wrong kind. Checking only the val set would
    wrongly pass this candidate; only checking both sets catches it."""
    wrong_plan = mux.WordPlan(
        groups=(mux.Group(guard="ctrl", a="a", b="b", kind="A", active_val=0),),
        leftover=("c",),
    )
    width = 8

    def true_value(v):  # the true value: kind "B" (enabled when ctrl==1), the opposite of the declared kind "A"
        active = v.get("ctrl", 0) == 1
        return (v.get("c", 0) + (v.get("a", 0) * v.get("b", 0) if active else 0)) % (1 << width)

    val_vecs = [{"a": 0, "b": 0, "ctrl": 0, "c": 5}, {"a": 0, "b": 0, "ctrl": 1, "c": 3}]
    verify_vecs = [{"a": 2, "b": 3, "ctrl": 1, "c": 1}]
    val_rows = [{"out": true_value(v)} for v in val_vecs]
    verify_rows = [{"out": true_value(v)} for v in verify_vecs]

    check("the val set alone cannot tell the kind was guessed wrong (a*b is always 0, both kinds coincidentally agree)",
          all(mux._formula(wrong_plan, width, v) == r["out"] for v, r in zip(val_vecs, val_rows)),
          True)
    check("the verify set exposes the mis-guessed kind (a and b both nonzero, the two kinds give different answers)",
          mux._formula(wrong_plan, width, verify_vecs[0]) == verify_rows[0]["out"], False)

    ctx = mux.Context(plans={"out": wrong_plan}, val_vectors=val_vecs, val_results=val_rows,
                       verify_vectors=verify_vecs, verify_results=verify_rows)
    check("try_mux must run the verify set to reject this mis-guessed candidate (returns None)",
          mux.try_mux(None, "out", width, ctx, pool=None), None)
def test_mux_sign_boundary_defence_catches_signed_product():
    """Gap 1: the `_sign_boundary_vectors` guard itself must be protected by a
    red light -- measured in practice, changing its body to return `[]`
    still leaves `tools/selftest.py` fully green, because
    `test_mux_signed_product_rejected` runs through a whole real circuit
    where the signal probability falls in the mid-range; the existing
    512+512 fixed-seed random vectors are almost certain to hit the
    difference anyway, so it was already passing stably before boundary
    vectors were added, proving nothing about whether the new guard is
    necessary.

    This is changed to build a `Context` directly (not going through a real
    circuit), constructing the minimal scenario where "the val set and the
    verify set are deliberately restricted to mid-range values that never
    cross a signedness flip (`a,b < 8`, where signed==unsigned and both
    formulas coincidentally give the same answer), and only a boundary point
    can give it away": kind A, a single group `(a,b)`, where the true value
    is actually a signed product, but the declared `WordPlan` (same as
    throughout `mux.py`) is an unsigned formula. Both `val_vectors` and
    `verify_vectors` deliberately avoid the boundary, so the training/
    verification pair alone cannot block this mis-guessed candidate -- only
    `boundary_vectors`/`boundary_results` (this test **passes them in
    explicitly**, not relying on `Context`'s namedtuple default) can catch
    it."""
    def s(v, w):
        return v - (1 << w) if v >= (1 << (w - 1)) else v

    wa = wb = 4
    width = 5
    port_widths = {"a": 4, "b": 4, "ctrl": 1, "out": 5}
    zero_vec = {"a": 0, "b": 0, "ctrl": 0}
    plan = mux.WordPlan(
        groups=(mux.Group(guard="ctrl", a="a", b="b", kind="A", active_val=0),),
        leftover=(),
    )

    def true_out(v):  # the real circuit actually computes a signed product (kind A: enabled only when ctrl==0)
        if v.get("ctrl", 0) == 0:
            return (s(v.get("a", 0), wa) * s(v.get("b", 0), wb)) % (1 << width)
        return 0

    # a and b are both deliberately kept below 8 (signed == unsigned, never touching the signedness flip point): training/verification
    # the pair of sets alone gives no indication at all that this candidate was actually guessed wrong.
    val_vectors = [{"a": 3, "b": 4, "ctrl": 0}, {"a": 2, "b": 5, "ctrl": 0}]
    val_results = [{"out": true_out(v)} for v in val_vectors]
    verify_vectors = [{"a": 6, "b": 1, "ctrl": 0}]
    verify_results = [{"out": true_out(v)} for v in verify_vectors]
    check("the val/verify pair alone cannot tell the unsigned formula was actually guessed wrong (a,b<8, signed==unsigned)",
          all(mux._formula(plan, width, v) == r["out"]
              for v, r in zip(val_vectors + verify_vectors, val_results + verify_results)),
          True)

    boundary_vectors = mux._sign_boundary_vectors(zero_vec, port_widths, plan)
    boundary_results = [{"out": true_out(v)} for v in boundary_vectors]
    check("there really is a point in the boundary-vector set that exposes the unsigned formula",
          any(mux._formula(plan, width, v) != r["out"]
              for v, r in zip(boundary_vectors, boundary_results)),
          True)

    ctx = mux.Context(
        plans={"out": plan},
        val_vectors=val_vectors, val_results=val_results,
        verify_vectors=verify_vectors, verify_results=verify_results,
        boundary_vectors={"out": boundary_vectors},
        boundary_results={"out": boundary_results},
    )
    check("try_mux must run the boundary-vector set to reject this signed product mistakenly guessed unsigned",
          mux.try_mux(None, "out", width, ctx, pool=None), None)
def test_mux_boundary_vectors_must_probe_both_guard_states():
    """A gap caught in the third round of cold review: `_sign_boundary_vectors`
    was once changed to "only test the active guard state", on the reasoning
    that "the inactive branch inside `_formula` never evaluates a/b, so it
    has zero discriminating power for signedness" -- but that argument only
    proves that the Python-side model `_formula` itself is independent of
    a/b; it does not prove that the real circuit's inactive branch is also
    independent of a/b. The latter was only verified to be constant at a
    single probing corner in `_find_plan` step 1 (`p=full(p), q=full(q)`),
    and corners the probe never touches are not guaranteed to behave the
    same way.

    Here a ground-truth function is constructed where "the inactive branch
    is not actually constant, but happens to agree at the `full()` corner":
    kind A, single group `(a,b)`; when `ctrl==0` (active) the true value is
    exactly `a*b`, fully matching the declared formula; when `ctrl==1`
    (inactive) the true value is not the constant 0, but is 1 only when both
    `a,b` fall in `{7,8}` (the mid-range flip points of `_boundary_points(4)`),
    and is 0 everywhere else (including `a=b=15=full(4)`). `_find_plan`
    step 3 only probes the `full()` corner, so it misjudges this inactive
    branch as constant 0, and the shape check passes normally.
    `val_vectors`/`verify_vectors` deliberately avoid 7 and 8, so they cannot
    tell the true value is actually not constant. Only the point
    `guard=1, a=7, b=8` (the inactive state) inside `boundary_vectors` gives
    it away -- if boundary vectors only tested the active guard state, this
    point would never be generated at all, and the candidate would be
    wrongly accepted."""
    width = 8
    port_widths = {"a": 4, "b": 4, "ctrl": 1, "out": width}
    zero_vec = {"a": 0, "b": 0, "ctrl": 0}
    plan = mux.WordPlan(
        groups=(mux.Group(guard="ctrl", a="a", b="b", kind="A", active_val=0),),
        leftover=(),
    )

    def true_out(v):
        a, b, ctrl = v.get("a", 0), v.get("b", 0), v.get("ctrl", 0)
        if ctrl == 0:
            return (a * b) % (1 << width)
        return 1 if a in (7, 8) and b in (7, 8) else 0

    # the val/verify pair deliberately avoids 7 and 8: it cannot tell the inactive branch is actually not constant 0.
    val_vectors = [{"a": 3, "b": 4, "ctrl": 0}, {"a": 1, "b": 2, "ctrl": 1}]
    val_results = [{"out": true_out(v)} for v in val_vectors]
    verify_vectors = [{"a": 5, "b": 6, "ctrl": 0}, {"a": 2, "b": 3, "ctrl": 1}]
    verify_results = [{"out": true_out(v)} for v in verify_vectors]
    check("the val/verify pair alone cannot tell the inactive branch is actually not constant",
          all(mux._formula(plan, width, v) == r["out"]
              for v, r in zip(val_vectors + verify_vectors, val_results + verify_results)),
          True)

    boundary_vectors = mux._sign_boundary_vectors(zero_vec, port_widths, plan)
    boundary_results = [{"out": true_out(v)} for v in boundary_vectors]
    check("there really is a point in the boundary-vector set that exposes the formula in the inactive state",
          any(mux._formula(plan, width, v) != r["out"]
              for v, r in zip(boundary_vectors, boundary_results)),
          True)

    ctx = mux.Context(
        plans={"out": plan},
        val_vectors=val_vectors, val_results=val_results,
        verify_vectors=verify_vectors, verify_results=verify_results,
        boundary_vectors={"out": boundary_vectors},
        boundary_results={"out": boundary_results},
    )
    check("try_mux can only reject this candidate using the boundary vectors from the inactive guard state",
          mux.try_mux(None, "out", width, ctx, pool=None), None)
def _decide_with_family_call_counts(input_path):
    """Runs `decide()` once on a netlist, monkeypatching each registered
    family's `attempt` into a counter, returning `{kind: call count}`. After
    the M21 family registry went live, strings like
    `(undisclosed family).try_(undisclosed family)` no longer appear in
    `decide()`'s source (see the docstring of
    `test_(undisclosed family)_only_tried_when_candidates_empty`) -- a
    text-based assertion would silently break after the refactor, so this is
    replaced with directly counting how many times `attempt` is actually
    called, a behavioral assertion strictly stronger than the original
    text-based one."""
    nl = Netlist(input_path)
    pool = SharePool(fresh_prefix(nl.src, base="cse"),
                      signed_prefix=fresh_prefix(nl.src, base="s"),
                      port_widths=solver_main._all_port_widths(nl))
    families = registry.discover()
    counts = {f["kind"]: 0 for f in families}
    originals = {}
    for f in families:
        orig_attempt = f["attempt"]
        originals[f["kind"]] = orig_attempt

        def make_counting(kind, orig):
            def counting_attempt(env, ctx, name, width):
                counts[kind] += 1
                return orig(env, ctx, name, width)
            return counting_attempt

        f["attempt"] = make_counting(f["kind"], orig_attempt)
    try:
        solver_main.decide(nl, pool)
    finally:
        for f in families:
            f["attempt"] = originals[f["kind"]]
    return counts
class _FakeNL3:
    """Same spirit as `_FakeNL` (a hand-computed formula standing in for
    gate-level simulation), but adds a few more interfaces --
    `outputs`/`bits`/`support`/`drv`. `(undisclosed family).build_context`
    needs `nl.support()` (so does `quadratic.build_plan`), and
    `(undisclosed family).try_(undisclosed family)` needs `nl.bits`;
    `support()` here conservatively returns "every output bit depends on
    every input bit" (sufficient for these hand-built scenarios, since
    `(undisclosed family).py` only uses it to split into the ctrl/wide
    buckets and doesn't need precise fan-in)."""

    def __init__(self, inputs, outputs, formula):
        self.inputs = inputs
        self.outputs = outputs
        self._formula = formula
        self.drv = {}

    def bits(self, ports):
        r = []
        for n, w in ports:
            r += [n] if w == 1 else [f"{n}[{i}]" for i in range(w)]
        return r

    def simulate(self, vectors):
        return [{n: fn(v) for n, fn in self._formula.items()} for v in vectors]

    def support(self):
        all_in_bits = frozenset(self.bits(self.inputs))
        return {b: all_in_bits for b in self.bits(self.outputs)}
class _SeqPool:
    def __init__(self):
        self._names = {}

    def request(self, item):
        if item not in self._names:
            self._names[item] = f"w{len(self._names)}"
        return self._names[item]

    def price(self, refs):
        return 0
def _test26_get_val():
    """test26's `out2 - out1` residual get_val (reused by (undisclosed
    family)'s unit tests, so each test doesn't have to rewrite it)."""
    nl = Netlist(os.path.join(ROOT, "testcase", "test26", "top_primitive.v"))
    M = 1 << 16

    def get_val(row):
        return (row["out2"] - row["out1"]) % M
    return nl, get_val
class _FakeNl:
    """`(undisclosed family).run` only reads `nl.inputs`/`nl.outputs`, so it doesn't need to actually parse a netlist."""
    def __init__(self, inputs, outputs):
        self.inputs = inputs
        self.outputs = outputs
def _mk_poly_sq(port, coeff=1):
    return ("sq", port, False, None, False, coeff)
def test_atom_safe_wraps_only_when_ternary_present():
    """`atom_safe`: only parenthesizes when `?` is present, otherwise returns
    the value unchanged (the latter is the key to byte-identity -- no family
    currently prints a `?`, so Stage 0 must produce zero difference across
    the 42 existing outputs)."""
    from model import atom_safe
    check("contains a ternary -> parenthesized", atom_safe("s ? x : y"), "(s ? x : y)")
    check("contains a ternary (with a negative coefficient mixed in) -> parenthesized",
          atom_safe("s ? x : y - 8'd3 * z"), "(s ? x : y - 8'd3 * z)")
    check("no ? present -> returned as-is (existing output untouched)", atom_safe("a + b - 8'd3 * c"),
          "a + b - 8'd3 * c")
    check("a single identifier with no ? -> returned as-is", atom_safe("a"), "a")
def _verilog_arith_to_py(expr):
    """Converts the simple Verilog expressions printed by `(undisclosed
    family)`/`model` (a sized literal + a single-level `(cond ? then :
    else)`) into a Python expression that can be `eval`'d directly. Only for
    use by tests in this file, not a general-purpose translator -- it only
    recognizes the shapes the tests actually print (a single non-nested
    ternary, `W'd` sized literals)."""
    e = re.sub(r"\d+'d(\d+)", r"\1", expr)
    e = re.sub(r"\(([^?()]*)\?([^:()]*):([^()]*)\)", r"((\2) if (\1) else (\3))", e)
    return e
def test_common_factor_extraction_test04_shape():
    """For a shape like `out3 = in5 + in1*in2 + in1*in3 + in1*in4`,
    `render_word` should factor out the shared multiplier `in1` into
    `in1 * (in2 + in3 + in4)` (cost 4, saving 3 points over the flattened
    `in5 + in1*in2 + in1*in3 + in1*in4` (cost 7)) -- this is test04's out3,
    one of Stage 1's acceptance numbers (M9_spec.md)."""
    from model import Term, render_word, cost_of_word_assign
    terms = [Term("word", "in5", 13, 1),
             Term("prod", "in1", 13, 1, port2="in2", port2_width=13),
             Term("prod", "in1", 13, 1, port2="in3", port2_width=13),
             Term("prod", "in1", 13, 1, port2="in4", port2_width=13)]
    expr, _needs = render_word("out3", 13, 0, terms)
    check("test04 out3 shape: in1 is factored out into in1 * (in2+in3+in4)",
          expr, "in5 + in1 * (in2 + in3 + in4)")
    cost = cost_of_word_assign("out3", 13, expr, cost_of_text)
    check("after extraction, test04 out3's cost = 4 (the flattened rendering is 7, saving 3 points)", cost, 4)
def test_common_factor_extraction_shares_coefficient_not_just_variable():
    """'Coefficient grouping' and 'common-factor extraction' are the same rule
    (M9_spec.md): a multiplier shared by >= 2 terms is not limited to
    variables -- a numeric coefficient is itself a kind of multiplier.
    `3*a + 3*b` should have its shared `8'd3` factored out into
    `8'd3 * (a + b)` (cost 2, saving 1 point over the flattened
    `8'd3*a + 8'd3*b` (cost 3))."""
    from model import Term, render_word, cost_of_word_assign
    terms = [Term("word", "a", 8, 3), Term("word", "b", 8, 3)]
    expr, _needs = render_word("o", 8, 0, terms)
    check("coefficient grouping: 3*a + 3*b -> 8'd3 * (a + b)", expr, "8'd3 * (a + b)")
    cost = cost_of_word_assign("o", 8, expr, cost_of_text)
    check("after coefficient-group extraction, cost = 2 (the flattened rendering is 3)", cost, 2)
def test_common_factor_tie_prefers_original_rendering():
    """**On a tie, the original flattened rendering must win** (M9_spec.md: "the
    existing rendering path is always one of the candidates, and it wins on a
    tie"). Factoring the shared `a` out of `a + a*b` gives `a * (8'd1 + b)`
    -- both forms have cost 2 (`+`,`*` vs `*`,`+`), a genuine tie, not the
    false tie of "extraction is more expensive but wasn't picked". Asserts
    that `render_word` still prints the original `a + (a * b)`, not the
    extracted shape.

    WARNING note left by cold review for the primary conversation: for this
    tie case `render_word` has **two layers** of mutually-shielding guards --
    the `gain > 0` inside the greedy loop (a candidate that removes just one
    multiplier is not accepted on its own, `_common_factor_candidate` returns
    `None` directly) and the closing `cand_cost < base_cost` (only reached
    when `candidate is not None`, but when the inner guard is working
    correctly, `changed=True` mathematically implies `cand_cost < base_cost`
    always holds, so this line **can never be tested in isolation** while the
    inner guard is intact -- removing only this outer line, this assertion
    still measures green). This assertion can only prove "the two layers
    together block it", not whether the outer line itself is protected;
    `test_common_factor_second_round_tie_is_rejected` below is the test that
    uniquely covers the inner `gain > 0` guard.
    """
    from model import Term, render_word, cost_of_word_assign
    terms = [Term("word", "a", 8, 1),
             Term("prod", "a", 8, 1, port2="b", port2_width=8)]
    expr, _needs = render_word("o", 8, 0, terms)
    check("the original flattened rendering wins on a tie (not the extracted form a * (8'd1 + b))",
          expr, "a + (a * b)")
    cost = cost_of_word_assign("o", 8, expr, cost_of_text)
    check("the tie case: cost is still 2 (the extracted form is also 2, so it's not a reason to prefer it)", cost, 2)
def test_common_factor_second_round_tie_is_rejected():
    """Inside the greedy loop, **every round** must strictly save a point
    (`gain > 0`) to be accepted -- even if an earlier round already saved a
    point (making the outer `cand_cost < base_cost` always true and
    completely masking this guard), a later round that only ties must still
    not be accepted.

    Construction: `3*a + 3*b + c + c*d`. The first round extracts the shared
    coefficient `8'd3` (a genuine 1-point saving); the second round, `c`
    (shared between `word(c,1)` and `prod(c,d,1)`), is a tie -- the same tie
    as `test_common_factor_tie_prefers_original_rendering`, but this time
    wrapped inside a candidate that has already saved a point at the outer
    level, so only the inner `gain > 0` can block it; removing the outer
    `cand_cost < base_cost` does not turn this red (because the first
    round's saving still makes the overall `cand_cost` strictly less than
    `base_cost`).

    WARNING the way this was validated was by changing the strict greater-
    than in `if gain > 0 and (...)` inside `model.py`'s greedy loop to `>=`,
    and confirming in practice that this assertion really does turn red (see
    the M9_spec.md section 5 mutate entry
    `common_factor_tie_prefers_new_candidate`, now pointed at this line) --
    "a test was written" alone doesn't count.
    """
    from model import Term, render_word, cost_of_word_assign
    terms = [Term("word", "a", 8, 3), Term("word", "b", 8, 3),
             Term("word", "c", 8, 1),
             Term("prod", "c", 8, 1, port2="d", port2_width=8)]
    expr, _needs = render_word("o", 8, 0, terms)
    check("a second-round tie is not accepted (c is not extracted, c + (c * d) is kept)",
          expr, "c + (c * d) + 8'd3 * (a + b)")
    cost = cost_of_word_assign("o", 8, expr, cost_of_text)
    check("the first round did save a point (cost 5, not 6 without extraction)", cost, 5)
def test_common_factor_extraction_disabled_below_share_threshold():
    """When a multiplier is used by only 1 term (no second term sharing it),
    `render_word` must not extract it -- `_common_factor_candidate`'s O(n)
    pre-filter should return the original flattened rendering directly for
    this kind of input, never entering the search loop at all."""
    from model import Term, render_word
    terms = [Term("word", "a", 8, 3), Term("word", "b", 8, 5)]  # different coefficients, different vars, no shared multiplier at all
    expr, _needs = render_word("o", 8, 0, terms)
    check("no shared multiplier -> the flattened rendering is kept", expr, "8'd3 * a + 8'd5 * b")
def test_mux_selector_rejects_signed_1bit_port():
    """**The selector's `signed is False` condition is a hard correctness
    requirement** (M9_spec.md Stage 2 WARNING WARNING). `search_signs()` in
    `solver/quadratic.py:190-224` greedily flips signedness for every port
    without excluding 1-bit ports -- both `(undisclosed family).py`/
    `(undisclosed family).py` go through this fit, and a 1-bit port marked
    `signed=True` is data that genuinely occurs, not a theoretical
    possibility.

    What is tested here is `_mux_selector_candidates` (the first layer that
    picks candidates): for the same port, it must not be selected when
    `signed=True`, and must be selected when `signed=False`.
    """
    from model import Term, _mux_selector_candidates
    modulus = 1 << 8
    terms_signed = [Term("word", "s", 1, 5, signed=True), Term("word", "y", 8, 1)]
    check("1-bit port marked signed=True -> not a candidate selector",
          _mux_selector_candidates(terms_signed, modulus), [])
    terms_unsigned = [Term("word", "s", 1, 5, signed=False), Term("word", "y", 8, 1)]
    check("the same port with signed=False -> is a candidate selector",
          _mux_selector_candidates(terms_unsigned, modulus), ["s"])
def test_mux_classify_rejects_signed_1bit_port_from_p():
    """**The selector's `signed is False` condition, second layer (the layer
    that actually decides correctness)**.

    `_mux_selector_candidates` only picks "which ports can be candidates";
    what actually decides "should this term be folded into P (assuming
    s in {0,1} contributes a constant)" is `_classify_for_selector`'s own
    conditional -- it **independently** checks `signed is False` again,
    rather than trusting that the caller already filtered it.

    WARNING this test calls `_classify_for_selector` directly (bypassing
    `_mux_selector_candidates`'s filtering), demonstrating that **neither
    layer of the guard can be dropped**: under the normal `_mux_candidate`
    path, `s` would never be selected as a candidate in the first place (see
    the previous test), so `_classify_for_selector` is not normally called
    with a signed=True port -- but if only `_mux_selector_candidates`'s
    signed check is removed (imagine it broke), this layer's signed check
    would still catch the misclassification and `P` would still be empty.
    **Both layers must be removed together to produce wrong output** (this
    is the same pattern as M9 Stage 1's mutually-shielding `gain > 0`/
    `cand_cost < base_cost` two-layer guard; watch for this when recording
    mutations).

    Quantifying what this guard prevents: the term `word(s, coeff=5,
    signed=True)`, if misjudged as contributing the constant `+5` to P
    (assuming s is a boolean 0/1), would mean s=1 contributes +5; but
    `word_var` prints `$signed(s)` for a signed=True 1-bit port, so at s=1
    the value is **-1**, and the real contribution is `5 * (-1) = -5`. The
    two differ by 10, which is not a rounding error.
    """
    from model import Term, _classify_for_selector
    modulus = 1 << 8
    terms = [Term("word", "s", 1, 5, signed=True), Term("word", "y", 8, 1)]
    N, P = _classify_for_selector(terms, "s", 0, modulus)
    check("classify's own signed guard: word(s) with signed=True does not enter P (P should be empty)",
          P, {})
    check("that term falls into N instead (everything else -> N's catch-all)",
          N.get(("word", "s", 1, True)), 5)

    # positive control: only when the same port has signed=False does classify fold it into P.
    terms_ok = [Term("word", "s", 1, 5, signed=False), Term("word", "y", 8, 1)]
    N2, P2 = _classify_for_selector(terms_ok, "s", 0, modulus)
    check("signed=False: classify correctly folds word(s) into P's constant",
          P2.get("K"), 5)
def test_mux_classify_rejects_signed_prod_operand_from_p():
    """The same rule as the previous test, but verifying the `prod(s,o)` branch
    -- if `s` appears as an operand of `prod` with `signed=True` there
    too, it must likewise not be folded into P (contributing a `word(o)`).
    Both directions (`s` as `prod`'s `port` or `port2`) are each tested
    once."""
    from model import Term, _classify_for_selector
    modulus = 1 << 8
    t_port = Term("prod", "s", 1, 1, signed=True, port2="x", port2_width=8)
    _N, P = _classify_for_selector([t_port], "s", 0, modulus)
    check("prod(s,x), s on the port side with signed=True -> does not enter P", P, {})

    t_port2 = Term("prod", "x", 8, 1, port2="s", port2_width=1, signed2=True)
    _N2, P2 = _classify_for_selector([t_port2], "s", 0, modulus)
    check("prod(x,s), s on the port2 side with signed2=True -> does not enter P", P2, {})
def test_mux_selector_rejects_wide_port():
    """The selector's `port_width == 1` condition: `prod`'s operand can only be
    a whole word (a `bit` kind is always to the first power in terms, so it
    never appears in this classification anyway), so as long as
    `port_width != 1`, it cannot be a selector even if `signed` is False --
    a multi-bit port has no "s in {0,1}" premise, so the `s*P` derivation
    does not hold."""
    from model import Term, _mux_selector_candidates, _classify_for_selector
    modulus = 1 << 8
    terms = [Term("word", "s", 2, 5, signed=False), Term("word", "y", 8, 1)]
    check("2-bit port -> not a candidate selector", _mux_selector_candidates(terms, modulus), [])
    # classify itself also independently blocks this once (the same two-layer pattern as the signed guard).
    _N, P = _classify_for_selector(terms, "s", 0, modulus)
    check("classify's own port_width guard: a 2-bit port does not enter P", P, {})
def test_mux_selector_disqualified_by_square_or_pow_or_self_product():
    """`sq(s)`/`pow(s)`/`prod(s,s)` (even though s^2 = s holds) are not handled
    at this stage -- if any of them appear, s is abandoned entirely, not just
    by dropping that one term into N; even s's eligibility to be a selector
    is revoked (the list `_mux_selector_candidates` returns must exclude
    it)."""
    from model import Term, _mux_selector_candidates
    modulus = 1 << 8
    base = [Term("word", "s", 1, 1, signed=False), Term("word", "y", 8, 1)]
    check("the normal case: s is a candidate", _mux_selector_candidates(base, modulus), ["s"])

    with_sq = base + [Term("sq", "s", 1, 1, signed=False)]
    check("sq(s) appears -> s is abandoned entirely", _mux_selector_candidates(with_sq, modulus), [])

    with_pow = base + [Term("pow", "s", 1, 1, signed=False, exp=3)]
    check("pow(s) appears -> s is abandoned entirely", _mux_selector_candidates(with_pow, modulus), [])

    with_self_prod = base + [Term("prod", "s", 1, 1, signed=False,
                                   port2="s", port2_width=1, signed2=False)]
    check("prod(s,s) appears -> s is abandoned entirely",
          _mux_selector_candidates(with_self_prod, modulus), [])
def test_mux_end_to_end_basic_shape():
    """`x*s + y - y*s == s ? x : y` -- the most basic mux-restoration shape,
    exercised end-to-end through `render_word` (not by calling internal
    functions directly), confirming the whole path is wired up correctly."""
    from model import Term, render_word, cost_of_word_assign
    from costcount import cost_of_text
    terms = [Term("prod", "s", 8, 1, port2="x", port2_width=8),
              Term("prod", "s", 8, -1, port2="y", port2_width=8),
              Term("word", "y", 8, 1)]
    terms[0].port_width = 1
    terms[1].port_width = 1
    expr, _needs = render_word("o", 8, 0, terms)
    check("x*s + y - y*s -> s ? x : y", expr, "(s ? x : y)")
    cost = cost_of_word_assign("o", 8, expr, cost_of_text)
    check("the basic mux shape has cost = 1 (?: is one operator, R is empty)", cost, 1)
def test_mux_empty_e0_prints_zero_literal():
    """When E0 (the s=0 branch) is empty it must print `sized_literal(0,
    width)`, it cannot be omitted -- a ternary's branch cannot be missing,
    which is a different rule from R being empty and printable as nothing at
    all.

    WARNING under the current pricing rules this candidate **can never beat
    R1**: `(s ? P-content : 0)` and R1's `s * (P-content)` (with `s` factored
    out as a common multiplier) always have equal cost (`?:` and `*` each
    count as 1 operator, everything else is identical), and R1 comes before
    R2 in `render_word`'s candidate order, so R1 wins on a tie. So this test
    calls `_mux_candidate` directly (bypassing `render_word`'s candidate
    comparison) to verify in isolation that this bucket's rendered text is
    correct by itself, not that it would be selected."""
    from model import Term, _mux_candidate
    from costcount import cost_of_text
    modulus = 1 << 8
    terms = [Term("prod", "s", 8, 3, port2="x", port2_width=8),
              Term("prod", "s", 8, 5, port2="y", port2_width=8)]
    terms[0].port_width = 1
    terms[1].port_width = 1

    def word_var(port, port_width, signed):
        return port

    result = _mux_candidate("o", 8, 0, terms, modulus, cost_of_text, word_var)
    check("E0 empty -> prints sized_literal(0, width) (8'd0), not omitted",
          result[0], "(s ? 8'd3 * x + 8'd5 * y : 8'd0)")
    check("cost ties with R1's s*(...) rendering (4)", result[1], 4)
def test_mux_test21_shape_hits_target_cost():
    """A shrunk version of test21's out1: `13'd2*(a+b) + 13'd4*(c+d) +
    s*(p+q-r-t) + K` -- only worthwhile when both "common-coefficient
    extraction inside R" and "mux restoration" combine; this is the hard
    acceptance number for M9_spec.md Stage 2 (the real test21 goes from 62
    to 31). This test asserts the shape directly on the shrunk version,
    with no dependency on the corpus."""
    from model import Term, render_word, cost_of_word_assign
    from costcount import cost_of_text
    W = 13
    terms = [
        Term("word", "a", W, 2), Term("word", "b", W, 2),
        Term("word", "c", W, 4), Term("word", "d", W, 4),
        Term("prod", "s", W, 1, port2="p", port2_width=W),
        Term("prod", "s", W, 1, port2="q", port2_width=W),
        Term("prod", "s", W, -1, port2="r", port2_width=W),
        Term("prod", "s", W, -1, port2="t", port2_width=W),
        Term("word", "r", W, 1), Term("word", "t", W, 1),
    ]
    for t in terms:
        if t.port == "s":
            t.port_width = 1
    expr, _needs = render_word("o", W, 7, terms)
    check("the shrunk test21 shape: coefficient extraction inside R and mux restoration happen at the same time",
          expr,
          "13'd2 * (a + b) + 13'd4 * (c + d) + 13'd7 + (s ? p + q : r + t)")
    cost = cost_of_word_assign("o", W, expr, cost_of_text)
    check("the shrunk test21's cost = 10 (doing only R1 is 13, only R2 is 12, both more expensive than this)",
          cost, 10)
def test_r3_subset_match_with_sign():
    """R3's core criterion (M10_spec.md 2.2): `_apply_known_word_matches` tries
    `sigma` in (+1, -1) in order; it only counts as a hit when
    `sigma * Q subseteq B` (every key of Q must have a matching coefficient
    in B, and any key B has that Q lacks is kept as a residual) -- a
    coefficient off by one must not hit. It's not just "no shared keys at
    all" that fails to hit; "keys present but coefficients don't match" must
    also fail to hit."""
    from model import _apply_known_word_matches
    width = 8
    key_a = ("word", "in1", 8, False)
    key_b = ("word", "in2", 8, False)
    key_c = ("word", "in3", 8, False)

    # sigma = +1 hits: B is exactly equal to Q, the whole bucket is replaced with the known term.
    q1 = {key_a: 1, key_b: 1}
    b1 = {key_a: 1, key_b: 1}
    check("sigma=+1 hits the whole bucket", _apply_known_word_matches(b1, width, [("q1", q1)]),
          {("known", "q1"): 1})

    # sigma = -1 hits: B = -Q + residual term in3; the residual is kept, and -q2 is substituted in.
    q2 = {key_a: 1, key_b: 1}
    b2 = {key_a: -1, key_b: -1, key_c: 1}
    check("sigma=-1 hits, the residual term is kept", _apply_known_word_matches(b2, width, [("q2", q2)]),
          {key_c: 1, ("known", "q2"): -1})

    # a coefficient off by one (in2 is 2, not 1): neither a subset of +Q nor of -Q, should not hit.
    q3 = {key_a: 1, key_b: 1}
    b3 = {key_a: 1, key_b: 2}
    check("a coefficient off by one does not hit; the bucket is left unchanged", _apply_known_word_matches(b3, width, [("q3", q3)]), b3)
def test_r3_only_when_strictly_cheaper():
    """'The original rendering is always one of the candidates, and it wins on
    a tie' (M10_spec.md section 1, the same discipline as M9 R1/R2): after
    replacing `in1 + in2` with a known word `q` (whose terms are only
    `in1`), the residual `in2` still needs to be joined to `q` with a `+` --
    the cost ties (both are 1 operator), so `render_word` must print the
    original `in1 + in2` back, not `in2 + q`."""
    from model import Term, render_word
    terms = [Term("word", "in1", 8, 1), Term("word", "in2", 8, 1)]
    known_words = {"q": (0, [Term("word", "in1", 8, 1)], 8)}
    expr, _needs = render_word("o", 8, 0, terms, known_words=known_words)
    check("when cost ties, the original flattened rendering is returned", expr, "in1 + in2")
def test_r3_rejects_narrower_known_word():
    """G4 (M10_spec.md section 3): a known word `q`'s declared width `wq` must
    be `>= width` (the consumer's target width). This is not a theoretical
    concern -- construct a counterexample where the value actually differs:
    `q = a - b` (4 bits wide), at `a=0, b=1` q's 4-bit value is
    `(0-1) mod 16 = 15`; the same expression under an 8-bit context is
    `mod 256 = 255`. `15 != 255`: directly zero-extending q's identifier
    would print the wrong value, so the `wq < width` guard cannot be
    removed."""
    a, b = 0, 1
    q_val_4bit = (a - b) % 16
    correct_val_8bit = (a - b) % 256
    check("G4 counterexample: the value of a 4-bit q", q_val_4bit, 15)
    check("G4 counterexample: the true value at 8 bits", correct_val_8bit, 255)
    check("the two are not equal (zero-extension would print the wrong value)", q_val_4bit != correct_val_8bit, True)

    from model import Term

    class _FakeNl:
        @staticmethod
        def bits(_ports):
            return []

    decl_index = {"q": 0, "consumer": 1}
    entry_narrow = (0, [Term("word", "a", 4, 1), Term("word", "b", 4, -1)], 4)  # wq = 4
    filtered_narrow = solver_main._known_words_for_consumer(
        _FakeNl(), "consumer", 8, decl_index, {"q": entry_narrow}, {"q": frozenset()})
    check("G4: q excluded when wq(4) < width(8)", "q" in filtered_narrow, False)

    entry_wide = (0, [Term("word", "a", 8, 1), Term("word", "b", 8, -1)], 8)  # wq = 8
    filtered_wide = solver_main._known_words_for_consumer(
        _FakeNl(), "consumer", 8, decl_index, {"q": entry_wide}, {"q": frozenset()})
    check("control group: q passes when wq(8) >= width(8)", "q" in filtered_wide, True)
def test_r3_rejects_when_signed_prextension_needed():
    """G5 (M10_spec.md section 3): if the term being replaced satisfies
    `signed and port_width < width`, `word_var` in `render_word`'s main loop
    has already, before candidate selection, pushed its signedness
    pre-extension requirement into `signed_needs` -- replacing this term
    would leave behind an unreferenced `wire signed` declaration (cost 0,
    dead text but not a correctness issue; a conservative guard here simply
    avoids it, without digging into whether "dead text" actually matters)."""
    from model import _apply_known_word_matches
    width = 8
    key_signed = ("word", "p", 4, True)      # signed, port_width(4) < width(8) -> triggers G5
    key_unsigned = ("word", "p", 4, False)   # control group: signed=False, does not trigger G5

    bucket_signed = {key_signed: 1}
    result_signed = _apply_known_word_matches(bucket_signed, width, [("q", {key_signed: 1})])
    check("G5: reject substitution when signed and port_width < width (bucket unchanged)",
          result_signed, bucket_signed)

    bucket_unsigned = {key_unsigned: 1}
    result_unsigned = _apply_known_word_matches(bucket_unsigned, width, [("q", {key_unsigned: 1})])
    check("control group: when signed=False, substitution into the known term proceeds normally",
          result_unsigned, {("known", "q"): 1})
def test_r3_declaration_order_and_cone_guards():
    """G1 (declaration order) + G2 (cone check), M10_spec.md section 3.

    WARNING removing the guard cannot be verified just by asserting "q is
    not in filtered" and calling it done -- that could just be the false
    positive of "q would never have been substituted anyway" (M6a fell into
    this trap: removing the guard still returned None, but that was because
    the circuit was built wrong, not because the guard was actually blocking
    anything). This is done in two steps: (1) use `_known_words_for_consumer`
    (with the guard) to prove it correctly excludes q; (2) **bypass** the
    filter and feed the same q directly into `render_word`, proving that
    without this guard it really would print a reference to it (and that the
    substitution is triggered because it is strictly cheaper, not a tie
    misjudgment)."""
    from model import Term, render_word
    import fallback
    from netlist import Netlist

    # G1: outA is declared before outB, and structurally the two do not depend on each other.
    path = _write_tmp_netlist("r3_decl_order", _DECL_ORDER_NET)
    nl = Netlist(path)
    decl_index = {n: i for i, (n, _w) in enumerate(nl.outputs)}  # outA=0, outB=1

    known_a = {"outA": (0, [Term("word", "in1", 4, 1)], 1)}
    cone_a = {"outA": frozenset()}
    filtered_b = solver_main._known_words_for_consumer(nl, "outB", 1, decl_index, known_a, cone_a)
    check("G1 positive case: outB (declared later) can reference outA (declared earlier)", "outA" in filtered_b, True)

    known_b = {"outB": (0, [Term("word", "in1", 4, 1)], 1)}
    cone_b = {"outB": frozenset()}
    filtered_a = solver_main._known_words_for_consumer(nl, "outA", 1, decl_index, known_b, cone_b)
    check("G1 negative case: outA (declared earlier) cannot reference outB (declared later)", "outB" in filtered_a, False)

    # removing G1 (bypassing `_known_words_for_consumer` directly and stuffing outB into outA's
    # known_words): the real render_word really does print a reference to outB, and it is strictly cheaper
    # (the two terms in1+in2 are replaced by one identifier, saving one `+`), proving what was blocked is something real.
    terms_outA = [Term("word", "in1", 8, 1), Term("word", "in2", 8, 1)]
    bypass_expr, _needs = render_word("outA", 8, 0, terms_outA,
                                       known_words={"outB": (0, terms_outA, 8)})
    check("removing G1: outA would print a reference to outB, which is declared later", bypass_expr, "outB")

    # G2: outEarly is declared before outLate, but structurally outEarly's driving source is outLate
    # (`buf u2(outEarly, outLate)`) -- outLate's bit falls inside outEarly's cone.
    path2 = _write_tmp_netlist("r3_cone_guard", _CONE_GUARD_NET)
    nl2 = Netlist(path2)
    decl_index2 = {n: i for i, (n, _w) in enumerate(nl2.outputs)}  # outEarly=0, outLate=1
    cone_early = frozenset(fallback.cone_nets(nl2, nl2.bits([("outEarly", 1)]), set()))
    known_early = {"outEarly": (0, [Term("word", "in1", 4, 1)], 1)}
    filtered_late = solver_main._known_words_for_consumer(
        nl2, "outLate", 1, decl_index2, known_early, {"outEarly": cone_early})
    check("G2: outLate cannot reference outEarly (even though declared earlier, it would form a signal dependency cycle)",
          "outEarly" in filtered_late, False)

    # removing G2: likewise bypasses the filter and feeds it directly to render_word, proving it would print a reference that really would form a cycle.
    terms_outLate = [Term("word", "in1", 8, 1), Term("word", "in2", 8, 1)]
    bypass_expr2, _needs2 = render_word("outLate", 8, 0, terms_outLate,
                                         known_words={"outEarly": (0, terms_outLate, 8)})
    check("removing G2: outLate would print a reference to outEarly, which would create a cycle", bypass_expr2, "outEarly")
def test_r3_render_word_default_is_unchanged():
    """M10_spec.md section 4: `known_words` defaulting to `None` must be
    byte-identical in effect to explicitly passing `{}` -- this is the other
    half of the structural guarantee behind the statement "callers that don't
    supply an eligibility filter table (`share.py`/`compare.py`/...) are
    completely unaffected" (the other half being "only `main._candidate`
    ever passes a non-empty known_words", see M10_spec.md section 4)."""
    from model import Term, render_word
    terms = [Term("word", "in5", 13, 1),
             Term("prod", "in1", 13, 1, port2="in2", port2_width=13)]
    expr_default, needs_default = render_word("o", 13, 3, terms)
    expr_empty, needs_empty = render_word("o", 13, 3, terms, known_words={})
    check("not passing known_words produces byte-identical expression text to passing {}", expr_default, expr_empty)
    check("not passing known_words has the same signed_needs as passing {}", needs_default, needs_empty)
def test_r3_bucket_whole_match_in_mux_else():
    """The test23 shape (M10_spec.md 2.3 point 2's acceptance case): R2's E0
    (the `?:`'s else branch), the whole bucket, is replaced by R3 with a
    known word's identifier, `... : out1)`. This test asserts the shape and
    cost directly on the shrunk version, with no dependency on the corpus."""
    from model import Term, render_word, cost_of_word_assign
    W = 8
    C = 5
    terms = [
        Term("word", "in5", W, 1),
        Term("word", "in1", W, 1),
        Term("word", "in2", W, 1),
        Term("prod", "in1", W, -1, port2="in3", port2_width=W),
        Term("prod", "in2", W, -1, port2="in3", port2_width=W),
        Term("word", "in3", W, C),
    ]
    for t in terms:
        if t.port == "in3":
            t.port_width = 1
        if t.port2 == "in3":
            t.port2_width = 1
    known_out1 = (0, [Term("word", "in1", W, 1), Term("word", "in2", W, 1)], W)
    expr, _needs = render_word("out2", W, 0, terms, known_words={"out1": known_out1})
    check("test23 shape: the whole E0 bucket is replaced with out1", expr, f"in5 + (in3 ? {W}'d{C} : out1)")

    expr_without, _ = render_word("out2", W, 0, terms)
    cost_with = cost_of_word_assign("out2", W, expr, cost_of_text)
    cost_without = cost_of_word_assign("out2", W, expr_without, cost_of_text)
    check("R3 strictly lowers cost (2 < 3)", cost_with < cost_without, True)
_FAKE_FAMILY_TEMPLATE = (
    'FAMILY = {{\n'
    '    "kind": {kind!r},\n'
    '    "order": {order!r},\n'
    '    "stage": {stage!r},\n'
    '    "build_context": lambda env: None,\n'
    '    "attempt": lambda env, ctx, name, width: None,\n'
    '    "shape": {shape!r},\n'
    '    "label": {label!r},\n'
    '}}\n'
)
def _discover_with_temp_families(sources):
    """Builds a temporary directory holding fake family modules (`sources`:
    `{modname: file contents}`), and calls `registry.discover(that
    directory)`. **Does not touch the real `solver/*.py`** (that would risk
    stale `.pyc` files, and leave a broken working tree behind if the test
    fails). After the call, cleans up any trace in `sys.path`/`sys.modules`,
    using `finally` to guarantee cleanup even if `discover()` raises."""
    tmpdir = tempfile.mkdtemp(prefix="registry_test_")
    modnames = list(sources)
    try:
        for modname, src in sources.items():
            with open(os.path.join(tmpdir, modname + ".py"), "w") as f:
                f.write(src)
        sys.path.insert(0, tmpdir)
        return registry.discover(tmpdir)
    finally:
        if tmpdir in sys.path:
            sys.path.remove(tmpdir)
        for modname in modnames:
            sys.modules.pop(modname, None)
        shutil.rmtree(tmpdir, ignore_errors=True)
def test_registry_family_validation_raises():
    """M21 fix round, section 1: the `FAMILY` dict's format changed from a
    "convention" to "validation that raises". The primary conversation
    mistyped `(undisclosed family).py`'s `FAMILY["stage"]` from
    `"secondary"` to `"seconary"`, and measured that `discover()` silently
    dropped the entire family while `selftest.py` still printed "failed: 0"
    -- here, a temporary directory holds fake modules (the real
    `solver/*.py` is not touched), and for four kinds of malformed input it
    asserts that `discover()` raises `ValueError` with a message naming
    which field is broken."""
    try:
        _discover_with_temp_families({
            "fakefam_stage_typo": _FAKE_FAMILY_TEMPLATE.format(
                kind="fakestage", order=90001, stage="seconary", shape="expr3", label="a fake label"),
        })
    except ValueError as e:
        check("misspelled stage (seconary): raises ValueError and the message names stage",
              "stage" in str(e), True)
    else:
        check("a misspelled stage (seconary) should raise ValueError", "no error", "ValueError")

    try:
        _discover_with_temp_families({
            "fakefam_shape_typo": _FAKE_FAMILY_TEMPLATE.format(
                kind="fakeshape", order=90002, stage="secondary", shape="expr4", label="a fake label"),
        })
    except ValueError as e:
        check("invalid shape (expr4): raises ValueError and the message names shape",
              "shape" in str(e), True)
    else:
        check("an invalid shape (expr4) should raise ValueError", "no error", "ValueError")

    try:
        _discover_with_temp_families({
            "fakefam_kind_dup1": _FAKE_FAMILY_TEMPLATE.format(
                kind="fakedupkind", order=90003, stage="secondary", shape="expr3", label="a fake label"),
            "fakefam_kind_dup2": _FAKE_FAMILY_TEMPLATE.format(
                kind="fakedupkind", order=90004, stage="secondary", shape="expr3", label="a fake label"),
        })
    except ValueError as e:
        check("duplicate kind: raises ValueError and the message names the duplicate kind",
              "kind" in str(e) and "Duplicate" in str(e), True)
    else:
        check("a duplicate kind should raise ValueError", "no error", "ValueError")

    try:
        _discover_with_temp_families({
            "fakefam_order_dup1": _FAKE_FAMILY_TEMPLATE.format(
                kind="fakeorder1", order=90005, stage="secondary", shape="expr3", label="a fake label"),
            "fakefam_order_dup2": _FAKE_FAMILY_TEMPLATE.format(
                kind="fakeorder2", order=90005, stage="secondary", shape="expr3", label="a fake label"),
        })
    except ValueError as e:
        check("duplicate order: raises ValueError and the message names the duplicate order",
              "order" in str(e) and "Duplicate" in str(e), True)
    else:
        check("a duplicate order should raise ValueError", "no error", "ValueError")

    try:
        _discover_with_temp_families({
            "fakefam_missing_field": (
                'FAMILY = {\n'
                '    "kind": "fakemissing",\n'
                '    "order": 90006,\n'
                '    "stage": "secondary",\n'
                '    "build_context": lambda env: None,\n'
                '    "attempt": lambda env, ctx, name, width: None,\n'
                '}\n'
            ),
        })
    except ValueError as e:
        check("missing field (shape): raises ValueError and the message names the field",
              "shape" in str(e), True)
    else:
        check("a missing field (shape) should raise ValueError", "no error", "ValueError")

    try:
        _discover_with_temp_families({
            "fakefam_missing_label": (
                'FAMILY = {\n'
                '    "kind": "fakemissinglabel",\n'
                '    "order": 90007,\n'
                '    "stage": "secondary",\n'
                '    "build_context": lambda env: None,\n'
                '    "attempt": lambda env, ctx, name, width: None,\n'
                '    "shape": "expr3",\n'
                '}\n'
            ),
        })
    except ValueError as e:
        check("missing field (label): raises ValueError and the message names the field",
              "label" in str(e), True)
    else:
        check("a missing field (label) should raise ValueError", "no error", "ValueError")

    try:
        _discover_with_temp_families({
            "fakefam_label_not_str": _FAKE_FAMILY_TEMPLATE.format(
                kind="fakelabelnotstr", order=90008, stage="secondary", shape="expr3", label=123),
        })
    except ValueError as e:
        check("non-string label: raises ValueError and the message names label",
              "label" in str(e), True)
    else:
        check("a non-string label should raise ValueError", "no error", "ValueError")

    try:
        _discover_with_temp_families({
            "fakefam_label_empty": _FAKE_FAMILY_TEMPLATE.format(
                kind="fakelabelempty", order=90009, stage="secondary", shape="expr3", label=""),
        })
    except ValueError as e:
        check("empty label string: raises ValueError and the message names label",
              "label" in str(e), True)
    else:
        check("an empty label string should raise ValueError", "no error", "ValueError")
_DEADBRANCH_NET = """\
module top(in1, comp_a, comp_b, in2, in3, out1);
  input in1;
  input [2:0] comp_a;
  input [2:0] comp_b;
  input [1:0] in2;
  input [1:0] in3;
  output [1:0] out1;

  wire xa2;
  wire eq2;
  wire na2;
  wire lt2;
  xor g0(xa2, comp_a[2], comp_b[2]);
  not g1(eq2, xa2);
  not g2(na2, comp_a[2]);
  and g3(lt2, na2, comp_b[2]);
  wire res2;
  buf g4(res2, lt2);

  wire xa1;
  wire xeq1;
  wire na1;
  wire gt1and;
  wire lt1;
  xor g5(xa1, comp_a[1], comp_b[1]);
  not g6(xeq1, xa1);
  not g7(na1, comp_a[1]);
  and g8(gt1and, na1, comp_b[1]);
  and g9(lt1, eq2, gt1and);
  wire eq1;
  and g10(eq1, eq2, xeq1);
  wire res1;
  or g11(res1, res2, lt1);

  wire na0;
  wire gt0and;
  wire lt0;
  not g12(na0, comp_a[0]);
  and g13(gt0and, na0, comp_b[0]);
  and g14(lt0, eq1, gt0and);
  wire c;
  or g15(c, res1, lt0);

  wire nc;
  not g16(nc, c);

  wire t3_0;
  wire t3_1;
  buf g17(t3_0, in1);
  buf g18(t3_1, in1);

  wire p1_0;
  wire p2_0;
  wire t4_0;
  and g19(p1_0, c, in2[0]);
  and g20(p2_0, nc, t3_0);
  or g21(t4_0, p1_0, p2_0);

  wire p1_1;
  wire p2_1;
  wire t4_1;
  and g22(p1_1, c, in2[1]);
  and g23(p2_1, nc, t3_1);
  or g24(t4_1, p1_1, p2_1);

  wire q1_0;
  wire q2_0;
  and g25(q1_0, c, t4_0);
  and g26(q2_0, nc, in3[0]);
  or g27(out1[0], q1_0, q2_0);

  wire q1_1;
  wire q2_1;
  and g28(q1_1, c, t4_1);
  and g29(q2_1, nc, in3[1]);
  or g30(out1[1], q1_1, q2_1);
endmodule
"""
_RARE_TRIGGER_NET = """\
module top(ctrl, target, out1);
  input [3:0] ctrl;
  input [1:0] target;
  output [1:0] out1;
  wire a01;
  wire a23;
  wire allc;
  and g1(a01, ctrl[0], ctrl[1]);
  and g2(a23, ctrl[2], ctrl[3]);
  and g3(allc, a01, a23);
  and g4(out1[0], allc, target[0]);
  and g5(out1[1], allc, target[1]);
endmodule
"""
class _FakeCtxForFuncSup:
    """`_functional_support` only uses five fields -- `port_widths`/`zero_vec`/
    `boundary_vectors`/`val_vectors`/`verify_vectors` -- so a fake object can
    unit-test the pruning logic itself directly, with no need to hand-write
    a gate-level netlist (a 12-bit equality comparator's netlist is long to
    write, and it would shift the test's focus from "is the pruning
    criterion correct" to "is the netlist written correctly")."""

    def __init__(self, port_widths, boundary, val, verify):
        self.port_widths = port_widths
        self.zero_vec = {p: 0 for p in port_widths}
        self.boundary_vectors = {"out1": boundary}
        self.val_vectors = val
        self.verify_vectors = verify
class _FakeNLFromFunc:
    def __init__(self, fn):
        self._fn = fn

    def simulate(self, vecs):
        return [{"out1": self._fn(v)} for v in vecs]
def test_compare_derived_atom_end_to_end_and_no_synthetic_leak():
    """M27 fix round: run the **full pipeline** (`main.run`, including
    `assemble` and `_check_invariants`), not just up to `decide()`.

    Cold review pointed out two things:
    1. `test_compare_derived_cmp_atom_behavior_lock` only calls `decide()`,
       **never reaching `assemble()`**, so the three invariants I4/I6/I7 are
       ineffective for it.
    2. If a derived atom's synthetic name (`~cmp_...`) leaks into the
       rendered text, `costcount` will **not** flag it (it only counts
       operators) -- only iverilog would, so this must directly assert there
       is no `~` in the text.

    The three cases each exercise a different rendering path:
      synth25: two portviews compared directly (cost 2)
      synth26: a derived atom **folded into an affine expression** (cost 3)
        -- `synth25` never reaches this path
      synth27: **sensitive to pairing order** (cost 2) -- see the next
        test's explanation
    """
    for tag, want in (("synth25", 2), ("synth26", 3), ("synth27", 2)):
        path = os.path.join(ROOT, "testcase_synth", tag, "top_primitive.v")
        text = solver_main.run(path)
        check(f"{tag}: the rendered text contains no synthetic name `~` (a leak only iverilog would catch)",
              "~" in text, False)
        cost, _detail = cost_of_text(text)
        check(f"{tag}: the full pipeline's cost is {want}", cost, want)
def _build_wrap_netlist():
    """A hand-built milestone-30 netlist: `out1 = ($signed((in1-in2)[6:0]) <
    0)` -- `in1`/`in2` are each 8-bit, `(in1-in2)`'s natural width is 8, but
    only the low 7 bits are kept here (`narrow` = nat-1), corresponding to
    `_wrap_widths`'s `"narrow"` candidate. A purely linear `+-in1+-in2` cannot
    explain this function (the wraparound boundary is at `mod 128`, a
    different location from either port's own signedness flip point); it
    can only be solved with a wraparound atom. Returns the path to the
    prepared netlist."""
    b = Builder()
    b.add_input("in1", 8)
    b.add_input("in2", 8)
    diff = b.sub(b.port("in1"), b.port("in2"))   # 8-bit natural width
    narrow = diff[:7]                             # narrow = nat - 1 = 7
    s = b.slt(narrow, b.const_val(0, 7))
    b.set_output("out1", 1, [s])
    return _write_tmp_netlist("compare_wrap_narrow", b.emit("top"))
def test_compare_wrap_behavior_lock():
    """Milestone-30 behavior lock: a hand-built `(a-b) mod 2^w < C` shape (see
    `_build_wrap_netlist`), asserting it is solved by the **compare** family
    with the correct cost (2: one subtraction + one comparison, see
    `_WRAP_OP_COST` in the milestone-30 section of `solver/compare.py`'s
    module docstring)."""
    path = _build_wrap_netlist()
    text = solver_main.run(path)
    check("compare_wrap_narrow: the rendered text contains no synthetic name `~` (a leak only iverilog would catch)",
          "~" in text, False)
    cost, _detail = cost_of_text(text)
    check("compare_wrap_narrow: cost is 2 (subtraction + comparison)", cost, 2)
def test_compare_wrap_render_two_layer_unsigned_lock():
    """The (**most important**) milestone-30 rendering lock: the first-layer
    raw wire materialized by a wraparound atom must be **unsigned**, with
    width equal to `wrap_w` (7 here), and the second layer's width must
    equal `provider_width` -- see the module docstring's "rendering-layer
    landmine". Missing the second layer, or mistakenly declaring the first
    layer as `wire signed`, is something only iverilog would catch; this
    test locks down the Python-rendered text itself directly, guarding
    against it without needing to run iverilog."""
    path = _build_wrap_netlist()
    text = solver_main.run(path)
    m7 = re.search(r"wire\s+(\[6:0\])\s+(\S+);\n\s*assign \2 = \S+ - \S+;", text)
    check("the first layer's raw wire has width [6:0] (wrap_w=7) and is **not** signed", m7 is not None, True)
    check("the first layer's raw wire is not declared as `wire signed`",
          bool(re.search(r"wire\s+signed\s+\[6:0\]\s+\S+;\n\s*assign \S+ = \S+ - \S+;", text)),
          False)
    m2 = re.search(r"wire\s+signed\s+\[15:0\]\s+(\S+);\n\s*assign \1 = (\S+);", text)
    check("the second layer's raw wire has width [15:0] (provider_width)", m2 is not None, True)
def test_compare_wrap_affine_substitution_uses_second_layer():
    """Second milestone-30 rendering lock: when a wraparound atom and a base
    atom are combined into an **affine** (polynomial) expression (the
    combination allowed by `DERIVED_TERM_MAX_BASE=1`), the identifier
    `_materialize_derived_key` substitutes in **must be the second layer**
    (width == `provider_width`, value already pre-`$signed()`'d), not the
    first layer (width == `wrap_w`).

    This lock complements, rather than duplicates,
    `test_compare_wrap_render_two_layer_unsigned_lock`: that test measures
    "both layers exist", while this one measures "**the affine path really
    does use the second layer**" -- `solver/model.py`'s `word_var` only
    adds `$signed()` when `signed and port_width < width`; when
    `_side_render`/`_affine_word_terms` cannot find a synthetic wire's real
    width, it **defaults to the target width** (existing behavior of
    `share._port_width`, see its docstring "a port with no recorded width
    defaults to the target width"), which makes `port_width < width` always
    false, so `word_var` will **consume the bare wire directly without
    adding `$signed()`** -- if that bare wire is the first layer (which
    genuinely has only `wrap_w` bits and is declared unsigned), the signed
    readout would be entirely wrong, and only iverilog would catch it. The
    second layer is inherently safe because its value already has
    `$signed()` applied inside its own `raw` pool-item expression, so it
    does not depend on the downstream code guessing the width correctly.

    This is measured directly at the `compare._materialize_derived_key`
    layer, not through the full pipeline -- only this way can the affine
    path itself be locked down, without depending on the solver happening
    to pick this particular candidate (if the hand-built netlist's threshold
    were changed, `try_compare` might pick a different candidate and the
    lock would silently stop mattering; see `tools/mutate.py`'s
    `compare_wrap_drop_second_raw_layer`: applying it turns this test red,
    but the full pipeline's behavior lock stays green -- because the
    candidate simply isn't the one the solver picked, which is exactly the
    blind spot an end-to-end test alone would miss)."""
    pool = SharePool("cse_", signed_prefix="s_", port_widths={"in1": 8, "in2": 8, "in3": 8})
    atom_a = ("in1", False, 8, False)
    atom_b = ("in2", False, 8, False)
    payload = (compare.WRAP_TAG, "sub", atom_a, atom_b, 7, False, False, True)  # rs=True
    derived_lookup = {"~wrp_test_0": (payload, None)}
    provider_width = 16
    key = ("affine", (("~wrp_test_0", True, 1), ("in3", True, 1)), 0, provider_width)
    new_key, _extra = compare._materialize_derived_key(key, pool, derived_lookup, provider_width)
    subst_port = new_key[1][0][0]
    decls = {wn: (width, signed) for wn, _k, width, signed in pool.iter_decls()}
    check("the identifier substituted into affine has width == provider_width (second layer, already pre-$signed)",
          decls[subst_port][0], provider_width)
def test_compare_wrap_signed_unsigned_dual_read():
    """Milestone 30: for the same wraparound row value, both the signed and
    unsigned readouts of `_atom_value` must be asserted -- this is the
    foundation for whether the structural benefit of "a derived atom's value
    is computed once, and read separately through two synthetic names" can
    hold (see the module docstring). Tested directly at `compare`'s internal
    function layer, not through the full pipeline."""
    atom_a = ("in1", False, 5, False)
    atom_b = ("in2", False, 5, False)
    ww = 5
    payload = (compare.WRAP_TAG, "sub", atom_a, atom_b, ww, False, False, False)
    vec = {"in1": 3, "in2": 10}   # 3 - 10 = -7 ≡ 25 (mod 32)
    row = {}
    raw = compare._wrap_value(payload, vec, row)
    check("the wraparound value itself is the unsigned mod 2^w representation: 3-10 mod 32 == 25", raw, 25)

    row["~wrp_test_0"] = raw
    unsigned_read = compare._atom_value("~wrp_test_0", False, ww, True, vec, row)
    signed_read = compare._atom_value("~wrp_test_0", True, ww, True, vec, row)
    check("the unsigned readout == 25", unsigned_read, 25)
    check("the signed readout (5-bit two's complement) == 25 - 32 == -7", signed_read, -7)

    # switching to a value that does not trigger the sign bit, the two readouts should be equal.
    vec2 = {"in1": 20, "in2": 5}   # 20 - 5 = 15 < 16, sign bit is 0
    raw2 = compare._wrap_value(payload, vec2, {})
    row2 = {"~wrp_test_0": raw2}
    check("when not crossing the signedness boundary, signed and unsigned readouts are equal (both 15)",
          (compare._atom_value("~wrp_test_0", False, ww, True, vec2, row2),
           compare._atom_value("~wrp_test_0", True, ww, True, vec2, row2)),
          (15, 15))
def test_compare_wrap_deterministic():
    """Milestone 30: running the same input twice, the synthetic names and
    output text must be exactly the same -- extending compare's existing
    determinism discipline (see selftest's overall coverage of
    `PYTHONHASHSEED`) to this new wraparound-atom path, which must not drift
    just because `_build_wrap_atoms` happens to use some set/dict
    iteration."""
    path = _build_wrap_netlist()
    text1 = solver_main.run(path)
    text2 = solver_main.run(path)
    check("the rendered text from two runs is byte-identical", text1, text2)
def _wide_stage_coef2_netlist():
    """`out1 = (2*in1 < in2)` -- the atom pool has only 4 (one unsigned/one
    signed each for in1/in2); `enumerate_terms` (+-1, k<=3) cannot solve it,
    it needs the coefficient relaxation from `enumerate_terms_wide`
    (milestone 31). `b.const_mul(A, 2)` is the only way to express a
    coefficient of 2."""
    b = Builder()
    b.add_input("in1", 5)
    b.add_input("in2", 6)
    d2 = b.const_mul(b.port("in1"), 2)
    c = b.ult(d2, b.port("in2"))
    b.set_output("out1", 1, [c])
    return _write_tmp_netlist("wide_stage_coef2", b.emit("top"))
def test_compare_wide_stage_behavior_lock():
    """Milestone-31 behavior lock: the genuine shape `2*in1 < in2` is solved by
    `compare`, and really does go through the second stage
    (`compare.wide_stage_calls` at least +1) -- the criterion is that
    **"it got solved" only counts once paired with "the path was actually
    exercised"**, the same discipline as this project's other behavior
    locks."""
    path = _wide_stage_coef2_netlist()
    before = compare.wide_stage_calls
    nl = Netlist(path)
    pool = SharePool(fresh_prefix(nl.src, base="cse"),
                     signed_prefix=fresh_prefix(nl.src, base="s"),
                     port_widths=solver_main._all_port_widths(nl))
    decisions = solver_main.decide(nl, pool)
    d = decisions.get("out1")
    check("2*in1<in2: out1 solved by compare", getattr(d, "kind", None), "compare")
    check("the second stage really was exercised (wide_stage_calls has a +1)",
          compare.wide_stage_calls - before >= 1, True)
def test_compare_wide_stage_threshold_gate():
    """Milestone-31 threshold lock: `COMPARE_WIDE_POOL_MAX` really is blocking
    something, not decorative. With the same `2*in1<in2` fixture (atom pool
    4), after the threshold is set to 3 (< 4), it must no longer be
    solvable and the second stage must no longer be triggered -- directly
    proving that `try_compare`'s gate `len(atoms) <= COMPARE_WIDE_POOL_MAX`
    is a decisive keyword, not a decorative criterion that could be removed
    without affecting the result."""
    path = _wide_stage_coef2_netlist()
    orig = compare.COMPARE_WIDE_POOL_MAX
    compare.COMPARE_WIDE_POOL_MAX = 3
    try:
        before = compare.wide_stage_calls
        nl = Netlist(path)
        pool = SharePool(fresh_prefix(nl.src, base="cse"),
                         signed_prefix=fresh_prefix(nl.src, base="s"),
                         port_widths=solver_main._all_port_widths(nl))
        decisions = solver_main.decide(nl, pool)
    finally:
        compare.COMPARE_WIDE_POOL_MAX = orig
    d = decisions.get("out1")
    check("after the threshold is set to 3 (< the atom pool's 4), compare should no longer solve this word",
          getattr(d, "kind", None) == "compare", False)
    check("and the second stage was never triggered at all (the gate blocks it before enumerate_terms_wide)",
          compare.wide_stage_calls, before)
def _stage1_solves_small_pool_netlist():
    """`out1 = (in1 < in2)` -- the atom pool is likewise 4 (<=
    `COMPARE_WIDE_POOL_MAX`), but the first stage `enumerate_terms` (+-1)
    already solves it. It differs from `_wide_stage_coef2_netlist` by only
    one `const_mul`, used to isolate the other half of the second-stage
    condition (`not scored`) on its own."""
    b = Builder()
    b.add_input("in1", 5)
    b.add_input("in2", 6)
    c = b.ult(b.port("in1"), b.port("in2"))
    b.set_output("out1", 1, [c])
    return _write_tmp_netlist("stage1_small_pool", b.emit("top"))
def test_compare_wide_stage_not_scored_guard():
    """Milestone-31 guard lock: the second stage's condition is the
    conjunction `not scored and pool_ok`, and cold review on 2026-09-04
    pointed out that **only the `pool_ok` half has a red light**
    (`compare_wide_pool_max_disabled`) -- removing the `not scored` half
    would not turn any test red. This fills that gap.

    The fixture is `in1 < in2`: the pool is 4 (<= the cap, so `pool_ok` is
    true), but the first stage already solves it (so `scored` is non-empty).
    Three assertions are all required:

    1. It got solved (kind == "compare");
    2. The second stage was never triggered at all (`wide_stage_calls` delta
       = 0) -- this is the only effect of `not scored`;
    3. **The condition "the pool is small enough" really did hold at some
       point** (`wide_pool_seen` delta >= 1). Point 3 is the crucial one:
       without it, point 2 could degenerate into a no-op with an always-
       false condition and still stay green -- measured in practice, that
       is exactly what happens with test17/test29/test30 (see
       `tools/probe_m31.py`'s `MIN_POOL_SEEN`)."""
    path = _stage1_solves_small_pool_netlist()
    before_calls = compare.wide_stage_calls
    before_seen = compare.wide_pool_seen
    nl = Netlist(path)
    pool = SharePool(fresh_prefix(nl.src, base="cse"),
                     signed_prefix=fresh_prefix(nl.src, base="s"),
                     port_widths=solver_main._all_port_widths(nl))
    decisions = solver_main.decide(nl, pool)
    d = decisions.get("out1")
    check("in1<in2: out1 solved by compare's first stage", getattr(d, "kind", None), "compare")
    check("already solved in the first stage -> the second stage should never be triggered",
          compare.wide_stage_calls - before_calls, 0)
    check("and 'pool <= COMPARE_WIDE_POOL_MAX' really was true at some point (not an always-false no-op)",
          compare.wide_pool_seen - before_seen >= 1, True)
def test_compare_wide_stage_coef_max_runtime_override():
    """Milestone-31 tail end: `enumerate_terms_wide`'s default `coef_max` must
    look up `COMPARE_COEF_MAX` **at call time**, and must not be written as
    the default parameter `coef_max=COMPARE_COEF_MAX` (Python only evaluates
    that once, at `def` time).

    Cold review of the tail end on 2026-09-04 pointed out: that fix claimed
    to guard against "monkeypatching within the same process not taking
    effect", but at the time **no test would light up red for it** --
    `tools/mutate.py`'s `compare_wide_coef_max_disabled` edits the source
    text and launches a new subprocess, and the two styles behave the same
    on that path. This fills in the only observation method that can tell
    the two apart: overriding the attribute at runtime."""
    atoms = [("in1", False, 5, False)]
    orig = compare.COMPARE_COEF_MAX
    compare.COMPARE_COEF_MAX = 1
    try:
        mags = {abs(t[0][4]) for t in compare.enumerate_terms_wide(atoms)}
    finally:
        compare.COMPARE_COEF_MAX = orig
    check("overriding COMPARE_COEF_MAX to 1 at runtime, the enumerated coefficient magnitudes are only {1}", mags, {1})
    mags2 = {abs(t[0][4]) for t in compare.enumerate_terms_wide(atoms)}
    check("after restoring, the coefficient magnitudes go back to 1..COMPARE_COEF_MAX",
          mags2, set(range(1, compare.COMPARE_COEF_MAX + 1)))
def test_compare_boundary_solve_respects_wide_coeff():
    """Milestone-31 tail end: both `_direct_seed` and
    `_pivot_boundary_vectors` must genuinely **divide by** coeff, and must
    not keep the "multiply by coeff" shortcut that is only correct when
    `coeff in {1,-1}` (`coeff*coeff == 1`). The second stage is the first
    source in the codebase that produces `|coeff| > 1`.

    WARNING **why a unit test rather than end-to-end**: measured in
    practice on 2026-09-04, reverting these two spots to the old wrong
    formula left the whole selftest suite with 0 failures and
    `tools/probe_m31.py` all PASS -- because `_refine` returns immediately
    once `classify()` has already pinned the threshold (`lo == hi`), never
    reaching these two functions at all; and even if it did reach them,
    `_hillclimb`'s random re-rolls would absorb the wrong seed, only
    degrading convergence speed without changing the result. End-to-end is
    **structurally unable to detect this here**, so the back-solved value
    itself is locked directly at the unit level."""
    terms = [("in1", False, 8, False, 2)]      # 2*in1, coeff = 2
    vec = compare._direct_seed(terms, 10, {"in1": 0})
    check("_direct_seed: 2*in1 must hit 10, in1 should back-solve to 5 (the old formula gave 20)",
          vec["in1"], 5)
    cands = compare._pivot_boundary_vectors(terms, 10, [{"in1": 0}], [{}])
    check("_pivot_boundary_vectors: likewise back-solves to 5, with only one proposal",
          [c["in1"] for c in cands], [5])
    odd = compare._pivot_boundary_vectors(terms, 11, [{"in1": 0}], [{}])
    check("when the target is not evenly divisible by coeff there is no integer solution, so no proposal should be forced", odd, [])
def test_compare_wide_stage_deterministic():
    """Milestone-31 determinism: running `enumerate_terms_wide` twice on the
    same input, the order and content must match term-by-term (the docstring's
    promise of "a fixed nested-loop order, not affected by set/dict
    iteration"). Also runs the full `main.run` twice, asserting the rendered
    text is character-identical -- the same discipline as
    `test_compare_wrap_deterministic`, extended to this new coefficient-
    relaxation path."""
    atoms = [("in1", False, 5, False), ("in3", False, 2, False)]
    r1 = list(compare.enumerate_terms_wide(atoms))
    r2 = list(compare.enumerate_terms_wide(atoms))
    check("two runs of enumerate_terms_wide match term-by-term", r1, r2)
    path = _wide_stage_coef2_netlist()
    text1 = solver_main.run(path)
    text2 = solver_main.run(path)
    check("two renders of the full pipeline are byte-identical", text1, text2)
def _derived_ctx_batches(nl):
    """The four batches of vectors/results that `build_context` needs, generated the same way `main.decide` does."""
    vv = solver_main.gen_validation_vectors(nl)
    vr = nl.simulate(vv)
    fv = solver_main.gen_validation_vectors(nl, seed=solver_main.VALIDATION_SEED + 1)
    fr = nl.simulate(fv)
    return vv, vr, fv, fr
def _family_modules_on_disk():
    """Scans `solver/*.py` and returns the (sorted) names of the modules that
    define `FAMILY`.

    **Derive the expected value; do not hard-code 14.** Hard-coding it would
    make this test fail outright on "an export tree containing only public
    families" (which only has 4 families), and hard-coding is also weaker --
    the derived version incidentally catches gaps like "the file is there
    but `discover()` never picked it up" (e.g. `FAMILY` accidentally deleted,
    or a module import failure silently treated as the file being absent)."""
    solver_dir = os.path.join(ROOT, "solver")
    out = []
    for fname in sorted(os.listdir(solver_dir)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        if fname[:-3] in ("registry", "main"):
            continue
        with open(os.path.join(solver_dir, fname)) as f:
            if re.search(r"^FAMILY\s*=", f.read(), re.M):
                out.append(fname[:-3])
    return out
def test_registry_discover_matches_disk_no_skips():
    """The number of families `discover()` finds == the number of modules on
    disk that define `FAMILY`, `skipped` is empty, and the `stage`
    distribution is exactly primary 4 / secondary 10 (see the `order` table
    in `M21_registry_spec.md` section 1)."""
    families, skipped = registry.discover(return_skipped=True)
    on_disk = _family_modules_on_disk()
    check("the number of families discover() finds == the number of modules on disk that define FAMILY",
          len(families), len(on_disk))
    check("discover() does not skip any family (skipped is empty)", skipped, [])
    primary = [f for f in families if f["stage"] == "primary"]
    secondary = [f for f in families if f["stage"] == "secondary"]
    check("primary + secondary cover every family found (no stage left unassigned)",
          len(primary) + len(secondary), len(families))
    check("linear/quadratic/compare/mux are always primary (the core four families, present in both trees)",
          {"linear", "quadratic", "compare", "mux"} <= {f["kind"] for f in primary}, True)
def test_registry_kinds_match_main_solved_kinds_and_labels():
    """M22: `main.SOLVED_KINDS`/`main.LABELS` changed to be derived from
    `registry.discover()` (no longer 14 hand-written entries). This asserts
    the key sets are **exactly equal** (both directions, not just "everything
    discover found is in there"), and that the literal 14 never appears --
    the expected value is always computed live from `registry.discover()`,
    so this test still passes when the public tree only has 4 families."""
    families = registry.discover()
    kinds = {f["kind"] for f in families}
    check("main.SOLVED_KINDS' key set == registry.discover()'s kind set",
          set(solver_main.SOLVED_KINDS), kinds)
    check("main.LABELS' key set == registry.discover()'s kind set",
          set(solver_main.LABELS.keys()), kinds)
    check("every value in main.LABELS is byte-identical to the corresponding FAMILY['label']",
          {f["kind"]: solver_main.LABELS[f["kind"]] for f in families},
          {f["kind"]: f["label"] for f in families})
def test_registry_solver_families_env_filter():
    """`SOLVER_FAMILIES` whitelist filtering behavior (the real problem this
    round of the M21 fix was meant to solve, completely untested by the
    previous round): when set, it returns exactly the families in the
    whitelist and all of them are primary; when unset, it returns everything;
    **an empty string is treated as unset** (returns everything), not as
    "filter everything out" -- the latter would let a stray empty
    environment variable turn the entire solver into pure fallback with no
    red light at all. Uses `try/finally` to restore the environment
    variable, so later tests aren't polluted."""
    had_env = "SOLVER_FAMILIES" in os.environ
    old_val = os.environ.get("SOLVER_FAMILIES")
    try:
        os.environ["SOLVER_FAMILIES"] = "linear,quadratic,compare,mux"
        families = registry.discover()
        check("SOLVER_FAMILIES set to four families -> exactly those four families come back", len(families), 4)
        check("SOLVER_FAMILIES set to four families -> the kind set is correct",
              {f["kind"] for f in families}, {"linear", "quadratic", "compare", "mux"})
        check("SOLVER_FAMILIES set to four families -> all of them are primary",
              all(f["stage"] == "primary" for f in families), True)

        del os.environ["SOLVER_FAMILIES"]
        check("when SOLVER_FAMILIES is unset, everything comes back (== the number of FAMILY modules on disk)",
          len(registry.discover()), len(_family_modules_on_disk()))

        os.environ["SOLVER_FAMILIES"] = ""
        check("SOLVER_FAMILIES treats an empty string as unset, returns everything",
          len(registry.discover()), len(_family_modules_on_disk()))
    finally:
        if had_env:
            os.environ["SOLVER_FAMILIES"] = old_val
        else:
            os.environ.pop("SOLVER_FAMILIES", None)
if __name__ == "__main__":
    print("render_word, all-negative coefficients:")
    test_all_negative_first_term()
    print("costcount gate instances:")
    test_unnamed_gate_instance_costs_one()
    print("fresh_prefix, no collision:")
    test_fresh_prefix_avoids_collision()
    print("signed_wire_name prefix:")
    test_signed_wire_name_prefix()
    print("SharePool price/commit:")
    test_share_pool_price_commit()
    print("SharePool: pricing and rendering share the same source:")
    test_share_pool_price_matches_render()
    print("SharePool port view: signedness flows into the name:")
    test_share_pool_portview_signedness_in_name()
    print("SharePool names() and emit() are consistent:")
    test_share_pool_names_matches_emit()
    print("SharePool rejects a pool item that was never requested:")
    test_share_pool_rejects_unrequested()
    print("SharePool poly pool items: dedup and rendering share the same source:")
    test_share_pool_poly_dedup_and_render()
    print("SharePool poly/affine/portview naming does not collide:")
    test_share_pool_poly_name_prefix_no_collision()
    print("SharePool raw: _refs_of returns refs, the other three kinds always return empty (M6c 3.4(a)):")
    test_share_pool_raw_refs_of_and_others_empty()
    print("SharePool raw: _live_keys' transitive closure is correct (M6c 3.4(b)):")
    test_share_pool_raw_live_keys_transitive_closure()
    print("SharePool raw: signed=False renders without signed (M6c 3.4(c)):")
    test_share_pool_raw_unsigned_renders_without_signed()
    print("SharePool raw: a mutation (_refs_of always returning empty) can be distinguished by the test:")
    test_share_pool_raw_mutation_refs_of_returns_empty()
    print("SharePool raw: the I5 no-cycle check really does catch a cycle (measured in practice, not inferred from reading the code):")
    test_share_pool_raw_cycle_triggers_i5()
    print("compare: classify():")
    test_compare_classify()
    print("compare: the support coverage filter:")
    test_compare_covers_support()
    print("M26 Part 2: compare's value-range check must use every available batch of vectors:")
    test_compare_values_subset_bool_needs_all_three_batches()
    print("compare: rendering the cheapest partition:")
    test_compare_best_rendering_isolates_cheapest_atom()
    print("compare: threshold constant literals:")
    test_compare_threshold_literal()
    print("compare: the atom pool's declaration-order cycle guard:")
    test_compare_atom_pool_decl_order_guard()
    print("compare: the atom pool's cone-check cycle guard:")
    test_compare_atom_pool_cone_guard()
    print("compare: an unconverged refinement must be rejected:")
    test_compare_refine_rejects_when_not_converged()
    print("compare: RNG_SEED is a fixed constant:")
    test_compare_rng_seed_is_fixed_constant()
    print("compare: _direct_seed hits exactly:")
    test_compare_direct_seed_hits_target_exactly()
    print("compare: decision-boundary verification rejects a wrong model of the random_450 shape:")
    test_compare_decision_boundary_rejects_random450_shaped_model()
    print("compare: decision-boundary verification passes a correct model (control group + coverage):")
    test_compare_decision_boundary_accepts_correct_model()
    print("compare: try_compare rejects the random_450 shape end-to-end (with a control group):")
    test_compare_try_compare_rejects_wrong_model_end_to_end()
    print("output determinism:")
    test_output_is_deterministic()
    print("mux: rejecting an ambiguous guard:")
    test_mux_ambiguous_guard_rejected()
    print("mux: guard exclusivity (must not be shared across groups):")
    test_mux_shared_guard_reused_rejected()
    print("mux: rejecting an unpaired product factor:")
    test_mux_unpaired_factor_rejected()
    print("mux: rejecting a classification mismatch:")
    test_mux_classify_mismatch_rejected()
    print("mux: separation of the training/verification pair of sets:")
    test_mux_verify_split_catches_wrong_kind()
    print("mux: the signedness-boundary guard itself must be protected by a red light:")
    test_mux_sign_boundary_defence_catches_signed_product()
    print("mux: a signed product must be rejected:")
    test_mux_signed_product_rejected()
    print("mux: boundary vectors must test both guard states:")
    test_mux_boundary_vectors_must_probe_both_guard_states()
    print("mux: pairing dedup (the same factor must not be shared by two groups):")
    test_mux_pair_dedup_rejected()
    print("SharePool poly pool items containing a sq term:")
    print("M9 Stage 0: atom_safe only parenthesizes when `?` is present:")
    test_atom_safe_wraps_only_when_ternary_present()
    print("M4c Stage 2: sign-flip splitting requires Sigma(u) to be constant:")
    print("M4c Stage 2: sign-flip splitting rejects a diff leaf node:")
    print("M4c Stage 2: per-node common-term extraction must not apply to a leaf with a single uniform form:")
    print("M4c Stage 2: a common linear term with a single negative coefficient goes through a binary minus:")
    print("M4c Stage 2: Stage 2 is only used when strictly cheaper (Stage 1 wins on a tie):")
    print("M4c Stage 2: the pool-item shape of a private common subexpression and the pool.request call count:")
    print("M4c Stage 2 tail: no orphan pool items may be left behind when Stage 2 verification fails (invisible from the black box, the only direct assertion):")
    print("M9 Stage 1: test04 out3 shape -- the common multiplier in1 is factored out:")
    test_common_factor_extraction_test04_shape()
    print("M9 Stage 1: coefficient grouping and common-factor extraction are the same rule:")
    test_common_factor_extraction_shares_coefficient_not_just_variable()
    print("M9 Stage 1: the original flattened rendering wins on a tie:")
    test_common_factor_tie_prefers_original_rendering()
    print("M9 Stage 1: a second-round tie in the greedy loop is also rejected (uniquely covers the inner gain>0 guard):")
    test_common_factor_second_round_tie_is_rejected()
    print("M9 Stage 1: without a shared multiplier, the search is never entered and the flattened rendering is kept:")
    test_common_factor_extraction_disabled_below_share_threshold()
    print("M9 Stage 2: the selector's signed is False guard (first layer, _mux_selector_candidates):")
    test_mux_selector_rejects_signed_1bit_port()
    print("M9 Stage 2: the selector's signed is False guard (second layer, _classify_for_selector, the one that actually decides correctness):")
    test_mux_classify_rejects_signed_1bit_port_from_p()
    print("M9 Stage 2: the prod-operand version of the same rule:")
    test_mux_classify_rejects_signed_prod_operand_from_p()
    print("M9 Stage 2: the selector's port_width == 1 guard (two layers):")
    test_mux_selector_rejects_wide_port()
    print("M9 Stage 2: sq(s)/pow(s)/prod(s,s) causes the whole s to be given up on:")
    test_mux_selector_disqualified_by_square_or_pow_or_self_product()
    print("M9 Stage 2: x*s + y - y*s is restored end-to-end into s ? x : y:")
    test_mux_end_to_end_basic_shape()
    print("M9 Stage 2: an empty E0 prints 8'd0, not omitted:")
    test_mux_empty_e0_prints_zero_literal()
    print("M9 Stage 2: shrunk test21 shape, only worthwhile when R1+R2 combine:")
    test_mux_test21_shape_hits_target_cost()
    print("M10 R3: the core criterion for signed subset matching:")
    test_r3_subset_match_with_sign()
    print("M10 R3: substitution only happens when strictly cheaper; the original flattened rendering wins on a tie:")
    test_r3_only_when_strictly_cheaper()
    print("M10 R3: G4 width guard (wq < width is rejected, with a numeric counterexample):")
    test_r3_rejects_narrower_known_word()
    print("M10 R3: G5 signedness pre-extension guard:")
    test_r3_rejects_when_signed_prextension_needed()
    print("M10 R3: G1 declaration-order + G2 cone-check guards (removing the guard actually produces a real reference in practice):")
    test_r3_declaration_order_and_cone_guards()
    print("M10 R3: render_word's default known_words=None is byte-identical to passing {}:")
    test_r3_render_word_default_is_unchanged()
    print("M10 R3: test23 shape, the whole E0 bucket replaced by a known word:")
    test_r3_bucket_whole_match_in_mux_else()
    print("M11: blocks containing bit still take the raw tag; blocks without bit still take the poly tag:")
    print("M11 Stage 1: moving the constant is only allowed when every word in the wordset has the same k_const:")
    print("M11 Stage 2: narrow refit must verify every value, not just two points:")
    print("M11 Stage 2: narrow refit finds the minimal w:")
    print("M11 Stage 2: substitution only happens when strictly cheaper than Stage 1's wide version:")
    print("M11: the narrowing block's special-cased layout (K - a) does genuinely save 1 point (cold-review gap fill: previously nothing was guarding this):")
    print("M11 Stage 2: on the consumer side, Term.port_width must be the pool item's width, not the consumer's width:")
    print("M12: a pair of terms shared between poly pool items gets extracted (structurally isomorphic shrunk version of test28):")
    print("M12: a rewritten raw pool item keeps signed=True:")
    print("M12: identifier substitution in the decision expr is whole-word, not a bare str.replace:")
    print("M12: without gain > 0 (only 1 term shared) nothing is touched:")
    print("M12: refs on a raw pool item let _refs_of/_live_keys reach the sub-pool item:")
    print("M12 cold-review gap fill: the sub-pool-item width takes the max of all sharers (previously there was no guard at all):")
    print("M12 tail-end gap fill: a block whose gain is exactly 0 must not be extracted (the only direct guard for this):")
    print("M12 cold-review gap fill: whole-word identifier substitution must be enforced at the run_pool call site:")
    print("M13 cold-review gap fill: cross-width misfusion, replication misparsing, don't touch what wasn't touched:")
    print("M18 Stage 1: hoisting a common addend term out of a mux tree:")
    print("registry: FAMILY dict format validation raises:")
    test_registry_family_validation_raises()
    print("registry: the number of families discover() finds == the number of FAMILY modules on disk, skipped is empty:")
    test_registry_discover_matches_disk_no_skips()
    print("registry: every kind discover() finds is in main.SOLVED_KINDS/LABELS:")
    test_registry_kinds_match_main_solved_kinds_and_labels()
    print("registry: SOLVER_FAMILIES whitelist filtering (including empty-string semantics):")
    test_registry_solver_families_env_filter()
    print("M24 fix round: a single RANSAC candidate failing must not abort the whole search:")
    print("M25 fix round: `c ? X : c` (a condition wire used as both the selector and a branch value):")
    print("M26: a path where both branches are constants is actually exercised by a real shape, and is accepted:")
    print("M29 second round: the input-side fix to _covers_mux_support (structural cone -> functional support):")
    print("M29 second fix round: when the background has zero discriminating power, nothing is pruned (cold-review gap):")
    print("M29 second fix round: a conjunctive dependency spanning two narrow ports must not be mispruned:")
    print("M29 second fix round: boundary vectors are actually useful in the background pool (restoring mutation observability):")
    print("M29 second fix round: wide ports are never pruned:")
    print("M26 Part 2: compare's width filter replaced by a value-range check (synth24 cost 1, compare family):")
    print("M27: a compare condition may reference a derived (comparison-bit) atom (synth25 cost <= 3):")
    print("M27 fix round: full pipeline + synthetic names must not leak (synth25/26/27):")
    test_compare_derived_atom_end_to_end_and_no_synthetic_leak()
    print("M27 fix round: the pairing order of derived atoms must be guarded by a red light (synth27):")
    print("M30: behavior lock for wraparound/truncating-arithmetic derived atoms (hand-built (a-b) mod 2^w < C):")
    test_compare_wrap_behavior_lock()
    print("M30: rendering lock -- the first layer's raw wire is unsigned, width wrap_w:")
    test_compare_wrap_render_two_layer_unsigned_lock()
    print("M30: rendering lock -- the affine path really does use the second layer (not just happening to guess the right width):")
    test_compare_wrap_affine_substitution_uses_second_layer()
    print("M30: signed and unsigned readouts of the same wraparound row value:")
    test_compare_wrap_signed_unsigned_dual_read()
    print("M30: determinism of the wraparound atom path:")
    test_compare_wrap_deterministic()
    print("M31: behavior lock for compare's second stage (2*in1<in2):")
    test_compare_wide_stage_behavior_lock()
    print("M31: threshold lock for compare's second stage (COMPARE_WIDE_POOL_MAX):")
    test_compare_wide_stage_threshold_gate()
    test_compare_wide_stage_not_scored_guard()
    test_compare_wide_stage_coef_max_runtime_override()
    test_compare_boundary_solve_respects_wide_coeff()
    print("M31: determinism of compare's second stage:")
    test_compare_wide_stage_deterministic()
    print(f"\nfailed: {len(FAILED)}")
    sys.exit(1 if FAILED else 0)
