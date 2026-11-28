"""Finish enrichment, then evaluate follow-up and enriched question selection on the same 400."""
import argparse
from contextlib import ExitStack, closing
import fcntl
from functools import partial
from pathlib import Path
from unittest.mock import patch

import torch

import run_caption_split_selector as selector
import run_fifth_question_pilots as fifth
from guard_history_veto import guard_metal_timeout
from run_candidate_caption_enrichment import OUT as ENRICH, comparison
from run_matan_400 import INPUTS, ROOT, SNAPSHOT, mac, preflight, shared
from run_proxy_retrieval import WORKER, digest
from run_six_then_enrichment import stage

OUT = ROOT/'output/overnight-question-400'
POLICIES = {'followup': (fifth, 'proposal_trace'),
            'pixel_enriched_captions': (selector, 'selection_trace')}
METHODS = {'mmr85':'s3_current_mmr', 'vanilla':'vanilla'}


def cohort(policy):
    groups, vanilla, mmr = preflight()
    tasks = {t['qid']:(t,folder) for _,group,_,folder in groups for t in group}
    manifest = shared.read_json(ENRICH/'plan.json')
    ids = manifest['ids']
    pilots = [i for half in manifest['splits'] for i in half]
    module, _ = POLICIES[policy]
    pilot_plan = shared.read_json(module.OUT/'plan.json')
    reused = shared.read_records(module.OUT,policy)
    if (len(ids)!=400 or len(set(ids))!=400 or set(ids)!=tasks.keys()
            or pilot_plan['splits']!=manifest['splits'] or len(pilots)!=40 or set(pilots)!=reused.keys()
            or pilot_plan['controls']!=digest([mmr[i] for i in pilots])):
        raise ValueError('Cohort or MMR pilot mismatch')
    for i,row in reused.items():
        module.validate(row,mmr[i])
    refs = {'mmr85':mmr,'vanilla':vanilla}
    for method, data in refs.items():
        if digest([data[i] for i in ids])!=manifest['controls'][METHODS[method]]:
            raise ValueError(f'{method}: baseline differs from the enrichment run')
    return tasks,ids,reused,refs


def play(policy, method, pipe, task, folder, target, ref, cache):
    # Both existing pilot implementations install their question hooks around this
    # entry point. Dispatch only the evidence method to run their exact policy on vanilla too.
    run_dialogue = shared.run_dialogue
    def run_method(model, pilot_method, task, target_id, hits_only):
        if pilot_method!='s3_current_mmr':
            raise ValueError(f'Unexpected pilot evidence method: {pilot_method}')
        return run_dialogue(model,METHODS[method],task,target_id,hits_only=hits_only)
    with patch.object(shared,'run_dialogue',run_method):
        if policy=='followup':
            return fifth.play(pipe,policy,task,folder,target,ref)
        return selector.play(pipe,policy,task,folder,target,cache)


def validate(row,ref,module):
    if row['qid']!=ref['qid']:
        raise ValueError('Dialogue ID mismatch')
    module.validate(row,ref)
    if row['turns'] and row['turns'][0]['selected_ids']!=ref['turns'][0]['selected_ids']:
        raise ValueError(f'First-round evidence drift for dialogue {row["qid"]}')


@torch.inference_mode()
def run(policy,check):
    tasks,ids,reused,refs = cohort(policy)
    module,trace = POLICIES[policy]
    folder = OUT/policy
    plan = dict(ids=ids,policy=policy,methods=METHODS,model=SNAPSHOT.name,
                pilot_ids=sorted(reused),pilot_plan=digest(shared.read_json(module.OUT/'plan.json')),
                implementation=digest(Path(module.__file__).read_text()),
                controls={m:digest([rows[i] for i in ids]) for m,rows in refs.items()})
    rows = {m:shared.read_records(folder,m) for m in METHODS}
    path = folder/'plan.json'
    if (path.exists() and shared.read_json(path)!=plan) or (not path.exists() and any(rows.values())):
        raise ValueError('Incompatible overnight checkpoints')
    for method,data in rows.items():
        if not data.keys()<=set(ids):
            raise ValueError('Unexpected checkpoint IDs')
        for i,row in data.items():
            validate(row,refs[method][i],module)
    if check:
        print(f'{policy}: verified 400 IDs, both controls and 40 reusable MMR trajectories.',flush=True)
        return
    shared.atomic_json(path,plan)
    with ExitStack() as stack:
        stack.enter_context(patch.object(shared,'history_mmr',partial(shared.history_mmr,weight=.85)))
        pipe = None
        cache = {}
        for i in ids:
            updated = False
            for method,data in rows.items():
                if i in data:
                    continue
                ref = refs[method][i]
                if method=='mmr85' and i in reused:
                    row = dict(reused[i],execution='completed MMR pilot')
                elif ref['ranks'][0]<=10:
                    if ref['turns'] or len(ref['ranks'])!=1:
                        raise ValueError('Expected a stopped initial hit')
                    row = dict(ref,**{trace:[]},execution='shared initial retrieval')
                else:
                    if pipe is None:
                        if not torch.backends.mps.is_available():
                            raise SystemExit('Run the launcher in Mac Terminal for Metal access.')
                        with patch.object(mac,'__file__',str(WORKER)):
                            worker = stack.enter_context(closing(mac.MacQwen(SNAPSHOT,folder)))
                        pipe = mac.load_blip(INPUTS,worker)
                        positions = {name:j for j,name in enumerate(pipe.search_space)}
                    task,inputs = tasks[i]
                    row = play(policy,method,pipe,task,inputs,positions[task['img']],ref,cache)
                row.update(arm=method,policy=policy,hits_only=True)
                validate(row,ref,module)
                shared.atomic_json(folder/method/'dialogues'/f'{i}.json',row)
                data[i]=row
                updated=True
            if updated or i==ids[-1]:
                shared.atomic_json(folder/'summary.json',{
                    m:comparison(data,refs[m],ids) for m,data in rows.items()})
                print(f'{policy}: '+', '.join(f'{m} {len(d)}/400' for m,d in rows.items()),flush=True)
    if any(data.keys()!=set(ids) for data in rows.values()):
        raise ValueError('Incomplete stage')


def queue():
    # Wait for the existing eight-pilot launcher, or take over immediately if it stopped.
    with (fifth.OUT/'queue.lock').open('a') as lock:
        print('Waiting for the current queue if it is still running.',flush=True)
        fcntl.flock(lock,fcntl.LOCK_EX)
        ids=set(shared.read_json(ENRICH/'plan.json')['ids'])
        def enriched():
            return all(shared.read_records(ENRICH,a).keys()==ids
                       for a in ('mmr85_pixels','vanilla_pixels'))
        if not enriched():
            stage('run_candidate_caption_enrichment.py')
        if not enriched():
            raise ValueError('Pixel-enrichment runs have incomplete IDs')
        for policy in POLICIES:
            stage(Path(__file__).name,'--policy',policy)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--policy',choices=POLICIES)
    args=parser.parse_args()
    torch.set_num_threads(4)
    if args.check_only:
        for policy in POLICIES:
            run(policy,True)
    elif args.policy:
        (OUT/args.policy).mkdir(parents=True,exist_ok=True)
        with (OUT/args.policy/'run.lock').open('a') as lock, guard_metal_timeout():
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            run(args.policy,False)
    else:
        queue()
