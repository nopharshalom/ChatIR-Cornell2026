"""Test a single visual question at turn four on all 40 remaining MMR dialogues."""
import argparse
from contextlib import ExitStack, closing
import fcntl
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import torch
import run_matan_mmr_visual as visual
from run_a2r_unseen_pilots import paired_summary
from run_matan_400 import GRID_WORKER, INPUTS, ROOT, SNAPSHOT, cohorts, mac, shared

OUT = ROOT / 'output/mmr-round4-visual'
ARMS = {'images_only': visual.images.install_images_only,
        'images_and_captions': visual.base.install_visual_questioner}
PREFIX = 3


def prepare():
    all_refs, tasks = {}, {}
    for _, group, refs, inputs in cohorts():
        all_refs.update(refs)
        tasks.update({t['qid']: (t, inputs) for t in group})
    pending = sorted(i for i, row in all_refs.items() if min(row['ranks'][:PREFIX+1]) > 10)
    assert len(all_refs) == 400 and len(pending) == 40
    assert sum(min(all_refs[i]['ranks']) <= 10 for i in pending) == 16
    sources = [Path(__file__), Path(visual.__file__), Path(visual.images.__file__),
               Path(visual.base.__file__), GRID_WORKER, Path(shared.__file__), Path(mac.__file__),
               mac.HERE/'generate_dialog.py']
    plan = dict(ids=pending, trigger='no hit after three questions', visual_question=4,
                arms=list(ARMS), mmr=.85, selection='minimum KL, five proposals', display='ordinary NN',
                maximum_questions=10, model=SNAPSHOT.name, corpus=50000,
                primary='paired round-10 hits; list caption misses rescued and caption hits lost',
                secondary='cumulative hits at each round',
                sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                control_sha256=hashlib.sha256(json.dumps(all_refs,sort_keys=True).encode()).hexdigest())
    path = OUT/'plan.json'
    if path.exists() and shared.read_json(path) != plan:
        raise ValueError('Pilot plan or underlying controls differ from saved results.')
    shared.atomic_json(path,plan)
    return tasks, all_refs, pending


def run_one(pipe, task, inputs, ref, target, grid, arm):
    generate = shared.question_candidates
    selected = []
    visual_rounds = []

    def representatives(method, pool, ranks, vectors, cluster):
        assert method == 's3_current_mmr'
        selected[:] = shared.history_mmr(pool, ranks, vectors, count=10, weight=.85)
        return selected.copy()

    def proposals(model, history, query, evidence):
        # History has the initial caption plus one entry for every completed Q&A.
        if len(history) != PREFIX + 1:
            return generate(model, history, query, evidence)
        assert query == ref['queries'][PREFIX] and selected == ref['turns'][PREFIX]['selected_ids']
        with ExitStack() as settings:
            settings.enter_context(patch.object(model,'cluster_representatives',list))
            ARMS[arm](settings, model, grid)
            assert model.cluster_representatives(selected) == selected
            visual_rounds.append(len(history))
            return generate(model, history, query, evidence)

    shared.ROOT = inputs
    pipe.client.next_seed = 1021 + task['qid']
    with patch.object(shared,'representatives',representatives), patch.object(shared,'question_candidates',proposals):
        row = shared.run_dialogue(pipe,'s3_current_mmr',task,target,hits_only=True)
    assert visual_rounds == [4]
    assert row['queries'][:4] == ref['queries'][:4] and row['ranks'][:4] == ref['ranks'][:4]
    for turn, original in zip(row['turns'][:PREFIX], ref['turns'][:PREFIX]):
        for field in ('selected_ids','candidate_questions','question','answer','query_after'):
            assert turn[field] == original[field], (task['qid'],turn['round'],field)
    row.update(arm=arm,visual_question=4)
    return row


@torch.inference_mode()
def main(check_only):
    OUT.mkdir(parents=True,exist_ok=True)
    with ExitStack() as session:
        lock = session.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        tasks, refs, pending = prepare()
        print('Verified 40 eligible dialogues from 400: 16 eventual caption hits, 24 misses.',flush=True)
        if check_only:
            return
        pipe = None
        completed = {}
        for arm in ARMS:
            rows = shared.read_records(OUT,arm)
            assert rows.keys() <= set(pending)
            for i in pending:
                if i in rows:
                    continue
                if pipe is None:
                    if not torch.backends.mps.is_available():
                        raise SystemExit('Metal unavailable. Launch this pilot from Mac Terminal.')
                    with patch.object(mac,'__file__',str(GRID_WORKER)):
                        worker = session.enter_context(closing(mac.MacQwen(SNAPSHOT,OUT)))
                    pipe = mac.load_blip(INPUTS,worker)
                    positions = {name:j for j,name in enumerate(pipe.search_space)}
                    grid = Path(session.enter_context(TemporaryDirectory()))/'questioner-grid.png'
                task, inputs = tasks[i]
                row = run_one(pipe,task,inputs,refs[i],positions[task['img']],grid,arm)
                shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json',row)
                rows[i] = row
                print(f'{arm}: {len(rows)}/40 complete',flush=True)
            summary = paired_summary(rows,refs)
            # Remaining 360 already hit before the policy could change any decision.
            summary['full_400'] = paired_summary({**refs,**rows},refs)
            shared.atomic_json(OUT/arm/'summary.json',summary)
            completed[arm] = rows
            print(arm,'control / variant hits:',summary['control'][-1],summary['variant'][-1],
                  'rescued:',summary['gained'],'lost:',summary['lost'],flush=True)
        shared.atomic_json(OUT/'head_to_head.json',
                           paired_summary(completed['images_and_captions'],completed['images_only']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    torch.set_num_threads(4)
    main(parser.parse_args().check_only)
