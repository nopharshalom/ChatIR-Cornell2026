"""`agentir <command>` - see README for the workflow."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path


def _cfg(args):
    from .config import load_config

    return load_config(args.config, args.set)


def _add_cfg(p):
    p.add_argument("-c", "--config", action="append", default=[], help="YAML config (repeatable, later wins)")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="override, e.g. --set agent.ranker.kind=answer_divergence")


# ----------------------------------------------------------------------------- commands
def cmd_prepare_data(args):
    from .prepare_data import prepare

    prepare(args.data_dir, visdial_zip=args.visdial_zip, coco_zip=args.coco_zip, coco_by_url=args.coco_by_url)


def cmd_build_index(args):
    from .retrieval import CorpusIndex, build_encoder, build_index, load_captions, load_search_space

    cfg = _cfg(args)
    space = load_search_space(cfg.data.search_space)
    caps = load_captions(cfg.data.captions, space)
    enc = build_encoder(cfg.retriever)
    if args.from_plugir_cache:
        from .retrieval.index import with_caption_embeddings

        if cfg.retriever.backend != "blip" or cfg.retriever.finetuned_ckpt:
            raise SystemExit("--from-plugir-cache only matches the zero-shot BLIP retriever (eval.py --retriever blip)")
        print(f"reusing image embeddings from {args.from_plugir_cache}; encoding captions only")
        idx = with_caption_embeddings(CorpusIndex.from_plugir_cache(args.from_plugir_cache, space), enc, caps)
    else:
        print(f"encoding {len(space)} images with {cfg.retriever.model_name} on {enc.device}")
        idx = build_index(enc, cfg.data.corpus_dir, space, caps, cfg.retriever.batch_size, cfg.retriever.num_workers)
    idx.meta.update({"model": cfg.retriever.model_name, "finetuned_ckpt": cfg.retriever.finetuned_ckpt})
    idx.save(cfg.retriever.index_path)
    print(f"saved {cfg.retriever.index_path}")


def cmd_check_llm(args):
    """Sanity-check the endpoint: latency, thinking leakage, and image input."""
    from .llm import LLMClient, user_message_with_image

    cfg = _cfg(args)
    for role, lcfg in [("agent", cfg.agent.llm), ("user_sim", cfg.user_sim.llm)]:
        if lcfg is None:
            continue
        lcfg = lcfg.model_copy(update={"cache_path": None})
        llm = LLMClient(lcfg)
        print(f"[{role}] {lcfg.model} @ {lcfg.base_url}")
        t = time.time()
        out = llm.complete([{"role": "user", "content": "Ask one short question about a photo of a dog in a park."}],
                           max_tokens=48)
        print(f"  text  ({time.time() - t:.1f}s): {out!r}")
        if "<think>" in out or len(out) > 300:
            print("  WARNING: output looks like it contains reasoning; check disable_thinking / extra_body")
        if args.image:
            t = time.time()
            out = llm.complete([user_message_with_image("What is in this photo? Answer in five words.", args.image)],
                               max_tokens=32)
            print(f"  image ({time.time() - t:.1f}s): {out!r}")


def _build_handles(cfg):
    from .orchestrator import HttpAgentHandle, HttpUserHandle, LocalAgentHandle, LocalUserHandle
    from .runtime import build_agent, build_user_sim, load_retriever, make_llm

    retriever = load_retriever(cfg)
    if cfg.mode == "http":
        return HttpAgentHandle(cfg.agent_url), HttpUserHandle(cfg.user_url), retriever
    llm = make_llm(cfg.agent.llm)
    agent = build_agent(cfg, retriever, llm)
    user = build_user_sim(cfg, agent_llm=llm)
    return LocalAgentHandle(agent), LocalUserHandle(user, cfg.data.corpus_dir), retriever


def cmd_run(args):
    from .data import load_samples
    from .orchestrator import run_eval

    cfg = _cfg(args)
    agent, user, retriever = _build_handles(cfg)
    samples = load_samples(cfg.data)
    from .orchestrator import run_dir

    print(f"run -> {run_dir(cfg)}: {len(samples)} sessions x {cfg.eval.max_rounds} rounds, mode={cfg.mode}")
    run_eval(cfg, samples, agent, user, retriever)


def cmd_eval_dialogs(args):
    from .orchestrator import eval_dialog_file
    from .runtime import load_retriever

    cfg = _cfg(args)
    eval_dialog_file(cfg, args.dialogs, load_retriever(cfg), split=args.split, name=args.name)


def cmd_metrics(args):
    from .config import load_config
    from .orchestrator import finalize

    run_dir = Path(args.run_dir)
    cfg = load_config([str(run_dir / "config.yaml")], args.set)
    recs = [json.loads(line) for line in open(run_dir / "sessions.jsonl") if line.strip()]
    finalize(cfg, recs, run_dir)


def _serve(app, host, port):
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


def cmd_serve_agent(args):
    from .services.agent_app import create_app

    _serve(create_app(_cfg(args)), args.host, args.port)


def cmd_serve_user(args):
    from .services.user_app import create_app

    _serve(create_app(_cfg(args)), args.host, args.port)


def cmd_smoke(args):
    """End-to-end run on the synthetic world: proves the install works, needs no models."""
    from .agent import Agent
    from .config import RunConfig
    from .data import Sample
    from .orchestrator import LocalAgentHandle, LocalUserHandle, run_dir, run_eval
    from .retrieval import Retriever, SearchTool
    from .smoke import MockLLM, build_fake_world, scripted_answer
    from .user_sim import ScriptedUserSimulator

    enc, index, queries, lookup = build_fake_world(n_queries=args.n)
    retriever = Retriever(enc, index)
    samples = [Sample(i, q["img"], q["dialog"][0], q["dialog"]) for i, q in enumerate(queries)]
    results = {}
    for ranker in ["random", "plugir_kl", "answer_divergence"]:
        cfg = RunConfig.model_validate({
            "name": f"smoke-{ranker}", "output_dir": args.output_dir,
            "retriever": {"backend": "fake"},
            "agent": {"generator": {"thres_low": 200, "n_clusters": 5},
                      "ranker": {"kind": ranker, "top_k": 10, "thres_low": 200},
                      "reformulator": {"kind": "plugir"}},
            "eval": {"max_rounds": 4, "ks": [1, 10]}})
        for f in ("sessions.jsonl", "errors.jsonl"):
            (run_dir(cfg) / f).unlink(missing_ok=True)
        agent = Agent(cfg.agent, MockLLM(), SearchTool(retriever), seed=cfg.seed)
        user = ScriptedUserSimulator(scripted_answer(lookup))
        print(f"\n=== smoke: ranker={ranker} ===")
        results[ranker] = run_eval(cfg, samples, LocalAgentHandle(agent), LocalUserHandle(user, "."), retriever)
    print("\nsmoke OK. BRI by ranker:", {k: round(v["bri"], 3) for k, v in results.items()})


# ----------------------------------------------------------------------------- main
def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(prog="agentir")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare-data", help="assemble the 50k corpus images under data/unlabeled2017")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--visdial-zip", help="VisualDialog_val2018.zip, only used if targets are missing from COCO")
    p.add_argument("--coco-zip", help="path to COCO unlabeled2017.zip")
    p.add_argument("--coco-by-url", action="store_true", help="fetch only the 50k needed images one by one instead")
    p.set_defaults(fn=cmd_prepare_data)

    p = sub.add_parser("build-index", help="embed the 50k corpus images + captions once")
    _add_cfg(p)
    p.add_argument("--from-plugir-cache", metavar="PTH",
                   help="reuse eval.py's cache (e.g. cache/corpus_blip.pth) instead of re-embedding images")
    p.set_defaults(fn=cmd_build_index)

    p = sub.add_parser("check-llm", help="probe the configured LLM endpoint(s)")
    _add_cfg(p)
    p.add_argument("--image", help="optional image path to test VLM input")
    p.set_defaults(fn=cmd_check_llm)

    p = sub.add_parser("run", help="run interactive retrieval sessions and score them")
    _add_cfg(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("eval-dialogs", help="score a fixed dialog file (no LLM)")
    _add_cfg(p)
    p.add_argument("dialogs")
    p.add_argument("--split", action="store_true", help="dialog[r] is the full round-r query (PlugIR *_reconed.json)")
    p.add_argument("--name")
    p.set_defaults(fn=cmd_eval_dialogs)

    p = sub.add_parser("metrics", help="recompute metrics for a run directory")
    p.add_argument("run_dir")
    p.add_argument("--set", action="append", default=[])
    p.set_defaults(fn=cmd_metrics)

    for name, fn, port in [("serve-agent", cmd_serve_agent, 7001), ("serve-user", cmd_serve_user, 7002)]:
        p = sub.add_parser(name)
        _add_cfg(p)
        p.add_argument("--host", default="0.0.0.0")
        p.add_argument("--port", type=int, default=port)
        p.set_defaults(fn=fn)

    p = sub.add_parser("smoke", help="offline end-to-end check with a synthetic world")
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--output-dir", default="runs")
    p.set_defaults(fn=cmd_smoke)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
