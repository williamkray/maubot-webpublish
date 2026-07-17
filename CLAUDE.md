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

- **chat** — Top-level messages render as a scrollable chat log. The initial page renders only the most-recent window (`_get_messages(..., limit=200)` in `_handle_main`); **older history loads on demand** when the browser scrolls near the top of `#messages`, fetching a batch (`_CHAT_OLDER_BATCH`) from `/{alias}/older?before=<timestamp_ms>` (`_handle_older_fragment` → `_get_messages_before`) and prepending it. The cursor is the oldest rendered message's `data-ts` attribute; the client (`_chat_load_older_script`) dedupes by element id and preserves scroll position. Threaded replies are hidden from the main list; each root message that has replies shows a `.webpublish-thread-indicator` (count + up to 5 participant avatars) which opens a right-side `<aside id="thread-panel">` panel. The panel is lazy-loaded from `/{alias}/thread/{event_id}`, deep-linkable via `?thread=<event_id>`, and covers the full viewport on screens <600px.
- **journal** — Top-level messages are blog posts; threaded replies are comments on a post. The `thread_root` column in `messages` links comments to their post.

### Live updates (SSE)

`/{alias}/sse` keeps a long-lived HTTP connection open per browser tab. Each room has a `set[asyncio.Queue]` in `self._sse_queues`. When a new message arrives, `_notify_sse()` puts an item in every queue for that room. The SSE handler streams it to the browser as a JSON payload containing pre-rendered HTML.

**Event types emitted:**
- `new_message` — top-level messages in both modes; journal thread replies (old behavior — client filters by `d.thread_root`).
- `edit_message` — body edits; payload has `element_id` and `body_html`.
- `redact_message` — message deleted; payload has `element_id`.
- `thread_reply` *(chat mode only)* — new threaded reply. Payload: `thread_root`, `root_element_id`, `count`, `indicator_html`, `reply_html`, `reply_element_id`. Client replaces the indicator DOM on the root and, if the thread panel is open for that root, appends the reply.
- `thread_reply_removed` *(chat mode only)* — a threaded reply was redacted. Same payload shape as `thread_reply` but with `removed_element_id` instead of `reply_*`; `indicator_html` is empty when `count == 0`.
- `reaction_added` — a non-control reaction was added to a tracked message. Payload: `event_id`, `element_id`, `reactions_html` (full re-rendered `<div class="webpublish-reactions">…</div>` for the target; empty string = no reactions). The 📰 publish-signal in journal mode does not emit this event.
- `reaction_removed` — a reaction was redacted. Same payload shape as `reaction_added`; `reactions_html` is empty when the last reaction is gone.
- `post_unpublished` *(journal mode only)* — a top-level post transitioned to draft because the last privileged 📰 reaction was redacted. Payload: `element_id`, `event_id`. Landing page removes the card; post-detail page reloads (server 404s).
- `pinned_changed` — the room's `m.room.pinned_events` state changed. Payload depends on mode: chat → `{"banner_html": <str>}` (full `<div class="webpublish-pinned-banner">…</div>` or empty string); journal → `{"html": <str>}` (full `<section class="webpublish-pinned-posts">…</section>` or empty string). Empty payload means remove the element from the DOM.
- `succession_changed` — the room's `m.room.tombstone` state changed (forward-link to replacement room or archive marker). Payload: `{"banner_html": <str>}` (full `<div class="webpublish-succession-banner">…</div>` or empty string). Empty string means remove the banner from the DOM. `m.room.create`-derived predecessor back-links do not emit this event (they're fixed for a room's lifetime and rendered server-side from startup state). A tombstone with a non-empty `replacement_room` also kicks `_auto_migrate_to_replacement` in the background — see below.

### Room-succession auto-migration

When a published room's `m.room.tombstone` sets `replacement_room`, `_auto_migrate_to_replacement` tries to: (a) `POST /join/{room}` on the replacement, (b) verify access by reading `m.room.create` on it, (c) take over the old room's human alias for the new room (preferring the new room's own `m.room.canonical_alias` if present), (d) archive the old room by rewriting its `published_rooms.alias` to the room_id itself via `_archive_publication`. The old site then lives at `/{urlencoded_room_id}/`; the human alias routes to the new room.

Migration is retried from three triggers, with `self._migrating: set[str]` preventing concurrent duplicates for the same old room:

1. **Live tombstone** — `handle_tombstone_state` kicks `_launch_migration`.
2. **Invite to the bot** — `handle_member_state` fires on `m.room.member` invites to the bot's mxid. It reads the inviting room's `m.room.create.predecessor` and, if that predecessor is a published room, launches migration. Covers the race where tombstone arrives before the invite is processed.
3. **Plugin startup** — for any published room whose saved tombstone points to an unpublished replacement, `_launch_migration` runs once after `_load_published_rooms`.

If all three paths fail (bot not invited, room private, join rejected), the admin can run `!webpublish chat|journal` in the new room to publish manually — tombstoned rooms are typically read-only so a migrate command there is moot. `_launch_migration` wraps `asyncio.create_task` with a done-callback that logs any exception so failures are visible in the plugin log. If the new room's `m.room.create` lacks a `predecessor` pointer, one is synthesized in memory so the "View Archive" link still appears on the new room's pages.

**matrix: URI form for external targets** — When a predecessor or replacement room isn't in `_published`, the banner falls back to a matrix URI. Clients like Element resolve `matrix:r/<alias>` reliably but struggle with `matrix:roomid/<id>` unless a `via` hint is present. `_render_succession_banner_for_room` therefore passes the *current* room's human alias as a hint for the replacement target on a tombstoned-but-not-yet-migrated room — during a Matrix room upgrade the alias server typically reassigns the alias to the new room, so `matrix:r/<old_alias>` correctly opens the replacement. See `matrix_room_uri(room_id, alias)` in `templates.py`.

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
| `_pinned_events` | `room_id → ordered list[event_id]` — latest `m.room.pinned_events` contents; populated on start/publish and refreshed by the state handler |
| `_room_succession` | `room_id → {has_tombstone, replacement_room, tombstone_ts, predecessor_room}` — drives the footer banner. Populated on start/publish; tombstone side is refreshed live by `handle_tombstone_state` |
| `_backfill_tasks` | `room_id → asyncio.Task` — tracked handles for the async backfill crawl; cancelled in `stop()`. Existence of an entry does not imply the task is mid-request (the finer-grained `_backfilling` set does that) |

### Key maubot/mautrix patterns

- `@web.get("/path/{var}")` registers an aiohttp route under the plugin's webapp prefix.
- `@command.new("webpublish")` + `@<cmd>.subcommand("chat")` builds the command tree.
- `@event.on(EventType.ROOM_MESSAGE)` receives live Matrix events.
- `self.client.api.request(Method.GET, Path.v3.rooms[rid].messages, ...)` makes authenticated Matrix API calls.
- `self.database.fetch/fetchrow/fetchval/execute(sql, *args)` are the async DB methods.
- `self.webapp_url` is the auto-detected public URL of this plugin instance.

#### Fetching Matrix room state events

`self.client.get_state_event(room_id, EventType.X)` only works reliably for event types already proven in the codebase (ROOM_NAME, ROOM_TOPIC, ROOM_MEMBER, ROOM_CANONICAL_ALIAS, ROOM_ENCRYPTION, ROOM_POWER_LEVELS, ROOM_CREATE). For any other type the EventType attribute may not exist or the deserialized content may not have the expected attributes — exceptions disappear silently.

Raw-API state lookups already proven in the codebase: `m.room.avatar`, `m.room.pinned_events`, `m.room.tombstone`, `m.room.create`, and the plugin's own `org.jobmachine.webpublish.config`.

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

`css`, `pagination` (int), `max_backfill` (int — `<= 0` means unlimited resumable crawl), `backfill_batch_size` (int, clamped to `[1, 1000]`, default 50), `min_power_level` (int), `base_url` (str, empty = auto-detect), `journal_author_pl` (int), `journal_emoji_publish` (bool), `journal_enforce_messages` (bool), `chat_author_pl` (int), `chat_enforce_messages` (bool), `head_html` (str), `body_html` (str).

`head_html` / `body_html` are operator-supplied raw HTML injected **verbatim, unescaped** into every published HTML page — `head_html` early in `<head>` (via `_page_head`), `body_html` immediately before `</body>`. Intended for analytics/RUM snippets (Datadog RUM example is in `base-config.yaml`), extra meta tags, or deferred widgets. Both are in `OVERRIDABLE_CONFIG` (per-room override via the `org.jobmachine.webpublish.config` state event, `str` parser). Injection covers the five full HTML pages (chat, journal landing, journal post, tag index, tag filter) — **not** SSE/older/thread fragments and **not** the Atom feed (`render_atom_feed`).

`chat_enforce_messages` mirrors `journal_enforce_messages`: when true, top-level chat messages from users below `chat_author_pl` are redacted (threaded replies bypass). Enforcement is live-only — backfill does not retroactively redact, matching the journal precedent.

### Route registration order

`@web.*` decorated methods are registered via `dir()` (alphabetical by method name). Dynamic routes like `/{alias}` shadow literal routes registered later alphabetically — put specific routes on methods that sort *before* the catch-all, or use a regex pattern like `/{alias:[^/]+}` to prevent empty-alias matches.

Reserved alias names (checked in `setpath`, set as `RESERVED_ALIASES` in `bot.py`): `media`, `tiles`, `theme`, `tag`, `tags`, `post`, `sse`, `feed.xml`, `thread`, `older`. Any URI that would collide with a literal route segment belongs here.

### Maubot trailing-slash invariant

`handle_plugin_path` in maubot's server checks `request.path.startswith(plugin_prefix + "/")` — the plugin prefix always ends with `/`. A request to `/_matrix/maubot/plugin/bloggo` (no trailing slash) returns 404 at the maubot level, never reaching the plugin. Root alias pages must use trailing-slash URLs (`bloggo/`) to route correctly.

### JS modifying HTML link attributes

`_sse_*_script` functions in `templates.py` emit inline `<script>` blocks that run on page load and may modify DOM elements — including `href` attributes. When debugging link behavior, search `templates.py` for `querySelector` and `href =` before assuming the HTML source is what the browser uses.

### Live/backfill parity — critical invariant

Any logic applied in `handle_message()` (the live event handler) **must be mirrored in `_backfill_room()`**. These two paths are the only ways messages enter the DB; divergence causes re-published rooms to behave differently from live rooms. Before finishing any change to message storage or published-state logic, explicitly check both call sites.

Known parity points to keep in sync:
- `published` flag: computed from `mode`, `thread_root`, and `journal_emoji_publish` config
- `avatar_url`: fetched from `_avatar_urls` cache (populated by `_get_sender_name()`)
- Reaction storage and 📰 publish gate: `handle_reaction()` (live) vs. the *per-batch* `pending_reactions` pass inside the `_backfill_room_inner` loop. Both paths must (a) persist every `m.reaction` via `_store_reaction()` and (b) run the 📰-publish gate for top-level journal posts when `journal_emoji_publish` is true. Pending reactions/edits are flushed at the end of each batch (not once per crawl) so memory stays bounded during an unlimited crawl.

### Resumable backfill

`_backfill_room_inner()` supports two modes, driven by the `max_backfill` config:
- `max_backfill > 0` — bounded. Crawl stops when `total >= max_backfill` (`status='capped'`) or when the homeserver returns no `end_token` (`status='exhausted'`).

**Pagination gotcha — an empty `chunk` is NOT end-of-history.** Synapse can return `chunk: []` *with* a valid `end` token while paging backward across sparse windows (state events, lazy backfill boundaries). The terminal signal is the **absence of `end`** (or a non-advancing token), never an empty chunk — keep paging until `end` disappears. Treating the first empty chunk as `exhausted` silently truncates rooms mid-history. Refs: <https://matrix-org.github.io/synapse/latest/admin_api/rooms.html>.
- `max_backfill <= 0` — unlimited. Crawl continues until `status='exhausted'`, pacing with `_BACKFILL_BATCH_SLEEP` (default 1.0s) between `/messages` requests.

Progress is persisted per batch in the `backfill_progress` table (`room_id`, `end_token`, `status`, `total`, `updated_at`). On plugin start, `_resume_backfills()` re-launches any room whose status is `'running'`. `!webpublish rebuild` deletes the checkpoint + all stored messages before kicking a fresh crawl. Re-fetching an event is idempotent (messages use `INSERT ... ON CONFLICT DO NOTHING`).

First-time backfill of a room with pre-existing live-stored events seeds the pagination token via `/rooms/{id}/context/{oldest_event_id}` so the crawler skips past what we already have. Failure falls back to starting at the live edge.

Background crawl tasks are tracked in `self._backfill_tasks` and cancelled in `stop()`; the `CancelledError` path saves a `'running'` checkpoint so the crawl picks up where it left off on next start.

### Diagnosing "the site is missing older history"

Three independent layers can each cap visible history — check them in order, they are NOT the same thing:

1. **Render window (most common, chat mode).** The page renders only the most-recent `limit` messages; older ones load via scroll-to-top `/{alias}/older`. If JS/pagination is broken the DB can be full yet the page looks capped. Tell: `SELECT COUNT(*) FROM messages WHERE room_id=$1` ≫ visible count; redacting any recent message reveals exactly one more at the top.
2. **Backfill storage.** Only `max_backfill`-bounded *and* the empty-chunk gotcha above limit what's stored. Tell: `backfill_progress.total` ≈ visible count and `status='exhausted'`/`'capped'`. A config change (e.g. raising `max_backfill`) does NOT auto-re-crawl — only publish/`rebuild`/startup-resume(`running`)/migration launch a backfill, and an `'exhausted'` row short-circuits relaunch (`bot.py` ~`_backfill_room_inner` head). Use `!webpublish rebuild` to force a fresh crawl.
3. **Homeserver/history-visibility.** The bot only retrieves what its account+server can read. A clean crawl that exhausts (empty `end_token`) at a low `total` despite known older history points here — but rule out only after confirming the bot was a member since the start and is on a server that holds the history.

### Adding a message field — checklist

When adding a column to the `messages` table:

1. `db.py` — new `upgrade_vN` migration
2. `bot.py` `_store_message()` — add param, column, and `$N` placeholder
3. `bot.py` live handler (`handle_message`) — fetch/compute value, pass to `_store_message()`
4. `bot.py` backfill (`_backfill_room`) — same fetch/compute, same pass to `_store_message()`
5. `templates.py` `render_message_html()` — read from `msg` dict if rendering it
6. If the field affects the SSE live-update payload, update the `msg_dict` built after `_store_message()` in `handle_message()`

Reactions are stored in a separate `reactions` table, not on `messages`. Hydrate them onto message dicts via `_apply_reactions_to_messages(room_id, messages)` *after* `_enrich_reply_context` and *before* calling a render function. That helper also strips 📰 from top-level journal posts when `journal_emoji_publish` is enabled.

### Rendering conventions — check these before writing new UI code

**URL construction for published-room links.** No trailing slashes except the root. Pattern:

```python
url = f"{base_url}/" if alias == "/" else f"{base_url}/{quote(alias, safe='')}"
```

`@web.get("/{alias}")` does not match a trailing slash. `base_url/foo/` 404s; `base_url/foo` works. The root alias (`"/"`) is the one exception — maubot's plugin-prefix check requires the trailing `/`.

**Matrix URI construction.** Never hand-roll. Use `matrix_room_uri(room_id, alias="", homeserver_url="")` and `matrix_event_uri(room_id, event_id, homeserver_url="")` in `templates.py`. They enforce: prefer `matrix:r/<alias>` when an alias is known (widely supported by clients); fall back to `matrix:roomid/<id>?via=<bot_homeserver>`. Derive `via` from **the bot's homeserver hostname** (`urlparse(self._homeserver_url()).hostname`), not from the room id's server part — room v12 ids have no server part and an id-derived `via` wouldn't be a homeserver that actually federates the room anyway. Sigil-to-prefix lookups for body-rendering use `_MXID_SIGIL_TO_MATRIX_PREFIX`.

**Footer/chrome styling baseline.** Match `.webpublish-feed-footer`: `font-size: 0.85rem`, `color: var(--text-muted)`, minimal padding (`6px 0`), no top border, links hover to `var(--accent)`. If a footer needs left/right split, use flex with `justify-content: space-between` + inner `.webpublish-<name>-left` / `-right` wrappers; each group `display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap`.

**Sticky-footer layout.** `body` is already `display: flex; flex-direction: column; min-height: 100dvh`. Any main container that should push its footer to the viewport bottom needs `flex: 1 0 auto; width: 100%` — see `.webpublish-journal` and `.webpublish-post-full`. Chat mode uses `height: 100dvh; overflow: hidden` and a fixed-height flex column instead.

**Scoping a rendered UI element.** Before wiring a helper into `render_chat_page` / `render_journal_landing` / `render_journal_post`, decide per-view whether it applies. Post-detail is the individual article; the landing page is the list. A room-scoped footer (succession banner, pinned section) usually belongs on the list and chat-full-view — **not** the post detail. If unsure, ask the user before threading the parameter through multiple signatures and SSE scripts.

**Adding an SSE-emitted UI element.** When the element can change live, add a `<name>_changed` event to the event-type list above, emit the full re-rendered HTML as `banner_html`/`html` in the payload, and register a handler in *each* SSE script that hosts the element (`_sse_chat_script`, `_sse_journal_landing_script`, `_sse_post_detail_script`). Empty string payload means remove the DOM node. Don't add the handler to a script whose page doesn't render the element.

### Feature request files

`fr-*.md` files in the repo root describe planned features. Reference these when implementing new functionality.

### Clarify before implementing rendering or storage features

When a feature affects how messages are displayed or stored, ask before coding if the intention is unclear:
- Does it apply to historical messages (backfill) or only new ones?
- Does it need to appear in SSE live-update payloads?
- Are there mode-specific rules (chat vs. journal, top-level vs. thread)?
