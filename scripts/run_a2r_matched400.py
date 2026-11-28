"""Evaluate the project's Qwen/BLIP A2R implementation on the original matched 400."""
import argparse
from contextlib import ExitStack, closing
import fcntl
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import torch

from guard_history_veto import guard_metal_timeout
import replay_batched_proxy as replay
import run_qwen_a2r as a2r
from run_matan_400 import GRID_WORKER, INPUTS, ROOT, SNAPSHOT, mac, preflight, shared

OUT = ROOT/'output/a2r-matched-400'


def validate(row, ref):
    if (row['qid'],row['target'],row['queries'][0],row['ranks'][0]) != (
            ref['qid'],ref['target'],ref['queries'][0],ref['ranks'][0]):
        raise ValueError('Mismatched archived dialogue')
    if row['arm']!=a2r.ARM or not row['hits_only'] or len(row['a2r_trace'])!=len(row['turns']):
        raise ValueError('Incomplete A2R trace')
    for t, trace in zip(row['turns'],row['a2r_trace'],strict=True):
        if (t['question']!=trace['chosen'] or len(t['pool_ids'])!=50 or
                trace['ids']!=t['selected_ids'] or len(set(trace['ids']))!=10 or
                len(trace['questions'])!=5 or len(trace['answers'])!=5 or
                any(len(answers)!=10 for answers in trace['answers'])):
            raise ValueError(f'Invalid A2R turn in dialogue {row["qid"]}')


def report(rows, vanilla, mmr):
    result = {'n':len(rows),'complete':len(rows)==400,'questions':sum(len(r['turns']) for r in rows.values())}
    for name, refs in [('vanilla',vanilla),('mmr85',mmr)]:
        result[name] = a2r.previous.paired_summary(rows,refs)
        result[name]['control_questions'] = sum(len(refs[i]['turns']) for i in rows)
    return result


@torch.inference_mode()
def run(check):
    groups,vanilla,mmr = preflight()
    tasks = {t['qid']:(t,inputs) for _,group,_,inputs in groups for t in group}
    ids = list(tasks)
    original = shared.read_json(ROOT/'output/matched-vanilla-mmr85/plan.json')['original']
    if len(ids)!=400 or sorted(ids)!=sorted(original) or set(ids)!=vanilla.keys() or set(ids)!=mmr.keys():
        raise ValueError('Expected exactly the original 400-dialogue comparison')
    sources = [Path(__file__),Path(a2r.__file__),Path(a2r.previous.__file__),GRID_WORKER,
               Path(shared.__file__),Path(mac.__file__),mac.HERE/'generate_dialog.py']
    plan = dict(ids=ids,cohorts={name:[t['qid'] for t in group] for name,group,_,_ in groups},
                corpus=50000, model=SNAPSHOT.name, rounds=10, evidence='greedy DPP 10 from current top 50',
                selector='five grid questions; maximum normalized-answer entropy with cosine softmax weights',
                controls={k:replay.proxy.digest([v[i] for i in ids]) for k,v in [('vanilla',vanilla),('mmr85',mmr)]},
                sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    hits = sum(vanilla[i]['ranks'][0]<=10 for i in ids)
    print(f'Validated the original {len(ids)} IDs; {hits} shared round-zero hits, {400-hits} need A2R.',flush=True)
    if check:
        return
    first = replay.OUT/'summary.json'
    if not first.exists() or not shared.read_json(first)['complete']:
        raise SystemExit('The batched replay must finish before A2R starts.')
    OUT.mkdir(exist_ok=True,parents=True)
    with ExitStack() as stack:
        lock = stack.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path = OUT/'plan.json'
        if path.exists() and shared.read_json(path)!=plan:
            raise ValueError('Saved A2R plan differs')
        shared.atomic_json(path,plan)
        rows = shared.read_records(OUT,a2r.ARM)
        if not rows.keys()<=tasks.keys():
            raise ValueError('Unexpected checkpoint IDs')
        for i in ids:
            if i not in rows and vanilla[i]['ranks'][0]<=10:
                rows[i] = dict(vanilla[i],arm=a2r.ARM,a2r_trace=[],execution='shared round-zero retrieval')
                shared.atomic_json(OUT/a2r.ARM/'dialogues'/f'{i}.json',rows[i])
            if i in rows:
                validate(rows[i],mmr[i])
        pipe = None
        stack.enter_context(patch.object(a2r.previous,'OUT',OUT))
        failures = {}
        for i in ids:
            if i in rows:
                continue
            if pipe is None:
                if not torch.backends.mps.is_available():
                    raise SystemExit('Launch in Mac Terminal for Metal access.')
                with patch.object(mac,'__file__',str(GRID_WORKER)):
                    worker = stack.enter_context(closing(mac.MacQwen(SNAPSHOT,OUT)))
                pipe = mac.load_blip(INPUTS,worker)
                positions = {img:j for j,img in enumerate(pipe.search_space)}
                grid = Path(stack.enter_context(TemporaryDirectory()))/'questioner-grid.png'
            task,inputs = tasks[i]
            try:
                r = a2r.run_one(pipe,task,inputs,positions[task['img']],mmr[i],grid,a2r.previous.candidate_answer)
            except a2r.ProposalShortfall as exc:
                failures[i] = str(exc)
                shared.atomic_json(OUT/'incomplete.json',failures)
                print(f'Incomplete dialogue {i}: {exc}',flush=True)
                continue
            validate(r,mmr[i])
            shared.atomic_json(OUT/a2r.ARM/'dialogues'/f'{i}.json',r)
            rows[i] = r
            shared.atomic_json(OUT/'summary.json',report(rows,vanilla,mmr))
            print(f'A2R {len(rows)}/400 saved (ID {i})',flush=True)
        shared.atomic_json(OUT/'incomplete.json',failures)
        summary = report(rows,vanilla,mmr)
        shared.atomic_json(OUT/'summary.json',summary)
        if not summary['complete']:
            raise SystemExit(f'{len(rows)}/400 complete. Missing IDs are in incomplete.json.')
        print('Cumulative Hits@10, rounds 0-10',flush=True)
        print('A2R',summary['vanilla']['variant'],'Vanilla',summary['vanilla']['control'],
              'MMR 0.85',summary['mmr85']['control'],flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    torch.set_num_threads(4)
    with guard_metal_timeout():
        run(parser.parse_args().check_only)
