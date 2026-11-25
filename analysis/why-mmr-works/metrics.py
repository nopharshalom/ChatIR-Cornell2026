"""Per-turn metrics: pool position of shown images, caption-space coverage, question/answer flags."""
import re, numpy as np
from load import *
from sklearn.feature_extraction.text import TfidfVectorizer
STOP=set('a an the is are of on in with and to at it its there this that his her their some two one three four next near by for from up down out other image picture'.split())
def toks(s): return {w for w in re.findall(r'[a-z]+', s.lower()) if w not in STOP and len(w)>1}
vec = TfidfVectorizer(stop_words='english').fit(CAPS)
def tf(xs): 
    m = vec.transform(xs); return m
GENERIC='what is the other object in the image?'
def first_hit(r): return next((i for i,x in enumerate(r['ranks']) if x<=10), 99)
def turn_metrics(rec, t, k):
    tgt = IDX[rec['target']]
    pool = t['pool_ids']; pos = {p:i for i,p in enumerate(pool)}
    sel = t['selected_ids']
    caps = [CAPS[i] for i in sel]
    T = [toks(c) for c in caps]
    jac = [len(T[i]&T[j])/max(1,len(T[i]|T[j])) for i in range(len(T)) for j in range(i+1,len(T))]
    # caption-space coverage of the top-50 of the pool (relevance region) and of the whole pool
    S = tf(caps); P = tf([CAPS[i] for i in pool])
    cov = (P @ S.T).toarray().max(1)
    tsim = (tf([CAPS[tgt]]) @ S.T).toarray().max()
    q = t['question'].strip().lower()
    qt = toks(q); qry = toks(t['query_before']); ev = set().union(*T)
    rb, ra = rec['ranks'][k], rec['ranks'][k+1]
    return dict(
        target_in_pool=t['target_in_pool'], target_shown=t['target_shown'],
        target_pos=pos.get(tgt, len(pool)),
        sel_mean_pos=float(np.mean([pos[i] for i in sel])), sel_max_pos=max(pos[i] for i in sel),
        sel_in_top10=sum(pos[i]<10 for i in sel), sel_in_top50=sum(pos[i]<50 for i in sel),
        uniq_caps=len(set(caps)), mean_jaccard=float(np.mean(jac)),
        vocab=len(ev), cov_top50=float(cov[:50].mean()), cov_pool=float(cov.mean()),
        target_cap_sim=float(tsim),
        q_generic=q==GENERIC, q_new_word=bool(qt-qry), q_from_evidence=bool((qt-qry)&ev),
        cand_unique=len(set(c.lower() for c in t['candidate_questions'])),
        ans_unsure=bool(re.search(r"unsure|not sure|can't|cannot|no ", ' '+t['answer'].lower())),
        dlogrank=float(np.log(ra)-np.log(rb)), improved=ra<rb, hit_next=ra<=10)
