"""Offline behavioral checks. Run: python -m unittest -v test_bot.py"""
import copy
import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from answer_helpers import answer_options, selected_letters

from bot import (ADMIN, AGAIN, BACK, LEARNING, PRACTICE, RESUME, START, STOP, Bot,
                 Learning, Quiz, Store, load_questions, question_view)


def user(uid, username=None):
    return SimpleNamespace(id=uid, username=username, first_name=f"User {uid}", last_name=None)


def sample_bank():
    result = {
        1: {"id": 1, "page": 4, "question": "Один ответ", "options": {"а": "A", "б": "B", "в": "C"}, "correct": ["б"], "enabled": True},
        2: {"id": 2, "page": 5, "question": "Несколько ответов", "options": {"а": "A", "б": "B", "в": "C"}, "correct": ["а", "в"], "enabled": True},
        3: {"id": 3, "page": 6, "question": "Все варианты верны", "options": {"а": "A", "б": "B"}, "correct": ["а", "б"], "enabled": True},
        5: {"id": 5, "page": 6, "question": "Отключён", "options": {}, "correct": [], "enabled": False},
    }
    result[4] = dict(copy.deepcopy(result[1]), id=4)
    return result


def correct_choice_ids(question):
    """Choose the source key using actual button IDs, including repeated labels."""
    options = answer_options(question)
    return [next(option["id"] for option in options if option["letter"] == letter)
            for letter in question["correct"]]


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.touch(user(1))
        self.store.touch(user(2))
        self.bank = sample_bank()
        self.quiz = Quiz(self.store, self.bank, random.Random(10))

    def tearDown(self):
        self.store.close()

    def force(self, qid, uid=1):
        state = self.quiz.new(uid)
        state.update(current=qid, seen=[qid], selected=[], answered=False)
        self.store.save(uid, state)
        return state

    def answer(self, qid, keys, uid=1):
        state = self.force(qid, uid)
        for key in keys:
            self.quiz.toggle(uid, state["token"], key)
        return self.quiz.submit(uid, state["token"])

    def test_random_session_has_no_repeats_or_exact_duplicates(self):
        state = self.quiz.new(1)
        issued = []
        while state["status"] == "running":
            qid = state["current"]
            issued.append(qid)
            for key in correct_choice_ids(self.bank[qid]):
                self.quiz.toggle(1, state["token"], key)
            self.quiz.submit(1, state["token"])
            state = self.quiz.next(1)
        self.assertCountEqual(issued, [1, 2, 3])
        self.assertEqual(len(issued), len(set(issued)))
        self.assertEqual(state["correct_count"], 3)
        self.assertEqual(self.store.user_summary(1)["attempts"], 3)

    def test_single_choice_replaces_selection_and_can_be_unselected(self):
        state = self.force(1)
        self.quiz.toggle(1, state["token"], "а")
        self.assertEqual(self.quiz.toggle(1, state["token"], "б")["selected"], ["б"])
        self.assertEqual(self.quiz.toggle(1, state["token"], "б")["selected"], [])
        self.assertEqual(self.quiz.submit(1, state["token"]), "empty")
        self.assertEqual(self.store.user_summary(1)["attempts"], 0)

    def test_multiple_choice_requires_exact_set(self):
        for keys, expected in [(["а"], False), (["а", "б", "в"], False), (["в", "а"], True)]:
            with self.subTest(keys=keys):
                state = self.answer(2, keys)
                self.assertEqual(bool(state["correct_count"]), expected)
        summary = self.store.user_summary(1)
        self.assertEqual((summary["attempts"], summary["wrong"], summary["correct"]), (3, 2, 1))

    def test_all_options_can_be_correct(self):
        self.assertEqual(self.answer(3, ["а", "б"])["correct_count"], 1)

    def test_double_submit_and_stale_tokens_are_ignored(self):
        state = self.answer(2, ["а", "в"])
        self.assertIsNone(self.quiz.submit(1, state["token"]))
        self.assertIsNone(self.quiz.toggle(1, state["token"], "б"))
        self.quiz.next(1)
        self.assertIsNone(self.quiz.submit(1, state["token"]))
        self.assertEqual(self.store.user_summary(1)["attempts"], 1)

    def test_pause_resume_preserves_question_seen_and_selection(self):
        state = self.force(2)
        self.quiz.toggle(1, state["token"], "а")
        paused = self.quiz.pause(1)
        self.assertIsNone(self.quiz.toggle(1, paused["token"], "в"))
        resumed = self.quiz.resume(1)
        self.assertEqual((resumed["current"], resumed["seen"], resumed["selected"]), (2, [2], ["а"]))
        self.assertNotEqual(resumed["token"], state["token"])
        self.assertIsNone(self.quiz.submit(1, state["token"]))

    def test_reset_and_restart_preserve_history(self):
        state = self.answer(2, ["а"])
        self.quiz.reset(1)
        self.assertIsNone(self.store.state(1))
        self.assertIsNone(self.quiz.submit(1, state["token"]))
        self.quiz.new(1)
        self.assertEqual(self.store.user_summary(1)["wrong"], 1)
        self.assertEqual(len(self.store.wrong_questions(1)), 1)

    def test_personal_errors_are_isolated_and_sorted(self):
        self.answer(1, ["а"])
        self.answer(2, ["а"])
        self.answer(2, ["а"])
        self.answer(3, ["а"], uid=2)
        self.assertEqual([r["question_id"] for r in self.store.wrong_questions(1)], [2, 1])
        self.assertEqual([r["question_id"] for r in self.store.wrong_questions(2)], [3])
        self.assertEqual(self.quiz.new(1, "errors")["pool"], [2, 1])
        self.assertEqual(self.quiz.new(2, "errors")["pool"], [3])
        self.answer(2, ["а", "в"])
        self.assertEqual(self.store.wrong_questions(1)[0]["wrong"], 2)

    def test_empty_error_practice_keeps_existing_session(self):
        current = self.quiz.new(1)
        self.assertIsNone(self.quiz.new(1, "errors"))
        self.assertEqual(self.store.state(1), current)

    def test_database_unique_answer_constraint(self):
        state = self.answer(1, ["б"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record(1, state, self.bank[1])
        self.assertEqual(self.store.user_summary(1)["attempts"], 1)
        self.assertEqual(self.store.state(1)["correct_count"], 1)

    def test_sqlite_survives_process_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "progress.sqlite3"
            first = Store(path)
            first.touch(user(11))
            quiz = Quiz(first, self.bank)
            state = quiz.new(11)
            key = correct_choice_ids(self.bank[state["current"]])[0]
            quiz.toggle(11, state["token"], key)
            quiz.submit(11, state["token"])
            saved = first.state(11)
            first.close()
            second = Store(path)
            self.assertEqual(second.state(11), saved)
            self.assertEqual(second.user_summary(11)["attempts"], 1)
            second.close()

    def test_admin_binding_is_case_insensitive_and_stays_with_id(self):
        self.assertFalse(self.store.is_admin(user(1, "someone")))
        self.assertTrue(self.store.is_admin(user(2, "tImUr0_15")))
        self.assertTrue(self.store.is_admin(user(2, "renamed")))
        self.assertFalse(self.store.is_admin(user(1, "Timur0_15")))
        self.assertTrue(self.store.is_admin(user(1), configured_id=1))
        self.assertFalse(self.store.is_admin(user(2, "Timur0_15"), configured_id=1))


class FakeMessage:
    def __init__(self, message_id=1):
        self.message_id = message_id
        self.text = ""
        self.replies = []
        self.documents = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))
        return FakeMessage(100 + len(self.replies))

    async def reply_document(self, data, filename):
        self.documents.append((filename, data.getvalue()))


class LearningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bank = load_questions()

    def setUp(self):
        self.store = Store(":memory:")
        self.store.touch(user(1))
        self.store.touch(user(2))
        self.quiz = Quiz(self.store, self.bank, random.Random(123))
        self.learning = Learning(self.store, self.quiz)

    def tearDown(self):
        self.store.close()

    def test_create_30_then_add_30_gives_60_unique_questions(self):
        state, added = self.learning.change(1, 30, "create")
        first = list(state["ids"])
        self.assertEqual((len(first), added), (30, 30))
        expanded, added = self.learning.change(1, 30, "add", state["version"])
        self.assertEqual((len(expanded["ids"]), added), (60, 30))
        self.assertEqual(expanded["ids"][:30], first)
        self.assertEqual(len(set(expanded["ids"])), 60)
        self.assertTrue(set(expanded["ids"]) <= set(self.quiz.ids))

    def test_sets_are_personal_and_create_replaces_only_own_list(self):
        first, _ = self.learning.change(1, 30, "create")
        second, _ = self.learning.change(2, 50, "create")
        self.learning.change(1, 10, "create")
        self.assertEqual(len(self.learning.get(1)["ids"]), 10)
        self.assertEqual(self.learning.get(2), second)
        self.learning.reset(1)
        self.assertEqual(self.learning.get(1)["ids"], [])
        self.assertEqual(self.learning.get(2), second)

    def test_stale_add_and_reset_cannot_change_list_twice(self):
        state, _ = self.learning.change(1, 30, "create")
        old_version = state["version"]
        current, _ = self.learning.change(1, 30, "add", old_version)
        self.assertIsNone(self.learning.change(1, 30, "add", old_version))
        self.assertIsNone(self.learning.reset(1, old_version))
        self.assertEqual(self.learning.get(1), current)

    def test_limit_adds_remaining_without_repeats_and_rejects_invalid_count(self):
        self.learning.change(1, len(self.quiz.ids)-10, "create")
        state, added = self.learning.change(1, 30, "add")
        self.assertEqual(added, 10)
        self.assertEqual(len(set(state["ids"])), len(self.quiz.ids))
        state, added = self.learning.change(1, 30, "add")
        self.assertEqual(added, 0)
        for invalid in (0, -1, len(self.quiz.ids)+1, True, 3.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.learning.change(1, invalid, "create")
        self.assertEqual(len(self.learning.get(1)["ids"]), len(self.quiz.ids))

    def test_quiz_pool_expands_from_30_to_60_and_restarts_in_same_mode(self):
        original, _ = self.learning.change(1, 30, "create")
        session = self.quiz.new(1, "learning")
        current = session["current"]
        expanded, _ = self.learning.change(1, 30, "add")
        state = self.store.state(1)
        self.assertEqual(state["current"], current)
        self.assertCountEqual(state["pool"], expanded["ids"])
        self.assertEqual(state["seen"], [current])
        issued = []
        while state["status"] == "running":
            issued.append(state["current"])
            for key in correct_choice_ids(self.bank[state["current"]]):
                self.quiz.toggle(1, state["token"], key)
            self.quiz.submit(1, state["token"])
            state = self.quiz.next(1)
        self.assertCountEqual(issued, expanded["ids"])
        self.assertEqual(len(issued), len(set(issued)))
        self.assertEqual(self.quiz.new(1, "learning")["pool"], expanded["ids"])

    def test_reset_keeps_answer_history_and_regular_session(self):
        self.learning.change(1, 30, "create")
        session = self.quiz.new(1, "learning")
        for key in correct_choice_ids(self.bank[session["current"]]):
            self.quiz.toggle(1, session["token"], key)
        self.quiz.submit(1, session["token"])
        self.learning.reset(1)
        self.assertIsNone(self.store.state(1))
        self.assertEqual(self.store.user_summary(1)["attempts"], 1)
        regular = self.quiz.new(1)
        self.learning.change(1, 30, "create")
        self.learning.reset(1)
        self.assertEqual(self.store.state(1), regular)

    def test_learning_list_pending_and_position_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "learning.sqlite3"
            first = Store(path)
            first.touch(user(1))
            engine = Learning(first, Quiz(first, self.bank))
            state, _ = engine.change(1, 30, "create")
            state.update(study_index=12, revealed=True)
            first.save_learning(1, state)
            expected = engine.prompt(1, "add")
            first.close()
            second = Store(path)
            self.assertEqual(second.learning(1), expected)
            second.close()


def fake_update(person, message=None, query=None):
    return SimpleNamespace(effective_user=person, effective_chat=SimpleNamespace(id=person.id, type="private"),
                           effective_message=message or FakeMessage(), callback_query=query)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.store.touch(user(1, "student"))
        self.store.touch(user(2, "Timur0_15"))
        self.bot = Bot(self.store, sample_bank())
        self.context = SimpleNamespace(bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()))

    async def asyncTearDown(self):
        self.store.close()

    def query(self, data, message_id):
        return SimpleNamespace(data=data, message=FakeMessage(message_id), answer=AsyncMock(), edit_message_text=AsyncMock())

    async def test_start_pick_submit_next_stop_resume_and_back(self):
        update = fake_update(user(1, "student"))
        await self.bot.action(update, self.context, START)
        state = self.store.state(1)
        q = self.bot.bank[state["current"]]
        for key in correct_choice_ids(q):
            query = self.query(f"pick:{state['token']}:{key}", state["message_id"])
            await self.bot.callback(fake_update(user(1, "student"), query=query), self.context)
            query.edit_message_text.assert_awaited_once()
        query = self.query(f"check:{state['token']}", state["message_id"])
        await self.bot.callback(fake_update(user(1, "student"), query=query), self.context)
        self.assertEqual(self.store.user_summary(1)["correct"], 1)
        await self.bot.callback(fake_update(user(1, "student"), query=query), self.context)
        self.assertEqual(self.store.user_summary(1)["correct"], 1)
        query = self.query(f"next:{state['token']}", state["message_id"])
        await self.bot.callback(fake_update(user(1, "student"), query=query), self.context)
        self.assertNotEqual(self.store.state(1)["current"], state["current"])
        await self.bot.action(update, self.context, STOP)
        self.assertEqual(self.store.state(1)["status"], "paused")
        await self.bot.action(update, self.context, RESUME)
        self.assertEqual(self.store.state(1)["status"], "running")
        await self.bot.action(update, self.context, BACK)
        self.assertIsNone(self.store.state(1))
        self.assertEqual(self.store.user_summary(1)["attempts"], 1)

    async def test_old_message_and_other_users_buttons_cannot_submit(self):
        update = fake_update(user(1, "student"))
        await self.bot.action(update, self.context, START)
        state = self.store.state(1)
        query = self.query(f"check:{state['token']}", state["message_id"]+1)
        await self.bot.callback(fake_update(user(1), query=query), self.context)
        self.assertIn("устарела", query.answer.call_args.args[0])
        query = self.query(f"check:{state['token']}", state["message_id"])
        await self.bot.callback(fake_update(user(2, "Timur0_15"), query=query), self.context)
        self.assertIn("устарела", query.answer.call_args.args[0])
        self.assertEqual(self.store.user_summary(1)["attempts"], 0)

    async def test_admin_commands_buttons_and_exports_are_protected(self):
        update = fake_update(user(1, "student"))
        await self.bot.action(update, self.context, ADMIN)
        self.assertIn("только администратору", update.effective_message.replies[-1][0])
        for data in ["admin_page:0", "admin_user:2:0", "admin_export", "admin_answers:2"]:
            query = self.query(data, 1)
            await self.bot.callback(fake_update(user(1, "student"), query=query), self.context)
            self.assertIn("администратора", query.answer.call_args.args[0])
        admin = fake_update(user(2, "Timur0_15"))
        await self.bot.action(admin, self.context, ADMIN)
        text = admin.effective_message.replies[-1][0]
        self.assertIn("@student", text)
        self.assertIn("ID 1", text)
        await self.bot.admin_export(admin, self.context)
        csv_data = admin.effective_message.documents[-1][1].decode("utf-8-sig")
        self.assertIn("@student", csv_data)
        self.assertIn("@Timur0_15", csv_data)

    async def test_error_list_and_export_are_personal(self):
        state = self.bot.quiz.new(1)
        state.update(current=2, seen=[2], selected=["а"])
        self.store.save(1, state)
        self.bot.quiz.submit(1, state["token"])
        update = fake_update(user(1, "student"))
        await self.bot.export_mistakes(update, self.context)
        data = update.effective_message.documents[-1][1].decode("utf-8-sig")
        self.assertIn("Несколько ответов", data)
        other = fake_update(user(2, "Timur0_15"))
        await self.bot.mistakes(other, self.context)
        self.assertIn("Пока нет ошибок", other.effective_message.replies[-1][0])
        await self.bot.action(update, self.context, PRACTICE)
        self.assertEqual(self.store.state(1)["pool"], [2])

    async def test_learning_custom_count_card_reveal_and_personal_export(self):
        update = fake_update(user(1, "student"))
        await self.bot.action(update, self.context, LEARNING)
        await self.bot.learning_prompt(update, self.context, "create")
        update.effective_message.text = "2"
        await self.bot.message(update, self.context)
        state = self.store.learning(1)
        self.assertEqual(len(state["ids"]), 2)
        self.assertIsNone(state["pending"])
        self.assertEqual(self.store.learning(2)["ids"], [])
        query = self.query(f"learn_card:{state['version']}:0:0", 1)
        await self.bot.callback(fake_update(user(1, "student"), query=query), self.context)
        self.assertNotIn("✅ Ответ по ключу PDF:", query.edit_message_text.call_args.kwargs["text"])
        query = self.query(f"learn_card:{state['version']}:0:1", 1)
        await self.bot.callback(fake_update(user(1, "student"), query=query), self.context)
        self.assertIn("✅ Ответ по ключу PDF:", query.edit_message_text.call_args.kwargs["text"])
        self.assertEqual(self.store.user_summary(1)["attempts"], 0)
        await self.bot.learning_export(update, self.context)
        self.assertIn("2 заданий", update.effective_message.documents[-1][1].decode("utf-8-sig"))

    async def test_learning_stale_and_other_user_buttons_do_not_modify_lists(self):
        first, _ = self.bot.learning.change(1, 2, "create")
        query = self.query(f"learn_reset:{first['version']}", 1)
        await self.bot.callback(fake_update(user(2, "Timur0_15"), query=query), self.context)
        self.assertEqual(len(self.store.learning(1)["ids"]), 2)
        self.assertIn("изменился", query.answer.call_args.args[0])
        self.bot.learning.change(1, 1, "add")
        query = self.query(f"learn_reset:{first['version']}", 1)
        await self.bot.callback(fake_update(user(1, "student"), query=query), self.context)
        self.assertEqual(len(self.store.learning(1)["ids"]), 3)

    async def test_learning_quiz_again_and_general_back_keep_list(self):
        state, _ = self.bot.learning.change(1, 2, "create")
        query = self.query(f"learn_quiz:{state['version']}", 1)
        await self.bot.callback(fake_update(user(1, "student"), query=query), self.context)
        self.assertEqual(self.store.state(1)["mode"], "learning")
        update = fake_update(user(1, "student"))
        await self.bot.action(update, self.context, AGAIN)
        self.assertEqual(self.store.state(1)["mode"], "learning")
        self.assertEqual(len(self.store.state(1)["pool"]), 2)
        await self.bot.action(update, self.context, BACK)
        self.assertEqual(self.store.learning(1)["ids"], state["ids"])

    async def test_learning_menu_cancels_pending_numeric_request(self):
        update = fake_update(user(1, "student"))
        await self.bot.learning_prompt(update, self.context, "create")
        update.effective_message.text = LEARNING
        await self.bot.message(update, self.context)
        self.assertIsNone(self.store.learning(1)["pending"])


class FullBankTests(unittest.TestCase):
    def test_bank_contains_only_osteology(self):
        bank = load_questions()
        self.assertEqual(len(bank), 103)
        self.assertEqual({q['topic'] for q in bank.values()}, {'кости'})
        self.assertTrue(all(q['enabled'] for q in bank.values()))

    def test_every_enabled_question_can_be_answered_and_fits_telegram(self):
        bank = load_questions()
        store = Store(":memory:")
        store.touch(user(1))
        quiz = Quiz(store, bank)
        state = quiz.new(1)
        self.assertEqual(len(quiz.ids), 103)
        for qid, question in bank.items():
            if not question["enabled"]:
                continue
            state.update(current=qid, seen=[qid], selected=[], answered=False, token=f"test{qid}")
            store.save(1, state)
            # Duplicates are deliberately not issued by the engine.
            if qid not in quiz.ids:
                continue
            for key in correct_choice_ids(question):
                selected = quiz.toggle(1, state["token"], key)
                self.assertIsNotNone(selected)
            result = quiz.submit(1, state["token"])
            self.assertEqual(selected_letters(question, result["selected"]), set(question["correct"]))
            self.assertTrue(result["answered"])
            for answered in (False, True):
                view = dict(result, answered=answered)
                text, keyboard = question_view(bank, view)
                self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
                for row in keyboard.inline_keyboard:
                    for button in row:
                        self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)
        summary = store.user_summary(1)
        self.assertEqual((summary["attempts"], summary["correct"], summary["wrong"]), (103, 103, 0))
        store.close()


if __name__ == "__main__":
    unittest.main()
