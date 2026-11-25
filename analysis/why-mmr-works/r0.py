"""Per-turn evidence and question metrics; prints round-0 and all-turn tables, writes turns.pkl."""
from pathlib import Path
import pandas as pd, numpy as np
from metrics import *
rows=[]
for s in ARMS:
  for a in ARMS[s]:
    for q,rec in load(s,a).items():
      for k,t in enumerate(rec['turns']):
        m=turn_metrics(rec,t,k); m.update(sample=s,arm=a,qid=q,round=k); rows.append(m)
df=pd.DataFrame(rows); df.to_pickle(Path(__file__).with_name('turns.pkl'))
pd.set_option('display.width',250); pd.set_option('display.max_columns',40)
cols=['target_in_pool','target_shown','sel_mean_pos','sel_in_top10','sel_in_top50','uniq_caps','mean_jaccard','vocab','cov_top50','cov_pool','target_cap_sim','q_generic','q_from_evidence','cand_unique','ans_unsure','dlogrank','improved','hit_next']
print('ROUND 0 (identical query & pool across arms)')
r0=df[df['round']==0]
print(r0.groupby(['sample','arm'])[cols].mean().round(3).T)
print(r0.groupby(['sample','arm']).size())
print('ALL TURNS')
print(df.groupby(['sample','arm'])[cols].mean().round(3).T)
print(df.groupby(['sample','arm']).size())
