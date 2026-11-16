"""Compare CPU spherical e-means with PlugIR on saved vanilla question rounds."""

import argparse
import csv
import json
from pathlib import Path
from statistics import median
from time import perf_counter

from emeans_upstream.e_means_torch import Emeans
from fast_pytorch_kmeans import KMeans
import torch
from transformers import AutoProcessor


MODEL = 'Salesforce/blip-itm-large-coco'
REVISION = '19502f1e215844f7e48bd48473f86932486d3441'


def select(labels, entropy_order, pool):
    representatives = {}
    for position in entropy_order.tolist():
        representatives.setdefault(int(labels[position]), int(pool[position]))
    return [representatives[key] for key in sorted(representatives)]


def caption_vectors(pool_ids, captions, cache, output):
    from generate_dialog import BlipForRetrieval

    cache_file = output / 'caption_features.pt'
    if cache_file.is_file():
        saved = torch.load(cache_file, weights_only=True)
        if saved['ids'] == pool_ids and saved['revision'] == REVISION:
            return saved['features']
    snapshot = cache / 'models--Salesforce--blip-itm-large-coco' / 'snapshots' / REVISION
    processor = AutoProcessor.from_pretrained(snapshot, local_files_only=True)
    model = BlipForRetrieval.from_pretrained(snapshot, local_files_only=True).eval()
    batches = []
    for start in range(0, len(pool_ids), 64):
        ids = pool_ids[start:start + 64]
        tokens = processor.tokenizer([captions[i]['caption'][0] for i in ids], padding=True,
                                     truncation=True, max_length=512, return_tensors='pt')
        vectors = model.get_text_features(input_ids=tokens.input_ids, attention_mask=tokens.attention_mask)
        batches.append(torch.nn.functional.normalize(vectors, dim=-1))
        if start % 4096 == 0:
            print(f'Encoded {start + len(ids)}/{len(pool_ids)} unique captions', flush=True)
    features = torch.cat(batches)
    temporary = cache_file.with_suffix('.tmp')
    torch.save(dict(ids=pool_ids, revision=REVISION, features=features), temporary)
    temporary.replace(cache_file)
    return features


@torch.inference_mode()
def run(args):
    torch.set_num_threads(2)
    args.output.mkdir(parents=True, exist_ok=True)
    records = [json.loads(f.read_text()) for f in sorted(args.vanilla.glob('*.json'))]
    if len(records) != 200:
        raise ValueError('Expected all 200 vanilla dialogues')
    turns = [(r, t) for r in records for t in r['turns']]
    pool_ids = sorted({i for _, t in turns for i in t['pool_ids']})
    positions = {i: j for j, i in enumerate(pool_ids)}
    captions = json.loads((args.inputs / 'Protocol/visdial_captions.json').read_text())
    indices, features = torch.load(args.inputs / 'cache/corpus_blip.pth', weights_only=True,
                                   map_location='cpu')
    assert torch.equal(indices, torch.arange(len(captions)))
    id_by_image = {row['id']: i for i, row in enumerate(captions)}
    vectors = caption_vectors(pool_ids, captions, args.cache, args.output)
    results = []
    for record, turn in turns:
        pool = list(reversed(turn['pool_ids']))
        x = features[pool].float()
        target = id_by_image[record['target']]
        assert (target in pool) == turn['target_in_pool']
        assert (target in turn['selected_ids']) == turn['target_shown']
        seed = 1021 + 100 * record['qid'] + turn['round']
        logits = vectors[[positions[i] for i in pool]] @ x.T
        probabilities = logits.softmax(dim=1)
        entropy_order = (-(probabilities * probabilities.log()).sum(dim=1)).argsort()

        torch.manual_seed(seed)
        start = perf_counter()
        baseline = KMeans(n_clusters=10, mode='cosine', verbose=0).fit_predict(x)
        kmeans_ms = 1000 * (perf_counter() - start)
        start = perf_counter()
        em = Emeans(x.shape[1], 10, seed=seed, spherical=True)
        em.train(x)
        _, labels = em.assign(x)
        emeans_ms = 1000 * (perf_counter() - start)

        old_picks = select(baseline, entropy_order, pool)
        new_picks = select(labels, entropy_order, pool)
        assert len(old_picks) <= 10 and len(new_picks) <= 10
        results.append(dict(qid=record['qid'], round=turn['round'], pool_size=len(pool),
                            target_in_pool=target in pool, vanilla_saved=turn['target_shown'],
                            vanilla_replayed=target in old_picks, emeans=target in new_picks,
                            vanilla_saved_ids=turn['selected_ids'], vanilla_replayed_ids=old_picks,
                            emeans_ids=new_picks, same_vanilla_set=set(old_picks) == set(turn['selected_ids']),
                            kmeans_ms=kmeans_ms, emeans_ms=emeans_ms,
                            emeans_batch_size=em.recipe['batch_size']))
    with (args.output / 'rounds.csv').open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    eligible = [r for r in results if r['target_in_pool']]
    methods = ('vanilla_saved', 'vanilla_replayed', 'emeans')
    summary = dict(dialogues=len(records), question_rounds=len(results),
                   question_dialogues=len({r['qid'] for r in results}), in_pool_rounds=len(eligible),
                   faiss_revision='ebab8f784c1d177129602269d5ad966c525fbdcb', torch=torch.__version__,
                   matching_vanilla_selections=sum(r['same_vanilla_set'] for r in results),
                   exposure_rounds={m: sum(r[m] for r in eligible) for m in methods},
                   exposure_dialogues={m: len({r['qid'] for r in eligible if r[m]}) for m in methods},
                   gained_vs_replayed=sum(r['emeans'] and not r['vanilla_replayed'] for r in eligible),
                   lost_vs_replayed=sum(r['vanilla_replayed'] and not r['emeans'] for r in eligible),
                   median_clustering_ms={m: median(r[f'{m}_ms'] for r in results) for m in ('kmeans', 'emeans')})
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('vanilla', 'inputs', 'cache', 'output'):
        parser.add_argument(f'--{name}', type=Path, required=True)
    run(parser.parse_args())
