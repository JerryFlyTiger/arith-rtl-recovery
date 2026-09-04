"""Compiles an arithmetic spec into primitive-gate Verilog, so ABC's `cec` can do equivalence
comparison directly.

Why this is needed: `tools/zcheck.py` builds a bit-by-bit miter with z3, which hits an exponential
wall on specs containing multiplication (even constant multiplication, since it expands into a stack
of shift-adds). ABC's CEC uses structural hashing + SAT sweeping, and can solve **adder networks**
in under a second almost every time -- but ABC has no RTL front-end; `read_verilog` only ingests a
structured netlist, so the spec has to be compiled into gates first. This is that compiler.

Signal representation: `0` / `1` (a Python int) represents a constant, `str` represents a wire name.
Every gate constructor does constant folding, so a shift like `<<` never produces any gates.

    b = Builder()
    b.add_input("in1", 16); b.add_input("in2", 16)
    acc = b.add(b.const_mul(b.port("in1"), 123), b.port("in2"))
    b.set_output("out1", 34, acc)
    open("ref.v","w").write(b.emit("top"))

Buses are always **LSB-first lists**.
"""


class Builder:
    def __init__(self):
        self._ports_in = []      # [(name, width)]
        self._ports_out = []     # [(name, width, bits)]
        self._wires = []
        self._gates = []
        self._n = 0
        self._cache = {}

    # ---- Basics ----------------------------------------------------------

    def _fresh(self):
        self._n += 1
        w = f"r{self._n}"
        self._wires.append(w)
        return w

    def _emit_gate(self, op, ins):
        """Builds a gate, with CSE. ins is guaranteed to already be all wire names."""
        key = (op, tuple(ins)) if op in ("not", "buf") else (op, tuple(sorted(ins)))
        if key in self._cache:
            return self._cache[key]
        o = self._fresh()
        self._gates.append((op, o, list(ins)))
        self._cache[key] = o
        return o

    def add_input(self, name, width):
        self._ports_in.append((name, width))

    def port(self, name):
        """Returns the bit list of an input port (LSB-first)."""
        for nm, w in self._ports_in:
            if nm == name:
                return [nm if w == 1 else f"{nm}[{i}]" for i in range(w)]
        raise KeyError(name)

    def set_output(self, name, width, bits):
        """Pads bits with 0 if too short, truncates if too long -- consistent with Verilog's assignment
    semantics."""
        bits = list(bits)[:width] + [0] * max(0, width - len(bits))
        self._ports_out.append((name, width, bits))

    # ---- Logic gates (with constant folding) --------------------------------------------

    def NOT(self, a):
        if isinstance(a, int):
            return 1 - a
        return self._emit_gate("not", [a])

    def AND(self, a, b):
        if isinstance(a, int):
            return b if a else 0
        if isinstance(b, int):
            return a if b else 0
        if a == b:
            return a
        return self._emit_gate("and", [a, b])

    def OR(self, a, b):
        if isinstance(a, int):
            return 1 if a else b
        if isinstance(b, int):
            return 1 if b else a
        if a == b:
            return a
        return self._emit_gate("or", [a, b])

    def XOR(self, a, b):
        if isinstance(a, int) and isinstance(b, int):
            return a ^ b
        if isinstance(a, int):
            a, b = b, a
        if isinstance(b, int):
            return self.NOT(a) if b else a
        if a == b:
            return 0
        return self._emit_gate("xor", [a, b])

    def MUX(self, s, t, f):
        """s ? t : f"""
        if isinstance(s, int):
            return t if s else f
        if t == f:
            return t
        return self.OR(self.AND(s, t), self.AND(self.NOT(s), f))

    # ---- Arithmetic ----------------------------------------------------------

    def _fa(self, a, b, c):
        s1 = self.XOR(a, b)
        s = self.XOR(s1, c)
        co = self.OR(self.AND(a, b), self.AND(s1, c))
        return s, co

    def add(self, A, B, cin=0):
        """Ripple-carry addition. Returns max(len)+1 bits."""
        n = max(len(A), len(B))
        A = list(A) + [0] * (n - len(A))
        B = list(B) + [0] * (n - len(B))
        out, c = [], cin
        for a, b in zip(A, B):
            s, c = self._fa(a, b, c)
            out.append(s)
        out.append(c)
        return out

    def sub(self, A, B):
        """A - B, two's-complement wraparound. Returns max(len) bits (excluding the borrow)."""
        n = max(len(A), len(B))
        A = list(A) + [0] * (n - len(A))
        B = list(B) + [0] * (n - len(B))
        return self.add(A, [self.NOT(b) for b in B], cin=1)[:n]

    def neg(self, A):
        return self.sub([0] * len(A), A)

    @staticmethod
    def shl(A, k):
        return [0] * k + list(A)

    def const_val(self, value, width):
        return [(value >> i) & 1 for i in range(width)]

    def const_mul(self, A, c):
        """A * c. c is a non-negative integer; adds a shifted copy of A for each set bit."""
        if c < 0:
            # negative numbers in Python are infinite-precision two's complement; neither bit_length() nor >> raises an error,
            # the loop just silently computes the wrong thing. For signed constant multiplication, call neg() yourself before multiplying.
            raise ValueError(f"const_mul only accepts non-negative constants, got {c}; for a negative coefficient use neg() instead")
        if c == 0:
            return [0]
        acc = None
        for i in range(c.bit_length()):
            if (c >> i) & 1:
                term = self.shl(A, i)
                acc = term if acc is None else self.add(acc, term)
        return acc

    def mul(self, A, B):
        """Unsigned array multiplier."""
        acc = [0] * (len(A) + len(B))
        for j, b in enumerate(B):
            if b == 0:
                continue
            row = self.shl([self.AND(a, b) for a in A], j)
            acc = self.add(acc, row)
        return acc[: len(A) + len(B)]

    def sext(self, A, width):
        A = list(A)
        return A + [A[-1]] * (width - len(A)) if width > len(A) else A[:width]

    def zext(self, A, width):
        A = list(A)
        return A + [0] * (width - len(A)) if width > len(A) else A[:width]

    def ult(self, A, B):
        """Unsigned A < B, returns 1 bit."""
        n = max(len(A), len(B))
        A, B = self.zext(A, n), self.zext(B, n)
        # the borrow for A - B
        borrow = self.add(A, [self.NOT(b) for b in B], cin=1)[n]
        return self.NOT(borrow)

    def slt(self, A, B):
        """Signed A < B, returns 1 bit."""
        n = max(len(A), len(B)) + 1
        A, B = self.sext(A, n), self.sext(B, n)
        d = self.sub(A, B)
        return d[-1]

    def mux_bus(self, s, T, F):
        n = max(len(T), len(F))
        T, F = self.zext(T, n), self.zext(F, n)
        return [self.MUX(s, t, f) for t, f in zip(T, F)]

    # ---- Output ----------------------------------------------------------

    def emit(self, module="top"):
        # constant output bits must be wired to a physical wire: ABC's read_verilog doesn't accept assign,
        # nor does it accept a 1'b0 argument for buf, so 0 and 1 are constructed from in?[0] self-xor / xnor.
        gates = list(self._gates)
        p0 = self._ports_in[0][0]
        if self._ports_in[0][1] > 1:
            p0 += "[0]"
        const_wire = {}

        def tie(v):
            if v not in const_wire:
                w = self._fresh()
                gates.append(("xor" if v == 0 else "xnor", w, [p0, p0]))
                const_wire[v] = w
            return const_wire[v]

        for nm, w, bits in self._ports_out:
            for i, b in enumerate(bits):
                dst = nm if w == 1 else f"{nm}[{i}]"
                gates.append(("buf", dst, [tie(b) if isinstance(b, int) else b]))

        names = [n for n, _ in self._ports_in] + [n for n, _, _ in self._ports_out]
        L = [f"module {module}({', '.join(names)});"]
        for nm, w in self._ports_in:
            L.append(f"  input {f'[{w-1}:0] ' if w > 1 else ''}{nm};")
        for nm, w, _ in self._ports_out:
            L.append(f"  output {f'[{w-1}:0] ' if w > 1 else ''}{nm};")
        for w in self._wires:
            L.append(f"  wire {w};")
        L.append("")
        for i, (op, o, ins) in enumerate(gates):
            L.append(f"  {op} g{i}({o} ,{','.join(ins)});")
        L.append("endmodule")
        return "\n".join(L) + "\n"


def cec(ref_verilog_path, netlist_path, timeout=600):
    """Runs ABC's cec. Returns (ok, stdout). ok=True means equivalence was proven."""
    import subprocess
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    abc = subprocess.run(
        ["bash", os.path.join(root, "scripts", "find_abc.sh")],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    r = subprocess.run(
        [abc, "-c", f'cec "{ref_verilog_path}" "{netlist_path}"'],
        capture_output=True, text=True, timeout=timeout,
    )
    out = r.stdout + r.stderr
    return ("Networks are equivalent" in out), out
