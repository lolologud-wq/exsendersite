# EX Inviter (web)

Веб-панель инвайтера для exsender — порт логики из [exbrute/exinviter](https://github.com/exbrute/exinviter).

## Где открывается

- **Поддомен:** https://inviter.exsender.top
- **Старый путь:** https://exsender.top/inviter → редирект на поддомен

Доступ для **всех пользователей** exsender с активной подпиской (те же логины, что и `/app`). Админы видят все VDS; клиенты — только свои.

## Возможности

- парс участников чата по ссылке в очередь SQLite на VDS;
- выбор target-чата;
- запуск инвайта с limit/delay;
- несколько аккаунтов через слоты userbot на выбранном VDS.

## Код в репозитории

| Часть | Путь |
|-------|------|
| UI | `frontend/inviter.html`, `frontend/js/inviter.js` |
| API сайта | `web/app.py` → `/api/inviter/...` |
| Userbot | `bot/inviter/` (service, db, errors) |
| Nginx | `deploy/site/nginx-inviter.conf` |

Оригинальный Telegram-бот из exinviter не запускается отдельно — функции встроены в userbot API и веб-UI.

## DNS

```
inviter.exsender.top  A  <IP сервера exsender>
```

После деплоя: `certbot --nginx -d inviter.exsender.top`

В `.env` сайта:

```env
SITE_COOKIE_DOMAIN=.exsender.top
INVITER_HOSTS=inviter.exsender.top
```
