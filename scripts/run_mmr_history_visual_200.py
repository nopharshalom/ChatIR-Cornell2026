"""Matched MMR weight, top-ten display history, and first-question visual probe tests."""
import argparse
import fcntl
import hashlib
from pathlib import Path
from unittest.mock import patch

import torch

from run_overnight_hits import INPUTS, OVERNIGHT_OUTPUT, REPO, ROOT, SNAPSHOT, mac, shared, summarize

OUT = ROOT / 'output/agentir-mmr-history-visual-200'
ARMS = {'mmr85': .85, 'mmr50': .50, 'visual_probe_mmr70': .70}
BASE = 's3_current_mmr'


def load_plan():
    tasks = shared.read_json(OVERNIGHT_OUTPUT / 'plan.json')['fresh'][:200]
    ids = [t['qid'] for t in tasks]
    assert len(set(ids)) == len(ids) == 200
    controls = {}
    for arm in ('vanilla', BASE, 's2_history_pool'):
        data = shared.read_records(OVERNIGHT_OUTPUT, arm)
        controls[arm] = {i: data[i] for i in ids}
        for t in tasks:
            r = data[t['qid']]
            assert r['target'] == t['img'] and r['queries'][0] == t['caption']
            assert r['ranks'][0] == controls['vanilla'][t['qid']]['ranks'][0]
    plan = dict(tasks=tasks, arms=ARMS, corpus=50000, rounds=10,
                source=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                compare_to=list(controls), proxy_ranks=[11, 21, 31, 41])
    p = OUT / 'plan.json'
    if p.exists() and shared.read_json(p) != plan:
        raise ValueError('Plan mismatch: use a new output directory for a changed experiment.')
    shared.atomic_json(p, plan)
    return tasks, controls


def fill_screen(order, seen, votes=None):
    keep = [i for i in order[:10] if i not in seen]
    eligible = [i for i in order[10:] if i not in seen]
    if votes is not None:
        # Use only currently plausible alternatives. Stable ties favor current rank.
        group = set(order[:100])
        head = [i for i in eligible if i in group]
        eligible = sorted(head, key=lambda i: -votes.get(i, 0.)) + [i for i in eligible if i not in group]
    screen = keep + eligible[:10-len(keep)]
    assert len(set(screen)) == 10 and not seen.intersection(screen)
    return screen


def history_replay(tasks, controls):
    from run_probe_pool_sweep import cpu_retriever
    corpus = shared.read_json(INPUTS / 'Protocol/Search_Space_val_50k.json')
    pos = {n:i for i,n in enumerate(corpus)}
    cache = {}
    score = None
    output = {}
    for arm in ('vanilla', BASE):
        points = []
        for task in tasks:
            row = controls[arm][task['qid']]
            target = pos[row['target']]
            stop = next((r for r,k in enumerate(row['ranks']) if k <= 10), 11)
            times = dict(current=11, history=11)
            if stop == 0:
                times = dict(current=0, history=0)
            else:
                seen = {k:set() for k in times}
                votes, distinct = {}, set()
                for r,query in enumerate(row['queries']):
                    if all(t <= 10 for t in times.values()):
                        break
                    prefix = row['turns'][r]['pool_ids'] if r < len(row['turns']) else []
                    # The longest required list is 110: at most 100 prior displays.
                    order = prefix[:110]
                    if len(order) < 110:
                        if query not in cache:
                            if score is None:
                                score = cpu_retriever()
                            cache[query] = score([query])[0].argsort(descending=True)[:250].tolist()
                        fixed = set(order)
                        order = (order + [i for i in cache[query] if i not in fixed])[:110]
                    assert (target in order[:10]) == (row['ranks'][r] <= 10), (task['qid'],r)
                    if query not in distinct:
                        distinct.add(query)
                        for rank,i in enumerate(order[:100],1):
                            votes[i] = votes.get(i,0.) + 1/(60+rank)
                    for key in times:
                        if times[key] != 11:
                            continue
                        screen = fill_screen(order,seen[key],votes if key=='history' else None)
                        if target in screen:
                            times[key] = r
                        seen[key].update(screen)
                assert all(t <= stop for t in times.values())
            points.append(dict(qid=task['qid'], baseline=stop, **times))
        output[arm] = dict(rows=points, counts={
            k:[sum(p[k]<=r for p in points) for r in range(11)]
            for k in ('baseline','current','history')})
    shared.atomic_json(OUT/'history_replay.json',output)
    for arm, result in output.items():
        print(arm,result['counts'],flush=True)


def visual_choice(pipe, task, baseline, reserved):
    turn = baseline['turns'][0]
    initial = task['caption']
    candidates = list(dict.fromkeys([turn['question'], *turn['candidate_questions']]))
    # Draw from four depths outside the initial display; exclude every evaluation image.
    eligible = [i for i in turn['pool_ids'][10:] if pipe.search_space[i] not in reserved]
    proxies = [eligible[j] for j in (0,10,20,30)]
    trials = {}
    for question in candidates:
        answers = [pipe.client.answer(REPO/'data'/pipe.search_space[i], question) for i in proxies]
        rewrites = {a:pipe.reconstruct_dialog([initial, f'{question} {a}']) for a in dict.fromkeys(answers)}
        from run_five_question_probes import Search
        ranks = Search(pipe).similarity([rewrites[a] for a in answers],'proxy').argsort(descending=True)
        positions = [int(shared.inverse_ranks(order)[idx]) for order,idx in zip(ranks,proxies)]
        trials[question] = dict(answers=answers, rewrites=rewrites, ranks=positions,
                                hits=sum(n<=10 for n in positions))
    # Only override the default question for a strictly higher practice Hits@10.
    chosen = max(candidates,key=lambda q:trials[q]['hits'])
    return dict(chosen=chosen, default=turn['question'], ids=proxies, trials=trials)


def run_queue(tasks, controls):
    if not torch.backends.mps.is_available():
        raise SystemExit('The inference queue requires Metal. Open Run_AgentIR_Overnight.command on the Mac.')
    reserved = {t['img'] for t in shared.read_json(REPO/'dialogues/VisDial_v1.0_queries_val.json')}
    history_mmr = shared.history_mmr
    worker = mac.MacQwen(SNAPSHOT,OUT)
    try:
        pipe = mac.load_blip(INPUTS,worker)
        shared.ROOT = OVERNIGHT_OUTPUT/'fresh-inputs'
        pos = {n:i for i,n in enumerate(pipe.search_space)}
        assert all((shared.ROOT/'targets'/t['img']).is_file() for t in tasks)
        for arm,weight in ARMS.items():
            records = shared.read_records(OUT,arm)
            for index,task in enumerate(tasks,1):
                i = task['qid']
                ref = controls[BASE][i]
                if i in records:
                    continue
                probe = None
                if ref['turns'] and arm == 'visual_probe_mmr70':
                    path = OUT/'practice'/f'{i}.json'
                    if not path.exists():
                        worker.next_seed = 1021 + i
                        shared.atomic_json(path,visual_choice(pipe,task,ref,reserved))
                    probe = shared.read_json(path)
                if not ref['turns'] or (probe and probe['chosen'] == probe['default']):
                    row = dict(ref)
                else:
                    select = pipe.select_question
                    def choose(**kw):
                        if probe and kw['round'] == 1:
                            assert kw['questions'] == ref['turns'][0]['candidate_questions']
                            # Consume the same baseline question-selection calls for paired RNG.
                            assert select(**kw) == probe['default']
                            return probe['chosen']
                        return select(**kw)
                    def mmr(pool,ranks,embs):
                        return history_mmr(pool,ranks,embs,weight=weight)
                    worker.next_seed = 1021 + i
                    with patch.object(shared,'history_mmr',mmr), patch.object(pipe,'select_question',choose):
                        row = shared.run_dialogue(pipe,BASE,task,pos[task['img']],hits_only=True)
                assert row['ranks'][0] == ref['ranks'][0] and row['target'] == task['img']
                row['arm'] = arm
                shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json',row)
                records[i] = row
                print(f'{arm} {index}/200, qid {i}, ranks {row["ranks"]}',flush=True)
            shared.atomic_json(OUT/arm/'summary.json',{name:summarize(records,base) for name,base in controls.items()})
    finally:
        worker.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history-only',action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    with (OUT/'run.lock').open('a') as lock, torch.inference_mode():
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        tasks,controls = load_plan()
        torch.set_num_threads(4)
        if not (OUT/'history_replay.json').exists():
            history_replay(tasks,controls)
        if not args.history_only:
            run_queue(tasks,controls)
