"""Two paired pilots on fresh initial misses, preserving the original NN screens."""
import argparse
from collections import defaultdict
from contextlib import ExitStack, closing
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from unittest.mock import patch

import torch

import run_matan_mmr_visual as visual
from run_matan_400 import GRID_WORKER, INPUTS, REPO, ROOT, SNAPSHOT, cohorts, mac, shared

OUT = ROOT / 'output/a2r-unseen-paired-pilots'
OLD = ROOT / 'output/history-pixels-loop'
IMAGE_CONTROL = '2.2c_mmr85_images_only_5'
RULE = ('Answer the question from this one photograph. Give the shortest accurate answer, '
        'preferably one to three words. For yes/no questions use yes or no. '
        'Use unknown when the answer cannot be determined. Do not explain.')
ARMS = ('unseen', 'a2r')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def label(text):
    value = re.sub(r'[^\w\s]', ' ', text.lower()).split()
    if value[:1] in (['a'], ['an'], ['the']):
        value = value[1:]
    answer = ' '.join(value)
    return {'grey': 'gray', 'unsure': 'unknown', 'not sure': 'unknown',
            'cannot tell': 'unknown', '': 'unknown'}.get(answer, answer)


def information_gain(answers, weights):
    # Under one predicted answer per candidate, mutual information is answer entropy.
    buckets = defaultdict(float)
    for answer, weight in zip(answers, weights, strict=True):
        buckets[label(answer)] += weight
    return -math.fsum(mass * math.log2(mass) for mass in sorted(buckets.values()) if mass > 0)


def paired_summary(rows, refs):
    def first(row):
        return next((r for r, rank in enumerate(row['ranks']) if rank <= 10), 11)

    pairs = {i: [first(refs[i]), first(row)] for i, row in rows.items()}
    def curve(side):
        return [sum(pair[side] <= r for pair in pairs.values()) for r in range(11)]
    control, variant = curve(0), curve(1)
    return dict(n=len(pairs), first_hit_control_variant=pairs, control=control, variant=variant,
                early_delta_pp=100 * (sum(variant[1:6])-sum(control[1:6])) / (5*len(pairs)),
                gained=[i for i,(a,b) in pairs.items() if b<=10<a],
                lost=[i for i,(a,b) in pairs.items() if a<=10<b],
                earlier=sum(b<a for a,b in pairs.values()), later=sum(b>a for a,b in pairs.values()),
                tied=sum(a==b for a,b in pairs.values()))


def prepare():
    split = shared.read_json(OLD/'split.json')
    used = set(split['development_ids'] + split['confirmation_ids'] + split['old_visual_ids_excluded_from_confirmation'])
    tasks, caps = {}, {}
    for _, group, records, root in cohorts():
        for task in group:
            i = task['qid']
            tasks[i] = (task, root)
            caps[i] = records[i]
    imgs = shared.read_records(visual.OUT, IMAGE_CONTROL)
    assert len(tasks) == 400 and tasks.keys() == imgs.keys()
    for i, ref in caps.items():
        assert (ref['target'],ref['queries'][0],ref['ranks'][0]) == (
            imgs[i]['target'],imgs[i]['queries'][0],imgs[i]['ranks'][0])
    ids = sorted(i for i in tasks if caps[i]['ranks'][0]>10 and i not in used)
    assert 10 <= len(ids) <= 20
    refs = {'unseen': caps, 'a2r': imgs}
    sources = [Path(__file__), Path(shared.__file__), Path(mac.__file__), GRID_WORKER,
               Path(visual.__file__), Path(visual.images.__file__), mac.HERE/'generate_dialog.py']
    plan = dict(ids=ids, selection='all initial misses outside listed prior pilot IDs',
                excluded=sorted(used), corpus=50000, mmr=.85, model=SNAPSHOT.name, max_questions=10,
                display='original current-query NN top ten, repeats permitted',
                primary='mean cumulative Hits@10 R1-5; report all rounds and check final R10',
                unseen='exclude union of displayed IDs including current failed screen from evidence, refill pool',
                a2r='2.2c grid proposals, independent VQA per distinct proposal/image, normalized exact answers; '
                    'softmax of raw current-query cosine scores (temperature 1), maximum bucket entropy, '
                    'first proposal on ties, unknown is one shared answer bucket',
                source={p.name:digest(p.read_bytes()) for p in sources},
                controls={a:{str(i):digest(json.dumps(refs[a][i],sort_keys=True).encode()) for i in ids} for a in ARMS})
    path = OUT/'plan.json'
    if path.exists() and shared.read_json(path) != plan:
        raise RuntimeError('Existing checkpoints have a different source, sample or configuration.')
    shared.atomic_json(path, plan)
    return ids,tasks,refs


def candidate_answer(pipe, image_id, question):
    name = pipe.search_space[image_id]
    key = [SNAPSHOT.name, digest(GRID_WORKER.read_bytes()), RULE, 24, name, question]
    path = OUT/'answers'/(digest(json.dumps(key).encode())+'.json')
    if path.exists():
        record = shared.read_json(path)
        assert record['key'] == key
        return record['raw']
    messages = [dict(role='system', content=RULE), dict(role='user', content=question)]
    raw = pipe.client.complete(messages,24,image=REPO/'data'/name)
    shared.atomic_json(path, dict(key=key,raw=raw))
    return raw


def run_dialogue(arm, pipe, task, root, ref, target_id, grid):
    seen, evidence, audit = set(), [], []
    shared.ROOT = root
    pipe.client.next_seed = 1021 + task['qid']
    with ExitStack() as stack:
        if arm == 'unseen':
            def ranking(_arm, queries, history, order, budget):
                seen.update(order[:10].tolist())
                head = order[:budget+len(seen)].tolist()
                pool = order.new_tensor([i for i in head if i not in seen][:budget])
                assert len(pool)==budget
                return order, pool

            def representatives(_arm, pool, ranks, vectors, cluster):
                ids = shared.history_mmr(pool, ranks, vectors, count=10, weight=.85)
                assert seen.isdisjoint(ids)
                audit.append(dict(seen=sorted(seen),evidence=ids))
                return ids

            stack.enter_context(patch.object(shared,'rankings',ranking))
            stack.enter_context(patch.object(shared,'representatives',representatives))
        else:
            visual.install_arm(stack,pipe,grid,visual.images.install_images_only)
            render = shared.representatives
            def capture(*args):
                evidence[:] = render(*args)
                return evidence.copy()
            stack.enter_context(patch.object(shared,'representatives',capture))
            kl = pipe.select_question
            def select(**kwargs):
                qs = kwargs['questions']
                assert len(qs)==5 and len(set(evidence))==10
                default = kl(**kwargs)
                weights = kwargs['cossim_prev'][evidence].detach().cpu().double().softmax(0).tolist()
                answers = {q:[candidate_answer(pipe,i,q) for i in evidence] for q in dict.fromkeys(qs)}
                gains = [information_gain(answers[q],weights) for q in qs]
                chosen = qs[max(range(len(qs)),key=gains.__getitem__)]
                audit.append(dict(evidence=evidence.copy(),questions=qs,answers=answers,weights=weights,
                                  information_gain_bits=gains,kl=default,selected=chosen))
                return chosen
            stack.enter_context(patch.object(pipe,'select_question',select))
        row = shared.run_dialogue(pipe,'s3_current_mmr',task,target_id,hits_only=True)
    assert row['ranks'][0]==ref['ranks'][0] and len(row['turns'])==len(audit)
    if arm=='a2r':
        for t, saved, check in zip(row['turns'],ref['turns'],audit):
            for field in ('query_before','selected_ids','candidate_questions'):
                assert t[field]==saved[field], (task['qid'],field)
            assert check['kl']==saved['question']
            if t['question']!=saved['question']:
                break
            assert (t['answer'],t['query_after'])==(saved['answer'],saved['query_after'])
    row.update(arm=arm,audit=audit)
    return row


@torch.inference_mode()
def main(check_only):
    OUT.mkdir(exist_ok=True, parents=True)
    with ExitStack() as stack:
        lock=stack.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        ids,tasks,refs=prepare()
        print('Verified matched cohort:',ids,flush=True)
        if check_only:
            return
        pipe=None
        for arm in ARMS:
            rows=shared.read_records(OUT,arm)
            assert rows.keys() <= set(ids)
            for i in ids:
                if i in rows:
                    continue
                if pipe is None:
                    if not torch.backends.mps.is_available():
                        raise SystemExit('Metal unavailable here. Run the pilot launcher in Mac Terminal.')
                    with patch.object(mac,'__file__',str(GRID_WORKER)):
                        worker=stack.enter_context(closing(mac.MacQwen(SNAPSHOT,OUT)))
                    pipe=mac.load_blip(INPUTS,worker)
                    positions={name:j for j,name in enumerate(pipe.search_space)}
                    grid=Path(stack.enter_context(TemporaryDirectory()))/'questioner-grid.png'
                task,root=tasks[i]
                row=run_dialogue(arm,pipe,task,root,refs[arm][i],positions[task['img']],grid)
                shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json',row)
                rows[i]=row
                print(f'{arm}: {len(rows)}/{len(ids)}',flush=True)
            summary=paired_summary(rows,refs[arm])
            if arm=='a2r':
                summary['versus_mmr_captions']=paired_summary(rows,refs['unseen'])
            shared.atomic_json(OUT/arm/'summary.json',summary)
            print(arm,summary,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    torch.set_num_threads(4)
    main(parser.parse_args().check_only)
