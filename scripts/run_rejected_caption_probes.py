"""Compare KL and 1k answer-divergence selection on frozen rejected-caption questions."""
import fcntl
import json
import subprocess
import sys
import tempfile
import time

import torch

from run_five_question_probes import REVISION, compare_questions, git
from run_rejected_caption_pilot import (
    INPUTS, OUT as SOURCE_PILOT, PILOT, REPO, ROOT, SNAPSHOT, Search, mac, shared, score_outcome,
)

OUT = ROOT / 'output/agentir-rejected-caption-probes-100'


def summarize(rows):
    summary = dict(n=len(rows), changed_question=sum(r['kl']['question'] != r['probe']['question'] for r in rows))
    for metric in ('raw_rank', 'unseen_rank'):
        pairs = [(r['kl'][metric] <= 10, r['probe'][metric] <= 10) for r in rows]
        summary[metric] = dict(kl=sum(a for a, b in pairs), probe=sum(b for a, b in pairs),
                               gained=sum(b and not a for a, b in pairs), lost=sum(a and not b for a, b in pairs))
    return summary


@torch.inference_mode()
def run():
    ids = shared.read_json(SOURCE_PILOT / 'plan.json')['qids']
    assert len(ids) == len(set(ids)) == 100
    data = shared.read_json(REPO / 'dialogues/VisDial_v1.0_queries_val.json')
    cases = [shared.read_json(SOURCE_PILOT / 'cases' / f'{qid}.json') for qid in ids]
    for qid, c in zip(ids, cases):
        assert c['qid'] == qid and c['initial_rank'] > 10 and len(c['candidates']) == 5
        assert c['variant']['question'] in c['candidates'] and data[qid]['dialog'][0] == c['initial']
    saved = {m: sum(c['variant'][m] <= 10 for c in cases) for m in ('raw_rank', 'unseen_rank')}
    print(f'Validated 100 fixed candidate lists. KL hits: {saved}', flush=True)
    rows, pending = [], []
    for c in cases:
        path = OUT / 'cases' / f"{c['qid']}.json"
        if path.exists():
            row = shared.read_json(path)
            assert row['candidates'] == c['candidates'] and row['kl'] == c['variant']
            rows.append(row)
        else:
            pending.append(c)
    if pending:
        if not torch.backends.mps.is_available():
            raise SystemExit('Continue in macOS Terminal for Metal access.')
        alternatives = shared.read_json(ROOT / 'output/agentir-rejected-caption-lost-candidates/outcomes.json')
        sweep = shared.read_json(ROOT / 'output/agentir-probe-pool-sweep-100/additional_outcomes.json')
        worker = mac.MacQwen(SNAPSHOT, OUT)
        try:
            pipe = mac.load_blip(INPUTS, worker)
            positions = {img: i for i, img in enumerate(pipe.search_space)}
            for c in pending:
                start = time.monotonic()
                key = str(c['qid'])
                initial = c['initial']
                search = Search(pipe)
                choices, details = compare_questions(pipe, search, initial, c['evidence'], c['candidates'], c['qid'])
                assert choices['kl'] == c['variant']['question'], f'KL replay mismatch: {key}'
                question = choices['probe']
                # Only look up the target-image answer after the ranker commits to its question.
                original = shared.read_json(PILOT / 'cases' / f'{key}.json')
                known = {a['question']: a for a in original['arms'].values()}
                known.update(sweep.get(key, {}))
                known.update(alternatives.get(key, {}))
                for a in (c['control'], c['variant']):
                    known[a['question']] = a
                img = data[c['qid']]['img']
                outcome = known.get(question)
                if outcome is None:
                    answer = worker.answer(REPO / 'data' / img, question)
                    rewrite = pipe.reconstruct_dialog([initial, f'{question} {answer}'])
                    if rewrite == initial:
                        rewrite = pipe.paraphrase(rewrite)
                    outcome = dict(answer=answer, rewrite=rewrite)
                if 'unseen_rank' not in outcome:
                    shown = torch.tensor(c['displayed_ids'], device=pipe.img_embs.device)
                    outcome = score_outcome(search, outcome, positions[img], shown)
                    assert outcome.get('rank', outcome['raw_rank']) == outcome['raw_rank']
                row = dict(qid=c['qid'], candidates=c['candidates'], kl=c['variant'],
                           probe={**outcome, 'question': question}, probe_details=details,
                           reused_outcome=question in known)
                for p in details['answer_paths']:
                    if p.get('score') == float('-inf'):
                        p['score'] = None
                shared.atomic_json(OUT / 'cases' / f'{key}.json', row)
                rows.append(row)
                print(f"{len(rows)}/100 · QID {key} · raw rank {row['kl']['raw_rank']} vs "
                      f"{row['probe']['raw_rank']} · {time.monotonic()-start:.1f}s", flush=True)
        finally:
            worker.close()
    summary = summarize(rows)
    shared.atomic_json(OUT / 'summary.json', summary)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'run.lock').open('a') as lock, tempfile.TemporaryDirectory() as source:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        subprocess.run(['tar', '-xf', '-', '-C', source], input=git('archive', REVISION, 'agentir'), check=True)
        sys.path.insert(0, source)
        run()
