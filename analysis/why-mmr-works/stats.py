"""Paired bootstrap comparisons of time-to-hit vs k-means, plus MMR 0.85 vs top-10."""
import numpy as np, pandas as pd
from load import *
from metrics import first_hit
rng=np.random.default_rng(0)
def fh(s,a): return {q:min(first_hit(r),11) for q,r in load(s,a).items()}
def auc(h): return np.mean([[x<=k for k in range(1,11)] for x in h],axis=1)  # per-dialogue mean cumulative hit over rounds 1..10
def comp(pairs,label):
    a=np.array([p[0] for p in pairs]); b=np.array([p[1] for p in pairs])
    d=auc(b)-auc(a); n=len(d)
    bs=[d[rng.integers(0,n,n)].mean() for _ in range(10000)]
    r3=np.mean(b<=3)-np.mean(a<=3); r10=np.mean(b<=10)-np.mean(a<=10)
    bs3=[]; 
    for _ in range(10000):
        i=rng.integers(0,n,n); bs3.append(np.mean(b[i]<=3)-np.mean(a[i]<=3))
    win=int(((b<a)&(a<=10)|(b<a)&(a==11)).sum()); lose=int((a<b).sum())
    print(f'{label:42s} n={n} ΔAUC(r1-10)={100*d.mean():+.2f}pp [{100*np.percentile(bs,2.5):+.2f},{100*np.percentile(bs,97.5):+.2f}]  ΔHits@r3={100*r3:+.1f}pp [{100*np.percentile(bs3,2.5):+.1f},{100*np.percentile(bs3,97.5):+.1f}]  ΔHits@r10={100*r10:+.1f}pp  faster/slower={win}/{lose}')
V={s:fh(s,'kmeans (vanilla)') for s in 'AB'}
for s in 'AB':
  for a in ARMS[s]:
    if a.startswith('kmeans'): continue
    X=fh(s,a); comp([(V[s][q],X[q]) for q in X],f'{s}: {a} vs kmeans')
X={**{('A',q):v for q,v in fh('A','MMR λ=0.85').items()},**{('B',q):v for q,v in fh('B','MMR λ=0.85').items()}}
VV={**{('A',q):v for q,v in V['A'].items()},**{('B',q):v for q,v in V['B'].items()}}
comp([(VV[k],X[k]) for k in X],'A+B (400): MMR 0.85 vs kmeans')
T=fh('A','top-10 (λ=1)'); M=fh('A','MMR λ=0.85')
comp([(T[q],M[q]) for q in M],'A: MMR 0.85 vs top-10')
