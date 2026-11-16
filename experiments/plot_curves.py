"""Standard curves for every method: cumulative Hits@10 by round (primary), Recall@10 (secondary).

    python experiments/plot_curves.py --out runs/curves \
        "PlugIR=runs/v3_plugir-xxxx" "Oracle caption=runs/v3_oracle_target-xxxx" ...

The first run is the reference (vanilla PlugIR); it is drawn first and every other method is
overlaid. Only dialogs present in every run are used, so the curves are paired. Shaded bands are
95% bootstrap intervals over dialogs. Also writes curves.json and a table of final values with
paired bootstrap differences against the reference.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

COLORS = ["#2b2b2b", "#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b", "#17becf"]


def load_ranks(run: Path) -> dict[int, list[int]]:
    out = {}
    for line in open(run / "sessions.jsonl"):
        if line.strip():
            r = json.loads(line)
            out[r["qid"]] = r["ranks"]
    return out


def curves(ranks: np.ndarray, k: int):
    best = np.minimum.accumulate(ranks, axis=1)
    return (best < k).astype(float), (ranks < k).astype(float)


def boot_band(x: np.ndarray, n=2000, seed=0):
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), (n, len(x)))
    m = x[idx].mean(1) * 100
    return np.percentile(m, 2.5, 0), np.percentile(m, 97.5, 0)


def paired_diff(a: np.ndarray, b: np.ndarray, n=5000, seed=0):
    rng = np.random.default_rng(seed)
    d = b - a
    idx = rng.integers(0, len(d), (n, len(d)))
    m = d[idx].mean(1) * 100
    return round(float(d.mean() * 100), 2), round(float(np.percentile(m, 2.5)), 2), round(float(np.percentile(m, 97.5)), 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", help='"Label=path/to/run_dir" (first = reference)')
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    labels, data = [], []
    for spec in a.runs:
        lab, path = spec.split("=", 1)
        labels.append(lab)
        data.append(load_ranks(Path(path)))
    common = sorted(set.intersection(*(set(d) for d in data)))
    if not common:
        raise SystemExit("no dialogs in common")
    R = min(len(d[common[0]]) for d in data)
    arrs = [np.array([d[q][:R] for q in common]) for d in data]

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    res = {"k": a.k, "num_dialogs": len(common), "rounds": list(range(R)), "methods": {}}
    figs = {}
    for which, title in ((0, f"Hits@{a.k} (target seen in top {a.k} by round r)"),
                         (1, f"Recall@{a.k} (target in top {a.k} at round r)")):
        fig, ax = plt.subplots(figsize=(7.2, 4.4))
        for i, (lab, x) in enumerate(zip(labels, arrs)):
            y = curves(x, a.k)[which]
            mean = y.mean(0) * 100
            lo, hi = boot_band(y)
            c = COLORS[i % len(COLORS)]
            ax.plot(range(R), mean, marker="o", ms=3.5, lw=2.2 if i == 0 else 1.8, color=c, label=lab,
                    ls="-" if i == 0 else "-", zorder=3 if i == 0 else 2)
            ax.fill_between(range(R), lo, hi, color=c, alpha=0.12, lw=0)
            key = "hits" if which == 0 else "recall"
            res["methods"].setdefault(lab, {})[key] = mean.round(2).tolist()
        ax.set_xlabel("round (0 = initial caption)")
        ax.set_ylabel("% of dialogs")
        ax.set_xticks(range(R))
        ax.set_title(f"{title}, n={len(common)}", fontsize=10)
        ax.grid(alpha=0.25)
        ax.legend(frameon=False, fontsize=8.5)
        fig.tight_layout()
        name = f"{'hits' if which == 0 else 'recall'}{a.k}_curves.png"
        fig.savefig(out / name, dpi=150)
        plt.close(fig)
        figs[which] = out / name

    ref_h, ref_r = curves(arrs[0], a.k)
    table = []
    for lab, x in zip(labels, arrs):
        h, r = curves(x, a.k)
        row = {"method": lab, f"Hits@{a.k}@final": round(h[:, -1].mean() * 100, 2),
               f"Recall@{a.k}@final": round(r[:, -1].mean() * 100, 2),
               f"Hits@{a.k}@5": round(h[:, min(5, R - 1)].mean() * 100, 2)}
        if lab != labels[0]:
            row["dHits_final_vs_ref (mean, 95% CI)"] = paired_diff(ref_h[:, -1], h[:, -1])
            row["dRecall_final_vs_ref"] = paired_diff(ref_r[:, -1], r[:, -1])
        table.append(row)
    res["table"] = table
    (out / "curves.json").write_text(json.dumps(res, indent=2))
    for row in table:
        print(row)
    print("figures:", *figs.values())


if __name__ == "__main__":
    main()
