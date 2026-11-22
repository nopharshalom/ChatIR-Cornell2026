"""CPU fixtures for the ablation boundaries, conservative fallbacks and resume."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import run_novelty_grounding as runner


class Worker:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.calls = 0
        self.closed = False
    def complete(self, *args, **kwargs):
        self.calls += 1
        return next(self.responses)
    def answer(self, *args):
        self.calls += 1
        return 'white'
    def close(self):
        self.closed = True


class Pipe:
    def __init__(self, worker):
        self.client = worker
        self.img_embs = torch.eye(12)
        self.captions = [dict(caption=[f'candidate {i}']) for i in range(12)]
        self.search_space = [f'image{i}' for i in range(12)]
        self.selected_options = None
        self.rewrite_calls = 0
    def get_text_features(self, q):
        return torch.arange(12).float()[None] if q == 'initial' else torch.eye(12)[0:1]
    def cluster_representatives(self, pool):
        return [1, 2]
    def select_question(self, **kwargs):
        self.selected_options = kwargs['questions']
        return self.selected_options[0]
    def reconstruct_dialog(self, dialogue):
        self.rewrite_calls += 1
        return 'baseline rewrite'
    def paraphrase(self, query):
        raise AssertionError('Fixture should not require paraphrasing')


class AblationTests(unittest.TestCase):
    def test_novelty_uses_history_and_rejects_repeated_question_despite_judge(self):
        w = Worker([json.dumps({'decisions': [dict(id=0, novel=True), dict(id=1, novel=True)]})])
        accepted, debug = runner.novel_options(w, 'initial', [dict(question='Colour?', answer='white')],
                                                ['colour?', 'Where?'])
        self.assertEqual(accepted, ['Where?'])
        self.assertTrue(debug['valid'])

    def test_duplicate_decisions_do_not_silently_accept_question(self):
        w = Worker([json.dumps({'decisions': [dict(id=0, novel=True), dict(id=0, novel=True)]})])
        accepted, debug = runner.novel_options(w, 'initial', [], ['Colour?', 'Where?'])
        self.assertFalse(accepted)
        self.assertFalse(debug['valid'])

    def test_invalid_judge_and_regenerations_fall_back_to_original_kl(self):
        pipe = Pipe(Worker(['invalid', 'invalid', 'invalid']))
        q, debug = runner.novelty_selection(pipe, 'initial', [], ['Colour?', 'Where?'], [],
                                           'initial', torch.zeros(12), 1)
        self.assertEqual(q, 'Colour?')
        self.assertTrue(debug['fallback'])
        self.assertEqual(pipe.selected_options, ['Colour?', 'Where?'])

    @patch.object(runner.shared, 'question_candidates', return_value=['Colour?', 'Where?'])
    def test_grounding_changes_only_rewrite_and_stops_at_first_hit(self, _):
        pipe = Pipe(Worker(['grounded description']))
        d = runner.run_dialogue(pipe, 'grounded_rewrite', dict(qid=1, img='image0', caption='initial'), 0)
        self.assertEqual(pipe.selected_options, ['Colour?', 'Where?'])
        self.assertEqual(pipe.rewrite_calls, 0)
        self.assertEqual(d['ranks'], [12, 1])
        self.assertEqual(d['queries'], ['initial', 'grounded description'])

    @patch.object(runner.shared, 'question_candidates', return_value=['Colour?', 'Where?'])
    def test_novelty_keeps_baseline_rewrite(self, _):
        pipe = Pipe(Worker([json.dumps({'decisions': [dict(id=0, novel=False), dict(id=1, novel=True)]})]))
        d = runner.run_dialogue(pipe, 'novelty', dict(qid=1, img='image0', caption='initial'), 0)
        self.assertEqual(d['turns'][0]['question'], 'Where?')
        self.assertEqual(pipe.rewrite_calls, 1)
        self.assertEqual(d['queries'][-1], 'baseline rewrite')

    def test_graceful_stop_saves_current_and_resume_skips_completed(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            baseline = {i: dict(target=f'image{i}', queries=['initial'], ranks=[12]) for i in (0, 1)}
            tasks = [dict(qid=i, img=f'image{i}', caption='initial') for i in (0, 1)]
            def record(arm, task):
                return dict(arm=arm, qid=task['qid'], target=task['img'], queries=['initial','new'],
                            ranks=[12,1], turns=[{}], hits_only=True, seconds=1)
            first = out/'novelty/dialogues/0.json'
            runner.shared.atomic_json(first, record('novelty', tasks[0]))
            original = first.read_bytes()
            def run(pipe, arm, task, target):
                (out/'STOP').touch()
                return record(arm, task)
            worker = Worker()
            with patch.object(runner.mac, 'MacQwen', return_value=worker), \
                 patch.object(runner.mac, 'load_blip', return_value=Pipe(worker)), \
                 patch.object(runner, 'run_dialogue', side_effect=run) as execute:
                runner.run_queue(out, dict(tasks=tasks), baseline)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(execute.call_args.args[2]['qid'], 1)
            self.assertEqual(first.read_bytes(), original)
            self.assertTrue((out/'novelty/dialogues/1.json').exists())
            self.assertEqual(json.loads((out/'status.json').read_text())['state'], 'stopped')
            self.assertTrue(worker.closed)


if __name__ == '__main__':
    unittest.main()
