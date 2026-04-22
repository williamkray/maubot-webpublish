# Scaling maubot-webpublish

This plugin publishes a public website for every room it's been configured to publish. As you add more rooms or attract more visitors, you'll hit different bottlenecks at different points. This document describes the layers of scale, what the code already does, and the infrastructure steps you should take as traffic grows.

## What the plugin already does

Starting at version 0.2.0 the plugin ships these built-in performance behaviours:

- **HTML pages set `Cache-Control: public, max-age=60, stale-while-revalidate=600`.** This means any cache in front (browser, reverse proxy, CDN) can serve the last rendered HTML for up to 60 s of strict freshness plus 10 min of stale-while-revalidate. Live updates still arrive via SSE, so a 60 s cache window does not meaningfully delay visible changes.
- **Feed (`/feed.xml`) sets `max-age=300`** (5 min). Tiles, media, and themes set `max-age=86400` (24 h).
- **Partial index on `messages`** for published journal-post listings (migration `v8`).
- **Single batched `GROUP BY` query** for comment counts on landing/tag pages (no per-post N+1).
- **Bounded concurrent backfill** — at most 2 rooms pulling history from the homeserver simultaneously.
- **Streaming media proxy** — files larger than 2 MB stream from the homeserver to the client without occupying the in-memory LRU cache. Small files (avatars, thumbnails) still go through the 100-entry LRU.
- **Bounded in-memory caches** for media (100 entries), OSM tiles (256 entries), and Matrix display names/avatars (unbounded but populated on-demand from the event stream).

## When to reach for infrastructure

The plugin runs inside a single maubot process. There's a ceiling to what code alone can do. Use this table to decide when to add the next layer:

| Signal | Likely next step |
|---|---|
| Page response times creep above ~200 ms at the edge | **Reverse-proxy cache (Tier 2, step 1)** |
| You're publishing a room to a wider audience (Hacker News, newsletter blast, conference page) | **CDN (Tier 2, step 2)** before the traffic lands |
| Homeserver or DB saturates before the plugin does | **Migrate maubot to PostgreSQL** if it's still on SQLite (Tier 2, step 3) |
| SSE drops events under load, or you need multiple plugin replicas | **Pub/sub SSE fanout (Tier 3, step 1)** |
| A chat-mode room has grown past ~50k messages and landing-page load is slow | **Retention / archival (Tier 3, step 2)** |
| You measure CPU time in the template layer on repeated landing hits | **Rendered HTML cache (Tier 3, step 3)** |

## Tier 2 — infrastructure you can add without touching the plugin

### 1. Put a reverse-proxy cache in front of maubot

`nginx`, `Caddy`, and `Varnish` can all honour the `Cache-Control` headers the plugin now emits. A minimal nginx block:

```nginx
proxy_cache_path /var/cache/nginx/webpublish levels=1:2 keys_zone=webpublish:20m max_size=2g inactive=7d use_temp_path=off;

location /_matrix/maubot/plugin/your-instance-id/ {
    proxy_pass http://127.0.0.1:29316;
    proxy_cache webpublish;
    proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504;
    proxy_cache_background_update on;
    proxy_cache_lock on;
    proxy_cache_revalidate on;
    add_header X-Cache $upstream_cache_status;

    # SSE must bypass cache — long-lived connection, per-visitor stream.
    location ~ /sse$ {
        proxy_pass http://127.0.0.1:29316;
        proxy_cache off;
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

The keys here: `proxy_cache_use_stale` + `proxy_cache_background_update` turn the plugin's `stale-while-revalidate` hint into real behaviour; `/sse` is carved out because SSE needs `proxy_buffering off` and must never be cached.

**Reach for this first.** On a busy single-room site this converts the vast majority of hits into zero-work responses, for zero plugin changes. Works entirely on-host; no external dependencies.

### 2. Put a CDN in front of the reverse proxy (or directly in front of maubot)

When you need to serve a widely-linked room or you want readers outside your homeserver region to have fast pages, add a CDN (Cloudflare, Bunny, Fastly, Netlify Edge, etc.). Because almost every route on the plugin is a cacheable GET, most CDN defaults "just work," with three configuration points:

1. **Honour origin `Cache-Control` headers** for HTML. Don't override the 60 s TTL to something longer unless you're OK with stale pages after a new post — SSE will only update currently-open tabs, not fresh visitors. The 10 min stale-while-revalidate means a CDN hit on a post-dated page still gets served immediately while the CDN re-validates in the background.
2. **Aggressive cache on `/media/*`, `/tiles/*`, `/theme/*`** — the plugin already advertises 24 h. These are the biggest bytes, and once a media ID is minted it never changes content.
3. **Disable caching + buffering on `/sse`**. Most CDNs have a "do not cache" or "bypass cache" rule for specific paths. Same rule: SSE must stream uninterrupted. Cloudflare users need a Page Rule (or Configuration Rule) to disable caching and to disable "Rocket Loader"/"Auto Minify" on the SSE path.

**When to reach for CDN vs. reverse proxy:** reverse proxy first — it's cheaper, on-host, and covers 90% of the benefit. Only add a CDN if you're serving globally, have a known traffic spike coming, or need DDoS absorption the plugin alone can't provide.

### 3. Host maubot on PostgreSQL, not SQLite

maubot can be configured with either SQLite or PostgreSQL at the *maubot* level (this is a maubot setting, not a plugin setting). The plugin's SQL uses asyncpg-style `ON CONFLICT` and `$1, $2` placeholders — this happens to work on modern SQLite too, but SQLite imposes a single-writer lock that will serialize the plugin's event inserts against every other maubot plugin's writes. Under load that surfaces as "the website froze while a message was being stored."

Edit your maubot instance config (usually `config.yaml`) and set:

```yaml
database: postgres://maubot:password@localhost/maubot
```

…and migrate (see maubot's own docs). **Do this before you add a second published room with meaningful traffic.**

## Tier 3 — structural changes (only if Tier 2 isn't enough)

These are bigger lifts. Most installations will never need them. Reach for them when you hit the trigger listed for each.

### 1. Move SSE fanout to Redis (or similar) pub/sub

**Trigger:** you need to run multiple plugin replicas for HA or horizontal scale, or your single process can no longer hold all concurrent SSE connections.

Each tab currently corresponds to an `asyncio.Queue` inside the plugin process. A pub/sub layer (Redis `PUBLISH`/`SUBSCRIBE`, NATS, or just a shared pipe) decouples publishers from subscribers: the event handler publishes once, any number of replicas subscribe and fan out to their own connected browsers. This requires code changes to `_notify_sse` and `_handle_sse`, plus a Redis instance.

Not worth attempting until you've measured that concurrency is actually the bottleneck — one Python process on modest hardware can hold thousands of SSE connections with the current 256-item-per-queue buffer.

### 2. Add message retention / archival

**Trigger:** a published chat-mode room has ≥50 k messages and the `/{alias}` landing page is slow, or you're uncomfortable with unbounded DB growth.

There are two reasonable shapes:

- **Command-driven prune:** add e.g. `!webpublish prune before 2024-01-01` that deletes old rows from `messages` for this room. Simple, explicit, user-controlled.
- **Policy-driven archive:** add an `archived_at` column + a periodic task that moves rows older than a configurable cutoff into an `archived_messages` table (same schema) and filters them out of web rendering. Recoverable, but more code.

Journal-mode rooms generally don't need this — posts are sparse and the partial index keeps listings fast.

### 3. Precompute and cache rendered HTML

**Trigger:** you've added reverse proxy + CDN and your origin still shows CPU time in template functions on landing-page hits (check via profiling, not guessing).

The pure render functions in `templates.py` can be memoised. A small in-memory LRU keyed on `(event_id, edit_ts)` for `render_post_preview_html()` covers the landing page; `render_message_html()` can use the same key for comment rendering. Invalidate on edit/redaction (the `edit_ts` in the key does this implicitly if you include the timestamp of the most recent edit).

Usually unnecessary once Tier 2 is in place — the reverse proxy/CDN makes repeated render work irrelevant for cache hits, and cache misses are infrequent enough that the template work doesn't dominate.

### 4. Bounded, TTL-refreshed display-name cache

**Trigger:** users frequently change display names in published rooms and readers complain about stale names, or the `_display_names` / `_avatar_urls` dicts grow large enough that memory is a concern.

Replace the unbounded dicts with time-boxed entries that re-fetch from the homeserver on read after, say, an hour. Low priority — the existing cache is correct until a user changes their profile, and stale-after-change is usually acceptable on a published archive site.

## Measuring before you change anything

In order of rough cost to implement:

1. **Reverse proxy access logs with `$upstream_cache_status`** tell you what fraction of requests are cache hits vs. origin. If cache-hit ratio is already >90%, don't optimize the origin.
2. **PostgreSQL `pg_stat_statements`** identifies slow queries. After the `v8` partial index, `_get_posts` should be index-only on the partial index; if `EXPLAIN ANALYZE` shows a Seq Scan, your migration didn't apply.
3. **`top`/`htop` of the maubot process** during a traffic spike: if Python CPU is saturated on a single core, you've hit the asyncio single-thread ceiling and Tier 3 step 1 starts to matter. If it isn't, look at the homeserver, DB, or network instead.

## Changelog pointers

For the plugin code that underpins the "What the plugin already does" section, see the Tier 1 changes in the repo history (the commit that introduced `upgrade_v8`, the batched comment-count query, HTML `Cache-Control` headers, the backfill semaphore, and the streaming media proxy).
