"""Five usual and five rejection-informed proposals every turn on the matched 500."""
import fcntl
import json
import math
import subprocess
import sys
import tempfile
import time

import torch

from run_five_question_probes import LocalLLM, SAMPLE_SEED, REVISION, git
from run_overnight_hits import OVERNIGHT_OUTPUT, OLD_BASELINE, summarize
from run_rejected_caption_pilot import INPUTS, REPO, ROOT, SNAPSHOT, Search, generator, mac, shared

OUT = ROOT / 'output/agentir-mixed-probe-500'
FRESH = ROOT / 'output/agentir-probe-fresh-300'


def inputs():
    manifest = shared.read_json(OVERNIGHT_OUTPUT / 'plan.json')
    tasks = {}
    vanilla, probe = {}, {}
    for name, count, van_dir, probe_dir in [('original', 200, OLD_BASELINE, OVERNIGHT_OUTPUT),
                                            ('fresh', 300, OVERNIGHT_OUTPUT, FRESH)]:
        v = shared.read_records(van_dir, 'vanilla')
        p = shared.read_records(probe_dir, 'probe')
        group = manifest[name]
        ids = {t['qid'] for t in group}
        assert len(group) == len(ids) == count and ids == v.keys() == p.keys() and not ids.intersection(tasks)
        for t in group:
            qid = t['qid']
            image = (INPUTS / 'targets' if name == 'original' else REPO / 'data') / t['img']
            assert image.is_file() and p[qid]['target'] == v[qid]['target'] == t['img']
            assert p[qid]['queries'][0] == v[qid]['queries'][0] == t['caption']
            assert p[qid]['ranks'][0] == v[qid]['ranks'][0]
            tasks[qid] = (image, name)
        vanilla.update(v)
        probe.update(p)
    return tasks, vanilla, probe


def batch(pipe, state, source, seed, ranker, llm):
    pipe.client.next_seed = seed
    questions = shared.question_candidates(source, state.dialog_list(), state.query, state.scratch['context_captions'])
    decision = ranker.select(state, questions, llm, Search(pipe))
    return [dict(p, score=s if math.isfinite(s) else None)
            for p, s in zip(decision.debug['paths'], decision.scores, strict=True)]


def finished(row):
    return row['ranks'][-1] <= 10 or len(row['turns']) == shared.ROUNDS


def trajectory_matches(turns, baseline):
    return len(turns) < len(baseline) and all(
        all(t[k] == b[k] for k in ('question', 'answer', 'query_after'))
        for t, b in zip(turns, baseline))


def continue_dialogue(pipe, ranker, row, ref, target, image, path):
    from agentir.agent.state import DialogState, Turn

    qid = row['qid']
    state = DialogState(row['queries'][0], turns=[Turn(t['question'], t['answer']) for t in row['turns']],
                        query=row['queries'][-1], query_history=row['queries'].copy())
    llm = LocalLLM(pipe.client, SAMPLE_SEED + qid * 10000)
    seen = dict.fromkeys(idx for t in row['turns'] for idx in t['displayed_ids'])
    def ranking(query):
        scores = Search(pipe).similarity([query], 'rank')[0]
        order = scores.argsort().flip(0)
        return scores, order, int(shared.inverse_ranks(order)[target])
    scores, order, rank = ranking(state.query)
    assert rank == row['ranks'][-1]
    while not finished(row):
        r = len(row['turns'])
        displayed = order[:10].tolist()
        seen.update(dict.fromkeys(displayed))
        pool = order[:500 - 50*r]
        seed = 1021 + qid + 10000*r
        same = trajectory_matches(row['turns'], ref['turns'])
        if same:
            old = ref['turns'][r]
            assert state.query == old['query_before'] and pool.tolist() == old['pool_ids']
            ids, usual = old['selected_ids'], old['answer_paths']
        else:
            torch.manual_seed(1021 + 100*qid + r)
            ids = pipe.cluster_representatives(pool.flip(0))
        evidence = [pipe.captions[i]['caption'][0] for i in ids]
        state.scratch['context_captions'] = evidence
        if not same:
            usual = batch(pipe, state, pipe, seed, ranker, llm)
        rejected = generator(pipe, [pipe.captions[i]['caption'][0] for i in seen])
        extra = batch(pipe, state, rejected, seed, ranker, llm)
        assert len(usual) == len(extra) == 5
        valid = [p for p in usual + extra if p.get('score') is not None and math.isfinite(p['score'])]
        if valid:
            question = max(valid, key=lambda p: p['score'])['question']
        else:
            question = pipe.select_question(state.query, [p['question'] for p in usual], scores,
                                            10, pipe.img_embs, 500, r+1)
        if same and question == old['question']:
            answer, rewrite = old['answer'], old['query_after']
            state.turns.append(Turn(question, answer))
        else:
            answer = pipe.client.answer(image, question)
            state.turns.append(Turn(question, answer))
            rewrite = pipe.reconstruct_dialog(state.dialog_list())
            if rewrite == state.query:
                rewrite = pipe.paraphrase(rewrite)
        row['turns'].append(dict(round=r, query_before=state.query, question=question, answer=answer,
                                 query_after=rewrite, displayed_ids=displayed, selected_ids=ids,
                                 rejected_count=len(seen), usual_paths=usual, rejected_paths=extra,
                                 reused_usual_batch=same))
        state.query = rewrite
        state.query_history.append(rewrite)
        scores, order, rank = ranking(rewrite)
        row['queries'].append(rewrite)
        row['ranks'].append(rank)
        shared.atomic_json(path, row)


def report(rows, vanilla, probe):
    result = {}
    for label, subset in [('original_200','original'), ('extra_300','fresh'), ('combined_500',None)]:
        group = {i:r for i,r in rows.items() if subset is None or r['sample']==subset}
        result[label] = {}
        for name, ref in [('vanilla',vanilla), ('probe',probe)]:
            base = {i:ref[i] for i in group}
            result[label][name] = dict(by_round=summarize(group, base),
                                       baseline_questions=sum(len(r['turns']) for r in base.values()),
                                       mixed_questions=sum(len(r['turns']) for r in group.values()))
        print(label, 'mixed hits r1/r2/r5/r10',
              [result[label]['vanilla']['by_round'][r]['hits'] for r in (1,2,5,10)])
    shared.atomic_json(OUT / 'summary.json', result)


@torch.inference_mode()
def run():
    from agentir.agent.rankers import AnswerDivergenceRanker
    from agentir.config import RankerConfig

    tasks, vanilla, probe = inputs()
    rows, pending = {}, []
    paths = OUT / 'dialogues'
    for qid, (image, sample) in tasks.items():
        ref = probe[qid]
        path = paths / f'{qid}.json'
        if path.exists():
            row = shared.read_json(path)
            assert row['qid'] == qid and row['target'] == ref['target'] and row['sample'] == sample
            assert row['queries'][0] == ref['queries'][0] and row['ranks'][0] == ref['ranks'][0]
            assert len(row['ranks']) == len(row['queries']) == len(row['turns'])+1
        else:
            row = dict(qid=qid, sample=sample, target=ref['target'], queries=ref['queries'][:1],
                       ranks=ref['ranks'][:1], turns=[], hits_only=True)
            if finished(row):
                shared.atomic_json(path, row)
        rows[qid] = row
        if not finished(row):
            pending.append((qid, image, path))
    print(f'{len(rows)-len(pending)}/500 complete; {len(pending)} require GPU work.', flush=True)
    if pending:
        if not torch.backends.mps.is_available():
            raise SystemExit('Launch in macOS Terminal for Metal access.')
        worker = mac.MacQwen(SNAPSHOT, OUT)
        try:
            pipe = mac.load_blip(INPUTS, worker)
            pos = {name:i for i,name in enumerate(pipe.search_space)}
            ranker = AnswerDivergenceRanker(RankerConfig(kind='answer_divergence'))
            for n,(qid,image,path) in enumerate(pending,1):
                started = time.monotonic()
                row = rows[qid]
                continue_dialogue(pipe, ranker, row, probe[qid], pos[row['target']], image, path)
                print(f"{500-len(pending)+n}/500 · QID {qid} · {len(row['turns'])} questions · "
                      f"rank {row['ranks'][-1]} · {time.monotonic()-started:.1f}s", flush=True)
        finally:
            worker.close()
    report(rows, vanilla, probe)


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'run.lock').open('a') as lock, tempfile.TemporaryDirectory() as source:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        subprocess.run(['tar', '-xf', '-', '-C', source], input=git('archive', REVISION, 'agentir'), check=True)
        sys.path.insert(0, source)
        run()
