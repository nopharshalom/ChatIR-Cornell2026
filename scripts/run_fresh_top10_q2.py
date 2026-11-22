"""Compare repeated vs fresh top-ten question evidence at the second question only."""
import fcntl
import time

import torch

from run_s2_question_two_only import INPUTS, REPO, ROOT, SNAPSHOT, mac, scores_for, shared

SOURCE = ROOT / 'output/plugir-overnight-hits10-200/results/s3_current_top10/dialogues'
OUT = ROOT / 'output/agentir-fresh-top10-q2-200'


def evidence_ids(row):
    first, second = row['turns'][:2]
    seen = set(first['selected_ids'])
    pool = second['pool_ids']
    control = pool[:10]
    fresh = [idx for idx in pool if idx not in seen][:10]
    assert control == second['selected_ids'] and len(set(fresh)) == len(seen) == 10
    return control, fresh


def target_image(row):
    path = REPO / 'data' / row['target']
    if not path.is_file():
        path = INPUTS / 'targets' / row['target']
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def run_arm(pipe, row, ids, scores, target):
    first = row['turns'][0]
    query = row['queries'][1]
    dialogue = [row['queries'][0], f"{first['question']} {first['answer']}"]
    seed = 1021 + 100 * row['qid'] + 1
    torch.manual_seed(seed)
    pipe.client.next_seed = seed
    captions = [pipe.captions[i]['caption'][0] for i in ids]
    candidates = shared.question_candidates(pipe, dialogue, query, captions)
    question = pipe.select_question(query, candidates, scores, 10, pipe.img_embs, 500, 2)
    answer = pipe.client.answer(target_image(row), question)
    rewrite = pipe.reconstruct_dialog(dialogue + [f'{question} {answer}'])
    if rewrite == query:
        rewrite = pipe.paraphrase(rewrite)
    order = scores_for(pipe, rewrite).argsort().flip(0)
    return dict(evidence_ids=ids, captions=captions, candidates=candidates, question=question,
                answer=answer, rewrite=rewrite, rank=int(shared.inverse_ranks(order)[target]))


@torch.inference_mode()
def main():
    rows = [shared.read_json(p) for p in sorted(SOURCE.glob('*.json'))]
    if len({r['qid'] for r in rows}) != 200 or len(rows) != 200:
        raise ValueError('Expected the original 200 distinct S3 current-top-ten dialogues.')
    misses = [r for r in rows if min(r['ranks'][:2]) > 10]
    unchanged, changed = [], []
    for row in misses:
        control, fresh = evidence_ids(row)
        (unchanged if control == fresh else changed).append(row)
        target_image(row)
    plan = dict(source=str(SOURCE.relative_to(ROOT)), snapshot=SNAPSHOT.name,
                changed=[r['qid'] for r in changed], unchanged=[r['qid'] for r in unchanged])
    plan_path = OUT / 'plan.json'
    if plan_path.exists() and shared.read_json(plan_path) != plan:
        raise ValueError('Existing checkpoint plan differs.')
    shared.atomic_json(plan_path, plan)
    results = OUT / 'cases'
    pending = [r for r in changed if not (results / f"{r['qid']}.json").is_file()]
    print(f'{len(misses)} reached question two. {len(changed)} have repeats. '
          f'{len(pending)} pairs left to run.', flush=True)
    if pending:
        if not torch.backends.mps.is_available():
            raise SystemExit('Metal is unavailable here. Run this script in macOS Terminal.')
        worker = mac.MacQwen(SNAPSHOT, OUT)
        try:
            pipe = mac.load_blip(INPUTS, worker)
            targets = {name: i for i, name in enumerate(pipe.search_space)}
            for row in pending:
                started = time.monotonic()
                ctl, fresh = evidence_ids(row)
                target = targets[row['target']]
                scores = scores_for(pipe, row['queries'][1])
                order = scores.argsort().flip(0)
                assert order[:20].tolist() == row['turns'][1]['pool_ids'][:20]
                assert int(shared.inverse_ranks(order)[target]) == row['ranks'][1]
                a = run_arm(pipe, row, ctl, scores, target)
                b = run_arm(pipe, row, fresh, scores, target)
                result = dict(qid=row['qid'], before_rank=row['ranks'][1], control=a, fresh=b)
                shared.atomic_json(results / f"{row['qid']}.json", result)
                print(f"QID {row['qid']} · control {a['rank']} · fresh {b['rank']} · "
                      f"{time.monotonic()-started:.1f}s", flush=True)
        finally:
            worker.close()
    pairs = [shared.read_json(results / f"{r['qid']}.json") for r in changed]
    common = sum(r['ranks'][2] <= 10 for r in unchanged)
    hits = {arm: common + sum(p[arm]['rank'] <= 10 for p in pairs) for arm in ('control', 'fresh')}
    summary = dict(question_two_cases=len(misses), changed_evidence=len(pairs),
                   identical_cases_reused=len(unchanged), recoveries=hits,
                   gained=[p['qid'] for p in pairs if p['fresh']['rank'] <= 10 < p['control']['rank']],
                   lost=[p['qid'] for p in pairs if p['control']['rank'] <= 10 < p['fresh']['rank']],
                   cumulative_hits_out_of_200={arm: len(rows)-len(misses)+n for arm,n in hits.items()})
    shared.atomic_json(OUT / 'summary.json', summary)
    print(summary)


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()
