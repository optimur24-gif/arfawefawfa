"""Integration checks for the real Telegram routing and SQLite feature state."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from bot import Bot, Store, RESUME, START, load_questions


def person(uid, username=None):
    return SimpleNamespace(id=uid, username=username or f"student_{uid}",
                           first_name=f"Student {uid}", last_name=None)


class Message:
    serial = 1000

    def __init__(self, text="", message_id=None):
        self.text = text
        self.message_id = message_id or 1
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))
        Message.serial += 1
        return SimpleNamespace(message_id=Message.serial)


def update(uid=1, text="", data=None, message_id=None):
    message = Message(text, message_id)
    query = (SimpleNamespace(data=data, message=message, answer=AsyncMock(),
                             edit_message_text=AsyncMock()) if data is not None else None)
    return SimpleNamespace(effective_user=person(uid), effective_message=message,
                           effective_chat=SimpleNamespace(type="private", id=uid),
                           callback_query=query)


def bank():
    return {qid: {"id": qid, "page": 4 + qid // 10,
                  "question": f"Задание {qid}", "topic": "Кости" if qid <= 35 else "Мышцы",
                  "options": {"а": "Да", "б": "Нет"}, "correct": ["а"]}
            for qid in range(1, 81)}


class FeatureRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        for uid in (1, 2, 9):
            self.store.touch(person(uid))
        self.bot = Bot(self.store, bank(), admin_id=9)
        self.context = SimpleNamespace(bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()))

    async def asyncTearDown(self):
        self.store.close()

    async def call(self, data, uid=1, message_id=None):
        item = update(uid, data=data, message_id=message_id)
        await self.bot.callback(item, self.context)
        item.callback_query.answer.assert_awaited_once()
        return item

    async def test_star_report_routes_are_personal_and_admin_view_is_protected(self):
        await self.call("feat_star:17")
        await self.call("feat_star:17")
        self.assertEqual(self.bot.features.get_learning(1)["ids"], [17])
        self.assertEqual(self.bot.features.get_learning(2)["ids"], [])
        keys_before = copy.deepcopy(self.bot.bank[17]["correct"])
        await self.call("feat_report:17")
        await self.bot.message(update(1, text="Вопрос вызывает сомнение"), self.context)
        report = self.bot.features.reports()[0]
        self.assertEqual((report["user_id"], report["question_id"]), (1, 17))
        denied = await self.call(f"feat_resolve:{report['id']}:0", uid=2)
        self.assertTrue(denied.callback_query.answer.call_args.kwargs["show_alert"])
        self.assertEqual(self.bot.features.report_count(), 1)
        admin = await self.call("feat_reports:0", uid=9)
        shown = admin.callback_query.edit_message_text.call_args.kwargs["text"]
        self.assertIn("@student_1", shown)
        self.assertIn("ID 1", shown)
        await self.call(f"feat_resolve:{report['id']}:0", uid=9)
        self.assertEqual(self.bot.features.report_count(), 0)
        self.assertEqual(self.bot.bank[17]["correct"], keys_before)

    async def test_named_list_name_then_count_builds_and_extends_distinct_questions(self):
        await self.call("feat_newlist")
        await self.bot.message(update(1, text="К экзамену"), self.context)
        state = self.bot.features.get_learning(1)
        self.assertEqual((state["name"], state["pending"]), ("К экзамену", "create"))
        await self.bot.message(update(1, text="30"), self.context)
        state = self.bot.features.get_learning(1)
        self.assertEqual(len(state["ids"]), 30)
        self.assertIsNone(self.bot.features.get_intake(1))
        await self.call(f"learn_count:{state['version']}:add:30")
        state = self.bot.features.get_learning(1)
        self.assertEqual(len(state["ids"]), 60)
        self.assertEqual(len(set(state["ids"])), 60)
        self.assertEqual(self.bot.features.get_learning(2)["ids"], [])

    async def test_topic_start_replaces_pool_and_rejects_previous_question_nonce(self):
        await self.bot.action(update(), self.context, START)
        previous = self.store.state(1)
        # Topic indices follow the visible alphabetic ordering: Кости, Мышцы.
        await self.call("feat_topic:0")
        current = self.store.state(1)
        self.assertEqual(current["mode"], "topic")
        self.assertEqual(len(current["pool"]), 35)
        self.assertTrue(all(self.bot.bank[qid]["topic"] == "Кости" for qid in current["pool"]))
        await self.call(f"pick:{previous['token']}:а", message_id=previous["message_id"])
        self.assertEqual(self.store.state(1)["selected"], [])
        self.assertEqual(self.store.state(1)["session_id"], current["session_id"])

    async def test_switch_pauses_old_list_quiz_even_when_telegram_cannot_remove_buttons(self):
        first, _ = self.bot.features.append_question(1, 1)
        state = self.bot.quiz.new(1, "learning")
        state["message_id"] = 500
        self.store.save(1, state)
        second = self.bot.features.create_list(1, "Новый список")
        self.bot.features.append_question(1, 2)
        self.bot.features.switch_list(1, first["list_id"])
        self.context.bot.edit_message_reply_markup.side_effect = BadRequest("message to edit not found")
        await self.call(f"feat_switch:{second['list_id']}")
        self.assertEqual(self.store.state(1)["status"], "paused")
        await self.call(f"pick:{state['token']}:а", message_id=500)
        self.assertEqual(self.store.state(1)["selected"], [])
        await self.bot.action(update(), self.context, RESUME)
        resumed = self.store.state(1)
        self.assertEqual((resumed["list_id"], resumed["pool"]), (first["list_id"], [1]))
        self.assertEqual(self.bot.features.active_list_id(1), second["list_id"])
        self.assertNotEqual(resumed["token"], state["token"])

    async def test_learning_prompt_replaces_pending_report_before_numeric_text(self):
        state, _ = self.bot.features.append_question(1, 17)
        await self.call("feat_report:17")
        await self.call(f"learn_prompt:{state['version']}:add")
        await self.bot.message(update(1, text="30"), self.context)
        self.assertEqual(len(self.bot.features.get_learning(1)["ids"]), 31)
        self.assertEqual(self.bot.features.report_count(), 0)
        self.assertIsNone(self.bot.features.get_intake(1))

    async def test_full_report_requires_admin_and_keeps_entire_long_remark(self):
        text = "Проверить это замечание полностью. " + "д" * 1965
        self.assertLessEqual(len(text), 2000)
        rid = self.bot.features.add_report(1, 17, text)
        denied = await self.call(f"feat_report_full:{rid}:0", uid=2)
        self.assertTrue(denied.callback_query.answer.call_args.kwargs["show_alert"])
        denied.callback_query.edit_message_text.assert_not_awaited()
        self.assertEqual(denied.effective_message.replies, [])
        admin = await self.call(f"feat_report_full:{rid}:0", uid=9)
        shown = admin.callback_query.edit_message_text.call_args.kwargs["text"]
        self.assertIn(text, shown)
        self.assertIn("@student_1", shown)
        keyboard = admin.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[-1][0].callback_data, "feat_reports:0")
        self.assertEqual(self.bot.features.report_count(), 1)

    async def test_restored_source_question_card_preserves_missing_key_option(self):
        question = dict(load_questions()[1], id=380, correct=['г'], source_key='г',
                        source_options=[{'label': key, 'text': text} for key, text in
                                        [('а', 'Первый'), ('б', 'Второй'), ('в', 'Третий')]],
                        options={'а': 'Первый', 'б': 'Второй', 'в': 'Третий'})
        self.assertTrue(question.get("source_faithful"))
        self.assertEqual(question["correct"], ["г"])
        source = question["source_options"]
        self.assertEqual([option["label"] for option in source], ["а", "б", "в"])
        bot = Bot(self.store, {380: question}, admin_id=9)
        state, _ = bot.features.append_question(1, 380)
        item = update(data=f"learn_card:{state['version']}:0:1")
        await bot.callback(item, self.context)
        item.callback_query.answer.assert_awaited_once()
        text = item.callback_query.edit_message_text.call_args.kwargs["text"]
        for option in source:
            self.assertIn(f"{option['label']}) {option['text']}", text)
        self.assertIn("г) В PDF текст варианта отсутствует", text)
        self.assertEqual(self.store.user_summary(1)["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
