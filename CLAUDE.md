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
| `_published` | `room_id → {mode, alias}` — avoids DB lookup on every request |
| `_alias_to_room` | `alias → room_id` — used by all web handlers |
| `_display_names` | `sender_mxid → display_name` — avoids repeated API calls |
| `_sse_queues` | `room_id → set[Queue]` — live browser connections |
| `_media_cache` | `"server/id" → (content_type, bytes)` — LRU media cache |

### Key maubot/mautrix patterns

- `@web.get("/path/{var}")` registers an aiohttp route under the plugin's webapp prefix.
- `@command.new("webpublish")` + `@<cmd>.subcommand("chat")` builds the command tree.
- `@event.on(EventType.ROOM_MESSAGE)` receives live Matrix events.
- `self.client.api.request(Method.GET, Path.v3.rooms[rid].messages, ...)` makes authenticated Matrix API calls.
- `self.database.fetch/fetchrow/fetchval/execute(sql, *args)` are the async DB methods.
- `self.webapp_url` is the auto-detected public URL of this plugin instance.

### Config keys (`base-config.yaml`)

`css`, `pagination` (int), `max_backfill` (int), `min_power_level` (int), `base_url` (str, empty = auto-detect).

### Route registration order

`@web.*` decorated methods are registered via `dir()` (alphabetical by method name). Dynamic routes like `/{alias}` shadow literal routes registered later alphabetically — put specific routes on methods that sort *before* the catch-all, or use a regex pattern like `/{alias:[^/]+}` to prevent empty-alias matches.

### Maubot trailing-slash invariant

`handle_plugin_path` in maubot's server checks `request.path.startswith(plugin_prefix + "/")` — the plugin prefix always ends with `/`. A request to `/_matrix/maubot/plugin/bloggo` (no trailing slash) returns 404 at the maubot level, never reaching the plugin. Root alias pages must use trailing-slash URLs (`bloggo/`) to route correctly.

### JS modifying HTML link attributes

`_sse_*_script` functions in `templates.py` emit inline `<script>` blocks that run on page load and may modify DOM elements — including `href` attributes. When debugging link behavior, search `templates.py` for `querySelector` and `href =` before assuming the HTML source is what the browser uses.

### Feature request files

`fr-*.md` files in the repo root describe planned features. Reference these when implementing new functionality.
