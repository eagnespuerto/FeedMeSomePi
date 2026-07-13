#!/usr/bin/env python3
"""
FeedMeSomePi — a tiny RSS aggregator for Raspberry Pi Zero and up.

Deliberately built to keep steady-state memory small on a 512 MB Pi Zero:
- Tkinter (already on PiOS) for the UI, no heavier toolkit.
- feedparser + requests for I/O, both pure-Python.
- Thumbnails downscaled to 96x96 via PIL and cached to disk.
- One background worker thread for refresh, capped concurrency.
- Feed list persisted as JSON in ~/.feedmesomepi/feeds.json.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Iterable
from urllib.parse import urljoin, urlparse

import feedparser
import requests

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


APP_NAME = "FeedMeSomePi"
CONFIG_DIR = Path(os.path.expanduser("~")) / ".feedmesomepi"
FEEDS_FILE = CONFIG_DIR / "feeds.json"
CACHE_DIR = CONFIG_DIR / "cache"
THUMB_SIZE = (96, 96)
REFRESH_INTERVAL_SEC = 15 * 60
HTTP_TIMEOUT = 15
USER_AGENT = f"{APP_NAME}/1.0 (+https://github.com/eagnespuerto/FeedMeSomePi)"
MAX_ENTRIES_PER_FEED = 40

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
NOTIFY_SOUND = ASSETS_DIR / "notify.wav"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Feed:
    url: str
    title: str = ""
    last_checked: float = 0.0
    etag: str = ""
    modified: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "last_checked": self.last_checked,
            "etag": self.etag,
            "modified": self.modified,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Feed":
        return cls(
            url=data["url"],
            title=data.get("title", ""),
            last_checked=data.get("last_checked", 0.0),
            etag=data.get("etag", ""),
            modified=data.get("modified", ""),
        )


@dataclass
class Article:
    feed_url: str
    feed_title: str
    title: str
    link: str
    summary: str
    published: float
    thumbnail_url: str = ""
    seen: bool = False
    _id: str = field(default="", init=False)

    def __post_init__(self) -> None:
        key = f"{self.feed_url}|{self.link}|{self.title}".encode("utf-8")
        self._id = hashlib.sha1(key).hexdigest()

    @property
    def id(self) -> str:
        return self._id


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_feeds() -> list[Feed]:
    if not FEEDS_FILE.exists():
        return []
    try:
        raw = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [Feed.from_dict(d) for d in raw.get("feeds", [])]


def save_feeds(feeds: Iterable[Feed]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FEEDS_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"feeds": [f.to_dict() for f in feeds]}, indent=2),
        encoding="utf-8",
    )
    tmp.replace(FEEDS_FILE)


# ---------------------------------------------------------------------------
# HTML helpers — strip tags for the preview pane, no BeautifulSoup dep.
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif tag in {"p", "br", "li", "div", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def html_to_text(html_str: str) -> str:
    if not html_str:
        return ""
    p = _TextExtractor()
    try:
        p.feed(html_str)
    except Exception:
        return html.unescape(re.sub(r"<[^>]+>", "", html_str))
    return p.text()


# ---------------------------------------------------------------------------
# Feed autodiscovery — accept homepage URLs, not just direct feed URLs.
# ---------------------------------------------------------------------------

_FEED_LINK_RE = re.compile(
    r'<link[^>]+rel=["\']alternate["\'][^>]*>', re.IGNORECASE
)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_TYPE_RE = re.compile(r'type=["\']([^"\']+)["\']', re.IGNORECASE)
_FEED_MIME = {
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
}
_COMMON_PATHS = ["/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/index.xml"]


def discover_feed(url: str, session: requests.Session) -> str:
    """Return a working feed URL for `url` or "" if none can be found."""
    parsed = feedparser.parse(url)
    if parsed.entries:
        return url

    try:
        r = session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return ""

    if r.status_code >= 400:
        return ""

    body = r.text
    for match in _FEED_LINK_RE.findall(body):
        type_m = _TYPE_RE.search(match)
        if not type_m or type_m.group(1).lower() not in _FEED_MIME:
            continue
        href_m = _HREF_RE.search(match)
        if not href_m:
            continue
        candidate = urljoin(r.url, href_m.group(1))
        if feedparser.parse(candidate).entries:
            return candidate

    parsed_url = urlparse(r.url)
    base = f"{parsed_url.scheme}://{parsed_url.netloc}"
    for path in _COMMON_PATHS:
        candidate = base + path
        if feedparser.parse(candidate).entries:
            return candidate

    return ""


# ---------------------------------------------------------------------------
# Thumbnail cache — small compressed JPEGs on disk keyed by URL hash.
# ---------------------------------------------------------------------------

def _thumb_path(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.jpg"


def fetch_thumbnail(url: str, session: requests.Session) -> Path | None:
    if not (HAS_PIL and url):
        return None
    dest = _thumb_path(url)
    if dest.exists():
        return dest
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT, stream=True)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
    except (requests.RequestException, OSError):
        return None
    img.thumbnail(THUMB_SIZE)
    if img.mode not in {"RGB", "L"}:
        img = img.convert("RGB")
    try:
        img.save(dest, "JPEG", quality=70, optimize=True)
    except OSError:
        return None
    return dest


def _pick_thumbnail(entry) -> str:
    for key in ("media_thumbnail", "media_content"):
        val = entry.get(key)
        if val and isinstance(val, list) and val[0].get("url"):
            return val[0]["url"]
    for enc in entry.get("enclosures", []) or []:
        if enc.get("type", "").startswith("image/"):
            return enc.get("href", "")
    summary = entry.get("summary", "")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Feed refresh worker
# ---------------------------------------------------------------------------

def fetch_feed(feed: Feed, session: requests.Session) -> tuple[Feed, list[Article]]:
    headers = {}
    if feed.etag:
        headers["If-None-Match"] = feed.etag
    if feed.modified:
        headers["If-Modified-Since"] = feed.modified

    parsed = feedparser.parse(
        feed.url,
        etag=feed.etag or None,
        modified=feed.modified or None,
        request_headers=headers,
        agent=USER_AGENT,
    )

    feed.last_checked = time.time()
    if getattr(parsed, "status", 0) == 304:
        return feed, []

    if parsed.get("etag"):
        feed.etag = parsed.etag
    if parsed.get("modified"):
        feed.modified = parsed.modified
    if not feed.title:
        feed.title = parsed.feed.get("title", feed.url)

    articles: list[Article] = []
    for entry in parsed.entries[:MAX_ENTRIES_PER_FEED]:
        published_struct = (
            entry.get("published_parsed") or entry.get("updated_parsed")
        )
        published = time.mktime(published_struct) if published_struct else 0.0
        articles.append(
            Article(
                feed_url=feed.url,
                feed_title=feed.title,
                title=entry.get("title", "(untitled)"),
                link=entry.get("link", ""),
                summary=entry.get("summary", ""),
                published=published,
                thumbnail_url=_pick_thumbnail(entry),
            )
        )
    return feed, articles


# ---------------------------------------------------------------------------
# Notify sound
# ---------------------------------------------------------------------------

def play_notify() -> None:
    if not NOTIFY_SOUND.exists():
        return
    for player in ("aplay", "paplay", "afplay"):
        try:
            subprocess.Popen(
                [player, "-q", str(NOTIFY_SOUND)] if player == "aplay"
                else [player, str(NOTIFY_SOUND)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except FileNotFoundError:
            continue


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("820x520")
        self.root.minsize(640, 400)

        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

        self.feeds: list[Feed] = load_feeds()
        self.articles: dict[str, Article] = {}
        self.seen_ids: set[str] = set()
        self.thumb_images: dict[str, ImageTk.PhotoImage] = {}
        self.event_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._render_feed_list()
        self.root.after(200, self._pump_events)

        threading.Thread(target=self._refresh_loop, daemon=True).start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout ------------------------------------------------------------

    def _build_ui(self) -> None:
        style = ttk.Style()
        # "clam" is available on every PiOS Tk build and renders crisply on
        # the small Pi Zero framebuffer.
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        base_font = tkfont.nametofont("TkDefaultFont")
        base_font.configure(size=10)

        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(fill=tk.X)

        ttk.Label(top, text="Feed URL:").pack(side=tk.LEFT)
        self.url_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.url_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        entry.bind("<Return>", lambda _e: self._on_add_feed())

        ttk.Button(top, text="Add", command=self._on_add_feed).pack(side=tk.LEFT)
        ttk.Button(top, text="Refresh", command=self._trigger_refresh).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        paned = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        # Left: feed list
        left = ttk.Frame(paned)
        self.feed_list = tk.Listbox(left, exportselection=False, activestyle="none")
        self.feed_list.pack(fill=tk.BOTH, expand=True)
        self.feed_list.bind("<<ListboxSelect>>", lambda _e: self._render_articles())

        feed_btns = ttk.Frame(left)
        feed_btns.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(feed_btns, text="Remove", command=self._on_remove_feed).pack(
            side=tk.LEFT
        )
        paned.add(left, weight=1)

        # Middle: article list
        mid = ttk.Frame(paned)
        self.article_tree = ttk.Treeview(
            mid,
            columns=("title", "date"),
            show="headings",
            selectmode="browse",
        )
        self.article_tree.heading("title", text="Title")
        self.article_tree.heading("date", text="Published")
        self.article_tree.column("title", width=280, anchor=tk.W)
        self.article_tree.column("date", width=110, anchor=tk.W)
        self.article_tree.pack(fill=tk.BOTH, expand=True)
        self.article_tree.bind("<<TreeviewSelect>>", self._on_article_select)
        self.article_tree.bind("<Double-1>", self._on_article_open)
        paned.add(mid, weight=2)

        # Right: preview
        right = ttk.Frame(paned)
        header = ttk.Frame(right)
        header.pack(fill=tk.X)
        self.preview_title = ttk.Label(
            header, text="", wraplength=280, font=(base_font.cget("family"), 11, "bold")
        )
        self.preview_title.pack(anchor=tk.W)
        self.preview_meta = ttk.Label(header, text="", foreground="#666")
        self.preview_meta.pack(anchor=tk.W, pady=(2, 6))

        self.thumb_label = ttk.Label(right)
        self.thumb_label.pack(anchor=tk.W)

        text_wrap = ttk.Frame(right)
        text_wrap.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.preview_text = tk.Text(
            text_wrap, wrap=tk.WORD, height=10, relief=tk.FLAT,
            padx=6, pady=6,
        )
        self.preview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(
            text_wrap, orient=tk.VERTICAL, command=self.preview_text.yview
        )
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.preview_text.configure(yscrollcommand=scroll.set, state=tk.DISABLED)

        self.open_btn = ttk.Button(
            right, text="Open in browser", command=self._open_current_in_browser
        )
        self.open_btn.pack(anchor=tk.W, pady=(6, 0))
        paned.add(right, weight=2)

        # Status bar
        self.status_var = tk.StringVar(value="Ready.")
        status = ttk.Label(
            self.root, textvariable=self.status_var, anchor=tk.W, padding=(8, 3)
        )
        status.pack(fill=tk.X, side=tk.BOTTOM)

    # ---- rendering ---------------------------------------------------------

    def _render_feed_list(self) -> None:
        current = self.feed_list.curselection()
        self.feed_list.delete(0, tk.END)
        self.feed_list.insert(tk.END, "All feeds")
        for feed in self.feeds:
            label = feed.title or feed.url
            self.feed_list.insert(tk.END, label)
        if current:
            self.feed_list.selection_set(current[0])
        else:
            self.feed_list.selection_set(0)

    def _selected_feed(self) -> Feed | None:
        sel = self.feed_list.curselection()
        if not sel or sel[0] == 0:
            return None
        idx = sel[0] - 1
        if 0 <= idx < len(self.feeds):
            return self.feeds[idx]
        return None

    def _render_articles(self) -> None:
        self.article_tree.delete(*self.article_tree.get_children())
        feed = self._selected_feed()
        items = list(self.articles.values())
        if feed is not None:
            items = [a for a in items if a.feed_url == feed.url]
        items.sort(key=lambda a: a.published, reverse=True)
        for a in items:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(a.published)) \
                if a.published else "—"
            title = a.title if a.seen else "• " + a.title
            self.article_tree.insert(
                "", tk.END, iid=a.id, values=(title, when),
            )

    def _render_preview(self, article: Article) -> None:
        self.preview_title.configure(text=article.title)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(article.published)) \
            if article.published else "—"
        self.preview_meta.configure(text=f"{article.feed_title} · {when}")

        self.preview_text.configure(state=tk.NORMAL)
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert(tk.END, html_to_text(article.summary))
        self.preview_text.configure(state=tk.DISABLED)

        if article.thumbnail_url and HAS_PIL:
            threading.Thread(
                target=self._load_thumbnail_async,
                args=(article.thumbnail_url,),
                daemon=True,
            ).start()
        else:
            self.thumb_label.configure(image="")

    def _load_thumbnail_async(self, url: str) -> None:
        path = fetch_thumbnail(url, self.session)
        if not path:
            return
        self.event_queue.put(("thumb", url, str(path)))

    # ---- actions -----------------------------------------------------------

    def _on_add_feed(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if any(f.url == url for f in self.feeds):
            messagebox.showinfo(APP_NAME, "That feed is already in your list.")
            return
        self.url_var.set("")
        self.status_var.set(f"Looking up {url} …")
        threading.Thread(
            target=self._add_feed_worker, args=(url,), daemon=True
        ).start()

    def _add_feed_worker(self, url: str) -> None:
        discovered = discover_feed(url, self.session)
        if not discovered:
            self.event_queue.put((
                "error", f"No RSS or Atom feed found at {url}."
            ))
            return
        feed = Feed(url=discovered)
        self.event_queue.put(("feed_added", feed))

    def _on_remove_feed(self) -> None:
        feed = self._selected_feed()
        if feed is None:
            return
        if not messagebox.askyesno(APP_NAME, f"Remove feed:\n{feed.title or feed.url}?"):
            return
        self.feeds = [f for f in self.feeds if f.url != feed.url]
        for aid, a in list(self.articles.items()):
            if a.feed_url == feed.url:
                del self.articles[aid]
        save_feeds(self.feeds)
        self._render_feed_list()
        self._render_articles()

    def _on_article_select(self, _event) -> None:
        sel = self.article_tree.selection()
        if not sel:
            return
        article = self.articles.get(sel[0])
        if not article:
            return
        if not article.seen:
            article.seen = True
            self.seen_ids.add(article.id)
            self.article_tree.item(sel[0], values=(
                article.title,
                time.strftime("%Y-%m-%d %H:%M", time.localtime(article.published))
                if article.published else "—",
            ))
        self._render_preview(article)

    def _on_article_open(self, _event) -> None:
        self._open_current_in_browser()

    def _open_current_in_browser(self) -> None:
        sel = self.article_tree.selection()
        if not sel:
            return
        article = self.articles.get(sel[0])
        if article and article.link:
            webbrowser.open(article.link)

    def _trigger_refresh(self) -> None:
        self.event_queue.put(("trigger_refresh", None))

    # ---- background refresh -----------------------------------------------

    def _refresh_loop(self) -> None:
        """Periodic background refresh. Manual refresh is handled by the
        event pump spawning a one-shot worker; this loop only handles the
        timer."""
        while True:
            self._refresh_once()
            time.sleep(REFRESH_INTERVAL_SEC)

    def _refresh_once(self) -> None:
        if not self.feeds:
            return
        self.event_queue.put(("status", "Refreshing feeds…"))
        new_count = 0
        for feed in list(self.feeds):
            try:
                updated_feed, articles = fetch_feed(feed, self.session)
            except Exception as exc:  # noqa: BLE001 - never kill worker
                self.event_queue.put(("status", f"Error on {feed.url}: {exc}"))
                continue
            self.event_queue.put(("articles", updated_feed, articles))
            new_count += len(articles)
        self.event_queue.put((
            "status",
            f"Refreshed {len(self.feeds)} feed(s), {new_count} entries.",
        ))
        save_feeds(self.feeds)

    # ---- main-thread event pump -------------------------------------------

    def _pump_events(self) -> None:
        try:
            while True:
                evt = self.event_queue.get_nowait()
                self._handle_event(evt)
        except queue.Empty:
            pass
        self.root.after(200, self._pump_events)

    def _handle_event(self, evt: tuple) -> None:
        kind = evt[0]
        if kind == "status":
            self.status_var.set(evt[1])
        elif kind == "error":
            messagebox.showerror(APP_NAME, evt[1])
            self.status_var.set(evt[1])
        elif kind == "feed_added":
            feed: Feed = evt[1]
            self.feeds.append(feed)
            save_feeds(self.feeds)
            self._render_feed_list()
            threading.Thread(
                target=self._refresh_single, args=(feed,), daemon=True
            ).start()
        elif kind == "articles":
            feed: Feed = evt[1]
            articles: list[Article] = evt[2]
            for f in self.feeds:
                if f.url == feed.url:
                    f.title = feed.title
                    f.etag = feed.etag
                    f.modified = feed.modified
                    f.last_checked = feed.last_checked
                    break
            newly_added = 0
            for a in articles:
                if a.id in self.articles:
                    continue
                self.articles[a.id] = a
                newly_added += 1
            if newly_added:
                play_notify()
            self._render_feed_list()
            self._render_articles()
        elif kind == "thumb":
            _, url, path = evt
            if not HAS_PIL:
                return
            try:
                img = Image.open(path)
                photo = ImageTk.PhotoImage(img)
            except Exception:
                return
            self.thumb_images[url] = photo  # keep reference
            self.thumb_label.configure(image=photo)
        elif kind == "trigger_refresh":
            threading.Thread(target=self._refresh_once, daemon=True).start()

    def _refresh_single(self, feed: Feed) -> None:
        try:
            updated_feed, articles = fetch_feed(feed, self.session)
        except Exception as exc:  # noqa: BLE001
            self.event_queue.put(("status", f"Error on {feed.url}: {exc}"))
            return
        self.event_queue.put(("articles", updated_feed, articles))

    # ---- teardown ---------------------------------------------------------

    def _on_close(self) -> None:
        save_feeds(self.feeds)
        self.root.destroy()


def main() -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
