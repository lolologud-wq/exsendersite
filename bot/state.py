import json
import logging
import os
import random
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

STATE_PATH = os.path.join(os.path.dirname(__file__), "runtime_state.json")
_state_lock = threading.Lock()
logger = logging.getLogger(__name__)


@dataclass
class ChatSpamConfig:
    enabled: bool = False
    custom_message: Optional[str] = None
    text_variants: list[str] = field(default_factory=list)
    custom_interval_min: Optional[float] = None
    custom_interval_jitter: Optional[float] = None
    extra_text: str = ""
    source_channel_id: Optional[int] = None
    # Конкретный пост канала (если None — берётся последний). Задаётся ссылкой на сообщение в боте.
    source_message_id: Optional[int] = None
    # При True и заданном source_channel_id — пересылка последнего поста; иначе копирование текста.
    source_forward: bool = False
    message_limit: Optional[int] = None
    messages_sent: int = 0
    start_delay_min: Optional[float] = None
    last_sent_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "custom_message": self.custom_message,
            "text_variants": list(self.text_variants or []),
            "custom_interval_min": self.custom_interval_min,
            "custom_interval_jitter": self.custom_interval_jitter,
            "extra_text": self.extra_text,
            "source_channel_id": self.source_channel_id,
            "source_message_id": self.source_message_id,
            "source_forward": self.source_forward,
            "message_limit": self.message_limit,
            "messages_sent": self.messages_sent,
            "start_delay_min": self.start_delay_min,
            "last_sent_at": float(self.last_sent_at or 0),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatSpamConfig":
        cij = d.get("custom_interval_jitter")
        tv = d.get("text_variants")
        if isinstance(tv, list):
            variants = [str(x) for x in tv if str(x).strip()]
        else:
            variants = []
        return cls(
            enabled=bool(d.get("enabled", False)),
            custom_message=d.get("custom_message"),
            text_variants=variants,
            custom_interval_min=(
                float(d["custom_interval_min"])
                if d.get("custom_interval_min") is not None
                else None
            ),
            custom_interval_jitter=(
                float(cij) if cij is not None else None
            ),
            extra_text=str(d.get("extra_text", "") or ""),
            source_channel_id=(
                int(d["source_channel_id"])
                if d.get("source_channel_id") is not None
                else None
            ),
            source_message_id=(
                int(d["source_message_id"])
                if d.get("source_message_id") is not None
                else None
            ),
            source_forward=bool(d.get("source_forward", False)),
            message_limit=(
                int(d["message_limit"])
                if d.get("message_limit") is not None
                else None
            ),
            messages_sent=int(d.get("messages_sent", 0) or 0),
            start_delay_min=(
                float(d["start_delay_min"])
                if d.get("start_delay_min") is not None
                else None
            ),
            last_sent_at=float(d.get("last_sent_at", 0) or 0),
        )


@dataclass
class RuntimeState:
    spam_running: bool = False
    default_message: str = ""
    default_interval_min: float = 5.0
    default_interval_jitter: float = 0.0
    # Прокси для Telethon этого слота (логин:пароль@ip:port, ip:port, socks5://..., http://...).
    proxy: Optional[str] = None
    # Для новых привязок «канал / id / ссылка на пост» в чате (в самом чате можно переключить).
    default_source_forward: bool = True
    # Один источник для всех чатов, у которых в настройках чата не задан свой канал.
    global_source_channel_id: Optional[int] = None
    global_source_message_id: Optional[int] = None
    errors_total: int = 0
    # Bumped when interval settings change — spam_loop clears per-chat timers.
    interval_seq: int = 0
    # Wall-clock timestamp of last send on this account (survives restart/redeploy).
    last_send_at: float = 0.0
    chat_configs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def cfg(self, chat_id: int) -> ChatSpamConfig:
        raw = self.chat_configs.get(str(chat_id))
        if not raw:
            return ChatSpamConfig()
        return ChatSpamConfig.from_dict(raw)

    def set_cfg(self, chat_id: int, cfg: ChatSpamConfig) -> None:
        self.chat_configs[str(chat_id)] = cfg.to_dict()


@dataclass
class MultiAccountState:
    """Несколько userbot-аккаунтов в одном runtime_state.json."""

    active_account_id: str = ""
    account_order: list[str] = field(default_factory=list)
    accounts: dict[str, RuntimeState] = field(default_factory=dict)


def active_runtime_state(multi: MultiAccountState) -> RuntimeState:
    if multi.active_account_id and multi.active_account_id in multi.accounts:
        return multi.accounts[multi.active_account_id]
    # Fallback: return an empty in-memory state when nothing is configured yet
    # (e.g. a freshly-deployed VDS that hasn't had any slots added).
    return RuntimeState()


def _runtime_state_from_flat_dict(raw: dict[str, Any]) -> RuntimeState:
    chat_configs: dict[str, dict[str, Any]] = {}
    if isinstance(raw.get("chat_configs"), dict):
        chat_configs = {str(k): dict(v) for k, v in raw["chat_configs"].items()}

    if not chat_configs and raw.get("mirror_to_chat_ids"):
        for x in raw["mirror_to_chat_ids"]:
            cid = str(int(x))
            chat_configs[cid] = ChatSpamConfig(enabled=True).to_dict()
    elif not chat_configs and raw.get("target_chat_id") is not None:
        chat_configs[str(int(raw["target_chat_id"]))] = ChatSpamConfig(
            enabled=True
        ).to_dict()

    spam_running = bool(raw.get("spam_running", raw.get("mirroring_enabled", False)))
    dj = float(raw.get("default_interval_jitter", 0) or 0)
    dj = max(0.0, min(0.95, dj))
    di = float(raw.get("default_interval_min", 5) or 5)
    if di <= 0:
        di = 5.0
    dsf = raw.get("default_source_forward")
    default_source_forward = True if dsf is None else bool(dsf)
    gch = raw.get("global_source_channel_id")
    gmid = raw.get("global_source_message_id")
    return RuntimeState(
        spam_running=spam_running,
        default_message=str(raw.get("default_message", "") or ""),
        default_interval_min=di,
        default_interval_jitter=dj,
        proxy=(str(raw["proxy"]).strip() or None) if raw.get("proxy") else None,
        default_source_forward=default_source_forward,
        global_source_channel_id=(int(gch) if gch is not None else None),
        global_source_message_id=(int(gmid) if gmid is not None else None),
        errors_total=int(raw.get("errors_total", 0) or 0),
        interval_seq=int(raw.get("interval_seq", 0) or 0),
        last_send_at=float(raw.get("last_send_at", 0) or 0),
        chat_configs=chat_configs,
    )


def _read_state_json_raw() -> dict[str, Any] | None:
    if not os.path.isfile(STATE_PATH):
        return None
    try:
        with _state_lock:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        backup = STATE_PATH + ".broken"
        try:
            if os.path.isfile(STATE_PATH):
                os.replace(STATE_PATH, backup)
        except OSError:
            pass
        logger.warning(
            "Не удалось прочитать %s (%s). Создан новый state; бэкап: %s",
            STATE_PATH,
            e,
            backup if os.path.isfile(backup) else "нет",
        )
        return None


def load_multi_account_state() -> MultiAccountState:
    env_ids = default_account_ids_from_env()
    raw = _read_state_json_raw()
    if raw is None:
        # No state file yet and ACCOUNTS env is empty — start with zero slots.
        if not env_ids:
            return MultiAccountState(
                active_account_id="",
                account_order=[],
                accounts={},
            )
        return MultiAccountState(
            active_account_id=env_ids[0],
            account_order=list(env_ids),
            accounts={aid: RuntimeState() for aid in env_ids},
        )

    if isinstance(raw.get("accounts"), dict):
        accounts: dict[str, RuntimeState] = {}
        for k, v in raw["accounts"].items():
            aid = str(k).strip()
            if not aid:
                continue
            if isinstance(v, dict):
                accounts[aid] = _runtime_state_from_flat_dict(v)
            else:
                accounts[aid] = RuntimeState()
        # Only fall back to flat-state import if ACCOUNTS env actually requests it
        if not accounts and env_ids:
            accounts[env_ids[0]] = _runtime_state_from_flat_dict(raw)

        file_order_raw = raw.get("account_order")
        if isinstance(file_order_raw, list) and any(str(x).strip() for x in file_order_raw):
            order = [
                str(x).strip()
                for x in file_order_raw
                if str(x).strip()
            ]
        else:
            order = []
            for e in env_ids:
                if e in accounts:
                    order.append(e)
            for k in accounts:
                if k not in order:
                    order.append(k)

        # Auto-create only the slots explicitly listed in ACCOUNTS env
        for e in env_ids:
            if e not in accounts:
                accounts[e] = RuntimeState()
        for k in accounts:
            if k not in order:
                order.append(k)

        active = str(raw.get("active_account_id") or "").strip()
        if not active or active not in accounts:
            active = order[0] if order else (next(iter(accounts)) if accounts else "")
        return MultiAccountState(
            active_account_id=active,
            account_order=order,
            accounts=accounts,
        )

    # Legacy flat state — only import when ACCOUNTS env has at least one id
    if not env_ids:
        return MultiAccountState(
            active_account_id="",
            account_order=[],
            accounts={},
        )
    st = _runtime_state_from_flat_dict(raw)
    accounts = {env_ids[0]: st}
    for a in env_ids[1:]:
        accounts[a] = RuntimeState()
    return MultiAccountState(
        active_account_id=env_ids[0],
        account_order=list(env_ids),
        accounts=accounts,
    )


def save_multi_account_state(multi: MultiAccountState) -> None:
    seen: set[str] = set()
    order: list[str] = []
    for x in multi.account_order:
        if x in multi.accounts and x not in seen:
            order.append(x)
            seen.add(x)
    for k in multi.accounts:
        if k not in seen:
            order.append(k)
            seen.add(k)
    multi.account_order = order
    if multi.active_account_id not in multi.accounts:
        multi.active_account_id = order[0] if order else ""

    payload = json.dumps(
        {
            "active_account_id": multi.active_account_id,
            "account_order": list(multi.account_order),
            "accounts": {k: asdict(v) for k, v in multi.accounts.items()},
        },
        indent=2,
        ensure_ascii=False,
    )
    d = os.path.dirname(STATE_PATH) or "."
    with _state_lock:
        fd, tmp_path = tempfile.mkstemp(
            suffix=".json.tmp", prefix="state_", dir=d, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp_path, STATE_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def default_account_ids_from_env() -> list[str]:
    """Return list of slot ids requested via ACCOUNTS env.

    If ACCOUNTS is explicitly set (even to empty string) — respect it as-is.
    If ACCOUNTS is unset at all — keep legacy behaviour with a single 'default'
    slot to avoid breaking standalone bot.py runs.
    """
    raw_env = os.getenv("ACCOUNTS")
    if raw_env is None:
        return ["default"]
    parts = [x.strip() for x in raw_env.split(",") if x.strip()]
    return parts


def load_state() -> RuntimeState:
    """Совместимость: активный аккаунт из multi-файла (см. ACCOUNTS в .env)."""
    return active_runtime_state(load_multi_account_state())


def enabled_chat_ids(state: RuntimeState) -> list[int]:
    out: list[int] = []
    for k, v in state.chat_configs.items():
        if v.get("enabled"):
            try:
                out.append(int(k))
            except ValueError:
                continue
    return out


def effective_interval_min(state: RuntimeState, chat_id: int) -> float:
    c = state.cfg(chat_id)
    if c.custom_interval_min is not None and c.custom_interval_min > 0:
        return float(c.custom_interval_min)
    return max(0.1, float(state.default_interval_min or 5))


def effective_jitter(state: RuntimeState, chat_id: int) -> float:
    c = state.cfg(chat_id)
    j = c.custom_interval_jitter
    if j is None:
        j = state.default_interval_jitter
    j = float(j or 0)
    return max(0.0, min(0.95, j))


def random_interval_seconds(state: RuntimeState, chat_id: int) -> float:
    base_sec = effective_interval_min(state, chat_id) * 60.0
    j = effective_jitter(state, chat_id)
    if j <= 0:
        return base_sec
    factor = 1.0 + random.uniform(-j, j)
    factor = max(0.1, factor)
    return base_sec * factor


def effective_body_text(
    state: RuntimeState, chat_id: int, *, pick_random_variant: bool = False
) -> str:
    c = state.cfg(chat_id)
    vars_non_empty = [str(x).strip() for x in (c.text_variants or []) if str(x).strip()]
    if vars_non_empty:
        if pick_random_variant:
            return random.choice(vars_non_empty)
        return vars_non_empty[0]
    base = (
        c.custom_message
        if c.custom_message is not None and str(c.custom_message).strip()
        else state.default_message
    )
    return (base or "").strip()


def effective_send_text(
    state: RuntimeState, chat_id: int, *, pick_random_variant: bool = False
) -> str:
    body = effective_body_text(state, chat_id, pick_random_variant=pick_random_variant)
    extra = (state.cfg(chat_id).extra_text or "").strip()
    if extra and body:
        return body + "\n" + extra
    return body or extra


def chat_has_resolved_text(state: RuntimeState, chat_id: int) -> bool:
    c = state.cfg(chat_id)
    if c.source_channel_id is not None:
        return True
    if state.global_source_channel_id is not None:
        return True
    vars_non_empty = [str(x).strip() for x in (c.text_variants or []) if str(x).strip()]
    if vars_non_empty:
        return True
    return bool(effective_body_text(state, chat_id).strip())


def validate_spam_start(state: RuntimeState, client_connected: bool) -> tuple[bool, str]:
    from proxy_util import parse_proxy

    if not client_connected:
        return False, "Userbot не подключён к Telegram (нет сессии или сеть)."
    if state.proxy and str(state.proxy).strip() and parse_proxy(state.proxy) is None:
        return (
            False,
            "Прокси указан, но формат неверный. Используйте логин:пароль@ip:порт или ip:порт.",
        )
    ids = enabled_chat_ids(state)
    if not ids:
        return False, "Нет чатов с ✅ — включите хотя бы один в разделе «Чаты»."
    if state.default_interval_min <= 0:
        for cid in ids:
            ci = state.cfg(cid).custom_interval_min
            if ci is None or ci <= 0:
                return (
                    False,
                    "Задайте стандартный интервал &gt; 0 (Настройки) или свой интервал для каждого включённого чата.",
                )
    for cid in ids:
        if effective_interval_min(state, cid) <= 0:
            return False, f"Интервал для чата <code>{cid}</code> должен быть &gt; 0."
        c = state.cfg(cid)
        if c.source_channel_id is not None:
            continue
        if not chat_has_resolved_text(state, cid):
            return (
                False,
                f"Пустой текст для чата <code>{cid}</code>. Задайте стандартный текст, кастомный, варианты, общий или чатовый канал-источник.",
            )
    return True, ""

