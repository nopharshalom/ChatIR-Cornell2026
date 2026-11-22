"""Matched caption-only proxy answers for both MMR .85 visual-probe arms."""
from contextlib import contextmanager
import fcntl
import hashlib
from pathlib import Path
from unittest.mock import patch

import run_visual_mmr85 as visual

OUT = visual.ROOT / 'output/agentir-caption-probe-mmr85-200'
FIRST = 'caption_mmr85'
HISTORY = 'history_caption_mmr85'


@contextmanager
def caption_answers(pipe):
    captions = {visual.REPO / 'data' / name: row['caption'][0]
                for name, row in zip(pipe.search_space, pipe.captions)}
    def answer(proxy, question):
        messages = [
            {'role':'system', 'content':'Answer the question using only the supplied image caption. '
             'Be brief and say you are unsure when the caption does not show the answer.'},
            {'role':'user', 'content':f'Caption: {captions[proxy]}\nQuestion: {question}'},
        ]
        return pipe.client.complete(messages, 48)
    with patch.object(pipe.client, 'answer', answer):
        yield


def with_captions(choice):
    def wrapped(pipe, *args):
        with caption_answers(pipe):
            return choice(pipe, *args)
    return wrapped


def run():
    shared = visual.shared
    fingerprint = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    stamp = OUT / 'caption_protocol.json'
    if stamp.exists() and shared.read_json(stamp)['sha256'] != fingerprint:
        raise ValueError('Caption protocol changed since the first checkpoint')
    shared.atomic_json(stamp, dict(sha256=fingerprint, proxy_evidence='first indexed image caption'))
    pairs = ((FIRST,visual.FIRST), (HISTORY,visual.HISTORY))
    visual_output = visual.OUT
    with patch.multiple(visual, OUT=OUT, FIRST=FIRST, HISTORY=HISTORY,
                        visual_choice=with_captions(visual.visual_choice),
                        history_choice=with_captions(visual.history_choice)):
        visual.main()
    result = {}
    for text_arm, image_arm in pairs:
        text = shared.read_records(OUT, text_arm)
        images = shared.read_records(visual_output, image_arm)
        if len(images) != 200:
            print(f'{image_arm} has {len(images)}/200 results. Rerun to finalize the comparison.')
            continue
        result[text_arm] = visual.summarize(text, images)
        print(text_arm, 'round 10 vs pixels:', result[text_arm][10])
    shared.atomic_json(OUT/'caption_vs_pixels.json',result)


if __name__ == '__main__':
    OUT.mkdir(exist_ok=True, parents=True)
    with (OUT/'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        visual.torch.set_num_threads(4)
        run()
