"""Provision the userbot on a remote VDS over SSH (asyncssh).

Lives in `web/` (the site). Talks to `web.registry.BotRegistry`.

Flow:
1. SSH-connect with the password the operator typed in the form.
2. Install a locally-generated ed25519 deploy key in authorized_keys.
3. apt-get install python3 + venv + pip.
4. SFTP-upload tar.gz of `bot/` (without sessions/state/.env).
5. python -m venv + pip install -r bot/requirements.txt.
6. Generate BOT_API_TOKEN (stored in registry), write .env on remote.
7. Install/enable/restart `userbot.service`.
8. Enable systemd timer — restart userbot every USERBOT_RESTART_HOURS (default 24).

Subsequent operations (restart/stop/uninstall) use the deploy key.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import secrets
import tarfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import asyncssh

from registry import BotRecord, BotRegistry

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent
BOT_DIR = REPO_ROOT / "bot"
DEPLOY_KEY = WEB_DIR / "deploy_key"
DEPLOY_KEY_PUB = WEB_DIR / "deploy_key.pub"

USERBOT_RESTART_HOURS = max(0, int(os.getenv("USERBOT_RESTART_HOURS", "24") or 24))


def _restart_hours_for(rec: BotRecord) -> int:
    """Per-server auto-restart interval (0 = disabled)."""
    try:
        h = int(rec.restart_interval_hours)
    except (TypeError, ValueError):
        h = USERBOT_RESTART_HOURS
    if h <= 0:
        return 0
    return min(h, 168)

EXCLUDE_NAMES = {
    "sessions",
    "__pycache__",
    "runtime_state.json",
    "bot_api_token.txt",
    ".env",
    ".env.example",
    "node_modules",
    ".git",
}
EXCLUDE_SUFFIXES = (
    ".pyc",
    ".log",
    ".session",
    ".session-journal",
    ".session-wal",
    ".session-shm",
    ".broken",
    ".tmp",
)


# -------------------------------------------------------------- progress log
class DeployState:
    __slots__ = ("status", "log", "started_at", "finished_at", "operation")

    def __init__(self) -> None:
        self.status: str = "idle"
        self.operation: str = ""
        self.log: list[str] = []
        self.started_at: float = 0.0
        self.finished_at: float = 0.0

    def add(self, line: str) -> None:
        ts = time.strftime("%H:%M:%S")
        for chunk in str(line).rstrip().splitlines() or [""]:
            self.log.append(f"[{ts}] {chunk}")
        if len(self.log) > 1000:
            self.log = self.log[-1000:]

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation": self.operation,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "log": list(self.log),
        }


_states: dict[str, DeployState] = {}
_locks: dict[str, asyncio.Lock] = {}


def get_state(bid: str) -> DeployState:
    if bid not in _states:
        _states[bid] = DeployState()
    return _states[bid]


def _lock_for(bid: str) -> asyncio.Lock:
    if bid not in _locks:
        _locks[bid] = asyncio.Lock()
    return _locks[bid]


# -------------------------------------------------------------- helpers
def _sh_quote(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def _ensure_deploy_key() -> str:
    if not DEPLOY_KEY.exists() or not DEPLOY_KEY_PUB.exists():
        key = asyncssh.generate_private_key("ssh-ed25519", comment="userbot-site-deploy")
        DEPLOY_KEY.write_bytes(key.export_private_key())
        DEPLOY_KEY_PUB.write_bytes(key.export_public_key())
        try:
            os.chmod(DEPLOY_KEY, 0o600)
        except OSError:
            pass
    return DEPLOY_KEY_PUB.read_text("utf-8").strip()


def _build_bot_tarball() -> bytes:
    if not BOT_DIR.is_dir():
        raise RuntimeError(
            f"Папка bot/ не найдена на сервере сайта ({BOT_DIR}). "
            "Залей bot/ рядом с web/ или запусти scripts/sync-site-bot.ps1."
        )
    req = BOT_DIR / "requirements.txt"
    if not req.is_file():
        raise RuntimeError(f"Нет {req} — неполный bot/ на сервере сайта.")

    buf = io.BytesIO()
    n_files = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, files in os.walk(BOT_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_NAMES]
            rel = os.path.relpath(root, BOT_DIR)
            for f in files:
                if f in EXCLUDE_NAMES:
                    continue
                if f.endswith(EXCLUDE_SUFFIXES):
                    continue
                full = os.path.join(root, f)
                arc = ("bot/" + f) if rel == "." else ("bot/" + rel.replace("\\", "/") + "/" + f)
                tar.add(full, arcname=arc, recursive=False)
                n_files += 1
    data = buf.getvalue()
    if n_files == 0 or len(data) < 512:
        raise RuntimeError(
            f"Пустой архив bot/ ({n_files} файлов). Проверь {BOT_DIR} на сервере exsender."
        )
    return data


def _build_env_file(env: dict[str, str]) -> str:
    lines: list[str] = []
    for k, v in env.items():
        if v is None:
            continue
        s = str(v).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{k}="{s}"')
    return "\n".join(lines) + "\n"


def _systemd_unit(install_dir: str, run_as: str) -> str:
    user_line = f"User={run_as}\n" if run_as and run_as != "root" else ""
    return (
        "[Unit]\n"
        "Description=Userbot (Telethon)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"{user_line}"
        f"WorkingDirectory={install_dir}/bot\n"
        f"EnvironmentFile={install_dir}/.env\n"
        f"ExecStart={install_dir}/.venv/bin/python main.py\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f"StandardOutput=append:{install_dir}/userbot.log\n"
        f"StandardError=append:{install_dir}/userbot.log\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _systemd_restart_service() -> str:
    return (
        "[Unit]\n"
        "Description=Scheduled userbot restart\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/bin/systemctl restart userbot\n"
    )


def _systemd_restart_timer(interval: str) -> str:
    return (
        "[Unit]\n"
        "Description=Periodic userbot restart\n"
        "\n"
        "[Timer]\n"
        f"OnBootSec={interval}\n"
        f"OnUnitActiveSec={interval}\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


async def _install_userbot_restart_timer(
    conn: asyncssh.SSHClientConnection,
    st: DeployState,
    *,
    sudo: bool,
    sudo_password: str,
    hours: int = USERBOT_RESTART_HOURS,
) -> None:
    """Install or remove systemd timer that restarts userbot every N hours."""
    if hours <= 0:
        await _run(
            conn,
            "systemctl disable --now userbot-restart.timer 2>/dev/null || true",
            st,
            sudo=sudo,
            sudo_password=sudo_password,
            check=False,
        )
        await _run(
            conn,
            "rm -f /etc/systemd/system/userbot-restart.service "
            "/etc/systemd/system/userbot-restart.timer",
            st,
            sudo=sudo,
            sudo_password=sudo_password,
            check=False,
        )
        await _run(conn, "systemctl daemon-reload", st, sudo=sudo, sudo_password=sudo_password)
        return

    interval = f"{hours}h"
    st.add(f"Таймер перезапуска userbot каждые {hours} ч…")
    async with conn.start_sftp_client() as sftp:
        async with sftp.open("/tmp/userbot-restart.service", "w") as f:
            await f.write(_systemd_restart_service())
        async with sftp.open("/tmp/userbot-restart.timer", "w") as f:
            await f.write(_systemd_restart_timer(interval))
    await _run(
        conn,
        "mv /tmp/userbot-restart.service /etc/systemd/system/userbot-restart.service",
        st,
        sudo=sudo,
        sudo_password=sudo_password,
    )
    await _run(
        conn,
        "mv /tmp/userbot-restart.timer /etc/systemd/system/userbot-restart.timer",
        st,
        sudo=sudo,
        sudo_password=sudo_password,
    )
    await _run(conn, "systemctl daemon-reload", st, sudo=sudo, sudo_password=sudo_password)
    await _run(
        conn,
        "systemctl enable --now userbot-restart.timer",
        st,
        sudo=sudo,
        sudo_password=sudo_password,
    )


# -------------------------------------------------------------- ssh
async def _connect(
    rec: BotRecord, password: Optional[str]
) -> asyncssh.SSHClientConnection:
    last_err: Optional[BaseException] = None
    if rec.has_ssh_key and DEPLOY_KEY.exists():
        try:
            return await asyncssh.connect(
                rec.host,
                port=rec.ssh_port,
                username=rec.ssh_user,
                client_keys=[str(DEPLOY_KEY)],
                known_hosts=None,
                connect_timeout=20,
            )
        except (OSError, asyncssh.Error) as e:
            last_err = e
            logger.warning("Deploy-key auth failed for %s: %s", rec.host, e)
    if not password:
        if last_err:
            raise RuntimeError(f"Ключ не сработал ({last_err}), а пароль не задан")
        raise RuntimeError("Пароль не задан и ключ ещё не установлен")
    return await asyncssh.connect(
        rec.host,
        port=rec.ssh_port,
        username=rec.ssh_user,
        password=password,
        known_hosts=None,
        connect_timeout=20,
    )


async def _run(
    conn: asyncssh.SSHClientConnection,
    cmd: str,
    st: DeployState,
    *,
    check: bool = True,
    sudo: bool = False,
    sudo_password: str = "",
    quiet_cmd: bool = False,
) -> asyncssh.SSHCompletedProcess:
    full = f"sudo -S -p '' bash -lc {_sh_quote(cmd)}" if sudo else cmd
    if not quiet_cmd:
        st.add(f"$ {cmd}")
    proc_input = (sudo_password + "\n") if (sudo and sudo_password) else None
    result = await conn.run(full, input=proc_input, check=False)
    out = (result.stdout or "").rstrip()
    err = (result.stderr or "").rstrip()
    if out:
        for line in out.splitlines()[-50:]:
            st.add(line)
    if err:
        for line in err.splitlines()[-50:]:
            if line.strip() in ("", "[sudo] password:"):
                continue
            st.add("! " + line)
    if check and result.exit_status != 0:
        raise RuntimeError(f"Команда упала ({result.exit_status}): {cmd}")
    return result


def _normalize_bot_env(env: dict[str, str], api_token: str, api_port: int) -> dict[str, str]:
    """Map deploy form keys → bot .env keys."""
    api_id = str(env.get("API_ID", "")).strip()
    api_hash = str(env.get("API_HASH", "")).strip()
    profile = str(
        env.get("TELEGRAM_CLIENT_PROFILE")
        or env.get("telegramClientProfile")
        or ""
    ).strip().lower()
    device_profile = str(
        env.get("TELEGRAM_DEVICE_PROFILE")
        or env.get("telegramDeviceProfile")
        or "tdesktop"
    ).strip().lower()
    if device_profile not in ("tdesktop", "android"):
        device_profile = "tdesktop"

    # Existing sessions: keep API_ID/API_HASH, only refresh device fingerprint.
    if api_id and api_hash:
        profile = "custom"
        merged: dict[str, str] = {
            "TELEGRAM_CLIENT_PROFILE": "custom",
            "TELEGRAM_DEVICE_PROFILE": device_profile,
            "API_ID": api_id,
            "API_HASH": api_hash,
            "BOT_TOKEN": str(env.get("BOT_TOKEN") or env.get("TG_BOT_TOKEN", "")).strip(),
            "ADMIN_USER_IDS": str(env.get("ADMIN_USER_IDS", "")).strip(),
            "BOT_API_TOKEN": api_token,
            "BOT_API_HOST": "0.0.0.0",
            "BOT_API_PORT": str(api_port),
            "BOT_API_ENABLED": "1",
            "ACCOUNTS": "",
        }
    else:
        if profile not in ("tdesktop", "android", "custom"):
            profile = "tdesktop"
        merged = {
            "TELEGRAM_CLIENT_PROFILE": profile,
            "BOT_TOKEN": str(env.get("BOT_TOKEN") or env.get("TG_BOT_TOKEN", "")).strip(),
            "ADMIN_USER_IDS": str(env.get("ADMIN_USER_IDS", "")).strip(),
            "BOT_API_TOKEN": api_token,
            "BOT_API_HOST": "0.0.0.0",
            "BOT_API_PORT": str(api_port),
            "BOT_API_ENABLED": "1",
            "ACCOUNTS": "",
        }
        if profile == "custom":
            merged["API_ID"] = api_id
            merged["API_HASH"] = api_hash

    out = {k: v for k, v in merged.items() if v != ""}
    out["ACCOUNTS"] = ""
    out["BOT_API_ENABLED"] = "1"
    out["TELEGRAM_CLIENT_PROFILE"] = out.get("TELEGRAM_CLIENT_PROFILE", profile or "tdesktop")
    return out


def _merge_remote_env(env: dict[str, str], remote: dict[str, str]) -> dict[str, str]:
    """Fill missing deploy fields from existing VDS .env (preserve sessions)."""
    merged = dict(env)
    for key in (
        "API_ID",
        "API_HASH",
        "TELEGRAM_CLIENT_PROFILE",
        "TELEGRAM_DEVICE_PROFILE",
        "ADMIN_USER_IDS",
    ):
        if not str(merged.get(key, "")).strip() and remote.get(key):
            merged[key] = remote[key]
    if not str(merged.get("TG_BOT_TOKEN", "")).strip():
        merged["TG_BOT_TOKEN"] = remote.get("BOT_TOKEN", remote.get("TG_BOT_TOKEN", ""))
    if not str(merged.get("BOT_TOKEN", "")).strip() and merged.get("TG_BOT_TOKEN"):
        merged["BOT_TOKEN"] = merged["TG_BOT_TOKEN"]
    return merged


async def _pick_api_port(conn: asyncssh.SSHClientConnection, preferred: int) -> int:
    """First free TCP port for bot API (8080 often taken by Apache on VDS)."""
    seen: set[int] = set()
    candidates: list[int] = []
    for p in (preferred, 8765, 8780, 9080, 9880):
        if p not in seen:
            candidates.append(p)
            seen.add(p)
    for p in candidates:
        r = await conn.run(
            f"ss -tlnH 2>/dev/null | grep -q ':{p} ' && echo busy || echo free",
            check=False,
        )
        if "free" in (r.stdout or ""):
            return p
    raise RuntimeError(
        f"нет свободного порта для API бота (пробовали {candidates}). "
        "На VDS занят 8080 — обычно Apache."
    )


async def _wait_bot_health(
    conn: asyncssh.SSHClientConnection,
    api_port: int,
    *,
    attempts: int = 8,
    delay: float = 2.0,
) -> None:
    for _ in range(attempts):
        r = await conn.run(
            f"curl -sf http://127.0.0.1:{api_port}/api/local/health",
            check=False,
        )
        if r.exit_status == 0 and (r.stdout or "").strip():
            return
        await asyncio.sleep(delay)
    log = await conn.run("tail -n 20 /opt/userbot/userbot.log 2>/dev/null || true", check=False)
    tail = (log.stdout or "").strip()
    hint = f"\n{tail}" if tail else ""
    raise RuntimeError(
        f"бот не ответил на http://127.0.0.1:{api_port}/api/local/health{hint}"
    )


# -------------------------------------------------------------- operations
async def deploy(
    bid: str,
    registry: BotRegistry,
    *,
    password: Optional[str],
    env: dict[str, str],
) -> None:
    rec = registry.get(bid)
    if rec is None:
        raise RuntimeError(f"Бот {bid} не найден")

    async with _lock_for(bid):
        st = get_state(bid)
        st.status = "running"
        st.operation = "deploy"
        st.started_at = time.time()
        st.finished_at = 0.0
        st.log = []
        registry.update(bid, status="deploying", last_error="")

        try:
            # Generate / reuse BOT_API_TOKEN for this bot
            token = rec.api_token or secrets.token_urlsafe(32)
            registry.update(bid, api_token=token)

            deploy_env = dict(env)
            if rec.has_ssh_key and rec.status != "new":
                try:
                    remote = await read_remote_env(rec, password=password)
                    deploy_env = _merge_remote_env(deploy_env, remote)
                except Exception:
                    pass

            full_env = _normalize_bot_env(deploy_env, token, rec.api_port)

            st.add(f"Подключаюсь к {rec.ssh_user}@{rec.host}:{rec.ssh_port}…")
            async with await _connect(rec, password) as conn:
                st.add("Подключено.")

                who = await conn.run("id -u", check=True)
                is_root = (who.stdout or "").strip() == "0"
                sudo_password = "" if is_root else (password or "")
                sudo_needed = not is_root

                async def sh(cmd: str, check: bool = True, sudo: bool = False) -> Any:
                    return await _run(
                        conn,
                        cmd,
                        st,
                        check=check,
                        sudo=sudo and sudo_needed,
                        sudo_password=sudo_password,
                    )

                # pick API port before writing .env
                api_port = await _pick_api_port(conn, rec.api_port)
                if api_port != rec.api_port:
                    st.add(f"Порт {rec.api_port} занят (часто Apache) → используем {api_port}")
                    registry.update(bid, api_port=api_port)
                    rec = registry.get(bid) or rec
                full_env = _normalize_bot_env(env, token, api_port)

                # 1) deploy key
                pub = _ensure_deploy_key()
                await sh("mkdir -p ~/.ssh && chmod 700 ~/.ssh")
                await sh(
                    f"grep -qxF {_sh_quote(pub)} ~/.ssh/authorized_keys 2>/dev/null "
                    f"|| (echo {_sh_quote(pub)} >> ~/.ssh/authorized_keys && "
                    "chmod 600 ~/.ssh/authorized_keys)"
                )

                # 2) system deps
                st.add("apt-get install python3 / venv / pip…")
                await sh("DEBIAN_FRONTEND=noninteractive apt-get update -y", sudo=True)
                await sh(
                    "DEBIAN_FRONTEND=noninteractive apt-get install -y "
                    "python3 python3-venv python3-pip ca-certificates tar gzip",
                    sudo=True,
                )

                # 3) install dir
                install_dir = rec.install_dir
                await sh(f"mkdir -p {_sh_quote(install_dir)}", sudo=True)
                await sh(
                    f"chown -R {rec.ssh_user}:{rec.ssh_user} {_sh_quote(install_dir)}",
                    sudo=True,
                    check=False,
                )

                # 4) stop existing
                await sh("systemctl stop userbot 2>/dev/null || true", sudo=True, check=False)

                # 5) upload tarball
                st.add("Пакую файлы…")
                data = _build_bot_tarball()
                st.add(f"Загружаю {len(data) // 1024} KiB…")
                remote_tar = f"{install_dir}/_deploy.tar.gz"
                async with conn.start_sftp_client() as sftp:
                    async with sftp.open(remote_tar, "wb") as f:
                        await f.write(data)
                await sh(f"tar -xzf {_sh_quote(remote_tar)} -C {_sh_quote(install_dir)}")
                await sh(f"rm -f {_sh_quote(remote_tar)}")

                # 6) venv + deps
                st.add("Создаю venv и ставлю зависимости (1–2 минуты)…")
                await sh(f"cd {_sh_quote(install_dir)} && python3 -m venv .venv")
                await sh(f"cd {_sh_quote(install_dir)} && .venv/bin/pip install --upgrade pip --quiet")
                await sh(
                    f"cd {_sh_quote(install_dir)} && "
                    ".venv/bin/pip install -r bot/requirements.txt --quiet"
                )

                # 7) .env via sftp
                st.add("Пишу .env (с BOT_API_TOKEN)…")
                env_text = _build_env_file(full_env)
                async with conn.start_sftp_client() as sftp:
                    tmp_env = f"{install_dir}/.env.tmp"
                    async with sftp.open(tmp_env, "w") as f:
                        await f.write(env_text)
                await sh(f"mv {_sh_quote(install_dir)}/.env.tmp {_sh_quote(install_dir)}/.env")
                await sh(f"chmod 600 {_sh_quote(install_dir)}/.env")

                # 8) systemd unit
                st.add("Создаю systemd unit…")
                unit = _systemd_unit(install_dir, rec.ssh_user)
                async with conn.start_sftp_client() as sftp:
                    async with sftp.open("/tmp/userbot.service", "w") as f:
                        await f.write(unit)
                await sh(
                    "mv /tmp/userbot.service /etc/systemd/system/userbot.service",
                    sudo=True,
                )
                await sh("systemctl daemon-reload", sudo=True)
                await sh("systemctl enable userbot", sudo=True)
                await sh("systemctl restart userbot", sudo=True)

                await _install_userbot_restart_timer(
                    conn,
                    st,
                    sudo=sudo_needed,
                    sudo_password=sudo_password,
                    hours=_restart_hours_for(rec),
                )

                # 9) smoke + API health
                st.add("Проверяю API бота…")
                await _wait_bot_health(conn, api_port)
                await sh(
                    "systemctl --no-pager status userbot | head -n 12",
                    sudo=True,
                    check=False,
                )

                registry.update(
                    bid,
                    status="running",
                    last_deploy_at=time.time(),
                    has_ssh_key=True,
                    last_error="",
                    api_port=api_port,
                )
                st.add(
                    f"✅ Готово. Bot API: порт {api_port} "
                    f"(health OK на 127.0.0.1:{api_port})"
                )
                st.status = "success"
        except Exception as e:
            logger.exception("deploy failed for %s: %s", bid, e)
            st.add(f"❌ Ошибка: {e}")
            st.status = "error"
            registry.update(bid, status="error", last_error=str(e))
        finally:
            st.finished_at = time.time()


async def _simple_op(
    bid: str,
    registry: BotRegistry,
    *,
    op_name: str,
    password: Optional[str],
    cmds: list[str],
    on_success_status: Optional[str] = None,
    after_cmds: Optional[
        Callable[
            [asyncssh.SSHClientConnection, DeployState, bool, str],
            Awaitable[None],
        ]
    ] = None,
) -> None:
    rec = registry.get(bid)
    if rec is None:
        raise RuntimeError(f"Бот {bid} не найден")
    async with _lock_for(bid):
        st = get_state(bid)
        st.status = "running"
        st.operation = op_name
        st.started_at = time.time()
        st.finished_at = 0.0
        st.log = []
        st.add(f"=== {op_name} ===")
        try:
            async with await _connect(rec, password) as conn:
                who = await conn.run("id -u", check=True)
                is_root = (who.stdout or "").strip() == "0"
                sudo_pw = "" if is_root else (password or "")
                for cmd in cmds:
                    await _run(conn, cmd, st, sudo=not is_root, sudo_password=sudo_pw, check=False)
                if after_cmds is not None:
                    await after_cmds(conn, st, not is_root, sudo_pw)
            st.status = "success"
            if on_success_status:
                registry.update(bid, status=on_success_status, last_error="")
            st.add("Готово.")
        except Exception as e:
            st.add(f"Ошибка: {e}")
            st.status = "error"
            registry.update(bid, status="error", last_error=str(e))
        finally:
            st.finished_at = time.time()


async def read_remote_env(
    rec: BotRecord, *, password: Optional[str] = None
) -> dict[str, str]:
    """Read existing .env from VDS (for redeploy without re-entering API keys)."""
    env: dict[str, str] = {}
    async with await _connect(rec, password) as conn:
        r = await conn.run(
            f"cat {_sh_quote(rec.install_dir + '/.env')}",
            check=False,
        )
        if r.exit_status != 0:
            raise RuntimeError("Не удалось прочитать .env на сервере")
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            env[k] = v
    return env


async def restart_remote(bid: str, registry: BotRegistry, *, password: Optional[str] = None) -> None:
    rec = registry.get(bid)
    if rec is None:
        raise RuntimeError(f"Бот {bid} не найден")
    hours = _restart_hours_for(rec)

    async def _ensure_timer(
        conn: asyncssh.SSHClientConnection,
        st: DeployState,
        sudo: bool,
        sudo_password: str,
    ) -> None:
        await _install_userbot_restart_timer(
            conn, st, sudo=sudo, sudo_password=sudo_password, hours=hours
        )

    await _simple_op(
        bid, registry, op_name="restart", password=password,
        cmds=["systemctl restart userbot"], on_success_status="running",
        after_cmds=_ensure_timer,
    )


async def sync_restart_timer_remote(
    bid: str, registry: BotRegistry, *, password: Optional[str] = None
) -> None:
    rec = registry.get(bid)
    if rec is None:
        raise RuntimeError(f"Бот {bid} не найден")
    hours = _restart_hours_for(rec)

    async def _only_timer(
        conn: asyncssh.SSHClientConnection,
        st: DeployState,
        sudo: bool,
        sudo_password: str,
    ) -> None:
        await _install_userbot_restart_timer(
            conn, st, sudo=sudo, sudo_password=sudo_password, hours=hours
        )

    await _simple_op(
        bid,
        registry,
        op_name="sync_timer",
        password=password,
        cmds=[],
        after_cmds=_only_timer,
    )


async def stop_remote(bid: str, registry: BotRegistry, *, password: Optional[str] = None) -> None:
    await _simple_op(
        bid, registry, op_name="stop", password=password,
        cmds=["systemctl stop userbot"], on_success_status="stopped",
    )


async def uninstall_remote(
    bid: str, registry: BotRegistry, *, password: Optional[str] = None
) -> None:
    rec = registry.get(bid)
    if rec is None:
        return
    await _simple_op(
        bid, registry, op_name="uninstall", password=password,
        cmds=[
            "systemctl stop userbot 2>/dev/null || true",
            "systemctl disable userbot 2>/dev/null || true",
            "systemctl disable --now userbot-restart.timer 2>/dev/null || true",
            "rm -f /etc/systemd/system/userbot.service",
            "rm -f /etc/systemd/system/userbot-restart.service",
            "rm -f /etc/systemd/system/userbot-restart.timer",
            "systemctl daemon-reload",
            f"rm -rf {_sh_quote(rec.install_dir)}",
        ],
        on_success_status="stopped",
    )
