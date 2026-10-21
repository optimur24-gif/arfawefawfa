"""Latin routes kept separate from the osteology question bank."""
from __future__ import annotations
import secrets
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from latin_cards import LATIN_COLLECTIONS, LATIN_FIELDS, LATIN_FORM_FIELDS, latin_details

LATIN_RULES = """📘 Как читать словарную форму

Полная форма: ala, alae f.
ala — Nom. sg. (именительный падеж, единственное число).
alae — Gen. sg. (родительный падеж, единственное число).
f — женский род; m — мужской; n — средний.

Склонение определяют по Gen. sg.:
I — -ae; II — -i; III — -is; IV — -us; V — -ei.
Греческое raphe, raphes f — особая форма I склонения.

Основные подсказки:
-a, -ae — обычно f; poeta, nauta — m.
-us, -i — обычно m; humus — f, virus — n.
-um, -i — n.
-us, -us — обычно m; manus — f.
-u, -us — n: cornu, cornus; genu, genus.
-us, -oris — n: corpus, corporis.
-tas, -tatis — обычно f: cavitas, cavitatis.

Отдельно запомни:
caput, capitis n; crus, cruris n; os, ossis n (кость).
os, oris n — рот, другое слово.
Латинский род не определяют по русскому переводу.

У каждой словарной карточки есть свое правило и исключения из PDF."""


LATIN43_RULES = """📘 Латынь, 43 параграф — роды, формы и окончания

Три рода:
m — masculinum, мужской;
f — femininum, женский;
n — neutrum, средний.
Род определяют по латинскому словарю, а не по русскому переводу.

Прилагательные 1-й группы:
m: -us или -er; f: -a; n: -um.
rectus — recta — rectum.
У части слов на -er гласная e выпадает:
dexter — dextra — dextrum;
sinister — sinistra — sinistrum;
niger — nigra — nigrum;
ruber — rubra — rubrum;
sacer — sacra — sacrum.
У liber и asper гласная e сохраняется:
liber — libera — liberum; asper — aspera — asperum.

Прилагательные 2-й группы в этом PDF:
m и f: -is; n: -e.
cervicalis — cervicalis — cervicale;
mollis — mollis — molle.
Это две разные формы для трёх родов.

Существительные:
Учить Nom. sg., полный Gen. sg. и род.
Склонения по Gen. sg.: I — -ae; II — -i; III — -is; IV — -us; V — -ei.
-a, -ae — обычно f; poeta, nauta — m.
-us, -i — обычно m; -um, -i — n.
calcaneus, calcanei m — пяточная кость;
palatum, palati n — нёбо.
os, oris n — рот; os, ossis n — кость. Не путай эти слова.

Согласование:
Прилагательное согласуется с существительным по роду, числу и падежу:
fibula dextra; palatum molle.

Как учить: прочитай перевод, назови m/f/n либо Nom./Gen. + род, открой карточку и проверь правило.
Источник: присланный PDF, памятки на страницах 1 и 14."""


class LatinUI:
    async def _latin_panel(self, update, text, rows, edit=False):
        uid = update.effective_user.id
        state = self.latin.state(uid)
        token = secrets.token_hex(4)
        buttons = [[InlineKeyboardButton(label, callback_data=f"lt:{token}:{action}") for label, action in row] for row in rows]
        markup = InlineKeyboardMarkup(buttons)
        if edit and update.callback_query:
            try:
                await update.callback_query.edit_message_text(text=text, reply_markup=markup)
            except BadRequest as e:
                if "message is not modified" not in str(e).lower():
                    raise
            mid = update.callback_query.message.message_id
        else:
            message = await self.say(update, text, markup)
            mid = message.message_id
        state["panel"] = {"token": token, "message_id": mid}
        self.latin.save(uid, state)

    async def latin_home(self, update, context, edit=False):
        summary = self.latin.summary(update.effective_user.id)
        counts = {group: sum(c['collection']==group for c in self.latin.cards.values()) for group in LATIN_COLLECTIONS}
        text = ("📖 Латынь\n\n" + "\n".join(f"{name}: {counts[group]}" for group, name in LATIN_COLLECTIONS.items())
                + f"\n\nПроверено: {summary['attempts']} · верно: {summary['correct']} · ошибок: {summary['wrong']}\n"
                + "Карточки открывают перевод, полную словарную форму, род и правило. Тесты сохраняют личные результаты.")
        rows = [[(name, f"set:{group}")] for group, name in LATIN_COLLECTIONS.items()]
        session = self.latin.state(update.effective_user.id)["session"]
        if session and session["index"] < len(session["ids"]):
            rows.append([("▶️ Продолжить латынь", "resume")])
        rows += [[("📘 Правила и исключения", "rules")], [("⬅️ Главное меню", "exit")]]
        await self._latin_panel(update, text, rows, edit)

    async def latin_collection(self, update, group, edit=True):
        rows = [[("Карточки: латынь → русский", f"new:{group}:cards:translation")],
                [("Карточки: русский → латынь", f"new:{group}:cards:latin")],
                [("Тест: перевод на русский", f"new:{group}:test:translation")],
                [("Тест: перевод на латынь", f"new:{group}:test:latin")]]
        if group == "words":
            rows += [[("Тест: полная словарная форма", "new:words:test:formula")],
                     [("Тест: Gen. sg.", "new:words:test:genitive"), ("Тест: род", "new:words:test:gender")]]
        if group == "paragraph43":
            rows += [[("Тест: полная словарная форма", "new:paragraph43:test:formula")],
                     [("Тест: мужской род (m)", "new:paragraph43:test:masculine")],
                     [("Тест: женский род (f)", "new:paragraph43:test:feminine")],
                     [("Тест: средний род (n)", "new:paragraph43:test:neuter")],
                     [("Существительные: Gen. sg.", "new:paragraph43:test:genitive")],
                     [("Существительные: род", "new:paragraph43:test:gender")],
                     [("📘 Правила: роды и окончания", "rules43")]]
            session = self.latin.state(update.effective_user.id)["session"]
            if session and session["collection"] == group and session["index"] < len(session["ids"]):
                rows.append([("▶️ Продолжить §43", "resume")])
        rows += [[("⬅️ Разделы латыни", "home")]]
        text = "📖 " + LATIN_COLLECTIONS[group] + "\nВыбери способ изучения."
        if group == "paragraph43":
            summary = self.latin.summary(update.effective_user.id, group)
            text += ("\n\n66 слов из PDF: 44 прилагательных и 22 существительных."
                     "\nДля прилагательных — три формы (m/f/n), окончания и правила."
                     "\nДля существительных — полный Gen. sg., род и склонение."
                     f"\n\nПроверено в §43: {summary['attempts']} · верно: {summary['correct']} · ошибок: {summary['wrong']}")
        await self._latin_panel(update, text, rows, edit)

    async def latin_session(self, update, context, edit=True):
        state = self.latin.state(update.effective_user.id)
        s = state["session"]
        if not s:
            await self.latin_home(update, context, edit)
            return
        if s["index"] >= len(s["ids"]):
            text = f"🏁 Раздел завершён: {LATIN_COLLECTIONS[s['collection']]}"
            if s["kind"] == "test":
                text += f"\nВерно: {s['correct']} из {s['attempts']}."
            rows = [[("🔄 Заново", f"new:{s['collection']}:{s['kind']}:{s['field']}")], [("⬅️ Разделы латыни", "home")]]
            await self._latin_panel(update, text, rows, edit)
            return
        card = self.latin.cards[s["ids"][s["index"]]]
        header = f"📖 {LATIN_COLLECTIONS[s['collection']]} · {s['index']+1}/{len(s['ids'])}\n\n"
        rows = []
        if s["kind"] == "cards":
            prompt = card["translation"] if s["field"] == "latin" else card["latin"]
            text = header + (latin_details(card) if s["revealed"] else prompt + "\n\nВспомни ответ, затем открой карточку.")
            rows += [[("🙈 Скрыть ответ" if s["revealed"] else "👁 Показать ответ", "reveal")],
                     [("◀️ Предыдущая", "prev"), ("Следующая ▶️", "next")]]
        else:
            prompt = card["translation"] if s["field"] in ("latin", "formula") or s["field"] in LATIN_FORM_FIELDS else card["latin"]
            text = header + {**LATIN_FIELDS, **LATIN_FORM_FIELDS}[s["field"]] + "\n" + prompt
            if s["answered"]:
                ok = s["choices"][s["chosen"]] == s["expected"]
                text += "\n\n" + ("✅ Верно" if ok else "❌ Неверно") + "\nТвой ответ: " + s["choices"][s["chosen"]]
                text += "\nПравильный ответ: " + s["expected"] + "\n\n" + latin_details(card)
                rows.append([("Следующее задание ▶️", "next")])
            else:
                # Full choices stay in message text, avoiding Telegram button label truncation.
                text += "\n\n" + "\n".join(f"{i+1}. {choice}" for i,choice in enumerate(s["choices"]))
                rows.append([(str(i+1), f"answer:{i}") for i in range(len(s["choices"]))])
        rows.append([("⏸ Пауза / разделы", "home")])
        await self._latin_panel(update, text, rows, edit)

    async def latin_callback(self, update, context, parts):
        query = update.callback_query
        uid = update.effective_user.id
        state = self.latin.state(uid)
        panel = state["panel"]
        if (len(parts)<3 or not panel or panel["token"]!=parts[1]
                or panel["message_id"]!=query.message.message_id):
            await query.answer("Кнопка устарела. Открой /latin.")
            return
        action = parts[2]
        if action == "answer":
            try:
                option = int(parts[3])
            except (ValueError, IndexError):
                await query.answer("Некорректный ответ.")
                return
            ok = self.latin.answer(uid, option)
            if ok is None:
                await query.answer("Этот ответ уже проверен или недоступен.")
                return
            await query.answer("Верно!" if ok else "Показан правильный ответ.")
            await self.latin_session(update, context)
            return
        if action in ("reveal", "prev", "next"):
            s = state["session"]
            if not s or s["index"] >= len(s["ids"]):
                await query.answer("Открой раздел заново.")
                return
            if action == "reveal" and s["kind"] == "cards":
                s["revealed"] = not s["revealed"]
            elif action == "prev" and s["kind"] == "cards":
                s["index"] = max(0,s["index"]-1)
                s["revealed"] = False
            elif action == "next" and (s["kind"] == "cards" or s["answered"]):
                s["index"] += 1
                s["revealed"] = False
                if s["kind"] == "test" and s["index"] < len(s["ids"]):
                    self.latin.prepare(s)
            else:
                await query.answer("Сначала ответь на задание.")
                return
            self.latin.save(uid, state)
        await query.answer()
        if action in ("reveal", "prev", "next", "resume"):
            await self.latin_session(update, context)
        elif action == "set" and len(parts)==4 and parts[3] in LATIN_COLLECTIONS:
            await self.latin_collection(update, parts[3])
        elif action == "new" and len(parts)==6:
            try:
                self.latin.new(uid, parts[3], parts[4], parts[5])
            except ValueError:
                await self.latin_home(update, context, True)
                return
            await self.latin_session(update, context)
        elif action == "rules":
            await self._latin_panel(update, LATIN_RULES, [[("⬅️ Разделы латыни", "home")]], True)
        elif action == "rules43":
            await self._latin_panel(update, LATIN43_RULES, [[("⬅️ Латынь, 43 параграф", "set:paragraph43")]], True)
        elif action == "exit":
            self.latin.invalidate(uid)
            await query.edit_message_reply_markup(reply_markup=None)
            await self.say(update, "Главное меню. Латынь сохранена в своём разделе.", self.menu(update.effective_user))
        else:
            await self.latin_home(update, context, True)
