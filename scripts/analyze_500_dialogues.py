"""Audit saved matched AgentIR trajectories; no model calls or new dialogues.

Run from any directory with the project venv. Inputs remain outside the repo;
outputs are small, reviewable research tables and a trace appendix inside it.
"""
import argparse
import collections
import csv
import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO.parent
PATHS = {
    'vanilla': ['output/plugir-overnight-hits10-200/results/vanilla',
                'output/agentir-overnight-200-300/vanilla'],
    's2': ['output/plugir-overnight-hits10-200/results/s2_history_pool',
           'output/agentir-overnight-200-300/s2_history_pool'],
    's3': ['output/plugir-overnight-hits10-200/results/new_runs/s3_current_mmr',
           'output/agentir-overnight-200-300/s3_current_mmr'],
    'probe': ['output/agentir-overnight-200-300/probe',
              'output/agentir-probe-fresh-300/probe'],
}
ROUNDS = (1, 2, 5, 10)


def norm(text):
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def first_hit(d):
    return next((r for r, rank in enumerate(d['ranks']) if rank <= 10), 11)


def qtype(q):
    q = norm(q)
    if 'other object' in q:
        return 'generic_other_object'
    for name, pattern in [
        ('colour', r'\b(colou?r|colou?rs)\b'),
        ('count', r'\b(how many|number of)\b'),
        ('material', r'\b(material|made of)\b'),
        ('setting', r'\b(where|location|setting|background|weather|time of day|lighting|terrain|surface|sky condition)\b'),
        ('identity', r'\b(breed|species|team|airline|name|type|kind|gender|age)\b'),
        ('activity', r'\b(action|activity|doing|postur|position)\b'),
    ]:
        if re.search(pattern, q):
            return name
    return 'other'


def scene(caption):
    """Coarse lexical groups, not image-verified semantic annotations."""
    c = norm(caption)
    for name, pattern in [
        ('sports', r'\b(tennis|baseball|bat|catcher|batter|skis|ski|skiing|snowboard|skateboarder|surfing|surfer|racquet|racket)\b'),
        ('animals', r'\b(giraffe|giraffes|zebra|zebras|elephant|elephants|dog|dogs|cat|cats|sheep|bird|birds)\b'),
        ('interiors', r'\b(room|bathroom|bedroom|kitchen|bed|sink|toilet|desk|computer|computers|laptop|couch|furniture|fridge)\b'),
        ('transport_street', r'\b(train|trains|bus|buses|plane|planes|airplane|street|sign|signs|intersection|crosswalk|motorcycles|traffic)\b'),
        ('food', r'\b(food|foods|bakery|doughnuts|donuts|pizza|sandwich)\b'),
    ]:
        if re.search(pattern, c):
            return name
    return 'other'


def answer_flags(a):
    a = norm(a)
    return {
        'uncertain_answer': bool(re.search(r'\b(unsure|not sure|uncertain|cannot tell|can t tell|likely|appears|probably)\b', a)),
        'denial_answer': bool(re.search(r'\b(no|not|doesn t|isn t|aren t|without)\b', a)),
    }


def load(root=ROOT):
    data, sources = {}, []
    for arm, folders in PATHS.items():
        data[arm] = {}
        for folder, expected in zip(folders, (200, 300)):
            files = sorted((root / folder / 'dialogues').glob('*.json'))
            assert len(files) == expected, (folder, len(files))
            for f in files:
                raw = f.read_bytes()
                d = json.loads(raw)
                qid = d['qid']
                assert qid not in data[arm]
                assert len(d['ranks']) == len(d['turns']) + 1
                assert first_hit(d) <= 10 or len(d['turns']) == 10
                assert len(d['ranks']) <= 11
                if first_hit(d) <= 10:
                    assert first_hit(d) == len(d['turns'])
                for r, t in enumerate(d['turns']):
                    assert t['query_before'] == d['queries'][r]
                    assert t['query_after'] == d['queries'][r+1]
                data[arm][qid] = d
                sources.append({'arm': arm, 'qid': qid, 'path': str(f.relative_to(root)),
                                'sha256': hashlib.sha256(raw).hexdigest()})
        assert len(data[arm]) == 500
    ids = sorted(data['vanilla'])
    for arm in data:
        assert set(data[arm]) == set(ids)
        for i in ids:
            b, d = data['vanilla'][i], data[arm][i]
            assert b['target'] == d['target']
            assert b['ranks'][0] == d['ranks'][0]
            assert b['queries'][0] == d['queries'][0]
    return data, sources


def write_csv(path, rows):
    if not rows:
        return
    with path.open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def audit(root, output):
    import numpy as np
    data, sources = load(root)
    ids = sorted(data['vanilla'])
    hits = {m: {i: first_hit(d) for i, d in ds.items()} for m, ds in data.items()}
    corpus = json.loads((root / 'tmp/agentir-local-9b-inputs/Protocol/Search_Space_val_50k.json').read_text())
    positions = {name: idx for idx, name in enumerate(corpus)}
    output.mkdir(parents=True, exist_ok=True)
    dr, tr, divergences, pairs, curves, groups = [], [], [], [], [], []
    rng = np.random.default_rng(20261117)
    for arm, ds in data.items():
        for i in ids:
            d = ds[i]
            seen_q, seen_qa = set(), set()
            local_turns = []
            for r, t in enumerate(d['turns']):
                q, a = norm(t['question']), norm(t['answer'])
                target = positions[d['target']]
                row = dict(arm=arm, qid=i, round=r+1, scene=scene(d['queries'][0]),
                           final_hit=hits[arm][i] <= 10, rank_before=d['ranks'][r],
                           rank_after=d['ranks'][r+1], question=t['question'], answer=t['answer'],
                           question_type=qtype(t['question']), repeat_question=q in seen_q,
                           repeat_qa=(q, a) in seen_qa,
                           unique_candidates=len({norm(x) for x in t['candidate_questions']}),
                           unchanged_query=norm(t['query_before']) == norm(t['query_after']),
                           target_in_pool=target in t['pool_ids'],
                           target_shown=target in t['selected_ids'],
                           query_before=t['query_before'], query_after=t['query_after'],
                           **answer_flags(t['answer']))
                if 'target_in_pool' in t:
                    assert row['target_in_pool'] == t['target_in_pool']
                    assert row['target_shown'] == t['target_shown']
                local_turns.append(row)
                seen_q.add(q)
                seen_qa.add((q, a))
            tr.extend(local_turns)
            dr.append(dict(
                arm=arm, qid=i, target=d['target'], caption=d['queries'][0],
                scene=scene(d['queries'][0]), initial_rank=d['ranks'][0],
                first_hit_round=hits[arm][i] if hits[arm][i] <= 10 else '',
                success=hits[arm][i] <= 10, questions=len(local_turns),
                best_rank=min(d['ranks']), final_rank=d['ranks'][-1],
                repeated_questions=sum(t['repeat_question'] for t in local_turns),
                repeated_qa=sum(t['repeat_qa'] for t in local_turns),
                generic_questions=sum(t['question_type'] == 'generic_other_object' for t in local_turns),
                uncertain_answers=sum(t['uncertain_answer'] for t in local_turns),
                denial_answers=sum(t['denial_answer'] for t in local_turns),
                worsening_turns=sum(t['rank_after'] > t['rank_before'] for t in local_turns),
                ever_target_in_pool=any(t['target_in_pool'] for t in local_turns),
                ever_target_shown=any(t['target_shown'] for t in local_turns),
                seconds=d['seconds']))
        for r in range(11):
            gained = sum(hits[arm][i] <= r < hits['vanilla'][i] for i in ids)
            lost = sum(hits['vanilla'][i] <= r < hits[arm][i] for i in ids)
            # Exact distribution of a paired nonparametric bootstrap of {-1,0,+1}.
            samples = rng.multinomial(500, [gained/500, lost/500, 1-(gained+lost)/500], size=20000)
            ci = np.quantile((samples[:, 0]-samples[:, 1])/5, [.025, .975])
            curves.append(dict(arm=arm, round=r, hits=sum(hits[arm][i] <= r for i in ids),
                               gained=gained, lost=lost, delta_pp=(gained-lost)/5,
                               ci95_low_pp=float(ci[0]), ci95_high_pp=float(ci[1])))
    for arm in ('s2', 's3', 'probe'):
        for i in ids:
            b, d = data['vanilla'][i], data[arm][i]
            for r, (bt, dt) in enumerate(zip(b['turns'], d['turns'])):
                if all(bt[k] == dt[k] for k in ('question', 'answer', 'query_after')):
                    continue
                assert bt['query_before'] == dt['query_before']
                # Every previous exact Q/A/rewrite triple matched: full shared history.
                divergences.append(dict(
                    arm=arm, qid=i, round=r+1, query_before=bt['query_before'],
                    same_evidence=bt['selected_ids'] == dt['selected_ids'],
                    same_candidates=bt['candidate_questions'] == dt['candidate_questions'],
                    same_question=bt['question'] == dt['question'],
                    same_answer=bt['answer'] == dt['answer'],
                    vanilla_question=bt['question'], variant_question=dt['question'],
                    vanilla_answer=bt['answer'], variant_answer=dt['answer'],
                    vanilla_rewrite=bt['query_after'], variant_rewrite=dt['query_after'],
                    rank_before=b['ranks'][r], vanilla_rank_after=b['ranks'][r+1],
                    variant_rank_after=d['ranks'][r+1],
                    vanilla_final_hit=hits['vanilla'][i] <= 10,
                    variant_final_hit=hits[arm][i] <= 10))
                break
            if (hits[arm][i] <= 10) == (hits['vanilla'][i] <= 10):
                continue
            winner, loser = (arm, 'vanilla') if hits[arm][i] <= 10 else ('vanilla', arm)
            wd, ld = data[winner][i], data[loser][i]
            # These are observed signals, not mutually exclusive causal labels.
            lrow = next(x for x in dr if x['arm'] == loser and x['qid'] == i)
            j = max(range(10), key=lambda j: ld['ranks'][j+1]/ld['ranks'][j])
            t = ld['turns'][j]
            pairs.append(dict(
                variant=arm, qid=i, outcome='rescue' if winner == arm else 'loss',
                winner=winner, loser=loser, caption=ld['queries'][0], scene=scene(ld['queries'][0]),
                winner_hit_round=hits[winner][i], loser_best_rank=min(ld['ranks']),
                loser_repetition=lrow['repeated_questions'] > 0,
                loser_generic_loop=lrow['generic_questions'] >= 2,
                loser_uncertainty=lrow['uncertain_answers'] > 0,
                loser_denial=lrow['denial_answers'] > 0,
                loser_target_ever_in_pool=lrow['ever_target_in_pool'],
                loser_target_ever_shown=lrow['ever_target_shown'],
                largest_worsening_round=j+1, rank_before=ld['ranks'][j], rank_after=ld['ranks'][j+1],
                question=t['question'], answer=t['answer'], query_after=t['query_after'],
                successful_question=wd['turns'][-1]['question'],
                successful_answer=wd['turns'][-1]['answer']))
    for label in sorted({scene(data['vanilla'][i]['queries'][0]) for i in ids}):
        group = [i for i in ids if scene(data['vanilla'][i]['queries'][0]) == label and hits['vanilla'][i] > 0]
        for arm in data:
            groups.append(dict(scene=label, arm=arm, initial_misses=len(group),
                               recovered=sum(hits[arm][i] <= 10 for i in group),
                               gained=sum(hits[arm][i] <= 10 < hits['vanilla'][i] for i in group),
                               lost=sum(hits['vanilla'][i] <= 10 < hits[arm][i] for i in group)))
    notes_path = output/'review_notes.json'
    if notes_path.exists():
        notes = json.loads(notes_path.read_text())['notes']
        assert set(notes) == {str(p['qid']) for p in pairs}, 'Review notes do not cover the discordant dialogues'
        for p in pairs:
            note = notes[str(p['qid'])]
            p['qualitative_tags'] = '|'.join(note['tags'])
            p['qualitative_note'] = note['note']
    common = [i for i in ids if all(hits[m][i] > 10 for m in data)]
    summary = dict(
        sample_size=500, initial_hits=sum(hits['vanilla'][i] == 0 for i in ids),
        common_failure_ids=common, discordant_pair_count=len(pairs),
        discordant_dialogue_count=len({p['qid'] for p in pairs}),
        oracle_union={str(r): sum(any(hits[m][i] <= r for m in data) for i in ids) for r in ROUNDS},
        questions={m: sum(len(d['turns']) for d in ds.values()) for m, ds in data.items()},
        first_divergence={},
        failure_signals={},
        generic_selection={},
        conventions={'failure_first_hit': 'blank in CSV; internally 11 (beyond horizon)',
                     'ci': 'paired dialogue bootstrap, 20,000 resamples, seed 20261117; exploratory comparisons',
                     'scene': 'lexical initial-caption groups; descriptive only',
                     'signals': 'nonexclusive observations, not causal diagnoses',
                     'replay': 'matched observed branches only; no new generated answers'})
    for arm in ('s2', 's3', 'probe'):
        ds = [x for x in divergences if x['arm'] == arm]
        def stats(rows):
            return dict(n=len(rows), rank_better=sum(x['variant_rank_after'] < x['vanilla_rank_after'] for x in rows),
                        rank_worse=sum(x['variant_rank_after'] > x['vanilla_rank_after'] for x in rows),
                        rank_tied=sum(x['variant_rank_after'] == x['vanilla_rank_after'] for x in rows),
                        immediate_gained=sum(x['variant_rank_after'] <= 10 < x['vanilla_rank_after'] for x in rows),
                        immediate_lost=sum(x['vanilla_rank_after'] <= 10 < x['variant_rank_after'] for x in rows))
        summary['first_divergence'][arm] = {
            'all': stats(ds),
            'same_evidence_and_candidates': stats([x for x in ds if x['same_evidence'] and x['same_candidates']]),
            'better_immediately_but_final_loss': [x['qid'] for x in ds if x['variant_rank_after'] < x['vanilla_rank_after']
                                                 and x['vanilla_final_hit'] and not x['variant_final_hit']]}
    for arm in data:
        selected = [t for t in tr if t['arm'] == arm and norm(t['question']) == 'what is the other object in the image']
        summary['generic_selection'][arm] = dict(
            selected=len(selected), alternatives_available=sum(t['unique_candidates'] > 1 for t in selected))
        failed = [d for d in dr if d['arm'] == arm and not d['success']]
        summary['failure_signals'][arm] = dict(n=len(failed),
            repetition=sum(d['repeated_questions'] > 0 for d in failed),
            generic_loop=sum(d['generic_questions'] >= 2 for d in failed),
            ever_pool=sum(d['ever_target_in_pool'] for d in failed),
            ever_shown=sum(d['ever_target_shown'] for d in failed))
    for name, rows in [('dialogues', dr), ('turns', tr), ('first_divergence', divergences),
                       ('discordant_pairs', pairs), ('curves', curves), ('scene_groups', groups)]:
        write_csv(output / f'{name}.csv', rows)
    (output/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    (output/'sources.json').write_text(json.dumps(sources, indent=2)+'\n')
    # Include complete traces for every pair rather than only selected anecdotes.
    lines = ['# Complete discordant-pair trace audit', '',
             'All 87 variant-versus-vanilla final-outcome differences, plus six universal failures.',
             'Ranks are observed; the logs stop at the first success. No causal attribution is implied.', '']
    for p in pairs:
        lines += [f"## {p['variant']} {p['outcome']}: dialogue {p['qid']}", '', p['caption'], '']
        if 'qualitative_note' in p:
            lines += [f"Review note (across methods): {p['qualitative_note']}", '']
        for arm in ('vanilla', p['variant']):
            d = data[arm][p['qid']]
            lines += [f"### {arm}", '', f"Ranks: {d['ranks']}", '']
            for r, t in enumerate(d['turns'], 1):
                lines += [f"{r}. Q: {t['question']}\n\n   A: {t['answer']}\n\n   Rewrite: {t['query_after']}", '']
    for i in common:
        lines += [f'## Universal failure: dialogue {i}', '', data['vanilla'][i]['queries'][0], '']
        for arm in data:
            d = data[arm][i]
            lines += [f'### {arm}', '', f"Ranks: {d['ranks']}", '']
            for r, t in enumerate(d['turns'], 1):
                lines += [f"{r}. Q: {t['question']}\n\n   A: {t['answer']}\n\n   Rewrite: {t['query_after']}", '']
    (output/'trace_audit.md').write_text('\n'.join(lines))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=REPO/'analysis/combined500')
    args = parser.parse_args()
    audit(args.root, args.output)
