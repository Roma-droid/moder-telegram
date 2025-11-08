# moder-telegram (minimal)

Минимальный репозиторий с точкой входа `main.py` и простым пакетом `moder_telegram`.

Быстрый старт

1. Скопируйте `.env.sample` в `.env` и заполните `BOT_TOKEN`.
2. Установите зависимости в виртуальном окружении:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Запустите бота:

```bash
python3 main.py
```

Что внутри
- `main.py` — тонкий runner, вызывает `moder_telegram.app.run()`
- `moder_telegram/moderation.py` — чистые функции для обнаружения плохих сообщений (легко тестировать)
- `moder_telegram/app.py` — минимальная обвязка aiogram + регистрация обработчиков

Безопасность
- Никогда не коммитить реальные токены. Используйте переменные окружения или GitHub Secrets.
