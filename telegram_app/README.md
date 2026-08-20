# Paychain Telegram Mini App

Темний Telegram Mini App керує **локальним** агентом користувача. Paychain-пароль, cookie та браузерний профіль залишаються на його комп’ютері.

## Компоненти

- `server.py` — центральний HTTPS-сервер, Mini App і WebSocket-команди.
- `bot.py` — Telegram-бот, який відкриває Mini App командою `/start`.
- `agent.py` — локальна програма на комп’ютері кожного користувача.
- `webapp/` — темний інтерфейс Mini App.

## Встановлення

На сервері:

```cmd
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy telegram_app\settings.example.env telegram_app\settings.env
```

У `telegram_app/settings.env` вкажи:

- `TELEGRAM_BOT_TOKEN` — токен від @BotFather;
- `APP_URL` — публічна HTTPS-адреса сервера, наприклад `https://app.example.com`;

Не додавай `settings.env` у Git і нікому не надсилай токен бота.

Запусти два процеси:

```cmd
.venv\Scripts\uvicorn.exe telegram_app.server:app --host 0.0.0.0 --port 8000
.venv\Scripts\python.exe telegram_app\bot.py
```

Для Telegram URL має бути доступний з інтернету через HTTPS. Локальний `http://localhost:8000` годиться лише для розробки у браузері, але не як робочий Mini App.

## Підключення користувача

1. Користувач відкриває `/start` у боті й натискає **Відкрити додаток**.
2. На ПК двічі клацає `START/install_agent.cmd`.
3. У Mini App натискає **Підключити цей ПК**. Конфігурація завантажується автоматично.
4. Натискає **Відкрити Paychain для входу**, вводить email і 2FA, а потім натискає **Запустити алгоритм**.
5. Агент запускається автоматично разом із Windows:

```cmd
автозапуск Windows
```

Тепер кнопки Mini App запускають і зупиняють тільки його локальний Playwright-процес. Після успішного прийняття угоди агент надсилає серверу суму, а сервер відправляє користувачу Telegram-сповіщення.

Кнопка **«Запустити»** вмикає автоматичне підтвердження угод, сума яких не менша за вказаний поріг. Перед першим реальним запуском перевір поріг і сесію Paychain на тесті.

Для спрощеного встановлення на Windows двічі клацни `install_agent.cmd` у корені проєкту. Якщо запускаєш із PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_agent.ps1
```

Інсталятор створює `.venv`, ставить залежності й Playwright, а також додає агента в автозапуск Windows. Якщо Telegram відкритий на цьому ж ПК, після натискання **«Підключити цей ПК»** конфігурація завантажується автоматично, а агент сам забирає `agent-config.json` із папки «Завантаження». Після цього CMD для щоденної роботи не потрібен.

Кнопка **«Відключити цей ПК»** зупиняє моніторинг, видаляє локальний pairing-файл і від’єднує комп’ютер від Telegram. Сесію Paychain вона не видаляє.

Для входу використовуйте кнопки Mini App у такому порядку: спочатку **«Відкрити Paychain для входу»**, введіть email і 2FA у відкритому браузері, а після завершення входу натисніть **«Запустити алгоритм»**. До другого натискання сторінка не оновлюється.

## Розгортання на Railway

Створи на [Railway](https://railway.app/) порожній проєкт і підключи GitHub-репозиторій з цією папкою. Файл `railway.json` уже налаштовує вебсервіс: Railway встановить залежності з `requirements.txt` і запустить `telegram_app.server:app` на виданому порту.

У проєкті створи **два** сервіси з одного репозиторію:

1. **web** — не змінюй Start Command: він береться з `railway.json`.

2. **bot** — у Settings → Deploy в полі **Config File Path** вкажи `/railway.bot.json`. Цей файл запускає окремий процес бота:

   ```text
   python telegram_app/bot.py
   ```

В обидва сервіси додай однакові змінні `TELEGRAM_BOT_TOKEN` та `BOT_INTERNAL_TOKEN`. Для `BOT_INTERNAL_TOKEN` згенеруй випадкове значення (наприклад, `python -c "import secrets; print(secrets.token_urlsafe(32))"`) і не публікуй його. У сервіс `web` додай Volume, змонтуй його в `/data`, а в Variables додай `DATABASE_PATH=/data/control.sqlite3`. Після цього згенеруй Railway-домен для сервісу **web**, скопіюй його у `APP_URL` в обох сервісах і зроби Redeploy. У логах web-сервісу має з’явитися успішний старт Uvicorn, а `https://твій-домен/health` має відповісти `{"status":"ok"}`.

Після підключення локального агента користувач може надіслати боту `/stop`. Бот зупинить лише агент, прив’язаний до Telegram-акаунта відправника.

Потім у @BotFather відкрий створеного бота → **Bot Settings** → **Menu Button** або **Main Mini App** та вкажи цей самий Railway URL. Telegram підтримує запуск Mini App з кнопки меню та Main Mini App, що налаштовуються через BotFather. [Документація Telegram](https://core.telegram.org/bots/webapps)

## Вартість

Створення Telegram-бота та Mini App безкоштовні. Telegram прямо вказує, що Bot Platform безкоштовна для користувачів і розробників. Окремо може коштувати цілодобовий HTTPS-хостинг, домен або тунель до домашнього ПК. [Telegram Bots](https://core.telegram.org/bots)
