from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from urllib.parse import quote


# ---------------------------------------------------------------------------
# HTML sanitizer (allowlist-based, stdlib only)
# ---------------------------------------------------------------------------

ALLOWED_TAGS = frozenset({
    "p", "br", "b", "i", "u", "strong", "em", "code", "pre",
    "blockquote", "ol", "ul", "li", "a", "h1", "h2", "h3",
    "h4", "h5", "h6", "span", "del", "sup", "sub", "table",
    "thead", "tbody", "tr", "th", "td", "caption", "hr", "img",
    "details", "summary",
})

ALLOWED_ATTRS: dict[str, set[str]] = {
    "a": {"href", "rel", "target"},
    "img": {"src", "alt", "width", "height", "title"},
    "span": {"data-mx-color", "data-mx-bg-color"},
    "code": {"class"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}


class _Sanitizer(HTMLParser):
    """Strip all HTML tags/attributes not in the allowlist."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag not in ALLOWED_TAGS:
            return
        allowed = ALLOWED_ATTRS.get(tag, set())
        safe_attrs: list[str] = []
        for name, value in attrs:
            name = name.lower()
            if name not in allowed:
                continue
            if name.startswith("on"):
                continue
            val = value or ""
            if "javascript:" in val.lower() or "vbscript:" in val.lower():
                continue
            safe_attrs.append(f' {escape(name)}="{escape(val)}"')
        self.parts.append(f"<{tag}{''.join(safe_attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ALLOWED_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(escape(data))

    def handle_entityref(self, name: str) -> None:
        if not self._skip:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._skip:
            self.parts.append(f"&#{name};")

    def get_output(self) -> str:
        return "".join(self.parts)


def sanitize_html(html_str: str) -> str:
    s = _Sanitizer()
    s.feed(html_str)
    return s.get_output()


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

_SENDER_COLORS = [
    "#e91e63", "#9c27b0", "#673ab7", "#3f51b5", "#2196f3",
    "#00bcd4", "#009688", "#4caf50", "#ff9800", "#ff5722",
    "#e040fb", "#00e5ff", "#76ff03", "#ffd740",
]


def sender_color(mxid: str) -> str:
    h = int(hashlib.md5(mxid.encode()).hexdigest(), 16)
    return _SENDER_COLORS[h % len(_SENDER_COLORS)]


def sender_initials(name: str) -> str:
    local = name.lstrip("@").split(":")[0] if ":" in name else name
    if " " in local:
        words = local.split()
        return (words[0][0] + words[-1][0]).upper()
    return local[:2].upper()


def format_time(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


def format_date(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%B %d, %Y")


def format_iso(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.isoformat()


def safe_element_id(event_id: str) -> str:
    return event_id.replace("$", "evt-").replace(":", "-").replace(".", "_")


def mxc_to_http(mxc_url: str, homeserver_url: str, proxy_base_url: str = "") -> str:
    if not mxc_url or not mxc_url.startswith("mxc://"):
        return mxc_url or ""
    server_media = mxc_url[6:]
    if proxy_base_url:
        return f"{proxy_base_url.rstrip('/')}/media/{server_media}"
    hs = homeserver_url.rstrip("/")
    return f"{hs}/_matrix/media/v3/download/{server_media}"


# ---------------------------------------------------------------------------
# Message body rendering
# ---------------------------------------------------------------------------

def render_body(msg: dict, homeserver_url: str, proxy_base_url: str = "") -> str:
    if msg.get("redacted"):
        return '<em class="webpublish-redacted">this message was deleted</em>'

    msgtype = msg.get("msgtype", "m.text")

    if msgtype == "m.image":
        url = mxc_to_http(msg.get("media_url", ""), homeserver_url, proxy_base_url)
        body_text = msg.get("body", "")
        alt = escape(body_text or "image")
        # Show body as caption if it looks like prose rather than a bare filename
        # (filenames have no spaces and end in a file extension)
        is_filename = bool(re.fullmatch(r'[^\s]+\.[a-zA-Z0-9]{2,5}', body_text or ""))
        caption = "" if is_filename else escape(body_text)
        if url:
            img = f'<img class="webpublish-media" src="{escape(url)}" alt="{alt}" loading="lazy">'
            linked = f'<a href="{escape(url)}" target="_blank" rel="noopener">{img}</a>'
            if caption:
                return f'<figure class="webpublish-figure">{linked}<figcaption>{caption}</figcaption></figure>'
            return linked
        return f"[image: {alt}]"

    if msgtype == "m.file":
        url = mxc_to_http(msg.get("media_url", ""), homeserver_url, proxy_base_url)
        name = escape(msg.get("body", "file"))
        if url:
            return f'<a class="webpublish-file" href="{escape(url)}" download>{name}</a>'
        return f"[file: {name}]"

    if msgtype == "m.location":
        geo_uri = msg.get("geo_uri") or ""
        osm_url = "https://www.openstreetmap.org"
        try:
            coords = geo_uri.removeprefix("geo:").split(";")[0].split(",")
            lat, lon = float(coords[0]), float(coords[1])
            osm_url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=14/{lat}/{lon}"
            tile_url = f"{proxy_base_url.rstrip('/')}/tiles/{{z}}/{{x}}/{{y}}.png"
            map_id = f"map-{abs(hash(geo_uri)) % 1_000_000}"
            return (
                f'<div class="webpublish-map" id="{map_id}"'
                f' data-lat="{lat}" data-lon="{lon}"'
                f' data-tile-url="{escape(tile_url)}"'
                f' style="width:100%;height:300px;">'
                f'<noscript><a href="{escape(osm_url)}" target="_blank" rel="noopener">'
                f'View location ({lat}, {lon}) on OpenStreetMap</a></noscript>'
                f'</div>'
            )
        except (IndexError, ValueError):
            return (
                f'<a href="{escape(osm_url)}" target="_blank" rel="noopener">'
                f'[location: {escape(geo_uri or "unknown")}]</a>'
            )

    if msgtype == "m.emote":
        name = escape(msg.get("sender_name") or msg.get("sender", ""))
        body = escape(msg.get("body", ""))
        return f"<em>* {name} {body}</em>"

    # m.text, m.notice, fallback
    formatted = msg.get("formatted_body")
    if formatted:
        return sanitize_html(formatted)
    return escape(msg.get("body", "")).replace("\n", "<br>")


def _render_reply_header(msg: dict) -> str:
    """Render a clickable back-link to the message being replied to."""
    reply_el_id = msg.get("reply_to_element_id")
    if not reply_el_id:
        return ""
    reply_sender = escape(msg.get("reply_to_sender", ""))
    reply_body = escape(msg.get("reply_to_body", ""))
    reply_color = sender_color(msg.get("reply_to_mxid", reply_sender))
    return (
        f'<a class="webpublish-reply-to" href="#{reply_el_id}">'
        f'<span class="webpublish-reply-arrow">&#8617;</span> '
        f'<strong style="color:{reply_color}">{reply_sender}</strong> '
        f'<span class="webpublish-reply-preview">{reply_body}</span>'
        f'</a>\n'
    )


def render_message_html(msg: dict, homeserver_url: str, proxy_base_url: str = "", show_reply_header: bool = True) -> str:
    eid = safe_element_id(msg["event_id"])
    color = sender_color(msg["sender"])
    initials = sender_initials(msg.get("sender_name") or msg["sender"])
    name = escape(msg.get("sender_name") or msg["sender"])
    time_str = format_time(msg["timestamp"])
    iso = format_iso(msg["timestamp"])
    body_html = render_body(msg, homeserver_url, proxy_base_url)
    edited = ' <span class="webpublish-edited">(edited)</span>' if msg.get("edited") else ""
    notice_cls = " webpublish-notice" if msg.get("msgtype") == "m.notice" else ""
    reply_html = _render_reply_header(msg) if show_reply_header else ""
    raw_avatar = msg.get("avatar_url") or ""
    if raw_avatar:
        avatar_http = escape(mxc_to_http(raw_avatar, homeserver_url, proxy_base_url))
        avatar_img = f'<img class="webpublish-avatar-img" src="{avatar_http}" alt="" onerror="this.style.display=\'none\'">'
    else:
        avatar_img = ""

    return (
        f'<div class="webpublish-message{notice_cls}" id="{eid}">\n'
        f'  <div class="webpublish-avatar" style="background-color:{color}">{initials}{avatar_img}</div>\n'
        f'  <div class="webpublish-message-content">\n'
        f'    {reply_html}'
        f'    <div class="webpublish-message-header">\n'
        f'      <span class="webpublish-sender" style="color:{color}">{name}</span>\n'
        f'      <time class="webpublish-timestamp" datetime="{iso}">{time_str}</time>{edited}\n'
        f'    </div>\n'
        f'    <div class="webpublish-body">{body_html}</div>\n'
        f'  </div>\n'
        f'</div>'
    )


def render_post_preview_html(post: dict, alias: str, comment_count: int) -> str:
    eid = safe_element_id(post["event_id"])
    title_line = (post.get("body") or "").split("\n", 1)[0][:120]
    author = escape(post.get("sender_name") or post["sender"])
    date = format_date(post["timestamp"])
    if comment_count:
        comments_text = f"{comment_count} comment{'s' if comment_count != 1 else ''}"
    else:
        comments_text = "no comments"
    post_url = f"post/{quote(post['event_id'], safe='')}" if not alias else f"{alias}/post/{quote(post['event_id'], safe='')}"

    return (
        f'<article class="webpublish-post-preview" id="{eid}">\n'
        f'  <h2 class="webpublish-post-title">'
        f'<a href="{post_url}">{escape(title_line) or "<em>untitled</em>"}</a></h2>\n'
        f'  <div class="webpublish-post-meta">\n'
        f'    <span>{author}</span>\n'
        f'    <span>{date}</span>\n'
        f'    <span>{comments_text}</span>\n'
        f'  </div>\n'
        f'</article>'
    )


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

THEMES: dict[str, str] = {
    "light": """\
:root {
  --bg: #ffffff;
  --bg-secondary: #f6f8fa;
  --text: #1f2328;
  --text-muted: #656d76;
  --border: #d0d7de;
  --accent: #0969da;
  --code-bg: rgba(0,0,0,0.06);
}""",

    "dark": """\
:root {
  --bg: #0d1117;
  --bg-secondary: #161b22;
  --text: #e6edf3;
  --text-muted: #8b949e;
  --border: #30363d;
  --accent: #58a6ff;
  --code-bg: rgba(255,255,255,0.1);
}""",

    "catppuccin-light": """\
:root {
  --bg: #eff1f5;
  --bg-secondary: #e6e9ef;
  --text: #4c4f69;
  --text-muted: #9ca0b0;
  --border: #ccd0da;
  --accent: #1e66f5;
  --code-bg: rgba(0,0,0,0.06);
}""",

    "catppuccin-dark": """\
:root {
  --bg: #1e1e2e;
  --bg-secondary: #181825;
  --text: #cdd6f4;
  --text-muted: #6c7086;
  --border: #313244;
  --accent: #89b4fa;
  --code-bg: rgba(255,255,255,0.1);
}""",

    "gruvbox": """\
:root {
  --bg: #282828;
  --bg-secondary: #3c3836;
  --text: #ebdbb2;
  --text-muted: #928374;
  --border: #504945;
  --accent: #83a598;
  --code-bg: rgba(255,255,255,0.1);
}""",

    "solarized-light": """\
:root {
  --bg: #fdf6e3;
  --bg-secondary: #eee8d5;
  --text: #657b83;
  --text-muted: #93a1a1;
  --border: #d3cbb8;
  --accent: #268bd2;
  --code-bg: rgba(0,0,0,0.06);
}""",

    "solarized-dark": """\
:root {
  --bg: #002b36;
  --bg-secondary: #073642;
  --text: #839496;
  --text-muted: #586e75;
  --border: #1b4a59;
  --accent: #268bd2;
  --code-bg: rgba(255,255,255,0.1);
}""",
}

BASE_CSS = """\
:root {
  --bg: #ffffff;
  --bg-secondary: #f6f8fa;
  --text: #1f2328;
  --text-muted: #656d76;
  --border: #d0d7de;
  --accent: #0969da;
  --code-bg: rgba(0,0,0,0.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --bg-secondary: #161b22;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --border: #30363d;
    --accent: #58a6ff;
    --code-bg: rgba(255,255,255,0.1);
  }
}
*, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.5;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* header */
.webpublish-header {
  position: sticky; top: 0; z-index: 100;
  padding: 24px 32px; border-bottom: 1px solid var(--border); background: var(--bg-secondary);
  transition: padding 0.25s ease;
}
.webpublish-header h1 { font-size: 1.5rem; font-weight: 600; }
.webpublish-header p  {
  color: var(--text-muted); margin-top: 4px;
  max-height: 6rem; overflow: hidden;
  transition: max-height 0.25s ease, opacity 0.25s ease, margin-top 0.25s ease;
}
@media (max-width: 600px) {
  .webpublish-header.scrolled { padding: 10px 32px; cursor: pointer; }
  .webpublish-header.scrolled p { max-height: 0; opacity: 0; margin-top: 0; }
}

/* ---- chat mode ---- */
.webpublish-chat { display: flex; flex-direction: column; height: calc(100vh - 85px); }
.webpublish-messages { flex: 1; overflow-y: auto; padding: 16px 32px; }
.webpublish-message { display: flex; gap: 12px; padding: 8px 0; }
.webpublish-message:hover { background: rgba(255,255,255,0.02); border-radius: 4px; }
.webpublish-avatar {
  width: 36px; height: 36px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 0.75rem; font-weight: 600; color: #fff; flex-shrink: 0;
  position: relative; overflow: hidden;
}
.webpublish-avatar-img {
  position: absolute; inset: 0; width: 100%; height: 100%;
  object-fit: cover; border-radius: 50%;
}
.webpublish-message-content { min-width: 0; flex: 1; }
.webpublish-message-header  { display: flex; align-items: baseline; gap: 8px; }
.webpublish-sender    { font-weight: 600; font-size: 0.9rem; }
.webpublish-timestamp { font-size: 0.75rem; color: var(--text-muted); }
.webpublish-edited    { font-size: 0.75rem; color: var(--text-muted); font-style: italic; }
.webpublish-reply-to {
  display: flex; align-items: baseline; gap: 4px;
  font-size: 0.8rem; color: var(--text-muted); margin-bottom: 2px;
  padding: 2px 8px; border-left: 2px solid var(--border); border-radius: 2px;
  text-decoration: none; max-width: 100%; overflow: hidden;
}
.webpublish-reply-to:hover { background: rgba(255,255,255,0.04); text-decoration: none; }
.webpublish-reply-arrow { flex-shrink: 0; }
.webpublish-reply-preview {
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.webpublish-body { margin-top: 2px; word-wrap: break-word; overflow-wrap: break-word; }
.webpublish-body img.webpublish-media {
  max-width: 400px; max-height: 300px; border-radius: 8px; margin: 4px 0;
}
.webpublish-figure { display: inline-block; margin: 4px 0; }
.webpublish-figure figcaption { font-size: 0.85em; color: var(--text-muted); margin-top: 4px; }
.webpublish-body pre {
  background: var(--bg); padding: 12px; border-radius: 6px; overflow-x: auto; margin: 4px 0;
}
.webpublish-body code {
  background: var(--code-bg); padding: 2px 6px; border-radius: 3px; font-size: 0.9em;
}
.webpublish-body pre code { background: none; padding: 0; }
.webpublish-body blockquote {
  border-left: 3px solid var(--border); padding-left: 12px; color: var(--text-muted); margin: 4px 0;
}
.webpublish-notice  { opacity: 0.7; }
.webpublish-redacted { color: var(--text-muted); }

/* ---- journal mode ---- */
.webpublish-journal { max-width: 800px; margin: 0 auto; padding: 0 24px; }
.webpublish-posts   { margin-top: 24px; }
.webpublish-post-preview {
  border: 1px solid var(--border); border-radius: 8px; padding: 24px;
  margin-bottom: 16px; background: var(--bg-secondary); transition: border-color 0.2s;
}
.webpublish-post-preview:hover { border-color: var(--accent); }
.webpublish-post-title { font-size: 1.25rem; font-weight: 600; margin-bottom: 8px; }
.webpublish-post-title a { color: var(--text); }
.webpublish-post-title a:hover { color: var(--accent); }
.webpublish-post-meta {
  font-size: 0.85rem; color: var(--text-muted); margin-bottom: 12px; display: flex; gap: 16px;
}
.webpublish-post-excerpt { color: var(--text-muted); }

/* journal post detail */
.webpublish-post-full { max-width: 800px; margin: 0 auto; padding: 24px; }
.webpublish-post-full .webpublish-post-body {
  margin: 24px 0; line-height: 1.7; text-align: justify; hyphens: auto;
}
.webpublish-post-full .webpublish-post-body img { max-width: 100%; border-radius: 8px; }
.webpublish-post-full .webpublish-post-body p + p { margin-top: 1em; }
.webpublish-post-full .webpublish-post-body pre {
  background: var(--bg); padding: 16px; border-radius: 6px; overflow-x: auto;
}
.webpublish-post-full .webpublish-post-body code {
  background: var(--code-bg); padding: 2px 6px; border-radius: 3px;
}
.webpublish-post-full .webpublish-post-body pre code { background: none; padding: 0; }
.webpublish-post-full .webpublish-post-body blockquote {
  border-left: 3px solid var(--border); padding-left: 16px; color: var(--text-muted);
}
.webpublish-comments { margin-top: 32px; border-top: 1px solid var(--border); padding-top: 24px; }
.webpublish-comments h2 { font-size: 1.1rem; margin-bottom: 16px; }
.webpublish-comment {
  display: flex; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--border);
}
.webpublish-comment:last-child { border-bottom: none; }

/* pagination */
.webpublish-pagination {
  display: flex; justify-content: center; gap: 8px; padding: 24px 0;
}
.webpublish-pagination a, .webpublish-pagination span {
  padding: 8px 16px; border-radius: 6px; border: 1px solid var(--border);
}
.webpublish-pagination a:hover { background: var(--bg-secondary); }
.webpublish-pagination .active {
  background: var(--accent); color: #fff; border-color: var(--accent);
}
.webpublish-back-link { display: inline-block; margin-bottom: 16px; }
.webpublish-map { position: relative; border-radius: 8px; overflow: hidden; }
"""


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------

_LEAFLET_HEAD_ASSETS = (
    '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">\n'
    '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>'
)

_LEAFLET_INIT_SCRIPT = (
    '<script>\n'
    '(function() {\n'
    '  function initMaps(root) {\n'
    '    (root || document).querySelectorAll(".webpublish-map:not([data-map-init])").forEach(function(el) {\n'
    '      el.setAttribute("data-map-init", "1");\n'
    '      var lat = parseFloat(el.getAttribute("data-lat"));\n'
    '      var lon = parseFloat(el.getAttribute("data-lon"));\n'
    '      var tileUrl = el.getAttribute("data-tile-url");\n'
    '      if (isNaN(lat) || isNaN(lon)) return;\n'
    '      var map = L.map(el, {zoomControl:true, scrollWheelZoom:false}).setView([lat,lon], 14);\n'
    '      L.tileLayer(tileUrl, {maxZoom:19, attribution:\'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors\'}).addTo(map);\n'
    '      L.marker([lat,lon]).addTo(map);\n'
    '    });\n'
    '  }\n'
    '  initMaps();\n'
    '  window._wpInitMaps = initMaps;\n'
    '})();\n'
    '</script>'
)


def _page_head(title: str, custom_css: str, extra_head: str = "") -> str:
    # User CSS goes in a second <style> block that follows BASE_CSS. This ensures
    # @import-ed themes and :root variable overrides win via cascade order
    # (later blocks beat earlier blocks at the same specificity). Within the user
    # block, @import lines are hoisted to the top as the CSS spec requires.
    import_lines, override_lines = [], []
    for line in (custom_css or "").splitlines():
        (import_lines if line.strip().startswith("@import") else override_lines).append(line)
    css_imports = "\n".join(import_lines)
    css_overrides = "\n".join(override_lines)
    user_css = f"{css_imports}\n{css_overrides}".strip()
    user_style = f"<style>\n{user_css}\n</style>\n" if user_css else ""
    leaflet = f"{extra_head}\n" if extra_head else ""
    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{escape(title)}</title>\n'
        f'<style>\n{BASE_CSS}\n</style>\n'
        f'{user_style}'
        f'{leaflet}'
        f'</head>'
    )


_LOCALIZE_TIMESTAMPS_SCRIPT = (
    '<script>\n'
    '(function() {\n'
    '  function localizeTimestamps(root) {\n'
    '    (root || document).querySelectorAll("time.webpublish-timestamp").forEach(function(el) {\n'
    '      var iso = el.getAttribute("datetime");\n'
    '      if (!iso) return;\n'
    '      var d = new Date(iso);\n'
    '      if (isNaN(d)) return;\n'
    '      el.textContent = d.toLocaleString(undefined, {\n'
    '        year: "numeric", month: "short", day: "numeric",\n'
    '        hour: "2-digit", minute: "2-digit"\n'
    '      });\n'
    '    });\n'
    '  }\n'
    '  localizeTimestamps();\n'
    '  window._wpLocalizeTimestamps = localizeTimestamps;\n'
    '})();\n'
    '</script>'
)


def _sse_chat_script(encoded_alias: str) -> str:
    sse_url = "sse" if not encoded_alias else f"{encoded_alias}/sse"
    return (
        '<script>\n'
        '(function() {\n'
        '  var msgs = document.getElementById("messages");\n'
        '  function isNearBottom() {\n'
        '    return msgs.scrollHeight - msgs.clientHeight <= msgs.scrollTop + 80;\n'
        '  }\n'
        '  function scrollBottom() { msgs.scrollTop = msgs.scrollHeight; }\n'
        '  scrollBottom();\n'
        f'  var src = new EventSource("{sse_url}");\n'
        '  src.addEventListener("new_message", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var near = isNearBottom();\n'
        '    msgs.insertAdjacentHTML("beforeend", d.html);\n'
        '    if (window._wpLocalizeTimestamps) window._wpLocalizeTimestamps(msgs.lastElementChild);\n'
        '    if (window._wpInitMaps) window._wpInitMaps(msgs.lastElementChild);\n'
        '    if (near) scrollBottom();\n'
        '  });\n'
        '  src.addEventListener("edit_message", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var el = document.getElementById(d.element_id);\n'
        '    if (!el) return;\n'
        '    var body = el.querySelector(".webpublish-body");\n'
        '    if (body) body.innerHTML = d.body_html;\n'
        '    var hdr = el.querySelector(".webpublish-message-header");\n'
        '    if (hdr && !hdr.querySelector(".webpublish-edited")) {\n'
        '      hdr.insertAdjacentHTML("beforeend",\n'
        '        \' <span class="webpublish-edited">(edited)</span>\');\n'
        '    }\n'
        '  });\n'
        '  src.addEventListener("redact_message", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var el = document.getElementById(d.element_id);\n'
        '    if (el) el.remove();\n'
        '  });\n'
        '})();\n'
        '</script>'
    )


def _sse_journal_landing_script(encoded_alias: str) -> str:
    sse_url = "sse" if not encoded_alias else f"{encoded_alias}/sse"
    return (
        '<script>\n'
        '(function() {\n'
        f'  var src = new EventSource("{sse_url}");\n'
        '  src.addEventListener("new_message", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    if (d.is_thread) return;\n'
        '    var posts = document.querySelector(".webpublish-posts");\n'
        '    if (posts) posts.insertAdjacentHTML("afterbegin", d.html);\n'
        '  });\n'
        '  src.addEventListener("redact_message", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var el = document.getElementById(d.element_id);\n'
        '    if (el) el.remove();\n'
        '  });\n'
        '})();\n'
        '</script>'
    )


def _sse_post_detail_script(post_event_id: str) -> str:
    return (
        '<script>\n'
        '(function() {\n'
        '  var base = window.location.pathname.replace(/\\/post\\/[^\\/]*$/, "");\n'
        '  var src = new EventSource(base + "/sse");\n'
        '  src.addEventListener("new_message", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        f'    if (d.thread_root !== "{escape(post_event_id)}") return;\n'
        '    var el = document.getElementById("comments");\n'
        '    if (el) {\n'
        '      el.insertAdjacentHTML("beforeend", d.html);\n'
        '      if (window._wpLocalizeTimestamps) window._wpLocalizeTimestamps(el.lastElementChild);\n'
        '      if (window._wpInitMaps) window._wpInitMaps(el.lastElementChild);\n'
        '    }\n'
        '  });\n'
        '  src.addEventListener("edit_message", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var el = document.getElementById(d.element_id);\n'
        '    if (!el) return;\n'
        '    var body = el.querySelector(".webpublish-body");\n'
        '    if (body) body.innerHTML = d.body_html;\n'
        '  });\n'
        '  src.addEventListener("redact_message", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var el = document.getElementById(d.element_id);\n'
        '    if (el) el.remove();\n'
        '  });\n'
        '})();\n'
        '</script>'
    )


def _scroll_header_script() -> str:
    return (
        '<script>\n'
        '(function() {\n'
        '  var header = document.querySelector(".webpublish-header");\n'
        '  if (!header) return;\n'
        '  var mq = window.matchMedia("(max-width: 600px)");\n'
        '  var scroller = document.getElementById("messages") || window;\n'
        '  function getScrollY() {\n'
        '    return scroller === window ? window.scrollY : scroller.scrollTop;\n'
        '  }\n'
        '  function update() {\n'
        '    if (mq.matches && getScrollY() > 10) {\n'
        '      header.classList.add("scrolled");\n'
        '    } else {\n'
        '      header.classList.remove("scrolled");\n'
        '    }\n'
        '  }\n'
        '  scroller.addEventListener("scroll", update, {passive: true});\n'
        '  mq.addEventListener("change", update);\n'
        '  header.addEventListener("click", function() {\n'
        '    if (header.classList.contains("scrolled")) {\n'
        '      scroller.scrollTo({top: 0, behavior: "smooth"});\n'
        '    }\n'
        '  });\n'
        '})();\n'
        '</script>'
    )


# ---------------------------------------------------------------------------
# Full page functions
# ---------------------------------------------------------------------------

def _needs_leaflet(messages: list[dict]) -> bool:
    return any(m.get("msgtype") == "m.location" and m.get("geo_uri") for m in messages)

def render_chat_page(
    room_name: str,
    room_topic: str,
    messages: list[dict],
    encoded_alias: str,
    custom_css: str,
    homeserver_url: str,
    proxy_base_url: str = "",
) -> str:
    has_maps = _needs_leaflet(messages)
    head = _page_head(room_name, custom_css, extra_head=_LEAFLET_HEAD_ASSETS if has_maps else "")
    msgs_html = "\n".join(render_message_html(m, homeserver_url, proxy_base_url) for m in messages)
    topic_p = f"  <p>{escape(room_topic)}</p>" if room_topic else ""
    sse = _sse_chat_script(encoded_alias)
    leaflet_init = f"\n{_LEAFLET_INIT_SCRIPT}" if has_maps else ""
    scroll_script = _scroll_header_script()
    return (
        f'{head}\n<body>\n'
        f'<header class="webpublish-header">\n'
        f'  <h1>{escape(room_name)}</h1>\n{topic_p}\n'
        f'</header>\n'
        f'<main class="webpublish-chat">\n'
        f'  <div class="webpublish-messages" id="messages">\n{msgs_html}\n  </div>\n'
        f'</main>\n{_LOCALIZE_TIMESTAMPS_SCRIPT}{leaflet_init}\n{sse}\n{scroll_script}\n</body>\n</html>'
    )


def render_journal_landing(
    room_name: str,
    room_topic: str,
    posts: list[dict],
    encoded_alias: str,
    page: int,
    total_pages: int,
    custom_css: str,
    comment_counts: dict[str, int],
) -> str:
    head = _page_head(room_name, custom_css)
    topic_p = f"  <p>{escape(room_topic)}</p>" if room_topic else ""

    posts_parts = []
    for post in posts:
        count = comment_counts.get(post["event_id"], 0)
        posts_parts.append(render_post_preview_html(post, encoded_alias, count))
    posts_html = "\n".join(posts_parts)

    pag_parts: list[str] = []
    if total_pages > 1:
        for p in range(1, total_pages + 1):
            if p == page:
                pag_parts.append(f'<span class="active">{p}</span>')
            else:
                pag_parts.append(f'<a href="{encoded_alias}?page={p}">{p}</a>')
    pag_html = (
        f'<nav class="webpublish-pagination">{"".join(pag_parts)}</nav>'
        if pag_parts else ""
    )

    sse = _sse_journal_landing_script(encoded_alias)
    scroll_script = _scroll_header_script()
    return (
        f'{head}\n<body>\n'
        f'<header class="webpublish-header">\n'
        f'  <h1>{escape(room_name)}</h1>\n{topic_p}\n'
        f'</header>\n'
        f'<main class="webpublish-journal">\n'
        f'  <div class="webpublish-posts">\n{posts_html}\n  </div>\n'
        f'  {pag_html}\n'
        f'</main>\n{_LOCALIZE_TIMESTAMPS_SCRIPT}\n{sse}\n{scroll_script}\n</body>\n</html>'
    )


def render_journal_post(
    room_name: str,
    post: dict,
    comments: list[dict],
    encoded_alias: str,
    custom_css: str,
    homeserver_url: str,
    proxy_base_url: str = "",
) -> str:
    title = (post.get("body") or "").split("\n", 1)[0][:80]
    has_maps = _needs_leaflet([post] + comments)
    head = _page_head(f"{title} - {room_name}", custom_css, extra_head=_LEAFLET_HEAD_ASSETS if has_maps else "")
    body_html = render_body(post, homeserver_url, proxy_base_url)
    author = escape(post.get("sender_name") or post["sender"])
    date = format_date(post["timestamp"])
    edited = " (edited)" if post.get("edited") else ""

    comments_parts = [
        render_message_html(
            c, homeserver_url, proxy_base_url,
            show_reply_header=bool(c.get("reply_to")),
        )
        for c in comments
    ]
    comments_html = "\n".join(comments_parts)
    count = len(comments)
    label = f"{count} comment{'s' if count != 1 else ''}" if count else "No comments yet"

    matrix_link = f"https://matrix.to/#/{quote(post['room_id'])}/{quote(post['event_id'])}"
    sse = _sse_post_detail_script(post["event_id"])
    leaflet_init = f"\n{_LEAFLET_INIT_SCRIPT}" if has_maps else ""
    scroll_script = _scroll_header_script()
    back_href = "../" if not encoded_alias else f"../../{encoded_alias}"
    return (
        f'{head}\n<body>\n'
        f'<header class="webpublish-header">\n'
        f'  <h1>{escape(room_name)}</h1>\n'
        f'</header>\n'
        f'<main class="webpublish-post-full">\n'
        f'  <a class="webpublish-back-link" href="{back_href}">&larr; back to posts</a>\n'
        f'  <article>\n'
        f'    <div class="webpublish-post-meta">\n'
        f'      <span>{author}</span>\n'
        f'      <span>{date}{edited}</span>\n'
        f'    </div>\n'
        f'    <div class="webpublish-post-body">{body_html}</div>\n'
        f'  </article>\n'
        f'  <div class="webpublish-matrix-reply-link">\n'
        f'    <a href="{matrix_link}" target="_blank" rel="noopener noreferrer">Reply in Matrix</a>\n'
        f'  </div>\n'
        f'  <section class="webpublish-comments">\n'
        f'    <h2>{label}</h2>\n'
        f'    <div id="comments">\n{comments_html}\n    </div>\n'
        f'  </section>\n'
        f'</main>\n{_LOCALIZE_TIMESTAMPS_SCRIPT}{leaflet_init}\n{sse}\n{scroll_script}\n</body>\n</html>'
    )
