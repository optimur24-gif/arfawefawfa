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




# Bundled source modules
"""Source-faithful answer labels, including incomplete PDF rows."""


def answer_options(question):
    entries = question.get("source_options") if question.get("source_faithful") else None
    if not entries:
        entries = [{"label": key, "text": text} for key, text in question["options"].items()]
    labels = [str(entry["label"]).lower() for entry in entries]
    result = []
    for index, entry in enumerate(entries):
        label = str(entry["label"]).lower()
        letter = "абвгдежз"[int(label)-1] if label.isdigit() and 1 <= int(label) <= 8 else label
        option_id = letter if labels.count(label) == 1 else f"{letter}~{index}"
        result.append({"id": option_id, "letter": letter, "label": label,
                       "text": str(entry["text"]), "missing": False})
    # Keep the source body unchanged. The explicit empty button lets users choose
    # the PDF's literal key even when its option text was omitted in the PDF.
    present = {option["letter"] for option in result}
    for letter in question["correct"]:
        if letter not in present:
            result.append({"id": letter, "letter": letter, "label": letter,
                           "text": "В PDF текст варианта отсутствует", "missing": True})
    return result


def source_option_lines(question):
    if question.get("source_faithful") and question.get("source_options"):
        return [f"{entry['label']}) {entry['text']}".rstrip() for entry in question["source_options"]]
    return [f"{key}) {text}" for key, text in sorted(question["options"].items())]


def selected_letters(question, selected):
    mapping = {option["id"]: option["letter"] for option in answer_options(question)}
    return {mapping[key] for key in selected if key in mapping}


def is_correct_selection(question, selected):
    options = {option["id"] for option in answer_options(question)}
    return bool(selected) and set(selected) <= options and selected_letters(question, selected) == set(question["correct"])


def correct_answer_lines(question, letters=None):
    result = []
    options = answer_options(question)
    for letter in (question["correct"] if letters is None else sorted(letters)):
        matches = [option for option in options if option["letter"] == letter]
        for option in matches:
            text = option["text"] or "В PDF вариант оставлен пустым"
            label = option["label"]
            if label.isdigit():
                label += f" (ключ: {letter})"
            result.append(f"{label}) {text}")
    return result


def answer_breakdown(question, selected):
    chosen = selected_letters(question, selected)
    expected = set(question["correct"])
    return {
        "matched": sorted(chosen & expected),
        "missed": sorted(expected - chosen),
        "extra": sorted(chosen - expected),
    }


def breakdown_lines(question, selected):
    result = answer_breakdown(question, selected)
    return [
        "✅ Выбраны правильно: " + (", ".join(result["matched"]) or "—"),
        "⚠️ Пропущены: " + (", ".join(result["missed"]) or "—"),
        "❌ Лишние: " + (", ".join(result["extra"]) or "—"),
    ]


"""Persistent learning lists, spaced repetition, and question reports.

This module deliberately depends only on the Python standard library.  Each
Telegram user's lists, progress and reminders share the main bot's SQLite
connection, so an answer and its repetition schedule can commit atomically.
"""

import json
import secrets
from datetime import date, datetime, timedelta, timezone


def _moment(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if isinstance(now, date) and not isinstance(now, datetime):
        now = datetime.combine(now, datetime.min.time())
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _stamp(now=None):
    return _moment(now).isoformat(timespec="seconds")


def new_version():
    return secrets.token_hex(6)


def empty_learning():
    return {"ids": [], "version": new_version(), "pending": None,
            "study_index": 0, "revealed": False}


class FeatureStore:
    def __init__(self, store, bank, canonical):
        self.store = store
        self.db = store.db
        self.bank = bank
        self.canonical = canonical
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS learning_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                name TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS learning_lists_user ON learning_lists(user_id, id);
            CREATE TABLE IF NOT EXISTS learning_profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                active_list_id INTEGER REFERENCES learning_lists(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS question_progress (
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                question_id INTEGER NOT NULL,
                stage INTEGER NOT NULL DEFAULT 0 CHECK(stage BETWEEN 0 AND 3),
                last_correct_day TEXT, last_result INTEGER NOT NULL,
                due_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(user_id, question_id)
            );
            CREATE INDEX IF NOT EXISTS question_progress_due
                ON question_progress(user_id, due_at);
            CREATE TABLE IF NOT EXISTS feature_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                reminders_enabled INTEGER NOT NULL DEFAULT 0,
                reminder_hour_utc INTEGER NOT NULL DEFAULT 9 CHECK(reminder_hour_utc BETWEEN 0 AND 23),
                last_reminder_date TEXT
            );
            CREATE TABLE IF NOT EXISTS feature_intake (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                action TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS question_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                question_id INTEGER NOT NULL, text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
                resolved_at TEXT, resolved_by INTEGER REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS question_reports_pending ON question_reports(status, id);
            CREATE TABLE IF NOT EXISTS feature_migrations (
                name TEXT PRIMARY KEY, applied_at TEXT NOT NULL
            );
        """)
        with self.db:
            self._migrate_lists()
            self._seed_progress()

    def _migrate_lists(self):
        if self.db.execute("SELECT 1 FROM feature_migrations WHERE name='named_lists_v1'").fetchone():
            return
        legacy = self.db.execute("SELECT user_id, state, updated_at FROM learning_sets").fetchall()
        for row in legacy:
            existing = self.db.execute("SELECT id FROM learning_lists WHERE user_id=? ORDER BY id LIMIT 1",
                                       (row["user_id"],)).fetchone()
            if existing:
                lid = existing["id"]
            else:
                # Preserve the complete old JSON, including nonce and card position.
                cursor = self.db.execute("""
                    INSERT INTO learning_lists(user_id,name,state,created_at,updated_at)
                    VALUES(?, 'Мой список', ?, ?, ?)
                """, (row["user_id"], row["state"], row["updated_at"], row["updated_at"]))
                lid = cursor.lastrowid
            self.db.execute("INSERT OR IGNORE INTO learning_profiles VALUES(?,?)", (row["user_id"], lid))
        for row in self.db.execute("SELECT user_id,state FROM sessions").fetchall():
            state = json.loads(row["state"])
            if state.get("mode") == "learning" and state.get("list_id") is None:
                state["list_id"] = self._ensure_active_no_commit(row["user_id"])
                self.store._save(row["user_id"], state)
        self.db.execute("INSERT INTO feature_migrations VALUES('named_lists_v1',?)", (_stamp(),))

    def _seed_progress(self):
        if self.db.execute("SELECT 1 FROM feature_migrations WHERE name='progress_from_answers_v1'").fetchone():
            return
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(answers)")}
        visibility = " WHERE visible=1" if "visible" in columns else ""
        # Unfinished exam answers stay private until the exam is completed.
        # Legacy databases lack this column and contain ordinary visible answers.
        rows = self.db.execute(
            "SELECT user_id,question_id,is_correct,answered_at FROM answers"
            + visibility + " ORDER BY answered_at,id").fetchall()
        for row in rows:
            if row["question_id"] in self.canonical:
                self.update_progress_no_commit(row["user_id"], row["question_id"],
                                               bool(row["is_correct"]), row["answered_at"])
        self.db.execute("INSERT INTO feature_migrations VALUES('progress_from_answers_v1',?)", (_stamp(),))

    def _new_list_no_commit(self, uid, name, state=None):
        stamp = _stamp()
        cursor = self.db.execute("""
            INSERT INTO learning_lists(user_id,name,state,created_at,updated_at) VALUES(?,?,?,?,?)
        """, (uid, name, json.dumps(state if state is not None else empty_learning(), ensure_ascii=False),
              stamp, stamp))
        return cursor.lastrowid

    def _ensure_active_no_commit(self, uid):
        row = self.db.execute("""
            SELECT l.id FROM learning_profiles p JOIN learning_lists l ON l.id=p.active_list_id
            WHERE p.user_id=? AND l.user_id=?
        """, (uid, uid)).fetchone()
        if row:
            return row["id"]
        row = self.db.execute("SELECT id FROM learning_lists WHERE user_id=? ORDER BY id LIMIT 1", (uid,)).fetchone()
        lid = row["id"] if row else self._new_list_no_commit(uid, "Мой список")
        self.db.execute("""
            INSERT INTO learning_profiles VALUES(?,?)
            ON CONFLICT(user_id) DO UPDATE SET active_list_id=excluded.active_list_id
        """, (uid, lid))
        return lid

    def active_list_id(self, uid):
        with self.db:
            return self._ensure_active_no_commit(uid)

    def _list_state(self, row):
        state = json.loads(row["state"])
        state.setdefault("ids", [])
        state.setdefault("version", "")
        state.setdefault("pending", None)
        state.setdefault("study_index", 0)
        state.setdefault("revealed", False)
        state.update(list_id=row["id"], name=row["name"])
        return state

    def list_state(self, uid, lid):
        row = self.db.execute("SELECT * FROM learning_lists WHERE user_id=? AND id=?", (uid, lid)).fetchone()
        return self._list_state(row) if row else None

    def get_learning(self, uid):
        return self.list_state(uid, self.active_list_id(uid))

    def _save_list_state_no_commit(self, uid, lid, state):
        clean = dict(state)
        clean.pop("list_id", None)
        clean.pop("name", None)
        cursor = self.db.execute("""
            UPDATE learning_lists SET state=?,updated_at=? WHERE user_id=? AND id=?
        """, (json.dumps(clean, ensure_ascii=False), _stamp(), uid, lid))
        return bool(cursor.rowcount)

    def save_list_state(self, uid, lid, state):
        with self.db:
            return self._save_list_state_no_commit(uid, lid, state)

    def _save_learning_no_commit(self, uid, state):
        lid = state.get("list_id")
        if lid is None:
            lid = self._ensure_active_no_commit(uid)
        if not self._save_list_state_no_commit(uid, lid, state):
            raise ValueError("Список не найден или принадлежит другому пользователю")
        return lid

    def save_learning(self, uid, state):
        with self.db:
            self._save_learning_no_commit(uid, state)

    def lists(self, uid):
        active = self.active_list_id(uid)
        rows = self.db.execute("SELECT id,name,state FROM learning_lists WHERE user_id=? ORDER BY id", (uid,)).fetchall()
        return [{"id": row["id"], "name": row["name"],
                 "count": len(json.loads(row["state"]).get("ids", [])), "active": row["id"] == active}
                for row in rows]

    @staticmethod
    def _name(name):
        name = " ".join(str(name).split())
        if not 1 <= len(name) <= 64:
            raise ValueError("Название списка должно содержать от 1 до 64 символов")
        return name

    def create_list(self, uid, name):
        name = self._name(name)
        with self.db:
            lid = self._new_list_no_commit(uid, name)
            self.db.execute("""
                INSERT INTO learning_profiles VALUES(?,?)
                ON CONFLICT(user_id) DO UPDATE SET active_list_id=excluded.active_list_id
            """, (uid, lid))
        return self.list_state(uid, lid)

    def switch_list(self, uid, lid):
        state = self.list_state(uid, lid)
        if state is None:
            return None
        with self.db:
            self.db.execute("""
                INSERT INTO learning_profiles VALUES(?,?)
                ON CONFLICT(user_id) DO UPDATE SET active_list_id=excluded.active_list_id
            """, (uid, lid))
        return state

    def rename_list(self, uid, lid, name):
        name = self._name(name)
        state = self.list_state(uid, lid)
        if state is None:
            return False
        state["version"] = new_version()
        with self.db:
            self.db.execute("UPDATE learning_lists SET name=? WHERE user_id=? AND id=?", (name, uid, lid))
            self._save_list_state_no_commit(uid, lid, state)
        return True

    def delete_list(self, uid, lid):
        if self.list_state(uid, lid) is None:
            return False
        with self.db:
            self.db.execute("DELETE FROM learning_lists WHERE user_id=? AND id=?", (uid, lid))
            self._ensure_active_no_commit(uid)
            session = self.store.state(uid)
            if session and session.get("mode") == "learning" and session.get("list_id") == lid:
                self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        return True

    def append_question(self, uid, qid):
        if qid not in self.canonical:
            raise ValueError("Вопрос не найден")
        qid = self.canonical[qid]
        state = self.get_learning(uid)
        existing = {self.canonical.get(item, item) for item in state["ids"]}
        if qid in existing:
            return state, False
        state["ids"].append(qid)
        state.update(version=new_version(), pending=None)
        with self.db:
            self._save_learning_no_commit(uid, state)
            session = self.store.state(uid)
            if (session and session.get("mode") == "learning"
                    and session.get("list_id") == state["list_id"]
                    and session.get("status") in ("running", "paused")):
                if qid not in session["pool"]:
                    session["pool"].append(qid)
                    self.store._save(uid, session)
        return state, True

    def update_progress_no_commit(self, uid, qid, correct, now=None):
        if qid not in self.canonical:
            raise ValueError("Вопрос не найден")
        qid = self.canonical[qid]
        moment = _moment(now)
        today = moment.date().isoformat()
        row = self.db.execute("SELECT * FROM question_progress WHERE user_id=? AND question_id=?", (uid, qid)).fetchone()
        stage = row["stage"] if row else 0
        last_correct = row["last_correct_day"] if row else None
        if correct:
            if last_correct != today:
                stage = min(stage + 1, 3)
                last_correct = today
                due = (moment.date() + timedelta(days=(3, 7, 14)[stage - 1])).isoformat()
            else:
                # Repeated taps/practice in one day never advance the schedule.
                due = row["due_at"]
        else:
            stage = 0
            due = (moment.date() + timedelta(days=1)).isoformat()
        self.db.execute("""
            INSERT INTO question_progress(user_id,question_id,stage,last_correct_day,last_result,due_at,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(user_id,question_id) DO UPDATE SET stage=excluded.stage,
                last_correct_day=excluded.last_correct_day,last_result=excluded.last_result,
                due_at=excluded.due_at,updated_at=excluded.updated_at
        """, (uid, qid, stage, last_correct, int(bool(correct)), due, _stamp(moment)))
        return self.question_progress(uid, qid)

    def update_progress(self, uid, qid, correct, now=None):
        with self.db:
            return self.update_progress_no_commit(uid, qid, correct, now)

    def question_progress(self, uid, qid):
        if qid not in self.canonical:
            return None
        row = self.db.execute("SELECT * FROM question_progress WHERE user_id=? AND question_id=?",
                              (uid, self.canonical[qid])).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["status"] = "mastered" if result["stage"] >= 3 else "review"
        result["mastered"] = result["stage"] >= 3
        result["streak"] = result["stage"]
        return result

    def due_ids(self, uid, now=None):
        today = _moment(now).date().isoformat()
        valid = set(self.canonical.values())
        return [row["question_id"] for row in self.db.execute("""
            SELECT question_id FROM question_progress WHERE user_id=? AND due_at<=?
            ORDER BY due_at,question_id
        """, (uid, today)).fetchall() if row["question_id"] in valid]

    def progress(self, uid, qids=None):
        ids = (set(self.canonical.values()) if qids is None
               else {self.canonical[qid] for qid in qids if qid in self.canonical})
        rows = [row for row in self.db.execute("SELECT question_id,stage,due_at FROM question_progress WHERE user_id=?",
                                              (uid,)).fetchall() if row["question_id"] in ids]
        today = _moment().date().isoformat()
        mastered = sum(row["stage"] >= 3 for row in rows)
        return {"total": len(ids), "new": len(ids) - len(rows), "review": len(rows) - mastered,
                "mastered": mastered, "due": sum(row["due_at"] <= today for row in rows)}

    def reminder_settings(self, uid):
        row = self.db.execute("SELECT * FROM feature_settings WHERE user_id=?", (uid,)).fetchone()
        if row is None:
            return {"reminders_enabled": False, "reminder_hour_utc": 9, "last_reminder_date": None}
        result = dict(row)
        result["reminders_enabled"] = bool(result["reminders_enabled"])
        return result

    def toggle_reminders(self, uid):
        enabled = not self.reminder_settings(uid)["reminders_enabled"]
        with self.db:
            self.db.execute("""
                INSERT INTO feature_settings(user_id,reminders_enabled) VALUES(?,?)
                ON CONFLICT(user_id) DO UPDATE SET reminders_enabled=excluded.reminders_enabled
            """, (uid, int(enabled)))
        return enabled

    def reminders_due(self, now=None):
        moment = _moment(now)
        rows = self.db.execute("""
            SELECT user_id FROM feature_settings WHERE reminders_enabled=1
              AND reminder_hour_utc<=? AND (last_reminder_date IS NULL OR last_reminder_date<>?)
            ORDER BY user_id
        """, (moment.hour, moment.date().isoformat())).fetchall()
        return [row["user_id"] for row in rows if self.due_ids(row["user_id"], moment)]

    def mark_reminder(self, uid, now=None):
        with self.db:
            self.db.execute("""
                INSERT INTO feature_settings(user_id,last_reminder_date) VALUES(?,?)
                ON CONFLICT(user_id) DO UPDATE SET last_reminder_date=excluded.last_reminder_date
            """, (uid, _moment(now).date().isoformat()))

    def set_intake(self, uid, action, payload=None):
        if not isinstance(action, str) or not action.strip():
            raise ValueError("Не задано действие")
        with self.db:
            self.db.execute("""
                INSERT INTO feature_intake VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET action=excluded.action,payload=excluded.payload,
                    updated_at=excluded.updated_at
            """, (uid, action, json.dumps({} if payload is None else payload, ensure_ascii=False), _stamp()))
        return self.get_intake(uid)

    def get_intake(self, uid):
        row = self.db.execute("SELECT action,payload FROM feature_intake WHERE user_id=?", (uid,)).fetchone()
        return {"action": row["action"], "payload": json.loads(row["payload"])} if row else None

    def clear_intake(self, uid):
        with self.db:
            self.db.execute("DELETE FROM feature_intake WHERE user_id=?", (uid,))

    def add_report(self, uid, qid, text):
        if qid not in self.bank:
            raise ValueError("Вопрос не найден")
        text = str(text).strip()
        if not 1 <= len(text) <= 2000:
            raise ValueError("Замечание должно содержать от 1 до 2000 символов")
        with self.db:
            cursor = self.db.execute("""
                INSERT INTO question_reports(user_id,question_id,text,created_at) VALUES(?,?,?,?)
            """, (uid, qid, text, _stamp()))
        return cursor.lastrowid

    def reports(self, page=0, limit=8):
        if not isinstance(page, int) or page < 0 or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Некорректная страница списка")
        rows = self.db.execute("""
            SELECT r.*,u.username,u.first_name FROM question_reports r JOIN users u ON u.user_id=r.user_id
            WHERE r.status='pending' ORDER BY r.id LIMIT ? OFFSET ?
        """, (limit, page * limit)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.update(uid=item["user_id"], qid=item["question_id"])
            result.append(item)
        return result

    def resolve_report(self, rid, admin_uid):
        with self.db:
            cursor = self.db.execute("""
                UPDATE question_reports SET status='resolved',resolved_at=?,resolved_by=?
                WHERE id=? AND status='pending'
            """, (_stamp(), admin_uid, rid))
        return bool(cursor.rowcount)

    def report_count(self):
        return self.db.execute("SELECT COUNT(*) FROM question_reports WHERE status='pending'").fetchone()[0]


"""Personal learning tools and their Telegram interface.

FeatureUI is mixed into Bot.  All state is scoped to the effective Telegram user
and saved by FeatureStore; callback payloads never contain a user ID to switch to.
"""

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


BASE_DIR = Path(__file__).resolve().parent
EMBEDDED_QUESTIONS_JSON = r'''[
  {
    "id": 1,
    "page": 4,
    "page_end": 4,
    "question": "СТРУКТУРНОЙ ЕДИНИЦЕЙ КОСТИ ЯВЛЯЕТСЯ:",
    "source_options": [
      {
        "label": "а",
        "text": "Оссеин"
      },
      {
        "label": "б",
        "text": "Остеон"
      },
      {
        "label": "в",
        "text": "Красный костный мозг"
      },
      {
        "label": "г",
        "text": "Остеоцит"
      }
    ],
    "options": {
      "а": "Оссеин",
      "б": "Остеон",
      "в": "Красный костный мозг",
      "г": "Остеоцит"
    },
    "correct": [
      "б"
    ],
    "source_key": "б",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 1,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 2,
    "page": 4,
    "page_end": 4,
    "question": "НАЗОВИТЕ ЧАСТЬ КРЕСТЦА, НА КОТОРОЙ НАХОДИТСЯ FACIES AURICULARIS:",
    "source_options": [
      {
        "label": "а",
        "text": "Facies dorsalis"
      },
      {
        "label": "б",
        "text": "Basis ossis sacri"
      },
      {
        "label": "в",
        "text": "Pars lateralis"
      },
      {
        "label": "г",
        "text": "Facies pelvina"
      }
    ],
    "options": {
      "а": "Facies dorsalis",
      "б": "Basis ossis sacri",
      "в": "Pars lateralis",
      "г": "Facies pelvina"
    },
    "correct": [
      "в"
    ],
    "source_key": "в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 2,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 3,
    "page": 4,
    "page_end": 4,
    "question": "УКАЖИТЕ МЕСТО НА ПЕРВОМ РЕБРЕ, ГДЕ ПРОХОДИТ SULCUS ARTERIAE SUBCLAVIAE:",
    "source_options": [
      {
        "label": "а",
        "text": "Впереди от бугорка ребра"
      },
      {
        "label": "б",
        "text": "Позади от бугорка передней лестничной мышцы"
      },
      {
        "label": "в",
        "text": "Впереди от бугорка передней лестничной мышцы"
      },
      {
        "label": "г",
        "text": "На бугорке ребра"
      }
    ],
    "options": {
      "а": "Впереди от бугорка ребра",
      "б": "Позади от бугорка передней лестничной мышцы",
      "в": "Впереди от бугорка передней лестничной мышцы",
      "г": "На бугорке ребра"
    },
    "correct": [
      "б"
    ],
    "source_key": "б",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 3,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 4,
    "page": 4,
    "page_end": 4,
    "question": "УКАЖИТЕ МЕСТО РАСПОЛОЖЕНИЯ АNGULUS STERNI",
    "source_options": [
      {
        "label": "а",
        "text": "Место соединения рукоятки с телом"
      },
      {
        "label": "б",
        "text": "Место соединения тела с мечевидным отростком"
      },
      {
        "label": "в",
        "text": "На уровне середины тела"
      },
      {
        "label": "г",
        "text": "На уровне яремной вырезки гpудины"
      }
    ],
    "options": {
      "а": "Место соединения рукоятки с телом",
      "б": "Место соединения тела с мечевидным отростком",
      "в": "На уровне середины тела",
      "г": "На уровне яремной вырезки гpудины"
    },
    "correct": [
      "а"
    ],
    "source_key": "а",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 4,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 5,
    "page": 4,
    "page_end": 4,
    "question": "АНАТОМИЧЕСКОЕ ОБРАЗОВАНИЕ, РАЗДЕЛЯЮЩЕЕ БОЛЬШУЮ И МАЛУЮ СЕДАЛИЩНЫЕ ВЫРЕЗКИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Tuber ischiadicum"
      },
      {
        "label": "б",
        "text": "Tuberculum pubicum"
      },
      {
        "label": "в",
        "text": "Spina iliaca posterior inferior"
      },
      {
        "label": "г",
        "text": "Spina ischiadica"
      }
    ],
    "options": {
      "а": "Tuber ischiadicum",
      "б": "Tuberculum pubicum",
      "в": "Spina iliaca posterior inferior",
      "г": "Spina ischiadica"
    },
    "correct": [
      "г"
    ],
    "source_key": "г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 5,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 6,
    "page": 4,
    "page_end": 4,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, ПРИМЫКАЮЩИЕ К ТУРЕЦКОМУ СЕДЛУ СБОКУ:",
    "source_options": [
      {
        "label": "а",
        "text": "Fossa hypophysialis"
      },
      {
        "label": "б",
        "text": "Canalis opticus"
      },
      {
        "label": "в",
        "text": "Sulcus caroticus"
      },
      {
        "label": "г",
        "text": "Processus clinoidei posteriores"
      }
    ],
    "options": {
      "а": "Fossa hypophysialis",
      "б": "Canalis opticus",
      "в": "Sulcus caroticus",
      "г": "Processus clinoidei posteriores"
    },
    "correct": [
      "в"
    ],
    "source_key": "в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 6,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 7,
    "page": 4,
    "page_end": 4,
    "question": "УКАЖИТЕ КУДА ОТКРЫВАЕТСЯ APERTURA SINUS SPHENOIDALIS:",
    "source_options": [
      {
        "label": "а",
        "text": "Meatus nasi medius"
      },
      {
        "label": "б",
        "text": "Meatus nasi superior"
      },
      {
        "label": "в",
        "text": "Fossa pterygopalatina"
      },
      {
        "label": "г",
        "text": "Fossa cranii media"
      }
    ],
    "options": {
      "а": "Meatus nasi medius",
      "б": "Meatus nasi superior",
      "в": "Fossa pterygopalatina",
      "г": "Fossa cranii media"
    },
    "correct": [
      "б"
    ],
    "source_key": "б",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 7,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 8,
    "page": 4,
    "page_end": 4,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКОЕ ОБРАЗОВАНИЕ, В КОТОРОЕ ОТКРЫВАЮТСЯ ПЕРЕДНИЕ И СРЕДНИЕ ЯЧЕЙКИ РЕШЕТЧАТОЙ КОСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Orbita"
      },
      {
        "label": "б",
        "text": "Meatus nasi communis"
      },
      {
        "label": "в",
        "text": "Fossa cranii anterior"
      },
      {
        "label": "г",
        "text": "Meatus nasi medius"
      }
    ],
    "options": {
      "а": "Orbita",
      "б": "Meatus nasi communis",
      "в": "Fossa cranii anterior",
      "г": "Meatus nasi medius"
    },
    "correct": [
      "г"
    ],
    "source_key": "г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 8,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 9,
    "page": 5,
    "page_end": 5,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА ВЕРХУШКЕ ПИРАМИДЫ ВИСОЧНОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Apertura externa canalis carotici"
      },
      {
        "label": "б",
        "text": "Porus acusticus internus"
      },
      {
        "label": "в",
        "text": "Apertura interna canalis carotici"
      },
      {
        "label": "г",
        "text": "Canalis pterygoideus"
      }
    ],
    "options": {
      "а": "Apertura externa canalis carotici",
      "б": "Porus acusticus internus",
      "в": "Apertura interna canalis carotici",
      "г": "Canalis pterygoideus"
    },
    "correct": [
      "в"
    ],
    "source_key": "в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 9,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 10,
    "page": 5,
    "page_end": 5,
    "question": "УКАЖИТЕ ВЫХОДНОЕ ОТВЕРСТИЕ САNАLIS NЕRVI FACIALIS",
    "source_options": [
      {
        "label": "а",
        "text": "Hiatus canalis nervi petrosi majores"
      },
      {
        "label": "б",
        "text": "Porus acusticus internus"
      },
      {
        "label": "в",
        "text": "Foramen stylomastoideum"
      },
      {
        "label": "г",
        "text": "Fissura petrosquamosa"
      }
    ],
    "options": {
      "а": "Hiatus canalis nervi petrosi majores",
      "б": "Porus acusticus internus",
      "в": "Foramen stylomastoideum",
      "г": "Fissura petrosquamosa"
    },
    "correct": [
      "в"
    ],
    "source_key": "в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 10,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 11,
    "page": 5,
    "page_end": 5,
    "question": "НАЗОВИТЕ ОТВЕРСТИЕ, СООБЩАЮЩЕЕ КРЫЛОВИДНОНЁБНУЮ ЯМКУ С ПОЛОСТЬЮ НОСА:",
    "source_options": [
      {
        "label": "а",
        "text": "Foramen rotundum"
      },
      {
        "label": "б",
        "text": "Foramen sphenopalatinum"
      },
      {
        "label": "в",
        "text": "Foгamen ovale"
      },
      {
        "label": "г",
        "text": "Fissura pterygomaxillaris"
      }
    ],
    "options": {
      "а": "Foramen rotundum",
      "б": "Foramen sphenopalatinum",
      "в": "Foгamen ovale",
      "г": "Fissura pterygomaxillaris"
    },
    "correct": [
      "б"
    ],
    "source_key": "б",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 11,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 12,
    "page": 5,
    "page_end": 5,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКОЕ ОБРАЗОВАНИЕ, СООБЩАЮЩЕЕ КРЫЛОВИДНОНЁБНУЮ ЯМКУ С ПОЛОСТЬЮ РТА:",
    "source_options": [
      {
        "label": "а",
        "text": "Foramen sphenopalatinum"
      },
      {
        "label": "б",
        "text": "Саnаlis pterygoideus"
      },
      {
        "label": "в",
        "text": "Foramen rotundum"
      },
      {
        "label": "г",
        "text": "Canalis palatinus major"
      }
    ],
    "options": {
      "а": "Foramen sphenopalatinum",
      "б": "Саnаlis pterygoideus",
      "в": "Foramen rotundum",
      "г": "Canalis palatinus major"
    },
    "correct": [
      "г"
    ],
    "source_key": "г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 12,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 13,
    "page": 5,
    "page_end": 5,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, СООБЩАЮЩИЕ КРЫЛОВИДНОНЁБНУЮ ЯМКУ СО СРЕДНЕЙ ЧЕРЕПНОЙ ЯМКОЙ:",
    "source_options": [
      {
        "label": "а",
        "text": "Foramen rotundum"
      },
      {
        "label": "б",
        "text": "Canalis palatinus major"
      },
      {
        "label": "в",
        "text": "Canalis pterygoideus"
      },
      {
        "label": "г",
        "text": "Fоrаmеn sphenopalatinum"
      }
    ],
    "options": {
      "а": "Foramen rotundum",
      "б": "Canalis palatinus major",
      "в": "Canalis pterygoideus",
      "г": "Fоrаmеn sphenopalatinum"
    },
    "correct": [
      "а"
    ],
    "source_key": "а",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 13,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 633,
    "page": 72,
    "page_end": 72,
    "question": "ОСНОВНЫЕ ФУНКЦИИ СКЕЛЕТА ЧЕЛОВЕКА ЗАКЛЮЧАЮТСЯ В:",
    "source_options": [
      {
        "label": "а",
        "text": "Депо минеральных веществ"
      },
      {
        "label": "б",
        "text": "Опорная"
      },
      {
        "label": "в",
        "text": "Защитная"
      },
      {
        "label": "г",
        "text": "Локомоторная"
      }
    ],
    "options": {
      "а": "Депо минеральных веществ",
      "б": "Опорная",
      "в": "Защитная",
      "г": "Локомоторная"
    },
    "correct": [
      "а",
      "б",
      "в",
      "г"
    ],
    "source_key": "а,б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 633,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 634,
    "page": 72,
    "page_end": 72,
    "question": "УКАЖИТЕ СОСТАВНЫЕ ЧАСТИ ПОЗВОНКОВ",
    "source_options": [
      {
        "label": "а",
        "text": "Processus articularis"
      },
      {
        "label": "б",
        "text": "Arcus"
      },
      {
        "label": "в",
        "text": "Processus coronoideus"
      },
      {
        "label": "г",
        "text": "Corpus"
      }
    ],
    "options": {
      "а": "Processus articularis",
      "б": "Arcus",
      "в": "Processus coronoideus",
      "г": "Corpus"
    },
    "correct": [
      "а",
      "б",
      "г"
    ],
    "source_key": "а,б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 634,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 635,
    "page": 72,
    "page_end": 72,
    "question": "УКАЖИТЕ, КАКИЕ ОТРОСТКИ ИМЕЮТ ПОЗВОНКИ",
    "source_options": [
      {
        "label": "а",
        "text": "Processus styloideus"
      },
      {
        "label": "б",
        "text": "Processus sрinоsus"
      },
      {
        "label": "в",
        "text": "Processus articularis superior"
      },
      {
        "label": "г",
        "text": "Processus transversus"
      }
    ],
    "options": {
      "а": "Processus styloideus",
      "б": "Processus sрinоsus",
      "в": "Processus articularis superior",
      "г": "Processus transversus"
    },
    "correct": [
      "б",
      "в",
      "г"
    ],
    "source_key": "б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 635,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 636,
    "page": 72,
    "page_end": 72,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, ХАРАКТЕРНЫЕ ДЛЯ ТИПИЧНЫХ ШЕЙНЫХ ПОЗВОНКОВ",
    "source_options": [
      {
        "label": "а",
        "text": "Foramen processus transversus"
      },
      {
        "label": "б",
        "text": "Tuberculum caroticum"
      },
      {
        "label": "в",
        "text": "Processus spinosus (раздвоенный на конце)"
      },
      {
        "label": "г",
        "text": "Processus mamillaris"
      }
    ],
    "options": {
      "а": "Foramen processus transversus",
      "б": "Tuberculum caroticum",
      "в": "Processus spinosus (раздвоенный на конце)",
      "г": "Processus mamillaris"
    },
    "correct": [
      "а",
      "в"
    ],
    "source_key": "а,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 636,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 637,
    "page": 72,
    "page_end": 72,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ 1 ШЕЙНОГО ПОЗВОНКА",
    "source_options": [
      {
        "label": "а",
        "text": "Massae laterales"
      },
      {
        "label": "б",
        "text": "Processus accessorius"
      },
      {
        "label": "в",
        "text": "Fovea dentis"
      },
      {
        "label": "г",
        "text": "Arcus posterior"
      }
    ],
    "options": {
      "а": "Massae laterales",
      "б": "Processus accessorius",
      "в": "Fovea dentis",
      "г": "Arcus posterior"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 637,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 638,
    "page": 72,
    "page_end": 72,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ 2 ШЕЙНОГО ПОЗВОНКА",
    "source_options": [
      {
        "label": "а",
        "text": "Arcus anterior"
      },
      {
        "label": "б",
        "text": "Corpus"
      },
      {
        "label": "в",
        "text": "Dens"
      },
      {
        "label": "г",
        "text": "Facies articularis anterior"
      }
    ],
    "options": {
      "а": "Arcus anterior",
      "б": "Corpus",
      "в": "Dens",
      "г": "Facies articularis anterior"
    },
    "correct": [
      "б",
      "в",
      "г"
    ],
    "source_key": "б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 638,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 639,
    "page": 72,
    "page_end": 72,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ ХАРАКТЕРНЫЕ ДЛЯ ТИПИЧНЫХ ГPУДНЫХ ПОЗВОНКОВ:",
    "source_options": [
      {
        "label": "а",
        "text": "Foveae costales superiores еt inferiores"
      },
      {
        "label": "б",
        "text": "Processus costotransversarius"
      },
      {
        "label": "в",
        "text": "Foveae costalis processus transversus"
      },
      {
        "label": "г",
        "text": "Processus accessorius"
      }
    ],
    "options": {
      "а": "Foveae costales superiores еt inferiores",
      "б": "Processus costotransversarius",
      "в": "Foveae costalis processus transversus",
      "г": "Processus accessorius"
    },
    "correct": [
      "а",
      "в"
    ],
    "source_key": "а,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 639,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 640,
    "page": 72,
    "page_end": 72,
    "question": "ГРУДНЫЕ ПОЗВОНКИ, ИМЕЮЩИЕ НА ТЕЛЕ ПОЛНЫЕ РЕБЕРНЫЕ ЯМКИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Тh12"
      },
      {
        "label": "б",
        "text": "Th10"
      },
      {
        "label": "в",
        "text": "Th1"
      },
      {
        "label": "г",
        "text": "Th11"
      }
    ],
    "options": {
      "а": "Тh12",
      "б": "Th10",
      "в": "Th1",
      "г": "Th11"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 640,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 641,
    "page": 73,
    "page_end": 73,
    "question": "УКАЖИТЕ ОТРОСТКИ, ИМЕЮЩИЕСЯ ТОЛЬКО У ПОЯСНИЧНЫХ ПОЗВОНКОВ",
    "source_options": [
      {
        "label": "а",
        "text": "Processus transversus"
      },
      {
        "label": "б",
        "text": "Processus accessorius"
      },
      {
        "label": "в",
        "text": "Processus articulares superiores еt inferiores"
      },
      {
        "label": "г",
        "text": "Processus mamillaris"
      }
    ],
    "options": {
      "а": "Processus transversus",
      "б": "Processus accessorius",
      "в": "Processus articulares superiores еt inferiores",
      "г": "Processus mamillaris"
    },
    "correct": [
      "б",
      "г"
    ],
    "source_key": "б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 641,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 642,
    "page": 73,
    "page_end": 73,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ ДОРСАЛЬНОЙ ПОВЕРХНОСТИ КРЕСТЦА",
    "source_options": [
      {
        "label": "а",
        "text": "Crista sacralis mediana"
      },
      {
        "label": "б",
        "text": "Linae transversae"
      },
      {
        "label": "в",
        "text": "Canalis sacralis"
      },
      {
        "label": "г",
        "text": "Hiatus sacralis"
      }
    ],
    "options": {
      "а": "Crista sacralis mediana",
      "б": "Linae transversae",
      "в": "Canalis sacralis",
      "г": "Hiatus sacralis"
    },
    "correct": [
      "а",
      "г"
    ],
    "source_key": "а,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 642,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 643,
    "page": 73,
    "page_end": 73,
    "question": "УКАЖИТЕ АНОМАЛИИ ПЕРЕХОДНЫХ ОТДЕЛОВ ПОЗВОНОЧНОГО СТОЛБА",
    "source_options": [
      {
        "label": "а",
        "text": "Ассимиляция атланта"
      },
      {
        "label": "б",
        "text": "Спондилолиз"
      },
      {
        "label": "в",
        "text": "Сакрализация"
      },
      {
        "label": "г",
        "text": "Люмбализация"
      }
    ],
    "options": {
      "а": "Ассимиляция атланта",
      "б": "Спондилолиз",
      "в": "Сакрализация",
      "г": "Люмбализация"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 643,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 644,
    "page": 73,
    "page_end": 73,
    "question": "УКАЖИТЕ ЧАСТИ РЕБРА",
    "source_options": [
      {
        "label": "а",
        "text": "Caput"
      },
      {
        "label": "б",
        "text": "Collum"
      },
      {
        "label": "в",
        "text": "Corpus"
      },
      {
        "label": "г",
        "text": "Cartilago costalis"
      }
    ],
    "options": {
      "а": "Caput",
      "б": "Collum",
      "в": "Corpus",
      "г": "Cartilago costalis"
    },
    "correct": [
      "а",
      "б",
      "в",
      "г"
    ],
    "source_key": "а,б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 644,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 645,
    "page": 73,
    "page_end": 73,
    "question": "УКАЖИТЕ ЧАСТИ ГРУДИНЫ",
    "source_options": [
      {
        "label": "а",
        "text": "Corpus"
      },
      {
        "label": "б",
        "text": "Processus styloideus"
      },
      {
        "label": "в",
        "text": "Мanubrium"
      },
      {
        "label": "г",
        "text": "Incisura clavicularis"
      }
    ],
    "options": {
      "а": "Corpus",
      "б": "Processus styloideus",
      "в": "Мanubrium",
      "г": "Incisura clavicularis"
    },
    "correct": [
      "а",
      "в"
    ],
    "source_key": "а,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 645,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 646,
    "page": 73,
    "page_end": 73,
    "question": "АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ НАХОДЯЩИЕСЯ НА РУКОЯТКЕ ГРУДИНЫ:",
    "source_options": [
      {
        "label": "а",
        "text": "Facies costalis"
      },
      {
        "label": "б",
        "text": "Incisura jugularis"
      },
      {
        "label": "в",
        "text": "Incisura clavicularis"
      },
      {
        "label": "г",
        "text": "lncisura costalis"
      }
    ],
    "options": {
      "а": "Facies costalis",
      "б": "Incisura jugularis",
      "в": "Incisura clavicularis",
      "г": "lncisura costalis"
    },
    "correct": [
      "б",
      "в",
      "г"
    ],
    "source_key": "б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 646,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 647,
    "page": 73,
    "page_end": 73,
    "question": "УКАЖИТЕ ОСНОВНЫЕ ОТДЕЛЫ SKELETON MEMBRI SUPERIORIS",
    "source_options": [
      {
        "label": "а",
        "text": "Cingulum"
      },
      {
        "label": "б",
        "text": "Brachium"
      },
      {
        "label": "в",
        "text": "Manus"
      },
      {
        "label": "г",
        "text": "Skеlеtоп membri superioris liberi"
      }
    ],
    "options": {
      "а": "Cingulum",
      "б": "Brachium",
      "в": "Manus",
      "г": "Skеlеtоп membri superioris liberi"
    },
    "correct": [
      "а",
      "г"
    ],
    "source_key": "а,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 647,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 648,
    "page": 73,
    "page_end": 73,
    "question": "УКАЖИТЕ ОТДЕЛЫ SKELETON MEMBRI SUPERIORIS IIBERI",
    "source_options": [
      {
        "label": "а",
        "text": "Antebrachium"
      },
      {
        "label": "б",
        "text": "Humerus"
      },
      {
        "label": "в",
        "text": "Manus"
      },
      {
        "label": "г",
        "text": "Brachium"
      }
    ],
    "options": {
      "а": "Antebrachium",
      "б": "Humerus",
      "в": "Manus",
      "г": "Brachium"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 648,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 649,
    "page": 73,
    "page_end": 73,
    "question": "КОСТИ, ФОРМИРУЮЩИЕ CINGULUM MEMBRI SUPERIORIS:",
    "source_options": [
      {
        "label": "а",
        "text": "Sternum"
      },
      {
        "label": "б",
        "text": "Clavicula"
      },
      {
        "label": "в",
        "text": "Humerus"
      },
      {
        "label": "г",
        "text": "Scapula"
      }
    ],
    "options": {
      "а": "Sternum",
      "б": "Clavicula",
      "в": "Humerus",
      "г": "Scapula"
    },
    "correct": [
      "б",
      "г"
    ],
    "source_key": "б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 649,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 650,
    "page": 73,
    "page_end": 74,
    "question": "АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА ДОРСАЛЬНОЙ ПОВЕРХНОСТИ ЛОПАТКИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Processus acгomialis"
      },
      {
        "label": "б",
        "text": "Fossa supraspinata"
      },
      {
        "label": "в",
        "text": "Processus coracoideus"
      },
      {
        "label": "г",
        "text": "Spina scapulae"
      }
    ],
    "options": {
      "а": "Processus acгomialis",
      "б": "Fossa supraspinata",
      "в": "Processus coracoideus",
      "г": "Spina scapulae"
    },
    "correct": [
      "а",
      "б",
      "г"
    ],
    "source_key": "а,б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 650,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 651,
    "page": 74,
    "page_end": 74,
    "question": "АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ В ОБЛАСТИ ЛАТЕРАЛЬНОГО УГЛА ЛОПАТКИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Facies articularis acromialis"
      },
      {
        "label": "б",
        "text": "Fossa infraspinata"
      },
      {
        "label": "в",
        "text": "Cavitas glenoidalis"
      },
      {
        "label": "г",
        "text": "Tuberculum supraglenoidale"
      }
    ],
    "options": {
      "а": "Facies articularis acromialis",
      "б": "Fossa infraspinata",
      "в": "Cavitas glenoidalis",
      "г": "Tuberculum supraglenoidale"
    },
    "correct": [
      "в",
      "г"
    ],
    "source_key": "в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 651,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 652,
    "page": 74,
    "page_end": 74,
    "question": "АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА АКРОМИАЛЬНОМ КОНЦЕ КЛЮЧИЦЫ:",
    "source_options": [
      {
        "label": "а",
        "text": "Facies articularis acromialis"
      },
      {
        "label": "б",
        "text": "Tuberculum conoideum"
      },
      {
        "label": "в",
        "text": "Linea trapezoidea"
      },
      {
        "label": "г",
        "text": "Facies articularis stemalis"
      }
    ],
    "options": {
      "а": "Facies articularis acromialis",
      "б": "Tuberculum conoideum",
      "в": "Linea trapezoidea",
      "г": "Facies articularis stemalis"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 652,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 653,
    "page": 74,
    "page_end": 74,
    "question": "АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, РАСПОЛОЖЕННЫЕ НА ПРОКСИМАЛЬНОМ КОНЦЕ ПЛЕЧЕВОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Collum anatomicum"
      },
      {
        "label": "б",
        "text": "Epicondylus lateralis"
      },
      {
        "label": "в",
        "text": "Sulcus intertubercularis"
      },
      {
        "label": "г",
        "text": "Caput humeri"
      }
    ],
    "options": {
      "а": "Collum anatomicum",
      "б": "Epicondylus lateralis",
      "в": "Sulcus intertubercularis",
      "г": "Caput humeri"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 653,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 654,
    "page": 74,
    "page_end": 74,
    "question": "УКАЖИТЕ ПОВЕРХНОСТЬ ТЕЛА ПЛЕЧЕВОЙ КОСТИ ПО КОТОРОЙ ПРОХОДИТ SULCUS NERVI RADIALIS:",
    "source_options": [
      {
        "label": "а",
        "text": "Facies medialis"
      },
      {
        "label": "б",
        "text": "Facies lateralis"
      },
      {
        "label": "в",
        "text": "Facies anterior"
      },
      {
        "label": "г",
        "text": "Facies postetior"
      }
    ],
    "options": {
      "а": "Facies medialis",
      "б": "Facies lateralis",
      "в": "Facies anterior",
      "г": "Facies postetior"
    },
    "correct": [
      "б",
      "г"
    ],
    "source_key": "б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 654,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 655,
    "page": 74,
    "page_end": 74,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ ДИСТАЛЬНОГО ЭПИФИЗА ПЛЕЧЕВОЙ КОСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Trochlea humeri"
      },
      {
        "label": "б",
        "text": "Tuberculum majus"
      },
      {
        "label": "в",
        "text": "Sulcus nervi ulnaris"
      },
      {
        "label": "г",
        "text": "Fossa olecrani"
      }
    ],
    "options": {
      "а": "Trochlea humeri",
      "б": "Tuberculum majus",
      "в": "Sulcus nervi ulnaris",
      "г": "Fossa olecrani"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 655,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 656,
    "page": 74,
    "page_end": 74,
    "question": "КОСТИ, ОБРАЗУЮЩИЕ СКЕЛЕТ ПРЕДПЛЕЧЬЯ:",
    "source_options": [
      {
        "label": "а",
        "text": "Radius"
      },
      {
        "label": "б",
        "text": "Humerus"
      },
      {
        "label": "в",
        "text": "Fibula"
      },
      {
        "label": "г",
        "text": "Ulna"
      }
    ],
    "options": {
      "а": "Radius",
      "б": "Humerus",
      "в": "Fibula",
      "г": "Ulna"
    },
    "correct": [
      "а",
      "г"
    ],
    "source_key": "а,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 656,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 657,
    "page": 74,
    "page_end": 74,
    "question": "НАЗОВИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ НА ПРОКСИМАЛЬНОМ КОНЦЕ ЛОКТЕВОЙ КОСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Olecranon"
      },
      {
        "label": "б",
        "text": "Caput"
      },
      {
        "label": "в",
        "text": "Incisura radialis"
      },
      {
        "label": "г",
        "text": "Incisura trochlearis"
      }
    ],
    "options": {
      "а": "Olecranon",
      "б": "Caput",
      "в": "Incisura radialis",
      "г": "Incisura trochlearis"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 657,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 658,
    "page": 74,
    "page_end": 74,
    "question": "АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА ДИСТАЛЬНОМ КОНЦЕ ЛУЧЕВОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Collum"
      },
      {
        "label": "б",
        "text": "Caput"
      },
      {
        "label": "в",
        "text": "Incisura ulnaris"
      },
      {
        "label": "г",
        "text": "Processus styloideus"
      }
    ],
    "options": {
      "а": "Collum",
      "б": "Caput",
      "в": "Incisura ulnaris",
      "г": "Processus styloideus"
    },
    "correct": [
      "в",
      "г"
    ],
    "source_key": "в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 658,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 659,
    "page": 74,
    "page_end": 74,
    "question": "КОСТИ ВЕРХНЕЙ КОНЕЧНОСТИ, ИМЕЮЩИЕ СУСТАВНУЮ ОКРУЖНОСТЬ:",
    "source_options": [
      {
        "label": "а",
        "text": "Humerus"
      },
      {
        "label": "б",
        "text": "Ulna"
      },
      {
        "label": "в",
        "text": "Clavicula"
      },
      {
        "label": "г",
        "text": "Radius"
      }
    ],
    "options": {
      "а": "Humerus",
      "б": "Ulna",
      "в": "Clavicula",
      "г": "Radius"
    },
    "correct": [
      "б",
      "г"
    ],
    "source_key": "б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 659,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 660,
    "page": 75,
    "page_end": 75,
    "question": "КОСТИ, ИМЕЮЩИЕ ШИЛОВИДНЫЙ ОТРОСТОК:",
    "source_options": [
      {
        "label": "а",
        "text": "Os hamatum"
      },
      {
        "label": "б",
        "text": "Humerus"
      },
      {
        "label": "в",
        "text": "Ulna"
      },
      {
        "label": "г",
        "text": "Radius"
      }
    ],
    "options": {
      "а": "Os hamatum",
      "б": "Humerus",
      "в": "Ulna",
      "г": "Radius"
    },
    "correct": [
      "в",
      "г"
    ],
    "source_key": "в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 660,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 661,
    "page": 75,
    "page_end": 75,
    "question": "УКАЖИТЕ КАКИЕ ЧАСТИ ВЫДЕЛЯЮТ В СКЕЛЕТЕ КИСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Metacarpus"
      },
      {
        "label": "б",
        "text": "Tarsus"
      },
      {
        "label": "в",
        "text": "Carpus"
      },
      {
        "label": "г",
        "text": "Phalanges digitorum manus"
      }
    ],
    "options": {
      "а": "Metacarpus",
      "б": "Tarsus",
      "в": "Carpus",
      "г": "Phalanges digitorum manus"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 661,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 662,
    "page": 75,
    "page_end": 75,
    "question": "КОСТИ, ВХОДЯЩИЕ В ДИСТАЛЬНЫЙ РЯД КОСТЕЙ ЗАПЯСТЬЯ:",
    "source_options": [
      {
        "label": "а",
        "text": "Os trapezoideum"
      },
      {
        "label": "б",
        "text": "Os lunаtum"
      },
      {
        "label": "в",
        "text": "Os capitatum"
      },
      {
        "label": "г",
        "text": "Os hamatum"
      }
    ],
    "options": {
      "а": "Os trapezoideum",
      "б": "Os lunаtum",
      "в": "Os capitatum",
      "г": "Os hamatum"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 662,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 663,
    "page": 75,
    "page_end": 75,
    "question": "УКАЖИТЕ ЧАСТИ ПЯСТНЫХ КОСТЕЙ",
    "source_options": [
      {
        "label": "а",
        "text": "Basis"
      },
      {
        "label": "б",
        "text": "Collum"
      },
      {
        "label": "в",
        "text": "Corpus"
      },
      {
        "label": "г",
        "text": "Caput"
      }
    ],
    "options": {
      "а": "Basis",
      "б": "Collum",
      "в": "Corpus",
      "г": "Caput"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 663,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 664,
    "page": 75,
    "page_end": 75,
    "question": "УКАЖИТЕ ОСНОВНЫЕ ОТДЕЛЫ СКЕЛЕТА НИЖНЕЙ КОНЕЧНОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Femur"
      },
      {
        "label": "б",
        "text": "Pes"
      },
      {
        "label": "в",
        "text": "Cinglllum"
      },
      {
        "label": "г",
        "text": "Skeleton membri inferioris liberi"
      }
    ],
    "options": {
      "а": "Femur",
      "б": "Pes",
      "в": "Cinglllum",
      "г": "Skeleton membri inferioris liberi"
    },
    "correct": [
      "в",
      "г"
    ],
    "source_key": "в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 664,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 665,
    "page": 75,
    "page_end": 75,
    "question": "КОСТИ, ФОРМИРУЮЩИЕ OS СОХАЕ:",
    "source_options": [
      {
        "label": "а",
        "text": "Os pubis"
      },
      {
        "label": "б",
        "text": "Os sacrum"
      },
      {
        "label": "в",
        "text": "Os ischii"
      },
      {
        "label": "г",
        "text": "Os ilium"
      }
    ],
    "options": {
      "а": "Os pubis",
      "б": "Os sacrum",
      "в": "Os ischii",
      "г": "Os ilium"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 665,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 666,
    "page": 75,
    "page_end": 75,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, ПРИНАДЛЕЖАЩИЕ ПОДВЗДОШНОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Sulcus obturatorius"
      },
      {
        "label": "б",
        "text": "Facies auricularis"
      },
      {
        "label": "в",
        "text": "Facies symphysialis"
      },
      {
        "label": "г",
        "text": "Ala osis ilii"
      }
    ],
    "options": {
      "а": "Sulcus obturatorius",
      "б": "Facies auricularis",
      "в": "Facies symphysialis",
      "г": "Ala osis ilii"
    },
    "correct": [
      "б",
      "г"
    ],
    "source_key": "б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 666,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 667,
    "page": 75,
    "page_end": 75,
    "question": "АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА ПОДВЗДОШНОМ ГРЕБНЕ:",
    "source_options": [
      {
        "label": "а",
        "text": "Tuberositas iliaca"
      },
      {
        "label": "б",
        "text": "Spina iliaca anterior superior"
      },
      {
        "label": "в",
        "text": "Spina iliaca posterior inferior"
      },
      {
        "label": "г",
        "text": "Linea arcuata"
      }
    ],
    "options": {
      "а": "Tuberositas iliaca",
      "б": "Spina iliaca anterior superior",
      "в": "Spina iliaca posterior inferior",
      "г": "Linea arcuata"
    },
    "correct": [
      "б",
      "в"
    ],
    "source_key": "б,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 667,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 668,
    "page": 75,
    "page_end": 75,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ ВЕРТЛУЖНОЙ ВПАДИНЫ",
    "source_options": [
      {
        "label": "а",
        "text": "Facies lunata"
      },
      {
        "label": "б",
        "text": "Fossa acetabuli"
      },
      {
        "label": "в",
        "text": "lncisura acetabuli"
      },
      {
        "label": "г",
        "text": "Fovea capitis femoris"
      }
    ],
    "options": {
      "а": "Facies lunata",
      "б": "Fossa acetabuli",
      "в": "lncisura acetabuli",
      "г": "Fovea capitis femoris"
    },
    "correct": [
      "а",
      "б",
      "в"
    ],
    "source_key": "а,б,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 668,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 669,
    "page": 75,
    "page_end": 75,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ OS PUBIS",
    "source_options": [
      {
        "label": "а",
        "text": "Tuberculum pubicum"
      },
      {
        "label": "б",
        "text": "Sulcus obturatorius"
      },
      {
        "label": "в",
        "text": "Eminentia iliopubica"
      },
      {
        "label": "г",
        "text": "Crista pubica"
      }
    ],
    "options": {
      "а": "Tuberculum pubicum",
      "б": "Sulcus obturatorius",
      "в": "Eminentia iliopubica",
      "г": "Crista pubica"
    },
    "correct": [
      "а",
      "б",
      "в",
      "г"
    ],
    "source_key": "а,б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 669,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 670,
    "page": 76,
    "page_end": 76,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА ПРОКСИМАЛЬНОМ ЭПИФИЗЕ БЕДРЕННОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Trochanter major"
      },
      {
        "label": "б",
        "text": "Condylus medialis"
      },
      {
        "label": "в",
        "text": "Linea pectinea"
      },
      {
        "label": "г",
        "text": "Linea intertrochanterica"
      }
    ],
    "options": {
      "а": "Trochanter major",
      "б": "Condylus medialis",
      "в": "Linea pectinea",
      "г": "Linea intertrochanterica"
    },
    "correct": [
      "а",
      "г"
    ],
    "source_key": "а,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 670,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 671,
    "page": 76,
    "page_end": 76,
    "question": "УКАЖИТЕ ЧАСТИ ШЕРОХОВАТОЙ ЛИНИИ БЕДРЕННОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Tuberositas glutea"
      },
      {
        "label": "б",
        "text": "Labium mediale"
      },
      {
        "label": "в",
        "text": "Linea pectinea"
      },
      {
        "label": "г",
        "text": "Labium laterale"
      }
    ],
    "options": {
      "а": "Tuberositas glutea",
      "б": "Labium mediale",
      "в": "Linea pectinea",
      "г": "Labium laterale"
    },
    "correct": [
      "б",
      "г"
    ],
    "source_key": "б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 671,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 672,
    "page": 76,
    "page_end": 76,
    "question": "АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА ДИСТАЛЬНОМ ЭПИФИЗЕ БЕДРЕННОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Epicondylus lateralis"
      },
      {
        "label": "б",
        "text": "Condylus la!eralis"
      },
      {
        "label": "в",
        "text": "Facies poplitea"
      },
      {
        "label": "г",
        "text": "Facies patellaris"
      }
    ],
    "options": {
      "а": "Epicondylus lateralis",
      "б": "Condylus la!eralis",
      "в": "Facies poplitea",
      "г": "Facies patellaris"
    },
    "correct": [
      "а",
      "б",
      "в",
      "г"
    ],
    "source_key": "а,б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 672,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 673,
    "page": 76,
    "page_end": 76,
    "question": "УКАЖИТЕ, КАКИЕ КОСТИ НИЖНЕЙ КОНЕЧНОСТИ ИМЕЮТ ЛОДЫЖКИ",
    "source_options": [
      {
        "label": "а",
        "text": "Tibia"
      },
      {
        "label": "б",
        "text": "Talus"
      },
      {
        "label": "в",
        "text": "Fibula"
      },
      {
        "label": "г",
        "text": "Calcaneus"
      }
    ],
    "options": {
      "а": "Tibia",
      "б": "Talus",
      "в": "Fibula",
      "г": "Calcaneus"
    },
    "correct": [
      "а",
      "в"
    ],
    "source_key": "а,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 673,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 674,
    "page": 76,
    "page_end": 76,
    "question": "АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА ПРОКСИМАЛЬНОМ ЭПИФИЗЕ БОЛЬШЕБЕРЦОВОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Агеа intercondylaris anterior"
      },
      {
        "label": "б",
        "text": "Facies articularis fibularis"
      },
      {
        "label": "в",
        "text": "Incisura fibularis"
      },
      {
        "label": "г",
        "text": "Eminentia intercondylaris"
      }
    ],
    "options": {
      "а": "Агеа intercondylaris anterior",
      "б": "Facies articularis fibularis",
      "в": "Incisura fibularis",
      "г": "Eminentia intercondylaris"
    },
    "correct": [
      "а",
      "б",
      "г"
    ],
    "source_key": "а,б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 674,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 675,
    "page": 76,
    "page_end": 76,
    "question": "КОСТИ ПРЕДПЛЮСНЫ, ОБРАЗУЮЩИЕ ЕЕ ПРОКСИМАЛЬНЫЙ РЯД:",
    "source_options": [
      {
        "label": "а",
        "text": "Os cuneiforme mediale"
      },
      {
        "label": "б",
        "text": "Os naviculare"
      },
      {
        "label": "в",
        "text": "Calcaneus"
      },
      {
        "label": "г",
        "text": "Talus"
      }
    ],
    "options": {
      "а": "Os cuneiforme mediale",
      "б": "Os naviculare",
      "в": "Calcaneus",
      "г": "Talus"
    },
    "correct": [
      "в",
      "г"
    ],
    "source_key": "в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 675,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 676,
    "page": 76,
    "page_end": 76,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА ТАРАННОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Trochlea"
      },
      {
        "label": "б",
        "text": "Facies malleolaris medialis"
      },
      {
        "label": "в",
        "text": "Caput"
      },
      {
        "label": "г",
        "text": "Facies articularis calcanea posterior"
      }
    ],
    "options": {
      "а": "Trochlea",
      "б": "Facies malleolaris medialis",
      "в": "Caput",
      "г": "Facies articularis calcanea posterior"
    },
    "correct": [
      "а",
      "б",
      "в",
      "г"
    ],
    "source_key": "а,б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 676,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 677,
    "page": 76,
    "page_end": 76,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА ПЯТОЧНОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Facies malleolaris lateralis"
      },
      {
        "label": "б",
        "text": "Sustentaculum tali"
      },
      {
        "label": "в",
        "text": "Facies articularis talaris media"
      },
      {
        "label": "г",
        "text": "Facies anicularis navicularis"
      }
    ],
    "options": {
      "а": "Facies malleolaris lateralis",
      "б": "Sustentaculum tali",
      "в": "Facies articularis talaris media",
      "г": "Facies anicularis navicularis"
    },
    "correct": [
      "б",
      "в"
    ],
    "source_key": "б,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 677,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 678,
    "page": 76,
    "page_end": 76,
    "question": "УКАЖИТЕ КОСТИ, УЧАСТВУЮЩИЕ В ФОРМИРОВАНИИ CRANIUM CEREBRALE",
    "source_options": [
      {
        "label": "а",
        "text": "Os sphenoidale"
      },
      {
        "label": "б",
        "text": "Os occipitale"
      },
      {
        "label": "в",
        "text": "Os frontale"
      },
      {
        "label": "г",
        "text": "Os parietale"
      }
    ],
    "options": {
      "а": "Os sphenoidale",
      "б": "Os occipitale",
      "в": "Os frontale",
      "г": "Os parietale"
    },
    "correct": [
      "а",
      "б",
      "в",
      "г"
    ],
    "source_key": "а,б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 678,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 679,
    "page": 77,
    "page_end": 77,
    "question": "УКАЖИТЕ КОСТИ ЧЕРЕПА, ИМЕЮЩИЕ ВОЗДУХОНОСНЫЕ ПАЗУХИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Mandibula"
      },
      {
        "label": "б",
        "text": "Os sphenoidale"
      },
      {
        "label": "в",
        "text": "Os frontale"
      },
      {
        "label": "г",
        "text": "Maxilla"
      }
    ],
    "options": {
      "а": "Mandibula",
      "б": "Os sphenoidale",
      "в": "Os frontale",
      "г": "Maxilla"
    },
    "correct": [
      "б",
      "в",
      "г"
    ],
    "source_key": "б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 679,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 680,
    "page": 77,
    "page_end": 77,
    "question": "УКАЖИТЕ ОСНОВНЫЕ ЧАСТИ ЛОБНОЙ КОСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Squama"
      },
      {
        "label": "б",
        "text": "Sinus frontalis"
      },
      {
        "label": "в",
        "text": "Pars orbitalis"
      },
      {
        "label": "г",
        "text": "Pars nasalis"
      }
    ],
    "options": {
      "а": "Squama",
      "б": "Sinus frontalis",
      "в": "Pars orbitalis",
      "г": "Pars nasalis"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 680,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 681,
    "page": 77,
    "page_end": 77,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ ВНУТРЕННЕЙ ПОВЕРХНОСТИ ЧЕШУИ ЛОБНОЙ КОСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Sulcus sinus sagittalis superioris"
      },
      {
        "label": "б",
        "text": "Sulcus sinus sigmoidei"
      },
      {
        "label": "в",
        "text": "Crista galli"
      },
      {
        "label": "г",
        "text": "Foveolae granulares"
      }
    ],
    "options": {
      "а": "Sulcus sinus sagittalis superioris",
      "б": "Sulcus sinus sigmoidei",
      "в": "Crista galli",
      "г": "Foveolae granulares"
    },
    "correct": [
      "а",
      "г"
    ],
    "source_key": "а,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 681,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 682,
    "page": 77,
    "page_end": 77,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ НАРУЖНОЙ ПОВЕРХНОСТИ ЧЕШУИ ЛОБНОЙ КОСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Arcus superciliaris"
      },
      {
        "label": "б",
        "text": "Incisura ethmoidalis"
      },
      {
        "label": "в",
        "text": "Glabella"
      },
      {
        "label": "г",
        "text": "Linea temporalis"
      }
    ],
    "options": {
      "а": "Arcus superciliaris",
      "б": "Incisura ethmoidalis",
      "в": "Glabella",
      "г": "Linea temporalis"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 682,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 683,
    "page": 77,
    "page_end": 77,
    "question": "УКАЖИТЕ ОСНОВНЫЕ ЧАСТИ ЗАТЫЛОЧНОЙ КОСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Рагs basilaris"
      },
      {
        "label": "б",
        "text": "Clivus"
      },
      {
        "label": "в",
        "text": "Squama occipitalis"
      },
      {
        "label": "г",
        "text": "Foramen magnum"
      }
    ],
    "options": {
      "а": "Рагs basilaris",
      "б": "Clivus",
      "в": "Squama occipitalis",
      "г": "Foramen magnum"
    },
    "correct": [
      "а",
      "в"
    ],
    "source_key": "а,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 683,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 684,
    "page": 77,
    "page_end": 77,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ ВНУТРЕННЕЙ ПОВЕРХНОСТИ ЧЕШУИ ЗАТЫЛОЧНОЙ КОСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Sulcus sinus petrosi inferioris"
      },
      {
        "label": "б",
        "text": "Sulcus sinus transversus"
      },
      {
        "label": "в",
        "text": "Linea nuchae superior"
      },
      {
        "label": "г",
        "text": "Sulcus sinus sagittalis superiores"
      }
    ],
    "options": {
      "а": "Sulcus sinus petrosi inferioris",
      "б": "Sulcus sinus transversus",
      "в": "Linea nuchae superior",
      "г": "Sulcus sinus sagittalis superiores"
    },
    "correct": [
      "б",
      "г"
    ],
    "source_key": "б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 684,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 685,
    "page": 77,
    "page_end": 77,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА БОКОВЫХ ЧАСТЯХ ЗАТЫЛОЧНОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "lncisura jugularis"
      },
      {
        "label": "б",
        "text": "Sulcus sinus sigmoidei"
      },
      {
        "label": "в",
        "text": "Condylus occipitalis"
      },
      {
        "label": "г",
        "text": "Canalis hypoglossalis"
      }
    ],
    "options": {
      "а": "lncisura jugularis",
      "б": "Sulcus sinus sigmoidei",
      "в": "Condylus occipitalis",
      "г": "Canalis hypoglossalis"
    },
    "correct": [
      "а",
      "б",
      "в",
      "г"
    ],
    "source_key": "а,б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 685,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 686,
    "page": 77,
    "page_end": 77,
    "question": "НАЗОВИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ НА ВНУТРЕННЕЙ ПОВЕРХНОСТИ ТЕМЕННОЙ КОСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Linea temporalis"
      },
      {
        "label": "б",
        "text": "Тubег parietale"
      },
      {
        "label": "в",
        "text": "Sulci arteriosi"
      },
      {
        "label": "г",
        "text": "Sulcus sinus sagittalis superioris"
      }
    ],
    "options": {
      "а": "Linea temporalis",
      "б": "Тubег parietale",
      "в": "Sulci arteriosi",
      "г": "Sulcus sinus sagittalis superioris"
    },
    "correct": [
      "в",
      "г"
    ],
    "source_key": "в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 686,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 687,
    "page": 77,
    "page_end": 77,
    "question": "УКАЖИТЕ ОСНОВНЫЕ ЧАСТИ КЛИНОВИДНОЙ КОСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Ala major"
      },
      {
        "label": "б",
        "text": "Processus pterygoideus"
      },
      {
        "label": "в",
        "text": "Sinus sphenoidalis"
      },
      {
        "label": "г",
        "text": "Ala minor"
      }
    ],
    "options": {
      "а": "Ala major",
      "б": "Processus pterygoideus",
      "в": "Sinus sphenoidalis",
      "г": "Ala minor"
    },
    "correct": [
      "а",
      "б",
      "г"
    ],
    "source_key": "а,б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 687,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 688,
    "page": 78,
    "page_end": 78,
    "question": "УКАЖИТЕ ОСНОВНЫЕ ЧАСТИ РЕШЕТЧАТОЙ КОСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Lamina cribrosa"
      },
      {
        "label": "б",
        "text": "Labyrinthus ethmoidalis"
      },
      {
        "label": "в",
        "text": "Lamina perpendicularis"
      },
      {
        "label": "г",
        "text": "Cellulae ethmoidales"
      }
    ],
    "options": {
      "а": "Lamina cribrosa",
      "б": "Labyrinthus ethmoidalis",
      "в": "Lamina perpendicularis",
      "г": "Cellulae ethmoidales"
    },
    "correct": [
      "а",
      "б",
      "в"
    ],
    "source_key": "а,б,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 688,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 689,
    "page": 78,
    "page_end": 78,
    "question": "НАЗОВИТЕ НОСОВЫЕ РАКОВИНЫ, ЯВЛЯЮЩИМИСЯ ОБРАЗОВАНИЯМИ РЕШЕТЧАТОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Concha nasalis superior"
      },
      {
        "label": "б",
        "text": "Concha nasalis inferior"
      },
      {
        "label": "в",
        "text": "Concha nasalis media"
      },
      {
        "label": "г",
        "text": "Concha nasalis suprema"
      }
    ],
    "options": {
      "а": "Concha nasalis superior",
      "б": "Concha nasalis inferior",
      "в": "Concha nasalis media",
      "г": "Concha nasalis suprema"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 689,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 690,
    "page": 78,
    "page_end": 78,
    "question": "УКАЖИТЕ ОСНОВНЫЕ ЧАСТИ ВИСОЧНОЙ КОСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Pars petrosa"
      },
      {
        "label": "б",
        "text": "Processus mastoideus"
      },
      {
        "label": "в",
        "text": "Pars tympanica"
      },
      {
        "label": "г",
        "text": "Pars squamosa"
      }
    ],
    "options": {
      "а": "Pars petrosa",
      "б": "Processus mastoideus",
      "в": "Pars tympanica",
      "г": "Pars squamosa"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 690,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 691,
    "page": 78,
    "page_end": 78,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ НА ПЕРЕДНЕЙ ПОВЕРХНОСТИ ПИРАМИДЫ ВИСОЧНОЙ КОСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Eminentia аrcuata"
      },
      {
        "label": "б",
        "text": "Impressio trigemini"
      },
      {
        "label": "в",
        "text": "Fossula petrosa"
      },
      {
        "label": "г",
        "text": "Sulcus sinus petrosi infeгioris"
      }
    ],
    "options": {
      "а": "Eminentia аrcuata",
      "б": "Impressio trigemini",
      "в": "Fossula petrosa",
      "г": "Sulcus sinus petrosi infeгioris"
    },
    "correct": [
      "а",
      "б"
    ],
    "source_key": "а,б",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 691,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 692,
    "page": 78,
    "page_end": 78,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА ЗАДНЕЙ ПОВЕРХНОСТИ ПИРАМИДЫ ВИСОЧНОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Tegmen tympani"
      },
      {
        "label": "б",
        "text": "Sulcus sinus sigmoidei"
      },
      {
        "label": "в",
        "text": "Apertura externa agueduclus vestibuli"
      },
      {
        "label": "г",
        "text": "Porus acusticus internus"
      }
    ],
    "options": {
      "а": "Tegmen tympani",
      "б": "Sulcus sinus sigmoidei",
      "в": "Apertura externa agueduclus vestibuli",
      "г": "Porus acusticus internus"
    },
    "correct": [
      "в",
      "г"
    ],
    "source_key": "в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 692,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 693,
    "page": 78,
    "page_end": 78,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА НИЖНЕЙ ПОВЕРХНОСТИ ПИРАМИДЫ:",
    "source_options": [
      {
        "label": "а",
        "text": "Fossa subarcuata"
      },
      {
        "label": "б",
        "text": "Apertura inferior canaliculi tympanici"
      },
      {
        "label": "в",
        "text": "Foramen jugulare"
      },
      {
        "label": "г",
        "text": "Apertura externa canalis carotici"
      }
    ],
    "options": {
      "а": "Fossa subarcuata",
      "б": "Apertura inferior canaliculi tympanici",
      "в": "Foramen jugulare",
      "г": "Apertura externa canalis carotici"
    },
    "correct": [
      "б",
      "г"
    ],
    "source_key": "б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 693,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 694,
    "page": 78,
    "page_end": 78,
    "question": "УКАЖИТЕ ОТРОСТКИ, КОТОРЫЕ ИМЕЕТ ВИСОЧНАЯ КОСТЬ:",
    "source_options": [
      {
        "label": "а",
        "text": "Processus frontalis"
      },
      {
        "label": "б",
        "text": "Processus zygomaticus"
      },
      {
        "label": "в",
        "text": "Processus pterygoideus"
      },
      {
        "label": "г",
        "text": "Processus mastoideus"
      }
    ],
    "options": {
      "а": "Processus frontalis",
      "б": "Processus zygomaticus",
      "в": "Processus pterygoideus",
      "г": "Processus mastoideus"
    },
    "correct": [
      "б",
      "г"
    ],
    "source_key": "б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 694,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 695,
    "page": 78,
    "page_end": 78,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА СОСЦЕВИДНОМ ОТРОСТКЕ:",
    "source_options": [
      {
        "label": "а",
        "text": "Sulcus arteriae occipitalis"
      },
      {
        "label": "б",
        "text": "Sulcus sinus petrosi inferioris"
      },
      {
        "label": "в",
        "text": "Impressio trigemini"
      },
      {
        "label": "г",
        "text": "Incisura mastoidea"
      }
    ],
    "options": {
      "а": "Sulcus arteriae occipitalis",
      "б": "Sulcus sinus petrosi inferioris",
      "в": "Impressio trigemini",
      "г": "Incisura mastoidea"
    },
    "correct": [
      "а",
      "г"
    ],
    "source_key": "а,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 695,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 696,
    "page": 78,
    "page_end": 78,
    "question": "УКАЖИТЕ КАНАЛЫ, ПРОХОДЯЩИЕ ЧЕРЕЗ ПИРАМИДУ ВИСОЧНОЙ КОСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Canalis caroticus"
      },
      {
        "label": "б",
        "text": "Canalis opticus"
      },
      {
        "label": "в",
        "text": "Canalis facialis"
      },
      {
        "label": "г",
        "text": "Canaliculus mastoideus"
      }
    ],
    "options": {
      "а": "Canalis caroticus",
      "б": "Canalis opticus",
      "в": "Canalis facialis",
      "г": "Canaliculus mastoideus"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 696,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 697,
    "page": 79,
    "page_end": 79,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ ПЕРЕДНЕЙ ПОВЕРХНОСТИ ТЕЛА ВЕРХНЕЙ ЧЕЛЮСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Fossa canina"
      },
      {
        "label": "б",
        "text": "Sulcus infraorbitalis"
      },
      {
        "label": "в",
        "text": "Foramen infraorbitale"
      },
      {
        "label": "г",
        "text": "Tuber maxillae"
      }
    ],
    "options": {
      "а": "Fossa canina",
      "б": "Sulcus infraorbitalis",
      "в": "Foramen infraorbitale",
      "г": "Tuber maxillae"
    },
    "correct": [
      "а",
      "в"
    ],
    "source_key": "а,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 697,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 698,
    "page": 79,
    "page_end": 79,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА PARS ALVEOLARIS MANDIBULAE:",
    "source_options": [
      {
        "label": "а",
        "text": "Alveoli dentales"
      },
      {
        "label": "б",
        "text": "Septa interalveolaria"
      },
      {
        "label": "в",
        "text": "Septa interradicularia"
      },
      {
        "label": "г",
        "text": "Juga alveolaria"
      }
    ],
    "options": {
      "а": "Alveoli dentales",
      "б": "Septa interalveolaria",
      "в": "Septa interradicularia",
      "г": "Juga alveolaria"
    },
    "correct": [
      "а",
      "б",
      "в",
      "г"
    ],
    "source_key": "а,б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 698,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 699,
    "page": 79,
    "page_end": 79,
    "question": "УКАЖИТЕ ОТРОСТКИ ВЕРХНЕЙ ЧЕЛЮСТИ",
    "source_options": [
      {
        "label": "а",
        "text": "Processus palatinus"
      },
      {
        "label": "б",
        "text": "Processus pyramidalis"
      },
      {
        "label": "в",
        "text": "Processus frontalis"
      },
      {
        "label": "г",
        "text": "Processus orbitalis"
      }
    ],
    "options": {
      "а": "Processus palatinus",
      "б": "Processus pyramidalis",
      "в": "Processus frontalis",
      "г": "Processus orbitalis"
    },
    "correct": [
      "а",
      "в"
    ],
    "source_key": "а,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 699,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 700,
    "page": 79,
    "page_end": 79,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА ТЕЛЕ НИЖНЕЙ ЧЕЛЮСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Foгamen mandibulae"
      },
      {
        "label": "б",
        "text": "Spina mentalis"
      },
      {
        "label": "в",
        "text": "Fossa digastrica"
      },
      {
        "label": "г",
        "text": "Linea mylochyoidea"
      }
    ],
    "options": {
      "а": "Foгamen mandibulae",
      "б": "Spina mentalis",
      "в": "Fossa digastrica",
      "г": "Linea mylochyoidea"
    },
    "correct": [
      "б",
      "в",
      "г"
    ],
    "source_key": "б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 700,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 701,
    "page": 79,
    "page_end": 79,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, НАХОДЯЩИЕСЯ НА ВЕТВИ НИЖНЕЙ ЧЕЛЮСТИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Tuberositas pterygoidea"
      },
      {
        "label": "б",
        "text": "Foгamen mentale"
      },
      {
        "label": "в",
        "text": "Processus coronoideus"
      },
      {
        "label": "г",
        "text": "Sulcus mylohyoideus"
      }
    ],
    "options": {
      "а": "Tuberositas pterygoidea",
      "б": "Foгamen mentale",
      "в": "Processus coronoideus",
      "г": "Sulcus mylohyoideus"
    },
    "correct": [
      "а",
      "в"
    ],
    "source_key": "а,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 701,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 702,
    "page": 79,
    "page_end": 79,
    "question": "НАЗОВИТЕ КОСТИ, ПРИНИМАЮЩИЕ УЧАСТИЕ В ОБРАЗОВАНИИ FOSSA CRANIL ANTERIOR:",
    "source_options": [
      {
        "label": "а",
        "text": "Os frontale"
      },
      {
        "label": "б",
        "text": "Os ethmoidale"
      },
      {
        "label": "в",
        "text": "Os parietale"
      },
      {
        "label": "г",
        "text": "Os sphenoidale"
      }
    ],
    "options": {
      "а": "Os frontale",
      "б": "Os ethmoidale",
      "в": "Os parietale",
      "г": "Os sphenoidale"
    },
    "correct": [
      "а",
      "б",
      "г"
    ],
    "source_key": "а,б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 702,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 703,
    "page": 79,
    "page_end": 79,
    "question": "НАЗОВИТЕ ОТВЕРСТИЯ, НАХОДЯЩИЕСЯ НА ДНЕ FOSSA CRANII MEDIA:",
    "source_options": [
      {
        "label": "а",
        "text": "Foгamen spinosum"
      },
      {
        "label": "б",
        "text": "Porus acusticus internus"
      },
      {
        "label": "в",
        "text": "Foramen lacerum"
      },
      {
        "label": "г",
        "text": "Foramen jugulare"
      }
    ],
    "options": {
      "а": "Foгamen spinosum",
      "б": "Porus acusticus internus",
      "в": "Foramen lacerum",
      "г": "Foramen jugulare"
    },
    "correct": [
      "а",
      "в"
    ],
    "source_key": "а,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 703,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 704,
    "page": 79,
    "page_end": 79,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, СООБЩАЮЩИЕ FOSSA CRANII MEDIA С ГЛАЗНИЦЕЙ:",
    "source_options": [
      {
        "label": "а",
        "text": "Саnаlis opticus"
      },
      {
        "label": "б",
        "text": "Fissuгa orbitalis inferior"
      },
      {
        "label": "в",
        "text": "Fissura orbitalis superior"
      },
      {
        "label": "г",
        "text": "Foramen ovale"
      }
    ],
    "options": {
      "а": "Саnаlis opticus",
      "б": "Fissuгa orbitalis inferior",
      "в": "Fissura orbitalis superior",
      "г": "Foramen ovale"
    },
    "correct": [
      "а",
      "в"
    ],
    "source_key": "а,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 704,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 705,
    "page": 79,
    "page_end": 79,
    "question": "НАЗОВИТЕ ОТВЕРСТИЯ, КОТОРЫЕ СООБЩАЮТ ЗАДНЮЮ ЧЕРЕПНУЮ ЯМКУ С НАРУЖНЫМ ОСНОВАНИЕМ ЧЕРЕПА:",
    "source_options": [
      {
        "label": "а",
        "text": "Foramen ovale"
      },
      {
        "label": "б",
        "text": "Foгamen jugulare"
      },
      {
        "label": "в",
        "text": "Apertuгa extema aquaeductus vestibuli"
      },
      {
        "label": "г",
        "text": "Foгamen mаgnum"
      }
    ],
    "options": {
      "а": "Foramen ovale",
      "б": "Foгamen jugulare",
      "в": "Apertuгa extema aquaeductus vestibuli",
      "г": "Foгamen mаgnum"
    },
    "correct": [
      "б",
      "г"
    ],
    "source_key": "б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 705,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 706,
    "page": 80,
    "page_end": 80,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, ФОРМИРУЮЩИЕ СТЕНКИ FOSSA INFRATEMPORALLS:",
    "source_options": [
      {
        "label": "а",
        "text": "Lamina lateralis processus pterygoideus"
      },
      {
        "label": "б",
        "text": "Lamina medialis processus pterygoideus"
      },
      {
        "label": "в",
        "text": "Tuber maxillae"
      },
      {
        "label": "г",
        "text": "Ramus mandibulae"
      }
    ],
    "options": {
      "а": "Lamina lateralis processus pterygoideus",
      "б": "Lamina medialis processus pterygoideus",
      "в": "Tuber maxillae",
      "г": "Ramus mandibulae"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 706,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 707,
    "page": 80,
    "page_end": 80,
    "question": "НАЗОВИТЕ ОТВЕРСТИЯ, КОТОРЫЕ СООБЩАЮТ FOSSA INFRATEMPORALIS С СОСЕДНИМИ ПОЛОСТЯМИ ЧЕРЕПА:",
    "source_options": [
      {
        "label": "а",
        "text": "Fissuгa pterygomaxillaris"
      },
      {
        "label": "б",
        "text": "Fissuгa orbitalis inferior"
      },
      {
        "label": "в",
        "text": "Foгamen sphenopalatinum"
      },
      {
        "label": "г",
        "text": "Fissuгa orbitalis superior"
      }
    ],
    "options": {
      "а": "Fissuгa pterygomaxillaris",
      "б": "Fissuгa orbitalis inferior",
      "в": "Foгamen sphenopalatinum",
      "г": "Fissuгa orbitalis superior"
    },
    "correct": [
      "а",
      "б"
    ],
    "source_key": "а,б",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 707,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 708,
    "page": 80,
    "page_end": 80,
    "question": "УКАЖИТЕ АНАТОМИЧЕСКИЕ ОБРАЗОВАНИЯ, ФОРМИРУЮЩИЕ СТЕНКИ КРЫЛОВИДНОНЕБНОЙ ЯМКИ:",
    "source_options": [
      {
        "label": "а",
        "text": "Lamina perpendicularis os palatini"
      },
      {
        "label": "б",
        "text": "Tuber maxillae"
      },
      {
        "label": "в",
        "text": "Processus pterygoideus"
      },
      {
        "label": "г",
        "text": "Ramus mandibulae"
      }
    ],
    "options": {
      "а": "Lamina perpendicularis os palatini",
      "б": "Tuber maxillae",
      "в": "Processus pterygoideus",
      "г": "Ramus mandibulae"
    },
    "correct": [
      "а",
      "б",
      "в"
    ],
    "source_key": "а,б,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 708,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 709,
    "page": 80,
    "page_end": 80,
    "question": "НАЗОВИТЕ КОСТИ, УЧАСТВУЮЩИЕ В ОБРАЗОВАНИИ PARIES INFERIOR ORBITAE:",
    "source_options": [
      {
        "label": "а",
        "text": "Махillа"
      },
      {
        "label": "б",
        "text": "Os palatinum"
      },
      {
        "label": "в",
        "text": "Os sphenoidale"
      },
      {
        "label": "г",
        "text": "Os lacrimale"
      }
    ],
    "options": {
      "а": "Махillа",
      "б": "Os palatinum",
      "в": "Os sphenoidale",
      "г": "Os lacrimale"
    },
    "correct": [
      "а",
      "б"
    ],
    "source_key": "а,б",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 709,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 710,
    "page": 80,
    "page_end": 80,
    "question": "НАЗОВИТЕ КОСТИ, УЧАСТВУЮЩИЕ В ОБРАЗОВАНИИ PARIES LATERALIS ORBITAE:",
    "source_options": [
      {
        "label": "а",
        "text": "Os zygomaticum"
      },
      {
        "label": "б",
        "text": "Os sphenoidale"
      },
      {
        "label": "в",
        "text": "Махillа"
      },
      {
        "label": "г",
        "text": "Os frontale"
      }
    ],
    "options": {
      "а": "Os zygomaticum",
      "б": "Os sphenoidale",
      "в": "Махillа",
      "г": "Os frontale"
    },
    "correct": [
      "а",
      "б",
      "г"
    ],
    "source_key": "а,б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 710,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 711,
    "page": 80,
    "page_end": 80,
    "question": "НАЗОВИТЕ КОСТИ, УЧАСТВУЮЩИЕ В ОБРАЗОВАНИИ PARLES MEDIALLS ORBITAE:",
    "source_options": [
      {
        "label": "а",
        "text": "Os ethmoidale"
      },
      {
        "label": "б",
        "text": "Os sphenoidale"
      },
      {
        "label": "в",
        "text": "Махillа"
      },
      {
        "label": "г",
        "text": "Os lacrimale"
      }
    ],
    "options": {
      "а": "Os ethmoidale",
      "б": "Os sphenoidale",
      "в": "Махillа",
      "г": "Os lacrimale"
    },
    "correct": [
      "а",
      "б",
      "в",
      "г"
    ],
    "source_key": "а,б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 711,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 712,
    "page": 80,
    "page_end": 80,
    "question": "НАЗОВИТЕ ОТВЕРСТИЯ, КОТОРЫЕ ИМЕЮТСЯ В ГЛАЗНИЦЕ:",
    "source_options": [
      {
        "label": "а",
        "text": "Foramen ethmoidale anterius"
      },
      {
        "label": "б",
        "text": "Саnаlis opticus"
      },
      {
        "label": "в",
        "text": "Foramen ethmoidale posterius"
      },
      {
        "label": "г",
        "text": "Fоramen zygomaticoorbitale"
      }
    ],
    "options": {
      "а": "Foramen ethmoidale anterius",
      "б": "Саnаlis opticus",
      "в": "Foramen ethmoidale posterius",
      "г": "Fоramen zygomaticoorbitale"
    },
    "correct": [
      "а",
      "б",
      "в",
      "г"
    ],
    "source_key": "а,б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 712,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 713,
    "page": 80,
    "page_end": 80,
    "question": "НАЗОВИТЕ КОСТИ, УЧАСТВУЮЩИЕ В ОБРАЗОВАНИИ КОСТНОЙ ПЕРЕГОРОДКИ НОСА:",
    "source_options": [
      {
        "label": "а",
        "text": "Os nasale"
      },
      {
        "label": "б",
        "text": "Vоmег"
      },
      {
        "label": "в",
        "text": "Os lacrimale"
      },
      {
        "label": "г",
        "text": "Os ethmoidale"
      }
    ],
    "options": {
      "а": "Os nasale",
      "б": "Vоmег",
      "в": "Os lacrimale",
      "г": "Os ethmoidale"
    },
    "correct": [
      "б",
      "г"
    ],
    "source_key": "б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 713,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 714,
    "page": 80,
    "page_end": 80,
    "question": "НАЗОВИТЕ КОСТИ, УЧАСТВУЮЩИЕ В ОБРАЗОВАНИИ ЛАТЕРАЛЬНОЙ СТЕНКИ ПОЛОСТИ НОСА:",
    "source_options": [
      {
        "label": "а",
        "text": "Os lacrimale"
      },
      {
        "label": "б",
        "text": "Os zygomaticum"
      },
      {
        "label": "в",
        "text": "Os ethmoidale"
      },
      {
        "label": "г",
        "text": "Os palatinum"
      }
    ],
    "options": {
      "а": "Os lacrimale",
      "б": "Os zygomaticum",
      "в": "Os ethmoidale",
      "г": "Os palatinum"
    },
    "correct": [
      "а",
      "в",
      "г"
    ],
    "source_key": "а,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 714,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 715,
    "page": 81,
    "page_end": 81,
    "question": "УКАЖИТЕ ВОЗДУХОНОСНЫЕ ПАЗУХИ, КОТОРЫЕ ОТКРЫВАЮТСЯ В СРЕДНИЙ НОСОВОЙ ХОД:",
    "source_options": [
      {
        "label": "а",
        "text": "Sinus sphenoidalis"
      },
      {
        "label": "б",
        "text": "Sinus frontalis"
      },
      {
        "label": "в",
        "text": "Cellulae ethmoidales anteriores et mediae"
      },
      {
        "label": "г",
        "text": "Cellulae ethmoidales posteriores"
      }
    ],
    "options": {
      "а": "Sinus sphenoidalis",
      "б": "Sinus frontalis",
      "в": "Cellulae ethmoidales anteriores et mediae",
      "г": "Cellulae ethmoidales posteriores"
    },
    "correct": [
      "б",
      "в"
    ],
    "source_key": "б,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 715,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 716,
    "page": 81,
    "page_end": 81,
    "question": "НАЗОВИТЕ КОСТИ, ОБРАЗУЮЩИЕ КОСТНОЕ НЕБО:",
    "source_options": [
      {
        "label": "а",
        "text": "Vomer"
      },
      {
        "label": "б",
        "text": "Os palatinum"
      },
      {
        "label": "в",
        "text": "Os hyoideum"
      },
      {
        "label": "г",
        "text": "Махillа"
      }
    ],
    "options": {
      "а": "Vomer",
      "б": "Os palatinum",
      "в": "Os hyoideum",
      "г": "Махillа"
    },
    "correct": [
      "б",
      "г"
    ],
    "source_key": "б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 716,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 717,
    "page": 81,
    "page_end": 81,
    "question": "УКАЖИТЕ СТРУКТУРЫ, ОГРАНИЧИВАЮЩИЕ MEATUS NASI SUPERIOR:",
    "source_options": [
      {
        "label": "а",
        "text": "Concha nasalis suprema"
      },
      {
        "label": "б",
        "text": "Concha nasalis media"
      },
      {
        "label": "в",
        "text": "Concha nasalis inferior"
      },
      {
        "label": "г",
        "text": "Соnсhа nasalis superior"
      }
    ],
    "options": {
      "а": "Concha nasalis suprema",
      "б": "Concha nasalis media",
      "в": "Concha nasalis inferior",
      "г": "Соnсhа nasalis superior"
    },
    "correct": [
      "б",
      "г"
    ],
    "source_key": "б,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 717,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 765,
    "page": 86,
    "page_end": 86,
    "question": "УКАЖИТЕ СТРУКТУРЫ, КОТОРЫЕ ВХОДЯТ В СОСТАВ ПОГРАНИЧНОЙ ЛИНИИ, ОТДЕЛЯЮЩЕЙ БОЛЬШОЙ ТАЗ ОТ МАЛОГО:",
    "source_options": [
      {
        "label": "а",
        "text": "мыс крестца"
      },
      {
        "label": "б",
        "text": "дугообразная линия подвздошной кости"
      },
      {
        "label": "в",
        "text": "нижний край лобкового симфиза"
      },
      {
        "label": "г",
        "text": "верхушка крестца"
      }
    ],
    "options": {
      "а": "мыс крестца",
      "б": "дугообразная линия подвздошной кости",
      "в": "нижний край лобкового симфиза",
      "г": "верхушка крестца"
    },
    "correct": [
      "а",
      "б"
    ],
    "source_key": "а,б",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 765,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 775,
    "page": 87,
    "page_end": 87,
    "question": "УКАЖИТЕ ОСНОВНЫЕ ТОЧКИ ОПОРЫ НА ПОДОШВЕННОЙ ПОВЕРХНОСТИ СТОПЫ",
    "source_options": [
      {
        "label": "а",
        "text": "пяточный бугор"
      },
      {
        "label": "б",
        "text": "головка 1-ой плюсневой кости"
      },
      {
        "label": "в",
        "text": "головка 2-ой плюсневой кости"
      },
      {
        "label": "г",
        "text": "головка 5-ой плюсневой кости"
      }
    ],
    "options": {
      "а": "пяточный бугор",
      "б": "головка 1-ой плюсневой кости",
      "в": "головка 2-ой плюсневой кости",
      "г": "головка 5-ой плюсневой кости"
    },
    "correct": [
      "а",
      "б",
      "в"
    ],
    "source_key": "а,б,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 775,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 782,
    "page": 88,
    "page_end": 88,
    "question": "УКАЖИТЕ В КАКИХ ОТДЕЛАХ ПОЗВОНОЧНОГО СТОЛБА ИМЕЕТ МЕСТО ФИЗИОЛОГИЧЕСКИЙ ЛОРДОЗ:",
    "source_options": [
      {
        "label": "а",
        "text": "гpудном"
      },
      {
        "label": "б",
        "text": "шейном"
      },
      {
        "label": "в",
        "text": "поясничном"
      },
      {
        "label": "г",
        "text": "крестцовом"
      }
    ],
    "options": {
      "а": "гpудном",
      "б": "шейном",
      "в": "поясничном",
      "г": "крестцовом"
    },
    "correct": [
      "б",
      "в"
    ],
    "source_key": "б,в",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 782,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 783,
    "page": 88,
    "page_end": 88,
    "question": "УКАЖИТЕ В КАКИХ ОТДЕЛАХ ПОЗВОНОЧНОГО СТОЛБА ИМЕЕТ МЕСТО ФИЗИОЛОГИЧЕСКИЙ КИФОЗ:",
    "source_options": [
      {
        "label": "а",
        "text": "гpудном"
      },
      {
        "label": "б",
        "text": "поясничном"
      },
      {
        "label": "в",
        "text": "шейном"
      },
      {
        "label": "г",
        "text": "крестцовом"
      }
    ],
    "options": {
      "а": "гpудном",
      "б": "поясничном",
      "в": "шейном",
      "г": "крестцовом"
    },
    "correct": [
      "а",
      "г"
    ],
    "source_key": "а,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 783,
    "enabled": true,
    "topic": "кости"
  },
  {
    "id": 796,
    "page": 89,
    "page_end": 89,
    "question": "УКАЖИТЕ ПРИЗНАКИ ЧЕРЕПА НОВОРОЖДЕННОГО",
    "source_options": [
      {
        "label": "а",
        "text": "Роднички"
      },
      {
        "label": "б",
        "text": "Выраженные лобные и тeменные бугры"
      },
      {
        "label": "в",
        "text": "Слабая пневматизация костей черепа"
      },
      {
        "label": "г",
        "text": "Отсутствие сосцевидных отростков"
      }
    ],
    "options": {
      "а": "Роднички",
      "б": "Выраженные лобные и тeменные бугры",
      "в": "Слабая пневматизация костей черепа",
      "г": "Отсутствие сосцевидных отростков"
    },
    "correct": [
      "а",
      "б",
      "в",
      "г"
    ],
    "source_key": "а,б,в,г",
    "source_faithful": true,
    "source_issue": "",
    "source_number": 796,
    "enabled": true,
    "topic": "кости"
  }
]
'''
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


class Bot(FeatureUI):
    def __init__(self, store, bank, admin_id=None):
        self.store, self.bank, self.admin_id = store, bank, admin_id
        self.quiz = Quiz(store, bank)
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
                   [MISTAKES, PRACTICE], [STATS, REMINDERS], [AGAIN, STOP], [BACK]]
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
        self.learning.cancel_prompt(update.effective_user.id)
        self.features.clear_intake(update.effective_user.id)
        await self.say(update,
            "Анатомия человека · вопросы по странице 151 включительно.\n\n"
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
                    EXAM, DUE, PROGRESS, TOPICS, LISTS, REMINDERS, REPORTS):
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
                   "/exam": EXAM, "/reminders": REMINDERS, "/reports": REPORTS}
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
            BotCommand("start", "Главное меню"), BotCommand("learn", "Заучивание"),
            BotCommand("lists", "Мои списки"), BotCommand("due", "Повторить сегодня"),
            BotCommand("progress", "Прогресс обучения"), BotCommand("topics", "Темы анатомии"),
            BotCommand("exam", "Экзамен"), BotCommand("reminders", "Напоминания по желанию"),
            BotCommand("quiz", "Тренировка"), BotCommand("resume", "Продолжить"),
            BotCommand("again", "Заново"), BotCommand("stop", "Пауза"),
            BotCommand("mistakes", "Мои ошибки"), BotCommand("stats", "Статистика"), BotCommand("id", "Мой ID")])
        application.bot_data["background_task"] = asyncio.create_task(bot.background(application))
        print(f"Telegram подключён: @{me.username}. Доступны только вопросы по остеологии. Новые функции доступны через /start.", flush=True)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    application = (Application.builder().token(token).concurrent_updates(False)
                   .post_init(startup).post_stop(stopped).post_shutdown(shutdown).build())
    application.add_handler(CommandHandler(["start", "help"], bot.start))
    application.add_handler(CommandHandler(["quiz", "again", "stop", "back", "mistakes", "stats", "admin", "resume", "id", "learn",
        "lists", "due", "progress", "topics", "exam", "reminders", "reports"], bot.command))
    application.add_handler(CallbackQueryHandler(bot.callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.message))
    application.add_error_handler(bot.error)
    print("Бот запускается. Для остановки нажмите Ctrl+C.")
    application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
