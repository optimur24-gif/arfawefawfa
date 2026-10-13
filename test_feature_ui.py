"""Behavior checks for private list ownership and user interface callbacks."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from feature_ui import FeatureUI, REMINDERS


class Harness(FeatureUI):
    def __init__(self):
        self.features = MagicMock()
        self.features.get_intake.return_value = None
        self.features.due_ids.return_value = []
        self.features.get_learning.return_value = {
            "ids": [], "name": "Основной", "list_id": 1}
        self.features.lists.return_value = [
            {"id": 1, "name": "Основной", "count": 0, "active": True}]
        self.features.active_list_id.return_value = 1
        self.features.reminder_settings.return_value = {
            "reminders_enabled": False, "reminder_hour_utc": 9}
        self.bank = {
            1: {"id": 1, "question": "Один вопрос", "topic": "Кости"},
            2: {"id": 2, "question": "Другой вопрос", "topic": "Мышцы"},
            3: {"id": 3, "question": "Один вопрос", "topic": "Кости"},
        }
        self.quiz = SimpleNamespace(ids=[1, 2], canonical={1: 1, 2: 2, 3: 1}, pause=MagicMock())
        self.learning = MagicMock()
        self.store = MagicMock()
        self.store.state.return_value = None
        self.say = AsyncMock()
        self.panel = AsyncMock()
        self.start_pool = AsyncMock()
        self.learning_home = AsyncMock()
        self.learning_prompt = AsyncMock()
        self.remove_old_buttons = AsyncMock()
        self.menu = MagicMock(return_value="menu")
        self.authorized_admin = MagicMock(return_value=False)


def update(uid=7):
    query = SimpleNamespace(answer=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                           effective_chat=SimpleNamespace(id=uid),
                           callback_query=query)


class FeatureUIBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ui = Harness()
        self.update = update()
        self.context = SimpleNamespace()

    async def callback(self, *parts):
        await self.ui.feature_callback(self.update, self.context, list(parts))
        self.update.callback_query.answer.assert_awaited_once()

    async def test_non_admin_cannot_read_or_resolve_reports(self):
        for action in ("feat_reports", "feat_resolve", "feat_report_full"):
            with self.subTest(action=action):
                item = update()
                await self.ui.feature_callback(item, self.context, [action, "1", "0"])
                item.callback_query.answer.assert_awaited_once_with("Только для администратора.", show_alert=True)
        self.ui.features.reports.assert_not_called()
        self.ui.features.resolve_report.assert_not_called()

    async def test_other_users_list_id_cannot_be_switched_or_deleted(self):
        self.ui.features.list_state.return_value = None
        for action in ("feat_switch", "feat_rename", "feat_delete", "feat_delete_yes"):
            await self.ui.feature_callback(update(), self.context, [action, "999"])
        self.ui.features.list_state.assert_called_with(7, 999)
        self.ui.features.switch_list.assert_not_called()
        self.ui.features.delete_list.assert_not_called()
        self.ui.features.rename_list.assert_not_called()

    async def test_star_adds_canonical_question_to_own_list_and_preserves_quiz(self):
        self.ui.features.append_question.return_value = ({"ids": [1], "name": "Кости"}, True)
        await self.callback("feat_star", "3")
        self.ui.features.append_question.assert_called_once_with(7, 1)
        self.ui.panel.assert_not_awaited()
        self.ui.say.assert_not_awaited()
        self.ui.store.save.assert_not_called()

    async def test_unknown_question_star_is_rejected_without_mutation(self):
        await self.callback("feat_star", "999")
        self.ui.features.append_question.assert_not_called()
        self.assertTrue(self.update.callback_query.answer.call_args.kwargs["show_alert"])

    async def test_due_start_filters_duplicates_and_missing_questions(self):
        self.ui.features.due_ids.return_value = [1, 3, 999, 2]
        await self.callback("feat_due_start")
        self.ui.start_pool.assert_awaited_once_with(self.update, self.context, "due", [1, 2])

    async def test_named_list_creation_then_prompts_for_random_question_count(self):
        self.ui.features.get_intake.return_value = {"action": "listnew", "payload": {}}
        handled = await self.ui.feature_text(self.update, self.context, "  К экзамену  ")
        self.assertTrue(handled)
        self.ui.features.create_list.assert_called_once_with(7, "К экзамену")
        self.ui.features.clear_intake.assert_called_once_with(7)
        self.ui.learning_prompt.assert_awaited_once_with(self.update, self.context, "create")

    async def test_invalid_name_keeps_prompt_and_does_not_create_list(self):
        self.ui.features.get_intake.return_value = {"action": "listnew", "payload": {}}
        self.assertTrue(await self.ui.feature_text(self.update, self.context, "x" * 61))
        self.ui.features.create_list.assert_not_called()
        self.ui.features.clear_intake.assert_not_called()

    async def test_report_prompt_does_not_replace_question_and_saves_user_identity(self):
        await self.callback("feat_report", "1")
        self.ui.panel.assert_not_awaited()
        self.ui.features.set_intake.assert_called_once_with(7, "report", {"qid": 1})
        self.ui.features.get_intake.return_value = {"action": "report", "payload": {"qid": 1}}
        self.assertTrue(await self.ui.feature_text(self.update, self.context, "В PDF другой ключ"))
        self.ui.features.add_report.assert_called_once_with(7, 1, "В PDF другой ключ")

    async def test_long_report_keeps_prompt_without_storing(self):
        self.ui.features.get_intake.return_value = {"action": "report", "payload": {"qid": 1}}
        self.assertTrue(await self.ui.feature_text(self.update, self.context, "x" * 2001))
        self.ui.features.add_report.assert_not_called()
        self.ui.features.clear_intake.assert_not_called()

    async def test_topic_index_out_of_range_acknowledged_without_starting_quiz(self):
        await self.callback("feat_topic", "-1")
        self.ui.start_pool.assert_not_awaited()
        self.ui.say.assert_awaited_once()

    async def test_viewing_reminder_panel_does_not_enable_messages(self):
        self.assertTrue(await self.ui.feature_action(self.update, self.context, REMINDERS))
        self.ui.features.toggle_reminders.assert_not_called()
        text = self.ui.panel.call_args.args[1]
        self.assertIn("выключены", text)
        self.assertIn("09:00 UTC", text)

    async def test_deletion_requires_confirmation_button(self):
        self.ui.features.list_state.return_value = {"ids": [1, 2], "name": "Кости"}
        await self.callback("feat_delete", "1")
        self.ui.features.delete_list.assert_not_called()
        keyboard = self.ui.panel.call_args.args[2]
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "feat_delete_yes:1")


if __name__ == "__main__":
    unittest.main()
