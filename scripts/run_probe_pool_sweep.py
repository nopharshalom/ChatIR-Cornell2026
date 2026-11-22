"""Replay the saved first-question pilot at four probe scoring depths."""
import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import torch
from torch.nn.functional import normalize

from run_five_question_probes import INPUTS, REPO, REVISION, ROOT, SNAPSHOT, SOURCE, Search, git

sys.path.insert(0, str(SOURCE))
import generate_dialog as plugir
import local_pilot as mac
import run_dialogues as shared

PILOT = ROOT / 'output/agentir-hundred-probe'
OUT = ROOT / 'output/agentir-probe-pool-sweep-100'
DEPTHS = (1000, 5000, 10000, 25000)


def cpu_retriever():
    from transformers import AutoProcessor

    settings = dict(revision=shared.BLIP_REVISION, local_files_only=True, cache_dir=mac.CACHE / 'hub')
    tokenizer = AutoProcessor.from_pretrained(shared.BLIP, **settings).tokenizer
    model = plugir.BlipForRetrieval.from_pretrained(shared.BLIP, **settings).eval()
    ids, vectors = torch.load(INPUTS / 'cache/corpus_blip.pth', weights_only=True, map_location='cpu')
    assert len(vectors) == 50000 and torch.equal(ids, torch.arange(50000))

    def score(queries):
        tokens = tokenizer(queries, padding=True, truncation=True, max_length=512, return_tensors='pt')
        features = model.get_text_features(tokens.input_ids, tokens.attention_mask)
        return normalize(features, dim=-1) @ vectors.T
    return score


def replay(case, score, config, sep, divergence):
    paths = {}
    for p in case['answer_paths']:
        paths.setdefault(p['question'], p)
    assert list(paths) == list(dict.fromkeys(case['candidates']))
    initial = case['initial']
    base = score([initial])[0]
    pools = {n: base.topk(n).indices for n in DEPTHS}
    by_depth = {n: {} for n in DEPTHS}
    errors = []
    for q, p in paths.items():
        if len(p['answers']) < 2:
            continue
        sims = score([f'{initial}{sep}{q} {a}' for a in p['answers']])
        for n in DEPTHS:
            by_depth[n][q] = divergence(sims, pools[n], config.jsd_temperature)
        errors.append(abs(by_depth[1000][q] - p['score']))
    choices = {str(n): max(scores, key=scores.get) if scores else case['arms']['kl']['question']
               for n, scores in by_depth.items()}
    return dict(choices=choices, scores=by_depth, max_replay_error=max(errors, default=0))


def summarize(cases, replays, extra):
    rows = {}
    pending = []
    for n in DEPTHS:
        ranks, changes, gained, lost, better, worse = [], 0, 0, 0, 0, 0
        unknown = []
        for c, r in zip(cases, replays):
            qid = c['qid']
            q = r['choices'][str(n)]
            ref = c['arms']['probe']
            changes += q != ref['question']
            outcomes = {a['question']: a for a in c['arms'].values()}
            outcomes.update(extra.get(str(qid), {}))
            if q not in outcomes:
                unknown.append(qid)
                if (qid, q) not in pending:
                    pending.append((qid, q))
                continue
            rank = outcomes[q]['rank']
            ranks.append(rank)
            gained += rank <= 10 < ref['rank']
            lost += ref['rank'] <= 10 < rank
            better += rank < ref['rank']
            worse += rank > ref['rank']
        rows[n] = dict(changed_question=changes, evaluated=len(ranks), hits=sum(x <= 10 for x in ranks),
                       gained=gained, lost=lost, better_rank=better, worse_rank=worse,
                       median_rank=statistics.median(ranks) if ranks else None, unknown_qids=unknown)
    shared.atomic_json(OUT / 'summary.json', dict(rows=rows, pending=pending, cases=len(cases)))
    print('Depth | changed question | evaluated | top-10 hits | gained/lost vs 1k', flush=True)
    for n, row in rows.items():
        print(n, row['changed_question'], row['evaluated'], row['hits'], row['gained'], row['lost'], flush=True)
    return pending


def evaluate(pending, cases, extra):
    data = shared.read_json(REPO / 'dialogues/VisDial_v1.0_queries_val.json')
    worker = mac.MacQwen(SNAPSHOT, OUT)
    try:
        pipe = mac.load_blip(INPUTS, worker)
        search = Search(pipe)
        positions = {x: i for i, x in enumerate(pipe.search_space)}
        for qid, q in pending:
            img = data[qid]['img']
            initial = cases[qid]['initial']
            answer = worker.answer(REPO / 'data' / img, q)
            rewrite = pipe.reconstruct_dialog([initial, f'{q} {answer}'])
            if rewrite == initial:
                rewrite = pipe.paraphrase(rewrite)
            order = search.similarity([rewrite], 'evaluation')[0].argsort().flip(0)
            rank = int(shared.inverse_ranks(order)[positions[img]])
            extra.setdefault(str(qid), {})[q] = dict(answer=answer, rewrite=rewrite, rank=rank)
            shared.atomic_json(OUT / 'additional_outcomes.json', extra)
            print(f'QID {qid}: rank {rank}', flush=True)
    finally:
        worker.close()


@torch.inference_mode()
def run():
    from agentir.agent.rankers import js_divergence
    from agentir.config import RankerConfig, ReformulatorConfig

    torch.set_num_threads(4)
    meta = shared.read_json(PILOT / 'summary.json')
    assert meta['ranker_revision'] == REVISION and len(set(meta['cases'])) == len(meta['cases']) == 100
    cases = [shared.read_json(PILOT / 'cases' / f'{qid}.json') for qid in meta['cases']]
    assert all(c['initial_rank'] > 10 for c in cases)
    config, sep = RankerConfig(kind='answer_divergence'), ReformulatorConfig().sep
    score = None
    replays = []
    for i, c in enumerate(cases, 1):
        path = OUT / 'scores' / f"{c['qid']}.json"
        if path.exists():
            r = shared.read_json(path)
        else:
            if score is None:
                score = cpu_retriever()
            r = replay(c, score, config, sep, js_divergence)
            shared.atomic_json(path, r)
        replays.append(r)
        if i % 10 == 0:
            print(f'Rescored {i}/{len(cases)}', flush=True)
    del score
    mismatch = [c['qid'] for c, r in zip(cases, replays) if r['choices']['1000'] != c['arms']['probe']['question']]
    if mismatch:
        raise RuntimeError(f'1k replay disagrees for QIDs {mismatch}')
    path = OUT / 'additional_outcomes.json'
    extra = shared.read_json(path) if path.exists() else {}
    pending = summarize(cases, replays, extra)
    if pending:
        print(f'{len(pending)} new answers needed.', flush=True)
        if not torch.backends.mps.is_available():
            print('Re-run the same command in macOS Terminal to evaluate the remaining questions on Metal.')
            return
        evaluate(pending, {c['qid']: c for c in cases}, extra)
        assert not summarize(cases, replays, extra)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='agentir-pool-') as source:
        subprocess.run(['tar', '-xf', '-', '-C', source], input=git('archive', REVISION, 'agentir'), check=True)
        sys.path.insert(0, source)
        run()
