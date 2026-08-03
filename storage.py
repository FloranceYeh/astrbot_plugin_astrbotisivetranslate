from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite


def utc_now() -> str:
    """Return a sortable UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReadingStore:
    """Persist reading sessions and translated segments in SQLite."""

    def __init__(self, database_path: Path) -> None:
        """Initialize the store.

        Args:
            database_path: SQLite file location.
        """
        self.database_path = database_path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        """Open the database and create the current schema."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.database_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                is_automatic INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                rolling_summary TEXT NOT NULL DEFAULT '',
                final_summary TEXT NOT NULL DEFAULT '',
                summarized_sequence INTEGER NOT NULL DEFAULT 0,
                segment_count INTEGER NOT NULL DEFAULT 0,
                source_char_count INTEGER NOT NULL DEFAULT 0,
                delivered_at TEXT,
                injected_at TEXT,
                delivery_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                mode TEXT NOT NULL,
                source_text TEXT NOT NULL,
                output_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(article_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_articles_updated ON articles(updated_at);
            CREATE INDEX IF NOT EXISTS idx_segments_article ON segments(article_id, sequence);
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("reading store is not open")
        return self._db

    @staticmethod
    def _row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    async def create_article(
        self,
        article_id: str,
        title: str,
        *,
        automatic: bool = False,
    ) -> dict[str, Any]:
        """Create a reading session.

        Args:
            article_id: Stable article identifier.
            title: User-visible title.
            automatic: Whether standard requests created the session.

        Returns:
            The created article row.
        """
        now = utc_now()
        async with self._lock:
            db = self._connection()
            await db.execute(
                """INSERT INTO articles (
                    id, title, is_automatic, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    article_id,
                    title,
                    int(automatic),
                    now,
                    now,
                ),
            )
            await db.commit()
        article = await self.get_article(article_id)
        if article is None:
            raise RuntimeError("created article could not be loaded")
        return article

    async def get_article(self, article_id: str) -> dict[str, Any] | None:
        """Return one article by ID."""
        cursor = await self._connection().execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        )
        return self._row(await cursor.fetchone())

    async def get_active_automatic(self) -> dict[str, Any] | None:
        """Return the newest active automatically captured session."""
        cursor = await self._connection().execute(
            """SELECT * FROM articles
            WHERE status = 'active' AND is_automatic = 1
            ORDER BY updated_at DESC LIMIT 1"""
        )
        return self._row(await cursor.fetchone())

    async def list_articles(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent articles.

        Args:
            limit: Maximum result count.

        Returns:
            Recent article rows.
        """
        cursor = await self._connection().execute(
            "SELECT * FROM articles ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def add_segment(
        self,
        article_id: str,
        mode: str,
        source_text: str,
        output_text: str,
    ) -> int:
        """Append a translated segment and return its sequence number."""
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                "SELECT segment_count FROM articles WHERE id = ? AND status = 'active'",
                (article_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise KeyError(article_id)
            sequence = int(row["segment_count"]) + 1
            now = utc_now()
            await db.execute(
                """INSERT INTO segments (
                    article_id, sequence, mode, source_text, output_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (article_id, sequence, mode, source_text, output_text, now),
            )
            await db.execute(
                """UPDATE articles SET
                    segment_count = ?, source_char_count = source_char_count + ?, updated_at = ?
                WHERE id = ?""",
                (sequence, len(source_text), now, article_id),
            )
            await db.commit()
            return sequence

    async def get_segments(
        self, article_id: str, *, after_sequence: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Return ordered segments for one article."""
        cursor = await self._connection().execute(
            """SELECT * FROM segments
            WHERE article_id = ? AND sequence > ?
            ORDER BY sequence ASC LIMIT ?""",
            (article_id, max(0, after_sequence), max(1, min(limit, 5000))),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def update_rolling_summary(
        self, article_id: str, summary: str, summarized_sequence: int
    ) -> None:
        """Store rolling memory and the covered segment sequence."""
        async with self._lock:
            db = self._connection()
            await db.execute(
                """UPDATE articles SET rolling_summary = ?, summarized_sequence = ?
                WHERE id = ? AND summarized_sequence < ?""",
                (summary, summarized_sequence, article_id, summarized_sequence),
            )
            await db.commit()

    async def finish_article(
        self, article_id: str, final_summary: str, *, title: str | None = None
    ) -> dict[str, Any] | None:
        """Mark an article complete and persist its final summary."""
        now = utc_now()
        async with self._lock:
            db = self._connection()
            if title:
                await db.execute(
                    """UPDATE articles SET status = 'completed', final_summary = ?,
                    title = ?, finished_at = ?, updated_at = ? WHERE id = ?""",
                    (final_summary, title, now, now, article_id),
                )
            else:
                await db.execute(
                    """UPDATE articles SET status = 'completed', final_summary = ?,
                    finished_at = ?, updated_at = ? WHERE id = ?""",
                    (final_summary, now, now, article_id),
                )
            await db.commit()
        return await self.get_article(article_id)

    async def mark_delivery(
        self,
        article_id: str,
        *,
        delivered: bool,
        injected: bool,
        error: str = "",
    ) -> None:
        """Record proactive delivery and conversation injection results."""
        now = utc_now()
        async with self._lock:
            db = self._connection()
            await db.execute(
                """UPDATE articles SET delivered_at = ?, injected_at = ?, delivery_error = ?
                WHERE id = ?""",
                (
                    now if delivered else None,
                    now if injected else None,
                    error,
                    article_id,
                ),
            )
            await db.commit()

    async def delete_article(self, article_id: str) -> bool:
        """Delete an article and all of its segments."""
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                "DELETE FROM articles WHERE id = ?", (article_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def purge_older_than(self, days: int) -> int:
        """Delete completed articles older than the configured retention."""
        if days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        async with self._lock:
            db = self._connection()
            cursor = await db.execute(
                "DELETE FROM articles WHERE status = 'completed' AND updated_at < ?",
                (cutoff,),
            )
            await db.commit()
            return cursor.rowcount

    async def stale_active_articles(self, idle_minutes: int) -> list[dict[str, Any]]:
        """Return active articles whose last segment is beyond the idle timeout."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=max(1, idle_minutes))
        ).isoformat(timespec="seconds")
        cursor = await self._connection().execute(
            """SELECT * FROM articles
            WHERE status = 'active' AND segment_count > 0 AND updated_at < ?
            ORDER BY updated_at ASC""",
            (cutoff,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def export_markdown(self, article_id: str) -> str:
        """Render one complete reading record as Markdown."""
        article = await self.get_article(article_id)
        if article is None:
            raise KeyError(article_id)
        segments = await self.get_segments(article_id, limit=5000)
        lines = [f"# {article['title']}", ""]
        lines.extend(
            [
                f"- 阅读时间：{article['created_at']}",
                f"- 状态：{article['status']}",
                f"- 文本段数：{article['segment_count']}",
                "",
            ]
        )
        if article["final_summary"]:
            lines.extend(["## 阅读摘要", "", article["final_summary"], ""])
        elif article["rolling_summary"]:
            lines.extend(["## 当前摘要", "", article["rolling_summary"], ""])
        lines.extend(["## 阅读记录", ""])
        for segment in segments:
            lines.extend(
                [
                    f"### 片段 {segment['sequence']}",
                    "",
                    "**原文**",
                    "",
                    segment["source_text"],
                    "",
                    "**译文与批注**",
                    "",
                    segment["output_text"],
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"
