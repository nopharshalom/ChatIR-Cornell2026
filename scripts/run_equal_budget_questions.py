"""First-question recovery with two usual batches versus usual + rejected-caption batches."""
import fcntl
import json
import subprocess
import sys
import tempfile
import time

import torch

from run_five_question_probes import REVISION, compare_questions, git
from run_rejected_caption_pilot import INPUTS, REPO, ROOT, SNAPSHOT, Search, mac, shared

BASE = ROOT / 'output'
OUT = BASE / 'agentir-equal-budget-questions-200'
SECOND_SEED = 20261119


def finalist(question, paths):
    score = next(p.get('score') for p in paths if p['question'] == question)
    assert all(p.get('score') is None or score is not None and p['score'] <= score for p in paths)
    return dict(question=question, score=score)


def winner(a, b):
    # Each batch has five proposal slots. Keep the original on ties or joint fallback.
    if b['score'] is not None and (a['score'] is None or b['score'] > a['score']):
        return b
    return a


def case_file(folder, qid):
    return shared.read_json(BASE / folder / 'cases' / f'{qid}.json')


def load_cases():
    first = 'agentir-hundred-probe'
    second = 'agentir-fresh-rejected-caption-probes-100'
    data = shared.read_json(REPO / 'dialogues/VisDial_v1.0_queries_val.json')
    groups = [shared.read_json(BASE / first / 'summary.json')['cases'],
              shared.read_json(BASE / second / 'plan.json')['qids']]
    assert [len(x) for x in groups] == [100, 100] and len(set(groups[0]+groups[1])) == 200
    cases = []
    for sample, ids in enumerate(groups, 1):
        for qid in ids:
            if sample == 1:
                row = case_file(first, qid)
                rej = case_file('agentir-rejected-caption-probes-100', qid)
                context = case_file('agentir-rejected-caption-first-question-100', qid)
                assert context['initial'] == row['initial'] and context['evidence'] == row['evidence']
                a, b = row['arms']['probe'], rej['probe']
                paths_a, paths_b = row['answer_paths'], rej['probe_details']['answer_paths']
                rank_a = a['rank']
            else:
                row = case_file(second, qid)
                a, b = row['usual'], row['rejected']
                paths_a, paths_b = a['probe_details']['answer_paths'], b['probe_details']['answer_paths']
                rank_a = a['raw_rank']
            assert len(paths_a) == len(paths_b) == 5 and row['initial_rank'] > 10
            usual = finalist(a['question'], paths_a)
            rejected = finalist(b['question'], paths_b)
            known = {a['question']:rank_a, b['question']:b['raw_rank']}
            assert known[a['question']] == rank_a
            mixed = winner(usual, rejected)
            image = data[qid]['img']
            assert row['initial'] == data[qid]['dialog'][0] and (REPO / 'data' / image).is_file()
            cases.append(dict(qid=qid, sample=sample, image=image, initial=row['initial'],
                              evidence=row['evidence'], usual=usual, mixed=mixed, known=known,
                              first_candidates=[p['question'] for p in paths_a]))
    return cases


def evaluate(pipe, case, target):
    qid, initial = case['qid'], case['initial']
    torch.manual_seed(1021 + 100*qid)
    pipe.client.next_seed = SECOND_SEED + qid
    questions = shared.question_candidates(pipe, [initial], initial, case['evidence'])
    search = Search(pipe)
    choices, details = compare_questions(pipe, search, initial, case['evidence'], questions, qid)
    paths = details['answer_paths']
    for p in paths:
        if p.get('score') == float('-inf'):
            p['score'] = None
    second = finalist(choices['probe'], paths)
    control = winner(case['usual'], second)
    q = control['question']
    rank = case['known'].get(q)
    answer = rewrite = None
    if rank is None:
        answer = pipe.client.answer(REPO / 'data' / case['image'], q)
        rewrite = pipe.reconstruct_dialog([initial, f'{q} {answer}'])
        if rewrite == initial:
            rewrite = pipe.paraphrase(rewrite)
        order = search.similarity([rewrite], 'evaluation')[0].argsort().flip(0)
        rank = int(shared.inverse_ranks(order)[target])
    return dict(qid=qid, sample=case['sample'], first_candidates=case['first_candidates'],
                second_candidates=questions, paths=paths, control=control, mixed=case['mixed'],
                control_rank=rank, mixed_rank=case['known'][case['mixed']['question']],
                usual_rank=case['known'][case['usual']['question']], new_answer=answer, new_rewrite=rewrite)


def summarize(rows):
    return dict(n=len(rows), usual_five=sum(r['usual_rank'] <= 10 for r in rows),
                usual_ten=sum(r['control_rank'] <= 10 for r in rows),
                mixed_ten=sum(r['mixed_rank'] <= 10 for r in rows),
                mixed_gained=sum(r['mixed_rank'] <= 10 < r['control_rank'] for r in rows),
                mixed_lost=sum(r['control_rank'] <= 10 < r['mixed_rank'] for r in rows))


@torch.inference_mode()
def run():
    cases = load_cases()
    for sample in (1, 2):
        group = [c for c in cases if c['sample'] == sample]
        print(f"Sample {sample}: 100 initial misses, saved mixed recoveries "
              f"{sum(c['known'][c['mixed']['question']] <= 10 for c in group)}", flush=True)
    paths = {c['qid']: OUT / 'cases' / f"{c['qid']}.json" for c in cases}
    pending = [c for c in cases if not paths[c['qid']].exists()]
    print(f'{len(pending)} of 200 control dialogues pending.', flush=True)
    if pending:
        if not torch.backends.mps.is_available():
            raise SystemExit('Run this script in macOS Terminal for Metal access.')
        worker = mac.MacQwen(SNAPSHOT, OUT)
        try:
            pipe = mac.load_blip(INPUTS, worker)
            positions = {img: i for i, img in enumerate(pipe.search_space)}
            for i, c in enumerate(pending, 1):
                start = time.monotonic()
                row = evaluate(pipe, c, positions[c['image']])
                shared.atomic_json(paths[c['qid']], row)
                print(f"{200-len(pending)+i}/200 · QID {c['qid']} · usual ten rank "
                      f"{row['control_rank']} · mixed rank {row['mixed_rank']} · "
                      f"{time.monotonic()-start:.1f}s", flush=True)
        finally:
            worker.close()
    rows = [shared.read_json(paths[c['qid']]) for c in cases]
    summary = {f'sample_{i}': summarize([r for r in rows if r['sample'] == i]) for i in (1, 2)}
    summary['combined'] = summarize(rows)
    shared.atomic_json(OUT / 'summary.json', summary)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'run.lock').open('a') as lock, tempfile.TemporaryDirectory() as source:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        subprocess.run(['tar', '-xf', '-', '-C', source], input=git('archive', REVISION, 'agentir'), check=True)
        sys.path.insert(0, source)
        run()
