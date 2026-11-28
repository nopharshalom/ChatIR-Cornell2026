"""Checks for the matched visual-question experiment, using CPU fixtures."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

import torch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import pixel_fork_policy as p
import run_pixel_forks as run


class StubAudit:
    def __init__(self):
        images=torch.tensor([[1.,0.]]*5+[[0.,1.]]*5)
        def features(q):
            if ' is red.' in q:
                return torch.tensor([[1.,0.]])
            if ' is blue.' in q:
                return torch.tensor([[0.,1.]])
            return torch.tensor([[1.,1.]])
        self.pipe=SimpleNamespace(img_embs=images,get_text_features=features)
        self.images=[]

    def ask(self,instruction,payload,tokens,image=None):
        if instruction==p.ENTITIES:
            return '["bus"]'
        if instruction==p.SCOUT:
            return '[{"id":1,"family":"color","values":["red","blue"]}]'
        if instruction==p.HISTORY_ANSWER:
            return 'UNKNOWN'
        if instruction==p.HISTORY:
            return '{"status":"OPEN"}'
        if instruction==p.PICK:
            return '1' if 'main color' in str(payload['candidate_descriptions']) else '0'
        if instruction.startswith(p.LABELS):
            if image is not None:
                self.images.append(image)
            return json.dumps({'1':('unknown' if image is None else 'red' if image<5 else 'blue')})
        raise AssertionError(instruction)


class ForkTests(unittest.TestCase):
    def test_shared_opportunity_and_fair_tie(self):
        audit=StubAudit()
        ordinary='What is around the bus?'
        def choose(history):
            return p.choices(audit,history,history[0],list(range(10)),['A parked bus.']*10,
                             ordinary,[ordinary]*5,'fixed')
        choice,trace=choose(['A bus is parked.'])
        self.assertEqual(audit.images,list(range(10)))
        self.assertEqual(choice['picks'],dict(captions_annotations=0,captions_retrieval=0,
                                            pixels_annotations=1,pixels_retrieval=1,blind_alternative=1))
        self.assertEqual(trace['pixels_transfer'],[1.])
        choice,trace=choose(['A red bus is parked.'])
        self.assertIsNone(choice)
        self.assertEqual(len(audit.images),10)

    def test_no_divergence_reuses_ordinary_full_dialogue(self):
        ref=dict(ranks=[50,5])
        with TemporaryDirectory() as temp, patch.object(run,'pick_fork',return_value=dict(pivot=None,trace=[])):
            row=run.run_case(None,None,({'qid':1},None),ref,0,Path(temp))
        self.assertEqual(len(row['branches']),6)
        r=run.report({1:row})['retrieval']
        self.assertEqual(r['full_dialogues']['mmr']['variant'][-1],1)
        self.assertTrue(r['low_exposure'])
        self.assertFalse(r['passes'])

    def test_unique_branch_and_answer_ablation(self):
        ref=dict(qid=1,queries=['A bus'],ranks=[30,20,6],
                 turns=[dict(question='Where is it?',answer='outside'),dict(question='What else?',answer='a car')])
        changed=dict(qid=1,ranks=[30,4],turns=[dict(question='What color?',answer='red')])
        chosen=dict(pivot=0,questions=['Where is it?','What color?'],picks=dict.fromkeys(p.BASIC,0),
                    trace=[dict(caption_labels=[[None]*10],pixel_labels=[['red']*5+['blue']*5],pixel_supported=[True],
                                slots=[dict(values=['red','blue'])])])
        chosen['picks'].update(pixels_annotations=1,pixels_retrieval=1)
        def diag(pipe,audit,ref,j,row,target):
            return dict(answer=row['turns'][j]['answer'],actual_rank=row['ranks'][1],novel='NEW',
                        factual=dict(rank=row['ranks'][1]),withheld=dict(rank=30))
        with TemporaryDirectory() as temp, patch.object(run,'pick_fork',return_value=chosen), patch.object(
                run,'continue_from',return_value=changed) as rerun, patch.object(run,'answer_diagnostic',side_effect=diag) as diagnostics:
            for _ in range(2):
                row=run.run_case(None,None,({'qid':1},None),ref,0,Path(temp))
            self.assertEqual(rerun.call_count,1)
            self.assertEqual(diagnostics.call_count,2)
        r=run.report({1:row})['annotations']
        self.assertEqual(r['verified_immediate_rescues'],[1])
        self.assertEqual(r['full_dialogues']['mmr']['gained'],[])  # control also hits later
        row['branches']['pixels_annotations']['withheld']['rank']=7
        self.assertEqual(run.report({1:row})['annotations']['verified_immediate_rescues'],[])

    def test_diagnostic_changes_only_answer_and_uses_same_seed(self):
        seen=[]
        worker=SimpleNamespace(next_seed=None)
        def rewrite(h):
            seen.append((list(h),worker.next_seed))
            return 'blue' if h[-1].endswith('blue') else 'blank'
        pipe=SimpleNamespace(client=worker,reconstruct_dialog=rewrite,
                             img_embs=torch.tensor([[1.,0.],[0.,1.]]),
                             get_text_features=lambda s:torch.tensor([[1.,0.]]) if s=='blue' else torch.tensor([[0.,1.]]))
        ref=dict(qid=4,queries=['bus'],turns=[])
        row=dict(turns=[dict(question='What color?',answer='blue',query_before='bus')],ranks=[2,1])
        audit=SimpleNamespace(ask=lambda *a:'NEW')
        d=run.answer_diagnostic(pipe,audit,ref,0,row,0)
        self.assertEqual([x[0] for x in seen],[['bus','What color? blue'],['bus','What color? I cannot tell.']])
        self.assertEqual(seen[0][1],seen[1][1])
        self.assertEqual((d['factual']['rank'],d['withheld']['rank']),(1,2))

if __name__=='__main__':
    unittest.main()
