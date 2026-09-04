"""demo runner: runs the solver once on any gate-level netlist, prints the recovered RTL and cost, and verifies equivalence.

    python3 demo/demo.py                          # run everything in demo/netlists/
    python3 demo/demo.py demo/netlists/d4_macc.v  # a specific netlist
    python3 demo/demo.py testcase/test01/top_primitive.v   # also accepts real test data
    python3 demo/demo.py --nvec 20000 --seed 7    # increase verification strength

Each case does four things:
  1. `solver/main.py -input <net> -output demo/out/<name>.v`
  2. Print the recovered RTL text
  3. Compute cost with `tools/costcount.cost_of_text` (cost is the score itself for this problem)
  4. Use `tools/rtlcheck.check` to run the "recovered RTL" and the "original netlist" against each other in iverilog

Step 4 is the only instrument that crosses the gap between "the Python model" and "the rendered Verilog text",
so the demo runs it too, instead of settling for just printing a nice-looking expression.
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "solver"))

from costcount import cost_of_text  # noqa: E402
import rtlcheck  # noqa: E402

OUTDIR = os.path.join(HERE, "out")


def run_one(net, nvec, seed, timeout, verbose):
    name = os.path.basename(net)[:-2] if net.endswith(".v") else os.path.basename(net)
    if name == "top_primitive":                      # testcase/testNN/top_primitive.v
        name = os.path.basename(os.path.dirname(net))
    os.makedirs(OUTDIR, exist_ok=True)
    rtl_path = os.path.join(OUTDIR, name + ".v")

    print(f"\n{'=' * 72}\n[{name}]  {os.path.relpath(net, ROOT)}\n{'=' * 72}")

    cmd = [sys.executable, os.path.join(ROOT, "solver", "main.py"),
           "-input", net, "-output", rtl_path]
    if verbose:
        cmd.append("--verbose")
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"  solver TIMEOUT (>{timeout}s)")
        return False
    dt = time.time() - t0
    if r.returncode != 0:
        print(f"  solver failed (exit {r.returncode})\n{r.stdout}{r.stderr}")
        return False
    # the solver's `--verbose` goes to stderr (not stdout); print both, otherwise --verbose looks like it does nothing.
    diag = (r.stdout + r.stderr).strip()
    if diag:
        print("  --- solver diagnostic output ---")
        for line in diag.splitlines():
            print("  " + line)

    text = open(rtl_path).read()
    print(f"\n  --- recovered RTL ({os.path.relpath(rtl_path, ROOT)}) ---")
    for line in text.rstrip().splitlines():
        print("  " + line)

    try:
        total, detail = cost_of_text(text)
        breakdown = ", ".join(f"{k}×{v}" for k, v in sorted(detail.items()))
        print(f"\n  cost = {total}   ({breakdown or 'no operators'})")
    except Exception as e:                            # fallback: the demo should not crash just because cost computation fails
        print(f"\n  cost computation failed: {type(e).__name__}: {e}")

    ok, msg = rtlcheck.check(name, nvec=nvec, seed=seed,
                             workdir=os.path.join(OUTDIR, "rtlcheck", name),
                             sol=rtl_path, net=net)
    print(f"  equivalence check: {'PASS' if ok else 'FAIL'}  {msg}")
    print(f"  solver took {dt:.2f}s")
    return bool(ok)


def main():
    ap = argparse.ArgumentParser(description="demo runner")
    ap.add_argument("nets", nargs="*", help="netlist paths; leave empty to run everything in demo/netlists/")
    ap.add_argument("--nvec", type=int, default=5000, help="number of vectors for equivalence verification (default 5000)")
    ap.add_argument("--seed", type=int, default=1, help="random seed for equivalence verification")
    ap.add_argument("--timeout", type=float, default=300, help="per-case solver timeout in seconds")
    ap.add_argument("--verbose", action="store_true", help="forwarded to the solver as --verbose")
    args = ap.parse_args()

    nets = args.nets
    if not nets:
        nd = os.path.join(HERE, "netlists")
        if not os.path.isdir(nd) or not os.listdir(nd):
            print("demo/netlists/ is empty, run first: python3 demo/gen_netlists.py")
            return 2
        nets = [os.path.join(nd, f) for f in sorted(os.listdir(nd)) if f.endswith(".v")]

    results = []
    for net in nets:
        if not os.path.exists(net):
            print(f"netlist not found: {net}")
            results.append((net, False))
            continue
        results.append((net, run_one(net, args.nvec, args.seed, args.timeout, args.verbose)))

    bad = [n for n, ok in results if not ok]
    print(f"\n{'=' * 72}\nsummary: {len(results) - len(bad)}/{len(results)} passed equivalence check"
          f"({args.nvec} vectors each, seed={args.seed})")
    for n in bad:
        print(f"  FAIL  {os.path.relpath(n, ROOT)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
