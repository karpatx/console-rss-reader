#!/usr/bin/env python3
"""
RSS Fetcher Service - Background service that continuously fetches RSS feeds
and saves them to the database. This runs independently from the UI.
"""

import sqlite3
import time
import threading
from typing import Tuple
from rss_reader import fetch_rss
from rss_sources import SOURCES
from db import init_db, save_entries, is_entry_indexed, update_entry_full_text, get_selected_sources

class RSSFetcherService:
    """Background service for fetching RSS feeds"""
    
    def __init__(self, db_path: str = "rss_text.db", fetch_interval: int = 60):
        """
        Initialize the RSS fetcher service.
        
        Args:
            db_path: Path to the SQLite database
            fetch_interval: Interval in seconds between feed fetches (default: 60)
        """
        self.db_path = db_path
        self.fetch_interval = fetch_interval
        self.running = False
        self.thread = None
        self._lock = threading.Lock()
        
        # Initialize database with WAL mode
        init_db(db_path)
    
    def _get_connection(self):
        """Get a database connection with WAL mode enabled"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    
    def _fetch_source(self, source_id: int, source_name: str, url: str) -> Tuple[int, int]:
        """
        Fetch RSS feed for a single source.
        
        Returns:
            Tuple of (new_entries_count, indexed_count)
        """
        try:
            entries = fetch_rss(url)
            if not entries:
                return 0, 0
            
            # Save entries to database
            to_save = [(source_id, source_name, e) for e in entries]
            new_count = save_entries(to_save, self.db_path)
            
            # Index new entries
            indexed_count = 0
            for e in entries:
                entry_id = e.entry_id or f"{source_name}:{e.link or e.title}"
                if not is_entry_indexed(entry_id, self.db_path):
                    searchable_text = f"{e.title or ''} {e.summary or ''}".strip()
                    if searchable_text:
                        update_entry_full_text(entry_id, searchable_text, self.db_path)
                        indexed_count += 1
            
            return new_count, indexed_count
        except Exception as e:
            print(f"Error fetching {source_name}: {e}")
            return 0, 0
    
    def _fetch_all_sources(self):
        """Fetch all selected RSS sources (only those checked by the user)"""
        # Get selected sources from database
        selected_sources = get_selected_sources(self.db_path)
        
        # Only fetch sources that are explicitly selected by the user
        # Don't use default sources - if nothing is selected, don't fetch anything
        if not selected_sources:
            # No sources selected, skip fetching
            return
        
        total_new = 0
        total_indexed = 0
        
        # Only fetch the selected sources
        for source_id in selected_sources:
            source_data = SOURCES.get(source_id)
            if not source_data:
                continue
            
            country_code, name, url = source_data
            if not url:
                continue
            
            new_count, indexed_count = self._fetch_source(source_id, name, url)
            total_new += new_count
            total_indexed += indexed_count
            
            # Small delay between sources to avoid overwhelming servers
            time.sleep(0.5)
        
        if total_new > 0 or total_indexed > 0:
            print(f"Fetched: {total_new} new entries, {total_indexed} indexed from {len(selected_sources)} selected source(s)")
    
    def _run_loop(self):
        """Main loop for the fetcher service"""
        while self.running:
            try:
                self._fetch_all_sources()
            except Exception as e:
                print(f"Error in fetcher loop: {e}")
            
            # Sleep until next fetch interval
            for _ in range(self.fetch_interval):
                if not self.running:
                    break
                time.sleep(1)
    
    def start(self):
        """Start the fetcher service in a background thread"""
        if self.running:
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        print(f"RSS Fetcher Service started (interval: {self.fetch_interval}s)")
    
    def stop(self):
        """Stop the fetcher service"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5.0)
        print("RSS Fetcher Service stopped")
    
    def fetch_now(self):
        """Trigger an immediate fetch (non-blocking)"""
        if self.running:
            # Run in a separate thread to avoid blocking
            threading.Thread(target=self._fetch_all_sources, daemon=True).start()


def main():
    """Run the fetcher service as a standalone process"""
    import signal
    import sys
    
    service = RSSFetcherService(fetch_interval=60)
    
    def signal_handler(sig, frame):
        print("\nStopping RSS Fetcher Service...")
        service.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    service.start()
    
    # Keep the main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(None, None)


if __name__ == "__main__":
    main()

