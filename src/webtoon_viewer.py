#!/usr/bin/env python3
"""
Generate static HTML pages for viewing a series downloaded by
webtoon-downloader.

Supports both directory layouts (flat and ``--separate``), with or without
``--export-extra-media`` and ``--export-texts`` in any ``--export-format``.

Writes ``index.html`` (series overview and chapter list) and one page per
chapter (``${CH}.html`` for the flat layout, ``${CH}/index.html`` for the
structured one) into the output directory (default: the series
directory itself), plus a shared stylesheet and a small script that hides
and reveals the chapter toolbar, binds the arrow keys to chapter navigation
and starts/stops music while scrolling.
"""

import argparse
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

CSS_NAME = "webtoon-viewer.css"
JS_NAME = "webtoon-viewer.js"

RX_IMAGE = re.compile(r"^(?P<ch>\d+)_(?P<num>\d+)\.(?P<ext>[A-Za-z0-9]+)$")
RX_THUMB = re.compile(r"^(?P<ch>\d+)_thumbnail\.(?P<ext>[A-Za-z0-9]+)$")
RX_MUSIC = re.compile(r"^(?P<ch>\d+)_(?P<start>\d+)_(?P<stop>\d+)\.ts$")
RX_CH_TEXT = re.compile(r"^(?P<ch>\d+)_(?P<kind>title|notes)\.txt$")
RX_SERIES_IMG = re.compile(r"^(?P<kind>banner|background|thumbnail)\."
                           r"(?P<ext>[A-Za-z0-9]+)$")
NON_IMAGE_EXTS = {"ts", "txt", "json", "html", "htm", "css", "js", "log",
                  "m4a", "mp3", "ogg", "opus", "aac", "wav", "flac"}
# alternative audio containers that may sit next to a .ts file (e.g. created
# with --remux-music); listed before the .ts as browsers rarely play MPEG-TS
AUDIO_ALTERNATIVES = [("m4a", "audio/mp4"), ("mp3", "audio/mpeg"),
                      ("ogg", "audio/ogg"), ("opus", "audio/ogg")]


@dataclass
class Music:
    path: str
    start: int
    stop: int


@dataclass
class Chapter:
    number: int
    label: str
    """chapter number as used in the file names (possibly zero-padded)"""
    images: Dict[int, str] = field(default_factory=dict)
    thumbnail: Optional[str] = None
    music: List[Music] = field(default_factory=list)
    title: Optional[str] = None
    notes: Optional[str] = None
    subdir: str = ""
    """chapter subdirectory holding the images (structured layout), else """""

    @property
    def page(self) -> str:
        """path of the chapter's HTML page, relative to the output directory"""
        if self.subdir:
            return f"{self.subdir}/index.html"
        return f"{self.label}.html"

    @property
    def display_title(self) -> str:
        return self.title or f"Chapter {self.number}"


@dataclass
class Series:
    directory: str
    title: Optional[str] = None
    summary: Optional[str] = None
    banner: Optional[str] = None
    background: Optional[str] = None
    thumbnail: Optional[str] = None
    chapters: Dict[int, Chapter] = field(default_factory=dict)

    @property
    def display_title(self) -> str:
        return self.title or os.path.basename(os.path.abspath(self.directory))

    def chapter(self, number: int, label: str) -> Chapter:
        ch = self.chapters.get(number)
        if ch is None:
            ch = self.chapters[number] = Chapter(number, label)
        elif len(label) > len(ch.label):
            ch.label = label
        return ch


############################## scanning ######################################

def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().rstrip("\n")


def scan_directory(series: Series, rel: str) -> None:
    """Scans one directory (the series root or a chapter subdirectory).
    All stored paths are relative to the series root."""
    full = os.path.join(series.directory, rel)
    for name in sorted(os.listdir(full)):
        path = os.path.join(rel, name) if rel else name
        if os.path.isdir(os.path.join(series.directory, path)):
            continue
        m = RX_MUSIC.match(name)
        if m:
            ch = series.chapter(int(m["ch"]), m["ch"])
            ch.music.append(Music(path, int(m["start"]), int(m["stop"])))
            continue
        m = RX_THUMB.match(name)
        if m:
            series.chapter(int(m["ch"]), m["ch"]).thumbnail = path
            continue
        m = RX_CH_TEXT.match(name)
        if m:
            ch = series.chapter(int(m["ch"]), m["ch"])
            # texts from info.json take precedence (read later, overwrites)
            setattr(ch, m["kind"], read_text(os.path.join(series.directory, path)))
            continue
        m = RX_IMAGE.match(name)
        if m and m["ext"].lower() not in NON_IMAGE_EXTS:
            ch = series.chapter(int(m["ch"]), m["ch"])
            ch.images[int(m["num"])] = path
            if rel:
                ch.subdir = rel
            continue
        if rel:
            continue
        m = RX_SERIES_IMG.match(name)
        if m and m["ext"].lower() not in NON_IMAGE_EXTS:
            setattr(series, m["kind"], path)
        elif name == "title.txt":
            series.title = read_text(os.path.join(series.directory, path))
        elif name == "summary.txt":
            series.summary = read_text(os.path.join(series.directory, path))


def load_series(directory: str) -> Series:
    series = Series(directory)
    scan_directory(series, "")
    # structured layout: one subdirectory per chapter, named by its number
    for name in sorted(os.listdir(directory)):
        if name.isdigit() and os.path.isdir(os.path.join(directory, name)):
            scan_directory(series, name)

    info_path = os.path.join(directory, "info.json")
    if os.path.exists(info_path):
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        series.title = info.get("title") or series.title
        series.summary = info.get("summary") or series.summary
        for key, data in info.get("chapters", {}).items():
            number = int(key)
            ch = series.chapters.get(number)
            if ch is None:
                continue  # texts only, no images downloaded
            ch.title = data.get("title") or ch.title
            ch.notes = data.get("notes") or ch.notes

    # chapters without any comic images cannot be viewed
    for number in [n for n, ch in series.chapters.items() if not ch.images]:
        del series.chapters[number]
    series.chapters = dict(sorted(series.chapters.items()))
    for ch in series.chapters.values():
        ch.music.sort(key=lambda m: (m.start, m.stop))
    return series


######################## image dimensions ####################################

def image_size(path: str) -> Optional[Tuple[int, int]]:
    """Reads width and height from the header of PNG, JPEG, GIF and WebP
    files, so pages can reserve space for lazily loaded images."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
                return struct.unpack(">II", head[16:24])
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", head[6:10])
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                kind = head[12:16]
                if kind == b"VP8 ":
                    w, h = struct.unpack("<HH", head[26:30])
                    return w & 0x3FFF, h & 0x3FFF
                if kind == b"VP8L":
                    b = head[21:25]
                    w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                    h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | (b[1] >> 6))
                    return w, h
                if kind == b"VP8X":
                    w = 1 + int.from_bytes(head[24:27], "little")
                    h = 1 + int.from_bytes(head[27:30], "little")
                    return w, h
                return None
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    marker = f.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    if marker[1] in (0xD8, 0x01) or 0xD0 <= marker[1] <= 0xD7:
                        continue
                    length = struct.unpack(">H", f.read(2))[0]
                    if marker[1] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        h, w = struct.unpack(">xHH", f.read(5))
                        return w, h
                    f.seek(length - 2, os.SEEK_CUR)
    except (OSError, struct.error, IndexError):
        pass
    return None


############################## rendering #####################################

def esc(text: str) -> str:
    return html.escape(text, quote=True)


class Renderer:
    def __init__(self, series: Series, outdir: str, window: int):
        self.series = series
        self.outdir = outdir
        self.base = ""
        """directory of the page being rendered, relative to outdir"""
        self.window = window

    def url(self, path: str) -> str:
        """URL of a file of the series, relative to the current page."""
        return self.rel(os.path.join(self.series.directory, path))

    def link(self, path: str) -> str:
        """URL of a generated file (path relative to the output directory),
        relative to the current page."""
        return self.rel(os.path.join(self.outdir, path))

    def rel(self, full: str) -> str:
        here = os.path.join(self.outdir, self.base)
        return quote(os.path.relpath(full, here).replace(os.sep, "/"))

    def img(self, path: str, alt: str = "", cls: str = "",
            lazy: bool = True, size: bool = True, extra: str = "") -> str:
        attrs = [f'src="{self.url(path)}"', f'alt="{esc(alt)}"']
        if extra:
            attrs.append(extra)
        if cls:
            attrs.append(f'class="{cls}"')
        if size:
            dims = image_size(os.path.join(self.series.directory, path))
            if dims:
                attrs.append(f'width="{dims[0]}" height="{dims[1]}"')
        if lazy:
            attrs.append('loading="lazy"')
        return f"<img {' '.join(attrs)}>"

    def head(self, title: str, extra: str = "") -> str:
        icon = ""
        if self.series.thumbnail:
            icon = f'\n<link rel="icon" href="{self.url(self.series.thumbnail)}">'
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="{self.link(CSS_NAME)}">{icon}{extra}
</head>
"""

    # ---------------------------------------------------------------- index

    def render_index(self) -> str:
        s = self.series
        self.base = ""
        parts = [self.head(s.display_title), "<body class=\"index\">\n"]
        if s.banner or s.background:
            style = ""
            if s.background:
                style = f' style="background-image: url(\'{self.url(s.background)}\')"'
            parts.append(f'<header class="hero"{style}>\n')
            if s.banner:
                parts.append(self.img(s.banner, s.display_title, "banner",
                                      lazy=False) + "\n")
            parts.append("</header>\n")
        parts.append('<main class="overview">\n')
        if s.title or s.summary:
            parts.append(f"<h1>{esc(s.display_title)}</h1>\n")
            if s.summary:
                parts.append(f'<p class="summary">{esc(s.summary)}</p>\n')
        elif not s.banner:
            parts.append(f"<h1>{esc(s.display_title)}</h1>\n")
        parts.append('<ol class="chapter-list">\n')
        for ch in s.chapters.values():
            thumb = ""
            if ch.thumbnail:
                thumb = self.img(ch.thumbnail, "", "thumb", size=False)
            else:
                thumb = f'<span class="thumb no-thumb">{ch.number}</span>'
            music = ' <span class="has-music" title="has music">&#9835;</span>' \
                if ch.music else ""
            parts.append(
                f'<li><a href="{self.link(ch.page)}">{thumb}'
                f'<span class="number">#{ch.number}</span>'
                f'<span class="title">{esc(ch.display_title)}{music}</span>'
                f"</a></li>\n")
        parts.append("</ol>\n</main>\n</body>\n</html>\n")
        return "".join(parts)

    # -------------------------------------------------------------- chapter

    def nav_button(self, ch: Optional[Chapter], text: str, rel: str) -> str:
        if ch is None:
            return f'<span class="button disabled" aria-disabled="true">{text}</span>'
        # access keys work without the script (with the browser's modifier,
        # e.g. Alt+Shift+P/N); the script additionally binds the arrow keys
        key = "p" if rel == "prev" else "n"
        return (f'<a class="button" rel="{rel}" href="{self.link(ch.page)}" '
                f'accesskey="{key}" title="{esc(ch.display_title)}">{text}</a>')

    def tile(self, ch: Chapter, current: bool = False) -> str:
        if ch.thumbnail:
            pic = self.img(ch.thumbnail, "", size=False)
        else:
            pic = f'<span class="no-thumb">{ch.number}</span>'
        cls = "tile current" if current else "tile"
        aria = ' aria-current="page"' if current else ""
        return (f'<a class="{cls}" href="{self.link(ch.page)}"{aria} '
                f'title="{esc(ch.display_title)}">{pic}'
                f"<span>#{ch.number}</span></a>")

    def render_chapter(self, idx: int, chapters: List[Chapter]) -> str:
        s = self.series
        ch = chapters[idx]
        self.base = os.path.dirname(ch.page)
        prev_ch = chapters[idx - 1] if idx > 0 else None
        next_ch = chapters[idx + 1] if idx + 1 < len(chapters) else None

        links = "".join(
            f'\n<link rel="{rel}" href="{self.link(c.page)}">'
            for rel, c in (("prev", prev_ch), ("next", next_ch)) if c)
        script = f'\n<script src="{self.link(JS_NAME)}" defer></script>'
        parts = [self.head(f"{ch.display_title} – {s.display_title}",
                           links + script),
                 '<body class="chapter">\n']

        music_button = ""
        if ch.music:
            music_button = ('<button type="button" '
                            'class="button music-toggle" aria-pressed="false" '
                            'title="Play background music while reading">'
                            '&#9835; off</button>')
        toolbar = (
            '<nav class="toolbar">\n'
            '<div class="left">'
            f'<a class="series" href="{self.link("index.html")}">{esc(s.display_title)}</a>'
            f'<span class="sep">›</span>'
            f'<span class="chapter-title">{esc(ch.display_title)}</span></div>\n'
            '<div class="middle">'
            f'{self.nav_button(prev_ch, "&#9664;", "prev")}'
            f'<span class="chapter-number">#{ch.number}</span>'
            f'{self.nav_button(next_ch, "&#9654;", "next")}</div>\n'
            f'<div class="right">{music_button}</div>\n'
            "</nav>\n")
        # the slot keeps the toolbar's place in the page once the script
        # detaches the toolbar from it (see the stylesheet)
        parts.append(f'<header class="toolbar-slot">\n{toolbar}</header>\n')

        parts.append('<main class="pages">\n')
        for i, (num, path) in enumerate(sorted(ch.images.items())):
            alt = f"page {num}"
            parts.append(self.img(path, alt, lazy=i >= 2,
                                  extra=f'data-page="{num}"') + "\n")
        parts.append("</main>\n")

        for m in ch.music:
            sources = []
            stem = os.path.splitext(m.path)[0]
            for ext, mime in AUDIO_ALTERNATIVES:
                if os.path.exists(os.path.join(s.directory, f"{stem}.{ext}")):
                    sources.append(f'<source src="{self.url(stem + "." + ext)}" '
                                   f'type="{mime}">')
            sources.append(f'<source src="{self.url(m.path)}" type="video/mp2t">')
            parts.append(f'<audio class="bgm" preload="none" loop '
                         f'data-start="{m.start}" data-stop="{m.stop}">'
                         f'{"".join(sources)}</audio>\n')

        before = list(reversed(chapters[max(0, idx - self.window):idx]))
        after = chapters[idx + 1:idx + 1 + self.window]
        parts.append(
            '<nav class="strip" aria-label="nearby chapters">\n'
            '<div class="side before">'
            + "".join(self.tile(c) for c in before) +
            "</div>\n"
            + self.tile(ch, current=True) +
            '\n<div class="side after">'
            + "".join(self.tile(c) for c in after) +
            "</div>\n</nav>\n")

        if ch.notes:
            parts.append('<aside class="notes">\n<h2>Author notes</h2>\n'
                         f"<p>{esc(ch.notes)}</p>\n</aside>\n")
        parts.append("</body>\n</html>\n")
        return "".join(parts)

    # ---------------------------------------------------------------- write

    def write_all(self) -> int:
        os.makedirs(self.outdir, exist_ok=True)
        write(os.path.join(self.outdir, CSS_NAME), STYLESHEET)
        write(os.path.join(self.outdir, JS_NAME), SCRIPT)
        write(os.path.join(self.outdir, "index.html"), self.render_index())
        chapters = list(self.series.chapters.values())
        for idx, ch in enumerate(chapters):
            os.makedirs(os.path.join(self.outdir, os.path.dirname(ch.page)),
                        exist_ok=True)
            write(os.path.join(self.outdir, ch.page),
                  self.render_chapter(idx, chapters))
        return len(chapters)


def write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def remux_music(series: Series) -> None:
    """Copies the AAC stream of each .ts file into an .m4a container, which
    all common browsers can play (most cannot play MPEG-TS directly)."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        sys.exit("error: --remux-music needs ffmpeg in PATH")
    for ch in series.chapters.values():
        for m in ch.music:
            src = os.path.join(series.directory, m.path)
            dst = os.path.splitext(src)[0] + ".m4a"
            if os.path.exists(dst):
                continue
            print(f"remuxing {m.path}")
            subprocess.run([ffmpeg, "-loglevel", "error", "-i", src, "-vn",
                            "-c:a", "copy", dst], check=True)


############################### assets #######################################

STYLESHEET = """\
:root {
  color-scheme: light dark;
  --bg: #f4f4f4; --fg: #1c1c1c; --muted: #6a6a6a; --bar: #ffffff;
  --line: #d8d8d8; --accent: #00b84a; --tile: #e6e6e6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #121212; --fg: #e8e8e8; --muted: #9a9a9a; --bar: #1e1e1e;
    --line: #333333; --accent: #00d564; --tile: #2a2a2a;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--fg);
  font: 16px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
a { color: inherit; text-decoration: none; }
a:hover { color: var(--accent); }

/* ---------------------------------------------------------------- index */
.hero {
  display: flex; justify-content: center; align-items: flex-end;
  background: #000 center top / cover no-repeat;
  min-height: 240px; overflow: hidden;
}
.hero .banner { display: block; max-width: 100%; height: auto; max-height: 70vh; }
.overview { max-width: 960px; margin: 0 auto; padding: 1rem 1rem 3rem; }
.overview h1 { margin: 1rem 0 .5rem; }
.summary { color: var(--muted); white-space: pre-line; margin: 0 0 1.5rem; }
.chapter-list { list-style: none; margin: 0; padding: 0; }
.chapter-list li { border-bottom: 1px solid var(--line); }
.chapter-list a {
  display: flex; align-items: center; gap: 1rem; padding: .5rem .25rem;
}
.chapter-list .thumb {
  flex: none; width: 77px; height: 73px; object-fit: cover; border-radius: 4px;
}
.chapter-list .number { flex: none; width: 4.5em; color: var(--muted); }
.chapter-list .title { flex: 1; }
.no-thumb {
  display: flex; align-items: center; justify-content: center;
  background: var(--tile); color: var(--muted); font-weight: bold;
}
.has-music { color: var(--accent); }

/* -------------------------------------------------------------- chapter */
.chapter { --toolbar-h: 3.5rem; }
.toolbar {
  display: grid; grid-template-columns: 1fr auto 1fr; align-items: center;
  gap: 1rem; padding: 0 1rem; height: var(--toolbar-h);
  background: var(--bar); border-bottom: 1px solid var(--line);
}
/* The toolbar scrolls away with the page. Once it is out of view, the script
   sets .detached on <html>: the toolbar is then fixed just above the viewport,
   and its transparent ::after covers the strip where the toolbar would be.
   Hovering that strip (or tapping it, or tabbing to a toolbar link) hovers the
   toolbar, which slides in. .animate is set a frame after .detached, so the
   switch to fixed position is not animated. */
.toolbar-slot { height: var(--toolbar-h); }
.detached .toolbar {
  position: fixed; top: 0; left: 0; right: 0; z-index: 10;
  transform: translateY(-100%);
}
/* the strip is removed once the toolbar is in and restored once it is out
   again (both after the slide), so it neither extends the hover area below
   the shown toolbar nor catches the mouse while the toolbar slides away */
.detached .toolbar::after {
  content: ""; position: absolute; left: 0; right: 0; top: 100%;
  height: var(--toolbar-h); transition: height 0s .15s;
}
.detached .toolbar:hover::after { height: 0; }
.detached .toolbar:hover, .detached .toolbar:has(:focus-visible) {
  transform: none; box-shadow: 0 2px 8px rgba(0, 0, 0, .25);
}
.detached.animate .toolbar { transition: transform .15s ease-in; }
.detached.animate .toolbar:hover,
.detached.animate .toolbar:has(:focus-visible) {
  transition: transform .15s ease-out;
}
.toolbar .left {
  min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.toolbar .series { font-weight: bold; }
.toolbar .sep { color: var(--muted); margin: 0 .5em; }
.toolbar .middle { display: flex; align-items: center; gap: .75rem; }
.toolbar .right { display: flex; justify-content: flex-end; }
.chapter-number { min-width: 3em; text-align: center; font-weight: bold; }
.button {
  display: inline-block; padding: .25rem .75rem; border-radius: 4px;
  border: 1px solid var(--line); background: var(--tile); color: var(--fg);
  font: inherit; cursor: pointer;
}
a.button:hover, button.button:hover { border-color: var(--accent); }
.button.disabled { opacity: .35; cursor: default; }
.music-toggle[aria-pressed="true"] { border-color: var(--accent); color: var(--accent); }

/* images are shown at their natural size: the column is as wide as the widest
   image but at least as wide as the viewport, so narrow windows get a
   horizontal scrollbar for the page (plain centring would cut off the left
   part of an image wider than the viewport, where no scrollbar reaches) */
.pages {
  display: flex; flex-direction: column; align-items: center; background: #fff;
  width: max-content; min-width: 100%;
}
.pages img { display: block; }

/* thumbnail strip: the current chapter is always centred; each side holds
   only as many tiles as fit in one row, the rest wraps into further rows that
   are pushed far below (large row-gap) and clipped. Sizes are in px, as em in
   custom properties resolves per element and caused mismatched heights. */
.strip {
  --tile-w: 96px; --img-h: 91px; --caption: 20px;
  --tile-h: calc(var(--img-h) + var(--caption)); --gap: 8px;
  display: flex; gap: var(--gap); padding: 1rem;
  background: var(--bar); border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}
.strip .side {
  flex: 1 1 0; min-width: 0; height: calc(var(--tile-h) + 6px);
  display: flex; flex-wrap: wrap; gap: 1000px var(--gap); overflow: hidden;
  padding: 3px 0;
}
.strip .before { flex-direction: row-reverse; }
.tile {
  flex: none; width: var(--tile-w); height: var(--tile-h);
  display: flex; flex-direction: column; align-items: center;
  font-size: 13px; line-height: var(--caption); color: var(--muted);
}
.tile img, .tile .no-thumb {
  width: var(--tile-w); height: var(--img-h); flex: none;
  object-fit: cover; border-radius: 4px; display: block;
}
.tile.current { margin: 3px 0; color: var(--fg); font-weight: bold; }
.tile.current img, .tile.current .no-thumb {
  outline: 3px solid var(--accent); outline-offset: 0;
}

.notes { max-width: 800px; margin: 0 auto; padding: 1rem 1rem 3rem; }
.notes h2 { font-size: 1rem; color: var(--muted); margin: 0 0 .5rem; }
.notes p { white-space: pre-line; margin: 0; }
"""

SCRIPT = """\
// Detaches the chapter toolbar once it has scrolled out of view, so the
// stylesheet can show it as an overlay when hovering the top of the viewport.
(function () {
  "use strict";
  var slot = document.querySelector(".toolbar-slot");
  if (!slot) return;
  var root = document.documentElement;
  function check() {
    var out = slot.getBoundingClientRect().bottom <= 0;
    if (out === root.classList.contains("detached")) return;
    if (out) {
      root.classList.add("detached");
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          if (root.classList.contains("detached")) root.classList.add("animate");
        });
      });
    } else {
      root.classList.remove("detached", "animate");
    }
  }
  window.addEventListener("scroll", check, { passive: true });
  window.addEventListener("resize", check);
  check();
})();

// Left/right arrow keys go to the previous/next chapter, unless the pages are
// wider than the viewport: then the keys keep scrolling sideways as usual.
(function () {
  "use strict";
  function overflowsX(el) { return el && el.scrollWidth > el.clientWidth; }
  document.addEventListener("keydown", function (e) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    if (e.defaultPrevented || e.repeat ||
        e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    var t = e.target;
    if (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    if (overflowsX(document.scrollingElement || document.documentElement) ||
        overflowsX(document.querySelector(".pages"))) return;
    var rel = e.key === "ArrowLeft" ? "prev" : "next";
    var link = document.querySelector('.toolbar a[rel="' + rel + '"]');
    if (!link) return;
    e.preventDefault();
    window.location.href = link.href;
  });
})();

// Starts/stops the background music of a chapter depending on which page is
// at the vertical centre of the viewport. Browsers only allow audio after a
// user gesture, so playback is opt-in through the toolbar button.
(function () {
  "use strict";
  function all(selector) {
    return Array.prototype.slice.call(document.querySelectorAll(selector));
  }
  var buttons = all(".music-toggle");  // in the toolbar and its overlay copy
  var tracks = all("audio.bgm");
  if (!buttons.length || !tracks.length) return;
  var enabled = false;
  var current = -1;

  function update() {
    tracks.forEach(function (a) {
      var inside = current >= +a.dataset.start && current <= +a.dataset.stop;
      if (enabled && inside) {
        if (a.paused) a.play().catch(function () {});
      } else if (!a.paused) {
        a.pause();
        if (!inside) a.currentTime = 0;
      }
    });
  }

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) current = +e.target.dataset.page;
    });
    update();
  }, { rootMargin: "-50% 0px -50% 0px" });
  document.querySelectorAll(".pages img[data-page]").forEach(function (img) {
    observer.observe(img);
  });

  // leaving the pages (toolbar, thumbnail strip) means no page is current
  window.addEventListener("scroll", function () {
    var pages = document.querySelector(".pages").getBoundingClientRect();
    var mid = window.innerHeight / 2;
    if (pages.top > mid || pages.bottom < mid) { current = -1; update(); }
  }, { passive: true });

  function setEnabled(on) {
    enabled = on;
    buttons.forEach(function (b) {
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.innerHTML = on ? "&#9835; on" : "&#9835; off";
    });
    try { localStorage.setItem("webtoon-viewer-music", on ? "1" : "0"); } catch (e) {}
    update();
  }
  buttons.forEach(function (b) {
    b.addEventListener("click", function () { setEnabled(!enabled); });
  });
  // remember the choice; may still need a click if autoplay is blocked
  try { if (localStorage.getItem("webtoon-viewer-music") === "1") setEnabled(true); } catch (e) {}
})();
"""


################################ main ########################################

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate HTML pages to read a series downloaded with "
                    "webtoon-downloader.")
    parser.add_argument("directory", help="directory of the downloaded series")
    parser.add_argument("-o", "--output", metavar="DIR",
                        help="where to write the HTML files "
                             "(default: the series directory)")
    parser.add_argument("--strip-size", type=int, default=12, metavar="N",
                        help="maximum number of chapters shown on each side "
                             "of the current one in the thumbnail strip "
                             "(fewer if the screen is too narrow; default: 12)")
    parser.add_argument("--remux-music", action="store_true",
                        help="use ffmpeg to copy each .ts music file into an "
                             ".m4a file, which more browsers can play")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.directory):
        parser.error(f"not a directory: {args.directory}")
    series = load_series(args.directory)
    if not series.chapters:
        sys.exit(f"error: no chapter images found in {args.directory}")
    if args.remux_music:
        remux_music(series)
    outdir = args.output or args.directory
    n = Renderer(series, outdir, max(0, args.strip_size)).write_all()
    print(f"Wrote index.html and {n} chapter pages to {outdir}")


if __name__ == "__main__":
    main()
