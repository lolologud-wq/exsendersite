from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "inviter.db"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path: Path | None = None) -> Path:
    path = db_path or DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS invite_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                source_chat_id TEXT NOT NULL,
                source_chat_title TEXT NOT NULL,
                tg_user_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL,
                UNIQUE(account_id, tg_user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS parsed_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                source_chat_id TEXT NOT NULL,
                source_chat_title TEXT NOT NULL,
                parsed_at TEXT NOT NULL,
                UNIQUE(account_id, source_chat_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS invite_blocklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                tg_user_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(account_id, tg_user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inviter_settings (
                account_id TEXT PRIMARY KEY,
                target_ref TEXT NOT NULL DEFAULT '',
                target_peer_id INTEGER,
                target_title TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        try:
            conn.execute(
                "ALTER TABLE invite_queue ADD COLUMN access_hash INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
        for ddl in (
            "ALTER TABLE parsed_chats ADD COLUMN traffic_category TEXT NOT NULL DEFAULT 'other'",
            "ALTER TABLE parsed_chats ADD COLUMN note TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE parsed_chats ADD COLUMN archived_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE parsed_chats ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()
    return path


def is_chat_parsed(db_path: Path, account_id: str, source_chat_id: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT 1 FROM parsed_chats
            WHERE account_id = ? AND source_chat_id = ?
            LIMIT 1
            """,
            (account_id, source_chat_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_chat_parsed(
    db_path: Path,
    account_id: str,
    source_chat_id: str,
    source_chat_title: str,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO parsed_chats (account_id, source_chat_id, source_chat_title, parsed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id, source_chat_id)
            DO UPDATE SET source_chat_title = excluded.source_chat_title,
                          parsed_at = excluded.parsed_at
            """,
            (account_id, source_chat_id, source_chat_title, _utc_now()),
        )
        conn.commit()
    finally:
        conn.close()


def list_parsed_chats(
    db_path: Path,
    account_id: str,
    *,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        where = "p.account_id = ?"
        params: list[Any] = [account_id]
        if not include_archived:
            where += " AND COALESCE(p.archived_at, '') = ''"
        rows = conn.execute(
            f"""
            SELECT
                p.source_chat_id,
                p.source_chat_title,
                p.parsed_at,
                COALESCE(p.traffic_category, 'other') AS traffic_category,
                COALESCE(p.note, '') AS note,
                COALESCE(p.archived_at, '') AS archived_at,
                COALESCE(p.updated_at, '') AS updated_at,
                COUNT(q.id) AS queue_count
            FROM parsed_chats p
            LEFT JOIN invite_queue q
              ON q.account_id = p.account_id
             AND q.source_chat_id = p.source_chat_id
            WHERE {where}
            GROUP BY p.source_chat_id
            ORDER BY p.parsed_at DESC, p.id DESC
            """,
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_parsed_chat_meta(
    db_path: Path,
    account_id: str,
    source_chat_id: str,
    *,
    traffic_category: str,
    note: str,
) -> dict[str, Any] | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            UPDATE parsed_chats
            SET traffic_category = ?, note = ?, updated_at = ?
            WHERE account_id = ? AND source_chat_id = ?
            """,
            (traffic_category, note, _utc_now(), account_id, source_chat_id),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT source_chat_id, source_chat_title, parsed_at,
                   COALESCE(traffic_category, 'other') AS traffic_category,
                   COALESCE(note, '') AS note,
                   COALESCE(archived_at, '') AS archived_at,
                   COALESCE(updated_at, '') AS updated_at
            FROM parsed_chats
            WHERE account_id = ? AND source_chat_id = ?
            """,
            (account_id, source_chat_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_parsed_chat_archived(
    db_path: Path,
    account_id: str,
    source_chat_id: str,
    *,
    archived: bool,
) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            UPDATE parsed_chats
            SET archived_at = ?, updated_at = ?
            WHERE account_id = ? AND source_chat_id = ?
            """,
            (_utc_now() if archived else "", _utc_now(), account_id, source_chat_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def clear_queue_for_source(db_path: Path, account_id: str, source_chat_id: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            DELETE FROM invite_queue
            WHERE account_id = ? AND source_chat_id = ?
            """,
            (account_id, source_chat_id),
        )
        conn.commit()
        return int(cur.rowcount)
    finally:
        conn.close()


def add_users_to_queue(
    db_path: Path,
    account_id: str,
    source_chat_id: str,
    source_chat_title: str,
    users: list[tuple[int, str, str] | tuple[int, str, str, int]],
) -> tuple[int, int, int]:
    conn = sqlite3.connect(db_path)
    try:
        inserted = 0
        duplicated = 0
        blocked = 0
        for item in users:
            if len(item) >= 4:
                tg_user_id, username, display_name, access_hash = item[0], item[1], item[2], int(item[3] or 0)
            else:
                tg_user_id, username, display_name = item[0], item[1], item[2]
                access_hash = 0
            blocked_row = conn.execute(
                """
                SELECT reason FROM invite_blocklist
                WHERE account_id = ? AND tg_user_id = ?
                LIMIT 1
                """,
                (account_id, tg_user_id),
            ).fetchone()
            if blocked_row is not None:
                blocked += 1
                continue
            cur = conn.execute(
                """
                INSERT INTO invite_queue
                (account_id, source_chat_id, source_chat_title, tg_user_id, username, display_name, access_hash, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, tg_user_id) DO UPDATE SET
                    username = excluded.username,
                    display_name = excluded.display_name,
                    access_hash = CASE
                        WHEN excluded.access_hash != 0 THEN excluded.access_hash
                        ELSE invite_queue.access_hash
                    END
                """,
                (
                    account_id,
                    source_chat_id,
                    source_chat_title,
                    tg_user_id,
                    username,
                    display_name,
                    access_hash,
                    _utc_now(),
                ),
            )
            if cur.rowcount == 1:
                inserted += 1
            else:
                duplicated += 1
        conn.commit()
        return inserted, duplicated, blocked
    finally:
        conn.close()


def get_queue(db_path: Path, account_id: str, limit: int = 0) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = """
            SELECT id, tg_user_id, username, display_name, source_chat_title, access_hash
            FROM invite_queue
            WHERE account_id = ?
            ORDER BY id ASC
        """
        params: tuple[Any, ...]
        if limit > 0:
            query += " LIMIT ?"
            params = (account_id, limit)
        else:
            params = (account_id,)
        return [dict(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def queue_needs_hash_backfill(db_path: Path, account_id: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT 1 FROM invite_queue
            WHERE account_id = ? AND COALESCE(access_hash, 0) = 0
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_queue_source_chats(db_path: Path, account_id: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT source_chat_id, source_chat_title
            FROM invite_queue
            WHERE account_id = ?
            """,
            (account_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def bulk_update_access_hashes(
    db_path: Path,
    account_id: str,
    uid_to_hash: dict[int, int],
) -> int:
    if not uid_to_hash:
        return 0
    conn = sqlite3.connect(db_path)
    updated = 0
    try:
        for uid, access_hash in uid_to_hash.items():
            if not access_hash:
                continue
            cur = conn.execute(
                """
                UPDATE invite_queue
                SET access_hash = ?
                WHERE account_id = ? AND tg_user_id = ?
                """,
                (int(access_hash), account_id, int(uid)),
            )
            updated += cur.rowcount
        conn.commit()
        return updated
    finally:
        conn.close()


def remove_queue_item(db_path: Path, item_id: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM invite_queue WHERE id = ?", (item_id,))
        conn.commit()
    finally:
        conn.close()


def get_counts(db_path: Path, account_id: str) -> tuple[int, int]:
    conn = sqlite3.connect(db_path)
    try:
        queue_row = conn.execute(
            "SELECT COUNT(*) FROM invite_queue WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        parsed_row = conn.execute(
            "SELECT COUNT(*) FROM parsed_chats WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        return int(queue_row[0] if queue_row else 0), int(parsed_row[0] if parsed_row else 0)
    finally:
        conn.close()


def mark_blocked_user(
    db_path: Path, account_id: str, tg_user_id: int, reason: str
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO invite_blocklist (account_id, tg_user_id, reason, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id, tg_user_id)
            DO UPDATE SET reason = excluded.reason, updated_at = excluded.updated_at
            """,
            (account_id, tg_user_id, reason, _utc_now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_blocked_user_ids(db_path: Path, account_id: str) -> set[int]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT tg_user_id FROM invite_blocklist WHERE account_id = ?",
            (account_id,),
        ).fetchall()
        return {int(r[0]) for r in rows}
    finally:
        conn.close()


def get_block_reason(db_path: Path, account_id: str, tg_user_id: int) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT reason FROM invite_blocklist
            WHERE account_id = ? AND tg_user_id = ?
            LIMIT 1
            """,
            (account_id, tg_user_id),
        ).fetchone()
        return str(row[0]) if row else None
    finally:
        conn.close()


def get_target(db_path: Path, account_id: str) -> dict[str, Any] | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT target_ref, target_peer_id, target_title
            FROM inviter_settings WHERE account_id = ?
            """,
            (account_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "ref": row["target_ref"] or "",
            "peerId": row["target_peer_id"],
            "title": row["target_title"] or "",
        }
    finally:
        conn.close()


def set_target(
    db_path: Path,
    account_id: str,
    target_ref: str,
    target_peer_id: int,
    target_title: str,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO inviter_settings (account_id, target_ref, target_peer_id, target_title, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                target_ref = excluded.target_ref,
                target_peer_id = excluded.target_peer_id,
                target_title = excluded.target_title,
                updated_at = excluded.updated_at
            """,
            (account_id, target_ref, target_peer_id, target_title, _utc_now()),
        )
        conn.commit()
    finally:
        conn.close()
