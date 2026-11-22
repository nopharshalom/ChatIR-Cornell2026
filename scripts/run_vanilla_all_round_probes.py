"""Vanilla clustering with current-ranking caption or pixel probes before every question."""
import fcntl
import hashlib
from pathlib import Path
from unittest.mock import patch

import torch

from run_five_question_probes import Search
from run_overnight_hits import INPUTS, OVERNIGHT_OUTPUT, REPO, ROOT, SNAPSHOT, mac, shared, summarize

OUT = ROOT / 'output/agentir-vanilla-all-round-probes-200'
ARMS = ('caption_all_rounds', 'visual_all_rounds')


def practice(pipe, arm, proxy, question):
    if arm == 'visual_all_rounds':
        return pipe.client.answer(REPO / 'data' / pipe.search_space[proxy], question)
    caption = pipe.captions[proxy]['caption'][0]
    messages = [
        {'role': 'system', 'content': 'Answer the question using only the supplied image caption. '
         'Be brief and say you are unsure when the caption does not show the answer.'},
        {'role': 'user', 'content': f'Caption: {caption}\nQuestion: {question}'},
    ]
    return pipe.client.complete(messages, 48)


def probe_dialogue(pipe, task, target, arm, reserved):
    generate = shared.question_candidates
    select = pipe.select_question
    prefix, displayed, probes = [], set(), []
    def candidates(model, dialogue, query, evidence):
        prefix[:] = dialogue
        return generate(model, dialogue, query, evidence)

    def choose(**kwargs):
        round_no = kwargs['round']
        assert round_no == len(prefix) == len(probes) + 1
        default = select(**kwargs)
        ranking = kwargs['cossim_prev'].argsort().flip(0).tolist()
        displayed.update(ranking[:10])
        eligible = (i for i in ranking if i not in displayed and i not in reserved)
        from itertools import islice
        proxies = list(islice(eligible, 0, 31, 10))
        assert len(proxies) == 4
        trials = {}
        for question in dict.fromkeys([default, *kwargs['questions']]):
            answers = [practice(pipe, arm, proxy, question) for proxy in proxies]
            rewrites = {a: pipe.reconstruct_dialog([*prefix, f'{question} {a}']) for a in dict.fromkeys(answers)}
            scores = Search(pipe).similarity([rewrites[a] for a in answers], 'proxy')
            orders = scores.argsort(descending=True)
            ranks = [int(shared.inverse_ranks(order)[i]) for order, i in zip(orders, proxies)]
            trials[question] = dict(answers=answers, rewrites=rewrites, ranks=ranks, hits=sum(r <= 10 for r in ranks))
        chosen = max(trials, key=lambda q: trials[q]['hits'])
        probes.append(dict(round=round_no, ids=proxies, default=default, chosen=chosen, trials=trials))
        return chosen

    pipe.client.next_seed = 1021 + task['qid']
    with patch.object(shared, 'question_candidates', candidates), patch.object(pipe, 'select_question', choose):
        row = shared.run_dialogue(pipe, 'vanilla', task, target, hits_only=True)
    assert len(row['turns']) == len(probes)
    row.update(arm=arm, probes=probes)
    return row


@torch.inference_mode()
def main():
    tasks = shared.read_json(OVERNIGHT_OUTPUT / 'plan.json')['fresh'][:200]
    ids = {t['qid'] for t in tasks}
    assert len(ids) == len(tasks) == 200
    prior = shared.read_records(OVERNIGHT_OUTPUT, 'vanilla')
    baseline = {i:prior[i] for i in ids}
    plan = dict(tasks=tasks, corpus=50000, rounds=10, arms=ARMS, selector='vanilla clustering',
                source=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), snapshot=SNAPSHOT.name)
    plan['arms'] = list(ARMS)
    path = OUT / 'plan.json'
    if path.exists() and shared.read_json(path) != plan:
        raise ValueError('Experiment configuration differs from existing checkpoints')
    shared.atomic_json(path, plan)
    results = {}
    for arm in ARMS:
        rows = shared.read_records(OUT, arm)
        assert rows.keys() <= ids
        for task in tasks:
            i = task['qid']
            ref = baseline[i]
            assert (ref['target'], ref['queries'][0]) == (task['img'], task['caption'])
            if i not in rows and not ref['turns']:
                rows[i] = dict(ref, arm=arm, probes=[])
                shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json', rows[i])
        results[arm] = rows
        print(f'{arm}: {len(rows)}/200 completed', flush=True)
    if any(len(rows) < 200 for rows in results.values()):
        if not torch.backends.mps.is_available():
            raise SystemExit('Remaining dialogues require Metal. Run Run_Vanilla_All_Round_Probes.command on the Mac.')
        worker = mac.MacQwen(SNAPSHOT, OUT)
        try:
            pipe = mac.load_blip(INPUTS, worker)
            shared.ROOT = OVERNIGHT_OUTPUT/'fresh-inputs'
            positions = {name:i for i,name in enumerate(pipe.search_space)}
            reserved = {positions[t['img']] for t in shared.read_json(REPO/'dialogues/VisDial_v1.0_queries_val.json')
                        if t['img'] in positions}
            for arm, rows in results.items():
                for task in tasks:
                    i = task['qid']
                    if i in rows:
                        continue
                    row = probe_dialogue(pipe, task, positions[task['img']], arm, reserved)
                    assert row['ranks'][0] == baseline[i]['ranks'][0]
                    shared.atomic_json(OUT/arm/'dialogues'/f'{i}.json', row)
                    rows[i] = row
                    print(f'{arm}: {len(rows)}/200 completed', flush=True)
                shared.atomic_json(OUT/arm/'summary.json', summarize(rows, baseline))
        finally:
            worker.close()
    for arm, rows in results.items():
        shared.atomic_json(OUT/arm/'summary.json', summarize(rows, baseline))
    shared.atomic_json(OUT/'visual_vs_caption.json', summarize(results[ARMS[1]], results[ARMS[0]]))


if __name__ == '__main__':
    OUT.mkdir(exist_ok=True, parents=True)
    with (OUT/'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        torch.set_num_threads(4)
        main()
