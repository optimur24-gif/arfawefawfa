"""Latin flashcards and durable, user-specific practice state."""
from __future__ import annotations
import json
import random
from pathlib import Path

EMBEDDED_LATIN_JSON = None  # EMBED_LATIN_HERE
LATIN = "📖 Латынь"
LATIN43 = "📖 Латынь, 43 параграф"
LATIN_COLLECTIONS = {"words": "Слова и словарные формы", "sayings": "Крылатые выражения", "medical": "Медицинские выражения", "paragraph43": "Латынь, 43 параграф"}
LATIN_GENDERS = {"m": "мужской (masculinum)", "f": "женский (femininum)", "n": "средний (neutrum)"}
LATIN_FIELDS = {"translation": "Латынь → русский", "latin": "Русский → латынь", "genitive": "Gen. sg.", "gender": "Род", "formula": "Полная словарная форма"}
LATIN_FORM_FIELDS = {"masculine": "Мужской род (m)", "feminine": "Женский род (f)", "neuter": "Средний род (n)"}


def is_dictionary_card(card):
    return card["collection"] == "words" or card.get("part_of_speech") == "noun"


def supports_field(card, field):
    if field in ("translation", "latin"):
        return True
    if field in ("genitive", "gender"):
        return is_dictionary_card(card)
    if field == "formula":
        return is_dictionary_card(card) or card.get("part_of_speech") == "adjective"
    return field in LATIN_FORM_FIELDS and card.get("part_of_speech") == "adjective"


def load_latin_cards(path=None):
    raw = EMBEDDED_LATIN_JSON
    if raw is None:
        raw = Path(path or Path(__file__).resolve().parent / "latin_data.json").read_text(encoding="utf-8-sig")
    cards = json.loads(raw)["cards"]
    bank = {}
    for card in cards:
        cid = card["id"]
        if cid in bank or card["collection"] not in LATIN_COLLECTIONS:
            raise ValueError("Invalid Latin card ID or collection")
        if not card["latin"].strip() or not card["translation"].strip():
            raise ValueError("Empty Latin card")
        if card["collection"] == "paragraph43" and card.get("part_of_speech") not in ("noun", "adjective"):
            raise ValueError("Missing paragraph 43 part of speech")
        if card.get("part_of_speech") == "adjective":
            if not all(card.get(field, "").strip() for field in LATIN_FORM_FIELDS) or not card.get("rule"):
                raise ValueError("Incomplete adjective card")
            if card["latin"] != card["masculine"] or card.get("formula") != ", ".join(card[field] for field in LATIN_FORM_FIELDS):
                raise ValueError("Invalid adjective forms")
        if is_dictionary_card(card):
            if card["gender"] not in LATIN_GENDERS or not card["genitive"] or not card["rule"]:
                raise ValueError("Incomplete dictionary card")
            if card["formula"] != f"{card['latin']}, {card['genitive']} {card['gender']}":
                raise ValueError("Invalid full dictionary form")
        bank[cid] = card
    if not bank:
        raise ValueError("No Latin cards")
    return bank


def latin_details(card):
    lines = [card["latin"], "Перевод: " + card["translation"]]
    if card.get("part_of_speech") == "adjective":
        lines += ["", "Полная словарная форма: " + card["formula"],
                  "Мужской (m): " + card["masculine"],
                  "Женский (f): " + card["feminine"],
                  "Средний (n): " + card["neuter"],
                  "Группа прилагательных: " + card["adjective_group"],
                  "", "Правило / особенности:", card["rule"]]
    if is_dictionary_card(card):
        lines += ["", "Полная словарная форма: " + card["formula"],
                  "Nom. sg.: " + card["latin"], "Gen. sg.: " + card["genitive"],
                  "Окончание Gen.: " + card["genitive_ending"],
                  "Род: " + card["gender"] + " — " + LATIN_GENDERS[card["gender"]],
                  "Склонение: " + card["declension"], "", "Правило / исключения:", card["rule"]]
    if card["collection"] == "paragraph43":
        lines += ["", f"§43 · Раздел {card['source_section']} · №{card['source_number']} из PDF"]
    return "\n".join(lines)


class LatinStore:
    def __init__(self, store, cards=None, rng=None):
        self.db = store.db
        self.cards = cards if cards is not None else load_latin_cards()
        self.rng = rng or random.Random()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS latin_sessions (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id), state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS latin_results (
                user_id INTEGER NOT NULL REFERENCES users(user_id), card_id TEXT NOT NULL,
                field TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                correct INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, card_id, field)
            );
        """)

    def state(self, uid):
        row = self.db.execute("SELECT state FROM latin_sessions WHERE user_id=?", (uid,)).fetchone()
        return json.loads(row[0]) if row else {"session": None, "panel": None}

    def save(self, uid, state):
        with self.db:
            self.db.execute("INSERT INTO latin_sessions VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET state=excluded.state", (uid, json.dumps(state, ensure_ascii=False)))

    def invalidate(self, uid):
        state = self.state(uid)
        state["panel"] = None
        self.save(uid, state)

    def new(self, uid, collection, kind, field):
        if collection not in LATIN_COLLECTIONS or kind not in ("cards", "test") or field not in {**LATIN_FIELDS, **LATIN_FORM_FIELDS}:
            raise ValueError("Invalid Latin mode")
        ids = [cid for cid, c in self.cards.items() if c["collection"] == collection and supports_field(c, field)]
        if not ids:
            raise ValueError("No cards for this Latin mode")
        if kind == "test":
            self.rng.shuffle(ids)
        session = {"collection": collection, "kind": kind, "field": field, "ids": ids,
                   "index": 0, "revealed": False, "answered": False, "correct": 0, "attempts": 0}
        if kind == "test":
            self.prepare(session)
        state = self.state(uid)
        state["session"] = session
        self.save(uid, state)
        return session

    def value(self, card, field):
        if field == "gender":
            return card["gender"] + " — " + LATIN_GENDERS[card["gender"]]
        return card[field]

    def genitive_choices(self, card):
        ending = card["genitive_ending"].lstrip("-")
        stem = card["genitive"][:-len(ending)]
        return list(dict.fromkeys([card["genitive"]] + [stem + e for e in ("ae", "i", "is", "us", "es")]))

    def prepare(self, session):
        card = self.cards[session["ids"][session["index"]]]
        field = session["field"]
        expected = self.value(card, field)
        if field == "gender":
            candidates = [g + " — " + name for g, name in LATIN_GENDERS.items()]
        elif field == "genitive":
            candidates = self.genitive_choices(card)
        elif field == "formula" and card.get("part_of_speech") == "adjective":
            m, f, n = (card[key] for key in LATIN_FORM_FIELDS)
            candidates = [expected, f"{m}, {n}, {f}", f"{n}, {f}, {m}", f"{m}, {m}, {m}"]
        elif field == "formula":
            wrong_gen = next(g for g in self.genitive_choices(card) if g != card["genitive"])
            candidates = [f"{card['latin']}, {card['genitive']} {g}" for g in LATIN_GENDERS]
            candidates += [f"{card['latin']}, {wrong_gen} {card['gender']}"]
        elif field in LATIN_FORM_FIELDS:
            stem = card["masculine"][:-2]
            candidates = [card[key] for key in LATIN_FORM_FIELDS]
            candidates += [stem + ending for ending in ("us", "a", "um", "is", "e", "er")]
        else:
            candidates = [self.value(c, field) for c in self.cards.values() if c["collection"] == session["collection"]]
        wrong = list(dict.fromkeys(v for v in candidates if v != expected))
        choices = [expected] + self.rng.sample(wrong, min(3, len(wrong)))
        self.rng.shuffle(choices)
        session.update(choices=choices, expected=expected, answered=False, chosen=None, revealed=False)

    def answer(self, uid, option):
        state = self.state(uid)
        session = state["session"]
        if not session or session["kind"] != "test" or session["answered"] or session["index"] >= len(session["ids"]):
            return None
        if not 0 <= option < len(session["choices"]):
            return None
        correct = session["choices"][option] == session["expected"]
        session.update(answered=True, chosen=option)
        session["attempts"] += 1
        session["correct"] += int(correct)
        cid = session["ids"][session["index"]]
        with self.db:
            self.db.execute("""INSERT INTO latin_results VALUES (?,?,?,1,?)
                ON CONFLICT(user_id,card_id,field) DO UPDATE SET attempts=attempts+1, correct=correct+excluded.correct""",
                (uid, cid, session["field"], int(correct)))
            self.db.execute("UPDATE latin_sessions SET state=? WHERE user_id=?", (json.dumps(state, ensure_ascii=False), uid))
        return correct

    def summary(self, uid, collection=None):
        query = "SELECT COALESCE(SUM(attempts),0), COALESCE(SUM(correct),0) FROM latin_results WHERE user_id=?"
        args = [uid]
        if collection is not None:
            ids = [cid for cid, card in self.cards.items() if card["collection"] == collection]
            if not ids:
                return {"attempts": 0, "correct": 0, "wrong": 0}
            query += " AND card_id IN (" + ",".join("?" for _ in ids) + ")"
            args.extend(ids)
        row = self.db.execute(query, args).fetchone()
        return {"attempts": row[0], "correct": row[1], "wrong": row[0]-row[1]}
