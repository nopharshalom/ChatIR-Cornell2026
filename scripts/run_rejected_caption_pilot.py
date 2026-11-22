"""Paired one-question pilot on the saved 100 initial misses."""
import argparse
import fcntl
import sys
import time
from types import SimpleNamespace

import torch

from run_five_question_probes import INPUTS, REPO, ROOT, SNAPSHOT, SOURCE, Search

sys.path.insert(0, str(SOURCE))
import local_pilot as mac
import run_dialogues as shared

PILOT = ROOT / 'output/agentir-hundred-probe'
OUT = ROOT / 'output/agentir-rejected-caption-first-question-100'
INSTRUCTION = ('These specific photos were rejected. The target can still share their details. '
               'Ask about a visible detail that varies across plausible photos '
               'and has not already been answered.')


def generator(pipe, rejected):
    label = ('the ten displayed photos' if len(rejected) == 10
             else 'all distinct displayed photos')
    def referring(*, prompt_fake, **kwargs):
        context = ('[Current cluster representative captions]\n' + prompt_fake +
                   f'[Captions of {label} the user rejected]\n' +
                   ''.join(f'{i}. {cap}\n' for i, cap in enumerate(rejected)) +
                   INSTRUCTION + '\n')
        return pipe.generate_questions_referring(prompt_fake=context, **kwargs)

    return SimpleNamespace(generate_questions_referring=referring, filter_questions=pipe.filter_questions,
                           generate_questions=pipe.generate_questions)


def metrics(order, target, shown):
    ranks = shared.inverse_ranks(order)
    raw = int(ranks[target])
    unseen = raw - int((ranks[shown] < raw).sum())
    return dict(raw_rank=raw, unseen_rank=unseen)


def score_outcome(search, outcome, target, shown):
    order = search.similarity([outcome['rewrite']], 'evaluation')[0].argsort().flip(0)
    return {**outcome, **metrics(order, target, shown)}


def run_case(pipe, c, img, target):
    qid, initial = c['qid'], c['initial']
    search = Search(pipe)
    scores = search.similarity([initial], 'initial')[0]
    order = scores.argsort().flip(0)
    assert int(shared.inverse_ranks(order)[target]) == c['initial_rank'] > 10
    shown = order[:10]
    rejected = [pipe.captions[i]['caption'][0] for i in shown.tolist()]
    control = score_outcome(search, c['arms']['kl'], target, shown)
    assert control['raw_rank'] == control['rank']
    torch.manual_seed(1021 + 100 * qid)
    pipe.client.next_seed = 1021 + qid
    candidates = shared.question_candidates(generator(pipe, rejected), [initial], initial, c['evidence'])
    question = pipe.select_question(initial, candidates, scores, 10, pipe.img_embs, 500, 1)
    previous = {a['question']: a for a in c['arms'].values()}
    if question in previous:
        outcome = previous[question]
    else:
        answer = pipe.client.answer(img, question)
        rewrite = pipe.reconstruct_dialog([initial, f'{question} {answer}'])
        if rewrite == initial:
            rewrite = pipe.paraphrase(rewrite)
        outcome = dict(question=question, answer=answer, rewrite=rewrite)
    variant = score_outcome(search, outcome, target, shown)
    return dict(qid=qid, initial=initial, initial_rank=c['initial_rank'], evidence=c['evidence'],
                displayed_ids=shown.tolist(), rejected_captions=rejected, candidates=candidates,
                control_candidates=c['candidates'], control=control, variant=variant)


def summarize(rows):
    out = dict(n=len(rows), changed_question=sum(
        r['control']['question'].strip().casefold() != r['variant']['question'].strip().casefold()
        for r in rows))
    for metric in ('raw_rank', 'unseen_rank'):
        pairs = [(r['control'][metric] <= 10, r['variant'][metric] <= 10) for r in rows]
        out[metric] = dict(vanilla=sum(a for a, b in pairs), with_rejections=sum(b for a, b in pairs),
                           gained=sum(b and not a for a, b in pairs), lost=sum(a and not b for a, b in pairs))
    return out


@torch.inference_mode()
def run(prepare_only):
    ids = shared.read_json(PILOT / 'summary.json')['cases']
    data = shared.read_json(REPO / 'dialogues/VisDial_v1.0_queries_val.json')
    cases = [shared.read_json(PILOT / 'cases' / f'{qid}.json') for qid in ids]
    assert len(set(ids)) == len(cases) == 100
    for qid, c in zip(ids, cases):
        assert qid == c['qid'] and c['initial_rank'] > 10 and 0 < len(c['evidence']) <= 10
        assert c['initial'] == data[qid]['dialog'][0] and (REPO / 'data' / data[qid]['img']).is_file()
    plan = dict(qids=ids, instruction=INSTRUCTION, snapshot=SNAPSHOT.name)
    path = OUT / 'plan.json'
    if path.exists() and shared.read_json(path) != plan:
        raise ValueError('Existing checkpoints use a different pilot plan.')
    shared.atomic_json(path, plan)
    print(f"Validated {len(cases)} initial misses. Saved vanilla raw hits: "
          f"{sum(c['arms']['kl']['rank'] <= 10 for c in cases)}.", flush=True)
    if prepare_only:
        return
    rows, remaining = [], []
    for c in cases:
        path = OUT / 'cases' / f"{c['qid']}.json"
        if path.exists():
            rows.append(shared.read_json(path))
        else:
            remaining.append(c)
    if remaining:
        if not torch.backends.mps.is_available():
            raise SystemExit('This session has no Metal access. Run the same script in macOS Terminal.')
        worker = mac.MacQwen(SNAPSHOT, OUT)
        try:
            pipe = mac.load_blip(INPUTS, worker)
            positions = {img: i for i, img in enumerate(pipe.search_space)}
            for c in remaining:
                started = time.monotonic()
                img = data[c['qid']]['img']
                row = run_case(pipe, c, REPO / 'data' / img, positions[img])
                shared.atomic_json(OUT / 'cases' / f"{c['qid']}.json", row)
                rows.append(row)
                print(f"{len(rows)}/100 · QID {c['qid']} · raw rank "
                      f"{row['control']['raw_rank']} vs {row['variant']['raw_rank']} · "
                      f"{time.monotonic()-started:.1f}s", flush=True)
        finally:
            worker.close()
    summary = summarize(rows)
    assert summary['raw_rank']['vanilla'] == sum(c['arms']['kl']['hit'] for c in cases)
    shared.atomic_json(OUT / 'summary.json', summary)
    print(summary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(args.prepare_only)
