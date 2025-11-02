from __future__ import annotations

from html.parser import HTMLParser
from typing import List, Tuple, Optional
from urllib.parse import urljoin
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import os

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover
    Image = None  # type: ignore


class _ImgExtractor(HTMLParser):
    def __init__(self, base_url: Optional[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: List[str] = []
        self.images: List[Tuple[str, Optional[str]]] = []  # (url, alt)
        self._img_index = 0

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag == "img":
            attrs_dict = dict(attrs)
            src = attrs_dict.get("src", "").strip()
            alt = attrs_dict.get("alt")
            if src:
                full = urljoin(self.base_url, src) if self.base_url else src
                marker = f"{{{{IMG{self._img_index}}}}}"
                self.parts.append(marker)
                self.images.append((full, alt))
                self._img_index += 1
        else:
            # Preserve other tags minimally
            attr_str = " ".join(f"{k}='{v}'" for k, v in attrs)
            if attr_str:
                self.parts.append(f"<{tag} {attr_str}>")
            else:
                self.parts.append(f"<{tag}>")

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag != "img":
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str):
        self.parts.append(data)

    def get_html_with_markers(self) -> str:
        return "".join(self.parts)


def _to_hex(rgb: Tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"#{r:02x}{g:02x}{b:02x}"


def _image_to_rich_colored(path: str, max_width: int = 60) -> Optional[str]:
    """Return a Rich-markup colored block using '▀' with fg/bg colors.
    Each line represents two image rows (upper=fg, lower=bg).
    """
    if Image is None:
        return None
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            w, h = img.size
            if w <= 0 or h <= 0:
                return None
            new_w = min(max_width, w)
            # We pair two rows into one terminal row, so scale height accordingly
            scale = w / new_w
            new_h = max(2, int(h / scale))
            # Ensure even height for pairing
            if new_h % 2 == 1:
                new_h -= 1
            img = img.resize((new_w, new_h))
            pixels = img.load()
            lines: List[str] = []
            for y in range(0, new_h, 2):
                row_chars: List[str] = []
                for x in range(new_w):
                    top = pixels[x, y]
                    bottom = pixels[x, y + 1]
                    top_hex = _to_hex(top)
                    bottom_hex = _to_hex(bottom)
                    # '▀' draws the upper half in fg color; use bg for lower half
                    row_chars.append(f"[{top_hex} on {bottom_hex}]▀[/]")
                lines.append("".join(row_chars))
            return "\n".join(lines)
    except Exception:
        return None


def _safe_filename(url: str) -> str:
    name = url.split("?")[0].rstrip("/").split("/")[-1] or "image"
    return name[:100]


def process_images_in_html(html: str, download_dir: str, base_url: Optional[str] = None) -> Tuple[str, List[str]]:
    """
    Find <img> tags, download images to download_dir, convert to Rich-markup colored blocks (if Pillow exists),
    and replace the <img> with a <rich>...</rich> block. Returns (new_html, saved_paths).
    """
    os.makedirs(download_dir, exist_ok=True)
    parser = _ImgExtractor(base_url)
    parser.feed(html)
    parser.close()
    html_with_markers = parser.get_html_with_markers()

    saved_paths: List[str] = []
    replacements: List[Tuple[str, str]] = []

    for idx, (img_url, alt) in enumerate(parser.images):
        local_name = _safe_filename(img_url)
        local_path = os.path.join(download_dir, local_name)
        colored_markup: Optional[str] = None
        try:
            req = Request(img_url, headers={"User-Agent": "rss-text/0.1"})
            with urlopen(req, timeout=15) as resp, open(local_path, "wb") as f:
                f.write(resp.read())
            saved_paths.append(local_path)
            colored_markup = _image_to_rich_colored(local_path)
        except Exception:
            colored_markup = None

        alt_text = (alt or "kép").strip()
        marker = f"{{{{IMG{idx}}}}}"
        if colored_markup:
            # Wrap with <rich> so it passes through the HTML→Rich converter unchanged
            markup = f"<rich>\n[dim]- - - kép: {alt_text} - - -[/dim]\n{colored_markup}\n[dim]- - -[/dim]\n</rich>"
        else:
            # Fallback to link placeholder
            show_path = local_path.replace("\\", "/") if os.path.exists(local_path) else img_url
            markup = f"<rich>\n[dim]kép:[/] [link={show_path}]{alt_text}[/link]\n</rich>"
        replacements.append((marker, markup))

    new_html = html_with_markers
    for marker, repl in replacements:
        new_html = new_html.replace(marker, repl)

    return new_html, saved_paths
