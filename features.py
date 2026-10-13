"""Persistent learning lists, spaced repetition, and question reports.

This module deliberately depends only on the Python standard library.  Each
Telegram user's lists, progress and reminders share the main bot's SQLite
connection, so an answer and its repetition schedule can commit atomically.
"""
from __future__ import annotations

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
