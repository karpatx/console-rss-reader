from __future__ import annotations

import sqlite3
from typing import Iterable, Optional, Tuple
from dataclasses import asdict

from rss_reader import RssEntry


def init_db(db_path: str = "rss_text.db") -> None:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                entry_id TEXT PRIMARY KEY,
                source_id INTEGER,
                source_name TEXT,
                title TEXT,
                link TEXT,
                published TEXT,
                summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Add is_read column if it doesn't exist (for existing databases)
        try:
            cur.execute("ALTER TABLE entries ADD COLUMN is_read INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            # Column already exists
            pass
        # PRIMARY KEY on entry_id guarantees an index; still add explicit index if desired
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_source_id ON entries(source_id)
            """
        )
        conn.commit()
    finally:
        conn.close()


def save_entries(entries: Iterable[Tuple[int, str, RssEntry]], db_path: str = "rss_text.db") -> int:
    """
    Save multiple RSS entries. Skips existing ones via INSERT OR IGNORE on unique entry_id.
    Returns the number of newly inserted rows.
    """
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        rows = [
            (
                e.entry_id or f"{source_id}:{e.link or e.title}",
                source_id,
                source_name,
                e.title,
                e.link,
                e.published,
                e.summary,
            )
            for (source_id, source_name, e) in (
                (sid, sname, entry) for (sid, sname, entry) in (
                    (source_id, source_name, entry) for (source_id, source_name, entry) in entries
                )
            )
        ]
        cur.executemany(
            """
            INSERT OR IGNORE INTO entries (
                entry_id, source_id, source_name, title, link, published, summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0
    finally:
        conn.close()


def mark_as_read(entry_id: str, db_path: str = "rss_text.db") -> bool:
    """
    Mark an entry as read. Returns True if an entry was updated, False otherwise.
    """
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE entries SET is_read = 1 WHERE entry_id = ?",
            (entry_id,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_read_entries(db_path: str = "rss_text.db") -> set[str]:
    """
    Get a set of all entry IDs that have been marked as read.
    """
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT entry_id FROM entries WHERE is_read = 1")
        return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()
