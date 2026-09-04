"""Cross-word shared intermediate identifiers: collision-free prefixes + a common
subexpression pool.

After wiring into M3b (the compare family), rendering of affine pool items was
changed to directly reuse `model.render_word` (see "Approach after wiring into
M3b" in the class docstring below): there is no longer a second set of
layout/constant rules to maintain, and pricing/rendering correctness directly
inherits render_word's existing 30+9-case validation.
"""
import re

from model import Term, render_word

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def fresh_prefix(src, base="cse"):
    """Returns a string (ending in `_`) guaranteed not to collide with any identifier
    prefix in `src`.

    QA A6: no assumption may be made about how the netlist names its variables, so
    every identifier the solver generates must sit under this prefix. When there is
    no collision, it returns `base + "_"` — this is the case for the vast majority
    of real cases, and the path the "byte-identical output" acceptance check needs
    to preserve.

    Each round of collision makes the prefix one character longer (`p[:-1] + "z_"`);
    identifier length is bounded, so the loop is guaranteed to terminate.
    """
    ids = set(_IDENT_RE.findall(src))
    p = base + "_"
    while any(i.startswith(p) for i in ids):
        p = p[:-1] + "z_"
    return p


class SharePool:
    """Cross-word shared intermediate wire pool.

    Kinds of pool items:
      - port view: `("portview", port, width, signed)` —
        `wire signed [W-1:0] <prefix>v_<port>_w<W>; assign ... = $signed(port) (or port);`
        operator count 0 (pure renaming/pre-extension, not a real operation).
      - affine: `("affine", terms, K, W)`, where `terms` is an iterable of
        `(port, signed, coeff)` —
        `wire signed [W-1:0] <prefix>a<idx>; assign ... = <linear combination>;`
        operator count is computed by `_op_count` from the actually rendered text
        (pricing and rendering share the same source).
      - poly (M5a, used by `(unpublished family).py`): `("poly", terms, K, W)`,
        where `terms` is an iterable of `(kind, port1, signed1, port2, signed2, coeff)`
        sextuples, `kind ∈ {"word", "sq", "prod"}` — affine's triples can't express a
        product term, so this tag is used when the intermediate value is quadratic,
        without touching affine's existing key format (to avoid breaking the compare
        family's byte-identical output). `wire signed [W-1:0] <prefix>p<idx>;`, the
        rendering/pricing path fully shares `render_word` with affine (see
        `_poly_word_terms`).
      - raw (M6c, used by `(unpublished family).py`): `("raw", expr_text, width, refs,
        signed)` — `expr_text` is the right-hand-side expression text the caller has
        already rendered (it may directly reference another pool item's wire name,
        e.g. `request()`-ing an `h` first, then building `aug`'s `expr_text` with its
        wire name), `refs` is the set of other pool items this text references (item
        tuples before normalization), and `_refs_of` returns it directly — this is
        the **first** tag for which `_refs_of` does not return an empty tuple, and
        the first time the transitive closure of `_live_keys` and the I5 acyclicity
        check actually have an edge to walk. `signed` decides whether the declaration
        carries `signed` (the three existing tags `portview`/`affine`/`poly` stay
        unconditionally `wire signed`, unaffected by this field — only `raw` reads
        it). `wire[ signed] [W-1:0] <prefix>r<idx>;`.
        All three pre-existing tags are "single polynomials" — none of them can take
        another pool item as a term; `raw` is deliberately left as "text is
        structure", handing the whole freedom of assembly to the caller, instead of
        forcing the caller to cram nested structure into the terms model of
        `Term`/`render_word`.

    Approach after wiring into M3b (the two open issues recorded in the original M3a
    docstring, now settled by iverilog testing):

    1. **Term order and constant literals directly reuse `model.render_word`.** When
       rendering, an affine pool item's `terms` (`(port, signed, coeff)` triples) are
       folded into `model.Term("word", ...)` and fed to `render_word`, obtaining its
       already-validated optimal layout (positive-coefficient/non-±1 terms preferred
       as the first term) and constant handling (`K` is always printed as an
       **unsigned** sized literal, e.g. `8'd3`). `render_word` needs `port_width`
       (used to decide whether pre-extension into a `width`-bit signed wire is
       needed) — `SharePool` takes a `port_widths` lookup table (port -> original
       width) at construction time; when not given (e.g. selftest's synthetic test
       cases) it defaults to `port_width == width`, equivalent to "no pre-extension
       needed" — this only affects whether an extra `$signed()` pre-extension wire
       gets added, not the self-consistency of cost/correctness (selftest only tests
       price/render self-consistency to begin with, not real Verilog semantics).

       Testing (iverilog, 40-bit context, K=3,000,000,000 falling between 2^31 and
       2^32) confirms this combination is safe: **as long as any signed term that
       "needs extension" is always pre-extended into a `wire signed` in its own
       independent assign (which is exactly what `render_word` already does — it
       never embeds `$signed(narrow port)` inline in a combined expression),
       mixing an unsigned sized-literal constant into a combined expression will
       never make an already pre-extended signed wire degrade to unsigned** —
       because by then every operand in the mixed expression is already as wide as
       the context and needs no further extension, so the signedness tag's tradeoff
       has no effect on the bit computation for operands that "need no extension".
       **The dangerous combination is the reverse**: embedding `$signed(narrow
       port)` directly in the same expression while also mixing in an unsigned
       sized-literal constant, which degrades the whole expression's signedness and
       wrongly zero-extends `$signed(narrow port)` — `render_word`'s existing
       discipline (every `$signed()` appears only in its own independent
       pre-extension assign) already avoids this pitfall, so reusing it is safe.
       (The compare family's *own* threshold constants take a different path — see
       the comment at the top of `compare.py`: there, both operands are already
       pre-extended `wire signed`, and a plain decimal unsized literal defaults to
       signed, which is safe enough.)

    2. **Any extra signed pre-extension wire that needs to be generated is handed to
       the caller through `signed_needs()`.** If an affine pool item needs
       `render_word`'s pre-extension mechanism, `_render_expr` returns only the
       combined-expression text referencing those pre-extension wire names (the same
       approach used by the word-level linear family), and the actual `wire signed
       [...] ...; assign ... = $signed(port);` declarations are all generated
       uniformly by `main.py.assemble()` — `signed_needs(live_refs)` returns the
       `(port, port_width, width)` set that this batch of pool items needs, and the
       caller merges it into the same `signed_needs_all` shared with word-level
       decisions (same `signed_wire_name` prefix, same key format, naturally
       deduplicated: the same (port, port_width, width) only ever produces one
       declaration, whether requested by a word-level candidate or a pool item).

    Dedup key (`request`/`price`/`commit` share the same normalization):
      - port view: `(port, width, signed)`.
      - affine: `(tuple(sorted(terms)), K, W)` — terms are compared only after
        sorting, so the same set of terms in a different input order is treated as
        the same pool item.

    Determinism: a pool item's insertion order is the order in which `request()` was
    first called on it (the caller must guarantee that the order "declaration-order
    word × sorted term" is itself deterministic — the pool itself is only
    responsible for preserving insertion order, and does not reorder calls in any
    way).
    """

    def __init__(self, prefix, signed_prefix="s_", port_widths=None):
        self.prefix = prefix
        # The signed pre-extension wire prefix fed to `render_word` when rendering affine pool items must match
        # must exactly match the prefix `main.py.assemble()` uses when generating `signed_wire_name(...)`,
        # otherwise the names referenced inside a pool item's expression won't match the declared names (see `fresh_prefix`).
        self.signed_prefix = signed_prefix
        # port -> original width, looked up when rendering affine pool items to decide whether pre-extension is needed. When not given,
        # (e.g. selftest's synthetic test data) falls through to `_port_width`'s default: treated as port_width
        # == target width (no pre-extension needed); only affects whether an extra `$signed()` pre-extension wire is added.
        self.port_widths = dict(port_widths) if port_widths else {}
        self._order = []       # insertion order: a list of normalized keys
        self._seen = set()
        self._names = {}       # key -> wire name
        self._committed = set()  # keys that have already been paid for by some "adopted" decision

    @staticmethod
    def _normalize(item):
        tag = item[0]
        if tag == "portview":
            _, port, width, signed = item
            return ("portview", port, width, bool(signed))
        if tag == "affine":
            _, terms, k_const, width = item
            norm_terms = tuple(sorted((p, bool(s), c) for p, s, c in terms))
            return ("affine", norm_terms, k_const, width)
        if tag == "poly":
            # M5a (truncating right-shift family): `terms` is `(kind, port1, signed1, port2, signed2,
            # coeff)` sextuple, `kind ∈ {"word", "sq", "prod"}` — affine's triple
            # can't express a product term (the intermediate value in `(unpublished family).py` is quadratic), so a separate tag is opened,
            # without touching affine's existing key format (that would break the compare family's byte-identical
            # output). `word`/`sq` have `port2 = None`, `signed2 = False`.
            _, terms, k_const, width = item
            norm_terms = tuple(sorted(
                (kind, p1, bool(s1), p2, bool(s2), c)
                for kind, p1, s1, p2, s2, c in terms))
            return ("poly", norm_terms, k_const, width)
        if tag == "raw":
            _, expr_text, width, refs, signed = item
            # refs are sorted after normalization so the key is deterministic (same reason as
            # sorting terms in affine/poly). Recursively calling `_normalize` is deliberate: elements inside refs
            # should already be an item that some other pool item used when it got its name via `request()`; here
            # normalize once more to ensure the comparison is on the key, not the raw, unnormalized item form.
            norm_refs = tuple(sorted(SharePool._normalize(r) for r in refs))
            return ("raw", expr_text, width, norm_refs, bool(signed))
        raise ValueError(f"unknown pool item type: {tag!r}")

    def _op_count(self, key):
        """Operator count of the pool item.

        **Do not compute it by hand** — feed the rendered expression straight into
        `cost_of_text`. A hand-computed version once existed (nonzero-term count - 1,
        plus 1 if the constant is nonzero), and it missed two chargeable cases: the
        `*` that `_render` prints when `|coeff| != 1`, and the unary `-` when the
        first term's coefficient is negative (costs 1 point when attached to an
        identifier, free only when attached to a numeric literal — see point 1 of
        the comment at the top of `tools/costcount.py`). Pricing should only ever
        have one implementation, sharing its source with rendering, so it can't
        drift apart again.
        """
        if key[0] == "portview":
            return 0
        from costcount import cost_of_text  # deferred import: when `tools/` gets added to sys.path is up to the caller
        total, _ = cost_of_text(f"output x;\nassign x = {self._render_expr(key)};\n")
        return total

    def _name_for(self, key, idx):
        if key[0] == "portview":
            # signedness must be part of the name: `("portview", p, w, True)` and `(..., False)` are two different
            # pool item (`_normalize` folds signed into the key); omitting it would let the two collide on the same name,
            # producing bad RTL where "the same identifier is driven by two assigns".
            _, port, width, signed = key
            return f"{self.prefix}v{'s' if signed else 'u'}_{port}_w{width}"
        if key[0] == "poly":
            return f"{self.prefix}p{idx}"
        if key[0] == "raw":
            return f"{self.prefix}r{idx}"
        return f"{self.prefix}a{idx}"

    def request(self, item):
        """Requests a pool item, returning its wire name (idempotent: requesting the same
        pool item a second time returns the same name, and does not create a new
        insertion-order slot)."""
        key = self._normalize(item)
        if key not in self._seen:
            idx = len(self._order)
            self._order.append(key)
            self._seen.add(key)
            self._names[key] = self._name_for(key, idx)
        return self._names[key]

    def price(self, refs):
        """Total operator count of the pool items in `refs` that have "not yet been
        materialized by an adopted decision"."""
        total = 0
        for item in refs:
            key = self._normalize(item)
            if key in self._committed:
                continue
            total += self._op_count(key)
        return total

    def commit(self, refs):
        """Called when this word is adopted: from then on, the pool items in `refs` are
        always priced at 0 for other words."""
        for item in refs:
            self._committed.add(self._normalize(item))

    def _port_width(self, port, width):
        """The original width of a port, looked up when rendering an affine pool item;
        a port with no recorded entry defaults to the target width (i.e. no
        pre-extension needed — see the note in `__init__`)."""
        return self.port_widths.get(port, width)

    def _affine_word_terms(self, key):
        """Folds an affine pool item's `(port, signed, coeff)` triples into a list of
        `model.Term("word", ...)`, fed to `render_word` (terms/K/width are unpacked
        from the key)."""
        _, terms, k_const, width = key
        word_terms = [Term("word", port, self._port_width(port, width), coeff, signed=signed)
                      for port, signed, coeff in terms if coeff != 0]
        return word_terms, k_const, width

    def _poly_word_terms(self, key):
        """Folds a poly pool item's `(kind, port1, signed1, port2, signed2, coeff)` sextuples
        into a list of `model.Term`, fed to `render_word` (`model.py`'s `Term.__slots__`
        already has `port2`/`port2_width`/`signed2`, and the `word`/`sq`/`prod` branches
        of `render_word` already support them, sharing the same rendering path as
        affine)."""
        _, terms, k_const, width = key
        word_terms = []
        for kind, port1, signed1, port2, signed2, coeff in terms:
            if coeff == 0:
                continue
            if kind == "prod":
                word_terms.append(Term(kind, port1, self._port_width(port1, width), coeff,
                                        signed=signed1, port2=port2,
                                        port2_width=self._port_width(port2, width),
                                        signed2=signed2))
            else:
                word_terms.append(Term(kind, port1, self._port_width(port1, width), coeff,
                                        signed=signed1))
        return word_terms, k_const, width

    def signed_needs(self, live_refs):
        """The signed pre-extension wire requirements needed by all affine/poly pool items
        in the transitive closure of `live_refs` (a set of `(port, port_width, width)`),
        for `main.py.assemble()` to merge into `signed_needs_all` and emit declarations
        uniformly (sharing the same mechanism/dedup key as word-level candidates)."""
        keys = self._live_keys(live_refs)
        out = set()
        for k in keys:
            if k[0] == "affine":
                word_terms, k_const, width = self._affine_word_terms(k)
            elif k[0] == "poly":
                word_terms, k_const, width = self._poly_word_terms(k)
            else:
                continue
            _, needs = render_word("_pool", width, k_const, word_terms,
                                    signed_prefix=self.signed_prefix)
            out |= needs
        return out

    def _refs_of(self, key):
        """Other pool items that this pool item's `key` itself references. Enabled in M6c:
        `raw` returns its normalized `refs` directly (`_normalize` has already sorted
        them); the other three kinds (`portview`/`affine`/`poly`) keep the M3a-era
        behavior of always returning an empty tuple — they are "single polynomials"
        and structurally cannot reference other pool items. This is also the edge set
        used by the I5 acyclicity check to build its graph: `main._check_invariants`
        takes this method's return value directly to build the digraph, which is fed
        to `_assert_no_cycle`."""
        if key[0] == "raw":
            return key[3]
        return ()

    def _live_keys(self, live_refs):
        """Transitive closure of `live_refs`. If the walk encounters a pool item that was
        never `request()`-ed, it must raise immediately — otherwise `self._names[key]`
        would blow up later with a KeyError whose cause is impossible to trace, and
        the whole point of a named invariant is to make violations identifiable."""
        keys = set()
        stack = [self._normalize(r) for r in live_refs]
        while stack:
            k = stack.pop()
            if k in keys:
                continue
            if k not in self._names:
                raise ValueError(f"pool item referenced without going through request(): {k!r}")
            keys.add(k)
            stack.extend(self._refs_of(k))
        return keys

    def names(self, live_refs):
        """Same transitive closure and insertion order as `emit()`, but returns only the
        pool items' wire names (used by `main.assemble`'s I3/I4 invariant checks)."""
        keys = self._live_keys(live_refs)
        return [self._names[key] for key in self._order if key in keys]

    def iter_decls(self):
        """Read-only: returns `(wire_name, key, width, signed)` for **all** pool items that
        have ever been `request()`-ed in the pool so far, in insertion order (not
        limited to the transitive closure of `live_refs`, unlike `names()`/`emit()`).
        Used by M13 (`(unpublished family).py`) to tell whether an identifier in an
        expression is the wire of an existing pool item, and to get that pool item's
        width/signedness/normalized key (the latter is used as `refs` for a new pool
        item); it builds no topological relationship — declaration order is still
        decided by `emit()`/`_order` — this is purely a query and does not mutate any
        pool state."""
        for key in self._order:
            wn = self._names[key]
            if key[0] == "raw":
                width, signed = key[2], key[4]
            elif key[0] == "portview":
                width, signed = key[2], True
            else:  # affine/poly
                width, signed = key[3], True
            yield wn, key, width, signed

    def emit(self, live_refs):
        """Emits only the pool items referenced by actually-adopted decisions (transitive
        closure), returned in insertion order."""
        keys = self._live_keys(live_refs)
        return [self._render(key) for key in self._order if key in keys]

    def _render(self, key):
        wn = self._names[key]
        if key[0] == "raw":
            # only raw reads the key's own signed field; the other three kinds (portview/affine/
            # poly) keep unconditional `wire signed`, unaffected by this branch.
            _, _expr_text, width, _refs, signed = key
            sig = " signed" if signed else ""
            return (f"  wire{sig} [{width - 1}:0] {wn};\n"
                    f"  assign {wn} = {self._render_expr(key)};")
        width = key[2] if key[0] == "portview" else key[3]
        return (f"  wire signed [{width - 1}:0] {wn};\n"
                f"  assign {wn} = {self._render_expr(key)};")

    def _render_expr(self, key):
        """The pool item's right-hand-side expression (declaration and `assign` not
        included). `_op_count` also consumes this, so pricing and rendering share
        the same source."""
        if key[0] == "portview":
            _, port, _width, signed = key
            return f"$signed({port})" if signed else port
        if key[0] == "raw":
            return key[1]
        if key[0] == "poly":
            word_terms, k_const, width = self._poly_word_terms(key)
        else:
            word_terms, k_const, width = self._affine_word_terms(key)
        expr, _needs = render_word("_pool", width, k_const, word_terms,
                                    signed_prefix=self.signed_prefix)
        return expr
