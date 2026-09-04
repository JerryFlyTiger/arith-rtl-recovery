#!/usr/bin/env bash
# demo one-shot run: check environment -> generate netlists -> solver recovers RTL -> compute cost -> iverilog verifies equivalence
#
#   ./demo/run_demo.sh              # only run the demo-generated netlists (no organizer test data needed)
#   ./demo/run_demo.sh --with-real  # also run one real testcase (needs testcase/, not version-controlled)
set -u
cd "$(dirname "$0")/.."
ROOT=$(pwd)

echo "=============================================================="
echo " 0. Environment check"
echo "=============================================================="
python3 -V || { echo "python3 not found"; exit 1; }
if command -v iverilog >/dev/null 2>&1; then
    echo "iverilog: $(iverilog -V 2>&1 | head -1)"
else
    echo "⚠ iverilog not found (macOS: brew install icarus-verilog)"
    echo "  Without it, equivalence verification cannot run — a nice-looking printed expression and a correct one are two different things."
    exit 1
fi

# After manually editing source and reverting it, a stale .pyc can make it "read correct, run wrong" -- clear it first.
find "$ROOT" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null

echo
echo "=============================================================="
echo " 1. Generate demo netlists (demo/netlists/)"
echo "=============================================================="
python3 -u demo/gen_netlists.py || exit 1

echo
echo "=============================================================="
echo " 2. Run solver + compute cost + verify equivalence"
echo "=============================================================="
python3 -u demo/demo.py
rc=$?

if [ "${1:-}" = "--with-real" ]; then
    echo
    echo "=============================================================="
    echo " 3. Real test data (testcase/ is not version-controlled, skipped if absent)"
    echo "=============================================================="
    for t in test01 test17 test22; do
        n="testcase/$t/top_primitive.v"
        [ -f "$n" ] && python3 -u demo/demo.py "$n" --nvec 2000
    done
fi

echo
echo "=============================================================="
echo " Things to try next"
echo "=============================================================="
cat <<'EOF'
  python3 demo/demo.py demo/netlists/d4_macc.v --verbose   # see the solver's decision for each word
  python3 demo/demo.py --nvec 50000 --seed 7               # increase verification strength, use an independent seed
  python3 tools/solverbench.py --synth                     # synthetic cases (no test data needed)
  python3 tools/solverbench.py 1 17 22                     # the real regression rig (needs testcase/)
  python3 -u tools/selftest.py                             # full self-test (about 600 seconds, this is normal)
EOF
exit $rc
