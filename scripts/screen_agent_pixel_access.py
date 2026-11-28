"""Compare matched caption and pixel reviews of the same first-round question slate."""
import argparse
from collections import Counter
from contextlib import closing
import fcntl
import hashlib
import json
import re
from pathlib import Path
from unittest.mock import patch

from guard_history_veto import guard_metal_timeout
from run_matan_400 import GRID_WORKER, INPUTS, REPO, ROOT, SNAPSHOT, mac, shared

OUT = ROOT / 'output/mmr-agent-pixel-access-screen'
SCREEN = ROOT / 'output/mmr-first-round-question-screen'
REF = ROOT / 'output/mmr-within-round-questions/retry_fix_unique/dialogues'
ARCHIVE = ROOT / 'output/agentir-hundred-probe/cases'
PLAN = """Choose a useful question for someone looking at their own photograph. The aim is for
their answer to add a retrievable visual detail to the current description. You may keep the KL
choice. Use the historical examples as illustrations only. Give a provisional question index,
one specific detail worth checking to distinguish the proposed questions, and the indices of two
different evidence photos to inspect. All indices start at zero. Return only JSON:
{"choice":0,"check":"short factual check","photos":[0,1]}."""
OBSERVE = """Check only the requested detail for this one candidate. Use its caption and its
photo if supplied. Report concrete visible or explicitly described facts relevant to the check.
If the detail cannot be established, say unknown. One sentence. Do not recommend a question."""
DECIDE = """Choose one of the indexed questions to ask the searcher about their own photograph.
Use the current description, candidate evidence and inspection notes. Prefer a question whose
answer can add a concrete distinguishing detail to the search description. Keep the provisional
choice if the evidence does not support changing it. Return only JSON: {"choice":0}."""
STOP = set('a an the above next near and of on to is are with in at from for by an there'.split())


def tokens(text):
    return set(re.findall('[a-z0-9]+', text.lower())) - STOP


def examples():
    bank = []
    for path in sorted(ARCHIVE.glob('*.json')):
        row = shared.read_json(path)
        labels = {v['question']: v['rank'] <= 10 for v in row['arms'].values()}
        if len(set(labels.values())) == 2:
            bank.append((row['qid'], tokens(row['initial']),
                         dict(description=row['initial'], question_outcomes=labels)))
    return bank


def historical_examples(query, bank):
    words = tokens(query)
    def similarity(item):
        return len(words & item[1]) / max(1, len(words | item[1]))
    return sorted(bank, key=lambda item: (-similarity(item), item[0]))[:2]


def call(worker, instruction, data, budget, image=None):
    return worker.complete([dict(role='system', content=instruction),
                            dict(role='user', content=json.dumps(data))],
                           budget, temperature=0, image=image)


def parse(text):
    body = text.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
    try:
        value = json.loads(body)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def valid_index(value, size):
    return type(value) is int and 0 <= value < size


def choose(worker, data, photos):
    raw = call(worker, PLAN, data, 200)
    plan = parse(raw)
    initial, check, ids = (plan.get(k) for k in ('choice','check','photos'))
    if not valid_index(initial, len(data['questions'])):
        initial = data['kl']
    valid = (isinstance(check, str) and check.strip() and isinstance(ids,list) and len(ids)==2
             and all(valid_index(i,len(photos)) for i in ids) and ids[0]!=ids[1])
    record = dict(plan_raw=raw, provisional=initial, valid_plan=bool(valid))
    for arm in ('caption', 'pixels'):
        if not valid:
            record[arm] = dict(choice=initial, fallback=True, observations=[])
            continue
        notes = []
        for i in ids:
            obs = call(worker, OBSERVE, dict(check=check, caption=data['captions'][i]), 70,
                       photos[i] if arm=='pixels' else None)
            notes.append(dict(index=i, observation=obs))
        decision = call(worker, DECIDE, dict(**data, provisional=initial, check=check, notes=notes), 48)
        choice = parse(decision).get('choice')
        ok = valid_index(choice,len(data['questions']))
        record[arm] = dict(choice=choice if ok else initial, fallback=not ok,
                           observations=notes, raw=decision)
    return record


def prepare():
    splits = shared.read_json(SCREEN/'plan.json')['splits']
    ids = [i for split in splits for i in split]
    bank = examples()
    corpus = shared.read_json(INPUTS/'Protocol/Search_Space_val_50k.json')
    historical_ids = {int(p.stem) for p in ARCHIVE.glob('*.json')}
    assert len(corpus)==50000 and len(set(ids))==len(ids)==40 and list(map(len,splits))==[20,20]
    assert len(historical_ids)==100 and not historical_ids.intersection(ids) and len(bank)>=2
    tasks, outcomes, target_in_evidence = {}, {}, []
    for i in ids:
        ref = shared.read_json(REF/f'{i}.json')
        screen = shared.read_json(SCREEN/'cases'/f'{i}.json')
        t = ref['turns'][0]
        qs = t['candidate_questions']
        assert ref['qid']==screen['qid']==i and t['question']==screen['selection']['kl']
        assert list(screen['outcomes'])==qs and ref['ranks'][0]>10
        names = [corpus[k] for k in t['selected_ids']]
        assert len(set(names))==len(names)==len(t['selected_captions'])==10
        assert all((REPO/'data'/name).is_file() for name in names)
        if ref['target'] in names:
            target_in_evidence.append(i)
        demo = historical_examples(t['query_before'],bank)
        tasks[i] = dict(data=dict(description=t['query_before'], questions=qs,
                                  kl=qs.index(t['question']), captions=t['selected_captions'],
                                  historical_examples=[d[2] for d in demo]),
                        photos=names, example_ids=[d[0] for d in demo])
        outcomes[i] = [screen['outcomes'][q]['rank'] for q in qs]
    return splits, tasks, outcomes, target_in_evidence


def summarize(ids, records, tasks, outcomes):
    counts = Counter()
    comparisons = {key:dict(changed=0, gains=[], losses=[]) for key in
                   ('caption_vs_kl','pixels_vs_caption','pixels_vs_kl')}
    for i in ids:
        r = records[i]
        choices = dict(kl=tasks[i]['data']['kl'],caption=r['caption']['choice'],pixels=r['pixels']['choice'])
        hits = {arm:outcomes[i][q]<=10 for arm,q in choices.items()}
        counts.update({arm:int(hit) for arm,hit in hits.items()})
        for key,entry in comparisons.items():
            arm, control = key.split('_vs_')
            entry['changed'] += choices[arm]!=choices[control]
            if hits[arm] and not hits[control]:
                entry['gains'].append(i)
            if hits[control] and not hits[arm]:
                entry['losses'].append(i)
    return dict(n=len(ids),hits=dict(counts),paired=comparisons,
                invalid_plans=sum(not records[i]['valid_plan'] for i in ids),
                fallbacks={k:sum(records[i][k]['fallback'] for i in ids) for k in ('caption','pixels')})


def run(check):
    splits,tasks,outcomes,overlap = prepare()
    ids = list(tasks)
    def digest(value):
        return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
    plan = dict(splits=splits,snapshot=SNAPSHOT.name,inputs=digest(tasks),outcomes=digest(outcomes),
                code={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (Path(__file__),GRID_WORKER,Path(mac.__file__))},
                target_among_ten=overlap)
    print(f'Validated {len(ids)} slates and {sum(map(len,outcomes.values()))} question outcomes. '
          f'Target incidentally retrieved among the ten for {len(overlap)} dialogue(s).',flush=True)
    if check:
        return
    OUT.mkdir(exist_ok=True,parents=True)
    with (OUT/'run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        path=OUT/'plan.json'
        if path.exists() and shared.read_json(path)!=plan:
            raise ValueError('Checkpoints belong to a different protocol')
        shared.atomic_json(path,plan)
        records={}
        for path in (OUT/'cases').glob('*.json'):
            i=int(path.stem)
            r=shared.read_json(path)
            if (i not in tasks or r['qid']!=i or any(not valid_index(r[k]['choice'],len(outcomes[i]))
                                                     for k in ('caption','pixels'))):
                raise ValueError(f'Invalid checkpoint {path}')
            records[i]=r
        if len(records)<len(ids):
            with patch.object(mac,'__file__',str(GRID_WORKER)):
                client=mac.MacQwen(SNAPSHOT,OUT)
            with closing(client):
                for i in ids:
                    if i in records:
                        continue
                    task=tasks[i]
                    row=choose(client,task['data'],[REPO/'data'/p for p in task['photos']])
                    row.update(qid=i,example_ids=task['example_ids'])
                    shared.atomic_json(OUT/'cases'/f'{i}.json',row)
                    records[i]=row
                    print(f'{len(records)}/40 saved',flush=True)
        result=dict(all=summarize(ids,records,tasks,outcomes),
                    groups=[summarize(g,records,tasks,outcomes) for g in splits],
                    target_outside_evidence=summarize([i for i in ids if i not in overlap],records,tasks,outcomes))
        shared.atomic_json(OUT/'summary.json',result)
        print(json.dumps(result['all'],indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    with guard_metal_timeout():
        run(parser.parse_args().check_only)
