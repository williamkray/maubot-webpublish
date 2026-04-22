from mautrix.util.async_db import UpgradeTable, Connection

upgrade_table = UpgradeTable()


@upgrade_table.register(description="Initial schema: published_rooms and messages tables")
async def upgrade_v1(conn: Connection) -> None:
    await conn.execute("""
        CREATE TABLE published_rooms (
            room_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            alias TEXT NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE messages (
            event_id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            sender_name TEXT,
            body TEXT NOT NULL,
            formatted_body TEXT,
            msgtype TEXT NOT NULL DEFAULT 'm.text',
            media_url TEXT,
            timestamp BIGINT NOT NULL,
            thread_root TEXT,
            edited BOOLEAN NOT NULL DEFAULT FALSE,
            redacted BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    await conn.execute(
        "CREATE INDEX idx_messages_room_ts ON messages (room_id, timestamp)"
    )
    await conn.execute(
        "CREATE INDEX idx_messages_thread ON messages (thread_root)"
    )


@upgrade_table.register(description="Add reply_to column for reply back-links")
async def upgrade_v2(conn: Connection) -> None:
    await conn.execute("ALTER TABLE messages ADD COLUMN reply_to TEXT")


@upgrade_table.register(description="Add geo_uri for location messages")
async def upgrade_v3(conn: Connection) -> None:
    await conn.execute("ALTER TABLE messages ADD COLUMN geo_uri TEXT")


@upgrade_table.register(description="Add published flag for emoji-gated journal posts")
async def upgrade_v4(conn: Connection) -> None:
    await conn.execute(
        "ALTER TABLE messages ADD COLUMN published BOOLEAN NOT NULL DEFAULT TRUE"
    )


@upgrade_table.register(description="Add avatar_url for sender profile pictures")
async def upgrade_v5(conn: Connection) -> None:
    await conn.execute("ALTER TABLE messages ADD COLUMN avatar_url TEXT")


@upgrade_table.register(description="Add default_alias to track canonical room alias for redirects")
async def upgrade_v6(conn: Connection) -> None:
    await conn.execute("ALTER TABLE published_rooms ADD COLUMN default_alias TEXT")
    await conn.execute("UPDATE published_rooms SET default_alias = alias")


@upgrade_table.register(description="Add post_tags table for hashtag indexing")
async def upgrade_v7(conn: Connection) -> None:
    await conn.execute("""
        CREATE TABLE post_tags (
            event_id TEXT NOT NULL REFERENCES messages(event_id) ON DELETE CASCADE,
            room_id  TEXT NOT NULL,
            tag      TEXT NOT NULL,
            PRIMARY KEY (event_id, tag)
        )
    """)
    await conn.execute(
        "CREATE INDEX idx_post_tags_room_tag ON post_tags (room_id, tag)"
    )


@upgrade_table.register(description="Partial index to accelerate published journal post listings")
async def upgrade_v8(conn: Connection) -> None:
    # Matches the exact predicate of _get_posts(); turns the landing-page query
    # into an index scan instead of a filtered scan of every row in the room.
    await conn.execute(
        "CREATE INDEX idx_messages_published_posts "
        "ON messages (room_id, timestamp DESC) "
        "WHERE thread_root IS NULL AND redacted = FALSE AND published = TRUE"
    )
