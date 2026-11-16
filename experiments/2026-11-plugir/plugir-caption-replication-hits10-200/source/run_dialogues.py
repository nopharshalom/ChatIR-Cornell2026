"""Paired full-dialogue PlugIR ablations with Drive checkpoints."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import random
from pathlib import Path
from types import SimpleNamespace
import time
import traceback
import uuid

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent
ARMS = ('vanilla', 's1_original_current', 's2_history_pool',
        's3_current_top10', 's3_fused_top10', 's3_history_mmr',
        's2_original_current_pool', 's3_current_mmr', 's3_split_5_5', 'target_caption',
        'random_caption_swap')
ROUNDS = 2
QWEN = 'Qwen/Qwen3.5-9B'
BLIP = 'Salesforce/blip-itm-large-coco'
BLIP_REVISION = '19502f1e215844f7e48bd48473f86932486d3441'


def utc():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(obj, indent=2) + '\n')
    temp.replace(path)


def read_json(path):
    return json.loads(path.read_text())


def inverse_ranks(order):
    """One-based ranks indexed by corpus position."""
    ranks = torch.empty_like(order)
    ranks[order] = torch.arange(1, len(order) + 1, device=order.device)
    return ranks


def fused_order(rank_history, queries):
    ranks = [rank_history[q] for q in dict.fromkeys(queries)]
    if len(ranks) == 1:
        return ranks[0].argsort()
    device = ranks[0].device
    if device.type == 'mps':
        # Preserve the float64 score and tie order on Apple Silicon.
        ranks = [rank.cpu() for rank in ranks]
    score = sum(1.0 / (60 + rank.double()) for rank in ranks)
    return score.argsort(descending=True, stable=True).to(device)


def history_mmr(pool, fused_ranks, embeddings, count=10, weight=.7):
    """Match S3's reciprocal-rank relevance, max cosine penalty and ID tie break."""
    ids = np.asarray(sorted(pool.tolist()), dtype=int)
    relevance = 1 / (60 + fused_ranks[ids.tolist()].cpu().numpy().astype(float))
    spread = np.ptp(relevance)
    relevance = (relevance - relevance.min()) / spread if spread > 1e-12 else np.zeros(len(ids))
    features = embeddings[ids.tolist()].float()
    similarity = (features @ features.T).clamp(-1, 1).cpu().numpy()
    available = np.ones(len(ids), dtype=bool)
    chosen = []
    for _ in range(min(count, len(ids))):
        score = relevance.copy()
        if chosen:
            score = weight * relevance - (1 - weight) * similarity[:, chosen].max(axis=1)
        score[~available] = -np.inf
        pos = int(score.argmax())
        available[pos] = False
        chosen.append(pos)
    return ids[chosen].tolist()


def rankings(arm, queries, rank_history, current_order, budget):
    """Return user-facing ranking and question-evidence pool, both best first."""
    if arm == 's1_original_current':
        order = fused_order(rank_history, (queries[0], queries[-1]))
        return order, order[:budget]
    if arm == 's2_history_pool':
        return current_order, fused_order(rank_history, queries)[:budget]
    if arm == 's2_original_current_pool':
        return current_order, fused_order(rank_history, (queries[0], queries[-1]))[:budget]
    return current_order, current_order[:budget]


def representatives(arm, pool, history_rank, embeddings, cluster):
    if arm == 's3_current_top10':
        return pool[:10].tolist()
    if arm == 's3_fused_top10':
        # Sort ID first for the same stable tie rule used by rank fusion.
        ids = pool.sort().values
        return ids[history_rank[ids].argsort(stable=True)[:10]].tolist()
    if arm == 's3_history_mmr':
        return history_mmr(pool, history_rank, embeddings)
    if arm == 's3_current_mmr':
        return history_mmr(pool, history_rank, embeddings)
    if arm == 's3_split_5_5':
        current = pool[:5].tolist()
        remaining = pool[5:].sort().values
        past = remaining[history_rank[remaining].argsort(stable=True)[:5]].tolist()
        return current + past
    # The original implementation passes the top candidates worst to best.
    return cluster(pool.flip(0))


class QwenChat:
    def __init__(self, revision):
        from transformers import AutoProcessor, BitsAndBytesConfig, Qwen3_5ForConditionalGeneration

        self.processor = AutoProcessor.from_pretrained(QWEN, revision=revision)
        self.dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        self.model = Qwen3_5ForConditionalGeneration.from_pretrained(
            QWEN, revision=revision, device_map={'': 0}, dtype=self.dtype,
            attn_implementation='sdpa',
            quantization_config=BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type='nf4',
                bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=self.dtype)).eval()
        self.calls = 0

    @torch.inference_mode()
    def generate(self, messages, max_tokens, temperature=0, top_p=1, seed=None, image=None):
        from transformers import GenerationConfig

        if image is None:
            tokenizer = self.processor.tokenizer
            prompt = self.processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
            inputs = tokenizer(prompt, return_tensors='pt', add_special_tokens=False)
        else:
            inputs = self.processor.apply_chat_template(
                messages, tokenize=True, return_dict=True, return_tensors='pt',
                add_generation_prompt=True, enable_thinking=False)
        inputs = inputs.to(self.model.device)
        if 'pixel_values' in inputs:
            inputs['pixel_values'] = inputs['pixel_values'].to(self.dtype)
        cfg = self.model.generation_config
        config = GenerationConfig(
            max_new_tokens=max_tokens, do_sample=temperature > 0, use_cache=True,
            eos_token_id=cfg.eos_token_id, pad_token_id=cfg.pad_token_id,
            bos_token_id=cfg.bos_token_id, repetition_penalty=1.0)
        if temperature > 0:
            config.temperature, config.top_p, config.top_k = temperature, top_p, 0
        with torch.random.fork_rng(devices=[0], enabled=seed is not None):
            if seed is not None:
                torch.manual_seed(seed)
            output = self.model.generate(**inputs, generation_config=config)
        self.calls += 1
        text = self.processor.tokenizer.decode(
            output[0, inputs['input_ids'].shape[1]:], skip_special_tokens=True).strip()
        if '</think>' in text:
            text = text.split('</think>', 1)[1].strip()
        if not text:
            raise RuntimeError('Qwen returned an empty answer.')
        return text

    def create(self, messages, n=1, max_tokens=512, **kwargs):
        return {'choices': [{'message': {'content': self.generate(messages, max_tokens, **kwargs)}}
                            for _ in range(n)]}

    def answer(self, image, question):
        from PIL import Image

        with Image.open(image) as file:
            picture = file.convert('RGB')
        picture.thumbnail((768, 768))
        messages = [
            {'role': 'system', 'content': [{'type': 'text', 'text':
             'Answer the question using only the target image. '
             'Be brief and say you are unsure when the image does not show the answer.'}]},
            {'role': 'user', 'content': [
                {'type': 'image', 'image': picture}, {'type': 'text', 'text': question}]},
        ]
        return self.generate(messages, 48, image=picture)


def load_pipeline(qwen_revision):
    import generate_dialog as plugir
    from fast_pytorch_kmeans import KMeans
    from transformers import AutoProcessor

    plugir.device = 'cuda:0'
    plugir.args = SimpleNamespace(llm_backend='cuda', n_clusters=10, cluster_on_cpu=True,
                                  early_stop_questions=False, cache_filter_results=False)
    plugir.SEED = 1021
    plugir.processor = AutoProcessor.from_pretrained(BLIP, revision=BLIP_REVISION)
    plugir.blip = plugir.BlipForRetrieval.from_pretrained(
        BLIP, revision=BLIP_REVISION).to(plugir.device).eval()
    # Use a processor/tokenizer call that is stable across Transformers versions.
    def text_features(text):
        tokenized = plugir.processor.tokenizer(
            text, padding=True, truncation=True, max_length=512, return_tensors='pt')
        return plugir.blip.get_text_features(
            input_ids=tokenized.input_ids.to(plugir.device),
            attention_mask=tokenized.attention_mask.to(plugir.device))
    plugir.get_text_features = text_features
    plugir.search_space = read_json(ROOT / 'Protocol/Search_Space_val_50k.json')
    plugir.captions = read_json(ROOT / 'Protocol/visdial_captions.json')
    cache = torch.load(ROOT / 'cache/corpus_blip.pth', map_location=plugir.device, weights_only=True)
    plugir.img_embs = cache[1]
    if (len(plugir.search_space) != 50_000 or len(plugir.img_embs) != 50_000
            or [x['id'] for x in plugir.captions] != plugir.search_space):
        raise ValueError('The image embeddings, captions and 50k corpus manifest must match.')
    # The cache stores corpus positions as well as the embedding matrix.
    if not torch.equal(cache[0], torch.arange(50_000, device=plugir.device)):
        raise ValueError('The cached BLIP corpus order differs from the manifest.')
    plugir.caption_features_cache = {}
    plugir.kmeans = KMeans(n_clusters=10, mode='cosine', verbose=0)
    plugir.client = QwenChat(qwen_revision)
    return plugir


def question_candidates(plugir, dialogue, query, evidence):
    """PlugIR's existing five-question generation/filter loop."""
    questions, prior = [], []
    prompt = ''.join(f'{i}. {text}\n' for i, text in enumerate(evidence))
    for _ in range(5):
        for _ in range(3):
            result = plugir.generate_questions_referring(
                descrip=dialogue, prompt_fake=prompt, n=1, ques_prior=questions + prior)
            question = result['choices'][0]['message']['content'].split('?')[0] + '?'
            if 'uncertain' not in plugir.filter_questions(query, question):
                prior.append(question)
            else:
                break
        if len(prior) == 3:
            result = plugir.generate_questions(dialogue, n=1)
            question = result['choices'][0]['message']['content'].split('?')[0] + '?'
            if 'uncertain' not in plugir.filter_questions(query, question):
                question = 'what is the other object in the image?'
        questions.append(question)
    return questions


@torch.inference_mode()
def run_dialogue(plugir, arm, task, target_id, hits_only=False):
    started = time.perf_counter()
    started_at = utc()
    initial = task['caption']
    dialogue, queries, ranks, turns = [initial], [initial], [], []
    rank_history = {}
    for round_index in range(ROUNDS + 1):
        turn_started = time.perf_counter()
        calls_before = plugir.client.calls
        query = queries[-1]
        query_vector = torch.nn.functional.normalize(plugir.get_text_features(query), dim=-1)
        scores = (query_vector @ plugir.img_embs.T).flatten()
        current_order = scores.argsort().flip(0)
        rank_history[query] = inverse_ranks(current_order)
        budget = 500 - 50 * round_index
        result, pool = rankings(arm, queries, rank_history, current_order, budget)
        ranks.append(int(inverse_ranks(result)[target_id]))
        if round_index == ROUNDS or (hits_only and ranks[-1] <= 10):
            break
        # Keep matching random streams for a task/round across variants and resumes.
        seed = 1021 + 100 * task['qid'] + round_index
        torch.manual_seed(seed)
        history_rank = inverse_ranks(fused_order(rank_history, queries)) if arm in (
            's3_fused_top10', 's3_history_mmr') else None
        if arm == 's3_current_mmr':
            history_rank = rank_history[query]
        if arm == 's3_split_5_5':
            history_rank = inverse_ranks(fused_order(rank_history, queries[:-1] or queries))
        ids = representatives(arm, pool, history_rank, plugir.img_embs, plugir.cluster_representatives)
        exposure = {}
        if arm in ('target_caption', 'random_caption_swap'):
            original_ids = list(ids)
            slot = None
            replacement = None
            if target_id not in ids:
                rng = random.Random(seed)
                slot = rng.randrange(len(ids))
                replacement = target_id
                if arm == 'random_caption_swap':
                    excluded = set(ids) | {target_id}
                    replacement = rng.choice([i for i in pool.tolist() if i not in excluded])
                ids[slot] = replacement
            exposure = dict(original_selected_ids=original_ids, injected_slot=slot,
                            replacement_id=replacement)
        evidence = [plugir.captions[i]['caption'][0] for i in ids]
        questions = question_candidates(plugir, dialogue, query, evidence)
        # The original question ranker still uses the current-query cosine scores.
        question = plugir.select_question(
            caption_recon=query, questions=questions, cossim_prev=scores, k=10,
            img_embs=plugir.img_embs, threshold=500, round=round_index + 1)
        answer = plugir.client.answer(ROOT / 'targets' / task['img'], question)
        dialogue.append(f'{question} {answer}')
        updated = plugir.reconstruct_dialog(dialogue)
        if updated == query:
            updated = plugir.paraphrase(updated)
        queries.append(updated)
        turns.append(dict(
            round=round_index, question=question, answer=answer, query_before=query,
            query_after=updated, candidate_questions=questions, pool_ids=pool.tolist(),
            selected_ids=ids, selected_captions=evidence,
            target_in_pool=bool((pool == target_id).any()), target_shown=target_id in ids,
            llm_calls=plugir.client.calls - calls_before,
            seconds=round(time.perf_counter() - turn_started, 3), **exposure))
        print(f'{arm} task {task["qid"]}, answer {round_index + 1}/{ROUNDS}: '
              f'{turns[-1]["seconds"]:.1f}s', flush=True)
    record = dict(arm=arm, qid=task['qid'], target=task['img'], queries=queries, ranks=ranks,
                  turns=turns, started_utc=started_at, finished_utc=utc(),
                  seconds=round(time.perf_counter() - started, 3))
    if hits_only:
        record['hits_only'] = True
    return record


def read_records(out, arm):
    records = {}
    for path in sorted((out / arm / 'dialogues').glob('*.json')):
        rec = read_json(path)
        ranks = rec['ranks']
        if rec.get('hits_only'):
            length_ok = (1 <= len(ranks) <= ROUNDS + 1 and
                         all(r > 10 for r in ranks[:-1]) and
                         (len(ranks) == ROUNDS + 1 or ranks[-1] <= 10))
        else:
            length_ok = len(ranks) == ROUNDS + 1
        if (rec['arm'] != arm or path.stem != str(rec['qid']) or not length_ok
                or len(rec['turns']) + 1 != len(ranks) or
                not all(1 <= r <= 50_000 for r in ranks)):
            raise ValueError(f'Invalid completed checkpoint: {path}')
        records[rec['qid']] = rec
    return records


def curves(ranks):
    values = {}
    for k in (10, 50):
        hit = ranks <= k
        values[f'Hits@{k}'] = np.maximum.accumulate(hit, axis=1).mean(axis=0) * 100
        values[f'Recall@{k}'] = hit.mean(axis=0) * 100
    return values


def cumulative_hits(records):
    """Carry a first hit forward without inventing any post-hit ranks."""
    rounds = np.arange(ROUNDS + 1)
    return {
        f'Hits@{k}': (np.array([
            next((r for r, rank in enumerate(rec['ranks']) if rank <= k), ROUNDS + 1)
            for rec in records
        ])[:, None] <= rounds).mean(axis=0) * 100
        for k in (10, 50)
    }


def report(out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    if not out.exists():
        raise ValueError(f'No run folder yet: {out}')
    config_path = out / 'config.json'
    config = read_json(config_path) if config_path.is_file() else {}
    hits_only = config.get('hits_only', False)
    records = {arm: read_records(out, arm) for arm in ARMS}
    if any(bool(rec.get('hits_only')) != hits_only for rows in records.values() for rec in rows.values()):
        raise ValueError('Full-dialogue and Hits-only checkpoints require separate run folders.')
    for arm, done in records.items():
        print(f'{arm}: {len(done)} saved dialogues, '
              f'{sum(r["seconds"] for r in done.values()) / 3600:.2f} dialogue hours')
    for path in sorted((out / 'attempts').glob('*.json')):
        attempt = read_json(path)
        print(f'{attempt["started_utc"]} | {attempt["status"]} | '
              f'{attempt["elapsed_seconds"] / 3600:.2f} h through {attempt["updated_utc"]}')
    figures = out / 'figures'
    figures.mkdir(exist_ok=True)
    for label, group in [('S1', ARMS[1:2]), ('S2', ARMS[2:3]), ('S3', ARMS[3:])]:
        variants = [arm for arm in group if records[arm]]
        if not variants or not records['vanilla']:
            continue
        arms = ['vanilla', *variants]
        matched = sorted(set.intersection(*(set(records[a]) for a in arms)))
        if not matched:
            continue
        matched_records = {arm: [records[arm][qid] for qid in matched] for arm in arms}
        results = {arm: cumulative_hits(rows) if hits_only else
                   curves(np.array([r['ranks'] for r in rows]))
                   for arm, rows in matched_records.items()}
        names = {'vanilla':'Vanilla PlugIR', 's1_original_current':'Original + current RRF',
                 's2_history_pool':'History pool', 's3_current_top10':'Current top 10',
                 's3_fused_top10':'Fused top 10', 's3_history_mmr':'History MMR'}
        metrics = tuple(config.get('report_metrics', ('Hits@10', 'Hits@50'))) if hits_only else (
            'Hits@10', 'Hits@50', 'Recall@10', 'Recall@50')
        columns = min(2, len(metrics))
        fig, axes = plt.subplots(1 if hits_only else 2, columns, squeeze=False,
                                 figsize=(5.5 * columns, 4.5 if hits_only else 7),
                                 sharex=True, sharey=True)
        for ax, metric in zip(axes.flat, metrics):
            for arm in arms:
                ax.plot(range(11), results[arm][metric], marker='o', ms=3,
                        label=names[arm], color='black' if arm == 'vanilla' else None)
            ax.set(title=metric, xticks=range(11), ylim=(0, 102))
            ax.grid(alpha=.2)
        for ax in axes[:, 0]:
            ax.set_ylabel('Percent of tasks')
        for ax in axes[-1]:
            ax.set_xlabel('Round (0 = initial description)')
        fig.suptitle(f'{label} · {len(matched)} matched complete dialogues')
        handles, labels = axes[0, 0].get_legend_handles_labels()
        fig.legend(handles, labels, loc='lower center', ncol=2)
        fig.tight_layout(rect=(0, .1, 1, .96))
        fig.savefig(figures / f'{label}.png', dpi=170)
        plt.close(fig)
        with (figures / f'{label}_metrics.csv').open('w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['arm', 'round', 'matched_dialogues', *metrics])
            for arm in arms:
                for r in range(11):
                    writer.writerow([arm, r, len(matched), *(results[arm][m][r] for m in metrics)])
        atomic_json(figures / f'{label}_matched_ids.json', matched)


def run(out, arms, limit, hits_only=False):
    from huggingface_hub import model_info

    if not torch.cuda.is_available():
        raise RuntimeError('Select a Colab GPU runtime.')
    tasks = read_json(ROOT / 'sample.json')['tasks']
    if not 1 <= limit <= len(tasks):
        raise ValueError('Limit must be between 1 and the frozen sample size.')
    out.mkdir(parents=True, exist_ok=True)
    config_path = out / 'config.json'
    old = read_json(config_path) if config_path.exists() else None
    revision = old['qwen_revision'] if old else model_info(QWEN).sha
    config = dict(
        qwen=QWEN, qwen_revision=revision, quantization='NF4', blip=BLIP,
        qwen_compute_dtype='bfloat16' if torch.cuda.is_bf16_supported() else 'float16',
        blip_revision=BLIP_REVISION, corpus_size=50_000, rounds=ROUNDS, arms=list(ARMS),
        sample_ids=[x['qid'] for x in tasks], rrf_k=60, mmr_weight=.7,
        package_versions={n: importlib.metadata.version(n) for n in
                          ('torch', 'transformers', 'bitsandbytes', 'fast-pytorch-kmeans')},
        source_sha256={n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in
                       ('run_dialogues.py', 'generate_dialog.py', 'sample.json')})
    if hits_only:
        config['hits_only'] = True
    if old is not None and old != config:
        raise ValueError('Settings or software changed. Use a fresh output folder for that run.')
    atomic_json(config_path, config)
    chosen_tasks = tasks[:limit]
    completed = {a: read_records(out, a) for a in arms}
    # Cycle through arms in batches so early partial results have matched tasks.
    batch = 10
    todo = [(a, t) for start in range(0, limit, batch) for a in arms
            for t in chosen_tasks[start:start + batch] if t['qid'] not in completed[a]]
    if not todo:
        print('All requested dialogues are already saved.', flush=True)
        return
    attempt_path = out / 'attempts' / f'{uuid.uuid4().hex}.json'
    began = time.perf_counter()
    attempt = dict(started_utc=utc(), gpu=torch.cuda.get_device_name(0),
                   requested_arms=arms, requested_tasks_per_arm=limit,
                   planned_new_dialogues=len(todo), finished_new_dialogues=0)

    def checkpoint(status, **extra):
        attempt.update(status=status, elapsed_seconds=round(time.perf_counter() - began, 3),
                       updated_utc=utc(), **extra)
        atomic_json(attempt_path, attempt)

    checkpoint('loading models')
    try:
        plugir = load_pipeline(revision)
        target_indices = {name: i for i, name in enumerate(plugir.search_space)}
        checkpoint('running', model_setup_seconds=round(time.perf_counter() - began, 3))
        for arm, task in todo:
            checkpoint('running', active_arm=arm, active_qid=task['qid'])
            result = run_dialogue(plugir, arm, task, target_indices[task['img']], hits_only=hits_only)
            atomic_json(out / arm / 'dialogues' / f'{task["qid"]}.json', result)
            attempt['finished_new_dialogues'] += 1
            checkpoint('running', active_arm=None, active_qid=None)
            print(f'Saved {arm} task {task["qid"]} in {result["seconds"]:.1f}s '
                  f'({attempt["finished_new_dialogues"]}/{len(todo)} new this session)', flush=True)
        checkpoint('finished', finished_utc=utc())
    except BaseException:
        checkpoint('interrupted or failed', error=traceback.format_exc())
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'report'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--arms', nargs='+', choices=ARMS, default=list(ARMS))
    parser.add_argument('--limit', type=int, default=200)
    parser.add_argument('--hits-only', action='store_true', help='Stop each dialogue at first top-ten hit.')
    args = parser.parse_args()
    if args.action == 'run':
        run(args.output, list(dict.fromkeys(args.arms)), args.limit, hits_only=args.hits_only)
    else:
        report(args.output)


if __name__ == '__main__':
    main()
