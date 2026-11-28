"""Paired, full-dialogue tests of candidate captions enriched with single-photo evidence."""
import argparse
from collections import Counter
from contextlib import ExitStack, closing
import fcntl
import hashlib
import json
from pathlib import Path
import random
from unittest.mock import patch

import torch
import enrichment_evidence as evidence
from run_a2r_unseen_pilots import paired_summary
from run_matan_400 import GRID_WORKER, INPUTS, REPO, ROOT, SNAPSHOT, mac, preflight, shared

OUT = ROOT/'output/mmr-grounded-visual-pilots'
RULES = ('annotations','retrieval')
SOURCES = ('captions','pixels')
TRANSFER_THRESHOLD = 2/3
SPLIT_SEED = 57872601


def digest(data):
    return hashlib.sha256(data).hexdigest()


def prepare():
    groups, _, refs = preflight()
    tasks = {t['qid']:(t,inputs) for _, group, _, inputs in groups for t in group}
    eligible = sorted(i for i,r in refs.items() if r['ranks'][0] > 10)
    assert len(eligible) == 110
    # Splitting by initial rank alone gives both batches representation across the difficulty range.
    ordered = sorted(eligible,key=lambda i:(refs[i]['ranks'][0],i))
    rng = random.Random(SPLIT_SEED)
    first, second = [], []
    for quartile in range(4):
        group = ordered[len(ordered)*quartile//4:len(ordered)*(quartile+1)//4]
        chosen = rng.sample(group,10)
        first.extend(chosen[:5])
        second.extend(chosen[5:])
    rng.shuffle(first)
    rng.shuffle(second)
    paths = [Path(__file__),Path(evidence.__file__),GRID_WORKER,Path(mac.__file__),
             Path(shared.__file__),mac.HERE/'generate_dialog.py',INPUTS/'Protocol/visdial_captions.json',
             INPUTS/'Protocol/Search_Space_val_50k.json']
    plan = dict(model=SNAPSHOT.name, corpus=50000, mmr=.85, baseline_questions=5,
                display='ordinary top 10; original ranked results including repeats',
                maximum_questions=10, seed=SPLIT_SEED, development=first, confirmation=second,
                arms=[f'{s}_{r}' for r in RULES for s in SOURCES],
                min_known=6, min_labels_with_two_examples=2, retrieval_threshold=TRANSFER_THRESHOLD,
                sampling='5 per initial-rank quartile per split, no later outcome in sampling',
                primary='R10 cumulative Hits@10; mean cumulative R1-R5 as secondary',
                advance='pixels beats both ordinary MMR and its text twin at R10, with nonnegative early delta vs both; or ties/beats both at R10 and improves early by at least 5 percentage points vs both. Select by smaller of two R10 gains, then smaller early gain, then annotations. Require a changed question on a turn with pixel additions. Repeat the same rule on confirmation.',
                controls=digest(json.dumps(refs,sort_keys=True).encode()),
                sources={str(p.relative_to(ROOT)):digest(p.read_bytes()) for p in paths})
    path = OUT/'plan.json'
    if path.exists() and shared.read_json(path) != plan:
        raise ValueError('Saved plan differs. Existing pilot results cannot be mixed with this version.')
    shared.atomic_json(path,plan)
    return tasks, refs, plan


def run_one(pipe, audit, case, ref, target, source, rule):
    task, inputs = case
    selected, trace = [], []
    propose, choose = shared.question_candidates, pipe.select_question
    context = {}

    def representatives(method, pool, ranks, vectors, cluster):
        assert method == 's3_current_mmr'
        selected[:] = shared.history_mmr(pool,ranks,vectors,count=10,weight=.85)
        return selected.copy()

    def proposals(model, history, query, captions):
        obs = audit.inspect(source,selected,captions,query,history)
        notes = rule == 'annotations' and obs['reason'] == 'supported'
        shown = evidence.annotated(captions,obs) if notes else captions
        context.update(history=list(history),obs=obs,notes=notes)
        return propose(model,history,query,shown)

    def select(**kwargs):
        chosen = choose(**kwargs)
        obs, notes = context['obs'], context['notes']
        question = chosen
        if notes:
            guard = (evidence.standalone(chosen) and
                     audit.ask(evidence.QUESTION_CHECK,dict(dialogue=context['history'],question=chosen),
                               10).strip().upper().rstrip('.') == 'PASS')
            if not guard:
                # The slot itself has already passed the independent single-photo question screen.
                question = obs['slot']['question']
            obs['questioner_evidence'] = evidence.annotated(
                [pipe.captions[i]['caption'][0] for i in selected],obs)
            obs['guard_fallback'] = not bool(guard)
        elif rule == 'retrieval' and obs['reason'] == 'supported' and obs['transfer'] >= TRANSFER_THRESHOLD:
            question = obs['slot']['question']
        obs.update(kl_question=chosen, asked=question, annotated=notes)
        trace.append(obs)
        return question

    shared.ROOT = inputs
    pipe.client.next_seed = 1021 + task['qid']
    with patch.object(shared,'representatives',representatives), patch.object(
            shared,'question_candidates',proposals), patch.object(pipe,'select_question',select):
        row = shared.run_dialogue(pipe,'s3_current_mmr',task,target,hits_only=True)
    assert row['ranks'][0] == ref['ranks'][0] and len(trace) == len(row['turns'])
    # Reproduce each unmodified prefix exactly. Once the proposal prompt changes, the
    # sampler may have consumed a different number of tokens even if it asks the same question.
    untouched = True
    for j,(obs,turn) in enumerate(zip(trace,row['turns'])):
        assert len(turn['candidate_questions']) == 5 and obs['asked'] == turn['question']
        if not untouched:
            continue
        archived = ref['turns'][j]
        assert turn['selected_ids'] == archived['selected_ids'] and turn['query_before'] == archived['query_before']
        if not obs['annotated']:
            assert turn['candidate_questions'] == archived['candidate_questions'] and obs['kl_question'] == archived['question'], (task['qid'],j,'baseline prefix drift')
        if obs['annotated'] or turn['question'] != archived['question']:
            untouched = False
        else:
            assert (turn['answer'],turn['query_after'],row['ranks'][j+1]) == (
                archived['answer'],archived['query_after'],ref['ranks'][j+1])
    row.update(arm=f'{source}_{rule}',audit=trace)
    return row


def summary(rows, refs):
    result = paired_summary(rows,refs)
    audits = [o for r in rows.values() for o in r['audit']]
    result['rounds'] = len(audits)
    result['screen_reasons'] = dict(Counter(o['reason'] for o in audits))
    result['enriched_rounds'] = sum(o.get('filled',0)>0 for o in audits)
    result['annotated_rounds'] = sum(o['annotated'] for o in audits)
    result['direct_overrides'] = sum(o['asked'] != o['kl_question'] for o in audits if not o['annotated'])
    result['guard_fallbacks'] = sum(o.get('guard_fallback',False) for o in audits)
    result['active_answer_uncertain'] = sum(
        (o['annotated'] or o['asked'] != o['kl_question']) and any(x in t['answer'].casefold() for x in
            ('unsure','unclear','hard to tell','cannot tell',"can't tell"))
        for r in rows.values() for o,t in zip(r['audit'],r['turns']))
    return result


def decision(pixel, twin, refs):
    against_mmr, against_text = summary(pixel,refs), paired_summary(pixel,twin)
    worst_endpoint = min(x['variant'][-1]-x['control'][-1] for x in (against_mmr,against_text))
    worst_early = min(x['early_delta_pp'] for x in (against_mmr,against_text))
    differing = []
    for i,p in pixel.items():
        for t,u,o in zip(p['turns'],twin[i]['turns'],p['audit']):
            if t['question'] != u['question']:
                if (t['query_before'] == u['query_before'] and t['selected_ids'] == u['selected_ids']
                        and o.get('filled',0) and (o['annotated'] or o['asked'] != o['kl_question'])):
                    differing.append(i)
                break
    passes = bool(differing and (
        (worst_endpoint>0 and worst_early>=0) or (worst_endpoint>=0 and worst_early>=5)))
    return dict(passes=passes,endpoint=worst_endpoint,early=worst_early,
                matched_pixel_question_changes=differing,pixels_vs_mmr=against_mmr,pixels_vs_captions=against_text)


@torch.inference_mode()
def main(check):
    OUT.mkdir(parents=True,exist_ok=True)
    with ExitStack() as s:
        lock = s.enter_context((OUT/'run.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        tasks, refs, plan = prepare()
        print('Frozen 20 development + 20 disjoint confirmation IDs. Both are from the original 400.')
        if check:
            return
        pipe = None
        rules = list(RULES)
        for stage in ('development','confirmation'):
            decisions = {}
            folder = OUT/stage
            ids = plan[stage]
            for rule in rules:
                by_source = {}
                for source in SOURCES:
                    arm = f'{source}_{rule}'
                    rows = shared.read_records(folder,arm)
                    assert set(rows) <= set(ids)
                    for i in ids:
                        if i in rows:
                            continue
                        if pipe is None:
                            if not torch.backends.mps.is_available():
                                raise SystemExit('This runner needs Metal. Launch it from Mac Terminal.')
                            with patch.object(mac,'__file__',str(GRID_WORKER)):
                                worker = s.enter_context(closing(mac.MacQwen(SNAPSHOT,OUT)))
                            pipe = mac.load_blip(INPUTS,worker)
                            positions = {name:j for j,name in enumerate(pipe.search_space)}
                            audit = evidence.Audit(pipe,OUT/'evidence-cache',REPO/'data',SNAPSHOT.name,shared.atomic_json)
                        target = positions[tasks[i][0]['img']]
                        row = run_one(pipe,audit,tasks[i],refs[i],target,source,rule)
                        shared.atomic_json(folder/arm/'dialogues'/f'{i}.json',row)
                        rows[i] = row
                        print(f'{stage} {arm} {len(rows)}/{len(ids)} complete',flush=True)
                    result = summary(rows,refs)
                    shared.atomic_json(folder/arm/'summary.json',result)
                    print(f"{stage} {arm} R10 {result['variant'][-1]}/{len(rows)}, MMR {result['control'][-1]}/{len(rows)}; annotated {result['annotated_rounds']} turns, directly changed {result['direct_overrides']} questions.",flush=True)
                    by_source[source] = rows
                decisions[rule] = decision(by_source['pixels'],by_source['captions'],refs)
            shared.atomic_json(folder/'comparisons.json',decisions)
            passed = [r for r in rules if decisions[r]['passes']]
            if stage == 'development' and passed:
                best = max(passed,key=lambda r:(decisions[r]['endpoint'],decisions[r]['early']))
                rules = [best]
                print(f'Advancing {best} and its caption twin to confirmation.',flush=True)
            else:
                verdict = ('Consider the frozen rule for a paired 400-dialogue run.' if passed else
                           'Keep MMR 0.85 as the reported method. This pilot did not clear its advancement rule.')
                shared.atomic_json(OUT/'decision.json',dict(stage=stage,qualifying=passed,verdict=verdict))
                print(verdict,flush=True)
                return


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    torch.set_num_threads(4)
    main(parser.parse_args().check_only)
