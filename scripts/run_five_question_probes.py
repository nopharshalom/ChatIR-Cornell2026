"""Run paired first-question comparisons on fresh VisDial examples."""
import argparse
from collections import Counter
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT / 'ChatIR-Cornell2026'
SOURCE = ROOT / 'output/plugir-overnight-hits10-200/source'
INPUTS = ROOT / 'tmp/agentir-local-9b-inputs'
REVISION = '0fb9191'
SAMPLE_SEED = 20261116
SNAPSHOT = (
    REPO / 'cache/huggingface/hub/models--mlx-community--Qwen3.5-9B-4bit'
    / 'snapshots/8b2b98c00a6b4d291155e4890773ca8f769aee53'
)


def git(*args):
    return subprocess.check_output(['git', '-C', str(REPO), *args])


class Search:
    def __init__(self, pipeline):
        from torch.nn.functional import normalize

        self.pipeline = pipeline
        self.normalize = normalize
        self.counts = Counter()

    def similarity(self, queries, purpose):
        self.counts[purpose] += len(queries)
        features = self.normalize(self.pipeline.get_text_features(queries), dim=-1)
        return features @ self.pipeline.img_embs.T


class LocalLLM:
    def __init__(self, worker, seed):
        self.worker = worker
        self.seed = seed

    def complete(self, messages, *, temperature, max_tokens, tag, seed_offset=0):
        return self.worker.complete(messages, max_tokens, temperature=temperature,
                                    seed=self.seed + seed_offset)


def compare_questions(plugir, search, initial, evidence, questions, qid):
    import torch
    from agentir.agent.rankers import AnswerDivergenceRanker, RandomRanker
    from agentir.agent.state import DialogState
    from agentir.config import RankerConfig

    state = DialogState(initial, scratch={'context_captions': evidence})
    scores = search.similarity([initial], 'initial')[0]
    start = time.perf_counter()
    kl = plugir.select_question(initial, questions, scores, 10, plugir.img_embs, 500, 1)
    kl_seconds = time.perf_counter() - start
    random_question = RandomRanker(SAMPLE_SEED).select(state, questions, None, None).question
    before = plugir.client.calls
    start = time.perf_counter()
    probe = AnswerDivergenceRanker(RankerConfig(kind='answer_divergence')).select(
        state, questions, LocalLLM(plugir.client, SAMPLE_SEED + qid * 10000), search)
    probe_seconds = time.perf_counter() - start
    valid_probes = any(torch.isfinite(torch.tensor(probe.scores)))
    return (
        {'kl': kl, 'random': random_question, 'probe': probe.question if valid_probes else kl},
        {'answer_paths': probe.debug['paths'],
         'fallback_to_kl': not valid_probes,
         'kl_selection_seconds': round(kl_seconds, 3),
         'probe_selection_seconds': round(probe_seconds, 3),
         'probe_llm_calls': plugir.client.calls - before,
         'probe_search_queries': search.counts['probe']},
    )


def candidate_ids(data, previous_cases=()):
    original = json.loads((INPUTS / 'sample.json').read_text())
    replica = json.loads((ROOT / 'output/plugir-caption-replication-hits10-200/sample.json').read_text())
    excluded = set(original['excluded_ids'])
    excluded.update(t['qid'] for t in original['tasks'] + replica['tasks'])
    excluded.update(json.loads(git('show', f'{REVISION}:configs/subsets/random200_seed20261112.json')))
    excluded.update(previous_cases)
    ids = [qid for qid in range(len(data)) if qid not in excluded]
    random.Random(SAMPLE_SEED).shuffle(ids)
    return ids


def run(output, limit, previous_cases):
    import torch

    sys.path.insert(0, str(SOURCE))
    import local_pilot as mac
    import run_dialogues as shared

    if not torch.backends.mps.is_available():
        raise SystemExit('Metal is unavailable. Run this command in the macOS Terminal app.')
    output.mkdir(parents=True, exist_ok=False)
    data = shared.read_json(REPO / 'dialogues/VisDial_v1.0_queries_val.json')
    ids = candidate_ids(data, previous_cases)
    started = time.perf_counter()
    worker = mac.MacQwen(SNAPSHOT, output)
    try:
        with torch.inference_mode():
            plugir = mac.load_blip(INPUTS, worker)
            positions = {name: i for i, name in enumerate(plugir.search_space)}
            setup_seconds = time.perf_counter() - started
            records = []
            skipped_hits = 0
            print(f'Models loaded in {setup_seconds:.1f}s', flush=True)
            for qid in ids:
                task = data[qid]
                image = REPO / 'data' / task['img']
                if task['img'] not in positions or not image.is_file():
                    continue
                target = positions[task['img']]
                initial = task['dialog'][0]
                search = Search(plugir)
                scores = search.similarity([initial], 'initial')[0]
                order = scores.argsort().flip(0)
                initial_rank = int(shared.inverse_ranks(order)[target])
                if initial_rank <= 10:
                    skipped_hits += 1
                    continue
                case_start = time.perf_counter()
                torch.manual_seed(1021 + 100 * qid)
                worker.next_seed = 1021 + qid
                selected = plugir.cluster_representatives(order[:500].flip(0))
                evidence = [plugir.captions[i]['caption'][0] for i in selected]
                questions = shared.question_candidates(plugir, [initial], initial, evidence)
                choices, details = compare_questions(plugir, search, initial, evidence, questions, qid)
                outcomes = {}
                for question in dict.fromkeys(choices.values()):
                    answer = worker.answer(image, question)
                    rewritten = plugir.reconstruct_dialog([initial, f'{question} {answer}'])
                    if rewritten == initial:
                        rewritten = plugir.paraphrase(rewritten)
                    ranks = search.similarity([rewritten], 'evaluation')[0].argsort().flip(0)
                    rank = int(shared.inverse_ranks(ranks)[target])
                    outcomes[question] = dict(question=question, answer=answer, rewrite=rewritten,
                                              rank=rank, hit=rank <= 10)
                record = dict(qid=qid, initial=initial, initial_rank=initial_rank,
                              evidence=evidence, candidates=questions, **details,
                              arms={arm: outcomes[q] for arm, q in choices.items()},
                              seconds=round(time.perf_counter() - case_start, 3))
                # A failed answer proposal can carry -inf in the upstream debug trace.
                for path in record['answer_paths']:
                    if path.get('score') == float('-inf'):
                        path['score'] = None
                records.append(record)
                shared.atomic_json(output / 'cases' / f'{qid}.json', record)
                ranks = ', '.join(f'{a}={x["rank"]}' for a, x in record['arms'].items())
                print(f'{len(records)}/{limit} · qid {qid} · start {initial_rank} · {ranks} '
                      f'· {record["seconds"]:.1f}s', flush=True)
                if len(records) == limit:
                    break
            if len(records) != limit:
                raise RuntimeError(f'Found only {len(records)} of {limit} eligible initial misses.')
            paired_probe = {}
            for baseline in ('kl', 'random'):
                pairs = [(r['arms']['probe']['hit'], r['arms'][baseline]['hit']) for r in records]
                paired_probe[baseline] = dict(
                    gained=sum(probe and not other for probe, other in pairs),
                    lost=sum(other and not probe for probe, other in pairs),
                    different_question=sum(
                        r['arms']['probe']['question'] != r['arms'][baseline]['question']
                        for r in records),
                )
            shared.atomic_json(output / 'summary.json', dict(
                seed=SAMPLE_SEED, ranker_revision=REVISION, cases=[r['qid'] for r in records],
                excluded_previous_cases=sorted(previous_cases),
                skipped_initial_hits=skipped_hits, setup_seconds=setup_seconds,
                wall_seconds=time.perf_counter() - started,
                hits={a: sum(r['arms'][a]['hit'] for r in records) for a in choices},
                paired_probe=paired_probe,
            ))
    finally:
        worker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='A new output directory')
    parser.add_argument('--limit', type=int, default=5, help='Number of initial misses to test')
    parser.add_argument('--exclude-summary', type=Path, action='append', default=[],
                        help='Prior summary.json whose cases should be excluded')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit must be positive')
    previous_cases = {
        qid for path in args.exclude_summary for qid in json.loads(path.read_text())['cases']
    }
    with tempfile.TemporaryDirectory(prefix='agentir-probe-') as source:
        subprocess.run(['tar', '-xf', '-', '-C', source],
                       input=git('archive', REVISION, 'agentir'), check=True)
        sys.path.insert(0, source)
        run(args.output.resolve(), args.limit, previous_cases)


if __name__ == '__main__':
    main()
