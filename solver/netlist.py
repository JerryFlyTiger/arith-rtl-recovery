import re, random
class Netlist:
    def __init__(self, path):
        src=open(path,errors='replace').read()
        self.src=src
        m=re.search(r'module\s+(\w+)\s*\(',src); self.top=m.group(1)
        self.inputs=self._ports('input'); self.outputs=self._ports('output')
        self.gates=[]
        for g in re.finditer(r'\b(and|or|nand|nor|not|buf|xor|xnor)\s+(\w+)\s*\(([^)]*)\)\s*;',src):
            a=[x.strip() for x in g.group(3).split(',')]
            self.gates.append((g.group(1),a[0],a[1:]))
        self.drv={o:(op,ins) for op,o,ins in self.gates}
    def _ports(self,kind):
        out=[]
        for m in re.finditer(r'^\s*'+kind+r'\s+(?:\[(\d+):(\d+)\]\s*)?([^;]+);',self.src,re.M):
            hi,lo,names=m.group(1),m.group(2),m.group(3)
            for n in names.split(','):
                n=n.strip()
                if not n: continue
                out.append((n, 1 if hi is None else abs(int(hi)-int(lo))+1))
        # dedup keep first
        seen={}; res=[]
        for n,w in out:
            if n not in seen: seen[n]=w; res.append((n,w))
        return res
    def bits(self,ports):
        r=[]
        for n,w in ports:
            r += [n] if w==1 else [f"{n}[{i}]" for i in range(w)]
        return r
    def eval(self, assign, mask):
        """assign: dict wire-> int bitvector. returns env"""
        env=dict(assign); env["1'b0"]=0; env["1'b1"]=mask
        pend=list(self.gates); 
        while pend:
            rest=[]; prog=False
            for op,o,ins in pend:
                if all(i in env for i in ins):
                    v=[env[i] for i in ins]
                    if   op=='and': r=v[0]
                    elif op=='or':  r=v[0]
                    elif op=='not': r=mask&~v[0]
                    elif op=='buf': r=v[0]
                    elif op=='nand':r=mask&~(v[0]&v[1])
                    elif op=='nor': r=mask&~(v[0]|v[1])
                    elif op=='xor': r=v[0]^v[1]
                    elif op=='xnor':r=mask&~(v[0]^v[1])
                    if op in('and',): 
                        for x in v[1:]: r&=x
                    if op in('or',):
                        for x in v[1:]: r|=x
                    if op in('xor',):
                        for x in v[2:]: r^=x
                    env[o]=r; prog=True
                else: rest.append((op,o,ins))
            if not prog: raise RuntimeError("combinational loop / undriven: %r"%rest[:2])
            pend=rest
        return env
    def simulate(self, vectors):
        """vectors: list of dict portname->int. returns list of dict portname->int"""
        N=len(vectors); mask=(1<<N)-1
        assign={}
        for n,w in self.inputs:
            for i in range(w):
                col=0
                for k,vec in enumerate(vectors):
                    if (vec[n]>>i)&1: col|=1<<k
                assign[n if w==1 else f"{n}[{i}]"]=col
        env=self.eval(assign,mask)
        res=[]
        for k in range(N):
            d={}
            for n,w in self.outputs:
                val=0
                for i in range(w):
                    wn=n if w==1 else f"{n}[{i}]"
                    if (env[wn]>>k)&1: val|=1<<i
                d[n]=val
            res.append(d)
        return res
    def support(self):
        """for each output bit, set of primary input bits in its cone"""
        pin=set(self.bits(self.inputs))
        memo={}
        import sys; sys.setrecursionlimit(100000)
        def sup(n):
            if n in pin: return frozenset([n])
            if n in memo: return memo[n]
            if n not in self.drv: return frozenset()
            memo[n]=frozenset()  # cycle guard
            s=set()
            for i in self.drv[n][1]: s|=sup(i)
            memo[n]=frozenset(s); return memo[n]
        return {b:sup(b) for b in self.bits(self.outputs)}
