# Telegram-бот по остеологии

103 вопроса только по костям. Остальные разделы удалены; исходные ID, тексты, варианты и ключи сохранены.

Функции: тренировка, заучивание, личные списки, экзамен, ошибки, статистика, повторения и напоминания. Полное описание команд: [README.txt](README.txt).

## Быстрый временный запуск через GitHub Actions

1. Settings → Secrets and variables → Actions → New repository secret: имя `BOT_TOKEN`, значение — токен от BotFather.
2. Actions → **Run Telegram bot (temporary)** → **Run workflow**.
3. Дождитесь строки `Telegram подключён` в журнале, затем отправьте боту `/start`.

Этот запуск работает до 5 часов 45 минут. Он не обеспечивает постоянную работу и не сохраняет SQLite между запусками. Не запускайте несколько экземпляров с одним токеном.

## Постоянный запуск на сервере с Docker

Скопируйте `.env.example` в `.env`, укажите `BOT_TOKEN` и выполните:

```bash
docker compose up -d --build
docker compose logs --tail=30 bot
```

Бот перезапускается автоматически; прогресс хранится в постоянном Docker volume `progress`. Обновление: повторить `docker compose up -d --build`. Остановка: `docker compose down` (без `-v`, чтобы сохранить данные).

## Локальный запуск

Python 3.10+:

```bash
python -m pip install -r requirements.txt
python bot.py --check
python bot.py
```

`bot_single.py` содержит код и вопросы в одном файле. Для него также нужны установленные зависимости и токен в окружении или `.env`.

## Проверки

```bash
python -m unittest discover -q
python bot_single.py --check
```

Токен и персональные базы данных в репозиторий не включаются.
