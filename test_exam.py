"""Offline exam checks: delayed results, atomic publication and persistent timing."""
import copy
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from answer_helpers import answer_options
from bot import EXAM, STATS, STOP, Bot, Quiz, Store, load_questions, question_view
from test_bot import FakeMessage, fake_update


def student(uid):
    return SimpleNamespace(id=uid, username=f"student_{uid}", first_name=f"User {uid}", last_name=None)


def exam_bank(count=60):
    # Every question has two correct answers and distinct wording so sample
    # uniqueness is meaningful; the duplicate is deliberately canonicalized.
    bank = {
        qid: {"id": qid, "page": 4, "question": f"Экзаменационное задание {qid}",
              "options": {"а": "Первый", "б": "Второй", "в": "Третий", "г": "Четвёртый"},
              "correct": ["а", "в"], "enabled": True}
        for qid in range(1, count + 1)
    }
    bank[count + 1] = dict(copy.deepcopy(bank[1]), id=count + 1)
    bank[count + 2] = {"id": count + 2, "page": 4, "question": "Отключённый вопрос",
                       "options": {}, "correct": [], "enabled": False}
    return bank


def correct_ids(question):
    options = answer_options(question)
    return [next(item["id"] for item in options if item["letter"] == letter)
            for letter in question["correct"]]


class ExamTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.touch(student(1))
        self.store.touch(student(2))
        self.bank = exam_bank()
        self.bot = Bot(self.store, self.bank)
        self.quiz = self.bot.quiz
        self.quiz.rng = random.Random(51)
        self.features = self.bot.features

    def tearDown(self):
        self.store.close()

    def answer(self, uid=1, keys=None):
        state = self.store.state(uid)
        if keys is None:
            keys = correct_ids(self.bank[state["current"]])
        for key in keys:
            self.quiz.toggle(uid, state["token"], key)
        return self.quiz.submit(uid, state["token"])

    def row_count(self, visible=None, uid=1):
        condition = " AND visible=?" if visible is not None else ""
        args = (uid, visible) if visible is not None else (uid,)
        return self.store.db.execute(
            "SELECT COUNT(*) FROM answers WHERE user_id=?" + condition, args).fetchone()[0]

    def expire(self, uid=1):
        state = self.store.state(uid)
        state["deadline"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        self.store.save(uid, state)
        return state

    def test_default_exam_samples_45_unique_canonical_questions(self):
        state = self.quiz.start_exam(1)
        self.assertEqual((state["mode"], state["status"], len(state["pool"])), ("exam", "running", 45))
        self.assertEqual(len(state["pool"]), len(set(state["pool"])))
        self.assertTrue(set(state["pool"]) <= set(range(1, 61)))
        deadline = datetime.fromisoformat(state["deadline"])
        self.assertIsNotNone(deadline.tzinfo)
        self.assertGreater((deadline - datetime.now(timezone.utc)).total_seconds(), 44 * 60)
        self.assertLessEqual((deadline - datetime.now(timezone.utc)).total_seconds(), 45 * 60)

    def test_short_bank_caps_exam_and_disabled_duplicates_are_not_issued(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.touch(student(10))
        quiz = Quiz(store, exam_bank(10), random.Random(2))
        state = quiz.start_exam(10, timed=False)
        self.assertCountEqual(state["pool"], range(1, 11))
        self.assertIsNone(state["deadline"])
        self.assertFalse(quiz.exam_expired(state, datetime.now(timezone.utc) + timedelta(days=365)))

    def test_real_source_bank_completes_45_without_repeated_questions(self):
        bank = load_questions()
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.touch(student(10))
        bot = Bot(store, bank)
        bot.quiz.rng = random.Random(21)
        state = bot.quiz.start_exam(10, timed=False)
        issued = []
        while state["status"] == "running":
            qid = state["current"]
            issued.append(qid)
            for key in correct_ids(bank[qid]):
                bot.quiz.toggle(10, state["token"], key)
            bot.quiz.submit(10, state["token"])
            self.assertEqual(store.user_summary(10)["attempts"], 0)
            state = bot.quiz.next(10)
        self.assertEqual((len(issued), len(set(issued))), (45, 45))
        self.assertEqual((state["correct_count"], state["wrong_count"]), (45, 0))
        self.assertEqual(store.user_summary(10)["attempts"], 45)
        self.assertEqual(len(store.exam_result(10, state["session_id"])["exam_history"]), 45)

    def test_multiple_answer_scoring_requires_exact_set(self):
        for keys, expected in [(["а"], False), (["а", "б", "в"], False), (["в", "а"], True)]:
            with self.subTest(keys=keys):
                self.quiz.start_exam(1, timed=False, count=1)
                state = self.answer(keys=keys)
                self.assertEqual(state["exam_history"][-1]["correct"], expected)
                self.assertEqual(state["correct_count"], int(expected))
                self.assertEqual(state["wrong_count"], int(not expected))
                self.assertEqual(self.row_count(0), 1)
                self.quiz.finish_exam(1)

    def test_answer_is_hidden_from_personal_admin_errors_and_progress_until_finish(self):
        state = self.quiz.start_exam(1, timed=False, count=3)
        qid = state["current"]
        self.answer(keys=["а"])
        self.assertEqual((self.row_count(0), self.row_count(1)), (1, 0))
        self.assertEqual(self.store.user_summary(1)["attempts"], 0)
        self.assertEqual(self.store.wrong_questions(1), [])
        admin = next(row for row in self.store.user_rows() if row["user_id"] == 1)
        self.assertEqual((admin["attempts"], admin["correct"], admin["wrong"]), (0, 0, 0))
        self.assertIsNone(self.features.question_progress(1, qid))
        self.assertEqual(self.features.progress(1, [qid])["new"], 1)
        self.assertIsNone(self.store.exam_result(1, state["session_id"]))
        self.quiz.finish_exam(1)
        self.assertEqual((self.row_count(0), self.row_count(1)), (0, 1))
        self.assertEqual(self.store.user_summary(1)["wrong"], 1)
        self.assertEqual(self.store.wrong_questions(1)[0]["question_id"], qid)
        self.assertEqual(self.features.question_progress(1, qid)["stage"], 0)
        self.assertEqual(self.features.progress(1, [qid])["new"], 0)

    def test_empty_double_and_stale_submissions_do_not_record_twice(self):
        state = self.quiz.start_exam(1, timed=False, count=2)
        self.assertEqual(self.quiz.submit(1, state["token"]), "empty")
        self.assertEqual(self.row_count(), 0)
        answered = self.answer()
        self.assertIsNone(self.quiz.submit(1, answered["token"]))
        self.assertIsNone(self.quiz.toggle(1, answered["token"], "б"))
        self.quiz.next(1)
        self.assertIsNone(self.quiz.submit(1, answered["token"]))
        self.assertEqual(self.row_count(), 1)
        self.assertEqual(len(self.store.state(1)["exam_history"]), 1)

    def test_last_next_publishes_and_archives_all_results(self):
        state = self.quiz.start_exam(1, timed=False, count=3)
        issued = []
        while state["status"] == "running":
            issued.append(state["current"])
            self.answer()
            self.assertEqual(self.store.user_summary(1)["attempts"], 0)
            state = self.quiz.next(1)
        self.assertEqual((self.row_count(0), self.row_count(1)), (0, 3))
        self.assertEqual(self.store.user_summary(1)["correct"], 3)
        self.assertEqual(state["finished_reason"], "completed")
        self.assertTrue(state.get("finished_at"))
        self.assertEqual(self.store.exam_result(1, state["session_id"]), state)
        for qid in issued:
            self.assertEqual(self.features.question_progress(1, qid)["stage"], 1)

    def test_finishing_twice_publishes_progress_only_once(self):
        state = self.quiz.start_exam(1, timed=False, count=2)
        self.answer()
        self.quiz.next(1)
        self.answer(keys=["а"])
        hook = self.features.update_progress_no_commit
        with patch.object(self.features, "update_progress_no_commit", wraps=hook) as recorded:
            result = self.quiz.finish_exam(1)
            before = self.features.progress(1)
            duplicate = self.quiz.finish_exam(1)
            self.assertEqual(recorded.call_count, 2)
        self.assertEqual(duplicate, result)
        self.assertEqual(self.features.progress(1), before)
        self.assertEqual(self.store.user_summary(1)["attempts"], 2)
        self.assertEqual(len(self.store.exam_result(1, state["session_id"])["exam_history"]), 2)

    def test_progress_failure_rolls_back_entire_finish_then_retry_succeeds(self):
        self.quiz.start_exam(1, timed=False, count=3)
        self.answer()
        self.quiz.next(1)
        self.answer(keys=["а"])
        before = self.store.state(1)
        hook = self.features.update_progress_no_commit
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            result = hook(*args, **kwargs)
            calls += 1
            if calls == 2:
                raise RuntimeError("Simulated second progress-write failure")
            return result

        with patch.object(self.features, "update_progress_no_commit", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "second progress-write"):
                self.quiz.finish_exam(1)
        self.assertEqual(self.store.state(1), before)
        self.assertEqual((self.row_count(0), self.row_count(1)), (2, 0))
        self.assertEqual(self.store.user_summary(1)["attempts"], 0)
        self.assertIsNone(self.store.exam_result(1, before["session_id"]))
        for entry in before["exam_history"]:
            self.assertIsNone(self.features.question_progress(1, entry["qid"]))
        finished = self.quiz.finish_exam(1)
        self.assertEqual(finished["status"], "completed")
        self.assertEqual((self.row_count(0), self.row_count(1)), (0, 2))

    def test_results_are_personal_and_survive_starting_another_session(self):
        first = self.quiz.start_exam(1, timed=False, count=2)
        self.answer()
        saved = self.quiz.finish_exam(1)
        second = self.quiz.start_exam(2, timed=False, count=2)
        self.answer(2, ["а"])
        self.quiz.finish_exam(2)
        self.assertIsNone(self.store.exam_result(2, first["session_id"]))
        self.assertIsNone(self.store.exam_result(1, second["session_id"]))
        self.assertEqual(self.store.exam_result(1, first["session_id"]), saved)
        self.quiz.new(1)
        self.assertEqual(self.store.exam_result(1, first["session_id"]), saved)
        self.assertEqual(self.store.user_summary(1)["correct"], 1)
        self.assertEqual(self.store.user_summary(2)["wrong"], 1)

    def test_restarting_archives_unfinished_exam_and_publishes_submitted_answers(self):
        first = self.quiz.start_exam(1, timed=False, count=3)
        self.answer(keys=["а"])
        state = self.quiz.new(1)
        archived = self.store.exam_result(1, first["session_id"])
        self.assertEqual((archived["status"], archived["finished_reason"]), ("completed", "restarted"))
        self.assertEqual(len(archived["exam_history"]), 1)
        self.assertEqual((state["mode"], self.store.user_summary(1)["wrong"]), ("all", 1))
        self.assertEqual(self.row_count(0), 0)

    def test_reset_archives_exam_and_keeps_history(self):
        first = self.quiz.start_exam(1, timed=False, count=3)
        self.answer()
        self.quiz.reset(1)
        self.assertIsNone(self.store.state(1))
        archived = self.store.exam_result(1, first["session_id"])
        self.assertEqual(archived["finished_reason"], "cancelled")
        self.assertEqual(self.store.user_summary(1)["correct"], 1)
        self.assertEqual(self.row_count(0), 0)

    def test_expiry_boundary_is_inclusive_and_only_applies_to_open_timed_exam(self):
        state = self.quiz.start_exam(1)
        deadline = datetime.fromisoformat(state["deadline"])
        self.assertFalse(self.quiz.exam_expired(state, deadline - timedelta(microseconds=1)))
        self.assertTrue(self.quiz.exam_expired(state, deadline))
        self.assertFalse(self.quiz.exam_expired(dict(state, status="completed"), deadline))
        self.assertFalse(self.quiz.exam_expired(dict(state, mode="all"), deadline))
        self.assertFalse(self.quiz.exam_expired(dict(state, deadline=None), deadline))
        self.assertFalse(self.quiz.exam_expired(None, deadline))

    def test_expired_exam_cannot_change_or_submit_selection(self):
        state = self.quiz.start_exam(1, count=3)
        self.quiz.toggle(1, state["token"], "а")
        expired = self.expire()
        self.assertIsNone(self.quiz.toggle(1, expired["token"], "в"))
        self.assertEqual(self.store.state(1)["selected"], ["а"])
        self.assertEqual(self.quiz.submit(1, expired["token"]), "expired")
        finished = self.store.state(1)
        self.assertEqual((finished["status"], finished["finished_reason"]), ("completed", "timeout"))
        self.assertEqual(self.row_count(), 0)
        self.assertIsNone(self.quiz.submit(1, expired["token"]))

    def test_next_after_timeout_finishes_and_publishes_prior_answer(self):
        state = self.quiz.start_exam(1, count=3)
        self.answer()
        self.expire()
        finished = self.quiz.next(1)
        self.assertEqual(finished["finished_reason"], "timeout")
        self.assertEqual(self.store.user_summary(1)["correct"], 1)
        self.assertEqual(len(self.store.exam_result(1, state["session_id"])["exam_history"]), 1)

    def test_pause_resume_keeps_deadline_selection_and_invalidates_old_buttons(self):
        state = self.quiz.start_exam(1)
        self.quiz.toggle(1, state["token"], "а")
        paused = self.quiz.pause(1)
        self.assertEqual((paused["status"], paused["deadline"]), ("paused", state["deadline"]))
        self.assertIsNone(self.quiz.submit(1, paused["token"]))
        resumed = self.quiz.resume(1)
        self.assertEqual((resumed["deadline"], resumed["current"], resumed["selected"]),
                         (state["deadline"], state["current"], ["а"]))
        self.assertNotEqual(resumed["token"], state["token"])
        self.assertIsNone(self.quiz.toggle(1, state["token"], "в"))
        self.quiz.pause(1)
        expired = self.expire()
        finished = self.quiz.resume(1)
        self.assertEqual((finished["status"], finished["finished_reason"], finished["deadline"]),
                         ("completed", "timeout", expired["deadline"]))

    def test_hidden_answers_do_not_seed_progress_when_features_are_first_attached(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.touch(student(10))
        quiz = Quiz(store, self.bank)
        state = quiz.start_exam(10, timed=False, count=3)
        for key in correct_ids(self.bank[state["current"]]):
            quiz.toggle(10, state["token"], key)
        quiz.submit(10, state["token"])
        # Simulates an upgrade whose first FeatureStore migration sees an
        # unfinished exam; its hidden answers must not reveal correctness.
        bot = Bot(store, self.bank)
        self.assertIsNone(bot.features.question_progress(10, state["current"]))
        self.assertEqual(store.user_summary(10)["attempts"], 0)
        bot.quiz.finish_exam(10)
        self.assertEqual(bot.features.question_progress(10, state["current"])["stage"], 1)

    def test_hidden_exam_answer_and_clock_survive_database_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exam.sqlite3"
            first = Store(path)
            first.touch(student(10))
            bot = Bot(first, self.bank)
            state = bot.quiz.start_exam(10, count=3)
            qid = state["current"]
            for key in correct_ids(self.bank[qid]):
                bot.quiz.toggle(10, state["token"], key)
            bot.quiz.submit(10, state["token"])
            saved = bot.quiz.pause(10)
            first.close()
            reopened = Store(path)
            try:
                second = Bot(reopened, self.bank)
                self.assertEqual(reopened.state(10), saved)
                self.assertIsNone(second.features.question_progress(10, qid))
                self.assertEqual(reopened.user_summary(10)["attempts"], 0)
                resumed = second.quiz.resume(10)
                self.assertEqual(resumed["deadline"], saved["deadline"])
                self.assertEqual(resumed["exam_history"], saved["exam_history"])
                finished = second.quiz.finish_exam(10)
                expected = copy.deepcopy(finished)
            finally:
                reopened.close()
            archived_store = Store(path)
            try:
                third = Bot(archived_store, self.bank)
                self.assertEqual(archived_store.exam_result(10, state["session_id"]), expected)
                self.assertEqual(third.features.question_progress(10, qid)["stage"], 1)
                self.assertEqual(archived_store.user_summary(10)["attempts"], 1)
            finally:
                archived_store.close()

    def test_question_view_never_reveals_verdict_key_breakdown_or_score_in_exam(self):
        for keys in (["а"], ["а", "в"]):
            with self.subTest(keys=keys):
                self.quiz.start_exam(1, timed=False, count=2)
                state = self.answer(keys=keys)
                state["progress_label"] = "Выучен"
                text, keyboard = question_view(self.bank, state)
                self.assertIn("Ответ сохранён", text)
                self.assertIn("Ваш выбор:", text)
                for forbidden in ("✅ Правильно!", "❌ Неправильно.", "Выбраны правильно:",
                                  "Пропущены:", "Лишние:", "Ответ по ключу PDF:",
                                  "В сессии:", "Прогресс:"):
                    self.assertNotIn(forbidden, text)
                callbacks = [item.callback_data for row in keyboard.inline_keyboard for item in row]
                self.assertTrue(any(value.startswith("next:") for value in callbacks))
                self.assertFalse(any(value.startswith("check:") for value in callbacks))
                self.quiz.finish_exam(1)


class ExamUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.store.touch(student(1))
        self.store.touch(student(2))
        self.bank = exam_bank()
        self.bot = Bot(self.store, self.bank)
        self.bot.quiz.rng = random.Random(9)
        self.context = SimpleNamespace(bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()))

    async def asyncTearDown(self):
        self.store.close()

    @staticmethod
    def query(data, message_id=1):
        return SimpleNamespace(data=data, message=FakeMessage(message_id),
                               answer=AsyncMock(), edit_message_text=AsyncMock())

    def answer(self, keys=None):
        state = self.store.state(1)
        keys = keys if keys is not None else correct_ids(self.bank[state["current"]])
        for key in keys:
            self.bot.quiz.toggle(1, state["token"], key)
        return self.bot.quiz.submit(1, state["token"])

    def partial_result(self):
        self.bot.quiz.start_exam(1, timed=False, count=4)
        self.answer()
        self.bot.quiz.next(1)
        self.answer(["а"])
        return self.bot.quiz.finish_exam(1, "manual")

    async def test_setup_nonce_belongs_to_user_and_cannot_be_reused(self):
        update = fake_update(student(1))
        await self.bot.action(update, self.context, EXAM)
        intake = self.bot.features.get_intake(1)
        nonce = intake["payload"]["version"]
        parts = ["exam_start", nonce, "30", "0"]
        forged = self.query(":".join(parts))
        await self.bot.exam_callback(fake_update(student(2), query=forged), self.context, parts)
        self.assertIn("устарела", forged.answer.call_args.args[0])
        self.assertIsNone(self.store.state(2))
        query = self.query(":".join(parts))
        await self.bot.exam_callback(fake_update(student(1), query=query), self.context, parts)
        state = self.store.state(1)
        self.assertEqual((len(state["pool"]), state["timed"], state["deadline"]), (30, False, None))
        self.assertIsNone(self.bot.features.get_intake(1))
        repeat = self.query(":".join(parts))
        await self.bot.exam_callback(fake_update(student(1), query=repeat), self.context, parts)
        self.assertIn("устарела", repeat.answer.call_args.args[0])
        self.assertEqual(self.store.state(1), state)

    async def test_invalid_setup_modes_leave_current_session_and_intake_untouched(self):
        self.bot.quiz.new(1)
        state = self.store.state(1)
        update = fake_update(student(1))
        await self.bot.exam_settings(update, self.context)
        intake = self.bot.features.get_intake(1)
        nonce = intake["payload"]["version"]
        for count, minutes in (("300", "45"), ("45", "1"), ("nan", "45")):
            query = self.query(f"exam_start:{nonce}:{count}:{minutes}")
            await self.bot.exam_callback(fake_update(student(1), query=query), self.context,
                                         ["exam_start", nonce, count, minutes])
            self.assertIn("Некорректный", query.answer.call_args.args[0])
            self.assertEqual(self.store.state(1), state)
            self.assertEqual(self.bot.features.get_intake(1), intake)

    async def test_open_exam_cannot_be_reviewed_or_saved_as_completed_result(self):
        state = self.bot.quiz.start_exam(1, timed=False, count=4)
        self.answer(["а"])
        before = self.store.state(1)
        for action in ("exam_review", "exam_result", "exam_add"):
            query = self.query(f"{action}:{state['session_id']}:0")
            update = fake_update(student(1), query=query)
            await self.bot.exam_callback(update, self.context, [action, state["session_id"], "0"])
            self.assertIn("недоступен", query.answer.call_args.args[0])
            query.edit_message_text.assert_not_awaited()
            self.assertEqual(update.effective_message.replies, [])
        self.assertEqual(self.store.state(1), before)
        self.assertEqual(self.store.learning(1)["ids"], [])

    async def test_other_user_cannot_read_or_add_questions_from_archived_exam(self):
        result = self.partial_result()
        before = self.store.state(1)
        for action in ("exam_review", "exam_result", "exam_add"):
            query = self.query(f"{action}:{result['session_id']}:0")
            update = fake_update(student(2), query=query)
            await self.bot.exam_callback(update, self.context, [action, result["session_id"], "0"])
            self.assertIn("недоступен", query.answer.call_args.args[0])
            query.edit_message_text.assert_not_awaited()
            self.assertEqual(update.effective_message.replies, [])
        self.assertEqual(self.store.state(1), before)
        self.assertEqual(self.store.learning(2)["ids"], [])

    async def test_summary_scores_unanswered_questions_and_provides_owner_archive_buttons(self):
        result = self.partial_result()
        text, keyboard = self.bot.exam_summary_view(result)
        self.assertIn("Всего заданий: 4", text)
        self.assertIn("Отвечено: 2", text)
        self.assertIn("✅ Правильно: 1", text)
        self.assertIn("❌ Неправильно: 1", text)
        self.assertIn("Без ответа: 2", text)
        self.assertIn("Результат: 25.0%", text)
        callbacks = [item.callback_data for row in keyboard.inline_keyboard for item in row]
        self.assertIn(f"exam_review:{result['session_id']}:0", callbacks)
        self.assertIn(f"exam_add:{result['session_id']}", callbacks)

    async def test_archive_review_after_new_session_does_not_replace_current_question(self):
        result = self.partial_result()
        current = self.bot.quiz.new(1)
        query = self.query(f"exam_review:{result['session_id']}:0")
        await self.bot.exam_callback(fake_update(student(1), query=query), self.context,
                                     ["exam_review", result["session_id"], "0"])
        text = query.edit_message_text.call_args.kwargs["text"]
        self.assertIn("Разбор экзамена", text)
        self.assertIn("Ответ по ключу PDF:", text)
        self.assertIn("Пропущены:", text)
        self.assertEqual(self.store.state(1), current)

    async def test_save_errors_adds_wrong_and_unanswered_only_to_active_personal_list(self):
        result = self.partial_result()
        correctly_answered = {item["qid"] for item in result["exam_history"] if item["correct"]}
        expected = set(result["pool"]) - correctly_answered
        self.bot.features.create_list(1, "Экзамен")
        self.bot.features.create_list(2, "Другой пользователь")
        current = self.bot.quiz.new(1)
        query = self.query(f"exam_add:{result['session_id']}")
        update = fake_update(student(1), query=query)
        await self.bot.exam_callback(update, self.context, ["exam_add", result["session_id"]])
        self.assertEqual(set(self.store.learning(1)["ids"]), expected)
        self.assertEqual(self.store.learning(2)["ids"], [])
        self.assertEqual(self.store.state(1), current)
        self.assertIn("Добавлено 3", update.effective_message.replies[-1][0])
        repeat = fake_update(student(1), query=self.query(query.data))
        await self.bot.exam_callback(repeat, self.context, ["exam_add", result["session_id"]])
        self.assertIn("Добавлено 0", repeat.effective_message.replies[-1][0])
        self.assertEqual(set(self.store.learning(1)["ids"]), expected)

    async def test_stop_and_stats_do_not_leak_exam_score_before_completion(self):
        self.bot.quiz.start_exam(1, count=4)
        self.answer(["а"])
        update = fake_update(student(1))
        await self.bot.action(update, self.context, STOP)
        pause_text = update.effective_message.replies[-1][0]
        self.assertIn("Таймер продолжает идти", pause_text)
        self.assertNotIn("В сессии:", pause_text)
        await self.bot.action(update, self.context, STATS)
        text = update.effective_message.replies[-1][0]
        self.assertIn("Ответов: 0", text)
        self.assertIn("❌ Ошибок: 0", text)
        self.assertNotIn("В сессии:", text)

    async def test_admin_answer_export_hides_exam_rows_until_completion(self):
        self.bot.quiz.start_exam(1, timed=False, count=4)
        self.answer(["а"])
        update = fake_update(student(2))
        await self.bot.admin_export(update, self.context, 1)
        before = update.effective_message.documents[-1][1].decode("utf-8-sig")
        self.assertEqual(len(before.splitlines()), 1)
        self.bot.quiz.finish_exam(1)
        await self.bot.admin_export(update, self.context, 1)
        after = update.effective_message.documents[-1][1].decode("utf-8-sig")
        self.assertEqual(len(after.splitlines()), 2)

    async def test_dispatcher_routes_exam_start_review_and_finish_callbacks(self):
        setup = fake_update(student(1))
        await self.bot.action(setup, self.context, EXAM)
        nonce = self.bot.features.get_intake(1)["payload"]["version"]
        query = self.query(f"exam_start:{nonce}:30:0")
        await self.bot.callback(fake_update(student(1), query=query), self.context)
        state = self.store.state(1)
        self.assertIsNotNone(state)
        self.assertEqual(state["mode"], "exam")
        self.answer(["а"])
        finish = self.query(f"finish_exam:{state['token']}", state["message_id"])
        update = fake_update(student(1), query=finish)
        await self.bot.callback(update, self.context)
        completed = self.store.state(1)
        self.assertEqual((completed["status"], completed["finished_reason"]), ("completed", "manual"))
        self.assertIn("Результат:", update.effective_message.replies[-1][0])
        review = self.query(f"exam_review:{state['session_id']}:0")
        await self.bot.callback(fake_update(student(1), query=review), self.context)
        self.assertIn("Разбор экзамена", review.edit_message_text.call_args.kwargs["text"])

    async def test_check_callback_after_timeout_finishes_without_type_error_or_new_answer(self):
        state = self.bot.quiz.start_exam(1, count=4)
        for key in ("а", "в"):
            self.bot.quiz.toggle(1, state["token"], key)
        state = self.store.state(1)
        state.update(deadline=(datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat(), message_id=12)
        self.store.save(1, state)
        query = self.query(f"check:{state['token']}", 12)
        update = fake_update(student(1), query=query)
        await self.bot.callback(update, self.context)
        self.assertEqual(self.store.state(1)["finished_reason"], "timeout")
        self.assertEqual(self.store.user_summary(1)["attempts"], 0)
        self.assertIn("Время вышло", update.effective_message.replies[-1][0])


if __name__ == "__main__":
    unittest.main()
