# web publishing maubot plugin

this plugin will sit in your matrix room, and publish posts in that room as a website!

# how does it work?

the maubot plugin will host its own web view by running a web handler at a path equal to your room's canonical alias.
this means in order to work the room must have at least one room alias defined.

this bot also will refuse to work in encrypted rooms as this effectively defeats the purpose of the bot and will not be
tested or validated.

for example, if you run this plugin on a standard maubot deployment, you could invite your bot to your room such as
`#my-cool-room:example.com`. then run one of the following commands:

- `!webpublish chat`
- `!webpublish journal`
- `!webpublish disable`
- `!webpublish link`

you must tell the bot to publish the room, it will not automatically begin publishing just because it is invited.
when published, the room will be accessible at maubot's web endpoint correlating to a url-encoded version of the
room alias, so in this case it would be:

http://your.maubot.host:32196/_matrix/maubot/plugin/webpublish-bot/my-cool-room%3Aexample.com

more information about this can be found in the [maubot docs here](https://docs.mau.fi/maubot/dev/handlers/web.html)

`chat` style will generate a web view formatted like a regular chat view, making your chat room viewable to users who do
not have a matrix account! you can post a link to this page alone, or embed it as a live-chat in another website.

`journal` style will generate a web view formatted more like a blog site, the landing page is a list of entries. each
normal message is treated as an entry or blog post, while threaded conversations will be treated like the comments
section on that post. how you control the people in your room is an exercise left to the user, but this is a simple way
to publish a blog right from a matrix room! the landing page list of entries will default to paginating every 10 entries
but you can configure this.

journal rooms also expose several additional pages and features described below.

## journal features

### hashtags

write `#tagname` anywhere in a post body to tag it. tags must start with a letter and contain only letters, numbers,
hyphens, and underscores (Matrix room aliases like `#room:server` are ignored). tags are indexed at publish time and
updated when posts are edited.

two tag pages are available:

- `/{alias}/tags` — lists all tags used in the room with post counts
- `/{alias}/tag/{name}` — shows all posts with a given tag, paginated

tag chips appear on post preview cards and on individual post pages. a `!webpublish rebuild` will re-index all tags
from existing posts.

### atom feed

journal rooms expose an Atom 1.0 feed at:

```
/{alias}/feed.xml
```

this contains the 20 most recent published posts and is compatible with any RSS reader (Feedly, NetNewsWire, etc.).

### social sharing (open graph)

the journal landing page and each post detail page include `<meta property="og:*">` tags so that links shared on
Mastodon, Slack, iMessage, and similar platforms generate rich preview cards. post detail pages include the post title,
a plain-text excerpt, the publish date, the author name, and (when the post contains an image) an `og:image`.

---

`disable` will stop publishing the site if you have already published it.

`link` will return the website url of the room, or simply `this room is not published` if publishing is not
enabled.

# standalone quickstart

you can run this bot without a full maubot deployment using the included docker compose setup.

**1. copy the example config**

```bash
cp example-standalone-config.yaml data/config.yaml
```

**2. edit `data/config.yaml`** and set the following values:

| key | what to set |
|-----|-------------|
| `user.credentials.id` | your bot's full Matrix ID, e.g. `@webpublish:example.com` |
| `user.credentials.homeserver` | your homeserver URL, e.g. `https://example.com` |
| `user.credentials.access_token` | the bot account's access token |
| `server.public_url` | the public URL where the web UI will be reachable by browsers |

the bot account must already exist on your homeserver. you can create one and obtain an access token using [element web](https://app.element.io) or the registration API.

**3. start the bot**

```bash
docker compose up -d
```

the web UI will be available on port 8080. put a reverse proxy in front of it to serve it over HTTPS at the `public_url` you configured.

**4. invite the bot and publish a room**

invite the bot to an unencrypted room that has a room alias set, then run:

```
!webpublish chat
```

or `!webpublish journal` for blog-style output. the bot will post a link to the published page.

---

# configuration

in the plugin config settings you can configure some options:

`css`: this is an open text field where you can configure customized css for your site formatting. import css hosted
elsewhere for a cleaner config, or write out your custom css here. relevant css classes you may want to override are:

- `.webpublish-header` — page header (room name and topic)
- `.webpublish-messages` — chat message list container
- `.webpublish-message` — individual chat message row
- `.webpublish-avatar` — sender avatar circle
- `.webpublish-sender` — sender display name
- `.webpublish-timestamp` — message timestamp
- `.webpublish-body` — message body content
- `.webpublish-edited` — "(edited)" indicator
- `.webpublish-notice` — m.notice messages (dimmed by default)
- `.webpublish-journal` — journal mode main container
- `.webpublish-posts` — journal post list
- `.webpublish-post-preview` — journal post card
- `.webpublish-post-title` — post title link
- `.webpublish-post-meta` — post metadata (author, date, comment count)
- `.webpublish-post-excerpt` — post excerpt text
- `.webpublish-post-full` — full post detail view
- `.webpublish-comments` — comments section on a post
- `.webpublish-pagination` — page navigation controls
- `.webpublish-back-link` — "back to posts" link
- `.webpublish-tags` — tag chip row (on post previews and post detail)
- `.webpublish-tag-chip` — individual tag link pill
- `.webpublish-tag-header` — heading on tag index and tag filter pages
- `.webpublish-tag-list` — tag list on the tag index page
- `.webpublish-figure-full` — full-width image figure in journal post body
- `.webpublish-image-body` — prose content below a journal image post

css custom properties (variables) that control the color scheme:

- `--bg` — page background
- `--bg-secondary` — card/header background
- `--text` — primary text color
- `--text-muted` — secondary/muted text
- `--border` — border color
- `--accent` — link and highlight color

`pagination`: used for journal-style displays, how many entries should be shown per page? defaults to 10.

`max_backfill`: when publishing is enabled, how many messages should the bot retrieve from room history? defaults to
1000.

`min_power_level`: minimum Matrix power level required to run `!webpublish` commands. defaults to 100 (room admin).

`journal_author_pl`: minimum power level to post top-level entries in journal rooms. users below this level are subject
to the `journal_enforce_messages` and `journal_emoji_publish` rules. ignored in chat rooms. defaults to 50.

`journal_emoji_publish`: when `true`, new top-level journal posts are held as unpublished drafts until an author
(meeting `journal_author_pl`) reacts to the post with the 📰 (newspaper) emoji. the post then appears on the site.
useful for multi-author rooms where posts should go through a lightweight approval step before going live.
defaults to `false` (all posts publish immediately).

`journal_enforce_messages`: when `true`, the bot redacts top-level messages from users below `journal_author_pl` and
sends them a notice explaining why. requires the bot to have redact permission in the room. when `false`, non-author
messages are silently ignored and never appear on the site — useful if you want to allow readers to post in the room
without having the bot redact their messages. defaults to `true`.

`base_url`: the full base url of this plugin's web endpoint. leave empty to auto-detect from maubot. only needed if
your maubot instance is behind a reverse proxy with a different public url.

# scaling

for larger deployments (many rooms, many concurrent readers, high-traffic pages) see [SCALING.md](SCALING.md) —
it documents the caching behaviour the plugin ships with, when to add a reverse-proxy cache or CDN in front of maubot,
and the situations in which the more structural changes (pub/sub SSE fanout, retention policies, rendered-HTML caching)
are worth the lift.
