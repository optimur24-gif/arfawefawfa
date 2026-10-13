"""Checks for feature migrations and scheduling through the actual bot engine."""
import asyncio
import copy
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import AGAIN, Bot, Learning, Quiz, Store
from test_bot import correct_choice_ids, fake_update, sample_bank, user


class FeatureIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.touch(user(1, "student"))
        self.store.touch(user(2, "Timur0_15"))
        self.bank = sample_bank()
        self.bot = Bot(self.store, self.bank)
        self.bot.quiz.rng = random.Random(123)

    def tearDown(self):
        self.store.close()

    def answer_current(self, uid=1):
        state = self.store.state(uid)
        for key in correct_choice_ids(self.bank[state["current"]]):
            self.bot.quiz.toggle(uid, state["token"], key)
        return self.bot.quiz.submit(uid, state["token"])

    def test_actual_bot_migrates_old_named_state_and_paused_learning_session(self):
        legacy = Store(":memory:")
        legacy.touch(user(1))
        quiz = Quiz(legacy, self.bank)
        learning = Learning(legacy, quiz)
        original, _ = learning.change(1, 2, "create")
        original.update(study_index=1, revealed=True, pending="add")
        legacy.save_learning(1, original)
        session = quiz.new(1, "learning")
        quiz.pause(1)
        bot = Bot(legacy, self.bank)
        migrated = legacy.learning(1)
        for key, value in original.items():
            self.assertEqual(migrated[key], value)
        self.assertEqual(legacy.state(1)["list_id"], migrated["list_id"])
        self.assertEqual(legacy.state(1)["pool"], session["pool"])
        self.assertEqual(legacy.state(1)["status"], "paused")
        self.assertEqual(bot.features.lists(1)[0]["count"], 2)
        legacy.close()

    def test_learning_pool_stays_attached_to_original_list_after_active_switch(self):
        first = self.bot.features.create_list(1, "Первый")
        first["ids"] = [1]
        self.store.save_learning(1, first)
        session = self.bot.quiz.new(1, "learning")
        second = self.bot.features.create_list(1, "Второй")
        second["ids"] = [2]
        self.store.save_learning(1, second)
        self.bot.learning.change(1, 1, "add")
        self.assertEqual(self.store.state(1), session)
        self.bot.learning.reset(1)
        self.assertEqual(self.store.state(1), session)
        self.bot.quiz.pause(1)
        resumed = self.bot.quiz.resume(1)
        self.assertEqual(resumed["list_id"], first["list_id"])
        self.assertEqual(resumed["pool"], [1])
        self.bot.features.switch_list(1, first["list_id"])
        self.bot.features.append_question(1, 3)
        self.assertEqual(self.store.state(1)["pool"], [1, 3])
        self.assertEqual(self.store.state(1)["seen"], [1])
        self.answer_current()
        self.assertEqual(self.bot.quiz.next(1)["current"], 3)

    def test_old_list_nonce_cannot_reset_new_active_list(self):
        first = self.bot.features.get_learning(1)
        self.bot.features.append_question(1, 1)
        first = self.store.learning(1)
        second = self.bot.features.create_list(1, "Другой")
        self.bot.features.append_question(1, 2)
        before = self.store.learning(1)
        self.assertIsNone(self.bot.learning.reset(1, first["version"]))
        self.assertIsNone(self.bot.learning.change(1, 2, "create", first["version"]))
        self.assertEqual(self.store.learning(1), before)
        self.assertEqual(self.bot.features.list_state(1, first["list_id"])["ids"], [1])
        self.assertEqual(self.store.learning(1)["list_id"], second["list_id"])

    def test_again_restarts_fixed_session_list_even_when_another_list_is_active(self):
        first = self.bot.features.create_list(1, "Первый")
        first["ids"] = [1, 3]
        self.store.save_learning(1, first)
        self.bot.quiz.new(1, "learning")
        second = self.bot.features.create_list(1, "Второй")
        second["ids"] = [2]
        self.store.save_learning(1, second)
        context = SimpleNamespace(bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()))
        asyncio.run(self.bot.action(fake_update(user(1, "student")), context, AGAIN))
        session = self.store.state(1)
        self.assertEqual(session["list_id"], first["list_id"])
        self.assertEqual(session["list_name"], first["name"])
        self.assertEqual(session["pool"], [1, 3])
        self.assertEqual(len(session["seen"]), 1)
        self.assertEqual(self.bot.features.active_list_id(1), second["list_id"])

    def test_answer_updates_progress_once_and_duplicate_submission_keeps_schedule(self):
        state = self.bot.quiz.new(1)
        qid = state["current"]
        self.answer_current()
        progress = self.bot.features.question_progress(1, qid)
        self.assertEqual(progress["stage"], 1)
        self.assertIsNone(self.bot.quiz.submit(1, state["token"]))
        self.assertEqual(self.bot.features.question_progress(1, qid), progress)
        self.assertEqual(self.store.user_summary(1)["attempts"], 1)
        self.assertIsNone(self.bot.features.question_progress(2, qid))

    def test_progress_failure_rolls_back_answer_and_saved_session(self):
        state = self.bot.quiz.new(1)
        for key in correct_choice_ids(self.bank[state["current"]]):
            self.bot.quiz.toggle(1, state["token"], key)
        before = self.store.state(1)
        original = self.bot.features.update_progress_no_commit

        def failing_hook(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("Schedule persistence failed")

        with patch.object(self.bot.features, "update_progress_no_commit", side_effect=failing_hook):
            with self.assertRaises(RuntimeError):
                self.bot.quiz.submit(1, state["token"])
        self.assertEqual(self.store.state(1), before)
        self.assertEqual(self.store.user_summary(1)["attempts"], 0)
        self.assertIsNone(self.bot.features.question_progress(1, state["current"]))

    def test_new_features_survive_real_database_close_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.sqlite3"
            store = Store(path)
            store.touch(user(1))
            bot = Bot(store, self.bank)
            state = bot.features.create_list(1, "К экзамену")
            bot.features.append_question(1, 2)
            state = bot.features.get_learning(1)
            state.update(study_index=0, revealed=True, pending="add")
            store.save_learning(1, state)
            bot.features.update_progress(1, 2, False, "2026-10-12T12:00:00+00:00")
            bot.features.toggle_reminders(1)
            bot.features.set_intake(1, "listnew", {})
            rid = bot.features.add_report(1, 2, "Замечание")
            expected = copy.deepcopy(state)
            store.close()
            reopened = Store(path)
            second = Bot(reopened, self.bank)
            self.assertEqual(reopened.learning(1), expected)
            self.assertEqual(second.features.question_progress(1, 2)["due_at"], "2026-10-13")
            self.assertTrue(second.features.reminder_settings(1)["reminders_enabled"])
            self.assertEqual(second.features.get_intake(1), {"action": "listnew", "payload": {}})
            self.assertEqual(second.features.reports()[0]["id"], rid)
            self.assertEqual(len(second.features.lists(1)), 1)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
