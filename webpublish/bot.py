from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from typing import Any, Callable, Type
from urllib.parse import unquote

from aiohttp.web import Request, Response, StreamResponse
from maubot import Plugin, MessageEvent
from maubot.handlers import command, event, web
from mautrix.api import Method, Path
from mautrix.types import EventType
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .db import upgrade_table
from .templates import (
    parse_hashtags,
    render_atom_feed,
    render_body,
    render_chat_page,
    render_journal_landing,
    render_journal_post,
    render_message_html,
    render_pinned_banner_html,
    render_pinned_section_html,
    render_post_preview_html,
    render_reactions_html,
    render_succession_banner,
    render_tag_filter_page,
    render_tag_index_page,
    render_thread_indicator_html,
    render_thread_panel_fragment,
    safe_element_id,
)

# Short freshness + generous stale-while-revalidate. SSE pushes updates live, so
# cache refresh isn't the primary path to seeing new messages. This mostly
# offloads repeat/refresh traffic to browser + reverse proxy + CDN.
_HTML_CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=600"

# Media above this size streams through without occupying an LRU cache slot.
# Keeps the 100-entry cache useful for avatars/small images and avoids a single
# video eviction-thrashing everything else.
_MEDIA_STREAM_THRESHOLD = 2 * 1024 * 1024
_MEDIA_CACHE_CONTROL = "public, max-age=86400"

# Matrix state event type used to store per-room config overrides.
CONFIG_STATE_TYPE = "org.jobmachine.webpublish.config"

# Aliases that would collide with web routes if allowed as a room URI.
RESERVED_ALIASES: frozenset[str] = frozenset({
    "media", "tiles", "theme", "tag", "tags", "post", "sse", "feed.xml", "thread",
    "older",
})

# Batch size for chat-mode scroll-to-top "load older" requests.
_CHAT_OLDER_BATCH = 50


def _parse_bool(s: str) -> bool:
    low = s.strip().lower()
    if low in ("true", "yes", "1", "on"):
        return True
    if low in ("false", "no", "0", "off"):
        return False
    raise ValueError(f"expected true/false, got {s!r}")


# Config keys that can be overridden per-room, mapped to their value parsers.
# `base_url` is intentionally excluded — it's infrastructure-level, not per-room.
OVERRIDABLE_CONFIG: dict[str, Callable[[str], Any]] = {
    "css": str,
    "pagination": int,
    "max_backfill": int,
    "backfill_batch_size": int,
    "min_power_level": int,
    "journal_author_pl": int,
    "journal_emoji_publish": _parse_bool,
    "journal_enforce_messages": _parse_bool,
    "chat_author_pl": int,
    "chat_enforce_messages": _parse_bool,
    "head_html": str,
    "body_html": str,
}


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("css")
        helper.copy("pagination")
        helper.copy("max_backfill")
        helper.copy("backfill_batch_size")
        helper.copy("base_url")
        helper.copy("min_power_level")
        helper.copy("journal_author_pl")
        helper.copy("journal_emoji_publish")
        helper.copy("journal_enforce_messages")
        helper.copy("chat_author_pl")
        helper.copy("chat_enforce_messages")
        helper.copy("head_html")
        helper.copy("body_html")


class WebPublishBot(Plugin):

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @classmethod
    def get_db_upgrade_table(cls):
        return upgrade_table

    async def start(self) -> None:
        self.config.load_and_update()
        self._published: dict[str, dict] = {}       # room_id -> {mode, alias, default_alias}
        self._alias_to_room: dict[str, str] = {}    # alias (no #) -> room_id
        self._redirect_aliases: dict[str, str] = {}  # default_alias -> override_alias
        self._sse_queues: dict[str, set[asyncio.Queue]] = {}
        self._display_names: dict[str, str] = {}
        self._avatar_urls: dict[str, str] = {}
        self._room_avatars: dict[str, str] = {}
        self._backfilling: set[str] = set()
        self._backfill_tasks: dict[str, asyncio.Task] = {}
        # Cap simultaneous room backfills so rebuilding several rooms at once
        # doesn't saturate the homeserver pagination endpoint or the DB pool.
        self._backfill_semaphore: asyncio.Semaphore = asyncio.Semaphore(2)
        self._media_cache: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
        self._media_cache_max: int = 100
        self._tile_cache: OrderedDict[str, bytes] = OrderedDict()
        self._tile_cache_max: int = 256
        self._room_create_cache: dict[str, tuple[int, set[str]]] = {}
        self._room_config_overrides: dict[str, dict[str, Any]] = {}
        self._pinned_events: dict[str, list[str]] = {}  # room_id -> ordered pinned event_ids
        # Room-succession state for tombstone / predecessor banners.
        # room_id -> {
        #   "has_tombstone": bool,
        #   "replacement_room": str,      # "" == dead-end; non-empty == forward link
        #   "tombstone_ts": int | None,
        #   "predecessor_room": str | None,
        # }
        self._room_succession: dict[str, dict] = {}
        # Guards concurrent auto-migration calls for the same old room. A race
        # is possible because both the tombstone handler and the invite
        # handler kick migration for the same pair.
        self._migrating: set[str] = set()
        await self._load_published_rooms()
        for rid in list(self._published.keys()):
            self._pinned_events[rid] = await self._get_pinned_events(rid)
            self._room_succession[rid] = await self._fetch_room_succession(rid)
        await self._resume_backfills()
        # Attempt migration for any published room that was already tombstoned
        # when we booted. Runs in the background so startup isn't blocked on
        # join/fetch round-trips.
        for rid in list(self._published.keys()):
            succession = self._room_succession.get(rid) or {}
            replacement = succession.get("replacement_room") or ""
            if replacement and replacement not in self._published:
                self.log.info(
                    f"Startup: published room {rid} is tombstoned -> {replacement}; "
                    f"scheduling migration"
                )
                self._launch_migration(rid, replacement)

    async def stop(self) -> None:
        for queues in self._sse_queues.values():
            for q in queues:
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass
        self._sse_queues.clear()
        tasks = list(self._backfill_tasks.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._backfill_tasks.clear()

    def _launch_backfill(self, room_id: str) -> None:
        """Spawn a tracked backfill task. Safe to call repeatedly; the inner
        guard (`_backfilling`) prevents duplicate work if one is already running."""
        existing = self._backfill_tasks.get(room_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._backfill_room(room_id))
        self._backfill_tasks[room_id] = task

        def _cleanup(t: asyncio.Task, rid: str = room_id) -> None:
            # Only pop if the slot still holds this task (avoids racing with a
            # re-launch that replaced the entry).
            if self._backfill_tasks.get(rid) is t:
                self._backfill_tasks.pop(rid, None)

        task.add_done_callback(_cleanup)

    async def _resume_backfills(self) -> None:
        rows = await self.database.fetch(
            "SELECT room_id, status FROM backfill_progress WHERE status='running'"
        )
        for row in rows:
            rid = row["room_id"]
            if rid in self._published:
                self.log.info(f"Resuming in-progress backfill for {rid}")
                self._launch_backfill(rid)

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    async def _load_published_rooms(self) -> None:
        rows = await self.database.fetch(
            "SELECT room_id, mode, alias, default_alias FROM published_rooms"
        )
        for row in rows:
            rid, alias = row["room_id"], row["alias"]
            default_alias = row["default_alias"] or alias
            self._published[rid] = {"mode": row["mode"], "alias": alias, "default_alias": default_alias}
            self._alias_to_room[alias] = rid
            if default_alias != alias:
                self._alias_to_room[default_alias] = rid
                self._redirect_aliases[default_alias] = alias
            self._room_config_overrides[rid] = await self._fetch_room_overrides(rid)

    async def _archive_publication(self, room_id: str, archived_alias: str) -> None:
        """Rewrite a published row to a room-id-based alias so the human alias
        it previously held is free for a replacement room to claim. Clears any
        setpath-style `default_alias` redirect; the archive URL is canonical."""
        info = self._published.get(room_id)
        if not info:
            return
        old_alias = info["alias"]
        old_default = info.get("default_alias") or old_alias
        self._alias_to_room.pop(old_alias, None)
        if old_default != old_alias:
            self._alias_to_room.pop(old_default, None)
        self._redirect_aliases.pop(old_default, None)
        await self.database.execute(
            "UPDATE published_rooms SET alias=$1, default_alias=$1 WHERE room_id=$2",
            archived_alias, room_id,
        )
        self._published[room_id] = {
            "mode": info["mode"],
            "alias": archived_alias,
            "default_alias": archived_alias,
        }
        self._alias_to_room[archived_alias] = room_id

    async def _set_published(self, room_id: str, mode: str, alias: str) -> None:
        old = self._published.get(room_id)
        default_alias = old["default_alias"] if old else alias
        if old and old["alias"] != alias and old["alias"] != default_alias:
            self._alias_to_room.pop(old["alias"], None)
        await self.database.execute(
            "INSERT INTO published_rooms (room_id, mode, alias, default_alias) VALUES ($1, $2, $3, $3) "
            "ON CONFLICT (room_id) DO UPDATE SET mode = $2, alias = $3",
            room_id, mode, alias,
        )
        self._published[room_id] = {"mode": mode, "alias": alias, "default_alias": default_alias}
        self._alias_to_room[alias] = room_id
        if default_alias != alias:
            self._alias_to_room[default_alias] = room_id
            self._redirect_aliases[default_alias] = alias
        else:
            self._redirect_aliases.pop(default_alias, None)

    async def _remove_published(self, room_id: str) -> None:
        info = self._published.pop(room_id, None)
        if info:
            self._alias_to_room.pop(info["alias"], None)
            default_alias = info.get("default_alias")
            if default_alias and default_alias != info["alias"]:
                self._alias_to_room.pop(default_alias, None)
                self._redirect_aliases.pop(default_alias, None)
        self._pinned_events.pop(room_id, None)
        self._room_avatars.pop(room_id, None)
        self._room_succession.pop(room_id, None)
        await self.database.execute(
            "DELETE FROM published_rooms WHERE room_id = $1", room_id
        )

    async def _store_message(
        self, room_id: str, event_id: str, sender: str, sender_name: str,
        body: str, formatted_body: str | None, msgtype: str,
        media_url: str | None, timestamp: int, thread_root: str | None,
        reply_to: str | None = None, geo_uri: str | None = None,
        published: bool = True, avatar_url: str | None = None,
    ) -> None:
        await self.database.execute(
            "INSERT INTO messages "
            "(event_id, room_id, sender, sender_name, body, formatted_body, "
            "msgtype, media_url, timestamp, thread_root, reply_to, geo_uri, "
            "edited, redacted, published, avatar_url) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,FALSE,FALSE,$13,$14) "
            "ON CONFLICT (event_id) DO NOTHING",
            event_id, room_id, sender, sender_name, body, formatted_body,
            msgtype, media_url, timestamp, thread_root, reply_to, geo_uri,
            published, avatar_url or None,
        )

    async def _update_message_edit(
        self, event_id: str, body: str, formatted_body: str | None,
    ) -> None:
        await self.database.execute(
            "UPDATE messages SET body=$1, formatted_body=$2, edited=TRUE "
            "WHERE event_id=$3",
            body, formatted_body, event_id,
        )

    async def _mark_redacted(self, event_id: str) -> None:
        await self.database.execute(
            "DELETE FROM messages WHERE event_id = $1", event_id
        )

    async def _get_messages(
        self, room_id: str, limit: int = 500, top_level_only: bool = False,
    ) -> list[dict]:
        if top_level_only:
            rows = await self.database.fetch(
                "SELECT * FROM messages WHERE room_id=$1 AND redacted=FALSE "
                "AND thread_root IS NULL ORDER BY timestamp DESC LIMIT $2",
                room_id, limit,
            )
        else:
            rows = await self.database.fetch(
                "SELECT * FROM messages WHERE room_id=$1 AND redacted=FALSE "
                "ORDER BY timestamp DESC LIMIT $2",
                room_id, limit,
            )
        return [dict(r) for r in reversed(rows)]

    async def _get_messages_before(
        self, room_id: str, before_ts: int, limit: int = 50,
        top_level_only: bool = True,
    ) -> list[dict]:
        """Fetch the batch of messages immediately older than before_ts, for
        chat-mode scroll-to-top pagination. Returned chronological (oldest
        first) to match the main list's ordering."""
        if top_level_only:
            rows = await self.database.fetch(
                "SELECT * FROM messages WHERE room_id=$1 AND redacted=FALSE "
                "AND thread_root IS NULL AND timestamp < $2 "
                "ORDER BY timestamp DESC LIMIT $3",
                room_id, before_ts, limit,
            )
        else:
            rows = await self.database.fetch(
                "SELECT * FROM messages WHERE room_id=$1 AND redacted=FALSE "
                "AND timestamp < $2 ORDER BY timestamp DESC LIMIT $3",
                room_id, before_ts, limit,
            )
        return [dict(r) for r in reversed(rows)]

    async def _get_posts(
        self, room_id: str, page: int, per_page: int,
        exclude_event_ids: list[str] | None = None,
    ) -> tuple[list[dict], int]:
        # total_pages always reflects the full post set so pagination stays
        # stable across requests that exclude pinned posts on page 1 only.
        total = await self.database.fetchval(
            "SELECT COUNT(*) FROM messages "
            "WHERE room_id=$1 AND thread_root IS NULL AND redacted=FALSE AND published=TRUE",
            room_id,
        ) or 0
        total_pages = max(1, (total + per_page - 1) // per_page)
        offset = (page - 1) * per_page
        exclude = exclude_event_ids or []
        if exclude:
            placeholders = ",".join(f"${i + 2}" for i in range(len(exclude)))
            rows = await self.database.fetch(
                f"SELECT * FROM messages "
                f"WHERE room_id=$1 AND thread_root IS NULL AND redacted=FALSE "
                f"AND published=TRUE AND event_id NOT IN ({placeholders}) "
                f"ORDER BY timestamp DESC LIMIT ${len(exclude) + 2} OFFSET ${len(exclude) + 3}",
                room_id, *exclude, per_page, offset,
            )
            return [dict(r) for r in rows], total_pages
        rows = await self.database.fetch(
            "SELECT * FROM messages "
            "WHERE room_id=$1 AND thread_root IS NULL AND redacted=FALSE AND published=TRUE "
            "ORDER BY timestamp DESC LIMIT $2 OFFSET $3",
            room_id, per_page, offset,
        )
        return [dict(r) for r in rows], total_pages

    async def _get_comment_counts(self, event_ids: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {eid: 0 for eid in event_ids}
        if not event_ids:
            return counts
        placeholders = ",".join(f"${i + 1}" for i in range(len(event_ids)))
        rows = await self.database.fetch(
            f"SELECT thread_root, COUNT(*) AS n FROM messages "
            f"WHERE thread_root IN ({placeholders}) AND redacted=FALSE "
            f"GROUP BY thread_root",
            *event_ids,
        )
        for row in rows:
            counts[row["thread_root"]] = row["n"]
        return counts

    async def _get_thread_participants(
        self, root_event_ids: list[str], per_root_limit: int = 6,
    ) -> dict[str, list[dict]]:
        """Return up to `per_root_limit` distinct participants per thread root,
        ordered by earliest reply timestamp. Each entry is a dict with sender,
        sender_name, avatar_url."""
        result: dict[str, list[dict]] = {eid: [] for eid in root_event_ids}
        if not root_event_ids:
            return result
        placeholders = ",".join(f"${i + 1}" for i in range(len(root_event_ids)))
        rows = await self.database.fetch(
            f"SELECT thread_root, sender, sender_name, avatar_url FROM ("
            f"  SELECT thread_root, sender,"
            f"         MIN(sender_name) AS sender_name,"
            f"         MIN(avatar_url) AS avatar_url,"
            f"         MIN(timestamp) AS first_ts,"
            f"         ROW_NUMBER() OVER ("
            f"           PARTITION BY thread_root ORDER BY MIN(timestamp) ASC"
            f"         ) AS rn"
            f"  FROM messages"
            f"  WHERE thread_root IN ({placeholders}) AND redacted=FALSE"
            f"  GROUP BY thread_root, sender"
            f") s WHERE rn <= ${len(root_event_ids) + 1} "
            f"ORDER BY thread_root, first_ts ASC",
            *root_event_ids, per_root_limit,
        )
        for row in rows:
            result[row["thread_root"]].append({
                "sender": row["sender"],
                "sender_name": row["sender_name"] or row["sender"],
                "avatar_url": row["avatar_url"] or "",
            })
        return result

    async def _get_post(self, event_id: str) -> dict | None:
        row = await self.database.fetchrow(
            "SELECT * FROM messages WHERE event_id=$1 AND redacted=FALSE AND published=TRUE",
            event_id,
        )
        return dict(row) if row else None

    async def _get_post_internal(self, event_id: str) -> dict | None:
        row = await self.database.fetchrow(
            "SELECT * FROM messages WHERE event_id=$1 AND redacted=FALSE",
            event_id,
        )
        return dict(row) if row else None

    async def _publish_post(self, event_id: str) -> None:
        await self.database.execute(
            "UPDATE messages SET published=TRUE WHERE event_id=$1", event_id
        )

    async def _unpublish_post(self, event_id: str) -> None:
        await self.database.execute(
            "UPDATE messages SET published=FALSE WHERE event_id=$1", event_id
        )

    async def _has_privileged_publish_reaction(
        self, room_id: str, target_id: str,
    ) -> bool:
        """True iff at least one non-redacted 📰 reaction on target_id has a
        sender whose current power level meets journal_author_pl."""
        rows = await self.database.fetch(
            "SELECT DISTINCT sender FROM reactions "
            "WHERE target_event_id=$1 AND key=$2 AND redacted=FALSE",
            target_id, "📰",
        )
        if not rows:
            return False
        author_pl = self._get_room_config(room_id, "journal_author_pl")
        for row in rows:
            user_level = await self._get_effective_power_level(room_id, row["sender"])
            if user_level >= author_pl:
                return True
        return False

    async def _store_tags(self, event_id: str, room_id: str, body: str) -> None:
        tags = parse_hashtags(body)
        await self.database.execute("DELETE FROM post_tags WHERE event_id=$1", event_id)
        for tag in tags:
            await self.database.execute(
                "INSERT INTO post_tags (event_id, room_id, tag) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                event_id, room_id, tag,
            )

    async def _get_tag_counts(self, room_id: str) -> list[tuple[str, int]]:
        rows = await self.database.fetch(
            "SELECT pt.tag, COUNT(*) AS cnt FROM post_tags pt "
            "JOIN messages m ON m.event_id = pt.event_id "
            "WHERE pt.room_id=$1 AND m.published=TRUE AND m.redacted=FALSE "
            "GROUP BY pt.tag ORDER BY cnt DESC, pt.tag ASC",
            room_id,
        )
        return [(r["tag"], r["cnt"]) for r in rows]

    async def _get_posts_by_tag(
        self, room_id: str, tag: str, page: int, per_page: int,
    ) -> tuple[list[dict], int]:
        total = await self.database.fetchval(
            "SELECT COUNT(*) FROM post_tags pt "
            "JOIN messages m ON m.event_id = pt.event_id "
            "WHERE pt.room_id=$1 AND pt.tag=$2 AND m.published=TRUE AND m.redacted=FALSE AND m.thread_root IS NULL",
            room_id, tag,
        ) or 0
        total_pages = max(1, (total + per_page - 1) // per_page)
        offset = (page - 1) * per_page
        rows = await self.database.fetch(
            "SELECT m.* FROM messages m "
            "JOIN post_tags pt ON pt.event_id = m.event_id "
            "WHERE pt.room_id=$1 AND pt.tag=$2 AND m.published=TRUE AND m.redacted=FALSE AND m.thread_root IS NULL "
            "ORDER BY m.timestamp DESC LIMIT $3 OFFSET $4",
            room_id, tag, per_page, offset,
        )
        return [dict(r) for r in rows], total_pages

    async def _get_tags_for_posts(self, event_ids: list[str]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {eid: [] for eid in event_ids}
        if not event_ids:
            return result
        placeholders = ",".join(f"${i + 1}" for i in range(len(event_ids)))
        rows = await self.database.fetch(
            f"SELECT event_id, tag FROM post_tags WHERE event_id IN ({placeholders}) ORDER BY tag",
            *event_ids,
        )
        for row in rows:
            result[row["event_id"]].append(row["tag"])
        return result

    async def _get_tags_for_post(self, event_id: str) -> list[str]:
        rows = await self.database.fetch(
            "SELECT tag FROM post_tags WHERE event_id=$1 ORDER BY tag", event_id
        )
        return [r["tag"] for r in rows]

    async def _get_thread_comments(self, thread_root: str) -> list[dict]:
        rows = await self.database.fetch(
            "SELECT * FROM messages "
            "WHERE thread_root=$1 AND redacted=FALSE ORDER BY timestamp ASC",
            thread_root,
        )
        return [dict(r) for r in rows]

    async def _get_messages_after_event(
        self, room_id: str, after_event_id: str,
    ) -> list[dict]:
        ref = await self.database.fetchrow(
            "SELECT timestamp FROM messages WHERE event_id=$1", after_event_id
        )
        if not ref:
            return []
        rows = await self.database.fetch(
            "SELECT * FROM messages "
            "WHERE room_id=$1 AND timestamp>$2 AND redacted=FALSE "
            "ORDER BY timestamp ASC",
            room_id, ref["timestamp"],
        )
        return [dict(r) for r in rows]

    async def _store_reaction(
        self, reaction_event_id: str, target_event_id: str, room_id: str,
        sender: str, key: str, timestamp: int,
    ) -> None:
        await self.database.execute(
            "INSERT INTO reactions "
            "(reaction_event_id, target_event_id, room_id, sender, key, timestamp) "
            "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (reaction_event_id) DO NOTHING",
            reaction_event_id, target_event_id, room_id, sender, key, timestamp,
        )

    async def _mark_reaction_redacted(
        self, reaction_event_id: str,
    ) -> tuple[str, str, str] | None:
        row = await self.database.fetchrow(
            "SELECT target_event_id, room_id, key FROM reactions "
            "WHERE reaction_event_id=$1 AND redacted=FALSE",
            reaction_event_id,
        )
        if not row:
            return None
        await self.database.execute(
            "UPDATE reactions SET redacted=TRUE WHERE reaction_event_id=$1",
            reaction_event_id,
        )
        return (row["target_event_id"], row["room_id"], row["key"])

    async def _get_reactions_for_events(
        self, event_ids: list[str],
    ) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {eid: [] for eid in event_ids}
        if not event_ids:
            return result
        placeholders = ",".join(f"${i + 1}" for i in range(len(event_ids)))
        rows = await self.database.fetch(
            f"SELECT target_event_id, key, sender FROM reactions "
            f"WHERE target_event_id IN ({placeholders}) AND redacted=FALSE "
            f"ORDER BY timestamp",
            *event_ids,
        )
        # Group in Python so the query works on both asyncpg and aiosqlite
        # (ARRAY_AGG / ANY(...::text[]) are Postgres-only).
        grouped: dict[str, dict[str, list[str]]] = {}
        for row in rows:
            by_key = grouped.setdefault(row["target_event_id"], {})
            senders = by_key.setdefault(row["key"], [])
            if row["sender"] not in senders:
                senders.append(row["sender"])
        for tid, by_key in grouped.items():
            items = [
                {"key": k, "count": len(senders), "senders": senders}
                for k, senders in by_key.items()
            ]
            items.sort(key=lambda x: (-x["count"], x["key"]))
            result[tid] = items
        return result

    def _should_hide_publish_emoji(self, room_id: str, msg: dict) -> bool:
        """📰 on a top-level journal post is a control signal when
        `journal_emoji_publish` is on — hide it from the reactions UI."""
        info = self._published.get(room_id) or {}
        if info.get("mode") != "journal":
            return False
        if not self._get_room_config(room_id, "journal_emoji_publish"):
            return False
        return msg.get("thread_root") is None

    async def _apply_reactions_to_messages(
        self, room_id: str, messages: list[dict],
    ) -> None:
        """Hydrate msg['reactions'] for each message. Strips 📰 on top-level
        journal posts when `journal_emoji_publish` is enabled (control signal)."""
        if not messages:
            return
        event_ids = [m["event_id"] for m in messages]
        reactions = await self._get_reactions_for_events(event_ids)
        for m in messages:
            lst = reactions.get(m["event_id"], [])
            if lst and self._should_hide_publish_emoji(room_id, m):
                lst = [r for r in lst if r["key"] != "📰"]
            m["reactions"] = lst

    async def _reactions_html_for_event(
        self, room_id: str, event_id: str,
    ) -> str:
        target = await self.database.fetchrow(
            "SELECT event_id, thread_root FROM messages WHERE event_id=$1",
            event_id,
        )
        target_dict = dict(target) if target else {"event_id": event_id, "thread_root": None}
        reactions = (await self._get_reactions_for_events([event_id])).get(event_id, [])
        if reactions and self._should_hide_publish_emoji(room_id, target_dict):
            reactions = [r for r in reactions if r["key"] != "📰"]
        return render_reactions_html(reactions, self._homeserver_url(), self._base_url)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_sender_name(self, room_id: str, sender: str) -> str:
        if sender in self._display_names:
            return self._display_names[sender]
        try:
            member = await self.client.get_state_event(
                room_id, EventType.ROOM_MEMBER, sender
            )
            name = member.displayname or sender
            avatar = str(member.avatar_url) if getattr(member, "avatar_url", None) else ""
        except Exception:
            name = sender
            avatar = ""
        self._display_names[sender] = name
        self._avatar_urls[sender] = avatar
        return name

    async def _get_room_name(self, room_id: str) -> str:
        try:
            ev = await self.client.get_state_event(room_id, EventType.ROOM_NAME)
            return ev.name or ""
        except Exception:
            return ""

    async def _get_room_topic(self, room_id: str) -> str:
        try:
            ev = await self.client.get_state_event(room_id, EventType.ROOM_TOPIC)
            return ev.topic or ""
        except Exception:
            return ""

    async def _get_room_avatar(self, room_id: str) -> str:
        if room_id in self._room_avatars:
            return self._room_avatars[room_id]
        try:
            content = await self.client.api.request(
                Method.GET,
                Path.v3.rooms[room_id].state["m.room.avatar"],
            )
            url = content.get("url", "") if isinstance(content, dict) else ""
        except Exception as e:
            self.log.debug(f"Could not fetch room avatar for {room_id}: {e}")
            url = ""
        self._room_avatars[room_id] = url
        return url

    async def _get_pinned_events(self, room_id: str) -> list[str]:
        try:
            content = await self.client.api.request(
                Method.GET,
                Path.v3.rooms[room_id].state["m.room.pinned_events"],
            )
            pinned = content.get("pinned", []) if isinstance(content, dict) else []
            return [str(e) for e in pinned if isinstance(e, str)]
        except Exception as e:
            self.log.debug(f"Could not fetch pinned events for {room_id}: {e}")
            return []

    def _launch_migration(self, old_room_id: str, replacement_room_id: str) -> None:
        """Fire-and-forget wrapper so asyncio.create_task exceptions don't get
        silently swallowed."""
        task = asyncio.create_task(
            self._auto_migrate_to_replacement(old_room_id, replacement_room_id)
        )

        def _log_errors(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                self.log.error(
                    f"Migrate: background task {old_room_id} -> {replacement_room_id} "
                    f"raised {exc!r}",
                    exc_info=exc,
                )

        task.add_done_callback(_log_errors)

    async def _auto_migrate_to_replacement(
        self, old_room_id: str, replacement_room_id: str,
    ) -> bool:
        """Follow an `m.room.tombstone` forward link: join the replacement,
        take over the old room's alias for it, and archive the old publication
        under a room-id-based URL. Returns True iff the migration completed.

        Safe to call repeatedly — short-circuits if the replacement is already
        published, and a per-old-room guard prevents concurrent duplicates."""
        if old_room_id not in self._published:
            self.log.debug(f"Migrate: old {old_room_id} not published, skipping")
            return False
        if not isinstance(replacement_room_id, str) or not replacement_room_id.startswith("!"):
            self.log.debug(f"Migrate: invalid replacement {replacement_room_id!r}")
            return False
        if replacement_room_id == old_room_id:
            return False
        if replacement_room_id in self._published:
            self.log.info(
                f"Migrate: {replacement_room_id} already published; no-op"
            )
            return False
        if old_room_id in self._migrating:
            self.log.debug(f"Migrate: {old_room_id} already migrating, skipping")
            return False

        self._migrating.add(old_room_id)
        try:
            return await self._auto_migrate_to_replacement_inner(
                old_room_id, replacement_room_id,
            )
        except Exception as e:
            self.log.exception(
                f"Migrate: unhandled error for {old_room_id} -> {replacement_room_id}: {e}"
            )
            return False
        finally:
            self._migrating.discard(old_room_id)

    async def _auto_migrate_to_replacement_inner(
        self, old_room_id: str, replacement_room_id: str,
    ) -> bool:
        old_info = self._published[old_room_id]
        mode = old_info["mode"]
        preserved_alias = old_info["alias"]
        self.log.info(
            f"Migrate: start {old_room_id} -> {replacement_room_id} "
            f"(mode={mode}, preserved_alias={preserved_alias!r})"
        )

        # Attempt join. /join accepts a pending invite if one exists; fails
        # otherwise. Errors here aren't fatal — we verify access next.
        try:
            await self.client.api.request(
                Method.POST,
                Path.v3.join[replacement_room_id],
                content={},
            )
            self.log.info(f"Migrate: joined {replacement_room_id}")
        except Exception as e:
            self.log.info(
                f"Migrate: /join {replacement_room_id} errored (may already be "
                f"joined; verifying): {e!r}"
            )

        # Verify visibility. Fetching m.room.create is the cheapest read a
        # joined member can do; if it fails, bot can't see the room.
        try:
            await self.client.api.request(
                Method.GET,
                Path.v3.rooms[replacement_room_id].state["m.room.create"],
            )
        except Exception as e:
            self.log.warning(
                f"Migrate: no access to {replacement_room_id} (from "
                f"{old_room_id}): {e!r}. Bot needs an invite. Will retry on "
                f"the next invite / restart. Manual: `!webpublish migrate` in "
                f"the old room once the bot is invited."
            )
            return False
        self.log.debug(f"Migrate: {replacement_room_id} accessible")

        # Prefer the replacement's own canonical alias; else reuse the old
        # alias (typically the upgrade reassigns the alias to the new room).
        new_alias = preserved_alias
        try:
            ca = await self.client.api.request(
                Method.GET,
                Path.v3.rooms[replacement_room_id].state["m.room.canonical_alias"],
            )
            if isinstance(ca, dict):
                a = ca.get("alias")
                if isinstance(a, str) and a.startswith("#"):
                    new_alias = a.lstrip("#")
                    self.log.info(
                        f"Migrate: new room's canonical alias is {a}, using {new_alias!r}"
                    )
        except Exception as e:
            self.log.debug(
                f"Migrate: no canonical alias on {replacement_room_id}; "
                f"keeping preserved={preserved_alias!r}: {e!r}"
            )

        # Alias collision with another published room (edge case): fall back
        # to room-id URL so we don't silently overwrite.
        colliding = self._alias_to_room.get(new_alias)
        if colliding and colliding != old_room_id and colliding != replacement_room_id:
            self.log.warning(
                f"Migrate: alias {new_alias!r} is claimed by {colliding}; "
                f"using the replacement's room id as its URL instead"
            )
            new_alias = replacement_room_id

        await self._archive_publication(old_room_id, old_room_id)
        self.log.info(f"Migrate: archived {old_room_id} -> /{old_room_id}/")
        await self._set_published(replacement_room_id, mode, new_alias)
        try:
            self._room_config_overrides[replacement_room_id] = await self._fetch_room_overrides(replacement_room_id)
        except Exception as e:
            self.log.debug(f"Migrate: overrides fetch failed: {e!r}")
            self._room_config_overrides[replacement_room_id] = {}
        self._pinned_events[replacement_room_id] = await self._get_pinned_events(replacement_room_id)

        new_succession = await self._fetch_room_succession(replacement_room_id)
        if not new_succession.get("predecessor_room"):
            new_succession["predecessor_room"] = old_room_id
        self._room_succession[replacement_room_id] = new_succession

        self.log.info(
            f"Migrate: complete. {old_room_id} archived; "
            f"{replacement_room_id} now serves /{new_alias}/"
        )
        # Refresh the OLD room's banner so open tabs update the "View Current
        # Content" link from matrix: URI to the internal path.
        old_banner = self._render_succession_banner_for_room(old_room_id)
        self._notify_sse(old_room_id, "succession_changed", {"banner_html": old_banner})

        self._launch_backfill(replacement_room_id)
        return True

    def _render_succession_banner_for_room(self, room_id: str) -> str:
        succession = self._room_succession.get(room_id)
        if not succession:
            return ""
        rooms_map = {
            rid: {"alias": info.get("alias", "")}
            for rid, info in self._published.items()
        }
        # Alias hints for external (unpublished) targets. When the replacement
        # room isn't yet published AND this room's stored alias is a human
        # alias (not an archived room-id), we pass the human alias through as
        # a hint — the Matrix alias server typically moves the alias to the
        # new room during an upgrade, so `matrix:r/<old_alias>` resolves to
        # the replacement. Better than a bare `matrix:roomid/...` which most
        # clients won't open without via hints.
        hints: dict[str, str] = {}
        my_info = self._published.get(room_id) or {}
        my_alias = my_info.get("alias", "")
        my_alias_is_human = my_alias and not my_alias.startswith("!")
        repl = succession.get("replacement_room") or ""
        if repl and repl not in self._published and my_alias_is_human:
            hints[repl] = my_alias
        return render_succession_banner(
            succession, rooms_map, self._base_url or "", hints,
            homeserver_url=self._homeserver_url(),
        )

    async def _fetch_room_succession(self, room_id: str) -> dict:
        """Look up m.room.tombstone and m.room.create to drive the footer
        banner. Called on plugin start and after a room is published. The
        live `handle_tombstone_state` handler refreshes this on state changes
        and is responsible for capturing `tombstone_ts`."""
        result = {
            "has_tombstone": False,
            "replacement_room": "",
            "tombstone_ts": None,
            "predecessor_room": None,
        }
        try:
            tomb = await self.client.api.request(
                Method.GET,
                Path.v3.rooms[room_id].state["m.room.tombstone"],
            )
            if isinstance(tomb, dict):
                result["has_tombstone"] = True
                repl = tomb.get("replacement_room", "")
                result["replacement_room"] = repl if isinstance(repl, str) else ""
        except Exception as e:
            self.log.debug(f"No tombstone for {room_id}: {e}")
        try:
            create = await self.client.api.request(
                Method.GET,
                Path.v3.rooms[room_id].state["m.room.create"],
            )
            if isinstance(create, dict):
                predecessor = create.get("predecessor") or {}
                if isinstance(predecessor, dict):
                    pred_rid = predecessor.get("room_id")
                    if isinstance(pred_rid, str) and pred_rid:
                        result["predecessor_room"] = pred_rid
        except Exception as e:
            self.log.debug(f"Could not fetch m.room.create for {room_id}: {e}")
        return result

    async def _hydrate_pinned_messages(self, room_id: str) -> list[dict]:
        """Return DB rows for pinned event_ids that exist locally and are
        non-redacted, preserving pin order. Missing ids are skipped."""
        pinned_ids = self._pinned_events.get(room_id, [])
        if not pinned_ids:
            return []
        placeholders = ",".join(f"${i + 2}" for i in range(len(pinned_ids)))
        rows = await self.database.fetch(
            f"SELECT * FROM messages "
            f"WHERE room_id=$1 AND redacted=FALSE AND event_id IN ({placeholders})",
            room_id, *pinned_ids,
        )
        by_id = {r["event_id"]: dict(r) for r in rows}
        return [by_id[eid] for eid in pinned_ids if eid in by_id]

    async def _hydrate_chat_pinned_for_banner(
        self, room_id: str,
    ) -> list[dict]:
        """Chat banner needs a row per pinned id, whether we have it locally
        or not. Missing rows get `in_db=False` and a placeholder sender."""
        pinned_ids = self._pinned_events.get(room_id, [])
        if not pinned_ids:
            return []
        hydrated = await self._hydrate_pinned_messages(room_id)
        by_id = {m["event_id"]: m for m in hydrated}
        result: list[dict] = []
        for eid in pinned_ids:
            if eid in by_id:
                m = dict(by_id[eid])
                m["in_db"] = True
                result.append(m)
            else:
                result.append({
                    "event_id": eid,
                    "sender": "",
                    "sender_name": "",
                    "body": "",
                    "in_db": False,
                })
        return result

    async def _build_pinned_payload(
        self, room_id: str, mode: str | None,
    ) -> dict | None:
        if mode == "chat":
            pinned = await self._hydrate_chat_pinned_for_banner(room_id)
            banner = render_pinned_banner_html(
                pinned, room_id, self._homeserver_url(),
            )
            return {"banner_html": banner}
        if mode == "journal":
            pinned = await self._hydrate_pinned_messages(room_id)
            # Only include published top-level posts.
            pinned = [
                m for m in pinned
                if m.get("thread_root") is None and m.get("published")
            ]
            counts: dict[str, int] = {}
            if pinned:
                eids = [m["event_id"] for m in pinned]
                counts = await self._get_comment_counts(eids)
                tags = await self._get_tags_for_posts(eids)
                for m in pinned:
                    m["tags"] = tags.get(m["event_id"], [])
            info = self._published.get(room_id) or {}
            alias = info.get("alias", "")
            encoded = "" if alias == "/" else alias
            html = render_pinned_section_html(
                pinned, encoded, counts, self._base_url,
            )
            return {"html": html}
        return None

    async def _get_room_create_info(self, room_id: str) -> tuple[int, set[str]]:
        """Return (room_version, set_of_creator_mxids) for the room, cached."""
        if room_id in self._room_create_cache:
            return self._room_create_cache[room_id]

        result: tuple[int, set[str]] = (1, set())
        try:
            create_content = await self.client.get_state_event(
                room_id, EventType.ROOM_CREATE
            )
            ver_str = getattr(create_content, "room_version", None) or "1"
            try:
                room_version = int(ver_str)
            except (ValueError, TypeError):
                room_version = 1

            creators: set[str] = set()

            # Primary creator field (present in v1–v10 and v12+)
            creator = getattr(create_content, "creator", None)
            if creator:
                creators.add(str(creator))

            # additional_creators introduced in v12
            for ac in (getattr(create_content, "additional_creators", None) or []):
                creators.add(str(ac))

            # If creator field is absent (removed in v11), fall back to the event sender
            if not creators:
                all_state = await self.client.api.request(
                    Method.GET, Path.v3.rooms[room_id].state
                )
                for evt_raw in (all_state if isinstance(all_state, list) else []):
                    if (
                        evt_raw.get("type") == "m.room.create"
                        and evt_raw.get("state_key") == ""
                    ):
                        sender = evt_raw.get("sender")
                        if sender:
                            creators.add(sender)
                        break

            result = (room_version, creators)
        except Exception:
            pass

        self._room_create_cache[room_id] = result
        return result

    async def _get_effective_power_level(self, room_id: str, user_id: str) -> int:
        """Return the user's power level, granting implicit infinite power to v12+ creators."""
        try:
            levels = await self.client.get_state_event(
                room_id, EventType.ROOM_POWER_LEVELS
            )
            user_level = levels.get_user_level(user_id)
        except Exception:
            user_level = 0

        room_version, creators = await self._get_room_create_info(room_id)
        if room_version >= 12 and str(user_id) in creators:
            return 2**31 - 1  # effectively infinite per Matrix room v12 spec

        return user_level

    def _get_room_config(self, room_id: str | None, key: str) -> Any:
        """Return the effective config value for a room, preferring per-room overrides."""
        if room_id:
            overrides = self._room_config_overrides.get(room_id)
            if overrides and key in overrides:
                return overrides[key]
        return self.config[key]

    async def _fetch_room_overrides(self, room_id: str) -> dict[str, Any]:
        try:
            content = await self.client.api.request(
                Method.GET,
                Path.v3.rooms[room_id].state[CONFIG_STATE_TYPE],
            )
        except Exception as e:
            self.log.debug(f"no room config overrides for {room_id}: {e}")
            return {}
        return content if isinstance(content, dict) else {}

    async def _put_room_overrides(self, room_id: str, overrides: dict[str, Any]) -> None:
        await self.client.send_state_event(
            room_id,
            EventType.find(CONFIG_STATE_TYPE, t_class=EventType.Class.STATE),
            overrides,
        )

    async def _check_power_level(self, evt: MessageEvent) -> bool:
        """Return True if the sender meets the configured min_power_level."""
        required = self._get_room_config(evt.room_id, "min_power_level")
        user_level = await self._get_effective_power_level(evt.room_id, str(evt.sender))
        if user_level < required:
            await evt.reply(
                f"You need a power level of at least {required} to use this command."
            )
            return False
        return True

    @property
    def _base_url(self) -> str:
        base = self.config.get("base_url", "")
        if base:
            return base.rstrip("/")
        try:
            return str(self.webapp_url).rstrip("/")
        except Exception:
            return f"/_matrix/maubot/plugin/{self.id}"

    def _homeserver_url(self) -> str:
        return str(self.client.api.base_url).rstrip("/")

    async def _enrich_reply_context(self, messages: list[dict]) -> None:
        """Add reply_to_sender / reply_to_body / reply_to_element_id fields."""
        by_id = {m["event_id"]: m for m in messages}
        # collect targets not already in the message set
        missing: set[str] = set()
        for m in messages:
            target = m.get("reply_to") or m.get("thread_root")
            if target and target not in by_id:
                missing.add(target)
        for mid in missing:
            row = await self.database.fetchrow(
                "SELECT event_id, sender, sender_name, body FROM messages "
                "WHERE event_id=$1", mid,
            )
            if row:
                by_id[mid] = dict(row)
        for m in messages:
            target_id = m.get("reply_to") or m.get("thread_root")
            if not target_id or target_id not in by_id:
                continue
            target = by_id[target_id]
            m["reply_to_sender"] = target.get("sender_name") or target.get("sender", "")
            m["reply_to_mxid"] = target.get("sender", "")
            m["reply_to_body"] = (target.get("body") or "")[:80]
            m["reply_to_element_id"] = safe_element_id(target_id)

    async def _enrich_single_reply(self, msg: dict) -> None:
        """Enrich a single message dict with reply context (for SSE)."""
        target_id = msg.get("reply_to") or msg.get("thread_root")
        if not target_id:
            return
        row = await self.database.fetchrow(
            "SELECT sender, sender_name, body FROM messages WHERE event_id=$1",
            target_id,
        )
        if not row:
            return
        msg["reply_to_sender"] = row["sender_name"] or row["sender"]
        msg["reply_to_mxid"] = row["sender"]
        msg["reply_to_body"] = (row["body"] or "")[:80]
        msg["reply_to_element_id"] = safe_element_id(target_id)

    def _notify_sse(self, room_id: str, event_type: str, data: dict) -> None:
        for q in self._sse_queues.get(room_id, set()).copy():
            try:
                q.put_nowait({"type": event_type, "data": data})
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @command.new(name="webpublish", require_subcommand=True)
    async def webpublish(self, evt: MessageEvent) -> None:
        pass

    @webpublish.subcommand(help="Publish this room as a chat view")
    async def chat(self, evt: MessageEvent) -> None:
        if not await self._check_power_level(evt):
            return
        await self._publish_room(evt, "chat")

    @webpublish.subcommand(help="Publish this room as a journal / blog")
    async def journal(self, evt: MessageEvent) -> None:
        if not await self._check_power_level(evt):
            return
        await self._publish_room(evt, "journal")

    @webpublish.subcommand(help="Wipe stored messages and re-backfill from scratch")
    async def rebuild(self, evt: MessageEvent) -> None:
        if not await self._check_power_level(evt):
            return
        if evt.room_id not in self._published:
            await evt.reply("This room is not currently published.")
            return
        if evt.room_id in self._backfilling:
            await evt.reply("A backfill is already in progress for this room.")
            return
        await self.database.execute("DELETE FROM messages WHERE room_id = $1", evt.room_id)
        await self._clear_backfill_progress(evt.room_id)
        await evt.reply("Message history cleared. Rebuilding from scratch…")
        self._launch_backfill(evt.room_id)

    @webpublish.subcommand(help="Stop publishing this room")
    async def disable(self, evt: MessageEvent) -> None:
        if not await self._check_power_level(evt):
            return
        if evt.room_id not in self._published:
            await evt.reply("This room is not currently published.")
            return
        await self._remove_published(evt.room_id)
        await evt.reply("Room publishing disabled.")

    @webpublish.subcommand(help="Get the published URL for this room")
    async def link(self, evt: MessageEvent) -> None:
        info = self._published.get(evt.room_id)
        if not info:
            await evt.reply("This room is not published.")
            return
        alias = info["alias"]
        url = f"{self._base_url}/" if alias == "/" else f"{self._base_url}/{alias}"
        await evt.reply(f"Published ({info['mode']} mode): {url}")

    @webpublish.subcommand("setpath", aliases=["seturi"], help="Override the URL path for this room's published site (omit argument or pass \"\" to revert to default)")
    @command.argument("path", pass_raw=True, required=False)
    async def setpath(self, evt: MessageEvent, path: str) -> None:
        if not await self._check_power_level(evt):
            return
        info = self._published.get(evt.room_id)
        if not info:
            await evt.reply("This room is not published. Use `!webpublish chat` or `!webpublish journal` first.")
            return
        path = (path or "").strip()
        if path in ("''", '""'):
            path = ""
        if path == "":
            default_alias = info.get("default_alias") or info["alias"]
            if info["alias"] == default_alias:
                await evt.reply("No custom path override is set for this room.")
                return
            await self._set_published(evt.room_id, info["mode"], default_alias)
            url = f"{self._base_url}/" if default_alias == "/" else f"{self._base_url}/{default_alias}"
            await evt.reply(f"Path override removed. Site now available at {url}")
            return
        if path == "/":
            alias = "/"
        else:
            alias = path.strip("/")
            if not re.match(r'^[a-zA-Z0-9_\-]+$', alias):
                await evt.reply("Invalid path. Use only letters, numbers, hyphens, and underscores.")
                return
            if alias in RESERVED_ALIASES:
                await evt.reply(
                    f"`{alias}` is reserved for internal routes. Choose a different path."
                )
                return
        existing_owner = self._alias_to_room.get(alias)
        if existing_owner and existing_owner != evt.room_id:
            await evt.reply("That path is already in use by another room.")
            return
        await self._set_published(evt.room_id, info["mode"], alias)
        url = f"{self._base_url}/" if alias == "/" else f"{self._base_url}/{alias}"
        await evt.reply(f"Path updated! Site now available at {url}")

    @webpublish.subcommand("config", help="Set or view per-room config overrides")
    @command.argument("args", pass_raw=True, required=False)
    async def config_cmd(self, evt: MessageEvent, args: str) -> None:
        if not await self._check_power_level(evt):
            return
        args = (args or "").strip()
        overrides = self._room_config_overrides.get(evt.room_id, {})

        if not args:
            lines = []
            for k in sorted(OVERRIDABLE_CONFIG):
                if k in overrides:
                    lines.append(f"- `{k}` *(override)*: `{json.dumps(overrides[k])}`")
                else:
                    lines.append(f"- `{k}`: `{json.dumps(self.config[k])}` (default)")
            body = (
                "Per-room config values (effective for this room):\n"
                + "\n".join(lines)
                + "\n\nSet with `!webpublish config <key> <value>`. "
                "Clear an override with `!webpublish config <key> \"\"`."
            )
            await evt.reply(body)
            return

        parts = args.split(None, 1)
        key = parts[0]

        if key not in OVERRIDABLE_CONFIG:
            valid = ", ".join(sorted(OVERRIDABLE_CONFIG))
            await evt.reply(f"Unknown config key `{key}`. Overridable keys: {valid}.")
            return

        if len(parts) == 1:
            if key in overrides:
                await evt.reply(f"`{key}` (override): `{json.dumps(overrides[key])}`")
            else:
                default = self.config[key]
                await evt.reply(
                    f"`{key}`: not overridden (using bot default: `{json.dumps(default)}`)"
                )
            return

        raw_value = parts[1].strip()
        new_overrides = dict(overrides)

        if raw_value in ("", '""', "''"):
            if key not in new_overrides:
                await evt.reply(f"No override to clear for `{key}`.")
                return
            new_overrides.pop(key)
            try:
                await self._put_room_overrides(evt.room_id, new_overrides)
            except Exception as e:
                await evt.reply(f"Failed to update room state: {e}")
                return
            self._room_config_overrides[evt.room_id] = new_overrides
            await evt.reply(f"Cleared override for `{key}`; reverted to bot default.")
            return

        parser = OVERRIDABLE_CONFIG[key]
        try:
            value = parser(raw_value)
        except Exception as e:
            await evt.reply(f"Invalid value for `{key}`: {e}")
            return

        new_overrides[key] = value
        try:
            await self._put_room_overrides(evt.room_id, new_overrides)
        except Exception as e:
            await evt.reply(f"Failed to update room state: {e}")
            return
        self._room_config_overrides[evt.room_id] = new_overrides
        await evt.reply(f"Set `{key}` = `{json.dumps(value)}` for this room.")

    async def _publish_room(self, evt: MessageEvent, mode: str) -> None:
        room_id = evt.room_id

        # refuse encrypted rooms
        try:
            enc = await self.client.get_state_event(
                room_id, EventType.ROOM_ENCRYPTION
            )
            if enc:
                await evt.reply(
                    "This bot cannot publish encrypted rooms."
                )
                return
        except Exception:
            pass  # no encryption state = not encrypted

        existing = self._published.get(room_id)
        if existing:
            alias_str = existing["alias"]
        else:
            try:
                alias_evt = await self.client.get_state_event(
                    room_id, EventType.ROOM_CANONICAL_ALIAS
                )
                alias = alias_evt.canonical_alias
                if not alias:
                    raise ValueError("empty alias")
            except Exception:
                await evt.reply(
                    "This room must have a canonical alias set before publishing."
                )
                return
            alias_str = str(alias).lstrip("#")

        await self._set_published(room_id, mode, alias_str)
        self._pinned_events[room_id] = await self._get_pinned_events(room_id)
        self._room_succession[room_id] = await self._fetch_room_succession(room_id)

        url = f"{self._base_url}/" if alias_str == "/" else f"{self._base_url}/{alias_str}"
        await evt.reply(f"Room published in **{mode}** mode!\n\n{url}")

        # backfill in background
        self._launch_backfill(room_id)

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------

    # Sleep between /messages requests during a crawl. Keeps the homeserver
    # responsive for real client traffic when a room has years of history.
    _BACKFILL_BATCH_SLEEP: float = 1.0

    async def _load_backfill_progress(self, room_id: str) -> dict | None:
        row = await self.database.fetchrow(
            "SELECT end_token, status, total FROM backfill_progress WHERE room_id=$1",
            room_id,
        )
        if not row:
            return None
        return {"end_token": row["end_token"], "status": row["status"], "total": row["total"]}

    async def _save_backfill_progress(
        self, room_id: str, end_token: str | None, status: str, total: int,
    ) -> None:
        await self.database.execute(
            "INSERT INTO backfill_progress (room_id, end_token, status, total, updated_at) "
            "VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (room_id) DO UPDATE SET "
            "end_token=EXCLUDED.end_token, status=EXCLUDED.status, "
            "total=EXCLUDED.total, updated_at=EXCLUDED.updated_at",
            room_id, end_token, status, total, int(time.time() * 1000),
        )

    async def _clear_backfill_progress(self, room_id: str) -> None:
        await self.database.execute(
            "DELETE FROM backfill_progress WHERE room_id=$1", room_id,
        )

    async def _seed_backfill_token_from_context(self, room_id: str) -> str | None:
        """If the room already has stored messages but no checkpoint, use the
        oldest known event as a seed so we don't re-walk events handled live."""
        oldest = await self.database.fetchval(
            "SELECT event_id FROM messages WHERE room_id=$1 "
            "ORDER BY timestamp ASC LIMIT 1",
            room_id,
        )
        if not oldest:
            return None
        try:
            content = await self.client.api.request(
                Method.GET,
                Path.v3.rooms[room_id].context[oldest],
                query_params={"limit": "1"},
            )
        except Exception as e:
            self.log.debug(f"Context seed failed for {room_id}/{oldest}: {e}")
            return None
        if isinstance(content, dict):
            return content.get("start") or None
        return None

    async def _backfill_room(self, room_id: str) -> None:
        if room_id in self._backfilling:
            return
        self._backfilling.add(room_id)
        try:
            async with self._backfill_semaphore:
                await self._backfill_room_inner(room_id)
        finally:
            self._backfilling.discard(room_id)

    async def _backfill_room_inner(self, room_id: str) -> None:
        max_msgs = self._get_room_config(room_id, "max_backfill")
        try:
            batch_size = int(self._get_room_config(room_id, "backfill_batch_size"))
        except (TypeError, ValueError):
            batch_size = 50
        batch_size = max(1, min(batch_size, 1000))
        unlimited = max_msgs is None or max_msgs <= 0

        info = self._published.get(room_id, {})
        is_journal_emoji = (
            info.get("mode") == "journal"
            and self._get_room_config(room_id, "journal_emoji_publish")
        )

        # Resume from checkpoint if we have one.
        progress = await self._load_backfill_progress(room_id)
        if progress and progress["status"] == "exhausted":
            self.log.info(f"Backfill: {room_id} already exhausted, nothing to do")
            return
        if progress:
            end_token: str | None = progress["end_token"]
            total = int(progress["total"] or 0)
        else:
            # No checkpoint: if the room already has stored history (live events),
            # skip past it via /context. Otherwise start at the live edge.
            end_token = await self._seed_backfill_token_from_context(room_id)
            total = 0

        self.log.info(
            f"Backfill starting for {room_id}: mode={info.get('mode')} "
            f"max={'unlimited' if unlimited else max_msgs} batch_size={batch_size} "
            f"resume={'yes' if progress else 'no'} seeded={'yes' if end_token else 'no'}"
        )

        try:
            while unlimited or total < max_msgs:
                if unlimited:
                    batch = batch_size
                else:
                    batch = min(batch_size, max_msgs - total)
                qp: dict[str, str] = {"dir": "b", "limit": str(batch)}
                if end_token:
                    qp["from"] = end_token

                try:
                    content = await self.client.api.request(
                        Method.GET,
                        Path.v3.rooms[room_id].messages,
                        query_params=qp,
                    )
                except Exception as e:
                    self.log.warning(f"Backfill request failed for {room_id}: {e}")
                    await self._save_backfill_progress(room_id, end_token, "error", total)
                    return

                chunk = content.get("chunk", [])

                # Per-batch pending state. Flushed before the next batch so
                # memory doesn't grow unbounded during a long crawl.
                pending_reactions: list[tuple[str, str, str, str, int]] = []
                pending_edits: dict[str, tuple[str, str | None]] = {}
                batch_added = 0

                for raw in chunk:
                    if raw.get("type") == "m.reaction":
                        if raw.get("unsigned", {}).get("redacted_because"):
                            continue
                        rc = (raw.get("content") or {}).get("m.relates_to") or {}
                        if rc.get("rel_type") != "m.annotation":
                            continue
                        rkey = rc.get("key", "") or ""
                        rtarget = rc.get("event_id", "") or ""
                        rsender = raw.get("sender", "") or ""
                        reid = raw.get("event_id", "") or ""
                        if rkey and rtarget and rsender and reid:
                            pending_reactions.append((
                                reid, rtarget, rsender, rkey,
                                raw.get("origin_server_ts", 0),
                            ))
                        continue

                    raw_type = raw.get("type")
                    if raw_type not in ("m.room.message", "m.sticker"):
                        continue
                    if raw.get("unsigned", {}).get("redacted_because"):
                        continue
                    c = raw.get("content", {})
                    relates = c.get("m.relates_to") or {}

                    if raw_type == "m.room.message" and relates.get("rel_type") == "m.replace":
                        target_event_id = relates.get("event_id", "")
                        if target_event_id and target_event_id not in pending_edits:
                            new_content = c.get("m.new_content") or {}
                            pending_edits[target_event_id] = (
                                new_content.get("body", ""),
                                new_content.get("formatted_body"),
                            )
                        continue
                    # skip bot messages
                    sender = raw.get("sender", "")
                    if sender == self.client.mxid:
                        continue
                    body = c.get("body", "")
                    if raw_type == "m.room.message" and body.startswith("!webpublish"):
                        continue

                    thread_root = None
                    if relates.get("rel_type") == "m.thread":
                        thread_root = relates.get("event_id")

                    # extract reply-to target; is_falling_back means in_reply_to is a
                    # threading compatibility fallback, not a genuine explicit reply
                    in_reply_to = None
                    if not relates.get("is_falling_back"):
                        in_reply_to = (relates.get("m.in_reply_to") or {}).get("event_id")

                    sender_name = await self._get_sender_name(room_id, sender)
                    sender_avatar = self._avatar_urls.get(sender, "") or None
                    if raw_type == "m.sticker":
                        backfill_msgtype = "m.sticker"
                    else:
                        backfill_msgtype = c.get("msgtype", "m.text")
                    backfill_geo_uri = None
                    if backfill_msgtype == "m.location":
                        backfill_geo_uri = (
                            (c.get("org.matrix.msc3488.location") or {}).get("uri")
                            or c.get("geo_uri")
                        )
                    if backfill_msgtype == "m.sticker":
                        backfill_published = not (
                            info.get("mode") == "journal" and thread_root is None
                        )
                    else:
                        backfill_published = not (is_journal_emoji and thread_root is None)
                    await self._store_message(
                        room_id=room_id,
                        event_id=raw["event_id"],
                        sender=sender,
                        sender_name=sender_name,
                        body=body,
                        formatted_body=c.get("formatted_body"),
                        msgtype=backfill_msgtype,
                        media_url=c.get("url"),
                        timestamp=raw.get("origin_server_ts", 0),
                        thread_root=thread_root,
                        reply_to=in_reply_to,
                        geo_uri=backfill_geo_uri,
                        avatar_url=sender_avatar,
                        published=backfill_published,
                    )
                    if info.get("mode") == "journal" and thread_root is None:
                        await self._store_tags(raw["event_id"], room_id, body)
                    total += 1
                    batch_added += 1

                # Flush this batch's reactions before moving on.
                for reid, target_id, rsender, rkey, rts in pending_reactions:
                    await self._store_reaction(
                        reaction_event_id=reid,
                        target_event_id=target_id,
                        room_id=room_id,
                        sender=rsender,
                        key=rkey,
                        timestamp=rts,
                    )

                # 📰 publish-gate pass for this batch.
                if pending_reactions and is_journal_emoji:
                    author_pl = self._get_room_config(room_id, "journal_author_pl")
                    for _reid, target_id, rsender, rkey, _rts in pending_reactions:
                        if rkey != "📰":
                            continue
                        if not target_id or not rsender:
                            continue
                        user_level = await self._get_effective_power_level(room_id, rsender)
                        if user_level < author_pl:
                            continue
                        post = await self._get_post_internal(target_id)
                        if not post or post["room_id"] != room_id or post["thread_root"] is not None:
                            continue
                        await self._publish_post(target_id)

                # Apply pending edits for this batch.
                for target_event_id, (new_body, new_formatted_body) in pending_edits.items():
                    await self._update_message_edit(target_event_id, new_body, new_formatted_body)
                    if info.get("mode") == "journal":
                        thread_root_val = await self.database.fetchval(
                            "SELECT thread_root FROM messages WHERE event_id=$1", target_event_id
                        )
                        if thread_root_val is None and new_body:
                            await self._store_tags(target_event_id, room_id, new_body)

                # The token we paginated from this batch; used to detect a
                # non-advancing token (runaway guard) below.
                prev_token = end_token
                next_token = content.get("end")
                # Persist progress now that this batch is fully in the DB.
                await self._save_backfill_progress(room_id, next_token, "running", total)

                # Terminal condition: the homeserver signals end-of-history by
                # omitting the `end` token. An empty `chunk` is NOT terminal —
                # Synapse can return chunk:[] with a valid `end` while paginating
                # across sparse windows (state events, lazy backfill boundaries),
                # so we must keep paging until `end` disappears. The
                # `next_token == prev_token` clause guards against an infinite
                # loop if the server keeps handing back the same token.
                if not next_token or next_token == prev_token:
                    await self._save_backfill_progress(room_id, next_token, "exhausted", total)
                    self.log.info(
                        f"Backfill exhausted for {room_id}: {total} messages stored"
                    )
                    return
                end_token = next_token

                if not unlimited and total >= max_msgs:
                    await self._save_backfill_progress(room_id, end_token, "capped", total)
                    self.log.info(
                        f"Backfill capped for {room_id}: {total} messages (max_backfill={max_msgs})"
                    )
                    return

                await asyncio.sleep(self._BACKFILL_BATCH_SLEEP)

        except asyncio.CancelledError:
            await self._save_backfill_progress(room_id, end_token, "running", total)
            self.log.info(f"Backfill cancelled for {room_id} at {total} messages")
            raise
        except Exception as e:
            self.log.error(f"Backfill error for {room_id}: {e}")
            await self._save_backfill_progress(room_id, end_token, "error", total)

    # ------------------------------------------------------------------
    # Live event handlers
    # ------------------------------------------------------------------

    @event.on(EventType.ROOM_MESSAGE)
    async def handle_message(self, evt: MessageEvent) -> None:
        if evt.room_id not in self._published:
            return

        content = evt.content
        body = content.body or ""
        if body.startswith("!webpublish"):
            return

        # --- edit ---
        relates = content.relates_to
        rel_type = ""
        if relates:
            rt = getattr(relates, "rel_type", None)
            rel_type = rt.value if hasattr(rt, "value") else str(rt or "")

        if rel_type == "m.replace":
            target_id = str(relates.event_id)
            new_body = content.body or body
            new_fmt = getattr(content, "formatted_body", None)
            await self._update_message_edit(target_id, new_body, new_fmt)
            info = self._published[evt.room_id]
            if info["mode"] == "journal":
                thread_root_val = await self.database.fetchval(
                    "SELECT thread_root FROM messages WHERE event_id=$1", target_id
                )
                if thread_root_val is None and new_body:
                    await self._store_tags(target_id, evt.room_id, new_body)

            body_html = render_body(
                {"body": new_body, "formatted_body": new_fmt, "redacted": False,
                 "msgtype": str(content.msgtype or "m.text")},
                self._homeserver_url(),
                self._base_url,
            )
            self._notify_sse(evt.room_id, "edit_message", {
                "element_id": safe_element_id(target_id),
                "body_html": body_html,
            })
            return

        # --- thread & reply detection ---
        thread_root = None
        if rel_type == "m.thread":
            thread_root = str(relates.event_id)

        reply_to = None
        if relates and not relates.is_falling_back:
            irt = getattr(relates, "in_reply_to", None)
            if irt:
                irt_id = getattr(irt, "event_id", None)
                reply_to = str(irt_id) if irt_id else None

        # --- journal author check ---
        info = self._published[evt.room_id]
        if info["mode"] == "journal" and thread_root is None:
            if str(content.msgtype) == "m.notice":
                return
            if self._get_room_config(evt.room_id, "journal_enforce_messages"):
                author_pl = self._get_room_config(evt.room_id, "journal_author_pl")
                user_level = await self._get_effective_power_level(
                    evt.room_id, str(evt.sender)
                )
                if user_level < author_pl:
                    try:
                        await self.client.redact(
                            evt.room_id, evt.event_id,
                            reason="Non-author top-level message in journal room",
                        )
                    except Exception:
                        await evt.reply(
                            "This message was not published to the journal site because "
                            "you are not an author of this journal (requires power level "
                            f"{author_pl}). It should be redacted, but the bot lacks "
                            "permission to do so — please redact it manually."
                        )
                    return

        # --- chat enforcement (parallels journal rule; live-only, like journal) ---
        if info["mode"] == "chat" and thread_root is None:
            if self._get_room_config(evt.room_id, "chat_enforce_messages"):
                author_pl = self._get_room_config(evt.room_id, "chat_author_pl")
                user_level = await self._get_effective_power_level(
                    evt.room_id, str(evt.sender)
                )
                if user_level < author_pl:
                    try:
                        await self.client.redact(
                            evt.room_id, evt.event_id,
                            reason="Top-level chat message below chat_author_pl",
                        )
                    except Exception:
                        await evt.reply(
                            "Top-level messages in this room require power level "
                            f"{author_pl}. Reply in a thread to participate, or ask "
                            "an admin to raise your level. This message should be "
                            "redacted but the bot lacks permission — please redact "
                            "it manually."
                        )
                    return

        sender_name = await self._get_sender_name(evt.room_id, str(evt.sender))
        sender_avatar = self._avatar_urls.get(str(evt.sender), "") or None
        msgtype = str(content.msgtype) if content.msgtype else "m.text"
        media_url = str(content.url) if getattr(content, "url", None) else None
        formatted_body = (
            content.formatted_body
            if getattr(content, "formatted_body", None)
            else None
        )
        geo_uri = None
        if msgtype == "m.location":
            geo_uri = (
                (content.get("org.matrix.msc3488.location") or {}).get("uri")
                or content.get("geo_uri")
            )
            if geo_uri:
                geo_uri = str(geo_uri)

        published = not (
            info["mode"] == "journal"
            and thread_root is None
            and self._get_room_config(evt.room_id, "journal_emoji_publish")
        )

        await self._store_message(
            room_id=evt.room_id,
            event_id=str(evt.event_id),
            sender=str(evt.sender),
            sender_name=sender_name,
            body=body,
            formatted_body=formatted_body,
            msgtype=msgtype,
            media_url=media_url,
            timestamp=evt.timestamp,
            thread_root=thread_root,
            reply_to=reply_to,
            geo_uri=geo_uri,
            published=published,
            avatar_url=sender_avatar,
        )
        if info["mode"] == "journal" and thread_root is None:
            await self._store_tags(str(evt.event_id), evt.room_id, body)

        # SSE notification
        hs = self._homeserver_url()
        msg_dict = {
            "event_id": str(evt.event_id),
            "room_id": evt.room_id,
            "sender": str(evt.sender),
            "sender_name": sender_name,
            "body": body,
            "formatted_body": formatted_body,
            "msgtype": msgtype,
            "media_url": media_url,
            "timestamp": evt.timestamp,
            "thread_root": thread_root,
            "reply_to": reply_to,
            "geo_uri": geo_uri,
            "edited": False,
            "redacted": False,
            "avatar_url": sender_avatar,
        }
        await self._enrich_single_reply(msg_dict)

        base = self._base_url
        if info["mode"] == "chat":
            if thread_root:
                count_map = await self._get_comment_counts([thread_root])
                count = count_map.get(thread_root, 0)
                parts_map = await self._get_thread_participants([thread_root], 6)
                indicator_html = render_thread_indicator_html(
                    thread_root, count, parts_map.get(thread_root, []), hs, base,
                )
                reply_html = render_message_html(
                    msg_dict, hs, base, show_reply_header=bool(reply_to),
                )
                self._notify_sse(evt.room_id, "thread_reply", {
                    "thread_root": thread_root,
                    "root_element_id": safe_element_id(thread_root),
                    "count": count,
                    "indicator_html": indicator_html,
                    "reply_html": reply_html,
                    "reply_element_id": safe_element_id(str(evt.event_id)),
                    "event_id": str(evt.event_id),
                })
            else:
                html = render_message_html(msg_dict, hs, base)
                self._notify_sse(evt.room_id, "new_message", {
                    "html": html, "event_id": str(evt.event_id),
                })
        else:  # journal
            if thread_root:
                html = render_message_html(msg_dict, hs, base, show_reply_header=bool(reply_to))
                self._notify_sse(evt.room_id, "new_message", {
                    "html": html, "is_thread": True,
                    "thread_root": thread_root,
                    "event_id": str(evt.event_id),
                })
            elif published:
                msg_dict["tags"] = parse_hashtags(body)
                html = render_post_preview_html(msg_dict, info["alias"], 0)
                self._notify_sse(evt.room_id, "new_message", {
                    "html": html, "is_thread": False,
                    "event_id": str(evt.event_id),
                })

    @event.on(EventType.STICKER)
    async def handle_sticker(self, evt: MessageEvent) -> None:
        if evt.room_id not in self._published:
            return

        content = evt.content
        body = content.body or ""
        media_url = str(content.url) if getattr(content, "url", None) else None

        relates = content.relates_to
        thread_root = None
        reply_to = None
        if relates:
            rt = getattr(relates, "rel_type", None)
            rel_type = rt.value if hasattr(rt, "value") else str(rt or "")
            if rel_type == "m.thread":
                thread_root = str(relates.event_id)
            if not relates.is_falling_back:
                irt = getattr(relates, "in_reply_to", None)
                if irt:
                    irt_id = getattr(irt, "event_id", None)
                    reply_to = str(irt_id) if irt_id else None

        info = self._published[evt.room_id]
        sender_name = await self._get_sender_name(evt.room_id, str(evt.sender))
        sender_avatar = self._avatar_urls.get(str(evt.sender), "") or None

        # Stickers never auto-publish as top-level journal posts; they can
        # only become visible via a privileged 📰 reaction, which itself
        # requires journal_emoji_publish to be on.
        published = not (info["mode"] == "journal" and thread_root is None)

        await self._store_message(
            room_id=evt.room_id,
            event_id=str(evt.event_id),
            sender=str(evt.sender),
            sender_name=sender_name,
            body=body,
            formatted_body=None,
            msgtype="m.sticker",
            media_url=media_url,
            timestamp=evt.timestamp,
            thread_root=thread_root,
            reply_to=reply_to,
            geo_uri=None,
            published=published,
            avatar_url=sender_avatar,
        )
        if info["mode"] == "journal" and thread_root is None:
            await self._store_tags(str(evt.event_id), evt.room_id, body)

        hs = self._homeserver_url()
        msg_dict = {
            "event_id": str(evt.event_id),
            "room_id": evt.room_id,
            "sender": str(evt.sender),
            "sender_name": sender_name,
            "body": body,
            "formatted_body": None,
            "msgtype": "m.sticker",
            "media_url": media_url,
            "timestamp": evt.timestamp,
            "thread_root": thread_root,
            "reply_to": reply_to,
            "geo_uri": None,
            "edited": False,
            "redacted": False,
            "avatar_url": sender_avatar,
        }
        await self._enrich_single_reply(msg_dict)

        base = self._base_url
        if info["mode"] == "chat":
            if thread_root:
                count_map = await self._get_comment_counts([thread_root])
                count = count_map.get(thread_root, 0)
                parts_map = await self._get_thread_participants([thread_root], 6)
                indicator_html = render_thread_indicator_html(
                    thread_root, count, parts_map.get(thread_root, []), hs, base,
                )
                reply_html = render_message_html(
                    msg_dict, hs, base, show_reply_header=bool(reply_to),
                )
                self._notify_sse(evt.room_id, "thread_reply", {
                    "thread_root": thread_root,
                    "root_element_id": safe_element_id(thread_root),
                    "count": count,
                    "indicator_html": indicator_html,
                    "reply_html": reply_html,
                    "reply_element_id": safe_element_id(str(evt.event_id)),
                    "event_id": str(evt.event_id),
                })
            else:
                html = render_message_html(msg_dict, hs, base)
                self._notify_sse(evt.room_id, "new_message", {
                    "html": html, "event_id": str(evt.event_id),
                })
        else:  # journal
            if thread_root:
                html = render_message_html(
                    msg_dict, hs, base, show_reply_header=bool(reply_to),
                )
                self._notify_sse(evt.room_id, "new_message", {
                    "html": html, "is_thread": True,
                    "thread_root": thread_root,
                    "event_id": str(evt.event_id),
                })
            elif published:
                msg_dict["tags"] = parse_hashtags(body)
                html = render_post_preview_html(msg_dict, info["alias"], 0)
                self._notify_sse(evt.room_id, "new_message", {
                    "html": html, "is_thread": False,
                    "event_id": str(evt.event_id),
                })

    @event.on(EventType.ROOM_REDACTION)
    async def handle_redaction(self, evt: MessageEvent) -> None:
        if evt.room_id not in self._published:
            return
        redacted_id = getattr(evt, "redacts", None)
        if not redacted_id:
            c = evt.content
            redacted_id = c.get("redacts") if hasattr(c, "get") else None
        if not redacted_id:
            return

        redacted_id = str(redacted_id)

        # Reaction redaction: a reaction event's own event_id is what Matrix
        # redacts. Check the reactions table first; if found, re-render the
        # target's reactions strip and stop — the id is not a message.
        reaction_entry = await self._mark_reaction_redacted(redacted_id)
        if reaction_entry is not None:
            target_id, target_room, reaction_key = reaction_entry
            if target_room != evt.room_id:
                return
            reactions_html = await self._reactions_html_for_event(
                evt.room_id, target_id,
            )
            self._notify_sse(evt.room_id, "reaction_removed", {
                "event_id": target_id,
                "element_id": safe_element_id(target_id),
                "reactions_html": reactions_html,
            })
            # Unpublish if the last privileged 📰 reaction has been redacted.
            if reaction_key == "📰":
                info = self._published.get(evt.room_id)
                emoji_publish = self._get_room_config(
                    evt.room_id, "journal_emoji_publish",
                )
                if info and info["mode"] == "journal" and emoji_publish:
                    target = await self._get_post_internal(target_id)
                    if (
                        target
                        and target["thread_root"] is None
                        and target["published"]
                        and not await self._has_privileged_publish_reaction(
                            evt.room_id, target_id,
                        )
                    ):
                        await self._unpublish_post(target_id)
                        self._notify_sse(evt.room_id, "post_unpublished", {
                            "element_id": safe_element_id(target_id),
                            "event_id": target_id,
                        })
            return

        # Capture thread_root before the row is deleted so we can update the
        # indicator on the root message if a threaded reply was redacted.
        row = await self.database.fetchrow(
            "SELECT thread_root FROM messages WHERE event_id=$1", redacted_id,
        )
        thread_root = row["thread_root"] if row else None

        await self._mark_redacted(redacted_id)
        self._notify_sse(evt.room_id, "redact_message", {
            "element_id": safe_element_id(redacted_id),
        })

        info = self._published.get(evt.room_id)
        if info and info["mode"] == "chat" and thread_root:
            count_map = await self._get_comment_counts([thread_root])
            count = count_map.get(thread_root, 0)
            parts_map = await self._get_thread_participants([thread_root], 6)
            hs = self._homeserver_url()
            indicator_html = render_thread_indicator_html(
                thread_root, count, parts_map.get(thread_root, []),
                hs, self._base_url,
            )
            self._notify_sse(evt.room_id, "thread_reply_removed", {
                "thread_root": thread_root,
                "root_element_id": safe_element_id(thread_root),
                "count": count,
                "indicator_html": indicator_html,
                "removed_element_id": safe_element_id(redacted_id),
            })

    @event.on(EventType.REACTION)
    async def handle_reaction(self, evt: MessageEvent) -> None:
        if evt.room_id not in self._published:
            return
        info = self._published[evt.room_id]

        relates = evt.content.relates_to
        if not relates or str(getattr(relates, "rel_type", "")) != "m.annotation":
            return
        key = getattr(relates, "key", "") or ""
        target_id = str(getattr(relates, "event_id", "") or "")
        if not key or not target_id:
            return

        # Look up the target message once — used by both the publish-gate and
        # the reactions-UI paths.
        target = await self._get_post_internal(target_id)
        if not target or target["room_id"] != evt.room_id:
            return

        # Persist every reaction. Publish-signal events are stored too; the
        # renderer filters them out via _should_hide_publish_emoji.
        await self._store_reaction(
            reaction_event_id=str(evt.event_id),
            target_event_id=target_id,
            room_id=evt.room_id,
            sender=str(evt.sender),
            key=key,
            timestamp=evt.timestamp,
        )

        journal_emoji_publish = self._get_room_config(
            evt.room_id, "journal_emoji_publish",
        )
        is_publish_signal = (
            info["mode"] == "journal"
            and journal_emoji_publish
            and key == "📰"
            and target["thread_root"] is None
        )

        if is_publish_signal:
            author_pl = self._get_room_config(evt.room_id, "journal_author_pl")
            user_level = await self._get_effective_power_level(
                evt.room_id, str(evt.sender),
            )
            if user_level >= author_pl and not target["published"]:
                await self._publish_post(target_id)
                comment_count = await self.database.fetchval(
                    "SELECT COUNT(*) FROM messages "
                    "WHERE thread_root=$1 AND redacted=FALSE",
                    target_id,
                ) or 0
                # Re-fetch so we emit the post in its post-publish state.
                published_post = await self._get_post_internal(target_id) or target
                html = render_post_preview_html(
                    published_post, info["alias"], comment_count,
                )
                self._notify_sse(evt.room_id, "new_message", {
                    "html": html, "is_thread": False,
                    "event_id": target_id,
                })
            # Publish-signal reactions are not surfaced as UI reactions.
            return

        reactions_html = await self._reactions_html_for_event(evt.room_id, target_id)
        self._notify_sse(evt.room_id, "reaction_added", {
            "event_id": target_id,
            "element_id": safe_element_id(target_id),
            "reactions_html": reactions_html,
        })

    @event.on(EventType.find(CONFIG_STATE_TYPE, t_class=EventType.Class.STATE))
    async def handle_config_state(self, evt) -> None:
        # State-key must be empty for our override event; ignore anything else.
        if getattr(evt, "state_key", "") != "":
            return
        content = evt.content
        if hasattr(content, "serialize"):
            content = content.serialize()
        if not isinstance(content, dict):
            content = {}
        self._room_config_overrides[evt.room_id] = dict(content)

    @event.on(EventType.find("m.room.pinned_events", t_class=EventType.Class.STATE))
    async def handle_pinned_state(self, evt) -> None:
        if evt.room_id not in self._published:
            return
        if getattr(evt, "state_key", "") != "":
            return
        content = evt.content
        if hasattr(content, "serialize"):
            content = content.serialize()
        pinned = content.get("pinned", []) if isinstance(content, dict) else []
        self._pinned_events[evt.room_id] = [
            str(e) for e in pinned if isinstance(e, str)
        ]
        info = self._published.get(evt.room_id) or {}
        payload = await self._build_pinned_payload(evt.room_id, info.get("mode"))
        if payload is not None:
            self._notify_sse(evt.room_id, "pinned_changed", payload)

    @event.on(EventType.find("m.room.tombstone", t_class=EventType.Class.STATE))
    async def handle_tombstone_state(self, evt) -> None:
        if evt.room_id not in self._published:
            return
        if getattr(evt, "state_key", "") != "":
            return
        content = evt.content
        if hasattr(content, "serialize"):
            content = content.serialize()
        succession = dict(self._room_succession.get(evt.room_id) or {
            "has_tombstone": False,
            "replacement_room": "",
            "tombstone_ts": None,
            "predecessor_room": None,
        })
        if isinstance(content, dict):
            succession["has_tombstone"] = True
            repl = content.get("replacement_room", "")
            succession["replacement_room"] = repl if isinstance(repl, str) else ""
            ts = getattr(evt, "timestamp", None) or getattr(evt, "origin_server_ts", None)
            if ts is not None:
                try:
                    succession["tombstone_ts"] = int(ts)
                except (TypeError, ValueError):
                    pass
        self._room_succession[evt.room_id] = succession
        banner_html = self._render_succession_banner_for_room(evt.room_id)
        self._notify_sse(evt.room_id, "succession_changed", {"banner_html": banner_html})
        replacement = succession.get("replacement_room") or ""
        self.log.info(
            f"Tombstone state fired on {evt.room_id}: replacement="
            f"{replacement!r} (published={evt.room_id in self._published})"
        )
        if replacement:
            self._launch_migration(evt.room_id, replacement)

    @event.on(EventType.find("m.room.member", t_class=EventType.Class.STATE))
    async def handle_member_state(self, evt) -> None:
        """When the bot is invited to a room whose `m.room.create.predecessor`
        points to a published room, complete the migration (join + publish +
        archive). Covers the race where the tombstone handler fires before
        the invite has propagated to the bot's sync."""
        try:
            bot_mxid = self.client.mxid
        except Exception:
            return
        if getattr(evt, "state_key", "") != bot_mxid:
            return
        content = evt.content
        if hasattr(content, "serialize"):
            content = content.serialize()
        if not isinstance(content, dict):
            return
        if content.get("membership") != "invite":
            return
        new_room_id = evt.room_id
        if new_room_id in self._published:
            return
        # Look up predecessor on the invited room. State API works on invited
        # rooms for key state events via the /state endpoint.
        try:
            create_content = await self.client.api.request(
                Method.GET,
                Path.v3.rooms[new_room_id].state["m.room.create"],
            )
        except Exception as e:
            self.log.debug(
                f"Invite to {new_room_id}: cannot read m.room.create yet: {e!r}"
            )
            return
        if not isinstance(create_content, dict):
            return
        predecessor = create_content.get("predecessor") or {}
        if not isinstance(predecessor, dict):
            return
        old_room_id = predecessor.get("room_id", "")
        if not isinstance(old_room_id, str) or not old_room_id:
            return
        if old_room_id not in self._published:
            return
        self.log.info(
            f"Invite: {new_room_id} has predecessor {old_room_id} which is "
            f"published; launching migration"
        )
        self._launch_migration(old_room_id, new_room_id)

    @event.on(EventType.find("m.room.avatar", t_class=EventType.Class.STATE))
    async def handle_room_avatar_state(self, evt) -> None:
        if evt.room_id not in self._published:
            return
        if getattr(evt, "state_key", "") != "":
            return
        content = evt.content
        if hasattr(content, "serialize"):
            content = content.serialize()
        url = content.get("url", "") if isinstance(content, dict) else ""
        self._room_avatars[evt.room_id] = url if isinstance(url, str) else ""

    # ------------------------------------------------------------------
    # Web handlers
    # ------------------------------------------------------------------

    def _media_cache_get(self, cache_key: str) -> tuple[str, bytes] | None:
        if cache_key in self._media_cache:
            self._media_cache.move_to_end(cache_key)
            return self._media_cache[cache_key]
        return None

    def _media_cache_put(self, cache_key: str, value: tuple[str, bytes]) -> None:
        if len(self._media_cache) >= self._media_cache_max:
            self._media_cache.popitem(last=False)
        self._media_cache[cache_key] = value

    async def _fetch_tile(self, z: int, x: int, y: int) -> bytes | None:
        cache_key = f"{z}/{x}/{y}"
        if cache_key in self._tile_cache:
            self._tile_cache.move_to_end(cache_key)
            return self._tile_cache[cache_key]
        url = f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        try:
            async with self.client.api.session.get(
                url, headers={"User-Agent": "maubot-webpublish/1.0 (matrix bot tile proxy)"}
            ) as resp:
                if resp.status != 200:
                    return None
                body = await resp.read()
        except Exception as e:
            self.log.warning(f"Tile proxy fetch failed for {cache_key}: {e}")
            return None
        if len(self._tile_cache) >= self._tile_cache_max:
            self._tile_cache.popitem(last=False)
        self._tile_cache[cache_key] = body
        return body

    @web.get("/tiles/{z}/{x}/{y}.png")
    async def web_tile_proxy(self, req: Request) -> Response:
        try:
            z = int(req.match_info["z"])
            x = int(req.match_info["x"])
            y = int(req.match_info["y"])
        except (ValueError, KeyError):
            return Response(status=400, text="Invalid tile coordinates")
        if not (0 <= z <= 19 and 0 <= x < 2**z and 0 <= y < 2**z):
            return Response(status=400, text="Tile coordinates out of range")
        body = await self._fetch_tile(z, x, y)
        if body is None:
            return Response(status=502, text="Upstream tile unavailable")
        return Response(
            body=body,
            content_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @web.get("/media/{server_name}/{media_id}")
    async def web_media_proxy(self, req: Request) -> StreamResponse:
        server_name = req.match_info["server_name"]
        media_id = req.match_info["media_id"]
        cache_key = f"{server_name}/{media_id}"

        cached = self._media_cache_get(cache_key)
        if cached is not None:
            content_type, body = cached
            return Response(body=body, content_type=content_type,
                            headers={"Cache-Control": _MEDIA_CACHE_CONTROL})

        hs = self._homeserver_url()
        url = f"{hs}/_matrix/media/v3/download/{cache_key}"
        try:
            upstream_cm = self.client.api.session.get(
                url, headers={"Authorization": f"Bearer {self.client.api.token}"}
            )
            async with upstream_cm as upstream:
                if upstream.status != 200:
                    return Response(status=404, text="Media not found")
                content_type = upstream.content_type or "application/octet-stream"
                clen = upstream.content_length

                # Small/known-size: buffer + cache, as before.
                if clen is not None and clen <= _MEDIA_STREAM_THRESHOLD:
                    body = await upstream.read()
                    self._media_cache_put(cache_key, (content_type, body))
                    return Response(body=body, content_type=content_type,
                                    headers={"Cache-Control": _MEDIA_CACHE_CONTROL})

                # Large or unknown-size: stream chunk-by-chunk, skip the cache.
                stream = StreamResponse(
                    headers={"Cache-Control": _MEDIA_CACHE_CONTROL}
                )
                stream.content_type = content_type
                if clen is not None:
                    stream.content_length = clen
                await stream.prepare(req)
                try:
                    async for chunk in upstream.content.iter_chunked(64 * 1024):
                        await stream.write(chunk)
                    await stream.write_eof()
                except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
                    pass
                return stream
        except Exception as e:
            self.log.warning(f"Media proxy fetch failed for {cache_key}: {e}")
            return Response(status=404, text="Media not found")

    @web.get("/theme/{name}.css")
    async def web_theme_css(self, req: Request) -> Response:
        from .templates import THEMES
        name = req.match_info["name"]
        css = THEMES.get(name)
        if css is None:
            return Response(status=404, text="Theme not found")
        return Response(text=css, content_type="text/css",
                        headers={"Cache-Control": "public, max-age=86400"})

    @web.get("/{alias}/feed.xml")
    async def web_feed_xml(self, req: Request) -> Response:
        return await self._handle_feed(req, unquote(req.match_info["alias"]))

    @web.get("/feed.xml")
    async def web_feed_xml_root(self, req: Request) -> Response:
        return await self._handle_feed(req, "/")

    async def _handle_feed(self, req: Request, alias: str) -> Response:
        room_id = self._alias_to_room.get(alias)
        if not room_id:
            return Response(status=404, text="Room not found")
        info = self._published.get(room_id, {})
        if info.get("mode") != "journal":
            return Response(status=404, text="Not a journal room")
        posts, _ = await self._get_posts(room_id, page=1, per_page=20)
        room_name = await self._get_room_name(room_id) or alias
        room_topic = await self._get_room_topic(room_id)
        encoded = "" if alias == "/" else alias
        xml = render_atom_feed(room_name, room_topic, posts, encoded, self._base_url, self._homeserver_url())
        return Response(text=xml, content_type="application/atom+xml",
                        headers={"Cache-Control": "public, max-age=300"})

    @web.get("/{alias}/tags")
    async def web_tag_index(self, req: Request) -> Response:
        return await self._handle_tag_index(req, unquote(req.match_info["alias"]))

    @web.get("/tags")
    async def web_all_tags_root(self, req: Request) -> Response:
        return await self._handle_tag_index(req, "/")

    async def _handle_tag_index(self, req: Request, alias: str) -> Response:
        room_id = self._alias_to_room.get(alias)
        if not room_id:
            return Response(status=404, text="Room not found")
        info = self._published.get(room_id, {})
        if info.get("mode") != "journal":
            return Response(status=404, text="Not a journal room")
        room_name = await self._get_room_name(room_id) or alias
        room_avatar_url = await self._get_room_avatar(room_id)
        tags = await self._get_tag_counts(room_id)
        encoded = "" if alias == "/" else alias
        html = render_tag_index_page(
            room_name, tags, encoded, self._get_room_config(room_id, "css"),
            self._base_url, room_avatar_url=room_avatar_url,
            head_html=self._get_room_config(room_id, "head_html"),
            body_html=self._get_room_config(room_id, "body_html"),
        )
        return Response(text=html, content_type="text/html",
                        headers={"Cache-Control": _HTML_CACHE_CONTROL})

    @web.get("/{alias}/tag/{name}")
    async def web_tag_filter(self, req: Request) -> Response:
        return await self._handle_tag_filter(
            req, unquote(req.match_info["alias"]), unquote(req.match_info["name"])
        )

    @web.get("/tag/{name}")
    async def web_tag_filter_root(self, req: Request) -> Response:
        return await self._handle_tag_filter(req, "/", unquote(req.match_info["name"]))

    async def _handle_tag_filter(self, req: Request, alias: str, tag: str) -> Response:
        room_id = self._alias_to_room.get(alias)
        if not room_id:
            return Response(status=404, text="Room not found")
        info = self._published.get(room_id, {})
        if info.get("mode") != "journal":
            return Response(status=404, text="Not a journal room")
        page = int(req.query.get("page", "1"))
        per_page = self._get_room_config(room_id, "pagination")
        posts, total_pages = await self._get_posts_by_tag(room_id, tag.lower(), page, per_page)
        eids = [p["event_id"] for p in posts]
        counts = await self._get_comment_counts(eids)
        post_tags = await self._get_tags_for_posts(eids)
        for post in posts:
            post["tags"] = post_tags.get(post["event_id"], [])
        room_name = await self._get_room_name(room_id) or alias
        room_avatar_url = await self._get_room_avatar(room_id)
        encoded = "" if alias == "/" else alias
        html = render_tag_filter_page(
            room_name, tag.lower(), posts, encoded, page, total_pages,
            self._get_room_config(room_id, "css"), counts, self._base_url,
            room_avatar_url=room_avatar_url,
            head_html=self._get_room_config(room_id, "head_html"),
            body_html=self._get_room_config(room_id, "body_html"),
        )
        return Response(text=html, content_type="text/html",
                        headers={"Cache-Control": _HTML_CACHE_CONTROL})

    @web.get("/{alias}")
    async def web_main(self, req: Request) -> Response:
        return await self._handle_main(req, unquote(req.match_info["alias"]))

    @web.get("/")
    async def web_main_root(self, req: Request) -> Response:
        return await self._handle_main(req, "/")

    async def _handle_main(self, req: Request, alias: str) -> Response:
        room_id = self._alias_to_room.get(alias)
        if not room_id:
            return Response(status=404, text="Room not found")
        target_alias = self._redirect_aliases.get(alias)
        if target_alias:
            location = f"{self._base_url}/" if target_alias == "/" else f"{self._base_url}/{target_alias}"
            return Response(status=302, headers={"Location": location})

        info = self._published[room_id]
        room_name = await self._get_room_name(room_id) or (alias if alias != "/" else "")
        room_topic = await self._get_room_topic(room_id)
        room_avatar_url = await self._get_room_avatar(room_id)
        hs = self._homeserver_url()
        css = self._get_room_config(room_id, "css")
        head_html = self._get_room_config(room_id, "head_html")
        body_html = self._get_room_config(room_id, "body_html")
        encoded = "" if alias == "/" else alias

        if info["mode"] == "chat":
            messages = await self._get_messages(
                room_id, limit=200, top_level_only=True,
            )
            await self._enrich_reply_context(messages)
            await self._apply_reactions_to_messages(room_id, messages)
            eids = [m["event_id"] for m in messages]
            comment_counts = await self._get_comment_counts(eids)
            thread_participants = await self._get_thread_participants(eids, 6)
            pinned_chat = await self._hydrate_chat_pinned_for_banner(room_id)
            pinned_banner = render_pinned_banner_html(
                pinned_chat, room_id, hs,
            )
            html = render_chat_page(
                room_name, room_topic, messages, encoded, css, hs, self._base_url,
                room_avatar_url=room_avatar_url,
                comment_counts=comment_counts,
                thread_participants=thread_participants,
                pinned_banner_html=pinned_banner,
                succession_banner_html=self._render_succession_banner_for_room(room_id),
                head_html=head_html,
                body_html=body_html,
            )
        else:
            page = int(req.query.get("page", "1"))
            per_page = self._get_room_config(room_id, "pagination")
            pinned_section_html = ""
            exclude: list[str] = []
            if page == 1:
                pinned_posts = await self._hydrate_pinned_messages(room_id)
                pinned_posts = [
                    p for p in pinned_posts
                    if p.get("thread_root") is None and p.get("published")
                ]
                if pinned_posts:
                    p_eids = [p["event_id"] for p in pinned_posts]
                    p_counts = await self._get_comment_counts(p_eids)
                    p_tags = await self._get_tags_for_posts(p_eids)
                    for p in pinned_posts:
                        p["tags"] = p_tags.get(p["event_id"], [])
                    pinned_section_html = render_pinned_section_html(
                        pinned_posts, encoded, p_counts, self._base_url,
                    )
                    exclude = p_eids
            posts, total_pages = await self._get_posts(
                room_id, page, per_page, exclude_event_ids=exclude,
            )
            eids = [p["event_id"] for p in posts]
            counts = await self._get_comment_counts(eids)
            post_tags = await self._get_tags_for_posts(eids)
            for post in posts:
                post["tags"] = post_tags.get(post["event_id"], [])
            html = render_journal_landing(
                room_name, room_topic, posts, encoded,
                page, total_pages, css, counts,
                base_url=self._base_url,
                room_avatar_url=room_avatar_url,
                pinned_section_html=pinned_section_html,
                succession_banner_html=self._render_succession_banner_for_room(room_id),
                head_html=head_html,
                body_html=body_html,
            )

        return Response(text=html, content_type="text/html",
                        headers={"Cache-Control": _HTML_CACHE_CONTROL})

    @web.get("/{alias}/post/{event_id}")
    async def web_post_detail(self, req: Request) -> Response:
        return await self._handle_post_detail(
            req, unquote(req.match_info["alias"]), unquote(req.match_info["event_id"])
        )

    @web.get("/post/{event_id}")
    async def web_post_detail_root(self, req: Request) -> Response:
        return await self._handle_post_detail(req, "/", unquote(req.match_info["event_id"]))

    async def _handle_post_detail(self, req: Request, alias: str, event_id: str) -> Response:
        room_id = self._alias_to_room.get(alias)
        if not room_id:
            return Response(status=404, text="Room not found")
        target_alias = self._redirect_aliases.get(alias)
        if target_alias:
            if target_alias == "/":
                location = f"{self._base_url}/post/{event_id}"
            else:
                location = f"{self._base_url}/{target_alias}/post/{event_id}"
            return Response(status=302, headers={"Location": location})

        post = await self._get_post(event_id)
        if not post or post["room_id"] != room_id:
            return Response(status=404, text="Post not found")

        comments = await self._get_thread_comments(event_id)
        await self._enrich_reply_context(comments)
        await self._apply_reactions_to_messages(room_id, [post, *comments])
        room_name = await self._get_room_name(room_id) or (alias if alias != "/" else "")
        room_topic = await self._get_room_topic(room_id)
        room_avatar_url = await self._get_room_avatar(room_id)
        hs = self._homeserver_url()
        css = self._get_room_config(room_id, "css")
        encoded = "" if alias == "/" else alias
        post["tags"] = await self._get_tags_for_post(event_id)

        html = render_journal_post(
            room_name, room_topic, post, comments, encoded, css, hs, self._base_url,
            room_avatar_url=room_avatar_url,
            head_html=self._get_room_config(room_id, "head_html"),
            body_html=self._get_room_config(room_id, "body_html"),
        )
        return Response(text=html, content_type="text/html",
                        headers={"Cache-Control": _HTML_CACHE_CONTROL})

    @web.get("/{alias}/thread/{event_id}")
    async def web_alias_thread(self, req: Request) -> Response:
        return await self._handle_thread_fragment(
            req, unquote(req.match_info["alias"]),
            unquote(req.match_info["event_id"]),
        )

    @web.get("/thread/{event_id}")
    async def web_alias_thread_root(self, req: Request) -> Response:
        return await self._handle_thread_fragment(
            req, "/", unquote(req.match_info["event_id"]),
        )

    async def _handle_thread_fragment(
        self, req: Request, alias: str, event_id: str,
    ) -> Response:
        room_id = self._alias_to_room.get(alias)
        if not room_id:
            return Response(status=404, text="Room not found")
        target_alias = self._redirect_aliases.get(alias)
        if target_alias:
            if target_alias == "/":
                location = f"{self._base_url}/thread/{event_id}"
            else:
                location = f"{self._base_url}/{target_alias}/thread/{event_id}"
            return Response(status=302, headers={"Location": location})

        info = self._published[room_id]
        if info["mode"] != "chat":
            return Response(status=404, text="Thread panel only available in chat mode")

        root = await self._get_post_internal(event_id)
        if not root or root["room_id"] != room_id or root["thread_root"] is not None:
            return Response(status=404, text="Thread root not found")

        comments = await self._get_thread_comments(event_id)
        await self._enrich_reply_context(comments)
        await self._apply_reactions_to_messages(room_id, [root, *comments])
        hs = self._homeserver_url()
        html = render_thread_panel_fragment(root, comments, hs, self._base_url)
        return Response(
            text=html, content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @web.get("/{alias}/older")
    async def web_alias_older(self, req: Request) -> Response:
        return await self._handle_older_fragment(
            req, unquote(req.match_info["alias"]),
        )

    @web.get("/older")
    async def web_alias_older_root(self, req: Request) -> Response:
        return await self._handle_older_fragment(req, "/")

    async def _handle_older_fragment(self, req: Request, alias: str) -> Response:
        room_id = self._alias_to_room.get(alias)
        if not room_id:
            return Response(status=404, text="Room not found")
        target_alias = self._redirect_aliases.get(alias)
        if target_alias:
            qs = f"?{req.query_string}" if req.query_string else ""
            if target_alias == "/":
                location = f"{self._base_url}/older{qs}"
            else:
                location = f"{self._base_url}/{target_alias}/older{qs}"
            return Response(status=302, headers={"Location": location})

        info = self._published[room_id]
        if info["mode"] != "chat":
            return Response(status=404, text="Load-older only available in chat mode")

        try:
            before = int(req.query.get("before", "0"))
        except (TypeError, ValueError):
            before = 0
        if before <= 0:
            return Response(text="", content_type="text/html",
                            headers={"Cache-Control": "no-store"})

        messages = await self._get_messages_before(
            room_id, before, _CHAT_OLDER_BATCH,
        )
        if not messages:
            return Response(text="", content_type="text/html",
                            headers={"Cache-Control": "no-store"})

        await self._enrich_reply_context(messages)
        await self._apply_reactions_to_messages(room_id, messages)
        eids = [m["event_id"] for m in messages]
        comment_counts = await self._get_comment_counts(eids)
        thread_participants = await self._get_thread_participants(eids, 6)
        hs = self._homeserver_url()

        chunks: list[str] = []
        for m in messages:
            eid = m["event_id"]
            count = comment_counts.get(eid, 0)
            indicator = render_thread_indicator_html(
                eid, count, thread_participants.get(eid, []), hs, self._base_url,
            ) if count else ""
            chunks.append(render_message_html(
                m, hs, self._base_url, thread_indicator_html=indicator,
            ))
        return Response(
            text="\n".join(chunks), content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @web.get("/{alias}/sse")
    async def web_sse(self, req: Request) -> StreamResponse:
        return await self._handle_sse(req, unquote(req.match_info["alias"]))

    @web.get("/sse")
    async def web_sse_root(self, req: Request) -> StreamResponse:
        return await self._handle_sse(req, "/")

    async def _handle_sse(self, req: Request, alias: str) -> StreamResponse:
        room_id = self._alias_to_room.get(alias)
        if not room_id:
            return Response(status=404, text="Room not found")
        target_alias = self._redirect_aliases.get(alias)
        if target_alias:
            location = f"{self._base_url}/sse" if target_alias == "/" else f"{self._base_url}/{target_alias}/sse"
            return Response(status=302, headers={"Location": location})

        resp = StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        await resp.prepare(req)

        # replay missed events on reconnect
        hs = self._homeserver_url()
        info = self._published.get(room_id, {})
        last_id = req.headers.get("Last-Event-ID")
        if last_id:
            missed = await self._get_messages_after_event(room_id, last_id)
            await self._apply_reactions_to_messages(room_id, missed)
            for msg in missed:
                thread_root = msg.get("thread_root")
                if info.get("mode") == "chat" and thread_root:
                    count_map = await self._get_comment_counts([thread_root])
                    count = count_map.get(thread_root, 0)
                    parts_map = await self._get_thread_participants([thread_root], 6)
                    indicator_html = render_thread_indicator_html(
                        thread_root, count, parts_map.get(thread_root, []),
                        hs, self._base_url,
                    )
                    reply_html = render_message_html(
                        msg, hs, self._base_url,
                        show_reply_header=bool(msg.get("reply_to")),
                    )
                    payload = json.dumps({
                        "thread_root": thread_root,
                        "root_element_id": safe_element_id(thread_root),
                        "count": count,
                        "indicator_html": indicator_html,
                        "reply_html": reply_html,
                        "reply_element_id": safe_element_id(msg["event_id"]),
                        "event_id": msg["event_id"],
                    })
                    await resp.write(
                        f"id: {msg['event_id']}\n"
                        f"event: thread_reply\n"
                        f"data: {payload}\n\n".encode()
                    )
                else:
                    html = render_message_html(msg, hs, self._base_url)
                    payload = json.dumps({"html": html, "event_id": msg["event_id"]})
                    await resp.write(
                        f"id: {msg['event_id']}\n"
                        f"event: new_message\n"
                        f"data: {payload}\n\n".encode()
                    )

        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._sse_queues.setdefault(room_id, set()).add(queue)

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                etype = item["type"]
                data = json.dumps(item["data"])
                eid = item["data"].get("event_id", "")
                id_line = f"id: {eid}\n" if eid else ""
                await resp.write(
                    f"{id_line}event: {etype}\ndata: {data}\n\n".encode()
                )
        except (asyncio.CancelledError, ConnectionResetError, ConnectionError):
            pass
        finally:
            self._sse_queues.get(room_id, set()).discard(queue)

        return resp
