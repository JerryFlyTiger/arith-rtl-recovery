"""Uses a yosys miter + SAT to formally prove equivalence between the "original netlist (gold)" and the "candidate RTL (gate)".

Fills a gap in `rtlcheck.py`: random vectors cannot catch rare counterexamples such as a 1/4096 one.

Five outcomes: only EQUIV is a green light; NOT-EQUIV and ERROR are red lights (a broken instrument must block too, exit 1);
neither TIMEOUT nor UNSUPPORTED is (not proven does not mean passed).
    EQUIV / NOT-EQUIV / TIMEOUT / UNSUPPORTED / ERROR

    python3 tools/formalcheck.py [case...] [--timeout SEC] [--gate-dir DIR] [--sol] [--synth]
                                 [--min-proved N]

The `**` expansion is done only on temporary files under .formalcheck/ (yosys SAT does not support $pow);
it never writes back into .solverout/ or solutions/, otherwise costcount's count of 1 for `**` would become many times that for repeated multiplication.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Do not hardcode /opt/homebrew/bin/yosys -- this tool is exported to the public repo by mkpublic.py.
# If not found, keep the string; subprocess.run will raise FileNotFoundError (an OSError), and check_pair judges it ERROR.
YOSYS = shutil.which("yosys") or "yosys"

# A 20-second timeout misreports test11 as TIMEOUT: three unloaded measurements on 2026-10-04 were 25.6 / 28.7 / 43.3 seconds
# (the spread is large, so do not set it right at the maximum). TIMEOUT is not a red light, so this kind of misreport is **silent**
# Silently losing coverage is more dangerous than a red light. Using this default on all 30 cases exceeds the 600-second single-command limit, so run them in batches in the background.
DEFAULT_TIMEOUT = 60

_PAREN_POW = re.compile(r"\(\s*([A-Za-z_]\w*)\s*\*\*\s*(\d+)\s*\)")
_BARE_POW = re.compile(r"(?<![\w$'])([A-Za-z_]\w*)\s*\*\*\s*(\d+)")
_UNARY_OPS = "-+~!&|^"
# If the characters before a unary operator are one of these (or the start of the line), it is unary rather than binary
_BEFORE_UNARY = "=(,[{?:;<>*/%" + _UNARY_OPS


def _has_unary_prefix(code, pos, prev_last=""):
    """Whether the `x ** k` at the start of code[pos:] is preceded by a unary operator.

    Verilog unary operators bind tighter than `**`: `-a ** 2` is `(-a) ** 2`, and expanding it to `-(a * a)` would change the semantics.
    Looking only at the previous non-whitespace character is not enough: the `+` in `b + a ** 2` is binary. So look back one more position:
    it counts as unary only if what precedes the operator is `=`, `(`, `,`, another operator, or the start of the line.
    A multi-line expression with a binary operator at the start of a continuation line is misjudged as unary -> not expanded -> UNSUPPORTED (a loud failure).
    When only whitespace precedes `**` on the same line, look instead at the last character of the previous non-blank code line (`prev_last`):
    if it is a unary-operator character, do not expand (`-\n a ** 2` is `(-a) ** 2`).
    Known to be misjudged as unary (not expanded -> UNSUPPORTED, a loud failure, deliberately left unfixed): a `**` preceded by `&&`, `||`, or `~^`.
    """
    head = code[:pos].rstrip()
    if not head:
        return bool(prev_last) and prev_last in _UNARY_OPS
    if head[-1] not in _UNARY_OPS:
        return False
    before = head[:-1].rstrip()
    return not before or before[-1] in _BEFORE_UNARY


def _mul_expand(m):
    base, k = m.group(1), int(m.group(2))
    if k < 2:
        # `a ** 0` -> `1` is not value-preserving in mixed-signedness comparisons (`s < (a ** 0)` and `s < 1` propagate signedness differently),
        # so k<2 is not expanded; yosys is left to report UNSUPPORTED (a loud failure).
        return m.group(0)
    return "(" + " * ".join([base] * k) + ")"


def expand_pow(text):
    r"""Expands `(x ** k)` and `x ** k` into `(x * x * ...)`; skips everything after `//`;
    a bare `-a ** 2` preceded by a unary operator is not expanded (see `_has_unary_prefix`; the result is UNSUPPORTED).

    Skipping after `//` is semantically **unnecessary** (leaving it in is harmless, and a test locks that in): expanding a `**` that falls inside a comment
    leaves it inside the comment after the rewrite, with zero semantic effect; markdown bold (`**add up into one word**`) cannot be hit either,
    because the pattern needs "ASCII identifier + `**` + digits".

    ⚠ The shape that can really fail is `/* ... // ... */ assign ... (a ** 2)`: the in-line `//` makes the **real code** after it
    be treated as a comment and not expanded -> yosys returns UNSUPPORTED (loud, not silent).
    WARNING `/* */` block comments: as soon as the code part (before `//`) contains `/*`, the whole text is **returned unchanged, not expanded**.
    Because in `- /* n */ a ** 2` the unary minus is separated by the comment, `prev_last`/`head[-1]` cannot see it,
    and it would be wrongly expanded into `-(a * a)` (a silent change of semantics; the correct reading is `(-a) ** 2`). Not expanding -> yosys returns
    UNSUPPORTED (a loud failure). Checked on 2026-10-04: neither `solutions/*.v` nor `.solverout/*.v` contains `/*`,
    so this does not regress anything.
    WARNING `(* attr *)` attributes work the same way: in `- (* keep *) a ** 2` the unary minus is separated by the attribute,
    so if the code part contains `(*` the whole text is also returned unchanged. Checked on 2026-10-08: neither `solutions/*.v` nor `.solverout/*.v` contains `(*`.

    The base takes `[A-Za-z_]\w*`, so pre-extended signed wire names such as `s_in1_w8` are recognized too
    (generated by `solver/model.py:750-755`). The exponent must be a number, so `a ** b`
    (var ** var, which the QA explicitly says is illegal) is not expanded by mistake.
    """
    if any("/*" in c or "(*" in c for c in (line.partition("//")[0] for line in text.split("\n"))):
        return text
    out = []
    prev_last = ""  # the last character of the previous non-blank code line (comment lines and blank lines are skipped)
    for line in text.split("\n"):
        code, sep, comment = line.partition("//")
        code = _PAREN_POW.sub(_mul_expand, code)
        code = _BARE_POW.sub(
            lambda m, c=code, pl=prev_last: m.group(0) if _has_unary_prefix(c, m.start(), pl)
            else _mul_expand(m), code)
        if code.strip():
            prev_last = code.rstrip()[-1]
        out.append(code + sep + comment)
    return "\n".join(out)


def classify(rc, out):
    """Classification of the five outcomes. rc=1 is both NOT-EQUIV and UNSUPPORTED, so they can only be told apart by the text. Checking UNSUPPORTED before NOT-EQUIV is a convention; in practice the two outputs are mutually exclusive."""
    if rc == 124:
        return "TIMEOUT"
    if "No SAT model available for cell" in out:
        return "UNSUPPORTED"
    if "proof did fail" in out:
        return "NOT-EQUIV"
    if rc == 0:
        return "EQUIV"
    return "ERROR"


def check_pair(gold, gate, workdir, timeout=DEFAULT_TIMEOUT):
    """Returns (result, seconds, yosys output). The gate's `**` is expanded first in a temporary file in workdir."""
    t0 = time.time()
    log = os.path.join(workdir, "yosys.log")
    try:
        os.makedirs(workdir, exist_ok=True)
        os.remove(log)  # so that a stale counterexample from the previous run is not left behind when yosys fails to start
    except OSError:
        pass
    # A failure in the preparation stage must be folded into an ERROR for this case; the exception must not blow through the batch loop
    # (same approach as rtlcheck.py) -- otherwise the cases after it and the summary never run.
    try:
        os.makedirs(workdir, exist_ok=True)
        gate_x = os.path.join(workdir, "gate_expanded.v")
        with open(gate) as f:
            txt = f.read()
        with open(gate_x, "w") as f:
            f.write(expand_pow(txt))
    except Exception as e:
        return "ERROR", time.time() - t0, f"preparation stage failed: {type(e).__name__}: {e}"
    script = (
        f"read_verilog {gold}; rename top gold; prep -flatten -top gold; design -stash G;\n"
        f"design -reset; read_verilog -sv {gate_x}; rename top gate; prep -flatten -top gate; design -stash T;\n"
        "design -copy-from G -as gold gold; design -copy-from T -as gate gate;\n"
        "miter -equiv -flatten -make_assert gold gate miter;\n"
        "prep -top miter; sat -verify -prove-asserts -show-inputs -show-outputs miter"
    )
    t0 = time.time()
    try:
        # Do not use the external `timeout` command (native macOS does not have it); a timeout is always handed to classify as rc=124.
        p = subprocess.run([YOSYS, "-q", "-l", log, "-p", script], capture_output=True, text=True,
                           timeout=timeout)
        rc, out = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        rc = 124
        out = (e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) \
            + (e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))
    except (OSError, OverflowError, ValueError) as e:
        # OverflowError/ValueError: the timeout is too large for subprocess (e.g. 1e10); collapse it into ERROR
        return "ERROR", time.time() - t0, f"{type(e).__name__}: {e}"
    return classify(rc, out), time.time() - t0, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="*")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    default_gate_dir = os.path.join(ROOT, ".solverout")
    ap.add_argument("--gate-dir", default=default_gate_dir)
    ap.add_argument("--sol", action="store_true")
    ap.add_argument("--synth", action="store_true")
    ap.add_argument("--min-proved", type=int, default=None,
                    help="At least this many EQUIV are required, otherwise return nonzero."
                         "Without this threshold, even an all-TIMEOUT run would exit 0 and look like a pass.")
    a = ap.parse_args()
    if not (0 < a.timeout < float("inf")):
        ap.error("--timeout must be a finite number greater than 0 (0 does not mean \"no limit\", it times out immediately; nan/inf make subprocess raise an exception)")

    gdir = os.path.join(ROOT, "testcase_synth" if a.synth else "testcase")
    pat = r"synth\d+" if a.synth else r"test\d+"
    if a.sol and a.gate_dir != default_gate_dir:
        print("Warning: --gate-dir is ignored with --sol", file=sys.stderr)
    explicit = bool(a.cases)
    prefix = "synth" if a.synth else "test"
    cases = []
    for c in a.cases:
        m = re.fullmatch(r"(?:%s)?0*(\d{1,2})" % prefix, c.strip())
        cases.append(f"{prefix}{int(m.group(1)):02d}" if m else c.strip())
    if not cases:
        gate_root = os.path.join(ROOT, "solutions") if a.sol else a.gate_dir
        for label, d in (("gold directory", gdir), ("gate directory", gate_root)):
            if not os.path.isdir(d):
                print(f"error: {label} {d} not found (automatic enumeration needs it; use --synth or name the cases explicitly)",
                      file=sys.stderr)
                sys.exit(1)
    cases = cases or sorted(d for d in os.listdir(gdir) if re.fullmatch(pat, d))

    counts = {k: 0 for k in ("EQUIV", "NOT-EQUIV", "TIMEOUT", "UNSUPPORTED", "ERROR")}
    skipped = []
    for tag in cases:
        gold = os.path.join(gdir, tag, "top_primitive.v")
        gate = os.path.join(ROOT, "solutions", tag + ".v") if a.sol else os.path.join(a.gate_dir, tag + ".v")
        missing = [os.path.relpath(x, ROOT) for x in (gold, gate) if not os.path.exists(x)]
        if missing:
            if explicit:
                # A case the user explicitly named has no file (most likely a typo) -> error, must not exit 0
                counts["ERROR"] += 1
                print(f"{tag:10s} ERROR (not found: {', '.join(missing)})", flush=True)
            else:
                skipped.append(tag)
                print(f"{tag:10s} SKIP (missing {', '.join(missing)})", flush=True)
            continue
        res, sec, out = check_pair(gold, gate, os.path.join(ROOT, ".formalcheck", tag), a.timeout)
        counts[res] += 1
        print(f"{tag:10s} {res:12s} {sec:6.1f}s", flush=True)
        if res == "ERROR":
            print("   " + "\n   ".join(out.strip().splitlines()[-3:]))
        elif res == "NOT-EQUIV":
            print(f"   counterexample in {os.path.relpath(os.path.join(ROOT, '.formalcheck', tag, 'yosys.log'), ROOT)}")
    print("Summary:" + "  ".join(f"{k}={v}" for k, v in counts.items()) + f"  skipped={len(skipped)}")
    if counts["TIMEOUT"] or counts["UNSUPPORTED"]:
        print(f"⚠ {counts['TIMEOUT']} TIMEOUT, {counts['UNSUPPORTED']} UNSUPPORTED were not proven (not a pass)")
    short = a.min_proved is not None and counts["EQUIV"] < a.min_proved
    if short:
        print(f"✗ only proved {counts['EQUIV']}, below --min-proved {a.min_proved}")
    sys.exit(1 if counts["NOT-EQUIV"] or counts["ERROR"] or short else 0)


if __name__ == "__main__":
    main()
