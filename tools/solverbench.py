"""The solver's regression bench: runs `solver/main.py`, verifies equivalence with `rtlcheck`,
scores with `cost_of_text`, and compares against the manual solutions under `solutions/`.

    python3 tools/solverbench.py            # all 30 cases
    python3 tools/solverbench.py 5 24        # specific cases
    python3 tools/solverbench.py --timeout 300

`rtlcheck FAIL` is the most serious failure -- it's better for a whole case to fall back entirely
than to be non-equivalent, so this tool always looks at the rtlcheck result first, cost is secondary.
The solver itself forbids parallel computation, but this tool uses subprocess to run each case in
isolation with a timeout -- that's purely execution management for the regression bench, and isn't
bound by that restriction.
"""
import argparse
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "solver"))
from netlist import Netlist  # noqa: E402
from costcount import cost_of_text, readme_costs, warn_if_empty  # noqa: E402
import rtlcheck  # noqa: E402
from main import LABELS  # noqa: E402

# the label is taken **directly from `solver/main.py`'s `LABELS`; don't copy another one here**.
# this regex has been bitten by the same pitfall twice: M3b shipping the compare family, M4a shipping the guard-product family -- both times forgot to
# forgot to update this here, and the result was that cases solved purely by the new family showed `-` in the `words` column, and didn't count toward any column of the summary line
# (test13 was even mislabeled as "all fallback", when in fact two words had already been solved). `cost` and `rtlcheck`
# the two columns are unaffected, so the error can lurk for a long time -- but this is exactly the table looked at during acceptance.
# after switching to deriving it from LABELS, the next family to ship just needs to be added to LABELS per convention, and this follows automatically.
#
# Gap 3: Python regex's `|` is first-match, not longest-match -- `"|".join(...)` following LABELS's
# in lexicographic order, implicitly assuming "none of LABELS's values is a prefix of another". That's fine for now, but
# should a new family's label ever happen to be a prefix of an existing label, the shorter one would match first and eat the tail; this regex
# would silently split in the wrong place. Sorting by string length from longest to shortest before join eliminates this assumption once and for all,
# no longer needs a human to remember "a new label must not be a prefix of an old label".
WORD_LINE = re.compile(
    r"^\s*(\S+): (" +
    "|".join(re.escape(v) for v in sorted(LABELS.values(), key=len, reverse=True)) +
    r"|fallback)", re.M)
def gate_count(net_path):
    return len(Netlist(net_path).gates)


def emitted_cost(text):
    """Returns (operator cost, gate instance count, total cost).

    The billing rule for gate instances lives in `costcount.cost_of_text` (each gate instance in a
    fallback output costs 1, see that file's comment on `GATE_INST` for the reason). This just breaks
    out the details for the table display -- **do not define a separate billing rule here** -- there
    should only ever be one billing rule.
    """
    total, detail = cost_of_text(text)
    n_gates = detail.get("gate instance", 0)
    return total - n_gates, n_gates, total


def run_one(tag, outdir, timeout, nvec, net=None):
    net = net or os.path.join(ROOT, "testcase", tag, "top_primitive.v")
    out = os.path.join(outdir, f"{tag}.v")
    solver_main = os.path.join(ROOT, "solver", "main.py")
    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, solver_main, "-input", net, "-output", out, "--verbose"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return dict(tag=tag, status="TIMEOUT", dt=timeout, words="-", cost=None, rtl="-")
    dt = time.time() - t0
    if r.returncode != 0 or not os.path.exists(out):
        return dict(tag=tag, status="CRASH", dt=dt, words="-", cost=None, rtl="-",
                     err=(r.stderr or "")[-2000:])

    solved = sum(1 for m in WORD_LINE.finditer(r.stderr)
                 if m.group(2) != "fallback")
    total_words = sum(1 for m in WORD_LINE.finditer(r.stderr))
    if total_words == 0:
        words_summary = "-"
    elif solved == 0:
        words_summary = f"all fallback(0/{total_words})"
    elif solved == total_words:
        words_summary = f"all solved({solved}/{total_words})"
    else:
        words_summary = f"partial({solved}/{total_words})"

    try:
        op_cost, n_gates, cost = emitted_cost(open(out).read())
    except Exception:
        op_cost = n_gates = cost = None

    ok, msg = rtlcheck.check(tag, nvec=nvec, sol=out, net=net)
    rtl = "PASS" if ok else ("SKIP" if ok is None else "FAIL")

    status = "OK" if (ok and cost is not None) else ("FAIL" if ok is False else "WARN")
    return dict(tag=tag, status=status, dt=dt, words=words_summary, cost=cost,
                op_cost=op_cost, n_gates=n_gates, rtl=rtl, rtlmsg=msg)


def _s(v):
    return "-" if v is None else str(v)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="*", help="case number, leave empty to run all 30")
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--nvec", type=int, default=5000,
                    help="vector count per case for rtlcheck (default 5000 for speed;"
                         "use 20000 to match README's verification strength)")
    ap.add_argument("--synth", action="store_true",
                    help="instead run every synthetic case under testcase_synth/ (no manual solution,"
                         "the 'manual solution/ratio' columns print - )")
    args = ap.parse_args(argv)

    if args.synth:
        synth_root = os.path.join(ROOT, "testcase_synth")
        tags = sorted(d for d in os.listdir(synth_root) if d.startswith("synth"))
        net_dir = synth_root
    else:
        tags = ([f"test{int(c):02d}" for c in args.cases] if args.cases
                else sorted(d for d in os.listdir(os.path.join(ROOT, "testcase"))
                            if d.startswith("test")))
        net_dir = os.path.join(ROOT, "testcase")

    outdir = os.path.join(ROOT, ".solverout")
    os.makedirs(outdir, exist_ok=True)

    # --synth never had a manual-solution table, so the warning would be false
    want = readme_costs() if args.synth else warn_if_empty(readme_costs())
    rows = []
    hdr = (f"{'case':8} {'gates':>7} {'cost':>7} {'=op':>6} {'+gate':>7} {'red%':>7} "
           f"{'manual':>7} {'ratio':>7} {'words':16} {'rtlcheck':9} {'sec':>6}")
    print(hdr)
    print("-" * len(hdr))
    for tag in tags:
        net = os.path.join(net_dir, tag, "top_primitive.v")
        gates = gate_count(net)
        res = run_one(tag, outdir, args.timeout, args.nvec, net=net)
        rows.append(res)
        c = res["cost"]
        if args.synth:
            wref = None
        else:
            _, wref = want.get(tag[4:], (None, None))
        ratio = f"{c / wref:.1f}" if (c is not None and wref) else "-"
        red = f"{100 * (1 - c / gates):.1f}" if (c is not None and gates) else "-"
        print(f"{tag:8} {gates:7d} {_s(c):>7} {_s(res.get('op_cost')):>6} "
              f"{_s(res.get('n_gates')):>7} {red:>7} {_s(wref):>7} {ratio:>7} "
              f"{res['words']:16} {res['rtl']:9} {res['dt']:6.1f}")
        if res["status"] in ("CRASH", "FAIL"):
            if "err" in res:
                print("    " + res["err"].replace("\n", "\n    "))
            elif "rtlmsg" in res:
                print("    " + res["rtlmsg"].replace("\n", "\n    "))

    n_full = sum(1 for r in rows if r["words"].startswith("all solved"))
    n_partial = sum(1 for r in rows if r["words"].startswith("partial"))
    n_none = sum(1 for r in rows if r["words"].startswith("all fallback"))
    n_fail = sum(1 for r in rows if r["rtl"] == "FAIL")
    n_timeout = sum(1 for r in rows if r["status"] == "TIMEOUT")
    n_crash = sum(1 for r in rows if r["status"] == "CRASH")
    tot_cost = sum(r["cost"] for r in rows if r.get("cost") is not None)
    tot_gates = sum(gate_count(os.path.join(net_dir, r["tag"],
                                            "top_primitive.v")) for r in rows)
    print()
    print(f"summary: all solved {n_full}, partially solved {n_partial}, all fallback {n_none},"
          f"rtlcheck FAIL {n_fail}, TIMEOUT {n_timeout}, CRASH {n_crash}")
    print(f"      total cost {tot_cost} / total gates {tot_gates}"
          f"(overall reduction {100 * (1 - tot_cost / tot_gates):.2f}%)")
    return 1 if (n_fail or n_crash) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
