#!/usr/bin/env python3
"""Telegram anatomy quiz. Python 3.10+ / python-telegram-bot 22.5."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import io
import json
import logging
import os
import random
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from latin_cards import LATIN, LATIN43, LatinStore, load_latin_cards
from latin_ui import LatinUI

from answer_helpers import (answer_options, source_option_lines, selected_letters,
                            is_correct_selection, correct_answer_lines, breakdown_lines)
from features import FeatureStore
from feature_ui import FeatureUI, DUE, PROGRESS, TOPICS, LISTS, REMINDERS, REPORTS


BASE_DIR = Path(__file__).resolve().parent
EMBEDDED_QUESTIONS_JSON = None  # EMBED_DATABASE_HERE
ADMIN_USERNAME = "Timur0_15"
LOG = logging.getLogger("anatomy_quiz")
START = "🎲 Начать"
RESUME = "▶️ Продолжить"
AGAIN = "🔄 Заново"
STOP = "⏹ Стоп"
BACK = "⬅️ Назад / сброс сессии"
MISTAKES = "📚 Мои ошибки"
PRACTICE = "🎯 Повторить ошибки"
STATS = "📈 Моя статистика"
ADMIN = "📊 Статистика пользователей"
LEARNING = "🧠 Заучивание"
EXAM = "🎓 Экзамен"
PAGE_SIZE = 8


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_env(path):
    """Simple KEY=value configuration, without a separate dotenv dependency."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_questions(path=None):
    raw = EMBEDDED_QUESTIONS_JSON
    if raw is None:
        path = Path(path or BASE_DIR / "questions.txt")
        raw = path.read_text(encoding="utf-8-sig")
    records = json.loads(raw)
    if not isinstance(records, list):
        raise ValueError("База вопросов должна быть JSON-массивом")
    bank = {}
    for record in records:
        qid = record["id"]
        if not isinstance(qid, int) or qid <= 0 or qid in bank:
            raise ValueError(f"Неверный или повторяющийся ID: {qid}")
        if record.get("enabled", True):
            options = record["options"]
            correct = record["correct"]
            source_faithful = record.get("source_faithful", False)
            choices = answer_options(record)
            if (not record["question"].strip() or not 2 <= len(choices) <= 10
                    or any(choice["letter"] not in "абвгдежз" for choice in choices)
                    or not correct or len(set(correct)) != len(correct)
                    or any(key not in "абвгдежз" for key in correct)
                    or (not source_faithful and (any(not text.strip() for text in options.values())
                        or not set(correct) <= set(options)))):
                raise ValueError(f"В задании {qid} неверные варианты/ключ; исправьте или задайте enabled=false")
        bank[qid] = record
    if not any(q.get("enabled", True) for q in bank.values()):
        raise ValueError("В базе нет включённых вопросов")
    return bank


class Store:
    def __init__(self, path):
        self.features = None
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                last_name TEXT, joined_at TEXT NOT NULL, last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                state TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                question_id INTEGER NOT NULL, selected TEXT NOT NULL,
                expected TEXT NOT NULL, is_correct INTEGER NOT NULL,
                session_id TEXT NOT NULL, question_token TEXT NOT NULL,
                answered_at TEXT NOT NULL,
                UNIQUE(user_id, question_token)
            );
            CREATE INDEX IF NOT EXISTS answers_user_question ON answers(user_id, question_id);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS learning_sets (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                state TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exam_results (
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                session_id TEXT NOT NULL, state TEXT NOT NULL,
                PRIMARY KEY(user_id,session_id)
            );
        """)
        if "visible" not in {row[1] for row in self.db.execute("PRAGMA table_info(answers)")}:
            self.db.execute("ALTER TABLE answers ADD COLUMN visible INTEGER NOT NULL DEFAULT 1")
            self.db.commit()

    def close(self):
        self.db.close()

    def touch(self, user):
        now = utcnow()
        with self.db:
            self.db.execute("""
                INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                first_name=excluded.first_name, last_name=excluded.last_name,
                last_seen=excluded.last_seen
            """, (user.id, user.username, user.first_name, user.last_name, now, now))

    def state(self, uid):
        row = self.db.execute("SELECT state FROM sessions WHERE user_id=?", (uid,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, uid, state):
        self.db.execute("""
            INSERT INTO sessions VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at
        """, (uid, json.dumps(state, ensure_ascii=False), utcnow()))
        if state.get("mode") == "exam" and state.get("status") == "completed":
            hidden = self.db.execute("SELECT question_id,is_correct,answered_at FROM answers WHERE user_id=? AND session_id=? AND visible=0 ORDER BY id",
                                     (uid, state["session_id"])).fetchall()
            for row in hidden:
                if self.features:
                    self.features.update_progress_no_commit(uid, row["question_id"], bool(row["is_correct"]),
                                                           datetime.fromisoformat(row["answered_at"]))
            self.db.execute("UPDATE answers SET visible=1 WHERE user_id=? AND session_id=?",
                            (uid, state["session_id"]))
            self.db.execute("""
                INSERT INTO exam_results VALUES(?,?,?)
                ON CONFLICT(user_id,session_id) DO UPDATE SET state=excluded.state
            """, (uid, state["session_id"], json.dumps(state, ensure_ascii=False)))

    def exam_result(self, uid, session_id):
        row = self.db.execute("SELECT state FROM exam_results WHERE user_id=? AND session_id=?",
                              (uid, session_id)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, uid, state):
        with self.db:
            self._save(uid, state)

    def is_admin(self, user, configured_id=None):
        if configured_id is not None:
            return user.id == configured_id
        row = self.db.execute("SELECT value FROM settings WHERE key='admin_id'").fetchone()
        if row:
            return user.id == int(row[0])
        if (user.username or "").casefold() != ADMIN_USERNAME.casefold():
            return False
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('admin_id', ?)", (str(user.id),))
        row = self.db.execute("SELECT value FROM settings WHERE key='admin_id'").fetchone()
        return user.id == int(row[0])

    def record(self, uid, state, question):
        """Persist the answer and session in a single transaction; reject repeats."""
        correct = is_correct_selection(question, state["selected"])
        state["answered"] = True
        state["correct_count" if correct else "wrong_count"] += 1
        with self.db:
            self.db.execute("""
                INSERT INTO answers (user_id, question_id, selected, expected,
                    is_correct, session_id, question_token, answered_at, visible)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (uid, question["id"], json.dumps(state["selected"], ensure_ascii=False),
                  json.dumps(question["correct"], ensure_ascii=False), int(correct),
                  state["session_id"], state["token"], utcnow(), int(state["mode"] != "exam")))
            self._save(uid, state)
            if self.features and state["mode"] != "exam":
                self.features.update_progress_no_commit(uid, question["id"], correct)
        return correct

    def wrong_questions(self, uid):
        return self.db.execute("""
            SELECT question_id, COUNT(*) AS attempts,
                SUM(1-is_correct) AS wrong, SUM(is_correct) AS correct,
                MAX(answered_at) AS last_answer
            FROM answers WHERE user_id=? AND visible=1 GROUP BY question_id HAVING wrong>0
            ORDER BY wrong DESC, (1.0*SUM(1-is_correct)/COUNT(*)) DESC,
                     attempts DESC, question_id ASC
        """, (uid,)).fetchall()

    def user_summary(self, uid):
        return self.db.execute("""
            SELECT COUNT(*) AS attempts, COALESCE(SUM(is_correct), 0) AS correct,
                COALESCE(SUM(1-is_correct), 0) AS wrong,
                COUNT(DISTINCT question_id) AS questions
            FROM answers WHERE user_id=? AND visible=1
        """, (uid,)).fetchone()

    def user_rows(self):
        return self.db.execute("""
            SELECT u.*, COUNT(a.id) AS attempts,
                COALESCE(SUM(a.is_correct), 0) AS correct,
                COALESCE(SUM(1-a.is_correct), 0) AS wrong,
                COUNT(DISTINCT a.question_id) AS questions
            FROM users u LEFT JOIN answers a ON a.user_id=u.user_id AND a.visible=1
            GROUP BY u.user_id ORDER BY attempts DESC, u.user_id ASC
        """).fetchall()

    def learning(self, uid):
        if self.features:
            return self.features.get_learning(uid)
        row = self.db.execute("SELECT state FROM learning_sets WHERE user_id=?", (uid,)).fetchone()
        return json.loads(row[0]) if row else {
            "ids": [], "version": "", "pending": None, "study_index": 0, "revealed": False}

    def _save_learning(self, uid, state):
        if self.features:
            self.features._save_learning_no_commit(uid, state)
            return
        self.db.execute("""
            INSERT INTO learning_sets VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at
        """, (uid, json.dumps(state, ensure_ascii=False), utcnow()))

    def save_learning(self, uid, state):
        with self.db:
            self._save_learning(uid, state)


class Quiz:
    def __init__(self, store, bank, rng=None):
        self.store, self.bank = store, bank
        self.rng = rng or random.SystemRandom()
        # Keep all source records in the TXT, but issue identical tasks once.
        signatures = {}
        self.canonical = {}
        for qid, q in sorted(bank.items()):
            if not q.get("enabled", True):
                continue
            signature = (q["question"], tuple((o["label"], o["text"]) for o in answer_options(q)), tuple(sorted(q["correct"])))
            canonical = signatures.setdefault(signature, qid)
            self.canonical[qid] = canonical
        self.ids = sorted(set(self.canonical.values()))

    def new(self, uid, mode="all", pool_override=None, **extra):
        if pool_override is not None:
            pool = list(dict.fromkeys(self.canonical[qid] for qid in pool_override if qid in self.canonical))
        elif mode == "all":
            pool = self.ids
        elif mode == "errors":
            pool = list(dict.fromkeys(
                self.canonical[row["question_id"]] for row in self.store.wrong_questions(uid)
                if row["question_id"] in self.canonical))
        elif mode == "learning":
            pool = list(dict.fromkeys(
                self.canonical[qid] for qid in self.store.learning(uid)["ids"] if qid in self.canonical))
        else:
            raise ValueError("Неизвестный режим тренировки")
        if not pool:
            return None
        previous = self.store.state(uid)
        if previous and previous.get("mode") == "exam" and previous["status"] in ("running", "paused"):
            self.finish_exam(uid, "restarted")
        state = {"session_id": secrets.token_hex(8), "status": "running", "mode": mode,
                 "pool": list(pool), "seen": [], "current": None, "selected": [],
                 "answered": False, "token": "", "message_id": None,
                 "correct_count": 0, "wrong_count": 0}
        if mode == "learning":
            current_list = self.store.learning(uid)
            state.update(list_id=current_list.get("list_id"), list_name=current_list.get("name", "Мой список"))
        state.update(extra)
        self.store.save(uid, state)
        return self.next(uid)

    def next(self, uid):
        state = self.store.state(uid)
        if self.exam_expired(state):
            return self.finish_exam(uid, "timeout")
        if not state or state["status"] != "running":
            return state
        if state["current"] is not None and not state["answered"]:
            return state
        seen = set(state["seen"])
        remaining = [qid for qid in state["pool"] if qid not in seen and qid in self.ids]
        if not remaining:
            if state["mode"] == "exam":
                return self.finish_exam(uid, "completed")
            state.update(status="completed", current=None, selected=[], token="", message_id=None)
        else:
            qid = self.rng.choice(remaining)
            state["seen"].append(qid)
            state.update(current=qid, selected=[], answered=False, token=secrets.token_hex(6), message_id=None)
        self.store.save(uid, state)
        return state

    def pause(self, uid):
        state = self.store.state(uid)
        if state and state["status"] == "running":
            state.update(status="paused", token=secrets.token_hex(6), message_id=None)
            self.store.save(uid, state)
        return state

    def resume(self, uid):
        state = self.store.state(uid)
        if self.exam_expired(state):
            return self.finish_exam(uid, "timeout")
        if not state or state["status"] not in ("running", "paused"):
            return None
        state.update(status="running", token=secrets.token_hex(6), message_id=None)
        self.store.save(uid, state)
        return state

    def exam_expired(self, state, now=None):
        if not state or state.get("mode") != "exam" or state.get("status") not in ("running", "paused"):
            return False
        deadline = state.get("deadline")
        return bool(deadline and (now or datetime.now(timezone.utc)) >= datetime.fromisoformat(deadline))

    def start_exam(self, uid, timed=True, count=45, minutes=45):
        pool = self.rng.sample(self.ids, min(count, len(self.ids)))
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat() if timed else None
        return self.new(uid, "exam", pool_override=pool, deadline=deadline, timed=bool(timed),
                        duration_minutes=minutes, exam_history=[], finished_reason=None)

    def finish_exam(self, uid, reason="completed"):
        state = self.store.state(uid)
        if not state or state.get("mode") != "exam":
            return None
        if state["status"] == "completed":
            return state
        state.update(status="completed", current=None, token="", message_id=None,
                     finished_reason=reason, finished_at=utcnow(), notification_pending=reason == "timeout")
        self.store.save(uid, state)
        return state

    def reset(self, uid):
        state = self.store.state(uid)
        if state and state.get("mode") == "exam" and state["status"] in ("running", "paused"):
            self.finish_exam(uid, "cancelled")
        with self.store.db:
            self.store.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))

    def toggle(self, uid, token, key):
        state = self.store.state(uid)
        if (not state or state["status"] != "running" or state["answered"]
                or state["token"] != token or state["current"] not in self.ids):
            return None
        if self.exam_expired(state):
            return None
        question = self.bank[state["current"]]
        if key not in {option["id"] for option in answer_options(question)}:
            return None
        selected = set(state["selected"])
        if key in selected:
            selected.remove(key)
        elif len(question["correct"]) == 1:
            selected = {key}
        else:
            selected.add(key)
        state["selected"] = sorted(selected)
        self.store.save(uid, state)
        return state

    def submit(self, uid, token):
        state = self.store.state(uid)
        if (not state or state["status"] != "running" or state["answered"]
                or state["token"] != token or state["current"] not in self.ids):
            return None
        if self.exam_expired(state):
            self.finish_exam(uid, "timeout")
            return "expired"
        if not state["selected"]:
            return "empty"
        if state["mode"] == "exam":
            question = self.bank[state["current"]]
            state.setdefault("exam_history", []).append({
                "qid": question["id"], "selected": list(state["selected"]),
                "expected": list(question["correct"]),
                "correct": is_correct_selection(question, state["selected"]),
            })
        self.store.record(uid, state, self.bank[state["current"]])
        return state


class Learning:
    """One persistent, extendable learning set per Telegram user."""
    def __init__(self, store, quiz):
        self.store, self.quiz = store, quiz

    def get(self, uid):
        state = self.store.learning(uid)
        if not state["version"]:
            state["version"] = secrets.token_hex(6)
            self.store.save_learning(uid, state)
        return state

    def prompt(self, uid, mode):
        if mode not in ("create", "add"):
            raise ValueError("Неизвестное действие со списком")
        state = self.get(uid)
        state.update(pending=mode, version=secrets.token_hex(6))
        self.store.save_learning(uid, state)
        return state

    def cancel_prompt(self, uid):
        state = self.get(uid)
        if state["pending"]:
            state.update(pending=None, version=secrets.token_hex(6))
            self.store.save_learning(uid, state)
        return state

    def change(self, uid, count, mode, version=None):
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= len(self.quiz.ids):
            raise ValueError(f"Введите целое число от 1 до {len(self.quiz.ids)}")
        if mode not in ("create", "add"):
            raise ValueError("Неизвестное действие со списком")
        state = self.get(uid)
        if version is not None and state["version"] != version:
            return None
        existing = [] if mode == "create" else list(state["ids"])
        existing_set = set(existing)
        remaining = [qid for qid in self.quiz.ids if qid not in existing_set]
        added = self.quiz.rng.sample(remaining, min(count, len(remaining)))
        state.update(ids=existing + added, pending=None, version=secrets.token_hex(6))
        if mode == "create":
            state.update(study_index=0, revealed=False)
        session = self.store.state(uid)
        with self.store.db:
            self.store._save_learning(uid, state)
            if session and session["mode"] == "learning" and session.get("list_id") == state.get("list_id"):
                if mode == "create":
                    self.store.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
                elif session["status"] in ("running", "paused"):
                    session["pool"] += [qid for qid in added if qid not in session["pool"]]
                    self.store._save(uid, session)
        return state, len(added)

    def reset(self, uid, version=None):
        state = self.get(uid)
        if version is not None and state["version"] != version:
            return None
        state.update(ids=[], pending=None, study_index=0, revealed=False, version=secrets.token_hex(6))
        session = self.store.state(uid)
        with self.store.db:
            self.store._save_learning(uid, state)
            if session and session["mode"] == "learning" and session.get("list_id") == state.get("list_id"):
                self.store.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        return state


def q_label(question):
    number = question.get("source_number", question["id"])
    return f"№{number}" if number is not None else "Без номера (стр. 146–147)"


def percent(correct, attempts):
    return f"{100 * correct / attempts:.1f}%" if attempts else "0%"


def session_summary(state):
    attempts = state["correct_count"] + state["wrong_count"]
    return (f"В сессии: {attempts} ответов · ✅ {state['correct_count']} · "
            f"❌ {state['wrong_count']} · {percent(state['correct_count'], attempts)}")


def question_view(bank, state):
    question = bank[state["current"]]
    exam = state["mode"] == "exam"
    mode = {"errors": "Повторение ошибок", "learning": f"Список: {state.get('list_name', 'Заучивание')}",
            "due": "Повторить сегодня", "topic": f"Тема: {state.get('topic', '')}",
            "exam": "Экзамен"}.get(state["mode"], "Тренировка")
    page = str(question["page"])
    if question.get("page_end", question["page"]) != question["page"]:
        page += f"–{question['page_end']}"
    lines = [f"{mode} · Вопрос {len(state['seen'])}/{len(state['pool'])}",
             f"{q_label(question)} · стр. {page}", "", question["question"], ""]
    lines += source_option_lines(question)
    if question.get("source_issue"):
        lines += ["", "Обозначения сохранены как в PDF. Кнопки без текста отмечены отдельно."]
    if exam:
        if state.get("deadline"):
            remaining = max(0, int((datetime.fromisoformat(state["deadline"]) - datetime.now(timezone.utc)).total_seconds()))
            lines += ["", f"⏱ Осталось {remaining//60}:{remaining%60:02d}. Пауза не останавливает таймер."]
        else:
            lines += ["", "⏱ Без таймера. Ответы откроются после завершения экзамена."]
    elif state.get("progress_label"):
        lines += ["", "Прогресс: " + state["progress_label"]]
    if state["answered"]:
        if exam:
            lines += ["", "Ответ сохранён. Результат и правильные варианты появятся в конце.",
                      "Ваш выбор: " + ", ".join(sorted(selected_letters(question, state["selected"])))]
        else:
            correct = is_correct_selection(question, state["selected"])
            lines += ["", "✅ Правильно!" if correct else "❌ Неправильно."]
            lines += breakdown_lines(question, state["selected"])
            lines += ["Ответ по ключу PDF:"] + correct_answer_lines(question)
            lines += ["", session_summary(state)]
        buttons = [[InlineKeyboardButton("➡️ Следующий вопрос", callback_data=f"next:{state['token']}")]]
    else:
        lines += ["", "Выберите один вариант и нажмите «Проверить»." if len(question["correct"]) == 1
                  else "Выберите все правильные варианты и нажмите «Проверить»."]
        buttons = []
        for option in answer_options(question):
            key = option["id"]
            text = option["text"] or "Пустой вариант в PDF"
            short = text if len(text) <= 54 else text[:51] + "…"
            label = f"{'✅' if key in state['selected'] else '⬜'} {option['label']}) {short}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"pick:{state['token']}:{key}")])
        buttons.append([InlineKeyboardButton("✅ Проверить", callback_data=f"check:{state['token']}")])
    buttons += [[InlineKeyboardButton("⭐ В заучивание", callback_data=f"feat_star:{question['id']}"),
                 InlineKeyboardButton("⚠️ Ошибка в задании", callback_data=f"feat_report:{question['id']}")],
                [InlineKeyboardButton(STOP, callback_data=f"pause:{state['token']}"),
                 InlineKeyboardButton(AGAIN, callback_data=f"restart:{state['token']}")],
                [InlineKeyboardButton(BACK, callback_data=f"back:{state['token']}")]]
    if exam:
        buttons.insert(-2, [InlineKeyboardButton("🏁 Завершить экзамен", callback_data=f"finish_exam:{state['token']}")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


class Bot(FeatureUI, LatinUI):
    def __init__(self, store, bank, admin_id=None):
        self.store, self.bank, self.admin_id = store, bank, admin_id
        self.quiz = Quiz(store, bank)
        self.latin = LatinStore(store)
        self.features = FeatureStore(store, bank, self.quiz.canonical)
        self.store.features = self.features
        self.learning = Learning(store, self.quiz)
        for row in self.store.db.execute("SELECT user_id,state FROM sessions").fetchall():
            saved = json.loads(row["state"])
            if saved.get("mode") == "all" and saved.get("status") in ("running", "paused"):
                added = [qid for qid in self.quiz.ids if qid not in saved["pool"]]
                if added:
                    saved["pool"].extend(added)
                    self.store.save(row["user_id"], saved)

    def authorized_admin(self, user):
        return self.store.is_admin(user, self.admin_id)

    def menu(self, user):
        buttons = [[START, RESUME], [LEARNING, LISTS], [DUE, PROGRESS], [TOPICS, EXAM],
                   [MISTAKES, PRACTICE], [STATS, REMINDERS], [AGAIN, STOP], [LATIN], [LATIN43], [BACK]]
        if self.authorized_admin(user):
            buttons.append([ADMIN])
            buttons.append([REPORTS])
        return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

    async def say(self, update, text, reply_markup=None):
        return await update.effective_message.reply_text(text, reply_markup=reply_markup)

    async def remove_old_buttons(self, context, chat_id, state):
        if state and state.get("message_id"):
            try:
                await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=state["message_id"], reply_markup=None)
            except TelegramError:
                # State checks still invalidate these buttons, even if edit fails.
                pass

    async def show_question(self, update, context, state, edit=False):
        if state["status"] == "completed":
            if state["mode"] == "exam":
                await self.show_exam_result(update, context, state)
                return
            await self.say(update, "🏁 Вопросы в этой сессии закончились.\n" + session_summary(state)
                           + "\nНажмите «Заново», чтобы начать новую сессию.", self.menu(update.effective_user))
            return
        progress = self.features.question_progress(update.effective_user.id, state["current"])
        view_state = dict(state, progress_label={"mastered": "Выучен", "review": "Повторить", "new": "Новый"}.get(
            progress.get("status") if progress else "new", "Новый"))
        text, keyboard = question_view(self.bank, view_state)
        if edit and update.callback_query:
            try:
                await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
                return
            except BadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
        message = await self.say(update, text, keyboard)
        state["message_id"] = message.message_id
        self.store.save(update.effective_user.id, state)

    async def start_pool(self, update, context, mode, pool, **extra):
        old = self.store.state(update.effective_user.id)
        await self.remove_old_buttons(context, update.effective_chat.id, old)
        state = self.quiz.new(update.effective_user.id, mode, pool_override=pool, **extra)
        if state:
            await self.show_question(update, context, state)
        else:
            await self.say(update, "В этом наборе пока нет вопросов.", self.menu(update.effective_user))

    async def exam_settings(self, update, context):
        uid = update.effective_user.id
        version = secrets.token_hex(6)
        self.features.set_intake(uid, "exam_setup", {"version": version})
        text = ("🎓 Экзамен\n45 случайных вопросов без повторов.\n"
                "Правильные ответы, разбор и результат появятся после завершения.\n"
                "Можно выбрать 45 минут или отключить таймер. Пауза не останавливает таймер.\n"
                "До окончания результаты экзамена скрыты и в вашей статистике.")
        buttons = [[InlineKeyboardButton("45 вопросов / 45 минут", callback_data=f"exam_start:{version}:45:45")],
                   [InlineKeyboardButton("45 вопросов / без таймера", callback_data=f"exam_start:{version}:45:0")],
                   [InlineKeyboardButton("30 вопросов / без таймера", callback_data=f"exam_start:{version}:30:0")],
                   [InlineKeyboardButton("⬅️ В меню", callback_data="feat_menu")]]
        await self.say(update, text, InlineKeyboardMarkup(buttons))

    @staticmethod
    def exam_summary_view(state):
        total = len(state["pool"])
        answered = state["correct_count"] + state["wrong_count"]
        score = 100 * state["correct_count"] / total if total else 0
        grade = "Отлично" if score >= 91 else "Хорошо" if score >= 81 else "Удовлетворительно" if score >= 71 else "Нужно повторить"
        reason = {"timeout": "Время вышло", "manual": "Завершён вами", "cancelled": "Прерван",
                  "restarted": "Начата другая сессия"}.get(state.get("finished_reason"), "Завершён")
        text = (f"🎓 Экзамен: {reason}\nВсего заданий: {total}\nОтвечено: {answered}\n"
                f"✅ Правильно: {state['correct_count']}\n❌ Неправильно: {state['wrong_count']}\n"
                f"Без ответа: {total-answered}\nРезультат: {score:.1f}% · {grade}\n\n"
                "Процент рассчитан от всех заданий. Можно посмотреть ответы и сохранить ошибки в свой список.")
        sid = state["session_id"]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Разбор ответов", callback_data=f"exam_review:{sid}:0")],
            [InlineKeyboardButton("⭐ Ошибки в заучивание", callback_data=f"exam_add:{sid}")],
            [InlineKeyboardButton("🎓 Новый экзамен", callback_data="exam_menu")],
        ])
        return text, keyboard

    async def show_exam_result(self, update, context, state):
        text, keyboard = self.exam_summary_view(state)
        await self.say(update, text, keyboard)
        if state.get("notification_pending"):
            state["notification_pending"] = False
            current = self.store.state(update.effective_user.id)
            if current and current["session_id"] == state["session_id"]:
                self.store.save(update.effective_user.id, state)

    async def exam_review(self, update, context, state, page=0):
        history = {entry["qid"]: entry for entry in state.get("exam_history", [])}
        ids = state["seen"] + [qid for qid in state["pool"] if qid not in state["seen"]]
        size = 3
        pages = max(1, (len(ids)+size-1)//size)
        page = max(0, min(page, pages-1))
        lines = [f"🔍 Разбор экзамена · {page+1}/{pages}", ""]
        buttons = []
        for qid in ids[page*size:(page+1)*size]:
            q = self.bank[qid]
            entry = history.get(qid)
            status = "✅ Правильно" if entry and entry["correct"] else "❌ Неправильно" if entry else "Без ответа"
            lines += [f"{q_label(q)} · {status}", q["question"]] + source_option_lines(q)
            if entry:
                lines += breakdown_lines(dict(q, correct=entry["expected"]), entry["selected"])
            lines += ["Ответ по ключу PDF:"] + correct_answer_lines(q, entry["expected"] if entry else None) + [""]
            buttons.append([InlineKeyboardButton(f"⭐ Сохранить {q_label(q)}", callback_data=f"feat_star:{qid}")])
        nav = []
        if page:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"exam_review:{state['session_id']}:{page-1}"))
        if page+1 < pages:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"exam_review:{state['session_id']}:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("🏁 Результат", callback_data=f"exam_result:{state['session_id']}")])
        await self.panel(update, "\n".join(lines), InlineKeyboardMarkup(buttons), edit=True)

    async def exam_callback(self, update, context, parts):
        query = update.callback_query
        uid = update.effective_user.id
        action = parts[0]
        if action == "exam_start":
            intake = self.features.get_intake(uid)
            if (len(parts) != 4 or not intake or intake.get("action") != "exam_setup"
                    or (intake.get("payload") or {}).get("version") != parts[1]):
                await query.answer("Эта кнопка устарела. Откройте «Экзамен» заново.")
                return
            try:
                count, minutes = int(parts[2]), int(parts[3])
                if count not in (30, 45) or minutes not in (0, 45):
                    raise ValueError
            except ValueError:
                await query.answer("Некорректный режим экзамена.")
                return
            self.features.clear_intake(uid)
            await query.answer()
            old = self.store.state(uid)
            await self.remove_old_buttons(context, update.effective_chat.id, old)
            await self.show_question(update, context, self.quiz.start_exam(uid, minutes > 0, count, minutes or 45))
            return
        if action == "exam_menu":
            await query.answer()
            await self.exam_settings(update, context)
            return
        state = self.store.exam_result(uid, parts[1]) if len(parts) >= 2 else None
        if state is None:
            await query.answer("Этот результат недоступен. Завершите свой экзамен.")
            return
        await query.answer()
        if action == "exam_review":
            try:
                await self.exam_review(update, context, state, int(parts[2]) if len(parts) == 3 else 0)
            except ValueError:
                await self.say(update, "Откройте разбор экзамена заново.")
        elif action == "exam_result":
            text, keyboard = self.exam_summary_view(state)
            await self.panel(update, text, keyboard, edit=True)
        elif action == "exam_add":
            answered_correctly = {entry["qid"] for entry in state.get("exam_history", []) if entry["correct"]}
            added = 0
            for qid in state["pool"]:
                if qid not in answered_correctly:
                    _, changed = self.features.append_question(uid, qid)
                    added += int(changed)
            await self.say(update, f"⭐ Добавлено {added} заданий в активный список. Учтены неверные и пропущенные ответы.",
                           self.menu(update.effective_user))

    async def background(self, application):
        """Persisted deadlines and explicitly opted-in reminders, without extra deps."""
        while True:
            await asyncio.sleep(20)
            try:
                rows = self.store.db.execute("SELECT user_id,state FROM sessions").fetchall()
                for row in rows:
                    state = json.loads(row["state"])
                    if self.quiz.exam_expired(state):
                        message_id = state.get("message_id")
                        state = self.quiz.finish_exam(row["user_id"], "timeout")
                        if message_id:
                            with contextlib.suppress(TelegramError):
                                await application.bot.edit_message_reply_markup(chat_id=row["user_id"], message_id=message_id, reply_markup=None)
                    if state.get("mode") == "exam" and state.get("notification_pending"):
                        text, keyboard = self.exam_summary_view(state)
                        try:
                            await application.bot.send_message(chat_id=row["user_id"], text=text, reply_markup=keyboard)
                            state["notification_pending"] = False
                            current = self.store.state(row["user_id"])
                            if current and current["session_id"] == state["session_id"]:
                                self.store.save(row["user_id"], state)
                        except TelegramError:
                            pass
                for uid in self.features.reminders_due():
                    if not self.features.reminder_settings(uid)["reminders_enabled"]:
                        continue
                    count = len(self.features.due_ids(uid))
                    try:
                        await application.bot.send_message(chat_id=uid,
                            text=f"📅 Сегодня стоит повторить {count} заданий. Откройте «Повторить сегодня».",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📅 Повторить сегодня", callback_data="feat_due")]]))
                        self.features.mark_reminder(uid)
                    except TelegramError:
                        pass
            except Exception as exc:
                LOG.error("Фоновая задача: %s", type(exc).__name__)

    async def start(self, update, context):
        if update.effective_chat.type != "private":
            await self.say(update, "Откройте личный чат с ботом и нажмите /start.")
            return
        self.store.touch(update.effective_user)
        self.latin.invalidate(update.effective_user.id)
        self.learning.cancel_prompt(update.effective_user.id)
        self.features.clear_intake(update.effective_user.id)
        await self.say(update,
            "Остеология и латынь · 103 вопроса по костям.\n\n"
            "📖 Латынь — слова, правила, словарные формы и выражения.\n"
            "📖 Латынь, 43 параграф — 66 слов, три рода, окончания и правила из PDF.\n"
            "🎲 Начать — случайные вопросы без повторов в сессии.\n"
            "Выберите вариант или несколько вариантов и нажмите «Проверить».\n"
            "⏹ Стоп — пауза; «Продолжить» вернёт текущий вопрос.\n"
            "🔄 Заново — новая сессия.\n"
            "⬅️ Назад — сброс сессии и возврат в меню.\n"
            "📚 Мои ошибки — ваш личный список, самые частые ошибки сверху.\n"
            "🎯 Повторить ошибки — отдельная сессия по этому списку.\n"
            "🧠 Заучивание — свой список: создать, добавить задания, учить и сбросить.\n\n"
            "📂 Мои списки — несколько наборов с названиями.\n"
            "📅 Повторить сегодня — повторения через 1/3/7/14 дней.\n"
            "✅ Прогресс — новый, повторить, выучен.\n"
            "🦴 Темы — тренировка по разделам анатомии.\n"
            "🎓 Экзамен — результаты и ответы в конце.\n"
            "⭐ В заучивание — сохранить текущий вопрос.\n"
            "⚠️ Ошибка в задании — оставить замечание администратору.\n\n"
            "Сброс сессии не удаляет историю ответов. Ответы проверяются по ключам PDF.",
            self.menu(update.effective_user))

    async def action(self, update, context, action):
        uid = update.effective_user.id
        state = self.store.state(uid)
        self.latin.invalidate(uid)
        if action == LATIN43:
            await self.latin_collection(update, "paragraph43", False)
            return
        if action == LATIN:
            await self.latin_home(update, context)
            return
        if await self.feature_action(update, context, action):
            return
        if action == EXAM:
            await self.exam_settings(update, context)
            return
        if self.quiz.exam_expired(state):
            state = self.quiz.finish_exam(uid, "timeout")
            await self.show_exam_result(update, context, state)
            if action in (START, RESUME, STOP):
                return
        if action == START:
            if state and state["status"] in ("running", "paused"):
                await self.remove_old_buttons(context, update.effective_chat.id, state)
                await self.show_question(update, context, self.quiz.resume(uid))
            else:
                await self.show_question(update, context, self.quiz.new(uid))
        elif action == AGAIN or action == PRACTICE:
            if action == PRACTICE and not any(r["question_id"] in self.quiz.canonical for r in self.store.wrong_questions(uid)):
                await self.say(update, "Пока нет ошибок по доступным вопросам. Начните тренировку.", self.menu(update.effective_user))
                return
            await self.remove_old_buttons(context, update.effective_chat.id, state)
            if action == AGAIN and state and state["mode"] == "exam":
                await self.show_question(update, context, self.quiz.start_exam(uid, state.get("timed", False),
                    len(state["pool"]), state.get("duration_minutes", 45)))
                return
            mode = "errors" if action == PRACTICE else ("learning" if state and state["mode"] == "learning" else "all")
            if mode == "learning" and state.get("list_id"):
                original = self.features.list_state(uid, state["list_id"])
                if original and original["ids"]:
                    await self.show_question(update, context, self.quiz.new(uid, mode, pool_override=original["ids"],
                        list_id=original["list_id"], list_name=original["name"]))
                    return
            if mode == "learning" and not self.learning.get(uid)["ids"]:
                await self.learning_home(update, context)
                return
            await self.show_question(update, context, self.quiz.new(uid, mode))
        elif action == RESUME:
            await self.remove_old_buttons(context, update.effective_chat.id, state)
            resumed = self.quiz.resume(uid)
            if resumed:
                await self.show_question(update, context, resumed)
            else:
                if state and state["mode"] == "exam" and state["status"] == "completed":
                    await self.show_exam_result(update, context, state)
                else:
                    await self.say(update, "Нет незавершённой сессии. Нажмите «Начать».", self.menu(update.effective_user))
        elif action == STOP:
            await self.remove_old_buttons(context, update.effective_chat.id, state)
            paused = self.quiz.pause(uid)
            text = "⏹ Сессия на паузе. Нажмите «Продолжить»." if paused and paused["status"] == "paused" else "Активной сессии нет."
            if paused:
                if paused["mode"] == "exam":
                    text += "\nОтветы откроются в конце экзамена."
                    if paused.get("deadline"):
                        text += " Таймер продолжает идти."
                else:
                    text += "\n" + session_summary(paused)
            await self.say(update, text, self.menu(update.effective_user))
        elif action == BACK:
            await self.remove_old_buttons(context, update.effective_chat.id, state)
            self.quiz.reset(uid)
            await self.say(update, "Сессия сброшена. История ответов и личные ошибки сохранены.", self.menu(update.effective_user))
        elif action == MISTAKES:
            await self.mistakes(update, context)
        elif action == STATS:
            summary = self.store.user_summary(uid)
            text = (f"📈 Ваша статистика\nОтветов: {summary['attempts']}\n"
                    f"✅ Правильно: {summary['correct']}\n❌ Ошибок: {summary['wrong']}\n"
                    f"Точность: {percent(summary['correct'], summary['attempts'])}\n"
                    f"Разных вопросов отвечено: {summary['questions']}")
            if state and state["mode"] != "exam":
                text += "\n\n" + session_summary(state)
            await self.say(update, text, self.menu(update.effective_user))
        elif action == ADMIN:
            await self.admin_stats(update, context)
        elif action == LEARNING:
            await self.learning_home(update, context)

    async def message(self, update, context):
        if update.effective_chat.type != "private":
            return
        self.store.touch(update.effective_user)
        text = update.effective_message.text
        if text in (START, RESUME, AGAIN, STOP, BACK, MISTAKES, PRACTICE, STATS, ADMIN, LEARNING,
                    EXAM, DUE, PROGRESS, TOPICS, LISTS, REMINDERS, REPORTS, LATIN, LATIN43):
            self.learning.cancel_prompt(update.effective_user.id)
            self.features.clear_intake(update.effective_user.id)
            await self.action(update, context, text)
        elif await self.feature_text(update, context, text):
            return
        elif self.learning.get(update.effective_user.id)["pending"]:
            await self.learning_count(update, context, text)
        else:
            await self.say(update, "Выберите действие в меню. /help — инструкция.", self.menu(update.effective_user))

    async def command(self, update, context):
        if update.effective_chat.type != "private":
            return
        self.store.touch(update.effective_user)
        command = update.effective_message.text.split()[0].split("@")[0].lower()
        if command == "/id":
            await self.say(update, f"Ваш Telegram ID: {update.effective_user.id}")
            return
        actions = {"/quiz": START, "/again": AGAIN, "/stop": STOP, "/back": BACK,
                   "/mistakes": MISTAKES, "/stats": STATS, "/admin": ADMIN, "/resume": RESUME, "/learn": LEARNING,
                   "/lists": LISTS, "/due": DUE, "/progress": PROGRESS, "/topics": TOPICS,
                   "/exam": EXAM, "/reminders": REMINDERS, "/reports": REPORTS, "/latin": LATIN, "/latin43": LATIN43}
        self.learning.cancel_prompt(update.effective_user.id)
        self.features.clear_intake(update.effective_user.id)
        await self.action(update, context, actions[command])

    async def learning_home(self, update, context, notice="", edit=False):
        state = self.learning.cancel_prompt(update.effective_user.id)
        count = len(state["ids"])
        version = state["version"]
        text = ("🧠 Ваш личный список для заучивания\n"
                f"Название: {state.get('name', 'Основной')}\n"
                f"Заданий в списке: {count}\n"
                f"Можно добавить новых: {len(self.quiz.ids)-count}\n\n"
                "Создайте список на любое количество вопросов, например 30. "
                "Кнопка «Добавить ещё 30» расширит его до 60 без повторов.\n"
                "«Учить» — карточки с показом ответа. «Проверить себя» — тест по списку.\n"
                "Список сохраняется отдельно для каждого пользователя.")
        if notice:
            text = notice + "\n\n" + text
        buttons = [[InlineKeyboardButton("📂 Другие списки / новый", callback_data="feat_lists:0")],
                   [InlineKeyboardButton("🔄 Заполнить случайными", callback_data=f"learn_prompt:{version}:create"),
                    InlineKeyboardButton("📋 Список", callback_data=f"learn_list:{version}:0")]]
        if count:
            buttons += [[InlineKeyboardButton("➕ Добавить ещё 30", callback_data=f"learn_count:{version}:add:30")],
                        [InlineKeyboardButton("🔢 Добавить другое количество", callback_data=f"learn_prompt:{version}:add")],
                        [InlineKeyboardButton("📖 Учить", callback_data=f"learn_card:{version}:{state['study_index']}:0"),
                         InlineKeyboardButton("🎲 Проверить себя", callback_data=f"learn_quiz:{version}")],
                        [InlineKeyboardButton("🗑 Сбросить список", callback_data=f"learn_reset:{version}")]]
        buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data=f"learn_menu:{version}")])
        await self.panel(update, text, InlineKeyboardMarkup(buttons), edit)

    async def learning_prompt(self, update, context, mode):
        state = self.learning.prompt(update.effective_user.id, mode)
        version = state["version"]
        text = "Сколько случайных вопросов включить в новый список?" if mode == "create" else "Сколько новых вопросов добавить к вашему списку?"
        text += f"\nНапишите число от 1 до {len(self.quiz.ids)} или выберите кнопку."
        if mode == "create" and state["ids"]:
            text += "\nНовый список заменит текущий; история ответов сохранится."
        buttons = [[InlineKeyboardButton(str(n), callback_data=f"learn_count:{version}:{mode}:{n}")
                    for n in (10, 30, 50) if n <= len(self.quiz.ids)],
                   [InlineKeyboardButton("Отмена", callback_data=f"learn_home:{version}")]]
        await self.panel(update, text, InlineKeyboardMarkup(buttons), edit=bool(update.callback_query))

    async def learning_count(self, update, context, text):
        state = self.learning.get(update.effective_user.id)
        try:
            count = int(text.strip())
            if not state["pending"]:
                return
            await self.learning_change(update, context, count, state["pending"], state["version"])
        except ValueError:
            await self.say(update, f"Введите целое число от 1 до {len(self.quiz.ids)}, например 30. /learn — отмена.")

    async def learning_change(self, update, context, count, mode, version):
        old_session = self.store.state(update.effective_user.id)
        result = self.learning.change(update.effective_user.id, count, mode, version)
        if result is None:
            await self.say(update, "Список уже изменился. Откройте «Заучивание» заново.")
            return
        state, added = result
        if mode == "create" and old_session and old_session["mode"] == "learning":
            await self.remove_old_buttons(context, update.effective_chat.id, old_session)
        notice = f"✅ Создан список из {len(state['ids'])} разных заданий." if mode == "create" else f"✅ Добавлено {added} новых заданий. Теперь в списке {len(state['ids'])}."
        if added < count:
            notice += "\nДобавлены все оставшиеся вопросы: база закончилась."
        await self.learning_home(update, context, notice, edit=bool(update.callback_query))

    async def learning_list(self, update, context, page=0, edit=True):
        state = self.learning.get(update.effective_user.id)
        if not state["ids"]:
            await self.learning_home(update, context, "Ваш список пока пуст. Нажмите «Создать список».", edit)
            return
        version = state["version"]
        total_pages = (len(state["ids"]) + PAGE_SIZE - 1) // PAGE_SIZE
        page = max(0, min(page, total_pages-1))
        lines = [f"📋 {state.get('name', 'Список для заучивания')} · {len(state['ids'])} заданий · {page+1}/{total_pages}", ""]
        buttons = []
        for index in range(page*PAGE_SIZE, min((page+1)*PAGE_SIZE, len(state["ids"]))):
            q = self.bank[state["ids"][index]]
            progress = self.features.question_progress(update.effective_user.id, q["id"])
            status = "✅ Выучен" if progress and progress["status"] == "mastered" else "🔁 Повторить" if progress else "🆕 Новый"
            lines += [f"{index+1}. {q_label(q)} · {status} · {q['question']}", ""]
            buttons.append([InlineKeyboardButton(f"Учить: {index+1}. {q_label(q)}", callback_data=f"learn_card:{version}:{index}:0")])
        nav = []
        if page:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"learn_list:{version}:{page-1}"))
        if page+1 < total_pages:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"learn_list:{version}:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons += [[InlineKeyboardButton("📄 Скачать список с ответами TXT", callback_data=f"learn_export:{version}")],
                    [InlineKeyboardButton("⬅️ Заучивание", callback_data=f"learn_home:{version}")]]
        await self.panel(update, "\n".join(lines), InlineKeyboardMarkup(buttons), edit)

    async def learning_card(self, update, context, index, revealed=False):
        state = self.learning.get(update.effective_user.id)
        if not state["ids"]:
            await self.learning_home(update, context, "Ваш список пока пуст.", edit=True)
            return
        index = max(0, min(index, len(state["ids"])-1))
        state.update(study_index=index, revealed=bool(revealed))
        self.store.save_learning(update.effective_user.id, state)
        version = state["version"]
        q = self.bank[state["ids"][index]]
        lines = [f"📖 Заучивание · карточка {index+1}/{len(state['ids'])}",
                 f"{q_label(q)} · стр. {q['page']}", "", q["question"], ""]
        lines += source_option_lines(q)
        buttons = []
        if revealed:
            lines += ["", "✅ Ответ по ключу PDF:"]
            lines += correct_answer_lines(q)
        else:
            lines += ["", "Вспомните ответ, затем откройте его."]
            buttons.append([InlineKeyboardButton("👀 Показать ответ", callback_data=f"learn_card:{version}:{index}:1")])
        nav = []
        if index:
            nav.append(InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"learn_card:{version}:{index-1}:0"))
        if index+1 < len(state["ids"]):
            nav.append(InlineKeyboardButton("➡️ Следующая", callback_data=f"learn_card:{version}:{index+1}:0"))
        else:
            nav.append(InlineKeyboardButton("🔄 С начала", callback_data=f"learn_card:{version}:0:0"))
        buttons += [nav, [InlineKeyboardButton("⚠️ Ошибка в задании", callback_data=f"feat_report:{q['id']}")],
                    [InlineKeyboardButton("📋 К списку", callback_data=f"learn_list:{version}:{index//PAGE_SIZE}")],
                    [InlineKeyboardButton("⬅️ Заучивание", callback_data=f"learn_home:{version}")]]
        await self.panel(update, "\n".join(lines), InlineKeyboardMarkup(buttons), edit=True)

    async def learning_export(self, update, context):
        state = self.learning.get(update.effective_user.id)
        lines = [f"Личный список для заучивания · {len(state['ids'])} заданий", "Ответы — по ключам PDF.", ""]
        for qid in state["ids"]:
            q = self.bank[qid]
            lines += [f"{q_label(q)} · стр. {q['page']}", q["question"]]
            lines += source_option_lines(q)
            lines += ["Правильные варианты: " + ", ".join(q["correct"]), ""]
        await update.effective_message.reply_document(io.BytesIO("\n".join(lines).encode("utf-8-sig")), filename="my_learning_list.txt")

    async def learning_callback(self, update, context, parts):
        query = update.callback_query
        state = self.learning.get(update.effective_user.id)
        if len(parts) < 2 or parts[1] != state["version"]:
            await query.answer("Этот список уже изменился. Откройте «Заучивание» заново.")
            return
        self.features.clear_intake(update.effective_user.id)
        await query.answer()
        action = parts[0]
        try:
            if action == "learn_home":
                await self.learning_home(update, context, edit=True)
            elif action == "learn_prompt" and len(parts) == 3:
                await self.learning_prompt(update, context, parts[2])
            elif action == "learn_count" and len(parts) == 4:
                await self.learning_change(update, context, int(parts[3]), parts[2], parts[1])
            elif action == "learn_list" and len(parts) == 3:
                await self.learning_list(update, context, int(parts[2]))
            elif action == "learn_card" and len(parts) == 4:
                await self.learning_card(update, context, int(parts[2]), bool(int(parts[3])))
            elif action == "learn_quiz":
                if not state["ids"]:
                    await self.learning_home(update, context, "Сначала создайте список.", edit=True)
                    return
                old_session = self.store.state(update.effective_user.id)
                await self.remove_old_buttons(context, update.effective_chat.id, old_session)
                await self.show_question(update, context, self.quiz.new(update.effective_user.id, "learning"))
            elif action == "learn_reset":
                old_session = self.store.state(update.effective_user.id)
                if self.learning.reset(update.effective_user.id, parts[1]) is not None:
                    if old_session and old_session["mode"] == "learning":
                        await self.remove_old_buttons(context, update.effective_chat.id, old_session)
                    await self.learning_home(update, context, "🗑 Ваш список очищен. История ответов и ошибки сохранены.", edit=True)
            elif action == "learn_export":
                await self.learning_export(update, context)
            elif action == "learn_menu":
                self.learning.cancel_prompt(update.effective_user.id)
                await self.say(update, "Главное меню. Ваш список для заучивания сохранён.", self.menu(update.effective_user))
        except ValueError:
            await self.say(update, f"Введите целое число от 1 до {len(self.quiz.ids)}. /learn — раздел заучивания.")

    async def mistakes(self, update, context, page=0, edit=False):
        rows = self.store.wrong_questions(update.effective_user.id)
        if not rows:
            await self.say(update, "📚 Пока нет ошибок. Все ваши ответы будут сохранены отдельно.")
            return
        total_pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
        page = max(0, min(page, total_pages - 1))
        text = [f"📚 Ваши частые ошибки · {page+1}/{total_pages}",
                "Сначала вопросы с наибольшим числом неверных ответов.", ""]
        for row in rows[page*PAGE_SIZE:(page+1)*PAGE_SIZE]:
            q = self.bank.get(row["question_id"])
            label = q_label(q) if q else f"ID {row['question_id']}"
            title = q["question"] if q else "Вопрос удалён из базы"
            text += [f"{label}: {title[:190]}{'…' if len(title)>190 else ''}",
                     f"❌ {row['wrong']} из {row['attempts']} · ошибок {percent(row['wrong'], row['attempts'])}", ""]
        buttons = []
        nav = []
        if page:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"mistakes:{page-1}"))
        if page+1 < total_pages:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"mistakes:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("📄 Скачать полный список TXT", callback_data="export_mistakes")])
        await self.panel(update, "\n".join(text), InlineKeyboardMarkup(buttons), edit)

    async def panel(self, update, text, keyboard, edit=False):
        if edit and update.callback_query:
            try:
                await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
                return
            except BadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
        await self.say(update, text, keyboard)

    async def export_mistakes(self, update, context):
        rows = self.store.wrong_questions(update.effective_user.id)
        lines = [f"Личные ошибки пользователя {update.effective_user.id}",
                 "Сортировка: число ошибок, затем доля неверных ответов.",
                 "Правильные ответы приведены по ключу PDF.", ""]
        for row in rows:
            q = self.bank.get(row["question_id"])
            if q:
                lines += [f"{q_label(q)} · стр. {q['page']}", q["question"]]
                lines += source_option_lines(q)
                lines += ["Ответ по PDF: " + ", ".join(q["correct"])]
            else:
                lines.append(f"ID {row['question_id']}: вопрос отсутствует в текущей базе")
            lines += [f"Ошибок: {row['wrong']} / {row['attempts']}", ""]
        data = io.BytesIO("\n".join(lines).encode("utf-8-sig"))
        await update.effective_message.reply_document(data, filename="my_mistakes.txt")

    async def admin_stats(self, update, context, page=0, edit=False):
        if not self.authorized_admin(update.effective_user):
            await self.say(update, "Эта статистика доступна только администратору.")
            return
        rows = self.store.user_rows()
        total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        attempts = sum(row["attempts"] for row in rows)
        correct = sum(row["correct"] for row in rows)
        text = [f"📊 Пользователи · {page+1}/{total_pages}",
                f"Всего пользователей: {len(rows)} · ответов: {attempts}",
                f"✅ {correct} · ❌ {attempts-correct} · точность {percent(correct, attempts)}", ""]
        buttons = []
        for row in rows[page*PAGE_SIZE:(page+1)*PAGE_SIZE]:
            tag = f"@{row['username']}" if row["username"] else "Без username"
            name = " ".join(x for x in (row["first_name"], row["last_name"]) if x)[:80]
            text += [f"{name} · {tag} · ID {row['user_id']}",
                     f"Ответов {row['attempts']} · ✅ {row['correct']} · ❌ {row['wrong']} · {percent(row['correct'], row['attempts'])}", ""]
            buttons.append([InlineKeyboardButton(f"Подробнее: {tag if row['username'] else row['user_id']}",
                            callback_data=f"admin_user:{row['user_id']}:{page}")])
        nav = []
        if page:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"admin_page:{page-1}"))
        if page+1 < total_pages:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"admin_page:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("📥 Скачать CSV всех пользователей", callback_data="admin_export")])
        await self.panel(update, "\n".join(text), InlineKeyboardMarkup(buttons), edit)

    async def admin_user(self, update, context, uid, page):
        row = self.store.db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if row is None:
            await self.say(update, "Пользователь не найден.")
            return
        summary = self.store.user_summary(uid)
        tag = f"@{row['username']}" if row["username"] else "Без username"
        lines = [f"{tag} · ID {uid}", f"Ответов: {summary['attempts']}",
                 f"✅ {summary['correct']} · ❌ {summary['wrong']} · {percent(summary['correct'], summary['attempts'])}",
                 f"Разных вопросов: {summary['questions']}", "", "Самые частые ошибки:"]
        for error in self.store.wrong_questions(uid)[:5]:
            q = self.bank.get(error["question_id"])
            title = q["question"][:190] if q else "Нет в текущей базе"
            lines.append(f"{q_label(q) if q else error['question_id']}: {title}\n❌ {error['wrong']} из {error['attempts']}")
        if not self.store.wrong_questions(uid):
            lines.append("Ошибок пока нет.")
        buttons = [[InlineKeyboardButton("📥 История ответов CSV", callback_data=f"admin_answers:{uid}")],
                   [InlineKeyboardButton("⬅️ Список пользователей", callback_data=f"admin_page:{page}")]]
        await self.panel(update, "\n".join(lines), InlineKeyboardMarkup(buttons), edit=True)

    async def admin_export(self, update, context, uid=None):
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=";")
        if uid is None:
            writer.writerow(["Telegram ID", "Username", "Имя", "Ответов", "Правильно", "Ошибок", "Точность", "Разных вопросов", "Последняя активность UTC"])
            for row in self.store.user_rows():
                name = " ".join(x for x in (row["first_name"], row["last_name"]) if x)
                # Prevent names from becoming formulas when CSV is opened in Excel.
                if name[:1] in ("=", "+", "-", "@"):
                    name = "'" + name
                writer.writerow([row["user_id"], "@"+row["username"] if row["username"] else "",
                                 name, row["attempts"], row["correct"], row["wrong"],
                                 percent(row["correct"], row["attempts"]), row["questions"], row["last_seen"]])
            filename = "users_statistics.csv"
        else:
            writer.writerow(["Telegram ID", "ID задания", "Номер в PDF", "Вопрос", "Выбранные варианты", "Ключ PDF", "Правильно (1/0)", "Дата UTC", "Сессия"])
            for row in self.store.db.execute("SELECT * FROM answers WHERE user_id=? AND visible=1 ORDER BY id", (uid,)):
                q = self.bank.get(row["question_id"])
                writer.writerow([uid, row["question_id"], q_label(q) if q else "Удалён",
                                 q["question"] if q else "", ", ".join(sorted(selected_letters(q, json.loads(row["selected"])))) if q else row["selected"],
                                 ", ".join(json.loads(row["expected"])), row["is_correct"], row["answered_at"], row["session_id"]])
            filename = f"answers_{uid}.csv"
        await update.effective_message.reply_document(io.BytesIO(output.getvalue().encode("utf-8-sig")), filename=filename)

    async def callback(self, update, context):
        query = update.callback_query
        if update.effective_chat.type != "private":
            await query.answer("Откройте личный чат с ботом.")
            return
        self.store.touch(update.effective_user)
        parts = (query.data or "").split(":")
        action = parts[0]
        if action == "lt":
            await self.latin_callback(update, context, parts)
            return
        self.latin.invalidate(update.effective_user.id)
        if action.startswith("admin_") and not self.authorized_admin(update.effective_user):
            await query.answer("Только для администратора.", show_alert=True)
            return
        if action.startswith("learn_"):
            await self.learning_callback(update, context, parts)
            return
        if action.startswith("feat_"):
            await self.feature_callback(update, context, parts)
            return
        if action.startswith("exam_"):
            await self.exam_callback(update, context, parts)
            return
        if action in ("pick", "check", "next", "pause", "restart", "back", "finish_exam"):
            state = self.store.state(update.effective_user.id)
            if self.quiz.exam_expired(state):
                await query.answer("Время экзамена вышло.")
                await self.remove_old_buttons(context, update.effective_chat.id, state)
                await self.show_exam_result(update, context, self.quiz.finish_exam(update.effective_user.id, "timeout"))
                return
            if (len(parts) < 2 or not state or state["status"] != "running"
                    or state["token"] != parts[1]
                    or state["message_id"] != query.message.message_id):
                await query.answer("Эта кнопка устарела. Используйте текущий вопрос или «Продолжить».")
                return
            if action == "pick" and len(parts) == 3:
                state = self.quiz.toggle(update.effective_user.id, parts[1], parts[2])
                await query.answer("Выбор обновлён." if state else "Ответ уже проверен.")
                if state:
                    await self.show_question(update, context, state, edit=True)
            elif action == "check":
                state = self.quiz.submit(update.effective_user.id, parts[1])
                if state == "empty":
                    await query.answer("Выберите хотя бы один вариант.", show_alert=True)
                elif state == "expired":
                    await query.answer("Время экзамена вышло.")
                    await self.show_exam_result(update, context, self.store.state(update.effective_user.id))
                elif state:
                    await query.answer()
                    await self.show_question(update, context, state, edit=True)
                else:
                    await query.answer("Ответ уже проверен.")
            elif action == "next":
                if not state["answered"]:
                    await query.answer("Сначала проверьте ответ.")
                    return
                await query.answer()
                await self.remove_old_buttons(context, update.effective_chat.id, state)
                await self.show_question(update, context, self.quiz.next(update.effective_user.id))
            elif action in ("pause", "restart", "back"):
                await query.answer()
                await self.action(update, context, {"pause": STOP, "restart": AGAIN, "back": BACK}[action])
            elif action == "finish_exam" and state["mode"] == "exam":
                await query.answer()
                await self.remove_old_buttons(context, update.effective_chat.id, state)
                await self.show_exam_result(update, context, self.quiz.finish_exam(update.effective_user.id, "manual"))
            else:
                await query.answer("Неизвестная кнопка.")
            return
        await query.answer()
        try:
            if action == "mistakes" and len(parts) == 2:
                await self.mistakes(update, context, int(parts[1]), edit=True)
            elif action == "export_mistakes":
                await self.export_mistakes(update, context)
            elif action == "admin_page" and len(parts) == 2:
                await self.admin_stats(update, context, int(parts[1]), edit=True)
            elif action == "admin_user" and len(parts) == 3:
                await self.admin_user(update, context, int(parts[1]), int(parts[2]))
            elif action == "admin_export":
                await self.admin_export(update, context)
            elif action == "admin_answers" and len(parts) == 2:
                await self.admin_export(update, context, int(parts[1]))
        except ValueError:
            await self.say(update, "Некорректная кнопка. Откройте меню заново: /start.")

    async def error(self, update, context):
        LOG.error("Обработчик завершился с ошибкой типа %s", type(context.error).__name__)
        if isinstance(update, Update) and update.effective_message:
            try:
                await self.say(update, "Не удалось выполнить действие. Нажмите «Продолжить» или /start.")
            except TelegramError:
                pass


def main():
    parser = argparse.ArgumentParser(description="Telegram-бот с вопросами по анатомии")
    parser.add_argument("--check", action="store_true", help="Проверить базу без токена и соединения с Telegram")
    args = parser.parse_args()
    load_env(BASE_DIR / ".env")
    bank = load_questions()
    active = sum(q.get("enabled", True) for q in bank.values())
    print(f"База: {len(bank)} заданий; доступно {active}; отключено {len(bank)-active}.")
    if args.check:
        latin_cards = load_latin_cards()
        print(f"Латынь: {len(latin_cards)} карточек: слова, выражения и 43 параграф (66 слов).")
        for question in bank.values():
            if question.get("enabled", True):
                dummy = {"current": question["id"], "mode": "all", "seen": [question["id"]],
                         "pool": list(bank), "selected": list(question["options"]), "answered": True,
                         "token": "0"*12, "correct_count": 1, "wrong_count": 0}
                text, keyboard = question_view(bank, dummy)
                if len(text.encode("utf-16-le")) // 2 > 4096:
                    raise ValueError(f"Задание {question['id']} превышает лимит Telegram")
                for row in keyboard.inline_keyboard:
                    for button in row:
                        if len(button.callback_data.encode("utf-8")) > 64:
                            raise ValueError("Слишком длинный callback_data")
        print("Проверка пройдена: ключи, варианты, ID и лимиты сообщений.")
        return
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token or token == "PASTE_YOUR_BOT_TOKEN_HERE":
        parser.error("Укажите BOT_TOKEN в .env или переменной окружения. Инструкция: README.txt")
    admin_value = os.environ.get("ADMIN_ID", "").strip()
    if admin_value and (not admin_value.isdigit() or int(admin_value) <= 0):
        parser.error("ADMIN_ID должен быть положительным числовым Telegram ID")
    db_path = Path(os.environ.get("DB_PATH", "progress.sqlite3"))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)
    bot = Bot(store, bank, int(admin_value) if admin_value else None)

    async def shutdown(application):
        task = application.bot_data.get("background_task")
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        store.close()

    async def stopped(application):
        task = application.bot_data.get("background_task")
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def startup(application):
        me = await application.bot.get_me()
        await application.bot.set_my_commands([
            BotCommand("latin43", "Латынь, 43 параграф: роды и окончания"),
            BotCommand("latin", "Латынь: карточки и выражения"), BotCommand("start", "Главное меню"), BotCommand("learn", "Заучивание"),
            BotCommand("lists", "Мои списки"), BotCommand("due", "Повторить сегодня"),
            BotCommand("progress", "Прогресс обучения"), BotCommand("topics", "Темы анатомии"),
            BotCommand("exam", "Экзамен"), BotCommand("reminders", "Напоминания по желанию"),
            BotCommand("quiz", "Тренировка"), BotCommand("resume", "Продолжить"),
            BotCommand("again", "Заново"), BotCommand("stop", "Пауза"),
            BotCommand("mistakes", "Мои ошибки"), BotCommand("stats", "Статистика"), BotCommand("id", "Мой ID")])
        application.bot_data["background_task"] = asyncio.create_task(bot.background(application))
        print(f"Telegram подключён: @{me.username}. Остеология и латынь, включая 43 параграф. Меню: /start.", flush=True)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    application = (Application.builder().token(token).concurrent_updates(False)
                   .post_init(startup).post_stop(stopped).post_shutdown(shutdown).build())
    application.add_handler(CommandHandler(["start", "help"], bot.start))
    application.add_handler(CommandHandler(["quiz", "again", "stop", "back", "mistakes", "stats", "admin", "resume", "id", "learn",
        "lists", "due", "progress", "topics", "exam", "reminders", "reports", "latin", "latin43"], bot.command))
    application.add_handler(CallbackQueryHandler(bot.callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.message))
    application.add_error_handler(bot.error)
    print("Бот запускается. Для остановки нажмите Ctrl+C.")
    application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
