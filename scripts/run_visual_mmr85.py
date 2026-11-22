"""Paired first-question and history-aware second-question probes on MMR 0.85."""
import fcntl
import hashlib
from pathlib import Path
from unittest.mock import patch

import torch

from run_five_question_probes import Search
from run_mmr_history_visual_200 import OUT as PREVIOUS, BASE, visual_choice
from run_overnight_hits import INPUTS, OVERNIGHT_OUTPUT, REPO, ROOT, SNAPSHOT, mac, shared, summarize

OUT = ROOT / 'output/agentir-visual-mmr85-200'
FIRST = 'visual_mmr85'
HISTORY = 'history_visual_mmr85'


def history_proxies(first_pool, current_pool, reserved):
    # Only consider plausible current alternatives, skipping both rejected displays.
    excluded = reserved.union(first_pool[:10], current_pool[:10])
    prior = {idx: 1 / (60 + rank) for rank, idx in enumerate(first_pool[:100], 1)}
    scores = {idx: prior.get(idx, 0) + 1 / (60 + rank)
              for rank, idx in enumerate(current_pool[:100], 1) if idx not in excluded}
    order = sorted(scores, key=scores.get, reverse=True)
    # Preserve depth diversity even when fewer than 31 proxies survive screening.
    return [order[min(j, len(order) - 4 + k)] for k, j in enumerate((0, 10, 20, 30))] if len(order) >= 4 else []


def history_choice(pipe, row, reserved):
    first, current = row['turns'][:2]
    ids = history_proxies(first['pool_ids'], current['pool_ids'], reserved)
    default = current['question']
    prefix = [row['queries'][0], f"{first['question']} {first['answer']}"]
    trials = {}
    if ids:
        for q in dict.fromkeys([default, *current['candidate_questions']]):
            answers = [pipe.client.answer(REPO / 'data' / pipe.search_space[i], q) for i in ids]
            queries = {a: pipe.reconstruct_dialog([*prefix, f'{q} {a}']) for a in dict.fromkeys(answers)}
            orders = Search(pipe).similarity([queries[a] for a in answers], 'proxy').argsort(descending=True)
            ranks = [int(shared.inverse_ranks(order)[i]) for order, i in zip(orders, ids)]
            trials[q] = dict(answers=answers, rewrites=queries, ranks=ranks, hits=sum(r <= 10 for r in ranks))
    chosen = max(trials, key=lambda q: trials[q]['hits']) if trials else default
    return dict(ids=ids, prefix=prefix, default=default, chosen=chosen, trials=trials)


def rerun(pipe, task, reference, target, changed_round, chosen):
    select = pipe.select_question
    def choose(**kw):
        r = kw['round']
        default = select(**kw)
        if r > changed_round:
            return default
        turn = reference['turns'][r - 1]
        assert kw['questions'] == turn['candidate_questions'] and kw['caption_recon'] == turn['query_before']
        if r == changed_round:
            assert turn['question'] == default
            return chosen
        return turn['question']

    pipe.client.next_seed = 1021 + task['qid']
    with patch.object(pipe, 'select_question', choose):
        result = shared.run_dialogue(pipe, BASE, task, target, hits_only=True)
    assert result['ranks'][:changed_round] == reference['ranks'][:changed_round]
    return result


def save(records, arm, row):
    row = dict(row, arm=arm)
    shared.atomic_json(OUT / arm / 'dialogues' / f"{row['qid']}.json", row)
    records[arm][row['qid']] = row


@torch.inference_mode()
def main():
    prior = shared.read_json(PREVIOUS / 'plan.json')
    tasks = prior['tasks']
    mmr = shared.read_records(PREVIOUS, 'mmr85')
    vanilla = shared.read_records(OVERNIGHT_OUTPUT, 'vanilla')
    ids = {t['qid'] for t in tasks}
    vanilla = {i: vanilla[i] for i in ids}
    assert ids == set(mmr) and len(ids) == len(tasks) == 200
    assert prior['arms']['mmr85'] == .85 and prior['corpus'] == 50000
    for t in tasks:
        for row in (mmr[t['qid']], vanilla[t['qid']]):
            assert (row['target'], row['queries'][0]) == (t['img'], t['caption'])
    plan = dict(qids=[t['qid'] for t in tasks], corpus=50000, rounds=10, mmr=.85,
                model=SNAPSHOT.name, arms=[FIRST,HISTORY], controls=['mmr85','vanilla'],
                source=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    plan_path = OUT / 'plan.json'
    if plan_path.exists() and shared.read_json(plan_path) != plan:
        raise ValueError('Existing checkpoints have a different experiment plan')
    shared.atomic_json(plan_path, plan)
    records = {arm: shared.read_records(OUT, arm) for arm in (FIRST,HISTORY)}
    for arm, rows in records.items():
        assert rows.keys() <= ids
        for i, row in mmr.items():
            if i not in rows and not row['turns']:
                save(records, arm, row)
    print('Resolved before probing:', {arm:len(rows) for arm,rows in records.items()}, flush=True)
    if any(len(rows) < 200 for rows in records.values()):
        if not torch.backends.mps.is_available():
            raise SystemExit('Remaining dialogues require the Mac Metal GPU. Run Run_AgentIR_Visual_MMR85.command.')
        worker = mac.MacQwen(SNAPSHOT, OUT)
        try:
            pipe = mac.load_blip(INPUTS, worker)
            positions = {name:i for i,name in enumerate(pipe.search_space)}
            reserved = {t['img'] for t in shared.read_json(REPO / 'dialogues/VisDial_v1.0_queries_val.json')}
            reserved_ids = {positions[name] for name in reserved if name in positions}
            shared.ROOT = OVERNIGHT_OUTPUT / 'fresh-inputs'
            from functools import partial
            with patch.object(shared, 'history_mmr', partial(shared.history_mmr, weight=.85)):
                for arm, stage in ((FIRST,1), (HISTORY,2)):
                    for t in tasks:
                        i = t['qid']
                        if i in records[arm]:
                            continue
                        ref = mmr[i] if stage == 1 else records[FIRST][i]
                        row = ref
                        if len(ref['turns']) >= stage:
                            path = OUT / arm / 'practice' / f'{i}.json'
                            if not path.exists():
                                worker.next_seed = 1021 + i
                                trial = (visual_choice(pipe, t, ref, reserved) if stage == 1
                                         else history_choice(pipe, ref, reserved_ids))
                                shared.atomic_json(path, trial)
                            trial = shared.read_json(path)
                            if trial['chosen'] != trial['default']:
                                row = rerun(pipe, t, ref, positions[t['img']], stage, trial['chosen'])
                        save(records, arm, row)
                        print(f'{arm} {len(records[arm])}/200', flush=True)
        finally:
            worker.close()
    for arm,rows in records.items():
        controls = dict(mmr85=mmr, vanilla=vanilla)
        if arm == HISTORY:
            controls[FIRST] = records[FIRST]
        comparisons = {key:summarize(rows, control) for key,control in controls.items()}
        shared.atomic_json(OUT / arm / 'summary.json', comparisons)
        print(arm, 'round 10 vs MMR 0.85', comparisons['mmr85'][10], flush=True)


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        torch.set_num_threads(4)
        main()
