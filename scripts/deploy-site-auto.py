"""Non-interactive site deploy (password via EXSENDER_SSH_PASSWORD env var)."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import asyncssh

ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get("EXSENDER_SSH_HOST", "178.236.252.6")
USER = os.environ.get("EXSENDER_SSH_USER", "root")
PORT = int(os.environ.get("EXSENDER_SSH_PORT", "22"))
DOMAIN = os.environ.get("EXSENDER_DOMAIN", "exsender.top")


async def main() -> int:
    password = os.environ.get("EXSENDER_SSH_PASSWORD", "").strip()
    if not password:
        print("Set EXSENDER_SSH_PASSWORD", file=sys.stderr)
        return 1

    tmp = Path(tempfile.gettempdir()) / "exsender-site.tar.gz"
    install_lf = Path(tempfile.gettempdir()) / "install-on-server.sh"

    print(f"==> Packing site from {ROOT}")
    excludes = [
        "--exclude=web/.env",
        "--exclude=web/users.json",
        "--exclude=web/bots.json",
        "--exclude=web/invoices.json",
        "--exclude=web/promos.json",
        "--exclude=web/notifications.json",
        "--exclude=web/admin_audit.json",
        "--exclude=web/security_state.json",
        "--exclude=web/.venv",
        "--exclude=web/__pycache__",
    ]
    cmd = ["tar", "-czf", str(tmp), *excludes, "-C", str(ROOT), "web", "frontend", "bot", "deploy/site"]
    subprocess.run(cmd, check=True)

    install_src = ROOT / "deploy" / "site" / "install-on-server.sh"
    install_lf.write_text(install_src.read_text(encoding="utf-8").replace("\r\n", "\n"), encoding="utf-8")

    print(f"==> Upload to {USER}@{HOST}")
    async with asyncssh.connect(
        HOST,
        port=PORT,
        username=USER,
        password=password,
        known_hosts=None,
        connect_timeout=30,
    ) as conn:
        async with conn.start_sftp_client() as sftp:
            await sftp.put(str(tmp), "/tmp/exsender-site.tar.gz")
            await sftp.put(str(install_lf), "/tmp/install-on-server.sh")

        print("==> Install on server")
        install_cmd = (
            "sed -i 's/\\r$//' /tmp/install-on-server.sh; "
            "chmod +x /tmp/install-on-server.sh; "
            f"bash /tmp/install-on-server.sh {DOMAIN} /tmp/exsender-site.tar.gz"
        )
        result = await conn.run(install_cmd, check=False)
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
        if result.exit_status != 0:
            print(f"Install failed: exit {result.exit_status}", file=sys.stderr)
            return result.exit_status

        verify = await conn.run(
            "systemctl is-active exsender nginx && curl -sI http://127.0.0.1:3000/login | head -3",
            check=False,
        )
        print(verify.stdout or verify.stderr)

    print(f"\nDone. Open https://{DOMAIN}/login")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
