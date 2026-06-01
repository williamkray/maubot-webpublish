from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from urllib.parse import quote, urlparse


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
    "img": {"src", "alt", "width", "height", "title", "data-mx-emoticon"},
    "span": {"data-mx-color", "data-mx-bg-color"},
    "code": {"class"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}


class _Sanitizer(HTMLParser):
    """Strip all HTML tags/attributes not in the allowlist."""

    def __init__(self, homeserver_url: str = "", proxy_base_url: str = "") -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0
        self._homeserver_url = homeserver_url
        self._proxy_base_url = proxy_base_url

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
        is_custom_emoji = False
        for name, value in attrs:
            name = name.lower()
            if name not in allowed:
                continue
            if name.startswith("on"):
                continue
            val = value or ""
            if "javascript:" in val.lower() or "vbscript:" in val.lower():
                continue
            if tag == "img" and name == "src" and val.startswith("mxc://"):
                val = mxc_to_http(val, self._homeserver_url, self._proxy_base_url)
            if tag == "img" and name == "data-mx-emoticon":
                is_custom_emoji = True
            safe_attrs.append(f' {escape(name)}="{escape(val)}"')
        if tag == "img" and is_custom_emoji:
            safe_attrs.append(' class="webpublish-custom-emoji"')
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


def sanitize_html(html_str: str, homeserver_url: str = "", proxy_base_url: str = "") -> str:
    s = _Sanitizer(homeserver_url=homeserver_url, proxy_base_url=proxy_base_url)
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

# Matrix room aliases (#localpart:server.tld). Localparts are very permissive
# per the spec (any non-surrogate Unicode except `:` and NUL), so we strip the
# whole alias before hashtag extraction rather than enumerating allowed chars
# in the hashtag regex's lookahead.
_MATRIX_ALIAS_RE = re.compile(r'#[^\s:#]+:[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')

_HASHTAG_RE = re.compile(r'(?<![&\w])#([a-zA-Z][a-zA-Z0-9_-]{0,49})(?![:\w-])')


def parse_hashtags(body: str) -> list[str]:
    body = _MATRIX_ALIAS_RE.sub("", body)
    return sorted({t.lower() for t in _HASHTAG_RE.findall(body)})


# Auto-linkify patterns for plaintext message bodies and topics. Order of
# alternation matters: the Matrix-identifier branch must precede the bare-email
# branch so `@user:server.tld` matches as a MXID, not an email.
_LINKIFY_RE = re.compile(
    r"""(?P<url>https?://[^\s<>]+)
        |(?P<matrix>matrix:[^\s<>]+)
        |(?P<mailto>mailto:[^\s<>]+@[^\s<>]+)
        |(?P<mxid>[#@!][^\s:]+:[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?::\d+)?)
        |(?P<email>[\w.+\-]+@[\w\-]+\.[\w.\-]+)
    """,
    re.VERBOSE,
)
_MXID_SIGIL_TO_MATRIX_PREFIX = {"#": "r/", "@": "u/", "!": "roomid/"}


def _trim_trailing_punct(s: str) -> tuple[str, str]:
    """Strip trailing punctuation unlikely to be part of a URL.

    Returns (url, trailing). Always strips `.,;:!?`. Strips an unbalanced
    closing bracket (`)]}>`) — e.g. the outer `)` in
    `(https://en.wikipedia.org/wiki/Foo_(bar))` — while leaving matched
    brackets intact. A trailing matched quote (`"`, `'`) is also stripped when
    there's an odd count in the URL."""
    bracket_pairs = {")": "(", "]": "[", "}": "{", ">": "<"}
    quote_chars = ('"', "'")
    trailing = ""
    while s:
        c = s[-1]
        if c in ".,;:!?":
            trailing = c + trailing
            s = s[:-1]
        elif c in bracket_pairs:
            opener = bracket_pairs[c]
            if s[:-1].count(opener) < s[:-1].count(c) + 1:
                trailing = c + trailing
                s = s[:-1]
            else:
                break
        elif c in quote_chars and s[:-1].count(c) % 2 == 0:
            trailing = c + trailing
            s = s[:-1]
        else:
            break
    return s, trailing


def linkify_plaintext(text: str, newlines_to_br: bool = True) -> str:
    """Escape plaintext and wrap recognizable URLs, Matrix URIs, mailto/email,
    and bare Matrix identifiers (#room:server, @user:server, !id:server) in
    anchor tags. For use on render paths that don't have a formatted_body."""
    if not text:
        return ""
    out: list[str] = []
    pos = 0
    for m in _LINKIFY_RE.finditer(text):
        start, end = m.span()
        if start > pos:
            chunk = escape(text[pos:start])
            out.append(chunk.replace("\n", "<br>") if newlines_to_br else chunk)
        match = m.group(0)
        match, trailing = _trim_trailing_punct(match)
        if not match:
            # Entire match was trimmed away (edge case); emit as plain text.
            out.append(escape(m.group(0)))
            pos = end
            continue
        if m.lastgroup == "email":
            href = f"mailto:{match}"
        elif m.lastgroup == "mxid":
            sigil, rest = match[0], match[1:]
            href = f"matrix:{_MXID_SIGIL_TO_MATRIX_PREFIX[sigil]}{rest}"
        else:
            href = match
        out.append(
            f'<a href="{escape(href)}" target="_blank" rel="noopener nofollow">'
            f'{escape(match)}</a>'
        )
        if trailing:
            out.append(escape(trailing))
        pos = end
    if pos < len(text):
        chunk = escape(text[pos:])
        out.append(chunk.replace("\n", "<br>") if newlines_to_br else chunk)
    return "".join(out)


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


def matrix_event_uri(room_id: str, event_id: str, homeserver_url: str = "") -> str:
    """Build a native matrix: URI for an event in a room (MSC2312)."""
    rid = room_id.lstrip("!")
    eid = event_id.lstrip("$")
    uri = f"matrix:roomid/{quote(rid, safe=':')}/e/{quote(eid, safe=':')}"
    if homeserver_url:
        host = urlparse(homeserver_url).hostname
        if host:
            uri += f"?via={quote(host)}"
    return uri


def matrix_room_uri(room_id: str, alias: str = "", homeserver_url: str = "") -> str:
    """Build a matrix: URI for a room, preferring the `matrix:r/<alias>` form
    (widely supported by clients) when an alias is known. Falls back to
    `matrix:roomid/<id>?via=<bot_homeserver>`. The via is taken from the bot's
    homeserver since the bot is (or was) in the room \u2014 that server can serve
    federation for it. Works the same for v12 rooms whose ids have no server
    part; don't try to derive a via from the room id itself.
    """
    if alias:
        a = alias.lstrip("#")
        if a:
            return f"matrix:r/{quote(a, safe=':')}"
    rid = room_id.lstrip("!")
    if not rid:
        return ""
    uri = f"matrix:roomid/{quote(rid, safe=':')}"
    if homeserver_url:
        host = urlparse(homeserver_url).hostname
        if host:
            uri += f"?via={quote(host)}"
    return uri


# ---------------------------------------------------------------------------
# Message body rendering
# ---------------------------------------------------------------------------

def render_body(msg: dict, homeserver_url: str, proxy_base_url: str = "", journal: bool = False) -> str:
    if msg.get("redacted"):
        return '<em class="webpublish-redacted">this message was deleted</em>'

    msgtype = msg.get("msgtype", "m.text")

    if msgtype == "m.sticker":
        url = mxc_to_http(msg.get("media_url", ""), homeserver_url, proxy_base_url)
        alt = escape(msg.get("body", "") or "sticker")
        if url:
            return (
                f'<img class="webpublish-sticker" src="{escape(url)}"'
                f' alt="{alt}" loading="lazy">'
            )
        return f"[sticker: {alt}]"

    if msgtype == "m.image":
        url = mxc_to_http(msg.get("media_url", ""), homeserver_url, proxy_base_url)
        body_text = msg.get("body", "")
        alt = escape(body_text or "image")
        is_filename = bool(re.fullmatch(r'[^\s]+\.[a-zA-Z0-9]{2,5}', body_text or ""))
        if url:
            img = f'<img class="webpublish-media" src="{escape(url)}" alt="{alt}" loading="lazy">'
            linked = f'<a href="{escape(url)}" target="_blank" rel="noopener">{img}</a>'
            if journal:
                # Journal posts: image displayed full-width, body rendered as prose below
                formatted = msg.get("formatted_body")
                if formatted:
                    text_block = f'<div class="webpublish-image-body">{sanitize_html(formatted, homeserver_url, proxy_base_url)}</div>'
                elif not is_filename:
                    text_block = f'<div class="webpublish-image-body">{linkify_plaintext(body_text)}</div>'
                else:
                    text_block = ""
                figure = f'<figure class="webpublish-figure webpublish-figure-full">{linked}</figure>'
                return figure + ("\n" + text_block if text_block else "")
            else:
                caption = "" if is_filename else escape(body_text)
                if caption:
                    return f'<figure class="webpublish-figure">{linked}<figcaption>{caption}</figcaption></figure>'
                return linked
        return f"[image: {alt}]"

    if msgtype == "m.video":
        url = mxc_to_http(msg.get("media_url", ""), homeserver_url, proxy_base_url)
        body_text = msg.get("body", "")
        alt = escape(body_text or "video")
        is_filename = bool(re.fullmatch(r'[^\s]+\.[a-zA-Z0-9]{2,5}', body_text or ""))
        if url:
            video = (
                f'<video class="webpublish-media" src="{escape(url)}"'
                f' controls preload="metadata"></video>'
            )
            if journal:
                formatted = msg.get("formatted_body")
                if formatted:
                    text_block = f'<div class="webpublish-image-body">{sanitize_html(formatted, homeserver_url, proxy_base_url)}</div>'
                elif not is_filename:
                    text_block = f'<div class="webpublish-image-body">{linkify_plaintext(body_text)}</div>'
                else:
                    text_block = ""
                figure = f'<figure class="webpublish-figure webpublish-figure-full">{video}</figure>'
                return figure + ("\n" + text_block if text_block else "")
            caption = "" if is_filename else escape(body_text)
            if caption:
                return f'<figure class="webpublish-figure">{video}<figcaption>{caption}</figcaption></figure>'
            return video
        return f"[video: {alt}]"

    if msgtype == "m.audio":
        url = mxc_to_http(msg.get("media_url", ""), homeserver_url, proxy_base_url)
        body_text = msg.get("body", "")
        alt = escape(body_text or "audio")
        is_filename = bool(re.fullmatch(r'[^\s]+\.[a-zA-Z0-9]{2,5}', body_text or ""))
        if url:
            audio = (
                f'<audio class="webpublish-media" src="{escape(url)}"'
                f' controls preload="metadata"></audio>'
            )
            caption = "" if is_filename else escape(body_text)
            if caption:
                return f'<figure class="webpublish-figure">{audio}<figcaption>{caption}</figcaption></figure>'
            return audio
        return f"[audio: {alt}]"

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
        body = linkify_plaintext(msg.get("body", ""), newlines_to_br=False)
        return f"<em>* {name} {body}</em>"

    # m.text, m.notice, fallback
    formatted = msg.get("formatted_body")
    if formatted:
        return sanitize_html(formatted, homeserver_url, proxy_base_url)
    return linkify_plaintext(msg.get("body", ""))


# Unicode emoji ranges for "big emoji" detection. Intentionally broad but
# cheap; we only use it to decide whether to enlarge the body, so false
# positives merely enlarge; they don't mangle content.
_EMOJI_ONLY_RE = re.compile(
    "^(?:"
    r"[\U0001F000-\U0001FFFF]"       # supplementary pictographs
    r"|[\u2600-\u27BF]"              # misc symbols, dingbats
    r"|[\uFE00-\uFE0F]"              # variation selectors
    r"|[\u200D\u20E3]"               # ZWJ, keycap
    r"|[\U000E0020-\U000E007F]"      # tag sequences
    r"|\s"                            # whitespace between emoji is fine
    ")+$"
)
_CUSTOM_EMOJI_TAG_RE = re.compile(
    r'<img\b[^>]*\bclass="[^"]*webpublish-custom-emoji[^"]*"[^>]*>', re.IGNORECASE,
)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_EMOJI_CLUSTER_RE = re.compile(
    r"(?:"
    r"[\U0001F000-\U0001FFFF]"
    r"|[\u2600-\u27BF]"
    r"|[\U0001F300-\U0001F9FF]"
    r")[\uFE0F\u200D\u20E3\U000E0020-\U000E007F]*"
)


def _is_emoji_only(body_html: str, max_count: int = 6) -> bool:
    """True if the rendered HTML body contains only emoji (unicode + custom)
    with no other text — up to `max_count` emoji units."""
    if not body_html:
        return False
    custom_count = len(_CUSTOM_EMOJI_TAG_RE.findall(body_html))
    stripped = _CUSTOM_EMOJI_TAG_RE.sub("", body_html)
    # Strip any remaining HTML tags (e.g. <p>, <br>).
    stripped = _ANY_TAG_RE.sub("", stripped)
    stripped = stripped.replace("&nbsp;", " ").strip()
    if not stripped:
        return 0 < custom_count <= max_count
    if not _EMOJI_ONLY_RE.match(stripped):
        return False
    unicode_count = len(_EMOJI_CLUSTER_RE.findall(stripped))
    total = custom_count + unicode_count
    return 0 < total <= max_count


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


def render_message_html(
    msg: dict,
    homeserver_url: str,
    proxy_base_url: str = "",
    show_reply_header: bool = True,
    thread_indicator_html: str = "",
) -> str:
    eid = safe_element_id(msg["event_id"])
    color = sender_color(msg["sender"])
    initials = sender_initials(msg.get("sender_name") or msg["sender"])
    name = escape(msg.get("sender_name") or msg["sender"])
    time_str = format_time(msg["timestamp"])
    iso = format_iso(msg["timestamp"])
    body_html = render_body(msg, homeserver_url, proxy_base_url)
    edited = ' <span class="webpublish-edited">(edited)</span>' if msg.get("edited") else ""
    notice_cls = " webpublish-notice" if msg.get("msgtype") == "m.notice" else ""
    msgtype = msg.get("msgtype", "m.text")
    body_cls = "webpublish-body"
    if msgtype in ("m.text", "m.notice") and _is_emoji_only(body_html):
        body_cls += " webpublish-emoji-only"
    reply_html = _render_reply_header(msg) if show_reply_header else ""
    raw_avatar = msg.get("avatar_url") or ""
    if raw_avatar:
        avatar_http = escape(mxc_to_http(raw_avatar, homeserver_url, proxy_base_url))
        avatar_img = f'<img class="webpublish-avatar-img" src="{avatar_http}" alt="" onerror="this.style.display=\'none\'">'
    else:
        avatar_img = ""
    indicator = f'    {thread_indicator_html}\n' if thread_indicator_html else ""
    reactions_html = render_reactions_html(
        msg.get("reactions") or [], homeserver_url, proxy_base_url,
    )
    reactions_section = f'    {reactions_html}\n' if reactions_html else ""

    return (
        f'<div class="webpublish-message{notice_cls}" id="{eid}" data-ts="{msg["timestamp"]}">\n'
        f'  <div class="webpublish-avatar" style="background-color:{color}">{initials}{avatar_img}</div>\n'
        f'  <div class="webpublish-message-content">\n'
        f'    {reply_html}'
        f'    <div class="webpublish-message-header">\n'
        f'      <span class="webpublish-sender" style="color:{color}">{name}</span>\n'
        f'      <time class="webpublish-timestamp" datetime="{iso}">{time_str}</time>{edited}\n'
        f'    </div>\n'
        f'    <div class="{body_cls}">{body_html}</div>\n'
        f'{reactions_section}'
        f'{indicator}'
        f'  </div>\n'
        f'</div>'
    )


def _render_mini_avatar(participant: dict, homeserver_url: str, proxy_base_url: str) -> str:
    sender = participant.get("sender", "")
    name = participant.get("sender_name") or sender
    color = sender_color(sender)
    initials = sender_initials(name)
    raw_avatar = participant.get("avatar_url") or ""
    if raw_avatar:
        http = escape(mxc_to_http(raw_avatar, homeserver_url, proxy_base_url))
        img = (
            f'<img class="webpublish-avatar-img" src="{http}" alt=""'
            f' onerror="this.style.display=\'none\'">'
        )
    else:
        img = ""
    title = escape(name)
    return (
        f'<span class="webpublish-thread-avatar" style="background-color:{color}"'
        f' title="{title}">{initials}{img}</span>'
    )


def render_reactions_html(
    reactions: list[dict],
    homeserver_url: str = "",
    proxy_base_url: str = "",
) -> str:
    if not reactions:
        return ""
    pills: list[str] = []
    for r in reactions:
        senders = r.get("senders") or []
        title = escape(", ".join(senders))
        raw_key = r.get("key", "") or ""
        count = int(r.get("count", 0))
        if raw_key.startswith("mxc://"):
            src = mxc_to_http(raw_key, homeserver_url, proxy_base_url)
            key_html = (
                f'<img class="webpublish-custom-emoji" src="{escape(src)}" alt="">'
            )
        else:
            key_html = escape(raw_key)
        pills.append(
            f'<span class="webpublish-reaction" title="{title}">'
            f'<span class="webpublish-reaction-key">{key_html}</span>'
            f'<span class="webpublish-reaction-count">{count}</span>'
            f'</span>'
        )
    return f'<div class="webpublish-reactions">{"".join(pills)}</div>'


def render_thread_indicator_html(
    root_event_id: str,
    count: int,
    participants: list[dict],
    homeserver_url: str,
    proxy_base_url: str = "",
) -> str:
    if count <= 0:
        return ""
    avatars = "".join(
        _render_mini_avatar(p, homeserver_url, proxy_base_url)
        for p in participants[:5]
    )
    more = (
        '<span class="webpublish-thread-more" aria-hidden="true">+</span>'
        if len(participants) > 5 else ""
    )
    label = f"{count} repl{'y' if count == 1 else 'ies'}"
    return (
        f'<button type="button" class="webpublish-thread-indicator"'
        f' data-root="{escape(root_event_id)}"'
        f' aria-label="Open thread with {label}">'
        f'<span class="webpublish-thread-avatars">{avatars}{more}</span>'
        f'<span class="webpublish-thread-count">{label}</span>'
        f'</button>'
    )


def render_tag_chips(tags: list[str], encoded_alias: str, base_url: str = "") -> str:
    if not tags:
        return ""
    chips = []
    for tag in tags:
        tag_path = f"tag/{quote(tag, safe='')}"
        if base_url:
            url = f"{base_url}/{encoded_alias}/{tag_path}" if encoded_alias else f"{base_url}/{tag_path}"
        elif encoded_alias:
            url = f"./{encoded_alias}/{tag_path}"
        else:
            url = f"./{tag_path}"
        chips.append(f'<a class="webpublish-tag-chip" href="{url}">#{escape(tag)}</a>')
    return f'<div class="webpublish-tags">{"".join(chips)}</div>'


def render_post_preview_html(
    post: dict, alias: str, comment_count: int, base_url: str = "",
    pinned: bool = False,
) -> str:
    eid = safe_element_id(post["event_id"])
    title_line = (post.get("body") or "").split("\n", 1)[0][:120]
    author = escape(post.get("sender_name") or post["sender"])
    date = format_date(post["timestamp"])
    if comment_count:
        comments_text = f"{comment_count} comment{'s' if comment_count != 1 else ''}"
    else:
        comments_text = "no comments"
    eid_enc = quote(post["event_id"], safe="")
    if base_url:
        post_url = f"{base_url}/{alias}/post/{eid_enc}" if alias else f"{base_url}/post/{eid_enc}"
    else:
        post_url = f"./post/{eid_enc}" if not alias else f"./{alias}/post/{eid_enc}"
    tags_html = render_tag_chips(post.get("tags", []), alias, base_url)
    tags_section = f'\n  {tags_html}' if tags_html else ""
    pin_badge = '<span class="webpublish-pin-badge" aria-label="Pinned">📌</span> ' if pinned else ""
    article_cls = "webpublish-post-preview"
    if pinned:
        article_cls += " webpublish-post-pinned"

    return (
        f'<article class="{article_cls}" id="{eid}">\n'
        f'  <h2 class="webpublish-post-title">{pin_badge}'
        f'<a href="{post_url}">{escape(title_line) or "<em>untitled</em>"}</a></h2>\n'
        f'  <div class="webpublish-post-meta">\n'
        f'    <span>{author}</span>\n'
        f'    <span>{date}</span>\n'
        f'    <span>{comments_text}</span>\n'
        f'  </div>\n'
        f'{tags_section}\n'
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
  min-height: 100dvh;
  display: flex; flex-direction: column;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* header */
.webpublish-header {
  position: sticky; top: 0; z-index: 100;
  padding: 24px 32px; border-bottom: 1px solid var(--border); background: var(--bg-secondary);
  transition: padding 0.25s ease;
}
.webpublish-header-title { display: flex; align-items: center; gap: 0.5em; }
.webpublish-header h1 { font-size: 1.5rem; font-weight: 600; }
.webpublish-room-avatar { height: 2em; width: 2em; border-radius: 50%; object-fit: cover; flex-shrink: 0; }
.webpublish-header p  {
  color: var(--text-muted); margin-top: 4px;
  max-height: 6rem; overflow: hidden;
  transition: max-height 0.25s ease, opacity 0.25s ease, margin-top 0.25s ease;
}
.webpublish-topic-toggle {
  display: none;
  background: none; border: 0; padding: 0;
  color: var(--accent); cursor: pointer; font: inherit;
  margin-top: 4px; text-decoration: underline;
}
@media (max-width: 600px) {
  .webpublish-header.scrolled:not(.expanded) { padding: 10px 32px; cursor: pointer; }
  .webpublish-header.scrolled:not(.expanded) p { max-height: 0; opacity: 0; margin-top: 0; }
  .webpublish-header.expanded p { max-height: 50vh; overflow-y: auto; }
  .webpublish-topic-toggle:not([hidden]) { display: inline-block; }
  .webpublish-header.scrolled:not(.expanded) .webpublish-topic-toggle { display: none; }
}

/* ---- chat mode ---- */
body.webpublish-chat-mode { height: 100dvh; overflow: hidden; display: flex; flex-direction: column; }
body.webpublish-chat-mode .webpublish-header { flex: 0 0 auto; }
.webpublish-chat { display: flex; flex-direction: column; flex: 1 1 0; min-height: 0; }
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
.webpublish-body video.webpublish-media {
  max-width: 100%; max-height: 60vh; width: auto; height: auto;
  display: block; border-radius: 8px; margin: 4px 0;
}
.webpublish-body audio.webpublish-media {
  max-width: 100%; display: block; margin: 4px 0;
}
img.webpublish-custom-emoji {
  height: 1.4em; width: auto; vertical-align: middle; display: inline-block;
}
.webpublish-body img.webpublish-sticker {
  max-width: 160px; max-height: 160px; width: auto; height: auto;
  display: block; margin: 4px 0;
}
.webpublish-body.webpublish-emoji-only { font-size: 2.2em; line-height: 1.2; }
.webpublish-body.webpublish-emoji-only img.webpublish-custom-emoji { height: 1em; }
.webpublish-pinned-banner {
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: 8px; margin: 8px 0; padding: 4px 12px;
}
.webpublish-pinned-toggle {
  background: none; border: 0; cursor: pointer; color: var(--text);
  padding: 4px 0; font: inherit;
}
.webpublish-header-actions {
  display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; align-items: center;
}
.webpublish-header-actions:not(:has(> :not([hidden]))) { display: none; }
body.webpublish-chat-mode .webpublish-pinned-banner {
  display: contents;
}
body.webpublish-chat-mode .webpublish-pinned-toggle,
.webpublish-jump-latest {
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: 8px; padding: 4px 12px; cursor: pointer;
  color: var(--text); font: inherit;
}
body.webpublish-chat-mode .webpublish-pinned-list {
  order: 2; flex: 0 0 100%;
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 12px 8px 32px; margin: 0;
}
.webpublish-jump-latest[hidden] { display: none; }
.webpublish-pinned-list { margin: 4px 0 8px 0; padding-left: 20px; }
.webpublish-pinned-list li { margin: 2px 0; }
.webpublish-pinned-list a { color: var(--accent); text-decoration: none; }
.webpublish-pinned-list a:hover { text-decoration: underline; }
.webpublish-pin-badge { opacity: 0.75; }
.webpublish-pinned-posts {
  border-bottom: 2px solid var(--border);
  padding-bottom: 8px; margin-top: 24px; margin-bottom: 16px;
}
.webpublish-post-pinned { background: var(--bg-secondary); border-radius: 8px; padding: 4px 8px; }
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
  border-left: 3px solid var(--accent); padding-left: 12px; color: var(--accent); margin: 1em 0;
}
.webpublish-body h1 { margin: 1.34em 0; }
.webpublish-body h2 { margin: 1.66em 0; }
.webpublish-body h3 { margin: 2em 0; }
.webpublish-body h4 { margin: 2.66em 0; }
.webpublish-body h5 { margin: 3.34em 0; }
.webpublish-body h6 { margin: 4.66em 0; }
.webpublish-notice  { opacity: 0.7; }
.webpublish-redacted { color: var(--text-muted); }

/* ---- journal mode ---- */
.webpublish-journal { max-width: 800px; margin: 0 auto; padding: 0 24px; width: 100%; flex: 1 0 auto; }
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

/* tags */
.webpublish-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.webpublish-tag-chip {
  display: inline-block; padding: 2px 10px; border-radius: 12px;
  background: var(--bg-secondary); border: 1px solid var(--border);
  font-size: 0.8rem; color: var(--text-muted); text-decoration: none;
}
.webpublish-post-preview .webpublish-tag-chip { background: var(--bg); }
.webpublish-tag-chip:hover { border-color: var(--accent); color: var(--accent); text-decoration: none; }
.webpublish-tag-list { list-style: none; display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px; }
.webpublish-tag-header { font-size: 1.3rem; font-weight: 600; margin: 16px 0 4px; }
.webpublish-text-muted { color: var(--text-muted); }

/* journal post detail */
.webpublish-post-full { max-width: 800px; margin: 0 auto; padding: 24px; width: 100%; flex: 1 0 auto; }
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
  border-left: 3px solid var(--accent); padding-left: 16px; color: var(--accent); margin: 1em 0;
}
.webpublish-post-full .webpublish-post-body h1 { margin: 1.34em 0; }
.webpublish-post-full .webpublish-post-body h2 { margin: 1.66em 0; }
.webpublish-post-full .webpublish-post-body h3 { margin: 2em 0; }
.webpublish-post-full .webpublish-post-body h4 { margin: 2.66em 0; }
.webpublish-post-full .webpublish-post-body h5 { margin: 3.34em 0; }
.webpublish-post-full .webpublish-post-body h6 { margin: 4.66em 0; }
/* full-width image figures in journal post detail */
.webpublish-figure-full { display: block; margin: 0 0 4px; }
.webpublish-figure-full img.webpublish-media { max-width: 100%; max-height: none; border-radius: 8px; }
.webpublish-figure-full video.webpublish-media {
  max-width: 100%; max-height: 60vh; width: auto; height: auto;
  display: block; margin: 0 auto; border-radius: 8px;
}
.webpublish-image-body { margin-top: 12px; line-height: 1.7; }

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
.webpublish-feed-footer {
  text-align: center; padding: 16px 0 32px; font-size: 0.85rem; color: var(--text-muted);
}
.webpublish-feed-footer a { color: var(--text-muted); }
.webpublish-feed-footer a:hover { color: var(--accent); }
.webpublish-map { position: relative; border-radius: 8px; overflow: hidden; }

/* ---- thread indicator & side panel (chat mode) ---- */
.webpublish-thread-indicator {
  display: inline-flex; align-items: center; gap: 8px;
  margin-top: 6px; padding: 4px 10px 4px 4px;
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: 16px; cursor: pointer; font: inherit; color: var(--accent);
}
.webpublish-thread-indicator:hover { border-color: var(--accent); }
.webpublish-thread-avatars { display: inline-flex; align-items: center; }
.webpublish-thread-avatar {
  width: 22px; height: 22px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 0.65rem; font-weight: 600; color: #fff;
  position: relative; overflow: hidden;
  border: 2px solid var(--bg-secondary); box-sizing: content-box;
}
.webpublish-thread-avatar + .webpublish-thread-avatar { margin-left: -8px; }
.webpublish-thread-more {
  margin-left: -4px; padding: 0 6px; font-size: 0.75rem;
  color: var(--text-muted); align-self: center;
}
.webpublish-thread-count { font-size: 0.8rem; font-weight: 500; }

/* ---- reactions ---- */
.webpublish-reactions {
  display: flex; flex-wrap: wrap; gap: 4px;
  margin-top: 6px;
}
.webpublish-reaction {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px;
  background: var(--bg-secondary); border: 1px solid var(--border);
  border-radius: 12px; font-size: 0.8rem; line-height: 1.4;
  color: var(--text);
}
.webpublish-reaction-key { font-size: 0.95rem; line-height: 1; }
.webpublish-reaction-count { color: var(--text-muted); font-variant-numeric: tabular-nums; }

.webpublish-thread-panel {
  position: fixed; top: 0; right: 0; bottom: 0;
  width: min(420px, 40vw);
  background: var(--bg); border-left: 1px solid var(--border);
  box-shadow: -4px 0 16px rgba(0,0,0,0.18);
  transform: translateX(100%); transition: transform 0.2s ease;
  z-index: 200; display: flex; flex-direction: column;
}
.webpublish-thread-panel[hidden] { display: none; }
.webpublish-thread-panel.open { transform: none; }
.webpublish-thread-panel-inner { display: flex; flex-direction: column; height: 100%; }
.webpublish-thread-panel-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; border-bottom: 1px solid var(--border);
  background: var(--bg-secondary); flex-shrink: 0;
}
.webpublish-thread-panel-title { font-weight: 600; font-size: 0.95rem; }
.webpublish-thread-panel-close {
  background: none; border: 0; color: var(--text); cursor: pointer;
  font-size: 1.4rem; line-height: 1; padding: 0 4px;
}
.webpublish-thread-panel-close:hover { color: var(--accent); }
.webpublish-thread-panel-body {
  flex: 1; overflow-y: auto; padding: 12px 16px;
}
.webpublish-thread-panel-body .webpublish-message { padding: 8px 0; }
body.has-thread-panel .webpublish-chat .webpublish-messages {
  padding-right: calc(min(420px, 40vw) + 32px);
}
@media (max-width: 600px) {
  .webpublish-thread-panel { width: 100%; left: 0; }
  body.has-thread-panel .webpublish-chat .webpublish-messages {
    padding-right: 32px;
  }
}
.webpublish-succession-banner {
  display: flex; justify-content: space-between; align-items: baseline;
  gap: 12px; flex-wrap: wrap;
  padding: 6px 0; font-size: 0.85rem; color: var(--text-muted);
}
.webpublish-succession-left, .webpublish-succession-right {
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
}
.webpublish-succession-right { margin-left: auto; }
.webpublish-succession-link { color: var(--text-muted); text-decoration: none; }
.webpublish-succession-link:hover { color: var(--accent); }
.webpublish-succession-archived { font-style: italic; }
"""


# ---------------------------------------------------------------------------
# Open Graph helpers
# ---------------------------------------------------------------------------

def _og_meta_site(room_name: str, room_topic: str, encoded_alias: str, base_url: str, room_avatar_url: str = "", proxy_base_url: str = "") -> str:
    url = f"{base_url}/" if not encoded_alias else f"{base_url}/{encoded_alias}"
    desc = escape((room_topic or room_name)[:200])
    lines = [
        f'<meta property="og:type" content="website">',
        f'<meta property="og:title" content="{escape(room_name)}">',
        f'<meta property="og:description" content="{desc}">',
        f'<meta property="og:url" content="{escape(url)}">',
    ]
    if room_avatar_url and proxy_base_url:
        avatar_http = mxc_to_http(room_avatar_url, "", proxy_base_url)
        if avatar_http:
            lines.append(f'<meta property="og:image" content="{escape(avatar_http)}">')
    return "\n".join(lines)


def _og_meta_post(post: dict, room_name: str, encoded_alias: str, base_url: str, homeserver_url: str, room_avatar_url: str = "") -> str:
    title = escape((post.get("body") or "").split("\n", 1)[0][:80] or "Untitled")
    desc = escape((post.get("body") or "")[:200].replace("\n", " "))
    eid_enc = quote(post["event_id"], safe="")
    url = f"{base_url}/post/{eid_enc}" if not encoded_alias else f"{base_url}/{encoded_alias}/post/{eid_enc}"
    iso = format_iso(post["timestamp"])
    author = escape(post.get("sender_name") or post.get("sender", ""))
    lines = [
        f'<meta property="og:type" content="article">',
        f'<meta property="og:title" content="{title}">',
        f'<meta property="og:description" content="{desc}">',
        f'<meta property="og:url" content="{escape(url)}">',
        f'<meta property="article:published_time" content="{iso}">',
        f'<meta property="article:author" content="{author}">',
    ]
    if post.get("msgtype") == "m.image" and post.get("media_url"):
        img_url = mxc_to_http(post["media_url"], homeserver_url, base_url)
        if img_url:
            lines.append(f'<meta property="og:image" content="{escape(img_url)}">')
    elif room_avatar_url:
        avatar_http = mxc_to_http(room_avatar_url, homeserver_url, base_url)
        if avatar_http:
            lines.append(f'<meta property="og:image" content="{escape(avatar_http)}">')
    return "\n".join(lines)


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


def _page_head(title: str, custom_css: str, extra_head: str = "", og_meta: str = "") -> str:
    import_lines, override_lines = [], []
    for line in (custom_css or "").splitlines():
        (import_lines if line.strip().startswith("@import") else override_lines).append(line)
    css_imports = "\n".join(import_lines)
    css_overrides = "\n".join(override_lines)
    user_css = f"{css_imports}\n{css_overrides}".strip()
    user_style = f"<style>\n{user_css}\n</style>\n" if user_css else ""
    leaflet = f"{extra_head}\n" if extra_head else ""
    og = f"{og_meta}\n" if og_meta else ""
    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{escape(title)}</title>\n'
        f'{og}'
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
    sse_url = "./sse" if not encoded_alias else f"./{encoded_alias}/sse"
    return (
        '<script>\n'
        '(function() {\n'
        '  var msgs = document.getElementById("messages");\n'
        '  var panel = document.getElementById("thread-panel");\n'
        '  function isNearBottom() {\n'
        '    return msgs.scrollHeight - msgs.clientHeight <= msgs.scrollTop + 80;\n'
        '  }\n'
        '  function scrollBottom() { msgs.scrollTop = msgs.scrollHeight; }\n'
        '  function panelInnerRoot() {\n'
        '    if (!panel || panel.hidden) return null;\n'
        '    var inner = panel.querySelector(".webpublish-thread-panel-inner");\n'
        '    return inner && inner.dataset.root ? inner : null;\n'
        '  }\n'
        '  function findInPanel(elementId) {\n'
        '    var inner = panelInnerRoot();\n'
        '    if (!inner) return null;\n'
        '    var body = inner.querySelector(".webpublish-thread-panel-body");\n'
        '    return body ? body.querySelector("#" + CSS.escape(elementId)) : null;\n'
        '  }\n'
        '  function applyEdit(el, d) {\n'
        '    var body = el.querySelector(".webpublish-body");\n'
        '    if (body) body.innerHTML = d.body_html;\n'
        '    var hdr = el.querySelector(".webpublish-message-header");\n'
        '    if (hdr && !hdr.querySelector(".webpublish-edited")) {\n'
        '      hdr.insertAdjacentHTML("beforeend",\n'
        '        \' <span class="webpublish-edited">(edited)</span>\');\n'
        '    }\n'
        '  }\n'
        '  function applyReactions(el, html) {\n'
        '    if (!el) return;\n'
        '    var content = el.querySelector(".webpublish-message-content") || el;\n'
        '    var existing = content.querySelector(".webpublish-reactions");\n'
        '    if (html) {\n'
        '      if (existing) { existing.outerHTML = html; return; }\n'
        '      var body = content.querySelector(".webpublish-body");\n'
        '      if (body) body.insertAdjacentHTML("afterend", html);\n'
        '      else content.insertAdjacentHTML("beforeend", html);\n'
        '    } else if (existing) {\n'
        '      existing.remove();\n'
        '    }\n'
        '  }\n'
        '  // Pin to bottom on load. A second scroll after window.load catches\n'
        '  // images/custom emoji that finish loading after the initial paint\n'
        '  // and would otherwise push the newest messages below the fold.\n'
        '  var initialBottom = true;\n'
        '  scrollBottom();\n'
        '  function maybeKeepBottom() {\n'
        '    if (initialBottom) scrollBottom();\n'
        '  }\n'
        '  window.addEventListener("load", maybeKeepBottom);\n'
        '  msgs.querySelectorAll("img").forEach(function(img) {\n'
        '    if (!img.complete) img.addEventListener("load", maybeKeepBottom, {once: true});\n'
        '  });\n'
        '  var jumpBtn = document.querySelector(".webpublish-jump-latest");\n'
        '  function updateJumpBtn() {\n'
        '    if (!jumpBtn) return;\n'
        '    jumpBtn.hidden = isNearBottom();\n'
        '  }\n'
        '  if (jumpBtn) {\n'
        '    jumpBtn.addEventListener("click", function() {\n'
        '      initialBottom = true;\n'
        '      msgs.scrollTo({top: msgs.scrollHeight, behavior: "smooth"});\n'
        '    });\n'
        '  }\n'
        '  updateJumpBtn();\n'
        '  // Once the user scrolls away from the bottom we stop forcing it.\n'
        '  msgs.addEventListener("scroll", function() {\n'
        '    if (!isNearBottom()) initialBottom = false;\n'
        '    updateJumpBtn();\n'
        '  }, {passive: true});\n'
        f'  var src = new EventSource("{sse_url}");\n'
        '  src.addEventListener("new_message", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var near = isNearBottom();\n'
        '    msgs.insertAdjacentHTML("beforeend", d.html);\n'
        '    if (window._wpLocalizeTimestamps) window._wpLocalizeTimestamps(msgs.lastElementChild);\n'
        '    if (window._wpInitMaps) window._wpInitMaps(msgs.lastElementChild);\n'
        '    if (near) scrollBottom();\n'
        '  });\n'
        '  src.addEventListener("thread_reply", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var root = document.getElementById(d.root_element_id);\n'
        '    if (root) {\n'
        '      var content = root.querySelector(".webpublish-message-content");\n'
        '      if (content) {\n'
        '        var existing = content.querySelector(".webpublish-thread-indicator");\n'
        '        if (existing) existing.outerHTML = d.indicator_html;\n'
        '        else content.insertAdjacentHTML("beforeend", d.indicator_html);\n'
        '      }\n'
        '    }\n'
        '    var inner = panelInnerRoot();\n'
        '    if (inner && inner.dataset.root === d.thread_root) {\n'
        '      var body = inner.querySelector(".webpublish-thread-panel-body");\n'
        '      if (body && !body.querySelector("#" + CSS.escape(d.reply_element_id))) {\n'
        '        var near = body.scrollHeight - body.clientHeight <= body.scrollTop + 80;\n'
        '        body.insertAdjacentHTML("beforeend", d.reply_html);\n'
        '        if (window._wpLocalizeTimestamps) window._wpLocalizeTimestamps(body.lastElementChild);\n'
        '        if (window._wpInitMaps) window._wpInitMaps(body.lastElementChild);\n'
        '        if (near) body.scrollTop = body.scrollHeight;\n'
        '      }\n'
        '    }\n'
        '  });\n'
        '  src.addEventListener("thread_reply_removed", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var root = document.getElementById(d.root_element_id);\n'
        '    if (root) {\n'
        '      var content = root.querySelector(".webpublish-message-content");\n'
        '      if (content) {\n'
        '        var existing = content.querySelector(".webpublish-thread-indicator");\n'
        '        if (d.indicator_html) {\n'
        '          if (existing) existing.outerHTML = d.indicator_html;\n'
        '          else content.insertAdjacentHTML("beforeend", d.indicator_html);\n'
        '        } else if (existing) {\n'
        '          existing.remove();\n'
        '        }\n'
        '      }\n'
        '    }\n'
        '    var inPanel = findInPanel(d.removed_element_id);\n'
        '    if (inPanel) inPanel.remove();\n'
        '  });\n'
        '  src.addEventListener("edit_message", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var el = document.getElementById(d.element_id) || findInPanel(d.element_id);\n'
        '    if (el) applyEdit(el, d);\n'
        '  });\n'
        '  src.addEventListener("redact_message", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var el = document.getElementById(d.element_id);\n'
        '    if (el) el.remove();\n'
        '    var p = findInPanel(d.element_id);\n'
        '    if (p) p.remove();\n'
        '  });\n'
        '  function applyReactionsBoth(d) {\n'
        '    applyReactions(document.getElementById(d.element_id), d.reactions_html);\n'
        '    applyReactions(findInPanel(d.element_id), d.reactions_html);\n'
        '  }\n'
        '  src.addEventListener("reaction_added", function(e) {\n'
        '    applyReactionsBoth(JSON.parse(e.data));\n'
        '  });\n'
        '  src.addEventListener("reaction_removed", function(e) {\n'
        '    applyReactionsBoth(JSON.parse(e.data));\n'
        '  });\n'
        '  src.addEventListener("pinned_changed", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var existing = document.querySelector(".webpublish-pinned-banner");\n'
        '    if (d.banner_html) {\n'
        '      if (existing) { existing.outerHTML = d.banner_html; }\n'
        '      else {\n'
        '        var actions = document.querySelector(".webpublish-header-actions");\n'
        '        if (actions) actions.insertAdjacentHTML("afterbegin", d.banner_html);\n'
        '      }\n'
        '    } else if (existing) {\n'
        '      existing.remove();\n'
        '    }\n'
        '  });\n'
        '  src.addEventListener("succession_changed", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var existing = document.querySelector(".webpublish-succession-banner");\n'
        '    if (d.banner_html) {\n'
        '      if (existing) { existing.outerHTML = d.banner_html; }\n'
        '      else {\n'
        '        var anchor = document.querySelector("main") || document.body;\n'
        '        anchor.insertAdjacentHTML("afterend", d.banner_html);\n'
        '      }\n'
        '    } else if (existing) {\n'
        '      existing.remove();\n'
        '    }\n'
        '  });\n'
        '})();\n'
        '</script>'
    )


def _chat_load_older_script(encoded_alias: str) -> str:
    # Scroll-to-top pagination: when the user nears the top of the message log,
    # fetch the batch of messages older than the current oldest (cursor =
    # firstElementChild's data-ts) and prepend them, preserving scroll position.
    older_url = "./older" if not encoded_alias else f"./{encoded_alias}/older"
    return (
        '<script>\n'
        '(function() {\n'
        '  var msgs = document.getElementById("messages");\n'
        '  if (!msgs) return;\n'
        '  var loading = false, exhausted = false;\n'
        '  function loadOlder() {\n'
        '    if (loading || exhausted) return;\n'
        '    var first = msgs.firstElementChild;\n'
        '    var cursor = first && first.dataset ? first.dataset.ts : null;\n'
        '    if (!cursor) return;\n'
        '    loading = true;\n'
        f'    fetch("{older_url}?before=" + encodeURIComponent(cursor), '
        '{headers: {"Accept": "text/html"}})\n'
        '      .then(function(r) { return r.ok ? r.text() : ""; })\n'
        '      .then(function(html) {\n'
        '        if (!html || !html.trim()) { exhausted = true; return; }\n'
        '        var tpl = document.createElement("div");\n'
        '        tpl.innerHTML = html;\n'
        '        var nodes = Array.prototype.slice.call(tpl.children).filter(function(n) {\n'
        '          return !(n.id && document.getElementById(n.id));\n'
        '        });\n'
        '        if (!nodes.length) { exhausted = true; return; }\n'
        '        var prevHeight = msgs.scrollHeight, prevTop = msgs.scrollTop;\n'
        '        for (var i = nodes.length - 1; i >= 0; i--) {\n'
        '          msgs.insertBefore(nodes[i], msgs.firstChild);\n'
        '          if (window._wpLocalizeTimestamps) window._wpLocalizeTimestamps(nodes[i]);\n'
        '          if (window._wpInitMaps) window._wpInitMaps(nodes[i]);\n'
        '        }\n'
        '        msgs.scrollTop = prevTop + (msgs.scrollHeight - prevHeight);\n'
        '      })\n'
        '      .catch(function() {})\n'
        '      .finally(function() { loading = false; });\n'
        '  }\n'
        '  msgs.addEventListener("scroll", function() {\n'
        '    if (msgs.scrollTop < 150) loadOlder();\n'
        '  }, {passive: true});\n'
        '})();\n'
        '</script>'
    )


def _thread_panel_script(encoded_alias: str) -> str:
    # Base URL for thread fragment fetches is computed client-side to keep the
    # script alias-agnostic; the server registers both /{alias}/thread/{id}
    # and /thread/{id}.
    return (
        '<script>\n'
        '(function() {\n'
        '  var panel = document.getElementById("thread-panel");\n'
        '  if (!panel) return;\n'
        '  var currentRoot = null;\n'
        '  function buildUrl(rootEid) {\n'
        '    var path = window.location.pathname.replace(/\\/$/, "");\n'
        '    return path + "/thread/" + encodeURIComponent(rootEid);\n'
        '  }\n'
        '  function stripThreadQuery() {\n'
        '    var sp = new URLSearchParams(window.location.search);\n'
        '    sp.delete("thread");\n'
        '    var qs = sp.toString();\n'
        '    return window.location.pathname + (qs ? "?" + qs : "") + window.location.hash;\n'
        '  }\n'
        '  function setThreadQuery(rootEid) {\n'
        '    var sp = new URLSearchParams(window.location.search);\n'
        '    sp.set("thread", rootEid);\n'
        '    return window.location.pathname + "?" + sp.toString() + window.location.hash;\n'
        '  }\n'
        '  function wireClose() {\n'
        '    var btn = panel.querySelector(".webpublish-thread-panel-close");\n'
        '    if (btn) btn.addEventListener("click", function() {\n'
        '      closePanel(true);\n'
        '    });\n'
        '  }\n'
        '  function openPanel(rootEid, pushHistory) {\n'
        '    if (currentRoot === rootEid && !panel.hidden) return;\n'
        '    fetch(buildUrl(rootEid), {credentials: "same-origin"}).then(function(resp) {\n'
        '      if (!resp.ok) throw new Error("fetch failed: " + resp.status);\n'
        '      return resp.text();\n'
        '    }).then(function(html) {\n'
        '      panel.innerHTML = html;\n'
        '      panel.hidden = false;\n'
        '      requestAnimationFrame(function() { panel.classList.add("open"); });\n'
        '      document.body.classList.add("has-thread-panel");\n'
        '      currentRoot = rootEid;\n'
        '      wireClose();\n'
        '      if (window._wpLocalizeTimestamps) window._wpLocalizeTimestamps(panel);\n'
        '      if (window._wpInitMaps) window._wpInitMaps(panel);\n'
        '      if (pushHistory) {\n'
        '        history.pushState({thread: rootEid}, "", setThreadQuery(rootEid));\n'
        '      }\n'
        '    }).catch(function(err) {\n'
        '      console.error("thread panel fetch failed", err);\n'
        '    });\n'
        '  }\n'
        '  function closePanel(pushHistory) {\n'
        '    panel.classList.remove("open");\n'
        '    document.body.classList.remove("has-thread-panel");\n'
        '    currentRoot = null;\n'
        '    setTimeout(function() {\n'
        '      if (!panel.classList.contains("open")) {\n'
        '        panel.hidden = true;\n'
        '        panel.innerHTML = "";\n'
        '      }\n'
        '    }, 220);\n'
        '    if (pushHistory) {\n'
        '      history.pushState({}, "", stripThreadQuery());\n'
        '    }\n'
        '  }\n'
        '  document.addEventListener("click", function(e) {\n'
        '    var btn = e.target.closest(".webpublish-thread-indicator");\n'
        '    if (!btn) return;\n'
        '    e.preventDefault();\n'
        '    var root = btn.getAttribute("data-root");\n'
        '    if (!root) return;\n'
        '    if (currentRoot === root && !panel.hidden) closePanel(true);\n'
        '    else openPanel(root, true);\n'
        '  });\n'
        '  window.addEventListener("popstate", function(e) {\n'
        '    var sp = new URLSearchParams(window.location.search);\n'
        '    var t = sp.get("thread");\n'
        '    if (t) openPanel(t, false);\n'
        '    else closePanel(false);\n'
        '  });\n'
        '  var initial = new URLSearchParams(window.location.search).get("thread");\n'
        '  if (initial) openPanel(initial, false);\n'
        '})();\n'
        '</script>'
    )


def _sse_journal_landing_script(encoded_alias: str) -> str:
    sse_url = "./sse" if not encoded_alias else f"./{encoded_alias}/sse"
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
        '  src.addEventListener("post_unpublished", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var el = document.getElementById(d.element_id);\n'
        '    if (el) el.remove();\n'
        '  });\n'
        '  src.addEventListener("pinned_changed", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var existing = document.querySelector(".webpublish-pinned-posts");\n'
        '    if (d.html) {\n'
        '      if (existing) { existing.outerHTML = d.html; }\n'
        '      else {\n'
        '        var main = document.querySelector(".webpublish-journal");\n'
        '        if (main) main.insertAdjacentHTML("afterbegin", d.html);\n'
        '      }\n'
        '    } else if (existing) {\n'
        '      existing.remove();\n'
        '    }\n'
        '  });\n'
        '  src.addEventListener("succession_changed", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    var existing = document.querySelector(".webpublish-succession-banner");\n'
        '    if (d.banner_html) {\n'
        '      if (existing) { existing.outerHTML = d.banner_html; }\n'
        '      else {\n'
        '        var anchor = document.querySelector("main") || document.body;\n'
        '        anchor.insertAdjacentHTML("afterend", d.banner_html);\n'
        '      }\n'
        '    } else if (existing) {\n'
        '      existing.remove();\n'
        '    }\n'
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
        f'  var postId = "{escape(post_event_id)}";\n'
        '  function applyReactions(el, html) {\n'
        '    if (!el) return;\n'
        '    var scope = el.classList.contains("webpublish-message")\n'
        '      ? (el.querySelector(".webpublish-message-content") || el) : el;\n'
        '    var existing = scope.querySelector(":scope > .webpublish-reactions");\n'
        '    if (!existing) existing = scope.querySelector(".webpublish-reactions");\n'
        '    if (html) {\n'
        '      if (existing) { existing.outerHTML = html; return; }\n'
        '      var body = scope.querySelector(".webpublish-body, .webpublish-post-body");\n'
        '      if (body) body.insertAdjacentHTML("afterend", html);\n'
        '      else scope.insertAdjacentHTML("beforeend", html);\n'
        '    } else if (existing) {\n'
        '      existing.remove();\n'
        '    }\n'
        '  }\n'
        '  function findPostOrComment(eventElementId) {\n'
        '    var byId = document.getElementById(eventElementId);\n'
        '    if (byId) return byId;\n'
        '    return null;\n'
        '  }\n'
        '  src.addEventListener("new_message", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    if (d.thread_root !== postId) return;\n'
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
        '  src.addEventListener("post_unpublished", function(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    if (d.event_id === postId) {\n'
        '      // The currently-viewed post was unpublished; reload to get the\n'
        '      // authoritative 404 from the server.\n'
        '      window.location.reload();\n'
        '    }\n'
        '  });\n'
        '  function handleReactions(e) {\n'
        '    var d = JSON.parse(e.data);\n'
        '    if (d.event_id === postId) {\n'
        '      var article = document.querySelector(".webpublish-post-full article");\n'
        '      if (article) applyReactions(article, d.reactions_html);\n'
        '      return;\n'
        '    }\n'
        '    var el = findPostOrComment(d.element_id);\n'
        '    if (el) applyReactions(el, d.reactions_html);\n'
        '  }\n'
        '  src.addEventListener("reaction_added", handleReactions);\n'
        '  src.addEventListener("reaction_removed", handleReactions);\n'
        '})();\n'
        '</script>'
    )


def _scroll_header_script() -> str:
    return (
        '<script>\n'
        '(function() {\n'
        '  var header = document.querySelector(".webpublish-header");\n'
        '  if (!header) return;\n'
        '  var topic = header.querySelector("p");\n'
        '  var toggle = header.querySelector(".webpublish-topic-toggle");\n'
        '  var mq = window.matchMedia("(max-width: 600px)");\n'
        '  var scroller = document.getElementById("messages") || window;\n'
        '  function getScrollY() {\n'
        '    return scroller === window ? window.scrollY : scroller.scrollTop;\n'
        '  }\n'
        '  // Hysteresis: different thresholds to enter vs leave the collapsed\n'
        '  // state so scroll jitter and mobile address-bar transitions near a\n'
        '  // single threshold cannot oscillate the class.\n'
        '  var ADD_AT = 80;\n'
        '  var REMOVE_AT = 20;\n'
        '  function update() {\n'
        '    if (header.classList.contains("expanded") || !mq.matches) {\n'
        '      header.classList.remove("scrolled");\n'
        '      return;\n'
        '    }\n'
        '    var y = getScrollY();\n'
        '    var isScrolled = header.classList.contains("scrolled");\n'
        '    if (!isScrolled && y > ADD_AT) header.classList.add("scrolled");\n'
        '    else if (isScrolled && y < REMOVE_AT) header.classList.remove("scrolled");\n'
        '  }\n'
        '  function checkOverflow() {\n'
        '    if (!topic || !toggle) return;\n'
        '    if (!mq.matches) { toggle.hidden = true; return; }\n'
        '    var wasExpanded = header.classList.contains("expanded");\n'
        '    if (wasExpanded) header.classList.remove("expanded");\n'
        '    var overflowing = topic.scrollHeight - 1 > topic.clientHeight;\n'
        '    if (wasExpanded) header.classList.add("expanded");\n'
        '    toggle.hidden = !overflowing;\n'
        '  }\n'
        '  scroller.addEventListener("scroll", update, {passive: true});\n'
        '  mq.addEventListener("change", function() { checkOverflow(); update(); });\n'
        '  window.addEventListener("resize", checkOverflow, {passive: true});\n'
        '  header.addEventListener("click", function(e) {\n'
        '    // Ignore clicks on interactive controls inside the header\n'
        '    // (links, buttons) so they do not double as scroll-to-top.\n'
        '    if (e.target.closest("a, button")) return;\n'
        '    scroller.scrollTo({top: 0, behavior: "smooth"});\n'
        '  });\n'
        '  header.style.cursor = "pointer";\n'
        '  if (toggle) {\n'
        '    toggle.addEventListener("click", function(e) {\n'
        '      e.stopPropagation();\n'
        '      var expanded = header.classList.toggle("expanded");\n'
        '      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");\n'
        '      toggle.textContent = expanded ? "Show less" : "Show more";\n'
        '      update();\n'
        '    });\n'
        '  }\n'
        '  checkOverflow();\n'
        '})();\n'
        '</script>'
    )


def render_pinned_banner_html(
    pinned_msgs: list[dict],
    room_id: str,
    homeserver_url: str,
) -> str:
    """Chat-mode banner listing pinned messages with anchor links to in-feed
    elements. Messages whose bodies we don't have render as matrix: URI links."""
    if not pinned_msgs:
        return ""
    items: list[str] = []
    for m in pinned_msgs:
        eid = m.get("event_id", "")
        if not eid:
            continue
        sender = escape(m.get("sender_name") or m.get("sender", ""))
        preview = (m.get("body") or "").strip().split("\n", 1)[0][:80]
        preview_html = escape(preview) if preview else "<em>(no text)</em>"
        if m.get("in_db"):
            href = f"#{safe_element_id(eid)}"
            items.append(
                f'<li><a href="{href}">{preview_html}</a>'
                f' <span class="webpublish-pinned-sender">— {sender}</span></li>'
            )
        else:
            mx_href = matrix_event_uri(room_id, eid, homeserver_url)
            items.append(
                f'<li><a href="{escape(mx_href)}" target="_blank" rel="noopener">'
                f'View in Matrix</a>'
                f' <span class="webpublish-pinned-sender">— {sender}</span></li>'
            )
    if not items:
        return ""
    count = len(items)
    label = f"{count} pinned message{'s' if count != 1 else ''}"
    return (
        f'<div class="webpublish-pinned-banner" data-count="{count}">\n'
        f'  <button class="webpublish-pinned-toggle" type="button" aria-expanded="false">'
        f'📌 <span class="webpublish-pinned-count">{label}</span></button>\n'
        f'  <ul class="webpublish-pinned-list" hidden>{"".join(items)}</ul>\n'
        f'</div>'
    )


def render_pinned_section_html(
    pinned_msgs: list[dict],
    encoded_alias: str,
    comment_counts: dict[str, int],
    base_url: str = "",
) -> str:
    """Journal-mode sticky section — rendered above the regular post list on
    page 1. `pinned_msgs` must only contain rows that exist in the DB."""
    if not pinned_msgs:
        return ""
    parts = [
        render_post_preview_html(
            p, encoded_alias, comment_counts.get(p["event_id"], 0),
            base_url, pinned=True,
        )
        for p in pinned_msgs
    ]
    return (
        '<section class="webpublish-pinned-posts" aria-label="Pinned posts">\n'
        + "\n".join(parts)
        + "\n</section>"
    )


def render_succession_banner(
    succession: dict,
    published_rooms_by_room_id: dict[str, dict],
    base_url: str,
    alias_hints: dict[str, str] | None = None,
    homeserver_url: str = "",
) -> str:
    """Footer banner linking to a replacement room (tombstone) and/or the
    predecessor room (m.room.create predecessor). Empty string when the room
    has neither a tombstone nor a predecessor.

    `succession` shape (see WebPublishBot._room_succession):
      - has_tombstone: bool
      - replacement_room: str ("" = explicit dead-end)
      - tombstone_ts: int | None (ms since epoch)
      - predecessor_room: str | None

    `published_rooms_by_room_id` maps room_id -> {"alias": ...} for locally
    published rooms; used to decide between an internal link and a matrix: URI.
    """
    if not succession:
        return ""
    has_tomb = bool(succession.get("has_tombstone"))
    replacement = succession.get("replacement_room") or ""
    predecessor = succession.get("predecessor_room") or ""
    if not has_tomb and not predecessor:
        return ""

    hints = alias_hints or {}

    def _internal_or_matrix_href(target_room_id: str) -> str:
        pub = published_rooms_by_room_id.get(target_room_id)
        if pub and pub.get("alias") and base_url:
            a = pub["alias"]
            if a == "/":
                return f"{base_url}/"
            return f"{base_url}/{quote(a, safe='')}"
        return matrix_room_uri(
            target_room_id, hints.get(target_room_id, ""), homeserver_url,
        )

    # Left group: back-link to the predecessor (if any).
    left_parts: list[str] = []
    if predecessor:
        href = _internal_or_matrix_href(predecessor)
        left_parts.append(
            f'<a class="webpublish-succession-link webpublish-succession-back" '
            f'href="{escape(href)}">&larr; View Archive</a>'
        )

    # Right group: "this room was archived" text (whenever the room is
    # tombstoned, regardless of whether a replacement is set), followed by
    # the forward-link when a replacement exists.
    right_parts: list[str] = []
    if has_tomb:
        ts = succession.get("tombstone_ts")
        date_str = ""
        if isinstance(ts, int) and ts > 0:
            try:
                date_str = datetime.fromtimestamp(
                    ts / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            except (OSError, ValueError, OverflowError):
                date_str = ""
        if date_str:
            right_parts.append(
                f'<span class="webpublish-succession-archived">'
                f'This site was archived on {escape(date_str)}.</span>'
            )
        else:
            right_parts.append(
                '<span class="webpublish-succession-archived">'
                'This site was archived.</span>'
            )
        if replacement:
            href = _internal_or_matrix_href(replacement)
            right_parts.append(
                f'<a class="webpublish-succession-link webpublish-succession-forward" '
                f'href="{escape(href)}">View Current Content &rarr;</a>'
            )

    if not left_parts and not right_parts:
        return ""

    left_html = (
        '  <div class="webpublish-succession-left">\n    '
        + "\n    ".join(left_parts)
        + "\n  </div>\n"
    ) if left_parts else ""
    right_html = (
        '  <div class="webpublish-succession-right">\n    '
        + "\n    ".join(right_parts)
        + "\n  </div>\n"
    ) if right_parts else ""
    return (
        '<div class="webpublish-succession-banner">\n'
        + left_html
        + right_html
        + '</div>'
    )


def _pinned_toggle_script() -> str:
    return (
        '<script>\n'
        '(function() {\n'
        '  document.addEventListener("click", function(e) {\n'
        '    var btn = e.target.closest(".webpublish-pinned-toggle");\n'
        '    if (!btn) return;\n'
        '    var banner = btn.closest(".webpublish-pinned-banner");\n'
        '    if (!banner) return;\n'
        '    var list = banner.querySelector(".webpublish-pinned-list");\n'
        '    if (!list) return;\n'
        '    var expanded = btn.getAttribute("aria-expanded") === "true";\n'
        '    btn.setAttribute("aria-expanded", expanded ? "false" : "true");\n'
        '    list.hidden = expanded;\n'
        '  });\n'
        '})();\n'
        '</script>'
    )


def _render_room_avatar_img(room_avatar_url: str, proxy_base_url: str) -> str:
    if not room_avatar_url or not proxy_base_url:
        return ""
    http_url = mxc_to_http(room_avatar_url, "", proxy_base_url)
    return f'<img class="webpublish-room-avatar" src="{escape(http_url)}" alt="">' if http_url else ""


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
    room_avatar_url: str = "",
    comment_counts: dict[str, int] | None = None,
    thread_participants: dict[str, list[dict]] | None = None,
    pinned_banner_html: str = "",
    succession_banner_html: str = "",
) -> str:
    counts = comment_counts or {}
    parts = thread_participants or {}
    has_maps = _needs_leaflet(messages)
    head = _page_head(room_name, custom_css, extra_head=_LEAFLET_HEAD_ASSETS if has_maps else "")
    msg_chunks: list[str] = []
    for m in messages:
        eid = m["event_id"]
        count = counts.get(eid, 0)
        indicator = render_thread_indicator_html(
            eid, count, parts.get(eid, []), homeserver_url, proxy_base_url,
        ) if count else ""
        msg_chunks.append(render_message_html(
            m, homeserver_url, proxy_base_url,
            thread_indicator_html=indicator,
        ))
    msgs_html = "\n".join(msg_chunks)
    topic_p = (
        f"  <p>{linkify_plaintext(room_topic, newlines_to_br=False)}</p>\n"
        f'  <button class="webpublish-topic-toggle" type="button" aria-expanded="false" hidden>Show more</button>'
    ) if room_topic else ""
    sse = _sse_chat_script(encoded_alias)
    load_older = _chat_load_older_script(encoded_alias)
    panel_script = _thread_panel_script(encoded_alias)
    pinned_script = _pinned_toggle_script()
    leaflet_init = f"\n{_LEAFLET_INIT_SCRIPT}" if has_maps else ""
    scroll_script = _scroll_header_script()
    avatar_img = _render_room_avatar_img(room_avatar_url, proxy_base_url)
    actions_inner = (
        f'{pinned_banner_html}\n  ' if pinned_banner_html else ''
    ) + (
        '<button class="webpublish-jump-latest" type="button" hidden>'
        '&#x2B07; Jump to newest</button>'
    )
    succession_footer = f'{succession_banner_html}\n' if succession_banner_html else ''
    return (
        f'{head}\n<body class="webpublish-chat-mode">\n'
        f'<header class="webpublish-header">\n'
        f'  <div class="webpublish-header-title">{avatar_img}<h1>{escape(room_name)}</h1></div>\n{topic_p}\n'
        f'  <div class="webpublish-header-actions">\n  {actions_inner}\n  </div>\n'
        f'</header>\n'
        f'<main class="webpublish-chat">\n'
        f'  <div class="webpublish-messages" id="messages">\n{msgs_html}\n  </div>\n'
        f'</main>\n'
        f'<aside class="webpublish-thread-panel" id="thread-panel" hidden></aside>\n'
        f'{succession_footer}'
        f'{_LOCALIZE_TIMESTAMPS_SCRIPT}{leaflet_init}\n{sse}\n{load_older}\n{panel_script}\n{pinned_script}\n{scroll_script}\n'
        f'</body>\n</html>'
    )


def render_thread_panel_fragment(
    root_msg: dict,
    comments: list[dict],
    homeserver_url: str,
    proxy_base_url: str = "",
) -> str:
    root_html = render_message_html(
        root_msg, homeserver_url, proxy_base_url, show_reply_header=False,
    )
    comments_html = "\n".join(
        render_message_html(
            c, homeserver_url, proxy_base_url,
            show_reply_header=bool(c.get("reply_to")),
        )
        for c in comments
    )
    count = len(comments)
    label = f"{count} repl{'y' if count == 1 else 'ies'}" if count else "No replies yet"
    return (
        f'<div class="webpublish-thread-panel-inner" data-root="{escape(root_msg["event_id"])}">\n'
        f'  <div class="webpublish-thread-panel-header">\n'
        f'    <span class="webpublish-thread-panel-title">Thread · {escape(label)}</span>\n'
        f'    <button type="button" class="webpublish-thread-panel-close" aria-label="Close">&times;</button>\n'
        f'  </div>\n'
        f'  <div class="webpublish-thread-panel-body">\n'
        f'    {root_html}\n'
        f'    {comments_html}\n'
        f'  </div>\n'
        f'</div>'
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
    base_url: str = "",
    room_avatar_url: str = "",
    pinned_section_html: str = "",
    succession_banner_html: str = "",
) -> str:
    og = _og_meta_site(room_name, room_topic, encoded_alias, base_url, room_avatar_url, base_url) if base_url else ""
    head = _page_head(room_name, custom_css, og_meta=og)
    topic_p = (
        f"  <p>{linkify_plaintext(room_topic, newlines_to_br=False)}</p>\n"
        f'  <button class="webpublish-topic-toggle" type="button" aria-expanded="false" hidden>Show more</button>'
    ) if room_topic else ""

    posts_parts = []
    for post in posts:
        count = comment_counts.get(post["event_id"], 0)
        posts_parts.append(render_post_preview_html(post, encoded_alias, count, base_url))
    posts_html = "\n".join(posts_parts)
    pinned_block = f'{pinned_section_html}\n' if pinned_section_html else ''

    pag_parts: list[str] = []
    if total_pages > 1:
        for p in range(1, total_pages + 1):
            if p == page:
                pag_parts.append(f'<span class="active">{p}</span>')
            else:
                pag_parts.append(f'<a href="./{encoded_alias}?page={p}">{p}</a>')
    pag_html = (
        f'<nav class="webpublish-pagination">{"".join(pag_parts)}</nav>'
        if pag_parts else ""
    )

    feed_url = f"{base_url}/feed.xml" if not encoded_alias else f"{base_url}/{encoded_alias}/feed.xml"
    feed_footer = (
        f'  <div class="webpublish-feed-footer">'
        f'<a href="{escape(feed_url)}">&#x2605; Atom feed</a></div>\n'
    ) if base_url else ""

    sse = _sse_journal_landing_script(encoded_alias)
    scroll_script = _scroll_header_script()
    avatar_img = _render_room_avatar_img(room_avatar_url, base_url)
    succession_footer = f'{succession_banner_html}\n' if succession_banner_html else ''
    return (
        f'{head}\n<body>\n'
        f'<header class="webpublish-header">\n'
        f'  <div class="webpublish-header-title">{avatar_img}<h1>{escape(room_name)}</h1></div>\n{topic_p}\n'
        f'</header>\n'
        f'<main class="webpublish-journal">\n'
        f'  {pinned_block}'
        f'  <div class="webpublish-posts">\n{posts_html}\n  </div>\n'
        f'  {pag_html}\n'
        f'{feed_footer}'
        f'</main>\n{succession_footer}{_LOCALIZE_TIMESTAMPS_SCRIPT}\n{sse}\n{scroll_script}\n</body>\n</html>'
    )


def render_journal_post(
    room_name: str,
    room_topic: str,
    post: dict,
    comments: list[dict],
    encoded_alias: str,
    custom_css: str,
    homeserver_url: str,
    proxy_base_url: str = "",
    room_avatar_url: str = "",
) -> str:
    title = (post.get("body") or "").split("\n", 1)[0][:80]
    has_maps = _needs_leaflet([post] + comments)
    og = _og_meta_post(post, room_name, encoded_alias, proxy_base_url, homeserver_url, room_avatar_url) if proxy_base_url else ""
    head = _page_head(f"{title} - {room_name}", custom_css, extra_head=_LEAFLET_HEAD_ASSETS if has_maps else "", og_meta=og)
    body_html = render_body(post, homeserver_url, proxy_base_url, journal=True)
    author = escape(post.get("sender_name") or post["sender"])
    date = format_date(post["timestamp"])
    edited = " (edited)" if post.get("edited") else ""

    tags = post.get("tags", [])
    tags_html = render_tag_chips(tags, encoded_alias, proxy_base_url)
    tags_section = f'\n    {tags_html}' if tags_html else ""

    post_reactions_html = render_reactions_html(
        post.get("reactions") or [], homeserver_url, proxy_base_url,
    )
    post_reactions_section = f'    {post_reactions_html}\n' if post_reactions_html else ""

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

    matrix_link = matrix_event_uri(post["room_id"], post["event_id"], homeserver_url)
    sse = _sse_post_detail_script(post["event_id"])
    leaflet_init = f"\n{_LEAFLET_INIT_SCRIPT}" if has_maps else ""
    scroll_script = _scroll_header_script()
    back_href = "../" if not encoded_alias else f"../../{encoded_alias}"
    avatar_img = _render_room_avatar_img(room_avatar_url, proxy_base_url)
    topic_p = (
        f"  <p>{linkify_plaintext(room_topic, newlines_to_br=False)}</p>\n"
        f'  <button class="webpublish-topic-toggle" type="button" aria-expanded="false" hidden>Show more</button>'
    ) if room_topic else ""
    return (
        f'{head}\n<body>\n'
        f'<header class="webpublish-header">\n'
        f'  <div class="webpublish-header-title">{avatar_img}<h1>{escape(room_name)}</h1></div>\n{topic_p}\n'
        f'</header>\n'
        f'<main class="webpublish-post-full">\n'
        f'  <a class="webpublish-back-link" href="{back_href}">&larr; back to posts</a>\n'
        f'  <article>\n'
        f'    <div class="webpublish-post-meta">\n'
        f'      <span>{author}</span>\n'
        f'      <span>{date}{edited}</span>\n'
        f'    </div>\n'
        f'{tags_section}\n'
        f'    <div class="webpublish-post-body">{body_html}</div>\n'
        f'{post_reactions_section}'
        f'  </article>\n'
        f'  <section class="webpublish-comments">\n'
        f'    <h2>{label}</h2>\n'
        f'    <div class="webpublish-matrix-reply-link">\n'
        f'      <a href="{matrix_link}" target="_blank" rel="noopener noreferrer">Reply in Matrix</a>\n'
        f'    </div>\n'
        f'    <div id="comments">\n{comments_html}\n    </div>\n'
        f'  </section>\n'
        f'</main>\n{_LOCALIZE_TIMESTAMPS_SCRIPT}{leaflet_init}\n{sse}\n{scroll_script}\n</body>\n</html>'
    )


def render_atom_feed(
    room_name: str,
    room_topic: str,
    posts: list[dict],
    encoded_alias: str,
    base_url: str,
    homeserver_url: str,
) -> str:
    feed_url = f"{base_url}/" if not encoded_alias else f"{base_url}/{encoded_alias}"
    self_url = f"{base_url}/feed.xml" if not encoded_alias else f"{base_url}/{encoded_alias}/feed.xml"
    updated = format_iso(posts[0]["timestamp"]) if posts else format_iso(0)

    entries = []
    for post in posts:
        title = escape((post.get("body") or "").split("\n", 1)[0][:80] or "Untitled")
        eid_enc = quote(post["event_id"], safe="")
        post_url = f"{base_url}/post/{eid_enc}" if not encoded_alias else f"{base_url}/{encoded_alias}/post/{eid_enc}"
        iso = format_iso(post["timestamp"])
        author = escape(post.get("sender_name") or post.get("sender", ""))
        body_html = render_body(post, homeserver_url, base_url, journal=True)
        content = escape(body_html)

        enclosure = ""
        if post.get("msgtype") == "m.image" and post.get("media_url"):
            enc_url = mxc_to_http(post["media_url"], homeserver_url, base_url)
            if enc_url:
                enclosure = f'\n    <link rel="enclosure" href="{escape(enc_url)}"/>'

        entries.append(
            f'  <entry>\n'
            f'    <id>{escape(post["event_id"])}</id>\n'
            f'    <title>{title}</title>\n'
            f'    <link href="{escape(post_url)}"/>{enclosure}\n'
            f'    <published>{iso}</published>\n'
            f'    <updated>{iso}</updated>\n'
            f'    <author><name>{author}</name></author>\n'
            f'    <content type="html">{content}</content>\n'
            f'  </entry>'
        )

    entries_xml = "\n".join(entries)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        f'  <title>{escape(room_name)}</title>\n'
        f'  <subtitle>{escape(room_topic or "")}</subtitle>\n'
        f'  <link href="{escape(feed_url)}"/>\n'
        f'  <link rel="self" href="{escape(self_url)}"/>\n'
        f'  <updated>{updated}</updated>\n'
        f'  <id>{escape(feed_url)}</id>\n'
        f'{entries_xml}\n'
        '</feed>'
    )


def render_tag_index_page(
    room_name: str,
    tags_with_counts: list[tuple[str, int]],
    encoded_alias: str,
    custom_css: str,
    base_url: str = "",
    room_avatar_url: str = "",
) -> str:
    og = _og_meta_site(room_name, "", encoded_alias, base_url, room_avatar_url, base_url) if base_url else ""
    head = _page_head(f"Tags - {room_name}", custom_css, og_meta=og)
    back_href = "./" if not encoded_alias else f"../{encoded_alias}"

    tag_items = []
    for tag, count in tags_with_counts:
        url = f"./{encoded_alias}/tag/{quote(tag, safe='')}" if encoded_alias else f"./tag/{quote(tag, safe='')}"
        tag_items.append(
            f'<li><a class="webpublish-tag-chip" href="{url}">#{escape(tag)}</a>'
            f' <span class="webpublish-text-muted">({count})</span></li>'
        )
    tags_html = "\n    ".join(tag_items) if tag_items else "<li>No tags yet.</li>"

    scroll_script = _scroll_header_script()
    avatar_img = _render_room_avatar_img(room_avatar_url, base_url)
    return (
        f'{head}\n<body>\n'
        f'<header class="webpublish-header">\n'
        f'  <div class="webpublish-header-title">{avatar_img}<h1>{escape(room_name)}</h1></div>\n'
        f'</header>\n'
        f'<main class="webpublish-journal">\n'
        f'  <a class="webpublish-back-link" href="{back_href}">&larr; back to posts</a>\n'
        f'  <h2 class="webpublish-tag-header">All Tags</h2>\n'
        f'  <ul class="webpublish-tag-list">\n    {tags_html}\n  </ul>\n'
        f'</main>\n{scroll_script}\n</body>\n</html>'
    )


def render_tag_filter_page(
    room_name: str,
    tag: str,
    posts: list[dict],
    encoded_alias: str,
    page: int,
    total_pages: int,
    custom_css: str,
    comment_counts: dict[str, int],
    base_url: str = "",
    room_avatar_url: str = "",
) -> str:
    og = _og_meta_site(f"#{tag} - {room_name}", "", encoded_alias, base_url, room_avatar_url, base_url) if base_url else ""
    head = _page_head(f"#{tag} - {room_name}", custom_css, og_meta=og)
    back_href = "../" if not encoded_alias else f"../../{encoded_alias}"

    posts_parts = []
    for post in posts:
        count = comment_counts.get(post["event_id"], 0)
        posts_parts.append(render_post_preview_html(post, encoded_alias, count, base_url))
    posts_html = "\n".join(posts_parts) if posts_parts else "<p>No posts with this tag.</p>"

    pag_parts: list[str] = []
    if total_pages > 1:
        tag_enc = quote(tag, safe="")
        for p in range(1, total_pages + 1):
            if p == page:
                pag_parts.append(f'<span class="active">{p}</span>')
            else:
                url_part = f"./{encoded_alias}/tag/{tag_enc}" if encoded_alias else f"./tag/{tag_enc}"
                pag_parts.append(f'<a href="{url_part}?page={p}">{p}</a>')
    pag_html = (
        f'<nav class="webpublish-pagination">{"".join(pag_parts)}</nav>'
        if pag_parts else ""
    )

    feed_url = f"{base_url}/feed.xml" if not encoded_alias else f"{base_url}/{encoded_alias}/feed.xml"
    feed_footer = (
        f'  <div class="webpublish-feed-footer">'
        f'<a href="{escape(feed_url)}">&#x2605; Atom feed</a></div>\n'
    ) if base_url else ""

    scroll_script = _scroll_header_script()
    avatar_img = _render_room_avatar_img(room_avatar_url, base_url)
    return (
        f'{head}\n<body>\n'
        f'<header class="webpublish-header">\n'
        f'  <div class="webpublish-header-title">{avatar_img}<h1>{escape(room_name)}</h1></div>\n'
        f'</header>\n'
        f'<main class="webpublish-journal">\n'
        f'  <a class="webpublish-back-link" href="{back_href}">&larr; back to posts</a>\n'
        f'  <h2 class="webpublish-tag-header">#{escape(tag)}</h2>\n'
        f'  <div class="webpublish-posts">\n{posts_html}\n  </div>\n'
        f'  {pag_html}\n'
        f'{feed_footer}'
        f'</main>\n{_LOCALIZE_TIMESTAMPS_SCRIPT}\n{scroll_script}\n</body>\n</html>'
    )
