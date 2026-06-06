# exsender

Платформа для автоматизации Telegram: массовая рассылка в чаты и отдельный сервис инвайта. Состоит из **control plane** (сайт) и **userbot** на каждом VDS.

| Продукт | URL | Назначение |
|---------|-----|------------|
| **exsender** | https://exsender.top | Рассылка, аккаунты, чаты, источники |
| **EX Inviter** | https://inviter.exsender.top | Парс и инвайт участников в target-чат |

---

## Архитектура

```
┌─────────────────────────────────────────────────────────────┐
│  Site VPS (control plane)                                   │
│  FastAPI :3000 + nginx + Let's Encrypt                      │
│  /opt/exsender/                                             │
│    web/       — API, auth, реестр ботов, деплой по SSH      │
│    frontend/  — SPA (app, admin, inviter, landing)          │
│    data/      — users.json, bots.json, invoices… (persist)  │
└──────────────────────────┬──────────────────────────────────┘
                           │ SSH-туннель → bot API
                           │ (или прямой HTTP, если открыт порт)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Bot VPS (userbot) × N                                      │
│  Telethon + FastAPI :8765 (или :8080)                       │
│  /opt/userbot/                                              │
│    bot/           — рассылка, аккаунты, чаты                │
│    bot/inviter/   — парс, очередь, job инвайта (SQLite)   │
│    systemd: userbot.service                                   │
└─────────────────────────────────────────────────────────────┘
```

**Контракт между сайтом и ботом:** `GET/POST /api/local/*` с заголовком `Authorization: Bearer <BOT_API_TOKEN>`. Код `bot/` и `web/` связаны только этим API и SSH-деплоем — отдельные процессы, отдельные VDS.

---

## Возможности

### exsender (рассылка)

- Реестр VDS: добавление, деплой, рестарт, логи через UI
- Несколько Telegram-аккаунтов (слотов) на одном VDS
- Авторизация Telethon: телефон → код → 2FA, загрузка `.session`
- Управление чатами, источниками, расписанием рассылки
- Дашборд со статистикой отправки
- Подписки, оплата через Crypto Bot, рефералы, trial

### EX Inviter

- Парс участников исходного чата в очередь (SQLite на VDS)
- Выбор target-чата, запуск инвайта с limit/delay
- Автоподготовка `access_hash` перед инвайтом (ускорение)
- Батч-инвайт с корректным учётом `missing_invitees`
- Можно использовать VDS exsender (borrow) или отдельный inviter VDS
- Доступ: пользователи с активной подпиской; админы видят все серверы

Подробнее: [inviter/README.md](inviter/README.md)

---

## Структура репозитория

```
exsenderV2/
├── bot/                    # Userbot на VDS
│   ├── main.py             # Точка входа (Telethon + bot API)
│   ├── bot_api.py          # HTTP API для сайта
│   ├── bot_service.py      # Аккаунты, чаты, спам
│   ├── control_bot.py      # Telegram control-bot (опционально)
│   ├── inviter/            # Модуль инвайтера (service, db, errors)
│   └── requirements.txt
│
├── web/                    # Control plane
│   ├── main.py             # uvicorn :3000
│   ├── app.py              # FastAPI: auth, bots, inviter, payments
│   ├── deployer.py         # SSH-деплой на VDS (asyncssh)
│   ├── proxy.py            # SSH-туннель к bot API
│   ├── registry.py         # Реестр ботов (bots.json)
│   ├── auth.py             # Cookie-сессии (admin + user)
│   ├── security.py         # CSRF, rate limit
│   └── requirements.txt
│
├── frontend/               # Статика (без сборщика)
│   ├── index.html          # Панель exsender (/app)
│   ├── inviter.html        # Панель инвайтера
│   ├── admin.html          # Админка
│   ├── landing.html        # Лендинг
│   └── js/, css/
│
├── deploy/site/            # systemd + nginx для сайта
│   ├── exsender.service
│   ├── nginx-exsender.conf
│   ├── nginx-inviter.conf
│   └── install-on-server.sh
│
├── scripts/                # Деплой с Windows
│   ├── deploy-site.ps1     # Полный деплой сайта
│   └── sync-site-bot.ps1   # Только bot/ на сервер (для deploy из UI)
│
└── inviter/README.md       # Документация инвайтера
```

---

## Продакшен

| Роль | Пример | Путь |
|------|--------|------|
| Сайт | `178.236.252.6` | `/opt/exsender/` |
| Bot VDS | `185.100.157.243` | `/opt/userbot/` |

Сервисы: `systemctl status exsender` (сайт), `systemctl status userbot` (бот).

### DNS

```
exsender.top          A  <IP сайта>
www.exsender.top      A  <IP сайта>
inviter.exsender.top  A  <IP сайта>    # тот же сервер, отдельный vhost
```

### Деплой сайта (PowerShell)

```powershell
.\scripts\deploy-site.ps1 -ServerHost 178.236.252.6 -User root -KeyPath web\deploy_key
```

Скрипт упаковывает `web/`, `frontend/`, `bot/`, `deploy/site/`, заливает на VPS и запускает `install-on-server.sh` (nginx, certbot, systemd).

### Деплой только кода бота (для UI-деплоя VDS)

```powershell
.\scripts\sync-site-bot.ps1 -ServerHost 178.236.252.6 -User root -KeyPath web\deploy_key
```

### Деплой bot/ на VDS вручную

```powershell
scp -i web\deploy_key -r bot\inviter\*.py root@<bot-host>:/opt/userbot/bot/inviter/
ssh -i web\deploy_key root@<bot-host> systemctl restart userbot
```

### SSL

При установке `install-on-server.sh` запрашивает сертификаты Let's Encrypt. Для inviter отдельно:

```bash
certbot --nginx -d inviter.exsender.top --non-interactive --agree-tos -m admin@exsender.top --redirect
```

---

## Переменные окружения

### Сайт (`web/.env`)

См. [web/.env.example](web/.env.example). Ключевые:

| Переменная | Описание |
|------------|----------|
| `SITE_PUBLIC_URL` | Публичный URL (`https://exsender.top`) |
| `SITE_COOKIE_DOMAIN` | Общая cookie для поддоменов (`.exsender.top`) |
| `INVITER_HOSTS` | Хосты инвайтера (`inviter.exsender.top`) |
| `SITE_DATA_DIR` | Персистентные JSON (`/opt/exsender/data`) |
| `SITE_SECRET` | Подпись cookie-сессий (обязательно сменить) |
| `CRYPTO_BOT_TOKEN` | Оплата через @CryptoBot |

Данные **не перезаписываются** при деплое: `users.json`, `bots.json` и др. лежат в `SITE_DATA_DIR`.

### Бот (`bot/.env`)

См. [bot/.env.example](bot/.env.example). Ключевые:

| Переменная | Описание |
|------------|----------|
| `API_ID`, `API_HASH` | Telegram API |
| `BOT_API_TOKEN` | Токен для сайта (генерируется при деплое) |
| `BOT_API_PORT` | Порт API (часто `8765`) |
| `TG_BOT_TOKEN` | Control-bot в Telegram (опционально) |

---

## Локальная разработка

### 1. Userbot

```powershell
cd bot
py -m pip install -r requirements.txt
copy .env.example .env
# Заполни API_ID, API_HASH
py main.py
```

API: `http://127.0.0.1:8080/api/local/health`

### 2. Сайт

```powershell
cd web
py -m pip install -r requirements.txt
copy .env.example .env
# Заполни SITE_SECRET
py main.py
```

Открой `http://127.0.0.1:3000/login`.

Для инвайтера локально добавь в `hosts`: `127.0.0.1 inviter.exsender.top` и в `.env`: `INVITER_HOSTS=inviter.exsender.top`.

---

## Аутентификация и доступ

| Роль | Где входит | Доступ |
|------|------------|--------|
| **Админ** | `SITE_LOGIN` / `SITE_ADMINS` | `/admin`, все VDS, inviter |
| **Пользователь** | Регистрация + подписка | `/app`, inviter (свои VDS) |

Сессия общая на `exsender.top` и `inviter.exsender.top` через cookie `domain=.exsender.top`.

Реестр ботов: у клиентских VDS поле `owner_id` = id пользователя. Пустой `owner_id` — legacy, виден только админу.

---

## API (кратко)

### Сайт

- `POST /api/auth/login`, `GET /api/auth/me` — сессия
- `GET/POST /api/bots` — реестр VDS (exsender)
- `POST /api/bots/{id}/deploy` — SSH-деплой
- `ANY /api/bots/{id}/proxy/{path}` — прокси в bot API
- `GET/POST /api/inviter/bots` — реестр для инвайтера
- `ANY /api/inviter/bots/{id}/{path}` — inviter API на VDS

### Userbot

- `GET /api/local/health` — без auth
- `GET /api/local/overview` — аккаунты, статус
- `POST /api/local/accounts/{id}/spam` — рассылка
- `POST /api/local/inviter/parse` — парс
- `POST /api/local/inviter/run` — запуск инвайта
- `GET /api/local/inviter/job` — статус job

Полный список — в `bot/bot_api.py` и `web/app.py`.

---

## Безопасность

- Смени `SITE_SECRET`, пароли админов и `BOT_API_TOKEN` перед продом
- Не коммить `web/.env`, `web/deploy_key`, `bot/.env`, `bots.json` с токенами
- Bot API с VDS доступен сайту через **SSH-туннель** (порт 8765 закрыт снаружи)
- CSRF включён для мутаций (`SITE_CSRF=1`)
- SSH-ключ деплоя: `web/deploy_key` → `authorized_keys` на VDS при первом deploy

---

## Требования

- **Сайт:** Python 3.11+, Ubuntu/Debian, nginx, certbot
- **Бот VDS:** Python 3.11+, systemd, исходящий доступ к Telegram
- **Разработка:** Windows/Linux, PowerShell для скриптов деплоя

---

## Ссылки

- Инвайтер (детали): [inviter/README.md](inviter/README.md)
- Идеи по сайту: [docs/site-feature-ideas.txt](docs/site-feature-ideas.txt)
- Оригинал логики инвайтера: [exbrute/exinviter](https://github.com/exbrute/exinviter)
