# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Building

Maubot plugins are packaged as `.mbp` files (zip archives). Build with the maubot CLI:

```bash
mbc build
```

Or manually:

```bash
zip -9r org.jobmachine.webpublish-v0.1.0.mbp maubot.yaml base-config.yaml webpublish/
```

Upload the resulting `.mbp` to a maubot instance via its admin UI or API to deploy. There is no test suite; validate changes by deploying to a dev maubot instance.

## Architecture

The plugin has three source files:

- **`webpublish/bot.py`** — The plugin class (`WebPublishBot extends maubot.Plugin`). Contains Matrix event handlers, bot commands, web route handlers, and all in-memory state.
- **`webpublish/templates.py`** — Pure functions that generate HTML using f-strings (no templating engine). No side effects.
- **`webpublish/db.py`** — PostgreSQL schema via mautrix `UpgradeTable`. Add new migrations by registering additional `upgrade_vN` functions.

### Data flow

Messages arrive via `@event.on(EventType.ROOM_MESSAGE)` in `handle_message()`, get stored in the `messages` table, then are rendered on-demand per HTTP request. The bot never pushes HTML proactively to stored records — rendering always happens at request time from the database rows.

### Two display modes

Set per-room via `!webpublish chat|journal`:

- **chat** — All messages rendered as a scrollable chat log.
- **journal** — Top-level messages are blog posts; threaded replies are comments on a post. The `thread_root` column in `messages` links comments to their post.

### Live updates (SSE)

`/{alias}/sse` keeps a long-lived HTTP connection open per browser tab. Each room has a `set[asyncio.Queue]` in `self._sse_queues`. When a new message arrives, `_notify_sse()` puts an item in every queue for that room. The SSE handler streams it to the browser as a JSON payload containing pre-rendered HTML.

### Media proxy

`/media/{server_name}/{media_id}` fetches media from the homeserver using the bot's credentials (`self.client.api.session` + `self.client.api.token`), never exposing the token to clients. Results are cached in `self._media_cache` (an `OrderedDict` acting as an LRU cache, capped at 100 entries). Templates call `mxc_to_http(..., proxy_base_url=self._base_url)` to generate proxy URLs instead of direct homeserver URLs.

**Route ordering matters:** `web_media_proxy` is registered before `web_post_detail` so that `/media/post/<id>` resolves to the media proxy (not a room aliased "media" with post detail).

### In-memory caches (all initialized in `start()`)

| Attribute | Purpose |
|---|---|
| `_published` | `room_id → {mode, alias, default_alias}` — avoids DB lookup on every request |
| `_alias_to_room` | `alias → room_id` — used by all web handlers |
| `_redirect_aliases` | `default_alias → override_alias` — 302 redirect map |
| `_display_names` | `sender_mxid → display_name` — avoids repeated API calls |
| `_avatar_urls` | `sender_mxid → avatar mxc:// URL` — avoids repeated API calls |
| `_room_avatars` | `room_id → room avatar mxc:// URL` — avoids repeated API calls |
| `_sse_queues` | `room_id → set[Queue]` — live browser connections |
| `_media_cache` | `"server/id" → (content_type, bytes)` — LRU media cache |
| `_tile_cache` | `"z/x/y" → bytes` — OSM tile proxy LRU cache |
| `_backfilling` | `set[room_id]` — guards against concurrent backfills |
| `_room_create_cache` | `room_id → (version, set[creator_mxid])` — room v12 creator check |

### Key maubot/mautrix patterns

- `@web.get("/path/{var}")` registers an aiohttp route under the plugin's webapp prefix.
- `@command.new("webpublish")` + `@<cmd>.subcommand("chat")` builds the command tree.
- `@event.on(EventType.ROOM_MESSAGE)` receives live Matrix events.
- `self.client.api.request(Method.GET, Path.v3.rooms[rid].messages, ...)` makes authenticated Matrix API calls.
- `self.database.fetch/fetchrow/fetchval/execute(sql, *args)` are the async DB methods.
- `self.webapp_url` is the auto-detected public URL of this plugin instance.

#### Fetching Matrix room state events

`self.client.get_state_event(room_id, EventType.X)` only works reliably for event types already proven in the codebase (ROOM_NAME, ROOM_TOPIC, ROOM_MEMBER, ROOM_CANONICAL_ALIAS, ROOM_ENCRYPTION, ROOM_POWER_LEVELS, ROOM_CREATE). For any other type the EventType attribute may not exist or the deserialized content may not have the expected attributes — exceptions disappear silently.

**For any new/unfamiliar state event type, use the raw API:**

```python
content = await self.client.api.request(
    Method.GET,
    Path.v3.rooms[room_id].state["m.room.event_type"],
)
value = content.get("field", "") if isinstance(content, dict) else ""
```

Always add `self.log.debug(f"...: {e}")` in the except clause so failures are visible rather than silently returning empty string.

### Config keys (`base-config.yaml`)

`css`, `pagination` (int), `max_backfill` (int), `min_power_level` (int), `base_url` (str, empty = auto-detect), `journal_author_pl` (int), `journal_emoji_publish` (bool), `journal_enforce_messages` (bool).

### Route registration order

`@web.*` decorated methods are registered via `dir()` (alphabetical by method name). Dynamic routes like `/{alias}` shadow literal routes registered later alphabetically — put specific routes on methods that sort *before* the catch-all, or use a regex pattern like `/{alias:[^/]+}` to prevent empty-alias matches.

### Maubot trailing-slash invariant

`handle_plugin_path` in maubot's server checks `request.path.startswith(plugin_prefix + "/")` — the plugin prefix always ends with `/`. A request to `/_matrix/maubot/plugin/bloggo` (no trailing slash) returns 404 at the maubot level, never reaching the plugin. Root alias pages must use trailing-slash URLs (`bloggo/`) to route correctly.

### JS modifying HTML link attributes

`_sse_*_script` functions in `templates.py` emit inline `<script>` blocks that run on page load and may modify DOM elements — including `href` attributes. When debugging link behavior, search `templates.py` for `querySelector` and `href =` before assuming the HTML source is what the browser uses.

### Live/backfill parity — critical invariant

Any logic applied in `handle_message()` (the live event handler) **must be mirrored in `_backfill_room()`**. These two paths are the only ways messages enter the DB; divergence causes re-published rooms to behave differently from live rooms. Before finishing any change to message storage or published-state logic, explicitly check both call sites.

Known parity points to keep in sync:
- `published` flag: computed from `mode`, `thread_root`, and `journal_emoji_publish` config
- `avatar_url`: fetched from `_avatar_urls` cache (populated by `_get_sender_name()`)
- Reaction handling: `handle_reaction()` (live) vs. `pending_reactions` post-loop pass (backfill)

### Adding a message field — checklist

When adding a column to the `messages` table:

1. `db.py` — new `upgrade_vN` migration
2. `bot.py` `_store_message()` — add param, column, and `$N` placeholder
3. `bot.py` live handler (`handle_message`) — fetch/compute value, pass to `_store_message()`
4. `bot.py` backfill (`_backfill_room`) — same fetch/compute, same pass to `_store_message()`
5. `templates.py` `render_message_html()` — read from `msg` dict if rendering it
6. If the field affects the SSE live-update payload, update the `msg_dict` built after `_store_message()` in `handle_message()`

### Feature request files

`fr-*.md` files in the repo root describe planned features. Reference these when implementing new functionality.

### Clarify before implementing rendering or storage features

When a feature affects how messages are displayed or stored, ask before coding if the intention is unclear:
- Does it apply to historical messages (backfill) or only new ones?
- Does it need to appear in SSE live-update payloads?
- Are there mode-specific rules (chat vs. journal, top-level vs. thread)?
