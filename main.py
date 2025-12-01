from typing import cast, Dict, Tuple, List, DefaultDict, Iterable, Optional
from textual.app import App, ComposeResult
from textual.screen import Screen, ModalScreen
from textual.widgets import Static, SelectionList, ListView, ListItem, Button, Input, Tree, Select
from textual.binding import Binding
from textual.widgets.tree import TreeNode
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual import events
from time import sleep

from rss_reader import fetch_rss, RssEntry
from rich.text import Text
from rich.markup import escape
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
try:
    from dateutil import parser as date_parser
    HAS_DATEUTIL = True
except ImportError:
    HAS_DATEUTIL = False
    date_parser = None  # type: ignore
from html.parser import HTMLParser
import asyncio
import re

from db import init_db, save_entries, mark_as_read, get_read_entries, is_entry_indexed, update_entry_full_text, search_entries, index_all_unindexed_entries, mark_as_detail_viewed, mark_as_opened, get_detail_viewed_entries, get_opened_entries, get_cached_article_text, cache_article_text, save_selected_sources, get_selected_sources, save_language, get_language, get_entries_by_sources
from rss_fetcher_service import RSSFetcherService
from image_processor import process_images_in_html
from translations import TRANSLATIONS
from rss_sources import SOURCES

try:
    from newspaper import Article
    HAS_NEWSPAPER = True
except ImportError:
    HAS_NEWSPAPER = False
    print("Warning: newspaper4k not installed")


class LoadingScreen(ModalScreen):
    """Modal screen for showing loading indicator"""
    CSS_PATH = "xcss.tcss"
    
    def __init__(self, language: str = "hu") -> None:
        super().__init__()
        self.language = language
    
    def compose(self) -> ComposeResult:
        with Vertical(id="loading-container"):
            yield Static(TRANSLATIONS.get(self.language, TRANSLATIONS["hu"])["loading"], id="loading-text")
    
    def on_mount(self) -> None:
        """Start animation when screen is mounted"""
        self._animate_loading()
    
    def _animate_loading(self) -> None:
        """Animate loading text with dots"""
        loading_text = self.query_one("#loading-text", Static)
        dots = ["", ".", "..", "..."]
        dot_index = 0
        
        def update_dots() -> None:
            nonlocal dot_index
            base_text = TRANSLATIONS.get(self.language, TRANSLATIONS["hu"])["loading"]
            # Remove existing dots if any, then add new ones
            base_text_clean = base_text.rstrip(".")
            loading_text.update(f"{base_text_clean}{dots[dot_index]}")
            dot_index = (dot_index + 1) % len(dots)
            self.set_timer(0.5, update_dots)
        
        self.set_timer(0.5, update_dots)


class ArticleScreen(ModalScreen):
    """Modal screen for displaying full article content"""
    CSS_PATH = "xcss.tcss"
    
    def __init__(self, title: str, content: str, source: str, link: str, language: str = "hu") -> None:
        super().__init__()
        self.article_title = title
        self.article_content = content
        self.article_source = source
        self.article_link = link
        self.language = language
    
    def compose(self) -> ComposeResult:
        # Parse markup strings into Text objects with error handling
        try:
            article_title_text = Text.from_markup(f"[bold]{escape(self.article_title)}[/bold]")
        except Exception:
            article_title_text = escape(self.article_title)
        
        try:
            article_source_text = Text.from_markup(f"[dim]{escape(self.article_source)}[/dim]")
        except Exception:
            article_source_text = escape(self.article_source)
        
        # The article content is the most likely to have markup errors
        try:
            if self.article_content:
                article_content_text = Text.from_markup(self.article_content)
            else:
                article_content_text = Text("")
        except Exception:
            # Fall back to escaped plain text if markup parsing fails
            article_content_text = escape(str(self.article_content))
        
        try:
            article_link_text = Text.from_markup(f"[cyan]{escape(self.article_link)}[/cyan]")
        except Exception:
            article_link_text = escape(self.article_link)
        
        with Vertical(id="article-container"):
            with Horizontal(id="article-header"):
                yield Static(article_title_text, id="article-title")
                yield Button("✕", id="close-x-btn", variant="default")
            yield Static(article_source_text, id="article-subheader")
            with ScrollableContainer(id="article-body"):
                yield Static(article_content_text, id="article-text")
            with Horizontal(id="article-footer"):
                yield Static(article_link_text, id="article-link")
                yield Button("Bezárás (Esc)", id="close-btn", variant="primary")
    
    def action_close(self) -> None:
        self.dismiss()
    
    def on_key(self, event: events.Key) -> None:
        """Handle key events - close on ESC"""
        if event.key == "escape":
            event.prevent_default()
            self.action_close()
        else:
            # Let other keys be handled normally
            pass
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-btn" or event.button.id == "close-x-btn":
            self.action_close()
    


class DockLayoutExample(App[None]):
    CSS_PATH = "xcss.tcss"

    def get_country_name(self, country_code: str) -> str:
        """Get translated country name"""
        return self.t(f"country_{country_code}")

    # Pressing Tab moves focus to the content list
    BINDINGS = [
        ("tab", "focus_content", "Focus"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("down", "cursor_down", "Down"),
        ("up", "cursor_up", "Up"),
        ("pageup", "page_up", "Page Up"),
        ("pagedown", "page_down", "Page Down"),
        ("enter", "show_detail", "Full article"),
        ("delete", "mark_read", "Mark read"),
        ("ctrl+q", "quit", "Quit"),
        ("l", "change_language", "Language"),
    ]
    
    # Supported languages
    LANGUAGES = {
        "en": "English",
        "hu": "Magyar",
        "fr": "Français",
        "it": "Italiano",
        "es": "Español",
        "pt": "Português",
        "de": "Deutsch",
    }
    
    def __init__(self) -> None:
        super().__init__()
        # Load saved language from database, or use default
        init_db()  # Ensure database is initialized
        saved_language = get_language()
        self.language = saved_language if saved_language else "hu"  # Default language
        
        # Start RSS fetcher service in background
        self.fetcher_service = RSSFetcherService(fetch_interval=60)
        self.fetcher_service.start()
    
    def t(self, key: str) -> str:
        """Get translation for current language"""
        lang = str(self.language) if self.language else "hu"
        return TRANSLATIONS.get(lang, TRANSLATIONS["hu"]).get(key, key)

    # Store rendered entries to show details on highlight
    _entries: List[tuple[str, RssEntry]]
    # Cache entries per source id
    _source_entries: Dict[int, List[RssEntry]]
    _last_selected: set[int]
    # Track read entries
    _read_entries: set[str]
    # Track new entries for visual distinction
    _new_entries: set[str]
    # Track entries that were shown in detail view (bottom panel)
    _detail_viewed_entries: set[str]
    # Track entries that were opened in modal window
    _opened_entries: set[str]
    # Current search query
    _search_query: str = ""
    # Timer for search debounce
    _search_timer = None
    # Page tracking for windowed view
    _page_offset: int = 0  # Index of first visible entry
    _page_size: int = 20  # Number of visible items (will be calculated dynamically)
    _last_page_offset: int = 0  # Track previous page offset to detect page changes

    class _RichMarkupHTMLParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: List[str] = []
            self.link_stack: List[str] = []
            self.list_depth: int = 0
            self._in_rich: int = 0

        def handle_starttag(self, tag: str, attrs):
            tag = tag.lower()
            attrs_dict = dict(attrs)
            if tag == "rich":
                self._in_rich += 1
                return
            if tag in ("b", "strong"):
                self.parts.append("[bold]")
            elif tag in ("i", "em"):
                self.parts.append("[italic]")
            elif tag == "u":
                self.parts.append("[underline]")
            elif tag == "a":
                href = attrs_dict.get("href", "")
                if href:
                    self.parts.append(f"[link={escape(href)}]")
                    self.link_stack.append("link")
            elif tag == "br":
                self.parts.append("\n")
            elif tag in ("p", "div"):
                # Paragraph break before content if not at start
                if self.parts and not self.parts[-1].endswith("\n\n"):
                    self.parts.append("\n\n")
            elif tag in ("ul", "ol"):
                self.list_depth += 1
            elif tag == "li":
                self.parts.append("\n" + ("  " * max(self.list_depth - 1, 0)) + "- ")

        def handle_endtag(self, tag: str):
            tag = tag.lower()
            # rich passthrough end
            if tag == "rich":
                if self._in_rich > 0:
                    self._in_rich -= 1
                return
            if tag in ("b", "strong"):
                self.parts.append("[/bold]")
            elif tag in ("i", "em"):
                self.parts.append("[/italic]")
            elif tag == "u":
                self.parts.append("[/underline]")
            elif tag == "a":
                if self.link_stack:
                    self.link_stack.pop()
                    self.parts.append("[/link]")
            elif tag in ("p", "div"):
                if not self.parts or not self.parts[-1].endswith("\n\n"):
                    self.parts.append("\n\n")
            elif tag in ("ul", "ol"):
                self.list_depth = max(self.list_depth - 1, 0)

        def handle_data(self, data: str):
            if not data:
                return
            if self._in_rich:
                # passthrough Rich markup without escaping
                self.parts.append(data)
            else:
                self.parts.append(escape(data))

        def get_value(self) -> str:
            text = "".join(self.parts)
            # Normalize excessive blank lines
            while "\n\n\n" in text:
                text = text.replace("\n\n\n", "\n\n")
            return text.strip()

    @staticmethod
    def _html_to_markup(html: str) -> str:
        parser = DockLayoutExample._RichMarkupHTMLParser()
        try:
            parser.feed(html)
            parser.close()
        except Exception:
            # On parser errors, fall back to escaped plain text
            return escape(html)
        return parser.get_value()

    def _highlight_search_terms(self, text: str, search_query: str, preserve_markup: bool = False) -> str:
        """
        Highlight search terms in text using Rich markup.
        Returns the text with search terms wrapped in [bold yellow] tags.
        If preserve_markup is True, preserves existing Rich markup (like <rich>...</rich> blocks).
        """
        if not search_query or len(search_query.strip()) < 3:
            return text
        
        # Split query into keywords
        keywords = [kw.strip() for kw in search_query.split() if kw.strip()]
        if not keywords:
            return text
        
        # If preserve_markup is True, protect markup blocks
        if preserve_markup and '<rich>' in text:
            # Split text into parts: markup blocks and regular text
            parts = []
            current_pos = 0
            for match in re.finditer(r'<rich>.*?</rich>', text, re.DOTALL | re.IGNORECASE):
                # Add text before the markup block
                if match.start() > current_pos:
                    text_before = text[current_pos:match.start()]
                    # Highlight keywords in text before
                    for keyword in keywords:
                        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
                        text_before = pattern.sub(
                            lambda m: f"[bold yellow]{m.group()}[/bold yellow]",
                            escape(text_before)
                        )
                    parts.append(text_before)
                # Add the markup block unchanged
                parts.append(match.group())
                current_pos = match.end()
            # Add remaining text after last markup block
            if current_pos < len(text):
                text_after = text[current_pos:]
                for keyword in keywords:
                    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
                    text_after = pattern.sub(
                        lambda m: f"[bold yellow]{m.group()}[/bold yellow]",
                        escape(text_after)
                    )
                parts.append(text_after)
            return ''.join(parts)
        else:
            # Simple case: escape and highlight
            escaped_text = escape(text)
            highlighted_text = escaped_text
            for keyword in keywords:
                pattern = re.compile(re.escape(keyword), re.IGNORECASE)
                highlighted_text = pattern.sub(
                    lambda m: f"[bold yellow]{m.group()}[/bold yellow]",
                    highlighted_text
                )
            return highlighted_text

    def Sidebar(self, selected_sources: set[int] | None = None):
        # Ensure database is initialized before trying to read from it
        init_db()
        # Use provided selected_sources, or load from database
        if selected_sources is None:
            selected_sources = get_selected_sources()
        # If no saved selection, use default (first 3 Hungarian sources)
        if not selected_sources:
            selected_sources = {0, 1, 2}
        
        # Create Tree widget
        tree = Tree(self.t("rss_sources"), id="sources-tree")
        tree.show_root = False
        
        # Group sources by country code
        countries: Dict[str, List[Tuple[int, str, str, str]]] = {}
        for source_id, (country_code, name, url) in SOURCES.items():
            if country_code not in countries:
                countries[country_code] = []
            countries[country_code].append((source_id, country_code, name, url))
        
        # Sort countries alphabetically by translated name
        sorted_countries = sorted(countries.keys(), key=lambda cc: self.get_country_name(cc))
        
        # Add countries as parent nodes and sources as child nodes
        for country_code in sorted_countries:
            country_name = self.get_country_name(country_code)
            country_node = tree.root.add(country_name, data={"type": "country", "country": country_code})
            # Expand by default if any source in this country is selected
            country_selected = any(sid in selected_sources for sid, _, _, _ in countries[country_code])
            if country_selected:
                country_node.expand()
            
            for source_id, country_code, name, url in sorted(countries[country_code], key=lambda x: x[2]):
                is_selected = source_id in selected_sources
                # Add checkbox indicator (using ASCII-compatible characters)
                label = f"{'[X]' if is_selected else '[ ]'} {name}"
                source_node = country_node.add(label, data={"type": "source", "source_id": source_id, "name": name, "url": url, "selected": is_selected})
        
        return tree

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static(self.t("header"), id="header-text")
            yield Select(
                [(name, code) for code, name in self.LANGUAGES.items()],
                value=self.language,
                id="language-select",
                prompt=self.t("language"),
            )
        yield Input(placeholder=self.t("search_placeholder"), id="search-input")
        with Horizontal(id="main"):
            with Static('sidebar', id='sidebar'):
                yield self.Sidebar()
            with Vertical(id="content"):
                yield ListView(id="list")
                with ScrollableContainer(id="detail"):
                    yield Static("", id="detail-content")
        yield Static("", id="footer")

    def action_focus_content(self) -> None:
        # Toggle between list and detail focus
        list_view = self.query_one("#list", ListView)
        detail = self.query_one("#detail", ScrollableContainer)
        if self.focused == list_view:
            detail.focus()
        else:
            list_view.focus()

    def action_cursor_down(self) -> None:
        if self.focused and hasattr(self.focused, 'id') and self.focused.id == "search-input":
            return
        list_view = self.query_one("#list", ListView)
        if self.focused != list_view:
            list_view.focus()
        
        # Get current index in visible list BEFORE moving
        current_visible_idx = getattr(list_view, "index", 0) or 0
        visible_count = len(list_view.children)
        
        self.log(f"action_cursor_down: current_idx={current_visible_idx}, visible_count={visible_count}, page_offset={self._page_offset}")
        
        # Check if we're at the bottom of visible page BEFORE moving
        if current_visible_idx >= visible_count - 1:
            # At bottom - check if we can page down
            if hasattr(self, "_entries") and self._entries:
                total_entries = len(self._entries)
                # Use visible_count to get the actual end of current visible page
                # This ensures we don't skip any items
                current_page_end = self._page_offset + visible_count
                self.log(f"At bottom: current_page_end={current_page_end}, total_entries={total_entries}, visible_count={visible_count}")
                if current_page_end < total_entries:
                    # Move to the next page - start from current_page_end (which is the end of current visible page)
                    new_offset = current_page_end
                    old_offset = self._page_offset
                    self._page_offset = new_offset
                    self.log(f"Paging down: old_offset={old_offset}, new_offset={self._page_offset}")
                    # Re-render with new page - index will be set to 0 by _render_entries_into_list
                    self._render_entries_into_list(self._entries, preserve_position=False)
                    # Explicitly set index to 0 after render to ensure cursor is at top of new page
                    def set_index_to_zero():
                        try:
                            list_view = self.query_one("#list", ListView)
                            if len(list_view.children) > 0:
                                list_view.index = 0
                                self.log(f"Set index to 0 after paging down")
                        except Exception as e:
                            self.log(f"Error setting index to 0: {e}")
                    self.call_after_refresh(set_index_to_zero)
                    self._update_detail_from_list()
                    return
                else:
                    # Already at the end, don't move
                    self.log(f"Cursor down at bottom: already at end")
                    return
        
        # Not at bottom, just move cursor down normally
        # Call the ListView's action_cursor_down to move the cursor
        self.log(f"Not at bottom, moving cursor down normally")
        list_view.action_cursor_down()
        self._update_detail_from_list()

    def action_cursor_up(self) -> None:
        if self.focused and hasattr(self.focused, 'id') and self.focused.id == "search-input":
            return
        list_view = self.query_one("#list", ListView)
        if self.focused != list_view:
            list_view.focus()
        
        # Get current index in visible list BEFORE moving
        current_visible_idx = getattr(list_view, "index", 0) or 0
        visible_count = len(list_view.children)
        
        # Check if we're at the top of visible page BEFORE moving
        if current_visible_idx == 0:
            # At top - check if we can page up
            if self._page_offset > 0:
                current_page_size = getattr(self, "_page_size", visible_count)
                # Move page up - go back by one page_size, but align to page boundaries
                new_offset = max(0, self._page_offset - current_page_size)
                # Make sure we don't go negative
                if new_offset < 0:
                    new_offset = 0
                self._page_offset = new_offset
                self.log(f"Cursor up at top: paging up, new offset={self._page_offset}")
                # Re-render with new page - index will be set to last item by _render_entries_into_list
                if hasattr(self, "_entries") and self._entries:
                    self._render_entries_into_list(self._entries, preserve_position=False)
                    # Set cursor to last item of new page after render
                    def set_to_last_item():
                        try:
                            list_view = self.query_one("#list", ListView)
                            if len(list_view.children) > 0:
                                list_view.index = len(list_view.children) - 1
                        except Exception:
                            pass
                    self.call_after_refresh(set_to_last_item)
                    self._update_detail_from_list()
                    return
            else:
                # Already at the beginning, don't move
                self.log(f"Cursor up at top: already at beginning")
                return
        
        # Not at top, just move cursor up normally
        # Call the ListView's action_cursor_up to move the cursor
        list_view.action_cursor_up()
        self._update_detail_from_list()
    
    def action_page_up(self) -> None:
        """Move page up - replace all visible entries with previous page"""
        if not hasattr(self, "_entries") or not self._entries:
            return
        list_view = self.query_one("#list", ListView)
        was_focused = (self.focused == list_view)
        if not was_focused:
            list_view.focus()
        
        # Move page up by page_size - replace all visible entries with previous page
        new_offset = max(0, self._page_offset - self._page_size)
        if new_offset != self._page_offset:
            self._page_offset = new_offset
            self._render_entries_into_list(self._entries, preserve_position=False)
        self._update_detail_from_list()
    
    def action_page_down(self) -> None:
        """Move page down - replace all visible entries with next page"""
        if not hasattr(self, "_entries") or not self._entries:
            return
        
        list_view = self.query_one("#list", ListView)
        was_focused = (self.focused == list_view)
        if not was_focused:
            list_view.focus()
        
        # Get current page_size
        current_page_size = getattr(self, "_page_size", 20)
        current_offset = getattr(self, "_page_offset", 0)
        total_entries = len(self._entries)
        
        # Calculate new offset - move forward by one page
        new_offset = current_offset + current_page_size
        
        # Only move if there are more entries to show
        if new_offset <= total_entries:
            # Update offset
            self._page_offset = new_offset
            self._render_entries_into_list(self._entries, preserve_position=False)
        self._update_detail_from_list()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Handle Tree node selection - toggle source selection or filter by country"""
        node = event.node
        if node.data and node.data.get("type") == "source":
            # Toggle selection
            is_selected = not node.data.get("selected", False)
            node.data["selected"] = is_selected
            source_id = node.data["source_id"]
            name = node.data["name"]
            
            # Update checkbox in label (using ASCII-compatible characters)
            label = f"{'[X]' if is_selected else '[ ]'} {name}"
            node.set_label(label)
            
            # Update selection and render
            new_selected = self._get_selected_sources_from_tree()
            if new_selected != self._last_selected:
                self._last_selected = new_selected
                save_selected_sources(self._last_selected)
                # Render immediately (will use cache if available)
                self._render_from_selection()
            self.set_status(f"[green]{self.t('selected') if is_selected else self.t('deselected')}:[/] {name}")
        elif node.data and node.data.get("type") == "country":
            # Filter by country: select/deselect all sources in this country
            country_code = node.data.get("country")
            if country_code:
                # Get all sources in this country
                country_sources = [sid for sid, (cc, _, _) in SOURCES.items() if cc == country_code]
                
                # Check if all sources in country are selected
                current_selected = self._get_selected_sources_from_tree()
                all_selected = all(sid in current_selected for sid in country_sources)
                
                # Toggle all sources in country
                tree = self.query_one("#sources-tree", Tree)
                for source_id in country_sources:
                    # Find the source node
                    for child in node.children:
                        if child.data and child.data.get("type") == "source" and child.data.get("source_id") == source_id:
                            # Toggle selection
                            new_selected_state = not all_selected
                            child.data["selected"] = new_selected_state
                            source_name = child.data.get("name", "")
                            label = f"{'[X]' if new_selected_state else '[ ]'} {source_name}"
                            child.set_label(label)
                            break
                
                # Update selection and render
                new_selected = self._get_selected_sources_from_tree()
                if new_selected != self._last_selected:
                    self._last_selected = new_selected
                    save_selected_sources(self._last_selected)
                    # Render immediately (will use cache if available)
                    self._render_from_selection()
                
                country_name = self.get_country_name(country_code)
                action = self.t('selected') if not all_selected else self.t('deselected')
                self.set_status(f"[green]{action}:[/] {country_name} ({len(country_sources)} {self.t('sources')})")
            else:
                # Fallback: just toggle expand/collapse
                if node.is_expanded:
                    node.collapse()
                else:
                    node.expand()
    
    def on_select_changed(self, event: Select.Changed) -> None:
        """Handle language selection change"""
        if event.select.id == "language-select":
            if event.select.value is not None:
                self.language = str(event.select.value)
                # Save language to database
                save_language(self.language)
            # Update header text
            header_text = self.query_one("#header-text", Static)
            header_text.update(self.t("header"))
            # Update search placeholder
            search_input = self.query_one("#search-input", Input)
            search_input.placeholder = self.t("search_placeholder")
            # Rebuild sidebar tree to update country names
            sidebar_container = self.query_one("#sidebar", Static)
            old_tree = self.query_one("#sources-tree", Tree)
            # Save selected sources before removing
            selected_sources = self._get_selected_sources_from_tree()
            old_tree.remove()
            # Wait a bit for the removal to complete
            def rebuild_tree():
                new_tree = self.Sidebar(selected_sources=selected_sources)
                sidebar_container.mount(new_tree)
                # Restore selection after rebuild
                self._last_selected = selected_sources
                # Save to database to keep it in sync
                save_selected_sources(selected_sources)
                self.refresh()
            self.set_timer(0.01, rebuild_tree)
    
    def action_change_language(self) -> None:
        """Action to change language - cycles through languages"""
        lang_codes = list(self.LANGUAGES.keys())
        current_lang = str(self.language) if self.language else "hu"
        current_index = lang_codes.index(current_lang) if current_lang in lang_codes else 0
        next_index = (current_index + 1) % len(lang_codes)
        self.language = lang_codes[next_index]
        # Save language to database
        save_language(self.language)
        # Update Select widget
        language_select = self.query_one("#language-select", Select)
        language_select.value = self.language
        # Update header text
        header_text = self.query_one("#header-text", Static)
        header_text.update(self.t("header"))
        # Update search placeholder
        search_input = self.query_one("#search-input", Input)
        search_input.placeholder = self.t("search_placeholder")
        # Rebuild sidebar tree to update country names
        sidebar_container = self.query_one("#sidebar", Static)
        old_tree = self.query_one("#sources-tree", Tree)
        # Save selected sources before removing
        selected_sources = self._get_selected_sources_from_tree()
        old_tree.remove()
        # Wait a bit for the removal to complete
        def rebuild_tree():
            new_tree = self.Sidebar()
            sidebar_container.mount(new_tree)
            # Restore selection after rebuild
            self._last_selected = selected_sources
            self.refresh()
        self.set_timer(0.01, rebuild_tree)
    
    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Handle ListView highlight changes (works for both j/k and arrow keys)"""
        # Update detail view whenever the highlighted item changes in our list
        # This ensures the summary appears when navigating with arrow keys too
        self._update_detail_from_list()
    
    def on_key(self, event: events.Key) -> None:
        """Handle key events - intercept Page Down/Up and j/k before ListView can handle them"""
        # Only intercept when ListView is focused
        if self.focused and hasattr(self.focused, 'id') and self.focused.id == "list":
            if event.key == "pagedown":
                event.prevent_default()
                event.stop()
                self.log("=== Page Down key intercepted! ===")
                self.action_page_down()
                return
            elif event.key == "pageup":
                event.prevent_default()
                event.stop()
                self.log("=== Page Up key intercepted! ===")
                self.action_page_up()
                return
            elif event.key == "j" or event.key == "down":
                # Intercept j/down to handle pagination at bottom
                event.prevent_default()
                event.stop()
                self.action_cursor_down()
                return
            elif event.key == "k" or event.key == "up":
                # Intercept k/up to handle pagination at top
                event.prevent_default()
                event.stop()
                self.action_cursor_up()
                return
        # For all other keys, don't do anything - let them be handled normally

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle ListView selection (Enter key pressed or mouse click) - show full article"""
        # When Enter is pressed in the list or item is clicked, show the full article
        list_view = getattr(event, 'list_view', None) or getattr(event, 'control', None)
        if list_view and getattr(list_view, 'id', None) == "list":
            self.action_show_detail()
    
    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle search input changes with debounce"""
        if event.input.id == "search-input":
            query = event.value.strip()
            self._search_query = query
            
            # Cancel previous timer if exists
            if self._search_timer is not None:
                self._search_timer.stop()
            
            # If query is empty, clear search immediately
            if len(query) == 0:
                self._render_from_selection()
                return
            
            # Only search if at least 3 characters, with 1 second debounce
            if len(query) >= 3:
                def perform_search():
                    self._render_search_results()
                    self._search_timer = None
                
                self._search_timer = self.set_timer(1.0, perform_search)

    def action_mark_read(self) -> None:
        # Mark current article as read
        if not hasattr(self, "_entries") or not self._entries:
            self.set_status(f"[red]{self.t('no_selection')}[/red]")
            return
        
        list_view = self.query_one("#list", ListView)
        # Get visible index and map to absolute index
        visible_idx = getattr(list_view, "index", 0) or 0
        absolute_idx = self._page_offset + visible_idx
        if absolute_idx < 0 or absolute_idx >= len(self._entries):
            self.set_status(f"[red]{self.t('no_selection')}[/red]")
            return
        
        name, entry = self._entries[absolute_idx]
        entry_id = entry.entry_id or f"{name}:{entry.link or entry.title}"
        
        # Mark as read in database
        mark_as_read(entry_id)
        # Add to local set
        self._read_entries.add(entry_id)
        # Remove from new entries if present
        self._new_entries.discard(entry_id)
        
        # Remove from displayed list
        self._render_from_selection()
        self.set_status(f"[green]{self.t('marked_read')}: {name}[/green]")
    
    def action_show_detail(self) -> None:
        # Download and show full article in modal screen
        # Ensure list view has focus when Enter is pressed
        list_view = self.query_one("#list", ListView)
        if self.focused != list_view and not (self.focused and hasattr(self.focused, 'id') and self.focused.id == "search-input"):
            list_view.focus()
        
        if not hasattr(self, "_entries") or not self._entries:
            self.set_status(f"[red]{self.t('no_selection')}[/red]")
            return
        
        # Get visible index and map to absolute index
        visible_idx = getattr(list_view, "index", 0) or 0
        absolute_idx = self._page_offset + visible_idx
        if absolute_idx < 0 or absolute_idx >= len(self._entries):
            self.set_status(f"[red]{self.t('no_selection')}[/red]")
            return
        
        name, entry = self._entries[absolute_idx]
        
        # Check if newspaper4k is available
        if not HAS_NEWSPAPER:
            self.set_status(f"[red]{self.t('newspaper_not_installed')}[/red]")
            return
        
        # Get the article URL
        article_url = entry.link or ""
        if not article_url:
            self.set_status(f"[red]{self.t('no_link')}[/red]")
            return
        
        # Show loading screen and start async download
        loading_screen = LoadingScreen(str(self.language) if self.language else "hu")
        self.push_screen(loading_screen)
        # Use set_timer to ensure the loading screen is rendered before starting the worker
        def start_download():
            self.run_worker(self._download_article_async(entry, name, article_url, loading_screen))
        self.set_timer(0.1, start_download)
    
    async def _download_article_async(self, entry: RssEntry, name: str, article_url: str, loading_screen: LoadingScreen) -> None:
        """Download article asynchronously"""
        entry_id = entry.entry_id or f"{name}:{entry.link or entry.title}"
        
        # Check if article is cached
        cached_text = get_cached_article_text(entry_id)
        if cached_text:
            # Use cached article text
            article_text = cached_text
            title = entry.title or f"({self.t('no_title')})"
            self.pop_screen()
            self.push_screen(ArticleScreen(title, article_text, name, article_url, str(self.language) if self.language else "hu"))
            self.set_status(f"[green]{self.t('article_opened_cache')}: {name}[/green]")
            
            # Mark this entry as opened in modal window
            was_already_opened = entry_id in self._opened_entries
            if not was_already_opened:
                self._opened_entries.add(entry_id)
                mark_as_opened(entry_id)
                # Update the list to show the opened indicator
                if hasattr(self, "_entries"):
                    self._render_entries_into_list(self._entries, preserve_position=True)
            return
        
        try:
            # Download article with newspaper4k - run blocking operations in thread pool
            article = Article(article_url, language='hu')
            await asyncio.to_thread(article.download)
            await asyncio.to_thread(article.parse)
            
            # Get the article content - prefer plain text from newspaper4k
            article_text = ""
            
            # Use plain text first (newspaper4k extracts clean article text)
            if article.text:
                article_text = escape(article.text)
            
            # If we still don't have content, fall back to summary
            if not article_text:
                summary_html = entry.summary or ""
                if summary_html:
                    try:
                        processed_html, _ = process_images_in_html(summary_html, download_dir="images", base_url=entry.link)
                        article_text = self._html_to_markup(processed_html)
                    except Exception:
                        article_text = escape(summary_html)
            
            # Cache the article text
            if article_text:
                cache_article_text(entry_id, article_text)
            
            # Close loading screen and show article
            title = article.title or entry.title or "(cím nélkül)"
            # Textual workers run in the same thread, so we can call UI methods directly
            self.pop_screen()
            self.push_screen(ArticleScreen(title, article_text, name, article_url, str(self.language) if self.language else "hu"))
            self.set_status(f"[green]{self.t('article_opened')}: {name}[/green]")
            
            # Index the full article text for search
            if article_text:
                # Combine title and full text for search
                full_searchable_text = f"{title} {article_text}".strip()
                was_indexed = is_entry_indexed(entry_id)
                update_entry_full_text(entry_id, full_searchable_text)
                if not was_indexed:
                    self.set_status(f"[yellow]{self.t('indexing')}:[/] {self.t('article_indexed')}")
                self.log(f"DB: Cikk teljes szövege indexelve: {title[:50]}...")
            
            # Mark this entry as opened in modal window
            was_already_opened = entry_id in self._opened_entries
            if not was_already_opened:
                self._opened_entries.add(entry_id)
                mark_as_opened(entry_id)
                # Update the list to show the opened indicator
                if hasattr(self, "_entries"):
                    self._render_entries_into_list(self._entries, preserve_position=True)
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            self.log(f"Error downloading article: {error_trace}")
            self.pop_screen()
            self.set_status(f"[red]{self.t('download_error')}: {e}[/red]")
            # Fall back to showing summary in modal
            try:
                title = entry.title or f"({self.t('no_title')})"
                summary_html = entry.summary or ""
                processed_html, _ = process_images_in_html(summary_html, download_dir="images", base_url=entry.link)
                article_text = self._html_to_markup(processed_html)
                self.push_screen(ArticleScreen(title, article_text, name, article_url, str(self.language) if self.language else "hu"))
                self.set_status(f"[yellow]{self.t('summary_shown')}[/yellow]")
            except Exception as e2:
                error_trace2 = traceback.format_exc()
                self.log(f"Error showing summary: {error_trace2}")
                self.set_status(f"[red]{self.t('open_error')}: {e2}[/red]")

    def set_status(self, message: str) -> None:
        footer = self.query_one("#footer", Static)
        footer.update(Text.from_markup(message))

    def _render_entries_into_list(self, entries: List[tuple[str, RssEntry]], preserve_position: bool = False) -> None:
        try:
            list_view = self.query_one("#list", ListView)
        except Exception as e:
            self.log(f"Error getting list view: {e}")
            return
        
        # Calculate page size based on ListView height - exactly match visible height
        try:
            list_height = list_view.size.height
            # If height is 0 or very small (not yet initialized), use a reasonable default
            if list_height <= 0:
                # Use a larger default if height is not available yet
                if not hasattr(self, "_page_size") or self._page_size == 20:
                    self._page_size = 50
            else:
                # Use exactly the height - each item is 1 line, so we can fit exactly list_height items
                # Don't add extra, we want exactly the visible height
                new_page_size = max(1, list_height)
                # Update if different
                if not hasattr(self, "_page_size") or self._page_size != new_page_size:
                    self._page_size = new_page_size
        except Exception:
            # Fallback to default if height calculation fails
            if not hasattr(self, "_page_size"):
                self._page_size = 50
        
        # Ensure page_offset is valid (don't limit - if there are no entries, empty list is fine)
        # NEVER limit the offset here - let it go beyond entries, we'll just show empty list
        # Only ensure it's not negative
        if self._page_offset < 0:
            self._page_offset = 0
        
        # Only reset offset if we're not preserving position AND there are no entries
        if not entries and not preserve_position:
            self._page_offset = 0
        
        # Check if entries actually changed (by comparing entry IDs)
        entries_changed = True
        # Get last page offset, default to current if not set
        last_page_offset = getattr(self, "_last_page_offset", self._page_offset)
        page_changed = (self._page_offset != last_page_offset)
        if preserve_position and hasattr(self, "_entries") and self._entries:
            # Compare entry IDs to see if list actually changed
            old_entry_ids = {e.entry_id or f"{name}:{e.link or e.title}" for name, e in self._entries}
            new_entry_ids = {e.entry_id or f"{name}:{e.link or e.title}" for name, e in entries}
            entries_changed = (old_entry_ids != new_entry_ids)
        
        # Save current cursor position if we want to preserve it
        current_entry_id = None
        current_absolute_index = None  # Index in full entries list
        current_visible_index = None  # Index in visible page
        
        
        # Only skip rendering if entries didn't change AND page didn't change AND preserving position
        # BUT: always render if list is empty (no children) to ensure placeholder is shown
        list_has_children = len(list_view.children) > 0
        if preserve_position and hasattr(self, "_entries") and self._entries and not entries_changed and not page_changed and list_has_children:
            # If entries didn't change and page didn't change, just update indicators and return early
            current_visible_index = getattr(list_view, "index", None)
            if current_visible_index is not None and 0 <= current_visible_index < len(list_view.children):
                # Map visible index to absolute index
                current_absolute_index = self._page_offset + current_visible_index
                if 0 <= current_absolute_index < len(self._entries):
                    _, current_entry = self._entries[current_absolute_index]
                    current_entry_id = current_entry.entry_id or f"{self._entries[current_absolute_index][0]}:{current_entry.link or current_entry.title}"
            # Only update indicators for changed entries, don't rebuild entire list
            self._entries = entries
            self._update_detail_from_list()
            return
        
        # Save current position if preserving
        if preserve_position and hasattr(self, "_entries") and self._entries:
            current_visible_index = getattr(list_view, "index", None)
            if current_visible_index is not None and 0 <= current_visible_index < len(list_view.children):
                # Map visible index to absolute index
                current_absolute_index = self._page_offset + current_visible_index
                if 0 <= current_absolute_index < len(self._entries):
                    _, current_entry = self._entries[current_absolute_index]
                    current_entry_id = current_entry.entry_id or f"{self._entries[current_absolute_index][0]}:{current_entry.link or current_entry.title}"
        
        # Get visible entries slice (only render what's visible)
        # If offset is beyond entries, show empty list (that's fine, it means we've reached the end)
        if not entries:
            visible_entries = []
        elif self._page_offset >= len(entries):
            visible_entries = []
        else:
            end_offset = min(self._page_offset + self._page_size, len(entries))
            visible_entries = entries[self._page_offset:end_offset]
        
        # Debug: log what we're rendering
        self.log(f"Render: offset={self._page_offset}, page_size={self._page_size}, total={len(entries)}, visible={len(visible_entries)}, entries_changed={entries_changed}, page_changed={page_changed}, preserve={preserve_position}")
        
        # Always clear and rebuild when page changes or entries change
        # (page changes always require rebuild, even with preserve_position)
        # Also rebuild if list is empty (to show placeholder)
        # FORCE rebuild on first render (when _entries doesn't exist or is empty)
        is_first_render = not hasattr(self, "_entries") or not self._entries or len(self._entries) == 0
        should_rebuild = entries_changed or page_changed or not preserve_position or len(list_view.children) == 0 or is_first_render
        if should_rebuild:
            self.log(f"Clearing list and rebuilding... (entries_changed={entries_changed}, page_changed={page_changed}, preserve={preserve_position}, children={len(list_view.children)})")
            # Save current index before clearing
            current_index = getattr(list_view, "index", None) if hasattr(list_view, "index") else None
            
            try:
                list_view.clear()
            except Exception as e:
                self.log(f"Error clearing list view: {e}")
                return
            # Check if we're in search mode
            is_search_mode = hasattr(self, "_search_query") and self._search_query and len(self._search_query.strip()) >= 3
            
            # Build all items first, then append in batch (only for visible entries)
            items_to_add = []
            for idx, (name, entry) in enumerate(visible_entries):
                # Calculate absolute index in full entries list
                absolute_idx = self._page_offset + idx
                
                title = entry.title or f"({self.t('no_title')})"
                # Don't truncate - let the widget handle overflow
                # title = self._truncate_title(title)
                # Get date in YYYY-MM-DD HH:MM format
                date_str = self._format_entry_date(entry)
                entry_id = entry.entry_id or f"{name}:{entry.link or entry.title}"
                # Check if this is a new entry
                is_new = entry_id in self._new_entries
                # Check if this entry was viewed in detail view
                is_detail_viewed = entry_id in getattr(self, "_detail_viewed_entries", set())
                # Check if this entry was opened in modal window
                is_opened = entry_id in getattr(self, "_opened_entries", set())
                
                # Highlight search terms in title if in search mode
                if is_search_mode:
                    highlighted_title = self._highlight_search_terms(title, self._search_query)
                    highlighted_name = self._highlight_search_terms(name, self._search_query)
                else:
                    highlighted_title = escape(title)
                    highlighted_name = escape(name)
                
                # Build indicators (using ASCII-compatible characters)
                indicators = []
                if is_new:
                    indicators.append("[bold cyan]*[/bold cyan]")
                if is_detail_viewed:
                    indicators.append("[dim]o[/dim]")
                if is_opened:
                    indicators.append("[bold yellow]+[/bold yellow]")
                
                indicator_text = " ".join(indicators) + " " if indicators else ""
                
                # Add entry number (absolute index + 1 for 1-based display)
                entry_number = f"[dim]#{absolute_idx + 1}/{len(entries)}[/dim] "
                
                if is_new:
                    item_text = Text.from_markup(f"{entry_number}{indicator_text}[dim]{date_str}[/dim] [bold]{highlighted_name}: {highlighted_title}[/bold]")
                else:
                    item_text = Text.from_markup(f"{entry_number}{indicator_text}[dim]{date_str}[/dim] {highlighted_name}: {highlighted_title}")
                items_to_add.append(ListItem(Static(item_text)))
            
            # Batch append all items at once (faster than one-by-one)
            for item in items_to_add:
                list_view.append(item)
            
            # If no entries, show a placeholder message
            if not items_to_add:
                placeholder_text = self.t("no_entries") if hasattr(self, 't') else "Nincsenek bejegyzések"
                try:
                    placeholder_item = ListItem(Static(Text.from_markup(f"[dim]{placeholder_text}[/dim]")))
                    list_view.append(placeholder_item)
                except Exception:
                    pass
            
            # Set index after render is complete to ensure cursor appears
            def set_index_after_render():
                try:
                    list_view = self.query_one("#list", ListView)
                    # If list is still empty after rendering, ensure placeholder is shown
                    if len(list_view.children) == 0 and not entries:
                        placeholder_text = self.t("no_entries") if hasattr(self, 't') else "Nincsenek bejegyzések"
                        try:
                            placeholder_item = ListItem(Static(Text.from_markup(f"[dim]{placeholder_text}[/dim]")))
                            list_view.append(placeholder_item)
                            self.log(f"Added placeholder message to empty list")
                        except Exception as e:
                            self.log(f"Error adding placeholder: {e}")
                    if not visible_entries or len(list_view.children) == 0:
                        return
                    
                    if preserve_position and current_entry_id is not None:
                        # Find the entry in the visible entries
                        restored_visible_index = None
                        for idx, (name, entry) in enumerate(visible_entries):
                            entry_id = entry.entry_id or f"{name}:{entry.link or entry.title}"
                            if entry_id == current_entry_id:
                                restored_visible_index = idx
                                break
                        if restored_visible_index is not None and restored_visible_index < len(list_view.children):
                            list_view.index = restored_visible_index
                        elif current_visible_index is not None and current_visible_index < len(list_view.children):
                            list_view.index = current_visible_index
                        elif len(list_view.children) > 0:
                            list_view.index = 0
                    elif len(list_view.children) > 0:
                        list_view.index = 0
                    # Always set focus to list view after render
                    list_view.focus()
                    # Update detail view
                    self._update_detail_from_list()
                except Exception as e:
                    self.log(f"Error in set_index_after_render: {e}")
            
            # Use call_after_refresh to ensure render is complete before setting index
            self.call_after_refresh(set_index_after_render)

        
        
        self._entries = entries
        self._last_page_offset = self._page_offset  # Save current page offset
        self._update_detail_from_list()


    def _entry_dt(self, e: RssEntry) -> datetime:
        """Parse published date from RSS entry. Returns datetime for sorting (normalized to UTC)."""
        s = e.published or ""
        if not s:
            # If no published date, use a very old date so it appears at the end
            return datetime(1970, 1, 1, tzinfo=timezone.utc)
        
        # Try email.utils.parsedate_to_datetime first (for RFC 2822 format like "Mon, 01 Jan 2024 12:00:00 +0000")
        try:
            dt = parsedate_to_datetime(s)
            if dt is not None:
                # Convert to UTC for consistent sorting
                if dt.tzinfo is None:
                    # If no timezone info, assume UTC (don't add timezone, just use as-is for comparison)
                    # But for proper sorting, we need timezone-aware datetime
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    # Convert to UTC if timezone is present
                    dt = dt.astimezone(timezone.utc)
                return dt
        except Exception:
            pass
        
        # Try dateutil.parser for ISO 8601 and other formats (like "2024-01-01T12:00:00Z")
        if HAS_DATEUTIL and date_parser is not None:
            try:
                dt = date_parser.parse(s)
                # Convert to UTC for consistent sorting
                if dt.tzinfo is None:
                    # If no timezone info, assume UTC
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    # Convert to UTC if timezone is present
                    dt = dt.astimezone(timezone.utc)
                return dt
            except Exception:
                pass
        
        # If parsing fails, log it and use a very old date so it appears at the end
        self.log(f"Warning: Could not parse date '{s}' for entry '{e.title[:50]}'")
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    
    def _format_entry_date(self, e: RssEntry) -> str:
        """Format entry date as YYYY-MM-DD HH:MM"""
        dt = self._entry_dt(e)
        return dt.strftime("%Y-%m-%d %H:%M")
    
    def _truncate_title(self, title: str, max_length: Optional[int] = None) -> str:
        """Truncate title to max_length characters, adding '...' if truncated.
        If max_length is None, calculates based on ListView width."""
        if max_length is None:
            # Calculate max_length based on ListView width
            try:
                list_view = self.query_one("#list", ListView)
                # Get available width
                available_width = 100  # Default fallback
                if hasattr(list_view, 'content_size') and hasattr(list_view.content_size, 'width'):
                    if list_view.content_size.width > 0:
                        available_width = list_view.content_size.width
                elif hasattr(list_view, 'size') and hasattr(list_view.size, 'width'):
                    if list_view.size.width > 0:
                        available_width = list_view.size.width
                
                # Reserve space for other elements (more conservative estimate)
                # Entry number: ~8 chars, indicators: ~3 chars, date: ~16 chars, name: ~15 chars, separator: ~2 chars, padding: ~2 chars
                reserved_space = 30
                max_length = max(40, available_width - reserved_space)
            except Exception:
                # If we can't get the ListView, use a safe default
                max_length = 80  # Default fallback
        
        if len(title) <= max_length:
            return title
        return title[:max_length - 3] + "..."
    
    def _update_list_item_at_index(self, visible_index: int) -> None:
        """Update a single list item at the given visible index to reflect current state"""
        if not hasattr(self, "_entries") or not self._entries:
            return
        
        # Map visible index to absolute index
        absolute_index = self._page_offset + visible_index
        if absolute_index < 0 or absolute_index >= len(self._entries):
            return
        
        list_view = self.query_one("#list", ListView)
        if visible_index < 0 or visible_index >= len(list_view.children):
            return
        
        name, entry = self._entries[absolute_index]
        title = entry.title or "(cím nélkül)"
        # Don't truncate - let the widget handle overflow
        # title = self._truncate_title(title)
        date_str = self._format_entry_date(entry)
        entry_id = entry.entry_id or f"{name}:{entry.link or entry.title}"
        
        # Check states
        is_new = entry_id in self._new_entries
        is_detail_viewed = entry_id in getattr(self, "_detail_viewed_entries", set())
        is_opened = entry_id in getattr(self, "_opened_entries", set())
        is_search_mode = hasattr(self, "_search_query") and self._search_query and len(self._search_query.strip()) >= 3
        
        # Highlight search terms if in search mode
        if is_search_mode:
            highlighted_title = self._highlight_search_terms(title, self._search_query)
            highlighted_name = self._highlight_search_terms(name, self._search_query)
        else:
            highlighted_title = escape(title)
            highlighted_name = escape(name)
        
        # Build indicators (using ASCII-compatible characters)
        indicators = []
        if is_new:
            indicators.append("[bold cyan]*[/bold cyan]")
        if is_detail_viewed:
            indicators.append("[dim]o[/dim]")
        if is_opened:
            indicators.append("[bold yellow]+[/bold yellow]")
        
        indicator_text = " ".join(indicators) + " " if indicators else ""
        
        if is_new:
            item_text = Text.from_markup(f"{indicator_text}[dim]{date_str}[/dim] [bold]{highlighted_name}: {highlighted_title}[/bold]")
        else:
            item_text = Text.from_markup(f"{indicator_text}[dim]{date_str}[/dim] {highlighted_name}: {highlighted_title}")
        
        # Update the list item (use visible_index for list_view.children)
        try:
            list_item = list_view.children[visible_index]
            if hasattr(list_item, 'children') and list_item.children:
                static_widget = list_item.children[0]
                if hasattr(static_widget, 'update'):
                    static_widget.update(item_text)
        except (IndexError, AttributeError):
            # If update fails, just re-render the whole list
            self._render_entries_into_list(self._entries, preserve_position=True)

    def _update_detail_from_list(self) -> None:
        list_view = self.query_one("#list", ListView)
        detail = self.query_one("#detail-content", Static)
        if not hasattr(self, "_entries") or not self._entries:
            detail.update("")
            return
        # Get visible index and map to absolute index
        visible_idx = getattr(list_view, "index", 0) or 0
        absolute_idx = self._page_offset + visible_idx
        if absolute_idx < 0 or absolute_idx >= len(self._entries):
            detail.update("")
            return
        name, entry = self._entries[absolute_idx]
        entry_id = entry.entry_id or f"{name}:{entry.link or entry.title}"
        
        # Remove from new entries if it was marked as new (user has seen it now)
        was_new = entry_id in self._new_entries
        if was_new:
            self._new_entries.discard(entry_id)
        
        # Mark this entry as viewed in detail view (so it shows the "viewed" indicator)
        was_already_viewed = entry_id in self._detail_viewed_entries
        if not was_already_viewed:
            self._detail_viewed_entries.add(entry_id)
            # Save to database
            mark_as_detail_viewed(entry_id)
        
        # Update the list item if state changed (removed "new" or added "viewed")
        if was_new or not was_already_viewed:
            # Pass visible index to update method
            self._update_list_item_at_index(visible_idx)
        title = entry.title or "(cím nélkül)"
        link = entry.link or ""
        published = entry.published or ""
        summary_html = entry.summary or ""
        
        # Check if we're in search mode
        is_search_mode = hasattr(self, "_search_query") and self._search_query and len(self._search_query.strip()) >= 3
        
        # Process images and convert to markup
        processed_html, _ = process_images_in_html(summary_html, download_dir="images", base_url=entry.link)
        summary_markup = self._html_to_markup(processed_html)
        
        # Highlight search terms if in search mode
        if is_search_mode:
            highlighted_title = self._highlight_search_terms(title, self._search_query)
            highlighted_name = self._highlight_search_terms(name, self._search_query)
            # Don't highlight summary if it contains Rich markup (images) - it would break the markup
            # Instead, just use the original summary_markup
            highlighted_summary = summary_markup
            highlighted_published = self._highlight_search_terms(published, self._search_query)
        else:
            highlighted_title = escape(title)
            highlighted_name = escape(name)
            highlighted_summary = summary_markup
            highlighted_published = escape(published)
        
        # Build the detail text carefully to avoid markup conflicts
        # The summary_markup may already contain Rich markup, so we need to handle it separately
        try:
            # Create Text objects for each part
            title_text = Text.from_markup(f"[bold]{highlighted_title}[/bold]")
            name_text = Text.from_markup(f"[dim]{highlighted_name}[/dim]")
            published_text = Text.from_markup(highlighted_published) if is_search_mode else Text(highlighted_published)
            link_text = Text.from_markup(f"[cyan]{escape(link)}[/cyan]")
            
            # For summary, try to parse as markup, but fall back to plain text if it fails
            try:
                summary_text = Text.from_markup(highlighted_summary)
            except Exception:
                # If markup parsing fails, escape and use as plain text
                summary_text = Text(escape(highlighted_summary))
            
            # Combine all parts
            detail_text = title_text
            detail_text.append("\n")
            detail_text.append(name_text)
            detail_text.append("\n")
            detail_text.append(published_text)
            detail_text.append("\n\n")
            detail_text.append(summary_text)
            detail_text.append("\n\n")
            detail_text.append(link_text)
            
            detail.update(detail_text)
        except Exception as e:
            # Fallback: use plain text if anything goes wrong
            self.log(f"Error updating detail: {e}")
            detail.update(f"{title}\n{name}\n{published}\n\n{summary_html}\n\n{link}")

    def on_mount(self) -> None:
        # Initialize database
        self.set_status(f"[yellow]{self.t('initializing')}:[/] {self.t('db_check')}...")
        init_db()
        
        self._source_entries = {}
        self._last_selected = set()
        
        # Initialize page tracking
        self._page_offset = 0
        self._page_size = 20
        self._last_page_offset = 0
        
        # Load read entries from database
        self.set_status(f"[yellow]{self.t('loading_read')}[/]")
        self._read_entries = get_read_entries()
        
        # Load viewed entries from database
        self.set_status(f"[yellow]{self.t('loading_viewed')}[/]")
        self._detail_viewed_entries = get_detail_viewed_entries()
        self._opened_entries = get_opened_entries()
        
        # Track new entries
        self._new_entries = set()
        
        # Initialize _entries to empty list to ensure it exists
        if not hasattr(self, "_entries"):
            self._entries = []
        
        # Index all unindexed entries on startup
        self.set_status(f"[yellow]{self.t('indexing_unindexed')}[/]")
        indexed_count = index_all_unindexed_entries()
        if indexed_count > 0:
            self.set_status(f"[yellow]{self.t('indexing')}:[/] {indexed_count} {self.t('entries_indexed')}")
            self.log(f"DB: {indexed_count} bejegyzés indexelve az induláskor")
        else:
            self.set_status(f"[green]{self.t('indexing')}:[/] {self.t('all_indexed')}")
        
        # Load initial feeds
        self._load_initial_feeds()
        
        # Recalculate page size after ListView is fully initialized and re-render
        def recalculate_and_render():
            try:
                list_view = self.query_one("#list", ListView)
                if list_view.size.height > 0:
                    # Recalculate page size with actual height
                    list_height = list_view.size.height
                    new_page_size = max(10, list_height + 2)
                    # Only update if significantly different
                    if not hasattr(self, "_page_size") or abs(self._page_size - new_page_size) > 5:
                        self._page_size = new_page_size
                        # Re-render to fill the window properly, but preserve position
                        if hasattr(self, "_entries") and self._entries:
                            self._render_entries_into_list(self._entries, preserve_position=True)
            except Exception as e:
                self.log(f"Error in recalculate_and_render: {e}")
        self.set_timer(0.5, recalculate_and_render)
        
        # Set focus to list view after initial load
        def set_focus_to_list():
            try:
                list_view = self.query_one("#list", ListView)
                list_view.focus()
            except Exception:
                pass  # List view might not be ready yet
        self.set_timer(0.2, set_focus_to_list)
        
        # Poll sidebar selection periodically as a robust fallback
        self.set_interval(0.2, self._poll_sidebar_selection)
        # Refresh feeds every 30 seconds (reload from database, fetcher service handles fetching)
        self.set_interval(30.0, self._refresh_feeds)
        
        # Force initial render after a delay to ensure everything is ready
        # Use a flag to prevent multiple renders
        if not hasattr(self, '_initial_render_done'):
            self._initial_render_done = False
        
        def force_initial_render():
            # Only render once
            if self._initial_render_done:
                return
            
            try:
                self.log("Force initial render called")
                # Ensure _last_selected is set
                if not self._last_selected:
                    selected = get_selected_sources()
                    if not selected:
                        selected = {0, 1, 2}
                    self._last_selected = selected
                    save_selected_sources(self._last_selected)
                
                # Ensure _source_entries is loaded
                if not hasattr(self, '_source_entries') or not self._source_entries:
                    self._source_entries = self._load_entries_from_db(list(self._last_selected))
                
                # Collect entries directly (don't rely on tree)
                collected: List[tuple[str, RssEntry]] = []
                for sid in self._last_selected:
                    source_data = SOURCES.get(sid)
                    if not source_data:
                        continue
                    country, name, url = source_data
                    for e in self._source_entries.get(sid, []):
                        entry_id = e.entry_id or f"{name}:{e.link or e.title}"
                        if entry_id not in self._read_entries:
                            collected.append((name, e))
                collected.sort(key=lambda ne: self._entry_dt(ne[1]), reverse=True)
                
                # Render directly
                self._render_entries_into_list(collected, preserve_position=False)
                self.log(f"Force initial render completed, entries: {len(collected)}")
                self.set_status(f"[green]{self.t('displayed')}:[/] {len(collected)} {self.t('entries')} {f'({len(self._last_selected)} {self.t('sources')})' if self._last_selected else f'(0 {self.t('sources')})'}")
                
                # Set focus and index after render
                def set_focus_and_index():
                    try:
                        list_view = self.query_one("#list", ListView)
                        if list_view:
                            # Set index to first item if available
                            if len(list_view.children) > 0:
                                list_view.index = 0
                                self.log("Set index to 0")
                            # Set focus to list view
                            list_view.focus()
                            self.log("Set focus to list view")
                            # Update detail view
                            self._update_detail_from_list()
                        else:
                            self.log("List view not found in set_focus_and_index")
                    except Exception as e:
                        self.log(f"Error setting focus and index: {e}")
                
                self.call_after_refresh(set_focus_and_index)
                
                # Mark as done
                self._initial_render_done = True
                
                # Double check: if list is still empty, render empty list with placeholder
                def check_list():
                    try:
                        list_view = self.query_one("#list", ListView)
                        if list_view and len(list_view.children) == 0:
                            self.log("List still empty after force render, showing placeholder")
                            self._render_entries_into_list([], preserve_position=False)
                            # Set focus even for empty list
                            list_view.focus()
                    except Exception:
                        pass
                self.call_after_refresh(check_list)
            except Exception as e:
                self.log(f"Error in force_initial_render: {e}")
                import traceback
                self.log(traceback.format_exc())
        
        # Try once after a short delay to ensure UI is ready
        self.set_timer(0.5, force_initial_render)

    def _get_selected_sources_from_tree(self) -> set[int]:
        """Get currently selected sources from the tree widget"""
        try:
            tree = self.query_one("#sources-tree", Tree)
            selected = set()
            # Traverse all nodes to find selected sources
            def traverse(node: TreeNode):
                if node.data and node.data.get("type") == "source":
                    if node.data.get("selected"):
                        selected.add(node.data["source_id"])
                for child in node.children:
                    traverse(child)
            traverse(tree.root)
            return selected
        except Exception:
            return set()

    def _poll_sidebar_selection(self) -> None:
        try:
            current = self._get_selected_sources_from_tree()
            if current != self._last_selected:
                self._last_selected = current
                # Save selected sources to database
                save_selected_sources(current)
                self._render_from_selection()
                sel = ", ".join(str(v) for v in sorted(current)) or "(semmi)"
                self.set_status(f"[dim]Event:[/] selection_changed(poll)  [dim]Kiválasztva:[/] {sel}")
        except Exception:
            pass

    def _ensure_source_loaded(self, source_id: int, check_new: bool = False) -> int:
        """
        Load entries for a source. Returns count of new entries found.
        """
        source_data = SOURCES.get(source_id)
        if not source_data:
            if source_id not in self._source_entries:
                self._source_entries[source_id] = []
            return 0
        country, name, url = source_data
        if not url:
            if source_id not in self._source_entries:
                self._source_entries[source_id] = []
            return 0
        
        # Don't reload if already loaded (unless checking for updates)
        if source_id in self._source_entries and not check_new:
            return 0
        
        # Show loading status only if not in initial load (to avoid too many messages)
        if check_new:
            self.set_status(f"[yellow]{self.t('refreshing')}:[/] {name} {self.t('rss_fetch')}")
        
        entries = fetch_rss(url)
        new_count = 0
        
        if check_new and source_id in self._source_entries:
            # Check for new entries
            existing_ids = {e.entry_id or f"{name}:{e.link or e.title}" for e in self._source_entries[source_id]}
            for e in entries:
                entry_id = e.entry_id or f"{name}:{e.link or e.title}"
                if entry_id not in existing_ids:
                    self._new_entries.add(entry_id)
                    new_count += 1
        
        self._source_entries[source_id] = entries
        # Persist to DB (unique on entry_id)
        to_save = [(source_id, name, e) for e in entries]
        try:
            inserted = save_entries(to_save)
            if inserted:
                if check_new:
                    self.set_status(f"[green]{self.t('refreshing')}:[/] {inserted} {self.t('new_entries')} {name}")
                self.log(f"DB: {inserted} új bejegyzés mentve {name}")
        except Exception as e:
            self.log(f"DB mentési hiba: {e}")
        
        # Index entries that are not yet indexed
        indexed_count = 0
        for e in entries:
            entry_id = e.entry_id or f"{name}:{e.link or e.title}"
            if not is_entry_indexed(entry_id):
                # Create searchable text from title and summary
                searchable_text = f"{e.title or ''} {e.summary or ''}".strip()
                if searchable_text:
                    update_entry_full_text(entry_id, searchable_text)
                    indexed_count += 1
        
        if indexed_count > 0 and check_new:
            self.set_status(f"[yellow]{self.t('indexing')}:[/] {indexed_count} {self.t('entries_indexed_for')} {name}")
            self.log(f"DB: {indexed_count} bejegyzés indexelve {name}")
        
        return new_count

    def _load_entries_from_db(self, source_ids: Iterable[int]) -> Dict[int, List[RssEntry]]:
        """
        Load entries from database for given source IDs.
        Returns a dictionary mapping source_id to list of RssEntry objects.
        """
        if not source_ids:
            return {}
        
        # Get entries from database
        db_entries = get_entries_by_sources(source_ids)
        
        # Convert to RssEntry objects and group by source_id
        result: Dict[int, List[RssEntry]] = {}
        for entry_id, source_id, source_name, title, link, published, summary in db_entries:
            if source_id not in result:
                result[source_id] = []
            entry = RssEntry(
                entry_id=entry_id,
                title=title,
                link=link,
                published=published,
                summary=summary
            )
            result[source_id].append(entry)
        
        return result

    def _render_from_selection(self, preserve_position: bool = False) -> None:
        # If there's a search query, use search results instead
        if self._search_query and len(self._search_query.strip()) >= 3:
            self._render_search_results(preserve_position=preserve_position)
            return
        
        # Reset page offset if not preserving position
        if not preserve_position:
            self._page_offset = 0
        
        # Try to get selected sources from tree, but fall back to _last_selected or database
        # IMPORTANT: On initial load, tree might not be ready, so always check _last_selected first
        selected_values = None
        if self._last_selected:
            # Use _last_selected if available (set during _load_initial_feeds)
            selected_values = list(self._last_selected)
        else:
            # Try tree, but don't fail if it's not ready
            try:
                selected_values = list(self._get_selected_sources_from_tree())
            except Exception:
                pass
            
            # If still no selection, load from database or use default
            if not selected_values:
                selected_values = list(get_selected_sources())
                if not selected_values:
                    selected_values = [0, 1, 2]  # Default selection
        
        selected_set = set(selected_values)
        
        # Only reload from database if selection actually changed
        if selected_set != self._last_selected:
            self._last_selected = selected_set
            # Save selected sources to database
            save_selected_sources(selected_values)
            # Load entries from database (fetcher service handles fetching)
            # Use cached entries if available, only load missing ones
            if not hasattr(self, '_source_entries'):
                self._source_entries = {}
            # Only load entries for newly selected sources
            missing_sources = [sid for sid in selected_values if sid not in self._source_entries]
            if missing_sources:
                new_entries = self._load_entries_from_db(missing_sources)
                self._source_entries.update(new_entries)
        elif not hasattr(self, '_source_entries') or not self._source_entries:
            # If no cached entries, load them
            self._source_entries = self._load_entries_from_db(selected_values)
        
        # Collect and sort, filtering out read entries
        collected: List[tuple[str, RssEntry]] = []
        for sid in selected_values:
            source_data = SOURCES.get(sid)
            if not source_data:
                continue
            country, name, url = source_data
            for e in self._source_entries.get(sid, []):
                entry_id = e.entry_id or f"{name}:{e.link or e.title}"
                if entry_id not in self._read_entries:
                    collected.append((name, e))
        collected.sort(key=lambda ne: self._entry_dt(ne[1]), reverse=True)
        self._render_entries_into_list(collected, preserve_position=preserve_position)
        self.set_status(f"[green]{self.t('displayed')}:[/] {len(collected)} {self.t('entries')} {f'({len(selected_values)} {self.t('sources')})' if selected_values else f'(0 {self.t('sources')})'}")
    
    def _render_search_results(self, preserve_position: bool = False) -> None:
        """Render search results based on current search query"""
        # Reset page offset if not preserving position
        if not preserve_position:
            self._page_offset = 0
        
        results = search_entries(self._search_query)
        collected: List[tuple[str, RssEntry]] = []
        
        # Convert search results to RssEntry objects
        for entry_id, source_id, source_name, title, link, published, summary in results:
            # Filter out read entries
            if entry_id not in self._read_entries:
                entry = RssEntry(
                    entry_id=entry_id,
                    title=title,
                    link=link,
                    published=published,
                    summary=summary
                )
                collected.append((source_name, entry))
        
        collected.sort(key=lambda ne: self._entry_dt(ne[1]), reverse=True)
        self._render_entries_into_list(collected, preserve_position=preserve_position)
        self.set_status(f"[green]{self.t('search')}:[/] {len(collected)} {self.t('results_for')} '{self._search_query}'")

    def _refresh_feeds(self) -> None:
        """Refresh feeds by reloading from database (fetcher service handles fetching)"""
        selected_values = list(self._get_selected_sources_from_tree())
        
        # Trigger immediate fetch in background (non-blocking)
        if self.fetcher_service:
            self.fetcher_service.fetch_now()
        
        # Reload entries from database
        if selected_values:
            old_entry_ids = set()
            for sid in selected_values:
                for e in self._source_entries.get(sid, []):
                    entry_id = e.entry_id or f"{SOURCES.get(sid, ('', '', ''))[1]}:{e.link or e.title}"
                    old_entry_ids.add(entry_id)
            
            # Reload from database
            self._source_entries = self._load_entries_from_db(selected_values)
            
            # Count new entries and add them to _new_entries set
            new_entry_ids = set()
            for sid in selected_values:
                for e in self._source_entries.get(sid, []):
                    entry_id = e.entry_id or f"{SOURCES.get(sid, ('', '', ''))[1]}:{e.link or e.title}"
                    new_entry_ids.add(entry_id)
            
            # Find truly new entries (not in old set)
            truly_new = new_entry_ids - old_entry_ids
            # Add new entries to _new_entries set for visual indication
            for entry_id in truly_new:
                self._new_entries.add(entry_id)
            
            total_new = len(truly_new)
            
            # Only re-render if there are new entries
            if total_new > 0:
                # Save current index before re-rendering
                list_view = self.query_one("#list", ListView)
                current_idx = getattr(list_view, "index", None) or 0
                
                # Re-render with highlighted new entries, preserving cursor position
                self._render_from_selection(preserve_position=True)
                self.set_status(f"[bold green]*[/bold green] [green]{self.t('refreshed')}:[/] {total_new} {self.t('new_articles')}")
                
                # Set focus to list view after refresh and ensure cursor is visible
                def set_focus_after_refresh():
                    try:
                        list_view = self.query_one("#list", ListView)
                        if list_view:
                            # Restore index to ensure cursor is visible
                            if current_idx < len(list_view.children):
                                list_view.index = current_idx
                            list_view.focus()
                    except Exception:
                        pass
                self.set_timer(0.1, set_focus_after_refresh)
            # If no new entries, don't refresh the list, just update status silently
            # (or don't update status at all to avoid unnecessary UI updates)
    
    def _load_initial_feeds(self) -> None:
        # Load only initially selected sources and render from database
        # Try to get from tree first, but fall back to database if tree is not ready
        try:
            selected_values = list(self._get_selected_sources_from_tree())
        except Exception:
            selected_values = []
        
        # If tree doesn't have selection yet, load from database
        if not selected_values:
            selected_values = list(get_selected_sources())
            # If still no selection, use default (first 3 Hungarian sources)
            if not selected_values:
                selected_values = [0, 1, 2]
                # Save default selection to database
                save_selected_sources(set(selected_values))
        
        # Trigger initial fetch in background
        if self.fetcher_service:
            self.fetcher_service.fetch_now()
        
        # Load entries from database (fetcher service handles fetching)
        if selected_values:
            self.set_status(f"[yellow]{self.t('loading_channels')} ({len(selected_values)} {self.t('sources')})...")
            self._source_entries = self._load_entries_from_db(selected_values)
            total_entries = sum(len(entries) for entries in self._source_entries.values())
            self.set_status(f"[green]{self.t('ready')}:[/] {total_entries} {self.t('entries')}")
        else:
            self.set_status(f"[dim]{self.t('no_channels_selected')}[/dim]")
        
        # Update _last_selected to match what we loaded
        self._last_selected = set(selected_values)
        # Save selected sources to ensure they're available for rendering
        save_selected_sources(self._last_selected)
        
        # Note: Actual rendering will be done in on_mount's force_initial_render
        # This ensures the UI is fully ready before rendering



    # Fallback: after any click in the sidebar area, re-render shortly after
    def on_click(self, event) -> None:
        try:
            if getattr(event, "target", None) and hasattr(event.target, "id"):
                # If click occurred within the sidebar subtree
                node = event.target
                while node is not None:
                    if getattr(node, "id", "") == "sidebar":
                        def _delayed():
                            self._render_from_selection()
                            try:
                                selection_list = cast(SelectionList[int], self.query_one("#sidebar SelectionList", SelectionList))
                                sel = ", ".join(str(v) for v in selection_list.selected) or "(semmi)"
                                self.set_status(f"[dim]Event:[/] click(refresh)  [dim]Kiválasztva:[/] {sel}")
                            except Exception:
                                pass
                        self.set_timer(0.05, _delayed)
                        break
                    node = getattr(node, "parent", None)
        except Exception:
            pass


app = DockLayoutExample()
if __name__ == "__main__":
    app.run()