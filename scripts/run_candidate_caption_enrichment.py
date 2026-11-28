"""Compare one per-photo visual detail sentence with the archived candidate captions."""
import argparse
from contextlib import ExitStack, closing
import fcntl
from functools import partial
import hashlib
from pathlib import Path
from unittest.mock import patch

import torch
from torch.nn.functional import normalize

from guard_history_veto import guard_metal_timeout
from run_a2r_unseen_pilots import paired_summary
from run_matan_400 import GRID_WORKER, INPUTS, REPO, ROOT, SNAPSHOT, mac, preflight, shared
from run_proxy_retrieval import WORKER, digest, isolated_rng

OUT = ROOT/'output/candidate-caption-enrichment'
ARMS = {'mmr85_pixels': ('s3_current_mmr','pixels'), 'vanilla_pixels': ('vanilla','pixels'),
        'mmr85_text': ('s3_current_mmr','text'), 'vanilla_text': ('vanilla','text')}
PROMPTS = {
    'pixels': 'Inspect the photograph. Complement its caption with one short factual sentence '
              'of up to 25 words about clearly visible, specific details useful for distinguishing '
              'similar photographs. Prefer details missing from the caption. Do not speculate. '
              'Return only the sentence.',
    'text': 'Read the caption. Write one short factual sentence of up to 25 words restating its '
            'specific details useful for distinguishing similar photographs. Use only what the '
            'caption establishes. Do not invent additional facts. Return only the sentence.'}


def own_rank(pipe, caption, idx):
    vector = normalize(pipe.get_text_features(caption), dim=-1)
    order = (vector @ pipe.img_embs.T).flatten().argsort().flip(0)
    return int(shared.inverse_ranks(order)[idx])


def detail(pipe, mode, idx, cache):
    key = (mode,idx)
    if key in cache:
        return cache[key]
    path = OUT/'captions'/mode/f'{idx}.json'
    name = pipe.search_space[idx]
    caption = pipe.captions[idx]['caption'][0]
    if path.exists():
        row = shared.read_json(path)
        if (row['photo'],row['original']) != (name,caption):
            raise ValueError(f'Stale cached evidence at {path}')
    else:
        pipe.client.next_seed = 5787000 + idx
        extra = pipe.client.complete([dict(role='system',content=PROMPTS[mode]),
                                      dict(role='user',content=caption)], 72,
                                     image=REPO/'data'/name if mode=='pixels' else None)
        extra = ' '.join(extra.split())
        if not extra:
            raise ValueError(f'Empty supplemental sentence for {name}')
        enriched = f'{caption} {extra}'
        row = dict(photo=name,original=caption,extra=extra,enriched=enriched,
                   original_rank=own_rank(pipe,caption,idx),enriched_rank=own_rank(pipe,enriched,idx))
        shared.atomic_json(path,row)
    cache[key] = row
    return row


def play(pipe, arm, task, inputs, target, cache):
    method,mode = ARMS[arm]
    selected = []
    additions = []
    represent,generate = shared.representatives,shared.question_candidates

    def representatives(*args):
        selected[:] = represent(*args)
        return selected.copy()

    def proposals(model, history, query, captions):
        if not 1<=len(selected)<=10 or captions != [model.captions[i]['caption'][0] for i in selected]:
            raise ValueError('Evidence is not aligned with candidate images')
        # Side calls must not advance the original question/answer sampling stream.
        with isolated_rng(model.client):
            records = [detail(model,mode,i,cache) for i in selected]
        additions.append([r['extra'] for r in records])
        return generate(model,history,query,[r['enriched'] for r in records])

    pipe.client.next_seed = 1021 + task['qid']
    with patch.object(shared,'ROOT',inputs), patch.object(shared,'representatives',representatives), patch.object(
            shared,'question_candidates',proposals):
        row = shared.run_dialogue(pipe,method,task,target,hits_only=True)
    return dict(row,arm=arm,added_sentences=additions)


def validate(row, ref):
    if (row['qid'],row['target'],row['queries'][0],row['ranks'][0]) != (
            ref['qid'],ref['target'],ref['queries'][0],ref['ranks'][0]):
        raise ValueError('Mismatched starting state')
    if row['turns'] and row['turns'][0]['selected_ids'] != ref['turns'][0]['selected_ids']:
        raise ValueError(f'First-round evidence drift for dialogue {row["qid"]}')
    for turn,extras in zip(row['turns'],row['added_sentences'],strict=True):
        if len(turn['candidate_questions'])!=5 or len(extras)!=len(turn['selected_ids']) or not all(extras):
            raise ValueError('Incomplete enrichment record')


def comparison(data, refs, ids):
    rows = {i:data[i] for i in ids if i in data and i in refs}
    pair = paired_summary(rows,refs) if rows else {}
    return dict(planned=len(ids),completed=len(rows),paired=pair,
                questions=sum(len(r['turns']) for r in rows.values()),
                reference_questions=sum(len(refs[i]['turns']) for i in rows))


def summary(rows, refs, ids, splits, pilot):
    result = {}
    for arm,(method,mode) in ARMS.items():
        data,base = rows[arm],refs[method]
        group = ids if mode=='pixels' else pilot
        result[arm] = dict(vs_archived=comparison(data,base,group),
                           pilot_halves=[comparison(data,base,half) for half in splits])
        if mode=='pixels':
            result[arm]['outside_pilot'] = comparison(data,base,[i for i in ids if i not in pilot])
            if method=='vanilla':
                result[arm]['vs_plain_mmr85'] = comparison(data,refs['s3_current_mmr'],ids)
            text_rows = rows[arm.replace('_pixels','_text')]
            result[arm]['vs_text_on_pilot'] = comparison(data,text_rows,pilot)
    return result


def rank_check(refs, pilot):
    # First-round evidence is fixed before any arm has influenced the dialogue.
    ids = {k for base in refs.values() for i in pilot for k in base[i]['turns'][0]['selected_ids']}
    stats = {'unique_pilot_images':len(ids)}
    for mode in PROMPTS:
        records = [shared.read_json(OUT/'captions'/mode/f'{k}.json') for k in ids]
        stats[mode] = dict(original_top10=sum(r['original_rank']<=10 for r in records),
                           enriched_top10=sum(r['enriched_rank']<=10 for r in records),
                           improved=sum(r['enriched_rank']<r['original_rank'] for r in records),
                           worsened=sum(r['enriched_rank']>r['original_rank'] for r in records))
    return stats


@torch.inference_mode()
def run(check, pilot_only):
    groups,vanilla,mmr = preflight()
    refs = {'s3_current_mmr':mmr,'vanilla':vanilla}
    tasks = {t['qid']:(t,folder) for _,group,_,folder in groups for t in group}
    ids = list(tasks)
    splits = shared.read_json(ROOT/'output/mmr-within-round-questions/plan.json')['splits']
    pilot = [i for group in splits for i in group]
    if (len(ids)!=400 or list(map(len,splits))!=[20,20] or len(set(pilot))!=40 or
            any(vanilla[i]['ranks'][0]<=10 for i in pilot)):
        raise ValueError('Unexpected cohort')
    files = [Path(__file__),WORKER,GRID_WORKER,Path(shared.__file__),Path(mac.__file__),mac.HERE/'generate_dialog.py']
    plan = dict(ids=ids,splits=splits,arms=ARMS,prompts=PROMPTS,model=SNAPSHOT.name,corpus=50000,
                metric='paired cumulative Hits@10 rounds 0-10; questions through first hit capped at ten',
                controls={method:digest([data[i] for i in ids]) for method,data in refs.items()},
                sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
    # Convert tuples to the JSON representation before comparing a saved plan.
    plan['arms'] = {arm:list(spec) for arm,spec in ARMS.items()}
    print('Verified 400 matched IDs, 290 initial hits, two pilot groups of 20 initial misses.',flush=True)
    if check:
        return
    OUT.mkdir(parents=True,exist_ok=True)
    with ExitStack() as stack:
        lock = stack.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path = OUT/'plan.json'
        if path.exists() and shared.read_json(path)!=plan:
            raise ValueError('Existing run uses another plan')
        shared.atomic_json(path,plan)
        rows = {arm:shared.read_records(OUT,arm) for arm in ARMS}
        for arm,(method,mode) in ARMS.items():
            allowed = ids if mode=='pixels' else pilot
            if not rows[arm].keys()<=set(allowed):
                raise ValueError(f'Unexpected saved IDs for {arm}')
            for i,r in rows[arm].items():
                validate(r,refs[method][i])
        stack.enter_context(patch.object(shared,'history_mmr',partial(shared.history_mmr,weight=.85)))
        pipe = None
        cache = {}
        order = pilot + ([] if pilot_only else [i for i in ids if i not in pilot])
        for i in order:
            for arm,(method,mode) in ARMS.items():
                if i in rows[arm] or (mode=='text' and i not in pilot):
                    continue
                ref = refs[method][i]
                if not ref['turns']:
                    row = dict(ref,arm=arm,added_sentences=[],execution='shared initial retrieval')
                else:
                    if pipe is None:
                        if not torch.backends.mps.is_available():
                            raise SystemExit('Launch in Mac Terminal for Metal access.')
                        with patch.object(mac,'__file__',str(WORKER)):
                            worker = stack.enter_context(closing(mac.MacQwen(SNAPSHOT,OUT)))
                        pipe = mac.load_blip(INPUTS,worker)
                        positions = {img:j for j,img in enumerate(pipe.search_space)}
                    task,inputs = tasks[i]
                    row = play(pipe,arm,task,inputs,positions[task['img']],cache)
                validate(row,ref)
                shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json',row)
                rows[arm][i] = row
                print(f'{arm}: {len(rows[arm])} saved (ID {i})',flush=True)
            if i==pilot[-1]:
                shared.atomic_json(OUT/'own-image-retrieval.json',rank_check(refs,pilot))
            shared.atomic_json(OUT/'summary.json',summary(rows,refs,ids,splits,pilot))
        print('Completed requested cohort. Matched curves and question counts are in',OUT/'summary.json',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--pilot-only',action='store_true')
    args=parser.parse_args()
    torch.set_num_threads(4)
    with guard_metal_timeout():
        run(args.check_only,args.pilot_only)
