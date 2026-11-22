"""Choose a saved first question by trying it on five captioned practice images."""
import argparse
from collections import Counter
import json
from pathlib import Path

import torch

from run_probe_pool_sweep import INPUTS, PILOT, REPO, ROOT, SNAPSHOT, cpu_retriever, mac, plugir, shared

OUT = ROOT / 'output/agentir-practice-questions-100'
RANKS = (11, 15, 20, 30, 50)


def prepare(score):
    corpus = shared.read_json(INPUTS / 'Protocol/visdial_captions.json')
    names = shared.read_json(INPUTS / 'Protocol/Search_Space_val_50k.json')
    assert [c['id'] for c in corpus] == names
    # Reserve the entire evaluation target set, identically for every question.
    # This restriction applies only to practice images; all searches score the full corpus.
    held_out = {r['img'] for r in shared.read_json(REPO / 'dialogues/VisDial_v1.0_queries_val.json')}
    order = shared.read_json(PILOT / 'summary.json')['cases']
    assert len(set(order)) == len(order) == 100
    cases = []
    for qid in order:
        src = shared.read_json(PILOT/'cases'/f'{qid}.json')
        allowed = {v['question'] for v in src['arms'].values()}
        questions = list(dict.fromkeys(q for q in src['candidates'] if q in allowed))
        assert set(questions) == allowed
        ranked = score([src['initial']])[0].argsort(descending=True, stable=True)[:300].tolist()
        eligible = [i for i in ranked[10:] if names[i] not in held_out]
        pics = [eligible[r-11] for r in RANKS]
        cases.append(dict(qid=qid, initial=src['initial'], questions=questions,
                          probe=questions.index(src['arms']['probe']['question']),
                          ids=pics, captions=[corpus[i]['caption'][0] for i in pics]))
    shared.atomic_json(OUT/'cases.json', cases)
    return cases


def infer(worker, c):
    answers = []
    for q in c['questions']:
        messages = [dict(role='system', content="Answer the question separately for each caption. "
                   "Use only details stated in that caption, including unambiguous synonyms. "
                   "If the requested detail or its subject is missing, write null. For 'other object' "
                   "use an explicitly named additional object beyond the initial description. "
                   "Do not guess colors, locations or other facts. Give one short answer per caption. "
                   "Return only a JSON array of five strings or nulls in caption order."),
                    dict(role='user', content=json.dumps(dict(initial=c['initial'], question=q,
                                                             captions=c['captions'])))]
        for attempt in range(2):
            raw = worker.complete(messages, 160)
            try:
                a = json.loads(raw.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
                if not isinstance(a, list) or len(a) != 5 or any(
                        s is not None and (not isinstance(s,str) or not s.strip()) for s in a):
                    raise ValueError('invalid list')
                break
            except ValueError:
                if attempt:
                    raise ValueError(f'Bad answers for {c["qid"]}: {raw}')
                messages += [dict(role='assistant',content=raw),dict(role='user',content='Return just the JSON array.')]
        pairs = {}
        rows = []
        for ans in a:
            if ans and ans.strip().lower().strip('.') not in {'unknown','unsure','not stated','not specified','n/a'}:
                if ans not in pairs:
                    query = plugir.reconstruct_dialog([c['initial'],f'{q} {ans}'])
                    if query == c['initial']:
                        worker.next_seed = 1021 + c['qid']
                        query = plugir.paraphrase(query)
                    pairs[ans] = query
                rows.append([ans,pairs[ans]])
            else:
                rows.append(None)
        answers.append(rows)
    return answers


def decide(c, annotations, score):
    assert len(annotations) == len(c['questions']) and all(len(a)==5 for a in annotations)
    texts = list(dict.fromkeys(pair[1] for a in annotations for pair in a if pair))
    top = {q:set(ids) for q,ids in zip(texts,score(texts).topk(10).indices.tolist())} if texts else {}
    scores = [sum(bool(p and i in top[p[1]]) for i,p in zip(c['ids'],row)) for row in annotations]
    known = [sum(p is not None for p in row) for row in annotations]
    winners = [j for j,s in enumerate(scores) if s==max(scores)]
    chosen = c['probe'] if c['probe'] in winners else winners[0]
    return dict(chosen=chosen,scores=scores,known=known,unique_winner=len(winners)==1)


def evaluate(cases, predictions):
    counts = Counter()
    changed = []
    for c,p in zip(cases,predictions):
        record = shared.read_json(PILOT/'cases'/f'{c["qid"]}.json')
        outcomes = {v['question']:v['rank'] for v in record['arms'].values()}
        rank = outcomes[c['questions'][p['chosen']]]
        base = record['arms']['probe']['rank']
        if p['chosen'] != c['probe']:
            changed.append(dict(qid=c['qid'],probe_rank=base,chosen_rank=rank))
        counts.update(dict(cases=1, practice_hits=rank<=10,probe_hits=base<=10,
            vanilla_hits=record['arms']['kl']['rank']<=10,random_hits=record['arms']['random']['rank']<=10,
            oracle=min(outcomes.values())<=10,gained=rank<=10<base,lost=base<=10<rank,
            no_answers=sum(p['known'])==0,no_recoveries=max(p['scores'])==0,
            informative=len(set(p['scores']))>1,only_one_question=len(p['scores'])==1,
            switched=p['chosen']!=c['probe'], practice_pairs=5*len(p['scores']),
            answered=sum(p['known']), recovered=sum(p['scores'])))
    return dict(totals=dict(counts),changed=changed)


@torch.inference_mode()
def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('--prepare-only',action='store_true')
    args.add_argument('--annotations',type=Path,help='Blinded offline answer/rewrite grid in case order')
    a=args.parse_args()
    torch.set_num_threads(4)
    OUT.mkdir(parents=True,exist_ok=True)
    score=cpu_retriever()
    manifest=OUT/'cases.json'
    cases=shared.read_json(manifest) if manifest.exists() else prepare(score)
    if a.prepare_only:
        return
    if a.annotations:
        grid=shared.read_json(a.annotations)
        assert set(grid)=={str(c['qid']) for c in cases}
        mode='offline'
    else:
        mode='qwen'
        fname=OUT/'qwen_answers.json'
        grid=shared.read_json(fname) if fname.exists() else {}
        if any(str(c['qid']) not in grid for c in cases):
            if not torch.backends.mps.is_available():
                raise SystemExit('Qwen requires Metal. Run in macOS Terminal, or use --annotations for the offline comparison.')
            worker=mac.MacQwen(SNAPSHOT,OUT)
            plugir.SEED=1021
            plugir.client=worker
            from types import SimpleNamespace
            plugir.args=SimpleNamespace(llm_backend='mlx')
            try:
                for c in cases:
                    if str(c['qid']) not in grid:
                        grid[str(c['qid'])]=infer(worker,c)
                        shared.atomic_json(fname,grid)
                        print(f'{len(grid)}/100',flush=True)
            finally:
                worker.close()
    preds=[decide(c,grid[str(c['qid'])],score) for c in cases]
    shared.atomic_json(OUT/f'{mode}_predictions.json',preds)
    result=evaluate(cases,preds)
    shared.atomic_json(OUT/f'{mode}_result.json',result)
    print(json.dumps(result['totals'],indent=2))


if __name__=='__main__':
    main()
