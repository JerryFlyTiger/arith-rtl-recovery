# demo: understand what this solver does in five minutes

This directory is **fully self-contained** — it needs no organizer-provided `testcase/` (that material is not version-controlled);
the netlists are compiled on the fly by `tools/refgen.py`.

```bash
./demo/run_demo.sh
```

Requires `python3` and `iverilog` (macOS: `brew install icarus-verilog`).
The whole run takes about 10 seconds.

## What it does

Runs four steps for each netlist:

1. `solver/main.py -input <netlist.v> -output <rtl.v>` — recover an RTL expression from the gate-level netlist
2. Print the recovered RTL text
3. `tools/costcount.cost_of_text` computes the cost — **cost is the score itself for this problem**
   (scoring is `Σ min(cost across teams) / own cost`, so lower cost is better)
4. `tools/rtlcheck.check` drops the "recovered RTL" and the "original netlist" into the same iverilog
   testbench and run 5,000 vectors against each other — compare outputs vector by vector

Step 4 is not decoration. The pitfall this project hits over and over is "the Python model is correct, but the rendered
Verilog text is not" (width propagation, signedness, the context width of `<<`).
**Only feeding the text into a real simulator can cross that gap**, so the demo runs it too.

## Five demo cases

| netlist | actual expression | recovered by solver | cost |
|---|---|---|---|
| `d1_add3` | `in1 + in2 + in3` | `in1 + in2 + in3` | 2 |
| `d2_linear_const` | `3*in1 + 5*in2 + 7` | `12'd3 * in1 + 12'd5 * in2 + 12'd7` | 4 |
| `d3_cmp_mux` | `(in1 < in2) ? in3 : in4` | **fallback: gate-level transcription as-is** | 73 |
| `d4_macc` | `in1 * in2 + in3` | `in3 + (in1 * in2)` | 2 |
| `d5_shift_sub` | `(in1 << 3) - in2` | `11'd8 * in1 - in2` | 2 |

`d3_cmp_mux` is deliberately left failing. It hits the **currently largest known gap** — the comparison-driven mux family
(89% of the attackable gap is related to comparisons, see milestone 21 in `README.md`).
when the solver can't recognize the shape, it falls back: **transcribing the entire netlist as-is into Verilog**.
The cost looks bad (73), but **equivalence still holds** — this discipline is non-negotiable:
we'd rather a whole case fully fall back and score low than emit non-equivalent RTL. So its equivalence check still PASSes.

`d5_shift_sub` is also worth a look: `in1 << 3` is recovered as `8 * in1`. The two are equivalent and have the same cost
(both `<<` and `*` cost 1), the solver just takes the constant-multiplication route.

## Common variations

```bash
python3 demo/demo.py demo/netlists/d4_macc.v --verbose      # see the solver's decision for each word
python3 demo/demo.py --nvec 50000 --seed 7                  # increase vector count, use an independent seed
python3 demo/demo.py demo/netlists/d3_cmp_mux.v             # run a single case only
python3 demo/gen_netlists.py                                # regenerate the netlists only
```

`demo/demo.py` also accepts real test data (if you have it):

```bash
python3 demo/demo.py testcase/test22/top_primitive.v
./demo/run_demo.sh --with-real                              # test01 / test17 / test22
```

## Adding your own case

Edit `demo/gen_netlists.py`: write a function that returns a `Builder`, add it to `SPECS`, and rerun.

```python
def d6_mine():
    b = Builder()
    b.add_input("in1", 8); b.add_input("in2", 8)
    b.set_output("out1", 17, b.mul(b.port("in1"), b.port("in2")))
    return b
```

`Builder`'s API pitfalls (learned the hard way):

- **`ult` / `slt` return a single wire name (`str`), not a list.** Writing `b.ult(...)[0]` will end up grabbing
  the string's first character, creating an undeclared wire, so the output is all X — and **equivalence verification still PASSes**
  (`!==` treats both sides as equal when both are x).
- **`add` returns `max(len)+1` bits** (including the carry), `sub` returns `max(len)` bits.
- **`const_mul` only accepts non-negative constants**, negative coefficients need `neg()` applied yourself.
- Buses are always **LSB-first lists**; `0` / `1` (Python int) represent constants and get constant-folded away.
- Multiplier gate counts grow fast; use narrow words for `mul` operands (the demo uses 6-bit).

## Where the artifacts go

| path | contents | tracked |
|---|---|---|
| `demo/netlists/*.v` | generated gate-level netlists | not tracked (`gen_netlists.py` can reproduce them) |
| `demo/out/*.v` | RTL recovered by the solver | not tracked |
| `demo/out/rtlcheck/<name>/` | iverilog testbench and simulation artifacts | not tracked |

Delete `demo/netlists` and `demo/out` entirely and rerun `run_demo.sh` to rebuild everything.

## What this demo does **not** do

The real acceptance gate is far stricter than this — don't treat the demo's green light as a delivery standard. See the project root's
`CLAUDE.md`'s "minimum acceptance standard"; the key differences:

- the demo only runs 5,000 vectors with a single seed; the real acceptance gate needs **three independent seeds × 50,000 vectors**
- the demo does not run `tools/selftest.py` (about 600 seconds), nor `tools/mutate.py`
- the demo does not run the 30-case regression rig `tools/solverbench.py`
