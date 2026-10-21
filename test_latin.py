"""Behavioral regressions for Latin practice, independent of Telegram/network."""
import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from bot import Bot, Store, START
from latin_cards import LATIN, LatinStore, load_latin_cards, LATIN_FIELDS
from test_bot import FakeMessage, fake_update, sample_bank, user


class LatinPersistenceTests(unittest.TestCase):
    def test_session_and_scores_survive_restart_without_changing_osteology(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder)/'progress.sqlite3'
            store = Store(db)
            store.touch(user(1));store.touch(user(2))
            bot = Bot(store,sample_bank())
            osteology = bot.quiz.new(1)
            bot.latin.new(1,'words','test','genitive')
            s=bot.latin.state(1)['session']
            bot.latin.answer(1,s['choices'].index(s['expected']))
            expected=bot.latin.state(1)
            store.close()
            store=Store(db);self.addCleanup(store.close)
            restored=Bot(store,sample_bank())
            self.assertEqual(restored.latin.state(1),expected)
            self.assertEqual(store.state(1),osteology)
            self.assertEqual(restored.latin.summary(1),{'attempts':1,'correct':1,'wrong':0})
            self.assertEqual(restored.latin.summary(2)['attempts'],0)
            self.assertIsNone(restored.latin.answer(1,0))
            self.assertEqual(restored.latin.summary(1)['attempts'],1)

    def test_sources_and_full_forms_have_expected_counts(self):
        cards=load_latin_cards()
        self.assertEqual(len(cards),170)
        self.assertEqual({g:sum(c['collection']==g for c in cards.values()) for g in ['words','sayings','medical']}, {'words':57,'sayings':30,'medical':17})
        self.assertEqual(cards['word_07']['formula'],'caput, capitis n')
        self.assertEqual(cards['word_26']['formula'],'os, ossis n')
        self.assertEqual(cards['word_27']['formula'],'raphe, raphes f')
        self.assertEqual(cards['word_39']['formula'],'genu, genus n')
        self.assertEqual(cards['sayings_30']['latin'],'Invia est in medicina via sine lingua latina')
        self.assertEqual(cards['medical_17']['latin'],'Prognosis pessima')


class LatinRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:')
        self.store.touch(user(1));self.store.touch(user(2))
        self.bot=Bot(self.store,sample_bank())
        self.bot.latin.rng=random.Random(9)
        self.context=SimpleNamespace(bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()))

    async def asyncTearDown(self):
        self.store.close()

    def callback(self,action,uid=1):
        panel=self.bot.latin.state(uid)['panel']
        q=SimpleNamespace(data=f"lt:{panel['token']}:{action}",message=FakeMessage(panel['message_id']),answer=AsyncMock(),edit_message_text=AsyncMock(),edit_message_reply_markup=AsyncMock())
        return fake_update(user(uid),query=q)

    async def test_actual_menu_and_command_open_latin_and_reveal_full_card(self):
        update=fake_update(user(1))
        self.assertIn(LATIN,[button.text for row in self.bot.menu(user(1)).keyboard for button in row])
        update.effective_message.text='/latin'
        await self.bot.command(update,self.context)
        selection=self.callback('new:words:cards:translation')
        await self.bot.callback(selection,self.context)
        before=selection.callback_query.edit_message_text.call_args.kwargs['text']
        self.assertNotIn('alae',before)
        reveal=self.callback('reveal')
        await self.bot.callback(reveal,self.context)
        text=reveal.callback_query.edit_message_text.call_args.kwargs['text']
        self.assertIn('ala, alae f',text)
        self.assertIn('Правило / исключения:',text)
        self.assertIn('poeta',text)
        self.assertEqual(self.bot.latin.summary(1)['attempts'],0)

    async def test_quiz_correct_wrong_stale_buttons_and_user_isolation(self):
        self.bot.latin.new(1,'words','test','formula')
        await self.bot.latin_session(fake_update(user(1)),self.context,False)
        s=self.bot.latin.state(1)['session']
        answer=self.callback('answer:'+str(s['choices'].index(s['expected'])))
        await self.bot.callback(answer,self.context)
        await self.bot.callback(answer,self.context)
        self.assertEqual(self.bot.latin.summary(1)['attempts'],1)
        self.assertEqual(self.store.user_summary(1)['attempts'],0)
        next_item=self.callback('next')
        await self.bot.callback(next_item,self.context)
        s=self.bot.latin.state(1)['session']
        wrong=next(i for i,c in enumerate(s['choices']) if c!=s['expected'])
        await self.bot.callback(self.callback(f'answer:{wrong}'),self.context)
        self.assertEqual(self.bot.latin.summary(1),{'attempts':2,'correct':1,'wrong':1})
        foreign=self.callback('next')
        foreign.effective_user=user(2);foreign.effective_chat.id=2
        await self.bot.callback(foreign,self.context)
        foreign.callback_query.edit_message_text.assert_not_awaited()
        self.assertEqual(self.bot.latin.summary(2)['attempts'],0)

    async def test_osteology_navigation_invalidates_latin_buttons_but_keeps_session(self):
        self.bot.latin.new(1,'sayings','cards','latin')
        await self.bot.latin_session(fake_update(user(1)),self.context,False)
        old=self.callback('reveal')
        expected=self.bot.latin.state(1)['session']
        await self.bot.action(fake_update(user(1)),self.context,START)
        await self.bot.callback(old,self.context)
        old.callback_query.edit_message_text.assert_not_awaited()
        self.assertEqual(self.bot.latin.state(1)['session'],expected)

    async def test_all_cards_and_test_fields_fit_telegram_and_finish(self):
        for group in ['words','sayings','medical']:
            fields=list(LATIN_FIELDS) if group=='words' else ['translation','latin']
            for field in fields:
                self.bot.latin.new(1,group,'test',field)
                s=self.bot.latin.state(1)['session']
                s['index']=len(s['ids'])-1
                self.bot.latin.prepare(s)
                state=self.bot.latin.state(1);state['session']=s;self.bot.latin.save(1,state)
                update=fake_update(user(1))
                await self.bot.latin_session(update,self.context,False)
                choice=s['choices'].index(s['expected'])
                await self.bot.callback(self.callback(f'answer:{choice}'),self.context)
                end=self.callback('next');await self.bot.callback(end,self.context)
                self.assertIn('Раздел завершён',end.callback_query.edit_message_text.call_args.kwargs['text'])
            self.bot.latin.new(1,group,'cards','translation')
            for i in range(len(self.bot.latin.state(1)['session']['ids'])):
                state=self.bot.latin.state(1);state['session'].update(index=i,revealed=True);self.bot.latin.save(1,state)
                update=fake_update(user(1));await self.bot.latin_session(update,self.context,False)
                text,markup=update.effective_message.replies[-1]
                self.assertLessEqual(len(text.encode('utf-16-le'))//2,4096)
                for row in markup.inline_keyboard:
                    for b in row:self.assertLessEqual(len(b.callback_data.encode()),64)


if __name__=='__main__':
    unittest.main()
