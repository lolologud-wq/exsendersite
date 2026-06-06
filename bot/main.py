import asyncio
import logging
import os

from dotenv import load_dotenv
from telethon import TelegramClient

from bot_api import serve_bot_api
from bot_service import BotService
from inviter.service import InviterService
from health import restore_from_persisted
from spam_scheduler import start_spam_loop_background
from state import (
    load_multi_account_state,
    prune_phantom_default_slot,
    save_multi_account_state,
)
from telethon_accounts import connect_client_with_fallback, make_telethon_client
from telethon_client_profile import get_telegram_api_config

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

API_CONFIG = get_telegram_api_config()
API_ID = API_CONFIG.api_id
API_HASH = API_CONFIG.api_hash
logger.info(
    "Telegram API: profile=%s api_id=%s device=%s",
    API_CONFIG.profile,
    API_ID,
    API_CONFIG.device_model,
)


async def run_control_bot_polling(app) -> None:
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Future()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


async def run_control_bot_forever(
    token: str,
    multi,
    telethon_clients: dict[str, TelegramClient],
    boot_account_ids: frozenset[str],
    authorized_at_boot: frozenset[str],
) -> None:
    from control_bot import build_application

    while True:
        app = build_application(
            multi,
            token,
            telethon_clients,
            boot_account_ids=boot_account_ids,
            authorized_at_boot=authorized_at_boot,
        )
        try:
            await run_control_bot_polling(app)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Control bot недоступен (TimedOut/сеть). Повтор через 15 секунд."
            )
            await asyncio.sleep(15)


async def main() -> None:
    multi = load_multi_account_state()
    if prune_phantom_default_slot(multi):
        save_multi_account_state(multi)
        logger.info("Удалён пустой слот 'default' (не задан в ACCOUNTS).")

    for aid, st in multi.accounts.items():
        restore_from_persisted(
            aid,
            errors_total=int(st.errors_total or 0),
            last_error=st.last_error or "",
            last_error_kind=st.last_error_kind or "",
            last_error_at=float(st.last_error_at or 0),
            last_error_chat_id=st.last_error_chat_id,
        )

    if os.getenv("STOP_SPAM_ON_START", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        for st in multi.accounts.values():
            st.spam_running = False
        save_multi_account_state(multi)
        logger.info("Спам сброшен при старте (STOP_SPAM_ON_START).")
    elif any(st.spam_running for st in multi.accounts.values()):
        logger.info("Спам продолжится после старта (runtime_state.json).")

    telethon_clients: dict[str, TelegramClient] = {}
    for aid in multi.account_order:
        if aid not in multi.accounts:
            continue
        telethon_clients[aid] = make_telethon_client(
            aid,
            API_ID,
            API_HASH,
            proxy_raw=multi.accounts[aid].proxy,
            profile=API_CONFIG,
        )

    authorized_at_boot: set[str] = set()
    for aid, client in telethon_clients.items():
        connected = False
        try:
            client = await connect_client_with_fallback(
                client,
                account_id=aid,
                api_id=API_ID,
                api_hash=API_HASH,
                proxy_raw=multi.accounts[aid].proxy,
                allow_direct_fallback=True,
                profile=API_CONFIG,
            )
            telethon_clients[aid] = client
            connected = True
        except Exception as e:
            logger.warning(
                "Userbot [%s]: не удалось подключиться (%s). "
                "Слот останется офлайн до ручного переподключения.",
                aid,
                e,
            )
        if connected:
            if await client.is_user_authorized():
                authorized_at_boot.add(aid)
                me = await client.get_me()
                logger.info("Userbot [%s] запущен @%s (%s)", aid, me.username or "-", me.id)
            else:
                logger.warning(
                    "Userbot [%s]: нет авторизации — в боте: Аккаунты → нажмите этот слот.",
                    aid,
                )
        start_spam_loop_background(
            telethon_clients[aid],
            multi.accounts[aid],
            persist=lambda: save_multi_account_state(multi),
            account_key=aid,
        )

    authorized_boot_frozen = frozenset(authorized_at_boot)
    if (
        multi.active_account_id not in authorized_at_boot
        and authorized_at_boot
    ):
        for aid in multi.account_order:
            if aid in authorized_at_boot:
                multi.active_account_id = aid
                save_multi_account_state(multi)
                logger.info(
                    "Активный слот переключён на %s (предыдущий офлайн).",
                    aid,
                )
                break
    authorized_clients = [
        telethon_clients[aid]
        for aid in multi.account_order
        if aid in telethon_clients and aid in authorized_at_boot
    ]

    token = os.getenv("BOT_TOKEN", "").strip()
    admins_raw = os.getenv("ADMIN_USER_IDS", "").strip()
    boot_ids = frozenset(multi.account_order)

    background_tasks: list[asyncio.Task] = []

    if os.getenv("BOT_API_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off"):
        bot_service = BotService(
            multi,
            telethon_clients,
            api_id=API_ID,
            api_hash=API_HASH,
            api_config=API_CONFIG,
            save=lambda: save_multi_account_state(multi),
        )
        inviter_service = InviterService(bot_service)
        background_tasks.append(
            asyncio.create_task(serve_bot_api(bot_service, inviter_service))
        )

    if token:
        if not admins_raw:
            logger.warning(
                "ADMIN_USER_IDS пуст — бот управления всё равно запущен; "
                "отправьте /start боту — там подскажет, что добавить в .env."
            )
        background_tasks.append(
            asyncio.create_task(
                run_control_bot_forever(
                    token, multi, telethon_clients, boot_ids, authorized_boot_frozen
                )
            )
        )

    try:
        if authorized_clients:
            await asyncio.gather(*(c.run_until_disconnected() for c in authorized_clients))
        else:
            logger.info("Нет авторизованных userbot при старте — ждём вход через бота / дашборд.")
            await asyncio.Future()
    finally:
        for t in background_tasks:
            t.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
