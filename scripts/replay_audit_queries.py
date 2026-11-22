"""CPU-only frozen-BLIP query ablations for the combined-500 audit.

These are hand-specified diagnostic queries, NOT a learned or end-to-end policy.
Some image-assisted queries explicitly use target information (oracle diagnostics).
No Qwen calls, generated answers, or modifications to saved runs occur.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import time

from analyze_500_dialogues import REPO, ROOT, load


def specifications(data):
    cases = []

    def add(qid, arm, round_, label, query, evidence, privileged=False):
        d = data[arm][qid]
        cases.append(dict(qid=qid, arm=arm, round=round_, label=label,
                          target=d['target'], control_query=d['queries'][round_],
                          logged_control_rank=d['ranks'][round_], edited_query=query,
                          evidence=evidence, target_image_assisted=privileged))

    add(106, 'probe', 3, 'remove_unconfirmed_cat',
        'a white bathroom sink with a granite countertop, set against a mirror and lights',
        'Round 3 answers only white; cat appears in the question premise. Round 8 later denies a cat.')
    add(343, 'probe', 2, 'remove_unconfirmed_sandwich',
        'an assortment of different foods on a table, including salad, with a white plate',
        'Round 2 answers white to plate colour; sandwich exists only in the question premise.')
    add(161, 'probe', 2, 'remove_unconfirmed_second_train',
        'a green and yellow train standing at a station under a cloudy sky',
        'Round 2 answers yellow; question introduces driving along tracks with freight cars; rewrite adds another train.')
    add(161, 'probe', 1, 'colour_word_order_only',
        'a yellow and green train at a station with a cloudy sky overhead',
        'Only reverse green and yellow; same two stated colours, illustrating wording sensitivity.')
    # Find the first rewrite that imports this premise, without guessing its round.
    d = data['probe'][1542]
    for r, t in enumerate(d['turns'], 1):
        if 'fireplace' in t['query_after'] and 'fireplace' not in t['query_before']:
            q = t['query_after'].replace(', with a fireplace visible in the background', '')
            assert q != t['query_after']
            add(1542, 'probe', r, 'remove_unconfirmed_fireplace', q,
                'Question presupposes fireplace; answer confirms only television colour.')
            break
    add(1894, 'vanilla', 2, 'remove_denied_object_phrase',
        'brown wooden furniture decor in a living room with wood flooring, inside a house',
        'Simulator denies a fireplace; omit without a fireplace rather than encode the denied object.')
    # Verify exact chosen state for the denial ablation.
    d = data['vanilla'][1894]
    denial_round = next(r for r, t in enumerate(d['turns'], 1) if 'without a fireplace' in t['query_after'])
    cases[-1].update(round=denial_round, control_query=d['queries'][denial_round],
                     logged_control_rank=d['ranks'][denial_round])
    add(1648, 's2', 10, 'preserve_background_relation',
        'three black and white zebras standing and grazing on green grass with a hill in the background',
        'The question asks what is behind the zebras; answer a hill. Rewrite incorrectly puts zebras on the hill.')
    add(675, 'probe', 10, 'remove_frisbee_and_extra_player',
        'a baseball game scene featuring a crouching catcher in a red uniform with a black hat, positioned behind a batter in a white uniform, with a white ball in play',
        'Uses recorded catcher/batter facts; removes frisbee premise and extra gray-uniform player introduced during inconsistent answers.')
    add(310, 'probe', 10, 'remove_cats_image_verified',
        'a messy brown computer desk featuring a silver PC, a silver laptop, a keyboard, a mouse, papers, and a lamp',
        'Visual inspection shows no cats; simulator repeatedly answered black to cat-colour questions. Privileged correction.', True)
    add(853, 'probe', 10, 'remove_second_person_image_verified',
        'a person in a black jacket and black pants is performing a ski jump on a snowy slope with white skis',
        'Image shows one airborne skier; repeated questions about skier/snowboarder became two people. Privileged correction.', True)
    oracle = {
        310: 'a cluttered computer desk with a desktop monitor and a laptop, surrounded by maps and papers pinned on the walls',
        675: 'a baseball batter swinging a bat with a catcher crouching behind him and an umpire, with spectators in the stands',
        786: 'a tennis player in a blue sleeveless shirt and white shorts serving on a green tennis court with Garnier advertisements',
        853: 'a skier jumping off a snowy ramp with flags, snow covered mountains and a clear blue sky in the background',
        1648: 'three zebras on a grassy plain with a herd of dark animals and a wooded hill in the background',
        1743: 'a wooden bed with gray bedding and patterned pillows beside an exposed brick wall, tall windows and a white rug',
    }
    for i, query in oracle.items():
        add(i, 'vanilla', 0, 'image_assisted_missing_details', query,
            'Handwritten after inspecting target image; diagnostic of retriever headroom only, never an agent input.', True)
    captions = json.loads((ROOT/'tmp/agentir-local-9b-inputs/Protocol/visdial_captions.json').read_text())
    by_path = {c['id']: c['caption'][0] for c in captions}
    for i in oracle:
        add(i, 'vanilla', 0, 'target_stored_caption', by_path[data['vanilla'][i]['target']],
            'The stored caption of the target, issued directly as a query; oracle diagnostic only.', True)
    return cases


def run(output):
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    import torch
    from torch.nn.functional import normalize
    from transformers import AutoTokenizer, BlipForImageTextRetrieval
    torch.set_num_threads(4)
    data, _ = load()
    cases = specifications(data)
    output.mkdir(parents=True, exist_ok=True)
    (output/'query_replay_specifications.json').write_text(json.dumps(cases, indent=2)+'\n')
    snapshot = REPO/'cache/huggingface/hub/models--Salesforce--blip-itm-large-coco/snapshots/19502f1e215844f7e48bd48473f86932486d3441'
    start = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
    model = BlipForImageTextRetrieval.from_pretrained(snapshot, local_files_only=True).eval()
    inputs = ROOT/'tmp/agentir-local-9b-inputs'
    corpus = json.loads((inputs/'Protocol/Search_Space_val_50k.json').read_text())
    positions = {name: i for i, name in enumerate(corpus)}
    indices, vectors = torch.load(inputs/'cache/corpus_blip.pth', map_location='cpu', weights_only=True)
    assert torch.equal(indices, torch.arange(50000)) and len(corpus) == len(vectors) == 50000
    print(f'Model loaded on CPU in {time.perf_counter()-start:.1f}s', flush=True)
    unique = list(dict.fromkeys(q for c in cases for q in (c['control_query'], c['edited_query'])))
    features = {}
    with torch.inference_mode():
        for offset in range(0, len(unique), 4):
            batch = unique[offset:offset+4]
            tokens = tokenizer(batch, padding=True, truncation=True, max_length=512, return_tensors='pt')
            hidden = model.text_encoder(input_ids=tokens.input_ids, attention_mask=tokens.attention_mask,
                                        return_dict=True).last_hidden_state
            embeds = normalize(model.text_proj(hidden[:, 0, :]), dim=-1)
            # Archived get_text_features normalizes internally, then Search normalizes again.
            embeds = normalize(embeds, dim=-1)
            for q, emb in zip(batch, embeds):
                features[q] = emb
        rows = []
        for c in cases:
            def rank(query):
                scores = features[query] @ vectors.T
                target = positions[c['target']]
                return int((scores > scores[target]).sum())+1
            row = dict(**c, cpu_control_rank=rank(c['control_query']), edited_rank=rank(c['edited_query']))
            row['control_rank_difference'] = row['cpu_control_rank']-c['logged_control_rank']
            rows.append(row)
            print(f"{c['qid']} {c['label']}: saved {c['logged_control_rank']}, CPU {row['cpu_control_rank']}, edited {row['edited_rank']}", flush=True)
    with (output/'query_replays.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    (output/'query_replay_metadata.json').write_text(json.dumps(dict(
        model_revision=snapshot.name, device='cpu', rank_definition='1 + count(strictly higher scores)',
        cases=len(cases), elapsed_seconds=time.perf_counter()-start,
        max_control_rank_difference=max(abs(r['control_rank_difference']) for r in rows),
        limitations='Handpicked, handwritten query ablations; some use target images. No new question/answer generation, no end-to-end evaluation.'
    ), indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=REPO/'analysis/combined500')
    run(parser.parse_args().output)
