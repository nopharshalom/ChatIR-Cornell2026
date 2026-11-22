"""Run the original matched sample with a constant 500-image clustering pool."""
import torch

from run_overnight_hits import INPUTS, OLD_BASELINE, ROOT, SNAPSHOT, mac, shared


def fixed_pool(arm, queries, rank_history, current_order, budget):
    return current_order, current_order[:500]


def main():
    if not torch.backends.mps.is_available():
        raise SystemExit("The matched BLIP/MLX runner requires the Mac GPU (MPS is unavailable).")
    out = ROOT / 'output/agentir-fixed-top500-original200'
    out.mkdir(exist_ok=True)
    tasks = shared.read_json(OLD_BASELINE / 'vanilla/config.json')['sample_ids']
    vanilla = shared.read_records(OLD_BASELINE, 'vanilla')
    assert len(tasks) == 200 and set(tasks) == set(vanilla)
    arm = 'fixed_top500'
    finished = shared.read_records(out, arm)
    shared.ROOT = INPUTS
    shared.rankings = fixed_pool
    worker = mac.MacQwen(SNAPSHOT, out)
    try:
        with torch.inference_mode():
            pipe = mac.load_blip(INPUTS, worker)
            positions = {name: i for i, name in enumerate(pipe.search_space)}
            for qid in tasks:
                if qid in finished:
                    continue
                ref = vanilla[qid]
                task = dict(qid=qid, img=ref['target'], caption=ref['queries'][0])
                worker.next_seed = 1021 + qid
                row = shared.run_dialogue(pipe, arm, task, positions[task['img']], hits_only=True)
                assert row['ranks'][:2] == ref['ranks'][:2]
                assert all(len(turn['pool_ids']) == 500 for turn in row['turns'])
                shared.atomic_json(out / arm / 'dialogues' / f'{qid}.json', row)
    finally:
        worker.close()


if __name__ == '__main__':
    main()
