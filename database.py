import asyncio
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite


PHOTO_PRICE = 10
VIDEO_PRICE = 20
RESERVATION_SECONDS = 30 * 60


@dataclass(frozen=True)
class MediaItem:
    id: str
    file_path: str
    media_type: str
    price: int


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def normalize_username(username: str | None) -> str | None:
        if username is None:
            return None
        normalized = username.strip().removeprefix("@").lower()
        return normalized or None

    @staticmethod
    def media_id_for(media_type: str, relative_path: str) -> str:
        digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:16]
        return f"{media_type}_{digest}"

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA foreign_keys = ON")
        await self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS media_files (
                id TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                media_type TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
                price INTEGER NOT NULL CHECK (price IN (10, 20)),
                is_active INTEGER NOT NULL DEFAULT 1,
                telegram_file_id TEXT DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS purchases (
                user_id INTEGER NOT NULL,
                media_id TEXT NOT NULL,
                purchased_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                payment_charge_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY (user_id, media_id),
                FOREIGN KEY (media_id) REFERENCES media_files(id)
            );

            CREATE TABLE IF NOT EXISTS package_orders (
                order_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                product_id TEXT NOT NULL,
                price INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                payment_charge_id TEXT UNIQUE,
                purchased_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS reservations (
                user_id INTEGER NOT NULL,
                media_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (user_id, media_id),
                FOREIGN KEY (media_id) REFERENCES media_files(id)
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS manual_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                admin_id INTEGER NOT NULL,
                media_id TEXT,
                media_type TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
                media_name TEXT NOT NULL,
                telegram_file_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_media_type_active
                ON media_files(media_type, is_active, file_path);
            CREATE INDEX IF NOT EXISTS idx_purchases_user
                ON purchases(user_id, purchased_at);
            CREATE INDEX IF NOT EXISTS idx_package_orders_user
                ON package_orders(user_id, purchased_at);
            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            CREATE INDEX IF NOT EXISTS idx_manual_grants_user
                ON manual_grants(user_id, created_at);
            """
        )
        cursor = await self.connection.execute("PRAGMA table_info(media_files)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "telegram_file_id" not in columns:
            await self.connection.execute(
                "ALTER TABLE media_files ADD COLUMN telegram_file_id TEXT DEFAULT NULL"
            )
        cursor = await self.connection.execute("PRAGMA table_info(manual_grants)")
        grant_columns = {row["name"] for row in await cursor.fetchall()}
        if "telegram_file_id" not in grant_columns:
            await self.connection.execute(
                "ALTER TABLE manual_grants ADD COLUMN telegram_file_id TEXT"
            )
        await self.connection.commit()

    def _connection(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("Database has not been initialized")
        return self.connection

    async def refresh_catalog(self, items: list[MediaItem]) -> None:
        connection = self._connection()
        async with self._lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute("UPDATE media_files SET is_active = 0")
                await connection.executemany(
                    """
                    INSERT INTO media_files (id, file_path, media_type, price, is_active)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(id) DO UPDATE SET
                        file_path = excluded.file_path,
                        media_type = excluded.media_type,
                        price = excluded.price,
                        is_active = 1
                    """,
                    [(item.id, item.file_path, item.media_type, item.price) for item in items],
                )
                await connection.execute(
                    """
                    DELETE FROM reservations
                    WHERE media_id IN (
                        SELECT id FROM media_files WHERE is_active = 0
                    )
                    """
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise

    async def reserve_next(
        self, user_id: int, media_type: str
    ) -> tuple[aiosqlite.Row | None, bool]:
        """Return (media, already_reserved); reserve the first unpurchased item."""
        connection = self._connection()
        now = time.time()
        async with self._lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute(
                    "DELETE FROM reservations WHERE expires_at <= ?", (now,)
                )
                cursor = await connection.execute(
                    """
                    SELECT m.* FROM reservations r
                    JOIN media_files m ON m.id = r.media_id
                    WHERE r.user_id = ? AND m.media_type = ?
                      AND m.is_active = 1
                      AND NOT EXISTS (
                          SELECT 1 FROM purchases p
                          WHERE p.user_id = r.user_id AND p.media_id = m.id
                      )
                    ORDER BY m.file_path
                    LIMIT 1
                    """,
                    (user_id, media_type),
                )
                reserved = await cursor.fetchone()
                if reserved is not None:
                    await connection.commit()
                    return reserved, True

                cursor = await connection.execute(
                    """
                    SELECT m.* FROM media_files m
                    WHERE m.media_type = ? AND m.is_active = 1
                      AND NOT EXISTS (
                          SELECT 1 FROM purchases p
                          WHERE p.user_id = ? AND p.media_id = m.id
                      )
                    ORDER BY m.file_path
                    LIMIT 1
                    """,
                    (media_type, user_id),
                )
                media = await cursor.fetchone()
                if media is not None:
                    await connection.execute(
                        "INSERT INTO reservations (user_id, media_id, expires_at) VALUES (?, ?, ?)",
                        (user_id, media["id"], now + RESERVATION_SECONDS),
                    )
                await connection.commit()
                return media, False
            except Exception:
                await connection.rollback()
                raise

    async def release_reservation(self, user_id: int, media_id: str) -> None:
        connection = self._connection()
        async with self._lock:
            await connection.execute(
                "DELETE FROM reservations WHERE user_id = ? AND media_id = ?",
                (user_id, media_id),
            )
            await connection.commit()

    async def get_checkout_media(
        self, user_id: int, media_id: str
    ) -> aiosqlite.Row | None:
        connection = self._connection()
        async with self._lock:
            cursor = await connection.execute(
                """
                SELECT m.* FROM media_files m
                JOIN reservations r ON r.media_id = m.id
                WHERE r.user_id = ? AND r.media_id = ? AND r.expires_at > ?
                  AND m.is_active = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM purchases p
                      WHERE p.user_id = ? AND p.media_id = m.id
                  )
                """,
                (user_id, media_id, time.time(), user_id),
            )
            return await cursor.fetchone()

    async def get_media(self, media_id: str) -> aiosqlite.Row | None:
        connection = self._connection()
        async with self._lock:
            cursor = await connection.execute(
                "SELECT * FROM media_files WHERE id = ?", (media_id,)
            )
            return await cursor.fetchone()

    async def create_package_order(
        self, order_id: str, user_id: int, product_id: str, price: int
    ) -> None:
        connection = self._connection()
        async with self._lock:
            await connection.execute(
                """
                INSERT INTO package_orders (order_id, user_id, product_id, price)
                VALUES (?, ?, ?, ?)
                """,
                (order_id, user_id, product_id, price),
            )
            await connection.commit()

    async def delete_pending_package_order(self, order_id: str) -> None:
        connection = self._connection()
        async with self._lock:
            await connection.execute(
                "DELETE FROM package_orders WHERE order_id = ? AND status = 'pending'",
                (order_id,),
            )
            await connection.commit()

    async def get_package_order(self, order_id: str) -> aiosqlite.Row | None:
        connection = self._connection()
        async with self._lock:
            cursor = await connection.execute(
                "SELECT * FROM package_orders WHERE order_id = ?",
                (order_id,),
            )
            return await cursor.fetchone()

    async def record_package_payment(
        self,
        order_id: str,
        user_id: int,
        payment_charge_id: str,
    ) -> bool:
        connection = self._connection()
        async with self._lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    UPDATE package_orders
                    SET status = 'paid', payment_charge_id = ?,
                        purchased_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE order_id = ? AND user_id = ? AND status = 'pending'
                    """,
                    (payment_charge_id, order_id, user_id),
                )
                recorded = cursor.rowcount == 1
                if not recorded:
                    cursor = await connection.execute(
                        """
                        SELECT 1 FROM package_orders
                        WHERE order_id = ? AND user_id = ? AND status = 'paid'
                          AND payment_charge_id = ?
                        """,
                        (order_id, user_id, payment_charge_id),
                    )
                    recorded = await cursor.fetchone() is not None
                await connection.commit()
                return recorded
            except Exception:
                await connection.rollback()
                raise

    async def package_purchase_history(
        self, user_id: int
    ) -> list[aiosqlite.Row]:
        connection = self._connection()
        async with self._lock:
            cursor = await connection.execute(
                """
                SELECT product_id, price, purchased_at
                FROM package_orders
                WHERE user_id = ? AND status = 'paid'
                ORDER BY purchased_at DESC
                """,
                (user_id,),
            )
            return await cursor.fetchall()

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str,
        last_name: str | None,
    ) -> None:
        normalized_username = self.normalize_username(username)

        connection = self._connection()
        async with self._lock:
            await connection.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name, updated_at)
                VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    updated_at = excluded.updated_at
                """,
                (user_id, normalized_username, first_name, last_name or ""),
            )
            await connection.commit()

    async def find_user(self, identifier: str) -> aiosqlite.Row | None:
        connection = self._connection()
        normalized = self.normalize_username(identifier)
        if not normalized or (not normalized.isdecimal() and not all(
            character.isalnum() or character == "_" for character in normalized
        )):
            return None
        async with self._lock:
            if normalized.isdecimal():
                cursor = await connection.execute(
                    """
                    SELECT user_id, username, first_name, last_name
                    FROM users WHERE user_id = ?
                    UNION ALL
                    SELECT p.user_id, NULL, '', ''
                    FROM purchases p
                    WHERE p.user_id = ? AND NOT EXISTS (
                        SELECT 1 FROM users u WHERE u.user_id = p.user_id
                    )
                    LIMIT 1
                    """,
                    (int(normalized), int(normalized)),
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT user_id, username, first_name, last_name
                    FROM users WHERE username = ?
                    """,
                    (normalized,),
                )
            return await cursor.fetchone()

    async def record_manual_grant(
        self,
        user_id: int,
        admin_id: int,
        media_id: str | None,
        media_type: str,
        media_name: str,
        telegram_file_id: str | None,
        status: str,
    ) -> None:
        connection = self._connection()
        async with self._lock:
            await connection.execute(
                """
                INSERT INTO manual_grants
                    (user_id, admin_id, media_id, media_type, media_name,
                     telegram_file_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    admin_id,
                    media_id,
                    media_type,
                    media_name,
                    telegram_file_id,
                    status,
                ),
            )
            await connection.commit()

    async def record_purchase(
        self, user_id: int, media_id: str, payment_charge_id: str
    ) -> bool:
        connection = self._connection()
        async with self._lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    INSERT OR IGNORE INTO purchases (user_id, media_id, payment_charge_id)
                    VALUES (?, ?, ?)
                    """,
                    (user_id, media_id, payment_charge_id),
                )
                recorded = cursor.rowcount == 1
                if recorded:
                    await connection.execute(
                        "DELETE FROM reservations WHERE user_id = ? AND media_id = ?",
                        (user_id, media_id),
                    )
                await connection.commit()
                return recorded
            except Exception:
                await connection.rollback()
                raise

    async def purchase_history(self, user_id: int) -> list[aiosqlite.Row]:
        connection = self._connection()
        async with self._lock:
            cursor = await connection.execute(
                """
                SELECT p.media_id, p.purchased_at, m.media_type, m.file_path
                FROM purchases p
                JOIN media_files m ON m.id = p.media_id
                WHERE p.user_id = ?
                ORDER BY p.purchased_at DESC
                """,
                (user_id,),
            )
            return await cursor.fetchall()

    async def health_summary(self) -> dict[str, int | str]:
        connection = self._connection()
        async with self._lock:
            cursor = await connection.execute("PRAGMA integrity_check")
            integrity = await cursor.fetchone()
            cursor = await connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM media_files WHERE is_active = 1) AS media_count,
                    (
                        (SELECT COUNT(*) FROM purchases) +
                        (SELECT COUNT(*) FROM package_orders WHERE status = 'paid')
                    ) AS purchase_count,
                    (
                        SELECT COUNT(DISTINCT user_id)
                        FROM (
                            SELECT user_id FROM purchases
                            UNION
                            SELECT user_id FROM package_orders WHERE status = 'paid'
                        )
                    ) AS buyer_count,
                    (
                        (SELECT COALESCE(SUM(m.price), 0)
                         FROM purchases p JOIN media_files m ON m.id = p.media_id) +
                        (SELECT COALESCE(SUM(price), 0)
                         FROM package_orders WHERE status = 'paid')
                    ) AS stars_earned
                """
            )
            summary = dict(await cursor.fetchone())
            summary["integrity"] = integrity[0] if integrity else "unknown"
            return summary

    async def active_media(
        self, media_type: str | None = None
    ) -> list[aiosqlite.Row]:
        connection = self._connection()
        async with self._lock:
            if media_type is None:
                cursor = await connection.execute(
                    "SELECT * FROM media_files WHERE is_active = 1 ORDER BY file_path"
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT * FROM media_files
                    WHERE is_active = 1 AND media_type = ?
                    ORDER BY file_path
                    """,
                    (media_type,),
                )
            return await cursor.fetchall()

    async def uncached_media(self) -> list[aiosqlite.Row]:
        connection = self._connection()
        async with self._lock:
            cursor = await connection.execute(
                """
                SELECT * FROM media_files
                WHERE is_active = 1 AND telegram_file_id IS NULL
                ORDER BY file_path
                """
            )
            return await cursor.fetchall()

    async def set_telegram_file_id(
        self, media_id: str, file_id: str | None
    ) -> None:
        connection = self._connection()
        async with self._lock:
            await connection.execute(
                "UPDATE media_files SET telegram_file_id = ? WHERE id = ?",
                (file_id, media_id),
            )
            await connection.commit()

    async def recent_purchases(self, limit: int = 5) -> list[aiosqlite.Row]:
        connection = self._connection()
        async with self._lock:
            cursor = await connection.execute(
                """
                SELECT p.user_id, p.media_id, p.purchased_at,
                       p.payment_charge_id, m.media_type, m.price
                FROM purchases p
                JOIN media_files m ON m.id = p.media_id
                UNION ALL
                SELECT user_id, product_id AS media_id, purchased_at,
                       payment_charge_id, 'package' AS media_type, price
                FROM package_orders
                WHERE status = 'paid'
                ORDER BY purchased_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return await cursor.fetchall()

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None
