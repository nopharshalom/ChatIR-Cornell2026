"""Time the same ten vanilla PlugIR dialogues with local Qwen3.5-9B on a Mac."""
import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
from types import SimpleNamespace
from zipfile import ZipFile

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
REPO = PROJECT / 'ChatIR-Cornell2026'
CACHE = REPO / 'cache/huggingface'
VLM_PYTHON = PROJECT / 'tmp/agentir-mlx-vlm-env/bin/python'
MODEL = 'mlx-community/Qwen3.5-9B-4bit'


def worker(snapshot):
    """Run MLX-VLM separately from the existing pinned BLIP environment."""
    import mlx.core as mx
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.utils import load_config
    from PIL import Image

    protocol = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        model, processor = load(str(snapshot))
        config = load_config(str(snapshot))
    print(json.dumps(dict(ready=True, mlx_vlm=importlib.metadata.version('mlx-vlm'),
                          mlx=importlib.metadata.version('mlx'))), file=protocol, flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        if request.get('shutdown'):
            return
        try:
            with contextlib.redirect_stdout(sys.stderr):
                if request.get('seed') is not None:
                    mx.random.seed(request['seed'])
                images = None
                if request.get('image'):
                    with Image.open(request['image']) as file:
                        picture = file.convert('RGB')
                    picture.thumbnail((768, 768))
                    images = [picture]
                prompt = apply_chat_template(processor, config, request['messages'],
                                             num_images=int(images is not None), enable_thinking=False)
                started = time.perf_counter()
                result = generate(model, processor, prompt=prompt, image=images,
                                  max_tokens=request['max_tokens'],
                                  temperature=request['temperature'], top_p=request['top_p'],
                                  top_k=0, repetition_penalty=1.0, verbose=False)
                text = result.text.split('</think>')[-1].strip()
                if not text:
                    raise RuntimeError('Qwen returned empty text.')
                response = dict(text=text, seconds=time.perf_counter() - started,
                                input_tokens=result.prompt_tokens, output_tokens=result.generation_tokens,
                                peak_mlx_gb=mx.get_peak_memory() / 1e9)
        except Exception as exc:
            response = dict(error=f'{type(exc).__name__}: {exc}')
        print(json.dumps(response), file=protocol, flush=True)


class MacQwen:
    def __init__(self, snapshot, output):
        self.log = (output / 'mlx_worker.log').open('a')
        self.requests = (output / 'llm_requests.jsonl').open('a')
        self.process = subprocess.Popen(
            [str(VLM_PYTHON), '-u', str(__file__), '--worker', str(snapshot)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, bufsize=1, env={**os.environ, 'HF_HOME': str(CACHE), 'HF_HUB_OFFLINE': '1'})
        self.calls = 0
        self.next_seed = None
        try:
            self.metadata = self.read()
        except BaseException:
            self.close()
            raise

    def read(self):
        if not select.select([self.process.stdout], [], [], 240)[0]:
            self.close()
            raise TimeoutError('MLX worker timed out; see mlx_worker.log')
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError('MLX worker exited; see mlx_worker.log')
        response = json.loads(line)
        if 'error' in response:
            raise RuntimeError(response['error'])
        return response

    def complete(self, messages, max_tokens, temperature=0, top_p=1, seed=None, image=None):
        seed = self.next_seed if self.next_seed is not None else seed if temperature > 0 else None
        self.next_seed = None
        request = dict(messages=messages, max_tokens=max_tokens, temperature=temperature,
                       top_p=top_p, seed=seed, image=str(image) if image else None)
        started = time.perf_counter()
        self.process.stdin.write(json.dumps(request) + '\n')
        self.process.stdin.flush()
        response = self.read()
        self.calls += 1
        self.requests.write(json.dumps(dict(call=self.calls, image=bool(image),
                                            elapsed_seconds=time.perf_counter() - started,
                                            **response)) + '\n')
        self.requests.flush()
        return response['text']

    def create(self, messages, n=1, max_tokens=512, **kwargs):
        return {'choices': [{'message': {'content': self.complete(messages, max_tokens, **kwargs)}}
                            for _ in range(n)]}

    def answer(self, image, question):
        messages = [
            {'role': 'system', 'content': 'Answer the question using only the target image. '
             'Be brief and say you are unsure when the image does not show the answer.'},
            {'role': 'user', 'content': question},
        ]
        return self.complete(messages, 48, image=image)

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.write('{"shutdown":true}\n')
                self.process.stdin.flush()
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        self.log.close()
        self.requests.close()


def load_blip(inputs, qwen):
    import torch
    import generate_dialog as plugir
    import run_dialogues as shared
    from fast_pytorch_kmeans import KMeans
    from transformers import AutoProcessor

    if not torch.backends.mps.is_available():
        raise RuntimeError('MPS is unavailable. Run this pilot with Metal access.')
    plugir.device = 'mps'
    plugir.args = SimpleNamespace(llm_backend='mlx', n_clusters=10, cluster_on_cpu=True,
                                  early_stop_questions=False, cache_filter_results=False)
    plugir.SEED = 1021
    settings = dict(revision=shared.BLIP_REVISION, local_files_only=True, cache_dir=CACHE/'hub')
    plugir.processor = AutoProcessor.from_pretrained(shared.BLIP, **settings)
    plugir.blip = plugir.BlipForRetrieval.from_pretrained(shared.BLIP, **settings).to('mps').eval()

    def features(text):
        tokens = plugir.processor.tokenizer(
            text, padding=True, truncation=True, max_length=512, return_tensors='pt').to('mps')
        return plugir.blip.get_text_features(
            input_ids=tokens.input_ids, attention_mask=tokens.attention_mask)

    plugir.get_text_features = features
    plugir.search_space = shared.read_json(inputs/'Protocol/Search_Space_val_50k.json')
    plugir.captions = shared.read_json(inputs/'Protocol/visdial_captions.json')
    indices, vectors = torch.load(inputs/'cache/corpus_blip.pth', map_location='cpu', weights_only=True)
    if (len(plugir.search_space) != 50_000 or not torch.equal(indices, torch.arange(50_000))
            or len(vectors) != 50_000 or [x['id'] for x in plugir.captions] != plugir.search_space):
        raise ValueError('Corpus, captions, and cached BLIP vectors are not aligned.')
    plugir.img_embs = vectors.to('mps')
    plugir.caption_features_cache = {}
    plugir.kmeans = KMeans(n_clusters=10, mode='cosine', verbose=0)
    plugir.client = qwen
    return plugir


def run_pilot(snapshot, output, limit):
    import numpy as np
    import torch
    import run_dialogues as shared

    started = time.perf_counter()
    output.mkdir(parents=True, exist_ok=True)
    inputs = PROJECT/'tmp/agentir-local-9b-inputs'
    archive = HERE.parent/'agentir_full_dialogue_bundle_v2.zip'
    with ZipFile(archive) as bundle:
        for entry in bundle.namelist():
            if entry == 'sample.json' or entry.startswith(('Protocol/', 'cache/', 'targets/')):
                bundle.extract(entry, inputs)
    shared.ROOT = inputs
    tasks = shared.read_json(inputs/'sample.json')['tasks'][:limit]
    config = dict(model=MODEL, revision=snapshot.name, backend='MLX 4-bit',
                  retrieval='BLIP float32 on MPS', selector='PlugIR cosine k-means and entropy',
                  round_count=10, corpus_size=50_000, tasks=[x['qid'] for x in tasks],
                  runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    config_path = output/'config.json'
    if config_path.exists() and shared.read_json(config_path) != config:
        raise ValueError('The run configuration differs. Use a new output folder.')
    shared.atomic_json(config_path, config)
    old = shared.read_records(output, 'vanilla')
    qwen = MacQwen(snapshot, output)
    try:
        torch.set_grad_enabled(False)
        plugir = load_blip(inputs, qwen)
        target_ids = {path: idx for idx, path in enumerate(plugir.search_space)}
        setup_seconds = time.perf_counter() - started
        print(f'Models and index loaded in {setup_seconds:.1f}s', flush=True)
        # Check both paths before starting the ten tasks. Save setup separately.
        probe_start = time.perf_counter()
        qwen.next_seed = 1021
        qwen.complete([{'role':'user','content':'Say hello in one word.'}], 8)
        qwen.answer(inputs/'targets'/tasks[0]['img'], 'Is the image in color?')
        probe_seconds = time.perf_counter() - probe_start
        shared.atomic_json(output/'setup.json', dict(setup_seconds=setup_seconds,
                                                    probe_seconds=probe_seconds, **qwen.metadata))
        print(f'Text and image smoke checks passed in {probe_seconds:.1f}s', flush=True)
        for task in tasks:
            if task['qid'] in old:
                continue
            qwen.next_seed = 1021 + task['qid']
            result = shared.run_dialogue(plugir, 'vanilla', task, target_ids[task['img']])
            shared.atomic_json(output/'vanilla/dialogues'/f'{task["qid"]}.json', result)
            old[task['qid']] = result
            print(f'Saved {len(old)}/{limit}: {result["seconds"]:.1f}s for task {task["qid"]}', flush=True)
        completed = [old[task['qid']] for task in tasks]
        elapsed = [r['seconds'] for r in completed]
        curves = shared.curves(np.array([r['ranks'] for r in completed]))
        summary = dict(
            dialogues=len(elapsed), setup_seconds=setup_seconds, probe_seconds=probe_seconds,
            pilot_wall_seconds=time.perf_counter()-started, dialogue_seconds=sum(elapsed),
            seconds_by_dialogue=elapsed, mean_dialogue_seconds=float(np.mean(elapsed)),
            median_dialogue_seconds=float(np.median(elapsed)),
            projected_hours_200_one_arm=float(np.mean(elapsed)*200/3600),
            qwen_calls=sum(t['llm_calls'] for r in completed for t in r['turns']),
            metrics={key:value.tolist() for key,value in curves.items()}, **qwen.metadata)
        shared.atomic_json(output/'summary.json', summary)
        print(f'Mean: {summary["mean_dialogue_seconds"]:.1f}s; '
              f'200-dialogue projection: {summary["projected_hours_200_one_arm"]:.2f}h', flush=True)
    finally:
        qwen.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--limit', type=int, choices=range(1, 11), default=10)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
    else:
        if not args.snapshot or not args.output:
            parser.error('--snapshot and --output are required')
        run_pilot(args.snapshot, args.output, args.limit)
