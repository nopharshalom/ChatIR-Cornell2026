"""Baseline MLX worker with full-resolution questioner grids."""
import contextlib
import importlib.metadata
import json
from pathlib import Path
import sys
import time


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
                    if Path(request['image']).name == 'questioner-grid.png':
                        if picture.size != (1600, 640):
                            raise ValueError('Invalid questioner grid size')
                    else:
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


if __name__ == '__main__':
    worker(Path(sys.argv[2]))
