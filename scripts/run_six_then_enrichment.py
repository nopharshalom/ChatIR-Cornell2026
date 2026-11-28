"""Resume four enrichment pilots, run two proposal pilots, then finish both pixel arms."""
import argparse
import fcntl
from pathlib import Path
import subprocess
import sys

from run_candidate_caption_enrichment import OUT as ENRICH, ARMS, shared
from run_fifth_question_pilots import OUT as FIFTH, PROMPTS

HERE=Path(__file__).resolve().parent


def stage(script,*flags):
    cmd=[sys.executable,'-u',str(HERE/script),*flags]
    result=subprocess.run(cmd)
    if result.returncode==75:
        print('GPU timeout; resuming this stage once.',flush=True)
        result=subprocess.run(cmd)
    result.check_returncode()


def save_pilots():
    refs=shared.read_json(ENRICH/'plan.json')['splits']
    ids=[i for s in refs for i in s]
    base=shared.read_json(ENRICH/'summary.json')
    generation=shared.read_json(FIFTH/'summary.json')
    saved={}
    for arm,folder in [(a,ENRICH) for a in ARMS]+[(a,FIFTH) for a in PROMPTS]:
        rows=shared.read_records(folder,arm)
        if not set(ids)<=rows.keys():
            raise ValueError(f'{arm}: missing matched pilot IDs')
        halves=base[arm]['pilot_halves'] if arm in ARMS else generation[arm]['halves']
        pairs={int(i):x for half in halves for i,x in half['paired']['first_hit_control_variant'].items()}
        if set(pairs)!=set(ids):
            raise ValueError('Incomplete pilot comparison')
        metrics={}
        for side,name in enumerate(['control','variant']):
            metrics[name]=dict(hits=[sum(p[side]<=r for p in pairs.values()) for r in range(11)],
                               questions=sum(min(p[side],10) for p in pairs.values()))
        saved[arm]=dict(n=len(pairs),halves=halves,**metrics)
        print(arm,'R1/R3/R5/R10:',[metrics['variant']['hits'][r] for r in (1,3,5,10)],
              'questions:',metrics['variant']['questions'],flush=True)
    shared.atomic_json(FIFTH/'six-pilots.json',dict(ids=ids,arms=saved))


def main(check,include_selection=False):
    if check:
        scripts=['run_candidate_caption_enrichment.py','run_fifth_question_pilots.py']
        if include_selection:
            scripts.append('run_caption_split_selector.py')
        for script in scripts:
            stage(script,'--check-only')
        return
    FIFTH.mkdir(exist_ok=True,parents=True)
    with (FIFTH/'queue.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        print('Stage 1: four caption-enrichment pilots',flush=True)
        stage('run_candidate_caption_enrichment.py','--pilot-only')
        print('Stage 2: two fifth-question pilots',flush=True)
        stage('run_fifth_question_pilots.py')
        save_pilots()
        if include_selection:
            print('Comparing original vs pixel-enriched captions for question selection.',flush=True)
            stage('run_caption_split_selector.py')
            from run_caption_split_selector import OUT as SELECT, ARMS as selectors
            saved=shared.read_json(FIFTH/'six-pilots.json')
            summary=shared.read_json(SELECT/'summary.json')
            for arm in selectors:
                comparison=summary[arm]['all']['paired']
                if {int(i) for i in comparison['first_hit_control_variant']}!=set(saved['ids']):
                    raise ValueError(f'Incomplete matched IDs for {arm}')
                metrics={}
                for side in ('control','variant'):
                    col=int(side=='variant')
                    pairs=comparison['first_hit_control_variant'].values()
                    metrics[side]=dict(hits=comparison[side],questions=sum(min(p[col],10) for p in pairs))
                saved['arms'][arm]=dict(n=len(saved['ids']),halves=summary[arm]['halves'],**metrics)
                print(arm,'R1/R3/R5/R10:',[comparison['variant'][r] for r in (1,3,5,10)],flush=True)
            shared.atomic_json(FIFTH/'eight-pilots.json',saved)
        print('Continuing both pixel-enrichment arms to 400.',flush=True)
        stage('run_candidate_caption_enrichment.py')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--include-caption-selection',action='store_true')
    args=parser.parse_args()
    main(args.check_only,args.include_caption_selection)
