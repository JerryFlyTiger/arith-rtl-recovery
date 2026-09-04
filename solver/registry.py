"""Family registry: scans `solver/*.py`, collects modules that have a `FAMILY` attribute, sorted by
`order`.

This is the seam of a single codebase where "the public build has only four families, the private
build has fourteen" — the public build simply **doesn't copy over** the closed-source family modules;
`discover()` silently skips a file it scans that doesn't exist, and `decide()` doesn't need to know
which families are missing. See M21_registry_spec.md.

`Env` is the read-only environment passed to each family's `build_context`/`attempt`, whose fields are
shared data built once outside `decide()`'s loop (unchanged across the word loop).
"""
import importlib
import os
from collections import namedtuple

Env = namedtuple("Env", "nl pool weights_by_word quad_plan quad_probe_results "
                        "val_vectors val_results verify_vectors verify_results "
                        "signed_prefix")


_REQUIRED_FIELDS = ("kind", "order", "stage", "build_context", "attempt", "shape", "label")
_VALID_STAGES = {"primary", "secondary"}
_VALID_SHAPES = {"res", "expr3"}


def _validate_family(family, modname):
    """Turns the format of the `FAMILY` dict from a "convention" into a "validation that raises" — any
    field that doesn't conform makes it `raise ValueError` naming which module and which field, never
    silently skipped, never just a warning.

    This was the direct motivation for the M21 fix round: `(an undisclosed family).py`'s
    `FAMILY["stage"]` had `"secondary"` typo'd into `"seconary"`; `discover()` still returned all 14
    families, but neither the `primary_families` nor `secondary_families` list picked up `(an
    undisclosed family)` — the whole family was silently excluded from dispatch, and `selftest.py`
    still printed "failures: 0"."""
    for field in _REQUIRED_FIELDS:
        if field not in family:
            raise ValueError(f"{modname}.FAMILY is missing required field {field!r}")

    stage = family["stage"]
    if stage not in _VALID_STAGES:
        raise ValueError(
            f"{modname}.FAMILY['stage'] = {stage!r}, must be exactly 'primary' or 'secondary'")

    shape = family["shape"]
    if shape not in _VALID_SHAPES:
        raise ValueError(
            f"{modname}.FAMILY['shape'] = {shape!r}, must be exactly 'res' or 'expr3'")

    order = family["order"]
    if not isinstance(order, int) or isinstance(order, bool):
        raise ValueError(f"{modname}.FAMILY['order'] = {order!r}, must be an int")

    kind = family["kind"]
    if not isinstance(kind, str) or not kind:
        raise ValueError(f"{modname}.FAMILY['kind'] = {kind!r}, must be a non-empty str")

    label = family["label"]
    if not isinstance(label, str) or not label:
        raise ValueError(f"{modname}.FAMILY['label'] = {label!r}, must be a non-empty str")


def discover(solver_dir=None, return_skipped=False):
    """Scans `solver/*.py`, returns a list of `FAMILY` dicts sorted by `order`.

    - `sorted(os.listdir(...))`: filename alphabetical order determines the scan order, but **the
      sort key is `order` only** (see below), independent of scan order — this is so
      `test_output_is_deterministic` stays consistent across `PYTHONHASHSEED`.
    - A module `import` failure (`ImportError`) means this family's file isn't present (the public
      build's seam), and is **silently skipped** (but recorded into `skipped`, see below); other
      exceptions (the module itself is broken, the `FAMILY` dict is malformed) propagate up and must
      never be mistaken for "this family doesn't exist".
    - Duplicate `order` or duplicate `kind` are treated as configuration errors and `raise ValueError`,
      never silent.
    - A malformed `FAMILY` dict structure (missing fields, invalid `stage`/`shape`) likewise `raise
      ValueError`, see `_validate_family`.
    - Reads the environment variable `SOLVER_FAMILIES` (a comma-separated `kind` whitelist) and
      filters once more; unset or set to an empty string are both treated as "not set" (no filtering,
      return everything) — if an empty string were treated as "filter everything out", a single slip
      of an empty environment variable would turn the whole solver into pure fallback, with no red
      light at all.
    - When `return_skipped=True`, returns `(families, skipped)`, where `skipped` is
      `[(modname, reason string), ...]`, letting "the file genuinely isn't present" and "the module is
      broken" be asserted apart in the private tree.
    """
    if solver_dir is None:
        solver_dir = os.path.dirname(os.path.abspath(__file__))

    families = []
    skipped = []
    seen_orders = {}
    seen_kinds = {}
    for fname in sorted(os.listdir(solver_dir)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        modname = fname[:-3]
        if modname in ("registry", "main"):
            continue
        try:
            mod = importlib.import_module(modname)
        except ImportError as exc:
            skipped.append((modname, str(exc)))
            continue
        family = getattr(mod, "FAMILY", None)
        if family is None:
            continue

        _validate_family(family, modname)

        order = family["order"]
        if order in seen_orders:
            raise ValueError(
                f"Duplicate FAMILY order: {order} belongs to both {seen_orders[order]!r} and {modname!r}")
        seen_orders[order] = modname

        kind = family["kind"]
        if kind in seen_kinds:
            raise ValueError(
                f"Duplicate FAMILY kind: {kind!r} belongs to both {seen_kinds[kind]!r} and {modname!r}")
        seen_kinds[kind] = modname

        families.append(family)

    families.sort(key=lambda f: f["order"])

    allowlist = os.environ.get("SOLVER_FAMILIES")
    if allowlist:
        kinds = {k.strip() for k in allowlist.split(",") if k.strip()}
        families = [f for f in families if f["kind"] in kinds]

    if return_skipped:
        return families, skipped
    return families
