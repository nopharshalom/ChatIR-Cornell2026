"""Alternative pixel use: the frozen generic-fallback question selector on no-repeat screens."""
import argparse
from contextlib import ExitStack, closing
import fcntl
import json
from pathlib import Path
import shutil
import time
from unittest.mock import patch

import torch
import run_history_pixels_pilot as pilot

base,idx,SH,OUT=pilot.base,pilot.indexed,pilot.SH,pilot.OUT
OLD=idx.ROOT
OriginalSelector=idx.Selector

class CaptionSelector(OriginalSelector):
    def review(self, ids, proposals, default):
        listing='\n'.join(f'{j}. {self.captions[i]["caption"][0]}' for j,i in enumerate(ids))
        complete=self.worker.complete
        def caption_complete(messages,max_tokens,image):
            messages=[dict(m) for m in messages]
            messages[0]['content']+=' Each numbered caption is the only evidence about its photo. Use unknown for an unstated attribute.'
            messages[-1]['content']+='\nNumbered candidate captions:\n'+listing
            return complete(messages,max_tokens)
        with patch.object(self.worker,'complete',caption_complete), patch.object(base,'build_grid',lambda *args: None):
            return super().review(ids,proposals,default)

def source_for(i):
    for split in ('tune','confirm'):
        path=OLD/'fallback'/split/'indexed/dialogues'/f'{i}.json'
        if path.exists(): return path
    return None

@torch.inference_mode()
def main(split,arms):
    ids=SH.read_json(OUT/'split.json')[split+'_ids']
    assert len(set(ids))==20
    sources={p.name:pilot.sha(p.read_bytes()) for p in (Path(__file__),Path(pilot.__file__),Path(idx.__file__),
                                        Path(idx.__file__).with_name('check_indexed_visual_answers.py'),base.GRID_WORKER)}
    if split=='confirmation':
        freeze=SH.read_json(OUT/'freeze.json')
        assert set(arms)<=set(freeze['arms']) and freeze['question_sources']==sources
    refs={t['qid']:(t,root,records[t['qid']]) for _,tasks,records,root in base.cohorts() for t in tasks if t['qid'] in ids}
    with ExitStack() as stack:
        lock=stack.enter_context((OUT/'runner.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        pipe=None
        for arm in arms:
            folder=OUT/split/arm
            cache=OUT/('q-cache-'+arm)
            plan=dict(ids=ids,arm=arm,sources=sources,model=base.SNAPSHOT.name,selector='frozen other-object fallback',
                      captions_sha256=pilot.sha((base.INPUTS/'Protocol/visdial_captions.json').read_bytes()),
                      control_hash={str(i):pilot.sha(json.dumps(refs[i][2],sort_keys=True).encode()) for i in ids},
                      split_sha=pilot.sha((OUT/'split.json').read_bytes()),mmr=.85,proposals=5,max_questions=10,corpus=50000)
            if (folder/'plan.json').exists(): assert SH.read_json(folder/'plan.json')==plan
            SH.atomic_json(folder/'plan.json',plan)
            if arm=='q-pixels' and not (cache/'answers').exists():
                shutil.copytree(OLD/'answers',cache/'answers')
            rows=[]
            for i in ids:
                path=folder/'dialogues'/f'{i}.json'
                if not path.exists():
                    if pipe is None:
                        with patch.object(base.mac,'__file__',str(base.GRID_WORKER)):
                            worker=stack.enter_context(closing(base.mac.MacQwen(base.SNAPSHOT,OUT/split)))
                        pipe=base.mac.load_blip(base.INPUTS,worker)
                        positions={name:i for i,name in enumerate(pipe.search_space)}
                    t,root,ref=refs[i]
                    target=positions[t['img']]
                    started=time.perf_counter()
                    saved=source_for(i) if arm=='q-pixels' else None
                    if saved:
                        # The previous question policy did not consume displays. Reuse only the
                        # actual matching prefix, with fresh evaluation of every no-repeat screen.
                        prior=SH.read_json(saved)
                        assert (prior['target'],prior['queries'][0])==(ref['target'],ref['queries'][0])
                        row=pilot.dialogue(pipe,t,root,prior,'ordinary',target,folder,live=False)
                        assert row is not None
                        for j,turn in enumerate(row['turns']):
                            review=prior['reviews'][j]
                            assert turn['question']==review['selected']
                            turn.update(review=review,default=review['default'])
                        row['archived_source']=str(saved.relative_to(base.ROOT))
                        row['archived_sha256']=pilot.sha(saved.read_bytes())
                    else:
                        klass=CaptionSelector if arm=='q-caption' else OriginalSelector
                        with ExitStack() as scope:
                            scope.enter_context(patch.object(idx,'ROOT',cache))
                            scope.enter_context(patch.object(idx,'Selector',klass))
                            if arm=='q-caption':
                                scope.enter_context(patch.object(klass,'captions',pipe.captions,create=True))
                            args=(pipe,t,root,ref,'fallback',target,folder)
                            row=pilot.dialogue(*args,live=False)
                            if row is None: row=pilot.dialogue(*args,live=True)
                    row.update(arm=arm,seconds=time.perf_counter()-started)
                    SH.atomic_json(path,row)
                rows.append(SH.read_json(path))
                print(arm,len(rows),i,'first',rows[-1]['first'],flush=True)
            result=pilot.metrics(rows)
            SH.atomic_json(folder/'summary.json',result)
            print(arm,result,flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('split',choices=['development','confirmation'])
    p.add_argument('arms',nargs='+',choices=['q-pixels','q-caption'])
    args=p.parse_args()
    main(args.split,args.arms)
