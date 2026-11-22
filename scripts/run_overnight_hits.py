"""Run the original-200 probe and the matched fresh-300 Hits@10 comparisons."""
import argparse
import fcntl
import hashlib
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time

import torch

from run_five_question_probes import INPUTS, REPO, REVISION, ROOT, SNAPSHOT, SOURCE
from run_five_question_probes import SAMPLE_SEED, LocalLLM, Search, candidate_ids, git

sys.path.insert(0, str(SOURCE))
import local_pilot as mac
import run_dialogues as shared

ARCHIVE = SOURCE.parent
OLD_BASELINE = ARCHIVE / 'results'
OVERNIGHT_OUTPUT = ROOT / 'output/agentir-overnight-200-300'
# The first overnight run pinned the script hash in its manifest.
ORIGINAL_RUNNER_HASH = 'ca2efe7efa7c30e7dfc90aaa2f9eb9ea5e51617c65ed092d2c6091f74838bb08'
FRESH_SEED = 20261117
QUEUE = ('probe', 'vanilla', 's2_history_pool', 's3_current_mmr')


def prepare_plan():
    reference = shared.read_json(OLD_BASELINE / 'vanilla/config.json')
    current = shared.read_json(OLD_BASELINE / 'new_runs/config.json')
    original_source = ROOT / 'output/plugir-s1-hits10-200/source'
    original = shared.read_json(ARCHIVE / 'sample.json')['tasks']
    if len(original) != 200 or [t['qid'] for t in original] != reference['sample_ids']:
        raise ValueError('Original sample IDs differ from the saved vanilla run')
    for folder, config in ((SOURCE, current), (original_source, reference)):
        for name in ('local_pilot.py', 'run_dialogues.py', 'generate_dialog.py'):
            digest = hashlib.sha256((folder / name).read_bytes()).hexdigest()
            if digest != config['source_sha256'][name]:
                raise ValueError(f'{folder / name} differs from its archived run')
    if reference['revision'] != SNAPSHOT.name or not reference['hits_only']:
        raise ValueError('The saved vanilla settings do not match this run')
    old = shared.read_records(OLD_BASELINE, 'vanilla')
    for task in original:
        record = old[task['qid']]
        if (record['target'] != task['img'] or record['queries'][0] != task['caption']
                or not record['hits_only'] or not (INPUTS / 'targets' / task['img']).is_file()):
            raise ValueError(f'Original dialogue {task["qid"]} does not match its baseline')

    data = shared.read_json(REPO / 'dialogues/VisDial_v1.0_queries_val.json')
    pilot = shared.read_json(ROOT / 'output/agentir-five-probe/summary.json')['cases']
    hundred = shared.read_json(ROOT / 'output/agentir-hundred-probe/summary.json')['cases']
    screened = set()
    for run_ids, prior in ((pilot, ()), (hundred, pilot)):
        order = candidate_ids(data, prior)
        # Exclude the whole screened prefix, including the initial hits it skipped.
        screened.update(order[:order.index(run_ids[-1]) + 1])
    corpus = set(shared.read_json(INPUTS / 'Protocol/Search_Space_val_50k.json'))
    excluded = screened | set(old)
    available = [qid for qid in candidate_ids(data, excluded)
                 if data[qid]['img'] in corpus and (REPO / 'data' / data[qid]['img']).is_file()]
    fresh = [dict(qid=qid, img=data[qid]['img'], caption=data[qid]['dialog'][0])
             for qid in random.Random(FRESH_SEED).sample(available, 300)]
    return dict(queue=list(QUEUE), original=original, fresh=fresh,
                fresh_seed=FRESH_SEED, screened_exclusions=len(screened),
                snapshot=SNAPSHOT.name, ranker_revision=REVISION,
                hits_only=True, max_rounds=shared.ROUNDS,
                baseline_source=reference['source_sha256'],
                fresh_source=current['source_sha256'],
                runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


def prepare_fresh_probe_plan():
    overnight = shared.read_json(OVERNIGHT_OUTPUT / 'plan.json')
    tasks = overnight['fresh']
    vanilla = shared.read_records(OVERNIGHT_OUTPUT, 'vanilla')
    if len(tasks) != 300 or len(vanilla) != 300 or set(vanilla) != {t['qid'] for t in tasks}:
        raise ValueError('The saved fresh sample and vanilla results must match on all 300 IDs')
    if (overnight['snapshot'], overnight['ranker_revision'], overnight['max_rounds'],
            overnight['hits_only']) != (SNAPSHOT.name, REVISION, shared.ROUNDS, True):
        raise ValueError('The overnight run settings do not match this probe run')
    for name in ('local_pilot.py', 'run_dialogues.py', 'generate_dialog.py'):
        if hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() != overnight['fresh_source'][name]:
            raise ValueError(f'{name} differs from the fresh vanilla run')
    for task in tasks:
        row = vanilla[task['qid']]
        if (row['target'] != task['img'] or row['queries'][0] != task['caption']
                or not row['hits_only'] or not (REPO / 'data' / task['img']).is_file()):
            raise ValueError(f'Fresh dialogue {task["qid"]} does not match its baseline')
    return dict(queue=['probe'], fresh=tasks, baseline=str(OVERNIGHT_OUTPUT / 'vanilla'),
                snapshot=SNAPSHOT.name, ranker_revision=REVISION, hits_only=True,
                max_rounds=shared.ROUNDS, fresh_source=overnight['fresh_source'],
                runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


@torch.inference_mode()
def run_probe(plugir, task, target_id, target_dir=INPUTS / 'targets'):
    from agentir.agent.rankers import AnswerDivergenceRanker
    from agentir.agent.state import DialogState, Turn
    from agentir.config import RankerConfig

    started = time.perf_counter()
    initial = task['caption']
    state = DialogState(initial)
    ranker = AnswerDivergenceRanker(RankerConfig(kind='answer_divergence'))
    llm = LocalLLM(plugir.client, SAMPLE_SEED + task['qid'] * 10000)
    ranks, turns = [], []
    for r in range(shared.ROUNDS + 1):
        start_turn = time.perf_counter()
        calls_before = plugir.client.calls
        query = state.query
        scores = (torch.nn.functional.normalize(plugir.get_text_features(query), dim=-1)
                  @ plugir.img_embs.T).flatten()
        order = scores.argsort().flip(0)
        ranks.append(int(shared.inverse_ranks(order)[target_id]))
        if r == shared.ROUNDS or ranks[-1] <= 10:
            break

        torch.manual_seed(1021 + 100 * task['qid'] + r)
        pool = order[:500 - 50 * r]
        selected = plugir.cluster_representatives(pool.flip(0))
        evidence = [plugir.captions[i]['caption'][0] for i in selected]
        questions = shared.question_candidates(plugir, state.dialog_list(), query, evidence)
        state.scratch['context_captions'] = evidence
        search = Search(plugir)
        start_probe = time.perf_counter()
        decision = ranker.select(state, questions, llm, search)
        probe_seconds = time.perf_counter() - start_probe
        fallback = not any(torch.isfinite(torch.tensor(decision.scores)))
        question = decision.question
        if fallback:
            question = plugir.select_question(query, questions, scores, 10, plugir.img_embs, 500, r + 1)
        answer = plugir.client.answer(target_dir / task['img'], question)
        state.turns.append(Turn(question, answer))
        updated = plugir.reconstruct_dialog(state.dialog_list())
        if updated == query:
            updated = plugir.paraphrase(updated)
        state.query = updated
        state.query_history.append(updated)
        paths = [{**path, 'score': None} if path.get('score') == float('-inf') else path
                 for path in decision.debug['paths']]
        turns.append(dict(round=r, question=question, answer=answer, query_before=query,
                          query_after=updated, candidate_questions=questions, pool_ids=pool.tolist(),
                          selected_ids=selected, selected_captions=evidence, answer_paths=paths,
                          fallback_to_kl=fallback, probe_search_queries=search.counts['probe'],
                          probe_selection_seconds=round(probe_seconds, 3),
                          llm_calls=plugir.client.calls - calls_before,
                          seconds=round(time.perf_counter() - start_turn, 3)))
    return dict(arm='probe', qid=task['qid'], target=task['img'], queries=state.query_history,
                ranks=ranks, turns=turns, seconds=round(time.perf_counter() - started, 3), hits_only=True)


def summarize(records, baseline):
    assert set(records) == set(baseline)
    if any(row['ranks'][0] != baseline[qid]['ranks'][0] for qid, row in records.items()):
        raise ValueError('Matched initial ranks differ')
    result = {}
    for r in range(shared.ROUNDS + 1):
        hits = {qid for qid, row in records.items() if min(row['ranks'][:r+1]) <= 10}
        base = {qid for qid, row in baseline.items() if min(row['ranks'][:r+1]) <= 10}
        result[r] = dict(hits=len(hits), vanilla_hits=len(base), gained=len(hits-base),
                         lost=len(base-hits), total=len(records))
    return result


def run_queue(output, plan, fresh_probe=False):
    fresh_inputs = output / 'fresh-inputs'
    queue = plan['queue']
    if fresh_probe:
        probe_baseline = shared.read_records(OVERNIGHT_OUTPUT, 'vanilla')
        probe_targets = REPO / 'data'
        print('Queue: probe on the same 300 dialogues as the saved fresh vanilla run.', flush=True)
    else:
        fresh_inputs.mkdir(exist_ok=True)
        targets = fresh_inputs / 'targets'
        if not targets.is_symlink():
            targets.symlink_to(REPO / 'data', target_is_directory=True)
        if targets.resolve() != (REPO / 'data').resolve():
            raise ValueError('The fresh target path does not point to the corpus')
        probe_baseline = shared.read_records(OLD_BASELINE, 'vanilla')
        probe_targets = INPUTS / 'targets'
        print('Queue: probe (200), vanilla (300), S2 history (300), S3 MMR (300).', flush=True)
    runs = {arm: shared.read_records(output, arm) for arm in queue}
    tasks = {arm: plan['original' if arm == 'probe' and not fresh_probe else 'fresh'] for arm in queue}
    with tempfile.TemporaryDirectory(prefix='agentir-overnight-ranker-') as source:
        subprocess.run(['tar', '-xf', '-', '-C', source],
                       input=git('archive', REVISION, 'agentir'), check=True)
        sys.path.insert(0, source)
        qwen = mac.MacQwen(SNAPSHOT, output)
        try:
            pipe = mac.load_blip(INPUTS, qwen)
            positions = {name: i for i, name in enumerate(pipe.search_space)}
            summary = {}
            for arm in queue:
                shared.ROOT = INPUTS if arm == 'probe' else fresh_inputs
                baseline = probe_baseline if arm == 'probe' else runs['vanilla']
                for i, task in enumerate(tasks[arm], 1):
                    qid = task['qid']
                    if qid in runs[arm]:
                        continue
                    qwen.next_seed = 1021 + qid
                    target = positions[task['img']]
                    if arm == 'probe':
                        rec = run_probe(pipe, task, target, probe_targets)
                    else:
                        rec = shared.run_dialogue(pipe, arm, task, target, hits_only=True)
                    if arm != 'vanilla' and rec['ranks'][0] != baseline[qid]['ranks'][0]:
                        raise ValueError(f'{arm} initial rank differs from vanilla for {qid}')
                    shared.atomic_json(output / arm / 'dialogues' / f'{qid}.json', rec)
                    runs[arm][qid] = rec
                    print(f'{arm} {i}/{len(tasks[arm])} · qid {qid} · '
                          f'{len(rec["turns"])} questions · {rec["seconds"]:.1f}s', flush=True)
                summary[arm] = summarize(runs[arm], baseline)
                shared.atomic_json(output / 'summary.json', summary)
                print(f'{arm} complete. Hits by rounds 1, 2, 5, 10: '
                      f'{[summary[arm][r]["hits"] for r in (1, 2, 5, 10)]}', flush=True)
        finally:
            qwen.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true', help='Verify and save the sample plan')
    parser.add_argument('--probe-fresh-300', action='store_true',
                        help='Run probe selection through round 10 on the saved fresh sample')
    args = parser.parse_args()
    output = ROOT / 'output/agentir-probe-fresh-300' if args.probe_fresh_300 else OVERNIGHT_OUTPUT
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('The overnight queue is already running.')
        plan_path = output / 'plan.json'
        plan = prepare_fresh_probe_plan() if args.probe_fresh_300 else prepare_plan()
        if plan_path.exists():
            saved = shared.read_json(plan_path)
            if not args.probe_fresh_300 and saved.get('runner_sha256') == ORIGINAL_RUNNER_HASH:
                # Probe on the original 200 still uses its original target directory and settings.
                plan['runner_sha256'] = ORIGINAL_RUNNER_HASH
            if saved != plan:
                raise ValueError('Run plan changed. Inspect the plan before resuming.')
        else:
            shared.atomic_json(plan_path, plan)
        counts = '300 fresh IDs' if args.probe_fresh_300 else '200 original and 300 fresh IDs'
        print(f'Plan verified: {counts}.', flush=True)
        if not args.prepare_only:
            if not torch.backends.mps.is_available():
                raise SystemExit('Metal is unavailable in this session. Launch from macOS Terminal.')
            run_queue(output, plan, fresh_probe=args.probe_fresh_300)


if __name__ == '__main__':
    main()
