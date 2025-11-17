from __future__ import annotations

import sqlite3
from typing import Iterable, Optional, Tuple
from dataclasses import asdict

from rss_reader import RssEntry


def get_connection(db_path: str = "rss_text.db") -> sqlite3.Connection:
    """
    Get a database connection with WAL mode enabled for concurrent access.
    """
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: str = "rss_text.db") -> None:
    conn = get_connection(db_path)
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
        # Add is_indexed column if it doesn't exist
        try:
            cur.execute("ALTER TABLE entries ADD COLUMN is_indexed INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            # Column already exists
            pass
        # Add full_text column if it doesn't exist
        try:
            cur.execute("ALTER TABLE entries ADD COLUMN full_text TEXT")
        except sqlite3.OperationalError:
            # Column already exists
            pass
        # Add is_detail_viewed column if it doesn't exist
        try:
            cur.execute("ALTER TABLE entries ADD COLUMN is_detail_viewed INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            # Column already exists
            pass
        # Add is_opened column if it doesn't exist
        try:
            cur.execute("ALTER TABLE entries ADD COLUMN is_opened INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            # Column already exists
            pass
        # Add cached_article_text column if it doesn't exist (for downloaded articles)
        try:
            cur.execute("ALTER TABLE entries ADD COLUMN cached_article_text TEXT")
        except sqlite3.OperationalError:
            # Column already exists
            pass
        # Create index for full-text search
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_full_text ON entries(full_text)
            """
        )
        # PRIMARY KEY on entry_id guarantees an index; still add explicit index if desired
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_source_id ON entries(source_id)
            """
        )
        # Index for published date (used in ORDER BY)
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_published ON entries(published DESC)
            """
        )
        # Index for created_at (used in ORDER BY fallback)
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_created_at ON entries(created_at DESC)
            """
        )
        # Indexes for boolean flags (used in WHERE clauses)
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_is_read ON entries(is_read)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_is_detail_viewed ON entries(is_detail_viewed)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_is_opened ON entries(is_opened)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_is_indexed ON entries(is_indexed)
            """
        )
        # Composite index for common query pattern: source_id + published
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entries_source_published ON entries(source_id, published DESC)
            """
        )
        # Create table for selected RSS sources
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS selected_sources (
                source_id INTEGER PRIMARY KEY
            )
            """
        )
        # Create table for app settings (language, etc.)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT entry_id FROM entries WHERE is_read = 1")
        return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def is_entry_indexed(entry_id: str, db_path: str = "rss_text.db") -> bool:
    """
    Check if an entry is indexed.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT is_indexed FROM entries WHERE entry_id = ?", (entry_id,))
        row = cur.fetchone()
        return row[0] == 1 if row else False
    finally:
        conn.close()


def update_entry_full_text(entry_id: str, full_text: str, db_path: str = "rss_text.db") -> bool:
    """
    Update the full text and mark entry as indexed.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE entries SET full_text = ?, is_indexed = 1 WHERE entry_id = ?",
            (full_text, entry_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def index_all_unindexed_entries(db_path: str = "rss_text.db") -> int:
    """
    Index all entries that are not yet indexed. Returns the number of indexed entries.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        # Get all unindexed entries
        cur.execute("SELECT entry_id, title, summary FROM entries WHERE is_indexed = 0 OR is_indexed IS NULL")
        unindexed = cur.fetchall()
        
        indexed_count = 0
        for entry_id, title, summary in unindexed:
            # Create searchable text from title and summary
            searchable_text = f"{title or ''} {summary or ''}".strip()
            if searchable_text:
                cur.execute(
                    "UPDATE entries SET full_text = ?, is_indexed = 1 WHERE entry_id = ?",
                    (searchable_text, entry_id)
                )
                indexed_count += 1
        
        conn.commit()
        return indexed_count
    finally:
        conn.close()


def mark_as_detail_viewed(entry_id: str, db_path: str = "rss_text.db") -> bool:
    """
    Mark an entry as viewed in detail view. Returns True if an entry was updated, False otherwise.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE entries SET is_detail_viewed = 1 WHERE entry_id = ?",
            (entry_id,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_as_opened(entry_id: str, db_path: str = "rss_text.db") -> bool:
    """
    Mark an entry as opened in modal window. Returns True if an entry was updated, False otherwise.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE entries SET is_opened = 1 WHERE entry_id = ?",
            (entry_id,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_detail_viewed_entries(db_path: str = "rss_text.db") -> set[str]:
    """
    Get a set of all entry IDs that have been viewed in detail view.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT entry_id FROM entries WHERE is_detail_viewed = 1")
        return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def get_opened_entries(db_path: str = "rss_text.db") -> set[str]:
    """
    Get a set of all entry IDs that have been opened in modal window.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT entry_id FROM entries WHERE is_opened = 1")
        return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def get_cached_article_text(entry_id: str, db_path: str = "rss_text.db") -> Optional[str]:
    """
    Get cached article text for an entry. Returns None if not cached.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT cached_article_text FROM entries WHERE entry_id = ?", (entry_id,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()


def cache_article_text(entry_id: str, article_text: str, db_path: str = "rss_text.db") -> bool:
    """
    Cache article text for an entry. Returns True if an entry was updated, False otherwise.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE entries SET cached_article_text = ? WHERE entry_id = ?",
            (article_text, entry_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def save_selected_sources(source_ids: Iterable[int], db_path: str = "rss_text.db") -> None:
    """
    Save selected RSS source IDs to database.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        # Clear existing selections
        cur.execute("DELETE FROM selected_sources")
        # Insert new selections
        if source_ids:
            cur.executemany(
                "INSERT INTO selected_sources (source_id) VALUES (?)",
                [(sid,) for sid in source_ids]
            )
        conn.commit()
    finally:
        conn.close()


def get_selected_sources(db_path: str = "rss_text.db") -> set[int]:
    """
    Get set of selected RSS source IDs from database.
    Returns empty set if table doesn't exist yet.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        # Check if table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='selected_sources'")
        if not cur.fetchone():
            # Table doesn't exist yet, return empty set
            return set()
        cur.execute("SELECT source_id FROM selected_sources")
        return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def save_language(language: str, db_path: str = "rss_text.db") -> None:
    """
    Save selected language to database.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            ("language", language)
        )
        conn.commit()
    finally:
        conn.close()


def get_language(db_path: str = "rss_text.db") -> Optional[str]:
    """
    Get saved language from database. Returns None if not set.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        # Check if table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='app_settings'")
        if not cur.fetchone():
            # Table doesn't exist yet, return None
            return None
        cur.execute("SELECT value FROM app_settings WHERE key = ?", ("language",))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def search_entries(query: str, db_path: str = "rss_text.db") -> list[Tuple[str, int, str, str, str, str, str]]:
    """
    Search entries by keywords. Returns list of (entry_id, source_id, source_name, title, link, published, summary).
    Query must be at least 3 characters. Multiple words are ANDed together.
    """
    if len(query.strip()) < 3:
        return []
    
    # Split query into keywords
    keywords = [kw.strip().lower() for kw in query.split() if kw.strip()]
    if not keywords:
        return []
    
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        # Build search conditions - all keywords must be present
        conditions = []
        params = []
        for keyword in keywords:
            conditions.append("(LOWER(title) LIKE ? OR LOWER(summary) LIKE ? OR LOWER(COALESCE(full_text, '')) LIKE ?)")
            pattern = f"%{keyword}%"
            params.extend([pattern, pattern, pattern])
        
        where_clause = " AND ".join(conditions)
        # Get all matching entries - we'll sort by date in Python for better date format support
        sql = f"""
            SELECT entry_id, source_id, source_name, title, link, published, summary
            FROM entries
            WHERE {where_clause}
        """
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def get_entries_by_sources(source_ids: Iterable[int], db_path: str = "rss_text.db") -> list[Tuple[str, int, str, str, str, str, str]]:
    """
    Get all entries for given source IDs from database.
    Returns list of (entry_id, source_id, source_name, title, link, published, summary).
    """
    source_ids_list = list(source_ids)
    if not source_ids_list:
        return []
    
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        placeholders = ','.join('?' * len(source_ids_list))
        sql = f"""
            SELECT entry_id, source_id, source_name, title, link, published, summary
            FROM entries
            WHERE source_id IN ({placeholders})
            ORDER BY published DESC, created_at DESC
        """
        cur.execute(sql, source_ids_list)
        return cur.fetchall()
    finally:
        conn.close()
