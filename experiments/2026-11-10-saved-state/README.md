# AgentIR saved-state experiments · November 10, 2026

These three experiments replay saved PlugIR descriptions after four and nine answers. They use the same frozen BLIP retriever and 50,000-image search corpus. The smoke runs cover 10 targets and the development runs cover 100. Each folder has the Colab notebook, the exact ZIP it expects as input, and the archived results.

| Experiment | What it measures | Files |
| --- | --- | --- |
| S1 · Multi-query retrieval | Retrieval ranks for the current summary, original description and Qwen rewrites | [Notebook](01-multi-query/AgentIR_Multi_Query_Pilot.ipynb) · [Data and code bundle](01-multi-query/multi_query_pilot_bundle.zip) · [Results](01-multi-query/AgentIR_multi_query_results.zip) |
| S2 · Candidate memory | Target coverage in current and historical candidate pools, before and after selecting ten evidence images | [Notebook](02-candidate-memory/AgentIR_Candidate_Memory_Pilot.ipynb) · [Tools](02-candidate-memory/candidate_memory_tools.zip) · [Results](02-candidate-memory/AgentIR_candidate_memory_results.zip) |
| S3 · Evidence selection | Target coverage for four rules that pick ten evidence images from fixed pools | [Notebook](03-evidence-selection/AgentIR_Evidence_Selection_Pilot.ipynb) · [Tools](03-evidence-selection/evidence_selection_tools.zip) · [Results](03-evidence-selection/AgentIR_evidence_selection_results.zip) |

## Run in Colab

1. Open the notebook in Colab with a GPU runtime and mount Drive. Put the input ZIPs in the directories below, or update the notebook paths to your Shared Drive locations.

   | ZIP | Default folder under `MyDrive` |
   | --- | --- |
   | `multi_query_pilot_bundle.zip` | `AgentIR_multi_query/` |
   | `candidate_memory_tools.zip` | `AgentIR_candidate_memory/` |
   | `evidence_selection_tools.zip` | `AgentIR_evidence_selection/` |

2. Run the setup and smoke cells, then the pilot or development cells. Use a new `RUN_NAME` for a fresh run. The notebooks check the input ZIPs by SHA-256, so keep the archives intact.
3. S2 can run with the shared data bundle without rerunning S1. S3 also needs S2's saved `development/` results. Point `SOURCE_ROOT` at the saved S2 run, or upload the S2 results ZIP when S3 prompts for it.
4. Keep `RUN_HOLDOUT = False` during development. The results archives contain smoke and development outputs.

The shared data bundle contains the corpus manifest, saved queries, cached BLIP image embeddings and S1 code. S1 uses Qwen3-4B-Instruct-2507 to rewrite query text. S2 and S3 reuse the saved queries and embeddings for retrieval and selection.

## Read the results

Start with `pilot/summary.csv` for S1 and `development/summary.csv` for S2 and S3 inside their results ZIPs. The archives also include per-target records and paired comparisons. The [experiment site](https://agentir-research-results.grassynacre1.chatgpt.site) has plots and a walkthrough.

S1 reports retrieval Recall@10 at the saved stages. S2 and S3 report whether the target was in a candidate pool or among the ten selected evidence images. For cumulative Hits@10 through round 10, use complete per-round retrieval trajectories from a full dialogue run.
