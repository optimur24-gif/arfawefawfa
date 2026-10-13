"""Regression checks for the osteology-only bank."""
import hashlib
import json
import unittest
from bot import load_questions, Quiz, Store
from answer_helpers import is_correct_selection

class OsteologyBankTests(unittest.TestCase):
    def test_only_osteology_and_source_content_preserved(self):
        bank = load_questions()
        self.assertEqual(len(bank), 103)
        self.assertEqual({q['topic'] for q in bank.values()}, {'кости'})
        fields = ('id', 'page', 'page_end', 'question', 'source_options', 'source_key', 'correct', 'source_number')
        snapshot = [{f: q[f] for f in fields} for _, q in sorted(bank.items())]
        raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), 'ab6c48d2ca5ff5f474de5d129240e2c35144bd4bd52b2603011667e6d2ce896f')
        for q in bank.values():
            self.assertTrue(q['enabled'])
            self.assertTrue(q['source_faithful'])
            self.assertTrue(is_correct_selection(q, q['correct']))

    def test_all_quiz_modes_use_only_bones(self):
        bank = load_questions()
        with self.subTest(mode='pool'):
            store = Store(':memory:')
            self.addCleanup(store.close)
            quiz = Quiz(store, bank)
            self.assertEqual(len(quiz.ids), 103)
            self.assertTrue(all(bank[q]['topic'] == 'кости' for q in quiz.ids))
