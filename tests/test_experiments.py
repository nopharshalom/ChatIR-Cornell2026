"""Pieces added for the meeting experiments (E1-E3)."""
import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))

from agentir.agent.context import select_representatives  # noqa: E402
from agentir.agent.reformulators import build_reformulator, strip_leaked_sentences  # noqa: E402
from agentir.agent.state import DialogState, Turn  # noqa: E402
from agentir.config import ReformulatorConfig, load_config  # noqa: E402


def test_fingerprints_unchanged_by_new_defaults():
    # values recorded before the E1/E2 options existed
    assert load_config(["configs/base.yaml", "configs/experiments/plugir.yaml"]).fingerprint() == "f4b10a69e035"
    assert load_config(["configs/base.yaml", "configs/experiments/divergence.yaml"]).fingerprint() == "f88c33fd6185"
    assert load_config(["configs/base.yaml", "configs/experiments/e1_rep_rank.yaml"]).fingerprint() != "f4b10a69e035"


def test_strip_leaks():
    s = "a camel in a black suitcase sits on the floor, with a pig also in a suitcase among the retrieval candidates"
    assert strip_leaked_sentences(s) == "a camel in a black suitcase sits on the floor"
    s = "A red van in a city. The bus mentioned in the retrieval candidates is a minibus. A lady waits."
    assert strip_leaked_sentences(s) == "A red van in a city. A lady waits."
    assert strip_leaked_sentences("a dog in a park") == "a dog in a park"


def test_anchor_caption():
    class L:
        def complete(self, *a, **k):
            return "a brown dog on grass"
    r = build_reformulator(ReformulatorConfig(kind="plugir", anchor_caption=True))
    st = DialogState(caption="a dog in a park")
    st.turns.append(Turn("what color?", "brown"))
    assert r.reformulate(st, L()) == "a dog in a park. a brown dog on grass"


def test_rank_representative_is_best_ranked_member():
    torch.manual_seed(0)
    img = torch.nn.functional.normalize(torch.randn(60, 16), dim=-1)
    picks = select_representatives(img, img, 5, seed=0, representative="rank")
    from agentir.agent.context import spherical_kmeans
    labels = spherical_kmeans(img, 5, seed=0)
    for p in picks:
        assert p == int((labels == labels[p]).nonzero().min())


def test_dpp_prefers_diverse_items():
    from common import dpp_greedy
    a = torch.tensor([[1.0, 0.0], [0.999, 0.04], [0.0, 1.0]])
    picks = dpp_greedy(a, torch.tensor([0.9, 0.89, 0.85]), 2, alpha=1.0)
    assert picks == [0, 2]
