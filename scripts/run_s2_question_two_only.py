"""Paired continuation of the original 500 dialogues with S2 evidence only at question two."""
import argparse
import fcntl
import time

import torch

from analyze_500_dialogues import PATHS
from run_rejected_caption_pilot import INPUTS, REPO, ROOT, SNAPSHOT, mac, shared

OUT = ROOT / 'output/agentir-s2-question-two-only-500'


def records(arm):
    result = {}
    for folder, size in zip(PATHS[arm], (200, 300)):
        files = sorted((ROOT / folder / 'dialogues').glob('*.json'))
        assert len(files) == size
        for path in files:
            row = shared.read_json(path)
            assert row['qid'] not in result and row['hits_only']
            result[row['qid']] = row
    return result


def history(row, n):
    return [(t['question'], t['answer'], t['query_after']) for t in row['turns'][:n]]


def done(row, end):
    return min(row['ranks']) <= 10 or len(row['ranks']) > end


def scores_for(pipe, query):
    vector = torch.nn.functional.normalize(pipe.get_text_features(query), dim=-1)
    return (vector @ pipe.img_embs.T).flatten()


def continue_case(pipe, path, row, image, target, end):
    query = row['queries'][-1]
    dialogue = [row['queries'][0]] + [f"{t['question']} {t['answer']}" for t in row['turns']]
    scores = scores_for(pipe, query)
    order = scores.argsort().flip(0)
    assert int(shared.inverse_ranks(order)[target]) == row['ranks'][-1]
    while not done(row, end):
        r = len(row['turns'])
        # Set the continuation seed per turn so a stopped run can resume identically.
        seed = 1021 + 100 * row['qid'] + r
        torch.manual_seed(seed)
        pipe.client.next_seed = seed
        pool = order[:500 - 50*r]
        reps = shared.representatives('vanilla', pool, None, pipe.img_embs, pipe.cluster_representatives)
        evidence = [pipe.captions[i]['caption'][0] for i in reps]
        candidates = shared.question_candidates(pipe, dialogue, query, evidence)
        question = pipe.select_question(query, candidates, scores, 10, pipe.img_embs, 500, r+1)
        answer = pipe.client.answer(image, question)
        dialogue.append(f'{question} {answer}')
        new_query = pipe.reconstruct_dialog(dialogue)
        if new_query == query:
            new_query = pipe.paraphrase(new_query)
        row['turns'].append(dict(question=question, answer=answer, query_before=query, query_after=new_query,
                                 candidate_questions=candidates, selected_captions=evidence))
        query = new_query
        scores = scores_for(pipe, query)
        order = scores.argsort().flip(0)
        row['queries'].append(query)
        row['ranks'].append(int(shared.inverse_ranks(order)[target]))
        shared.atomic_json(path, row)


@torch.inference_mode()
def run(end):
    vanilla, s2 = records('vanilla'), records('s2')
    assert vanilla.keys() == s2.keys()
    hybrid, pending = {}, []
    for qid, s in s2.items():
        v = vanilla[qid]
        assert s['target'] == v['target'] and s['queries'][0] == v['queries'][0]
        assert s['ranks'][:2] == v['ranks'][:2] and history(s, 1) == history(v, 1)
        if done(s, 2) and min(s['ranks'][:3]) <= 10:
            hybrid[qid] = s
        elif history(s, 2) == history(v, 2):
            assert s['queries'][:3] == v['queries'][:3] and s['ranks'][:3] == v['ranks'][:3]
            hybrid[qid] = v
        else:
            path = OUT / 'cases' / f'{qid}.json'
            row = shared.read_json(path) if path.exists() else dict(
                qid=qid, target=s['target'], queries=s['queries'][:3], ranks=s['ranks'][:3],
                turns=s['turns'][:2])
            assert row['qid'] == qid and row['target'] == s['target'] and history(row, 2) == history(s, 2)
            assert row['ranks'][:3] == s['ranks'][:3] and len(row['turns'])+1 == len(row['ranks']) == len(row['queries'])
            hybrid[qid] = row
            if not done(row, end):
                image = REPO / 'data' / row['target']
                if not image.exists():
                    image = INPUTS / 'targets' / row['target']
                assert image.is_file()
                pending.append((path, row, image))
    print(f'{len(pending)} continuations pending to round {end} across {len(hybrid)} dialogues.', flush=True)
    if pending:
        if not torch.backends.mps.is_available():
            raise SystemExit('Use macOS Terminal for Metal access.')
        worker = mac.MacQwen(SNAPSHOT, OUT)
        try:
            pipe = mac.load_blip(INPUTS, worker)
            positions = {img:i for i,img in enumerate(pipe.search_space)}
            for n,(path,row,image) in enumerate(pending, 1):
                started = time.monotonic()
                continue_case(pipe, path, row, image, positions[row['target']], end)
                print(f"{n}/{len(pending)} · QID {row['qid']} · rank {row['ranks'][-1]} · "
                      f"{time.monotonic()-started:.1f}s", flush=True)
        finally:
            worker.close()
    summary = {}
    for name, arm in [('vanilla', vanilla), ('always_s2', s2), ('q2_only', hybrid)]:
        summary[name] = {r:sum(min(row['ranks'][:r+1]) <= 10 for row in arm.values())
                         for r in (1, 2, 5, 10) if r <= end}
        summary[name]['questions'] = sum(min(len(row['ranks'])-1, end) for row in arm.values())
    shared.atomic_json(OUT / f'summary_r{end}.json', summary)
    print(summary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--through-round', type=int, choices=(5, 10), default=5)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(args.through_round)
