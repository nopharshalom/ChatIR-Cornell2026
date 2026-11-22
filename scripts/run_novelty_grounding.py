"""Resume two isolated PlugIR ablations on the original matched 200 dialogues."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys
import time

import torch
from run_five_question_probes import INPUTS, REPO, ROOT, SNAPSHOT, SOURCE

sys.path.insert(0, str(SOURCE))
import local_pilot as mac
import run_dialogues as shared

OUTPUT = ROOT/'output/agentir-novelty-grounding-200'
ARMS = ('novelty', 'grounded_rewrite')
NOVELTY_PROMPT = '''Judge candidate questions for a target-image search using only the initial
description and previous user answers. Return JSON only: {"decisions": [
{"id": 0, "novel": true, "reason": "short reason"}, ...]} with one decision per candidate.
A question is novel if its answer would supply information not already available.
Reject paraphrases of answered questions, known attributes, and repeated generic object
questions that previously elicited the same object. Unknown or unsure previous answers
do not establish a fact, but repeating the identical question without clarification is
not useful. Allow an explicit clarification/correction of conflicting or ambiguous facts.
Reject malformed questions. Do not infer answers from candidate captions. Judge novelty,
not how interesting or important the question sounds. No target image is available.'''
REGENERATE_PROMPT = '''Generate three short, distinct questions that could help identify a
target image. Use the initial description, dialogue and retrieved candidate captions.
Ask about unresolved distinguishing attributes, relations or setting. Do not repeat
answered questions or the listed rejected questions. Retrieved captions are possible
images, not facts about the target; avoid assuming unconfirmed entities exist. Allow
clarification of conflicting information. Return JSON only:
{"questions": ["First question?", "Second question?", "Third question?"]}.
Each entry must be one complete question ending in a question mark.'''
GROUNDING_PROMPT = '''Write one concise image-search description using the initial caption
and actual user answers in the dialogue. Output only that description, no explanation.
Maintain the main scene, entities and their relationships. The initial caption may be
corrected by explicit user answers. A question's premise is NOT a confirmed fact: an
answer about an object's colour does not establish other objects mentioned in the question.
Do not invent extra people or objects to reconcile different descriptions of one entity.
Preserve which attribute belongs to which entity and whether an object is behind, on,
beside, or held by another. Ignore unsure answers as new facts. If an object is explicitly
denied, remove unsupported references to it rather than emphasizing its absence. Do not
turn unknown details into negative claims. Prefer explicit, specific corrections over
earlier conflicting claims, and omit unresolved conflicting attributes. Do not add facts
from your general knowledge. Preserve useful confirmed details without repetition.'''


def normalized(s):
    return re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()


def parse_object(raw):
    text = raw.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError('Expected JSON object')
    return obj


def novel_options(worker, initial, history, candidates):
    payload = dict(initial=initial, dialogue=history,
                   candidates=[dict(id=i, question=q) for i, q in enumerate(candidates)])
    raw = worker.complete([dict(role='system', content=NOVELTY_PROMPT),
                           dict(role='user', content=json.dumps(payload))], 384, temperature=0)
    try:
        decisions = parse_object(raw)['decisions']
        if not isinstance(decisions, list) or len(decisions) != len(candidates):
            raise ValueError('One decision required per candidate')
        by_id = {}
        for d in decisions:
            if type(d['id']) is not int or type(d['novel']) is not bool or d['id'] in by_id:
                raise ValueError('Invalid/duplicate candidate decision')
            by_id[d['id']] = d
        if set(by_id) != set(range(len(candidates))):
            raise ValueError('Candidate IDs differ')
        repeated = {normalized(t['question']) for t in history}
        accepted = [q for i, q in enumerate(candidates)
                    if by_id[i]['novel'] and normalized(q) not in repeated]
        return list(dict.fromkeys(accepted)), dict(raw=raw, valid=True, decisions=decisions)
    except (KeyError, TypeError, ValueError) as e:
        return [], dict(raw=raw, valid=False, error=str(e))


def novelty_selection(pipe, initial, history, candidates, evidence, query, scores, round_):
    debug = dict(judgements=[], regenerations=[], fallback=False)
    accepted, judgement = novel_options(pipe.client, initial, history, candidates)
    debug['judgements'].append(judgement)
    rejected = list(candidates)
    for attempt in range(2):
        if accepted:
            break
        raw = pipe.client.complete([
            dict(role='system', content=REGENERATE_PROMPT),
            dict(role='user', content=json.dumps(dict(initial=initial, dialogue=history,
                 candidate_captions=evidence, rejected_questions=rejected)))], 192, temperature=0)
        try:
            qs = parse_object(raw)['questions']
            if (not isinstance(qs, list) or not 1 <= len(qs) <= 3
                    or any(not isinstance(q, str) or not q.strip().endswith('?') for q in qs)):
                raise ValueError('Expected up to three complete questions')
            qs = list(dict.fromkeys(q.strip() for q in qs))
            accepted, judgement = novel_options(pipe.client, initial, history, qs)
            debug['judgements'].append(judgement)
            rejected.extend(qs)
            debug['regenerations'].append(dict(raw=raw, valid=True, questions=qs))
        except (KeyError, TypeError, ValueError) as e:
            debug['regenerations'].append(dict(raw=raw, valid=False, error=str(e)))
    if not accepted:
        # Keep the original ten-question protocol; do not silently abort difficult cases.
        accepted = candidates
        debug['fallback'] = True
    debug['eligible_questions'] = accepted
    question = pipe.select_question(caption_recon=query, questions=accepted, cossim_prev=scores,
                                   k=10, img_embs=pipe.img_embs, threshold=500, round=round_)
    return question, debug


def grounded_rewrite(pipe, initial, history, dialogue):
    raw = pipe.client.complete([
        dict(role='system', content=GROUNDING_PROMPT),
        dict(role='user', content=json.dumps(dict(initial=initial, dialogue=history)))],
        512, temperature=0)
    text = raw.strip().strip('"')
    valid = bool(text) and not text.startswith(('```', '{', '[')) and '\n' not in text and not text.endswith('?')
    if valid:
        return text, dict(raw=raw, fallback=False)
    return pipe.reconstruct_dialog(dialogue), dict(raw=raw, fallback=True)


@torch.inference_mode()
def run_dialogue(pipe, arm, task, target):
    started = time.perf_counter()
    initial = task['caption']
    dialogue, queries, ranks, turns, history = [initial], [initial], [], [], []
    for r in range(11):
        query = queries[-1]
        scores = (torch.nn.functional.normalize(pipe.get_text_features(query), dim=-1) @ pipe.img_embs.T).flatten()
        order = scores.argsort().flip(0)
        ranks.append(int(shared.inverse_ranks(order)[target]))
        if r == 10 or ranks[-1] <= 10:
            break
        start_turn, before = time.perf_counter(), pipe.client.calls
        torch.manual_seed(1021 + 100*task['qid'] + r)
        pool = order[:500-50*r]
        selected = pipe.cluster_representatives(pool.flip(0))
        evidence = [pipe.captions[i]['caption'][0] for i in selected]
        questions = shared.question_candidates(pipe, dialogue, query, evidence)
        debug = {}
        if arm == 'novelty':
            question, debug['novelty'] = novelty_selection(pipe, initial, history, questions, evidence, query, scores, r+1)
        else:
            question = pipe.select_question(caption_recon=query, questions=questions, cossim_prev=scores,
                                           k=10, img_embs=pipe.img_embs, threshold=500, round=r+1)
        answer = pipe.client.answer(INPUTS/'targets'/task['img'], question)
        history.append(dict(question=question, answer=answer))
        dialogue.append(f'{question} {answer}')
        if arm == 'grounded_rewrite':
            updated, debug['grounding'] = grounded_rewrite(pipe, initial, history, dialogue)
            # A justified unchanged description stays unchanged in this ablation.
        else:
            updated = pipe.reconstruct_dialog(dialogue)
            if updated == query:
                updated = pipe.paraphrase(updated)
        queries.append(updated)
        turns.append(dict(round=r, question=question, answer=answer, query_before=query,
                          query_after=updated, candidate_questions=questions, pool_ids=pool.tolist(),
                          selected_ids=selected, selected_captions=evidence,
                          target_in_pool=bool((pool == target).any()), target_shown=target in selected,
                          llm_calls=pipe.client.calls-before, seconds=round(time.perf_counter()-start_turn, 3),
                          **debug))
        print(f'{arm} qid {task["qid"]} round {r+1}/10 saved in memory · {turns[-1]["seconds"]:.1f}s', flush=True)
    return dict(arm=arm, qid=task['qid'], target=task['img'], queries=queries, ranks=ranks,
                turns=turns, seconds=round(time.perf_counter()-started, 3), hits_only=True)


def prepare_plan():
    archive = SOURCE.parent
    config = shared.read_json(archive/'results/new_runs/config.json')
    for name, digest in config['source_sha256'].items():
        if name in ('local_pilot.py', 'run_dialogues.py', 'generate_dialog.py'):
            assert hashlib.sha256((SOURCE/name).read_bytes()).hexdigest() == digest
    tasks = shared.read_json(archive/'sample.json')['tasks']
    baseline = shared.read_records(archive/'results', 'vanilla')
    assert len(tasks) == len(baseline) == 200 and {t['qid'] for t in tasks} == set(baseline)
    for t in tasks:
        b = baseline[t['qid']]
        assert b['target'] == t['img'] and b['queries'][0] == t['caption']
        assert (INPUTS/'targets'/t['img']).is_file()
    plan = dict(arms=list(ARMS), tasks=tasks, snapshot=SNAPSHOT.name, hits_only=True, max_rounds=10,
                source_sha256=config['source_sha256'], runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                baseline_sha256={str(i): hashlib.sha256((archive/f'results/vanilla/dialogues/{i}.json').read_bytes()).hexdigest()
                                 for i in baseline},
                prompts=dict(novelty=NOVELTY_PROMPT, regeneration=REGENERATE_PROMPT, grounding=GROUNDING_PROMPT))
    return plan, baseline


def summarize(runs, baseline):
    summary = {}
    for arm, records in runs.items():
        ids = set(records)
        summary[arm] = dict(completed=len(ids), total=200, rounds={})
        for r in (0, 1, 2, 5, 10):
            hits = {i for i in ids if min(records[i]['ranks'][:r+1]) <= 10}
            ref = {i for i in ids if min(baseline[i]['ranks'][:r+1]) <= 10}
            summary[arm]['rounds'][r] = dict(hits=len(hits), matched_vanilla_hits=len(ref),
                                             gained=len(hits-ref), lost=len(ref-hits))
    return summary


def run_queue(output, plan, baseline):
    runs = {arm: shared.read_records(output, arm) for arm in ARMS}
    for arm, records in runs.items():
        assert set(records) <= set(baseline)
        for i, d in records.items():
            assert d['target'] == baseline[i]['target'] and d['queries'][0] == baseline[i]['queries'][0]
            assert d['ranks'][0] == baseline[i]['ranks'][0]
    stop = output/'STOP'
    # Explicitly launching the command resumes a previously requested stop.
    stop.unlink(missing_ok=True)
    if all(len(rs) == 200 for rs in runs.values()):
        print('Both runs already complete.'); return
    worker = mac.MacQwen(SNAPSHOT, output)
    try:
        pipe = mac.load_blip(INPUTS, worker)
        positions = {name: i for i, name in enumerate(pipe.search_space)}
        for arm in ARMS:
            for task in plan['tasks']:
                if stop.exists():
                    shared.atomic_json(output/'status.json', dict(state='stopped', arm=arm))
                    print('STOPPED: completed dialogues are saved. Safe to close after the terminal prompt returns.', flush=True)
                    return
                qid = task['qid']
                if qid in runs[arm]:
                    continue
                shared.atomic_json(output/'status.json', dict(state='running', arm=arm, qid=qid))
                worker.next_seed = 1021+qid
                record = run_dialogue(pipe, arm, task, positions[task['img']])
                assert record['ranks'][0] == baseline[qid]['ranks'][0]
                shared.atomic_json(output/arm/'dialogues'/f'{qid}.json', record)
                runs[arm][qid] = record
                shared.atomic_json(output/'summary.json', summarize(runs, baseline))
                print(f'SAVED {arm} {len(runs[arm])}/200 · qid {qid} · {len(record["turns"])} questions · {record["seconds"]:.1f}s', flush=True)
        shared.atomic_json(output/'status.json', dict(state='complete'))
        print('COMPLETE: both 200-dialogue runs saved.', flush=True)
    except KeyboardInterrupt:
        shared.atomic_json(output/'status.json', dict(state='interrupted'))
        print('\nInterrupted. Completed dialogues are saved; the unfinished dialogue restarts on resume.', flush=True)
    finally:
        worker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--stop', action='store_true', help='Ask a running queue to stop after saving its current dialogue')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.stop:
        (output/'STOP').touch()
        print('Stop requested. Wait for the running terminal to return to its prompt before closing the lid.')
        return
    with (output/'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('This queue is already running.')
        plan, baseline = prepare_plan()
        path = output/'plan.json'
        if path.exists():
            if shared.read_json(path) != plan:
                raise SystemExit('Plan or code changed; use a new output directory to avoid mixing experiments.')
        else:
            shared.atomic_json(path, plan)
        print('Plan verified: novelty, then grounded rewrite; original 200 dialogues.', flush=True)
        if not args.prepare_only:
            if not torch.backends.mps.is_available():
                raise SystemExit('Metal unavailable here. Launch this command from macOS Terminal.')
            run_queue(output, plan, baseline)


if __name__ == '__main__':
    main()
