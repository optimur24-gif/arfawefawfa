"""Paragraph 43 PDF content, noun/adjective modes, UI and durable progress."""
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import Bot, Store
from latin_cards import LATIN43, LATIN_FIELDS, LATIN_FORM_FIELDS, load_latin_cards, latin_details
from latin_ui import LATIN43_RULES
from test_bot import FakeMessage, fake_update, sample_bank, user


class Paragraph43ContentTests(unittest.TestCase):
    def setUp(self):
        self.cards = load_latin_cards()
        self.chapter = [c for c in self.cards.values() if c['collection'] == 'paragraph43']

    def test_pdf_contains_exactly_66_distinct_source_cards(self):
        self.assertEqual(len(self.chapter), 66)
        self.assertEqual([c['source_number'] for c in self.chapter], list(range(1, 67)))
        self.assertEqual(sum(c['part_of_speech'] == 'adjective' for c in self.chapter), 44)
        self.assertEqual(sum(c['part_of_speech'] == 'noun' for c in self.chapter), 22)
        self.assertEqual({c['source_section'] for c in self.chapter}, {'А', 'Б', 'С'})
        self.assertEqual(len({c['id'] for c in self.chapter}), 66)
        self.assertEqual(self.chapter[0]['latin'], 'dexter')
        self.assertEqual(self.chapter[-1]['latin'], 'porus')
        self.assertTrue(all(c['rule'] and c['translation'] for c in self.chapter))

    def test_special_forms_and_os_follow_pdf_without_replacing_old_words(self):
        by_word = {c['latin']: c for c in self.chapter}
        expected = {'dexter': 'dexter, dextra, dextrum', 'liber': 'liber, libera, liberum',
                    'asper': 'asper, aspera, asperum', 'niger': 'niger, nigra, nigrum',
                    'sacer': 'sacer, sacra, sacrum', 'cervicalis': 'cervicalis, cervicalis, cervicale',
                    'mollis': 'mollis, mollis, molle'}
        for word, formula in expected.items():
            self.assertEqual(by_word[word]['formula'], formula)
        self.assertEqual(by_word['os']['formula'], 'os, oris n')
        self.assertEqual(by_word['os']['translation'], 'рот')
        self.assertEqual(self.cards['word_26']['formula'], 'os, ossis n')
        self.assertEqual(sum(c['collection'] != 'paragraph43' for c in self.cards.values()), 104)

    def test_rules_explain_all_three_genders_endings_and_agreement(self):
        for fragment in ('masculinum', 'femininum', 'neutrum', '-us', '-er', '-a', '-um', '-is', '-e',
                         'liber', 'asper', 'dexter', 'fibula dextra', 'palatum molle', 'os, oris n'):
            self.assertIn(fragment, LATIN43_RULES)
        self.assertLessEqual(len(LATIN43_RULES.encode('utf-16-le')) // 2, 4096)

    def test_full_cards_have_only_relevant_morphology(self):
        adjective = latin_details(self.cards['paragraph43_01'])
        self.assertIn('Мужской (m): dexter', adjective)
        self.assertIn('Женский (f): dextra', adjective)
        self.assertIn('Средний (n): dextrum', adjective)
        self.assertNotIn('Gen. sg.', adjective)
        noun = latin_details(self.cards['paragraph43_65'])
        self.assertIn('Gen. sg.: oris', noun)
        self.assertIn('средний (neutrum)', noun)
        self.assertNotIn('Мужской (m):', noun)


class Paragraph43StateTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.touch(user(1))
        self.store.touch(user(2))
        self.bot = Bot(self.store, sample_bank())
        self.bot.latin.rng = random.Random(43)

    def test_modes_filter_nouns_and_adjectives_and_generate_valid_choices(self):
        expected_counts = {'translation': 66, 'latin': 66, 'formula': 66, 'genitive': 22,
                           'gender': 22, 'masculine': 44, 'feminine': 44, 'neuter': 44}
        for field, expected_count in expected_counts.items():
            session = self.bot.latin.new(1, 'paragraph43', 'test', field)
            self.assertEqual(len(session['ids']), expected_count)
            for i, cid in enumerate(session['ids']):
                session['index'] = i
                self.bot.latin.prepare(session)
                choices = session['choices']
                self.assertTrue(2 <= len(choices) <= 4)
                self.assertEqual(len(choices), len(set(choices)))
                self.assertEqual(choices.count(session['expected']), 1)
                self.assertEqual(session['expected'], self.bot.latin.value(self.bot.latin.cards[cid], field))
                if field in LATIN_FORM_FIELDS:
                    self.assertEqual(self.bot.latin.cards[cid]['part_of_speech'], 'adjective')
                if field in ('genitive', 'gender'):
                    self.assertEqual(self.bot.latin.cards[cid]['part_of_speech'], 'noun')
        with self.assertRaises(ValueError):
            self.bot.latin.new(1, 'words', 'test', 'masculine')
        with self.assertRaises(ValueError):
            self.bot.latin.new(1, 'sayings', 'test', 'formula')

    def test_restart_preserves_answer_position_and_scoped_scores(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'progress.sqlite3'
            store = Store(path)
            try:
                store.touch(user(1))
                bot = Bot(store, sample_bank())
                anatomy = bot.quiz.new(1)
                session = bot.latin.new(1, 'paragraph43', 'test', 'feminine')
                choice = session['choices'].index(session['expected'])
                self.assertTrue(bot.latin.answer(1, choice))
                saved = bot.latin.state(1)
            finally:
                store.close()
            store = Store(path)
            try:
                restored = Bot(store, sample_bank())
                self.assertEqual(restored.latin.state(1), saved)
                self.assertEqual(store.state(1), anatomy)
                self.assertEqual(restored.latin.summary(1, 'paragraph43'), {'attempts': 1, 'correct': 1, 'wrong': 0})
                self.assertEqual(restored.latin.summary(1, 'words')['attempts'], 0)
                self.assertEqual(restored.latin.summary(2, 'paragraph43')['attempts'], 0)
                self.assertIsNone(restored.latin.answer(1, choice))
            finally:
                store.close()


class Paragraph43RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        self.store.touch(user(1))
        self.store.touch(user(2))
        self.bot = Bot(self.store, sample_bank())
        self.context = SimpleNamespace(bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()))

    async def asyncTearDown(self):
        self.store.close()

    def callback(self, action, uid=1):
        panel = self.bot.latin.state(uid)['panel']
        query = SimpleNamespace(data=f"lt:{panel['token']}:{action}", message=FakeMessage(panel['message_id']),
                                answer=AsyncMock(), edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
        return fake_update(user(uid), query=query)

    async def test_separate_main_button_and_command_open_chapter(self):
        buttons = [button.text for row in self.bot.menu(user(1)).keyboard for button in row]
        self.assertIn(LATIN43, buttons)
        update = fake_update(user(1))
        update.effective_message.text = LATIN43
        await self.bot.message(update, self.context)
        text, markup = update.effective_message.replies[-1]
        self.assertIn('Латынь, 43 параграф', text)
        self.assertIn('66 слов', text)
        actions = [b.callback_data.split(':', 2)[2] for row in markup.inline_keyboard for b in row]
        for action in ('rules43', 'new:paragraph43:test:masculine', 'new:paragraph43:test:feminine',
                       'new:paragraph43:test:neuter', 'new:paragraph43:test:genitive'):
            self.assertIn(action, actions)
        command = fake_update(user(1))
        command.effective_message.text = '/latin43'
        await self.bot.command(command, self.context)
        self.assertIn('Латынь, 43 параграф', command.effective_message.replies[-1][0])
        await self.bot.callback(self.callback('rules43'), self.context)
        back = self.callback('set:paragraph43')
        await self.bot.callback(back, self.context)
        self.assertIn('Выбери способ', back.callback_query.edit_message_text.call_args.kwargs['text'])

    async def test_cards_reveal_mfn_and_test_answers_are_personal_and_idempotent(self):
        self.bot.latin.new(1, 'paragraph43', 'cards', 'translation')
        await self.bot.latin_session(fake_update(user(1)), self.context, False)
        reveal = self.callback('reveal')
        await self.bot.callback(reveal, self.context)
        text = reveal.callback_query.edit_message_text.call_args.kwargs['text']
        self.assertIn('dexter, dextra, dextrum', text)
        self.assertIn('Правило / особенности', text)
        self.assertEqual(self.bot.latin.summary(1)['attempts'], 0)
        self.bot.latin.new(1, 'paragraph43', 'test', 'masculine')
        item = fake_update(user(1))
        await self.bot.latin_session(item, self.context, False)
        session = self.bot.latin.state(1)['session']
        prompt = item.effective_message.replies[-1][0].split('\n\n')[1]
        self.assertNotIn(session['expected'], prompt)
        answer = self.callback('answer:' + str(session['choices'].index(session['expected'])))
        await self.bot.callback(answer, self.context)
        await self.bot.callback(answer, self.context)
        self.assertEqual(self.bot.latin.summary(1, 'paragraph43')['attempts'], 1)
        foreign = self.callback('next')
        foreign.effective_user = user(2)
        foreign.effective_chat.id = 2
        await self.bot.callback(foreign, self.context)
        foreign.callback_query.edit_message_text.assert_not_awaited()
        self.assertEqual(self.bot.latin.summary(2)['attempts'], 0)

    async def test_both_directions_all_fields_finish_and_fit_telegram(self):
        for field in ('translation', 'latin'):
            session = self.bot.latin.new(1, 'paragraph43', 'cards', field)
            for i, cid in enumerate(session['ids']):
                state = self.bot.latin.state(1)
                state['session'].update(index=i, revealed=True)
                self.bot.latin.save(1, state)
                item = fake_update(user(1))
                await self.bot.latin_session(item, self.context, False)
                text, markup = item.effective_message.replies[-1]
                self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)
                for row in markup.inline_keyboard:
                    for button in row:
                        self.assertLessEqual(len(button.callback_data.encode()), 64)
        for field in {**LATIN_FIELDS, **LATIN_FORM_FIELDS}:
            session = self.bot.latin.new(1, 'paragraph43', 'test', field)
            session['index'] = len(session['ids']) - 1
            self.bot.latin.prepare(session)
            state = self.bot.latin.state(1)
            state['session'] = session
            self.bot.latin.save(1, state)
            item = fake_update(user(1))
            await self.bot.latin_session(item, self.context, False)
            await self.bot.callback(self.callback('answer:' + str(session['choices'].index(session['expected']))), self.context)
            finish = self.callback('next')
            await self.bot.callback(finish, self.context)
            self.assertIn('Раздел завершён: Латынь, 43 параграф', finish.callback_query.edit_message_text.call_args.kwargs['text'])

    async def test_pause_and_resume_from_separate_button_keeps_position(self):
        self.bot.latin.new(1, 'paragraph43', 'cards', 'latin')
        await self.bot.latin_session(fake_update(user(1)), self.context, False)
        await self.bot.callback(self.callback('next'), self.context)
        saved = self.bot.latin.state(1)['session']
        await self.bot.callback(self.callback('home'), self.context)
        await self.bot.action(fake_update(user(1)), self.context, LATIN43)
        self.assertEqual(self.bot.latin.state(1)['session'], saved)
        await self.bot.callback(self.callback('resume'), self.context)
        self.assertEqual(self.bot.latin.state(1)['session'], saved)


if __name__ == '__main__':
    unittest.main()
