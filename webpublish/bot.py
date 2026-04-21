from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from typing import Type
from urllib.parse import quote, unquote

from aiohttp.web import Request, Response, StreamResponse
from maubot import Plugin, MessageEvent
from maubot.handlers import command, event, web
from mautrix.api import Method, Path
from mautrix.types import EventType
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .db import upgrade_table
from .templates import (
    render_body,
    render_chat_page,
    render_journal_landing,
    render_journal_post,
    render_message_html,
    render_post_preview_html,
    safe_element_id,
)


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("css")
        helper.copy("pagination")
        helper.copy("max_backfill")
        helper.copy("base_url")
        helper.copy("min_power_level")
        helper.copy("journal_author_pl")


class WebPublishBot(Plugin):

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @classmethod
    def get_db_upgrade_table(cls):
        return upgrade_table

    async def start(self) -> None:
        self.config.load_and_update()
        self._published: dict[str, dict] = {}       # room_id -> {mode, alias}
        self._alias_to_room: dict[str, str] = {}    # alias (no #) -> room_id
        self._sse_queues: dict[str, set[asyncio.Queue]] = {}
        self._display_names: dict[str, str] = {}
        self._backfilling: set[str] = set()
        self._media_cache: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
        self._media_cache_max: int = 100
        self._tile_cache: OrderedDict[str, bytes] = OrderedDict()
        self._tile_cache_max: int = 256
        self._room_create_cache: dict[str, tuple[int, set[str]]] = {}
        await self._load_published_rooms()

    async def stop(self) -> None:
        for queues in self._sse_queues.values():
            for q in queues:
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass
        self._sse_queues.clear()

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    async def _load_published_rooms(self) -> None:
        rows = await self.database.fetch(
            "SELECT room_id, mode, alias FROM published_rooms"
        )
        for row in rows:
            rid, alias = row["room_id"], row["alias"]
            self._published[rid] = {"mode": row["mode"], "alias": alias}
            self._alias_to_room[alias] = rid

    async def _set_published(self, room_id: str, mode: str, alias: str) -> None:
        await self.database.execute(
            "INSERT INTO published_rooms (room_id, mode, alias) VALUES ($1, $2, $3) "
            "ON CONFLICT (room_id) DO UPDATE SET mode = $2, alias = $3",
            room_id, mode, alias,
        )
        self._published[room_id] = {"mode": mode, "alias": alias}
        self._alias_to_room[alias] = room_id

    async def _remove_published(self, room_id: str) -> None:
        info = self._published.pop(room_id, None)
        if info:
            self._alias_to_room.pop(info["alias"], None)
        await self.database.execute(
            "DELETE FROM published_rooms WHERE room_id = $1", room_id
        )

    async def _store_message(
        self, room_id: str, event_id: str, sender: str, sender_name: str,
        body: str, formatted_body: str | None, msgtype: str,
        media_url: str | None, timestamp: int, thread_root: str | None,
        reply_to: str | None = None, geo_uri: str | None = None,
    ) -> None:
        await self.database.execute(
            "INSERT INTO messages "
            "(event_id, room_id, sender, sender_name, body, formatted_body, "
            "msgtype, media_url, timestamp, thread_root, reply_to, geo_uri, edited, redacted) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,FALSE,FALSE) "
            "ON CONFLICT (event_id) DO NOTHING",
            event_id, room_id, sender, sender_name, body, formatted_body,
            msgtype, media_url, timestamp, thread_root, reply_to, geo_uri,
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
        self, room_id: str, limit: int = 500
    ) -> list[dict]:
        rows = await self.database.fetch(
            "SELECT * FROM messages WHERE room_id=$1 AND redacted=FALSE "
            "ORDER BY timestamp DESC LIMIT $2",
            room_id, limit,
        )
        return [dict(r) for r in reversed(rows)]

    async def _get_posts(
        self, room_id: str, page: int, per_page: int,
    ) -> tuple[list[dict], int]:
        total = await self.database.fetchval(
            "SELECT COUNT(*) FROM messages "
            "WHERE room_id=$1 AND thread_root IS NULL AND redacted=FALSE",
            room_id,
        ) or 0
        total_pages = max(1, (total + per_page - 1) // per_page)
        offset = (page - 1) * per_page
        rows = await self.database.fetch(
            "SELECT * FROM messages "
            "WHERE room_id=$1 AND thread_root IS NULL AND redacted=FALSE "
            "ORDER BY timestamp DESC LIMIT $2 OFFSET $3",
            room_id, per_page, offset,
        )
        return [dict(r) for r in rows], total_pages

    async def _get_comment_counts(self, event_ids: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for eid in event_ids:
            val = await self.database.fetchval(
                "SELECT COUNT(*) FROM messages "
                "WHERE thread_root=$1 AND redacted=FALSE",
                eid,
            )
            counts[eid] = val or 0
        return counts

    async def _get_post(self, event_id: str) -> dict | None:
        row = await self.database.fetchrow(
            "SELECT * FROM messages WHERE event_id=$1 AND redacted=FALSE",
            event_id,
        )
        return dict(row) if row else None

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
        except Exception:
            name = sender
        self._display_names[sender] = name
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

    async def _check_power_level(self, evt: MessageEvent) -> bool:
        """Return True if the sender meets the configured min_power_level."""
        required = self.config["min_power_level"]
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
        url = f"{self._base_url}/{quote(info['alias'], safe='')}"
        await evt.reply(f"Published ({info['mode']} mode): {url}")

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

        # require a canonical alias
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

        url = f"{self._base_url}/{quote(alias_str, safe='')}"
        await evt.reply(f"Room published in **{mode}** mode!\n\n{url}")

        # backfill in background
        asyncio.create_task(self._backfill_room(room_id))

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------

    async def _backfill_room(self, room_id: str) -> None:
        if room_id in self._backfilling:
            return
        self._backfilling.add(room_id)
        max_msgs = self.config["max_backfill"]
        total = 0
        end_token: str | None = None

        try:
            while total < max_msgs:
                batch = min(100, max_msgs - total)
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
                    break

                chunk = content.get("chunk", [])
                if not chunk:
                    break

                for raw in chunk:
                    if raw.get("type") != "m.room.message":
                        continue
                    c = raw.get("content", {})
                    relates = c.get("m.relates_to") or {}

                    # skip edits (originals already present)
                    if relates.get("rel_type") == "m.replace":
                        continue
                    # skip bot messages
                    sender = raw.get("sender", "")
                    if sender == self.client.mxid:
                        continue
                    body = c.get("body", "")
                    if body.startswith("!webpublish"):
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
                    backfill_msgtype = c.get("msgtype", "m.text")
                    backfill_geo_uri = None
                    if backfill_msgtype == "m.location":
                        backfill_geo_uri = (
                            (c.get("org.matrix.msc3488.location") or {}).get("uri")
                            or c.get("geo_uri")
                        )
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
                    )
                    total += 1

                end_token = content.get("end")
                if not end_token:
                    break

            self.log.info(f"Backfill complete for {room_id}: {total} messages stored")
        except Exception as e:
            self.log.error(f"Backfill error for {room_id}: {e}")
        finally:
            self._backfilling.discard(room_id)

    # ------------------------------------------------------------------
    # Live event handlers
    # ------------------------------------------------------------------

    @event.on(EventType.ROOM_MESSAGE)
    async def handle_message(self, evt: MessageEvent) -> None:
        if evt.room_id not in self._published:
            return
        if evt.sender == self.client.mxid:
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
            new_content = content.get("m.new_content") or {}
            new_body = new_content.get("body", body)
            new_fmt = new_content.get("formatted_body")
            await self._update_message_edit(target_id, new_body, new_fmt)

            body_html = render_body(
                {"body": new_body, "formatted_body": new_fmt, "redacted": False,
                 "msgtype": new_content.get("msgtype", "m.text")},
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
            author_pl = self.config["journal_author_pl"]
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

        sender_name = await self._get_sender_name(evt.room_id, str(evt.sender))
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
        )

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
        }
        await self._enrich_single_reply(msg_dict)

        base = self._base_url
        if info["mode"] == "chat":
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
            else:
                encoded = quote(info["alias"], safe="")
                html = render_post_preview_html(msg_dict, encoded, 0)
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
        await self._mark_redacted(redacted_id)
        self._notify_sse(evt.room_id, "redact_message", {
            "element_id": safe_element_id(redacted_id),
        })

    # ------------------------------------------------------------------
    # Web handlers
    # ------------------------------------------------------------------

    async def _fetch_media(self, server_name: str, media_id: str) -> tuple[str, bytes] | None:
        """Fetch media from the homeserver using bot credentials, with LRU caching."""
        cache_key = f"{server_name}/{media_id}"
        if cache_key in self._media_cache:
            self._media_cache.move_to_end(cache_key)
            return self._media_cache[cache_key]

        hs = self._homeserver_url()
        url = f"{hs}/_matrix/media/v3/download/{cache_key}"
        try:
            async with self.client.api.session.get(
                url, headers={"Authorization": f"Bearer {self.client.api.token}"}
            ) as resp:
                if resp.status != 200:
                    return None
                content_type = resp.content_type or "application/octet-stream"
                body = await resp.read()
        except Exception as e:
            self.log.warning(f"Media proxy fetch failed for {cache_key}: {e}")
            return None

        if len(self._media_cache) >= self._media_cache_max:
            self._media_cache.popitem(last=False)
        self._media_cache[cache_key] = (content_type, body)
        return self._media_cache[cache_key]

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
    async def web_media_proxy(self, req: Request) -> Response:
        server_name = req.match_info["server_name"]
        media_id = req.match_info["media_id"]
        result = await self._fetch_media(server_name, media_id)
        if result is None:
            return Response(status=404, text="Media not found")
        content_type, body = result
        return Response(
            body=body,
            content_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @web.get("/theme/{name}.css")
    async def web_theme_css(self, req: Request) -> Response:
        from .templates import THEMES
        name = req.match_info["name"]
        css = THEMES.get(name)
        if css is None:
            return Response(status=404, text="Theme not found")
        return Response(text=css, content_type="text/css",
                        headers={"Cache-Control": "public, max-age=86400"})

    @web.get("/{alias}")
    async def web_main(self, req: Request) -> Response:
        alias = unquote(req.match_info["alias"])
        room_id = self._alias_to_room.get(alias)
        if not room_id:
            return Response(status=404, text="Room not found")

        info = self._published[room_id]
        room_name = await self._get_room_name(room_id) or alias
        room_topic = await self._get_room_topic(room_id)
        hs = self._homeserver_url()
        css = self.config["css"]
        encoded = quote(alias, safe="")

        if info["mode"] == "chat":
            messages = await self._get_messages(room_id, limit=200)
            await self._enrich_reply_context(messages)
            html = render_chat_page(
                room_name, room_topic, messages, encoded, css, hs, self._base_url,
            )
        else:
            page = int(req.query.get("page", "1"))
            per_page = self.config["pagination"]
            posts, total_pages = await self._get_posts(room_id, page, per_page)
            eids = [p["event_id"] for p in posts]
            counts = await self._get_comment_counts(eids)
            html = render_journal_landing(
                room_name, room_topic, posts, encoded,
                page, total_pages, css, counts,
            )

        return Response(text=html, content_type="text/html")

    @web.get("/{alias}/post/{event_id}")
    async def web_post_detail(self, req: Request) -> Response:
        alias = unquote(req.match_info["alias"])
        event_id = unquote(req.match_info["event_id"])
        room_id = self._alias_to_room.get(alias)
        if not room_id:
            return Response(status=404, text="Room not found")

        post = await self._get_post(event_id)
        if not post or post["room_id"] != room_id:
            return Response(status=404, text="Post not found")

        comments = await self._get_thread_comments(event_id)
        await self._enrich_reply_context(comments)
        room_name = await self._get_room_name(room_id) or alias
        hs = self._homeserver_url()
        css = self.config["css"]
        encoded = quote(alias, safe="")

        html = render_journal_post(
            room_name, post, comments, encoded, css, hs, self._base_url,
        )
        return Response(text=html, content_type="text/html")

    @web.get("/{alias}/sse")
    async def web_sse(self, req: Request) -> StreamResponse:
        alias = unquote(req.match_info["alias"])
        room_id = self._alias_to_room.get(alias)
        if not room_id:
            return Response(status=404, text="Room not found")

        resp = StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        await resp.prepare(req)

        # replay missed events on reconnect
        hs = self._homeserver_url()
        last_id = req.headers.get("Last-Event-ID")
        if last_id:
            missed = await self._get_messages_after_event(room_id, last_id)
            for msg in missed:
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
