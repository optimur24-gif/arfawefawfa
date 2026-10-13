"""Personal learning tools and their Telegram interface.

FeatureUI is mixed into Bot.  All state is scoped to the effective Telegram user
and saved by FeatureStore; callback payloads never contain a user ID to switch to.
"""
from __future__ import annotations

import io

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


DUE = "📅 Повторить сегодня"
PROGRESS = "✅ Прогресс"
TOPICS = "🦴 Темы"
LISTS = "📂 Мои списки"
REMINDERS = "🔔 Напоминания"
REPORTS = "⚠️ Замечания пользователей"
FEATURE_ACTIONS = (DUE, PROGRESS, TOPICS, LISTS, REMINDERS, REPORTS)
FEATURE_PAGE_SIZE = 5


def _button(label, data):
    return InlineKeyboardButton(label, callback_data=data)


def _clip(value, limit):
    value = str(value or "")
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _label(question):
    number = question.get("source_number", question["id"])
    return f"№{number}" if number is not None else "Без номера"


def _name(text):
    """Keep list names readable, plain, and small enough for Telegram buttons."""
    name = " ".join(str(text or "").split())
    if not 1 <= len(name) <= 60 or any(ord(char) < 32 for char in name):
        raise ValueError("Название должно содержать от 1 до 60 символов.")
    return name


class FeatureUI:
    """Methods require Bot.features, quiz, bank, store and its message helpers."""

    def _feature_cancel_input(self, uid):
        self.features.clear_intake(uid)
        self.learning.cancel_prompt(uid)

    def _feature_topics(self):
        topics = {}
        for qid in self.quiz.ids:
            topic = self.bank[qid].get("topic") or "Прочие вопросы"
            topics.setdefault(topic, []).append(qid)
        return [(topic, topics[topic]) for topic in sorted(topics)]

    def _feature_canonical_ids(self, ids):
        return list(dict.fromkeys(
            self.quiz.canonical[qid] for qid in ids if qid in self.quiz.canonical))

    async def _feature_remove_learning_buttons(self, update, context):
        state = self.store.state(update.effective_user.id)
        if state and state.get("mode") == "learning":
            await self.remove_old_buttons(context, update.effective_chat.id, state)
            # Telegram may refuse an old markup edit.  Pause also invalidates
            # the nonce in SQLite while retaining this list's test for Resume.
            self.quiz.pause(update.effective_user.id)

    async def feature_action(self, update, context, action):
        """Return whether a reply-keyboard action belongs to this interface."""
        if action not in FEATURE_ACTIONS:
            return False
        self._feature_cancel_input(update.effective_user.id)
        if action == DUE:
            await self.feature_due(update, context)
        elif action == PROGRESS:
            await self.feature_progress(update, context)
        elif action == TOPICS:
            await self.feature_topics(update, context)
        elif action == LISTS:
            await self.feature_lists(update, context)
        elif action == REMINDERS:
            await self.feature_reminders(update, context)
        elif action == REPORTS:
            if not self.authorized_admin(update.effective_user):
                await self.say(update, "Замечания доступны только администратору.")
            else:
                await self.feature_reports(update, context)
        return True

    async def feature_lists(self, update, context, page=0, edit=False, notice=""):
        uid = update.effective_user.id
        rows = self.features.lists(uid)
        total_pages = max(1, (len(rows) + FEATURE_PAGE_SIZE - 1) // FEATURE_PAGE_SIZE)
        page = max(0, min(int(page), total_pages - 1))
        lines = [f"📂 Ваши списки · {page + 1}/{total_pages}",
                 "Каждый список хранится отдельно. ⭐ добавляет вопрос в активный список.", ""]
        buttons = []
        for row in rows[page * FEATURE_PAGE_SIZE:(page + 1) * FEATURE_PAGE_SIZE]:
            lid = row["id"]
            marker = "✅ Активный" if row["active"] else ""
            lines.append(f"{row['name']} · заданий: {row['count']} {marker}".strip())
            buttons.append([_button(
                f"{'✅' if row['active'] else '📂'} {_clip(row['name'], 40)} · {row['count']}",
                f"feat_switch:{lid}")])
            buttons.append([_button("✏️ Название", f"feat_rename:{lid}"),
                            _button("🗑 Удалить", f"feat_delete:{lid}")])
        if not rows:
            lines.append("Пока нет списков. Создайте первый.")
        nav = []
        if page:
            nav.append(_button("⬅️", f"feat_lists:{page - 1}"))
        if page + 1 < total_pages:
            nav.append(_button("➡️", f"feat_lists:{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons += [[_button("➕ Новый список", "feat_newlist")],
                    [_button("🧠 Заучивание активного списка", "feat_learning")],
                    [_button("✅ Прогресс", "feat_progress"), _button("⬅️ В меню", "feat_menu")]]
        if notice:
            lines = [notice, ""] + lines
        await self.panel(update, "\n".join(lines), InlineKeyboardMarkup(buttons), edit)

    async def feature_due(self, update, context, edit=False):
        uid = update.effective_user.id
        ids = self._feature_canonical_ids(self.features.due_ids(uid))
        lines = ["📅 Повторить сегодня", f"Вопросов к повторению: {len(ids)}", "",
                 "После ошибки вопрос возвращается на следующий день. "
                 "После правильного ответа — через 3, затем 7 и 14 дней.",
                 "Этот список объединяет вопросы из ваших тренировок и списков. "
                 "Сначала повторяются вопросы, срок которых уже наступил."]
        buttons = []
        if ids:
            buttons.append([_button(f"🎯 Повторить {len(ids)} вопросов", "feat_due_start")])
        else:
            lines += ["", "На сегодня всё повторено. Можно начать обычную тренировку или учить свой список."]
        buttons += [[_button("🔔 Напоминания", "feat_reminders"), _button("✅ Прогресс", "feat_progress")],
                    [_button("⬅️ В меню", "feat_menu")]]
        await self.panel(update, "\n".join(lines), InlineKeyboardMarkup(buttons), edit)

    async def feature_progress(self, update, context, edit=False):
        uid = update.effective_user.id
        overall = self.features.progress(uid, self.quiz.ids)
        state = self.features.get_learning(uid)
        ids = self._feature_canonical_ids(state["ids"])
        current = self.features.progress(uid, ids)
        lines = ["✅ Ваш прогресс", "", "По всей базе:",
                 f"Новых: {overall['new']} · повторить: {overall['review']} · выучено: {overall['mastered']}",
                 f"Пора повторить сегодня: {overall['due']}", "",
                 f"Активный список: {state.get('name', 'Основной')}",
                 f"Всего заданий: {len(ids)}", 
                 f"Новых: {current['new']} · повторить: {current['review']} · выучено: {current['mastered']}",
                 f"Пора повторить сегодня: {current['due']}", "",
                 "«Новый» — ещё не отвечали. «Повторить» — отвечали, но материал ещё закрепляется.",
                 "«Выучен» — три правильных ответа в разные дни после последней ошибки. "
                 "Просмотр ответа на карточке не засчитывается как правильный ответ."]
        buttons = [[_button("📅 Повторить сегодня", "feat_due")],
                   [_button("📂 Мои списки", "feat_lists:0"), _button("🧠 Заучивание", "feat_learning")],
                   [_button("⬅️ В меню", "feat_menu")]]
        await self.panel(update, "\n".join(lines), InlineKeyboardMarkup(buttons), edit)

    async def feature_topics(self, update, context, edit=False):
        topics = self._feature_topics()
        lines = ["🦴 Тренировка по теме", "Выберите раздел — бот начнёт случайные вопросы без повторов.", ""]
        buttons = []
        for index, (name, ids) in enumerate(topics):
            lines.append(f"{name}: {len(ids)} заданий")
            buttons.append([_button(f"{_clip(name, 50)} · {len(ids)}", f"feat_topic:{index}")])
        buttons.append([_button("⬅️ В меню", "feat_menu")])
        await self.panel(update, "\n".join(lines), InlineKeyboardMarkup(buttons), edit)

    async def feature_reminders(self, update, context, edit=False):
        settings = self.features.reminder_settings(update.effective_user.id)
        enabled = bool(settings["reminders_enabled"])
        hour = int(settings.get("reminder_hour_utc", 9))
        text = ("🔔 Напоминания о повторении\n"
                f"Сейчас: {'включены' if enabled else 'выключены'}.\n\n"
                f"Если есть вопросы к повторению, бот присылает одно напоминание в день в {hour:02d}:00 UTC. "
                "Например, 09:00 UTC — это 12:00 по Москве.\n"
                "Напоминания отправляются только после вашего включения; их можно отключить этой же кнопкой.")
        buttons = [[_button("🔕 Выключить" if enabled else "🔔 Включить", "feat_reminders_toggle")],
                   [_button("📅 Повторить сегодня", "feat_due")],
                   [_button("⬅️ В меню", "feat_menu")]]
        await self.panel(update, text, InlineKeyboardMarkup(buttons), edit)

    async def feature_reports(self, update, context, page=0, edit=False):
        if not self.authorized_admin(update.effective_user):
            await self.say(update, "Замечания доступны только администратору.")
            return
        count = self.features.report_count()
        total_pages = max(1, (count + FEATURE_PAGE_SIZE - 1) // FEATURE_PAGE_SIZE)
        page = max(0, min(int(page), total_pages - 1))
        rows = self.features.reports(page=page, limit=FEATURE_PAGE_SIZE)
        lines = [f"⚠️ Замечания пользователей · {page + 1}/{total_pages}",
                 f"Ожидают проверки: {count}. Ключи PDF сохраняются при закрытии замечания.", ""]
        buttons = []
        for row in rows:
            qid = row.get("question_id", row.get("qid"))
            uid = row.get("user_id", row.get("uid"))
            question = self.bank.get(qid)
            label = _label(question) if question else f"ID задания {qid}"
            tag = f"@{row['username']}" if row.get("username") else "Без username"
            lines += [f"#{row['id']} · {tag} · ID {uid}",
                      f"{label}: {_clip(question['question'] if question else 'Нет в базе', 160)}",
                      _clip(row.get("text", ""), 350), "Статус: ожидает проверки", ""]
            buttons.append([_button(f"📄 Полный текст #{row['id']}", f"feat_report_full:{row['id']}:{page}"),
                            _button("✅ Проверено", f"feat_resolve:{row['id']}:{page}")])
        if not rows:
            lines.append("Новых замечаний нет.")
        nav = []
        if page:
            nav.append(_button("⬅️", f"feat_reports:{page - 1}"))
        if page + 1 < total_pages:
            nav.append(_button("➡️", f"feat_reports:{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([_button("🔄 Обновить", f"feat_reports:{page}"), _button("⬅️ В меню", "feat_menu")])
        await self.panel(update, "\n".join(lines), InlineKeyboardMarkup(buttons), edit)

    async def feature_report_full(self, update, context, rid, page=0):
        if not self.authorized_admin(update.effective_user):
            await self.say(update, "Замечания доступны только администратору.")
            return
        row = self.features.db.execute("""
            SELECT r.*,u.username FROM question_reports r
            LEFT JOIN users u ON u.user_id=r.user_id WHERE r.id=?
        """, (rid,)).fetchone()
        if row is None:
            await self.feature_reports(update, context, page, edit=True)
            return
        question = self.bank.get(row["question_id"])
        tag = f"@{row['username']}" if row["username"] else "Без username"
        label = _label(question) if question else f"ID задания {row['question_id']}"
        title = _clip(question["question"], 400) if question else "Вопрос отсутствует в базе"
        status = "проверено" if row["status"] == "resolved" else "ожидает проверки"
        text = (f"⚠️ Полное замечание #{rid}\n{tag} · ID {row['user_id']}\n"
                f"{label}: {title}\nСтатус: {status}\n\n{row['text']}")
        buttons = []
        if row["status"] == "pending":
            buttons.append([_button("✅ Проверено", f"feat_resolve:{rid}:{page}")])
        buttons.append([_button("⬅️ К замечаниям", f"feat_reports:{page}")])
        # Telegram counts UTF-16 units.  A 2000-emoji remark may need a file,
        # even though an ordinary 2000-character remark fits the full panel.
        if len(text.encode("utf-16-le")) // 2 > 4096:
            await update.effective_message.reply_document(
                io.BytesIO(text.encode("utf-8-sig")), filename=f"remark_{rid}.txt")
            text = f"📄 Полное замечание #{rid} отправлено файлом TXT без сокращений.\n{tag} · ID {row['user_id']}\n{label}"
        await self.panel(update, text, InlineKeyboardMarkup(buttons), edit=True)

    async def _feature_report_prompt(self, update, context, qid):
        question = self.bank.get(qid)
        if question is None:
            await self.say(update, "Этот вопрос отсутствует в базе.")
            return
        uid = update.effective_user.id
        self._feature_cancel_input(uid)
        self.features.set_intake(uid, "report", {"qid": qid})
        text = (f"⚠️ Замечание к {_label(question)}\n{_clip(question['question'], 400)}\n\n"
                "Напишите, что вызывает сомнение: текст, варианты или ключ ответа. "
                "Можно указать страницу PDF. До 2000 символов.\n"
                "Администратор увидит замечание вместе с вашим тегом или ID. "
                "Ответ по PDF останется без изменений.")
        # A new message keeps the quiz question and its answer buttons usable.
        await self.say(update, text, InlineKeyboardMarkup([[_button("Отмена", "feat_cancel")]]))

    async def feature_text(self, update, context, text):
        """Handle persisted list-name/report prompts, or defer to normal Bot text."""
        uid = update.effective_user.id
        intake = self.features.get_intake(uid)
        if not intake:
            return False
        action = intake.get("action")
        payload = intake.get("payload") or {}
        if action not in ("listnew", "lrename", "report"):
            return False
        if action == "report":
            remark = str(text or "").strip()
            if not 1 <= len(remark) <= 2000:
                await self.say(update, "Напишите замечание длиной от 1 до 2000 символов или нажмите «Отмена».")
                return True
            qid = payload.get("qid")
            if qid not in self.bank:
                self.features.clear_intake(uid)
                await self.say(update, "Вопрос уже отсутствует в базе. Откройте другой вопрос.", self.menu(update.effective_user))
                return True
            self.features.add_report(uid, qid, remark)
            self.features.clear_intake(uid)
            await self.say(update, "✅ Замечание сохранено для проверки администратором. Можно продолжать отвечать.",
                           self.menu(update.effective_user))
            return True
        try:
            name = _name(text)
            if action == "listnew":
                await self._feature_remove_learning_buttons(update, context)
                self.features.create_list(uid, name)
                self.features.clear_intake(uid)
                await self.say(update, f"✅ Создан активный список «{name}». Теперь выберите количество случайных вопросов.")
                await self.learning_prompt(update, context, "create")
            else:
                lid = int(payload["lid"])
                if self.features.list_state(uid, lid) is None:
                    self.features.clear_intake(uid)
                    await self.feature_lists(update, context, notice="Список уже удалён. Выберите другой.")
                else:
                    self.features.rename_list(uid, lid, name)
                    self.features.clear_intake(uid)
                    await self.feature_lists(update, context, notice=f"✅ Название изменено на «{name}».")
        except (ValueError, KeyError) as exc:
            await self.say(update, str(exc) if isinstance(exc, ValueError) else "Откройте «Мои списки» заново.")
        return True

    async def feature_callback(self, update, context, parts):
        """Acknowledge every callback exactly once and enforce admin/list ownership."""
        query = update.callback_query
        uid = update.effective_user.id
        action = parts[0] if parts else ""
        if action in ("feat_reports", "feat_resolve", "feat_report_full") and not self.authorized_admin(update.effective_user):
            await query.answer("Только для администратора.", show_alert=True)
            return
        known = {"feat_lists", "feat_newlist", "feat_switch", "feat_rename", "feat_delete",
                 "feat_delete_yes", "feat_learning", "feat_due", "feat_due_start", "feat_progress",
                 "feat_topics", "feat_topic", "feat_reminders", "feat_reminders_toggle",
                 "feat_report", "feat_reports", "feat_report_full", "feat_resolve", "feat_menu", "feat_cancel", "feat_star"}
        if action not in known:
            await query.answer("Эта кнопка устарела. Откройте меню заново.")
            return
        if action == "feat_star":
            try:
                if len(parts) != 2:
                    raise ValueError
                qid = self.quiz.canonical.get(int(parts[1]))
                if qid is None:
                    raise ValueError
                state, added = self.features.append_question(uid, qid)
            except (TypeError, ValueError):
                await query.answer("Этот вопрос сейчас недоступен.", show_alert=True)
                return
            notice = f"⭐ {'Добавлено' if added else 'Уже есть'} в «{_clip(state.get('name', 'Основной'), 60)}»: {len(state['ids'])} заданий."
            await query.answer(notice)
            return
        await query.answer()
        try:
            self._feature_cancel_input(uid)
            if action == "feat_lists":
                await self.feature_lists(update, context, int(parts[1]) if len(parts) == 2 else 0, edit=True)
            elif action == "feat_newlist":
                self.features.set_intake(uid, "listnew", {})
                await self.panel(update, "➕ Новый список\nНапишите название от 1 до 60 символов, например «К экзамену». "
                                 "После этого выберите количество случайных вопросов.",
                                 InlineKeyboardMarkup([[_button("Отмена", "feat_lists:0")]]), edit=True)
            elif action in ("feat_switch", "feat_rename", "feat_delete", "feat_delete_yes"):
                if len(parts) != 2:
                    raise ValueError
                lid = int(parts[1])
                state = self.features.list_state(uid, lid)
                if state is None:
                    await self.feature_lists(update, context, edit=True, notice="Список уже удалён или недоступен.")
                    return
                if action == "feat_switch":
                    if lid != self.features.active_list_id(uid):
                        await self._feature_remove_learning_buttons(update, context)
                    self.features.switch_list(uid, lid)
                    await self.learning_home(update, context, notice=f"📂 Активный список: {state.get('name', 'Основной')}", edit=True)
                elif action == "feat_rename":
                    self.features.set_intake(uid, "lrename", {"lid": lid})
                    await self.panel(update, f"✏️ Название списка «{state.get('name', 'Основной')}»\n"
                                     "Напишите новое название от 1 до 60 символов.",
                                     InlineKeyboardMarkup([[_button("Отмена", "feat_lists:0")]]), edit=True)
                elif action == "feat_delete":
                    await self.panel(update, f"Удалить список «{state.get('name', 'Основной')}» ({len(state['ids'])} заданий)?\n"
                                     "История ответов, ошибки и прогресс сохранятся.",
                                     InlineKeyboardMarkup([[_button("🗑 Удалить список", f"feat_delete_yes:{lid}")],
                                                           [_button("Отмена", "feat_lists:0")]]), edit=True)
                else:
                    if lid == self.features.active_list_id(uid):
                        await self._feature_remove_learning_buttons(update, context)
                    deleted = self.features.delete_list(uid, lid)
                    await self.feature_lists(update, context, edit=True,
                                             notice="🗑 Список удалён." if deleted else "Этот список уже удалён.")
            elif action == "feat_learning":
                await self.learning_home(update, context, edit=True)
            elif action == "feat_due":
                await self.feature_due(update, context, edit=True)
            elif action == "feat_due_start":
                ids = self._feature_canonical_ids(self.features.due_ids(uid))
                if ids:
                    await self.start_pool(update, context, "due", ids)
                else:
                    await self.feature_due(update, context, edit=True)
            elif action == "feat_progress":
                await self.feature_progress(update, context, edit=True)
            elif action == "feat_topics":
                await self.feature_topics(update, context, edit=True)
            elif action == "feat_topic":
                if len(parts) != 2:
                    raise ValueError
                index = int(parts[1])
                topics = self._feature_topics()
                if not 0 <= index < len(topics):
                    raise ValueError
                topic, ids = topics[index]
                await self.start_pool(update, context, "topic", ids, topic=topic)
            elif action == "feat_reminders":
                await self.feature_reminders(update, context, edit=True)
            elif action == "feat_reminders_toggle":
                self.features.toggle_reminders(uid)
                await self.feature_reminders(update, context, edit=True)
            elif action == "feat_report":
                if len(parts) != 2:
                    raise ValueError
                await self._feature_report_prompt(update, context, int(parts[1]))
            elif action == "feat_reports":
                await self.feature_reports(update, context, int(parts[1]) if len(parts) == 2 else 0, edit=True)
            elif action == "feat_report_full":
                if len(parts) != 3:
                    raise ValueError
                await self.feature_report_full(update, context, int(parts[1]), max(0, int(parts[2])))
            elif action == "feat_resolve":
                if len(parts) != 3:
                    raise ValueError
                self.features.resolve_report(int(parts[1]), uid)
                await self.feature_reports(update, context, int(parts[2]), edit=True)
            elif action in ("feat_menu", "feat_cancel"):
                await self.say(update, "Ввод отменён. Главное меню." if action == "feat_cancel" else "Главное меню.",
                               self.menu(update.effective_user))
        except (ValueError, IndexError, KeyError):
            await self.say(update, "Кнопка уже недоступна. Откройте нужный раздел заново.", self.menu(update.effective_user))
