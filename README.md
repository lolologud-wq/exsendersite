# Userbot + Site (control plane)

Два независимых сервиса:

```
ASsite/
├── bot/        # один экземпляр userbot'а. Слушает HTTP API на :8080.
│               # Ставится на каждый VDS-бот.
└── web/        # сайт (control plane). Слушает HTTP на :3000.
                # Хранит реестр ботов, деплоит их по SSH,
                # ходит в их API за данными.
```

`bot/` и `web/` ничего не знают друг о друге, кроме контракта:
`web/` ходит на `http://<bot-host>:8080/api/local/*` с заголовком
`Authorization: Bearer <token>`.

## Этап 1 — что уже есть

- `bot/bot_api.py` — FastAPI бота, Bearer-auth, endpoint'ы:
  - `GET  /api/local/health` (без auth)
  - `GET  /api/local/overview`, `/accounts`, `/accounts/{id}`,
    `/accounts/{id}/chats`
  - `POST /api/local/accounts` (создать слот), `DELETE /api/local/accounts/{id}`
  - `PATCH /api/local/accounts/{id}`
  - `POST /api/local/accounts/{id}/activate`
  - `POST /api/local/accounts/{id}/spam`
  - `POST /api/local/accounts/{id}/auth/send_code`
  - `POST /api/local/accounts/{id}/auth/sign_in`
  - `POST /api/local/accounts/{id}/auth/2fa`
  - `POST /api/local/accounts/upload_session` (multipart, `slot_id`, `proxy`,
    `session_file`)
  - чаты: `POST /accounts/{id}/chats`, `PATCH /accounts/{id}/chats/{chat_id}`,
    `DELETE /accounts/{id}/chats/{chat_id}`
- `web/app.py` — FastAPI сайта:
  - `POST /api/auth/login` (cookie-session), `/api/auth/logout`, `/api/auth/me`
  - реестр: `GET/POST /api/bots`, `PATCH/DELETE /api/bots/{id}`
  - деплой: `POST /api/bots/{id}/deploy`, `/restart`, `/stop`, `/uninstall`
  - `GET /api/bots/{id}/deploy/log` — лог последней операции
  - `GET /api/bots/{id}/overview` — короткий пинг + overview бота
  - **прокси к боту:** `ANY /api/bots/{id}/proxy/<path>` →
    `http://bot-host:8080/api/local/<path>` (auth подставляется автоматически)
  - `POST /api/bots/{id}/accounts/upload_session` (multipart, проксирует в бота)
- `web/deployer.py` — SSH-деплой (asyncssh):
  - устанавливает на VDS python3/venv/pip
  - заливает только `bot/` (без `sessions/`, `.env`, `runtime_state.json`)
  - генерирует `BOT_API_TOKEN`, кладёт в `.env` бота и в реестр сайта
  - ставит и запускает `userbot.service` через systemd

## Креды и токены

- Логин в сайт (по умолчанию из ТЗ): `favory` / `gubkina2868`.
  Переопределяется в `web/.env` (`SITE_LOGIN`, `SITE_PASSWORD`).
  `SITE_SECRET` — обязательно поменять перед продом.
- Токен между сайтом и каждым ботом (`BOT_API_TOKEN`) генерируется при
  первом деплое и хранится в `web/bots.json` под ключом `api_token`.
- SSH-ключ для последующих операций после деплоя создаётся автоматически
  в `web/deploy_key` + `web/deploy_key.pub` и кладётся на VDS в
  `~/.ssh/authorized_keys`. После этого SSH-пароль больше не нужен.

## Локальный запуск (для разработки)

### 1) Бот

```powershell
cd bot
py -V:3.13 -m pip install -r requirements.txt
copy .env.example .env
# отредактируй API_ID, API_HASH
py -V:3.13 main.py
```

Бот поднимет:
- свой HTTP API: `http://127.0.0.1:8080/api/local/health`
- Telegram control-bot (если задан `BOT_TOKEN`).

При первом старте, если в `.env` нет `BOT_API_TOKEN`, токен будет
сгенерирован и сохранён в `bot/bot_api_token.txt` — этот же токен надо
вписать в реестр сайта (или просто задеплоить через сайт — он сделает всё сам).

### 2) Сайт

```powershell
cd web
py -V:3.13 -m pip install -r requirements.txt
copy .env.example .env
# отредактируй SITE_SECRET (обязательно!)
py -V:3.13 main.py
```

Открой `http://127.0.0.1:3000/login`, войди (`favory`/`gubkina2868`).

## Продовый запуск

- Сайт ставится на отдельный VDS, открываешь порт 3000 (или ставишь nginx
  с HTTPS впереди — тогда в `auth.py` переключи `secure=True` для cookie).
- Бот ставится через UI сайта: вводишь host, ssh-пользователя, пароль,
  `API_ID`/`API_HASH` — сайт сам по SSH ставит зависимости, заливает код,
  поднимает systemd. После этого пароль больше не нужен.

## Что в этап 2

- Перепилка `frontend/` под новый бэк (`/api/auth/*`, `/api/bots/*`,
  `/api/bots/{id}/proxy/*`), страница логина, форма деплоя, выбор «в какого
  бота заливать `.session`», UI для Telethon-авторизации (телефон → код → 2FA).
