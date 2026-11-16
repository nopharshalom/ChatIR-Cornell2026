"""Our metrics must reproduce PlugIR's eval.py numbers exactly.

`_plugir_reference` is PlugIR's code (eval.py: get_first_hitting_time,
cumulative_hits_per_round, BRI loop) lifted with only the globals made arguments.
"""
import numpy as np
import torch

from agentir.metrics import compute_metrics


def _plugir_reference(ranks: np.ndarray, K: int):
    num_rounds = ranks.shape[1]
    target_recall = torch.tensor(ranks.T.reshape(-1), dtype=torch.float)  # round-major, as eval.py cats them
    target_recalls = target_recall.view(num_rounds, -1).T
    hits = target_recalls < K
    final_hits = torch.inf * torch.ones(target_recalls.shape[0])
    hitting_times, temp_hitting_times = [], []
    for ro_i in range(num_rounds):
        temp_hits = torch.inf * torch.ones(target_recalls.shape[0])
        rh = hits[:, ro_i]
        final_hits[rh] = torch.min(final_hits[rh], torch.ones(final_hits[rh].shape) * ro_i)
        temp_hits[rh] = torch.min(temp_hits[rh], torch.ones(temp_hits[rh].shape) * ro_i)
        hitting_times.append(final_hits.clone())
        temp_hitting_times.append(temp_hits)
    ht, tht = torch.stack(hitting_times), torch.stack(temp_hitting_times)
    hits_k = ((ht < torch.inf).sum(dim=-1) * 100 / ht[0].shape[0]).tolist()
    recall_k = ((tht < torch.inf).sum(dim=-1) * 100 / tht[0].shape[0]).tolist()

    min_ranks = []
    for dl in range(num_rounds):
        cur = torch.tensor(ranks[:, dl], dtype=torch.float)
        min_ranks.append(cur if dl == 0 else torch.minimum(min_ranks[dl - 1], cur))
    bri = 0
    for dl in range(num_rounds - 1):
        bri += ((torch.log(min_ranks[dl] + 1.0) + torch.log(min_ranks[dl + 1] + 1.0)) / 2).mean()
    bri /= num_rounds - 1
    return hits_k, recall_k, float(bri)


def test_matches_plugir_eval():
    rng = np.random.default_rng(0)
    ranks = rng.integers(0, 2000, size=(257, 11))
    ranks[rng.random(ranks.shape) < 0.3] //= 300  # make some hits happen
    m = compute_metrics(ranks, [1, 10, 50])
    for K in (1, 10, 50):
        hits_ref, recall_ref, bri_ref = _plugir_reference(ranks, K)
        np.testing.assert_allclose(m["hits"][K], hits_ref, atol=1e-3)
        np.testing.assert_allclose(m["recall"][K], recall_ref, atol=1e-3)
        np.testing.assert_allclose(m["bri"], bri_ref, rtol=1e-5)


def test_hits_monotone_recall_not():
    ranks = np.array([[0, 50, 50], [50, 5, 50]])
    m = compute_metrics(ranks, [10])
    assert m["recall"][10] == [50.0, 50.0, 0.0]
    assert m["hits"][10] == [50.0, 100.0, 100.0]
