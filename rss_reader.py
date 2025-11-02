from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET


@dataclass
class RssEntry:
    title: str
    link: Optional[str]
    published: Optional[str]
    summary: Optional[str]
    entry_id: Optional[str]


def _inner_text(element: ET.Element) -> str:
    # Join all descendant text pieces to avoid truncation
    return "".join(element.itertext()).strip()


def _child_text_by_localname(element: ET.Element, localname: str) -> Optional[str]:
    for child in element:
        tag_local = child.tag.split('}')[-1]
        if tag_local == localname:
            text = _inner_text(child)
            if text:
                return text
    return None


def _child_attr_by_localname(element: ET.Element, localname: str, attr: str) -> Optional[str]:
    for child in element:
        tag_local = child.tag.split('}')[-1]
        if tag_local == localname and attr in child.attrib:
            value = child.attrib.get(attr)
            if value:
                return value
    return None


def fetch_rss(url: str, user_agent: str = "rss-text/0.1 (+https://localhost)") -> List[RssEntry]:
    """
    Fetch and parse an RSS 2.0 or Atom feed and return a list of entries.

    Uses only the Python standard library.
    - For RSS 2.0, reads channel/item fields: title, link, pubDate, description, guid, content:encoded
    - For Atom, reads entry fields: title, link[@rel=alternate]/@href, updated|published, content|summary, id
    """
    req = Request(url, headers={"User-Agent": user_agent})
    try:
        with urlopen(req, timeout=15) as resp:
            xml_bytes = resp.read()
    except (HTTPError, URLError):
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    root_local = root.tag.split('}')[-1].lower()

    entries: List[RssEntry] = []

    if root_local == 'rss':  # RSS 2.0
        channel = next((c for c in root if c.tag.split('}')[-1] == 'channel'), None)
        if channel is None:
            return []
        for item in channel:
            if item.tag.split('}')[-1] != 'item':
                continue
            title = _child_text_by_localname(item, 'title') or ''
            link = _child_text_by_localname(item, 'link')
            published = _child_text_by_localname(item, 'pubDate')
            # Prefer content:encoded when available, else description
            summary = (
                _child_text_by_localname(item, 'encoded')
                or _child_text_by_localname(item, 'description')
            )
            entry_id = _child_text_by_localname(item, 'guid') or link
            entries.append(RssEntry(title=title, link=link, published=published, summary=summary, entry_id=entry_id))
        return entries

    if root_local == 'feed':  # Atom
        for entry in root:
            if entry.tag.split('}')[-1] != 'entry':
                continue
            title = _child_text_by_localname(entry, 'title') or ''
            # Prefer alternate link href
            link_href: Optional[str] = None
            for child in entry:
                if child.tag.split('}')[-1] == 'link':
                    rel = child.attrib.get('rel')
                    href = child.attrib.get('href')
                    if href and (rel is None or rel == 'alternate'):
                        link_href = href
                        break
            published = _child_text_by_localname(entry, 'updated') or _child_text_by_localname(entry, 'published')
            # Prefer full content over summary in Atom
            summary = _child_text_by_localname(entry, 'content') or _child_text_by_localname(entry, 'summary')
            entry_id = _child_text_by_localname(entry, 'id') or link_href
            entries.append(RssEntry(title=title, link=link_href, published=published, summary=summary, entry_id=entry_id))
        return entries

    # Unknown root; attempt to find common item/entry tags regardless of feed type
    for item in root.iter():
        local = item.tag.split('}')[-1]
        if local not in ('item', 'entry'):
            continue
        title = _child_text_by_localname(item, 'title') or ''
        link = _child_text_by_localname(item, 'link') or _child_attr_by_localname(item, 'link', 'href')
        published = (
            _child_text_by_localname(item, 'pubDate')
            or _child_text_by_localname(item, 'updated')
            or _child_text_by_localname(item, 'published')
        )
        # Try content first (Atom), then encoded (RSS), then description/summary
        summary = (
            _child_text_by_localname(item, 'content')
            or _child_text_by_localname(item, 'encoded')
            or _child_text_by_localname(item, 'description')
            or _child_text_by_localname(item, 'summary')
        )
        entry_id = (
            _child_text_by_localname(item, 'guid')
            or _child_text_by_localname(item, 'id')
            or link
        )
        entries.append(RssEntry(title=title, link=link, published=published, summary=summary, entry_id=entry_id))

    return entries
