"""SQLite-backed checks for persistence, privacy and repetition scheduling."""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from features import FeatureStore


class BaseStore:
    """Minimal pre-upgrade schema; exercises the real migrations independently."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE users(user_id INTEGER PRIMARY KEY,username TEXT,first_name TEXT);
            CREATE TABLE sessions(user_id INTEGER PRIMARY KEY REFERENCES users(user_id),state TEXT,updated_at TEXT);
            CREATE TABLE answers(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(user_id),
                question_id INTEGER,is_correct INTEGER,answered_at TEXT);
            CREATE TABLE learning_sets(user_id INTEGER PRIMARY KEY REFERENCES users(user_id),state TEXT,updated_at TEXT);
            INSERT INTO users VALUES(1,'student_one','Один');
            INSERT INTO users VALUES(2,NULL,'Два');
            INSERT INTO users VALUES(9,'Timur0_15','Администратор');
        """)

    def state(self, uid):
        row = self.db.execute("SELECT state FROM sessions WHERE user_id=?", (uid,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, uid, state):
        self.db.execute("""
            INSERT INTO sessions VALUES(?,?,'2026-01-01')
            ON CONFLICT(user_id) DO UPDATE SET state=excluded.state
        """, (uid, json.dumps(state)))

    def save(self, uid, state):
        with self.db:
            self._save(uid, state)


def feature_store(store):
    bank = {1: {"id": 1}, 2: {"id": 2}, 3: {"id": 3}, 4: {"id": 4}}
    return FeatureStore(store, bank, {1: 1, 2: 2, 3: 3, 4: 1})


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.store = BaseStore()
        self.features = feature_store(self.store)
        self.day = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.store.db.close()

    def test_old_list_migration_preserves_cards_nonce_and_session(self):
        store = BaseStore()
        old = {"ids": [3, 1], "version": "nonce", "pending": "add",
               "study_index": 1, "revealed": True}
        with store.db:
            store.db.execute("INSERT INTO learning_sets VALUES(1,?,'2026-01-01')", (json.dumps(old),))
            store._save(1, {"mode": "learning", "status": "paused", "pool": [3, 1]})
        features = feature_store(store)
        migrated = features.get_learning(1)
        self.assertEqual({key: migrated[key] for key in old}, old)
        self.assertEqual(store.state(1)["list_id"], migrated["list_id"])
        self.assertEqual(migrated["name"], "Мой список")
        again = feature_store(store)
        self.assertEqual(again.get_learning(1), migrated)
        self.assertEqual(len(again.lists(1)), 1)
        store.db.close()

    def test_lists_are_personal_and_foreign_actions_do_not_leak_or_modify(self):
        mine = self.features.create_list(1, "Кости")
        other = self.features.create_list(2, "Нервы")
        self.features.append_question(2, 2)
        before = self.features.get_learning(2)
        self.assertIsNone(self.features.switch_list(1, other["list_id"]))
        self.assertIsNone(self.features.list_state(1, other["list_id"]))
        self.assertFalse(self.features.save_list_state(1, other["list_id"], mine))
        self.assertFalse(self.features.rename_list(1, other["list_id"], "Чужое"))
        self.assertFalse(self.features.delete_list(1, other["list_id"]))
        with self.assertRaises(ValueError):
            self.features.save_learning(1, other)
        self.assertEqual(self.features.get_learning(2), before)
        self.assertEqual(self.features.get_learning(1)["list_id"], mine["list_id"])

    def test_append_canonicalizes_and_prevents_duplicate_and_stale_nonce(self):
        state, added = self.features.append_question(1, 4)
        self.assertTrue(added)
        self.assertEqual(state["ids"], [1])
        version = state["version"]
        repeated, added = self.features.append_question(1, 1)
        self.assertFalse(added)
        self.assertEqual(repeated["version"], version)
        expanded, added = self.features.append_question(1, 2)
        self.assertTrue(added)
        self.assertNotEqual(expanded["version"], version)

    def test_named_list_sessions_survive_switch_and_only_own_pool_expands(self):
        first = self.features.create_list(1, "Первый")
        self.features.append_question(1, 1)
        session = {"mode": "learning", "status": "paused", "list_id": first["list_id"], "pool": [1]}
        self.store.save(1, session)
        second = self.features.create_list(1, "Второй")
        self.features.append_question(1, 2)
        self.assertEqual(self.store.state(1), session)
        self.features.switch_list(1, first["list_id"])
        self.features.append_question(1, 3)
        self.assertEqual(self.store.state(1)["pool"], [1, 3])
        self.features.delete_list(1, second["list_id"])
        self.assertEqual(self.store.state(1)["pool"], [1, 3])
        self.features.delete_list(1, first["list_id"])
        self.assertIsNone(self.store.state(1))
        self.assertEqual(len(self.features.lists(1)), 1)
        self.assertEqual(self.features.get_learning(1)["ids"], [])

    def test_deleting_active_list_keeps_nonlearning_session(self):
        state = self.features.get_learning(1)
        session = {"mode": "all", "status": "running", "pool": [1, 2, 3]}
        self.store.save(1, session)
        self.assertTrue(self.features.delete_list(1, state["list_id"]))
        self.assertEqual(self.store.state(1), session)
        self.assertNotEqual(self.features.active_list_id(1), state["list_id"])

    def test_inactive_state_save_does_not_change_active_list(self):
        first = self.features.create_list(1, "Первый")
        second = self.features.create_list(1, "Второй")
        first["ids"] = [3]
        self.features.save_learning(1, first)
        self.assertEqual(self.features.get_learning(1), second)
        self.assertEqual(self.features.list_state(1, first["list_id"])["ids"], [3])
        self.assertTrue(self.features.rename_list(1, first["list_id"], "  Длинное\n имя  "))
        self.assertEqual(self.features.list_state(1, first["list_id"])["name"], "Длинное имя")

    def test_three_distinct_correct_days_and_intervals(self):
        for offset, expected_stage, interval in [(0, 1, 3), (3, 2, 7), (10, 3, 14)]:
            now = self.day + timedelta(days=offset)
            result = self.features.update_progress(1, 1, True, now)
            self.assertEqual(result["stage"], expected_stage)
            self.assertEqual(result["due_at"], (now.date() + timedelta(days=interval)).isoformat())
            repeat = self.features.update_progress(1, 4, True, now + timedelta(hours=1))
            self.assertEqual(repeat["stage"], expected_stage)
            self.assertEqual(repeat["due_at"], result["due_at"])
        self.assertEqual(result["status"], "mastered")
        self.assertTrue(result["mastered"])
        self.assertEqual(self.features.progress(1, [1, 4, 2]),
                         {"total": 2, "new": 1, "review": 0, "mastered": 1, "due": 0})

    def test_error_resets_streak_and_same_day_correct_does_not_recover_mastered(self):
        for offset in (0, 3, 10):
            self.features.update_progress(1, 1, True, self.day + timedelta(days=offset))
        now = self.day + timedelta(days=10, hours=1)
        wrong = self.features.update_progress(1, 1, False, now)
        self.assertEqual((wrong["stage"], wrong["mastered"]), (0, False))
        self.assertEqual(wrong["due_at"], (now.date() + timedelta(days=1)).isoformat())
        repeat = self.features.update_progress(1, 1, True, now + timedelta(hours=1))
        self.assertEqual((repeat["stage"], repeat["mastered"]), (0, False))
        next_day = self.features.update_progress(1, 1, True, now + timedelta(days=1))
        self.assertEqual(next_day["stage"], 1)

    def test_due_questions_and_progress_are_personal_and_canonical(self):
        self.features.update_progress(1, 4, False, self.day)
        self.features.update_progress(1, 2, True, self.day)
        self.features.update_progress(2, 3, False, self.day)
        self.assertEqual(self.features.due_ids(1, self.day), [])
        self.assertEqual(self.features.due_ids(1, self.day + timedelta(days=1)), [1])
        self.assertEqual(self.features.due_ids(1, self.day + timedelta(days=3)), [1, 2])
        self.assertEqual(self.features.due_ids(2, self.day + timedelta(days=1)), [3])
        self.assertIsNone(self.features.question_progress(2, 1))
        self.assertEqual(self.features.progress(1, [1, 4, 2, 3])["new"], 1)

    def test_no_commit_progress_hook_rolls_back_with_answer_transaction(self):
        with self.assertRaises(RuntimeError):
            with self.store.db:
                self.store.db.execute("INSERT INTO answers VALUES(1,1,1,0,'2026-10-13T12:00:00+00:00')")
                self.features.update_progress_no_commit(1, 1, False, self.day)
                raise RuntimeError("Simulated save failure")
        self.assertIsNone(self.features.question_progress(1, 1))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)

    def test_historical_answers_seed_once_in_order_by_utc_day(self):
        store = BaseStore()
        rows = [(3, 1, 4, 1, "2026-10-13T13:00:00+00:00"),
                (1, 1, 1, 1, "2026-10-13T12:00:00+00:00"),
                (4, 1, 1, 1, "2026-10-20T12:00:00+00:00"),
                (2, 1, 1, 1, "2026-10-16T12:00:00+00:00")]
        with store.db:
            store.db.executemany("INSERT INTO answers VALUES(?,?,?,?,?)", rows)
        features = feature_store(store)
        result = features.question_progress(1, 1)
        self.assertEqual(result["stage"], 3)
        self.assertEqual(result["due_at"], "2026-11-03")
        features.update_progress(1, 1, False, "2026-10-21T12:00:00+00:00")
        again = feature_store(store)
        self.assertEqual(again.question_progress(1, 1)["stage"], 0)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 4)
        store.db.close()

    def test_progress_seed_ignores_unpublished_exam_answers(self):
        store = BaseStore()
        store.db.execute("ALTER TABLE answers ADD COLUMN visible INTEGER NOT NULL DEFAULT 1")
        with store.db:
            store.db.executemany("INSERT INTO answers VALUES(?,?,?,?,?,?)", [
                (1, 1, 1, 1, "2026-10-13T12:00:00+00:00", 1),
                (2, 1, 1, 1, "2026-10-16T12:00:00+00:00", 0),
                (3, 1, 2, 0, "2026-10-16T12:00:00+00:00", 0),
                (4, 2, 3, 1, "2026-10-16T12:00:00+00:00", 1),
            ])
        features = feature_store(store)
        self.assertEqual(features.question_progress(1, 1)["stage"], 1)
        self.assertEqual(features.question_progress(1, 1)["last_correct_day"], "2026-10-13")
        self.assertIsNone(features.question_progress(1, 2))
        self.assertEqual(features.question_progress(2, 3)["stage"], 1)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers WHERE visible=0").fetchone()[0], 2)
        # Reinitialization cannot publish or count a still-active exam.
        again = feature_store(store)
        self.assertEqual(again.question_progress(1, 1)["stage"], 1)
        self.assertIsNone(again.question_progress(1, 2))
        store.db.close()

    def test_reminders_opt_in_hour_due_and_one_per_utc_day(self):
        self.features.update_progress(1, 1, False, self.day)
        self.features.update_progress(2, 1, False, self.day)
        tomorrow = self.day + timedelta(days=1)
        self.assertEqual(self.features.reminders_due(tomorrow), [])
        self.assertTrue(self.features.toggle_reminders(1))
        self.assertEqual(self.features.reminders_due(tomorrow.replace(hour=8)), [])
        self.assertEqual(self.features.reminders_due(tomorrow.replace(hour=9)), [1])
        self.features.mark_reminder(1, tomorrow)
        self.assertEqual(self.features.reminders_due(tomorrow), [])
        self.assertEqual(self.features.reminders_due(tomorrow + timedelta(days=1)), [1])
        self.assertFalse(self.features.toggle_reminders(1))
        self.assertEqual(self.features.reminders_due(tomorrow + timedelta(days=1)), [])

    def test_intake_and_reports_are_persistent_and_reports_resolve_once(self):
        self.features.set_intake(1, "report", {"qid": 2})
        self.features.set_intake(2, "listnew")
        self.assertEqual(self.features.get_intake(1), {"action": "report", "payload": {"qid": 2}})
        self.assertEqual(self.features.get_intake(2), {"action": "listnew", "payload": {}})
        self.features.clear_intake(1)
        self.assertIsNone(self.features.get_intake(1))
        self.assertIsNotNone(self.features.get_intake(2))
        rid = self.features.add_report(1, 2, "Ключ отличается от текста")
        self.features.add_report(2, 1, "Проверьте опечатку")
        report = self.features.reports(limit=1)[0]
        self.assertEqual((report["id"], report["uid"], report["username"], report["qid"], report["status"]),
                         (rid, 1, "student_one", 2, "pending"))
        self.assertEqual(self.features.reports(page=1, limit=1)[0]["uid"], 2)
        self.assertEqual(self.features.report_count(), 2)
        self.assertTrue(self.features.resolve_report(rid, 9))
        self.assertFalse(self.features.resolve_report(rid, 9))
        self.assertEqual(self.features.report_count(), 1)
        resolved = self.store.db.execute("SELECT * FROM question_reports WHERE id=?", (rid,)).fetchone()
        self.assertEqual(resolved["resolved_by"], 9)
        self.assertEqual(resolved["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
