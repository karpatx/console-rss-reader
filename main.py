from typing import cast, Dict, Tuple, List, DefaultDict
from textual.app import App, ComposeResult
from textual.screen import Screen, ModalScreen
from textual.widgets import Static, SelectionList, ListView, ListItem, Button
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual import events

from rss_reader import fetch_rss, RssEntry
from rich.text import Text
from rich.markup import escape
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from html.parser import HTMLParser

from db import init_db, save_entries, mark_as_read, get_read_entries
from image_processor import process_images_in_html

try:
    from newspaper import Article
    HAS_NEWSPAPER = True
except ImportError:
    HAS_NEWSPAPER = False
    print("Warning: newspaper3k not installed")

TEXT = """\
Docking a widget removes it from the layout and fixes its position, aligned to either the top, right, bottom, or left edges of a container.

Docked widgets will not scroll out of view, making them ideal for sticky headers, footers, and sidebars.

"""


class ArticleScreen(ModalScreen):
    """Modal screen for displaying full article content"""
    CSS_PATH = "xcss.tcss"
    
    BINDINGS = [
        ("escape", "close", "Bezárás"),
    ]
    
    def __init__(self, title: str, content: str, source: str, link: str) -> None:
        super().__init__()
        self.article_title = title
        self.article_content = content
        self.article_source = source
        self.article_link = link
    
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
            yield Static(article_title_text, id="article-header")
            yield Static(article_source_text, id="article-subheader")
            with ScrollableContainer(id="article-body"):
                yield Static(article_content_text, id="article-text")
            with Horizontal(id="article-footer"):
                yield Static(article_link_text, id="article-link")
                yield Button("Bezárás (Esc)", id="close-btn", variant="primary")
    
    def action_close(self) -> None:
        self.dismiss()
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-btn":
            self.action_close()


class DockLayoutExample(App[None]):
    CSS_PATH = "xcss.tcss"

    # Map SelectionList values -> (name, feed_url)
    SOURCES: Dict[int, Tuple[str, str]] = {
        0: ("telex.hu", "https://telex.hu/rss"),
        1: ("444.hu", "https://444.hu/feed"),
        2: ("hvg.hu", "https://hvg.hu/rss"),
        3: ("magyarnarancs.hu", "https://magyarnarancs.hu/rss"),
        4: ("24.hu", "https://24.hu/rss"),
        5: ("hang.hu", "https://hang.hu/feed"),
    }

    # Pressing Tab moves focus to the content list
    BINDINGS = [
        ("tab", "focus_content", "Fókusz a tartalomra"),
        ("j", "cursor_down", "Le"),
        ("k", "cursor_up", "Fel"),
        ("down", "cursor_down", "Le"),
        ("up", "cursor_up", "Fel"),
        ("enter", "show_detail", "Teljes cikk"),
        ("delete", "mark_read", "Olvasottnak jelölés"),
    ]

    # Store rendered entries to show details on highlight
    _entries: List[tuple[str, RssEntry]]
    # Cache entries per source id
    _source_entries: Dict[int, List[RssEntry]]
    _last_selected: set[int]
    # Track read entries
    _read_entries: set[str]
    # Track new entries for visual distinction
    _new_entries: set[str]

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

    def Sidebar(self):
        return SelectionList[int](  
            ('telex.hu', 0, True),
            ('444.hu', 1, True),
            ('hvg.hu', 2, True),
            ('magyarnarancs.hu', 3),
            ('24.hu', 4),
            ('hang.hu', 5),
        )

    def compose(self) -> ComposeResult:
        yield Static("RSS Text — v0.1 | Tab: váltás | ↑↓/jk: navigálás | Enter: teljes cikk | Del: olvasott", id="header")
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
        self.query_one("#list", ListView).action_cursor_down()
        self._update_detail_from_list()

    def action_cursor_up(self) -> None:
        self.query_one("#list", ListView).action_cursor_up()
        self._update_detail_from_list()

    def action_mark_read(self) -> None:
        # Mark current article as read
        if not hasattr(self, "_entries") or not self._entries:
            self.set_status("[red]Nincs kiválasztott bejegyzés[/red]")
            return
        
        list_view = self.query_one("#list", ListView)
        idx = getattr(list_view, "index", 0) or 0
        if idx < 0 or idx >= len(self._entries):
            self.set_status("[red]Nincs kiválasztott bejegyzés[/red]")
            return
        
        name, entry = self._entries[idx]
        entry_id = entry.entry_id or f"{name}:{entry.link or entry.title}"
        
        # Mark as read in database
        mark_as_read(entry_id)
        # Add to local set
        self._read_entries.add(entry_id)
        # Remove from new entries if present
        self._new_entries.discard(entry_id)
        
        # Remove from displayed list
        self._render_from_selection()
        self.set_status(f"[green]Olvasottnak jelölve: {name}[/green]")
    
    def action_show_detail(self) -> None:
        # Download and show full article in modal screen
        if not hasattr(self, "_entries") or not self._entries:
            self.set_status("[red]Nincs kiválasztott bejegyzés[/red]")
            return
        
        list_view = self.query_one("#list", ListView)
        idx = getattr(list_view, "index", 0) or 0
        if idx < 0 or idx >= len(self._entries):
            self.set_status("[red]Nincs kiválasztott bejegyzés[/red]")
            return
        
        name, entry = self._entries[idx]
        
        # Check if newspaper3k is available
        if not HAS_NEWSPAPER:
            self.set_status("[red]newspaper3k nincs telepítve[/red]")
            return
        
        # Get the article URL
        article_url = entry.link or ""
        if not article_url:
            self.set_status("[red]Nincs elérhető link[/red]")
            return
        
        # Download article with newspaper3k
        self.set_status("[yellow]Cikk letöltése...[/yellow]")
        try:
            article = Article(article_url, language='hu')
            article.download()
            article.parse()
            
            # Get the article content - prefer plain text from newspaper3k
            article_text = ""
            
            # Use plain text first (newspaper3k extracts clean article text)
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
            
            # Show modal screen with article
            title = article.title or entry.title or "(cím nélkül)"
            self.push_screen(ArticleScreen(title, article_text, name, article_url))
            self.set_status(f"[green]Cikk megnyitva: {name}[/green]")
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            self.log(f"Error downloading article: {error_trace}")
            self.set_status(f"[red]Hiba a cikk letöltésekor: {e}[/red]")
            # Fall back to showing summary in modal
            try:
                title = entry.title or "(cím nélkül)"
                summary_html = entry.summary or ""
                processed_html, _ = process_images_in_html(summary_html, download_dir="images", base_url=entry.link)
                article_text = self._html_to_markup(processed_html)
                self.push_screen(ArticleScreen(title, article_text, name, article_url))
                self.set_status("[yellow]Összefoglaló megjelenítve (teljes cikk nem elérhető)[/yellow]")
            except Exception as e2:
                error_trace2 = traceback.format_exc()
                self.log(f"Error showing summary: {error_trace2}")
                self.set_status(f"[red]Nem sikerült megnyitni a cikket: {e2}[/red]")

    def set_status(self, message: str) -> None:
        footer = self.query_one("#footer", Static)
        footer.update(Text.from_markup(message))

    def _render_entries_into_list(self, entries: List[tuple[str, RssEntry]]) -> None:
        list_view = self.query_one("#list", ListView)
        list_view.clear()
        for name, entry in entries:
            title = entry.title or "(cím nélkül)"
            # Get date in YYYY-MM-DD HH:MM format
            date_str = self._format_entry_date(entry)
            entry_id = entry.entry_id or f"{name}:{entry.link or entry.title}"
            # Check if this is a new entry
            is_new = entry_id in self._new_entries
            if is_new:
                item_text = Text.from_markup(f"[bold cyan]◆[/bold cyan] [dim]{date_str}[/dim] [bold]{escape(name)}: {escape(title)}[/bold]")
            else:
                item_text = Text.from_markup(f"[dim]{date_str}[/dim] {escape(name)}: {escape(title)}")
            list_view.append(ListItem(Static(item_text)))
        # After rendering, move cursor to first item if available
        if entries:
            list_view.index = 0
        self._entries = entries
        self._update_detail_from_list()

    def _entry_dt(self, e: RssEntry) -> datetime:
        s = e.published or ""
        try:
            dt = parsedate_to_datetime(s)
            if dt is None:
                raise ValueError
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
    
    def _format_entry_date(self, e: RssEntry) -> str:
        """Format entry date as YYYY-MM-DD HH:MM"""
        dt = self._entry_dt(e)
        return dt.strftime("%Y-%m-%d %H:%M")

    def _update_detail_from_list(self) -> None:
        list_view = self.query_one("#list", ListView)
        detail = self.query_one("#detail-content", Static)
        if not hasattr(self, "_entries") or not self._entries:
            detail.update("")
            return
        idx = getattr(list_view, "index", 0) or 0
        if idx < 0 or idx >= len(self._entries):
            detail.update("")
            return
        name, entry = self._entries[idx]
        title = entry.title or "(cím nélkül)"
        link = entry.link or ""
        published = entry.published or ""
        summary_html = entry.summary or ""
        # Download/convert images and inline as Rich markup blocks
        processed_html, _ = process_images_in_html(summary_html, download_dir="images", base_url=entry.link)
        summary_markup = self._html_to_markup(processed_html)
        detail.update(Text.from_markup(
            f"[bold]{escape(title)}[/bold]\n[dim]{escape(name)}[/dim]\n{escape(published)}\n\n{summary_markup}\n\n[cyan]{escape(link)}[/cyan]"
        ))

    def on_mount(self) -> None:
        init_db()
        self._source_entries = {}
        self._last_selected = set()
        # Load read entries from database
        self._read_entries = get_read_entries()
        # Track new entries
        self._new_entries = set()
        self._load_initial_feeds()
        # Poll sidebar selection periodically as a robust fallback
        self.set_interval(0.2, self._poll_sidebar_selection)
        # Refresh feeds every minute
        self.set_interval(60.0, self._refresh_feeds)

    def _poll_sidebar_selection(self) -> None:
        try:
            selection_list = cast(SelectionList[int], self.query_one("#sidebar SelectionList", SelectionList))
            current = set(selection_list.selected)
            if current != self._last_selected:
                self._last_selected = current
                self._render_from_selection()
                sel = ", ".join(str(v) for v in sorted(current)) or "(semmi)"
                self.set_status(f"[dim]Event:[/] selection_changed(poll)  [dim]Kiválasztva:[/] {sel}")
        except Exception:
            pass

    def _ensure_source_loaded(self, source_id: int, check_new: bool = False) -> int:
        """
        Load entries for a source. Returns count of new entries found.
        """
        name, url = self.SOURCES.get(source_id, (str(source_id), ""))
        if not url:
            if source_id not in self._source_entries:
                self._source_entries[source_id] = []
            return 0
        
        # Don't reload if already loaded (unless checking for updates)
        if source_id in self._source_entries and not check_new:
            return 0
        
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
                self.log(f"DB: {inserted} új bejegyzés mentve {name}")
        except Exception as e:
            self.log(f"DB mentési hiba: {e}")
        
        return new_count

    def _render_from_selection(self) -> None:
        selection_list = cast(SelectionList[int], self.query_one("#sidebar SelectionList", SelectionList))
        selected_values = list(selection_list.selected)
        self._last_selected = set(selected_values)
        # Ensure newly selected sources are loaded
        for sid in selected_values:
            self._ensure_source_loaded(sid)
        # Collect and sort, filtering out read entries
        collected: List[tuple[str, RssEntry]] = []
        for sid in selected_values:
            name, _ = self.SOURCES.get(sid, (str(sid), ""))
            for e in self._source_entries.get(sid, []):
                entry_id = e.entry_id or f"{name}:{e.link or e.title}"
                if entry_id not in self._read_entries:
                    collected.append((name, e))
        collected.sort(key=lambda ne: self._entry_dt(ne[1]), reverse=True)
        self._render_entries_into_list(collected)
        self.set_status(f"[green]Megjelenítve:[/] {len(collected)} bejegyzés {f'({len(selected_values)} forrás)' if selected_values else '(0 forrás)'}")

    def _refresh_feeds(self) -> None:
        """Refresh feeds and detect new entries"""
        selection_list = cast(SelectionList[int], self.query_one("#sidebar SelectionList", SelectionList))
        selected_values = list(selection_list.selected)
        total_new = 0
        
        for sid in selected_values:
            new_count = self._ensure_source_loaded(sid, check_new=True)
            total_new += new_count
        
        # Re-render with highlighted new entries
        if selected_values:
            self._render_from_selection()
            if total_new > 0:
                self.set_status(f"[bold green]◆[/bold green] [green]Frissítve:[/] {total_new} új cikk")
            else:
                self.set_status(f"[dim]Frissítve:[/] nincs új cikk")
    
    def _load_initial_feeds(self) -> None:
        # Load only initially selected sources and render
        self.set_status("[yellow]Betöltés:[/] RSS csatornák lekérése...")
        selection_list = cast(SelectionList[int], self.query_one("#sidebar SelectionList", SelectionList))
        selected_values = list(selection_list.selected)
        for sid in selected_values:
            self._ensure_source_loaded(sid)
        self._render_from_selection()

    def on_selection_list_selected(self, event) -> None:
        # Sidebar selection changed (checked) → ensure loaded and render filter
        self._render_from_selection()
        try:
            selection_list = cast(SelectionList[int], self.query_one("#sidebar SelectionList", SelectionList))
            sel = ", ".join(str(v) for v in selection_list.selected) or "(semmi)"
            self.set_status(f"[dim]Event:[/] {type(event).__name__}  [dim]Kiválasztva:[/] {sel}")
        except Exception:
            self.set_status(f"[dim]Event:[/] {type(event).__name__}")
        self.action_focus_content()

    def on_selection_list_deselected(self, event) -> None:
        # Sidebar selection changed (unchecked) → render filter
        self._render_from_selection()
        try:
            selection_list = cast(SelectionList[int], self.query_one("#sidebar SelectionList", SelectionList))
            sel = ", ".join(str(v) for v in selection_list.selected) or "(semmi)"
            self.set_status(f"[dim]Event:[/] {type(event).__name__}  [dim]Kiválasztva:[/] {sel}")
        except Exception:
            self.set_status(f"[dim]Event:[/] {type(event).__name__}")
        self.action_focus_content()

    # Some Textual versions emit a generic selection-changed message; handle it too
    def on_selection_list_selection_changed(self, event) -> None:  # type: ignore[override]
        self._render_from_selection()
        try:
            selection_list = cast(SelectionList[int], self.query_one("#sidebar SelectionList", SelectionList))
            sel = ", ".join(str(v) for v in selection_list.selected) or "(semmi)"
            self.set_status(f"[dim]Event:[/] {type(event).__name__}  [dim]Kiválasztva:[/] {sel}")
        except Exception:
            self.set_status(f"[dim]Event:[/] {type(event).__name__}")

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