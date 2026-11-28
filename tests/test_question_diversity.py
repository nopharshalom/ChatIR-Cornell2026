import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_question_diversity as pilot


def response(q):
    return {'choices': [{'message': {'content': q}}]}


def generate(drafts, known=(), fallbacks=(), unique=True):
    pipe = Mock()
    pipe.generate_questions_referring.side_effect = map(response, drafts)
    pipe.generate_questions.side_effect = map(response, fallbacks)
    pipe.filter_questions.side_effect = lambda query, q: 'known' if q in known else 'uncertain'
    audit = []
    qs = pilot.proposals(pipe, ['base'], 'base', ['one', 'two'], unique=unique, audit=audit)
    return qs, audit[0], pipe


class QuestionDiversityTests(unittest.TestCase):
    def test_retry_budget_belongs_to_each_slot_and_prior_is_retained(self):
        qs, audit, pipe = generate(
            ['K?', 'K?', 'A?', 'K?', 'B?', 'C?', 'D?', 'E?'], known=['K?'], unique=False)
        self.assertEqual(qs, ['A?', 'B?', 'C?', 'D?', 'E?'])
        self.assertEqual(audit['fallbacks'], 0)
        self.assertTrue(audit['retry_changed'])
        self.assertIn('K?', pipe.generate_questions_referring.call_args.kwargs['ques_prior'])
        qs, audit, _ = generate(['K?', 'A?'] + ['K?']*3 + ['C?', 'D?', 'E?'],
                                known=['K?'], fallbacks=['B?'], unique=False)
        self.assertEqual(qs, ['A?', 'B?', 'C?', 'D?', 'E?'])
        self.assertEqual(audit['fallbacks'], 1)

    def test_replace_exact_duplicates_but_allow_paraphrases_and_keep_original_text(self):
        first = '**Question:** What color is the bus?'
        same = 'Question: Question: what COLOR is the bus?'
        paraphrase = 'What is the color of the bus?'
        qs, audit, pipe = generate([first, same, paraphrase, 'C?', 'D?', 'E?'])
        self.assertEqual(qs, [first, paraphrase, 'C?', 'D?', 'E?'])
        self.assertEqual(audit['duplicates'], 1)
        calls = pipe.generate_questions_referring.call_args_list
        self.assertNotIn(pilot.RETRY_HINT, calls[1].kwargs['prompt_fake'])
        self.assertIn(pilot.RETRY_HINT, calls[2].kwargs['prompt_fake'])

    def test_repeated_fallback_or_exhausted_slot_cannot_pad_the_slate(self):
        qs, audit, _ = generate(['K?']*15, known=['K?'], fallbacks=['K?']*5)
        self.assertEqual(qs, [pilot.GENERIC])
        self.assertEqual(audit['duplicates'], 4)
        qs, audit, _ = generate(['A?', 'A?', 'K?', 'K?', 'B?', 'C?', 'D?'], known=['K?'])
        self.assertEqual(qs, ['A?', 'B?', 'C?', 'D?'])
        self.assertEqual(audit['fallbacks'], 0)

    def test_history_stack_reuses_only_complete_identical_decisions(self):
        history = ['a bus', 'What color? red']
        qs = ['What color?', 'Where?']
        kept, extra = pilot.history_options(history, qs, qs[0])
        self.assertEqual(kept, ['Where?'])
        self.assertTrue(extra['history_changed'])
        kept, extra = pilot.history_options(history, qs[:1], qs[0])
        self.assertEqual(kept, qs[:1])
        self.assertTrue(extra['history_all_blocked'])
        kept, _ = pilot.history_options(['a bus', 'What color? unsure'], qs, qs[0])
        self.assertEqual(kept, qs)
        t1 = dict(question=qs[0], answer='red', candidate_questions=qs)
        t2 = dict(question='Where?', answer='street', candidate_questions=qs)
        row = dict(queries=['a bus'], turns=[t1, t2], proposal_audit=[{}, {}], ranks=[32, 21, 7])
        saved = pilot.reuse_if_identical(row)
        self.assertEqual(saved['ranks'], row['ranks'])
        self.assertEqual(saved['proposal_audit'][1]['eligible'], 1)
        self.assertEqual(row['proposal_audit'][1], {})
        t2['question'] = qs[0]
        self.assertIsNone(pilot.reuse_if_identical(row))


if __name__ == '__main__':
    unittest.main()
